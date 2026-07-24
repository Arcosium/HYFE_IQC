"""
server/reward.py — Composite reward scalar for HYFE_IQC alpha evaluation.

Purpose
-------
Single tunable scalar used downstream for:
  - Bandit arm weight updates
  - Mutation-parent selection
  - Seeding rank

Design goals
------------
- Rewards SUBMITTABLE, decorrelated, **고회전(HTVR) 대역** 알파 (2026-07-21 방향 전환).
- Does NOT reward raw Sharpe in isolation.
- 실수령 배수(피라미드×테마)와 HT 하위분류를 명시적으로 보상한다.
- Bakes in the real WQB submission bar (차단 FAIL 0 + self-corr ≤ 0.7).
- Pure: no IO, no DB, no side-effects. stdlib only.

2026-07-21 전면 개편 — 제출 규칙이 바뀌었다
--------------------------------------------
WQB 컨설턴트 기준이 개편되면서 **회전율이 제출 가능성의 1차 결정 변수**가 됐다.
고회전(HTVR) 분류(회전율>20% + 수익보존>0.75 or PnL지평<20일)를 얻으면 표준 컷
(Sharpe/Fitness/2Y/Cluster)이 전부 WARNING 으로 강등돼 FAIL 0 = 제출 가능이 된다
(2026-07-21 라이브 A/B 실측 — criteria.py 헤더 참조).

그런데 이 파일의 옛 보상은 정확히 **반대 방향**이었다: turnover_term 이 회전율을
낮출수록 점수를 줬다(0.125 아래 평평, 0.7 에서 0). 라이브 GA 의 turnover 중앙값은
9.6% 로 HT 문턱(20%)의 절반도 안 됐고, 그래서 개편 후 규칙에서 **구조적으로 제출
불가능한 알파만 양산**하고 있었다. 아래 route/turnover 항이 그 방향을 뒤집는다.

Formula (when all gates pass)
------------------------------
  base = w_route      * route_term       # ← 제출까지의 거리 (HT·표준 중 가까운 쪽)
       + w_multiplier * multiplier_term  # ← 피라미드×테마 배수 (실수령액)
       + w_sharpe     * norm_sharpe
       + w_fitness    * norm_fitness
       + w_returns    * norm_returns
       + w_turnover   * turnover_term    # ← HT 대역(20~70%) 안이면 만점
       + w_htclass    * htclass_term     # ← After Cost/Investable/Liquid/Orthogonal

  norm_sharpe     = clamp(sharpe  / SHARPE_REF,  0, 1)   # ref = 3.0
  norm_fitness    = clamp(fitness / FITNESS_REF, 0, 1)   # ref = 2.0
  norm_returns    = clamp(returns / RETURNS_REF, 0, 1)   # ref = 0.3
  route_term      = max(criteria.ht_progress, criteria.standard_progress)  # 둘 중 싼 쪽
  multiplier_term = (payout_multiplier - 1) / (MULTIPLIER_REF - 1) clamp[0,1]
  turnover_term   = 1.0 in [0.20, 0.65]; 양끝은 램프, 0.70 초과는 0 (제출 차단)
  htclass_term    = 획득한 HT 하위분류 수 / 4

  self_corr penalty:
    corr ≤ 0.3          → penalty = 0.0
    0.3 < corr ≤ 0.7   → penalty = 0.3 * (corr - 0.3) / 0.4   (linear, up to 0.3)
    corr > 0.7          → reward = 0.0  (cannot be submitted)

  reward = max(0.0, base - penalty)

Gates (return 0.0 immediately)
-------------------------------
  1. 차단 게이트: fail_count > 0  OR  error_count > 0  OR  평가된 체크가 없음
     ⚠ 옛 게이트는 `pass_count >= 7` 이었는데 이건 2026-07 개편 후 **치명적 버그**다.
       HT 분류를 얻은 알파는 표준 컷이 WARNING 으로 빠지면서 core PASS 가 4개
       (LOW_TURNOVER·HIGH_TURNOVER·CONCENTRATED_WEIGHT·LOW_SUB_UNIVERSE_SHARPE)밖에
       안 남는다 — 즉 **제출 가능한 알파가 정확히 보상 0 을 받았다**.
       (라이브 gJ9qkKWv: FAIL 0 / PASS 4 / PENDING 6 → 옛 게이트로는 reward=0.0)
  2. too-good guard: sharpe > sharpe_overfit  OR  returns > 0.5  (likely lookahead/overfit)

2026-07-14 개편 — 왜 Sharpe 가 1.0 에서 멈췄나 (이력)
------------------------------------------------------
세 가지가 동시에 GA 를 잘못된 최적점으로 끌고 갔다.

1. **turnover 바닥 추격 (가장 해로웠음).** WQB Fitness 정의는
       Fitness = Sharpe · sqrt(|Returns| / max(Turnover, 0.125))
   즉 turnover 가 **12.5% 밑으로 내려가면 Fitness 는 전혀 개선되지 않는다** (분모가
   0.125 로 바닥친다). 그런데 옛 turnover_term = 1 - turnover/0.7 은 3% → 5% 든
   0.5% → 0.3% 든 계속 가산점을 줬다. 라이브 알파의 turnover 는 실제로 ~3% 까지
   내려갔고(=바닥 한참 아래), 그 대가로 신호가 약해져 returns 가 2~4% 에 묶였다.
   → floor 아래는 **평평하게** 만들어 그 그래디언트를 없앤다.
2. **게이트 통과 후 선택압 소멸.** 로컬 게이트(Sharpe 1.25)를 넘는 순간 보상이
   포화돼 실제 제출컷(RC D1 Sharpe 1.58 / Fitness 1.0)까지 밀 유인이 없었다.
   → submit_term 이 '두 병목 중 나쁜 쪽' 을 min() 으로 잡아 계속 당긴다.
3. **2Y Sharpe 를 보지 못함.** 라이브 제출 거절 1위가 LOW_2Y_SHARPE 인데 그 값은
   IS 요약이 아니라 check 안에만 있어 보상에 한 번도 들어온 적이 없다.
   → wqb_api.harvest_alpha 가 metrics['sharpe_2y'] 로 승격하고, 여기서 가중한다.
"""

