"""Typed genome generators for HYFE IQC alpha search — genetic-algorithm core.

The split is by account type:
- StandardGenomeModel: 일반(비 RC) 계정. 유전자 범위(decay/truncation)가 넓고
  하한만 강제한다. 시뮬 백엔드는 RC 와 **동일한 REST API** 다 — 예전엔 Playwright
  브라우저 경로를 노렸으나 2026-07-13 제거됐다.
- ResearchConsultantGenomeModel: RC accounts. It renders stricter API-safe
  FASTEXPR and assumes the API backend will submit-attempt every completed alpha.

Population composition (진짜 GA):
- 탐색 라운드: seed(엘리트) 유전체가 있으면 교차(crossover)/변이(mutate) 자식 절반
  + 무작위 탐색 절반. seed 가 없으면 전량 무작위.
- focus 라운드: 부모 유전체를 fail 사유(정향, directed mutation)에 따라 변이.
  seed 가 있으면 마지막 2 슬롯은 부모×seed 교차.
- 밴딧 arm(slot_settings)은 '무작위 탐색' 슬롯의 settings 유전자에만 주입한다 —
  GA 자식은 부모에게서 settings 를 유전받는 것이 목적이므로 덮어쓰지 않는다.
"""
from __future__ import annotations

import os as _os

import dataclasses
from dataclasses import dataclass
import math
import random
import re
import zlib
from typing import Iterable

from . import mutation_learn as _mutation_learn

SHARED_DATASETS = {
    "pv": ("close", "open", "high", "low", "vwap", "volume", "returns", "adv20", "cap"),
    "fundamental": ("net_income_adjusted", "assets", "equity", "debt", "liabilities", "cashflow_op", "cashflow", "cash", "dividend"),
    "analyst": ("anl4_bvps_mean", "anl4_netdebt_mean", "anl4_adjusted_netincome_ft", "anl4_afv4_eps_mean", "anl4_afv4_eps_high", "anl4_afv4_eps_low", "anl4_afv4_eps_number"),
    "option": ("implied_volatility_call_120", "implied_volatility_put_120", "implied_volatility_mean_150", "implied_volatility_mean_skew_150", "historical_volatility_120", "mdl77_voldiff_pc"),
    "news": ("nws12_afterhsz_result2", "nws12_afterhsz_vol_ratio", "nws12_allz_result2", "news_dividend_yield"),
    # ── 2026-07-21~22 라이브 발굴에서 검증된 신규 계열 ──────────────────────
    # 그날 최고 알파들이 여기서 나왔는데 GA 는 이 필드들을 본 적이 없었다.
    #   -ts_zscore(divide(executed_short_trade_share_count,
    #               aggregate_executed_trade_share_count), 5)  단독 Sharpe 2.28
    #   -ts_zscore(shrt36_svol/(shrt36_totalvolume+1), 5)       단독 Sharpe 1.84
    # 공매도 체결 비중 = 당일 매도압력의 직접 계량. 가격 데이터를 전혀 안 써서
    # pv1 금지 테마에서도 살아남고, 가격반전 계열과 상관이 낮다(계열 분산의 열쇠).
    "shortinterest": ("shrt36_svol", "shrt36_totalvolume", "shrt36_sexemptvol",
                      "executed_short_trade_share_count",
                      "aggregate_executed_trade_share_count",
                      "reported_short_sale_share_quantity",
                      "reported_total_trade_share_quantity"),
    # 옵션 시장 수급 불균형 — 기관/시장조성자 방향성.
    "imbalance": ("customer_vol_imbalance", "firm_vol_imbalance",
                  "broker_dealer_vol_imbalance", "market_maker_vol_imbalance",
                  "customer_trade_imbalance", "broker_dealer_trade_imbalance"),
}
# option 계열에 **주가 대체 필드**를 추가한다 — pv1 금지 테마의 유일한 통로였다.
# opt6_vimtaxp = IV 산출 시점 기초자산 주가(커버리지 98%, 당시 사용 알파 9개).
# -ts_zscore(opt6_vimtaxp,5) 단독으로 Sharpe 2.18 (STATISTICAL/decay4).
SHARED_DATASETS["option"] = SHARED_DATASETS["option"] + (
    "opt6_vimtaxp", "opt6_xxpslckw1", "opt6_kw1gnhcxpkts", "opt6_ivpctile1y",
    "option_breakeven_10", "pcr_vol_10", "pcr_oi_10",
    "implied_volatility_call_30", "historical_volatility_10",
)
VECTOR_FIELDS = frozenset({"nws12_afterhsz_result2", "nws12_afterhsz_vol_ratio", "nws12_allz_result2"})
# USA 리전 실제 허용값 (OPTIONS /simulations 실측, 2026-07-21). TOP2000/TOPSP500 은
# 여태 GA 가 존재조차 몰랐던 유니버스다.
UNIVERSES = ("TOP3000", "TOP2000", "TOP1000", "TOP500", "TOP200", "TOPSP500")
# 중립화도 실측 11종 전부를 연다. 여태 뒤 5종(그룹 중립화)만 쓰고 있었는데,
# WQB 가 권장하는 **리스크 팩터 중립화**(SLOW/FAST/SLOW_AND_FAST/RAM/STATISTICAL/
# CROWDING)를 통째로 못 쓰고 있었던 셈이다. 특히 REVERSION_AND_MOMENTUM(=RAM)은
# 고회전 Orthogonal 하위분류의 유일한 열쇠다(criteria.HT_ORTHOGONAL_NEUTRALIZATION).
# ⚠ 리스크 중립화는 turnover 를 **올린다**(FAST 계열일수록 크게) — 고회전 전략과 상성이 좋다.
NEUTRALIZATIONS = ("INDUSTRY", "SECTOR", "SUBINDUSTRY", "MARKET", "NONE",
                   "REVERSION_AND_MOMENTUM", "FAST", "SLOW_AND_FAST", "SLOW",
                   "CROWDING", "STATISTICAL")
# 리스크 팩터 중립화 — 그룹 중립화와 달리 group_by 개념이 없다(내부적으로 market 중립).
RISK_NEUTRALIZATIONS = ("REVERSION_AND_MOMENTUM", "FAST", "SLOW_AND_FAST",
                        "SLOW", "CROWDING", "STATISTICAL")
GROUPS = {"INDUSTRY": "industry", "SECTOR": "sector", "SUBINDUSTRY": "subindustry"}

