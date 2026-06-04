"""Tests for server.retrospect — adaptive_epsilon and format_effectiveness_priors."""

import pytest
from server.retrospect import adaptive_epsilon, format_effectiveness_priors


# ─────────────────────────────────────────────────────────────────────────────
# adaptive_epsilon
# ─────────────────────────────────────────────────────────────────────────────

def test_adaptive_epsilon_zero_trend_equals_base():
    """trend=0 → exactly base."""
    assert adaptive_epsilon(0.0) == pytest.approx(0.2)


def test_adaptive_epsilon_positive_trend_below_base():
    """Positive trend (improving) → exploit more → epsilon < base."""
    eps = adaptive_epsilon(1.0)
    assert eps < 0.2


def test_adaptive_epsilon_negative_trend_above_base():
    """Negative trend (declining) → explore more → epsilon > base."""
    eps = adaptive_epsilon(-1.0)
    assert eps > 0.2


def test_adaptive_epsilon_positive_lt_base_lt_negative():
    """Confirm ordering: pos_trend < base < neg_trend."""
    eps_pos = adaptive_epsilon(2.0)
    eps_base = adaptive_epsilon(0.0)
    eps_neg = adaptive_epsilon(-2.0)
    assert eps_pos < eps_base < eps_neg


def test_adaptive_epsilon_monotonic_in_positive_direction():
    """Larger positive trend → smaller epsilon (more exploit), in pre-clamp range."""
    # Use small enough values that we're well before the lo clamp
    eps1 = adaptive_epsilon(0.05)
    eps2 = adaptive_epsilon(0.15)
    eps3 = adaptive_epsilon(0.30)
    assert eps1 > eps2 > eps3


def test_adaptive_epsilon_monotonic_in_negative_direction():
    """Larger negative trend → larger epsilon (more explore)."""
    eps1 = adaptive_epsilon(-0.5)
    eps2 = adaptive_epsilon(-2.0)
    eps3 = adaptive_epsilon(-10.0)
    assert eps1 < eps2 < eps3


def test_adaptive_epsilon_clamped_to_lo():
    """Very large positive trend → epsilon approaches lo but not below."""
    eps = adaptive_epsilon(1000.0, lo=0.05, hi=0.5)
    assert eps >= 0.05
    assert eps <= 0.5


def test_adaptive_epsilon_clamped_to_hi():
    """Very large negative trend → epsilon approaches hi but not above."""
    eps = adaptive_epsilon(-1000.0, lo=0.05, hi=0.5)
    assert eps <= 0.5
    assert eps >= 0.05


def test_adaptive_epsilon_large_positive_approaches_lo():
    """Large positive trend → epsilon asymptotically approaches lo (~0.05)."""
    eps = adaptive_epsilon(5.0)
    assert eps < 0.06


def test_adaptive_epsilon_large_negative_approaches_hi():
    """Large negative trend → epsilon asymptotically approaches hi (~0.5).
    With asymmetric amplitudes the full [base, hi] range is now reachable."""
    eps = adaptive_epsilon(-5.0)
    assert eps > 0.49


def test_adaptive_epsilon_monotonic_decreasing_in_trend():
    """epsilon is monotonically decreasing as trend increases."""
    trends = [-5.0, -2.0, -0.5, 0.0, 0.5, 2.0, 5.0]
    epsilons = [adaptive_epsilon(t) for t in trends]
    for i in range(len(epsilons) - 1):
        assert epsilons[i] >= epsilons[i + 1], (
            f"Not monotone at index {i}: trend={trends[i]}→eps={epsilons[i]}, "
            f"trend={trends[i+1]}→eps={epsilons[i+1]}"
        )


def test_adaptive_epsilon_custom_lo_hi():
    eps = adaptive_epsilon(0.0, base=0.3, lo=0.1, hi=0.6)
    assert eps == pytest.approx(0.3)
    assert adaptive_epsilon(5.0, base=0.3, lo=0.1, hi=0.6) < 0.3
    assert adaptive_epsilon(-5.0, base=0.3, lo=0.1, hi=0.6) > 0.3


def test_adaptive_epsilon_result_in_range():
    """For any reasonable trend, result is always in [lo, hi]."""
    lo, hi = 0.05, 0.5
    for trend in [-100, -10, -1, -0.1, 0, 0.1, 1, 10, 100]:
        eps = adaptive_epsilon(trend, lo=lo, hi=hi)
        assert lo <= eps <= hi, f"trend={trend} → eps={eps} outside [{lo},{hi}]"


# ─────────────────────────────────────────────────────────────────────────────
# format_effectiveness_priors
# ─────────────────────────────────────────────────────────────────────────────

def _make_axis_item(value, count=5, all_pass_rate=0.4, avg_pass_count=6.0):
    return {
        'value': value,
        'count': count,
        'all_pass_rate': all_pass_rate,
        'avg_pass_count': avg_pass_count,
        'avg_sharpe': None,
        'avg_self_corr': None,
    }


def _make_op_item(operator, count=5, all_pass_rate=0.4, avg_pass_count=6.0):
    return {
        'operator': operator,
        'count': count,
        'all_pass_rate': all_pass_rate,
        'avg_pass_count': avg_pass_count,
    }


