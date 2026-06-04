"""alpha_mutate — Pure, deterministic evolutionary mutation engine for WQB alpha expressions.

Public API
----------
numeric_param_variants(code, *, max_variants=12) -> list[str]
    Vary numeric literal parameters one-at-a-time (integer or float).

negate(code) -> str
    Return 'subtract(0, (CODE))' — robust sign-flip.

swap_variants(code) -> list[str]
    One-at-a-time curated operator/field swaps.

mutate(code, *, max_variants=16, include_negation=True) -> list[str]
    Combine all strategies; dedupe; cap; deterministic.

focus_priority helpers are in server/focus_priority.py (imported below for convenience).
"""

from __future__ import annotations

import re
from typing import Sequence

# ---------------------------------------------------------------------------
# Regex for numeric parameter sites.
# Matches a number preceded by ',', '(', or whitespace.
# Negative-lookahead '(?![A-Za-z_])' ensures we do NOT match numbers that are
# immediately followed by a letter, which would mean they are part of an
# identifier (e.g. the '20' in 'adv20').
# ---------------------------------------------------------------------------
_PARAM_NUM_RX = re.compile(
    r'(?<=[,(\s])(-?\d*\.?\d+)(?![A-Za-z0-9_])'
)

# Bounds / sanity
_INT_MIN = 1          # lookback windows must be >= 1
_INT_MAX = 500
_FLOAT_MIN = 1e-9
_FLOAT_MAX = 1e6


def _is_int(s: str) -> bool:
    """True when s represents an integer (no decimal point, or trailing .0 exactly)."""
    try:
        f = float(s)
        return f == int(f) and '.' not in s
    except ValueError:
        return False


def _int_candidates(v: int) -> list[int]:
    """Neighbour integers for a window/lookback parameter."""
    step = max(1, v // 5)
    raw = [v - step, v + step, round(v * 1.5)]
    return sorted({int(c) for c in raw if _INT_MIN <= c <= _INT_MAX and c != v})


def _float_candidates(v: float) -> list[float]:
    """Neighbour floats for a decay/threshold parameter."""
    raw = [round(v * 0.9, 1), round(v * 1.1, 1)]
    return sorted({c for c in raw if _FLOAT_MIN <= c <= _FLOAT_MAX and c != v})


def numeric_param_variants(code: str, *, max_variants: int = 12) -> list[str]:
    """Return code variants where ONE numeric parameter site is altered at a time.

    Identifier-embedded numbers (e.g. 'adv20') are NOT touched because the
    regex requires the number to be preceded by ',', '(', or whitespace — the
    character class of argument separators — and NOT followed by a letter/digit
    or underscore.

    Returns list[str] of variant codes (excluding the original); capped at
    *max_variants*.  Returns [] on empty/non-str input without raising.
    """
    if not isinstance(code, str) or not code.strip():
        return []

    # Find all match objects with their spans.
    matches = list(_PARAM_NUM_RX.finditer(code))
    if not matches:
        return []

    variants: list[str] = []

    for m in matches:
        raw = m.group(1)
        start, end = m.start(1), m.end(1)

        try:
            fval = float(raw)
        except ValueError:
            continue

        if _is_int(raw):
            candidates = [str(c) for c in _int_candidates(int(fval))]
        else:
            candidates = [str(c) for c in _float_candidates(fval)]

        for cand in candidates:
            variant = code[:start] + cand + code[end:]
            if variant != code and variant not in variants:
                variants.append(variant)
                if len(variants) >= max_variants:
                    return variants

    return variants[:max_variants]


def negate(code: str) -> str:
    """Return sign-flipped alpha: 'subtract(0, (CODE))'.

    Uses an explicit wrapper so complex expressions are not mis-parsed.
    Returns '' for non-string or empty input (safe, won't raise).
    """
    if not isinstance(code, str) or not code.strip():
        return ''
    return f'subtract(0, ({code.strip()}))'


# ---------------------------------------------------------------------------
# Swap map — interchangeable operators / field names (whole-token only).
# Each entry is a frozenset of equivalents; any member can swap to any other.
# NOTE: Groups are DELIBERATELY loose — they exploit structural similarity to
# explore nearby alpha regions, NOT strict semantic equivalents.  Do not prune
# them on correctness grounds; the sim sandbox is the real gatekeeper.
# ---------------------------------------------------------------------------
_SWAP_GROUPS: list[frozenset[str]] = [
    frozenset({'ts_delta', 'ts_rank'}),
    frozenset({'ts_mean', 'ts_zscore'}),
    frozenset({'vec_max', 'vec_avg', 'vec_sum'}),
    frozenset({'adv20', 'adv60', 'adv120'}),
]


def _word_boundary_replace(code: str, old: str, new: str) -> str:
    """Replace all whole-token occurrences of *old* with *new* in *code*."""
    return re.sub(r'\b' + re.escape(old) + r'\b', new, code)


def swap_variants(code: str) -> list[str]:
    """Return code variants produced by one curated token swap at a time.

    Only whole-token matches (word boundaries) are replaced.
    Returns [] on empty/non-str input without raising.
    """
    if not isinstance(code, str) or not code.strip():
        return []

    variants: list[str] = []

    for group in _SWAP_GROUPS:
        for token in sorted(group):   # deterministic iteration order
            # Only act if this token actually appears in the code.
            if not re.search(r'\b' + re.escape(token) + r'\b', code):
                continue
            for replacement in sorted(group):
                if replacement == token:
                    continue
                variant = _word_boundary_replace(code, token, replacement)
                if variant != code and variant not in variants:
                    variants.append(variant)

    return variants


def mutate(code: str, *, max_variants: int = 16, include_negation: bool = True) -> list[str]:
    """Combine numeric_param_variants + swap_variants (+ negation if requested).

    Ordering (deterministic, stable):
      1. numeric variants (param changes, by match order)
      2. swap variants (group order, then token order within group)
      3. negation (last, if include_negation=True)

    Dedupes against the original code and against each other.
    Caps at *max_variants*.
    Returns [] for empty/non-str input without raising.
    """
    if not isinstance(code, str) or not code.strip():
        return []

    seen: set[str] = {code}
    results: list[str] = []

    def _add(v: str) -> bool:
        if v and v not in seen and len(results) < max_variants:
            seen.add(v)
            results.append(v)
            return True
        return False

    # 1. Numeric variants
    for v in numeric_param_variants(code, max_variants=max_variants):
        _add(v)
        if len(results) >= max_variants:
            return results

    # 2. Swap variants
    for v in swap_variants(code):
        _add(v)
        if len(results) >= max_variants:
            return results

    # 3. Negation
    if include_negation:
        neg = negate(code)
        _add(neg)

    return results
