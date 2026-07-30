"""criteria — WQB **컨설턴트 제출 기준**의 단일 진실 소스 (2026-07-21 개편).

왜 이 모듈이 생겼나
--------------------
2026-07 WQB 가 컨설턴트 제출 규칙을 크게 바꿨다. 그전까지 이 코드베이스는
'Sharpe 1.58 / Fitness 1.0 / PASS 7개 이상' 이라는 **단일 컷**을 여기저기
하드코딩해 두고 있었는데, 실제 규칙은 이제 이렇다:

1. **컷이 delay 별로 다르다** — D0 는 Sharpe 2.69 / Fitness 1.5, D1 은 1.58 / 1.0.
   (구 코드는 두 값을 섞어 하드코딩했다: SUBMIT_SHARPE_REF=1.58 인데 SHARPE_2Y_REF=2.69.)
2. **고회전 알파는 사다리 문턱이 완화된다.** 2026-07-21 라이브에서는 "HT 분류를 얻으면
   표준 컷이 WARNING 으로 강등된다"고 읽혔는데, 같은 날 공식 문서(`Consultant Submission
   Tests` → "Interpreting Status Messages")를 받아보니 **실제 메커니즘은 IS Ladder 를
   0.75× / 0.85× / 1.0× 세 단계로 거는 것**이었다. LADDER_STAGES 참조.
   즉 제출 가능성의 1차 결정 변수가 Sharpe 가 아니라 **회전율과 신호 지평**이라는
   결론 자체는 맞지만, 그 이유는 '강등' 이 아니라 '사다리 할인' 이다.
3. 체크가 대폭 늘었다 — CLUSTER_TEST(2026-07-17 신설, 분류 전용·비차단),
   HT_* 11종, MATCHES_PYRAMID/THEMES/CLASSIFICATION, OSMOSIS_ALLOCATION,
   DATA_DIVERSITY, D0_SUBMISSION, REGULAR_SUBMISSION, POWER_POOL_CORRELATION.

출처: BRAIN Documentation — "Consultant Submission Tests" · "Getting Started with
High Turnover Alphas" · "Understanding PnL Realization Horizon" · "Multiplier Rules"
(2026-07-21 사장 제공) + 같은 날 라이브 API 실측.

순수 모듈: IO·DB·전역상태 없음, 예외를 던지지 않는다.
"""
from __future__ import annotations

import math

# ── 표준 제출 컷 (CHN 제외 전 지역) ──────────────────────────────────────────
# "Fitness > 1.5 for Delay 0, > 1 for Delay 1 / Sharpe > 2.69 for D0, > 1.58 for D1"
CUTOFFS = {
    '0': {'sharpe': 2.69, 'fitness': 1.5, 'ladder_fail': 2.69},
    '1': {'sharpe': 1.58, 'fitness': 1.0, 'ladder_fail': 1.59},
}
DEFAULT_DELAY = '1'

# ── CHN 전용 컷 (문서: "Alphas in CHN region") ──────────────────────────────
# "중국 시장은 거래비용이 높아 다른 리전보다 높은 수익률을 요구한다."
# USA 엔 아예 없는 **Returns 절대 하한**이 붙는다.
CHN_CUTOFFS = {
    '0': {'sharpe': 3.5, 'fitness': 1.5, 'returns': 0.12},
    '1': {'sharpe': 2.08, 'fitness': 1.0, 'returns': 0.08},
}
# 실측(2026-07-21, 알파 bldVO39p/xAd9bMEp): 체크 limit 이 D1 2.07 / D0 3.49 로 문서와 일치.
# CHN 전용 Robust Universe Test — 조정 유니버스에서 원본 대비 수익률·Sharpe 를 40% 이상 유지.
# ASI 는 같은 테스트를 90% 로 훨씬 빡세게 건다.
ROBUST_UNIVERSE_RETENTION = {'CHN': 0.40, 'ASI': 0.90}

# Turnover: "> 1% and < 70%" — 이 밖은 LOW_TURNOVER / HIGH_TURNOVER FAIL(차단).
TURNOVER_MIN = 0.01
TURNOVER_MAX = 0.70
# SuperAlpha 는 **회전율 규정만 다르다**: "2% <= turnover < 40%". 나머지는 알파와 동일.
SUPERALPHA_TURNOVER_MIN = 0.02
SUPERALPHA_TURNOVER_MAX = 0.40

# Weight test: "Max weight in any stock < 10%" (+ 충분한 종목수에 가중치가 실려야 한다.
# 최소 종목수는 유니버스마다 다르다). 시뮬 시작 직후 전종목 0 가중은 실패로 치지 않는다.
MAX_WEIGHT_PER_INSTRUMENT = 0.10

# 자기상관 — **0.7 초과라도 탈출구가 있다.** 문서 원문:
#   "Less than 0.7 PNL series correlation with user's alphas, **or a sharpe at least
#    10% greater than other correlated alphas submitted by user**"
# Prod-correlation 은 같은 규정을 BRAIN 전체 제출 알파에 적용한 것이다.
# 실측 근거: akEMEvk6(Sharpe 1.73)이 자기상관 0.876 으로도 제출에 성공했다 —
# 우리가 한때 "HT 가 SELF_CORRELATION 까지 강등한다"고 오판했던 사건의 진짜 원인.
CORRELATION_MAX = 0.70
CORRELATION_SHARPE_EDGE = 1.10       # 상관 알파 대비 Sharpe 가 이 배수 이상이면 통과