# Python 3.9 런타임(서버 = /usr/bin/python3 = 3.9) 호환: 시그니처의 `dict | None`
# 같은 PEP604 union 을 지연 평가(문자열)로 처리해 import 시 TypeError 를 방지한다.
from __future__ import annotations

from . import criteria as _criteria

# ── Tunable module-level reference constants ─────────────────────────────────
SHARPE_REF: float = 3.0   # denominator for norm_sharpe
FITNESS_REF: float = 2.0  # denominator for norm_fitness
RETURNS_REF: float = 0.3  # denominator for norm_returns

# 배수 정규화 기준. USA/D0/PV 피라미드 1.6 × GLB 고회전 테마 2~3 배가 현실적 상한대라
# 3.0 을 만점으로 둔다 (그 이상은 포화).
MULTIPLIER_REF: float = 3.0

# 하위호환용 별칭 — 옛 호출부/테스트가 참조한다. 실제 컷은 criteria.CUTOFFS(delay별)가
# 단일 진실이며, 그마저도 실측 cutoff(metrics['sharpe_cut'])가 있으면 그쪽이 이긴다.
SUBMIT_SHARPE_REF: float = _criteria.CUTOFFS['1']['sharpe']    # 1.58 (D1)
SUBMIT_FITNESS_REF: float = _criteria.CUTOFFS['1']['fitness']  # 1.0  (D1)
SHARPE_2Y_REF: float = _criteria.CUTOFFS['0']['ladder_fail']   # 2.69 (D0)

# WQB Fitness = Sharpe·sqrt(|Returns| / max(Turnover, 0.125)) 의 분모 바닥.
TURNOVER_FLOOR: float = 0.125

