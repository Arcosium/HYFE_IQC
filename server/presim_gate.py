"""presim_gate — structural decorrelation + complexity screen run BEFORE spending a
WQB simulation slot. Pure; never raises. Conservative: drop only clear near-duplicates
/ budget-busters, and report a reason for every drop (no silent caps).

#2 (2026-07-08): 여기에 두 겹의 '하드 프리플라이트'를 추가한다 — 확실히 불법인 것만 컷:
  - field 존재: alpha_ast.fields_used 의 필드가 라이브+정적 팔레트(datafield_palette)에도,
    genome curated 세트에도 없으면 컷. 팔레트가 빈약(부재/스테일)하면 skip(오탐 방지).
  - operator arity: operator_catalog(라이브 /operators)에 시그니처가 있는 연산자만,
    인자 수가 [min,max] 범위를 벗어나면 컷. 시그니처 미상이면 skip.
둘 다 IQC_PRESIM_FIELD_CHECK / IQC_PRESIM_ARITY_CHECK=0 으로 즉시 끌 수 있다."""
from __future__ import annotations

import os

from . import alpha_ast

# 하드 프리플라이트 on/off (기본 on). 오탐 시 env 로 즉시 차단. 라이브 데이터가 없으면
# 자연히 inert: field 는 팔레트 부재 시 skip, arity 는 시그니처 미상 시 op 별 skip.
_FIELD_CHECK = os.environ.get('IQC_PRESIM_FIELD_CHECK', '1') != '0'
_ARITY_CHECK = os.environ.get('IQC_PRESIM_ARITY_CHECK', '1') != '0'
# 팔레트가 이보다 적으면 field 검증 skip(부분 fetch 로 유효필드 오컷 방지).
_MIN_PALETTE = 50
# group 식별자 — group_* 연산자 인자로 field 자리에 오지만 datafield 아님(무조건 통과).
_GROUP_IDENTS = frozenset({'sector', 'industry', 'subindustry', 'market',
                           'country', 'exchange', 'currency', 'sector_ret'})
_ALWAYS_KNOWN = None   # genome curated 필드 ∪ group 식별자 (lazy)


def _always_known_fields() -> frozenset:
    """genome 이 실제로 산출하는 curated 필드(SHARED_DATASETS) ∪ group 식별자.
    pv 필드(close/open/...)는 /data-fields 목록에 없지만 보편 유효 → 여기서 통과시킨다."""
    global _ALWAYS_KNOWN
    if _ALWAYS_KNOWN is not None:
        return _ALWAYS_KNOWN
    fields = set(_GROUP_IDENTS)
    try:
        from .genome_models import SHARED_DATASETS, GROUPS
        for fam in SHARED_DATASETS.values():
            for f in fam:
                fields.add(str(f).lower())
        for v in GROUPS.values():
            fields.add(str(v).lower())
    except Exception:
        pass
    _ALWAYS_KNOWN = frozenset(fields)
    return _ALWAYS_KNOWN


def _field_reason(code, known):
    """확실히 존재하지 않는 datafield 만 사유 반환. known=팔레트 필드 set(lower)."""
    try:
        fields = alpha_ast.fields_used(code)   # operator 는 이미 제외됨
    except Exception:
        return None
    always = _always_known_fields()
    bad = []
    for f in fields:
        fl = str(f).lower()
        if fl in known or fl in always or fl.startswith('vec_'):
            continue
        bad.append(f)
    if bad:
        return 'unknown datafield(s) not in palette: ' + ', '.join(sorted(bad)[:4])
    return None


def _arity_reason(code):
    """라이브 시그니처가 있는 연산자의 인자 수가 [min,max] 밖이면 사유 반환."""
    try:
        calls = alpha_ast.call_arities(code)
    except Exception:
        return None
    if not calls:
        return None
    try:
        from . import operator_catalog
    except Exception:
        return None
    for name, n in calls:
        lo, hi = operator_catalog.arity(name)
        if lo is None:
            continue   # 시그니처 미상 → skip
        if n < lo or (hi is not None and n > hi):
            rng = str(lo) if hi == lo else f'{lo}..{hi if hi is not None else "N"}'
            return f'operator arity: {name}({n} args, expected {rng})'
    return None


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
    # 하드 프리플라이트 게이트 준비 (opts 로 개별 override, 기본은 env 상수).
    field_check = o.get('field_check', _FIELD_CHECK)
    arity_check = o.get('arity_check', _ARITY_CHECK)
    known = None
    if field_check:
        try:
            from . import datafield_palette
            known = datafield_palette.known_field_names()
        except Exception:
            known = None
        if known is not None and len(known) < _MIN_PALETTE:
            known = None   # 팔레트 빈약 → field 검증 skip(오탐 방지)
    kept, dropped = [], []
    for c in candidates or []:
        code = (c.get('code') or '') if isinstance(c, dict) else ''
        reason = _drop_reason(code, existing, o)
        if not reason and field_check and known is not None:
            reason = _field_reason(code, known)
        if not reason and arity_check:
            reason = _arity_reason(code)
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
