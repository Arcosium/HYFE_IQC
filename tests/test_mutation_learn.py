"""server/mutation_learn.py — 정향변이 온라인 학습 (categorize / Thompson / 채점).

Run: python3 -m pytest tests/test_mutation_learn.py -v
"""
import random

from server import mutation_learn as ml


# ── categorize ───────────────────────────────────────────────────────────────

def test_categorize_maps_wqb_fail_strings():
    assert ml.categorize(['Turnover(0.85>0.7)']) == ['turnover_high']
    assert ml.categorize(['Turnover(0.005<0.01)']) == ['turnover_low']
    assert ml.categorize(['Turnover of 0.5% is below cutoff of 1%']) == ['turnover_low']
    assert ml.categorize(['Sub-universe Sharpe(0.4<1.0)']) == ['sub_universe']
    assert ml.categorize(['LOW_SUB_UNIVERSE_SHARPE']) == ['sub_universe']
    assert ml.categorize(['Self-correlation of 0.94 is above cutoff']) == ['correlation']
    assert ml.categorize(['Weight concentration']) == ['concentration']
    # Fitness 는 'signal' 에서 떼어냈다 (2026-07-14) — 라이브 병목이 정확히 Fitness 인데
    # 무작위 신호 변이로 뭉뚱그려지고 있었다. 전용 축 'boost'(returns↑) 가 받는다.
    assert ml.categorize(['Sharpe(-0.06<1.25)', 'Fitness(0.1<1.0)']) == ['signal', 'fitness']
    # 2Y Sharpe / ladder = 시간 안정성. 이름에 'sharpe' 가 들어 있어 signal 로 새고 있었다.
    assert ml.categorize(['LOW_2Y_SHARPE']) == ['stability']
    assert ml.categorize(['IS_LADDER_SHARPE']) == ['stability']
    assert ml.categorize(['LOW_GLB_EMEA_SHARPE']) == ['regional']
    assert ml.categorize(['알 수 없는 항목']) == []
    assert ml.categorize(None) == []


def test_categorize_matches_rule_directive_keys():
    """모든 category 는 RULE_DIRECTIVE 에 매핑돼야 한다 (KeyError 방지 계약)."""
    samples = ['Turnover(0.85>0.7)', 'Turnover(0.005<0.01)', 'Sub-universe Sharpe(0.4<1.0)',
               'Self-correlation 0.9', 'Weight concentration', 'Sharpe(-0.06<1.25)',
               'Margin(0<0)', 'Drawdown(-0.4<-0.3)', 'Returns(0.01<0.05)']
    for c in ml.categorize(samples):
        assert c in ml.RULE_DIRECTIVE
        assert ml.RULE_DIRECTIVE[c] in ml.DIRECTIVES


# ── choose_directive — Thompson sampling ─────────────────────────────────────

def test_choose_no_fail_items_returns_none():
    assert ml.choose_directive([], {}, random.Random(0)) is None
    assert ml.choose_directive(None, None, random.Random(0)) is None


def test_choose_cold_start_prefers_rule_directive():
    """관측이 없으면 사전확률(규칙 Beta(3,1) vs 그 외 Beta(1,3))이 지배 —
    turnover 초과 부모는 대부분 smooth 를 골라야 한다 (기존 규칙과 동등 이상)."""
    picks = [ml.choose_directive(['Turnover(0.9>0.7)'], {}, random.Random(seed))
             for seed in range(300)]
    assert all(p in ml.DIRECTIVES for p in picks)
    assert picks.count('smooth') / len(picks) >= 0.5
    # 하지만 소량의 탐색(다른 축 선택)도 있어야 한다 — 순수 결정론이면 학습이 못 큼.
    assert len(set(picks)) >= 2


def test_choose_learns_when_data_contradicts_rule():
    """관측이 '규칙(smooth)은 안 먹히고 signal 이 고친다'고 말하면 데이터가 이긴다."""
    stats = {
        ('turnover_high', 'smooth'): {'n': 60, 'wins': 3},
        ('turnover_high', 'signal'): {'n': 60, 'wins': 50},
    }
    picks = [ml.choose_directive(['Turnover(0.9>0.7)'], stats, random.Random(seed))
             for seed in range(300)]
    assert picks.count('signal') / len(picks) >= 0.7


