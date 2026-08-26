"""Ranked, rotating, settings-scoped datafield palette for Gemini alpha generation.

Replaces the naive `_read_csv_text(DATAFIELDS_CSV, max_chars=8000)` call that
truncated to the first ~80 alphabetical rows of a 5201-row CSV.

Design:
  Three diversified buckets (de-duplicated, total ≈ n fields):
    1. ~40% highest coverage  — reliable, clean-simulating fields.
    2. ~30% "undiscovered gems" — low alphas AND coverage >= floor
                                   (low-popularity / decorrelated).
    3. ~30% rotating window   — stable sort over remainder, slice offset by
                                 `seed` mod len(remainder) so each round
                                 surfaces different long-tail fields.

Filtering:
  - Always filter by region (case-insensitive match).
  - If delay given, filter by delay==str(delay); if result < n, RELAX (drop
    delay filter) so palette is never starved.
  - If universe given, prefer rows matching universe; relax similarly.

Output:
  Compact text block — one line per field:
    "<name> | <category> | cov=<coverage> | pop=<alphas>"
  Plus a 1-line header.  Bounded to n+1 lines total.

Cache:
  CSV is parsed once per mtime (same pattern as operator_catalog.py).
  The module-level counter `_rotation_counter` provides a monotonically
  incrementing seed when the caller has no round_num available.

Robustness:
  Any CSV parse/IO error returns '' — caller falls back to old behaviour.
  Never raises.
"""
from __future__ import annotations

import csv
import json
import os
import threading
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATAFIELDS_CSV = os.path.join(_THIS_DIR, 'IQC_brain_datafields.csv')
_LIVE_CSV_PATH = os.path.join(os.path.dirname(_THIS_DIR), 'data', 'live_datafields.csv')


def _default_datafields_path() -> str:
    """라이브 CSV가 존재하고 비어있지 않으면 우선, 아니면 정적 CSV."""
    try:
        if os.path.exists(_LIVE_CSV_PATH) and os.path.getsize(_LIVE_CSV_PATH) > 0:
            return _LIVE_CSV_PATH
    except OSError:
        pass
    return DATAFIELDS_CSV

# path+mtime-keyed cache: dict mapping (path, mtime_or_None) -> list[dict]
# Keying on path prevents a test-injected file with the same mtime as the
# real CSV from returning wrong rows.
_CSV_CACHE: dict[tuple[str, float | None], list[dict]] = {}
_CSV_LOCK = threading.Lock()

# Module-level monotonic counter — fallback seed when caller has no round_num.
_rotation_counter = 0
_rotation_lock = threading.Lock()


def _next_rotation() -> int:
    """Atomically increment and return the module-level rotation counter."""
    global _rotation_counter
    with _rotation_lock:
        _rotation_counter += 1
        return _rotation_counter


def _load_csv(path: str = DATAFIELDS_CSV) -> list[dict]:
    """Parse the CSV, returning a list of row dicts.  Mtime+path-cached, thread-safe."""
    try:
        mtime: float | None = os.path.getmtime(path)
    except OSError:
        mtime = None

    cache_key = (path, mtime)
    with _CSV_LOCK:
        if cache_key in _CSV_CACHE:
            return _CSV_CACHE[cache_key]

        if mtime is None:
            # File does not exist — store empty sentinel so we don't keep retrying
            _CSV_CACHE[cache_key] = []
            return []

        rows: list[dict] = []
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rows.append(row)
        except Exception:
            _CSV_CACHE[cache_key] = []
            return []

        # 같은 파일의 옛 mtime 항목은 버린다 — 갱신할 때마다 수만 행짜리 리스트가
        # 하나씩 쌓이면 그대로 누수다 (팔레트가 GLB 29343행으로 커진 뒤 특히).
        for k in [k for k in _CSV_CACHE if k[0] == path and k != cache_key]:
            _CSV_CACHE.pop(k, None)
        _CSV_CACHE[cache_key] = rows
        return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def known_field_names() -> 'set[str] | None':
    """live + static datafield CSV 의 name 합집합(lowercase). 둘 다 로드 실패 → None.

    #2 프리플라이트 field-존재 검증용. region-무관(어느 region 이든 존재하면 known)으로
    보수적으로 판정해 '다른 region 팔레트라서' 유효 필드를 오컷하지 않는다. pv 보편필드
    (close/open/...)는 /data-fields 목록에 없으므로 presim_gate 가 curated 화이트리스트로 보완한다.
    """
    names: set[str] = set()
    got = False
    for path in (_LIVE_CSV_PATH, DATAFIELDS_CSV):
        try:
            rows = _load_csv(path)
        except Exception:
            rows = []
        for r in rows:
            n = (r.get('name') or '').strip().lower()
            if n:
                names.add(n)
                got = True
    return names if got else None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# GA 유전체용 family 팔레트 (2026-07-14 신설)
# ---------------------------------------------------------------------------
# 배경: genome_models.SHARED_DATASETS 는 family 당 4~9개, 총 ~35개 필드를 **하드코딩**
# 하고 있었다. 그동안 이 5200행 CSV 는 (지금은 제거된) Gemini 자유생성 프롬프트에서만
# 쓰였다 — 즉 결정론적 GA 는 우물 안에서 진화하고 있었다(2026-07-14 진단).
# 여기서 family 별 풀을 CSV 로 넓혀 GA 가 실제 탐색 공간을 갖게 한다.

