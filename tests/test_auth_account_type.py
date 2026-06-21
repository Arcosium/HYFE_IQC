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
    r=auth.validate_wqb_api('e','p')
    assert r['ok'] is False and r['reason']=='wqb_persona_required'
    assert 'inq_Z' in r.get('persona_url','')

def test_validate_login_routes_rc(monkeypatch):
    monkeypatch.setattr(auth, 'validate_gemini_key', lambda k: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_api', lambda u, p: {'ok': True, 'reason': 'ok'})
    monkeypatch.setattr(auth, 'validate_wqb_login', lambda u, p: {'ok': False, 'reason': 'should_not_call'})
    assert auth.validate_login('e', 'p', 'g', account_type='research_consultant')['ok'] is True
