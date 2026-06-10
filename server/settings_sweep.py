"""settings_sweep — deterministic (universe × neutralization) sweep of a parent
alpha's EXACT formula, run BEFORE spending Gemini calls in a focus round.

Why this exists
---------------
At delay=0 the datafields are locked to the Price-Volume set (fundamental/analyst
fields ERROR), so the formula space is narrow and the *only* extra edge lever is
the simulation **settings** — chiefly universe (how many names) and neutralization
(what risk is removed). The SAME signal can go from Sharpe 0.4 (TOP3000/INDUSTRY)
to a passing Sharpe under a tighter universe + finer neutralization that strips
more noise. LLM guessing wastes a generation call per probe; this sweeps the grid
deterministically with NO Gemini cost. Already-tried combos become free cache hits
downstream (cache key = code_hash + settings_fingerprint).

Pure: never raises, no I/O.
"""
from __future__ import annotations

# Ordered by typical delay=0 Sharpe leverage: finer neutralization first
# (removes more cross-sectional noise → higher Sharpe). Every combo is DISTINCT
# after the worker's _sanitize_settings, which folds TOP200/TOP500 × (INDUSTRY/
# SUBINDUSTRY) → SECTOR (too few names per group). We therefore pair fine
# neutralization only with TOP1000/TOP3000, and TOP200/TOP500 only with
# SECTOR/MARKET — so no two sweeps collapse to identical effective settings.
_GRID: list[tuple[str, str]] = [
    ('TOP1000', 'SUBINDUSTRY'),
    ('TOP3000', 'SUBINDUSTRY'),
    ('TOP1000', 'INDUSTRY'),
    ('TOP500', 'SECTOR'),
    ('TOP3000', 'INDUSTRY'),
    ('TOP200', 'SECTOR'),
    ('TOP1000', 'SECTOR'),
    ('TOP3000', 'MARKET'),
]

# settings keys we carry over from the parent so its tuned smoothing is preserved
# (we only vary universe + neutralization; delay is forced by the round).
_INHERIT_KEYS = ('decay', 'truncation', 'pasteurization', 'nan_handling')


def _norm(v) -> str:
    return str(v).strip().upper() if v is not None else ''


def sweep_candidates(parent_code, parent_settings=None, *, n=3, seed=0,
                     start_idx=101):
    """Return up to *n* sweep strategy dicts for *parent_code*.

    Each dict mirrors a Gemini strategy: {idx, code, desc, settings, sweep}.
    The parent's own (universe, neutralization) combo is skipped (re-running it
    is pointless). decay/truncation/etc. are inherited from parent_settings so
    the parent's tuned smoothing carries over. Deterministic given (seed, n).
    Returns [] on empty code or n<=0; never raises.
    """
    try:
        if not isinstance(parent_code, str) or not parent_code.strip():
            return []
        n = int(n)
        if n <= 0:
            return []
        ps = parent_settings if isinstance(parent_settings, dict) else {}
        parent_combo = (_norm(ps.get('universe')), _norm(ps.get('neutralization')))
        inherited = {
            k: str(ps[k]).strip()
            for k in _INHERIT_KEYS
            if ps.get(k) is not None and str(ps[k]).strip()
        }

        grid = _GRID
        offset = (int(seed) % len(grid)) if grid else 0
        rotated = grid[offset:] + grid[:offset]

        out: list[dict] = []
        for uni, neut in rotated:
            if len(out) >= n:
                break
            if (uni, neut) == parent_combo:
                continue  # identical to parent — no new information
            settings = dict(inherited)
            settings['universe'] = uni
            settings['neutralization'] = neut
            out.append({
                'idx': start_idx + len(out),
                'code': parent_code.strip(),
                'desc': f'⚙ settings 스윕: {uni}×{neut} (부모 동일 공식, 노이즈/유니버스 레버)',
                'settings': settings,
                'sweep': True,
            })
        return out
    except Exception:
        return []
