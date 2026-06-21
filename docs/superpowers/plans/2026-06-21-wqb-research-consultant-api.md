# WQB Research Consultant API 연동 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 계정 유형(일반/Research Consultant)을 나눠 RC 사용자는 WQB 공식 API(`requests` Basic Auth)로 전 과정을 돌리고, 단일 하우스 RC 계정이 실시간 data-fields를 모든 사용자의 생성 팔레트에 공급한다.

**Architecture:** Strategy 패턴 — `wqb_browser.simulate_batch` 를 dispatcher로 두고 기존 `BrowserBackend`(무변경)와 신규 `ApiBackend`(in-process `requests`)로 `account_type` 라우팅. WQB API 계약(sim 제출→`Location` 폴링→`/alphas/{id}` checks→correlation)은 `_wqb_pw_worker.py` 에서 검증된 형태를 그대로 미러링. 하우스 RC 세션이 `/data-fields`·`/operators` 를 `data/live_datafields.csv`(gitignore)로 캐시 → `datafield_palette` 가 우선 사용(정적 CSV 폴백).

**Tech Stack:** Python 3.11, `requests`, SQLite(`PRAGMA user_version` 마이그레이션), FastAPI/Flask(`server/app.py`), pytest.

## Global Constraints

- **테스트는 `python3.11 -m pytest`** — 기본 `python` 은 argon2 import 에서 죽는다.
- **무회귀:** `account_type='standard'` 경로(브라우저)는 동작·반환·partial 규약이 바이트 단위로 기존과 동일해야 한다.
- **simulate_batch 계약(불변):** 입력 batch 항목 `{idx, code, desc, settings}`, 반환 항목 `{idx, code, desc, pass_count, pass_items, fail_count, fail_items, submitted, submit_status, error_text, metrics, is_status:{pass:[],fail:[],error:[],pending:[]}, mode}`, `partial_fn({idx,status,error_text,is_status,metrics,submit_status,submitted})`.
- **생성 절대 안 깨짐:** 하우스 데이터 서비스/라이브 CSV의 어떤 실패도 `datafield_palette`/생성 흐름을 막지 않고 정적 CSV로 폴백.
- **WQB API base:** `https://api.worldquantbrain.com`. Auth=`HTTPBasicAuth(email, password)` on a `requests.Session`; 인증은 `POST /authentication`.
- **새 의존성:** `requests` 를 `requirements.txt` 에 추가(없으면).
- **배포:** 모듈 변경이므로 적용엔 서버 재시작 필요. **커밋은 사장이 명시적으로 요청할 때만** — 각 Task의 "Commit" 스텝은 `git add` 로 스테이징까지 하고, 실제 `git commit` 은 사장 승인 또는 자동 Backup 커밋에 맡긴다(스텝에 명시).
- **하우스 계정:** `HOUSE_RC_USERNAME` 기본 `platinumcasillas@gmail.com`, env `HYFE_HOUSE_RC_USERNAME` 오버라이드.
- **팔레트 새로고침 주기:** 기본 TTL 6시간.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `server/db.py` (수정) | v4 마이그레이션 + `account_type` 헬퍼 |
| `tests/test_db_migration_v4.py` (신규) | 마이그레이션·헬퍼 검증 |
| `scripts/wqb_api_smoke.py` (신규) | 하우스 RC 라이브 스모크 — §10 스키마 확정 |
| `server/wqb_api.py` (신규) | `WqbApiClient` — authenticate/submit/poll/cancel/harvest/correlation |
| `tests/test_wqb_api.py` (신규) | mock Session 단위테스트 |
| `server/wqb_backend.py` (신규) | `WqbBackend` 인터페이스 + `ApiBackend` + `BrowserBackend` |
| `tests/test_wqb_backend.py` (신규) | 라우팅·ApiBackend.simulate_batch 계약 |
| `server/wqb_browser.py` (수정) | `simulate_batch` 를 dispatcher 로(본문→BrowserBackend) |
| `server/auth.py` (수정) | `validate_wqb_api` + `validate_login(account_type)` 분기 |
| `tests/test_auth_account_type.py` (신규) | 인증 분기 |
| `server/app.py` (수정) | 가입/로그인 `account_type`, `/api/account/upgrade-to-rc` |
| `static/` · `index.html` (수정) | 가입 라디오 + 전환 버튼 |
| `server/wqb_data_service.py` (신규) | 하우스 데이터 서비스(라이브 CSV 생성) |
| `tests/test_wqb_data_service.py` (신규) | 매핑·원자적 기록·폴백 |
| `server/datafield_palette.py` (수정) | 라이브 CSV 우선 소스 |
| `tests/test_datafield_palette_live.py` (신규) | 라이브 우선·폴백 |
| `server/worker.py` (수정) | `account_type` → 백엔드 라우팅 |
| `server/config.py` 또는 `run_config.py` (수정) | `HOUSE_RC_USERNAME` |
| `requirements.txt` (수정) | `requests` |

---

## Task 1: DB 마이그레이션 v3→v4 (account_type)

**Files:**
- Modify: `server/db.py` (`_SCHEMA_VERSION`, 마이그레이션 게이트, `upsert_user`, 신규 헬퍼)
- Test: `tests/test_db_migration_v4.py`

**Interfaces:**
- Produces: `db.get_account_type(user_id:int)->str`, `db.set_account_type(user_id:int, account_type:str)->None`, `db.upsert_user(wqb_username, wqb_password, gemini_api_key, account_type:str='standard')->int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_migration_v4.py
import importlib, os, sqlite3, tempfile

def _fresh_db(tmp_path, monkeypatch):
    dbfile = str(tmp_path / 'iqc.db')
    monkeypatch.setenv('HYFE_DB_PATH', dbfile)  # db.py가 이 env로 경로 결정(아래 Step3에서 보장)
    import server.db as db
    importlib.reload(db)
    db.init_db()
    return db, dbfile

def test_account_type_column_and_default(tmp_path, monkeypatch):
    db, _ = _fresh_db(tmp_path, monkeypatch)
    uid = db.upsert_user('a@b.com', 'pw', 'gkey')
    assert db.get_account_type(uid) == 'standard'

def test_set_and_get_account_type(tmp_path, monkeypatch):
    db, _ = _fresh_db(tmp_path, monkeypatch)
    uid = db.upsert_user('a@b.com', 'pw', 'gkey', account_type='research_consultant')
    assert db.get_account_type(uid) == 'research_consultant'
    db.set_account_type(uid, 'standard')
    assert db.get_account_type(uid) == 'standard'

def test_migration_backfills_existing_user(tmp_path, monkeypatch):
    # v3 스키마(컬럼 없음)로 user 한 명 만든 뒤 init_db()가 v4로 ALTER+백필하는지.
    db, dbfile = _fresh_db(tmp_path, monkeypatch)
    with sqlite3.connect(dbfile) as c:
        c.execute("ALTER TABLE users DROP COLUMN account_type")  # v3로 되돌림
        c.execute("PRAGMA user_version=3")
    importlib.reload(db); db.init_db()
    with sqlite3.connect(dbfile) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    assert 'account_type' in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_db_migration_v4.py -v`
