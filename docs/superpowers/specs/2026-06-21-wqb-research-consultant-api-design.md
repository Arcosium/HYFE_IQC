# WQB Research Consultant API 연동 — 설계 스펙

- **날짜:** 2026-06-21
- **상태:** 설계 승인됨 (브레인스토밍 완료) → 검토 대기
- **작성자:** Claude (사장 지시 기반)

---

## 1. 배경 / 문제

현재 HYFE_IQC는 WorldQuant BRAIN(WQB)과의 **모든** 상호작용을 Playwright 브라우저 자동화로 한다:

- `server/auth.py` — `platform.worldquantbrain.com` 에 **웹 로그인**해 자격증명 검증.
- `server/_wqb_pw_worker.py` (3757줄) — 브라우저 DOM 구동: editor에 식 입력 → `click_simulate` → progress bar 폴링 → self-correlation을 DOM에서 읽기 → 리스트 추가. 일부만 `fetch('https://api.worldquantbrain.com/alphas/{id}', {credentials:'include'})` 로 브라우저 쿠키 기반 API 호출.
- `server/wqb_browser.py` — `simulate_batch(...)` 가 `_wqb_pw_worker.py` 를 subprocess(python3.11 + Playwright)로 띄우는 얇은 런처.

사장이 **Research Consultant(RC)** 자격을 얻어 공식 API 접근권을 갖게 되었고, 기존 브라우저 로그인이 더는 동작하지 않는다. RC 계정은 `https://api.worldquantbrain.com` 에 **HTTP Basic Auth** 기반 `requests.Session` 으로 직접 접근할 수 있다.

추가로, 시스템은 **멀티 사용자**다(사용자별 암호화 자격증명 + 사용자별 브라우저 프로필). 모두가 RC는 아니다.

## 2. 목표 / 비목표

### 목표
1. **계정 유형 이원화:** 가입 시 `Research Consultant` / `일반(standard)` 선택. 일반 → 기존 브라우저 경로, RC → 신규 공식 API 경로.
2. **RC = 전 과정 API:** 로그인 검증 + 시뮬 제출 + 폴링 + IS 지표 수집 + 자기상관 + (필요 시)제출까지 전부 공식 API.
3. **나중에 RC로 전환:** 기존 일반 사용자가 대시보드에서 RC로 승격 가능.
4. **하우스 RC 데이터 공유:** 단일 "하우스" RC 계정(`platinumcasillas@gmail.com`)이 `/data-fields`·`/operators` 를 실시간 조회·캐시 → **모든 사용자**(일반 포함)의 Gemini 생성 팔레트로 주입. 지금의 불완전·잘린 정적 CSV를 대체.
5. **무회귀:** 일반(브라우저) 경로는 바이트 단위로 그대로. RC 기능 추가가 기존 사용자에게 무해.

### 비목표 (Out of scope)
- 대시보드 "데이터셋 탐색" UI 뷰 — **하지 않음** (완전 자동화 목표). 라이브 데이터는 생성 팔레트로만 흐른다.
- 브라우저 경로(`_wqb_pw_worker.py`)의 리팩터/개선 — 이번 변경에서 손대지 않는다(seam 추출 외).
- Gemini 알파 생성 로직·AST·bandit·settings·reward·DB 스키마(아래 추가 컬럼 외) 변경 — 불변.

## 3. 핵심 결정 (브레인스토밍 산출)

| 질문 | 결정 |
|---|---|
| API로 어디까지? | 계정 유형 이원화: RC는 전 과정 API, 일반은 기존 브라우저 |
| RC 실행 범위 | 로그인+시뮬+폴링+수집+자기상관+제출 전부 API |
| 데이터 공급원 | **하우스 RC 계정 1개 고정** (`platinumcasillas@gmail.com`), config로 지정 |
| 공유 데이터 용도 | **Gemini 생성 팔레트 교체만**. 대시보드 탐색 뷰 없음 |
| 구조 | **Strategy 패턴** — `WqbBackend` 인터페이스 + `BrowserBackend`/`ApiBackend` |
| 실행/취소 | RC는 **in-process `requests`**, worker `_stop_event` 협조적 체크(subprocess/killpg 없음) |

## 4. 아키텍처 개요

