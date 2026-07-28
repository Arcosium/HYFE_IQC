# tests/test_shutdown_cancels_sims.py
# 2026-07-28 실측: 서비스를 죽이면 WQB 쪽 시뮬이 계속 돌며 동시 슬롯을 물고 있어,
# 재시작 직후 라운드가 빈 슬롯 1개로 시작했다(스레드 8개 중 7개가 t=0부터 대기,
# 첫 결과 30분 뒤). 종료 시 취소를 보내야 한다.
import server.wqb_backend as wb
import server.worker as w


class _Client:
    def __init__(self):
        self.cancelled = []

    def cancel(self, url):
        self.cancelled.append(url)


def _clear():
    with wb._INFLIGHT_LOCK:
        wb._INFLIGHT.clear()


def test_inflight_sims_are_cancelled_on_shutdown():
    _clear()
    c = _Client()
    wb._track_inflight('https://x/simulations/A', c)
    wb._track_inflight('https://x/simulations/B', c)
    assert wb.cancel_all_inflight() == 2
    assert sorted(c.cancelled) == ['https://x/simulations/A', 'https://x/simulations/B']
    assert wb.cancel_all_inflight() == 0, '두 번 취소하면 안 된다'


def test_completed_sim_is_no_longer_tracked():
    """폴링이 끝나면 추적에서 빠진다 — 안 빠지면 끝난 시뮬에 DELETE 를 쏜다."""
    _clear()
    c = _Client()
    wb._track_inflight('u1', c)
    wb._untrack_inflight('u1')
    assert wb.cancel_all_inflight() == 0


def test_one_failing_cancel_does_not_block_the_rest():
    _clear()
    bad = type('B', (), {'cancel': lambda self, u: (_ for _ in ()).throw(RuntimeError('net'))})()
    good = _Client()
    wb._track_inflight('bad', bad)
    wb._track_inflight('good', good)
    wb.cancel_all_inflight()
    assert good.cancelled == ['good']


# ── request_shutdown 은 paused 플래그를 남기지 않는다 ────────────────────────
# 남기면 list_running_user_ids(paused=0 만 고름)가 걸러내 재시작 후 워커가 안 켜진다.

def _fake_worker(monkeypatch, calls):
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = 7
    wk._stop_event = __import__('threading').Event()
    wk._lock = __import__('threading').Lock()
    wk._batch_proc_holder = {'proc': None}
    monkeypatch.setattr(w._db, 'set_user_running',
                        lambda uid, running, paused: calls.append((uid, running, paused)))
    return wk


def test_shutdown_does_not_persist_paused(monkeypatch):
    calls = []
    wk = _fake_worker(monkeypatch, calls)
    wk.request_shutdown()
    assert wk._stop_event.is_set(), '중단 신호는 세워야 한다'
    assert calls == [], f'paused 를 DB 에 남겼다 — 재시작 후 자동 재개가 막힌다: {calls}'


def test_user_pause_still_persists(monkeypatch):
    calls = []
    wk = _fake_worker(monkeypatch, calls)
    wk.request_pause()
    assert calls == [(7, True, True)]
