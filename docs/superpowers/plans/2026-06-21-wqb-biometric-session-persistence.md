# WQB Biometric 우회 — 세션 지속 + Persona 대시보드 플로우 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** WQB API의 biometric(persona) 인증 빈도를 "매 라운드/매 재시작"에서 "토큰 실제 만료 시 1회"로 낮춘다 — 인증 세션 쿠키를 디스크에 계정별로 저장·재사용하고, persona가 필요할 땐 대시보드에서 1회 완료한다.

**Architecture:** `WqbApiClient`가 계정별 쿠키 jar(`data/wqb_sessions/<sha1(email)>.pkl`)를 저장/로드한다. `authenticate()`는 저장 세션이 유효하면 `/authentication`을 아예 안 친다(=biometric 없음). 유효 세션이 없을 때만 Basic Auth POST → 성공 시 쿠키 저장, `401+persona`면 pending 세션(pre-auth 쿠키+inquiry URL)을 저장하고 persona-required를 신호한다. 대시보드가 persona URL을 보여주고, 사장이 브라우저에서 완료 후 '완료' 버튼 → 서버가 pending 세션으로 finalize → 진짜 세션 저장 → 워커가 재사용. (참조: OddMiss/WorldQuantBrain-Agent, RussellDash332/WQ-Brain, justinhuang0208/brain_viewer)

**Tech Stack:** Python 3.11, `requests` (cookie jar pickle), Flask, pytest.

## Global Constraints

- **테스트는 `python3.11 -m pytest`**.
- **무회귀:** 표준(브라우저) 경로 불변. 기존 RC 동작(시뮬·채점 계약)도 불변 — 이번 변경은 인증/세션 계층에만.
- **simulate_batch 계약 불변** (기존 13키 결과 dict + partial_fn).
- **생성 절대 안 깨짐:** 세션/persona 어떤 실패도 raise해서 워커/생성을 멈추면 안 됨 — graceful.
- **세션 파일은 `data/wqb_sessions/` (gitignore된 `data/` 하위)**, 계정별 `<sha1(email)[:16]>.pkl` + `.pending.pkl`. 비밀번호/쿠키는 절대 로그/응답에 노출 금지.
- **persona 미완 시 워커는 churn 금지** — persona-required면 라운드 시뮬을 빠르게 실패시키되 "biometric 필요" 명확 신호.
- WQB API base `https://api.worldquantbrain.com`. persona 완료 엔드포인트는 라이브로 최종 확정(아래 §live-confirm).
- **커밋은 사장 명시 요청 시에만**; Task별 커밋은 `git add` 후 커밋(main, 합의된 워크플로우). 끝에 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## live-confirm (사장 1회 biometric 완료 시 확정)
persona 완료의 정확한 finalize는 두 변형이 관찰됨: (a) WQ-Brain `POST {auth}/authentication/persona` (body=`{"inquiry":...}`), (b) brain_viewer persona URL GET 폴링 후 `/authentication` 재-POST. `complete_persona()`는 (a) 시도 → 검증(GET /authentication에 `user` 존재) 실패 시 (b) 재-POST 폴백, 둘 다 방어적으로 구현. 사장 첫 완료 때 실제 동작 확인.

---

## File Structure

| 파일 | 변경 |
|---|---|
| `server/wqb_api.py` (수정) | 세션 저장/로드/검증/재사용 + persona 감지 + pending + complete_persona |
| `tests/test_wqb_api_session.py` (신규) | 지속·재사용·persona 단위테스트 (mock Session + tmp 파일) |
| `server/wqb_backend.py` (수정) | ApiBackend가 계정별 session_file 경로 주입 + persona-required graceful 결과 |
| `server/auth.py` (수정) | validate_wqb_api가 persona → reason `wqb_persona_required` + url |
| `server/app.py` (수정) | `/api/account/wqb-persona-status` + `/api/account/wqb-persona-complete` |
| `static/index.html`·`static/app.js` (수정) | persona 안내 + URL 링크 + '완료' 버튼 + 상태 폴링 |
| `tests/test_auth_account_type.py`·`tests/test_app_account_type.py` (수정) | persona 분기 테스트 |
| `server/session_store.py` (신규, 선택) | 계정별 세션 파일 경로 헬퍼 (sha1) — wqb_api에 인라인해도 됨 |

