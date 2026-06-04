"""focus_priority — Closeness-to-pass scoring for the focus queue.

Convention
----------
closeness_score(fail_items) -> float

  Higher means CLOSER to passing — use this as a sort key with reverse=True
  to get near-misses first.

  Formula: score = -sum(relative_gaps)   when at least one item parses
           score = NEUTRAL_SCORE         when no items parse (neutral sentinel)

  For each fail item that can be parsed as:
    'of VALUE is [below|above] cutoff of CUTOFF'
  we compute:
    relative_gap = abs(cutoff - value) / max(abs(cutoff), 1e-9)

  Near-miss example:  Sharpe 1.20 vs cutoff 1.25 → gap ≈ 0.04 → score ≈ -0.04
  Far-miss  example:  Fitness 0.30 vs cutoff 1.0  → gap = 0.70 → score = -0.70
  Neutral   (no parseable items)                  → score = -1e9 (sorts last)

Usage in worker.py (safe, additive — can't crash a round):

    from server.focus_priority import closeness_score
    focus_queue = sorted(
        focus_queue,
        key=lambda e: closeness_score(e.get('parent_fail_items') or []),
        reverse=True,
    )
"""

from __future__ import annotations

import re

# Regex: captures VALUE and CUTOFF from WQB fail strings such as
#   'Fitness of 0.91 is below cutoff of 1.'
#   'Sharpe of 1.10 is below cutoff of 1.25.'
#   'Sub-universe Sharpe of 0.78 is below cutoff of 1.0.'
NEUTRAL_SCORE: float = -1e9
"""Sentinel returned when no fail items are parseable; sorts last in a reverse sort."""

# Regex: captures VALUE and CUTOFF from WQB fail strings such as
#   'Fitness of 0.91 is below cutoff of 1.'
#   'Sharpe of 1.10 is below cutoff of 1.25.'
#   'Sub-universe Sharpe of 0.78 is below cutoff of 1.0.'
#   'Turnover of 27.24% is above cutoff of 1%.'   (trailing % stripped)
_FAIL_RX = re.compile(
    r'of\s+(\d+(?:\.\d+)?)%?\s+is\s+(?:below|above)\s+cutoff\s+of\s+(\d+(?:\.\d+)?)%?',
    re.IGNORECASE,
)


def closeness_score(fail_items: object) -> float:
    """Compute closeness score for a list of WQB fail description strings.

    Returns a float:
      - Closer to 0  => near-miss (small gap between value and cutoff).
      - More negative => farther from passing.
      - NEUTRAL_SCORE  => neutral sentinel (no parseable items; sorts last).

    Safe: never raises; accepts any input type.
    """
    if not fail_items:
        return NEUTRAL_SCORE  # neutral sentinel — sorts last

    # Accept any iterable of items; coerce each to str.
    try:
        items = list(fail_items)
    except TypeError:
        return NEUTRAL_SCORE  # neutral sentinel

    total_gap = 0.0
    parsed = 0

    for item in items:
        try:
            text = str(item)
        except Exception:
            continue

        m = _FAIL_RX.search(text)
        if not m:
            continue

        try:
            value = float(m.group(1))
            cutoff = float(m.group(2))
        except (ValueError, IndexError):
            continue

        gap = abs(cutoff - value) / max(abs(cutoff), 1e-9)
        total_gap += gap
        parsed += 1

    if parsed == 0:
        # Neutral: no parseable items.  Use a large negative sentinel so these
        # entries sort AFTER any entry with a measurable gap (near or far), since
        # at least some gap information is better than none.
        return NEUTRAL_SCORE

    return -total_gap  # higher (less negative) => closer to pass
