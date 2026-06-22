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
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False)
    assert c.authenticate() is True

def test_harvest_alpha_maps_checks():
    sess = FakeSession()
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {
        'is': {'sharpe': 2.1, 'fitness': 1.4, 'turnover': 0.12,
               'checks': [
                   {'name': 'LOW_SHARPE', 'result': 'PASS', 'value': 2.1, 'limit': 1.25},
                   {'name': 'HIGH_TURNOVER', 'result': 'FAIL', 'value': 0.9, 'limit': 0.7},
               ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False)
    h = c.harvest_alpha('AB1')
    assert len(h['is_status']['pass']) == 1 and len(h['is_status']['fail']) == 1
    assert h['metrics']['sharpe'] == '2.1'


def test_harvest_alpha_check_items_are_str_contract():
    """is_status 항목의 value/cutoff 는 브라우저 스크레이퍼와 동일하게 **문자열**이어야 한다.
    워커 포맷 코드(_short_metric_label/_extract_self_corr_value)가 `.strip()` 을 호출하므로
    raw float 이면 `'float' object has no attribute 'strip'` 로 터진다(라이브 RC 회귀)."""
    sess = FakeSession()
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {
        'is': {'checks': [
            {'name': 'HIGH_TURNOVER', 'result': 'FAIL', 'value': 0.9, 'limit': 0.7},
            {'name': 'LOW_SHARPE', 'result': 'PASS', 'value': 2.1, 'limit': 1.25},
        ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False)
    h = c.harvest_alpha('AB1')
    for bucket in ('pass', 'fail'):
        for it in h['is_status'][bucket]:
            assert isinstance(it['value'], str), f"value not str: {it['value']!r}"
            assert isinstance(it['cutoff'], str), f"cutoff not str: {it['cutoff']!r}"
            # 실제 다운스트림 연산이 깨지지 않아야 한다.
            (it.get('value') or '').strip()
            (it.get('cutoff') or '').strip()
    fail0 = h['is_status']['fail'][0]
    assert fail0['value'] == '0.9' and fail0['cutoff'] == '0.7'


def test_harvest_alpha_none_value_becomes_empty_str():
    """value/limit 가 없는(None) 체크는 'None' 문자열이 아니라 빈 문자열이어야 한다."""
    sess = FakeSession()
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {
        'is': {'checks': [
            {'name': 'NO_VALUE', 'result': 'PASS', 'value': None, 'limit': None},
        ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False)
    h = c.harvest_alpha('AB1')
    it = h['is_status']['pass'][0]
    assert it['value'] == '' and it['cutoff'] == ''

def test_submit_returns_location():
    sess = FakeSession()
    sess.queue[('POST', '/simulations')] = [FakeResp(201, headers={'Location': 'https://api.worldquantbrain.com/simulations/SIM1'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    url = c.submit_simulation('rank(close)', {'region': 'USA', 'universe': 'TOP3000', 'delay': 1, 'neutralization': 'INDUSTRY'})
    assert url.endswith('/simulations/SIM1')

def test_poll_until_complete():
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [
        FakeResp(200, {'progress': 0.3, 'status': None, 'alpha': None}),
        FakeResp(200, {'progress': 1.0, 'status': 'COMPLETE', 'alpha': 'AB1'}),
    ]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=30, sleep=lambda _: None)
    assert res['status'] == 'COMPLETE' and res['alpha'] == 'AB1'

def test_submit_rate_limited():
    sess = FakeSession()
    sess.queue[('POST', '/simulations')] = [FakeResp(429)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    assert c.submit_simulation('rank(close)', {'region': 'USA', 'delay': 1}) == 'RATE_LIMITED'

def test_poll_respects_stop_event():
    import threading
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [FakeResp(200, {'progress': 0.1, 'status': None, 'alpha': None})] * 5
    ev = threading.Event(); ev.set()
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1', stop_event=ev, deadline_s=30)
    assert res['status'] == 'CANCELLED'


def test_sim_path_requests_pass_timeout():
    """무한 행 방지: 시뮬 경로(submit/poll/harvest/self-corr) requests 는 timeout 필수.
    라이브 회귀: poll 의 GET 에 timeout 이 없어 워커가 WQB 소켓에서 무한 대기(do_select)했다."""
    sess = FakeSession()
    sess.queue[('POST', '/simulations')] = [FakeResp(201, headers={'Location': 'https://api.worldquantbrain.com/simulations/SIM1'})]
    sess.queue[('GET', '/simulations/SIM1')] = [FakeResp(200, {'status': 'COMPLETE', 'alpha': 'AB1', 'progress': 1.0})]
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {'is': {'checks': []}})]
    sess.queue[('GET', '/alphas/AB1/correlations/self')] = [FakeResp(200, {'max': 0.3})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    url = c.submit_simulation('rank(close)', {'region': 'USA', 'delay': 1})
    c.poll(url, deadline_s=30, sleep=lambda _: None)
    c.harvest_alpha('AB1')
    c.read_self_correlation('AB1')
    sim_calls = [(m, u, kw) for (m, u, kw) in sess.calls if '/authentication' not in u]
    assert sim_calls, 'no sim-path calls captured'
    for (m, u, kw) in sim_calls:
        assert kw.get('timeout'), f'{m} {u} missing timeout: {kw}'


def test_poll_bounded_by_walltime_when_requests_stall(monkeypatch):
    """각 GET 이 (타임아웃 후) 즉시 예외로 떨어져도 poll 은 wall-clock deadline 안에 끝나야 한다.
    루프-카운트가 아니라 벽시계 기준이어야 무한 행이 안 난다."""
    import requests as _rq
    sess = FakeSession()
    class _Boom:
        def get(self, url, **kw): raise _rq.exceptions.Timeout('read timed out')
        def delete(self, url, **kw): pass
    c = wqb_api.WqbApiClient('e', 'p', session=_Boom(), session_file=False); c._authed = True
    # 가짜 단조시계: 호출마다 1초씩 전진 → deadline_s=3 이면 몇 번 안에 종료.
    ticks = {'t': 0.0}
    def _mono():
        ticks['t'] += 1.0
        return ticks['t']
    monkeypatch.setattr(wqb_api._time, 'monotonic', _mono)
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=3, interval_s=0.0, sleep=lambda _: None)
    assert res['status'] == 'TIMEOUT'