```
                       worker.py (스레드, 사용자별)
                       ├─ Gemini 생성 · AST · bandit · settings · DB   ← 불변(공유)
                       └─ simulate_batch(...)  ← 단일 seam
                                  │
                 account_type 라우팅 (dispatcher)
                ┌─────────────────┴─────────────────┐
        BrowserBackend                         ApiBackend (신규)
   (기존 wqb_browser.simulate_batch,        requests.Session + HTTPBasicAuth
    _wqb_pw_worker.py subprocess)           in-process, 동일 반환 규약
        standard 사용자                          RC 사용자

  하우스 RC 계정 ── wqb_data_service ──→ data/live_datafields.csv ──→ datafield_palette.build_palette()
   (config 지정)     /data-fields,/operators        (gitignore, 캐시)        모든 사용자 생성 팔레트
                     실시간 조회·TTL 캐시                                      (실패 시 정적 CSV 폴백)
```

**불변식:** worker, 생성, DB, 대시보드는 "어느 백엔드인지" 모른다. 갈라지는 건 `simulate_batch` 한 함수의 구현뿐.

## 5. 컴포넌트 상세

### 5.1 데이터 모델 — DB v3 → v4 (`server/db.py`)

`_SCHEMA_VERSION = 3` → **4**. `PRAGMA user_version < 4` 게이트 안에서:

```sql
ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'standard';
-- 값: 'standard' | 'research_consultant'
```

- 마이그레이션 백필: 기존 사용자 전원 `'standard'` (DEFAULT로 자동).
- 하우스 계정은 **config로 지정**(아래 5.6), DB 플래그 불필요. 시작 시 해당 username 사용자가 존재하면 `account_type='research_consultant'` 로 보정(idempotent UPDATE).
- 헬퍼: `db.get_account_type(user_id) -> str`, `db.set_account_type(user_id, account_type)`, `upsert_user(..., account_type='standard')` 시그니처 확장.

### 5.2 백엔드 인터페이스 — `server/wqb_backend.py` (신규)

기존 seam(`wqb_browser.simulate_batch`)의 시그니처·반환 규약을 그대로 계약으로 고정한다.

```python
class WqbBackend(Protocol):
    def simulate_batch(
        self, batch: list[dict], *,
        wqb_username: str, wqb_password: str,
        log_fn=None, proc_holder: dict | None = None,
        partial_fn=None, forced_delay: int | None = None,
    ) -> list[dict]: ...
```

- **batch 항목:** `{idx, code, desc, settings:{region,universe,delay,neutralization,...}}`
- **반환 항목(알파별):** `{idx, code, desc, pass_count, pass_items, fail_count, fail_items, submitted, submit_status, error_text, metrics, is_status:{pass:[],fail:[],error:[]}, mode}`
- **partial_fn(obj):** 알파 1개 완료 즉시 `{idx, status, error_text, is_status, metrics, submit_status, submitted}` 로 호출(모바일 실시간).

라우팅(확정): `wqb_browser.simulate_batch` 를 **dispatcher** 로 유지한다 — 기존 시그니처에 `account_type` 인자 1개만 추가, 내부에서 `BrowserBackend`/`ApiBackend` 로 위임. worker.py 호출부는 `account_type=...` 한 줄만 추가(나머지 무변경). (worker가 백엔드 객체를 직접 들고 다니는 대안은 호출부 변경이 커져 채택하지 않음.)

- `BrowserBackend` — 기존 `_wqb_pw_worker.py` subprocess 경로를 그대로 감싼 것(현 `wqb_browser.simulate_batch` 본문 이동). 동작 불변.
- `ApiBackend` — 5.3.

### 5.3 ApiBackend — `server/wqb_api.py` (신규)

`requests.Session` + `HTTPBasicAuth(username, password)` 기반, **in-process**.

핵심 메서드(내부):
- `authenticate()` — `POST /authentication`. 성공 시 세션 쿠키/토큰 보관. 만료 시 자동 재인증(1회 재시도).
- `submit_simulation(alpha_expr, settings) -> sim_id` — `POST /simulations`. 바디 예: `{"type":"REGULAR","settings":{"region","universe","delay","neutralization","decay","truncation","pasteurization","unitHandling","nanHandling","language","visualization":false},"regular": alpha_expr}`.
- `poll_simulation(sim_id) -> (status, alpha_id)` — `GET /simulations/{sim_id}` 폴링. `COMPLETE`/`ERROR`/`FAIL` 판정. `_stop_event` 와 deadline(예: 720s) 체크.
- `fetch_checks(alpha_id) -> is_status,metrics` — `GET /alphas/{alpha_id}` 의 `is.checks` 파싱 → 기존 `is_status:{pass/fail/error}` + `metrics` 매핑(브라우저 worker의 정규화 키와 동일하게: sharpe, fitness, turnover, subuniverse_sharpe, self_correlation, weight 등).
- `read_self_correlation(alpha_id) -> float` — `GET /alphas/{alpha_id}/correlations/self` 의 max 값.
- `submit_alpha(alpha_id)` — `POST /alphas/{alpha_id}/submit` (제출 활성 시).

