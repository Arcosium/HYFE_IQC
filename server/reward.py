"""
server/reward.py — Composite reward scalar for HYFE_IQC alpha evaluation.

Purpose
-------
Single tunable scalar used downstream for:
  - Bandit arm weight updates
  - Mutation-parent selection
  - Seeding rank

Design goals
------------
- Rewards SUBMITTABLE, decorrelated, low-turnover, all-pass alphas.
- Does NOT reward raw Sharpe in isolation.
- Attacks delay-0 turnover blowups via turnover-INVERSION term.
- Bakes in the real WQB submission bar (all-pass + self-corr ≤ 0.7).
- Pure: no IO, no DB, no side-effects. stdlib only.

Formula (when all gates pass)
------------------------------
  base = w_sharpe * norm_sharpe
       + w_fitness * norm_fitness
       + w_turnover * turnover_term
       + w_returns * norm_returns

  norm_sharpe    = clamp(sharpe  / SHARPE_REF,  0, 1)   # ref = 3.0
  norm_fitness   = clamp(fitness / FITNESS_REF, 0, 1)   # ref = 2.0
  norm_returns   = clamp(returns / RETURNS_REF, 0, 1)   # ref = 0.3
  turnover_term  = 1.0 - min(turnover / turnover_cap, 1.0)   # inversion

  self_corr penalty:
    corr ≤ 0.3          → penalty = 0.0
    0.3 < corr ≤ 0.7   → penalty = 0.3 * (corr - 0.3) / 0.4   (linear, up to 0.3)
    corr > 0.7          → reward = 0.0  (cannot be submitted)

  reward = max(0.0, base - penalty)

Gates (return 0.0 immediately)
-------------------------------
  1. all-pass gate: fail_count > 0  OR  error_count > 0  OR  pass_count < pass_threshold
  2. too-good guard: sharpe > sharpe_overfit  OR  returns > 0.5  (likely lookahead/overfit)
"""

# Python 3.9 런타임(서버 = /usr/bin/python3 = 3.9) 호환: 시그니처의 `dict | None`
# 같은 PEP604 union 을 지연 평가(문자열)로 처리해 import 시 TypeError 를 방지한다.
from __future__ import annotations

# ── Tunable module-level reference constants ─────────────────────────────────
SHARPE_REF: float = 3.0   # denominator for norm_sharpe
FITNESS_REF: float = 2.0  # denominator for norm_fitness
RETURNS_REF: float = 0.3  # denominator for norm_returns

# ── Default component weights (must sum to 1.0) ──────────────────────────────
DEFAULT_WEIGHTS: dict = {
    'sharpe':   0.4,
    'fitness':  0.3,
    'turnover': 0.2,
    'returns':  0.1,
}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _f(v) -> float:
    """
    Tolerant float coercion.
    - bool → 0.0  (avoids True==1 / False==0 accidents)
    - None → 0.0
    - str  → float(str) if parseable, else 0.0
    - int/float → float, but bool subclass intercepted above
    """
    if isinstance(v, bool):
        return 0.0
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# ── Public API ────────────────────────────────────────────────────────────────

def compute_reward(
    metrics: dict,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    error_count: int = 0,
    self_corr=None,
    weights: dict | None = None,
    turnover_cap: float = 0.7,
    pass_threshold: int = 7,
    sharpe_overfit: float = 5.0,
) -> float:
    """
    Compute the composite reward scalar for a single alpha result.

    Parameters
    ----------
    metrics : dict
        Scraped WQB metrics. Keys used: 'sharpe', 'fitness', 'turnover', 'returns'.
        Values may be str, int, float, or None; tolerantly coerced.
    pass_count : int
        Number of WQB IS-test checks that passed.
    fail_count : int
        Number of failed checks. Any fail → 0.0.
    error_count : int
        Number of errored checks. Any error → 0.0.
    self_corr : float | str | None
        Self-correlation (Maximum) from the WQB correlation panel.
        None = not measured; no penalty applied.
    weights : dict | None
        Override specific weight keys over DEFAULT_WEIGHTS (partial OK).
    turnover_cap : float
        Turnover level at which turnover_term → 0. Default 0.7 (70 %).
    pass_threshold : int
        Minimum pass_count required for all-pass gate. Default 7.
    sharpe_overfit : float
        Sharpe above this value triggers too-good guard. Default 5.0.

    Returns
    -------
    float
        Reward in [0.0, ~1.0].  Always non-negative.
    """
    # ── 1. Merge weights ────────────────────────────────────────────────────
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    # ── 2. Coerce raw metric values ─────────────────────────────────────────
    sharpe   = _f(metrics.get('sharpe'))
    fitness  = _f(metrics.get('fitness'))
    turnover = _f(metrics.get('turnover'))
    returns  = _f(metrics.get('returns'))

    # ── 3. Too-good-to-be-true guard ────────────────────────────────────────
    if sharpe > sharpe_overfit or returns > 0.5:
        return 0.0

    # ── 4. all-pass gate ────────────────────────────────────────────────────
    all_pass = (fail_count == 0 and error_count == 0 and pass_count >= pass_threshold)
    if not all_pass:
        return 0.0

    # ── 5. Normalised component scores ──────────────────────────────────────
    norm_sharpe   = _clamp(sharpe  / SHARPE_REF,  0.0, 1.0)
    norm_fitness  = _clamp(fitness / FITNESS_REF, 0.0, 1.0)
    norm_returns  = _clamp(returns / RETURNS_REF, 0.0, 1.0)

    # Turnover inversion: lower turnover → higher score
    # turnover >= cap → term = 0; turnover = 0 → term = 1
    turnover_term = 1.0 - min(turnover / turnover_cap, 1.0) if turnover_cap > 0 else 0.0

    # ── 6. Base score ────────────────────────────────────────────────────────
    base = (
        w.get('sharpe',   DEFAULT_WEIGHTS['sharpe'])   * norm_sharpe
        + w.get('fitness',  DEFAULT_WEIGHTS['fitness'])  * norm_fitness
        + w.get('turnover', DEFAULT_WEIGHTS['turnover']) * turnover_term
        + w.get('returns',  DEFAULT_WEIGHTS['returns'])  * norm_returns
    )

    # ── 7. Self-correlation penalty ──────────────────────────────────────────
    penalty = 0.0
    if self_corr is not None:
        corr = _f(self_corr)
        if corr > 0.7:
            # Heavy penalty: above submission gate → 0
            return 0.0
        elif corr > 0.3:
            # Linear penalty: 0 at 0.3 → 0.3 at 0.7
            penalty = 0.3 * (corr - 0.3) / 0.4
        # else: corr <= 0.3 → no penalty

    # ── 8. Final reward ──────────────────────────────────────────────────────
    return float(max(0.0, base - penalty))
