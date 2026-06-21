# tests/test_wqb_api_session.py
import stat, os
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
