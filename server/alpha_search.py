"""alpha_search — pure trajectory metrics + meta-strategy selector for round mode.
No I/O. Never raises. Modes: EXPLORE (fresh hypotheses), REFINE (directed mutation
of a near-miss), RECOMBINE (crossover of survivors), SIMPLIFY (reduce complexity)."""
from __future__ import annotations


def _floats(xs):
    out = []
    for x in (xs or []):
        try:
            v = float(x)
            if v != v:  # NaN
                continue
            out.append(v)
        except Exception:
            continue
    return out


def trajectory_metrics(scores) -> dict:
    """Return {'n','best','diversity','convergence','consecutive_declines'}.
    diversity = std/mean (coefficient of variation; 0 if mean==0 or n<2).
    convergence = (last-first)/(n-1) normalised by max|score|, clamped [-1,1].
    consecutive_declines = trailing strictly-decreasing step count."""
    s = _floats(scores)
    n = len(s)
    if n == 0:
        return {'n': 0, 'best': 0, 'diversity': 0.0, 'convergence': 0.0,
                'consecutive_declines': 0}
    best = max(s)
    if n < 2:
        return {'n': n, 'best': best, 'diversity': 0.0, 'convergence': 0.0,
                'consecutive_declines': 0}
    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    std = var ** 0.5
    diversity = (std / abs(mean)) if mean != 0 else 0.0
    scale = max((abs(x) for x in s), default=1.0) or 1.0
    slope = (s[-1] - s[0]) / (n - 1)
    convergence = max(-1.0, min(1.0, slope / scale))
    dec = 0
    for i in range(n - 1, 0, -1):
        if s[i] < s[i - 1]:
            dec += 1
        else:
            break
    return {'n': n, 'best': best, 'diversity': diversity,
            'convergence': convergence, 'consecutive_declines': dec}


def pick_mode(scores, *, has_near_miss: bool, survivor_count: int,
              max_depth_seen: int = 0) -> str:
    """Choose the round mode. Defaults to EXPLORE on any uncertainty."""
    try:
        if has_near_miss:
            return 'REFINE'
        m = trajectory_metrics(scores)
        if m['consecutive_declines'] >= 2 and (survivor_count or 0) >= 2:
            return 'RECOMBINE'
        if (max_depth_seen or 0) > 8:
            return 'SIMPLIFY'
        return 'EXPLORE'
    except Exception:
        return 'EXPLORE'