# 필드명 접두사 → family. 순서 중요(먼저 맞는 것이 이긴다).
_FAMILY_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('analyst', ('anl4_', 'anl', 'est_', 'actual_', 'forward_', 'guidance_',
                 'earnings_', 'eps_')),
    ('option', ('implied_volatility', 'historical_volatility', 'call_', 'put_',
                'option')),
    ('news', ('nws12_', 'nws', 'news_', 'snt_', 'sentiment')),
    ('fundamental', ('fnd6_', 'fnd2_', 'fnd_', 'fn_', 'fscore', 'cashflow',
                     'capex', 'book_', 'gross_', 'income_', 'net_', 'debt',
                     'assets', 'equity', 'liabilit', 'dividend', 'cash',
                     'free_', 'capital', 'operating', 'revenue', 'sales')),
    # WQB 의 모델/팩터 데이터셋 — 3000개가 넘는데 여태 GA 가 한 번도 못 봤다.
    # (가치·퀄리티·모멘텀 팩터가 이미 계산돼 들어 있다.)
    ('model', ('mdl177_', 'mdl77_', 'mdl')),
)

# family 당 동적 필드 상한. 너무 크면 무작위 필드 선택이 희석돼 GA 가 수렴을 못 한다.
FIELDS_PER_FAMILY = _safe_int(os.environ.get('IQC_FIELDS_PER_FAMILY'), 40)
# 커버리지 하한 — 데이터가 듬성한 필드는 시뮬이 NaN 투성이가 된다.
MIN_COVERAGE = _safe_int(os.environ.get('IQC_FIELD_MIN_COVERAGE'), 70)
DYNAMIC_FIELDS_ON = os.environ.get('IQC_DYNAMIC_FIELDS', '1') != '0'

_POOL_CACHE: dict[str, Any] = {}
_POOL_LOCK = threading.Lock()


# WQB 데이터셋 카테고리 → 우리 family 이름.
# ⚠ 2026-07-22 발견 — 이름 접두사 규칙만으로는 **어제 최고 알파를 만든 필드들이 전부
#   None 으로 떨어져 GA 에 아예 보이지 않았다**:
#     opt6_vimtaxp(option6, 단독 Sharpe 2.18) · executed_short_trade_share_count
#     (us_short_sale, 2.28) · shrt36_svol(shortinterest36, 1.84) · customer_vol_imbalance
#   이제 필드→데이터셋→카테고리 실매핑이 있으므로 추측을 버리고 그걸 쓴다.
#   shortinterest·imbalance·socialmedia·sentiment 는 **새로 열리는 계열**이다.
CATEGORY_FAMILY = {
    'analyst': 'analyst', 'earnings': 'analyst',
    'fundamental': 'fundamental',
    'option': 'option',
    'news': 'news', 'sentiment': 'news', 'socialmedia': 'news',
    'model': 'model', 'risk': 'model', 'other': 'model', 'macro': 'model',
    'shortinterest': 'shortinterest',
    'imbalance': 'imbalance', 'institutions': 'imbalance', 'insiders': 'imbalance',
    'pv': 'pv',
}
_DATASET_CATEGORY_CACHE: dict = {}
_FIELD_DATASET_CACHE: dict = {}


def dataset_category_map() -> dict:
    """{데이터셋 id: 카테고리 id}. `docs/brain_reference/datasets_by_grid.json` 기준.

    scripts/fetch_brain_reference.py 가 만든다. 없으면 빈 dict → 접두사 규칙으로 폴백.
    """
    if _DATASET_CATEGORY_CACHE:
        return _DATASET_CATEGORY_CACHE
    path = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..',
        'docs', 'brain_reference', 'datasets_by_grid.json'))
    try:
        with open(path) as fh:
            grids = json.load(fh)
    except (OSError, ValueError):
        return {}
    for rows in (grids or {}).values():
        for d in rows or []:
            cat = d.get('category')
            cat = cat.get('id') if isinstance(cat, dict) else cat
            if d.get('id') and cat:
                _DATASET_CATEGORY_CACHE.setdefault(str(d['id']).lower(), str(cat).lower())
    return _DATASET_CATEGORY_CACHE


# ── 데이터셋 Value Score (WQB Data Explorer) ─────────────────────────────────
# WQB 피드백(2026-08-26): "다른 데이터셋을 안 쓴다 — Dataset Value Score 로 저사용
# 고가치 필드를 찾아라." 그 Value Score·dateUpdated 를 데이터셋 단위로 한 번만 캡처해
# (`data/dataset_meta.json`, wqb_data_service.refresh 가 일일 갱신) 여기서 조회한다.
# 29,000행 필드 CSV 에 컬럼을 복제하지 않는다 — 데이터셋 id(=category 컬럼)로 붙이면 된다.
_DATASET_META_PATH = os.path.join(os.path.dirname(_LIVE_CSV_PATH), 'dataset_meta.json')
_DATASET_META_CACHE: dict = {}
_DATASET_META_MTIME: float = -1.0