---

## Task 1: WqbApiClient 세션 지속 (저장/로드/검증/재사용)

**Files:** Modify `server/wqb_api.py`; Test `tests/test_wqb_api_session.py`

**Interfaces:**
- Produces: `WqbApiClient(email, password, session=None, session_file=None)`; `_save_session()->bool`; `_load_session()->bool`; `_session_valid()->bool`; `authenticate()` reworked to reuse saved session.

- [ ] **Step 1: 실패 테스트**

```python
# tests/test_wqb_api_session.py
import pickle, os
import server.wqb_api as wqb_api

class FakeResp:
    def __init__(self, status=200, json_data=None, headers=None):
        self.status_code=status; self._j=json_data or {}; self.headers=headers or {}; self.text=''
    def json(self): return self._j
    @property
    def ok(self): return 200<=self.status_code<300

class FakeCookies:
    def __init__(self, d=None): self._d=dict(d or {})
    def get_dict(self): return dict(self._d)
    def update(self, d):
        d = d.get_dict() if hasattr(d,'get_dict') else d
        self._d.update(d)
    def __bool__(self): return bool(self._d)

class FakeSession:
    def __init__(self): self.auth=None; self.cookies=FakeCookies(); self.calls=[]; self.queue={}
    def _resp(self, m, url):
        key=(m, url.replace('https://api.worldquantbrain.com','').split('?')[0])
        self.calls.append(key)
        return self.queue.get(key, [FakeResp(200,{'user':{'id':'u'}})]).pop(0)
    def post(self,url,**k): return self._resp('POST',url)
    def get(self,url,**k): return self._resp('GET',url)

def test_save_then_load_roundtrip(tmp_path):
    sf=str(tmp_path/'s.pkl')
    sess=FakeSession(); sess.cookies=FakeCookies({'t':'JWT123'})
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    assert c._save_session() is True and os.path.exists(sf)
    sess2=FakeSession()
    c2=wqb_api.WqbApiClient('e','p',session=sess2,session_file=sf)
    assert c2._load_session() is True
    assert sess2.cookies.get_dict().get('t')=='JWT123'

def test_authenticate_reuses_valid_saved_session_no_post(tmp_path):
    sf=str(tmp_path/'s.pkl')
    seed=FakeSession(); seed.cookies=FakeCookies({'t':'JWT'})
    wqb_api.WqbApiClient('e','p',session=seed,session_file=sf)._save_session()
    sess=FakeSession()
    sess.queue[('GET','/authentication')]=[FakeResp(200,{'user':{'id':'u'}})]  # saved session valid
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    assert c.authenticate() is True
    assert ('POST','/authentication') not in sess.calls   # never re-authed → no biometric
```

- [ ] **Step 2: RED** — `python3.11 -m pytest tests/test_wqb_api_session.py -v` → fail (`session_file`/methods 미정의).

- [ ] **Step 3: 구현** (server/wqb_api.py)

