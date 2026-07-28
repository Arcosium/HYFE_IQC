"""combine_layer — 검증된 IS 알파 재조합 (WQB AAF·smilee 이식, 2026-07-26).

AAF(MKLee)·smilee 의 핵심 아이디어: 매 라운드 백지에서 생성하는 대신, **이미
시뮬을 통과한 알파 둘을 결합**해 탐색공간을 검증된 재료 위에서만 넓힌다 —
시뮬 낭비와 IS 과적합을 동시에 줄이는 레이어. LLM 비용 0.

  - 부모 가중치 = AAF 원문 그대로 sharpe⁴ (상한 8) — 좋은 신호가 뽑히기
    쉽되 약한 신호도 가끔 뽑힌다.
  - 결합 순서는 좁게→넓게(동일 데이터셋 > 동일 카테고리 > 유사 회전율)를
    affinity 가중으로 반영한다. 회전율이 크게 다른 쌍은 감점(로그비).
  - 결합 연산 5종은 AAF Combine-signals 슬라이드의 목록 그대로.

순수 모듈: IO/DB 없음, 예외 안 던짐. 후보는 워커의 기존 파이프라인
(repair→lint→hygiene→캐시→시뮬→DB→밴딧)을 그대로 통과한다.
"""
from __future__ import annotations

import math
import random as _random_module
import re

from . import alpha_ast
from .reward import _f

# 재조합 후보의 idx 대역 — 유전체(1..8)·개선 레이어(41+)와 겹치지 않게 31+.
IDX_BASE = 31

# AAF Combine-signals: scale 로 스케일 이슈를 없앤 뒤 결합한다.
COMBINERS: list[tuple[str, str]] = [
    ('boost', 'scale({a}) * (1 + scale({b}))'),
    ('vneut', 'vector_neut(scale({a}), scale({b}))'),
    ('rneut', 'regression_neut(rank({a}), zscore({b}))'),
    ('vproj', 'vector_proj(scale({a}), scale({b}))'),
    ('rproj', 'regression_proj(scale({a}), scale({b}))'),
]

_PV_FIELDS = frozenset({
    'open', 'close', 'high', 'low', 'vwap', 'volume', 'returns', 'adv20',
    'cap', 'sharesout', 'dividend', 'split',
})
_GROUP_IDENTS = frozenset({'sector', 'industry', 'subindustry', 'market',
                           'country', 'exchange', 'currency'})


def _dataset_of(field: str) -> str:
    f = str(field).lower()
    if f in _PV_FIELDS or f in _GROUP_IDENTS:
        return 'pv'
    return f.split('_', 1)[0]          # anl4_… → anl4 (AAF 관례: 접두=데이터셋)


def _category_of(dataset: str) -> str:
    return re.sub(r'\d+$', '', dataset)   # anl4 → anl (숫자 떼면 카테고리)


def _profile(row: dict) -> dict | None:
    """db.combine_pool 행 → 재조합 프로파일. 부적격(파싱 불가 등)이면 None."""
    code = str(row.get('code') or '').strip()
    if not code:
        return None
    try:
        fields = alpha_ast.fields_used(code)
    except Exception:
        return None
    metrics = row.get('metrics') or {}
    sharpe = _f(metrics.get('sharpe'))
    if sharpe <= 0:
        return None
    datasets = {_dataset_of(f) for f in fields} or {'pv'}
    return {
        'id': row.get('id'),
        'code': code,
        'sharpe': sharpe,
        'turnover': _f(metrics.get('turnover')),
        'weight': min(sharpe ** 4, 8.0),            # AAF: (sharpe**4).clip(upper=8)
        'datasets': datasets,
        'categories': {_category_of(d) for d in datasets},
        'settings': {
            k: str(row[k]) for k in ('universe', 'neutralization', 'decay',
                                     'truncation')
            if row.get(k) not in (None, '')
        },
    }