def dataset_meta() -> dict:
    """{dataset_id(lower): {vs, userCount, alphaCount, dateUpdated, ...}}. 없으면 빈 dict."""
    global _DATASET_META_CACHE, _DATASET_META_MTIME
    try:
        mt = os.path.getmtime(_DATASET_META_PATH)
    except OSError:
        return {}
    if mt != _DATASET_META_MTIME:
        try:
            with open(_DATASET_META_PATH, encoding='utf-8') as fh:
                _DATASET_META_CACHE = json.load(fh) or {}
        except (OSError, ValueError):
            _DATASET_META_CACHE = {}
        _DATASET_META_MTIME = mt
    return _DATASET_META_CACHE


def dataset_value_score(ds_id) -> float:
    """데이터셋의 Value Score (0.0 = 미상). 필드 선택 다변화 가중치용."""
    m = dataset_meta().get(str(ds_id or '').strip().lower())
    if not m:
        return 0.0
    try:
        return float(m.get('vs') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def classify_family(name: str) -> str | None:
    """필드명 → family.

    1순위는 **실매핑**(필드→데이터셋→카테고리)이다. 이름 접두사 추측은 그게 실패할
    때의 폴백으로만 남는다 — 접두사 규칙은 커버리지가 낮아 좋은 필드를 통째로 놓친다.
    """
    n = (name or '').strip().lower()
    if n:
        ds = field_dataset_map().get(n)
        if ds:
            fam = CATEGORY_FAMILY.get(dataset_category_map().get(str(ds).lower(), ''))
            if fam:
                return fam
    if not n:
        return None
    for fam, prefixes in _FAMILY_PREFIXES:
        for p in prefixes:
            if n.startswith(p):
                return fam
    return None


def _all_rows() -> list[dict]:
    """라이브 CSV ∪ 정적 CSV. (name, delay) 기준 중복 제거, 라이브 우선.

    ⚠ 하나만 고르면 안 된다. WQB /data-fields 는 count 를 10000 으로 캡하고 필드 id
    알파벳순으로 주는데, D1 은 필드가 그보다 훨씬 많아서 앞 10000개가 analyst 계열에
    쏠린다(실측: D1 라이브만 쓰면 family_pools 가 analyst 하나만 40개, 나머지는 0).
    정적 CSV(5200행·전부 D1)는 계열이 고르게 퍼진 검증된 팔레트라 그걸 버리면
    **D1 팔레트가 퇴화한다**. 그래서 둘을 합치고 delay 로 가른다:
      delay=1 → 정적 + 라이브 D1,   delay=0 → 라이브 D0.
    """
    rows: list[dict] = []
    # ⚠ dedup 키에 region/universe 포함 (2026-07-27, GLB 테마) — (name, delay) 만
    #   쓰면 같은 필드가 USA·GLB 양쪽에 실재할 때 GLB 행이 조용히 버려져
    #   리전 필터가 '이 필드는 GLB 에 없다' 고 오판한다.
    seen: set[tuple] = set()
    for path in (_LIVE_CSV_PATH, DATAFIELDS_CSV):
        try:
            for r in _load_csv(path):
                key = ((r.get('name') or '').strip().lower(),
                       str(r.get('delay') or '').strip(),
                       str(r.get('region') or '').strip().upper(),
                       str(r.get('universe') or '').strip().upper())
                if key[0] and key not in seen:
                    seen.add(key)
                    rows.append(r)
        except Exception:
            continue
    return rows


# pv1 전체 필드 (2026-07-22 라이브 /data-fields?dataset.id=pv1 실측, USA/D1/TOP1000).
# ⚠ CSV 만으로는 부족하다 — 라이브 CSV 는 필드 id 알파벳순 10000행에서 잘려서
#   volume·vwap·sharesout 처럼 뒤쪽 글자로 시작하는 pv1 필드가 통째로 빠진다.
#   pv1 은 Power Pool 이 가장 자주 금지하는 데이터셋이라 여기서 못 거르면 조건 위반
#   알파를 만들어 제출 예산을 버린다. 작고 안정적인 목록이라 박아두는 게 옳다.
PV1_FIELDS = frozenset({
    'adjfactor', 'adv20', 'cap', 'close', 'country', 'currency', 'cusip', 'dividend',
    'exchange', 'high', 'industry', 'isin', 'low', 'market', 'open', 'returns',
    'sector', 'sedol', 'sharesout', 'split', 'subindustry', 'ticker', 'volume', 'vwap',
})
# 2026-07-21 발굴에서 검증돼 genome_models.SHARED_DATASETS 에 시드로 넣은 필드들.
# ⚠ 데이터셋별 수집(field_dataset.json)은 데이터셋당 앞 50개만 받는다 — option6 는
#   133필드라 `opt6_vimtaxp` 처럼 뒤쪽 글자 필드가 빠진다. 시드 필드는 여기 못박아
#   분류·검증이 절대 놓치지 않게 한다.
CURATED_DATASET_FIELDS = {
    'pv1': PV1_FIELDS,
    'option6': frozenset({'opt6_vimtaxp', 'opt6_xxpslckw1', 'opt6_kw1gnhcxpkts',
                          'opt6_ivpctile1y', 'opt6_ivstdevsmean', 'opt6_slopeavg1y',
                          'opt6_m1gnhcxpkts', 'opt6_ivhvxernratiostd1y',
                          'opt6_fcstr2imp'}),
    'option9': frozenset({'option_breakeven_10', 'pcr_vol_10', 'pcr_oi_10',
                          'call_breakeven_10', 'put_breakeven_10'}),
    'shortinterest36': frozenset({'shrt36_svol', 'shrt36_totalvolume',
                                  'shrt36_sexemptvol', 'shrt36_mkt'}),
    'us_short_sale': frozenset({'executed_short_trade_share_count',
                                'aggregate_executed_trade_share_count',
                                'reported_short_sale_share_quantity',
                                'reported_total_trade_share_quantity'}),
    'order_flow_imb': frozenset({'customer_vol_imbalance', 'firm_vol_imbalance',
                                 'broker_dealer_vol_imbalance',
                                 'market_maker_vol_imbalance',
                                 'customer_trade_imbalance',
                                 'broker_dealer_trade_imbalance'}),
}


def field_dataset_map() -> dict[str, str]:
    """{필드명(소문자): 데이터셋 id}. CSV 의 `category` 열이 곧 데이터셋 id 다
    (예: option6 · pv1 · us_short_sale). 실패하면 curated 분만 남는다.

    Power Pool 테마가 `datasets not in ['pv1']` 처럼 **데이터셋 단위**로 거르기 때문에
    필드에서 데이터셋을 되짚을 수 있어야 조건을 만족하는 알파만 만들 수 있다.

    ⚠ CSV 커버리지가 완전하지 않아 **매핑에 없는 필드가 많다**. 그런 필드는 데이터셋을
      모르는 것이지 '안전' 한 게 아니다. 최종 안전망은 시뮬 후 WQB 가 알려주는 실제
      `datasets` 로 constraint_spec.compliant() 를 다시 돌리는 것이다.
    """
    # ⚠ 캐시 필수 — classify_family 가 필드마다 이걸 부른다. 캐시가 없으면 팔레트
    #   구축이 O(필드수²) 가 되어 테스트가 57초 → 212초로 늘어났다(2026-07-22 실측).
    if _FIELD_DATASET_CACHE:
        return _FIELD_DATASET_CACHE
    out: dict[str, str] = {}
    for ds, fields in CURATED_DATASET_FIELDS.items():
        for f in fields:
            out[f] = ds
    # 데이터셋별로 직접 수집한 매핑(scripts/fetch_brain_reference.py)이 가장 정확하다.
    # /data-fields 의 10000행 알파벳 캡을 dataset.id 필터로 우회해 받은 것이다.
    try:
        path = os.path.abspath(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), '..',
            'docs', 'brain_reference', 'field_dataset.json'))
        with open(path) as fh:
            for k, v in (json.load(fh) or {}).items():
                out.setdefault(str(k).strip().lower(), str(v))
    except (OSError, ValueError):
        pass
    try:
        for r in _all_rows():
            name = (r.get('name') or '').strip().lower()
            cat = (r.get('category') or '').strip()
            # 정적 CSV 는 category 열에 타입(matrix/vector)이 들어 있어 쓸 수 없다.
            # 데이터셋 id 로 보이는 값만 취한다.
            if name and cat and cat.lower() not in ('matrix', 'vector', 'group', 'symbol'):
                out.setdefault(name, cat)
    except Exception:
        _FIELD_DATASET_CACHE.update(out)
        return out
    _FIELD_DATASET_CACHE.update(out)
    return out


