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

def test_validate_wqb_api_persona(monkeypatch):
    class R:
        status_code=401
        headers={'WWW-Authenticate':'persona','Content-Type':'application/json'}
        text=''
        def json(self): return {'inquiry':'inq_Z'}
    monkeypatch.setattr(auth, '_api_post_auth', lambda u,p: R())
    monkeypatch.setattr(auth, '_resolve_persona_url', lambda u: u)  # no network in tests
    r=auth.validate_wqb_api('e','p')
    assert r['ok'] is False and r['reason']=='wqb_persona_required'
    assert 'inq_Z' in r.get('persona_url','')
    # inquiry field must be present so the frontend can pass it to the finalize call
    assert r.get('inquiry') == 'inq_Z'


def test_validate_wqb_api_persona_location_no_double_path(monkeypatch):
    # Location is root-relative and already includes '/authentication/...'.
    # Regression: must NOT become '.../authentication/authentication/persona' (404).
    class R:
        status_code=401
        headers={'WWW-Authenticate':'persona', 'Content-Type':'application/json',
                 'Location':'/authentication/persona?inquiry=inq_Z'}
        text=''
        def json(self): return {'inquiry':'inq_Z'}
    monkeypatch.setattr(auth, '_api_post_auth', lambda u,p: R())
    monkeypatch.setattr(auth, '_resolve_persona_url', lambda u: u)
    r=auth.validate_wqb_api('e','p')
    assert r['persona_url'] == 'https://api.worldquantbrain.com/authentication/persona?inquiry=inq_Z'
    assert '/authentication/authentication' not in r['persona_url']


def test_resolve_persona_url_follows_302_to_withpersona(monkeypatch):
    class RR:
        status_code=302
        headers={'Location':'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_Z'}
    monkeypatch.setattr(auth._requests, 'get', lambda url, **k: RR())
    out=auth._resolve_persona_url('https://api.worldquantbrain.com/authentication/persona?inquiry=inq_Z')
    assert out == 'https://worldquantbrain.withpersona.com/verify?inquiry-id=inq_Z'

def test_resolve_persona_url_falls_back_on_error(monkeypatch):
    def boom(url, **k): raise RuntimeError('net down')
    monkeypatch.setattr(auth._requests, 'get', boom)
    api='https://api.worldquantbrain.com/authentication/persona?inquiry=inq_Z'
    assert auth._resolve_persona_url(api) == api

def test_validate_login_routes_rc(monkeypatch):
    monkeypatch.setattr(auth, 'validate_gemini_key', lambda k: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_login', lambda u, p: {'ok': False, 'reason': 'should_not_call'})
    assert auth.validate_login('e', 'p', 'g', account_type='research_consultant')['ok'] is True


def test_validate_wqb_api_rate_limited(monkeypatch):
    """`_api_post_auth` 가 429 반환 시 reason=wqb_rate_limited 이어야 한다."""
    class R:
        status_code = 429
        text = ''
        headers = {'X-RateLimit-Limit-Minute': '5'}
        def json(self): return {}
    monkeypatch.setattr(auth, '_api_post_auth', lambda u, p: R())
    r = auth.validate_wqb_api('e', 'p')
    assert r['ok'] is False
    assert r['reason'] == 'wqb_rate_limited', f"got reason={r['reason']!r}"
    assert 'detail' in r