# ── Default component weights (must sum to 1.0) ──────────────────────────────
# 2026-07-21 전면 재배분. 옛 배분(sharpe .26 / submit .20 / turnover .09 …)은 '회전율은
# 낮을수록 좋다' 는 전제 위에 있었는데 그 전제가 규칙 개편으로 뒤집혔다.
#   route      — 제출 가능해지는 것이 압도적 1순위. HT 경로와 표준 경로 중 **싼 쪽**까지의 거리.
#   multiplier — 같은 제출이라도 실수령액이 1.1배~3배로 갈린다(피라미드×테마).
#   htclass    — After Cost/Investable/Liquid/Orthogonal 하위분류(테마 3배의 열쇠).
#   sharpe     — 여전히 필요하다: self/prod 상관 거절 시 'Sharpe 10% 우위' 가 유일한 예외이고,
#                하루 4개뿐인 제출 예산을 어디 쓸지 정하는 품질 지표이기도 하다.
#   margin     — 강등의 **유일한 판별자**(후비용 Sharpe > 0)를 GA 가 직접 겨냥할 수 있는
#                형태로 준 것. 2026-07-22 신설: 이게 없어서 GA 는 회전율만 올리면
#                제출된다고 학습했다(상위 알파 6개가 전부 회전율 122~144%).
DEFAULT_WEIGHTS: dict = {
    'route':      0.30,
    'margin':     0.12,
    'multiplier': 0.12,
    'sharpe':     0.12,
    'fitness':    0.08,
    'returns':    0.08,
    'turnover':   0.08,
    'htclass':    0.10,
}

# 회전율이 차단 컷(70%)을 넘었을 때 총점을 깎는 세기. 값이 클수록 급하게 깎인다.
# 8.0 → 75%:×0.71 · 100%:×0.29 · 143%:×0.15
OVERSHOOT_PENALTY_K: float = 8.0

# 게이트가 요구하는 **최소 평가 체크 수**. 옛 값은 7 이었는데, 개편 후 HT 알파는
# core PASS 가 4개뿐이라 7 을 요구하면 제출 가능한 알파가 통째로 0점을 받는다.
MIN_EVALUATED_PASSES: int = 1


# ── Internal helpers ──────────────────────────────────────────────────────────

def _f(v) -> float:
    """
    Tolerant float coercion.
    - bool → 0.0  (avoids True==1 / False==0 accidents)
    - None → 0.0
    - str  → float(str) if parseable, else 0.0
    - '9.45%' / '232.36‱' / '12bp' → 단위를 풀어서 0.0945 / 0.023236 / 0.0012

    ⚠ 단위 접미사 처리는 장식이 아니다. 브라우저(Playwright) 시대의 알파는 지표를
      **퍼센트 문자열**로 저장했고(`turnover: "9.45%"`, `returns: "43.54%"`), REST API
      시대는 분수로 저장한다(`"0.0972"`). 접미사를 못 읽으면 float() 가 ValueError 를
      내고 조용히 0.0 이 된다 → 레거시 알파의 returns/turnover 가 통째로 '0' 으로
      읽힌다. 명예의 전당(db.hall_of_fame_seeds)이 그 옛 행을 다시 시드로 올리는
      지금은 곧바로 점수 왜곡이다(2026-07-14 발견). wqb_backend._parse_metric_number
      와 같은 규칙을 쓴다.
    """
    v = _fopt(v)
    return 0.0 if v is None else v