Expected: FAIL — `get_account_type`/`set_account_type` 미정의, 컬럼 없음. (`HYFE_DB_PATH` 미지원이면 그것부터 Step3에서 처리.)

- [ ] **Step 3: Implement**

`server/db.py`:
1. `_SCHEMA_VERSION = 3` → `_SCHEMA_VERSION = 4`.
2. (이미 `HYFE_DB_PATH` 류 env 미지원이면) DB 경로 결정부에 `os.environ.get('HYFE_DB_PATH')` 우선 추가 — 테스트 격리용. 기존 기본 경로는 유지.
3. 마이그레이션 게이트(`if _ver < _SCHEMA_VERSION:`) 안 마지막에:

```python
# v4: 계정 유형 (standard=브라우저, research_consultant=공식 API)
if _column_missing(conn, 'users', 'account_type'):
    conn.execute(
        "ALTER TABLE users ADD COLUMN account_type TEXT NOT NULL DEFAULT 'standard'")
```

기존에 `_column_missing` 헬퍼가 없으면 추가:

```python
def _column_missing(conn, table: str, col: str) -> bool:
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    return col not in cols
```

4. `upsert_user` 시그니처/INSERT/UPDATE 에 `account_type` 반영:

```python
def upsert_user(wqb_username, wqb_password, gemini_api_key, account_type='standard'):
    ...
    # INSERT 컬럼에 account_type 추가, UPDATE 에도 account_type=? 추가
```

5. 헬퍼 (하우스 컨벤션 `@_with_conn` — conn 이 첫 인자로 주입됨):

```python
@_with_conn
def get_account_type(conn, user_id: int) -> str:
    row = conn.execute('SELECT account_type FROM users WHERE id=?', (user_id,)).fetchone()
    return (row[0] if row and row[0] else 'standard')

@_with_conn
def set_account_type(conn, user_id: int, account_type: str) -> None:
    conn.execute('UPDATE users SET account_type=? WHERE id=?', (account_type, user_id))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_db_migration_v4.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Regression — 기존 DB 테스트**

Run: `python3.11 -m pytest tests/test_db_migration_v2.py tests/test_db_v3_metrics.py -v`
Expected: PASS

- [ ] **Step 6: Stage (commit은 사장 승인/자동 Backup)**

```bash
git add server/db.py tests/test_db_migration_v4.py
# git commit -m "feat(db): v4 account_type column + helpers"   # 사장 승인 시
```

---

## Task 2: 하우스 RC 라이브 스모크 — API 계약 확정 (cred-gated)

> §10 미확정 스키마(시뮬 settings 바디·correlation 엔드포인트·data-fields 페이지네이션)를 라이브로 확정. **하우스 RC 자격증명 필요** — 사장이 실행(또는 승인)한다. 이 Task가 코드 변경의 가정을 검증하고, 차이가 있으면 Task 3·4·8의 mock/parse를 그에 맞춰 조정한다.

**Files:**
- Create: `scripts/wqb_api_smoke.py`

- [ ] **Step 1: 스모크 스크립트 작성**

```python
# scripts/wqb_api_smoke.py
"""하우스 RC 자격증명으로 WQB 공식 API 계약을 라이브 확인.
사용: WQB_EMAIL=... WQB_PASSWORD=... python3.11 scripts/wqb_api_smoke.py
어떤 데이터도 영구 변경하지 않음(시뮬은 읽기성 백테스트, 알파 제출 안 함)."""
import json, os, sys, time
import requests
from requests.auth import HTTPBasicAuth

BASE = 'https://api.worldquantbrain.com'
EMAIL = os.environ['WQB_EMAIL']; PW = os.environ['WQB_PASSWORD']

s = requests.Session(); s.auth = HTTPBasicAuth(EMAIL, PW)
r = s.post(BASE + '/authentication')
print('AUTH', r.status_code, r.headers.get('WWW-Authenticate'))
print('AUTH-BODY', r.text[:400])

# 최소 시뮬 — 보편 PV 식.
body = {"type": "REGULAR",
        "settings": {"instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000",
                     "delay": 1, "decay": 0, "neutralization": "INDUSTRY",
                     "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY",
                     "nanHandling": "OFF", "language": "FASTEXPR", "visualization": False},
        "regular": "rank(close)"}
r = s.post(BASE + '/simulations', json=body)
print('SIM', r.status_code, 'Location=', r.headers.get('Location'))
loc = r.headers.get('Location')
alpha_id = None
for _ in range(120):
    pr = s.get(loc); j = pr.json()
    print('POLL', pr.status_code, j.get('progress'), j.get('status'), j.get('alpha'))
    if j.get('status') in ('COMPLETE', 'ERROR', 'FAIL', 'WARNING'):
        alpha_id = j.get('alpha'); break
    time.sleep(5)
if alpha_id:
    a = s.get(BASE + f'/alphas/{alpha_id}'); print('ALPHA keys=', list(a.json().keys()))
    print('IS=', json.dumps(a.json().get('is'), indent=2)[:1200])
    c = s.get(BASE + f'/alphas/{alpha_id}/correlations/self')
    print('CORR', c.status_code, c.text[:600])

df = s.get(BASE + '/data-fields',
           params={'region': 'USA', 'delay': 1, 'universe': 'TOP3000', 'limit': 3, 'offset': 0})
print('DATAFIELDS', df.status_code, json.dumps(df.json(), indent=2)[:1000])
op = s.get(BASE + '/operators'); print('OPERATORS', op.status_code, str(op.json())[:400])
```

- [ ] **Step 2: 사장이 실행**

사장에게 안내: 프롬프트에 `! WQB_EMAIL=platinumcasillas@gmail.com WQB_PASSWORD=*** python3.11 scripts/wqb_api_smoke.py` 입력. 출력을 이 Task에 붙여 확정.

- [ ] **Step 3: 가정 확정/교정**

확인 항목 체크리스트(차이 있으면 해당 Task 조정):
- [ ] `POST /authentication` 성공 코드(201/200), 추가 챌린지(WWW-Authenticate persona) 유무
- [ ] `POST /simulations` 가 위 `settings` 키를 그대로 수용하는지, `Location` 헤더 형식
- [ ] poll JSON 의 `progress/status/alpha` 키(Task 검증: `_wqb_pw_worker.py:2985` 와 동일 예상)
- [ ] `GET /alphas/{id}` 의 `is.checks[].{name,result,value,limit}` (Task 검증: `:3032` 와 동일 예상)
- [ ] correlation 엔드포인트 경로·응답에서 **max self-correlation** 추출 키
- [ ] `/data-fields` 응답 키: `count`, `results[].{id,description,type,coverage,alphaCount,dataset}`

- [ ] **Step 4: Stage**

```bash
git add scripts/wqb_api_smoke.py
```

---

## Task 3: WqbApiClient — authenticate + harvest

**Files:**
- Create: `server/wqb_api.py`
- Test: `tests/test_wqb_api.py`

**Interfaces:**
- Produces: `WqbApiClient(email:str, password:str, session=None)` with `.authenticate()->bool`, `.harvest_alpha(alpha_id:str)->dict|None` (반환 `{'metrics':{...}, 'is_status':{'pass':[],'fail':[],'error':[],'pending':[]}}`)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wqb_api.py
import server.wqb_api as wqb_api

class FakeResp:
    def __init__(self, status=200, json_data=None, headers=None, text=''):
        self.status_code = status; self._j = json_data or {}
        self.headers = headers or {}; self.text = text
    def json(self): return self._j
    @property
    def ok(self): return 200 <= self.status_code < 300

class FakeSession:
    def __init__(self): self.auth = None; self.calls = []; self.queue = {}
    def post(self, url, **kw): self.calls.append(('POST', url, kw)); return self.queue[('POST', _path(url))].pop(0)
    def get(self, url, **kw): self.calls.append(('GET', url, kw)); return self.queue[('GET', _path(url))].pop(0)
    def delete(self, url, **kw): self.calls.append(('DELETE', url, kw)); return self.queue[('DELETE', _path(url))].pop(0)

def _path(url): return url.replace('https://api.worldquantbrain.com', '').split('?')[0]

def test_authenticate_ok():
    sess = FakeSession()
    sess.queue[('POST', '/authentication')] = [FakeResp(201)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess)
    assert c.authenticate() is True

def test_harvest_alpha_maps_checks():
    sess = FakeSession()
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {
        'is': {'sharpe': 2.1, 'fitness': 1.4, 'turnover': 0.12,
               'checks': [
                   {'name': 'LOW_SHARPE', 'result': 'PASS', 'value': 2.1, 'limit': 1.25},
                   {'name': 'HIGH_TURNOVER', 'result': 'FAIL', 'value': 0.9, 'limit': 0.7},
               ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess)
    h = c.harvest_alpha('AB1')
    assert len(h['is_status']['pass']) == 1 and len(h['is_status']['fail']) == 1
    assert h['metrics']['sharpe'] == '2.1'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_wqb_api.py -v`
