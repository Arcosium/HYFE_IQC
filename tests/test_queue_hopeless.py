# tests/test_queue_hopeless.py
# 제출 대기 큐는 '아직 판단·행동이 필요한 것' 만 남아야 한다. 같은 알파를 그대로 다시
# 내도 안 바뀌는 사유(상관 소진·사다리)는 손절해서 목록에서 내린다
# (2026-08-04 사장 지시 "어차피 제출 가능성 없는 애들은 배제").
from server import criteria


def test_correlation_and_ladder_are_hopeless():
    assert criteria.queue_hopeless(
        'rejected:LOW_SHARPE(1.04 vs 1.58); PROD_CORRELATION(0.7189 vs 0.7) (http_403)'
    ) == 'PROD_CORRELATION'
    assert criteria.queue_hopeless('rejected:SELF_CORRELATION (http_403)') == 'SELF_CORRELATION'
    assert criteria.queue_hopeless('rejected:IS_LADDER_SHARPE (http_403)') == 'IS_LADDER_SHARPE'


def test_cut_margin_does_not_matter():
    """컷을 아슬아슬하게 넘겨도 손절 — 같은 알파의 상관은 내려가지 않고 오르기만 한다."""
    assert criteria.queue_hopeless('PROD_CORRELATION(0.7001 vs 0.7)') == 'PROD_CORRELATION'


def test_weekly_varying_reasons_stay_in_queue():
    """샤프·핏(HT 완화컷 1.06)과 테마는 주마다 바뀐다 — 손절하면 안 된다."""
    for r in ('rejected:LOW_SHARPE(1.04 vs 1.58); LOW_FITNESS(0.31 vs 1.0) (http_403)',
              'rejected:PURE_POWER_POOL_THEME (http_403)',
              'rejected:HIGH_TURNOVER (http_403)', '', 'submitted'):
        assert criteria.queue_hopeless(r) == '', r


def test_accepts_check_name_list():
    assert criteria.queue_hopeless([{'name': 'PROD_CORRELATION'}]) == 'PROD_CORRELATION'
    assert criteria.queue_hopeless(['LOW_SHARPE']) == ''