```python
import hashlib, os, pickle, logging
LOG = logging.getLogger('hyfe.wqb_api')

def _default_session_file(email: str) -> str:
    h = hashlib.sha1((email or '').encode('utf-8')).hexdigest()[:16]
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'wqb_sessions')
    return os.path.join(d, f'{h}.pkl')

class WqbApiClient:
    def __init__(self, email, password, session=None, session_file=None):
        import requests
        from requests.auth import HTTPBasicAuth
        self.email=email; self.password=password
        self.session=session or requests.Session()
        self.session.auth=HTTPBasicAuth(email,password)
        # session_file semantics: None → default per-account path (worker/prod);
        #   False → persistence DISABLED (unit tests / no-persist); str → that path.
        if session_file is None:
            self.session_file=_default_session_file(email)
        elif session_file is False:
            self.session_file=None     # disabled
        else:
            self.session_file=session_file
        self.persona_url=None
        self.persona_required=False
        self._authed=False

    def _save_session(self) -> bool:
        try:
            if not self.session_file: return False   # persistence disabled
            ck=self.session.cookies
            d=ck.get_dict() if hasattr(ck,'get_dict') else dict(ck)
            if not d: return False
            os.makedirs(os.path.dirname(self.session_file), exist_ok=True)
            tmp=self.session_file+'.tmp'
            with open(tmp,'wb') as f: pickle.dump(d,f)
            os.replace(tmp,self.session_file)
            return True
        except Exception as e:
            LOG.warning('session save err: %s', e); return False

    def _load_session(self) -> bool:
        try:
            if not self.session_file or not os.path.exists(self.session_file): return False
            with open(self.session_file,'rb') as f: d=pickle.load(f)
            if not d: return False
            self.session.cookies.update(d)
            return True
        except Exception as e:
            LOG.warning('session load err: %s', e); return False

    def _session_valid(self) -> bool:
        try:
            r=self.session.get(f'{BASE}/authentication', timeout=15)
            return r.ok and isinstance(r.json(), dict) and ('user' in r.json())
        except Exception:
            return False

    def authenticate(self) -> bool:
        # 1) reuse persisted session — no /authentication POST → no biometric
        if self._load_session() and self._session_valid():
            self._authed=True; return True
        # 2) fresh Basic Auth (persona handling added in Task 2)
        try:
            r=self.session.post(f'{BASE}/authentication', timeout=30)
        except Exception as e:
            LOG.warning('authenticate network err: %s', e); return False
        body=r.json() if (r.headers.get('Content-Type','').startswith('application/json')) else {}
        if r.status_code in (200,201) and isinstance(body,dict) and 'user' in body:
            self._save_session(); self._authed=True; return True
        if r.status_code in (200,201):  # success without explicit user body
            self._save_session(); self._authed=True; return True
        LOG.warning('authenticate failed: HTTP %s', r.status_code)
        return False
```

(keep existing `harvest_alpha`/`submit_simulation`/`poll`/etc. unchanged; they use `self.session`.)

- [ ] **Step 4: 기존 test_wqb_api.py 격리** — 그 파일의 모든 `WqbApiClient('e','p',session=sess)` 생성에 **`session_file=False`** 인자를 추가한다(지속 비활성 → 실제 `data/wqb_sessions/` 파일을 읽지/쓰지 않음 → 격리 보장). FakeSession에 `cookies` 속성이 없어도 `session_file=False`면 save/load가 건너뛰어 안전. (이 한 줄 추가가 회귀의 핵심 — 빠뜨리면 실제 세션파일 존재 시 `authenticate`가 reuse 경로를 타서 기존 테스트가 깨짐.)

- [ ] **Step 5: GREEN** — `python3.11 -m pytest tests/test_wqb_api_session.py tests/test_wqb_api.py -v` 둘 다 pass; 그 다음 full `python3.11 -m pytest -q` 무회귀.

- [ ] **Step 6: Stage** — `git add server/wqb_api.py tests/test_wqb_api.py tests/test_wqb_api_session.py`.

---

## Task 2: Persona 감지 + pending 세션 + complete_persona

**Files:** Modify `server/wqb_api.py`; `tests/test_wqb_api_session.py` (추가)

**Interfaces:**
- Consumes: Task 1 client.
- Produces: `authenticate()` sets `self.persona_url` and returns False with `self.persona_required=True` on 401+persona; `complete_persona()->bool`; `_extract_persona_url(resp, body)`; pending file `<session_file>.pending`.

- [ ] **Step 1: 실패 테스트**