# Cluster Test (2026-07-17 신설): Cluster Sharpe >= 1.58 이면 Cluster Alpha 로 분류.
# **분류 전용이다** — 문서 명시: "Failing the Cluster Test does not block submission".
CLUSTER_SHARPE_MIN = 1.58

# ── IS Ladder (Check-IS-Sharpe) ─────────────────────────────────────────────
# 전 구간 Sharpe < FAIL_THRESHOLD → 즉시 실패. 아니면 N=2 년부터:
#   Sharpe[N] < FAIL → 실패 / Sharpe[N] > PASS[N] → 통과 / 사이면 N+=1.
LADDER_PASS = {          # years: (D1, D0)
    2: (2.38, 3.96), 3: (2.38, 3.96), 4: (2.38, 3.96), 5: (2.38, 3.96),
    6: (2.22, 3.64), 7: (2.06, 3.33), 8: (1.90, 3.17), 9: (1.74, 2.85),
    10: (1.59, 2.69),
}
# "If the turnover of Alpha is less than 30%, the IS Sharpe Ladder PASS_THRESHOLDS
#  are multiplied by a factor of 0.85. FAIL_THRESHOLD is not multiplied."
# ⚠ 이 할인은 후비용 Sharpe 조건과 **같은 방향**을 가리킨다 — 회전율 20~30% 대역이
#   (a) 후비용 요구치가 가장 낮고 (b) 2Y/ladder 문턱까지 15% 깎인다. 대역 하단이 두 번 싸다.
LADDER_LOW_TURNOVER_DISCOUNT = 0.85
LADDER_DISCOUNT_TURNOVER = 0.30

# ── 사다리는 실제 제출 판정에서 **3단계**로 걸린다 ───────────────────────────
# 문서 "Interpreting Status Messages in Simulation Results" 의 표는 **실패 메시지 목록**이다:
#
#   0.75× 실패                                     → "Improve Sharpe"
#   0.75× 통과 · 0.85× 실패 · 회전율 > 10%           → "Improve Sharpe or reduce turnover"
#   0.85× 통과 · 1.0× 실패 · 회전율 > 30% · 상관>0.3  → "…or reduce correlation"
#
# ⚠ 방향에 주의 — **회전율이 높을수록 더 높은 배수를 통과해야 한다**(강화지 완화가 아니다).
#   회전율 10% 이하면 0.75× 만 넘으면 되고, 30% 초과 + 상관 0.3 초과면 1.0× 를 다 넘어야 한다.
#   즉 회전율을 올리면 사다리가 비싸진다. 고회전 전략의 숨은 비용이다.
#
# ⚠ 이것은 우리가 라이브에서 본 '표준 컷이 WARNING 으로 강등되는 현상' 과 **별개 메커니즘**이다.
#   그 강등(HT 분류 + 후비용 Sharpe > 0 → LOW_SHARPE/LOW_FITNESS 가 WARNING)은 아직
#   문서에서 근거를 못 찾았다. 실측으로만 아는 규칙이므로 HT_WAIVER_* 주석을 그대로 둔다.
LADDER_STAGES = (
    {'mult': 0.75, 'turnover_min': None, 'corr_min': None},
    {'mult': 0.85, 'turnover_min': 0.10, 'corr_min': None},
    {'mult': 1.00, 'turnover_min': 0.30, 'corr_min': 0.30},
)

# ── 단일 데이터셋(Single Dataset / ATOM) 알파의 완화 규정 ────────────────────
# "Single Dataset Alphas … don't need to pass the IS Ladder Sharpe Test. Instead, only
#  the last two year Avg IS Sharpe must clear: Delay-1 2.38 / Delay-0 3.96"
#   → 이 값은 LADDER_PASS[2] 와 정확히 같다. 즉 사다리를 오르지 않고 **2년 칸 하나만** 본다.
# 정의: 6개 허용 그룹필드(country/exchange/market/sector/industry/subindustry)를 빼고
#   **한 데이터셋의 필드만** 쓴 알파. inst_pnl()·convert() 는 pv1 사용으로 간주된다.
#
# 실측(2026-07-21)으로 확인한 구조 — 문서엔 안 적힌 부분:
#   단일데이터셋 알파 → 체크 이름이 **LOW_2Y_SHARPE**  (limit = FAIL 문턱)
#   다중데이터셋 알파 → 체크 이름이 **IS_LADDER_SHARPE** (year 필드 동반)
#   예: kqZkEgmO(D0·단일) LOW_2Y_SHARPE limit 2.69 / d5ReNWXK(D0·다중) IS_LADDER year=2 limit 2.69
#   즉 **체크가 보여주는 limit 은 FAIL 문턱**이고, 위 2.38/3.96 은 PASS 문턱이다.
#   그래서 아래 함수는 (fail, pass) 두 값을 모두 돌려준다 — 하한은 FAIL, 목표는 PASS.
SINGLE_DATASET_CLASSIFICATION = 'SINGLE_DATA_SET'

