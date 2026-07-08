# tests/test_wqb_api_session.py
import stat, os
import pytest
import server.wqb_api as wqb_api


@pytest.fixture(autouse=True)
def no_persona_url_network(monkeypatch):
    def fake_public_url(u, session=None):
        return u.replace('https://api.worldquantbrain.com/authentication/persona?inquiry=',
                         'https://worldquantbrain.withpersona.com/verify?inquiry-id=')
    monkeypatch.setattr(wqb_api, '_public_persona_url', fake_public_url)

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

def test_session_file_is_owner_only_0600(tmp_path):
    sf = str(tmp_path / 's.json')
    sess = FakeSession(); sess.cookies = FakeCookies({'t': 'JWT123'})
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    assert c._save_session() is True
    mode = stat.S_IMODE(os.stat(sf).st_mode)
    assert mode == 0o600, oct(mode)

def test_load_rejects_non_dict_json(tmp_path):
    sf = str(tmp_path / 's.json')
    with open(sf, 'w') as f: f.write('["not", "a", "dict"]')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    assert c._load_session() is False

def test_load_rejects_corrupt_json(tmp_path):
    sf = str(tmp_path / 's.json')
    with open(sf, 'w') as f: f.write('{not valid json')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    assert c._load_session() is False

def test_authenticate_reuses_valid_saved_session_no_post(tmp_path):
    sf=str(tmp_path/'s.pkl')
    seed=FakeSession(); seed.cookies=FakeCookies({'t':'JWT'})
    wqb_api.WqbApiClient('e','p',session=seed,session_file=sf)._save_session()
    sess=FakeSession()
    sess.queue[('GET','/authentication')]=[FakeResp(200,{'user':{'id':'u'}})]  # saved session valid
    c=wqb_api.WqbApiClient('e','p',session=sess,session_file=sf)
    assert c.authenticate() is True
    assert ('POST','/authentication') not in sess.calls   # never re-authed → no biometric

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

def test_extract_persona_url_returns_refreshable_wqb_api_url(monkeypatch):
    seen = []
    monkeypatch.setattr(wqb_api, '_public_persona_url', lambda u, session=None: seen.append((u, session)) or 'unused')
    resp = FakeResp(401, {'inquiry': 'inq_X'}, headers={'Content-Type': 'application/json'})

    assert wqb_api.WqbApiClient._extract_persona_url(resp, resp.json()) == \
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_X'
    assert seen == []

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


