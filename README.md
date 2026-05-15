# Daily Research Briefing → Notion

매일 아침 한국시간 8시에 arXiv에서 PCM, 바로칼로릭, 데이터센터/패키징 열관리
관련 최신 논문을 가져와 OpenAI로 요약하고 **본인 Notion DB에 새 행으로 추가**합니다.

> 📅 본 가이드는 **2026년 5월 13일 Notion Developer Platform 개편 이후의 새 UI** 기준입니다.

---

## 세팅 가이드 (한 번만, 약 30~40분)

총 6단계입니다. 순서대로 따라하시면 됩니다.

---

### 1단계 · 개인 Notion 워크스페이스 확인 또는 생성 (5분)

> 학교/회사 워크스페이스에서는 권한 제약이 있을 수 있어요.
> 개인 워크스페이스를 쓰는 게 가장 안전합니다.

1. Notion 좌측 사이드바 맨 위에 있는 **워크스페이스 이름 클릭**
2. 드롭다운에서 본인 개인 워크스페이스가 있는지 확인
3. **없으면** 메뉴 맨 아래 `+ Create or join workspace` 클릭
   - **"For personal use"** 또는 **"For myself"** 선택
   - 이름은 자유 (예: "Soonwook personal")
4. **개인 워크스페이스로 전환된 상태**로 다음 단계 진행

---

### 2단계 · Notion 데이터베이스 만들기 (10분)

1. 개인 워크스페이스에서 새 페이지를 만듭니다. 이름 자유 (예: "Research Briefing").
2. 페이지 안에서 `/database` 입력 → **"Database - Full page"** 선택.
3. 다음 표대로 **컬럼(Property)**을 만들어주세요.

| 컬럼 이름 (정확히) | 타입 | 설명 |
|---|---|---|
| `Title` | Title | (기본으로 있음) 논문 제목 |
| `arXiv ID` | Text | 중복 방지 키 |
| `Authors` | Text | 저자 |
| `Published` | Date | 출간일 |
| `Relevance` | Number | 관련성 점수 0~100 |
| `Must read` | Checkbox | 관련성 90+ 자동 체크 |
| `Tags` | Multi-select | PCM, Barocaloric 등 |
| `Status` | Select | 옵션 미리 만들기 (아래 참고) |
| `arXiv link` | URL | 원문 링크 |
| `My notes` | Text | 본인 코멘트용 (코드는 안 건드림) |

> ⚠️ **컬럼 이름은 띄어쓰기/대소문자 포함 정확히 일치해야 합니다.**
> 예) `arXiv ID` (X와 i 사이 띄어쓰기 있음), `Must read` (소문자 r).
> 한 글자라도 다르면 "validation_error"가 납니다.

**Status 컬럼 옵션 (미리 만들기):**
- `To read` (기본값)
- `Reading`
- `Done`
- `Skip`

**Tags 컬럼 옵션 (자주 쓰일 것들, 미리 만들면 색깔 일관성 유지):**
- `PCM`, `Barocaloric`, `Data center`, `Packaging`, `Transient`
- `Chiplet`, `Hotspot`, `Review`, `Optimization`, `3D IC`

DB가 완성되면 위쪽에 빈 표가 보입니다. 그대로 두고 다음 단계로.

---

### 3단계 · Notion Connection (=API 토큰) 만들기 (5분)

> ⚠️ Notion은 2026년 5월부터 "Integration"을 **"Connection"**으로 이름 변경.
> 페이지도 새로 분리되었습니다. URL을 정확히 입력하세요.

