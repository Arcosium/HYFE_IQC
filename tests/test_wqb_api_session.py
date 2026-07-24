# tests/test_wqb_api_session.py
import stat, os, time, json
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
    def clear(self): self._d.clear()
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
    # TTL 이 지난 challenge 여야 authenticate() 가 POST 까지 간다 — 살아있는 challenge 는
    # 재사용되고 새로 발급되지 않는다(test_authenticate_reuses_live_pending_challenge).
    old = time.time() - wqb_api._PERSONA_PENDING_TTL_S - 60
    os.utime(sf + '.pending', (old, old))

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


def test_pending_persona_passive_mode_never_resolves(tmp_path, monkeypatch):
    """resolve=False 는 `.pending` 파일만 읽는다 — WQB 로 나가는 호출이 0건이어야 한다.

    링크 해석(`GET /authentication/persona?inquiry=…`)은 inquiry 를 재개시켜 직전 Persona
    세션을 무효화한다. 60초 폴링(watch)·대시보드 진입(status)이 이걸 부르면 사용자가 열어 둔
    인증 페이지가 계속 죽어 무한 새로고침 → 'session expired' 로 끝난다(사장 보고 2026-07-10).
    """
    def _boom(*a, **k):
        raise AssertionError('passive 조회가 링크를 해석하려 했다 (inquiry 재개 → 세션 무효화)')
    monkeypatch.setattr(wqb_api, '_public_persona_url', _boom)

    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_PASSIVE')
    sess.calls.clear()

    pend = c.pending_persona(resolve=False)

    # 'source' 는 2026-07-16 신설 — 선제갱신 마커 구분용. 일반 pending 은 빈 문자열.
    assert pend == {'persona_url': '', 'inquiry': 'inq_PASSIVE', 'source': ''}
    assert sess.calls == []
    assert os.path.exists(sf + '.pending')   # passive 조회는 challenge 를 지우지도 않는다


# ── authenticate(): 살아있는 challenge 를 새 것으로 갈아치우지 않는다 ──────────
# POST /authentication 은 매번 새 inquiry 를 만들고 WQB 는 직전 inquiry 를 폐기한다.
# 사용자가 그 링크로 인증하는 중이었으면 페이지가 그 자리에서 죽는다. 실측 2026-07-10:
# 데이터 새로고침과 워커가 2초 간격으로 각각 POST 해 inquiry 가 두 번 갈렸다.

def test_authenticate_reuses_live_pending_challenge(tmp_path):
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_LIVE')
    sess.calls.clear()

    assert c.authenticate() is False
    assert sess.calls == []                       # POST /authentication 이 나가면 안 된다
    assert c.persona_required is True
    assert 'inq_LIVE' in c.persona_url
    with open(sf + '.pending') as f:              # challenge 는 그대로 살아 있다
        assert 'inq_LIVE' in json.load(f)['persona_url']


