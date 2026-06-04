"""
tests/test_reward.py — TDD suite for server/reward.py

Pure unit tests: no DB, no IO, stdlib only.
"""
import math
import pytest

from server.reward import compute_reward, DEFAULT_WEIGHTS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strong_alpha(overrides=None):
    """Baseline all-pass, non-overfit, low-corr alpha metrics."""
    m = {'sharpe': 1.8, 'fitness': 1.2, 'turnover': 0.2, 'returns': 0.15}
    if overrides:
        m.update(overrides)
    return m


def _all_pass_kwargs(**kw):
    base = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Happy-path: all-pass strong alpha yields positive reward
# ---------------------------------------------------------------------------

def test_all_pass_strong_alpha_positive():
    r = compute_reward(
        _strong_alpha(),
        pass_count=7, fail_count=0, error_count=0, self_corr=0.2,
    )
    assert r > 0.3, f"expected > 0.3, got {r}"
    assert r <= 1.0, f"expected <= 1.0, got {r}"


# ---------------------------------------------------------------------------
# 2. all-pass gate
# ---------------------------------------------------------------------------

def test_fail_count_returns_zero():
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=1, error_count=0)
    assert r == 0.0

def test_error_count_returns_zero():
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=1)
    assert r == 0.0

def test_below_pass_threshold_returns_zero():
    r = compute_reward(_strong_alpha(), pass_count=5, fail_count=0, error_count=0)
    assert r == 0.0

def test_exactly_at_threshold_passes():
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    assert r > 0.0

def test_custom_pass_threshold():
    # With threshold=5, pass_count=5 should now pass
    r = compute_reward(
        _strong_alpha(),
        pass_count=5, fail_count=0, error_count=0, self_corr=0.2,
        pass_threshold=5,
    )
    assert r > 0.0


# ---------------------------------------------------------------------------
# 3. Too-good-to-be-true guard
# ---------------------------------------------------------------------------

def test_overfit_sharpe_returns_zero():
    r = compute_reward(
        _strong_alpha({'sharpe': 8.0}),
        **_all_pass_kwargs(),
    )
    assert r == 0.0, f"sharpe=8 should be guarded, got {r}"

def test_overfit_returns_returns_zero():
    r = compute_reward(
        _strong_alpha({'returns': 0.8}),
        **_all_pass_kwargs(),
    )
    assert r == 0.0, f"returns=0.8 should be guarded, got {r}"

def test_sharpe_just_below_overfit_guard_not_zero():
    # sharpe_overfit default = 5.0; sharpe=4.9 should NOT be guarded
    r = compute_reward(
        _strong_alpha({'sharpe': 4.9}),
        **_all_pass_kwargs(),
    )
    assert r > 0.0

def test_custom_sharpe_overfit():
    # If we lower overfit threshold to 2.0, sharpe=2.5 should be guarded
    r = compute_reward(
        _strong_alpha({'sharpe': 2.5}),
        **_all_pass_kwargs(),
        sharpe_overfit=2.0,
    )
    assert r == 0.0


# ---------------------------------------------------------------------------
# 4. Turnover inversion (delay-0 defense)
# ---------------------------------------------------------------------------

def test_high_turnover_lower_than_low_turnover():
    """turnover=0.9 > cap → turnover_term=0; turnover=0.1 → higher reward."""
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_low = compute_reward(_strong_alpha({'turnover': 0.1}), **kwargs)
    r_high = compute_reward(_strong_alpha({'turnover': 0.9}), **kwargs)
    assert r_low > r_high, f"low turnover {r_low} should exceed high turnover {r_high}"
    # high turnover may still be > 0 if other terms contribute (sharpe/fitness/returns weights)
    # but it must be strictly less

def test_turnover_at_cap_zero_term():
    """turnover == cap → turnover_term == 0 (contributes nothing to that weight)."""
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_at_cap = compute_reward(_strong_alpha({'turnover': 0.7}), **kwargs)
    r_below_cap = compute_reward(_strong_alpha({'turnover': 0.3}), **kwargs)
    assert r_below_cap > r_at_cap

