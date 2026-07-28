# tests/test_no_resubmit_rejected.py
# 2026-07-28 실측 루프: 알파 1YzG86aM 이 16:06 과 16:20 에 **똑같은 FAIL 5개**로 두 번
# 거절됐다. 후보 생성이 결정론이라 재시작하면 같은 식이 다시 만들어지고, 시뮬 결과는
# 캐시에서 나오므로 같은 알파를 또 제출한다. IS 판정엔 안 걸리고 제출 시점 판정에만
# 걸리는 알파라 blocking_fail 검사로는 못 막는다.
import threading

import pytest

from server import db
from server import worker as w
from server import wqb_backend as wb

CODE = 'ts_av_diff(rank(close), 20)'


@pytest.fixture
def wk(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'r.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('r@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    obj = w.Worker.__new__(w.Worker)
    obj.user_id = uid
    obj._stop_event = threading.Event()
    obj._lock = threading.Lock()
    obj._corr_fs_hold = set()
    yield obj
    db._INITIALIZED = False


def _reject(uid, code=CODE):
    db.record_submit_attempt(uid, 1, 1, code, False,
                             'rejected:LOW_SHARPE(1.0 vs 1.58); LOW_FITNESS(0.57 vs 1.0)')


def test_same_expression_is_not_submitted_twice(wk):
    _reject(wk.user_id)
    ok, reason = wk._submit_gate({'sharpe': '1.0', 'wqb_alpha_id': 'A1'}, None,
                                 fail_items=[], code=CODE)
    assert ok is False and reason.startswith('already_rejected'), reason


def test_a_different_expression_is_unaffected(wk):
    _reject(wk.user_id)
    ok, _ = wk._submit_gate({'sharpe': '1.0', 'wqb_alpha_id': 'A2'}, None,
                            fail_items=[], code='rank(open)')
    assert ok is True


def test_successful_submission_does_not_block_anything(wk):
    """성공은 거절이 아니다 — 막으면 재제출 재시도 경로가 죽는다."""
    db.record_submit_attempt(wk.user_id, 1, 1, CODE, True, 'submitted')
    ok, _ = wk._submit_gate({'sharpe': '2.0', 'wqb_alpha_id': 'A3'}, None,
                            fail_items=[], code=CODE)
    assert ok is True


def test_skips_and_gate_holds_do_not_count_as_rejection(wk):
    """게이트가 보류한 것은 WQB 판정이 아니다 — 나중에 낼 수 있어야 한다."""
    db.record_submit_attempt(wk.user_id, 1, 1, CODE, False,
                             'submit_skipped:daily_budget(4/4)→queued')
    ok, _ = wk._submit_gate({'sharpe': '2.0', 'wqb_alpha_id': 'A4'}, None,
                            fail_items=[], code=CODE)
    assert ok is True


def test_rejection_memory_expires_so_rule_changes_can_be_retried(wk, monkeypatch):
    """영구 차단은 안 된다 — Power Pool·테마 조건이 바뀌면 같은 알파가 통과할 수 있다
    (2026-07-28 사장 지적)."""
    import time as _t
    _reject(wk.user_id)
    # 거절 기록을 창 밖(25시간 전)으로 밀어 둔다
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.execute('UPDATE submit_attempts SET ts=?', (_t.time() - 25 * 3600,))
    conn.commit(); conn.close()
    ok, _ = wk._submit_gate({'sharpe': '1.0', 'wqb_alpha_id': 'A1'}, None,
                            fail_items=[], code=CODE)
    assert ok is True, '하루가 지났는데도 막혔다 — 조건이 바뀌어도 영원히 못 낸다'


def test_rejection_window_is_a_day_not_a_month():
    assert 12 * 3600 <= db.REJECT_MEMORY_S <= 48 * 3600, db.REJECT_MEMORY_S


def test_no_code_falls_back_to_old_behaviour(wk):
    """code 를 못 받는 옛 호출 경로에서도 게이트가 죽으면 안 된다."""
    _reject(wk.user_id)
    ok, _ = wk._submit_gate({'sharpe': '2.0', 'wqb_alpha_id': 'A5'}, None, fail_items=[])
    assert ok is True


# ── 백엔드가 code 를 실제로 넘기는가 (옛 시그니처 폴백 포함) ──────────────────

def test_backend_passes_code_to_the_gate():
    seen = {}

    def gate(metrics, self_corr, fail_items=None, genome=None, code=None):
        seen['code'] = code
        return True, ''

    wb.ApiBackend._check_submit_gate(gate, {}, None, [], code=CODE)
    assert seen['code'] == CODE


def test_backend_still_works_with_a_gate_that_has_no_code_param():
    def old_gate(metrics, self_corr, fail_items=None, genome=None):
        return False, 'old'

    assert wb.ApiBackend._check_submit_gate(
        old_gate, {}, None, [], code=CODE) == (False, 'old')
