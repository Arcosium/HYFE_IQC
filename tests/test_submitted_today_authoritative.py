# tests/test_submitted_today_authoritative.py
# 2026-07-28 사장 지적: "오늘 제출량 내가 직접 제출한거까지 3개라 1개만 더 제출가능해."
# 우리 집계(submit_attempts)는 **우리가 낸 것만** 센다 — BRAIN UI 로 직접 낸 건 모른다.
# 그대로 두면 남은 예산을 착각해 초과 제출하고 WQB 가 거절한다(후보 하나를 버린다).
import threading

import pytest

from server import db
from server import worker as w


@pytest.fixture
def wk(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 's.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('s@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    obj = w.Worker.__new__(w.Worker)
    obj.user_id = uid
    obj._stop_event = threading.Event()
    obj._lock = threading.Lock()
    obj._corr_fs_hold = set()
    yield obj
    db._INITIALIZED = False


def _remote(monkeypatch, value):
    """WqbApiClient.submissions_on 이 value 를 주도록."""
    import server.wqb_api as api
    monkeypatch.setattr(api.WqbApiClient, '__init__', lambda self, u, p: None)
    monkeypatch.setattr(api.WqbApiClient, 'submissions_on', lambda self, d: value)
    monkeypatch.setattr(w._db, 'get_user_credentials', lambda uid: ('u', 'p'))


def test_external_submission_counts(wk, monkeypatch):
    """UI 로 직접 낸 1건이 우리 집계엔 없어도 예산에 잡혀야 한다."""
    db.record_submit_attempt(wk.user_id, 1, 1, 'rank(close)', True, 'submitted')
    db.record_submit_attempt(wk.user_id, 1, 2, 'rank(open)', True, 'submitted')
    _remote(monkeypatch, 3)
    assert wk._submitted_today() == 3


def test_our_fresh_submission_wins_when_wqb_lags(wk, monkeypatch):
    """방금 우리가 낸 건 WQB 집계에 아직 안 잡힐 수 있다 — 큰 쪽을 쓴다."""
    for i in range(4):
        db.record_submit_attempt(wk.user_id, 1, i, 'rank(close)', True, 'submitted')
    _remote(monkeypatch, 1)
    assert wk._submitted_today() == 4


def test_api_failure_falls_back_to_local_count(wk, monkeypatch):
    db.record_submit_attempt(wk.user_id, 1, 1, 'rank(close)', True, 'submitted')
    _remote(monkeypatch, None)
    assert wk._submitted_today() == 1


def test_gate_blocks_once_external_submissions_fill_the_budget(wk, monkeypatch):
    """우리 집계는 2건이어도 WQB 가 4건이면 더 내면 안 된다."""
    for i in range(2):
        db.record_submit_attempt(wk.user_id, 1, i, 'rank(close)', True, 'submitted')
    _remote(monkeypatch, w.DAILY_SUBMIT_BUDGET)
    ok, reason = wk._submit_gate({'sharpe': '2.0', 'wqb_alpha_id': 'A1'}, None,
                                 fail_items=[])
    assert ok is False and reason.startswith('daily_budget')


def test_platform_date_matches_the_submission_day_boundary():
    """records 날짜와 예산 리셋 경계가 같은 눈금이어야 대조가 성립한다."""
    import datetime
    ts = db.day_start_ts()
    assert db.platform_date(ts) == db.platform_date(ts + 3600)
    assert db.platform_date(ts - 60) != db.platform_date(ts)
    datetime.date.fromisoformat(db.platform_date())   # 형식 검증