def _fopt(v):
    """_f 와 같되 '값 없음' 을 0.0 이 아니라 None 으로 구분한다 (미측정 ≠ 0점)."""
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


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _turnover_term(turnover: float, cap: float = _criteria.TURNOVER_MAX) -> float:
    """회전율 대역 항 — **고회전 대역(20~70%) 안이면 만점**.

    2026-07-21 방향 전환. 옛 공식은 회전율이 낮을수록 점수를 줬는데(0.125 아래 평평),
    개편된 규칙에서 그건 '제출 불가능한 알파를 만들라' 는 지시와 같다: HT 분류
    문턱이 20% 이고, 그 분류를 못 얻으면 D0 기준 Sharpe 2.69·Fitness 1.5 라는
    사실상 도달 불가능한 컷을 정면으로 뚫어야 한다.

    구간: [0, 0.20) 선형 상승 · [0.20, 0.65] 만점 · (0.65, 0.70) 급감 · ≥0.70 은 0
    (0.70 초과는 HIGH_TURNOVER FAIL = 제출 차단이므로 대역 상단은 피해야 한다).
    """
    t = max(0.0, float(turnover or 0.0))
    hi = cap if cap and cap > 0 else _criteria.TURNOVER_MAX
    safe_hi = hi * 0.93           # 0.70 대비 0.65 — 컷 바로 밑에 붙지 않게 여유를 둔다
    lo = _criteria.HT_TURNOVER_MIN
    if t >= hi:
        return 0.0
    if t <= 0:
        return 0.0
    if t < lo:
        return _clamp(t / lo, 0.0, 1.0)
    if t <= safe_hi:
        return 1.0
    return _clamp((hi - t) / (hi - safe_hi), 0.0, 1.0)


def _overshoot_factor(turnover: float, cap: float = _criteria.TURNOVER_MAX) -> float:
    """회전율이 차단 컷(70%)을 넘은 알파의 **총점 감쇄 계수** (0, 1].

    ⚠ 이게 없으면 GA 는 회전율 143% 에서 내려올 이유가 없다. 컷 위에서는
    `_turnover_term` 도 `criteria.ht_progress` 도 **똑같이 0** 이라 71% 와 143% 가
    동점인데, 그 사이 sharpe/returns/htclass 항은 회전율이 높을수록 잘 나오기
    때문이다 — 즉 컷 위쪽은 오르막이었다 (2026-07-22 실측: 상위 알파 6개가 전부
    회전율 122~144%, 전원 HIGH_TURNOVER FAIL).

    초과분에 단조 감소하는 계수를 곱해 **내려오는 기울기**를 만든다. 0 으로 만들지
    않는 이유: 완전히 0 이면 다시 평평해져 방향을 잃는다.
    """
    hi = cap if cap and cap > 0 else _criteria.TURNOVER_MAX
    t = max(0.0, float(turnover or 0.0))
    if t <= hi:
        return 1.0
    return 1.0 / (1.0 + OVERSHOOT_PENALTY_K * (t - hi))


def damp_if_positive(value: float, turnover: float,
                     cap: float = _criteria.TURNOVER_MAX) -> float:
    """회전율 초과 감쇄를 **양수 지표에만** 적용한다 (selection.obj_vector 용 공개 헬퍼).

    ⚠ 음수까지 곱하면 감쇄가 **개선**이 된다 — Sharpe −0.46 짜리가 회전율을 올려
    −0.07 로 '좋아지는' 뒤집힘이 생긴다. 나쁜 알파는 나쁜 채로 둔다.
    """
    v = float(value or 0.0)
    if v <= 0:
        return v
    return v * _overshoot_factor(turnover, cap)


def _margin_term(turnover: float, returns: float) -> float:
    """후비용 마진 항 — 강등선까지의 거리 [0,1]. 1.0 = 후비용 Sharpe > 0 도달.

    강등 조건 `후비용 Sharpe > 0` 은 닫힌 형태로 `returns > 0.1512 × turnover` 다
    (`criteria.required_returns`). 그 비율을 그대로 점수로 준다.

    핵심은 **분모에 회전율이 들어간다**는 것이다 — 회전율만 올리는 전략은 여기서
    자동으로 처벌받는다(회전율 143% 면 수익률 21.7% 를 내야 1.0 이 된다). 반대로
    2026-07-21 강등 성공 5건(회전율 40~62% · 수익률 7.5~11.4%)은 전부 1.0 이다.
    """
    need = _criteria.required_returns(turnover)
    if not need or need <= 0:
        return 0.0
    return _clamp(float(returns or 0.0) / need, 0.0, 1.0)