def test_complete_persona_with_inquiry_posts_finalize(tmp_path):
    """complete_persona(inquiry=...) must POST /authentication/persona with that
    inquiry and, on 200, save session + clear pending + set _authed."""
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    sess.cookies = FakeCookies({'t': 'JWT'})
    # Queue a 200 for the finalize POST
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(200, {'user': {}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    result = c.complete_persona(inquiry='inq_X')
    assert result is True
    # session file must be written
    assert os.path.exists(sf)
    # _authed must be set
    assert c._authed is True
    assert c.persona_required is False
    # POST /authentication/persona must have been called
    assert ('POST', '/authentication/persona') in sess.calls


def test_complete_persona_inquiry_incomplete_returns_false(tmp_path):
    """If the finalize POST returns 403 INQUIRY_INCOMPLETE (biometric not done),
    complete_persona must return False and NOT write a session file."""
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    sess.cookies = FakeCookies({'t': 'JWT'})
    sess.queue[('POST', '/authentication/persona')] = [
        FakeResp(403, {'detail': 'INQUIRY_INCOMPLETE'})
    ]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    result = c.complete_persona(inquiry='inq_Y')
    assert result is False
    # session file must NOT be created on failure
    assert not os.path.exists(sf)


def test_complete_persona_restores_pending_cookies(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_COOKIE')

    sess = FakeSession()
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(200, {'ok': True})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.complete_persona() is True
    assert sess.cookies.get_dict().get('pre') == 'COOKIE'

def test_complete_persona_with_explicit_inquiry_restores_pending_cookies(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_COOKIE')

    sess = FakeSession()
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(200, {'ok': True})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.complete_persona(inquiry='inq_COOKIE') is True
    assert sess.cookies.get_dict().get('pre') == 'COOKIE'

def test_complete_persona_reads_inquiry_id_from_persona_verify_url(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_VERIFY')

    sess = FakeSession()
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(200, {'ok': True})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.complete_persona() is True
    assert ('POST', '/authentication/persona') in sess.calls


def test_complete_persona_410_requires_valid_session_and_clears_stale_pending(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_GONE')

    sess = FakeSession()
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(410, {'detail': 'Gone'})]
    sess.queue[('GET', '/authentication')] = [FakeResp(401, {'detail': 'Unauthorized'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.complete_persona() is False
    assert not os.path.exists(sf + '.pending')
    assert not os.path.exists(sf)


def test_complete_persona_410_succeeds_only_after_session_validation(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_DONE')

    sess = FakeSession(); sess.cookies = FakeCookies({'t': 'JWT_AFTER'})
    sess.queue[('POST', '/authentication/persona')] = [FakeResp(410, {'detail': 'Gone'})]
    sess.queue[('GET', '/authentication')] = [FakeResp(200, {'user': {'id': 'u'}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.complete_persona() is True
    assert c._authed is True
    assert os.path.exists(sf) and not os.path.exists(sf + '.pending')


def test_authenticate_410_requires_valid_session(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'pre': 'COOKIE'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_pending(
        'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_AUTH_GONE')

    sess = FakeSession()
    sess.queue[('POST', '/authentication')] = [FakeResp(410, {'detail': 'Gone'})]
    sess.queue[('GET', '/authentication')] = [FakeResp(401, {'detail': 'Unauthorized'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.authenticate() is False
    assert not os.path.exists(sf + '.pending')
    assert c._authed is False


# ── pending_persona(): 저장된 미완료 challenge 를 네트워크 호출 없이 읽는다 ──
# (passive 상태조회가 POST /authentication 으로 biometric throttle 를 재무장시키던
#  버그의 근본 수정: 상태조회는 이 파일만 읽고 절대 POST 하지 않는다.)

def test_pending_persona_reads_saved_challenge_no_network(tmp_path):
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_PEND')
    sess.calls.clear()  # forget the save; we only care that READ makes no calls
    pend = c.pending_persona()
    assert pend is not None
    assert pend['persona_url'].startswith('https://worldquantbrain.withpersona.com/')
    assert pend['persona_url'].endswith('inquiry-id=inq_PEND')
    assert pend['inquiry'] == 'inq_PEND'
    # crucial: reading the pending challenge must NOT touch the network
    assert sess.calls == []


def test_pending_persona_clears_unresolvable_api_challenge(tmp_path, monkeypatch):
    monkeypatch.setattr(wqb_api, '_public_persona_url', lambda u, session=None: u)
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_GONE')

    assert c.pending_persona() is None
    assert not os.path.exists(sf + '.pending')


def test_pending_persona_clears_legacy_public_persona_url(tmp_path):
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_OLD')

    assert c.pending_persona() is None
    assert not os.path.exists(sf + '.pending')

def test_pending_persona_returns_browser_url_for_saved_api_challenge(tmp_path, monkeypatch):
    public = 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_PEND'
    monkeypatch.setattr(wqb_api, '_public_persona_url', lambda u, session=None: public)
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_PEND')

    pend = c.pending_persona()

    assert pend == {'persona_url': public, 'inquiry': 'inq_PEND'}


def test_pending_persona_returns_none_when_absent(tmp_path):
    sf = str(tmp_path / 's.pkl')
    c = wqb_api.WqbApiClient('e', 'p', session=FakeSession(), session_file=sf)
    assert c.pending_persona() is None


def test_pending_persona_keeps_challenge_on_transient_resolution_failure(tmp_path, monkeypatch):
    """URL 해석이 **일시** 실패(_public_persona_url → None)하면 pending 을 지우지 않고
    빈 URL 로 반환한다 — 지우면 다음 상태조회가 POST /authentication 을 다시 때려
    biometric throttle 이 재무장되는 루프(Details:Gone 재발)가 되기 때문."""
    monkeypatch.setattr(wqb_api, '_public_persona_url', lambda u, session=None: None)
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_TMP')

    pend = c.pending_persona()
    assert pend is not None
    assert pend['persona_url'] == ''          # UI 는 '준비 중' 표시
    assert pend['inquiry'] == 'inq_TMP'
    assert os.path.exists(sf + '.pending')    # challenge 보존