def fields_of_excluded_datasets(excluded) -> set[str]:
    """금지 데이터셋에 속한 필드명(소문자) 집합. 조건 밖 필드를 걸러낼 때 쓴다."""
    bad = {str(d).strip().lower() for d in (excluded or ()) if str(d).strip()}
    if not bad:
        return set()
    return {name for name, ds in field_dataset_map().items()
            if str(ds).strip().lower() in bad}


# 금지 필드 → 그 필드가 나타내는 **개념**의 검색어. 대체 필드를 찾을 때 쓴다.
# 2026-07-21 실측 근거: Power Pool 이 pv1 을 금지한 주에 계정의 고Sharpe 레시피가
# 전부 `-ts_zscore(close,5)`(단독 Sharpe 1.62)에 의존해 통째로 막혀 있었다.
# option6 에 `opt6_vimtaxp`("Stock price taken at the time of implied volatility
# calculation", 커버리지 98%, 사용 알파 9개)가 있는 걸 설명문 검색으로 찾아내
# pv1 없이 같은 신호를 복제했고, 그게 그날 발굴 전체의 돌파구였다.
CONCEPT_KEYWORDS = {
    'close': ('stock price', 'closing price', 'price at', 'underlying price', 'breakeven'),
    'open': ('opening price', 'stock price'),
    'high': ('high price', 'intraday high'),
    'low': ('low price', 'intraday low'),
    'vwap': ('volume weighted', 'average price'),
    'volume': ('trade volume', 'shares traded', 'total volume', 'trading volume'),
    'adv20': ('average volume', 'liquidity', 'shares traded'),
    'returns': ('price change', 'stock return', 'price return'),
    'cap': ('market capitalization', 'market cap', 'enterprise value'),
    'sharesout': ('shares outstanding',),
    'sector': ('sector',),
    'industry': ('industry',),
    'subindustry': ('sub-industry', 'subindustry'),
}