def _route_term(metrics: dict) -> float:
    """제출까지의 거리 — HT 경로와 표준 경로 중 **가까운 쪽**. criteria 가 단일 진실."""
    return _clamp(_criteria.submittability(metrics), 0.0, 1.0)


def _multiplier_term(metrics: dict) -> float:
    """실수령 배수(피라미드×테마)를 [0,1] 로. 배수 1.0(=기본)이면 0점."""
    mult = _criteria.payout_multiplier(metrics)
    if MULTIPLIER_REF <= 1.0:
        return 0.0
    return _clamp((mult - 1.0) / (MULTIPLIER_REF - 1.0), 0.0, 1.0)


def _htclass_term(metrics: dict) -> float:
    """HT 하위분류(After Cost·Investable·Liquid·Orthogonal) 획득 비율."""
    try:
        return _clamp(len(_criteria.ht_status(metrics).get('classes') or ()) / 4.0, 0.0, 1.0)
    except Exception:
        return 0.0


def _submit_term(sharpe: float, fitness: float) -> float:
    """(하위호환) 표준 컷까지의 거리 — D1 기준. 새 코드는 _route_term 을 쓴다."""
    s = _clamp(sharpe / SUBMIT_SHARPE_REF, 0.0, 1.0) if SUBMIT_SHARPE_REF > 0 else 0.0
    f = _clamp(fitness / SUBMIT_FITNESS_REF, 0.0, 1.0) if SUBMIT_FITNESS_REF > 0 else 0.0
    return min(s, f)


def _stability_term(metrics: dict, sharpe: float) -> float:
    """2Y Sharpe. HT 분류를 얻으면 이 체크도 WARNING 으로 강등돼 차단 요인은 아니지만,
    **표준 경로**에서는 여전히 관문이다.

    목표치는 criteria.stability_target 이 정한다 — delay(D0 3.96 / D1 2.38)·회전율
    30% 미만 할인(×0.85)·단일데이터셋 여부까지 반영한 값이라, 옛 고정 상수(2.69)보다
    알파마다 정확하다. 미측정이면 IS Sharpe 를 대리로 쓴다(엘리트 풀 몰살 방지).
    """
    v = _fopt(metrics.get('sharpe_2y'))
    if v is None:
        return _clamp(sharpe / SHARPE_REF, 0.0, 1.0)
    try:
        cut = _criteria.stability_target(metrics)
    except Exception:
        cut = SHARPE_2Y_REF
    if not cut or cut <= 0:
        cut = SHARPE_2Y_REF
    return _clamp(v / cut, 0.0, 1.0)


def _base_score(metrics: dict, w: dict, sharpe: float, fitness: float,
                turnover: float, returns: float, turnover_cap: float,
                turnover_term: float) -> float:
    """8항 가중합 × 회전율 초과 감쇄 — compute_reward 와 selection_score 의 단일 진실.

    감쇄를 **여기**서 곱하는 이유: 보상(다음 세대 방향)과 선택(부모 자격) 양쪽에
    같이 걸려야 한다. 한쪽만 걸면 회전율 폭주 알파가 부모로 계속 살아남는다.
    """
    score = (
        w.get('route',      DEFAULT_WEIGHTS['route'])      * _route_term(metrics)
        + w.get('margin',     DEFAULT_WEIGHTS['margin'])     * _margin_term(turnover, returns)
        + w.get('multiplier', DEFAULT_WEIGHTS['multiplier']) * _multiplier_term(metrics)
        + w.get('sharpe',     DEFAULT_WEIGHTS['sharpe'])     * _clamp(sharpe / SHARPE_REF, 0.0, 1.0)
        + w.get('fitness',    DEFAULT_WEIGHTS['fitness'])    * _clamp(fitness / FITNESS_REF, 0.0, 1.0)
        + w.get('returns',    DEFAULT_WEIGHTS['returns'])    * _clamp(returns / RETURNS_REF, 0.0, 1.0)
        + w.get('turnover',   DEFAULT_WEIGHTS['turnover'])   * turnover_term
        + w.get('htclass',    DEFAULT_WEIGHTS['htclass'])    * _htclass_term(metrics)
    )
    return score * _overshoot_factor(turnover, turnover_cap)


