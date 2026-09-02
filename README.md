# ticket_sync

Microsoft Engage Center(Services Hub)의 지원 케이스를 수집해 Excel로 저장하고,
신규 케이스만 Freshdesk에 티켓으로 등록하는 배치 프로그램입니다.

## 1. 목적

- Engage Center [지원] > [케이스 관리] 화면의 케이스 목록을 주기적으로 수집
- Microsoft Case ID 기준으로 중복 없이 로컬 SQLite(`data/tickets.db`)에 누적 저장
- 실행 결과를 `output/serviceshub_ticket_sync_YYYYMMDD.xlsx` 로 저장
- 신규로 확인된 케이스만 Freshdesk에 새 티켓으로 등록 (중복 등록 방지)

## 2. 동작 방식 요약 (중요)

Engage Center는 로그인 이후 화면을 동적으로 그리는 SPA라서 화면 구조(HTML)가 자주 바뀌고,
자동 로그인은 MFA 때문에 완전 자동화가 불가능합니다. 그래서 이 프로그램은:

1. **최초 1회는 브라우저 창에서 사람이 직접 로그인**합니다(MFA 포함). 이후에는 로그인 세션이
   `data/browser_profile` 폴더에 저장되어 자동으로 재사용됩니다. 세션이 만료되면 다시 수동
   로그인을 요청합니다.
2. 케이스 목록은 화면 테이블을 긁지 않고, 화면의 **"CSV로 다운로드"** 버튼을 눌러 파일로
   받은 뒤 파이썬에서 파싱합니다. 화면 구조 변경에 훨씬 덜 취약합니다.
3. 날짜 범위 필터는 화면 UI를 자동화하지 않고, CSV 전체를 받은 뒤 **생성일 기준으로
   파이썬에서 필터링**합니다.
4. CSV에 없는 상세 항목(심각도, 상태 메시지, 인시던트 관리자, 작업 영역, 국가/지역,
   표준 시간대, 지원 요청 소유자, 기본 연락 방법, 범주, 문제, 설명 등)은 날짜 필터를
   통과한 케이스마다 **목록 화면에서 제목을 실제로 클릭**해 상세 페이지로 들어가 읽어온
   뒤, 다시 목록 화면으로 돌아가 다음 케이스를 클릭하는 방식으로 순서대로 수집합니다.
   URL을 추측해서 바로 이동하지 않으므로 화면 라우팅 구조가 바뀌어도 안전합니다. 어떤
   라벨을 어떤 항목으로 읽어올지는 `config/selectors.json` 의 `case_detail.field_labels`
   에서 관리하며, 화면에 항목이 추가되면 코드 수정 없이 이 파일만 수정하면 됩니다.