# 라이브에서 **실제로 통한** 대체 필드 (2026-07-21~22 발굴 실측).
# ⚠ 왜 박아두나 — /data-fields 는 count 를 10000 으로 캡하고 필드 id 알파벳순으로 준다.
#   D1 은 필드가 그보다 훨씬 많아 option6·shortinterest36·us_short_sale 처럼 뒤쪽
#   글자로 시작하는 데이터셋이 CSV 에서 통째로 빠진다. 설명문 검색만으로는 이들을
#   영영 못 찾는다. 그날 발굴의 돌파구가 정확히 여기였으므로 지식을 잃으면 안 된다.
#   (CSV 가 그 필드를 담게 되면 아래 시드는 중복 제거로 자연히 흡수된다.)
KNOWN_SUBSTITUTES = {
    'close': (
        {'name': 'opt6_vimtaxp', 'dataset': 'option6', 'coverage': 98, 'alphas': 9,
         'description': 'Stock price taken at the time of implied volatility calculation. '
                        '2026-07-21 실측: -ts_zscore(opt6_vimtaxp,5) 단독 Sharpe 2.18 '
                        '(USA/D1/TOP1000/STATISTICAL/decay4).'},
        {'name': 'option_breakeven_10', 'dataset': 'option9', 'coverage': 98, 'alphas': 84,
         'description': 'Open-interest-weighted mean breakeven price — 주가를 추종한다. '
                        '실측 Sharpe 1.06.'},
        {'name': 'opt6_xxpslckw1', 'dataset': 'option6', 'coverage': 98, 'alphas': 3,
         'description': 'Stock price at the prior week (5 trading days ago).'},
    ),
    'returns': (
        {'name': 'opt6_kw1gnhcxpkts', 'dataset': 'option6', 'coverage': 98, 'alphas': 25,
         'description': 'Stock price change over 1 week. 실측 -rank() Sharpe 0.99.'},
        {'name': 'opt6_m1gnhcxpkts', 'dataset': 'option6', 'coverage': 98, 'alphas': 4,
         'description': 'Stock price change over 1 month.'},
    ),
    'volume': (
        {'name': 'shrt36_totalvolume', 'dataset': 'shortinterest36', 'coverage': 100,
         'alphas': 234,
         'description': 'Total shares traded on NYSE Arca. shrt36_svol 과의 비율이 '
                        '당일 매도압력 — 실측 단독 Sharpe 1.84.'},
        {'name': 'aggregate_executed_trade_share_count', 'dataset': 'us_short_sale',
         'coverage': 100, 'alphas': 29,
         'description': 'Total shares traded during regular hours. '
                        'executed_short_trade_share_count 와의 비율이 실측 Sharpe 2.28.'},
    ),
}


def find_substitutes(field, excluded=(), k: int = 12, delay=None) -> list[dict]:
    """금지 필드를 대신할 만한 **허용 데이터셋의 필드**를 설명문에서 찾는다.

    반환: [{'name','dataset','coverage','alphas','description'}…] — 커버리지 높은 순.

    금지 데이터셋을 피해 알파를 만들라는 요구는, 실무적으로는 "그 데이터가 나타내던
    개념을 다른 데이터셋에서 다시 찾아라" 는 뜻이다. 설명문(description)에 그 개념이
    적혀 있으므로 키워드로 훑으면 후보가 나온다. 완벽한 의미검색은 아니지만
    2026-07-21 에 실제로 통했다 — `close` → `opt6_vimtaxp` 를 이 방식으로 찾았다.
    """
    name = str(field or '').strip().lower()
    keywords = CONCEPT_KEYWORDS.get(name)
    if not keywords:
        return []
    bad_ds = {str(d).strip().lower() for d in (excluded or ())}
    want_delay = None if delay is None else str(delay).strip()

    scored: list[tuple] = []
    seen: set = set()
    out_seed: list[dict] = []
    # 검증된 시드를 먼저 얹는다 (금지 데이터셋에 속하면 당연히 뺀다).
    for cand in KNOWN_SUBSTITUTES.get(name, ()):
        if str(cand.get('dataset', '')).lower() in bad_ds:
            continue
        out_seed.append(dict(cand))
        seen.add(str(cand.get('name', '')).lower())
    try:
        rows = _all_rows()
    except Exception:
        return out_seed[:max(1, int(k))]
    for r in rows:
        fname = (r.get('name') or '').strip()
        ds = (r.get('category') or '').strip()
        if not fname or not ds or ds.lower() in ('matrix', 'vector', 'group', 'symbol'):
            continue
        if ds.lower() in bad_ds or fname.lower() in seen:
            continue
        if want_delay is not None and str(r.get('delay') or '').strip() != want_delay:
            continue
        desc = (r.get('description') or '').lower()
        hits = sum(1 for kw in keywords if kw in desc)
        if not hits:
            continue
        seen.add(fname.lower())
        cov = _safe_int(r.get('coverage'), 0)
        scored.append((hits, cov, {
            'name': fname, 'dataset': ds, 'coverage': cov,
            'alphas': _safe_int(r.get('alphas'), 0),
            'description': (r.get('description') or '')[:160],
        }))
    # 키워드 적중수 → 커버리지 순. 적중이 많을수록 개념이 정확히 일치한다.
    scored.sort(key=lambda t: (-t[0], -t[1]))
    return (out_seed + [x[2] for x in scored])[:max(1, int(k))]


