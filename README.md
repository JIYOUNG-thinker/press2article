# 📰 보도자료 → 기사 (press2article)

보도자료를 넣으면 지면에 실릴 수준의 **신문 기사 초안**이 나오는 서비스입니다.
Upstage 해커톤 출품작 · Upstage Studio 에이전트 기반

## 왜 만들었나

지역 언론사 기자는 하루에도 수십 건의 보도자료를 받습니다. 보도자료는 홍보 문구가 많고 형식도 제각각(PDF·스캔본·Word)이라, 기사로 옮기는 데 시간이 많이 듭니다.
이 서비스는 보도자료 파일을 넣기만 하면 **역피라미드 구조의 스트레이트 기사 초안**을 만들어, 기자가 취재와 검증에 더 많은 시간을 쓸 수 있게 돕습니다.

## 화면 구성

| 페이지 | 기능 |
|---|---|
| `/` 보도자료 첨부 | PDF·DOCX·PPTX·이미지 파일 첨부(드래그 앤 드롭) 또는 텍스트 붙여넣기, 기사 유형·매체명 선택 |
| `/article/<id>` 기사 보기 | 제목·부제·리드·본문을 신문 지면 스타일로 표시, 키워드·기자 확인 사항, 원문 보기, 기사 복사·인쇄 |

## 동작 방식

```
[보도자료 파일 / 붙여넣은 텍스트]
          │  (텍스트는 DOCX로 자동 변환)
          ▼
  ① Upstage Files API  ─ 파일 업로드 → file_id
          ▼
  ② Upstage Studio Agent (Agents API, v2/responses)
       Parse 노드: 문서 인식(OCR·레이아웃)
       Instruct 노드: 기사 작성
          ▼
  ③ 결과 폴링 → 제목/리드/본문 분리 → 기사 페이지
```

- 에이전트 결과가 JSON(`headline`, `lead`, `body` …)이면 그대로, 마크다운 텍스트면 `# 제목`/본문을 자동으로 나눠 표시합니다.
- 출처 표시(`【†1】`)·마크다운 기호는 화면에서 자동 제거합니다.

## 실행 방법

```bash
pip3 install -r requirements.txt
cp .env.example .env      # .env를 열어 아래 값 입력
PORT=5001 python3 app.py
```

브라우저에서 http://127.0.0.1:5001 접속
(macOS는 5000번 포트를 AirPlay가 쓰고 있어 5001번 사용을 권장합니다.)

### .env 설정

Upstage Studio 워크플로우 화면 → **⋯ → 코드** 패널에서 값을 확인할 수 있습니다.

| 변수 | 설명 |
|---|---|
| `UPSTAGE_API_KEY` | 에이전트가 속한 Studio 조직의 API 키 (`up_…`) |
| `UPSTAGE_AGENT_ID` | 에이전트(워크플로우) ID (`agt_…`) |
| `UPSTAGE_CONFIG_ID` | 설정 버전 번호 (비우면 최신 버전) |
| `UPSTAGE_PROJECT_ID` | 에이전트가 속한 프로젝트 ID (`proj_…`), `x-project-id` 헤더로 전송 |

> `.env`는 `.gitignore`에 포함되어 깃허브에 올라가지 않습니다. API 키를 코드에 직접 쓰지 마세요.

## 지원 형식

PDF, DOCX, PPTX, XLSX, PNG, JPG, BMP, TIFF, HEIC, TXT(자동 변환)
HWP는 에이전트가 직접 읽지 못하므로 한글에서 **PDF로 저장**한 뒤 올려주세요.

## 프로젝트 구조

```
press2article/
├── app.py               # Flask 서버, Agents API 호출, 결과 파싱
├── templates/
│   ├── base.html        # 공통 레이아웃(마스트헤드)
│   ├── index.html       # 보도자료 첨부 페이지
│   └── article.html     # 기사 보기 페이지
├── static/style.css     # 신문 지면 스타일
├── requirements.txt
└── .env.example
```

## 기술 스택

Python · Flask · Upstage Studio Agent (Agents API) · Upstage Files API · OpenAI Python SDK