## 3. 설치 (Python으로 실행하는 경우)

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
```

## 4. 설정 파일 준비

1. `.env.example` 을 복사해 `.env` 로 저장하고 값을 채웁니다. (계정 정보, Freshdesk API Key 등 민감정보)
2. `config.example.json` 을 복사해 `config.json` 으로 저장하고 값을 채웁니다. (도메인, 기본 그룹/상태/우선순위 등 비민감 정보)
3. `config/selectors.json`, `config/column_map.json` 은 실제 Engage Center 화면/CSV 헤더에
   맞춰 조정이 필요할 수 있습니다 (아래 6번 참고).

`config.json` 의 `default_group` 은 Freshdesk 그룹 **이름이 아니라 숫자 Group ID**입니다.
Freshdesk 관리자 화면 > 그룹 설정에서 확인하세요.

## 5. 실행 방법

### 5.1 대화형 실행 (수동 실행, 인자 없이)

```
python main.py
```

다음 순서로 값을 입력받습니다.
1. Freshdesk 연결 대상 선택 (운영 / 테스트 / 직접 입력)
2. 티켓 조회 범위 선택 (전체 / 날짜 범위 / 오늘 생성 티켓)
3. Freshdesk 등록 여부를 Y/N으로 입력 (기본값 N, 즉 미입력 시 Excel만 저장). Excel 저장은 선택과 무관하게 항상 수행됩니다.
4. 조건 확인 후 실행
5. 결과 요약 출력 (수집 건수, 신규 건수, 중복 건수, Freshdesk 성공/실패, Excel/로그 경로)

### 5.2 배치/스케줄러 실행 (인자로 지정, 입력 대기 없음)

```
python main.py --freshdesk prod --mode today --non-interactive
python main.py --freshdesk test --start-date 2026-07-01 --end-date 2026-07-08 --non-interactive
python main.py --freshdesk prod --all --create-freshdesk-ticket true --non-interactive
```

주요 인자:

| 인자 | 설명 |
|---|---|
| `--freshdesk {prod,test,custom}` | Freshdesk 연결 대상 (custom 은 `--domain --api-key --group --priority --status` 필요) |
| `--mode {all,range,today}` / `--all` / `--today` | 조회 범위 |
| `--start-date`, `--end-date` | 날짜 범위 조회 시 (`--mode range` 자동 적용) |
| `--create-freshdesk-ticket {true,false}` | Freshdesk 등록 여부 (기본 false, 미지정 시 Excel만 저장) |
| `--headless {true,false}` | 브라우저 창 표시 여부 (최초 로그인 시에는 false 권장) |
| `--non-interactive` | 입력 프롬프트 생략 (스케줄러용) |
| `--config`, `--env-file` | config.json / .env 경로 직접 지정 |

## 6. .exe 실행 방법 (Python 미설치 PC)

빌드 방법 (개발 PC에서, Python 필요):

```
build_exe.bat
```

`dist\ticket_sync.exe` 가 생성되고, 배포에 필요한 `config\`, `config.example.json`,
`.env.example`, `logs\`, `output\` 이 함께 `dist\` 에 복사됩니다.

**exe만 있는 배포 대상 PC에서:**

1. `dist` 폴더 전체를 복사해서 원하는 위치에 둡니다.
2. `config.example.json` → `config.json`, `.env.example` → `.env` 로 복사 후 값을 채웁니다.
3. Playwright는 브라우저 바이너리(Chromium)가 별도로 필요합니다. 둘 중 하나를 준비하세요.
   - 대상 PC에도 Python이 있다면: `python -m playwright install chromium` 1회 실행
   - Python이 전혀 없는 PC라면: 빌드 PC의 `%LOCALAPPDATA%\ms-playwright` 폴더를 통째로
     `dist\ms-playwright` 로 복사하고, 실행 전 환경변수를 설정
     (`set PLAYWRIGHT_BROWSERS_PATH=%~dp0ms-playwright`)
4. `ticket_sync.exe` 를 더블클릭(대화형) 하거나, cmd에서 인자를 붙여 실행(배치용)합니다.

```
ticket_sync.exe --freshdesk prod --mode today --non-interactive
```

## 7. Windows 작업 스케줄러 등록

1. "작업 스케줄러" 실행 → "기본 작업 만들기"
2. 트리거: 매일, 원하는 시간
3. 동작: "프로그램 시작"
   - 프로그램/스크립트: `C:\경로\ticket_sync.exe`
   - 인수 추가: `--freshdesk prod --mode today --non-interactive`
   - 시작 위치: `ticket_sync.exe` 가 있는 폴더 (config/.env 를 찾기 위해 반드시 지정)
4. "사용자가 로그온했는지 여부에 관계없이 실행" 체크 시, 최초 1회는 반드시 수동 로그인
   (`--headless false` 상태로 한 번 실행)을 미리 마쳐서 세션을 저장해둬야 무인 실행이 됩니다.
   세션이 만료되면 자동 실행이 로그인 대기 상태로 멈추므로, 로그 파일로 주기적 확인을 권장합니다.

Linux Cron 사용 시:

```
0 8 * * * cd /opt/ticket_sync && /usr/bin/python3 main.py --freshdesk prod --mode today --non-interactive
```

## 8. 날짜 범위 / 전체 조회

- 전체 조회: `--all` 또는 대화형에서 "전체 조회" 선택
- 날짜 범위: `--start-date YYYY-MM-DD --end-date YYYY-MM-DD`
- 오늘 생성 티켓만: `--today`

## 9. Excel 결과 파일 확인

`output\serviceshub_ticket_sync_YYYYMMDD.xlsx` 에 아래 4개 시트가 생성됩니다.

- `전체 수집 티켓`: 이번 실행에서 수집된 전체 목록
- `신규 등록 대상`: 로컬 DB에 없던(신규) 티켓만
- `Freshdesk 등록 결과`: 신규 티켓 + 이전 실행 실패분 재시도 결과
- `오류 및 실패 내역`: 수집/저장/Freshdesk 등록 중 발생한 오류

## 10. 로그 확인

`logs\ticket_sync_YYYYMMDD.log` 에 실행 시간, 조회 조건, 수집/신규/중복/실패 건수,
오류 메시지가 기록됩니다. 콘솔에도 동일한 로그가 출력됩니다.

## 11. 오류 발생 시 조치

| 증상 | 원인 / 조치 |
|---|---|
| "로그인 세션이 없거나 만료" 상태로 계속 대기 | 브라우저 창이 떴다면 직접 로그인(MFA 포함)하세요. `--headless false` 로 실행해야 창이 보입니다. 스케줄러 무인 실행 중이면 세션이 만료된 것이므로, 수동으로 한 번 실행해 재로그인하세요. |
| `'CSV로 다운로드' 버튼을 찾지 못했습니다` (PageStructureError) | Engage Center 화면이 바뀐 것입니다. `config/selectors.json` 의 `csv_download_button_text` 등을 실제 화면 문구로 수정하세요. |
| CSV 헤더 매핑 경고 로그 | 실제로 받은 CSV를 열어 헤더명을 확인하고 `config/column_map.json` 을 실제 헤더명에 맞게 수정하세요. |
| Freshdesk 등록 실패가 반복됨 | `.env`의 API Key, `config.json`의 `default_group`(숫자 ID인지) 을 확인하세요. 실패한 건은 DB에 남아 **다음 실행 시 자동 재시도**됩니다. |
| 브라우저 실행 실패 | `python -m playwright install chromium` 을 실행했는지 확인하세요 (exe 배포본은 8번 항목 참고). |
| 그 외 예기치 못한 오류 | 프로그램은 크래시 없이 종료 코드 1과 함께 종료되며, 로그 파일에 상세 스택 트레이스가 남습니다. |

## 12. 프로젝트 구조

```
main.py              배치 실행 진입점 (CLI/대화형)
crawler.py            Engage Center 로그인 및 케이스 수집
freshdesk_client.py   Freshdesk API 연동
storage.py            SQLite 저장 및 중복 판단
excel_exporter.py     Excel 결과 파일 생성
logging_setup.py       로깅 설정
config.py             환경 변수 / config.json 로딩
config/selectors.json  Engage Center 화면 셀렉터 (화면 변경 시 여기만 수정)
config/column_map.json CSV 헤더 -> 내부 필드명 매핑
logs/                 실행 로그
output/               Excel 결과 파일
data/tickets.db        동기화 이력 및 티켓 저장 (SQLite)
data/browser_profile/  로그인 세션 저장 (Playwright persistent context)
build_exe.bat          PyInstaller 빌드 스크립트
```

## 13. 1차 개발 범위에서 제외된 것

- 실시간/양방향 동기화 (Freshdesk 상태를 Engage Center에 반영하지 않음)
- 첨부파일, 전체 댓글/대화 이력 수집
- 클라우드 배포, GUI 화면
