# tests/test_presim_dup_guard.py
# 같은 식은 **시뮬 전**에 거른다 (2026-08-04 사장 지시 "같은 식이면 처음부터 하지 마라 /
# 제출은 일단 submit 떴으면 해보고"). 문 앞(제출 게이트)에서 막으면 그때는 이미 시뮬
# 슬롯을 태운 뒤다 — 실측 #11 이 S=1.23·fit=0.62 로 통과하고도 직전 거절작
# (S=1.24·fit=0.63)과 같은 식이라 제출되지 못했다.
import pytest

from server import worker

CODE = 'rank(ts_mean(close, 20))'
PRIOR = 'rejected:LOW_SHARPE(1.24 vs 1.58); LOW_FITNESS(0.63 vs 1.0)'


@pytest.fixture
def gate(monkeypatch):
    """제출 게이트를 예산·모드·보류창이 모두 열린 상태로 세운다."""
    monkeypatch.setattr(worker._db, 'code_rejected_before', lambda uid, code: PRIOR)
    monkeypatch.setattr(worker._db, 'code_submitted_before', lambda uid, code: False)
    monkeypatch.setattr(worker._db, 'get_submit_mode', lambda uid: 'auto')
    monkeypatch.setattr(worker.run_config, 'get_submit_hold_until', lambda: 0.0)
    monkeypatch.setattr(worker.Worker, '_submitted_today', lambda self: 0)
    return worker.Worker(2)


def test_previously_rejected_code_is_still_submitted(gate):
    """거절 이력이 있어도 제출은 시도한다 — 거절은 쿼터를 안 쓴다."""
    ok, why = gate._submit_gate({'wqb_alpha_id': 'X1'}, fail_items=[], code=CODE)
    assert ok, why


def test_submit_gate_has_no_same_code_guard_at_all(gate, monkeypatch):
    """이미 OS 에 오른 동일 코드조차 문 앞에서 막지 않는다 (2026-08-06 사장 재지시).

    막아 봐야 시뮬은 이미 태운 뒤다 — 아끼는 게 없고 화면엔 "재제출 무의미"만 뜬다.
    같은 식은 **시뮬 전**에 걸러야 하고, 그건 아래 라운드 후보 단계가 한다.
    """
    monkeypatch.setattr(worker._db, 'code_submitted_before', lambda uid, code: True)
    ok, why = gate._submit_gate({'wqb_alpha_id': 'X1'}, fail_items=[], code=CODE)
    assert ok, why
    assert 'already_' not in (why or '')


def test_fail_item_is_submitted_for_actual_rejection_reason(gate):
    """로컬 FAIL 예측보다 실제 WQB submit 응답을 우선한다."""
    ok, why = gate._submit_gate({'wqb_alpha_id': 'X1'},
                                fail_items=[{'name': 'PROD_CORRELATION'}], code=CODE)
    assert ok and why == ''