```python
# tests/test_wqb_api_session.py (추가)
def test_authenticate_detects_persona_body_inquiry(tmp_path):
    sf=str(tmp_path/'s.pkl')
    sess=FakeSession()
    sess.queue[('POST','/authentication')]=[FakeResp(401,{'inquiry':'inq_X'},
                                            headers={'Content-Type':'application/json'})]
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    assert c.authenticate() is False
    assert c.persona_required is True
    assert 'inq_X' in (c.persona_url or '')
    assert os.path.exists(sf+'.pending')   # pending session saved

def test_authenticate_detects_persona_header(tmp_path):
    sf=str(tmp_path/'s.pkl')
    sess=FakeSession()
    sess.queue[('POST','/authentication')]=[FakeResp(401,{},
        headers={'WWW-Authenticate':'persona','Location':'/authentication/persona?inquiry=inq_H'})]
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    assert c.authenticate() is False and c.persona_required is True
    assert 'inq_H' in (c.persona_url or '')

def test_complete_persona_saves_session(tmp_path):
    sf=str(tmp_path/'s.pkl')
    # seed a pending session
    sess=FakeSession(); sess.cookies=FakeCookies({'pre':'1'})
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_X')
    # finalize: POST /authentication/persona then GET /authentication shows user
    sess.queue[('POST','/authentication/persona')]=[FakeResp(200,{'ok':True})]
    sess.queue[('GET','/authentication')]=[FakeResp(200,{'user':{'id':'u'}})]
    sess.cookies=FakeCookies({'t':'JWT_AFTER'})
    assert c.complete_persona() is True
    assert os.path.exists(sf) and not os.path.exists(sf+'.pending')
```

- [ ] **Step 2: RED**.

- [ ] **Step 3: 구현** (server/wqb_api.py)

```python
    # __init__: add self.persona_required=False
    @staticmethod
    def _extract_persona_url(resp, body):
        try:
            from urllib.parse import urljoin
            if resp.status_code==401 and (resp.headers.get('WWW-Authenticate') or '').lower()=='persona':
                loc=resp.headers.get('Location')
                if loc: return urljoin(f'{BASE}/authentication', loc)
            inq=(body or {}).get('inquiry') if isinstance(body,dict) else None
            if inq: return f'{BASE}/authentication/persona?inquiry={inq}'
        except Exception: pass
        return None

    def _pending_file(self): return (self.session_file + '.pending') if self.session_file else None

    def _save_pending(self, persona_url):
        self.persona_url=persona_url; self.persona_required=True   # always signal, even if disabled
        try:
            pf=self._pending_file()
            if not pf: return   # persistence disabled — still signal above
            ck=self.session.cookies
            d=ck.get_dict() if hasattr(ck,'get_dict') else dict(ck)
            os.makedirs(os.path.dirname(pf), mode=0o700, exist_ok=True)
            try: os.chmod(os.path.dirname(pf), 0o700)
            except OSError: pass
            fd=os.open(pf, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)   # owner-only (holds pre-auth cookies)
            with os.fdopen(fd,'w') as f:
                json.dump({'cookies':d,'persona_url':persona_url}, f)
        except Exception as e:
            LOG.warning('pending save err: %s', e)

    # in authenticate(), replace the trailing "LOG.warning failed; return False" with:
        persona=self._extract_persona_url(r, body)
        if persona:
            self._save_pending(persona)
            LOG.warning('WQB persona/biometric required: %s', persona)
            return False
        LOG.warning('authenticate failed: HTTP %s', r.status_code)
        return False

    def complete_persona(self) -> bool:
        try:
            if not os.path.exists(self._pending_file()): 
                # maybe biometric already done out-of-band → try fresh auth
                return self.authenticate()
            with open(self._pending_file(),'r') as f: pend=json.load(f)
            if not isinstance(pend, dict): return False
            self.session.cookies.update(pend.get('cookies') or {})
            url=pend.get('persona_url') or f'{BASE}/authentication/persona'
            # variant (a): POST .../authentication/persona with inquiry body
            try:
                inq=None
                if 'inquiry=' in url: inq=url.split('inquiry=')[-1]
                self.session.post(f'{BASE}/authentication/persona', json={'inquiry':inq}, timeout=30)
            except Exception: pass
            if self._session_valid():
                self._save_session(); self._clear_pending(); self.persona_required=False
                self._authed=True; return True
            # variant (b): re-POST /authentication
            r=self.session.post(f'{BASE}/authentication', timeout=30)
            if r.status_code in (200,201) and self._session_valid():
                self._save_session(); self._clear_pending(); self.persona_required=False
                self._authed=True; return True
            return False
        except Exception as e:
            LOG.warning('complete_persona err: %s', e); return False

    def _clear_pending(self):
        try:
            if os.path.exists(self._pending_file()): os.remove(self._pending_file())
        except OSError: pass
```

