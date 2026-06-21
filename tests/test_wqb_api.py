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
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=30, sleep=lambda _: None)
    assert res['status'] == 'COMPLETE' and res['alpha'] == 'AB1'

def test_submit_rate_limited():
    sess = FakeSession()
    sess.queue[('POST', '/simulations')] = [FakeResp(429)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess); c._authed = True
    assert c.submit_simulation('rank(close)', {'region': 'USA', 'delay': 1}) == 'RATE_LIMITED'

def test_poll_respects_stop_event():
    import threading
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [FakeResp(200, {'progress': 0.1, 'status': None, 'alpha': None})] * 5
    ev = threading.Event(); ev.set()
    c = wqb_api.WqbApiClient('e', 'p', session=sess); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1', stop_event=ev, deadline_s=30)
    assert res['status'] == 'CANCELLED'
