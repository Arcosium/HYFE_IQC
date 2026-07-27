# tests/test_daily_budget_boundary.py
# 일일 제출 예산 리셋 경계 = 미국 동부시간 자정 (DST 반영).
# 근거(2026-07-27 API 실측): /users/self/activities/submissions 가 UTC 7/27 01:33
# (= EDT 7/26 21:33) 에 yesterday=2026-07-25 를 반환 → 플랫폼의 '오늘'은 EDT 7/26.
import datetime as dt

import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'bd.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def _utc(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def test_day_start_is_eastern_midnight_summer():
    # 여름(EDT, UTC-4): 동부 자정 = 04:00 UTC = KST 13:00
    now = _utc(2026, 7, 27, 1, 33)          # EDT 7/26 21:33
    assert db.day_start_ts(now) == _utc(2026, 7, 26, 4, 0)
    # UTC 자정을 넘겨도(=KST 09시) 동부로는 같은 날 → 리셋 아님
    assert db.day_start_ts(_utc(2026, 7, 27, 0, 5)) == _utc(2026, 7, 26, 4, 0)
    # 동부 자정 직후 → 새 날
    assert db.day_start_ts(_utc(2026, 7, 27, 4, 1)) == _utc(2026, 7, 27, 4, 0)


def test_day_start_is_eastern_midnight_winter():
    # 겨울(EST, UTC-5): 동부 자정 = 05:00 UTC = KST 14:00
    now = _utc(2026, 1, 15, 12, 0)
    assert db.day_start_ts(now) == _utc(2026, 1, 15, 5, 0)


def test_submitted_today_counts_eastern_day(isolated_db):
    uid = isolated_db
    conn = db._connect()
    # EDT 7/26 13:12 (= UTC 17:12, KST 7/27 02:12) 제출 2건 — 동부 기준 '오늘'
    for h in (17, 18):
        conn.execute(
            'INSERT INTO submit_attempts (user_id, round_num, idx, code, submitted, '
            'submit_status, pass_count, fail_count, ts) VALUES (?,?,?,?,?,?,?,?,?)',
            (uid, 1, 1, 'c', 1, 'submitted', 0, 0, _utc(2026, 7, 26, h, 12)))
    conn.commit(); conn.close()
    # KST 7/27 10:33 = EDT 7/26 21:33 — 아직 같은 동부 날짜라 2건으로 센다
    assert db.submitted_today(uid, now=_utc(2026, 7, 27, 1, 33)) == 2
    # 동부 자정(= KST 13:00) 넘기면 리셋
    assert db.submitted_today(uid, now=_utc(2026, 7, 27, 4, 30)) == 0


def test_default_budget_is_four():
    from server import worker
    assert worker.DAILY_SUBMIT_BUDGET == 4


def test_submit_hold_gate_queues_and_expires(isolated_db, monkeypatch, tmp_path):
    """보류창 안에서는 제출 대신 큐로, 지나면 통과 (2026-07-27)."""
    import time as _t
    from server import worker as w, run_config
    uid = isolated_db
    monkeypatch.setattr(run_config, '_PATH', str(tmp_path / 'rc.json'), raising=False)
    wk = w.Worker(uid)
    metrics = {'wqb_alpha_id': 'HOLD1', 'sharpe': '2.0', 'fitness': '1.2'}

    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: _t.time() + 3600)
    ok, reason = wk._submit_gate(metrics, None, fail_items=[])
    assert ok is False and reason.startswith('submit_hold')
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == ['HOLD1']

    # 보류창이 지나면 게이트가 막지 않는다
    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: _t.time() - 10)
    monkeypatch.setattr(db, 'submitted_today', lambda *a, **k: 0)
    monkeypatch.setattr(db, 'submitted_fieldsets_today', lambda *a, **k: [])
    monkeypatch.setattr(db, 'rejected_fieldsets', lambda *a, **k: [])
    ok2, _ = wk._submit_gate(metrics, None, fail_items=[])
    assert ok2 is True
