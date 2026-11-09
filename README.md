# 📰 보도자료 → 기사 (press2article)

보도자료를 넣으면 지면에 실릴 수준의 신문 기사 초안이 나오는 서비스입니다.
Upstage 해커톤 출품작.

| 페이지 | 설명 |
|---|---|
| `/` 보도자료 첨부 페이지 | PDF·HWP·DOCX·이미지 파일 첨부 또는 텍스트 붙여넣기, 기사 유형(스트레이트/기획/단신)·매체명 선택 |
| `/article/<id>` 기사 페이지 | 제목·부제·리드·본문, 키워드, **기자 확인 사항(팩트체크 포인트)**, 원문 보기, 복사·인쇄 |

## 동작 방식

```
보도자료 파일 ─┬─(Agent ID 있음)→ Upstage Agents API (Studio 워크플로우) ──┐
               └─(없음)→ Upstage Document Parse → Upstage Solar Pro 4 ───┴→ 기사 페이지
붙여넣은 텍스트 ────────────────────────→ Upstage Solar Pro 4 ─────────────┘
```

- **Document Parse**: HWP·PDF·스캔 이미지까지 레이아웃을 살려 텍스트로 변환
- **Solar Pro 4**: 편집기자 역할 프롬프트로 기사 작성. 보도자료에 없는 사실·인용은 만들지 않고, 홍보성 표현은 걸러내며, 확인이 필요한 부분은 따로 표시
- **Agents API**(선택): Upstage Studio에서 만든 워크플로우(Parse → Instruct 노드)를 그대로 호출

## 실행 방법

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# .env 파일을 열어 UPSTAGE_API_KEY 입력
python app.py
```

브라우저에서 http://localhost:5000 접속.

### (선택) Upstage Studio 에이전트로 실행하기
1. Upstage Studio에서 AI 워크플로우 생성: **Parse** 노드 → **Instruct** 노드
2. Instruct 노드 지시문에 "보도자료를 신문 기사로 작성하고, headline / subheadline / lead / body(문단 배열) / keywords / fact_check 키를 가진 JSON으로만 출력" 이라고 입력
3. 배포 후 Agent ID를 복사해 `.env`의 `UPSTAGE_AGENT_ID`에 입력

## 기술 스택
Python · Flask · Upstage Document Parse · Upstage Solar Pro 4 · Upstage Agents API
