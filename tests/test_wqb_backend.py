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
    assert seen and seen[0]['idx'] == 1 and seen[0]['status'] == 'pass'


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


def test_simulate_batch_rate_limited():
    class RateLimitedClient(FakeClient):
        def submit_simulation(self, expr, settings):
            self.submit_calls.append(expr)
            return 'RATE_LIMITED'

    be = wb.ApiBackend('e', 'p', client=RateLimitedClient())
    res = be.simulate_batch([{'idx': 4, 'code': 'x', 'desc': '', 'settings': {}}],
                            wqb_username='e', wqb_password='p')
    assert res[0]['mode'] == 'error'
    assert 'CONCURRENT' in res[0]['error_text'] or '429' in res[0]['error_text']