- [ ] **Step 4: GREEN** — new tests pass; full suite no regression.
- [ ] **Step 5: Stage** — `git add server/wqb_api.py tests/test_wqb_api_session.py`.

---

## Task 3: auth.validate_wqb_api persona reason

**Files:** Modify `server/auth.py`; `tests/test_auth_account_type.py` (추가)

- [ ] **Step 1: 실패 테스트**

```python
# tests/test_auth_account_type.py (추가)
def test_validate_wqb_api_persona(monkeypatch):
    class R:
        status_code=401
        headers={'WWW-Authenticate':'persona','Content-Type':'application/json'}
        text=''
        def json(self): return {'inquiry':'inq_Z'}
    monkeypatch.setattr(auth, '_api_post_auth', lambda u,p: R())
    r=auth.validate_wqb_api('e','p')
    assert r['ok'] is False and r['reason']=='wqb_persona_required'
    assert 'inq_Z' in r.get('persona_url','')
```

- [ ] **Step 2: RED**.
- [ ] **Step 3: 구현** — in `validate_wqb_api`, before the generic 401 branch, detect persona:

```python
    # after getting r:
    body = {}
    try:
        if (r.headers.get('Content-Type','') or '').startswith('application/json'): body=r.json()
    except Exception: body={}
    is_persona = (r.status_code==401 and (r.headers.get('WWW-Authenticate') or '').lower()=='persona') \
                 or (isinstance(body,dict) and bool(body.get('inquiry')))
    if is_persona:
        inq = body.get('inquiry') if isinstance(body,dict) else None
        loc = r.headers.get('Location')
        url = (f'{_WQB_API_BASE}/authentication{loc}' if loc and loc.startswith('/') else
               (f'{_WQB_API_BASE}/authentication/persona?inquiry={inq}' if inq else f'{_WQB_API_BASE}/authentication'))
        return {'ok': False, 'reason': 'wqb_persona_required',
                'detail': 'WQB biometric(Persona) 인증 필요 — 대시보드에서 1회 완료하세요.',
                'persona_url': url}
    if r.status_code==401:
        return {'ok': False, 'reason': 'wqb_credentials', 'detail': 'WQB API 401 — 자격증명 거절'}
    ...
```

- [ ] **Step 4: GREEN**; full suite.
- [ ] **Step 5: Stage** — `git add server/auth.py tests/test_auth_account_type.py`.

---

## Task 4: ApiBackend 계정별 session_file + persona graceful

**Files:** Modify `server/wqb_backend.py`; `tests/test_wqb_backend.py` (추가)

**Interfaces:**
- `ApiBackend.__init__` creates the client with a per-account `session_file` (default via `_default_session_file(username)`), so the worker and the dashboard share the same file for an account.
- When `authenticate()` fails AND `client.persona_required`, `simulate_batch` returns all-error results with `error_text='WQB biometric(Persona) 인증 필요 — 대시보드에서 완료'` and does the round quickly (no churn change needed; rounds already complete fast on auth fail).

- [ ] **Step 1: 실패 테스트**

```python
# tests/test_wqb_backend.py (추가)
def test_apibackend_persona_required_message():
    class PersonaClient(FakeClient):
        persona_required=True
        def authenticate(self): return False
    be=wb.ApiBackend('e','p',client=PersonaClient())
    res=be.simulate_batch([{'idx':1,'code':'x','desc':'','settings':{}}],wqb_username='e',wqb_password='p')
    assert res[0]['mode']=='error' and 'Persona' in res[0]['error_text']
```

