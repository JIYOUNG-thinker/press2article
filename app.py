"""
press2article — 보도자료를 넣으면 기사가 나오는 서비스

흐름
  보도자료(파일/텍스트) → Upstage Files API 업로드
  → Upstage Studio 에이전트 실행 (Parse → Classify → Extract → Instruct)
  → 단계별 결과에서 원문·유형·기사를 꺼내 화면에 표시
"""
import io
import json
import os
import re
import time
import uuid
import zipfile
from xml.sax.saxutils import escape

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, url_for
from openai import OpenAI

load_dotenv()

UPSTAGE_API_KEY = os.getenv("UPSTAGE_API_KEY", "").strip()
UPSTAGE_AGENT_ID = os.getenv("UPSTAGE_AGENT_ID", "").strip()
UPSTAGE_CONFIG_ID = os.getenv("UPSTAGE_CONFIG_ID", "").strip()
UPSTAGE_PROJECT_ID = os.getenv("UPSTAGE_PROJECT_ID", "").strip()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50MB

# 생성된 기사를 잠시 보관 (해커톤 데모용 메모리 저장소 — 서버를 끄면 사라짐)
ARTICLES: dict[str, dict] = {}

ALLOWED_EXT = {"pdf", "docx", "pptx", "xlsx", "png", "jpg", "jpeg", "bmp", "tiff", "heic", "txt"}

# Studio 워크플로우의 '보도자료유형분류' 노드와 같은 목록
PRESS_TYPES = [
    "사업·제도 시행", "성과·실적 홍보", "행사·일정 안내", "통계·조사 발표",
    "협약·MOU", "인사·동정", "해명·설명", "안전·재난 안내", "기타",
]


# ──────────────────────────────────────────────
# 입력 준비
# ──────────────────────────────────────────────
def text_to_docx(text: str) -> bytes:
    """붙여넣은 텍스트를 최소한의 Word(.docx) 파일로 변환 (에이전트는 TXT를 받지 않음)."""
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


# ──────────────────────────────────────────────
# Upstage Studio 에이전트 호출
# ──────────────────────────────────────────────
def _client() -> OpenAI:
    headers = {"x-project-id": UPSTAGE_PROJECT_ID} if UPSTAGE_PROJECT_ID else None
    return OpenAI(api_key=UPSTAGE_API_KEY, base_url="https://api.upstage.ai/v2",
                  default_headers=headers)


def _steps(job) -> list[tuple[str, str]]:
    """Job 결과의 output을 [(단계 이름, 텍스트), ...]로 정리."""
    steps = []
    for item in getattr(job, "output", None) or []:
        name = getattr(item, "model", None) or ""
        texts = []
        for c in getattr(item, "content", None) or []:
            t = getattr(c, "text", None)
            if t:
                texts.append(t)
        if texts:
            steps.append((str(name), "\n".join(texts)))
    return steps


def run_agent(filename: str, data: bytes, press_type: str | None = None):
    """파일 업로드 → 에이전트 Job 실행 → 완료까지 대기. 단계별 결과 목록을 돌려줌.
    press_type을 주면 '이 유형으로 작성하라'는 텍스트를 함께 보냄 (유형 수정 후 다시 만들기)."""
    client = _client()
    uploaded = client.files.create(file=(filename, data), purpose="user_data")

    content = [{"type": "input_file", "file_id": uploaded.id}]
    if press_type:
        content.append({
            "type": "input_text",
            "text": f"이 보도자료의 유형은 '{press_type}'입니다. 이 유형으로 분류해 그 형식에 맞는 기사를 작성하세요.",
        })
    extra = {"config_id": UPSTAGE_CONFIG_ID} if UPSTAGE_CONFIG_ID else None

    include = ["all"]  # 원문(Parse)과 유형(Classify) 결과도 받기 위해 모든 단계 결과 요청
    job = client.responses.create(model=UPSTAGE_AGENT_ID, include=include,
                                  input=[{"role": "user", "content": content}], extra_body=extra)
    for _ in range(150):  # 최대 약 5분 대기
        if job.status not in ("queued", "in_progress"):
            break
        time.sleep(2)
        job = client.responses.retrieve(job.id, include=include)
    if job.status != "completed":
        raise RuntimeError(f"에이전트 실행 실패 (status={job.status}, error={job.error})")

    steps = _steps(job)
    if not steps:
        raise RuntimeError("에이전트가 결과를 돌려주지 않았습니다. Studio 워크플로우를 확인하세요.")
    return steps


def _plain(text: str) -> str:
    """Parse 결과(JSON/HTML/마크다운)를 사람이 읽을 원문 텍스트로."""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            content = data.get("content", data)
            if isinstance(content, dict):
                text = content.get("markdown") or content.get("text") or content.get("html") or text
            elif isinstance(content, str):
                text = content
        elif isinstance(data, str):
            text = data
    except (ValueError, json.JSONDecodeError):
        pass
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|h\d|li|tr|div)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _norm(s: str) -> str:
    return re.sub(r"[\s·/\-_.,]", "", s).lower()