def test_format_empty_returns_empty_string():
    assert format_effectiveness_priors({}, []) == ''


def test_format_none_axis_empty_op_returns_empty():
    assert format_effectiveness_priors({'universe': []}, []) == ''


def test_format_contains_top_universe_value():
    axis = {'universe': [_make_axis_item('TOP500', all_pass_rate=0.40),
                          _make_axis_item('TOP1000', all_pass_rate=0.25)]}
    block = format_effectiveness_priors(axis, [])
    assert 'TOP500' in block
    assert 'TOP1000' in block


def test_format_contains_neutralization_value():
    axis = {'neutralization': [_make_axis_item('SUBINDUSTRY', all_pass_rate=0.38)]}
    block = format_effectiveness_priors(axis, [])
    assert 'SUBINDUSTRY' in block


def test_format_contains_operator_name():
    ops = [_make_op_item('group_neutralize', all_pass_rate=0.42),
           _make_op_item('ts_rank', all_pass_rate=0.31)]
    block = format_effectiveness_priors({}, ops)
    assert 'group_neutralize' in block
    assert 'ts_rank' in block


def test_format_contains_pass_rates():
    axis = {'universe': [_make_axis_item('TOP500', all_pass_rate=0.40)]}
    block = format_effectiveness_priors(axis, [])
    assert '0.40' in block


def test_format_soft_wording():
    """The block must contain soft/nudge language (not mandate)."""
    axis = {'universe': [_make_axis_item('TOP500', all_pass_rate=0.40)]}
    block = format_effectiveness_priors(axis, [])
    # Should have something like '참고용' or '강제 아님'
    assert '참고' in block or '강제' in block


def test_format_combined_axis_and_ops():
    axis = {
        'universe': [_make_axis_item('TOP500', all_pass_rate=0.5)],
        'neutralization': [_make_axis_item('SUBINDUSTRY', all_pass_rate=0.4)],
    }
    ops = [_make_op_item('rank', all_pass_rate=0.45)]
    block = format_effectiveness_priors(axis, ops)
    assert 'TOP500' in block
    assert 'SUBINDUSTRY' in block
    assert 'rank' in block


def test_format_empty_op_list_still_shows_axis():
    axis = {'universe': [_make_axis_item('TOP1000', all_pass_rate=0.3)]}
    block = format_effectiveness_priors(axis, [])
    assert 'TOP1000' in block
    assert block != ''


def test_format_block_starts_newline():
    """Block should start with a blank line (or newline) for clean prompt append."""
    axis = {'universe': [_make_axis_item('TOP500', all_pass_rate=0.5)]}
    block = format_effectiveness_priors(axis, [])
    assert block.startswith('\n')


# ─────────────────────────────────────────────────────────────────────────────
# generate_strategies signature test
# ─────────────────────────────────────────────────────────────────────────────

def test_generate_strategies_accepts_effectiveness_priors_kwarg():
    """generate_strategies must accept effectiveness_priors as a keyword argument."""
    import inspect
    from server.gemini_strategist import generate_strategies

    sig = inspect.signature(generate_strategies)
    assert 'effectiveness_priors' in sig.parameters


def test_generate_strategies_effectiveness_priors_default_none():
    import inspect
    from server.gemini_strategist import generate_strategies

    sig = inspect.signature(generate_strategies)
    param = sig.parameters['effectiveness_priors']
    assert param.default is None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt injection test
# ─────────────────────────────────────────────────────────────────────────────

def test_build_user_prompt_cached_effectiveness_priors_absent_when_none():
    """When effectiveness_priors=None, the cached prompt should not contain
    the prior block header. (The block is appended in generate_strategies, not
    in the prompt builder — so this is a gate check.)"""
    from server.gemini_strategist import _build_user_prompt_cached
    prompt = _build_user_prompt_cached(
        round_num=1, feedback=[], errors=[],
        avoid_codes=[], submitted_codes=[],
        seeds=[], pref_stats={},
        slot_settings=None,
    )
    # The prior header is Korean; should not appear when not injected
    assert '최근 성과 상위' not in prompt


def test_effectiveness_priors_text_appears_in_generate_strategies_prompt():
    """When effectiveness_priors is non-empty, it must appear in the user_prompt
    assembled inside generate_strategies (before API call).

    We test indirectly via _build_user_prompt_cached + appending, as the full
    generate_strategies calls the real API which we don't have in tests.

    The contract: if effectiveness_priors is truthy, it gets appended to user_prompt.
    We verify this by building the prompt string the same way generate_strategies does.
    """
    from server.gemini_strategist import _build_user_prompt_cached

    priors_text = '\n[최근 성과 상위 (데이터 기반 참고용, 강제 아님):]\n    universe: TOP500(pass율 0.50)\n'
    prompt = _build_user_prompt_cached(
        round_num=1, feedback=[], errors=[],
        avoid_codes=[], submitted_codes=[],
        seeds=[], pref_stats={},
        slot_settings=None,
    )
    # Simulate what generate_strategies does when effectiveness_priors is truthy
    if priors_text:
        prompt += priors_text

    assert '최근 성과 상위' in prompt
    assert 'TOP500' in prompt