`simulate_batch(...)` 구현: batch를 순회(동시 시뮬 한도 존중, 5.7 참조) → 각 알파 submit→poll→fetch_checks→(corr)→partial_fn → 결과 dict 누적. `_stop_event`(proc_holder가 아닌 worker의 stop_event를 주입) set이면 즉시 중단.

**테스트 가능성:** `Session` 을 주입 가능하게(`__init__(self, username, password, session=None)`) → mock으로 단위테스트.

### 5.4 로그인 검증 — `server/auth.py`

`validate_login(wqb_username, wqb_password, gemini_api_key, account_type)`:
1. Gemini 검증(기존, 두 유형 공통 — 생성 로직 불변이므로 RC도 키 필요).
2. account_type 분기:
   - `'research_consultant'` → `validate_wqb_api(username, password)`: `POST /authentication`. 매핑 — `201/200`=ok; `401`=`wqb_credentials`; `403`=`wqb_not_consultant`(권한 없음 안내); 연결 실패=`wqb_unreachable`.
   - `'standard'` → 기존 `validate_wqb_login`(브라우저).

### 5.5 가입 / 전환 — `server/app.py` + 프론트(`static/`, `index.html`)

- 가입/로그인 엔드포인트: `account_type` 파라미터 수용(라디오: 일반 / Research Consultant). 검증을 5.4로 라우팅. `upsert_user(..., account_type=...)`.
- 신규 엔드포인트 `POST /api/account/upgrade-to-rc` — 현재 로그인 사용자의 저장된 자격증명을 **API로 재검증**(`validate_wqb_api`) 후 성공 시 `account_type='research_consultant'` 로 전환. 실패 시 사유 반환(권한 없음/자격증명).
- 프론트: 가입폼 라디오 + 설정/계정 영역에 "Research Consultant로 전환" 버튼.

### 5.6 하우스 데이터 서비스 — `server/wqb_data_service.py` (신규)

- config: `HOUSE_RC_USERNAME`(기본 `platinumcasillas@gmail.com`). 환경변수 `HYFE_HOUSE_RC_USERNAME` 로 오버라이드 가능.
- 하우스 사용자의 저장된 자격증명(`db.get_user_credentials`)으로 `ApiBackend` 세션 생성.
- `refresh()` — `/data-fields`(region/universe/delay/dataset 페이지네이션) + `/operators` 조회. API JSON → 팔레트가 기대하는 행 dict(`name, category(=dataset/type), coverage, alphas(=alphaCount), region, delay, universe`)로 매핑. 결과를 **`data/live_datafields.csv`**(gitignore) 로 원자적 기록.
- 스케줄: 시작 시 1회 + TTL(예: 6h) 또는 라운드 N회마다 lazy refresh. 백그라운드 스레드/주기 태스크.
- 실패 시: 기존 `data/live_datafields.csv`(있으면) 유지, 없으면 정적 CSV로 폴백. **생성 절대 안 깨짐.**

`datafield_palette.build_palette(...)` 변경: 소스 파일을 `data/live_datafields.csv`(존재 시) → 정적 `IQC_brain_datafields.csv`(폴백) 순으로 선택. 나머지 랭킹/버킷/회전 로직은 불변.

### 5.7 worker 라우팅 — `server/worker.py`

- 시작 시 `account_type = db.get_account_type(user_id)`.
- `simulate_batch(...)` 호출에 `account_type`(또는 선택된 백엔드) 전달. 오케스트레이션 루프(생성·캐시·partial 처리·DB 기록·bandit) **무변경**.
- RC 경로: subprocess `proc_holder` 대신 worker의 `_stop_event` 를 ApiBackend에 주입(협조적 취소). pause 시 ApiBackend가 폴링 루프에서 빠져나옴.
- **동시 시뮬 한도:** RC라도 백엔드 큐 한도 존재(메모리: `CONCURRENT_SIMULATION_LIMIT_EXCEEDED 429`). ApiBackend는 순차 또는 소수 병렬(설정값, 기본 순차)로 제출하고 429 시 backoff·재시도. 포기 시 슬롯 반환(DELETE) — 메모리의 고아 슬롯 교훈 반영.

## 6. 데이터 흐름

