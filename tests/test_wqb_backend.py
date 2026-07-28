# tests/test_wqb_backend.py
import threading
import time as _time
import server.wqb_backend as wb

EXPECTED_KEYS = {
    'idx', 'code', 'desc', 'pass_count', 'pass_items',
    'fail_count', 'fail_items', 'submitted', 'submit_status',
    'error_text', 'metrics', 'is_status', 'mode',
}


class FakeClient:
    def __init__(self, *a, **k):
        self.submit_calls = []
        self.alpha_submit_calls = []

    def authenticate(self): return True

    def submit_simulation(self, expr, settings):
        self.submit_calls.append(expr)
        return 'https://api.worldquantbrain.com/simulations/SIM_' + expr[:3]

    def poll(self, url, stop_event=None, **k):
        return {'status': 'COMPLETE', 'alpha': 'A_' + url[-3:], 'message': '', 'progress': 1.0}

    def harvest_alpha(self, aid):
        return {'metrics': {'sharpe': '2.0'},
                'is_status': {'pass': [{'name': 'x'}] * 7, 'fail': [], 'error': [], 'pending': []}}

    def read_self_correlation(self, aid): return 0.3

    def submit_alpha(self, aid, **kw):
        self.alpha_submit_calls.append(aid)
        return True, 'submitted'

    def cancel(self, url): pass


def test_simulate_batch_contract():
    seen = []
    be = wb.ApiBackend('e', 'p', client=FakeClient())
    batch = [{'idx': 1, 'code': 'rank(close)', 'desc': 'd', 'settings': {'region': 'USA', 'delay': 1}}]
    res = be.simulate_batch(batch, wqb_username='e', wqb_password='p',
                            partial_fn=lambda o: seen.append(o), forced_delay=1)
    r0 = res[0]
    assert r0['idx'] == 1 and r0['pass_count'] == 7 and r0['error_text'] == ''
    assert set(r0) == EXPECTED_KEYS
    assert r0['mode'] == 'pass'
    assert r0['submitted'] is True and r0['submit_status'] == 'submitted'
    assert seen and seen[0]['idx'] == 1 and seen[0]['status'] == 'pass'
    assert seen[0]['submitted'] is True and seen[0]['submit_status'] == 'submitted'


