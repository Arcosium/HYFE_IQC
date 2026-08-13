"""pyramids — 피라미드(REGION/DELAY/CATEGORY) 보유 현황과 데이터셋→카테고리 사상.

WQB 피라미드는 한 칸에 **3건 이상** 올라가야 하나가 달성된다. 2026-08-13 실측
분포는 Price Volume 16 · Risk 16 · Model 10 으로, 55건 중 33건이 이미 달성한
칸에 덧쌓여 아무 점수도 더하지 못했다. 같은 칸을 열여섯 번 파면 PROD_CORRELATION
이 0.9대로 올라 그 계보 전체가 제출 불능이 된다(그날 12/12 거절). 그래서 피라미드
다변화와 상관 벽 돌파는 같은 문제다.

데이터셋→카테고리 사상은 **손으로 적지 않는다.** 우리 DB 에 이미 WQB 가 답을 준
표본이 있다 — `metrics['pyramids']` 가 붙은 알파 중 코드가 데이터셋 하나만 쓰는
것들. 2026-08-13 기준 4,865 표본에서 45개 데이터셋이 모순 0 으로 갈렸다.
"""
from __future__ import annotations

import collections
import json
import logging
import time
from typing import Any

LOG = logging.getLogger('genomicwqb.pyramids')

#: 한 칸이 '달성'되는 최소 알파 수 (WQB 피라미드 규칙).
PYRAMID_MIN = 3

#: 실측 사상이 없는 새 데이터셋용 최소 폴백. 관측된 접두만 적는다 —
#: 추측을 늘리면 틀린 칸을 목표로 삼아 웨이브를 통째로 버리게 된다.
_PREFIX_FALLBACK = (
    ('institution', 'INSTITUTIONS'), ('shortinterest', 'SHORTINTEREST'),
    ('us_short_sale', 'SHORTINTEREST'), ('fundamental', 'FUNDAMENTAL'),
    ('sentiment', 'SENTIMENT'), ('analyst', 'ANALYST'), ('insider', 'INSIDERS'),
    ('option', 'OPTION'), ('model', 'MODEL'), ('news', 'NEWS'),
    ('macro', 'MACRO'), ('earnings', 'EARNINGS'), ('risk', 'RISK'),
    ('rsk', 'RISK'), ('pv', 'PV'), ('other', 'OTHER'),
)

_MAP_CACHE: dict[str, str] = {}
_MAP_TS = 0.0
_MAP_TTL = 3600.0


def _learn_map() -> dict[str, str]:
    """단일 데이터셋 알파의 (코드, pyramids) 쌍에서 사상을 학습한다."""
    from . import alpha_ast, db as _db, datafield_palette as _pal
    field_ds = _pal.field_dataset_map()
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for code, pyr in _db.code_pyramid_pairs():
        cats = {p.rsplit('/', 1)[-1] for p in str(pyr or '').split(',') if p.strip()}
        if len(cats) != 1:
            continue
        try:
            used = {str(field_ds[f]).strip().lower()
                    for f in alpha_ast.fields_used(str(code or '')) if f in field_ds}
        except Exception:
            continue
        if len(used) != 1:          # 여러 데이터셋이 섞이면 어느 쪽 공로인지 모른다
            continue
        votes[used.pop()][cats.pop()] += 1
    return {ds: c.most_common(1)[0][0] for ds, c in votes.items() if c}


def _ensure_map() -> dict[str, str]:
    global _MAP_TS
    if not _MAP_CACHE or time.time() - _MAP_TS > _MAP_TTL:
        try:
            _MAP_CACHE.update(_learn_map())
        except Exception as e:
            LOG.warning('피라미드 사상 학습 실패(폴백 사용): %s', e)
        _MAP_TS = time.time()
    return _MAP_CACHE


def dataset_category(dataset: str) -> str:
    """데이터셋 id → 피라미드 카테고리 (실측 우선, 없으면 접두 폴백)."""
    ds = str(dataset or '').strip().lower()
    if not ds:
        return ''
    if ds in _ensure_map():
        return _MAP_CACHE[ds]
    for pre, cat in _PREFIX_FALLBACK:
        if ds.startswith(pre):
            return cat
    return ''


def quarter_start(now: float | None = None) -> float:
    """현재 분기 시작 (피라미드 집계 창 — WQB 는 분기마다 리셋한다)."""
    lt = time.localtime(time.time() if now is None else now)
    return time.mktime((lt.tm_year, ((lt.tm_mon - 1) // 3) * 3 + 1, 1,
                        0, 0, 0, 0, 0, -1))


def counts(user_id: int, now: float | None = None) -> collections.Counter:
    """이번 분기 제출(OS)된 알파의 피라미드 칸별 보유 수. 키는 'GLB/D1/RISK' 전체 이름."""
    from . import db as _db
    out: collections.Counter = collections.Counter()
    for metrics_json in _db.submitted_metrics_since(user_id, quarter_start(now)):
        try:
            m = json.loads(metrics_json or '{}')
        except (TypeError, ValueError):
            continue
        for name in str(m.get('pyramids') or '').split(','):
            if name.strip():
                out[name.strip().upper()] += 1
    return out


def name_for(region: str, delay: Any, category: str) -> str:
    return f'{str(region or "GLB").upper()}/D{str(delay or 1)}/{str(category or "").upper()}'


def shortfall(user_id: int, region: str, delay: Any,
              now: float | None = None) -> dict[str, int]:
    """이 region/delay 에서 **아직 3건이 안 된** 카테고리 → 남은 발 수.

    3건을 채운 칸은 빠진다 — 거기 더 쌓아 봐야 피라미드는 안 늘고 상관만 오른다.
    """
    have = counts(user_id, now)
    out = {}
    for cat in set(_ensure_map().values()) | {c for _, c in _PREFIX_FALLBACK}:
        n = have.get(name_for(region, delay, cat), 0)
        if n < PYRAMID_MIN:
            out[cat] = PYRAMID_MIN - n
    return out


def is_short(user_id: int, region: str, delay: Any, category: str,
             now: float | None = None) -> bool:
    """이 칸이 아직 미달인가 — 다변화 우대(HT 제약 면제)의 판정 기준."""
    cat = str(category or '').upper()
    if not cat:
        return False
    return counts(user_id, now).get(name_for(region, delay, cat), 0) < PYRAMID_MIN
