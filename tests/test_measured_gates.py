# tests/test_measured_gates.py
# 관문 판정은 **WQB 가 실제로 준 value/cutoff** 로만 한다. 우리 시뮬 수치를 컷과 비교해
# 미리 판정하면 안 된다 — 2026-08-06 재검증에서 확정.
#   · 제출 성공작 22건의 fitness 가 0.26~0.86 으로 전부 공식 컷 1.0 미만이다
#   · 8/5 16:51 에 fitness 0.55 알파(qMNZpGdA)가 정상 제출됐다
#   · LOW_FITNESS 단독 사유 403 은 전 기간 0건 — 늘 PROD/LADDER/GLB_* 와 동반한다
# 8/5 에 이 파일은 정반대("fitness 는 우회 불가")를 주장했다. 그 전제로 submittability 에
# 넣은 fitness 캡이 GA 를 고 fitness·사다리 사망 가계(inst18, S 3.0/fit 1.8/ladder 0.05)로
# 몰았다. 되돌렸다.
from server import criteria


def _m(**kw):
    base = {'sharpe': '2.15', 'fitness': '0.65', 'turnover': '0.49',
            'ht_turnover': '0.49', 'ht_returns_ratio': '0.92', 'ht_pnl_horizon': '3',
            '_delay': '1'}
    base.update({k: str(v) for k, v in kw.items()})
    return base


def test_our_own_low_fitness_does_not_block():
    """HT 경로가 열려 있으면 시뮬 fitness 0.65 만으로 제출 불가로 찍으면 안 된다.

    실제로 그 대역(0.26~0.86)이 22건 통과했다. 8/5 판 코드는 여기서 0.65 를 반환했다.
    """
    assert criteria.submittability(_m(ht_after_cost_sharpe='1.2')) == 1.0


def test_measured_fitness_fail_does_block():
    """WQB 가 직접 준 값이면 얘기가 다르다 — 실측 FAIL 은 관문이다."""
    m = _m(fitness_check='0.65', fitness_check_cutoff='1.0')
    assert criteria.submittability(m) < 1.0
    assert criteria.binding_gate(m)[0] == 'LOW_FITNESS'


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