- [ ] **Step 2: RED**.
- [ ] **Step 3: 구현** — in `ApiBackend.simulate_batch`, the auth-fail branch:

```python
        if not self._client.authenticate():
            msg = ('WQB biometric(Persona) 인증 필요 — 대시보드에서 완료'
                   if getattr(self._client, 'persona_required', False)
                   else 'WQB API 인증 실패 (RC 자격증명/권한 확인)')
            return [self._err(s, msg) for s in batch]
```

And `ApiBackend.__init__` default client gets the per-account session file:
```python
        self._client = client or wqb_api.WqbApiClient(username, password)  # uses _default_session_file(username)
```
(no change needed if WqbApiClient defaults session_file by email — confirm username==email here.)

- [ ] **Step 4: GREEN**; full suite.
- [ ] **Step 5: Stage** — `git add server/wqb_backend.py tests/test_wqb_backend.py`.

---

## Task 5: app.py persona 엔드포인트

**Files:** Modify `server/app.py`; `tests/test_app_account_type.py` (추가)

**Interfaces:**
- `GET /api/account/wqb-persona-status` → `{persona_required: bool, persona_url: str|''}` for the logged-in user (calls `auth.validate_wqb_api` with stored creds; if `wqb_persona_required` → returns url; if ok → persona_required False).
- `POST /api/account/wqb-persona-complete` → builds a `WqbApiClient` for the user's stored creds + the account session_file, calls `complete_persona()`; returns `{ok}`.