def _affinity(a: dict, b: dict) -> float:
    """좁게→넓게 결합 순서(AAF Combining order)의 연속화.

    동일 데이터셋 공유 +2 > 동일 카테고리 공유 +1 > 아무 관계 없음 0.
    회전율 격차는 로그비로 감점(같은 turnover 대역이 잘 섞인다는 관찰).
    """
    score = 0.0
    if a['datasets'] & b['datasets']:
        score += 2.0
    elif a['categories'] & b['categories']:
        score += 1.0
    t1, t2 = max(a['turnover'], 1e-3), max(b['turnover'], 1e-3)
    score -= min(abs(math.log(t1 / t2)), 2.0) / 2.0
    return score


def _weighted_pick(profiles: list[dict], weights: list[float], rng) -> dict:
    total = sum(weights)
    x = rng.random() * total
    acc = 0.0
    for p, w in zip(profiles, weights):
        acc += w
        if x <= acc:
            return p
    return profiles[-1]


def usable_combiners(operators=None) -> list[tuple[str, str]]:
    """이 계정이 실제로 쓸 수 있는 결합식만. operators=None 이면 (조회 실패 등) 전부.

    ⚠ 2026-07-28 실측: COMBINERS 5개 중 **3개가 우리 계정에서 접근 불가**였다
    (vector_proj·regression_neut·regression_proj — /operators 는 82개만 준다).
    고르는 순간엔 알 길이 없어 시뮬까지 간 뒤 'Attempted to use inaccessible or
    unknown operator' 로 죽었다 — 라운드마다 재조합 후보를 통째로 버린 셈이다.
    RC 라고 다 쓸 수 있는 게 아니라서 계정 종류로 건너뛰면 안 된다.
    """
    if not operators:
        return list(COMBINERS)
    ok = []
    for name, tmpl in COMBINERS:
        used = set(re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', tmpl))
        if used <= set(operators):
            ok.append((name, tmpl))
    return ok or list(COMBINERS)   # 전멸이면 차라리 옛 동작 — 조회가 틀렸을 수 있다


def candidates(pool: list[dict], *, n: int = 2, rng=None,
               max_code_len: int = 700, operators=None) -> list[dict]:
    """검증 알파 풀에서 재조합 후보 전략 dict ≤ n 개를 만든다.

    각 dict 은 워커 strategies 항목과 같은 모양:
    {idx, code, desc, settings, origin='combine', parent_alpha_id}.
    settings/parent_alpha_id 는 sharpe 가 높은 쪽 부모에서 상속한다.
    풀이 2개 미만이거나 만들 게 없으면 []. 절대 예외를 던지지 않는다.
    """
    try:
        _rng = rng if rng is not None else _random_module
        profiles = []
        seen_codes = set()
        for row in (pool or []):
            p = _profile(row)
            if p is None or p['code'] in seen_codes:
                continue
            seen_codes.add(p['code'])
            profiles.append(p)
        if len(profiles) < 2 or n <= 0:
            return []

        out: list[dict] = []
        made: set[str] = set()
        combiners = usable_combiners(operators)
        weights = [p['weight'] for p in profiles]
        for _attempt in range(n * 6):
            if len(out) >= n:
                break
            a = _weighted_pick(profiles, weights, _rng)
            rest = [p for p in profiles if p is not a]
            rest_w = [p['weight'] * (1.0 + max(_affinity(a, p), 0.0))
                      for p in rest]
            b = _weighted_pick(rest, rest_w, _rng)
            name, tmpl = combiners[int(_rng.random() * len(combiners)) % len(combiners)]
            code = tmpl.format(a=a['code'], b=b['code'])
            if len(code) > max_code_len or code in made:
                continue
            made.add(code)
            strong, weak = (a, b) if a['sharpe'] >= b['sharpe'] else (b, a)
            rel = ('동일DS' if a['datasets'] & b['datasets']
                   else '동일CAT' if a['categories'] & b['categories'] else '이종')
            out.append({
                'idx': IDX_BASE + len(out),
                'code': code,
                'desc': (f'🧬 재조합[{name}·{rel}]: α#{a["id"]}(S{a["sharpe"]:.2f})'
                         f' × α#{b["id"]}(S{b["sharpe"]:.2f})'),
                'settings': dict(strong['settings']),
                'origin': 'combine',
                'parent_alpha_id': strong['id'],
            })
        return out
    except Exception:
        return []