def test_choose_deterministic_given_same_rng_seed():
    stats = {('signal', 'signal'): {'n': 10, 'wins': 5}}
    a = ml.choose_directive(['Sharpe(0.5<1.25)'], stats, random.Random(42))
    b = ml.choose_directive(['Sharpe(0.5<1.25)'], stats, random.Random(42))
    assert a == b


def test_choose_pools_multiple_fail_categories():
    """turnover+sharpe 동시 실패 부모 — 두 category 관측을 합산해 표집한다.

    signal 도 규칙 사전확률(Beta(3,1))을 받는 후보라 절대 다수까지는 요구하지
    않는다 — 관측 66/80 을 가진 smooth 가 '최다 득표'면 합산이 동작하는 것이다.
    """
    stats = {
        ('turnover_high', 'smooth'): {'n': 40, 'wins': 36},
        ('signal', 'smooth'): {'n': 40, 'wins': 30},
    }
    picks = [ml.choose_directive(['Turnover(0.9>0.7)', 'Sharpe(0.5<1.25)'],
                                 stats, random.Random(seed))
             for seed in range(200)]
    counts = {d: picks.count(d) for d in ml.DIRECTIVES}
    assert max(counts, key=counts.get) == 'smooth'
    assert counts['smooth'] / len(picks) >= 0.4


# ── outcome_observations — 채점 규칙 ─────────────────────────────────────────

def _p(fails, pc):
    return {'fail_items': fails, 'pass_count': pc}


def test_outcome_win_when_target_resolved_and_no_regression():
    obs = ml.outcome_observations(
        _p(['Turnover'], 6),
        {'directive': 'smooth', 'fail_items': [], 'pass_count': 7, 'error_text': ''})
    assert obs == [('turnover_high', 'smooth', True)]


def test_outcome_loss_when_target_persists():
    obs = ml.outcome_observations(
        _p(['Turnover'], 6),
        {'directive': 'smooth', 'fail_items': ['Turnover'], 'pass_count': 6,
         'error_text': ''})
    assert obs == [('turnover_high', 'smooth', False)]


def test_outcome_loss_when_child_regressed_overall():
    """표적은 고쳤어도 pass_count 가 후퇴하면(다른 걸 부숨) 성공으로 치지 않는다."""
    obs = ml.outcome_observations(
        _p(['Turnover'], 6),
        {'directive': 'smooth', 'fail_items': ['Sharpe'], 'pass_count': 4,
         'error_text': ''})
    assert obs == [('turnover_high', 'smooth', False)]


def test_outcome_loss_when_child_errored():
    obs = ml.outcome_observations(
        _p(['Turnover'], 6),
        {'directive': 'smooth', 'fail_items': [], 'pass_count': 0,
         'error_text': 'sim exception'})
    assert obs == [('turnover_high', 'smooth', False)]


def test_outcome_one_observation_per_parent_category():
    obs = ml.outcome_observations(
        _p(['Turnover', 'Sharpe'], 5),
        {'directive': 'smooth', 'fail_items': ['Sharpe'], 'pass_count': 6,
         'error_text': ''})
    assert ('turnover_high', 'smooth', True) in obs
    assert ('signal', 'smooth', False) in obs
    assert len(obs) == 2


def test_outcome_empty_without_directive_or_parent_fails():
    assert ml.outcome_observations(_p(['Turnover'], 6),
                                   {'directive': '', 'fail_items': []}) == []
    assert ml.outcome_observations(_p([], 6),
                                   {'directive': 'smooth', 'fail_items': []}) == []


def test_regional_outcome_uses_weakest_region_improvement():
    parent = {
        'fail_items': ['LOW_GLB_EMEA_SHARPE'], 'pass_count': 6,
        'metrics': {'glb_amer_sharpe': '1.25', 'glb_emea_sharpe': '0.76',
                    'glb_apac_sharpe': '1.02'},
    }
    child = {
        'directive': 'region_balance', 'fail_items': ['LOW_GLB_EMEA_SHARPE'],
        'pass_count': 6, 'error_text': '',
        'metrics': {'glb_amer_sharpe': '1.18', 'glb_emea_sharpe': '0.90',
                    'glb_apac_sharpe': '1.04'},
    }
    assert ml.outcome_observations(parent, child) == [
        ('regional', 'region_balance', True)]