# ── Public API ────────────────────────────────────────────────────────────────

def compute_reward(
    metrics: dict,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    error_count: int = 0,
    self_corr=None,
    weights: dict | None = None,
    turnover_cap: float = _criteria.TURNOVER_MAX,
    pass_threshold: int = MIN_EVALUATED_PASSES,
    sharpe_overfit: float = 5.0,
) -> float:
    """
    Compute the composite reward scalar for a single alpha result.

    Parameters
    ----------
    metrics : dict
        Scraped WQB metrics. Keys used: 'sharpe', 'fitness', 'turnover', 'returns'.
        Values may be str, int, float, or None; tolerantly coerced.
    pass_count : int
        Number of WQB IS-test checks that passed.
    fail_count : int
        Number of failed checks. Any fail → 0.0.
    error_count : int
        Number of errored checks. Any error → 0.0.
    self_corr : float | str | None
        Self-correlation (Maximum) from the WQB correlation panel.
        None = not measured; no penalty applied.
    weights : dict | None
        Override specific weight keys over DEFAULT_WEIGHTS (partial OK).
    turnover_cap : float
        HIGH_TURNOVER FAIL 문턱. Default 0.7 (70 %).
    pass_threshold : int
        차단 게이트가 요구하는 최소 PASS 수. **Default 1** (구 7 은 개편 후 오작동 —
        모듈 docstring 의 '차단 게이트' 항목 참조).
    sharpe_overfit : float
        Sharpe above this value triggers too-good guard. Default 5.0.

    Returns
    -------
    float
        Reward in [0.0, ~1.0].  Always non-negative.
    """
    # ── 1. Merge weights ────────────────────────────────────────────────────
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    # ── 2. Coerce raw metric values ─────────────────────────────────────────
    sharpe   = _f(metrics.get('sharpe'))
    fitness  = _f(metrics.get('fitness'))
    turnover = _f(metrics.get('turnover'))
    returns  = _f(metrics.get('returns'))

    # ── 3. Too-good-to-be-true guard ────────────────────────────────────────
    if sharpe > sharpe_overfit or returns > 0.5:
        return 0.0

    # ── 4. 차단 게이트 — FAIL/ERROR 가 하나라도 있으면 제출 불가 ────────────────
    # WARNING 은 세지 않는다: 2026-07 개편에서 HT 분류를 얻은 알파의 표준 컷이 전부
    # 여기로 강등되며, 그 알파야말로 지금 우리가 원하는 것이다.
    all_pass = (fail_count == 0 and error_count == 0 and pass_count >= pass_threshold)
    if not all_pass:
        return 0.0

    # ── 5-6. Base score (7항: route·multiplier·sharpe·fitness·returns·turnover·htclass) ──
    base = _base_score(metrics, w, sharpe, fitness, turnover, returns, turnover_cap,
                       _turnover_term(turnover, turnover_cap))

    # ── 7. Self-correlation penalty ──────────────────────────────────────────
    penalty = 0.0
    if self_corr is not None:
        corr = _f(self_corr)
        if corr > 0.7:
            # Heavy penalty: above submission gate → 0
            return 0.0
        elif corr > 0.3:
            # Linear penalty: 0 at 0.3 → 0.3 at 0.7
            penalty = 0.3 * (corr - 0.3) / 0.4
        # else: corr <= 0.3 → no penalty

    # ── 8. Final reward ──────────────────────────────────────────────────────
    return float(max(0.0, base - penalty))


# ── Bandit reward (dense) ─────────────────────────────────────────────────────

BANDIT_DENSE_WEIGHT: float = 0.3
"""selection_score(무게이트, 조밀) 가 밴딧 보상에 섞이는 비중. 나머지는 gated."""


