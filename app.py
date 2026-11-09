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


def to_article(raw: str) -> dict:
    """모델 출력(JSON 또는 일반 텍스트)을 화면용 기사 dict로 변환."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1])
        if "headline" in data:
            data.setdefault("body", [])
            if isinstance(data["body"], str):
                data["body"] = [p for p in data["body"].split("\n") if p.strip()]
            return data
    except (ValueError, json.JSONDecodeError):
        pass
    # JSON이 아니면 첫 줄을 제목, 나머지를 본문으로
    lines = [l.strip().lstrip("#").strip() for l in raw.splitlines() if l.strip()]
    return {"headline": lines[0] if lines else "생성된 기사", "subheadline": "",
            "lead": "", "body": lines[1:], "keywords": [], "fact_check": []}


def run_agent(file_storage, style: str, outlet: str) -> dict:
    """Upstage Agents API: 파일 업로드 → Studio 에이전트 실행 → 결과 폴링."""
    import time
    client = OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v2")
    uploaded = client.files.create(
        file=(file_storage.filename, file_storage.stream.read()),
        purpose="user_data",
    )
    job = client.responses.create(
        model=UPSTAGE_AGENT_ID,
        include=["last"],
        input=[{
            "role": "user",
            "content": [{"type": "input_file", "file_id": uploaded.id}],
        }],
    )
    for _ in range(90):  # 최대 약 3분 대기
        if job.status not in ("queued", "in_progress"):
            break
        time.sleep(2)
        job = client.responses.retrieve(job.id)
    if job.status != "completed":
        raise RuntimeError(f"에이전트 실행 실패 (status={job.status}, error={job.error})")
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

        if has_file and ext != "txt" and UPSTAGE_AGENT_ID and not pasted:
            # 1순위: Studio에서 만든 에이전트(Agents API)
            article = run_agent(upload, style, outlet)
            press_text = f"(첨부 파일 '{upload.filename}'을 Upstage 에이전트가 직접 처리)"
            engine = f"Upstage Agent ({UPSTAGE_AGENT_ID})"
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