# ── Sub-universe ────────────────────────────────────────────────────────────
# TOPXXX: subuniverse_sharpe >= 0.75 * sqrt(sub_size / alpha_universe_size) * alpha_sharpe
# **자기 Sharpe 에 비례하는 상대 기준**이라 Sharpe 가 낮아도 원리적으로 통과 가능하다.
SUB_UNIVERSE_K = 0.75
UNIVERSE_SIZE = {
    'TOP3000': 3000, 'TOP2000': 2000, 'TOP1000': 1000,
    'TOP500': 500, 'TOP200': 200, 'TOPSP500': 500,
}
# 비-TOPXXX 유니버스는 크기 공식 대신 **고정 비율**을 쓴다:
#   subuniverse_sharpe >= subuniverse_ratio * alpha_sharpe
SUB_UNIVERSE_RATIO = {
    'ASI MINVOL1M': 0.295,
    'USA ILLIQUID_MINVOL1M': 0.41,
    'EUR ILLIQUID_MINVOL1M': 0.355,
}
# 어떤 유니버스가 하위유니버스로 쓰이는지 (라이브 cutoff 역산으로 확증:
# TOP3000→0.433·sharpe = 0.75·sqrt(1000/3000), TOP1000→0.530 = 0.75·sqrt(500/1000),
# TOP500→0.474 = 0.75·sqrt(200/500). TOP200 은 하위유니버스 체크 자체가 없다).
SUB_UNIVERSE_OF = {
    'TOP3000': 'TOP1000', 'TOP2000': 'TOP1000',
    'TOP1000': 'TOP500', 'TOP500': 'TOP200', 'TOP200': None,
}

# ── High Turnover (HTVR) 분류 ───────────────────────────────────────────────
# Base eligibility: Region USA · Turnover > 20% · (수익보존 > 0.75 OR PnL실현지평 < 20일)
#   - "Getting Started with High Turnover Alphas": Region USA, Turnover > 20%,
#     hightvrReturns / original return > 0.75
#   - "Understanding PnL Realization Horizon": Turnover > 20% AND
#     (PnL Realization Horizon < 20 days OR High TVR Returns > 75% of total return)
# 라이브 체크는 HT_TURNOVER / HT_HIGH_TURNOVER_RETURNS_RATIO / HT_PNL_REALIZATION_HORIZON.
HT_REGION = 'USA'
HT_TURNOVER_MIN = 0.20
HT_RETURNS_RATIO_MIN = 0.75
HT_PNL_HORIZON_MAX = 20

# 하위분류 4종 (각각 테마 배수를 3x 로 올리는 열쇠 — GLB High Turnover Theme 기준).
HT_AFTER_COST_SHARPE_MIN = 1.0        # Classification 1: After Cost
HT_INVESTABLE_SHARPE_MIN = 2.0        # Classification 2: Investable (maxTrade | maxPos)
HT_INVESTABLE_TURNOVER_MIN = 0.20
HT_LIQUID_TOP200_SHARPE_MIN = 1.0     # Classification 3: Liquid
HT_LIQUID_RATIO_MIN = 0.7
HT_ORTHOGONAL_NEUTRALIZATION = 'REVERSION_AND_MOMENTUM'   # Classification 4: Orthogonal
# WQB 체크는 이 설정값을 'RAM' 이라고 표기한다 (HT_ORTHOGONAL_RAM_NEUTRALIZATION).

# ── ⚠ HT 강등(waiver)의 진짜 조건: **후비용 Sharpe > 0** — 실측으로만 아는 부분 ────
# 문서에는 "HT 분류를 얻으면 표준 컷이 WARNING 이 된다" 는 말이 없다. 2026-07-21 라이브
# A/B 5건(전부 USA·D0·TOP3000·SUBINDUSTRY, 전부 HIGH_TURNOVER 분류 획득):
#
#   알파        Sharpe  회전율  HT수익비율  후비용Sharpe  →  표준컷 판정
#   np8k1q38     1.63   60.0%     0.875        +0.35        WARNING(강등)
#   gJ9qkKWv     1.53   54.0%     0.853        +0.41        WARNING(강등)
#   QP9rLo5Q     1.12   40.0%     0.844        +0.21        WARNING(강등)
#   E5eo9OLG     1.34   62.0%     0.862        -0.15        FAIL
#   RR1Zn0Ng    -0.44   61.5%     1.021        -2.10        FAIL
#
# **Sharpe 는 단조가 아니다** — 1.34 가 떨어지고 1.12 가 통과했다. 후비용 Sharpe 부호는
# 5건 전부를 정확히 가른다. 즉 강등 조건은 'HT 분류 + 후비용 Sharpe > 0' 이다.
# (고회전 캠페인의 경제적 취지 그대로다: 회전 비용을 내고도 남는 신호인가.)
HT_WAIVER_AFTER_COST_MIN = 0.0