def test_custom_turnover_cap():
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r = compute_reward(_strong_alpha({'turnover': 0.4}), **kwargs, turnover_cap=0.4)
    # turnover_term should be 0 at cap
    r_lower = compute_reward(_strong_alpha({'turnover': 0.2}), **kwargs, turnover_cap=0.4)
    assert r_lower > r


# ---------------------------------------------------------------------------
# 5. Self-correlation penalty
# ---------------------------------------------------------------------------

def test_self_corr_low_no_penalty():
    """corr <= 0.3 → no penalty."""
    r02 = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r03 = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.3)
    # Both should be equal (no penalty at or below 0.3)
    assert math.isclose(r02, r03, rel_tol=1e-9)

def test_self_corr_mid_reduces_reward():
    """corr=0.6 → linearly penalised, strictly less than corr=0.2."""
    r_low = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_mid = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.6)
    assert r_mid < r_low, f"corr=0.6 reward {r_mid} should be less than corr=0.2 reward {r_low}"
    assert r_mid >= 0.0

def test_self_corr_above_07_zero():
    """corr > 0.7 → heavy penalty → effectively 0.0 (cannot be submitted)."""
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.9)
    assert r == 0.0, f"corr=0.9 should yield 0.0, got {r}"

def test_self_corr_exactly_07_boundary():
    """corr = 0.7 is the submission boundary; >0.7 → 0."""
    r_at = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.7)
    r_over = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=0.71)
    assert r_over == 0.0, f"corr=0.71 should be 0.0, got {r_over}"
    # corr exactly 0.7 is borderline — at 0.7 the linear term reaches max (penalty=0.3)
    # but may still be non-negative; just check it's >= 0
    assert r_at >= 0.0

def test_self_corr_none_no_penalty():
    """self_corr=None → no penalty, still positive for all-pass."""
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0, self_corr=None)
    assert r > 0.0, f"self_corr=None should give positive reward, got {r}"


# ---------------------------------------------------------------------------
# 6. String metrics (tolerant float coercion)
# ---------------------------------------------------------------------------

def test_string_metrics_same_as_float():
    m_str = {'sharpe': '1.8', 'fitness': '1.2', 'turnover': '0.2', 'returns': '0.15'}
    m_flt = {'sharpe': 1.8,   'fitness': 1.2,   'turnover': 0.2,   'returns': 0.15}
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_str = compute_reward(m_str, **kwargs)
    r_flt = compute_reward(m_flt, **kwargs)
    assert math.isclose(r_str, r_flt, rel_tol=1e-9), f"str={r_str}, float={r_flt}"