def family_pools(per_family: int | None = None,
                 min_coverage: int | None = None,
                 delay=None, region=None, universe=None,
                 datasets=None) -> dict[str, list[str]]:
    """CSV → {family: [필드명…]}. 실패하면 빈 dict (호출부가 curated 로 폴백).

    선택 규칙(결정론적 — 같은 CSV 면 언제나 같은 결과):
      절반은 **검증된 필드** (coverage 높고 alphas(사용례) 많은 순),
      절반은 **미발굴 필드** (coverage 는 충족하되 alphas 가 적은 순) — 탈상관 레버.
    두 바구니를 번갈아 담아, 상한을 잘라도 성격이 한쪽으로 쏠리지 않게 한다.

    delay 를 주면 그 delay 로 **실제 조회된 필드만** 남긴다. D0 는 D1 과 필드 집합이
    다르고(option6 는 D0 131 / D1 133), CSV 에 그 delay 행이 하나도 없으면 빈 dict 를
    돌려준다 — 호출부가 '이 delay 팔레트는 없다' 를 구분할 수 있어야 하기 때문이다
    (없는데 D1 필드를 쓰면 D0 시뮬이 통째로 ERROR 난다).
    """
    n = FIELDS_PER_FAMILY if per_family is None else int(per_family)
    cov_floor = MIN_COVERAGE if min_coverage is None else int(min_coverage)
    want_delay = None if delay is None else str(delay).strip()
    # region/universe 필터 (2026-07-27, GLB 테마) — 필드 집합이 리전마다 다르다.
    # USA 필드를 GLB 에 쓰면 'unknown variable' 로 라운드가 통째로 ERROR 난다.
    want_region = None if region is None else str(region).strip().upper()
    want_universe = None if universe is None else str(universe).strip().upper()
    # 라이브 CSV mtime 을 키에 섞어 갱신 시 캐시가 자동 무효화되게 한다
    # (2026-07-27 — 리전 팔레트가 비었다가 refresh 후 채워지는 시나리오).
    try:
        _mt = os.path.getmtime(_LIVE_CSV_PATH)
    except OSError:
        _mt = 0
    # 계정 등급별 데이터셋 허용목록도 캐시 키에 넣는다 — 같은 리전이라도 계정마다
    # 팔레트가 다르다(RC 297 데이터셋 vs 일반 21, 2026-07-27 실측).
    _ds = frozenset(str(d) for d in datasets) if datasets else None
    _dskey = ('all' if _ds is None else f'{len(_ds)}:{hash(_ds) & 0xffffff:06x}')
    key = f'{n}:{cov_floor}:{want_delay}:{want_region}:{want_universe}:{_mt:.0f}:{_dskey}'
    with _POOL_LOCK:
        if key in _POOL_CACHE:
            return _POOL_CACHE[key]

    rows = _all_rows()
    if want_delay is not None:
        rows = [r for r in rows if str(r.get('delay') or '').strip() == want_delay]
    if want_region is not None:
        rows = [r for r in rows if str(r.get('region') or '').strip().upper() == want_region]
    if want_universe is not None:
        rows = [r for r in rows
                if str(r.get('universe') or '').strip().upper() == want_universe]
    if _ds is not None:
        # category 컬럼에 dataset.id 가 들어 있다 (map_datafields 참조).
        rows = [r for r in rows if str(r.get('category') or '').strip() in _ds]
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        name = (r.get('name') or '').strip()
        if not name:
            continue
        if _safe_int(r.get('coverage'), 0) < cov_floor:
            continue
        fam = classify_family(name)
        if fam:
            buckets.setdefault(fam, []).append(r)

    out: dict[str, list[str]] = {}
    for fam, rs in buckets.items():
        proven = sorted(rs, key=lambda r: (-_safe_int(r.get('coverage')),
                                           -_safe_int(r.get('alphas')),
                                           r.get('name', '')))
        gems = sorted(rs, key=lambda r: (_safe_int(r.get('alphas')),
                                         -_safe_int(r.get('coverage')),
                                         r.get('name', '')))
        # 🆕 고가치·저사용 (2026-08-26, WQB Data Explorer 피드백) — Dataset Value Score
        #   높은 데이터셋의 필드를 셋째 바구니로 끼운다. 한 family(특히 'model')가
        #   risk70·pv 같은 대형 데이터셋에 눌려 고가치 소형 데이터셋(quant_factor_lib
        #   VS5·ai_news_scores VS4 등)을 팔레트에 못 올리던 편중을 깬다. VS 미상(0)이면
        #   자연히 뒤로 밀려 기존 동작과 같다(fail-open).
        valued = sorted(rs, key=lambda r: (-dataset_value_score(r.get('category')),
                                           _safe_int(r.get('alphas')),
                                           -_safe_int(r.get('coverage')),
                                           r.get('name', '')))
        picked: list[str] = []
        seen: set[str] = set()
        for i in range(len(rs)):
            for src in (proven, gems, valued):
                if i < len(src):
                    nm = src[i].get('name', '')
                    if nm and nm not in seen:
                        seen.add(nm)
                        picked.append(nm)
                if len(picked) >= n:
                    break
            if len(picked) >= n:
                break
        out[fam] = picked

    with _POOL_LOCK:
        _POOL_CACHE[key] = out
    return out