def detect_type(steps: list[tuple[str, str]]) -> str | None:
    """최종 단계를 뺀 단계들에서 보도자료 유형 이름을 찾음 (분류 노드 결과)."""
    keys = {_norm(t): t for t in PRESS_TYPES}
    keys[_norm("others")] = "기타"
    middle = steps[:-1]
    # 이름에 classify/분류가 들어간 단계를 먼저 확인
    middle.sort(key=lambda s: 0 if re.search(r"classif|분류", s[0], re.I) else 1)
    for name, text in middle:
        try:  # JSON 안의 한글이 \uXXXX로 이스케이프된 경우 풀어서 비교
            text = json.dumps(json.loads(text), ensure_ascii=False)
        except (ValueError, json.JSONDecodeError):
            pass
        for hay in (_norm(text[:500]), _norm(name)):
            for k, v in keys.items():
                if k and k in hay:
                    return v
    return None


def find_source(steps: list[tuple[str, str]]) -> str:
    """Parse(문서 인식) 단계의 결과를 원문으로 사용."""
    for name, text in steps:
        if re.search(r"parse|인식", name, re.I):
            return _plain(text)
    return _plain(steps[0][1]) if len(steps) > 1 else ""


# ──────────────────────────────────────────────
# 기사 결과 정리
# ──────────────────────────────────────────────
def _clean(text: str) -> str:
    text = re.sub(r"【[^】]*】", "", str(text))  # 출처 표시 제거
    text = text.replace("**", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def to_article(raw) -> dict:
    """에이전트 최종 출력(JSON 객체, JSON 문자열, 마크다운)을 기사 dict로."""
    for _ in range(3):  # JSON으로 여러 번 감싸져 온 경우 벗겨내기
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

    if isinstance(raw, dict):
        if "headline" in raw or "body" in raw:
            body = raw.get("body", [])
            if isinstance(body, str):
                body = body.split("\n")
            titles = (raw.get("headlines") or raw.get("titles")
                      or raw.get("headline_candidates") or [])
            if isinstance(titles, str):
                titles = [titles]
            return {
                "headline": _clean(raw.get("headline", "")),
                "titles": [_clean(t) for t in titles if str(t).strip()],
                "lead": _clean(raw.get("lead", "")),
                "body": [_clean(p) for p in body if str(p).strip()],
            }
        texts = [v for v in raw.values() if isinstance(v, str)]
        raw = max(texts, key=len) if texts else json.dumps(raw, ensure_ascii=False)

    text = str(raw).replace("\\n", "\n")
    headline, body, titles = "", [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line == "---":
            continue
        m = re.match(r"^(?:제목\s*후보|후보|제목)\s*\d*\s*[:：.)]\s*(.+)$", line)
        if m:
            titles.append(_clean(m.group(1)))
            continue
        if line.startswith("# ") and not headline:
            headline = _clean(line[2:])
        else:
            cleaned = _clean(line.lstrip("#").lstrip("-").strip())
            if cleaned:
                body.append(cleaned)
    if not headline and body:
        headline = body.pop(0)
    return {"headline": headline or "생성된 기사", "titles": titles,
            "lead": body.pop(0) if len(body) > 1 else "", "body": body}


def title_candidates(article: dict) -> list[str]:
    """제목 후보 최대 3개 (에이전트가 후보를 주지 않으면 제목 1개)."""
    out = []
    for t in [article.get("headline", "")] + article.get("titles", []):
        if t and t not in out:
            out.append(t)
    return out[:3]


# ──────────────────────────────────────────────
# 원문 ↔ 기사 비교 (같은 표현 형광펜, 뺀 문장 취소선, 사용률)
# ──────────────────────────────────────────────
NGRAM = 8  # 공백을 뺀 8글자 이상 같으면 '원문과 같은 표현'으로 봄


def _chars(text: str):
    return [(ch, i) for i, ch in enumerate(text) if not ch.isspace()]


def _grams(text: str) -> set[str]:
    cs = "".join(ch for ch, _ in _chars(text))
    return {cs[i:i + NGRAM] for i in range(len(cs) - NGRAM + 1)}


def _marks(text: str, other: set[str]) -> list[bool]:
    """text의 각 글자가 other에도 있는 8글자 덩어리에 속하면 True."""
    cs = _chars(text)
    flags = [False] * len(text)
    seq = "".join(ch for ch, _ in cs)
    for i in range(len(seq) - NGRAM + 1):
        if seq[i:i + NGRAM] in other:
            for k in range(i, i + NGRAM):
                flags[cs[k][1]] = True
    # 형광펜이 단어 사이 공백에서 끊기지 않게 채우기
    for i in range(1, len(text) - 1):
        if text[i].isspace() and flags[i - 1] and flags[i + 1]:
            flags[i] = True
    return flags


def _segments(text: str, flags: list[bool]) -> list[dict]:
    segs, cur, cur_m = [], "", None
    for ch, m in zip(text, flags):
        if cur_m is None or m == cur_m:
            cur += ch
        else:
            segs.append({"t": cur, "m": cur_m})
            cur = ch
        cur_m = m
    if cur:
        segs.append({"t": cur, "m": bool(cur_m)})
    return segs


def compare(source: str, paragraphs: list[str]) -> dict:
    source = source[:20000]
    src_grams, art_grams = _grams(source), _grams("\n".join(paragraphs))

    art = []
    total = matched = 0
    for p in paragraphs:
        flags = _marks(p, src_grams)
        total += sum(1 for ch in p if not ch.isspace())
        matched += sum(1 for ch, f in zip(p, flags) if f and not ch.isspace())
        art.append(_segments(p, flags))

    src = []
    for line in source.splitlines():
        if not line.strip():
            continue
        flags = _marks(line, art_grams)
        n = sum(1 for ch in line if not ch.isspace())
        used = sum(1 for ch, f in zip(line, flags) if f and not ch.isspace())
        removed = n >= 10 and used / n < 0.15  # 기사에 거의 안 쓰인 문장 → 취소선
        src.append({"segs": _segments(line, flags), "removed": removed})

    return {"art": art, "src": src, "ratio": round(matched * 100 / total) if total else 0}


# ──────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────
def _build(item: dict, steps: list[tuple[str, str]], forced_type: str | None = None):
    article = to_article(steps[-1][1])
    if not item.get("source_fixed"):
        item["source"] = find_source(steps) or item.get("source", "")
    item["article"] = article
    item["titles"] = title_candidates(article)
    item["detected"] = detect_type(steps) or "기타"
    item["type"] = forced_type or item["detected"]
    paras = ([article["lead"]] if article["lead"] else []) + article["body"]
    item["cmp"] = compare(item["source"], paras)
    item["has_lead"] = bool(article["lead"])


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", ready=bool(UPSTAGE_API_KEY and UPSTAGE_AGENT_ID))


@app.route("/generate", methods=["POST"])
def generate():
    if not (UPSTAGE_API_KEY and UPSTAGE_AGENT_ID):
        return render_template("index.html", ready=False,
                               error="UPSTAGE_API_KEY와 UPSTAGE_AGENT_ID를 .env에 넣어주세요.")

    outlet = request.form.get("outlet", "").strip()
    pasted = request.form.get("press_text", "").strip()
    upload = request.files.get("press_file")

    try:
        item = {"outlet": outlet or "", "source": "", "source_fixed": False}
        if upload and upload.filename:
            ext = upload.filename.rsplit(".", 1)[-1].lower()
            if ext in ("hwp", "hwpx"):
                raise ValueError("HWP 파일은 에이전트가 읽지 못해요. 한글에서 'PDF로 저장'한 뒤 올려주세요.")
            if ext not in ALLOWED_EXT:
                raise ValueError(f"지원하지 않는 파일 형식입니다: .{ext}")
            data = upload.read()
            if ext == "txt":
                text = data.decode("utf-8", errors="ignore")
                item.update(filename=upload.filename, file=("press_release.docx", text_to_docx(text)),
                            source=text, source_fixed=True)
            else:
                item.update(filename=upload.filename, file=(upload.filename, data))
        elif pasted:
            item.update(filename=None, file=("press_release.docx", text_to_docx(pasted)),
                        source=pasted, source_fixed=True)
        else:
            raise ValueError("보도자료 파일을 첨부하거나 내용을 붙여넣어 주세요.")

        steps = run_agent(*item["file"])
        _build(item, steps)
    except Exception as e:  # 데모용: 오류를 화면에 그대로 보여줌
        return render_template("index.html", ready=True, error=str(e))

    article_id = uuid.uuid4().hex[:10]
    ARTICLES[article_id] = item
    return redirect(url_for("article", article_id=article_id))


@app.route("/article/<article_id>")
def article(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    return render_template("article.html", id=article_id, types=PRESS_TYPES,
                           error=request.args.get("error"), **item)


@app.route("/article/<article_id>/regenerate", methods=["POST"])
def regenerate(article_id):
    item = ARTICLES.get(article_id)
    if not item:
        return redirect(url_for("index"))
    new_type = request.form.get("press_type", "")
    if new_type not in PRESS_TYPES:
        return redirect(url_for("article", article_id=article_id))
    try:
        steps = run_agent(*item["file"], press_type=new_type)
        _build(item, steps, forced_type=new_type)
    except Exception as e:
        return redirect(url_for("article", article_id=article_id,
                                error=f"다시 만들기에 실패했어요: {e}"))
    return redirect(url_for("article", article_id=article_id))


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