def test_simulate_batch_error_status():
    class ErrClient(FakeClient):
        def poll(self, url, stop_event=None, **k):
            return {'status': 'ERROR', 'alpha': None, 'message': 'bad expr', 'progress': 0.1}

    be = wb.ApiBackend('e', 'p', client=ErrClient())
    res = be.simulate_batch([{'idx': 2, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'error' and 'bad expr' in res[0]['error_text']
    assert set(res[0]) == EXPECTED_KEYS


def test_simulate_batch_stop_event_aborts():
    import threading
    ev = threading.Event(); ev.set()
    client = FakeClient()
    be = wb.ApiBackend('e', 'p', client=client)
    res = be.simulate_batch([{'idx': 3, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p', stop_event=ev)
    assert res == []
    assert client.submit_calls == []  # aborted before submitting


def test_simulate_batch_fail_mode_requires_blocking_fail():
    """2026-07-21: 'PASS 갯수 부족' 은 더 이상 fail 이 아니다 — 차단 FAIL 이 있어야 fail.

    고회전 분류를 얻은 알파는 표준 컷이 WARNING 으로 강등돼 차단 PASS 가 4개까지
    줄어든다. 갯수로 판정하면 제출 가능한 알파를 실패로 기록하게 된다.
    """
    class SixPassClient(FakeClient):
        def harvest_alpha(self, aid):
            return {'metrics': {}, 'is_status': {'pass': [{'name': 'x'}] * 6, 'fail': [],
                                                 'error': [], 'pending': []}}

    be = wb.ApiBackend('e', 'p', client=SixPassClient())
    res = be.simulate_batch([{'idx': 9, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'pass' and res[0]['pass_count'] == 6
    assert res[0]['submitted'] is True and res[0]['submit_status'] == 'submitted'


def test_simulate_batch_fail_mode_on_blocking_fail():
    class FailClient(FakeClient):
        def harvest_alpha(self, aid):
            return {'metrics': {}, 'is_status': {'pass': [{'name': 'x'}] * 6,
                                                 'fail': [{'name': 'LOW_SHARPE'}],
                                                 'error': [], 'pending': []}}

    be = wb.ApiBackend('e', 'p', client=FailClient())
    res = be.simulate_batch([{'idx': 9, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'fail'


def test_simulate_batch_rate_limited(monkeypatch):
    # 지속적 429 → deadline 까지 인내심 재시도 끝에 포기하고 error.
    # sleep no-op + 가짜 시계(호출마다 100s 전진)로 deadline(600s) 을 빠르게 넘긴다.
    monkeypatch.setattr(wb._time, 'sleep', lambda *_: None)
    clk = {'t': 0.0}
    def _mono():
        clk['t'] += 100.0
        return clk['t']
    monkeypatch.setattr(wb._time, 'monotonic', _mono)

    class RateLimitedClient(FakeClient):
        def submit_simulation(self, expr, settings):
            self.submit_calls.append(expr)
            return 'RATE_LIMITED'

    rc = RateLimitedClient()
    be = wb.ApiBackend('e', 'p', client=rc)
    res = be.simulate_batch([{'idx': 4, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'error'
    assert 'CONCURRENT' in res[0]['error_text'] or '429' in res[0]['error_text']
    assert len(rc.submit_calls) >= 2  # 한 번에 포기 안 하고 재시도했다


def test_submit_retry_waits_for_slot_then_succeeds(monkeypatch):
    """429 가 여러 번 떠도(슬롯 대기) 슬롯이 빈 뒤엔 제출 성공해 알파가 통과해야 한다.
    (이게 핵심: 재시도 예산이 sim 시간보다 길어 8개를 던져도 웨이브로 전부 처리된다.)"""
    monkeypatch.setattr(wb._time, 'sleep', lambda *_: None)

    class SlotFreesClient(FakeClient):
        def __init__(self):
            super().__init__(); self.n = {}
        def submit_simulation(self, expr, settings):
            self.n[expr] = self.n.get(expr, 0) + 1
            if self.n[expr] <= 4:           # 처음 4번은 슬롯 없음
                return 'RATE_LIMITED'
            return 'https://api.worldquantbrain.com/simulations/SIM_' + expr[:3]

    be = wb.ApiBackend('e', 'p', client=SlotFreesClient(), concurrency=4)
    res = be.simulate_batch(_batch(3), wqb_username='e', wqb_password='p')
    assert all(r['mode'] == 'pass' for r in res), [r['error_text'] for r in res]


def test_rc_api_always_attempts_alpha_submit_after_completed_sim():
    class RejectSubmitClient(FakeClient):
        def submit_alpha(self, aid, **kw):
            self.alpha_submit_calls.append(aid)
            return False, 'submit_http_400: already submitted'

    client = RejectSubmitClient()
    be = wb.ApiBackend('e', 'p', client=client)
    res = be.simulate_batch([{'idx': 11, 'code': 'rank(close)', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert client.alpha_submit_calls, 'RC backend did not attempt /alphas/{id}/submit'
    assert res[0]['submitted'] is False
    assert res[0]['submit_status'].startswith('submit_http_400')


def test_apibackend_persona_required_message():
    class PersonaClient(FakeClient):
        persona_required = True
        def authenticate(self): return False

    be = wb.ApiBackend('e', 'p', client=PersonaClient())
    res = be.simulate_batch([{'idx': 1, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'error' and 'Persona' in res[0]['error_text']


# ── RC 동시 시뮬 (ThreadPool) ────────────────────────────────────────────

def _batch(n):
    return [{'idx': i, 'code': f'c{i}', 'desc': f'd{i}', 'settings': {'region': 'USA', 'delay': 1}}
            for i in range(1, n + 1)]


def test_simulate_batch_runs_concurrently():
    """concurrency=4, 알파 4개 → 4개가 동시에 poll 에 도달해야 Barrier 가 풀린다.
    순차였다면 첫 스레드가 Barrier 에서 영원히 대기 → timeout → 실패."""
    import threading
    barrier = threading.Barrier(4, timeout=5)
    peak = {'cur': 0, 'max': 0}
    lk = threading.Lock()

    class BarrierClient(FakeClient):
        def poll(self, url, stop_event=None, **k):
            with lk:
                peak['cur'] += 1; peak['max'] = max(peak['max'], peak['cur'])
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                return {'status': 'ERROR', 'alpha': None, 'message': 'not concurrent', 'progress': 0.1}
            with lk:
                peak['cur'] -= 1
            return {'status': 'COMPLETE', 'alpha': 'A_' + url[-3:], 'message': '', 'progress': 1.0}

    be = wb.ApiBackend('e', 'p', client=BarrierClient(), concurrency=4)
    res = be.simulate_batch(_batch(4), wqb_username='e', wqb_password='p')
    assert all(r['mode'] == 'pass' for r in res), [r['error_text'] for r in res]
    assert peak['max'] == 4


def test_simulate_batch_caps_concurrency():
    """concurrency=2, 알파 6개 → 동시 in-flight 가 2를 절대 넘지 않아야 한다."""
    import threading, time
    peak = {'cur': 0, 'max': 0}
    lk = threading.Lock()

    class SlowClient(FakeClient):
        def poll(self, url, stop_event=None, **k):
            with lk:
                peak['cur'] += 1; peak['max'] = max(peak['max'], peak['cur'])
            time.sleep(0.05)
            with lk:
                peak['cur'] -= 1
            return {'status': 'COMPLETE', 'alpha': 'A_' + url[-3:], 'message': '', 'progress': 1.0}

    be = wb.ApiBackend('e', 'p', client=SlowClient(), concurrency=2)
    res = be.simulate_batch(_batch(6), wqb_username='e', wqb_password='p')
    assert len(res) == 6 and all(r['mode'] == 'pass' for r in res)
    assert peak['max'] <= 2, f"max concurrent {peak['max']} exceeded cap 2"


def test_simulate_batch_preserves_order():
    """완료 순서가 뒤섞여도 결과는 batch(idx) 순서로 정렬돼 반환된다."""
    import time

    class ReverseSpeedClient(FakeClient):
        def poll(self, url, stop_event=None, **k):
            # 뒤 idx 가 먼저 끝나게: SIM_c1 가장 늦게.
            n = int(''.join(ch for ch in url if ch.isdigit()) or '0')
            time.sleep(max(0.0, (5 - n) * 0.02))
            return {'status': 'COMPLETE', 'alpha': 'A%d' % n, 'message': '', 'progress': 1.0}

    be = wb.ApiBackend('e', 'p', client=ReverseSpeedClient(), concurrency=4)
    res = be.simulate_batch(_batch(4), wqb_username='e', wqb_password='p')
    assert [r['idx'] for r in res] == [1, 2, 3, 4]


def test_simulate_batch_retries_transient_rate_limit(monkeypatch):
    """submit 이 처음엔 RATE_LIMITED(429) 라도 백오프 후 재시도해 결국 통과해야 한다."""
    monkeypatch.setattr(wb._time, 'sleep', lambda *_: None)

    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__(); self.attempts = {}
        def submit_simulation(self, expr, settings):
            self.attempts[expr] = self.attempts.get(expr, 0) + 1
            if self.attempts[expr] == 1:
                return 'RATE_LIMITED'
            return 'https://api.worldquantbrain.com/simulations/SIM_' + expr[:3]

    be = wb.ApiBackend('e', 'p', client=FlakyClient(), concurrency=4)
    res = be.simulate_batch(_batch(3), wqb_username='e', wqb_password='p')
    assert all(r['mode'] == 'pass' for r in res), [r['error_text'] for r in res]


def test_simulate_batch_partial_fn_once_per_alpha():
    """동시 실행이어도 partial_fn 은 알파당 정확히 1회, 모든 idx 가 빠짐없이 송출."""
    import threading
    seen = []
    lk = threading.Lock()

    def cb(o):
        with lk:
            seen.append(int(o['idx']))

    be = wb.ApiBackend('e', 'p', client=FakeClient(), concurrency=4)
    be.simulate_batch(_batch(8), wqb_username='e', wqb_password='p', partial_fn=cb)
    assert sorted(seen) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert len(seen) == 8  # 중복 없음


def test_simulate_batch_always_api(monkeypatch):
    """Playwright 제거 후 백엔드는 API 단일 — account_type 과 무관하게 ApiBackend 로 붙는다."""
    import server.wqb_backend as wbz
    called = {}

    class FakeApi:
        def __init__(self, *a, **k):
            pass

        def simulate_batch(self, batch, **kw):
            called['api'] = called.get('api', 0) + 1
            return [{'idx': 1, 'mode': 'pass'}]

    monkeypatch.setattr('server.wqb_backend.ApiBackend', FakeApi)
    for at in ('research_consultant', 'standard'):
        wbz.simulate_batch([{'idx': 1, 'code': 'x', 'settings': {}}],
                           wqb_username='e', wqb_password='p', account_type=at)
    assert called.get('api') == 2


def test_alpha_submits_are_serialized_across_threads():
    """WQB 는 계정당 제출을 한 번에 하나만 처리한다 — 동시 시뮬 스레드가 여럿이어도
    submit_alpha 호출은 절대 겹치면 안 된다 (겹치면 첫 번째 외 전부 429 로 죽는
    라이브 회귀). max in-flight == 1 을 검증한다."""
    import threading, time

    peak = {'cur': 0, 'max': 0}
    lk = threading.Lock()

    class ConcurrencySpyClient(FakeClient):
        def submit_alpha(self, aid, **kw):
            with lk:
                peak['cur'] += 1
                peak['max'] = max(peak['max'], peak['cur'])
            time.sleep(0.03)
            with lk:
                peak['cur'] -= 1
            self.alpha_submit_calls.append(aid)
            return True, 'submitted'

    client = ConcurrencySpyClient()
    be = wb.ApiBackend('e', 'p', client=client, concurrency=4)
    res = be.simulate_batch(_batch(4), wqb_username='e', wqb_password='p')
    assert len(client.alpha_submit_calls) == 4  # 전부 제출 시도됨
    assert peak['max'] == 1, f"동시 submit {peak['max']}건 — 직렬화 깨짐"
    assert all(r['submitted'] for r in res)


def test_submit_lock_wait_respects_stop_event():
    """제출 락을 기다리는 중 pause 되면 submit 을 생략하고 즉시 빠져나온다."""
    import threading, time

    class SlowSubmitClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.ev = threading.Event()

        def submit_alpha(self, aid, **kw):
            self.alpha_submit_calls.append(aid)
            self.ev.set()          # 첫 스레드가 락 안에 들어왔음을 알림
            time.sleep(0.5)        # 락을 오래 점유
            return True, 'submitted'

    client = SlowSubmitClient()
    be = wb.ApiBackend('e', 'p', client=client, concurrency=2)
    stop = threading.Event()

    def _pause_soon():
        client.ev.wait(timeout=5)
        time.sleep(0.05)
        stop.set()

    t = threading.Thread(target=_pause_soon)
    t.start()
    res = be.simulate_batch(_batch(2), wqb_username='e', wqb_password='p',
                            stop_event=stop)
    t.join()
    # 락을 잡았던 알파는 제출 완료, 기다리던 알파는 submit_skipped:paused.
    statuses = sorted(r['submit_status'] for r in res)
    assert 'submitted' in statuses
    assert any(s.startswith('submit_skipped') for s in statuses)


# ── 슬롯 재시도 예산 (2026-07-27 실측) ──────────────────────────────────────
# 슬롯은 앞선 sim 이 끝나야 빈다. 예산이 sim 소요시간보다 짧으면 대기자는 슬롯이
# 열리기도 전에 전부 포기한다 — 실제로 GLB sim ~1,200s 대 예산 600s 라 라운드마다
# 8개 중 2~6개가 429 로 죽고 있었다.

def test_retry_budget_outlasts_a_simulation():
    import server.wqb_backend as wb
    assert wb._RL_DEADLINE_S >= 1800, (
        f'재시도 예산 {wb._RL_DEADLINE_S}s 가 sim 소요시간(~1200s)보다 짧으면 '
        '대기자가 슬롯을 못 잡고 전멸한다')
    # 폴링 마감과 같은 눈금이어야 한다 — 한쪽만 늘리면 다시 어긋난다
    import server.wqb_api as wapi
    assert wb._RL_DEADLINE_S >= wapi._POLL_DEADLINE_S * 0.9


def test_retry_gives_up_only_after_the_budget(monkeypatch):
    """429 가 계속돼도 예산 안에서는 재시도하고, 넘겨야 RATE_LIMITED."""
    import server.wqb_backend as wb
    calls = []
    clock = {'t': 0.0}
    monkeypatch.setattr(wb._time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(wb._time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s))
    be = wb.ApiBackend.__new__(wb.ApiBackend)
    be._client = type('C', (), {
        'submit_simulation': lambda self, c, s: (calls.append(1), 'RATE_LIMITED')[1]})()
    assert be._submit_with_retry('rank(close)', {}, None) == 'RATE_LIMITED'
    assert len(calls) > 1, '한 번만 시도하고 포기했다 — 재시도 루프가 죽었다'
    assert clock['t'] >= wb._RL_DEADLINE_S


def test_failed_submit_is_not_logged_as_accepted(monkeypatch, caplog):
    """슬롯을 기다린 뒤 submit 이 None 을 뱉으면 '접수' 로 찍으면 안 된다.

    2026-07-28 실측: 한 라운드 18개 중 4개가 400 으로 죽었는데 로그엔 전부
    '슬롯 대기 N초 후 접수' 였다 — 실패가 성공 로그를 달고 나와 라운드 손실을
    슬롯 문제로 오독했다.
    """
    import logging
    import server.wqb_backend as wb
    clock = {'t': 0.0}
    monkeypatch.setattr(wb._time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(wb._time, 'sleep', lambda s: clock.__setitem__('t', clock['t'] + s))
    n = {'i': 0}

    def _submit(self, c, s):
        n['i'] += 1
        return 'RATE_LIMITED' if n['i'] == 1 else None   # 대기 후 실패

    be = wb.ApiBackend.__new__(wb.ApiBackend)
    be._client = type('C', (), {'submit_simulation': _submit})()
    with caplog.at_level(logging.INFO, logger=wb.LOG.name):
        assert be._submit_with_retry('rank(close)', {}, None) is None
    assert '접수' not in caplog.text, f'실패를 접수로 찍었다: {caplog.text}'


# ── 빈 슬롯 없이 돌기 (2026-07-27 사장 지시) ────────────────────────────────
# 후보 수 == 슬롯 수면 sim 하나가 끝나도 집어 갈 다음 후보가 없어 그 슬롯이 라운드
# 끝까지 논다. sim 이 ~20분이라 낭비가 크다 — 후보를 슬롯보다 많이 줘야 한다.

def test_candidate_count_exceeds_slot_count():
    import server.wqb_backend as wb
    from server import worker as w
    assert w.ALPHAS_PER_ROUND > wb._default_concurrency(), (
        f'후보 {w.ALPHAS_PER_ROUND} <= 슬롯 {wb._default_concurrency()} — '
        '시뮬이 끝나도 채울 후보가 없어 슬롯이 논다')


def test_pool_refills_a_freed_slot_immediately(monkeypatch):
    """슬롯(스레드)이 비면 대기 후보가 **즉시** 들어가야 한다 — 동시 실행은 슬롯 수를
    넘지 않으면서, 전체 후보가 모두 처리돼야 한다."""
    import server.wqb_backend as wb
    live, peak, done = {'n': 0}, {'n': 0}, []
    lock = threading.Lock()

    def fake_run_one(self, s, forced_delay, partial_fn, stop_event, submit_gate=None):
        with lock:
            live['n'] += 1
            peak['n'] = max(peak['n'], live['n'])
        _time.sleep(0.02)
        with lock:
            live['n'] -= 1
            done.append(s['idx'])
        return {'idx': s['idx']}

    monkeypatch.setattr(wb.ApiBackend, '_run_one', fake_run_one)
    be = wb.ApiBackend.__new__(wb.ApiBackend)
    be.concurrency = 4
    be._client = type('C', (), {'authenticate': lambda self: True})()
    batch = [{'idx': i} for i in range(1, 13)]        # 후보 12 > 슬롯 4
    out = be.simulate_batch(batch)

    assert len(out) == 12 and len(done) == 12, '후보 일부가 처리되지 않았다'
    assert peak['n'] <= 4, f'동시 실행이 슬롯 수를 넘었다: {peak["n"]}'
    assert peak['n'] == 4, f'슬롯을 다 못 채웠다: {peak["n"]}'