def region_field_names(delay=None, region=None, universe=None,
                       datasets=None) -> frozenset:
    """조건에 **실존하는 전체** 필드명 — family_pools 와 달리 계열분류·계열당
    상한·커버리지 컷을 적용하지 않는다. `_apply_constraint` 의 존재 검사 전용:
    캡 걸린 풀로 존재를 검사하면 멀쩡한 필드가 '리전에 없음'으로 오판돼 몰래
    치환된다 (2026-08-03 실측: 전략스펙 resvol→srisk, divyild→indmom 둔갑).
    빈 frozenset = '이 조건 팔레트가 없다' (호출부는 검사 자체를 접어야 한다)."""
    want_delay = None if delay is None else str(delay).strip()
    want_region = None if region is None else str(region).strip().upper()
    want_universe = None if universe is None else str(universe).strip().upper()
    _ds = frozenset(str(d) for d in datasets) if datasets else None
    try:
        _mt = os.path.getmtime(_LIVE_CSV_PATH)
    except OSError:
        _mt = 0
    _dskey = ('all' if _ds is None else f'{len(_ds)}:{hash(_ds) & 0xffffff:06x}')
    key = f'names:{want_delay}:{want_region}:{want_universe}:{_mt:.0f}:{_dskey}'
    with _POOL_LOCK:
        if key in _POOL_CACHE:
            return _POOL_CACHE[key]
    rows = _all_rows()
    if want_delay is not None:
        rows = [r for r in rows if str(r.get('delay') or '').strip() == want_delay]
    if want_region is not None:
        rows = [r for r in rows
                if str(r.get('region') or '').strip().upper() == want_region]
    if want_universe is not None:
        rows = [r for r in rows
                if str(r.get('universe') or '').strip().upper() == want_universe]
    if _ds is not None:
        rows = [r for r in rows if str(r.get('category') or '').strip() in _ds]
    names = frozenset((r.get('name') or '').strip() for r in rows) - {''}
    with _POOL_LOCK:
        _POOL_CACHE[key] = names
    return names


def vector_field_names() -> set[str]:
    """type 컬럼이 vector 인 필드명 집합 (lowercase). raw 로 쓰면 시뮬이 죽으므로
    genome_models 가 vec_avg() 로 감싸야 한다."""
    names: set[str] = set()
    for r in _all_rows():
        if (r.get('type') or '').strip().lower() == 'vector':
            n = (r.get('name') or '').strip()
            if n:
                names.add(n)
    return names


def _apply_region_filter(rows: list[dict], region: str) -> list[dict]:
    region_lc = region.lower()
    return [r for r in rows if r.get('region', '').lower() == region_lc]


def _apply_delay_filter(rows: list[dict], delay: str) -> list[dict]:
    return [r for r in rows if str(r.get('delay', '')) == delay]


def _apply_universe_filter(rows: list[dict], universe: str) -> list[dict]:
    uni_lc = universe.lower()
    return [r for r in rows if r.get('universe', '').lower() == uni_lc]


