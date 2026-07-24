"""노브 스윕 · 회전율↔감쇠 제어기 — 2026-07-22 신설.

배경: 2026-07-21 발굴이 통한 이유는 무작위 변이가 아니라 **엘리트의 노브를 한 축씩
체계적으로 훑은 것**이었다. 완전히 같은 식에서 중립화만 바꿔 Sharpe 1.72 → 2.47,
감쇠만 바꿔 회전율 100% → 48% 를 얻었다. 그 절차를 GA 에 굳혔는지 검증한다.
"""
import pytest

from server import constraint_spec as cs
from server import genome_models as gm


@pytest.fixture(autouse=True)
def _no_constraint():
    gm.set_constraint(None)
    yield
    gm.set_constraint(None)


PARENT = {
    'model': 'seed', 'family': 'option',
    'fields': ('opt6_vimtaxp', 'pcr_vol_10', 'historical_volatility_10'),
    'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'spread',
    'sign': -1, 'lookback_a': 5, 'lookback_b': 20,
    'universe': 'TOP1000', 'neutralization': 'SUBINDUSTRY',
    'decay': 4, 'truncation': 0.08,
}


# ── 회전율 → 감쇠 제어기 ─────────────────────────────────────────────────────

def test_회전율_초과면_감쇠를_올린다():
    got = gm.decay_for_target_turnover(4, 0.742)
    assert got is not None and got > 4


def test_이미_대역안이면_감쇠를_건드리지_않는다():
    """회전율을 더 죽일 이유가 없다 — 감쇠는 Sharpe 를 갉아먹는다."""
    assert gm.decay_for_target_turnover(4, 0.45) == 4


def test_측정값이_없으면_None():
    """모르는데 추측으로 감쇠 20 을 때리면 Sharpe 를 통째로 버린다."""
    assert gm.decay_for_target_turnover(4, None) is None
    assert gm.decay_for_target_turnover(4, 0) is None


def test_회전율이_높을수록_더_큰_감쇠():
    a = gm.decay_for_target_turnover(0, 0.70)
    b = gm.decay_for_target_turnover(0, 1.30)
    assert a is not None and b is not None and b >= a


# ── 노브 스윕 ────────────────────────────────────────────────────────────────

def _pop(**kw):
    return gm.generate_population(account_type='research_consultant', round_num=3,
                                  forced_delay='1', parent_genome=PARENT, **kw)


def test_첫_스윕은_중립화축_STATISTICAL():
    """실측 최고 설정을 가장 먼저 본다 — 늦게 보면 라운드를 낭비한다."""
    pop = _pop(n=4, parent_metrics={'turnover': 0.742})
    st = pop[0].get('settings') or {}
    assert st.get('neutralization') == 'STATISTICAL'
    # 중립화 축이므로 감쇠는 부모 그대로여야 한다(한 번에 한 축).
    assert str(st.get('decay')) == '4'


def test_둘째_스윕은_감쇠축_이고_제어기를_따른다():
    pop = _pop(n=4, parent_metrics={'turnover': 0.742})
    st = pop[1].get('settings') or {}
    assert st.get('neutralization') == 'SUBINDUSTRY'      # 부모 그대로
    assert int(st.get('decay')) == gm.decay_for_target_turnover(4, 0.742)


def test_스윕은_한번에_한_축만_바꾼다():
    """부모 대비 딱 한 유전자만 달라야 결과 차이를 그 축에 귀속시킬 수 있다."""
    pop = _pop(n=4, parent_metrics={'turnover': 0.742})
    for s in pop[:gm.SWEEP_SLOTS]:
        g = s.get('genome') or {}
        diff = [k for k in ('neutralization', 'decay', 'universe', 'truncation',
                            'fields', 'transform_a', 'transform_b', 'combine',
                            'sign', 'lookback_a', 'lookback_b')
                if g.get(k) != PARENT.get(k)]
        assert len(diff) == 1, f'한 축만 바뀌어야 하는데 {diff}'


def test_스윕도_탐색조건을_지킨다():
    gm.set_constraint(cs.parse(
        'region=USA & delay=1 & universe=TOP1000 & neutralization in (crowding, slow)'))
    pop = _pop(n=4, parent_metrics={'turnover': 0.742})
    got = {(s.get('settings') or {}).get('neutralization') for s in pop}
    assert got <= {'CROWDING', 'SLOW'}, got


def test_절단은_스윕축이_아니다():
    """실측: truncation 0.05·0.08·0.12·0.15 결과가 소수점까지 동일 — 훑으면 쿼터 낭비."""
    assert 'truncation' not in str(gm.SWEEP_NEUTRALIZATIONS)
    pop = _pop(n=4, parent_metrics={'turnover': 0.742})
    assert {str((s.get('settings') or {}).get('truncation')) for s in pop[:2]} == {'0.08'}
