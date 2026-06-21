# ArQuant식 회원관리 — register/login 분리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** `/api/login`(로그인+가입 겸용)을 ArQuant처럼 `/api/login`(기존 사용자 전용)과 `/api/register`(신규 전용)로 분리. account_type은 **가입 때만** 묻고(로그인 폼에서 제거), 일반→RC 전환은 프로필/계정 영역에 둔다.

**Architecture:** IQC는 WQB 이메일/비번이 로그인 ID. 현 `/api/login`의 "기존 사용자 fast-path"→`/api/login`, "신규 사용자 full-validation"→`/api/register`로 이동. account_type/Gemini-필수/in-flight락은 register로. 프론트는 로그인/회원가입 탭 토글.

**Tech Stack:** Flask, pytest, static HTML/JS.

## Global Constraints
- 테스트는 `python3.11 -m pytest`.
- 무회귀: 기존 사용자(예: platinumcasillas) 로그인은 account_type 없이 그대로 동작. 표준/RC 라우팅·worker 무영향.
- 보안: 비밀번호 mismatch 시 chromium subprocess 안 뜨게(기존 보호 유지). 신규가입만 full-validation(브라우저/API) 트리거.
- 커밋은 사장 명시 요청 시에만; Task별 커밋 main, 끝에 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

## Task 1: app.py — /api/login(기존 전용) + /api/register(신규 전용) 분리

**Files:** Modify `server/app.py`; Test `tests/test_app_account_type.py` (수정/추가)

**Interfaces:**
- `POST /api/login`: 기존 사용자만. 미존재→404 `not_registered`. 비번 mismatch→401 `wqb_credentials`. 일치→기존 Gemini-resolution fast-path→세션. **account_type 안 읽음/안 바꿈.**
- `POST /api/register`: 신규만. 이미 존재→409 `already_registered`. gemini 미입력→400. account_type(whitelist)+validate_login(account_type)+upsert_user(account_type)→세션.

- [ ] **Step 1: 실패 테스트** (tests/test_app_account_type.py)

```python
import server.app as app_mod

def _client(): return app_mod.app.test_client()

def test_login_rejects_unregistered(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)
    r = _client().post('/api/login', json={'wqb_username':'new@x.com','wqb_password':'pw'})
    assert r.status_code == 404 and r.get_json()['reason'] == 'not_registered'

def test_login_existing_password_match(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username',
        lambda u: {'id':2,'wqb_password':'pw','gemini_api_key':'gk'})
    monkeypatch.setattr(app_mod._auth, 'validate_gemini_key', lambda k: {'ok':True})
    monkeypatch.setattr(app_mod._db, 'update_user_secrets', lambda *a, **k: None)
    monkeypatch.setattr(app_mod, '_issue_session', lambda uid,u,r: app_mod.jsonify({'ok':True,'user_id':uid}))
    r = _client().post('/api/login', json={'wqb_username':'e','wqb_password':'pw','gemini_api_key':'gk'})
    assert r.get_json().get('ok') is True

def test_register_rejects_existing(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: {'id':2,'wqb_password':'pw'})
    r = _client().post('/api/register', json={'wqb_username':'e','wqb_password':'pw','gemini_api_key':'gk','account_type':'research_consultant'})
    assert r.status_code == 409 and r.get_json()['reason'] == 'already_registered'

def test_register_creates_with_account_type(monkeypatch):
    monkeypatch.setattr(app_mod._db, 'find_user_by_username', lambda u: None)
    captured = {}
    monkeypatch.setattr(app_mod._auth, 'validate_login',
        lambda u,p,g,account_type='standard': captured.setdefault('vl', account_type) or {'ok':True,'reason':'ok'})
    monkeypatch.setattr(app_mod._db, 'upsert_user',
        lambda u,p,g,account_type='standard': captured.setdefault('up', account_type) or 7)
    monkeypatch.setattr(app_mod, '_issue_session', lambda uid,u,r: app_mod.jsonify({'ok':True}))
    r = _client().post('/api/register', json={'wqb_username':'new@x.com','wqb_password':'pw','gemini_api_key':'gk','account_type':'research_consultant'})
    assert r.get_json().get('ok') is True
    assert captured['vl'] == 'research_consultant' and captured['up'] == 'research_consultant'
```