def _select_diverse(pool: list[dict], n: int, seed: int) -> list[dict]:
    """Return up to n diverse fields from pool using three-bucket strategy.

    Bucket 1 (~40%): highest coverage — reliable, clean-simulating fields.
    Bucket 2 (~30%): undiscovered gems — low alphas (popularity) AND
                     coverage >= floor — decorrelation lever.
    Bucket 3 (~30%): rotating window — stable-sorted pool sliced at
                     offset = seed % len(pool), wrapping around.
    All buckets de-duplicate by field name; later buckets fill unused quota.
    """
    if not pool:
        return []

    n = max(1, n)
    seen: set[str] = set()
    result: list[dict] = []

    def add_up_to(rows: list[dict], target: int) -> None:
        """Add unique rows from `rows` until len(result) == target (or rows exhausted)."""
        for r in rows:
            if len(result) >= target:
                return
            name = r.get('name', '')
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(r)

    # ---- Bucket 1: ~40% highest coverage --------------------------------
    b1_target = max(1, round(n * 0.40))
    by_coverage = sorted(pool, key=lambda r: _safe_int(r.get('coverage', 0)), reverse=True)
    add_up_to(by_coverage, b1_target)

    # ---- Bucket 2: ~30% undiscovered gems --------------------------------
    # coverage >= floor ensures the field actually simulates; low alphas = under-mined.
    coverage_floor = 20
    gems = [
        r for r in pool
        if _safe_int(r.get('coverage', 0)) >= coverage_floor
    ]
    # Sort: least popular first, then by coverage descending (prefer reliable low-pop)
    gems_sorted = sorted(
        gems,
        key=lambda r: (_safe_int(r.get('alphas', 0)),
                       -_safe_int(r.get('coverage', 0)))
    )
    b2_target = b1_target + max(1, round(n * 0.30))
    add_up_to(gems_sorted, b2_target)

    # ---- Bucket 3: rotating window over the whole pool ------------------
    # Stable sort by name guarantees deterministic ordering; the offset rotates
    # each round (via seed) so different long-tail fields are exposed each time.
    pool_sorted = sorted(pool, key=lambda r: r.get('name', ''))
    offset = seed % len(pool_sorted) if pool_sorted else 0
    rotated = pool_sorted[offset:] + pool_sorted[:offset]
    add_up_to(rotated, n)  # fill any remaining slots up to n

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_palette(
    region: str = 'USA',
    delay: 'str | int | None' = None,
    universe: 'str | None' = None,
    n: int = 55,
    seed: int = 0,
    _csv_path: 'str | None' = None,   # injectable for tests; None = auto-detect live vs static
) -> str:
    """Build a compact, diverse datafield palette for a Gemini alpha prompt.

    Parameters
    ----------
    region:   Filter rows by region (case-insensitive).  Default 'USA'.
    delay:    If given, also filter by delay==str(delay).  Relaxed if < n rows.
    universe: If given, prefer rows matching universe.    Relaxed if < n rows.
    n:        Number of fields to include.  Default 55.
    seed:     Rotating offset for bucket 3 (long-tail window).
              Pass round_num here so different rounds see different fields.

    Returns
    -------
    A compact text block bounded to n+1 lines (header + n fields),
    or '' on any error (caller falls back gracefully).
    """
    try:
        # 명시 경로가 있으면 그것만(테스트용), 아니면 라이브 ∪ 정적 합집합.
        # ⚠ 합집합엔 같은 필드가 D0·D1 두 행으로 들어온다 → **이름 기준 중복 제거**.
        #   안 하면 팔레트 n 슬롯의 절반이 같은 필드로 낭비된다.
        rows = _load_csv(_csv_path) if _csv_path else _all_rows()
        if not rows:
            return ''

        # ---- Step 1: region filter (mandatory) --------------------------
        # ⚠ **이름 중복 제거보다 먼저** 걸러야 한다 (2026-07-27). 라이브 CSV 가
        #   다중 리전을 담게 되면서, 이름 dedup 을 먼저 하면 같은 필드의 GLB 행이
        #   USA 행을 밀어내고 → 리전 필터가 그 필드를 통째로 떨어뜨린다(USA 팔레트에
        #   구멍). 리전을 먼저 고정하면 dedup 은 D0/D1 중복만 접는다.
        pool = _apply_region_filter(rows, region)
        if not pool:
            # If region matched nothing, use full set as last resort
            pool = list(rows)
        _seen_names: set = set()
        _uniq = []
        for _r in pool:
            _nm = (_r.get('name') or '').strip().lower()
            if _nm and _nm not in _seen_names:
                _seen_names.add(_nm)
                _uniq.append(_r)
        pool = _uniq

        # ---- Step 2: universe filter (optional, relax if starved) -------
        universe_relaxed = False
        if universe is not None:
            uni_pool = _apply_universe_filter(pool, universe)
            if len(uni_pool) >= n:
                pool = uni_pool
            else:
                # relax — keep broader pool
                universe_relaxed = True

        # ---- Step 3: delay filter (optional, relax if starved) ----------
        delay_str: str | None = None
        delay_relaxed = False
        if delay is not None:
            delay_str = str(delay)
            delay_pool = _apply_delay_filter(pool, delay_str)
            if len(delay_pool) >= n:
                pool = delay_pool
            else:
                # relax — keep broader pool (palette never starved)
                delay_relaxed = True

        # ---- Step 4: select diverse set ---------------------------------
        selected = _select_diverse(pool, n, seed)

        if not selected:
            return ''

        # ---- Step 5: format output (compact, token-efficient) -----------
        scope_parts = [f'region={region}']
        if delay_str is not None:
            delay_token = f'delay={delay_str}(relaxed)' if delay_relaxed else f'delay={delay_str}'
            scope_parts.append(delay_token)
        if universe is not None:
            uni_token = f'universe={universe}(relaxed)' if universe_relaxed else f'universe={universe}'
            scope_parts.append(uni_token)
        scope_parts.append(f'n={len(selected)}')
        header = f'# datafields palette ({", ".join(scope_parts)})'

        lines = [header]
        for r in selected:
            name = r.get('name', '')
            category = r.get('category', '')
            cov = r.get('coverage', '')
            pop = r.get('alphas', '')
            lines.append(f'{name} | {category} | cov={cov} | pop={pop}')

        return '\n'.join(lines)

    except Exception:
        return ''
