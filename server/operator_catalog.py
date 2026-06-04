"""brain_operators.csv 기반 단일 연산자 카탈로그.

3곳에 흩어진 하드코딩 op 리스트(alpha_similarity._KNOWN_OPS, db.operator_preference_stats
KNOWN_OPS)를 대체한다. arity 는 advisory (CSV description 이 짧아 hard-gate 안 함).
주용도: 연산자 이름 집합(Jaccard op/field 분류) + ts_*/vec_* 접두 휴리스틱.
무회귀 보장: CSV 파싱 결과를 내장 시드와 합집합한다 (이전보다 적게 인식하지 않음).
CSV 부재/파싱 실패 시 시드만으로 동작.
"""
from __future__ import annotations

import csv
import os

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_CSV = os.path.join(_THIS_DIR, 'brain_operators.csv')

# 구 _KNOWN_OPS 합집합 — 무회귀 baseline.
_SEED_OPS = frozenset({
    'rank', 'ts_rank', 'ts_delta', 'ts_mean', 'ts_std_dev', 'ts_sum', 'ts_min', 'ts_max',
    'ts_zscore', 'ts_corr', 'ts_decay_linear', 'ts_arg_min', 'ts_arg_max',
    'winsorize', 'zscore', 'delta', 'correlation', 'add', 'subtract', 'multiply',
    'divide', 'power', 'signed_power', 'abs', 'log', 'sqrt', 'inverse', 'min', 'max',
    'sign', 'reverse', 'group_rank', 'group_neutralize', 'group_sum', 'group_mean',
    'sum', 'mean', 'std_dev', 'scale', 'normalize', 'fraction', 'quantile',
    'if_else', 'trade_when', 'pasteurize', 'truncate', 'last_diff_value',
    'and', 'or', 'not', 'true', 'false', 'filter',
})

_CACHE = None  # (mtime_or_None, dict[name -> meta])


def _meta_for(name: str) -> dict:
    return {
        'category': '',
        'desc': '',
        'needs_lookback': name.startswith('ts_'),
        'scope': 'VECTOR' if name.startswith('vec_') else 'REGULAR',
    }


def _load() -> dict:
    global _CACHE
    try:
        mt = os.path.getmtime(OPERATORS_CSV)
    except OSError:
        mt = None
    if _CACHE is not None and _CACHE[0] == mt:
        return dict(_CACHE[1])

    ops = {n: _meta_for(n) for n in _SEED_OPS}   # baseline
    if mt is not None:
        try:
            with open(OPERATORS_CSV, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    name = (row.get('name') or '').strip().lower()
                    if not name:
                        continue
                    m = ops.get(name) or _meta_for(name)
                    m['category'] = (row.get('category') or '').strip()
                    m['desc'] = (row.get('description') or '').strip()
                    ops[name] = m
        except Exception:
            pass
    _CACHE = (mt, ops)
    return dict(ops)


def operator_names() -> frozenset:
    return frozenset(_load().keys())


def is_operator(tok) -> bool:
    return (str(tok or '').strip().lower()) in _load()


def needs_lookback(name) -> bool:
    return str(name or '').strip().lower().startswith('ts_')