1. 브라우저에서 **[https://www.notion.so/developers/connections](https://www.notion.so/developers/connections)** 접속.
2. 우측 상단 **`+ New connection`** 클릭.
3. 팝업창에서 다음과 같이 입력:
   - **Connection name**: `Research Briefing Bot` (자유)
   - **Authentication method**: **`Access token`** 선택 (OAuth 아님!)
     - 설명에 "Workspace-scoped static API token... Limited to 1 workspace" 적힌 옵션이 맞아요
   - **Installable in**: 우측 `Select workspace ▼` 클릭 → **개인 워크스페이스 선택**
4. 하단의 **`Create connection`** 클릭.

> 💡 **"Installable in"에 워크스페이스가 안 보이면**, 일단 팝업을 닫고
> 좌측 상단 `Back to Notion`을 눌러 본 앱으로 돌아갑니다. 그 후 사이드바 맨 위
> 워크스페이스 이름을 클릭해 개인 워크스페이스로 전환한 뒤, 이 페이지를
> 새로고침(F5)하고 다시 시도하세요.

**토큰 복사:**

5. Connection 생성 후 그 connection을 클릭해 상세 페이지로 들어가기.
6. **"Internal Integration Secret"** 또는 **"Access token"** 항목의 `Show` 클릭 → 복사.
   - `ntn_`로 시작하는 긴 문자열입니다.
   - 메모장에 임시 저장. 곧 GitHub Secrets에 등록합니다.

---

### 4단계 · DB에 Connection 연결 (가장 중요!) (2분)

> ⚠️ 이 단계가 빠지면 "object_not_found" 에러가 납니다. 가장 흔한 실수.

1. 2단계에서 만든 **DB 페이지로 돌아갑니다**.
2. 페이지 우상단 `…` (점 세 개) 클릭.
3. 메뉴에서 **`Connections`** 클릭.
4. **`+ Add connections`** 또는 검색창에서 방금 만든 봇 이름(`Research Briefing Bot`)을 검색.
5. 봇 선택 → 권한 확인 팝업에서 `Confirm`.

이제 봇이 이 DB에 글을 쓸 수 있습니다.

---

### 5단계 · Database ID 추출 (2분)

1. 2단계 DB 페이지의 브라우저 주소창 URL 확인:
   ```
   https://www.notion.so/내워크스페이스/abcd1234efgh5678ijkl9012mnop3456?v=...
                                       ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                       이 32자리 부분이 Database ID
   ```
2. `?` 앞까지의 **32자리 영숫자** 부분만 복사.
3. 메모장에 임시 저장.

> 💡 URL이 `notion.so/내워크스페이스/페이지제목-abcd1234...?v=` 처럼 보이면,
> 페이지제목 뒤 하이픈(`-`) 다음에 오는 32자리가 DB ID입니다.

---

### 6단계 · GitHub 저장소 + Secrets + 첫 실행 (15분)

#### 6-1. 저장소 만들기

1. [https://github.com](https://github.com) 로그인.
2. `+` → `New repository`. 다음과 같이 설정:
   - **Repository name**: `research-briefing` (자유)
   - **Private** 선택 (이번엔 공개할 필요 없음)
   - README 체크 X
3. `Create repository` 클릭.

#### 6-2. 파일 업로드

1. 저장소 페이지에서 `uploading an existing file` 링크 클릭.
2. 본 폴더의 **파일/폴더 전체**를 드래그:
   - `build_briefing.py`
   - `requirements.txt`
   - `README.md`
   - `.github/workflows/daily.yml` (폴더 구조 그대로!)
3. 페이지 하단 `Commit changes`.

#### 6-3. Secrets 등록 (3개)

1. 저장소 `Settings` 탭 → 왼쪽 `Secrets and variables` → `Actions`.
2. `New repository secret`을 **3번** 클릭해서 다음 차례로 등록:

| Name (정확히) | Value |
|---|---|
| `OPENAI_API_KEY` | 본인 OpenAI 키 (`sk-...`로 시작) |
| `NOTION_TOKEN` | 3단계에서 복사한 토큰 (`ntn_...`로 시작) |
| `NOTION_DATABASE_ID` | 5단계에서 복사한 32자리 DB ID |

#### 6-4. 첫 실행 (수동)

1. 저장소 `Actions` 탭 클릭.
2. 좌측에서 `Daily Notion briefing` 클릭.
3. 우측 `Run workflow` 드롭다운 → 초록색 `Run workflow` 버튼.
4. 1~3분 기다리면 완료.
5. **Notion DB로 가서 새 행들이 추가됐는지 확인** 🎉

각 행을 클릭하면 본문에 토글로 메커니즘 / 핵심 수치 / 한계 / 활용 포인트가
펼쳐집니다. `My notes` 컬럼이나 페이지 본문에 본인 코멘트를 자유롭게 달아도
다음 실행에서 코드는 그 부분을 안 건드립니다.

---

## 운영 팁

**Notion에서 활용하기 좋은 뷰 (View → Add view):**
- **Today's must-read**: Filter `Must read = checked` + Sort `Published` descending
- **Unread queue**: Filter `Status = To read` + Sort `Relevance` descending
- **PCM focus**: Filter `Tags contains PCM`
- **Last 7 days**: Filter `Published is within last week`

**키워드 추가/변경:**
`build_briefing.py` 상단의 `QUERIES` 리스트 수정 → commit → 다음 실행 반영.

**관련성 임계점 조정:**
너무 낮은 점수 논문까지 들어오면 `MIN_RELEVANCE_TO_PUSH`를 65 또는 70으로.

**스케줄 변경:**
`.github/workflows/daily.yml`의 `cron` 라인.
- 현재 `"0 23 * * *"` = UTC 23:00 = KST 08:00
- 평일만: `"0 23 * * 1-5"`
- 오전 7시: `"0 22 * * *"`

---

## 문제 해결

**Actions에서 "object_not_found" 에러**
→ 4단계의 **DB에 Connection 연결**을 안 하셨어요. DB 페이지 `…` → `Connections`에서 봇 추가.

**"validation_error: ... is not a property that exists"**
→ Notion DB 컬럼 이름이 2단계 표와 정확히 다릅니다. 띄어쓰기/대소문자 포함 일치 필요.

**"unauthorized" 또는 "API token is invalid"**
→ `NOTION_TOKEN` 값이 잘못 복사됨. 3단계에서 토큰 다시 복사. 끝에 공백 없는지 확인.

**"Database ID is invalid"**
→ 5단계 DB ID가 잘못됨. URL의 `?v=` 앞쪽 32자리 다시 확인.

**Installable in 드롭다운에 워크스페이스가 안 보임**
→ 1단계 개인 워크스페이스로 전환 후 페이지 새로고침(F5).

**Notion에 행은 추가되는데 본문이 비어있음**
→ OpenAI 호출 실패. Actions 로그에서 `[openai] ERROR` 검색해 원인 확인.

**같은 논문이 두 번 추가됨**
→ `arXiv ID` 컬럼이 정확히 Text 타입인지 확인.

---

## 비용

- GitHub Actions, GitHub repo: **무료** (private이어도 월 2000분 무료, 이 작업은 회당 ~1분)
- Notion API: **무료**
- OpenAI: gpt-4o-mini 기준 하루 8편 요약에 **약 $0.01~0.03** (월 $1 미만)
