"""
press2article — 보도자료를 넣으면 기사가 나오는 서비스
- Upstage Document Parse : 첨부한 보도자료 파일(PDF·HWP·DOCX·이미지 등)을 텍스트로 변환
- Upstage Solar (solar-pro4) : 보도자료 텍스트를 신문 기사 형식으로 재작성
"""
import json
import os
import uuid

import requests
from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from openai import OpenAI

load_dotenv()

UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY", "")
SOLAR_MODEL = os.getenv("SOLAR_MODEL", "solar-pro4")
# Upstage Studio에서 만든 에이전트(워크플로우) ID. 있으면 Agents API로 실행
UPSTAGE_AGENT_ID = os.getenv("UPSTAGE_AGENT_ID", "").strip()
UPSTAGE_CONFIG_ID = os.getenv("UPSTAGE_CONFIG_ID", "").strip()
# 에이전트가 속한 Studio 프로젝트 ID (proj_로 시작). 모든 요청에 x-project-id 헤더로 보냄
UPSTAGE_PROJECT_ID = os.getenv("UPSTAGE_PROJECT_ID", "").strip()
PARSE_URL = "https://api.upstage.ai/v1/document-digitization"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

# 생성된 기사를 잠시 보관 (해커톤 데모용 메모리 저장소)
ARTICLES: dict[str, dict] = {}

ALLOWED_EXT = {"pdf", "hwp", "hwpx", "docx", "pptx", "xlsx",
               "png", "jpg", "jpeg", "bmp", "tiff", "heic", "txt"}

STYLES = {
    "straight": "스트레이트 기사(사실 중심, 역피라미드 구조)",
    "feature": "기획·해설 기사(배경과 의미를 풀어 쓰는 구조)",
    "brief": "단신 기사(3~4문장 내외의 짧은 기사)",
}

SYSTEM_PROMPT = """당신은 한국 일간지의 경력 15년 차 편집기자입니다.
사용자가 주는 보도자료를 바탕으로 실제 지면에 실릴 수준의 신문 기사를 작성합니다.

[원칙]
1. 보도자료에 없는 사실·수치·인용은 절대 만들어 내지 않는다.
2. 홍보성 수식어(최고의, 획기적인, 국내 유일 등)는 사실로 확인되지 않으면 빼거나 "~라고 밝혔다"로 출처를 붙인다.
3. 리드(첫 문단)에 육하원칙의 핵심을 담는다.
4. 문장은 '~했다', '~이다' 체의 기사 문체로, 짧고 명확하게 쓴다.
5. 인용문은 보도자료에 있는 것만 사용하고 발언자를 밝힌다.
6. 기자가 추가 확인해야 할 부분(불명확한 수치, 출처 없는 주장 등)은 별도로 정리한다.

[출력 형식] 반드시 아래 JSON만 출력한다. 다른 말은 쓰지 않는다.
{
  "headline": "제목 (25자 내외)",
  "subheadline": "부제목 (40자 내외)",
  "lead": "리드 문단",
  "body": ["본문 문단1", "본문 문단2", "..."],
  "keywords": ["키워드1", "키워드2", "키워드3"],
  "fact_check": ["기자가 확인할 사항1", "..."]
}"""


def parse_document(file_storage) -> str:
    """Upstage Document Parse로 첨부 파일을 텍스트로 변환."""
    files = {"document": (file_storage.filename, file_storage.stream, file_storage.mimetype)}
    data = {"model": "document-parse", "ocr": "auto", "output_formats": "['markdown', 'text']"}
    res = requests.post(
        PARSE_URL,
        headers={"Authorization": f"Bearer {UPSTAGE_API_KEY}"},
        files=files,
        data=data,
        timeout=120,
    )
    res.raise_for_status()
    content = res.json().get("content", {})
    return content.get("markdown") or content.get("text") or content.get("html", "")