def bandit_reward(
    metrics: dict,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    error_count: int = 0,
    self_corr=None,
    weights: dict | None = None,
    turnover_cap: float = _criteria.TURNOVER_MAX,
    pass_threshold: int = MIN_EVALUATED_PASSES,
    sharpe_overfit: float = 5.0,
) -> float:
    """밴딧 arm 갱신용 보상 = (1-w)·compute_reward + w·selection_score.

    compute_reward 만 쓰면 all-pass 게이트 탓에 제출 가능 알파가 없는 구간에서
    모든 arm 이 0.0 만 받아 학습이 사실상 멈춘다(희소 보상 — 라이브에서 arm mean
    이 전부 0 근처로 눌리는 원인). selection_score 를 소량(기본 0.3) 섞어
    '거의 통과' 신호로도 arm 이 구분되게 한다. 제출 가능 알파가 나타나면
    0.7 가중의 게이트 보상이 지배하므로 '제출 가능성을 강화한다'는 원래 의미는
    유지된다. 치역은 [0, ~1] 로 동일.
    """
    gated = compute_reward(
        metrics, pass_count=pass_count, fail_count=fail_count,
        error_count=error_count, self_corr=self_corr, weights=weights,
        turnover_cap=turnover_cap, pass_threshold=pass_threshold,
        sharpe_overfit=sharpe_overfit)
    dense = selection_score(
        metrics, pass_count=pass_count, fail_count=fail_count,
        error_count=error_count, self_corr=self_corr, weights=weights,
        turnover_cap=turnover_cap, sharpe_overfit=sharpe_overfit)
    w = BANDIT_DENSE_WEIGHT
    return float((1.0 - w) * gated + w * dense)


# ── Selection fitness (seeding / parent picking) ──────────────────────────────

SELECTION_PASS_WEIGHT: float = 0.2
"""pass 비율이 selection_score 에 기여하는 비중. 나머지 (1-w) 는 지표 base."""

SELFCORR_UNSUBMITTABLE_PENALTY: float = 0.5
"""self-corr > 0.7 (제출 불가) 부모에 물리는 감점. 0.0 이 아닌 이유는 아래 docstring 참조."""


