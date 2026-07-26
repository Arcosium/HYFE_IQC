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


def test_harvest_alpha_ignores_auxiliary_checks():
    sess = FakeSession()
    sess.queue[('GET', '/alphas/AB1')] = [FakeResp(200, {
        'is': {'checks': [
            {'name': 'LOW_SHARPE', 'result': 'PASS', 'value': 2.1, 'limit': 1.25},
            {'name': 'HT_TURNOVER', 'result': 'PASS', 'value': 0.3, 'limit': 0.7},
            {'name': 'MATCHES_PYRAMID', 'result': 'PASS', 'value': None, 'limit': None},
        ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False)
    h = c.harvest_alpha('AB1')
    assert [x['name'] for x in h['is_status']['pass']] == ['LOW_SHARPE']


def test_submit_alpha_posts_alpha_submit_endpoint():
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(201, text='ok')]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is True and status == 'submitted'
    assert sess.calls[0][0] == 'POST' and sess.calls[0][1].endswith('/alphas/A1/submit')


def test_full_settings_accepts_snake_case_handling_keys():
    out = wqb_api.WqbApiClient._full_settings({
        'delay': 1, 'nan_handling': 'ON', 'unit_handling': 'OFF'
    })
    assert out['nanHandling'] == 'ON'
    assert out['unitHandling'] == 'OFF'



def test_submit_alpha_polls_retry_after_until_final_json(monkeypatch):
    sleeps = []
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: sleeps.append(s))
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(200, headers={'Retry-After': '0.5'})]
    sess.queue[('GET', '/alphas/A1/submit')] = [FakeResp(200, {'is': {'checks': [
        {'name': 'LOW_SHARPE', 'result': 'PASS'},
    ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is True and status == 'submitted'
    assert sleeps == [0.5]
    assert [call[0] for call in sess.calls] == ['POST', 'GET']


def test_submit_alpha_final_failed_checks_are_rejected(monkeypatch):
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: None)
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(200, headers={'Retry-After': '0.5'})]
    sess.queue[('GET', '/alphas/A1/submit')] = [FakeResp(200, {'is': {'checks': [
        {'name': 'SELF_CORRELATION', 'result': 'FAIL'},
    ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is False
    assert status.startswith('rejected:SELF_CORRELATION')


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


def test_submit_alpha_retries_429_with_retry_after_then_succeeds(monkeypatch):
    """제출 429(계정당 1건 슬롯)는 즉시 포기하지 않고 Retry-After 를 존중해 재시도한다."""
    sleeps = []
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: sleeps.append(s))
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [
        FakeResp(429, headers={'Retry-After': '2'}),
        FakeResp(201, text='ok'),
    ]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is True and status == 'submitted'
    assert sleeps == [2.0]
    assert [call[0] for call in sess.calls] == ['POST', 'POST']  # 재시도도 POST


def test_submit_alpha_429_gives_up_after_deadline(monkeypatch):
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: None)
    clk = {'t': 0.0}
    def _mono():
        clk['t'] += 100.0
        return clk['t']
    monkeypatch.setattr(wqb_api._time, 'monotonic', _mono)
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(429)] * 10
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1', deadline_s=150)
    assert ok is False
    assert status == 'submit_http_429: too_many_requests'


def test_submit_alpha_respects_stop_event():
    import threading
    ev = threading.Event(); ev.set()
    sess = FakeSession()  # 큐 비어있음 — 네트워크에 닿으면 KeyError 로 실패
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1', stop_event=ev)
    assert ok is False and status == 'submit_skipped:paused'
    assert sess.calls == []


def test_read_self_correlation_polls_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: sleeps.append(s))
    sess = FakeSession()
    sess.queue[('GET', '/alphas/A1/correlations/self')] = [
        FakeResp(200, headers={'Retry-After': '1'}),
        FakeResp(200, {'max': 0.42}),
    ]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    assert c.read_self_correlation('A1') == 0.42
    assert sleeps == [1.0]


def test_core_check_allowlist_and_bookkeeping_denylist():
    for nm in ('LOW_SHARPE', 'LOW_FITNESS', 'HIGH_TURNOVER', 'SELF_CORRELATION'):
        assert wqb_api._is_core_check(nm) is True
    for nm in ('HT_ANYTHING', 'MATCHES_PYRAMID', 'MATCHES_THEMES', ''):
        assert wqb_api._is_core_check(nm) is False
    # 처음 보는 이름은 (호환) core 로 세되 로깅만 한다.
    assert wqb_api._is_core_check('SOME_NEW_CHECK') is True


def test_submit_alpha_403_with_failed_checks_classified_as_rejected():
    """라이브 R84 관찰: 체크 미달 알파의 submit 은 403 + is.checks JSON 으로 거절된다.
    raw JSON 덤프가 아니라 rejected:<체크명> 으로 분류해야 한다."""
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(403, {
        'is': {'checks': [
            {'name': 'LOW_SHARPE', 'result': 'FAIL', 'limit': 1.58, 'value': -0.0},
            {'name': 'LOW_FITNESS', 'result': 'FAIL', 'limit': 1.0, 'value': -0.0},
            {'name': 'LOW_TURNOVER', 'result': 'PASS', 'limit': 0.01, 'value': 0.1},
        ]}})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is False
    assert status.startswith('rejected:LOW_SHARPE; LOW_FITNESS')
    assert 'http_403' in status


# ── 시뮬 폴링 재조정 (2026-07-21) ─────────────────────────────────────────────
# 라이브 에러의 75%(400건 중 299건)가 'sim TIMEOUT: poll deadline' 이었다 — WQB 실패가
# 아니라 우리가 720초에 포기한 것. 마감 상향 + Retry-After 존중 + 정체 감지로 고쳤다.

def test_poll_default_deadline_is_thirty_minutes():
    """기본 마감이 720초로 되돌아가면 타임아웃 폭증이 재발한다."""
    assert wqb_api._POLL_DEADLINE_S >= 1800


def test_poll_honours_retry_after_header():
    """BRAIN API 문서가 명시한 계약 — Retry-After 가 있으면 그만큼 쉰다.

    5초 고정 폴링은 30분×8알파면 라운드당 2880 GET 이라 "excessive load" 지침 위반이다.
    """
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [
        FakeResp(200, {'progress': 0.2, 'status': 'SIMULATING'}, headers={'Retry-After': '20'}),
        FakeResp(200, {'progress': 1.0, 'status': 'COMPLETE', 'alpha': 'AB1'}),
    ]
    slept = []
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=300, sleep=slept.append)
    assert res['status'] == 'COMPLETE'
    assert slept and slept[0] == 20.0, slept


def test_poll_retry_after_is_clamped():
    """서버가 0/음수/과대값을 줘도 폴링이 폭주하거나 멈추면 안 된다."""
    assert wqb_api._clamp_retry_after(0, 5.0) == 5.0
    assert wqb_api._clamp_retry_after(-3, 5.0) == 5.0
    assert wqb_api._clamp_retry_after(2, 5.0) == 5.0        # floor 아래는 floor
    assert wqb_api._clamp_retry_after(99999, 5.0) == wqb_api._POLL_RETRY_AFTER_MAX_S


def test_poll_gives_up_early_when_progress_stalls(monkeypatch):
    """진행률이 stall 시간 동안 안 움직이면 마감 전이라도 슬롯을 반환한다."""
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [
        FakeResp(200, {'progress': 0.1, 'status': 'SIMULATING'})] * 50
    sess.queue[('DELETE', '/simulations/SIM1')] = [FakeResp(204)]
    ticks = {'t': 0.0}

    def _mono():
        ticks['t'] += 60.0        # 호출마다 1분 전진
        return ticks['t']

    monkeypatch.setattr(wqb_api._time, 'monotonic', _mono)
    monkeypatch.setattr(wqb_api, '_POLL_STALL_S', 300.0)
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=100000, sleep=lambda _: None)
    assert res['status'] == 'TIMEOUT'
    assert 'no progress' in res['message'], res['message']


def test_poll_does_not_give_up_while_progress_advances(monkeypatch):
    """진행률이 움직이는 동안엔 stall 로 끊으면 안 된다 (느린 D0 시뮬 보호)."""
    sess = FakeSession()
    sess.queue[('GET', '/simulations/SIM1')] = [
        FakeResp(200, {'progress': p, 'status': 'SIMULATING'}) for p in (0.1, 0.2, 0.3, 0.4)
    ] + [FakeResp(200, {'progress': 1.0, 'status': 'COMPLETE', 'alpha': 'AB1'})]
    ticks = {'t': 0.0}

    def _mono():
        ticks['t'] += 200.0       # 호출마다 200초 — stall(300초) 문턱 근처지만 진행 중
        return ticks['t']

    monkeypatch.setattr(wqb_api._time, 'monotonic', _mono)
    monkeypatch.setattr(wqb_api, '_POLL_STALL_S', 300.0)
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    res = c.poll('https://api.worldquantbrain.com/simulations/SIM1',
                 deadline_s=100000, sleep=lambda _: None)
    assert res['status'] == 'COMPLETE'


# ── 404 맹점 (2026-07-27): 제출 완료 후 submit 리소스 소멸 → 폴링 GET 404 ──

def test_submit_alpha_404_on_poll_verified_as_submitted(monkeypatch):
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: None)
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(200, headers={'Retry-After': '0.5'})]
    sess.queue[('GET', '/alphas/A1/submit')] = [FakeResp(404)]
    # stage 실측 — 제출 완료 상태
    sess.queue[('GET', '/alphas/A1')] = [FakeResp(200, {'stage': 'OS',
                                                        'dateSubmitted': '2026-07-25T10:26:56-04:00'})]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is True and 'stage=OS' in status


def test_submit_alpha_404_on_poll_not_submitted_stays_failure(monkeypatch):
    monkeypatch.setattr(wqb_api._time, 'sleep', lambda s: None)
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(200, headers={'Retry-After': '0.5'})]
    sess.queue[('GET', '/alphas/A1/submit')] = [FakeResp(404)]
    sess.queue[('GET', '/alphas/A1')] = [FakeResp(200, {'stage': 'IS'})]   # 미제출
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is False and status.startswith('submit_http_404')


def test_submit_alpha_404_on_post_is_plain_failure():
    # POST 단계 404 = 알파 자체가 없음 — stage 검증 없이 실패 (GET 폴링 단계만 검증).
    sess = FakeSession()
    sess.queue[('POST', '/alphas/A1/submit')] = [FakeResp(404)]
    c = wqb_api.WqbApiClient('e', 'p', session=sess, session_file=False); c._authed = True
    ok, status = c.submit_alpha('A1')
    assert ok is False and status.startswith('submit_http_404')