def _clean(text: str) -> str:
    """출처 표시(【†1】), 마크다운 굵게(**) 등 화면에 불필요한 기호 제거."""
    import re
    text = re.sub(r"【[^】]*】", "", text)
    text = text.replace("**", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def to_article(raw) -> dict:
    """모델/에이전트 출력(JSON 객체, JSON 문자열, 마크다운 텍스트)을 화면용 기사 dict로 변환."""
    # 1) JSON으로 한 번 이상 감싸져 온 경우 벗겨내기 (예: "\"# 제목\\n\\n본문\"")
    for _ in range(3):
        if not isinstance(raw, str):
            break
        s = raw.strip()
        if s.startswith("```"):
            s = s.strip("`").removeprefix("json").strip()
        try:
            raw = json.loads(s)
        except (ValueError, json.JSONDecodeError):
            raw = s
            break

    # 2) JSON 객체로 온 경우
    if isinstance(raw, dict):
        if "headline" in raw:
            data = dict(raw)
            body = data.get("body", [])
            if isinstance(body, str):
                body = body.split("\n")
            data["body"] = [_clean(p) for p in body if str(p).strip()]
            for k in ("headline", "subheadline", "lead"):
                data[k] = _clean(str(data.get(k, "")))
            data.setdefault("keywords", [])
            data.setdefault("fact_check", [])
            return data
        # headline 키가 없으면 가장 긴 문자열 값을 기사 본문으로 사용
        texts = [v for v in raw.values() if isinstance(v, str)]
        raw = max(texts, key=len) if texts else json.dumps(raw, ensure_ascii=False)

    # 3) 텍스트/마크다운: '# 제목'은 제목, '## 부제'는 부제, 나머지는 본문 문단
    text = str(raw).replace("\\n", "\n")
    headline, sub, body = "", "", []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "---":
            continue
        if line.startswith("# ") and not headline:
            headline = _clean(line[2:])
        elif line.startswith("##") and not sub and not body:
            sub = _clean(line.lstrip("#"))
        else:
            cleaned = _clean(line.lstrip("#").lstrip("-").strip())
            if cleaned:
                body.append(cleaned)
    if not headline and body:
        headline = body.pop(0)
    return {"headline": headline or "생성된 기사", "subheadline": sub,
            "lead": body.pop(0) if len(body) > 1 else "", "body": body,
            "keywords": [], "fact_check": []}


def text_to_docx(text: str) -> bytes:
    """붙여넣은 텍스트를 최소한의 Word(.docx) 파일로 변환 (외부 라이브러리 없이)."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    paras = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>'
        for line in text.splitlines()
    )
    files = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>'),
        "word/document.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body>{paras}</w:body></w:document>'),
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


def run_agent(filename: str, data: bytes) -> dict:
    """Upstage Agents API (Studio '코드' 패널 안내와 같은 방식)
    파일 업로드 → Studio 에이전트 Job 생성 → 상태 폴링 → 결과 파싱."""
    import time
    headers = {"x-project-id": UPSTAGE_PROJECT_ID} if UPSTAGE_PROJECT_ID else None
    client = OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v2",
                    default_headers=headers)
    uploaded = client.files.create(file=(filename, data), purpose="user_data")
    job = client.responses.create(
        model=UPSTAGE_AGENT_ID,
        include=["last"],
        input=[{
            "role": "user",
            "content": [{"type": "input_file", "file_id": uploaded.id}],
        }],
        extra_body={"config_id": UPSTAGE_CONFIG_ID} if UPSTAGE_CONFIG_ID else None,
    )
    for _ in range(150):  # 최대 약 5분 대기
        if job.status not in ("queued", "in_progress"):
            break
        time.sleep(2)
        job = client.responses.retrieve(job.id, include=["last"])
    if job.status != "completed":
        raise RuntimeError(f"에이전트 실행 실패 (status={job.status}, error={job.error})")
    if not job.output_text:
        raise RuntimeError("에이전트가 결과(output_text)를 돌려주지 않았습니다. Studio 워크플로우의 마지막 노드를 확인하세요.")
    return to_article(job.output_text)


def write_article(press_text: str, style: str, outlet: str) -> dict:
    """Upstage Solar로 보도자료 → 기사 생성."""
    client = OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v1")
    user_msg = (
        f"[기사 유형] {STYLES.get(style, STYLES['straight'])}\n"
        f"[매체명] {outlet or '지역 일간지'}\n\n"
        f"[보도자료 원문]\n{press_text[:30000]}"
    )
    resp = client.chat.completions.create(
        model=SOLAR_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        response_format={"type": "json_object"},
    )
    return to_article(resp.choices[0].message.content)


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", styles=STYLES, has_key=bool(UPSTAGE_API_KEY))


@app.route("/generate", methods=["POST"])
def generate():
    if not UPSTAGE_API_KEY:
        return render_template("index.html", styles=STYLES, has_key=False,
                               error="UPSTAGE_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")

    style = request.form.get("style", "straight")
    outlet = request.form.get("outlet", "").strip()
    pasted = request.form.get("press_text", "").strip()
    upload = request.files.get("press_file")

    try:
        press_text = ""
        engine = f"Solar ({SOLAR_MODEL})"
        has_file = bool(upload and upload.filename)
        ext = upload.filename.rsplit(".", 1)[-1].lower() if has_file else ""
        if has_file and ext not in ALLOWED_EXT:
            raise ValueError(f"지원하지 않는 파일 형식입니다: .{ext}")

        if UPSTAGE_AGENT_ID:
            # 1순위: Studio에서 만든 에이전트(Agents API) — 해커톤 키는 이 방식만 사용 가능
            if has_file and ext in ("hwp", "hwpx"):
                raise ValueError("HWP 파일은 에이전트가 읽지 못해요. 한글에서 'PDF로 저장'한 뒤 올려주세요.")
            if has_file and ext == "txt":
                press_text = upload.read().decode("utf-8", errors="ignore")
                fname, data = "press_release.docx", text_to_docx(press_text)
            elif has_file:
                fname, data = upload.filename, upload.read()
                press_text = f"(첨부 파일 '{fname}'을 Upstage 에이전트가 직접 처리)"
            elif pasted:
                # 에이전트는 TXT를 받지 않으므로 붙여넣은 텍스트를 Word 파일로 만들어 전달
                fname, data = "press_release.docx", text_to_docx(pasted)
                press_text = pasted
            else:
                raise ValueError("보도자료 파일을 첨부하거나 내용을 붙여넣어 주세요.")
            article = run_agent(fname, data)
            engine = "Upstage Studio Agent"
        else:
            # 2순위: Document Parse + Solar 직접 호출
            if has_file:
                if ext == "txt":
                    press_text = upload.read().decode("utf-8", errors="ignore")
                else:
                    press_text = parse_document(upload)
                    engine = f"Document Parse + Solar ({SOLAR_MODEL})"
            if pasted:
                press_text = (press_text + "\n\n" + pasted).strip()
            if not press_text:
                raise ValueError("보도자료 파일을 첨부하거나 내용을 붙여넣어 주세요.")
            article = write_article(press_text, style, outlet)
    except Exception as e:  # 데모용: 오류를 화면에 그대로 보여줌
        return render_template("index.html", styles=STYLES, has_key=True, error=str(e))

    article_id = uuid.uuid4().hex[:10]
    ARTICLES[article_id] = {
        "article": article,
        "source": press_text,
        "style": STYLES.get(style),
        "outlet": outlet or "지역 일간지",
        "filename": upload.filename if upload and upload.filename else None,
        "engine": engine,
    }
    return redirect(url_for("article", article_id=article_id))


@app.route("/article/<article_id>")
def article(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    return render_template("article.html", **item)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
