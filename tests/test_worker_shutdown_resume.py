# tests/test_worker_shutdown_resume.py
# 배포 재시작 후 워커가 스스로 켜져야 한다(2026-08-04 실측 사고 — 재시작 뒤 라운드 0건).
# request_shutdown 은 paused 를 안 남겨서 _auto_resume_workers 가 켜주도록 설계됐는데,
# Worker.run 의 finally 가 running 까지 지워 버리면 그 설계가 무효가 된다.
import pytest

from server import worker as w


@pytest.fixture(autouse=True)
def _reset_flag():
    w._SHUTTING_DOWN = False
    yield
    w._SHUTTING_DOWN = False


def _stub(monkeypatch):
    calls = []
    monkeypatch.setattr(w._db, 'set_user_running',
                        lambda uid, running, paused: calls.append((running, paused)))
    monkeypatch.setattr(w.Worker, '_main_loop', lambda self: None)
    monkeypatch.setattr(w.Worker, '_drain_ticker', lambda self: None)
    return calls


def test_process_shutdown_keeps_running_flag(monkeypatch):
    calls = _stub(monkeypatch)
    monkeypatch.setattr(w.wqb_backend, 'cancel_all_inflight', lambda: 0)
    w.shutdown_all()                       # SIGTERM 경로
    w.Worker(2).run()
    assert not any(r is False for r, _ in calls)   # running=0 을 쓰면 안 된다


def test_user_stop_clears_running_flag(monkeypatch):
    calls = _stub(monkeypatch)
    w.Worker(2).run()                      # 평시 종료
    assert (False, False) in calls