Expected: FAIL — `server.wqb_api` 없음

- [ ] **Step 3: Implement (authenticate + harvest)**

```python
# server/wqb_api.py
"""WorldQuant BRAIN 공식 API 클라이언트 (Research Consultant 경로).
계약은 _wqb_pw_worker.py 에서 검증된 형태를 그대로 미러링한다."""
from __future__ import annotations
import logging
import requests
from requests.auth import HTTPBasicAuth

LOG = logging.getLogger('hyfe.wqb_api')
BASE = 'https://api.worldquantbrain.com'

class WqbApiClient:
    def __init__(self, email: str, password: str, session=None):
        self.email = email; self.password = password
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(email, password)
        self._authed = False

    def authenticate(self) -> bool:
        try:
            r = self.session.post(BASE + '/authentication')
        except Exception as e:
            LOG.warning('authenticate network err: %s', e); return False
        self._authed = r.status_code in (200, 201)
        return self._authed

    def _ensure_auth(self) -> bool:
        return self._authed or self.authenticate()

    def harvest_alpha(self, alpha_id: str) -> dict | None:
        """GET /alphas/{id} → {metrics, is_status}. _api_harvest_alpha 미러."""
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}')
            if not r.ok:
                return None
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        isf = data.get('is') or {}
        checks = isf.get('checks') or []
        out = {'pass': [], 'fail': [], 'error': [], 'pending': []}
        for ch in checks:
            res = str(ch.get('result') or '').upper()
            nm = ch.get('name')
            item = {'name': nm, 'value': ch.get('value'), 'cutoff': ch.get('limit'),
                    'result': res,
                    'desc': f"{nm}: {ch.get('result')} (value={ch.get('value')}, limit={ch.get('limit')})"}
            bucket = {'PASS': 'pass', 'FAIL': 'fail', 'PENDING': 'pending', 'ERROR': 'error'}.get(res)
            if bucket:
                out[bucket].append(item)
        metrics = {}
        for k in ('sharpe', 'fitness', 'returns', 'turnover', 'drawdown', 'margin'):
            if isf.get(k) is not None:
                metrics[k] = str(isf[k])
        return {'metrics': metrics, 'is_status': out}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_wqb_api.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Stage**

```bash
git add server/wqb_api.py tests/test_wqb_api.py
```

---

## Task 4: WqbApiClient — submit / poll / cancel / correlation

**Files:**
- Modify: `server/wqb_api.py`
- Test: `tests/test_wqb_api.py` (추가)

**Interfaces:**
- Consumes: Task 3 `WqbApiClient`
- Produces: `.submit_simulation(expr:str, settings:dict)->str|None` (progress URL), `.poll(progress_url, stop_event=None, deadline_s:int=720)->dict` (`{'status','alpha','message','progress'}`), `.cancel(progress_url)->None`, `.read_self_correlation(alpha_id:str)->float|None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wqb_api.py (추가)
def test_submit_returns_location():
    sess = FakeSession()
    sess.queue[('POST', '/simulations')] = [FakeResp(201, headers={'Location': 'https://api.worldquantbrain.com/simulations/SIM1'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess); c._authed = True
    url = c.submit_simulation('rank(close)', {'region': 'USA', 'universe': 'TOP3000', 'delay': 1, 'neutralization': 'INDUSTRY'})
    assert url.endswith('/simulations/SIM1')

def test_poll_until_complete():
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [
        FakeResp(200, {'progress': 0.3, 'status': None, 'alpha': None}),
        FakeResp(200, {'progress': 1.0, 'status': 'COMPLETE', 'alpha': 'AB1'}),
    ]
    c = wqb_api.WqbApiClient('e', 'p', session=sess); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1', deadline_s=30)
    assert res['status'] == 'COMPLETE' and res['alpha'] == 'AB1'

def test_poll_respects_stop_event():
    import threading
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [FakeResp(200, {'progress': 0.1, 'status': None, 'alpha': None})] * 5
    ev = threading.Event(); ev.set()
    c = wqb_api.WqbApiClient('e', 'p', session=sess); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1', stop_event=ev, deadline_s=30)
    assert res['status'] == 'CANCELLED'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_wqb_api.py -k "submit or poll" -v`
Expected: FAIL — 메서드 미정의

- [ ] **Step 3: Implement**

`server/wqb_api.py` 에 추가(클래스 메서드). 폴링 간격은 테스트에서 0이 되도록 인자화.

```python
    def submit_simulation(self, expr: str, settings: dict) -> str | None:
        if not self._ensure_auth():
            return None
        body = {'type': 'REGULAR', 'settings': self._full_settings(settings), 'regular': expr}
        try:
            r = self.session.post(f'{BASE}/simulations', json=body)
        except Exception as e:
            LOG.warning('submit network err: %s', e); return None
        if r.status_code == 429:  # CONCURRENT_SIMULATION_LIMIT_EXCEEDED
            return 'RATE_LIMITED'
        if r.status_code not in (200, 201):
            return None
        return r.headers.get('Location') or r.headers.get('location')

    @staticmethod
    def _full_settings(s: dict) -> dict:
        # UI 기본값 채움. Task 2 스모크로 키/기본값 확정 후 필요시 조정.
        return {
            'instrumentType': 'EQUITY',
            'region': s.get('region', 'USA'),
            'universe': s.get('universe', 'TOP3000'),
            'delay': int(s.get('delay', 1)),
            'decay': int(s.get('decay', 0)),
            'neutralization': s.get('neutralization', 'INDUSTRY'),
            'truncation': float(s.get('truncation', 0.08)),
            'pasteurization': s.get('pasteurization', 'ON'),
            'unitHandling': s.get('unitHandling', 'VERIFY'),
            'nanHandling': s.get('nanHandling', 'OFF'),
            'language': 'FASTEXPR',
            'visualization': False,
        }

    def poll(self, progress_url: str, stop_event=None, deadline_s: int = 720,
             interval_s: float = 5.0, sleep=None) -> dict:
        import time as _t
        sleep = sleep or _t.sleep
        deadline = None  # 단조 시계는 호출부 책임 X — 루프 횟수로 제한(테스트 결정성)
        loops = max(1, int(deadline_s / max(interval_s, 0.001)))
        last = {'status': None, 'alpha': None, 'message': None, 'progress': None}
        for _ in range(loops):
            if stop_event is not None and stop_event.is_set():
                self.cancel(progress_url)
                return {'status': 'CANCELLED', 'alpha': None, 'message': '', 'progress': last.get('progress')}
            try:
                r = self.session.get(progress_url)
                j = r.json() if r.ok else {}
            except Exception as e:
                j = {'message': str(e)}
            last = {'status': j.get('status'), 'alpha': j.get('alpha'),
                    'message': j.get('message'), 'progress': j.get('progress')}
            if last['status'] in ('COMPLETE', 'ERROR', 'FAIL', 'WARNING'):
                return last
            sleep(interval_s)
        # deadline 초과 — 슬롯 반환
        self.cancel(progress_url)
        return {'status': 'TIMEOUT', 'alpha': None, 'message': 'poll deadline', 'progress': last.get('progress')}

    def cancel(self, progress_url: str) -> None:
        if not progress_url:
            return
        try:
            self.session.delete(progress_url)  # COMPLETE면 400 — 무해
        except Exception:
            pass

    def read_self_correlation(self, alpha_id: str) -> float | None:
        # Task 2 스모크로 경로/키 확정. 일반형: records 의 max.
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}/correlations/self')
            if not r.ok:
                return None
            j = r.json()
        except Exception:
            return None
        return _extract_max_correlation(j)
```

모듈 함수:

```python
def _extract_max_correlation(j) -> float | None:
    """correlation 응답에서 max self-correlation 추출. 응답 형태가 여러 가지라 방어적."""
    if not isinstance(j, dict):
        return None
    # 1) {'max': 0.4} 류
    if isinstance(j.get('max'), (int, float)):
        return float(j['max'])
    # 2) {'records': [[...,corr], ...], 'schema': {...}} 류 — 모든 수치의 max
    recs = j.get('records')
    if isinstance(recs, list) and recs:
        vals = []
        for row in recs:
            if isinstance(row, (list, tuple)):
                vals += [x for x in row if isinstance(x, (int, float))]
        if vals:
            return max(vals)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_wqb_api.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Stage**

```bash
git add server/wqb_api.py tests/test_wqb_api.py
```

---

## Task 5: ApiBackend.simulate_batch — seam 계약 구현

**Files:**
- Create: `server/wqb_backend.py`
- Test: `tests/test_wqb_backend.py`

**Interfaces:**
- Consumes: Task 3·4 `WqbApiClient`
- Produces: `ApiBackend(username, password, client=None)` with `.simulate_batch(batch, *, wqb_username, wqb_password, log_fn=None, proc_holder=None, partial_fn=None, forced_delay=None, stop_event=None)->list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wqb_backend.py
import server.wqb_backend as wb

class FakeClient:
    def __init__(self, *a, **k): pass
    def authenticate(self): return True
    def submit_simulation(self, expr, settings): return 'https://api.worldquantbrain.com/simulations/SIM_' + expr[:3]
    def poll(self, url, stop_event=None, **k): return {'status': 'COMPLETE', 'alpha': 'A_' + url[-3:], 'message': '', 'progress': 1.0}
    def harvest_alpha(self, aid):
        return {'metrics': {'sharpe': '2.0'},
                'is_status': {'pass': [{'name': 'x'}] * 7, 'fail': [], 'error': [], 'pending': []}}
    def read_self_correlation(self, aid): return 0.3
    def cancel(self, url): pass

def test_simulate_batch_contract():
    seen = []
    be = wb.ApiBackend('e', 'p', client=FakeClient())
    batch = [{'idx': 1, 'code': 'rank(close)', 'desc': 'd', 'settings': {'region': 'USA', 'delay': 1}}]
    res = be.simulate_batch(batch, wqb_username='e', wqb_password='p',
                            partial_fn=lambda o: seen.append(o), forced_delay=1)
    r0 = res[0]
    assert r0['idx'] == 1 and r0['pass_count'] == 7 and r0['error_text'] == ''
    assert set(r0) >= {'idx','code','desc','pass_count','pass_items','fail_count',
                       'fail_items','submitted','submit_status','error_text','metrics','is_status','mode'}
    assert seen and seen[0]['idx'] == 1 and seen[0]['status'] == 'pass'

def test_simulate_batch_error_status():
    class ErrClient(FakeClient):
        def poll(self, url, stop_event=None, **k): return {'status': 'ERROR', 'alpha': None, 'message': 'bad expr', 'progress': 0.1}
    be = wb.ApiBackend('e', 'p', client=ErrClient())
    res = be.simulate_batch([{'idx': 2, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'error' and 'bad expr' in res[0]['error_text']

def test_simulate_batch_stop_event_aborts():
    import threading
    ev = threading.Event(); ev.set()
    be = wb.ApiBackend('e', 'p', client=FakeClient())
    res = be.simulate_batch([{'idx': 3, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p', stop_event=ev)
    assert res == [] or res[0].get('mode') in ('error', 'cancelled')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_wqb_backend.py -v`
Expected: FAIL — `server.wqb_backend` 없음

- [ ] **Step 3: Implement**

```python
# server/wqb_backend.py
"""WQB 백엔드 Strategy — BrowserBackend(기존)·ApiBackend(신규)가 동일 simulate_batch 계약 구현."""
from __future__ import annotations
import logging
from . import wqb_api

LOG = logging.getLogger('hyfe.wqb_backend')
PASS_FIELDS = ('pass', 'fail', 'error', 'pending')

class ApiBackend:
    def __init__(self, username: str, password: str, client=None):
        self.username = username; self.password = password
        self._client = client or wqb_api.WqbApiClient(username, password)

    def simulate_batch(self, batch, *, wqb_username=None, wqb_password=None,
                       log_fn=None, proc_holder=None, partial_fn=None,
                       forced_delay=None, stop_event=None):
        results = []
        if not self._client.authenticate():
            return [self._err(s, 'WQB API 인증 실패 (RC 자격증명/권한 확인)') for s in batch]
        for s in batch:
            if stop_event is not None and stop_event.is_set():
                break
            results.append(self._run_one(s, forced_delay, partial_fn, stop_event))
        return results

    def _run_one(self, s, forced_delay, partial_fn, stop_event):
        idx = int(s.get('idx') or 0); code = s.get('code', ''); desc = s.get('desc', '')
        settings = dict(s.get('settings') or {})
        if forced_delay is not None:
            settings['delay'] = forced_delay
        url = self._client.submit_simulation(code, settings)
        if url == 'RATE_LIMITED':
            return self._err(s, 'CONCURRENT_SIMULATION_LIMIT_EXCEEDED (429)')
        if not url:
            return self._err(s, 'simulation 제출 실패 (submit 응답 없음)')
        pr = self._client.poll(url, stop_event=stop_event)
        status = pr.get('status')
        if status == 'CANCELLED':
            return self._err(s, 'pause로 취소', mode='cancelled')
        if status in ('ERROR', 'FAIL') or not pr.get('alpha'):
            return self._err(s, f"sim {status}: {pr.get('message') or ''}".strip())
        alpha_id = pr['alpha']
        h = self._client.harvest_alpha(alpha_id) or {'metrics': {}, 'is_status': {k: [] for k in PASS_FIELDS}}
        is_status = h['is_status']; metrics = h['metrics']
        corr = self._client.read_self_correlation(alpha_id)
        if corr is not None:
            metrics['self_correlation'] = str(corr)
        p_n = len(is_status.get('pass', [])); f_n = len(is_status.get('fail', [])); e_n = len(is_status.get('error', []))
        is_pass = (p_n >= 7 and f_n == 0 and e_n == 0)
        out = {'idx': idx, 'code': code, 'desc': desc,
               'pass_count': p_n, 'pass_items': is_status.get('pass', []),
               'fail_count': f_n, 'fail_items': is_status.get('fail', []),
               'submitted': False, 'submit_status': '', 'error_text': '',
               'metrics': metrics, 'is_status': is_status,
               'mode': 'pass' if is_pass else 'fail'}
        if partial_fn:
            try:
                partial_fn({'idx': idx, 'status': out['mode'], 'error_text': '',
                            'is_status': is_status, 'metrics': metrics,
                            'submit_status': '', 'submitted': False})
            except Exception as e:
                LOG.warning('partial_fn err: %s', e)
        return out

    @staticmethod
    def _err(s, msg, mode='error'):
        return {'idx': int(s.get('idx') or 0), 'code': s.get('code', ''), 'desc': s.get('desc', ''),
                'pass_count': 0, 'pass_items': [], 'fail_count': 0, 'fail_items': [],
                'submitted': False, 'submit_status': '', 'error_text': msg,
                'metrics': {}, 'is_status': {k: [] for k in PASS_FIELDS}, 'mode': mode}
```

> 참고: PASS 임계 7은 기존 `worker.PASS_THRESHOLD` 와 일치해야 한다. worker가 `is_status` 길이로 재계산하므로 여기 `mode`는 partial 로그용. 구현 시 `worker.PASS_THRESHOLD` 를 import 해 상수 일치(순환 import 주의 — 숫자 상수만 `config` 로 빼는 것이 안전).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_wqb_backend.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Stage**

```bash
git add server/wqb_backend.py tests/test_wqb_backend.py
```

---

## Task 6: dispatcher 라우팅 (wqb_browser.simulate_batch + BrowserBackend)

**Files:**
- Modify: `server/wqb_browser.py` (현 `simulate_batch` 본문 → 내부 `_browser_simulate_batch`; `simulate_batch` 는 `account_type` 라우팅 dispatcher)
- Modify: `server/wqb_backend.py` (`BrowserBackend` 얇은 래퍼 — 선택)
- Test: `tests/test_wqb_backend.py` (라우팅 추가)

**Interfaces:**
- Consumes: Task 5 `ApiBackend`
- Produces: `wqb_browser.simulate_batch(batch, *, wqb_username, wqb_password, account_type='standard', **kw)` — `account_type=='research_consultant'` → ApiBackend, else 기존 브라우저.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wqb_backend.py (추가)
def test_dispatch_routes_by_account_type(monkeypatch):
    import server.wqb_browser as wbz
    called = {}
    def fake_browser(batch, **kw): called['browser'] = True; return [{'idx': 1, 'mode': 'fail'}]
    class FakeApi:
        def __init__(self, *a, **k): pass
        def simulate_batch(self, batch, **kw): called['api'] = True; return [{'idx': 1, 'mode': 'pass'}]
    monkeypatch.setattr(wbz, '_browser_simulate_batch', fake_browser)
    monkeypatch.setattr('server.wqb_backend.ApiBackend', FakeApi)
    wbz.simulate_batch([{'idx': 1, 'code': 'x', 'settings': {}}],
                       wqb_username='e', wqb_password='p', account_type='research_consultant')
    assert called.get('api') and not called.get('browser')
    called.clear()
    wbz.simulate_batch([{'idx': 1, 'code': 'x', 'settings': {}}],
                       wqb_username='e', wqb_password='p', account_type='standard')
    assert called.get('browser') and not called.get('api')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_wqb_backend.py::test_dispatch_routes_by_account_type -v`
Expected: FAIL — `_browser_simulate_batch` 없음 / `account_type` 미지원

- [ ] **Step 3: Implement**

`server/wqb_browser.py`:
1. 기존 `def simulate_batch(batch, *, wqb_username, wqb_password, log_fn=None, proc_holder=None, partial_fn=None, forced_delay=None):` 의 **본문 전체**를 `def _browser_simulate_batch(batch, *, wqb_username, wqb_password, log_fn=None, proc_holder=None, partial_fn=None, forced_delay=None):` 로 이름만 변경(로직 무변경).
2. 새 dispatcher:

```python
def simulate_batch(batch, *, wqb_username, wqb_password, account_type='standard',
                   log_fn=None, proc_holder=None, partial_fn=None,
                   forced_delay=None, stop_event=None):
    if account_type == 'research_consultant':
        from .wqb_backend import ApiBackend
        be = ApiBackend(wqb_username, wqb_password)
        return be.simulate_batch(batch, wqb_username=wqb_username, wqb_password=wqb_password,
                                 log_fn=log_fn, proc_holder=proc_holder, partial_fn=partial_fn,
                                 forced_delay=forced_delay, stop_event=stop_event)
    return _browser_simulate_batch(batch, wqb_username=wqb_username, wqb_password=wqb_password,
                                   log_fn=log_fn, proc_holder=proc_holder,
                                   partial_fn=partial_fn, forced_delay=forced_delay)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_wqb_backend.py -v`
Expected: PASS

- [ ] **Step 5: Stage**

```bash
git add server/wqb_browser.py server/wqb_backend.py tests/test_wqb_backend.py
```

---

## Task 7: worker 라우팅 (account_type 전달)

**Files:**
- Modify: `server/worker.py` (creds 로드부 + 두 `wqb_browser.simulate_batch(...)` 호출부)

**Interfaces:**
- Consumes: Task 6 `simulate_batch(..., account_type=...)`, Task 1 `db.get_account_type`

- [ ] **Step 1: Implement (회귀 테스트로 검증)**

`server/worker.py`:
1. creds 로드 직후(현 `username, password, api_key = creds` 부근) 추가:

```python
account_type = _db.get_account_type(self.user_id)
```

2. 두 `wqb_browser.simulate_batch(...)` 호출(메인 + setup-error 재시도) 모두에 인자 추가:

```python
results = wqb_browser.simulate_batch(
    batch, wqb_username=username, wqb_password=password,
    account_type=account_type, stop_event=self._stop_event,
    log_fn=None, proc_holder=self._batch_proc_holder,
    partial_fn=_on_partial, forced_delay=forced_delay,
)
```

(브라우저 경로는 `account_type='standard'`, `stop_event` 무시 — 시그니처가 받기만 함. 무회귀.)

- [ ] **Step 2: Regression**

Run: `python3.11 -m pytest -q`
Expected: 전부 PASS (기존 + 신규)

- [ ] **Step 3: Stage**

```bash
git add server/worker.py
```

---

## Task 8: 로그인 검증 분기 (auth.py)

**Files:**
- Modify: `server/auth.py` (`validate_wqb_api`, `validate_login(account_type)`)
- Test: `tests/test_auth_account_type.py`

**Interfaces:**
- Consumes: Task 3 `WqbApiClient`(혹은 직접 requests)
- Produces: `auth.validate_wqb_api(username, password)->dict`(`{ok,reason,detail}`), `auth.validate_login(wqb_username, wqb_password, gemini_api_key, account_type='standard')`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_auth_account_type.py
import server.auth as auth

class R:
    def __init__(self, code): self.status_code = code; self.text = ''
    def json(self): return {}

def test_validate_wqb_api_ok(monkeypatch):
    monkeypatch.setattr(auth, '_api_post_auth', lambda u, p: R(201))
    assert auth.validate_wqb_api('e', 'p')['ok'] is True

def test_validate_wqb_api_bad_creds(monkeypatch):
    monkeypatch.setattr(auth, '_api_post_auth', lambda u, p: R(401))
    r = auth.validate_wqb_api('e', 'p')
    assert r['ok'] is False and r['reason'] == 'wqb_credentials'

def test_validate_wqb_api_not_consultant(monkeypatch):
    monkeypatch.setattr(auth, '_api_post_auth', lambda u, p: R(403))
    r = auth.validate_wqb_api('e', 'p')
    assert r['reason'] == 'wqb_not_consultant'

def test_validate_login_routes_rc(monkeypatch):
    monkeypatch.setattr(auth, 'validate_gemini_key', lambda k: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_login', lambda u, p: {'ok': False, 'reason': 'should_not_call'})
    assert auth.validate_login('e', 'p', 'g', account_type='research_consultant')['ok'] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_auth_account_type.py -v`
Expected: FAIL — `validate_wqb_api` 없음 / `validate_login` account_type 미지원

- [ ] **Step 3: Implement**

`server/auth.py` 에 추가:

```python
import requests as _requests
from requests.auth import HTTPBasicAuth as _HTTPBasicAuth

_WQB_API_BASE = 'https://api.worldquantbrain.com'

def _api_post_auth(username: str, password: str):
    sess = _requests.Session()
    sess.auth = _HTTPBasicAuth(username, password)
    return sess.post(_WQB_API_BASE + '/authentication', timeout=30)

def validate_wqb_api(username: str, password: str) -> dict:
    if not username or not password:
        return {'ok': False, 'reason': 'wqb_credentials', 'detail': '아이디/비밀번호 비어있음'}
    try:
        r = _api_post_auth(username, password)
    except Exception as e:
        return {'ok': False, 'reason': 'wqb_unreachable',
                'detail': f'API 인증 연결 실패: {type(e).__name__}: {e}'}
    if r.status_code in (200, 201):
        return {'ok': True, 'reason': 'ok', 'detail': 'WQB API 인증 성공'}
    if r.status_code == 401:
        return {'ok': False, 'reason': 'wqb_credentials', 'detail': 'WQB API 401 — 자격증명 거절'}
    if r.status_code == 403:
        return {'ok': False, 'reason': 'wqb_not_consultant',
                'detail': 'WQB API 403 — Research Consultant 권한 없음(또는 API 미허용)'}
    return {'ok': False, 'reason': 'wqb_unreachable', 'detail': f'WQB API 인증 HTTP {r.status_code}'}
```

`validate_login` 에 account_type 분기 추가:

```python
def validate_login(wqb_username, wqb_password, gemini_api_key, account_type='standard'):
    g = validate_gemini_key(gemini_api_key)
    if not g.get('ok'):
        return g
    if account_type == 'research_consultant':
        w = validate_wqb_api(wqb_username, wqb_password)
    else:
        w = validate_wqb_login(wqb_username, wqb_password)
    if not w.get('ok'):
        return w
    return {'ok': True, 'reason': 'ok', 'detail': f'Gemini + WQB({account_type}) 통과'}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_auth_account_type.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Stage**

```bash
git add server/auth.py tests/test_auth_account_type.py
```

---

## Task 9: 가입/전환 엔드포인트 + 프론트 (app.py + index.html)

**Files:**
- Modify: `server/app.py` (가입/로그인 핸들러 `account_type`; 신규 `/api/account/upgrade-to-rc`)
- Modify: `static/` · `index.html` (가입 라디오 + 전환 버튼)

**Interfaces:**
- Consumes: Task 1 `db.upsert_user(..., account_type)`·`db.set_account_type`·`db.get_account_type`·`db.get_user_credentials`; Task 8 `auth.validate_login(..., account_type)`·`auth.validate_wqb_api`

- [ ] **Step 1: Write the failing test (가능 범위)**

```python
# tests/test_app_account_type.py (있으면 기존 test client 패턴 재사용)
# 핵심: 가입에 account_type 전달 시 upsert_user 가 그 값으로 호출되는지 + upgrade 엔드포인트.
```

기존 app 테스트 패턴(`tests/test_client*`/`test_run_config*`)을 따른다. 라우트 핸들러를 직접 호출하거나 test client로 검증.

- [ ] **Step 2: Implement — 가입/로그인 account_type**

`server/app.py` 가입/로그인 핸들러:
- 폼/JSON에서 `account_type = (payload.get('account_type') or 'standard')` 추출(허용값 화이트리스트: `{'standard','research_consultant'}`, 그 외 → `'standard'`).
- `auth.validate_login(wqb_username, wqb_password, gemini_api_key, account_type=account_type)` 로 검증.
- `db.upsert_user(..., account_type=account_type)`.

- [ ] **Step 3: Implement — 전환 엔드포인트**

```python
@app.route('/api/account/upgrade-to-rc', methods=['POST'])
def upgrade_to_rc():
    uid = _current_user_id()
    if not uid:
        return jsonify({'ok': False, 'reason': 'not_logged_in'}), 401
    creds = _db.get_user_credentials(uid)
    if not creds:
        return jsonify({'ok': False, 'reason': 'no_credentials'}), 400
    username, password, _ = creds
    v = auth.validate_wqb_api(username, password)
    if not v.get('ok'):
        return jsonify(v), 400
    _db.set_account_type(uid, 'research_consultant')
    return jsonify({'ok': True, 'account_type': 'research_consultant'})
```

(FastAPI면 해당 라우터 컨벤션에 맞춰 변환 — 기존 app.py 스타일을 따른다.)

- [ ] **Step 4: Implement — 프론트**

`index.html` 가입 폼: 라디오 추가

```html
<label><input type="radio" name="account_type" value="standard" checked> 일반</label>
<label><input type="radio" name="account_type" value="research_consultant"> Research Consultant</label>
```

가입 제출 JS에 `account_type: document.querySelector('input[name=account_type]:checked').value` 포함. 계정/설정 영역에 버튼:

```html
<button id="btn-upgrade-rc">Research Consultant로 전환</button>
```

```js
document.getElementById('btn-upgrade-rc').onclick = async () => {
  const r = await fetch('/api/account/upgrade-to-rc', {method:'POST'});
  const j = await r.json();
  alert(j.ok ? 'RC로 전환되었습니다.' : ('전환 실패: ' + (j.detail || j.reason)));
};
```

- [ ] **Step 5: Run tests + manual smoke**

Run: `python3.11 -m pytest tests/test_app_account_type.py -v` (작성 범위) + 서버 기동 후 가입/전환 수동 확인.
Expected: PASS + 전환 버튼 동작

- [ ] **Step 6: Stage**

```bash
git add server/app.py static/ index.html tests/test_app_account_type.py
```

---

## Task 10: 하우스 데이터 서비스 (wqb_data_service.py)

**Files:**
- Create: `server/wqb_data_service.py`
- Modify: `server/config.py` 또는 `run_config.py` (`HOUSE_RC_USERNAME`)
- Modify: `requirements.txt` (`requests`)
- Test: `tests/test_wqb_data_service.py`

**Interfaces:**
- Consumes: Task 3 `WqbApiClient`, `db.get_user_credentials`
- Produces: `wqb_data_service.map_datafields(api_results:list, region, universe, delay)->list[dict]`(CSV 행), `wqb_data_service.write_live_csv(rows, path)->None`, `wqb_data_service.refresh(now_ts:float|None=None)->bool`, 상수 `LIVE_CSV_PATH`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_wqb_data_service.py
import csv, os
import server.wqb_data_service as ds

def test_map_datafields():
    api = [{'id': 'close', 'description': 'Close', 'type': 'MATRIX', 'coverage': 1.0, 'alphaCount': 12,
            'dataset': {'id': 'pv1'}}]
    rows = ds.map_datafields(api, region='USA', universe='TOP3000', delay=1)
    r = rows[0]
    assert r['name'] == 'close' and r['region'] == 'USA' and r['delay'] == '1'
    assert r['alphas'] == '12' and int(r['coverage']) == 100 and r['category']

def test_write_live_csv_atomic_and_header(tmp_path):
    rows = ds.map_datafields([{'id': 'x', 'description': 'd', 'type': 'MATRIX',
                               'coverage': 0.5, 'alphaCount': 3, 'dataset': {'id': 'pv'}}],
                             region='USA', universe='TOP3000', delay=1)
    p = str(tmp_path / 'live.csv')
    ds.write_live_csv(rows, p)
    with open(p, newline='') as fh:
        rd = list(csv.DictReader(fh))
    assert rd[0]['name'] == 'x'
    assert set(['name','category','coverage','description','type','date_coverage_pct',
                'alphas','region','universe','delay']).issubset(rd[0].keys())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_wqb_data_service.py -v`
Expected: FAIL — 모듈 없음

- [ ] **Step 3: Implement**

```python
# server/wqb_data_service.py
"""하우스 RC 계정으로 /data-fields·/operators 실시간 조회 → data/live_datafields.csv.
실패는 절대 생성 흐름을 막지 않는다(호출부가 폴백)."""
from __future__ import annotations
import csv, logging, os, tempfile
from . import db as _db
from . import wqb_api

LOG = logging.getLogger('hyfe.wqb_data')
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_THIS_DIR), 'data')
LIVE_CSV_PATH = os.path.join(_DATA_DIR, 'live_datafields.csv')
CSV_COLUMNS = ['name','category','coverage','description','type',
               'date_coverage_pct','alphas','region','universe','delay']
HOUSE_RC_USERNAME = os.environ.get('HYFE_HOUSE_RC_USERNAME', 'platinumcasillas@gmail.com')
_TTL_SEC = 6 * 3600
_last_refresh = {'ts': 0.0}

def map_datafields(api_results, region, universe, delay) -> list[dict]:
    rows = []
    for d in api_results or []:
        cov = d.get('coverage')
        cov_pct = int(round(cov * 100)) if isinstance(cov, (int, float)) and cov <= 1.0 else int(cov or 0)
        typ = str(d.get('type') or '')
        rows.append({
            'name': d.get('id', ''),
            'category': (d.get('dataset') or {}).get('id') or typ.lower(),
            'coverage': str(cov_pct),
            'description': d.get('description', ''),
            'type': typ.title() if typ else '',
            'date_coverage_pct': str(cov_pct),
            'alphas': str(d.get('alphaCount', d.get('userCount', 0)) or 0),
            'region': region, 'universe': universe, 'delay': str(delay),
        })
    return rows

def write_live_csv(rows, path=LIVE_CSV_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, '') for k in CSV_COLUMNS})
        os.replace(tmp, path)  # 원자적
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass

def _house_client():
    uid = _db.get_user_id_by_username(HOUSE_RC_USERNAME)  # 없으면 None — 헬퍼 필요시 추가
    if not uid:
        return None
    creds = _db.get_user_credentials(uid)
    if not creds:
        return None
    u, p, _ = creds
    return wqb_api.WqbApiClient(u, p)

def refresh(now_ts: float | None = None,
            grid=(('USA', 'TOP3000', 1), ('USA', 'TOP3000', 0))) -> bool:
    """하우스 계정으로 grid 별 /data-fields 페이지네이션 수집 → 라이브 CSV. 성공 True."""
    c = _house_client()
    if not c or not c.authenticate():
        LOG.warning('house RC client 미가용 — 라이브 데이터 새로고침 skip'); return False
    all_rows = []
    try:
        for region, universe, delay in grid:
            offset = 0
            while True:
                r = c.session.get(f'{wqb_api.BASE}/data-fields',
                                  params={'region': region, 'universe': universe,
                                          'delay': delay, 'limit': 50, 'offset': offset})
                if not r.ok:
                    break
                j = r.json(); res = j.get('results') or []
                all_rows += map_datafields(res, region, universe, delay)
                offset += 50
                if offset >= int(j.get('count') or 0) or not res:
                    break
        if all_rows:
            write_live_csv(all_rows)
            if now_ts is not None:
                _last_refresh['ts'] = now_ts
            LOG.info('live datafields 새로고침: %d rows', len(all_rows))
            return True
    except Exception as e:
        LOG.warning('refresh 실패(폴백 유지): %s', e)
    return False

def maybe_refresh(now_ts: float) -> bool:
    if now_ts - _last_refresh['ts'] >= _TTL_SEC:
        return refresh(now_ts=now_ts)
    return False
```

`db.get_user_id_by_username` 가 없으면 `server/db.py` 에 추가(하우스 컨벤션):

```python
@_with_conn
def get_user_id_by_username(conn, wqb_username: str) -> int | None:
    row = conn.execute('SELECT id FROM users WHERE wqb_username=?', (wqb_username,)).fetchone()
    return row[0] if row else None
```

`requirements.txt` 에 `requests` 추가(없으면). `config.py`/`run_config.py` 에 `HOUSE_RC_USERNAME` 노출(옵션 — 환경변수만으로도 충분).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_wqb_data_service.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Stage**

```bash
git add server/wqb_data_service.py server/db.py requirements.txt tests/test_wqb_data_service.py
```

---

## Task 11: 팔레트 라이브 소스 우선 (datafield_palette.py)

**Files:**
- Modify: `server/datafield_palette.py` (`_default_datafields_path`, `build_palette` 기본 소스)
- Test: `tests/test_datafield_palette_live.py`

**Interfaces:**
- Consumes: Task 10 `wqb_data_service.LIVE_CSV_PATH`
- Produces: `datafield_palette._default_datafields_path()->str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datafield_palette_live.py
import importlib, os
import server.datafield_palette as dp

def _write_csv(path, names):
    import csv
    cols = ['name','category','coverage','description','type','date_coverage_pct','alphas','region','universe','delay']
    with open(path, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for n in names:
            w.writerow({'name': n, 'category': 'matrix', 'coverage': '100', 'description': 'd',
                        'type': 'Matrix', 'date_coverage_pct': '100', 'alphas': '5',
                        'region': 'USA', 'universe': 'TOP3000', 'delay': '1'})

def test_live_csv_preferred(tmp_path, monkeypatch):
    live = str(tmp_path / 'live_datafields.csv'); _write_csv(live, ['LIVE_FIELD_X'])
    monkeypatch.setattr(dp, '_LIVE_CSV_PATH', live)
    p = dp._default_datafields_path()
    assert p == live
    out = dp.build_palette(region='USA', delay=1, universe='TOP3000', n=1)
    assert 'LIVE_FIELD_X' in out

def test_falls_back_to_static_when_no_live(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, '_LIVE_CSV_PATH', str(tmp_path / 'nope.csv'))
    assert dp._default_datafields_path() == dp.DATAFIELDS_CSV
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_datafield_palette_live.py -v`
Expected: FAIL — `_default_datafields_path`/`_LIVE_CSV_PATH` 없음

- [ ] **Step 3: Implement**

`server/datafield_palette.py`:

```python
# 상단(상수부)
_LIVE_CSV_PATH = os.path.join(os.path.dirname(_THIS_DIR), 'data', 'live_datafields.csv')

def _default_datafields_path() -> str:
    """라이브 CSV가 존재하고 비어있지 않으면 우선, 아니면 정적 CSV."""
    try:
        if os.path.exists(_LIVE_CSV_PATH) and os.path.getsize(_LIVE_CSV_PATH) > 0:
            return _LIVE_CSV_PATH
    except OSError:
        pass
    return DATAFIELDS_CSV
```

`build_palette` 시그니처 변경: `_csv_path: str = DATAFIELDS_CSV` → `_csv_path: 'str | None' = None`, 본문 첫 줄:

```python
    if _csv_path is None:
        _csv_path = _default_datafields_path()
```

(테스트 주입 `_csv_path=...` 은 그대로 동작.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.11 -m pytest tests/test_datafield_palette_live.py tests/test_datafield_palette.py -v`
Expected: PASS (신규 + 기존 무회귀)

- [ ] **Step 5: Stage**

```bash
git add server/datafield_palette.py tests/test_datafield_palette_live.py
```

---

## Task 12: 통합 — 하우스 보정 + 데이터 서비스 기동 + 전체 회귀

**Files:**
- Modify: `server/app.py`(startup) 또는 `server/worker.py`(라운드 훅)
- Modify: `.gitignore` (`data/live_datafields.csv` 확인 — `data/` 가 이미 gitignore면 불필요)

**Interfaces:**
- Consumes: Task 10 `wqb_data_service.refresh`/`maybe_refresh`, Task 1 `db.set_account_type`/`get_user_id_by_username`

- [ ] **Step 1: 하우스 계정 RC 보정(startup)**

`server/app.py` 의 startup(또는 `_db.init_db()` 직후)에:

```python
try:
    _hid = _db.get_user_id_by_username(wqb_data_service.HOUSE_RC_USERNAME)
    if _hid and _db.get_account_type(_hid) != 'research_consultant':
        _db.set_account_type(_hid, 'research_consultant')
        LOG.info('house RC 계정 보정: %s', wqb_data_service.HOUSE_RC_USERNAME)
except Exception as e:
    LOG.warning('house RC 보정 skip: %s', e)
```

- [ ] **Step 2: 데이터 서비스 기동(주기 새로고침)**

worker 라운드 루프 시작부(또는 별도 백그라운드 스레드)에서:

```python
import time as _time
try:
    wqb_data_service.maybe_refresh(_time.time())
except Exception:
    pass  # 생성 절대 안 깨짐
```

(어느 사용자 라운드든 6h 경과 시 1회 새로고침. 가벼운 게이트.)

- [ ] **Step 3: gitignore 확인**

Run: `git check-ignore data/live_datafields.csv && echo IGNORED || echo NOT-IGNORED`
Expected: IGNORED (메모리: `data/` 는 대부분 gitignore). NOT-IGNORED면 `.gitignore` 에 `data/live_datafields.csv` 추가.

- [ ] **Step 4: 전체 회귀**

Run: `python3.11 -m pytest -q`
Expected: 전부 PASS

- [ ] **Step 5: 라이브 검증(사장)**

1. 서버 재시작.
2. 하우스 RC 계정으로 로그인 → 한 라운드 시뮬이 API 경로로 채점되는지 로그 확인(브라우저 subprocess 안 뜸).
3. `data/live_datafields.csv` 생성 + 일반 사용자 생성 프롬프트에 라이브 필드 반영 확인.
4. 일반 계정 로그인 → 기존 브라우저 경로 무변경 동작 확인(무회귀).

- [ ] **Step 6: Stage**

```bash
git add server/app.py server/worker.py .gitignore
# 사장 승인 시 전체 커밋 + 서버 재시작
```

---

## Self-Review (작성자 체크 결과)

- **Spec coverage:** §5.1→T1, §10 스모크→T2, §5.3→T3·T4, §5.2→T5·T6, §5.7→T7, §5.4→T8, §5.5→T9, §5.6→T10, 팔레트→T11, 부트스트랩/주기→T12. 전 절 매핑됨.
- **Placeholder scan:** 모든 코드 스텝에 실제 코드/테스트 포함. "TBD" 없음. (Task 2/4의 settings·correlation은 라이브 스모크로 확정하는 명시적 절차 — placeholder 아님.)
- **Type consistency:** simulate_batch 반환 키, `is_status` 버킷명(`pass/fail/error/pending`), `harvest_alpha` 반환형, `account_type` 값(`standard`/`research_consultant`)이 T1~T12에서 일관.
- **알려진 의존:** T5 PASS 임계 7은 `worker.PASS_THRESHOLD` 와 일치 필요(구현 시 상수 출처 확인). `_connect`/`get_user_credentials` 등 db 헬퍼명은 기존 `server/db.py` 실제 이름에 맞춰 사용.
