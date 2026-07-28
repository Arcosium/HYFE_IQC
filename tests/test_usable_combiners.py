# tests/test_usable_combiners.py
# 2026-07-28 실측: 재조합 결합식 5개 중 3개가 우리 계정에서 접근 불가였다
# (vector_proj·regression_neut·regression_proj). 고르는 순간엔 모르니 시뮬까지
# 간 뒤 'Attempted to use inaccessible or unknown operator' 로 죽어, 라운드마다
# 재조합 후보를 통째로 버렸다. **CONSULTANT 계정에서도** 막혀 있었다.
from server import combine_layer as cl

# 2026-07-28 /operators 실측값 중 결합식이 쓰는 것만
LIVE_OPS = {'scale', 'rank', 'zscore', 'vector_neut'}


def _names(ops):
    return {n for n, _ in cl.usable_combiners(ops)}


def test_inaccessible_combiners_are_dropped():
    got = _names(LIVE_OPS)
    assert 'vproj' not in got and 'rneut' not in got and 'rproj' not in got
    assert got == {'boost', 'vneut'}, got


def test_unknown_operator_set_keeps_everything():
    """조회 실패(None/빈 집합)면 옛 동작 그대로 — 조회 실패로 탐색을 죽이지 않는다."""
    assert _names(None) == {n for n, _ in cl.COMBINERS}
    assert _names(set()) == {n for n, _ in cl.COMBINERS}


def test_all_blocked_falls_back_rather_than_producing_nothing():
    """전멸이면 조회 쪽이 틀렸을 가능성이 크다 — 재조합을 통째로 끄지 않는다."""
    assert _names({'nothing_matches'}) == {n for n, _ in cl.COMBINERS}


def test_candidates_only_emits_accessible_operators():
    pool = [
        {'id': 1, 'code': 'rank(close)', 'sharpe': 2.0, 'turnover': 0.2,
         'universe': 'TOPDIV3000', 'neutralization': 'MARKET'},
        {'id': 2, 'code': 'rank(open)', 'sharpe': 1.5, 'turnover': 0.3,
         'universe': 'TOPDIV3000', 'neutralization': 'MARKET'},
    ]
    import random
    for seed in range(20):
        for c in cl.candidates(pool, n=2, rng=random.Random(seed), operators=LIVE_OPS):
            assert 'vector_proj' not in c['code']
            assert 'regression_neut' not in c['code']
            assert 'regression_proj' not in c['code']
