# tests/test_fitness_is_hard_gate.py
# 2026-08-05 실측: HT 분류를 받아도 fitness 미달이면 제출이 막힌다.
#   9q7E7J0r — MATCHES_CLASSIFICATION=["High Turnover","Investable High Turnover"] PASS,
#   LOW_SHARPE PASS(2.15 vs 1.58), 그런데 LOW_FITNESS(0.65 vs 1.0) 단독 사유로 403.
# 예전 submittability 는 max(ht, standard) 라 이런 알파를 '제출 가능'(1.0)으로 봤고,
# 그 착각 때문에 GA 가 fitness 0.3~0.8 대역을 12일간 파고들었다.
from server import criteria


def _m(**kw):
    base = {'sharpe': '2.15', 'fitness': '0.65', 'turnover': '0.49',
            'ht_turnover': '0.49', 'ht_returns_ratio': '0.92', 'ht_pnl_horizon': '3',
            '_delay': '1'}
    base.update({k: str(v) for k, v in kw.items()})
    return base


def test_ht_classified_but_low_fitness_is_not_submittable():
    """HT 관문을 다 통과해도 fitness 0.65 면 제출 불가로 봐야 한다."""
    assert criteria.submittability(_m()) < 1.0


def test_fitness_at_cutoff_unlocks():
    """fitness 가 컷(1.0)에 닿으면 비로소 제출 가능."""
    assert criteria.submittability(_m(fitness='1.0')) == 1.0


def test_fitness_gate_applies_to_standard_path_too():
    """HT 자격이 없어도 마찬가지 — fitness 는 공통 관문이다."""
    m = _m(fitness='0.9', ht_turnover='0.01', ht_returns_ratio='0.1', ht_pnl_horizon='99')
    assert criteria.submittability(m) < 1.0


def test_measured_cutoff_from_check_wins():
    """WQB 가 컷을 또 바꾸면 체크에서 승격된 실측값을 따른다."""
    assert criteria.fitness_progress(_m(fitness='0.8', fitness_check_cutoff='0.8')) == 1.0


def test_unmeasured_fitness_does_not_zero_the_gradient():
    """fitness 미측정을 0 으로 취급하면 GA 가 회전율 기울기를 잃는다 (테스트가 잡은 결함)."""
    def route(to):
        return criteria.submittability(dict(sharpe='1.2', turnover=str(to),
                                            returns=str(0.20 * to),
                                            ht_turnover=str(to), ht_returns_ratio='0.85'))
    assert route(0.05) < route(0.12) < route(0.22)
    assert criteria.fitness_progress({'sharpe': '1.2'}) is None


# ── 실측 컷 기반 일반 게이트 (2026-08-05 개편) ────────────────────────────────

def test_ladder_is_a_hard_gate():
    """사다리를 12일간 손절 신호로만 쓰고 목표로 삼은 적이 없었다 — 이제 관문이다."""
    m = _m(fitness='1.2', ladder_sharpe='0.3', ladder_sharpe_cutoff='1.58')
    assert criteria.submittability(m) < 1.0
    assert criteria.binding_gate(m)[0] == 'IS_LADDER_SHARPE'


def test_sub_universe_uses_measured_moving_cutoff():
    """서브유니버스 컷은 알파 샤프에 비례해 움직인다(1.89→2.14) — 실측값만 쓴다."""
    m = _m(fitness='1.2', sub_universe_sharpe='1.73', sub_universe_sharpe_cutoff='2.11')
    assert criteria.gate_progress(m)['LOW_SUB_UNIVERSE_SHARPE'] < 1.0


def test_correlation_gate_is_inverted():
    """상관은 낮을수록 좋다 — 방향을 뒤집지 않으면 소진 축을 '통과'로 읽는다."""
    good = criteria.gate_progress(_m(prod_correlation='0.3', prod_correlation_cutoff='0.7'))
    bad = criteria.gate_progress(_m(prod_correlation='0.88', prod_correlation_cutoff='0.7'))
    assert good['PROD_CORRELATION'] == 1.0 and bad['PROD_CORRELATION'] < 1.0


def test_all_gates_clear_means_submittable():
    m = _m(fitness='1.2', ladder_sharpe='2.4', ladder_sharpe_cutoff='1.58',
           sub_universe_sharpe='2.2', sub_universe_sharpe_cutoff='1.9')
    assert criteria.submittability(m) == 1.0