- [ ] **Step 1: 실패 테스트** (test client, monkeypatch `_current_user_id`, `_db.get_user_credentials`, `auth.validate_wqb_api`, and the client's `complete_persona`).

```python
def test_persona_status_and_complete(monkeypatch):
    import server.app as app_mod
    monkeypatch.setattr(app_mod,'_current_user_id',lambda: 2)
    monkeypatch.setattr(app_mod._db,'get_user_credentials',lambda uid:('e','p','g'))
    monkeypatch.setattr(app_mod._auth,'validate_wqb_api',
        lambda u,p:{'ok':False,'reason':'wqb_persona_required','persona_url':'https://x/persona?inquiry=Z'})
    cl=app_mod.app.test_client()
    r=cl.get('/api/account/wqb-persona-status'); j=r.get_json()
    assert j['persona_required'] is True and 'inquiry=Z' in j['persona_url']
    # complete: monkeypatch the client factory used by the endpoint
    import server.wqb_api as wqb_api
    class FakeCli:
        def __init__(self,*a,**k): pass
        def complete_persona(self): return True
    monkeypatch.setattr(wqb_api,'WqbApiClient',FakeCli)
    r2=cl.post('/api/account/wqb-persona-complete'); assert r2.get_json()['ok'] is True
```

- [ ] **Step 2: RED**.
- [ ] **Step 3: 구현** (app.py — follow existing route style, `_err`/`jsonify`/`_current_user_id`):

```python
@app.route('/api/account/wqb-persona-status', methods=['GET'])
def api_wqb_persona_status():
    uid=_current_user_id()
    if not uid: return _err('not_logged_in','로그인이 필요합니다',401)
    creds=_db.get_user_credentials(uid)
    if not creds: return _err('no_credentials','자격증명을 찾을 수 없습니다',400)
    u,p,_=creds
    v=_auth.validate_wqb_api(u,p)
    if v.get('reason')=='wqb_persona_required':
        return jsonify({'persona_required':True,'persona_url':v.get('persona_url','')})
    return jsonify({'persona_required':False,'persona_url':'','ok':bool(v.get('ok'))})

@app.route('/api/account/wqb-persona-complete', methods=['POST'])
def api_wqb_persona_complete():
    uid=_current_user_id()
    if not uid: return _err('not_logged_in','로그인이 필요합니다',401)
    creds=_db.get_user_credentials(uid)
    if not creds: return _err('no_credentials','자격증명을 찾을 수 없습니다',400)
    u,p,_=creds
    from .wqb_api import WqbApiClient
    ok=False
    try: ok=WqbApiClient(u,p).complete_persona()
    except Exception as e: return _err('persona_failed',f'완료 처리 실패: {e}',400)
    return jsonify({'ok':bool(ok)})
```

- [ ] **Step 4: GREEN**; full suite.
- [ ] **Step 5: Stage** — `git add server/app.py tests/test_app_account_type.py`.

---

## Task 6: 프론트 persona 플로우

**Files:** Modify `static/index.html`, `static/app.js`

- [ ] **Step 1: 구현** — 대시보드(또는 로그인 후 RC 계정)에서 persona 상태를 폴링·표시:
  - `index.html`: 숨김 배너 영역 추가:
    ```html
    <div id="persona-banner" hidden class="persona-banner">
      <p>이 RC 계정은 WQB biometric(Persona) 인증이 필요합니다.
        <a id="persona-link" href="#" target="_blank" rel="noopener">여기서 완료하기 ↗</a></p>
      <button type="button" id="btn-persona-complete">완료했습니다 — 세션 저장</button>
      <span id="persona-status"></span>
    </div>
    ```
  - `app.js`: 대시보드 진입 시(또는 RC 계정일 때) `GET /api/account/wqb-persona-status`; `persona_required`면 배너 표시 + `#persona-link.href=persona_url`. `#btn-persona-complete` 클릭 → `POST /api/account/wqb-persona-complete`(try/catch) → ok면 배너 숨김+"인증 완료" / 실패면 안내. (기존 `api()` 헬퍼 반환형에 맞춰 `r.data`.)

- [ ] **Step 2: 수동 확인** — 서버 기동 후 RC 계정에서 배너·링크·완료버튼 동작(라이브 persona는 Task 7에서).
- [ ] **Step 3: Stage** — `git add static/index.html static/app.js`.

---

## Task 7: 통합 + 라이브 검증 (사장)

**Files:** (코드 변경 없으면 검증만) — gitignore 확인.

- [ ] **Step 1: gitignore** — `git check-ignore data/wqb_sessions/x.pkl` → IGNORED (data/ 하위). 아니면 `.gitignore`에 추가.
- [ ] **Step 2: 전체 회귀** — `python3.11 -m pytest -q` 전부 pass.
- [ ] **Step 3: 라이브 (사장)** —
  1. 서버 재시작.
  2. RC 계정(platinumcasillas) 대시보드 → persona 배너 등장 확인 → '여기서 완료하기' 클릭 → 브라우저에서 biometric 완료 → '완료했습니다' 클릭 → 세션 저장.
  3. RC 워커 시작 → **이번엔 인증 통과**(저장 세션 재사용, `/authentication` POST 없음) → 실제 시뮬·채점 2사이클 관찰.
  4. 서버 재시작 후에도 biometric 재요구 없이 워커가 재사용하는지 확인(=핵심 우회 검증). 토큰 만료 시에만 배너 재등장.
- [ ] **Step 4: Stage/commit** — 사장 승인 시.

---

## Self-Review
- Spec→Task: 세션지속 T1, persona+complete T2, auth reason T3, ApiBackend 배선+graceful T4, app 엔드포인트 T5, 프론트 T6, 통합/검증 T7. 전부 매핑.
- Placeholder: 코드/테스트 실제 포함. persona finalize 정확형은 §live-confirm로 방어적 구현 + 사장 1회 확인(placeholder 아님).
- Type/이름 일관: `session_file`, `persona_required`, `persona_url`, reason `wqb_persona_required`, `complete_persona` — T1~T7 일관.
- 위험: 기존 test_wqb_api.py가 session_file 기본경로로 `_load_session`(파일부재→False) 후 POST 경로 타는지 T1 Step4에서 확인 필수.