- [ ] **Step 2: RED**.
- [ ] **Step 3: 구현** — `server/app.py`:
  1. `api_login` 본문을 "기존 사용자 전용"으로: missing→400; `existing=_db.find_user_by_username(...)`; `if not existing: return _err('not_registered','가입되지 않은 계정입니다. 회원가입을 먼저 해주세요.',404)`; `if existing['wqb_password'] != wqb_password: return _err('wqb_credentials',...,401)`; 그 다음 **기존 fast-path의 Gemini-resolution 블록 그대로**(입력키 검증→갱신, 저장키 폴백, 둘 다 무효→gemini_invalid) → `_issue_session`. account_type 읽기/처리 **삭제**.
  2. 새 `api_register`(`@app.route('/api/register', methods=['POST'])`): body 파싱 + `account_type` whitelist; missing username/pw→400; `if _db.find_user_by_username(wqb_username): return _err('already_registered','이미 가입된 계정입니다. 로그인해 주세요.',409)`; `if not gemini_api_key: return _err('missing_fields','신규 가입에는 Gemini API 키가 필요합니다.',400)`; **기존 신규-사용자 경로의 in-flight 락 + `_auth.validate_login(..., account_type=account_type)` + `_db.upsert_user(..., account_type=account_type)` 그대로 이동** → `_issue_session`.
- [ ] **Step 4: GREEN** — `tests/test_app_account_type.py` + 기존 account_type 테스트(가입을 검증하던 것은 `/api/register`로 갱신) + FULL suite 무회귀. (기존 `test_app_account_type.py`의 signup 테스트가 `/api/login`에 account_type을 보내 upsert를 검증했다면 `/api/register`로 경로 변경.)
- [ ] **Step 5: Stage** — `git add server/app.py tests/test_app_account_type.py`.

---

## Task 2: 프론트 — 로그인/회원가입 탭 + 가입에만 account_type + RC전환 프로필

**Files:** Modify `static/index.html`, `static/app.js`

**Interfaces:** Consumes Task 1 `/api/login`·`/api/register`.

- [ ] **Step 1: 구현** — `static/index.html`:
  - `#screen-login`에 **탭 토글** 추가(예: "로그인 | 회원가입" 버튼 2개, `#tab-login-btn`/`#tab-register-btn`).
  - **로그인 폼**(`#form-login`): WQB 이메일 + 비밀번호 + (선택)Gemini키. **account_type 라디오 제거.**
  - **회원가입 폼**(`#form-register`, 기본 hidden): WQB 이메일 + 비밀번호 + Gemini키(필수 안내) + **account_type 라디오**(일반/Research Consultant, 기존 마크업 이동).
  - 대시보드의 RC 전환 버튼(`#btn-upgrade-rc`)을 **계정/프로필 영역**으로 이동(예: userInfo 근처 또는 새 `#account-section`), "계정 유형: 일반 → Research Consultant로 전환" 라벨과 함께.
- [ ] **Step 2: 구현** — `static/app.js`:
  - 탭 토글 핸들러: `#tab-login-btn`/`#tab-register-btn` 클릭 시 두 폼 show/hide.
  - 로그인 제출: `#form-login` → `POST /api/login` body `{wqb_username,wqb_password,gemini_api_key,remember}` (**account_type 없음**). `not_registered`(404) 응답이면 "회원가입이 필요합니다 — 회원가입 탭으로" 안내 + 회원가입 탭으로 전환.
  - 회원가입 제출: `#form-register` → `POST /api/register` body `{wqb_username,wqb_password,gemini_api_key,remember,account_type}` (라디오에서 읽음). `already_registered`(409)면 "이미 가입됨 — 로그인하세요" 안내 + 로그인 탭.
  - 성공 시 둘 다 기존 `onLoggedIn`/대시보드 진입 흐름 재사용.
  - 기존 로그인 핸들러가 account_type을 읽던 코드 제거(로그인엔 없음).
- [ ] **Step 3: 확인** — `python3.11 -m pytest -q` 무회귀(백엔드 무관). 서버 기동 후 수동: 로그인 탭=ID/PW만, 회원가입 탭=유형선택, 기존 사용자 로그인 정상, RC 전환 버튼이 프로필에.
- [ ] **Step 4: Stage** — `git add static/index.html static/app.js`.

---

## Self-Review
- Spec→Task: register/login 분리 T1, 프론트 탭+가입전용 account_type+RC프로필 T2.
- 무회귀 핵심: 기존 사용자(platinumcasillas 등) 로그인이 account_type 없이 동작(T1 test_login_existing_password_match). 신규 검증/생성은 register로만.
- 보안: 비번 mismatch→401(브라우저 안 뜸), 신규만 full-validation — 기존 chromium 보호 유지.