# ── 후비용 Sharpe 의 **닫힌 형태** (2026-07-21 역산으로 확정) ─────────────────
# "Parameters in the Simulation results" 문서가 마진을 정의해 준 덕에 추정을 계산으로
# 바꿀 수 있었다: Margin = PnL / 거래대금, Returns = 연 PnL / (북사이즈/2),
# Turnover = 일 거래대금 / 북사이즈, 연 거래일 252.
#     margin = returns / (2 · 252 · turnover) = returns / (504 · turnover)
# 거래비용을 거래대금당 k 로 두면 비용 차감 후 마진은 (margin − k) 이고,
# 변동성이 그대로면 Sharpe 도 같은 비율로 줄어든다:
#     after_cost_sharpe = sharpe · (margin − k) / margin
# 라이브 6건에서 k 를 역산하니 **2.98 ~ 3.08 bp** 로 수렴했다 → k = 3bp 로 확정.
#
#   알파        Sharpe  회전율  수익률   마진    후비용   역산 k
#   np8k1q38     1.63   60.0%  11.40%  3.80bp  +0.35   2.98bp
#   gJ9qkKWv     1.53   54.0%  11.09%  4.11bp  +0.41   3.01bp
#   QP9rLo5Q     1.12   40.0%   7.54%  3.77bp  +0.21   3.06bp
#   E5eo9OLG     1.34   62.0%   8.60%  2.77bp  -0.15   3.08bp
#   RR1Zn0Ng    -0.44   61.5%  -2.42% -0.79bp  -2.10   2.98bp
#
# 따라서 강등 조건은 놀랍도록 단순해진다:  **마진 > 3bp**  (양의 Sharpe 전제)
# 이걸 GA 가 겨냥할 형태로 풀면:  **수익률 > 0.1512 × 회전율**
#   회전율 25% → 수익률 3.8% 면 충분 / 회전율 60% → 수익률 9.1% 필요.
# '대역 하단(20~30%)이 싸다' 는 결론이 여기서 정량적으로 나온다.
TRANSACTION_COST = 0.0003        # 거래대금당 3bp
TRADING_DAYS = 252
# 마진 = 수익률 / (504 · 회전율) 의 504 (= 2 · 252). 북사이즈의 절반이 분모라 2 가 붙는다.
MARGIN_TURNOVER_FACTOR = 2 * TRADING_DAYS

# ── IS check 분류 ───────────────────────────────────────────────────────────
# BLOCKING = FAIL 이면 제출이 막히는 체크. 나머지는 분류/장부용이라 FAIL 해도 무해하다.
# ⚠ CLUSTER_TEST 를 여기 넣으면 안 된다 — 문서가 비차단임을 명시한다.
BLOCKING_CHECKS = frozenset({
    'LOW_SHARPE', 'LOW_FITNESS', 'LOW_TURNOVER', 'HIGH_TURNOVER',
    'CONCENTRATED_WEIGHT', 'LOW_SUB_UNIVERSE_SHARPE', 'LOW_2Y_SHARPE',
    'IS_LADDER_SHARPE', 'SELF_CORRELATION', 'PROD_CORRELATION', 'UNITS',
    'BIAS', 'DATA_DIVERSITY', 'D0_SUBMISSION', 'REGULAR_SUBMISSION',
    'POWER_POOL_CORRELATION', 'MATCHES_COMPETITION',
})
# 분류/장부 체크 — pass 카운트에도, 차단 판정에도 넣지 않는다.
CLASSIFICATION_CHECKS = frozenset({
    'CLUSTER_TEST', 'MATCHES_CLASSIFICATION', 'MATCHES_PYRAMID', 'MATCHES_THEMES',
    'OSMOSIS_ALLOCATION',
})

# IS check 이름 → metrics 키. harvest_alpha 가 이 표로 값/컷오프를 승격한다
# (요약 `is` 블록엔 sharpe/fitness/returns/turnover/drawdown/margin 6개뿐이라,
#  승격하지 않으면 보상·선택이 HT 지표를 영원히 못 본다).
CHECK_METRIC_KEY = {
    # value → <key>, limit → <key>_cutoff 규칙이라 컷은 'sharpe_check_cutoff' 로 들어온다.
    'LOW_SHARPE': 'sharpe_check',
    'LOW_FITNESS': 'fitness_check',
    'LOW_2Y_SHARPE': 'sharpe_2y',
    # 리전별 Sharpe — 라이브 FAIL 목록의 절반이 이것인데 지도에 없어서 값·컷이
    # metrics 에 안 남았다. 알파 상세가 이름표만 띄우던 이유 (2026-07-30).
    'LOW_GLB_AMER_SHARPE': 'glb_amer_sharpe',
    'LOW_GLB_EMEA_SHARPE': 'glb_emea_sharpe',
    'LOW_GLB_APAC_SHARPE': 'glb_apac_sharpe',
    'IS_LADDER_SHARPE': 'ladder_sharpe',
    'LOW_SUB_UNIVERSE_SHARPE': 'sub_universe_sharpe',
    'CONCENTRATED_WEIGHT': 'weight_concentration',
    'SELF_CORRELATION': 'self_correlation',
    'PROD_CORRELATION': 'prod_correlation',
    'CLUSTER_TEST': 'cluster_sharpe',
    'HT_TURNOVER': 'ht_turnover',
    'HT_HIGH_TURNOVER_RETURNS_RATIO': 'ht_returns_ratio',
    'HT_PNL_REALIZATION_HORIZON': 'ht_pnl_horizon',
    'HT_AFTER_COST_SHARPE': 'ht_after_cost_sharpe',
    'HT_INVESTABLE_MAX_TRADE_SHARPE': 'ht_maxtrade_sharpe',
    'HT_INVESTABLE_MAX_TRADE_TURNOVER': 'ht_maxtrade_turnover',
    'HT_INVESTABLE_MAX_POSITION_SHARPE': 'ht_maxpos_sharpe',
    'HT_INVESTABLE_MAX_POSITION_TURNOVER': 'ht_maxpos_turnover',
    'HT_LIQUID_TOP200_SHARPE': 'ht_top200_sharpe',
    'HT_LIQUID_TOP500_TOP200_SHARPE_RATIO': 'ht_liquid_ratio',
}

