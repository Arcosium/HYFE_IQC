"""presim_gate — structural decorrelation + complexity screen run BEFORE spending a
WQB simulation slot. Pure; never raises. Conservative: drop only clear near-duplicates
/ budget-busters, and report a reason for every drop (no silent caps)."""
from __future__ import annotations

from . import alpha_ast

# Complexity caps are a RARE backstop for pathological alphas only — NOT a quality
# filter. The P1 prompt encourages multi-dimensional composites, so legit alphas run
# 250-350 chars and 9-12 fields (multi-statement `;` syntax inflates length). Tight
# caps (240/8) dropped 80-90% of a round's alphas → throughput collapse (live r563/564:
# 10→2 simulated). Per diversity-over-safety, let WQB judge complexity; only block truly
# pathological exprs here. Structural decorrelation (overlap) stays as the real lever.
_DEFAULTS = {
    'overlap_drop': 5,   # catches same-template near-dups (e.g. 6-node ts_corr with diff window)
    'max_symbol_length': 450,
    'max_base_features': 14,
    'max_free_const_ratio': 0.6,
}


def screen(candidates, existing_codes=None, opts=None):
    """Return (kept, dropped). dropped items are the candidate dict + {'reason': str}."""
    o = dict(_DEFAULTS)
    o.update(opts or {})
    existing = [c for c in (existing_codes or []) if isinstance(c, str) and c.strip()]
    kept, dropped = [], []
    for c in candidates or []:
        code = (c.get('code') or '') if isinstance(c, dict) else ''
        reason = _drop_reason(code, existing, o)
        if reason:
            d = dict(c)
            d['reason'] = reason
            dropped.append(d)
        else:
            kept.append(c)
    return kept, dropped


def _drop_reason(code, existing, o):
    try:
        sl = alpha_ast.symbol_length(code)
        if sl > o['max_symbol_length']:
            return f'over-complex: symbol_length {sl} > {o["max_symbol_length"]}'
        bf = alpha_ast.base_feature_count(code)
        if bf > o['max_base_features']:
            return f'too many base fields: {bf} > {o["max_base_features"]}'
        fc = alpha_ast.free_const_ratio(code)
        if fc > o['max_free_const_ratio']:
            return f'over-parameterised: const ratio {fc:.2f} > {o["max_free_const_ratio"]}'
        # overlap_drop falsy (None/0/negative) → structural decorrelation OFF.
        # Used by focus rounds, which INTENTIONALLY mutate a parent and so are
        # *meant* to resemble it; dropping near-dups there fights the objective
        # and silently starves throughput (live: 50-80% of focus alphas dropped).
        # Exact duplicates are still collapsed downstream by the code_hash dedup.
        if existing and o['overlap_drop'] and o['overlap_drop'] > 0:
            size, idx = alpha_ast.structural_overlap(code, existing)
            if size >= o['overlap_drop']:
                return f'structural near-duplicate (overlap {size}) of existing #{idx}'
        return None
    except Exception:
        return None  # ALLOW on uncertainty