def test_authenticate_replaces_pending_older_than_ttl(tmp_path, monkeypatch):
    """TTL 이 지난 challenge 는 죽었다고 보고 새로 발급한다 — 영구 교착 방지."""
    monkeypatch.setattr(wqb_api, '_PERSONA_PENDING_TTL_S', 60.0)
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    sess.queue[('POST', '/authentication')] = [
        FakeResp(401, {'inquiry': 'inq_NEW'},
                 {'WWW-Authenticate': 'persona', 'Content-Type': 'application/json'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_STALE')
    os.utime(sf + '.pending', (time.time() - 3600, time.time() - 3600))
    sess.calls.clear()

    assert c.authenticate() is False
    assert ('POST', '/authentication') in sess.calls
    with open(sf + '.pending') as f:
        assert 'inq_NEW' in json.load(f)['persona_url']


def test_authenticate_clears_stale_cookies_before_minting(tmp_path):
    """발급 직전 쿠키 항아리는 반드시 비어 있어야 한다 (2026-07-08 stale-URL 루프 재발 방지).

    `.pkl` 이 없어도 세션에 쿠키가 실려 있을 수 있다 — `/wqb-persona-link` 가 죽은 challenge 를
    해석하려다 `pending_persona(resolve=True)` 로 pending 쿠키를 세션에 넣은 직후가 그렇다.
    그 쿠키를 든 채 POST 하면 WQB 가 그 쿠키에 묶인 낡은 inquiry 를 그대로 재발급한다.
    """
    sf = str(tmp_path / 's.pkl')

    class CapturingSession(FakeSession):
        cookies_at_post = None
        def post(self, url, **k):
            self.cookies_at_post = self.cookies.get_dict()
            return super().post(url, **k)

    sess = CapturingSession()
    sess.queue[('POST', '/authentication')] = [
        FakeResp(401, {'inquiry': 'inq_FRESH'},
                 {'WWW-Authenticate': 'persona', 'Content-Type': 'application/json'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    c.session.cookies.update({'stale': 'CHALLENGE'})   # pending_persona(resolve=True) 가 실어 둔 쿠키

    assert c.authenticate() is False
    assert sess.cookies_at_post == {}, sess.cookies_at_post
    with open(sf + '.pending') as f:
        assert 'inq_FRESH' in json.load(f)['persona_url']


def test_authenticate_mints_challenge_when_none_pending(tmp_path):
    sf = str(tmp_path / 's.pkl')
    sess = FakeSession()
    sess.queue[('POST', '/authentication')] = [
        FakeResp(401, {'inquiry': 'inq_FIRST'},
                 {'WWW-Authenticate': 'persona', 'Content-Type': 'application/json'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)

    assert c.authenticate() is False
    assert ('POST', '/authentication') in sess.calls
    assert c.persona_required is True
    with open(sf + '.pending') as f:
        assert 'inq_FIRST' in json.load(f)['persona_url']


def test_pending_persona_returns_none_when_absent(tmp_path):
    sf = str(tmp_path / 's.pkl')
    c = wqb_api.WqbApiClient('e', 'p', session=FakeSession(), session_file=sf)
    assert c.pending_persona() is None


# ── #1 OPTIONS 헬스체크 + token.expiry 선제갱신 ──────────────────────────────
# OPTIONS /simulations 로 인증 엔드포인트를 건드리지 않고 세션 유효성만 확인한다
# (Persona/biometric throttle 재무장 방지). 만료를 사이드카(.meta)로 캐시해 만료 여유가
# 크면 네트워크 검증조차 건너뛴다(fast-path).

class OptSession(FakeSession):
    """OPTIONS 지원 FakeSession — 프로덕션(OPTIONS 가용) 경로 재현."""
    def options(self, url, **k): return self._resp('OPTIONS', url)


def test_session_valid_uses_options_not_authentication(tmp_path):
    sess = OptSession()
    sess.queue[('OPTIONS', '/simulations')] = [FakeResp(200)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=str(tmp_path / 's.pkl'))
    assert c._session_valid() is True
    assert ('OPTIONS', '/simulations') in sess.calls
    assert ('GET', '/authentication') not in sess.calls   # 인증 엔드포인트 무접촉
    assert ('POST', '/authentication') not in sess.calls


def test_session_valid_options_401_is_invalid_without_get_fallback(tmp_path):
    sess = OptSession()
    sess.queue[('OPTIONS', '/simulations')] = [FakeResp(401)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=str(tmp_path / 's.pkl'))
    assert c._session_valid() is False
    assert ('GET', '/authentication') not in sess.calls   # 401 은 확정 → GET 폴백 안 함


def test_session_valid_ambiguous_options_falls_back_to_get(tmp_path):
    sess = OptSession()
    sess.queue[('OPTIONS', '/simulations')] = [FakeResp(503)]   # 애매 → GET 폴백
    sess.queue[('GET', '/authentication')] = [FakeResp(200, {'user': {'id': 'u'}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=str(tmp_path / 's.pkl'))
    assert c._session_valid() is True
    assert ('GET', '/authentication') in sess.calls


def test_authenticate_reuse_via_options_learns_and_persists_expiry(tmp_path):
    sf = str(tmp_path / 's.pkl')
    seed = OptSession(); seed.cookies = FakeCookies({'t': 'JWT'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_session()
    sess = OptSession()
    sess.queue[('OPTIONS', '/simulations')] = [FakeResp(200)]                       # 유효
    sess.queue[('GET', '/authentication')] = [FakeResp(200, {'user': {'id': 'u'},
                                              'token': {'expiry': 14400}})]          # 만료 학습(1회 GET)
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    assert c.authenticate() is True
    assert ('POST', '/authentication') not in sess.calls                            # no biometric
    assert c._expiry_epoch is not None and c._expiry_epoch > time.time() + 3600
    assert os.path.exists(sf + '.meta')                                             # 사이드카 persist


def test_fast_path_skips_network_when_expiry_far(tmp_path):
    sess = OptSession()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=str(tmp_path / 's.pkl'))
    c._authed = True
    c._expiry_epoch = time.time() + 3600     # 15분 임계보다 훨씬 여유 → 무네트워크
    assert c.authenticate() is True
    assert sess.calls == []


def test_expiry_meta_roundtrip_across_clients(tmp_path):
    sf = str(tmp_path / 's.pkl')
    c1 = wqb_api.WqbApiClient('e', 'p', session=OptSession(), session_file=sf)
    c1._expiry_epoch = time.time() + 9999
    c1._save_meta()
    c2 = wqb_api.WqbApiClient('e', 'p', session=OptSession(), session_file=sf)
    c2._load_meta()
    assert c2._expiry_epoch is not None and c2._not_near_expiry() is True


def test_authenticate_clears_stale_cookies_before_reauth(tmp_path):
    """만료된 세션 쿠키를 로드한 뒤 POST 하면 WQB 가 낡은 inquiry 를 재발급하는 버그 수정:
    무효 세션이면 재인증 POST 전에 in-memory 쿠키를 비워 fresh inquiry 를 받게 한다."""
    sf = str(tmp_path / 's.pkl')
    seed = FakeSession(); seed.cookies = FakeCookies({'stale': 'OLD'})
    wqb_api.WqbApiClient('e', 'p', session=seed, session_file=sf)._save_session()
    sess = FakeSession()
    # OPTIONS 미지원(FakeSession) → GET /authentication 폴백이 401(무효) → 세션 invalid.
    sess.queue[('GET', '/authentication')] = [FakeResp(401, {'detail': 'unauthorized'})]
    # 재인증 POST → 새 persona inquiry.
    sess.queue[('POST', '/authentication')] = [FakeResp(401, {'inquiry': 'inq_FRESH'},
                                              headers={'Content-Type': 'application/json'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=sf)
    assert c.authenticate() is False
    assert c.persona_required is True
    assert 'inq_FRESH' in (c.persona_url or '')
    # 낡은 쿠키('stale')가 재인증 전에 비워졌어야 한다.
    assert 'stale' not in sess.cookies.get_dict()


def test_parse_expiry_relative_absolute_iso_and_garbage():
    now = time.time()
    rel = wqb_api._parse_expiry(3600)              # 상대 잔여초
    assert rel is not None and now + 3500 < rel < now + 3700
    ab = wqb_api._parse_expiry(now + 100000)       # 절대 epoch (> 컷오프)
    assert ab is not None and abs(ab - (now + 100000)) < 2
    iso = wqb_api._parse_expiry('2099-01-01T00:00:00Z')
    assert iso is not None and iso > now
    assert wqb_api._parse_expiry(None) is None
    assert wqb_api._parse_expiry('') is None
    assert wqb_api._parse_expiry(True) is None      # bool 은 만료값이 아님
    assert wqb_api._parse_expiry('nonsense') is None


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