def selection_score(
    metrics: dict,
    *,
    pass_count: int = 0,
    fail_count: int = 0,
    error_count: int = 0,
    self_corr=None,
    weights: dict | None = None,
    turnover_cap: float = 0.7,
    sharpe_overfit: float = 5.0,
) -> float:
    """엘리트 시딩 · focus 부모 선정에 쓰는 **연속** 적합도. [0.0, ~1.0].

    compute_reward() 와의 유일한 차이는 all-pass 게이트가 없다는 것이다. 그 게이트는
    밴딧 보상에는 옳지만(제출 가능한 알파만 arm 을 강화해야 하므로) 선택에 쓰면
    치명적이다: 자식은 거의 언제나 fail 을 하나는 갖고 있어 전원 0.0 이 되고,
    sharpe 1.11 인 자식과 0.02 인 자식이 같은 점수를 받아 **선택압이 소멸한다.**
    (2026-07-11 라이브 진단 — g1 자식 696개 전원 reward=0.0, 엘리트 풀이 184라운드째
     round 66 화석에서 동결, g2 가 단 하나도 태어나지 못함.)

    두 가지 함정을 명시적으로 막는다:
    - 시뮬 지표가 하나도 없는 알파(컴파일 에러·미완료)는 0.0. 안 그러면 turnover 결측이
      turnover_term=1.0 으로 읽혀 죽은 알파가 상위 시드로 올라온다.
    - self-corr > 0.7 은 제출은 못 하지만 **부모로는 여전히 쓸 만하다**(신호 자체는 살아
      있다). compute_reward 처럼 0.0 으로 죽이지 않고 강하게 후순위로 민다.
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update(weights)

    # 시뮬이 아무 지표도 남기지 못한 알파는 선택 후보가 아니다.
    if metrics.get('sharpe') in (None, '') and metrics.get('fitness') in (None, ''):
        return 0.0

    sharpe   = _f(metrics.get('sharpe'))
    fitness  = _f(metrics.get('fitness'))
    turnover = _f(metrics.get('turnover'))
    returns  = _f(metrics.get('returns'))

    # too-good guard (compute_reward 와 동일) — lookahead/overfit 로 의심되는 부모는 배제.
    if sharpe > sharpe_overfit or returns > 0.5:
        return 0.0

    # turnover 결측/0 은 '측정 안 됨' 이지 '완벽' 이 아니다 → 가산점 없음.
    # (측정된 turnover 는 고회전 대역 20~65% 에서 만점 — _turnover_term docstring 참조.)
    turnover_term = (_turnover_term(turnover, turnover_cap)
                     if (turnover > 0 and turnover_cap > 0) else 0.0)

    base = _base_score(metrics, w, sharpe, fitness, turnover, returns, turnover_cap,
                       turnover_term)

    checks = int(pass_count) + int(fail_count) + int(error_count)
    pass_term = (int(pass_count) / checks) if checks > 0 else 0.0
    score = (1.0 - SELECTION_PASS_WEIGHT) * base + SELECTION_PASS_WEIGHT * pass_term

    penalty = 0.0
    if self_corr is not None:
        corr = _f(self_corr)
        if corr > 0.7:
            penalty = SELFCORR_UNSUBMITTABLE_PENALTY
        elif corr > 0.3:
            penalty = 0.3 * (corr - 0.3) / 0.4

    return float(max(0.0, score - penalty))


# ── 제출 가치 (하루 4개 예산 배분용) ──────────────────────────────────────────

SUBMIT_VALUE_WEIGHTS: dict = {
    'multiplier': 0.35,   # 실수령 배수 — 같은 한 칸을 쓸 거면 배수 큰 쪽
    'sharpe':     0.30,   # 상관 거절 시 'Sharpe 10% 우위' 예외의 재료 + 품질
    'htclass':    0.20,   # 하위분류(테마 3배·Osmosis 조합 품질)
    'fitness':    0.15,
}


def submission_value(metrics: dict, *, self_corr=None) -> float:
    """제출 후보의 **가치** [0,1] — 하루 4개뿐인 제출 예산을 어디 쓸지 정하는 점수.

    WQB 컨설턴트는 하루 최대 4개만 제출할 수 있다(Power Pool 문서: "Max 4 alpha
    submissions in a day"). 개편 이후 HT 분류만 얻으면 Sharpe 1.5 짜리도 '제출 가능'
    이 되므로, **제출 가능성만으로 줄을 세우면 예산이 그날 처음 통과한 4개에 낭비**된다.
    그래서 compute_reward(=제출 가능성 중심)와 분리된 축이 필요하다: 이미 제출 가능한
    알파들 사이에서 무엇이 더 값진가.

    self_corr 가 높으면 제출해도 거절될 확률이 높으므로 그만큼 깎는다.
    """
    m = metrics or {}
    sharpe = _f(m.get('sharpe'))
    fitness = _f(m.get('fitness'))
    w = SUBMIT_VALUE_WEIGHTS
    score = (w['multiplier'] * _multiplier_term(m)
             + w['sharpe'] * _clamp(sharpe / SHARPE_REF, 0.0, 1.0)
             + w['htclass'] * _htclass_term(m)
             + w['fitness'] * _clamp(fitness / FITNESS_REF, 0.0, 1.0))
    if self_corr is not None:
        corr = _f(self_corr)
        if corr > 0.7:
            # 제출해도 거절된다 — 'Sharpe 10% 우위' 예외에 걸 만큼 좋지 않으면 무가치.
            score *= 0.2
        elif corr > 0.3:
            score *= 1.0 - 0.5 * (corr - 0.3) / 0.4
    return float(max(0.0, min(1.0, score)))