def test_none_metric_value_treated_as_zero():
    """None metric values don't crash and are treated as 0."""
    m = {'sharpe': 1.8, 'fitness': None, 'turnover': 0.2, 'returns': 0.15}
    r = compute_reward(m, pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    # fitness=None → 0 → lower reward than normal but still > 0
    assert r >= 0.0

def test_missing_metric_key_treated_as_zero():
    """Missing keys default to 0 (no KeyError)."""
    m = {'sharpe': 1.8}   # no fitness/turnover/returns
    r = compute_reward(m, pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    assert r >= 0.0

def test_bool_metric_treated_as_zero():
    """bool values (True/False) → 0 (not 1/0 accidentally promoted)."""
    m = {'sharpe': True, 'fitness': 1.2, 'turnover': 0.2, 'returns': 0.15}
    r = compute_reward(m, pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    # sharpe=True → 0 → should still compute, not crash
    assert r >= 0.0


# ---------------------------------------------------------------------------
# 7. Determinism and monotonicity
# ---------------------------------------------------------------------------

def test_deterministic():
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r1 = compute_reward(_strong_alpha(), **kwargs)
    r2 = compute_reward(_strong_alpha(), **kwargs)
    assert r1 == r2

def test_monotonic_sharpe():
    """Higher sharpe (below overfit) → higher reward, all else equal."""
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_low = compute_reward(_strong_alpha({'sharpe': 1.2}), **kwargs)
    r_mid = compute_reward(_strong_alpha({'sharpe': 1.8}), **kwargs)
    r_high = compute_reward(_strong_alpha({'sharpe': 2.5}), **kwargs)
    assert r_low < r_mid < r_high, f"monotonicity failed: {r_low}, {r_mid}, {r_high}"

def test_monotonic_fitness():
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_low = compute_reward(_strong_alpha({'fitness': 0.8}), **kwargs)
    r_high = compute_reward(_strong_alpha({'fitness': 1.8}), **kwargs)
    assert r_low < r_high

def test_reward_always_nonnegative():
    """reward is always in [0, ~1]."""
    test_cases = [
        ({}, dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.0)),
        ({}, dict(pass_count=7, fail_count=0, error_count=0, self_corr=1.0)),
        ({'sharpe': 0.0}, dict(pass_count=7, fail_count=0, error_count=0)),
        ({'sharpe': -1.0}, dict(pass_count=7, fail_count=0, error_count=0)),
    ]
    for m_overrides, kw in test_cases:
        r = compute_reward(_strong_alpha(m_overrides), **kw)
        assert r >= 0.0, f"Negative reward {r} for {m_overrides}, {kw}"


# ---------------------------------------------------------------------------
# 8. Tunable weights
# ---------------------------------------------------------------------------

def test_custom_weights_sharpe_only():
    """With sharpe weight=1, others=0, reward ≈ norm_sharpe."""
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r = compute_reward(
        _strong_alpha({'sharpe': 1.5}),
        **kwargs,
        weights={'sharpe': 1.0, 'fitness': 0.0, 'turnover': 0.0, 'returns': 0.0},
    )
    # norm_sharpe = clamp(1.5/3.0, 0, 1) = 0.5; no penalty (corr=0.2 ≤ 0.3)
    assert math.isclose(r, 0.5, rel_tol=1e-9), f"expected 0.5, got {r}"

def test_partial_weight_override_merges_defaults():
    """Partial weight dict merges over DEFAULT_WEIGHTS."""
    kwargs = dict(pass_count=7, fail_count=0, error_count=0, self_corr=0.2)
    r_custom = compute_reward(_strong_alpha(), **kwargs, weights={'sharpe': 0.9})
    r_default = compute_reward(_strong_alpha(), **kwargs)
    # Should differ (different sharpe weight)
    assert r_custom != r_default


# ---------------------------------------------------------------------------
# 9. Edge cases
# ---------------------------------------------------------------------------

def test_all_zeros_metrics():
    """All-zero metrics, all-pass → reward 0 (all terms clamp to 0)."""
    m = {'sharpe': 0.0, 'fitness': 0.0, 'turnover': 0.0, 'returns': 0.0}
    r = compute_reward(m, pass_count=7, fail_count=0, error_count=0)
    # turnover=0 → turnover_term=1.0 (max), but sharpe/fitness/returns all 0
    # base = w_turnover * 1.0 = 0.2 > 0
    assert r > 0.0  # turnover_term still contributes

def test_empty_metrics_dict():
    """Empty dict → all keys default to 0, no crash."""
    r = compute_reward({}, pass_count=7, fail_count=0, error_count=0)
    assert r >= 0.0

def test_reward_output_type():
    """Return type is always float."""
    r = compute_reward(_strong_alpha(), pass_count=7, fail_count=0, error_count=0)
    assert isinstance(r, float)

def test_default_weights_sum_to_one():
    """DEFAULT_WEIGHTS values sum to 1.0."""
    total = sum(DEFAULT_WEIGHTS.values())
    assert math.isclose(total, 1.0, rel_tol=1e-9), f"weights sum={total}"
