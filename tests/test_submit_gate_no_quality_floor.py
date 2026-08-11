# tests/test_submit_gate_no_quality_floor.py
# 2026-07-28 사장 지시: "그냥 제출할 수 있는건 무조건 제출하고 4개 한도 채웠으면
# 제출 대기에 넣어주는걸로." — 품질 문턱(below_value) 제거.
# 계기: 5 PASS / 0 FAIL 인 알파가 '품질 문턱 미달 0.14<0.15' 로 안 나갔다.
# 근거: 제출 실적 1·1·2·4·2 건(대개 4칸을 못 채움)인데 문턱으로 330건을 걸렀고,
#       안 쓴 예산은 이월되지 않는다.
import threading

import pytest

from server import constraint_spec, db
from server import worker as w


@pytest.fixture
def wk(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'g.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('g@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    obj = w.Worker.__new__(w.Worker)
    obj.user_id = uid
    obj._stop_event = threading.Event()
    obj._lock = threading.Lock()
    obj._corr_fs_hold = set()
    yield obj
    db._INITIALIZED = False


# 문턱에 막히던 그 알파 — submission_value 가 0.15 를 못 넘는 수준
_WEAK = {'sharpe': '1.04', 'fitness': '0.29', 'turnover': '0.3444',
         'wqb_alpha_id': 'WEAK1'}


def test_weak_but_submittable_alpha_is_submitted(wk):
    ok, reason = wk._submit_gate(_WEAK, None, fail_items=[])
    assert ok is True, f'낼 수 있는데 막았다: {reason}'
    assert 'below_value' not in reason


def test_blocking_fail_is_still_refused(wk):
    """0번 겹은 남는다 — WQB 가 반드시 403 낼 요청은 보낼 이유가 없다."""
    ok, reason = wk._submit_gate(
        {'sharpe': '0.1', 'turnover': '0.9', 'wqb_alpha_id': 'BAD1'}, None,
        fail_items=['HIGH_TURNOVER'])
    assert ok is False and 'blocking_fail' in reason


def test_budget_exhausted_goes_to_the_waiting_queue(wk):
    for i in range(w.DAILY_SUBMIT_BUDGET):
        db.record_submit_attempt(wk.user_id, 1, i, 'rank(close)', True, 'submitted')
    ok, reason = wk._submit_gate(_WEAK, None, fail_items=[])
    assert ok is False and reason.startswith('daily_budget')
    assert reason.endswith('→queued'), reason
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(wk.user_id)] == ['WEAK1']


def test_budget_exhausted_without_alpha_id_does_not_claim_it_queued(wk):
    """넣지도 못했으면서 '→queued' 라고 적으면 라이브 피드가 거짓말한다."""
    for i in range(w.DAILY_SUBMIT_BUDGET):
        db.record_submit_attempt(wk.user_id, 1, i, 'rank(close)', True, 'submitted')
    ok, reason = wk._submit_gate({'sharpe': '1.2'}, None, fail_items=[])
    assert ok is False and reason.endswith('→미보관'), reason
    assert db.submit_queue_list(wk.user_id) == []


def test_active_weekly_required_check_is_a_dynamic_submit_gate(wk):
    wk._active_constraint = constraint_spec.parse(
        'region=GLB & delay=1 & universe=TOPDIV3000 & Theme Alpha test PASS')
    base = dict(_WEAK, region='GLB', _delay='1', universe='TOPDIV3000')

    failed = dict(base, _check_results={'THEME_ALPHA': 'WARNING'})
    ok, reason = wk._submit_gate(failed, None, fail_items=[], code='rank(opt6_vimtaxp)')
    assert ok is False and 'THEME_ALPHA=WARNING' in reason

    passed = dict(base, _check_results={'THEME_ALPHA': 'PASS'})
    ok, reason = wk._submit_gate(passed, None, fail_items=[], code='rank(opt6_vimtaxp)')
    assert ok is True, reason


def test_active_scope_mismatch_is_blocked_before_submit(wk):
    wk._active_constraint = constraint_spec.parse(
        'region=EUR & delay=0 & universe=TOP2500')
    metrics = dict(_WEAK, region='USA', _delay='1', universe='TOP3000')
    ok, reason = wk._submit_gate(metrics, None, fail_items=[], code='rank(opt6_vimtaxp)')
    assert ok is False and 'region=USA' in reason and 'delay=1' in reason
