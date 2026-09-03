# tests/test_regular_submission_queues.py
# REGULAR_SUBMISSION FAIL = "오늘 쿼터 소진" 신호지 알파 결함이 아니다.
# 차단 FAIL 로 취급하면 첫 문에서 되돌려보내 익일 재시도 큐가 통째로 우회된다.
# 실측(2026-08-13): NY 8/12 14:37 이후 이 FAIL 단독인 알파 27건이 큐에 하나도
# 안 들어가고 버려졌다 — S 2.29 두 건 포함.
import time as _t

import pytest

from server import db, run_config
from server import worker as w


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'rs.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


@pytest.fixture
def gate(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(run_config, '_PATH', str(tmp_path / 'rc.json'), raising=False)
    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: _t.time() - 10)
    return isolated_db, w.Worker(isolated_db)


def test_quota_fail_goes_to_queue_not_discard(gate, monkeypatch):
    uid, wk = gate
    monkeypatch.setattr(db, 'submitted_today', lambda *a, **k: 4)
    ok, reason = wk._submit_gate({'wqb_alpha_id': 'QUOTA1'}, None,
                                 fail_items=['REGULAR_SUBMISSION'])
    assert ok is False
    assert reason.startswith('daily_budget'), reason
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == ['QUOTA1']


def test_wqb_counter_wins_over_our_stale_count(gate, monkeypatch):
    """우리 집계가 0 이어도 WQB 가 소진이라 하면 큐로 보낸다."""
    uid, wk = gate
    monkeypatch.setattr(db, 'submitted_today', lambda *a, **k: 0)
    ok, reason = wk._submit_gate({'wqb_alpha_id': 'QUOTA2'}, None,
                                 fail_items=['REGULAR_SUBMISSION'])
    assert ok is False and reason.startswith('daily_budget')
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == ['QUOTA2']


def test_real_fail_is_submitted_until_wqb_reports_quota(gate, monkeypatch):
    """PROD 실패 예측도 막지 않되 WQB의 실제 쿼터 신호는 따른다."""
    uid, wk = gate
    monkeypatch.setattr(db, 'submitted_today', lambda *a, **k: 0)
    ok, reason = wk._submit_gate({'wqb_alpha_id': 'BAD1'}, None,
                                 fail_items=['PROD_CORRELATION', 'REGULAR_SUBMISSION'])
    assert ok is False
    assert reason.startswith('daily_budget')
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == ['BAD1']