# 데이터셋 계열별 권장 중립화 (BRAIN "Neutralization 🥉" 문서). 밴딧/변이의 사전확률로 쓴다.
# ⚠ pv 는 문서가 **명시적으로 경고**한다: "Generic ideas work well across all instruments,
#   using Industry or Subindustry neutralization could reduce the performance."
#   그런데 라이브 GA 는 pv 알파에 SUBINDUSTRY/INDUSTRY 를 가장 많이 쓰고 있었다.
# ⚠ 2026-07-22 개정 — **STATISTICAL 을 전 계열의 1순위로 올린다.**
#   그날 실측: 완전히 같은 식에서 중립화만 바꿨을 때
#     SUBINDUSTRY 1.72 → SECTOR 1.40 → INDUSTRY 1.58 → MARKET 1.19 → STATISTICAL 2.47
#   Sharpe 가 +38% 뛰고 MDD 는 절반 이하(9%대 → 4.35%)로 줄었다. 통계적 공통위험(주성분)을
#   제거해 순수 알파만 남기기 때문이다. 게다가 HT 하위분류 3종(AFTER_COST·INVESTABLE·
#   LIQUID)을 함께 얻는다. 여태 GA 는 이 설정을 사실상 안 써봤다.
#   ⚠ RAM(REVERSION_AND_MOMENTUM)은 반대로 **가격반전 신호를 통째로 지운다**(2.38 → 0.46).
#     Orthogonal 하위분류를 노릴 때만 의도적으로 쓸 것 — 사전확률에 넣지 않는다.
FAMILY_NEUTRALIZATION = {
    "pv": ("STATISTICAL", "MARKET", "SECTOR"),
    "fundamental": ("STATISTICAL", "INDUSTRY"),
    "analyst": ("STATISTICAL", "INDUSTRY"),
    "model": ("STATISTICAL", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY"),
    "news": ("STATISTICAL", "SUBINDUSTRY"),
    "option": ("STATISTICAL", "MARKET", "SECTOR"),
    "shortinterest": ("STATISTICAL", "MARKET", "CROWDING"),
    "imbalance": ("STATISTICAL", "MARKET", "SECTOR"),
}

# ── 파생 pv 팩터 (synthetic fields) ──────────────────────────────────────────
# 'field' 자리에 쓸 수 있는 **합성 지표**. 원시 pv 9개(close/open/…)만으로는 표현할 수
# 없는 일중 구조(종가위치·갭·괴리·비유동성)를 유전자 한 칸에 담는다.
# 근거: 6월 최고 알파(Sharpe 3.77)의 핵심이 CLV = ((close-low)-(high-close))/(high-low)
# 였는데, 현행 문법은 그걸 **표현할 방법 자체가 없었다**(2026-07-14 진단).
# 전부 pv 원시 필드만 쓰므로 delay=0 제약을 깨지 않는다.
SYNTHETIC_FIELDS = {
    # 종가가 당일 고저 range 어디에 붙었나 [-1,1] — 매수/매도 압력.
    "syn_clv": "(((close-low)-(high-close))/(high-low+0.000001))",
    # 당일 시가대비 수익 — 장중 모멘텀.
    "syn_oc_ret": "((close-open)/(open+0.000001))",
    # 갭 — 전일 종가 대비 시가.
    "syn_gap": "((open-ts_delay(close,1))/(ts_delay(close,1)+0.000001))",
    # 일중 변동폭(정규화) — 변동성 프록시.
    "syn_range": "((high-low)/(close+0.000001))",
    # VWAP 괴리 — 체결가 대비 종가 위치(수급 압력).
    "syn_vwap_dev": "((close-vwap)/(vwap+0.000001))",
    # Amihud 비유동성 — 거래량당 가격충격.
    "syn_illiq": "(abs(returns)/(volume+1))",
    # 거래대금 회전 — 관심도.
    "syn_turn": "(volume/(adv20+1))",
    # ── 비-pv 합성 팩터 (2026-07-21 발굴 실측) ──────────────────────────────
    # ⚠ 왜 필요한가 — 유전체는 '필드 3개' 슬롯이라 `A/B` 같은 **비율식을 표현할 수
    #   없다**. 그런데 그날 최고 알파들이 정확히 비율이었다:
    #     -ts_zscore(shrt36_svol/(shrt36_totalvolume+1), 5)          단독 Sharpe 1.84
    #     -ts_zscore(divide(executed_short_trade_share_count,
    #                       aggregate_executed_trade_share_count), 5) 단독 Sharpe 2.28
    #   합성 필드로 감싸면 GA 가 그 영역을 **표현할 수 있게** 된다.
    #   pv1 을 전혀 안 쓰므로 pv1 금지 테마에서도 살아남는다(그게 핵심 가치다).
    # 당일 공매도 체결 비중 — 매도압력의 직접 계량 (NYSE Arca).
    "syn_short_ratio": "(shrt36_svol/(shrt36_totalvolume+1))",
    # 전 시장 공매도 체결 비중 (다른 소스 — 위와 상관이 낮아 결합 시 보완적).
    "syn_uss_ratio": "divide(executed_short_trade_share_count,"
                     "aggregate_executed_trade_share_count)",
    "syn_uss_ratio2": "divide(reported_short_sale_share_quantity,"
                      "reported_total_trade_share_quantity)",
    # 옵션 풋/콜 내재변동성 스큐 — 하방 헤지 수요.
    "syn_iv_skew": "(implied_volatility_put_30-implied_volatility_call_30)",
    # 내재/역사 변동성 비율 — 옵션시장이 실현변동성 대비 얼마나 겁먹었나.
    "syn_iv_hv": "divide(implied_volatility_call_30,historical_volatility_30)",
}

# 합성 팩터는 **원료가 속한 계열**에 붙인다. 전부 pv 에 몰아넣으면 pv1 금지 테마에서
# 공매도·옵션 합성까지 통째로 사라진다(원료엔 pv1 이 한 톨도 없는데도).
_SYNTHETIC_FAMILY = {
    "syn_short_ratio": "shortinterest", "syn_uss_ratio": "shortinterest",
    "syn_uss_ratio2": "shortinterest",
    "syn_iv_skew": "option", "syn_iv_hv": "option",
}
for _syn in SYNTHETIC_FIELDS:
    _fam = _SYNTHETIC_FAMILY.get(_syn, "pv")
    SHARED_DATASETS[_fam] = tuple(SHARED_DATASETS.get(_fam, ())) + (_syn,)


# ── delay=0 전용 필드 팔레트 ─────────────────────────────────────────────────
# 비어 있으면 '모른다' 는 뜻이고, 그 경우 D0 는 예전처럼 pv 로만 간다(안전 폴백).
# 채워지면 D0 에서도 option/fundamental/analyst/news/model 을 쓸 수 있다.
# 근거: option6 는 D0 에 131 필드, fundamental2 는 766 필드가 실재한다(2026-07-21 실측).
# 이게 비어 있던 탓에 USA/D0/OPTION 피라미드 **1.7배(전 항목 최고)** 를 구조적으로 못 받았다.
D0_DATASETS: dict = {}


def _load_d0_palette() -> None:
    """라이브 CSV 에서 delay=0 필드 팔레트를 읽는다. 없으면 빈 dict 유지(폴백)."""
    global D0_DATASETS
    try:
        from . import datafield_palette as _dp
        if not _dp.DYNAMIC_FIELDS_ON:
            return
        pools = _dp.family_pools(delay=0)
    except Exception:
        return
    if not pools:
        return
    # pv 는 curated(합성 필드 포함)를 쓴다 — syn_* 는 원시 pv 로만 만들어 D0 에서 항상 안전한데
    # API 팔레트엔 당연히 없다.
    d0 = {fam: tuple(names) for fam, names in pools.items() if names}
    d0['pv'] = SHARED_DATASETS['pv']
    D0_DATASETS = d0


def d0_allowed_fields() -> frozenset | None:
    """D0 에서 써도 되는 필드명 집합. 팔레트가 없으면 None ('모름' → 폴백)."""
    if not D0_DATASETS:
        return None
    return frozenset(f for fields in D0_DATASETS.values() for f in fields)


def _extend_datasets_from_csv() -> None:
    """CSV(5200행) 로 family 팔레트를 넓힌다 — curated 필드는 **항상 앞에 유지**한다.

    여태 GA 는 family 당 4~9개, 총 ~35개 하드코딩 필드만 보고 진화했다. 정작 5201행짜리
    datafield CSV 는 (지금은 없어진) LLM 자유생성 경로에서만 쓰였다 — 결정론적 GA 가
    우물 안에 갇혀 있었던 셈이다(2026-07-14 진단). 'model'(mdl77/mdl177 = 가치·퀄리티
    팩터 3000여개) family 는 여기서 **처음으로** GA 에 열린다.

    fail-open: CSV 를 못 읽으면 curated 팔레트 그대로 (기존 동작).
    킬스위치: IQC_DYNAMIC_FIELDS=0.
    """
    try:
        from . import datafield_palette as _dp
    except Exception:
        return
    if not _dp.DYNAMIC_FIELDS_ON:
        return
    try:
        # ⚠ **delay=1 행만** 쓴다. 라이브 CSV 는 이제 D0·D1 을 모두 담는데, 섞어서
        #   읽으면 D0 전용 필드가 D1 라운드 알파에 들어가 시뮬이 ERROR 난다.
        #   (정적 CSV 는 전부 D1 이라 예전엔 이 구분이 필요 없었다.)
        #   D1 행이 하나도 없으면(정적 CSV 등 delay 컬럼 부재) 전체로 폴백한다.
        pools = _dp.family_pools(delay=1) or _dp.family_pools()
        vectors = _dp.vector_field_names()
    except Exception:
        return
    if not pools:
        return
    global VECTOR_FIELDS
    VECTOR_FIELDS = frozenset(VECTOR_FIELDS | vectors)
    for fam, names in pools.items():
        cur = SHARED_DATASETS.get(fam, ())
        extra = tuple(n for n in names if n not in cur)
        if cur or extra:
            SHARED_DATASETS[fam] = tuple(cur) + extra


_extend_datasets_from_csv()
_load_d0_palette()

_FAMILY_OF_FIELD = {f: fam for fam, fs in SHARED_DATASETS.items() for f in fs}
_ALL_FIELDS_RX = re.compile(
    r"\b(" + "|".join(sorted(_FAMILY_OF_FIELD, key=len, reverse=True)) + r")\b")
_TRANSFORM_RX = re.compile(r"\b(ts_rank|ts_zscore|ts_delta|ts_mean|rank)\s*\(")
_TS_WINDOW_RX = re.compile(r"\bts_\w+\([^()]*?,\s*(\d+)\s*\)")


@dataclass(frozen=True)
class Genome:
    model: str
    family: str
    fields: tuple[str, str, str]
    transform_a: str
    transform_b: str
    combine: str
    sign: int
    lookback_a: int
    lookback_b: int
    universe: str
    neutralization: str
    decay: int
    truncation: float
    nan_handling: str = "OFF"
    # 바깥 스무딩 연산자 선택 (decay >= 8 일 때만 발현). ts_mean 은 균등가중,
    # ts_decay_linear 는 최근값 가중 — 같은 신호라도 turnover/Sharpe 가 크게 갈린다.
    decay_style: str = "mean"
    generation: int = 0
    # ── v2 유전자 (LLM 전략 아이디어를 무손실로 담기 위한 확장) ────────────────
    # ⚠ 전부 '기본값이면 render() 산출물이 확장 이전과 바이트 단위로 동일'해야 한다.
    #   기존 19k 알파의 code_hash(캐시 키)와 _dedup_key 가 그대로 살아야 하기 때문.
    #   tests/test_genome_v2.py 의 골든 테스트가 이 불변식을 못박는다.
    trade_when: str = "OFF"        # 조건부 진입 (turnover 억제 — 조건 밖이면 미보유)
    group_op: str = "neutralize"   # 그룹 연산: neutralize | rank | zscore | none
    group_by: str = "auto"         # 그룹 기준: auto(=neutralization 따름) | sector | ...
    winsor_std: int = 0            # 신호 이상치 절단 (0=off)
    weight_scheme: str = "1:1"     # 2팩터 가중 (sum/spread 에서만 발현)
    # ── v3 유전자 (2026-07-14) — 6월 고성과 알파의 표현을 담기 위한 확장 ──────────
    # ⚠ v2 와 같은 계약: **기본값이면 render() 산출물이 확장 이전과 바이트 동일**해야
    #   한다. tests/test_genome_v2.py + test_genome_v3.py 의 골든 테스트가 못박는다.
    transform_c: str = "ts_zscore"  # 3번째 팩터의 변환 (triple 결합에서만 발현)
    lookback_c: int = 0             # 0 = auto → max(20, lookback_b) (기존 동작)
    regime: str = "OFF"             # 레짐 조건부: 조건 밖에서 신호를 0 으로 (보유 X)
    hump: float = 0.0               # 0 = off. 신호 변화 억제 → turnover 직접 제어
    # ── v4 유전자 (2026-08-17) — 같은 골든 계약: 기본값이면 render() 산출물 불변.
    sentinel: str = "OFF"           # 센티널 값을 NaN 으로 (OFF | -1 | 0)


# 표준 시간창 — BRAIN "Recommended Practices" 권고: "DO restrict parameter search to
# simple & reasonable ones. For example 5, 20, 60, 120, 252 in case of days, instead of
# 37, 14 etc." (+ 고회전 신호용 짧은 창 2~10일). 임의의 창(37·80·240)은 과최적화로 읽히고,
# Genius 타이브레이커도 '알파당 distinct 연산자/필드 수가 적을수록 유리' 하다.
CANONICAL_LOOKBACKS = (2, 3, 5, 10, 20, 60, 120, 252)

# 강신호 골격 8종 (2026-07-31) — 무작위 슬롯의 절반에 입힌다. 균등 무작위
# 변환×결합 조합은 대부분 잡음이라 신선 필드 프로브가 광맥을 못 뚫는다
# (이번 주 news/analyst 프로브 ~300개 전멸). 같은 신선 필드에 이 골격을 입힌
# 시드는 당일 전 체크 통과를 만들었다(리비전 모멘텀·잔차화·감성 델타 등
# 부트캠프 강의의 검증된 시드 템플릿). 키는 Genome 유전자명과 일치해야 한다.
STRONG_TEMPLATES = (
    dict(transform_a="ts_zscore", transform_b="ts_zscore", combine="sum",
         sign=1, lookback_a=60, lookback_b=60, decay=6),
    dict(transform_a="rank", transform_b="rank", combine="spread",
         sign=-1, lookback_a=20, lookback_b=20, decay=4),
    dict(transform_a="ts_delta", transform_b="ts_mean", combine="sum",
         sign=-1, lookback_a=3, lookback_b=10, decay=2),
    dict(transform_a="ts_zscore", transform_b="ts_zscore", combine="resid",
         sign=1, lookback_a=20, lookback_b=20, decay=4),
    dict(transform_a="ts_delta", transform_b="ts_zscore", combine="product",
         sign=-1, lookback_a=5, lookback_b=20, decay=4, hump=0.03),
    dict(transform_a="ts_zscore", transform_b="ts_zscore", transform_c="ts_zscore",
         combine="triple", sign=1, lookback_a=60, lookback_b=60, lookback_c=60,
         decay=6),
    dict(transform_a="ts_av_diff", transform_b="ts_mean", combine="ratio",
         sign=1, lookback_a=60, lookback_b=120, decay=8),
    dict(transform_a="ts_rank", transform_b="ts_zscore", combine="corr",
         sign=-1, lookback_a=20, lookback_b=20, decay=2),
)


def _next_longer(v: int) -> int:
    """표준 창 중 v 보다 큰 첫 값 (없으면 252). smooth 축이 창을 늘릴 때 쓴다 —
    옛 코드는 `× rng.choice((2,3))` 이라 40→80·120→240 같은 비표준 창을 만들었다."""
    for c in CANONICAL_LOOKBACKS:
        if c > v:
            return c
    return CANONICAL_LOOKBACKS[-1]


DECAY_STYLES = ("mean", "linear")
# 변환 연산자 전집합 — BaseGenomeModel.transforms 의 단일 진실(모듈 레벨 검증용).
_TRANSFORM_KINDS = ("rank", "ts_rank", "ts_zscore", "ts_delta", "ts_mean", "ts_av_diff")

# 조건부 진입 유전자 — 조건이 참일 때만 알파를 보유하고, 거짓이면 -1(=미보유).
# 조건식은 **pv 필드만** 사용한다 (delay=0 라운드에서도 필드 제약을 깨지 않기 위해).
TRADE_WHEN_KINDS = ("OFF", "vol_calm", "vol_surge", "trend_up", "liquid")
_TRADE_WHEN_CONDS = {
    "vol_calm": "rank(ts_std_dev(returns,22))<0.5",
    "vol_surge": "ts_rank(volume,20)>0.7",
    "trend_up": "ts_delta(close,5)>0",
    "liquid": "rank(adv20)>0.5",
}
# 레짐 조건부 유전자 — trade_when 과 다르다. trade_when 은 **최종 알파**를 조건 밖에서
# -1(미보유)로 만들고, regime 은 **코어 신호**를 조건 밖에서 0 으로 만든 뒤 그 위에
# 중립화·평활을 얹는다. 6월 최고 알파(Sharpe 3.77)가 정확히 이 형태였다:
#   _sig = _range_vol > 1.3 ? -1*rank(clv) : 0 ; hump(group_neutralize(decay(_sig),subind))
# 조건식은 pv 필드만 쓴다 (delay=0 라운드 제약 유지).
REGIME_KINDS = ("OFF", "range_expand", "range_calm", "vol_high", "vol_low",
                "trend_up", "trend_down", "volume_surge")
_REGIME_CONDS = {
    # 6월 3.77 알파의 조건 — 일중 변동폭이 평소보다 크게 벌어진 국면.
    "range_expand": "(ts_std_dev(high-low,20)/(ts_mean(high-low,20)+0.000001))>1.3",
    "range_calm": "(ts_std_dev(high-low,20)/(ts_mean(high-low,20)+0.000001))<0.8",
    "vol_high": "rank(ts_std_dev(returns,22))>0.7",
    "vol_low": "rank(ts_std_dev(returns,22))<0.3",
    "trend_up": "ts_delta(close,20)>0",
    "trend_down": "ts_delta(close,20)<0",
    "volume_surge": "ts_rank(volume,20)>0.8",
}
# hump 문턱 — 신호가 이만큼 안 움직이면 포지션을 그대로 둔다(리밸런싱 억제).
# 6월 3.77 알파는 0.055, 3.43 알파는 0.03 을 썼다. 0 = off.
HUMPS = (0.0, 0.01, 0.03, 0.055, 0.1)
GROUP_OPS = ("neutralize", "rank", "zscore", "none")
# country (2026-07-31): GLB 리전에서 나라별 강건성을 올리는 그룹 축 — 부트캠프 3주차
# 강의(GAC)의 "컨트리 기준 그룹 중립화가 리전 서브유니버스 통과에 유리" 반영.
# 단일국 리전(USA)에선 퇴화(무해)라 팔레트에 그냥 둔다.
GROUP_BYS = ("auto", "sector", "industry", "subindustry", "market", "country")
WINSOR_STDS = (0, 3, 4, 5)
# 2팩터 가중 — 'a:b' = 첫 팩터 a배, 둘째 팩터 b배. sum/spread 결합에서만 발현한다
# (product/ratio/corr 은 스케일이 상쇄되거나 의미가 달라져 가중이 무의미).
WEIGHT_SCHEMES = {"1:1": None, "2:1": (2, 1), "1:2": (1, 2), "3:1": (3, 1)}

#: PROD/SELF 상관 거절 뒤 시도할 decay 사다리 (2026-08-18 실측 기반).
#: 24 와 36 에서 실제 제출이 나왔다. 12 는 못 뚫었고, 42 이상은 신호가 죽기 시작한다.
DECORR_DECAYS = (24, 30, 36, 42)

_GENE_NAMES = tuple(f.name for f in dataclasses.fields(Genome))


def _snap_hump(v) -> float:
    """hump 값을 팔레트(HUMPS)의 가장 가까운 값으로 스냅한다.

    ⚠ '팔레트 밖이면 0.0' 으로 죽이면 안 된다 — 6월 알파의 hump=0.055 는 팔레트에 있지만
    0.02 같은 값은 없다. 0 으로 눌러버리면 역추출 시딩에서 그 유전자가 통째로 증발한다.
    """
    try:
        f = float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if f <= 0:
        return 0.0
    return min(HUMPS, key=lambda h: abs(h - f))


def _coerce_genome(obj) -> Genome | None:
    """dict/Genome → Genome. 알 수 없는 키는 버리고 누락 키는 기본값으로 채운다."""
    if obj is None:
        return None
    if isinstance(obj, Genome):
        return obj
    if not isinstance(obj, dict):
        return None
    try:
        d = {k: obj[k] for k in _GENE_NAMES if k in obj}
        d.setdefault("model", "seed")
        d.setdefault("family", "pv")
        flds = tuple(d.get("fields") or ())
        if len(flds) != 3:
            flds = tuple(SHARED_DATASETS["pv"][:3])
        d["fields"] = tuple(str(f) for f in flds)
        d.setdefault("transform_a", "rank")
        d.setdefault("transform_b", "ts_zscore")
        d.setdefault("combine", "sum")
        d["sign"] = -1 if int(d.get("sign") or 1) < 0 else 1
        d["lookback_a"] = max(1, min(252, int(d.get("lookback_a") or 20)))
        d["lookback_b"] = max(1, min(252, int(d.get("lookback_b") or 60)))
        d.setdefault("universe", "TOP3000")
        d.setdefault("neutralization", "INDUSTRY")
        d["decay"] = max(0, min(30, int(d.get("decay") or 0)))
        d["truncation"] = max(0.01, min(0.15, float(d.get("truncation") or 0.08)))
        d.setdefault("nan_handling", "OFF")
        d["decay_style"] = "linear" if str(d.get("decay_style") or "") == "linear" else "mean"
        d["generation"] = int(d.get("generation") or 0)
        # v2 유전자 — 레거시 유전체(19k 행)엔 없으므로 '무해한 기본값'으로 채운다.
        # 이 기본값 조합은 render() 가 확장 이전과 동일한 문자열을 내도록 설계됐다.
        d["trade_when"] = (d.get("trade_when") if d.get("trade_when") in TRADE_WHEN_KINDS
                           else "OFF")
        d["group_op"] = (d.get("group_op") if d.get("group_op") in GROUP_OPS
                         else "neutralize")
        d["group_by"] = (d.get("group_by") if d.get("group_by") in GROUP_BYS else "auto")
        try:
            _ws = int(d.get("winsor_std") or 0)
        except (TypeError, ValueError):
            _ws = 0
        d["winsor_std"] = _ws if _ws in WINSOR_STDS else 0
        d["weight_scheme"] = (d.get("weight_scheme")
                              if d.get("weight_scheme") in WEIGHT_SCHEMES else "1:1")
        # v3 유전자 — 레거시/구 유전체엔 없다. 기본값은 render() 무변화를 보장한다.
        d["transform_c"] = (d.get("transform_c")
                            if d.get("transform_c") in _TRANSFORM_KINDS else "ts_zscore")
        try:
            _lc = int(d.get("lookback_c") or 0)
        except (TypeError, ValueError):
            _lc = 0
        d["lookback_c"] = max(0, min(252, _lc))
        d["regime"] = d.get("regime") if d.get("regime") in REGIME_KINDS else "OFF"
        d["hump"] = _snap_hump(d.get("hump"))
        # v4 — 값은 문자열로 다룬다. 0 과 '0' 을 섞으면 'OFF' 로 떨어져 조용히 무력화된다.
        d["sentinel"] = (str(d.get("sentinel"))
                         if str(d.get("sentinel")) in SENTINELS else "OFF")
        # 탐색 조건은 **맨 마지막**에 건다 — 위의 정규화가 끝난 뒤라야 유니버스·중립화·
        # 필드가 확정된 상태에서 안전하게 덮어쓸 수 있다.
        _apply_constraint(d)
        return Genome(**d)
    except Exception:
        return None


def _infer_regime(flat: str) -> str:
    """공백 제거된 코드 → regime 유전자 (없으면 'OFF').

    렌더러 산출물은 조건식이 그대로 박혀 있어 문자열 일치로 잡히지만, 레거시(LLM) 알파는
    조건을 임시변수로 빼 쓴다 (`_range_vol=…; _sig=_range_vol>1.3 ? … : 0`). 그 경우
    조건식 리터럴이 코드에 없으므로 **의미 기반 휴리스틱**으로 가장 가까운 축을 고른다.
    틀려도 손해가 작다 — 시드의 출발점일 뿐이고 GA 가 곧 변이시킨다.
    """
    if "?" not in flat:
        return "OFF"
    for kind, cond in _REGIME_CONDS.items():
        if cond.replace(" ", "") in flat:
            return kind
    cond_txt = flat.split("?", 1)[0]
    tail = cond_txt[-120:]          # 조건은 '?' 바로 앞에 있다
    up = ">" in tail
    if "ts_std_dev" in flat and ("high-low" in flat or "range" in flat):
        return "range_expand" if up else "range_calm"
    if "ts_std_dev" in flat and "returns" in flat:
        return "vol_high" if up else "vol_low"
    if "volume" in tail:
        return "volume_surge"
    if "ts_delta(close" in tail or "ts_delta(close" in flat:
        return "trend_up" if up else "trend_down"
    return "OFF"


def genome_from_alpha(code: str, settings: dict | None = None,
                      generation: int = 0) -> dict:
    """저장된 알파(code+settings)에서 유전체를 역추출한다 (best-effort).

    ⚠ 이것은 **손실 압축**이다. renderer 산출물이면 거의 정확하지만, 레거시(Gemini)
    다중문 알파는 3필드/2변환/1결합 템플릿으로 찌그러진다 — pass=11 짜리 3팩터 알파가
    2팩터로 줄고 ts_decay_linear 같은 유전자는 통째로 사라진다. 그런 시드로 만든 자식은
    부모를 복제조차 못 하므로 구조적으로 부모보다 나쁘다(2026-07-11 진단).
    그래서 시딩 경로는 이 함수가 아니라 `alphas.genome` 에 저장된 원본 유전체를 읽는다.
    이 함수는 (a) genome 컬럼이 없는 레거시 행의 일회성 백필과 (b) focus 큐의 레거시
    항목 폴백에만 남는다.

    `generation` 은 코드에서 복원할 수 없다 — 호출부가 DB 의 lineage 값을 넘겨야 한다.
    (기본값 0 을 하드코딩하던 과거 동작이 세대를 매 라운드 리셋해 g1 상한을 만들었다.)
    """
    code = str(code or "")
    st = dict(settings or {})
    rng = random.Random(zlib.crc32(code.encode("utf-8")))
    # 합성 팩터를 **먼저** 되찾는다. 안 그러면 CLV 식이 (close, low, high) 3개 원시필드로
    # 분해돼 유전체가 그 구조를 통째로 잃는다 — 6월 Sharpe 3.77 알파의 핵심이 바로 CLV 였다.
    flat_code = code.replace(" ", "")
    found: list[str] = []
    masked = flat_code
    # ⚠ 레짐 조건식은 **먼저** 지운다 (2026-07-27). 조건에 쓰인 high/low/returns 는
    #   regime 유전자에 속하는 가드지 신호 필드가 아닌데, 코드 맨 앞에 있어서 등장순
    #   추출이 이것부터 3개를 채우고 진짜 신호 필드를 밖으로 밀어냈다
    #   (실측: range_expand 알파에서 fields 가 (high, low, adv20…) 로 뒤바뀌고
    #    alternative_slippage_0025 가 통째로 유실 → focus 폴백이 엉뚱한 부모를 변이).
    for _cond in _REGIME_CONDS.values():
        masked = masked.replace(_cond.replace(" ", ""), "")
    for _syn, _expr in SYNTHETIC_FIELDS.items():
        _core = _expr.strip()
        while _core.startswith("(") and _core.endswith(")"):
            _core = _core[1:-1]
        if _core and _core in masked:
            found.append(_syn)
            masked = masked.replace(_core, _syn)   # 원시필드 재검출 방지
    for _f in _ALL_FIELDS_RX.findall(masked):
        if _f not in found:
            found.append(_f)
    family = _FAMILY_OF_FIELD.get(found[0], "pv") if found else "pv"
    pool = [f for f in SHARED_DATASETS.get(family, SHARED_DATASETS["pv"]) if f not in found]
    rng.shuffle(pool)
    while len(found) < 3:
        found.append(pool.pop() if pool else rng.choice(SHARED_DATASETS["pv"]))
    # ⚠ 결합/변환/창 검출은 **masked** 를 본다. 원본 code 를 보면 합성 팩터 내부의
    #   '/(' 나 ')-' 가 결합 연산으로 오탐된다 (CLV → combine='ratio' 오판 실측).
    trs = _TRANSFORM_RX.findall(masked)
    # render() 는 마지막에 rank() 로 감싸므로 첫 토큰이 rank 인 것은 래퍼일 확률이 높다.
    inner = [t for t in trs[1:]] or trs
    ta = inner[0] if inner else "rank"
    tb = inner[1] if len(inner) > 1 else ta
    if "vector_neut(" in masked:
        combine = "resid"
    elif "ts_corr(" in masked:
        combine = "corr"
    elif "/(" in masked:
        combine = "ratio"
    elif re.search(r"\)\s*\*\s*", masked) and "-1*(" not in masked:
        combine = "product"
    elif re.search(r"\)\s*-\s*", masked):
        combine = "spread"
    else:
        combine = "sum"
    windows = [int(w) for w in _TS_WINDOW_RX.findall(masked)[:2]]
    la = windows[0] if windows else 20
    lb = windows[1] if len(windows) > 1 else max(la, 60)
    try:
        decay = int(float(st.get("decay") or 0))
    except (TypeError, ValueError):
        decay = 0
    try:
        trunc = float(st.get("truncation") or 0.08)
    except (TypeError, ValueError):
        trunc = 0.08
    # v2/v3 유전자 역추출 — 코드에 흔적이 남는 것만 복원한다(나머지는 기본값).
    flat = code.replace(" ", "")
    # 코드에 group_* 가 없으면 group_op='none' 이어야 한다. 'neutralize'(기본값)로 두면
    # render() 가 원본에 없던 group_neutralize 를 **새로 끼워 넣어** 부모를 복제하지 못한다
    # (WQB 의 settings.neutralization 은 플랫폼이 적용하는 것이지 코드에 쓰는 게 아니다).
    group_op = "none"
    for _op, _fn in _GROUP_OP_FN.items():
        if f"{_fn}(" in code:
            group_op = _op
            break
    m_w = re.search(r"winsorize\([^()]*std=(\d+)", code)
    m_h = re.search(r"hump=([\d.]+)", code)
    m_tw = next((k for k, v in _TRADE_WHEN_CONDS.items()
                 if v.replace(" ", "") in flat), "OFF") if "trade_when(" in code else "OFF"
    m_rg = _infer_regime(flat)
    try:
        hump_v = float(m_h.group(1)) if m_h else 0.0
    except ValueError:
        hump_v = 0.0
    g = _coerce_genome({
        "model": "seed",
        "family": family,
        "fields": tuple(found[:3]),
        "transform_a": ta,
        "transform_b": tb,
        "combine": combine,
        # 레거시 알파는 '-1.0*' 로도 쓴다 — '-1*' 만 보면 부호가 뒤집힌 채 복제된다.
        "sign": -1 if ("-1*" in flat or "-1.0*" in flat) else 1,
        "lookback_a": la,
        "lookback_b": lb,
        "universe": str(st.get("universe") or "TOP3000"),
        "neutralization": str(st.get("neutralization") or "INDUSTRY"),
        "decay": decay,
        "truncation": trunc,
        "nan_handling": str(st.get("nan_handling") or "OFF"),
        "decay_style": "linear" if "ts_decay_linear(" in code else "mean",
        "generation": int(generation or 0),
        "group_op": group_op,
        "winsor_std": int(m_w.group(1)) if m_w else 0,
        "trade_when": m_tw,
        "regime": m_rg,
        # hump 값이 팔레트 밖이면(예: 6월 알파의 0.055 는 팔레트 안, 0.02 는 밖)
        # _coerce_genome 이 가장 가까운 유효값으로 눌러 준다.
        "hump": hump_v,
    })
    return dict(g.__dict__)


#: 센티널 유전자가 고를 수 있는 값. 'OFF' 는 감싸지 않는다(골든 계약).
SENTINELS = ("OFF", "-1", "0")


def _field_expr(field: str, sentinel: str = "OFF") -> str:
    """유전자의 field 한 칸 → FASTEXPR 조각.

    합성 팩터(syn_*)는 이름이 아니라 **식**으로 전개된다 — WQB 에 그런 필드는 없다.

    sentinel 은 '값이 없음' 을 뜻하는 약속값을 NaN 으로 되돌린다. 애널리스트 점수
    필드의 -1 이 대표적인데, 그대로 두면 최저 등급으로 취급돼 신호가 뒤집힌다.
    0 을 주면 안 되고 NaN 이어야 한다 — 0 은 중립화에서 평균을 끌고 다니지만 NaN 은
    그 종목의 롱·숏을 청산한다(2026-08-12 5주차 강의 실측: 샤프 1.24 → 2.10).
    합성 팩터는 이미 식이라 감싸지 않는다.
    """
    if field in SYNTHETIC_FIELDS:
        return SYNTHETIC_FIELDS[field]
    expr = f"vec_avg({field})" if field in VECTOR_FIELDS else field
    if sentinel and sentinel != "OFF":
        expr = f"to_nan({expr},value={sentinel})"
    return expr


def _transform(expr: str, kind: str, window: int) -> str:
    if kind == "rank":
        return f"rank({expr})"
    if kind == "ts_rank":
        return f"ts_rank({expr},{window})"
    if kind == "ts_zscore":
        return f"ts_zscore({expr},{window})"
    if kind == "ts_delta":
        return f"ts_delta({expr},{max(1, min(window, 20))})"
    if kind == "ts_mean":
        return f"ts_mean({expr},{window})"
    if kind == "ts_av_diff":
        # x - ts_mean(x, d) (NaN 무시). "Favor changes over levels" (고회전 문서) —
        # 레벨이 아니라 평균 대비 편차라 지평이 짧고 회전율이 자연히 높다.
        return f"ts_av_diff({expr},{window})"
    return f"rank({expr})"


def _combine(a: str, b: str, c: str, kind: str, window: int) -> str:
    if kind == "spread":
        return f"({a}-{b})"
    if kind == "sum":
        return f"add({a},{b})"
    if kind == "triple":
        return f"add(add({a},{b}),{c})"
    if kind == "product":
        return f"({a}*{b})"
    if kind == "ratio":
        return f"({a}/(abs({b})+0.000001))"
    if kind == "corr":
        return f"ts_corr({a},{b},{window})"
    if kind == "resid":
        # 잔차 결합 (2026-07-23) — a 에서 b 방향 성분을 빼 **b 와 직교인 신호**만 남긴다.
        # 컨설턴트 부트캠프 강의의 "X에서 Y를 regression/vector_neut 로 빼면 코릴레이션이
        # 낮은 알파가 나온다" 기법. PROD_CORRELATION 이 최종 관문이 된 지금(7/23 실측)
        # 가장 직접적인 탈상관 레버다. regression_neut 는 이 계정 /operators 에 없어
        # vector_neut(라이브 확인, arity 2)를 쓴다.
        return f"vector_neut({a},{b})"
    return f"add({a},{b})"


_GROUP_OP_FN = {"neutralize": "group_neutralize", "rank": "group_rank",
                "zscore": "group_zscore"}


def render(genome: Genome) -> str:
    """유전체 → FASTEXPR. v2/v3 유전자가 전부 기본값이면 산출물은 확장 이전과 **동일**하다
    (골든 테스트가 보증). 그래야 19k 기존 알파의 code_hash/캐시/dedup 이 살아남는다.

    조립 순서 (안쪽 → 바깥):
        combine(f1,f2[,f3]) → sign → winsorize → **regime** → group_op → decay
        → rank → **hump** → trade_when
    regime 이 group/decay 안쪽인 것이 중요하다 — 조건 밖 0 을 먼저 만들고 그 위에
    중립화·평활을 얹어야 6월 3.77 알파의 구조가 재현된다.
    """
    f1, f2, f3 = (_field_expr(f, genome.sentinel) for f in genome.fields)
    a = _transform(f1, genome.transform_a, genome.lookback_a)
    b = _transform(f2, genome.transform_b, genome.lookback_b)
    # lookback_c=0 → 기존 동작(max(20, lookback_b))을 그대로 재현하는 auto 값.
    c = _transform(f3, genome.transform_c,
                   genome.lookback_c or max(20, genome.lookback_b))
    w = WEIGHT_SCHEMES.get(genome.weight_scheme)
    if w and genome.combine == "sum":
        core = f"add({w[0]}*({a}),{w[1]}*({b}))"
    elif w and genome.combine == "spread":
        core = f"({w[0]}*({a})-{w[1]}*({b}))"
    else:
        core = _combine(a, b, c, genome.combine, genome.lookback_b)
    if genome.sign < 0:
        core = f"-1*({core})"
    if genome.winsor_std:
        core = f"winsorize({core},std={int(genome.winsor_std)})"
    if genome.regime != "OFF":
        # 레짐 밖에서는 신호 0 (중립). 조건이 참인 국면에서만 베팅한다.
        core = f"({_REGIME_CONDS[genome.regime]}?{core}:0)"
    # group_by='auto' → 기존 동작(neutralization 이 그룹을 정한다)을 그대로 재현.
    group = (GROUPS.get(genome.neutralization) if genome.group_by == "auto"
             else genome.group_by)
    if group and genome.group_op != "none" and genome.combine != "corr":
        core = f"{_GROUP_OP_FN[genome.group_op]}({core},{group})"
    if genome.decay >= 8:
        op = "ts_decay_linear" if genome.decay_style == "linear" else "ts_mean"
        core = f"{op}({core},{min(genome.decay, 30)})"
    out = f"rank({core})"
    if genome.hump > 0:
        # 신호가 문턱만큼 안 움직이면 포지션 유지 → turnover 를 직접 억제한다.
        out = f"hump({out},hump={genome.hump})"
    if genome.trade_when != "OFF":
        # 조건 밖에서는 -1 (미보유). turnover 를 직접 깎는 가장 강한 레버.
        out = f"trade_when({_TRADE_WHEN_CONDS[genome.trade_when]},{out},-1)"
    return out


def settings(genome: Genome, forced_delay=None) -> dict:
    out = {
        "universe": genome.universe,
        "neutralization": genome.neutralization,
        "decay": str(genome.decay),
        "truncation": str(genome.truncation),
        "nan_handling": genome.nan_handling,
    }
    if forced_delay is not None:
        out["delay"] = str(forced_delay)
    # 탐색 조건이 걸려 있으면 region/delay 를 강제한다 (universe·중립화는 _constrain 이
    # 유전자 단계에서 이미 가둬 놨다 — 여기서 또 덮으면 유전체와 설정이 어긋난다).
    c = _ACTIVE_CONSTRAINT
    if c is not None:
        if c.region:
            out["region"] = c.region
        if c.delay is not None:
            out["delay"] = str(c.delay)
    return out


# ── 탐색 조건 (Power Pool 테마·대회 필터 등) ─────────────────────────────────
# 워커가 라운드마다 run_config 에서 읽어 여기에 심는다. None 이면 제약 없음(기존 동작).
# **모든 유전체가 _constrain() 을 지나므로** 여기 한 곳만 잡으면 조건 밖 알파가
# 애초에 만들어지지 않는다. 사후 필터링보다 훨씬 싸다 — 시뮬 한 건이 곧 쿼터다.
_ACTIVE_CONSTRAINT = None
_CONSTRAINT_BANNED_FIELDS: frozenset = frozenset()
# 조건 region 이 USA 가 아닐 때의 지역 필드 팔레트 (D0_DATASETS 미러, 2026-07-27).
# 비면 '모름' — 강제하지 않는다(fail-open). GLB 첫 라운드 8/8 'unknown variable'
# 전멸이 신설 이유: USA 큐레이션 필드는 타 리전에 존재하지 않는 게 많다.
_REGION_DATASETS: dict = {}
# 조건 리전에 실존하는 **전체** 필드 (캡 없는 존재 검사용 — _apply_constraint 참조).
_REGION_FULL_FIELDS: frozenset | None = None


_ACCOUNT_DATASETS = None      # 이 계정이 접근 가능한 dataset.id 집합. None = 제한 없음.


def set_account_datasets(ids) -> None:
    """계정 등급이 허용하는 dataset.id 집합을 건다 (None = 제한 없음).

    ⚠ 왜 필요한가 — 팔레트는 **하우스 RC 계정**으로 긁는다. 일반 계정은 접근 가능한
    데이터셋이 훨씬 적어서(2026-07-27 실측: USA/TOP3000 기준 RC 297개 vs 일반 21개),
    RC 팔레트를 그대로 쓰면 시뮬이 'Invalid data field …' 로 죽는다.
    set_constraint 보다 **먼저** 불러야 그 안의 풀 계산에 반영된다.
    """
    global _ACCOUNT_DATASETS
    _ACCOUNT_DATASETS = frozenset(str(x) for x in ids) if ids else None


def set_constraint(spec) -> None:
    """탐색 조건을 건다. spec=None 이면 해제."""
    global _ACTIVE_CONSTRAINT, _CONSTRAINT_BANNED_FIELDS, _REGION_DATASETS, \
        _REGION_FULL_FIELDS
    if spec is not None and getattr(spec, 'is_empty', lambda: False)():
        spec = None
    _ACTIVE_CONSTRAINT = spec
    banned: set = set()
    if spec is not None and spec.excluded_datasets:
        try:
            from . import datafield_palette as _dfp
            banned = _dfp.fields_of_excluded_datasets(spec.excluded_datasets)
        except Exception:
            banned = set()
    _CONSTRAINT_BANNED_FIELDS = frozenset(banned)
    # 필드 풀을 다시 계산해야 하는 경우는 둘이다:
    #   (a) USA 밖 리전 — 리전마다 필드 집합이 다르다
    #   (b) 계정 등급 제한 — 일반 계정은 접근 가능한 데이터셋이 훨씬 적다(USA 라도)
    _region = str(getattr(spec, 'region', '') or '').upper() if spec is not None else ''
    pools: dict = {}
    full: frozenset | None = None
    if spec is not None and (_region not in ('', 'USA') or _ACCOUNT_DATASETS is not None):
        try:
            from . import datafield_palette as _dfp
            pools = _dfp.family_pools(
                delay=(spec.delay if spec.delay is not None else 1),
                region=(spec.region or 'USA'), universe=spec.universe,
                datasets=_ACCOUNT_DATASETS) or {}
            pools = {fam: tuple(names) for fam, names in pools.items()
                     if names and not all(_f in _CONSTRAINT_BANNED_FIELDS for _f in names)}
            # 존재 검사는 캡 없는 전체 집합으로 — 풀(캡·계열분류·커버리지 컷)로
            # 검사하면 실존 필드가 치환된다(2026-08-03 resvol→srisk 실측).
            full = _dfp.region_field_names(
                delay=(spec.delay if spec.delay is not None else 1),
                region=(spec.region or 'USA'), universe=spec.universe,
                datasets=_ACCOUNT_DATASETS) or None
        except Exception:
            pools = {}
            full = None
    _REGION_DATASETS = pools
    _REGION_FULL_FIELDS = (frozenset(full) - _CONSTRAINT_BANNED_FIELDS) if full else None


def region_allowed_fields() -> frozenset | None:
    """조건 리전에서 써도 되는 필드명 집합. 팔레트가 없으면 None ('모름' → 폴백)."""
    if not _REGION_DATASETS:
        return None
    return frozenset(f for fields in _REGION_DATASETS.values() for f in fields)


def active_constraint():
    return _ACTIVE_CONSTRAINT


def violates_constraint(genome) -> bool:
    """이 유전체가 현재 조건과 **구조적으로** 맞지 않는가.

    ⚠ 왜 필요한가 — `_apply_constraint` 는 금지 필드를 다른 필드로 갈아끼워 조건을
      만족시키지만, 그건 **원본의 구조를 파괴한다**. 2026-07-22 라이브 관측:
      GA 엘리트 풀이 전부 6월에 만든 pv1 알파(`_clv=((close-low)-(high-close))/…`,
      Sharpe 3.77)인데 그 주 테마가 pv1 을 금지하자, 시드마다 필드가 강제 교체되어
      Sharpe 0.2 짜리 불구 자식만 나왔다. 이런 시드는 **고치지 말고 빼야 한다**
      (조건에 맞는 다른 시드나 무작위 탐색이 훨씬 낫다).
    """
    if _ACTIVE_CONSTRAINT is None:
        return False
    d = dict(genome.__dict__) if isinstance(genome, Genome) else dict(genome or {})
    if _CONSTRAINT_BANNED_FIELDS:
        if any(_field_is_banned(f) for f in (d.get("fields") or ())):
            return True
        # 조건식·그룹 유전자도 pv1 을 하드코딩한다 — 이것까지 꺼야 하면 이미 원형이 아니다.
        if _gene_uses_banned("trade_when", d.get("trade_when")):
            return True
        if _gene_uses_banned("regime", d.get("regime")):
            return True
    return False


def constraint_banned_fields() -> frozenset:
    return _CONSTRAINT_BANNED_FIELDS


def _allowed_neutralizations() -> tuple:
    """조건이 허용하는 중립화. 조건이 없거나 우리가 모르는 값만 나열하면 전체를 쓴다."""
    c = _ACTIVE_CONSTRAINT
    if c is None or not c.neutralizations:
        return NEUTRALIZATIONS
    ok = tuple(n for n in c.neutralizations if n in NEUTRALIZATIONS)
    return ok or NEUTRALIZATIONS


# ── 감쇠 ↔ 회전율 곡선 (2026-07-21 실측) ────────────────────────────────────
# 완전히 동일한 식에서 decay 만 바꿔 잰 값. 감쇠는 회전율을 누르는 **가장 직접적인 레버**다.
#   decay  0 → 100.5%   2 → 79.6%   4 → 62.0%   6 → 53.3%   8 → 48.5%   12 → 53.6%*
#   (*12 는 다른 식에서 잰 값이라 8 보다 살짝 높다 — 단조로 보정해 쓴다)
# 상대비로만 쓰므로 절대값이 식마다 달라도 된다: turnover(d) ≈ turnover(cur)·f(d)/f(cur).
DECAY_TURNOVER_FACTOR = {0: 1.00, 2: 0.79, 4: 0.62, 6: 0.53, 8: 0.48,
                         10: 0.45, 12: 0.43, 16: 0.40, 20: 0.38, 30: 0.35}
# 목표 대역 — criteria 의 제출 컷(1~70%)보다 안쪽으로 잡는다. 70% 에 딱 붙이면
# 재시뮬에서 조금만 흔들려도 HIGH_TURNOVER FAIL 이다.
TURNOVER_TARGET = 0.60

# ── 노브 스윕 ────────────────────────────────────────────────────────────────
# 부모 유전체를 그대로 두고 **한 축만** 바꾼 변형을 만든다. 무작위 변이가 여러 축을
# 동시에 흔들어 "무엇이 효과였는지" 를 못 배우는 문제를 푼다.
# 축 선택 근거(2026-07-21 실측, 완전 동일 식):
#   중립화  SUBINDUSTRY 1.72 · SECTOR 1.40 · INDUSTRY 1.58 · MARKET 1.19 · STATISTICAL 2.47
#   감쇠    0→회전율 100.5% · 2→79.6% · 4→62.0% · 6→53.3% · 8→48.5%
#   절단    0.05·0.08·0.12·0.15 **결과 동일** → 스윕 축에서 제외(쿼터 낭비)
SWEEP_SLOTS = int(_os.environ.get("IQC_SWEEP_SLOTS", "2"))
SWEEP_NEUTRALIZATIONS = ("STATISTICAL", "CROWDING", "SLOW_AND_FAST", "MARKET",
                         "SUBINDUSTRY", "INDUSTRY")
SWEEP_DECAYS = (0, 2, 4, 6, 8, 12)


def _decay_factor(decay) -> float:
    """가장 가까운 실측 감쇠의 계수. 표 밖은 양끝 값으로 클램프."""
    try:
        d = max(0, int(decay))
    except (TypeError, ValueError):
        d = 0
    keys = sorted(DECAY_TURNOVER_FACTOR)
    if d <= keys[0]:
        return DECAY_TURNOVER_FACTOR[keys[0]]
    if d >= keys[-1]:
        return DECAY_TURNOVER_FACTOR[keys[-1]]
    return DECAY_TURNOVER_FACTOR[min(keys, key=lambda k: abs(k - d))]


def decay_for_target_turnover(cur_decay, cur_turnover, target: float = TURNOVER_TARGET):
    """회전율을 목표 대역으로 끌어내리는 **최소** 감쇠. 모르면 None.

    측정값이 있는데도 눈감고 decay=20 으로 점프하면 Sharpe 를 통째로 버린다
    (실측: 같은 식이 decay 4 에서 Sharpe 1.9, 20 에서 1.2). 필요한 만큼만 올린다.
    이미 목표 이하면 현재 감쇠를 그대로 둔다 — 회전율을 더 죽일 이유가 없다.
    """
    try:
        to = float(cur_turnover)
        cur = max(0, int(cur_decay))
    except (TypeError, ValueError):
        return None
    if to <= 0:
        return None
    if to <= target:
        return cur
    base = _decay_factor(cur)
    for d in sorted(DECAY_TURNOVER_FACTOR):
        if d < cur:
            continue
        if to * (_decay_factor(d) / base) <= target:
            return d
    return max(DECAY_TURNOVER_FACTOR)


def _template_fields(text: str) -> set:
    """조건식 템플릿이 참조하는 필드 식별자. `ts_std_dev(high-low,20)` → {high, low}."""
    import re as _re
    toks = set(_re.findall(r'[A-Za-z_][A-Za-z0-9_]*', str(text or '')))
    # 연산자 이름은 필드가 아니다 — 뒤에 '(' 가 붙은 토큰을 뺀다.
    calls = set(_re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', str(text or '')))
    return {t.lower() for t in (toks - calls)}


def _field_is_banned(field) -> bool:
    """이 필드가 금지 데이터셋을 건드리는가.

    ⚠ 이름 단순 비교로는 부족하다 — `syn_clv` 같은 합성 필드는 렌더링될 때
      `((close-low)-(high-close))/(high-low)` 로 **전개되어 pv1 필드를 끌어온다**.
      전개식의 식별자까지 봐야 한다.
    """
    banned = _CONSTRAINT_BANNED_FIELDS
    if not banned or not field:
        return False
    name = str(field).lower()
    if name in banned:
        return True
    expr = SYNTHETIC_FIELDS.get(str(field))
    return bool(expr) and bool(_template_fields(expr) & banned)


def _gene_uses_banned(gene: str, value, neutralization=None) -> bool:
    """이 유전자 값의 **렌더링 결과**가 금지 필드를 건드리는가.

    ⚠ 필드 유전자(fields)만 갈아끼우면 부족하다 — trade_when/regime 조건식과
      group_neutralize 의 그룹 인자는 pv1 필드(returns·volume·close·adv20·high·low·
      sector·industry·subindustry)를 **템플릿에 하드코딩**하고 있다.
      pv1 이 금지된 주에 이걸 안 끄면 조건 위반 알파를 계속 만들어낸다.
    """
    banned = _CONSTRAINT_BANNED_FIELDS
    if not banned or not value:
        return False
    if gene == "trade_when":
        return bool(_template_fields(_TRADE_WHEN_CONDS.get(value, "")) & banned)
    if gene == "regime":
        return bool(_template_fields(_REGIME_CONDS.get(value, "")) & banned)
    if gene == "group_by":
        grp = GROUPS.get(str(neutralization or "").upper()) if value == "auto" else value
        return bool(grp) and str(grp).lower() in banned
    return False


def _apply_constraint(d: dict) -> None:
    """유전체 dict 를 조건 안으로 밀어 넣는다 (_constrain 말미에서 호출).

    - universe 는 조건값으로 **덮어쓴다** (조건 밖 유니버스는 애초에 무의미하다).
    - neutralization 이 허용 밖이면 허용 목록에서 결정론적으로 하나 고른다.
      rng 를 쓰지 않는 이유: _constrain 은 캐시 키(_dedup_key)의 입력이라
      같은 유전체가 호출마다 다른 값이 되면 중복 판정이 무너진다.
    - 금지 데이터셋 필드는 **계열을 유지한 채** 같은 계열의 허용 필드로 갈아 끼운다.
      (전부 pv 로 되돌리면 2026-07-21 의 'D0=pv 전용' 퇴화가 재현된다.)
    """
    c = _ACTIVE_CONSTRAINT
    if c is None:
        return
    if c.universe:
        d["universe"] = c.universe
    allowed = _allowed_neutralizations()
    if d.get("neutralization") not in allowed:
        # 유전체 내용으로 안정적인 인덱스를 만든다 — 같은 유전체면 항상 같은 결과.
        key = f'{d.get("family")}|{d.get("fields")}|{d.get("decay")}'
        d["neutralization"] = allowed[hash(key) % len(allowed)]
    # 지역 팔레트 강제 (2026-07-27) — 조건 리전에 실존하는 필드로 전면 교체.
    # USA 필드를 GLB 에 보내면 'unknown variable' 로 시뮬이 통째로 죽는다.
    allowed_region = region_allowed_fields()
    # 존재 판정은 캡 없는 전체 집합 우선 — 풀 부분집합으로 판정하면 실존 필드가
    # '리전에 없음' 오판으로 몰래 치환된다(2026-08-03 전략스펙 resvol→srisk 실측).
    exists = _REGION_FULL_FIELDS if _REGION_FULL_FIELDS else allowed_region
    if allowed_region is not None:
        flds = list(d.get("fields") or ())
        if not all(f in exists for f in flds):
            fam = str(d.get("family") or "pv")
            pool = list(_REGION_DATASETS.get(fam) or ())
            pool = [f for f in pool if not _field_is_banned(f)]
            if len(pool) < 3:
                # 이 계열이 이 리전에 없다 — 유전체 내용 기반 결정론으로 대체 계열 선택
                # (rng 금지: _constrain 은 dedup 키 입력이라 호출마다 같아야 한다).
                fams = sorted(f for f, p in _REGION_DATASETS.items()
                              if len([x for x in p if not _field_is_banned(x)]) >= 3)
                if fams:
                    key = f'{d.get("family")}|{d.get("fields")}'
                    fam = fams[hash(key) % len(fams)]
                    pool = [f for f in _REGION_DATASETS[fam]
                            if not _field_is_banned(f)]
                    d["family"] = fam
            if len(pool) >= 3:
                key = f'{d.get("fields")}|{d.get("decay")}|{d.get("transform_a")}'
                base = hash(key) % len(pool)
                out = []
                for j, f in enumerate(flds):
                    if f in exists and f not in out:
                        out.append(f)
                    else:
                        i = base + j
                        while pool[i % len(pool)] in out:
                            i += 1
                        out.append(pool[i % len(pool)])
                d["fields"] = tuple(out[:3])
    banned = _CONSTRAINT_BANNED_FIELDS
    if banned:
        flds = list(d.get("fields") or ())
        if any(_field_is_banned(f) for f in flds):
            fam = str(d.get("family") or "pv")
            pool = [f for f in (SHARED_DATASETS.get(fam) or ())
                    if not _field_is_banned(f)]
            if len(pool) < 3:
                # 이 계열이 통째로 금지됐다 — 허용 필드가 남은 다른 계열로 옮긴다.
                for alt, fields in SHARED_DATASETS.items():
                    alt_pool = [f for f in fields if not _field_is_banned(f)]
                    if len(alt_pool) >= 3:
                        fam, pool = alt, alt_pool
                        d["family"] = alt
                        break
            if len(pool) >= 3:
                out, i = [], 0
                for f in flds:
                    if _field_is_banned(f):
                        while i < len(pool) and pool[i] in out:
                            i += 1
                        out.append(pool[i] if i < len(pool) else pool[0])
                    else:
                        out.append(f)
                d["fields"] = tuple(out[:3])
        # 조건식·그룹 유전자가 금지 필드를 쓰면 그 유전자를 끈다.
        if _gene_uses_banned("trade_when", d.get("trade_when")):
            d["trade_when"] = "OFF"
        if _gene_uses_banned("regime", d.get("regime")):
            d["regime"] = "OFF"
        if _gene_uses_banned("group_by", d.get("group_by"), d.get("neutralization")):
            # 명시 그룹이 금지면 auto 로, auto 마저 금지(=중립화가 industry 계열)면
            # 그룹 연산 자체를 끈다. 중립화는 설정으로 이미 걸리므로 신호는 안 죽는다.
            if d.get("group_by") != "auto":
                d["group_by"] = "auto"
            if _gene_uses_banned("group_by", "auto", d.get("neutralization")):
                d["group_op"] = "none"


def _error_tokens(errors: Iterable[dict] | None) -> set[str]:
    toks: set[str] = set()
    for e in errors or []:
        for ident in e.get("identifiers") or []:
            toks.add(str(ident))
        pat = str(e.get("pattern") or "")
        for fam in SHARED_DATASETS.values():
            for f in fam:
                if f in pat:
                    toks.add(f)
    return toks


def _pick_fields(rng: random.Random, family: str, forbidden: set[str], delay) -> tuple[str, str, str]:
    if str(delay) == "0":
        # D0 팔레트가 있으면 그 안에서 family 를 존중한다(없으면 옛 동작 = pv 전용).
        pool = list((D0_DATASETS.get(family) or ()) if D0_DATASETS else ())
        if len(pool) < 3:
            pool = list(SHARED_DATASETS["pv"])
    else:
        pool = list(SHARED_DATASETS.get(family) or SHARED_DATASETS["pv"])
        if len(pool) < 3:
            pool += list(SHARED_DATASETS["pv"])
    pool = [f for f in pool if f not in forbidden]
    # 조건이 금지한 데이터셋의 필드는 애초에 뽑지 않는다.
    banned = _CONSTRAINT_BANNED_FIELDS
    if banned:
        filtered = [f for f in pool if not _field_is_banned(f)]
        # 이 계열이 통째로 금지되면 폴백을 pv 로 두면 안 된다(pv1 이 금지 대상인 게 보통).
        if len(filtered) >= 3:
            pool = filtered
        else:
            pool = [f for fam in SHARED_DATASETS.values() for f in fam
                    if not _field_is_banned(f) and f not in forbidden]
    if len(pool) < 3:
        fallback = [f for f in SHARED_DATASETS["pv"] if not _field_is_banned(f)]
        pool = fallback if len(fallback) >= 3 else list(SHARED_DATASETS["pv"])
    rng.shuffle(pool)
    return (pool[0], pool[1], pool[2])


# ── directed mutation: fail 사유 → 어느 유전자 축을 움직일지 ─────────────────
# 파싱·매핑의 단일 진실은 mutation_learn (categorize + RULE_DIRECTIVE) — 이 함수는
# 규칙 기반 폴백 경로(directive_stats 미제공)용 어댑터로만 남는다.

def _directives(fail_items: Iterable[str], metrics: dict | None = None) -> list[str]:
    return [_mutation_learn.RULE_DIRECTIVE[c]
            for c in _mutation_learn.categorize(fail_items, metrics)]


class BaseGenomeModel:
    name = "base"
    # ts_av_diff = x - ts_mean(x,d) — 2026-07-21 추가. 고회전 연구의 기본 문법
    # ("Favor changes over levels: deltas, surprises, accelerations").
    transforms = ("rank", "ts_rank", "ts_zscore", "ts_delta", "ts_mean", "ts_av_diff")
    # 'resid' = vector_neut(a,b) 잔차 결합 (2026-07-23) — 탈상관 레버. _combine 참조.
    combines = ("spread", "sum", "product", "ratio", "corr", "triple", "resid")
    # settings 유전자 전집합 — bandit.DIMENSIONS 가 여기서 읽는다(단일 진실 소스).
    # ⚠ 하드코딩 금지: 2026-07-23 에 bandit 이 자체 5종 목록을 들고 있던 탓에
    #   STATISTICAL(전 arm 평균보상 1위)이 슬롯으로 선택 불가능한 팔이었다.
    universes = ("TOP3000", "TOP1000", "TOP500", "TOP200")
    neutralizations = NEUTRALIZATIONS
    # 'model' = WQB 의 mdl77/mdl177 팩터 데이터셋(가치·퀄리티·모멘텀이 이미 계산돼 있다).
    # CSV 에 3000개가 넘게 있는데 GA 는 2026-07-14 까지 한 번도 본 적이 없다.
    # 2026-07-22 확장 — shortinterest·imbalance 는 그날 최고 알파(단독 Sharpe 2.28·1.84)를
    # 낸 계열인데 classify_family 가 이름 접두사로 추측하느라 **통째로 안 보였다**.
    # 이제 필드→데이터셋→카테고리 실매핑으로 분류하므로 GA 에 처음 열린다.
    families = ("pv", "fundamental", "analyst", "option", "news", "model",
                "shortinterest", "imbalance")
    decays = (0, 2, 4, 6, 8, 12, 20)
    truncations = (0.08, 0.1, 0.12)

    def __init__(self, *, round_num: int, forced_delay=None, errors=None, feedback=None,
                 parent_genome=None, fail_items=None, seed_genomes=None,
                 slot_settings=None, salt: int = 0,
                 parent_alpha_id=None, seed_alpha_ids=None, directive_stats=None,
                 spec_genomes=None, spec_ids=None, parent_metrics=None,
                 search_mode: str = 'legacy'):
        self.round_num = int(round_num or 0)
        self.forced_delay = forced_delay
        self.feedback = feedback or []
        self.forbidden = _error_tokens(errors)
        self.parent = _coerce_genome(parent_genome)
        self.fail_items = [str(x) for x in (fail_items or []) if str(x).strip()]
        # 부모의 실측 지표 — 고회전(HTVR) 관문 미달은 FAIL 이 아니라 WARNING 이라
        # fail_items 에 안 나타난다. 정향변이가 그걸 보려면 지표가 필요하다.
        self.parent_metrics = dict(parent_metrics or {})
        # 시드 유전체 + (선택) 그 출처 알파의 DB id — 자식의 parent_alpha_id 귀속용.
        # seed_alpha_ids 는 seed_genomes 와 같은 인덱스로 정렬된 리스트여야 한다.
        _sids = list(seed_alpha_ids or [])
        self.seeds: list[Genome] = []
        self._alpha_id_by_genome: dict[Genome, int] = {}
        for _i, _s in enumerate(seed_genomes or []):
            _g = _coerce_genome(_s)
            if _g is None:
                continue
            self.seeds.append(_g)
            if _i < len(_sids) and _sids[_i] is not None:
                self._alpha_id_by_genome[_g] = int(_sids[_i])
        if self.parent is not None and parent_alpha_id is not None:
            self._alpha_id_by_genome[self.parent] = int(parent_alpha_id)
        self.slot_settings = list(slot_settings or [])
        self.salt = int(salt or 0)
        # (category, directive) → {'n','wins'} 관측 행렬. None 이면 규칙 기반 선택,
        # dict(빈 것 포함)이면 Thompson sampling 학습 선택.
        self.directive_stats = directive_stats
        self.search_mode = str(search_mode or 'legacy')
        self._last_directive: str | None = None
        # LLM 전략스펙 — **변이 없이 그대로** 시뮬되는 초기 개체(1회성).
        # 사용자가 요청한 아이디어를 GA 가 손대기 전에 원본 그대로 한 번 측정해야
        # '그 아이디어가 먹혔는지' 를 말할 수 있다. 다음 라운드부터는 이 알파들이
        # elite_seeds 를 통해 평범한 GA 재료가 된다.
        self.spec_genomes: list[Genome] = []
        self.spec_ids: list[int | None] = []
        _spids = list(spec_ids or [])
        for _i, _s in enumerate(spec_genomes or []):
            _g = _coerce_genome(_s)
            if _g is None:
                continue
            self.spec_genomes.append(_g)
            self.spec_ids.append(_spids[_i] if _i < len(_spids) else None)

    def _rng(self, nonce: int) -> random.Random:
        seed = ((self.round_num + 1) * 1009 + nonce * 9173
                + self.salt * 7919 + sum(ord(c) for c in self.name))
        return random.Random(seed)

    # ── population plan ──────────────────────────────────────
    def _plan(self, n: int) -> list[tuple[str, Genome | None, Genome | None]]:
        # 전략스펙이 있으면 그것이 최우선 — 사용자가 명시적으로 요청한 아이디어다.
        # 스펙이 n 개를 못 채우면 나머지는 평소 계획(시드 교차/변이 + 무작위)으로 채운다.
        if self.spec_genomes:
            plan: list[tuple[str, Genome | None, Genome | None]] = [
                ("spec", g, None) for g in self.spec_genomes[:n]]
            if len(plan) < n:
                plan.extend(self._plan_ga(n - len(plan)))
            return plan
        return self._plan_ga(n)

    def _plan_ga(self, n: int) -> list[tuple[str, Genome | None, Genome | None]]:
        if self.parent is not None:
            if self.search_mode == 'exploit':
                plan = [("sweep", self.parent, None)] * min(SWEEP_SLOTS, n)
                while len(plan) < n:
                    plan.append(("local", self.parent, None))
                # 소수 교차 슬롯은 국소 basin 에 새 유전자를 공급한다.
                for j in range(min(2, len(self.seeds), max(0, n - 2))):
                    plan[n - 1 - j] = ("crossover", self.parent, self.seeds[j])
                return plan
            if self.search_mode == 'escape':
                # A focus parent on a correlation wall must stop donating its
                # signal to the remainder of the batch.  Live 2.0 evidence
                # showed the former crossover tail reproducing the parent's
                # risk70 lineage and failing PROD correlation around 0.84.
                # Keep most slots as explicit structural jumps and spend the
                # rest on independent genomes; neither path preserves the
                # blocked parent expression.
                plan = [("escape", self.parent, None)] * max(1, math.ceil(n * 0.75))
                while len(plan) < n:
                    plan.append(("random", None, None))
                return plan[:n]
            plan: list[tuple[str, Genome | None, Genome | None]] = [
                ("mutate", self.parent, None)] * n
            # ── 노브 스윕 슬롯 ──────────────────────────────────────────────
            # 2026-07-21 발굴이 통한 진짜 이유는 무작위 변이가 아니라 **엘리트의
            # 노브를 체계적으로 훑은 것**이었다. 완전히 같은 식에서 중립화만 바꿔
            # Sharpe 1.72 → 2.47, decay 만 바꿔 회전율 100% → 48% 를 얻었다.
            # 무작위 변이는 이 두 축을 동시에 흔들어 인과를 흐린다.
            # 앞 슬롯을 스윕에 배정한다 — 부모가 좋을수록 스윕의 기대값이 크다.
            for j in range(min(SWEEP_SLOTS, max(0, n - 2))):
                plan[j] = ("sweep", self.parent, None)
            # 마지막 2 슬롯은 부모×엘리트 교차 — 새 유전자 유입 통로.
            for j in range(min(2, len(self.seeds), max(0, n - 2))):
                plan[n - 1 - j] = ("crossover", self.parent, self.seeds[j])
            return plan
        plan = []
        if self.seeds:
            if self.search_mode == 'exploit':
                # 리서치 결과: 유망 부모 주변은 정확히 1~2축을 바꾼 국소 변이가
                # 다축보다 덜 무너졌다. 70%는 local/sweep, 나머지만 새 구조에 쓴다.
                for _ in range(min(SWEEP_SLOTS, n)):
                    plan.append(("sweep", self.seeds[0], None))
                local_n = max(0, math.ceil(n * 0.70) - len(plan))
                for j in range(local_n):
                    plan.append(("local", self.seeds[j % len(self.seeds)], None))
                while len(plan) < n:
                    plan.append(("random", None, None))
                return plan
            if self.search_mode == 'escape':
                # 정체·PROD 상관벽에서는 필드/데이터 계열/결합을 함께 움직여
                # 현재 basin 을 벗어난다. 설정만 바꾸는 탈상관은 여기서 만들지 않는다.
                # A correlation-wall round must mostly leave the existing
                # basin.  The earlier 35% share still let elite crossovers
                # dominate; live 2.0 evidence produced strong IS results that
                # repeated the same high PROD correlation.  Match the parent
                # escape path and move 60% structurally.
                escape_n = max(1, math.ceil(n * 0.60))
                for j in range(escape_n):
                    plan.append(("escape", self.seeds[j % len(self.seeds)], None))
                cross_n = max(1, math.floor(n * 0.20)) if len(self.seeds) > 1 else 0
                for j in range(cross_n):
                    a = self.seeds[j % len(self.seeds)]
                    b = self.seeds[(j + 1) % len(self.seeds)]
                    plan.append(("crossover", a, b))
                while len(plan) < n:
                    plan.append(("random", None, None))
                return plan[:n]
            # ⚠ 2026-07-22 — 스윕을 **탐색 라운드에도** 넣는다. 처음엔 부모가 있는
            #   포커스 라운드에만 달았는데, 라이브에서 실제로 도는 건 대부분 이 시드
            #   경로라 스윕이 한 번도 발현되지 않았다(재시작 후 7시간 관측).
            #   최고 엘리트(seeds[0])의 중립화·감쇠를 한 축씩 훑는다 — 7/21 에
            #   Sharpe 1.72 → 2.47 을 만든 바로 그 절차다.
            for _ in range(min(SWEEP_SLOTS, max(0, n - 2))):
                plan.append(("sweep", self.seeds[0], None))
            k = min(max(0, n // 2 - len(plan)), max(2, len(self.seeds)))
            for j in range(k):
                a = self.seeds[j % len(self.seeds)]
                b = self.seeds[(j + 1) % len(self.seeds)]
                if len(self.seeds) >= 2 and j % 2 == 0:
                    plan.append(("crossover", a, b))
                else:
                    plan.append(("mutate", a, None))
        while len(plan) < n:
            plan.append(("random", None, None))
        return plan

    def generate(self, n: int = 8) -> list[dict]:
        plan = self._plan(n)
        # 밴딧 arm 은 무작위 탐색 슬롯에만 순서대로 배정 (GA 자식은 settings 를 유전).
        arms_by_slot: dict[int, dict] = {}
        ai = 0
        for si, (op, _, _) in enumerate(plan):
            if op == "random" and ai < len(self.slot_settings):
                arms_by_slot[si] = self.slot_settings[ai]
                ai += 1

        out: list[dict] = []
        seen: set[str] = set()
        if self.parent is not None:
            # 부모 그대로(무변이) 재출현 방지 — 이미 시뮬한 조합.
            seen.add(self._dedup_key(self.parent))
        i = 0
        while len(out) < n and i < n * 10:
            i += 1
            rng = self._rng(i)
            slot = len(out)
            op, a, b = plan[slot]
            directive = None
            base = None            # 유전자 diff 의 기준 부모 (mutate/xo 의 주부모)
            spec_id = None
            if op == "spec":
                # LLM 이 설계한 유전체 그대로 — 변이 금지. 원본의 성적을 먼저 잰다.
                g = a
                try:
                    spec_id = self.spec_ids[self.spec_genomes.index(a)]
                except (ValueError, IndexError):
                    spec_id = None
            elif op == "mutate":
                # 정향변이의 유전자 공간이 작아 중복이 계속되면(시도 예산 절반 소진)
                # 지시 없는 일반 변이로 강등해 슬롯을 채운다 — 세대가 8개 미만이면
                # 시뮬 예산이 놀게 되기 때문.
                g = self._mutate(a, rng, directed=(i <= n * 5))
                directive = self._last_directive
                base = a
            elif op == "local":
                g = self._local_mutate(a, rng)
                directive = "local_1_2_axis"
                base = a
            elif op == "escape":
                g = self._escape_mutate(a, rng)
                directive = "structural_escape"
                base = a
            elif op == "sweep":
                # 한 축만 바꾼다 — 슬롯 순서로 축·값을 정해 **결정론적**으로 훑는다.
                # (같은 라운드에서 같은 축의 같은 값이 두 번 나오면 dedup 이 걸러낸다.)
                g = self._sweep(a, slot, i)
                base = a
                directive = "sweep"
            elif op == "crossover":
                g = self._crossover(a, b, rng)
                base = a
            else:
                g = self._genome(i, rng)
                arm = arms_by_slot.get(slot)
                if arm:
                    g = self._apply_arm(g, arm, rng)
            g = self._constrain(g, rng)
            key = self._dedup_key(g)
            if key in seen:
                continue
            seen.add(key)
            # 귀속 기록 — 어떤 부모의 어떤 유전자를 바꿨는지. _constrain 이후의
            # 최종 유전체 기준이라 실제 시뮬되는 조합과 정확히 일치한다.
            genes_changed = None
            if base is not None:
                genes_changed = [k for k in _GENE_NAMES
                                 if k not in ("model", "generation")
                                 and getattr(g, k) != getattr(base, k)]
            out.append({
                "idx": len(out) + 1,
                "code": render(g),
                "desc": self._desc(g, op),
                "settings": settings(g, self.forced_delay),
                "genome": dict(g.__dict__),
                "generation": g.generation,
                "origin": op,
                "directive": directive,
                "parent_alpha_id": (self._alpha_id_by_genome.get(base)
                                    if base is not None else None),
                "genes_changed": genes_changed,
                "spec_id": spec_id,
            })
        return out

    @staticmethod
    def _dedup_key(g: Genome) -> str:
        # 코드가 같아도 settings 유전자가 다르면 다른 후보다 (settings 스윕 자식 보존).
        # decay_style 은 render() 산출물에 이미 드러나므로 따로 넣지 않는다.
        # ⚠ 절단은 키에 남겨 둔다 — 골든 계약(test_genome_v2)이 이 형식을 고정한다.
        #   중복 시뮬 위험은 _genome() 에서 절단을 탐색 축에서 내려 이미 사라졌다.
        return (f"{render(g)}|{g.universe}|{g.neutralization}|{g.decay}"
                f"|{g.truncation}|{g.nan_handling}")

    # ── GA operators ─────────────────────────────────────────
    def _local_mutate(self, parent: Genome, rng: random.Random) -> Genome:
        """Change exactly one or two interpretable axes around a strong parent."""
        d = dict(parent.__dict__)
        axes = ["neutralization", "decay", "universe", "lookback_a",
                "lookback_b", "trade_when", "regime"]
        for axis in rng.sample(axes, k=1 if rng.random() < 0.65 else 2):
            if axis == "neutralization":
                vals = [x for x in _allowed_neutralizations()
                        if x != d.get("neutralization")]
                if vals:
                    d[axis] = rng.choice(vals)
            elif axis == "decay":
                vals = [x for x in self.decays if x != int(d.get("decay") or 0)]
                d[axis] = rng.choice(vals)
            elif axis == "universe":
                vals = [x for x in self.universes if x != d.get("universe")]
                d[axis] = rng.choice(vals)
            elif axis in ("lookback_a", "lookback_b"):
                vals = [x for x in CANONICAL_LOOKBACKS if x != int(d.get(axis) or 0)]
                d[axis] = rng.choice(vals)
            elif axis == "trade_when":
                vals = [x for x in TRADE_WHEN_KINDS if x != d.get(axis)]
                d[axis] = rng.choice(vals)
            elif axis == "regime":
                vals = [x for x in REGIME_KINDS if x != d.get(axis)]
                d[axis] = rng.choice(vals)
        d["model"] = self.name
        d["generation"] = int(parent.generation or 0) + 1
        return Genome(**d)

    def _escape_mutate(self, parent: Genome, rng: random.Random) -> Genome:
        """Structural jump for a plateau or correlation wall (at least three axes)."""
        d = dict(parent.__dict__)
        families = [f for f in self.families if f != d.get("family")]
        family = rng.choice(families or list(self.families))
        d["family"] = family
        d["fields"] = _pick_fields(rng, family, self.forbidden, self.forced_delay)
        d["combine"] = rng.choice([x for x in self.combines if x != d.get("combine")]
                                  or list(self.combines))
        d["transform_a"] = rng.choice(self.transforms)
        if rng.random() < 0.65:
            d["transform_b"] = rng.choice(self.transforms)
        preferred = list(FAMILY_NEUTRALIZATION.get(family, ()))
        if preferred:
            d["neutralization"] = rng.choice(preferred)
        d["lookback_a"] = rng.choice(CANONICAL_LOOKBACKS)
        d["model"] = self.name
        d["generation"] = int(parent.generation or 0) + 1
        return Genome(**d)

    def _mutate(self, parent: Genome, rng: random.Random, directed: bool = True) -> Genome:
        d = dict(parent.__dict__)
        directive = None
        if directed:
            if self.directive_stats is not None:
                # 온라인 학습 경로 — 누적 (fail category × directive) 성공률로
                # Thompson sampling. 관측이 없으면 사전확률 = 기존 규칙 우세.
                directive = _mutation_learn.choose_directive(
                    self.fail_items, self.directive_stats, rng,
                    metrics=self.parent_metrics)
            else:
                dirs = _directives(self.fail_items, self.parent_metrics)
                directive = rng.choice(dirs) if dirs else None
        # generate() 가 후보 dict 에 귀속 기록으로 실어 보낸다.
        self._last_directive = directive
        if directive == "smooth":       # turnover 과다 → 스무딩 유전자 강화
            # 부모 회전율을 알면 **필요한 만큼만** 감쇠를 올린다. 눈감고 20 으로
            # 점프하면 Sharpe 를 통째로 버린다(실측: decay 4→20 이면 Sharpe 1.9→1.2).
            _tgt = decay_for_target_turnover(int(d["decay"]),
                                             self.parent_metrics.get("turnover"))
            d["decay"] = (_tgt if _tgt is not None
                          else max(int(d["decay"]), rng.choice((8, 12, 20))))
            # 표준 창 사다리를 1~2칸 올린다 (옛 ×2/×3 은 80·240 같은 비표준 창을 만들었다).
            for _ in range(rng.choice((1, 2))):
                d["lookback_a"] = _next_longer(int(d["lookback_a"]))
                d["lookback_b"] = _next_longer(int(d["lookback_b"]))
            d["decay_style"] = rng.choice(DECAY_STYLES)
            if rng.random() < 0.5 and "ts_mean" in self.transforms:
                d["transform_b"] = "ts_mean"
            # 조건부 진입 — 조건 밖에서 아예 미보유하므로 turnover 를 직접 깎는다.
            if rng.random() < 0.5:
                d["trade_when"] = rng.choice(("vol_calm", "trend_up", "liquid"))
        elif directive == "sharpen":    # turnover 과소 → 신호 민감도 강화
            d["decay"] = rng.choice((0, 2))
            d["lookback_a"] = rng.choice((5, 10, 20))
            d["trade_when"] = "OFF"     # 진입 조건이 turnover 를 죽이고 있었을 수 있다
            if rng.random() < 0.5 and "ts_delta" in self.transforms:
                d["transform_a"] = "ts_delta"
        elif directive == "concentration":  # weight 집중 → 분산 유전자
            d["neutralization"] = rng.choice(("SUBINDUSTRY", "INDUSTRY"))
            d["universe"] = "TOP3000"
            d["truncation"] = 0.08
            # 이상치 절단 — 소수 종목이 비중을 독식하는 걸 신호 단계에서 막는다.
            d["winsor_std"] = rng.choice((3, 4))
        elif directive == "universe":   # sub-universe sharpe → 큰 유니버스
            d["universe"] = "TOP3000"
            if d["neutralization"] in ("NONE", "MARKET"):
                d["neutralization"] = rng.choice(("SECTOR", "INDUSTRY"))
        elif directive == "decorrelate":  # self-corr → 다른 패밀리/조합으로 탈상관
            fam_pool = [f for f in self.families if f != d.get("family")] or list(self.families)
            fam = rng.choice(fam_pool)
            d["family"] = fam
            d["fields"] = _pick_fields(rng, fam, self.forbidden, self.forced_delay)
            d["combine"] = rng.choice(self.combines)
            # 그룹 기준·가중도 값싼 탈상관 레버 — 같은 신호도 다른 알파가 된다.
            d["group_by"] = rng.choice(GROUP_BYS)
            d["weight_scheme"] = rng.choice(tuple(WEIGHT_SCHEMES))
            # 🔑 decay 를 상관 축으로 올린다 (2026-08-18 사장 지시).
            #    08-17~18 에 **상관을 실제로 움직인 유일한 설정**이 이것이다:
            #      pv47 단독      decay  6 → 12   상관 0.78 → 0.74
            #      2재료 합성     decay 12 → 24   상관 0.75 → 통과 (첫 제출)
            #      srisk 조립     decay 24 → 36   상관 0.73 → 통과 (둘째 제출)
            #    회전이 내려가면서 겹치는 구간이 줄어드는 것으로 보인다. 부모보다
            #    **올리는 쪽으로만** 간다 — 내리면 상관이 도로 올라간 게 실측이다.
            d["decay"] = min(60, rng.choice(DECORR_DECAYS
                                            + (int(d.get("decay") or 0) + 12,)))
        elif directive == "boost":      # ── Fitness 미달 → returns 를 올린다 ──
            # Fitness = Sharpe·sqrt(|Returns|/max(Turnover,0.125)). 라이브 turnover 는
            # 이미 ~3% 로 바닥(0.125) 한참 아래라 **더 낮춰도 Fitness 는 안 오른다**.
            # 유일한 레버는 returns — 신호를 희석하는 것들을 걷어낸다.
            d["decay"] = rng.choice((0, 2, 4))          # 평활이 알파를 깎고 있었다
            d["truncation"] = rng.choice((0.1, 0.12, 0.15))   # 확신에 더 크게 베팅
            d["universe"] = rng.choice(("TOP500", "TOP200", "TOP1000"))  # 분산 큰 유니버스
            if rng.random() < 0.5:
                # 그룹 중립화는 리스크를 줄이지만 수익도 함께 깎는다 — 완화해 본다.
                d["group_op"] = rng.choice(("rank", "zscore", "none"))
            if rng.random() < 0.5:
                d["trade_when"] = "OFF"   # 미보유 구간은 수익을 못 낸다
            if rng.random() < 0.4:
                d["hump"] = 0.0           # hump 도 리밸런싱을 막아 수익을 깎을 수 있다
            if rng.random() < 0.4:
                d["winsor_std"] = 0       # 절단이 수익 꼬리를 자르고 있었을 수 있다
        elif directive == "churn":      # ── 고회전(HTVR) 분류 문턱(20%)을 넘긴다 ──
            # 2026-07-21 신설. 제출 규칙 개편으로 회전율 20% 가 사실상 제출의 1차 관문이
            # 됐다(criteria.py). 다만 BRAIN 문서가 경고하듯 **회전율 자체를 목표로 삼으면
            # 안 된다** — "High turnover should be a consequence of the idea, not the idea
            # itself", "Artificial turnover: turnover rises because the alpha is noisy".
            # 그래서 노이즈를 주입하는 게 아니라 **신호의 지평을 짧게** 만든다:
            # 평활 제거 · 짧은 창 · 변화량(레벨이 아니라 델타) · 회전 억제 유전자 해제.
            d["decay"] = 0                       # 평활은 회전율을 직접 죽인다
            d["hump"] = 0.0                      # 리밸런싱 억제 해제
            d["trade_when"] = "OFF"              # 미보유 구간 = 회전 0
            # 창 길이는 계열마다 다르게 잡는다. 옵션(IV) 계열은 상수만기 보간·평균이
            # 이미 걸려 있어 짧은 창이 노이즈만 재는 데이터다 — Option6 문서 명시:
            # "Asking 'what changed in the last 5 days?' mostly measures noise …
            #  Prefer ts_delta, ts_av_diff, or ts_zscore over windows on the order of
            #  a quarter." 회전율은 창이 아니라 평활·hump 제거로 올린다.
            if (d.get("family") or "pv") == "option":
                d["lookback_a"] = rng.choice((20, 60))
                d["lookback_b"] = rng.choice((60, 120))
            else:
                d["lookback_a"] = rng.choice((2, 3, 5, 10))
                d["lookback_b"] = rng.choice((5, 10, 20))
            # "Favor changes over levels: deltas, surprises, accelerations" (HT 문서)
            _fast = [t for t in ("ts_delta", "ts_av_diff", "ts_zscore") if t in self.transforms]
            if _fast:
                d["transform_a"] = rng.choice(_fast)
                if rng.random() < 0.5:
                    d["transform_b"] = rng.choice(_fast)
            if rng.random() < 0.35:
                # 리스크 팩터 중립화(특히 FAST 계열)는 회전율을 **올린다** — 문서 명시:
                # "the turnover of the output alpha is likely to increase … more so when
                # neutralizing to the Fast Factors". RAM 은 Orthogonal 하위분류도 얻는다.
                d["neutralization"] = rng.choice(
                    ("REVERSION_AND_MOMENTUM", "FAST", "SLOW_AND_FAST"))
            elif rng.random() < 0.4:
                # pv 계열엔 industry/subindustry 중립화가 성과를 깎는다(문서 권고).
                d["neutralization"] = rng.choice(FAMILY_NEUTRALIZATION.get(
                    d.get("family") or "pv", ("MARKET", "SECTOR")))
        elif directive == "region_balance":  # ── GLB 지역 최저 Sharpe 보강 ──
            # 필드·변환·결합은 그대로 둔다. 이미 살아 있는 전역 신호를 갈아엎지 않고,
            # 지역별 산업 구성과 이상치 민감도를 좌우하는 리스크 처리 축만 바꾼다.
            region_values = {
                key: _mutation_learn._metric(self.parent_metrics, key)
                for key in ("glb_amer_sharpe", "glb_emea_sharpe", "glb_apac_sharpe")
            }
            weak_region = min(
                ((value, key) for key, value in region_values.items()
                 if value is not None), default=(0.0, ""))[1]
            d["group_op"] = "neutralize"
            d["group_by"] = rng.choice(
                ("country", "sector", "industry")
                if weak_region else ("sector", "industry", "subindustry"))
            d["neutralization"] = rng.choice(
                ("SECTOR", "INDUSTRY", "SUBINDUSTRY", "MARKET"))
            d["truncation"] = rng.choice((0.05, 0.08, 0.1))
            d["winsor_std"] = rng.choice((3, 4))
        elif directive == "robustify":  # ── 2Y Sharpe / ladder → 시간 안정성 ──
            # 특정 국면·이상치에 얹힌 신호를 일반화한다: 창을 늘리고, 이상치를 자르고,
            # 노출을 중립화한다.
            #
            # ⚠ **평활(decay↑)은 일부러 넣지 않는다.** 처음엔 넣었다가 뺐다 —
            #   (1) 'smooth' 축의 라이브 승률이 signal·fitness 양쪽 0/118 (0%) 이고,
            #   (2) 실측(2026-07-14 재시작 후)에서 이 계정의 2Y Sharpe(1.16~1.30)는
            #       전 구간 Sharpe(0.92~1.01)보다 **오히려 높다**. 즉 LOW_2Y_SHARPE 는
            #       '최근이 무너져서' 가 아니라 컷(2.69)이 높아서 떨어지는 것이고,
            #       결국 신호를 키워야 뚫린다. 평활은 returns 를 깎아 반대로 간다.
            d["lookback_a"] = min(252, max(int(d["lookback_a"]), rng.choice((60, 120, 252))))
            d["lookback_b"] = min(252, max(int(d["lookback_b"]), rng.choice((60, 120, 252))))
            d["winsor_std"] = rng.choice((3, 4))
            d["neutralization"] = rng.choice(("INDUSTRY", "SECTOR", "SUBINDUSTRY"))
            d["group_op"] = "neutralize"
            d["regime"] = "OFF"                # 특정 국면에만 걸린 신호였을 수 있다
            if rng.random() < 0.5:
                # 표본을 넓혀 국면 의존을 줄인다 — 다만 항상 TOP3000 으로 못박으면
                # boost 가 찾아낸 '작은 유니버스 = 높은 returns' 를 매번 되돌린다.
                d["universe"] = "TOP3000"
        else:                           # signal 미달 or 사유 불명 → 신호 유전자 무작위 변이
            for gene in rng.sample(
                    ("fields", "transform_a", "transform_b", "transform_c", "combine",
                     "sign", "lookback_a", "lookback_b", "lookback_c", "decay_style",
                     "trade_when", "group_op", "group_by", "winsor_std",
                     "weight_scheme", "regime", "hump", "sentinel"),
                    k=rng.choice((1, 2))):
                if gene == "decay_style":
                    d["decay_style"] = rng.choice(DECAY_STYLES)
                elif gene == "fields":
                    d["fields"] = _pick_fields(rng, d.get("family") or "pv",
                                               self.forbidden, self.forced_delay)
                elif gene == "transform_a":
                    d["transform_a"] = rng.choice(self.transforms)
                elif gene == "transform_b":
                    d["transform_b"] = rng.choice(self.transforms)
                elif gene == "transform_c":
                    d["transform_c"] = rng.choice(self.transforms)
                elif gene == "combine":
                    d["combine"] = rng.choice(self.combines)
                elif gene == "sign":
                    d["sign"] = -d["sign"]
                elif gene == "lookback_a":
                    d["lookback_a"] = rng.choice(CANONICAL_LOOKBACKS[:-1])
                elif gene == "lookback_c":
                    d["lookback_c"] = rng.choice((0, 10, 20, 40, 60, 120))
                elif gene == "trade_when":
                    d["trade_when"] = rng.choice(TRADE_WHEN_KINDS)
                elif gene == "group_op":
                    d["group_op"] = rng.choice(GROUP_OPS)
                elif gene == "group_by":
                    d["group_by"] = rng.choice(GROUP_BYS)
                elif gene == "winsor_std":
                    d["winsor_std"] = rng.choice(WINSOR_STDS)
                elif gene == "weight_scheme":
                    d["weight_scheme"] = rng.choice(tuple(WEIGHT_SCHEMES))
                elif gene == "regime":
                    d["regime"] = rng.choice(REGIME_KINDS)
                elif gene == "hump":
                    d["hump"] = rng.choice(HUMPS)
                elif gene == "sentinel":
                    d["sentinel"] = rng.choice(SENTINELS)
                else:
                    d["lookback_b"] = rng.choice(CANONICAL_LOOKBACKS[2:])
        d["model"] = self.name
        d["generation"] = int(parent.generation or 0) + 1
        return Genome(**d)

    def _crossover(self, a: Genome, b: Genome, rng: random.Random) -> Genome:
        d = {}
        for gene in _GENE_NAMES:
            d[gene] = getattr(a if rng.random() < 0.5 else b, gene)
        fa, fb = list(a.fields), list(b.fields)
        mixed = tuple((fa[i] if rng.random() < 0.5 else fb[i]) for i in range(3))
        if len(set(mixed)) < 3:
            fam = d.get("family") or a.family
            mixed = _pick_fields(rng, fam, self.forbidden, self.forced_delay)
        d["fields"] = mixed
        d["family"] = _FAMILY_OF_FIELD.get(mixed[0], a.family)
        d["model"] = self.name
        d["generation"] = max(int(a.generation or 0), int(b.generation or 0)) + 1
        return Genome(**d)

    def _apply_arm(self, g: Genome, arm: dict, rng: random.Random | None = None) -> Genome:
        d = dict(g.__dict__)
        if arm.get("universe"):
            d["universe"] = str(arm["universe"])
        if arm.get("neutralization"):
            d["neutralization"] = str(arm["neutralization"])
        if arm.get("decay") is not None:
            try:
                d["decay"] = max(0, min(30, int(arm["decay"])))
            except (TypeError, ValueError):
                pass
        # 구조 유전자 arm (family/combine) — 설정 arm 과 같은 원리로 무작위 탐색
        # 슬롯에만 주입된다. family 교체는 필드 정합을 위해 rng 가 필요하다.
        if arm.get("family") and rng is not None:
            fam = str(arm["family"])
            if fam in self.families:
                d["family"] = fam
                d["fields"] = _pick_fields(rng, fam, self.forbidden, self.forced_delay)
        if arm.get("combine"):
            cmb = str(arm["combine"])
            if cmb in self.combines:
                d["combine"] = cmb
        return Genome(**d)

    def _sweep(self, parent: Genome, slot: int, attempt: int) -> Genome:
        """부모에서 **한 축만** 바꾼 변형. 축은 슬롯, 값은 시도 횟수로 정한다.

        무작위 변이와 달리 인과가 남는다 — 부모 대비 딱 한 유전자만 다르므로
        결과 차이를 그 축에 귀속시킬 수 있고, 다음 라운드의 밴딧/정향변이가 배운다.
        회전율이 측정돼 있으면 감쇠 축은 **필요한 값 주변**을 훑는다(맹목 스윕 방지).
        """
        d = dict(parent.__dict__)
        axis = slot % 2
        if axis == 0:
            # ⚠ 순회 순서는 **SWEEP_NEUTRALIZATIONS 기준**이어야 한다. NEUTRALIZATIONS
            #   순서를 그대로 쓰면 INDUSTRY 가 먼저 걸려 실측 최고(STATISTICAL)를 늦게 본다.
            ok = set(_allowed_neutralizations())
            allowed = [n for n in SWEEP_NEUTRALIZATIONS if n in ok] or list(ok)
            cur = d.get("neutralization")
            cand = [n for n in allowed if n != cur] or allowed
            # attempt 는 1부터 — 첫 시도가 실측 최고(STATISTICAL)를 집게 한다.
            d["neutralization"] = cand[(attempt - 1) % len(cand)]
        else:
            want = decay_for_target_turnover(int(d.get("decay") or 0),
                                             self.parent_metrics.get("turnover"))
            if want is not None and want != int(d.get("decay") or 0):
                d["decay"] = want
            else:
                # 회전율이 이미 목표면 감쇠는 더 볼 것이 없다 — **맹목 순회를 하지 않는다**.
                # 그 슬롯은 알파의 핵심 유전자에 쓰는 편이 수율이 높다(부트캠프 5주차:
                # "DK 를 20번 스윕해야 그중 하나가 나온다. 그것보다 데이터 필드를 바꾸고
                #  유의미한 변형을 20개 돌리는 게 낫다"). 절단은 같은 이유로 이미 뺐다.
                cur = d.get("transform_a")
                cand = [t for t in self.transforms if t != cur] or list(self.transforms)
                d["transform_a"] = cand[(attempt - 1) % len(cand)]
        d["generation"] = int(d.get("generation") or 0) + 1
        return Genome(**d)

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        """모델 불변식 재적용 — mutate/crossover/seed 유입 후에도 항상 성립해야 한다."""
        d = dict(g.__dict__)
        if d["transform_a"] not in self.transforms:
            d["transform_a"] = rng.choice(self.transforms)
        if d["transform_b"] not in self.transforms:
            d["transform_b"] = rng.choice(self.transforms)
        if d["combine"] not in self.combines:
            d["combine"] = rng.choice(self.combines)
        if d["decay_style"] not in DECAY_STYLES:
            d["decay_style"] = "mean"
        if d["universe"] not in UNIVERSES:
            d["universe"] = "TOP3000"
        if d["neutralization"] not in NEUTRALIZATIONS:
            d["neutralization"] = "INDUSTRY"
        # v2 유전자 풀 재검증 — 교차/변이/LLM 유입 후에도 항상 유효값이어야 한다.
        if d["trade_when"] not in TRADE_WHEN_KINDS:
            d["trade_when"] = "OFF"
        if d["group_op"] not in GROUP_OPS:
            d["group_op"] = "neutralize"
        if d["group_by"] not in GROUP_BYS:
            d["group_by"] = "auto"
        if d["winsor_std"] not in WINSOR_STDS:
            d["winsor_std"] = 0
        if d["weight_scheme"] not in WEIGHT_SCHEMES:
            d["weight_scheme"] = "1:1"
        # v3 유전자 풀 재검증.
        if d["transform_c"] not in self.transforms:
            d["transform_c"] = rng.choice(self.transforms)
        if d["regime"] not in REGIME_KINDS:
            d["regime"] = "OFF"
        d["hump"] = _snap_hump(d["hump"])
        try:
            d["lookback_c"] = max(0, min(252, int(d["lookback_c"])))
        except (TypeError, ValueError):
            d["lookback_c"] = 0
        if str(self.forced_delay) == "0":
            # D0 에서 쓸 수 없는 필드만 갈아끼운다. 팔레트를 모르면(None) 예전처럼
            # pv 로 강제 — 모르는 상태에서 D1 필드를 쓰면 라운드가 통째로 ERROR 난다.
            allowed = d0_allowed_fields()
            if allowed is None:
                allowed = frozenset(SHARED_DATASETS["pv"])
                if not all(f in allowed for f in d["fields"]):
                    d["fields"] = _pick_fields(rng, "pv", self.forbidden, "0")
                    d["family"] = "pv"
            elif not all(f in allowed for f in d["fields"]):
                fam = d.get("family") or "pv"
                if fam not in D0_DATASETS:
                    fam = "pv"
                d["fields"] = _pick_fields(rng, fam, self.forbidden, "0")
                d["family"] = fam
        if self.forbidden and any(f in self.forbidden for f in d["fields"]):
            d["fields"] = _pick_fields(rng, d.get("family") or "pv",
                                       self.forbidden, self.forced_delay)
        # 탐색 조건은 **맨 마지막**에 건다. 모든 유전체(random/mutate/crossover/spec/seed)가
        # 이 메서드를 지나므로 여기 한 곳이면 조건 밖 알파가 애초에 생기지 않는다.
        _apply_constraint(d)
        d["model"] = self.name
        return Genome(**d)

    def _genome(self, slot: int, rng: random.Random) -> Genome:
        family = self.families[(slot - 1) % len(self.families)]
        fields = _pick_fields(rng, family, self.forbidden, self.forced_delay)
        g = Genome(
            model=self.name,
            family=family,
            fields=fields,
            transform_a=rng.choice(self.transforms),
            transform_b=rng.choice(self.transforms),
            combine=rng.choice(self.combines),
            sign=-1 if rng.random() < 0.55 else 1,
            lookback_a=rng.choice(CANONICAL_LOOKBACKS[:-1]),
            lookback_b=rng.choice((10, 20, 40, 60, 120, 252)),
            universe=rng.choice(UNIVERSES),
            neutralization=rng.choice(NEUTRALIZATIONS),
            decay=rng.choice(self.decays),
            # 절단은 **무작위로 탐색하지 않는다**. 실측(2026-07-21)에서 0.05~0.15 가 결과
            # 동일이었고 그때 스윕 축에서 이미 뺐는데, 무작위 슬롯에선 계속 굴리고 있었다.
            # 필요한 곳(집중도 미달 정향변이)에선 그대로 조정한다 — 유전자를 없애는 게
            # 아니라 탐색 축에서 내리는 것이다.
            truncation=self.truncations[0],
            nan_handling="ON" if family in ("fundamental", "analyst", "option", "news") else "OFF",
            decay_style=rng.choice(DECAY_STYLES),
            generation=0,
            # v2 유전자는 기본값 쪽으로 강하게 편향 — 무작위 탐색의 분포가 확장 때문에
            # 급변하면 기존 밴딧/엘리트 통계와 비교 불가능해진다. 소수 슬롯만 신영역 탐침.
            trade_when=(rng.choice(TRADE_WHEN_KINDS[1:]) if rng.random() < 0.25 else "OFF"),
            group_op=(rng.choice(GROUP_OPS) if rng.random() < 0.2 else "neutralize"),
            group_by=(rng.choice(GROUP_BYS[1:]) if rng.random() < 0.15 else "auto"),
            winsor_std=(rng.choice(WINSOR_STDS[1:]) if rng.random() < 0.25 else 0),
            weight_scheme=(rng.choice(tuple(WEIGHT_SCHEMES)[1:])
                           if rng.random() < 0.2 else "1:1"),
            # v3 — 같은 원칙으로 소수 슬롯만 신영역 탐침(기본값 편향).
            transform_c=(rng.choice(self.transforms) if rng.random() < 0.3 else "ts_zscore"),
            lookback_c=(rng.choice((10, 20, 40, 60, 120)) if rng.random() < 0.2 else 0),
            regime=(rng.choice(REGIME_KINDS[1:]) if rng.random() < 0.25 else "OFF"),
            hump=(rng.choice(HUMPS[1:]) if rng.random() < 0.25 else 0.0),
        )
        # 무작위 슬롯의 절반은 검증된 강신호 골격을 입힌다 — 신선 필드(위의 순환
        # 팔레트)는 유지하고 변환·결합·감쇠만 교체. 나머지 절반은 순수 무작위로
        # 신조합 탐침을 유지한다. 골격은 라운드×슬롯으로 순환해 8종을 고루 쓴다.
        if rng.random() < 0.5:
            tpl = STRONG_TEMPLATES[(self.round_num + slot) % len(STRONG_TEMPLATES)]
            g = Genome(**{**g.__dict__, **tpl})
        return g

    def _desc(self, g: Genome, origin: str = "random") -> str:
        tag = {"mutate": "mut", "crossover": "xo", "random": "rand",
               "spec": "spec"}.get(origin, origin)
        gen = f" g{g.generation}" if g.generation else ""
        return (f"{self.name} {g.family}: {g.combine}/{g.transform_a}+{g.transform_b} "
                f"{g.universe}x{g.neutralization} [{tag}{gen}]")


class StandardGenomeModel(BaseGenomeModel):
    name = "standard-genome"
    # ⚠ 일반 계정은 **그룹 중립화 5종만** 쓸 수 있다 (2026-07-27 실계정 실측).
    #   리스크 중립화(STATISTICAL·CROWDING·FAST·SLOW·SLOW_AND_FAST·RAM)를 넣으면
    #   WQB 가 "Neutralization X is not available." 로 400 을 준다 — 시뮬이 아예
    #   접수되지 않는다. 유전자 풀에서 빼야 교차·변이로도 다시 안 생긴다.
    neutralizations = tuple(n for n in NEUTRALIZATIONS if n not in RISK_NEUTRALIZATIONS)

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        g = super()._constrain(g, rng)
        neut = (g.neutralization if g.neutralization in self.neutralizations
                else rng.choice(self.neutralizations))
        return Genome(**{**g.__dict__,
                         "neutralization": neut,
                         "decay": max(g.decay, 4),
                         "truncation": max(g.truncation, 0.08)})


class ResearchConsultantGenomeModel(BaseGenomeModel):
    name = "rc-api-genome"
    # `triple` 이 빠져 있던 탓에 RC 유전체는 fields[2] 를 **한 번도 발현하지 못했다** —
    # 3번째 유전자가 죽은 채로 교차/변이만 돌았다. ts_mean 도 같은 이유로 복원한다.
    transforms = ("rank", "ts_rank", "ts_zscore", "ts_delta", "ts_mean", "ts_av_diff")
    combines = ("spread", "sum", "product", "ratio", "corr", "triple", "resid")
    decays = (0, 2, 4, 6, 8)
    truncations = (0.08, 0.1)

    def _constrain(self, g: Genome, rng: random.Random) -> Genome:
        g = super()._constrain(g, rng)
        d = dict(g.__dict__)
        if d["neutralization"] == "NONE":
            d["neutralization"] = "MARKET"
        d["decay"] = min(int(d["decay"]), 8)
        d["truncation"] = min(max(float(d["truncation"]), 0.08), 0.1)
        d["nan_handling"] = "OFF"
        # RC 는 API 가 모든 후보를 제출 시도하므로 보수적으로: 이상치 절단은 off/4 만.
        if d["winsor_std"] not in (0, 4):
            d["winsor_std"] = 4
        return Genome(**d)


def generate_population(*, account_type: str, round_num: int, forced_delay=None,
                        errors=None, feedback=None, n: int = 8,
                        parent_genome=None, fail_items=None, seed_genomes=None,
                        slot_settings=None, salt: int = 0,
                        parent_alpha_id=None, seed_alpha_ids=None,
                        directive_stats=None, spec_genomes=None,
                        spec_ids=None, parent_metrics=None,
                        search_mode: str = 'legacy') -> list[dict]:
    cls = (ResearchConsultantGenomeModel
           if account_type == "research_consultant" else StandardGenomeModel)
    return cls(round_num=round_num, forced_delay=forced_delay, errors=errors,
               feedback=feedback, parent_genome=parent_genome, fail_items=fail_items,
               seed_genomes=seed_genomes, slot_settings=slot_settings,
               salt=salt, parent_alpha_id=parent_alpha_id,
               seed_alpha_ids=seed_alpha_ids,
               directive_stats=directive_stats, spec_genomes=spec_genomes,
               spec_ids=spec_ids, parent_metrics=parent_metrics,
               search_mode=search_mode).generate(n=n)