# 사람이 읽는 라벨 (로그·UI 용).
HT_GAP_LABEL = {
    'turnover': '회전율',
    'returns_ratio': 'HT수익비율',
    'pnl_horizon': 'PnL실현지평',
}


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _f(v):
    """관대한 float 변환 — 못 읽으면 None ('측정 안 됨' 과 0 을 구분한다).

    reward._fopt / wqb_backend._parse_metric_number 와 같은 단위 규칙(%, ‱, bp)을 쓴다.
    브라우저 시대 알파가 지표를 퍼센트 **문자열**로 저장했기 때문에 이 관용성이 필요하다.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    unit = 1.0
    if s.endswith('%'):
        unit, s = 1.0 / 100, s[:-1]
    elif s.endswith('‱'):
        unit, s = 1.0 / 10000, s[:-1]
    elif s[-2:].lower() == 'bp':
        unit, s = 1.0 / 10000, s[:-2]
    s = s.replace(',', '').strip()
    try:
        return float(s) * unit
    except ValueError:
        return None


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _delay_key(delay) -> str:
    """'0'|'1' 로 정규화. 알 수 없으면 D1(느슨한 쪽)으로 — 모르는데 D0 컷을 씌우면
    보상이 통째로 눌려 GA 가 죽는다."""
    s = str(delay).strip() if delay is not None else ''
    return '0' if s in ('0', '0.0') else DEFAULT_DELAY


# ── 공개 API ────────────────────────────────────────────────────────────────

def is_blocking(name) -> bool:
    """이 체크가 FAIL 이면 제출이 막히는가. 처음 보는 이름은 **차단으로 간주**한다
    (모르는 규칙을 무시해 '제출 가능' 이라고 착각하는 쪽이 훨씬 위험하다)."""
    nm = str(name or '').strip().upper()
    if not nm:
        return False
    if nm in CLASSIFICATION_CHECKS or nm.startswith('HT_') or nm.startswith('MATCHES_'):
        return False
    return True


def cutoffs(delay, region=None) -> dict:
    """delay(·region) 별 제출 컷.

    CHN 은 컷이 따로다 — Sharpe 가 30% 남짓 높고 **Returns 절대 하한**이 추가된다
    (USA 엔 Returns 체크 자체가 없다). region 을 안 주면 기존 동작(비-CHN)을 유지한다.
    """
    key = _delay_key(delay)
    if str(region or '').strip().upper() == 'CHN':
        return dict(CHN_CUTOFFS[key])
    return dict(CUTOFFS[key])


def ladder_stage(turnover=None, correlation=None) -> dict:
    """이 알파에 실제로 걸리는 IS Ladder 단계를 돌려준다.

    반환값의 `mult` 를 LADDER_PASS 문턱에 곱하면 그 알파가 실제로 넘어야 할 선이 나온다.
    회전율이 **높을수록** 상위 단계가 추가로 걸려 배수가 커진다(= 더 엄해진다).

    회전율/상관을 모르면 가장 엄한 1.0× 를 돌려준다 — 모르는데 완화를 가정하면
    '제출 가능' 을 낙관해 GA 가 헛것을 좇는다.
    """
    to = _f(turnover)
    corr = _f(correlation)
    applicable = LADDER_STAGES[0]
    for st in LADDER_STAGES:
        # 모르는 값은 **조건이 걸린 것으로 간주**한다 (보수적 = 더 높은 배수).
        if st['turnover_min'] is not None and to is not None and to <= st['turnover_min']:
            continue
        if st['corr_min'] is not None and corr is not None and corr <= st['corr_min']:
            continue
        applicable = st
    return dict(applicable)


def self_correlation_ok(correlation, sharpe=None, correlated_sharpe=None) -> bool:
    """자기상관 통과 여부. 0.7 미만이거나, **상관 알파보다 Sharpe 가 10% 이상 높으면** 통과.

    두 번째 조항이 실무에서 결정적이다 — 같은 아이디어를 개량해 성능을 끌어올리면
    상관이 높아도 제출된다(akEMEvk6 가 자기상관 0.876 으로 통과한 실제 경로).
    비교 대상 Sharpe 를 모르면 상관 컷만 본다.
    """
    c = _f(correlation)
    if c is None or c < CORRELATION_MAX:
        return True
    s, other = _f(sharpe), _f(correlated_sharpe)
    if s is None or other is None or other <= 0:
        return False
    return s >= other * CORRELATION_SHARPE_EDGE


def delay_of(metrics) -> str:
    """metrics 에 stamp 된 '_delay' 를 읽는다 (worker 가 라운드 강제 delay 를 심는다)."""
    return _delay_key((metrics or {}).get('_delay'))


def ladder_pass_threshold(years: int, delay, turnover=None):
    """IS Ladder 의 N년차 PASS 문턱. turnover < 30% 면 0.85 배 할인된다."""
    y = max(2, min(10, int(years or 2)))
    d1, d0 = LADDER_PASS[y]
    thr = d0 if _delay_key(delay) == '0' else d1
    t = _f(turnover)
    if t is not None and t < LADDER_DISCOUNT_TURNOVER:
        thr *= LADDER_LOW_TURNOVER_DISCOUNT
    return thr


def is_single_dataset(metrics) -> bool:
    """단일 데이터셋(ATOM) 알파인가. harvest 가 실은 classification id 로 판정한다.

    분류는 WQB 가 코드를 보고 정해 주는 것이라(우리가 세는 게 아니라) 실측값을 그대로
    믿는다 — 우리 렌더러의 'family' 는 데이터셋이 아니라 팔레트 계열이라 일치하지 않는다.
    """
    ids = str((metrics or {}).get('classification_ids') or '').upper()
    return SINGLE_DATASET_CLASSIFICATION in ids


def two_year_thresholds(delay, turnover=None, single_dataset=False):
    """(FAIL 문턱, PASS 문턱) — 최근 2년 IS Sharpe 기준.

    - FAIL 문턱: 이 밑이면 즉시 탈락 (체크가 limit 으로 보여주는 값).
    - PASS 문턱: 사다리 2년 칸. 회전율 30% 미만이면 0.85 배 할인.
    단일데이터셋 알파는 사다리를 오르지 않고 이 2년 칸 하나로 끝난다 —
    즉 **PASS 문턱만 넘으면 통과**이고, 다중데이터셋 알파는 못 넘으면 3년·4년…으로
    이어가므로 여기서 끝나지 않는다.
    """
    fail = CUTOFFS[_delay_key(delay)]['ladder_fail']
    return fail, ladder_pass_threshold(2, delay, turnover)


def stability_target(metrics):
    """이 알파가 실제로 넘어야 하는 2Y Sharpe 목표치.

    실측 컷(metrics['sharpe_2y_cutoff'])이 있으면 그건 FAIL 문턱이다 — 하한으로만 쓰고,
    목표는 PASS 문턱으로 잡는다. 단일데이터셋이면 그 한 칸이 곧 최종 관문이라 목표가 명확하고,
    다중데이터셋이면 사다리가 이어지므로 2년 칸은 '첫 관문' 이다.
    """
    m = metrics or {}
    fail, passing = two_year_thresholds(m.get('_delay'), _f(m.get('turnover')),
                                        is_single_dataset(m))
    live_fail = _f(m.get('sharpe_2y_cutoff'))
    if live_fail is not None and live_fail > 0:
        fail = live_fail
    return max(fail, passing)


def sub_universe_cutoff(sharpe, universe):
    """하위유니버스 Sharpe 컷 = 0.75·sqrt(sub/alpha)·alpha_sharpe.
    하위유니버스가 없는 유니버스(TOP200)면 None."""
    s = _f(sharpe)
    if s is None:
        return None
    uni = str(universe or 'TOP3000').strip().upper()
    sub = SUB_UNIVERSE_OF.get(uni, 'TOP1000')
    if not sub:
        return None
    a_size = UNIVERSE_SIZE.get(uni)
    s_size = UNIVERSE_SIZE.get(sub)
    if not a_size or not s_size:
        return None
    return SUB_UNIVERSE_K * math.sqrt(float(s_size) / float(a_size)) * s


def margin(metrics):
    """거래대금당 이익(마진). 실측이 있으면 그것, 없으면 수익률/(504·회전율).

    IS 요약에 margin 이 그대로 들어오지만(harvest 가 수확한다), 레거시 행에는 없을 수
    있어 항등식으로 복원한다 — 라이브 6건에서 둘의 오차는 소수 둘째자리(bp) 수준이었다.
    """
    m = metrics or {}
    v = _f(m.get('margin'))
    if v is not None:
        return v
    returns, turnover = _f(m.get('returns')), _f(m.get('turnover'))
    if returns is None or turnover is None or turnover <= 0:
        return None
    return returns / (MARGIN_TURNOVER_FACTOR * turnover)


def required_returns(turnover):
    """이 회전율에서 **거래비용을 넘기 위해** 필요한 최소 연수익률.

    = 504 · 회전율 · 3bp = 0.1512 · 회전율. GA 가 직접 겨냥할 수 있는 형태다.
    """
    t = _f(turnover)
    if t is None or t <= 0:
        return None
    return MARGIN_TURNOVER_FACTOR * t * TRANSACTION_COST


def after_cost_sharpe(metrics):
    """후비용 Sharpe. 실측(HT_AFTER_COST_SHARPE)이 있으면 그것, 없으면 마진에서 계산.

        after_cost = sharpe · (margin − k) / margin,  k = 3bp

    실측은 회전율이 낮아 HT 체크가 안 돌면 아예 없는데, 이 값이 강등의 유일한
    판별자라 없다고 0 으로 두면 GA 가 눈을 감고 진화한다. 위 항등식은 라이브 6건에서
    실측과 일치했다(상수 주석의 역산표 참조).
    """
    m = metrics or {}
    v = _f(m.get('ht_after_cost_sharpe'))
    if v is not None:
        return v
    sharpe = _f(m.get('sharpe'))
    mg = margin(m)
    # 마진이 0 근처면 식이 발산한다 — 그런 알파는 애초에 수익이 없다는 뜻이라 None.
    if sharpe is None or mg is None or abs(mg) < 1e-6:
        return None
    # ⚠ abs() 를 씌우면 안 된다. 마진이 **음수**인 알파(손실)에서 부호가 뒤집혀
    #   -2.10 이 +2.13 으로 읽힌다 — 라이브 RR1Zn0Ng 가 정확히 그 경우였다.
    #   순수 나눗셈이 양수·음수 마진 모두를 재현한다.
    return sharpe * (mg - TRANSACTION_COST) / mg


def ht_status(metrics, region=HT_REGION) -> dict:
    """고회전(HTVR) 자격 판정 + 하위분류.

    반환 dict:
      eligible          — 기본 자격 충족 여부 (표준 컷이 WARNING 으로 강등되는 조건)
      turnover_ok / returns_ratio_ok / horizon_ok — 개별 관문
      gaps              — 미달 관문 이름 리스트 ('turnover'|'returns_ratio'|'pnl_horizon')
      classes           — 획득한 하위분류 set ('after_cost'|'investable'|'liquid'|'orthogonal')
      measured          — HT 체크가 실제로 측정된 알파인가 (미측정이면 추정 불가)

    ⚠ HT 분류는 **USA 리전 전용**이다 (문서 "Base Eligibility Criteria: Region: USA").
      GLB High Turnover Theme 은 별개다 — 그쪽은 GLB 에서 돌려야 테마 배수를 받지만,
      그 대신 이 강등 혜택은 못 받는다(표준 D1 컷을 그대로 통과해야 한다).
    """
    m = metrics or {}
    out = {'eligible': False, 'turnover_ok': False, 'returns_ratio_ok': False,
           'horizon_ok': False, 'gaps': [], 'classes': set(), 'measured': False,
           'region_ok': str(region or HT_REGION).strip().upper() == HT_REGION}

    turnover = _f(m.get('ht_turnover'))
    if turnover is None:
        turnover = _f(m.get('turnover'))
    ratio = _f(m.get('ht_returns_ratio'))
    horizon = _f(m.get('ht_pnl_horizon'))
    out['measured'] = (ratio is not None or horizon is not None)

    out['turnover_ok'] = bool(turnover is not None and turnover > HT_TURNOVER_MIN)
    out['returns_ratio_ok'] = bool(ratio is not None and ratio > HT_RETURNS_RATIO_MIN)
    out['horizon_ok'] = bool(horizon is not None and horizon < HT_PNL_HORIZON_MAX)

    if not out['turnover_ok']:
        out['gaps'].append('turnover')
    # 수익보존·지평은 **OR** 조건이다 (PnL Realization Horizon 문서).
    if not (out['returns_ratio_ok'] or out['horizon_ok']):
        # 둘 다 미달이면 '수익보존' 을 대표 사유로 쓴다(직접 겨냥 가능한 레버).
        # 지평만 측정된 알파는 그쪽을 사유로 남긴다.
        out['gaps'].append('pnl_horizon' if (ratio is None and horizon is not None)
                           else 'returns_ratio')
    out['eligible'] = bool(out['region_ok'] and out['turnover_ok']
                           and (out['returns_ratio_ok'] or out['horizon_ok']))

    # 강등(waiver) 가능성 — 분류 자격 + 후비용 Sharpe > 0. 위 HT_WAIVER_* 실측 근거 참조.
    ac = after_cost_sharpe(m)
    out['after_cost_sharpe'] = ac
    out['quality_ok'] = bool(ac is not None and ac > HT_WAIVER_AFTER_COST_MIN)
    out['waiver_likely'] = bool(out['eligible'] and out['quality_ok'])

    # ── 하위분류 4종 ────────────────────────────────────────────────────────
    ac = _f(m.get('ht_after_cost_sharpe'))
    if ac is not None and ac > HT_AFTER_COST_SHARPE_MIN:
        out['classes'].add('after_cost')
    for sh_k, to_k in (('ht_maxtrade_sharpe', 'ht_maxtrade_turnover'),
                       ('ht_maxpos_sharpe', 'ht_maxpos_turnover')):
        sh, to = _f(m.get(sh_k)), _f(m.get(to_k))
        if (sh is not None and sh > HT_INVESTABLE_SHARPE_MIN
                and to is not None and to > HT_INVESTABLE_TURNOVER_MIN):
            out['classes'].add('investable')
    t200, ratio_l = _f(m.get('ht_top200_sharpe')), _f(m.get('ht_liquid_ratio'))
    if (t200 is not None and t200 > HT_LIQUID_TOP200_SHARPE_MIN
            and ratio_l is not None and ratio_l > HT_LIQUID_RATIO_MIN):
        out['classes'].add('liquid')
    if str(m.get('neutralization') or '').strip().upper() == HT_ORTHOGONAL_NEUTRALIZATION:
        out['classes'].add('orthogonal')
    return out


def ht_progress(metrics) -> float:
    """고회전 경로로 **제출 가능해지기까지의** 진척도 [0,1]. 1.0 = 강등 요건 충족.

    세 관문의 **가장 나쁜 쪽**이 점수를 정한다 — 병목을 고쳐야만 점수가 오른다:
      ① 회전율 > 20%              (분류 기본 자격)
      ② 수익보존 > 0.75 OR 지평 < 20일  (분류 기본 자격, 둘 중 좋은 쪽)
      ③ 품질 하한 (Sharpe ≥ 1.5, 후비용 Sharpe > 0)  ← HT_WAIVER_* 실측 근거 참조

    ③ 을 빠뜨리면 GA 가 '회전율만 올리면 제출된다' 고 착각해 Sharpe 를 통째로 포기한다
    (실제로 라이브에서 회전율 61%·Sharpe -0.44 알파가 HT 분류를 받고도 FAIL 3개였다).
    그래도 표준 경로(D0 Sharpe 2.69 + Fitness 1.5)보다는 훨씬 싼 문이다.
    """
    m = metrics or {}
    turnover = _f(m.get('ht_turnover'))
    if turnover is None:
        turnover = _f(m.get('turnover'))
    if turnover is None:
        return 0.0
    # 회전율 항: 20% 에서 1.0, 그 위는 유지(단 70% 초과는 제출 자체가 막히므로 감쇠).
    if turnover >= TURNOVER_MAX:
        t_term = 0.0
    elif turnover > HT_TURNOVER_MIN:
        t_term = 1.0
    else:
        t_term = _clamp(turnover / HT_TURNOVER_MIN)

    # ③ 후비용 Sharpe > 0 — 강등의 유일한 판별자(실측 6/6).
    #    **부호가 넘어가는 순간 만점**이다 (+0.21 짜리도 실제로 강등됐다). 그 아래로는
    #    0 까지의 거리를 램프로 줘서 GA 가 올라올 방향을 알게 한다.
    ac = after_cost_sharpe(m)
    if ac is None:
        # 수익률/마진이 없어 비용을 판단할 수 없다 — '모름' 이지 '나쁨' 이 아니다.
        # 0 으로 두면 지표가 덜 실린 레거시 행이 통째로 부모 자격을 잃고,
        # 1 로 두면 검증도 없이 강등을 낙관하게 된다. 중립값 0.5.
        w_term = 0.5
    elif ac > HT_WAIVER_AFTER_COST_MIN:
        w_term = 1.0
    else:
        w_term = _clamp(ac + 1.0) * 0.9

    ratio = _f(m.get('ht_returns_ratio'))
    horizon = _f(m.get('ht_pnl_horizon'))
    if ratio is None and horizon is None:
        # 미측정 — 세 관문 중 회전율·품질만 검증된 상태다. 레거시 행(브라우저 시대)이
        # 여기 해당한다. 0.5 를 곱해 '일부만 확인됨' 으로 취급한다 — 미측정을 자격
        # 충족처럼 읽으면 옛 화석이 시드 상위를 먹는다.
        return min(t_term, w_term) * 0.5
    terms = []
    if ratio is not None:
        terms.append(_clamp(ratio / HT_RETURNS_RATIO_MIN))
    if horizon is not None and horizon > 0:
        terms.append(_clamp(HT_PNL_HORIZON_MAX / horizon))
    q_term = max(terms) if terms else 0.0     # OR 조건 → 좋은 쪽
    return min(t_term, q_term, w_term)


def standard_progress(metrics, delay=None) -> float:
    """표준 컷(Sharpe·Fitness)까지의 진척도 [0,1]. 두 병목 중 나쁜 쪽."""
    m = metrics or {}
    d = delay if delay is not None else m.get('_delay')
    cut = cutoffs(d)
    # 실측 컷이 체크에서 승격돼 있으면 그쪽이 권위 (WQB 가 규칙을 또 바꿔도 따라간다).
    # 키 규칙: harvest 가 value 를 'sharpe_check', limit 을 'sharpe_check_cutoff' 로 넣는다.
    c_sharpe = _f(m.get('sharpe_check_cutoff')) or cut['sharpe']
    c_fitness = _f(m.get('fitness_check_cutoff')) or cut['fitness']
    sharpe = _f(m.get('sharpe')) or 0.0
    fitness = _f(m.get('fitness')) or 0.0
    s = _clamp(sharpe / c_sharpe) if c_sharpe > 0 else 0.0
    f = _clamp(fitness / c_fitness) if c_fitness > 0 else 0.0
    return min(s, f)


def submittability(metrics, delay=None, region=HT_REGION) -> float:
    """제출 가능성 [0,1] — **두 경로 중 가까운 쪽**.

    HT 경로(회전율·지평)와 표준 경로(Sharpe·Fitness)는 서로 대체재다. HT 자격을 얻으면
    표준 컷이 WARNING 으로 강등되므로, GA 는 둘 중 **싼 쪽**으로 밀면 된다.
    """
    return max(ht_progress(metrics), standard_progress(metrics, delay))


def combine_theme_multipliers(multipliers) -> float:
    """복수 테마 동시 매칭 시 최종 배수 = sum − count + 1 ("Multiplier Rules").
    빈 리스트면 1.0."""
    vals = []
    for v in multipliers or []:
        f = _f(v)
        if f is not None and f > 0:
            vals.append(f)
    if not vals:
        return 1.0
    return max(1.0, sum(vals) - len(vals) + 1.0)


def payout_multiplier(metrics) -> float:
    """이 알파가 받을 배수 ≈ 피라미드 배수 × 테마 배수. 미측정이면 1.0.

    피라미드(MATCHES_PYRAMID.multiplier)와 테마(MATCHES_THEMES)는 서로 다른 축이라
    곱으로 본다. 테마는 매칭됐을 때만(harvest 가 result=PASS 인 경우에만 승격) 값이 있다.
    """
    m = metrics or {}
    pyr = _f(m.get('pyramid_multiplier')) or 1.0
    thm = _f(m.get('theme_multiplier')) or 1.0
    return max(1.0, pyr) * max(1.0, thm)
