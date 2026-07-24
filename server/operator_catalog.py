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
# 라이브 /operators 수집본(wqb_data_service.refresh_operators) — arity/named 메타 제공.
# 있으면 정적 CSV 위에 병합(존재 시 우선). 없으면 정적+seed 로 동작(무회귀).
LIVE_OPERATORS_CSV = os.path.join(os.path.dirname(_THIS_DIR), 'data', 'live_operators.csv')

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

_CACHE = None  # ((static_mtime, live_mtime), dict[name -> meta])


def _meta_for(name: str) -> dict:
    return {
        'category': '',
        'desc': '',
        'needs_lookback': name.startswith('ts_'),
        'scope': 'VECTOR' if name.startswith('vec_') else 'REGULAR',
        # arity/named 은 라이브 CSV 가 있을 때만 채워진다. None = 미상(검증 skip).
        'min_args': None,
        'max_args': None,
        'required_named': frozenset(),
    }


def _mtime(path: str):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _int_or_none(v):
    try:
        s = str(v).strip()
        return int(s) if s != '' else None
    except (TypeError, ValueError):
        return None


def _load() -> dict:
    global _CACHE
    mt = _mtime(OPERATORS_CSV)
    live_mt = _mtime(LIVE_OPERATORS_CSV)
    key = (mt, live_mt)
    if _CACHE is not None and _CACHE[0] == key:
        return dict(_CACHE[1])

    ops = {n: _meta_for(n) for n in _SEED_OPS}   # baseline (무회귀)
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
    # 라이브 /operators 병합 — arity/named 메타를 얹는다(존재 시 우선). 이름은 union(무회귀).
    if live_mt is not None:
        try:
            with open(LIVE_OPERATORS_CSV, 'r', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    name = (row.get('name') or '').strip().lower()
                    if not name:
                        continue
                    m = ops.get(name) or _meta_for(name)
                    if (row.get('category') or '').strip():
                        m['category'] = row['category'].strip()
                    if (row.get('description') or '').strip():
                        m['desc'] = row['description'].strip()
                    m['min_args'] = _int_or_none(row.get('min_args'))
                    m['max_args'] = _int_or_none(row.get('max_args'))
                    named = (row.get('required_named') or '').strip()
                    m['required_named'] = frozenset(
                        x.strip() for x in named.split(',') if x.strip())
                    ops[name] = m
        except Exception:
            pass
    _CACHE = (key, ops)
    return dict(ops)


def arity(name) -> tuple:
    """(min_args, max_args). 라이브 CSV 없으면 (None, None) → 검증 skip."""
    m = _load().get(str(name or '').strip().lower())
    if not m:
        return (None, None)
    return (m.get('min_args'), m.get('max_args'))


def required_named(name) -> frozenset:
    m = _load().get(str(name or '').strip().lower())
    return m.get('required_named', frozenset()) if m else frozenset()


def has_signature(name) -> bool:
    """라이브 arity 메타가 있는 연산자인지 (min_args 가 채워졌는지)."""
    lo, _ = arity(name)
    return lo is not None


def operator_names() -> frozenset:
    return frozenset(_load().keys())


def is_operator(tok) -> bool:
    return (str(tok or '').strip().lower()) in _load()


def needs_lookback(name) -> bool:
    return str(name or '').strip().lower().startswith('ts_')