1. **RC 시뮬 라운드:** worker가 Gemini로 알파 생성 → `simulate_batch`(ApiBackend) → 각 알파 `POST /simulations` → `GET /simulations/{id}` 폴링 → `GET /alphas/{id}` checks 수집 → corr → partial_fn 스트림 → 결과 dict → worker가 DB 기록(기존 그대로).
2. **일반 시뮬 라운드:** 변경 없음(BrowserBackend = 기존 subprocess).
3. **팔레트 새로고침:** 하우스 서비스가 주기적으로 `/data-fields` → `data/live_datafields.csv`. 모든 사용자 생성 시 `build_palette` 가 이 파일을 우선 사용.

## 7. 에러 처리

- API 인증 실패: `401`→자격증명, `403`→RC권한 아님(전환 거부 안내), 연결 실패→unreachable. 명확한 reason 코드.
- 시뮬: `status:ERROR`(나쁜 알파, `.message` 보유)와 `429 CONCURRENT_LIMIT` 를 ApiBackend가 네이티브 구분(브라우저 worker가 fetch 폴백으로 하던 것을 정식 경로로). 포기 시 DELETE로 슬롯 반환.
- 하우스 데이터 서비스: 어떤 실패도 생성 흐름을 막지 않음 → 정적 CSV 폴백.
- 토큰 만료: ApiBackend가 1회 재인증 후 재시도.

## 8. 테스트

- `tests/test_wqb_backend.py` — account_type 라우팅(standard→Browser, rc→Api).
- `tests/test_wqb_api.py` — mock `requests.Session` 으로 authenticate/submit/poll/fetch_checks/correlation, 401/403/429 매핑, 결과 dict·partial_fn 규약, `_stop_event` 취소.
- `tests/test_db_migration_v4.py` — v3→v4 ALTER + 백필 + 헬퍼.
- `tests/test_auth_account_type.py` — 검증 분기.
- `tests/test_datafield_palette_live.py` — `data/live_datafields.csv` 우선 + 정적 CSV 폴백 + 빈 결과 폴백.
- `tests/test_wqb_data_service.py` — API JSON→행 매핑, 원자적 기록, 실패 폴백.
- 기존 테스트 전부 통과(무회귀). `python3.11 -m pytest`.

## 9. 배포 / 마이그레이션 / 재시작

- DB v4 마이그레이션은 첫 기동 시 1회(게이트). 기존 일반 사용자 무영향.
- 하우스 계정(`platinumcasillas@gmail.com`) 은 마이그레이션 후 RC로 보정 + 자격증명이 저장돼 있어야 데이터 서비스 가동.
- 모듈 추가/변경 → **서버 재시작 필요**(`sudo systemctl restart hyfe-iqc.service` 류; `run.sh`). 런타임 파라미터 아님.
- 커밋은 사장이 명시적으로 요청할 때만(자동 Backup 커밋이 별도 동작).

## 10. 구현 단계에서 검증할 미확정 사항

WQB API의 **정확한 요청/응답 스키마**는 아래 기준으로 plan/구현 단계에서 확정:
- 이미 코드에 있는 `GET /alphas/{id}` 호출(`server/_wqb_pw_worker.py:3023`) — checks 응답 형태 참고.
- 오픈소스 `wqb` / `brain` 파이썬 래퍼 — `POST /simulations` 바디·`Location` 헤더 폴링·`is.checks`·correlation 엔드포인트 관용.
- 라이브 하우스 RC 계정으로 1회 스모크: authenticate → 간단 알파 submit → poll → checks → corr 의 실제 필드명 확인.
- 확정 대상: 시뮬 settings JSON 키(특히 `neutralization` enum, `decay/truncation` 범위), 폴링 진행 표현(`progress`/`status`), correlation 엔드포인트 경로, 제출 체크 바디.

## 11. 영향 받는/추가되는 파일

| 파일 | 변경 |
|---|---|
| `server/db.py` | v4 마이그레이션 + account_type 헬퍼 |
| `server/wqb_backend.py` | **신규** 인터페이스 + 라우팅 |
| `server/wqb_api.py` | **신규** ApiBackend |
| `server/wqb_browser.py` | simulate_batch 본문을 BrowserBackend로(동작 불변), dispatcher화 |
| `server/auth.py` | account_type 분기 + `validate_wqb_api` |
| `server/app.py` | 가입/로그인 account_type, `/api/account/upgrade-to-rc` |
| `static/` · `index.html` | 가입 라디오 + 전환 버튼 |
| `server/wqb_data_service.py` | **신규** 하우스 데이터 서비스 |
| `server/datafield_palette.py` | 라이브 CSV 우선 소스 선택 |
| `server/worker.py` | account_type → 백엔드 라우팅(호출부 최소 변경) |
| `config`/env | `HOUSE_RC_USERNAME` |
| `tests/` | 신규 6종 + 무회귀 |
```
