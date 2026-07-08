# tests/test_wqb_backend.py
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


def test_simulate_batch_fail_mode_below_threshold():
    class SixPassClient(FakeClient):
        def harvest_alpha(self, aid):
            return {'metrics': {}, 'is_status': {'pass': [{'name': 'x'}] * 6, 'fail': [], 'error': [], 'pending': []}}

    be = wb.ApiBackend('e', 'p', client=SixPassClient())
    res = be.simulate_batch([{'idx': 9, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'fail' and res[0]['pass_count'] == 6
    assert res[0]['submitted'] is True and res[0]['submit_status'] == 'submitted'


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


def test_dispatch_routes_by_account_type(monkeypatch):
    import server.wqb_browser as wbz
    called = {}
    def fake_browser(batch, **kw): called['browser'] = True; return [{'idx': 1, 'mode': 'fail'}]
    class FakeApi:
        def __init__(self, *a, **k): pass
        def simulate_batch(self, batch, **kw): called['api'] = True; return [{'idx': 1, 'mode': 'pass'}]
    monkeypatch.setattr(wbz, '_browser_simulate_batch', fake_browser)
    monkeypatch.setattr('server.wqb_backend.ApiBackend', FakeApi)
    wbz.simulate_batch([{'idx': 1, 'code': 'x', 'settings': {}}],
                       wqb_username='e', wqb_password='p', account_type='research_consultant')
    assert called.get('api') and not called.get('browser')
    called.clear()
    wbz.simulate_batch([{'idx': 1, 'code': 'x', 'settings': {}}],
                       wqb_username='e', wqb_password='p', account_type='standard')
    assert called.get('browser') and not called.get('api')


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
