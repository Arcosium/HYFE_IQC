# tests/test_selection.py
# #6 rank-백분위 + NSGA-II + crowding, #5 구조적 다양성(greedy fitness-sharing).
import server.selection as sel


def test_rank_percentile_basic():
    assert sel.rank_percentile([3, 1, 2]) == [1.0, 0.0, 0.5]
    assert sel.rank_percentile([3, 1, 2], invert=True) == [0.0, 1.0, 0.5]
    assert sel.rank_percentile([]) == []
    assert sel.rank_percentile([5]) == [0.5]
    assert sel.rank_percentile([1.0, None]) == [1.0, 0.0]   # None → 최악


def test_non_dominated_single_dominator():
    fronts = sel.non_dominated_sort([[2, 2], [1, 1], [1, 2]])
    assert fronts[0] == [0]


def test_pareto_front_multiple_non_dominated():
    fronts = sel.non_dominated_sort([[2, 1], [1, 2], [0, 0]])
    assert set(fronts[0]) == {0, 1}
    assert fronts[1] == [2]


def test_crowding_boundary_inf():
    cd = sel.crowding_distance([[0, 0], [1, 1], [2, 2]])
    assert cd[0] == float('inf') and cd[2] == float('inf')
    assert cd[1] < float('inf')


def test_nsga2_order_front0_first():
    order = sel.nsga2_order([[2, 1], [1, 2], [0, 0]])
    assert set(order[:2]) == {0, 1}
    assert order[-1] == 2


def test_composite_scores_dominant_wins():
    recs = [{'metrics': {'sharpe': '3', 'fitness': '2', 'turnover': '0.3'}, 'self_corr': 0.1},
            {'metrics': {'sharpe': '1', 'fitness': '1', 'turnover': '0.6'}, 'self_corr': 0.5}]
    scores = sel.composite_scores(recs)
    assert scores[0] > scores[1]


def test_order_seed_records_ref_is_none():
    assert sel.order_seed_records([{'a': 1}], mode='ref') is None


def test_order_seed_records_percentile_sharpe_desc():
    recs = [{'metrics': {'sharpe': '1'}}, {'metrics': {'sharpe': '3'}}, {'metrics': {'sharpe': '2'}}]
    assert sel.order_seed_records(recs, mode='percentile') == [1, 2, 0]


def test_order_seed_records_nsga2_dominated_last():
    recs = [{'metrics': {'sharpe': '2', 'turnover': '0.6'}},
            {'metrics': {'sharpe': '1', 'turnover': '0.1'}},
            {'metrics': {'sharpe': '0', 'turnover': '0.9'}}]
    order = sel.order_seed_records(recs, mode='nsga2')
    assert order[-1] == 2   # 지배당한 개체 마지막


def test_greedy_diversified_promotes_distinct():
    recs = [{'code': 'rank(close)', 'metrics': {'sharpe': '3'}},
            {'code': 'rank(close)', 'metrics': {'sharpe': '2.9'}},
            {'code': 'ts_mean(volume, 10)', 'metrics': {'sharpe': '1'}}]
    sim = lambda a, b: 1.0 if a == b else 0.0   # noqa: E731
    order = sel.order_seed_records(recs, mode='percentile', lam=0.9, sim_fn=sim)
    assert order[0] == 0
    assert order[1] == 2   # 근사중복(#1) 대신 구조적으로 다른 #2 를 승격(#5)
