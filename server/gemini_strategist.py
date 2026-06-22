"""Gemini 2.5 Flash 로 8개 알파를 한 번에 생성. DB 기반 feedback/errors 사용.

원본 ArcAI.ve/Daily/IQC/gemini_strategist.py 의 알파 생성 로직을 그대로 사용하되:
  - API 키는 user_id 별로 받음 (env 폴백 없음).
  - feedback/errors 는 db.list_feedback / db.list_error_patterns 로부터 주입.
  - prompt cache 는 (model, csv_sig, gemini_key_hash) 키로 user 별 격리.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import logging
from typing import Any, Callable

from google import genai
from google.genai import types as genai_types

from . import datafield_palette
from . import alpha_seeds

LOG = logging.getLogger('hyfe.gemini')

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_CSV = os.path.join(_THIS_DIR, 'brain_operators.csv')
DATAFIELDS_CSV = os.path.join(_THIS_DIR, 'IQC_brain_datafields.csv')

MODEL = 'gemini-2.5-flash'
_FALLBACK_CHAIN_DEFAULT = (
    'gemini-2.5-flash',
    'gemini-flash-latest',
    'gemini-2.5-flash-lite',
)


def _model_chain() -> tuple[str, ...]:
    raw = os.environ.get('IQC_MODEL_CHAIN', '')
    if raw:
        return tuple(m.strip() for m in raw.split(',') if m.strip())
    return _FALLBACK_CHAIN_DEFAULT


SYSTEM_INSTRUCTION = """[역할]
너는 WorldQuant Brain(USA) 에서 **Sharpe ≥ 2.0, Fitness ≥ 1.3**, Turnover 1~70%, Weight 잘 분산(>10% 집중 금지), Sub-universe Sharpe 컷 통과, 그리고 **기존 제출 알파들과의 Self-Correlation ≤ 0.7** 을 동시에 노리는 시니어 퀀트 연구원이다. 이 컷들은 실제 대회 기준이다 — 절대 더 낮게 조준하지 마라.
brain_operators.csv / IQC_brain_datafields.csv 는 **참고용 일부 목록일 뿐 전체가 아니다**. 거기 없어도 아래 [검증된 팔레트]·[핵심 관용구] 에 있거나 WorldQuant 공식 커리큘럼에서 쓰는 연산자·필드는 실재하니 자유롭게 써라.

[🔴 필드 위생 — 필수 (Sharpe ~0.2 의 #1 원인 차단)]
모든 raw 데이터필드는 연산자에 넣기 **전에 반드시** 다음으로 감싼다: winsorize(ts_backfill(FIELD, 120), std=4)
- ts_backfill(FIELD, 120): 결측/희소(펀더멘털·애널리스트·옵션·뉴스 등) 를 과거값으로 채움.
- winsorize(..., std=4): 이상치 스파이크를 클립 — 한 종목/NaN 이 횡단면을 지배해 포트폴리오 변동성이 폭발(=Sharpe 0.2)하는 것을 막는다.
- 이 래퍼를 빼면 거의 항상 Sharpe ~0.2 로 죽는다. close/volume 같은 보편 PV 필드도 winsorize 권장.
- 예: rank(winsorize(ts_backfill(operating_income, 120), std=4) / (winsorize(ts_backfill(assets, 120), std=4) + 0.000001))

[가장 중요 — 상관관계 0.7 벽 깨기]
이 계정은 이미 price/return·기초 펀더멘털 위주의 알파를 30개 넘게 제출했다. 그래서 비슷한 발상은 거의 다 self-corr > 0.7 로 막힌다. **익숙하고 안전한 수식으로 후퇴하는 것이 진짜 실패다.** 매 배치는 서로, 그리고 기존 풀과 달라야 한다.
- **문법 오류를 두려워하지 마라.** 틀린 식별자·새 조합으로 에러가 나도 그 에러는 저장·학습되어 다음 배치를 좋게 만든다. 모르는 필드라도 가설이 좋으면 시도하라 — 잃는 건 시뮬 한 칸, 얻는 건 새 영역이다.
- 매 알파 = 서로 다른 **경제적 가설**. 한 배치 8개의 데이터셋·시간스케일·신호구조·settings 가 골고루 흩어져야 한다. 닮은 알파가 3개를 넘으면 실패다.
- **탈상관 레버 적극 사용**: 같은 신호라도 universe(TOP3000↔TOP500↔TOP200) 나 neutralization(SECTOR↔SUBINDUSTRY↔MARKET) 을 바꾸면 self-corr 가 크게 떨어진다 (공식 커리큘럼 권장). 한 배치 안에서 settings 를 분산하라.
- 잘 안 쓰는 dataset 을 의도적으로 파라: 애널리스트(anl4_*) · 옵션IV(implied_volatility_*) · 현금흐름/배당(cashflow_*, dividend) · 뉴스/소셜(nws12_*, scl12_*, snt_*) · 모델파생(*_rank_derivative, mdl*).

[Fitness 최적화 — 점수 직접 끌어올리기]
WorldQuant Fitness = Sharpe × √(|Returns| / max(Turnover, 0.125)). 레버 우선순위:
  1) 최종 신호를 ts_decay_linear(rank(signal), 5~10) 로 감싸 회전↓·단조성↑ (가장 강력).
  2) rank() 로 스케일 제거·횡단면 비교성 확보 (가장 중요한 연산자).
  3) raw 값 대신 ts_zscore/(x-ts_mean)/ts_std_dev 로 '수준'이 아닌 '변화'를 포착.
  4) 이중 rank: rank(ts_rank(x, 40)) 로 안정성↑.
  5) ts_av_diff(x, 50) * ts_corr(x, y, 50) 에 -1 곱: 구조적으로 유효할 때만 진입하는 상관 게이트.
  6) trade_when(저변동 조건, 신호, -1): 최강 회전 억제기.
측정된 고-Fitness 템플릿(변형해 사용): ts_decay_linear(rank((vwap-close)/close), 5) (~2.86),
rank(ts_rank(close/ts_delay(close,5)-1, 40)) (~1.5), ts_av_diff(close,50)*ts_corr(close,volume,50) 에 -1곱 (~1.70).

[고-Sharpe 핵심 규칙 — 실측 통과 알파의 문법 (반드시 지켜라)]
- **최종(최외곽) 신호는 반드시 rank(...) / group_rank(..., subindustry) / ts_zscore(...) 로 끝낸다.** 안 끝내면 Weight 집중 체크 실패 + 변동성 폭발로 Sharpe 급락. (최종 평활은 decay 연산자 대신 settings 의 decay 키로 — decay 연산자는 중간단계에만.)
- **검증된 스켈레톤 — 배치 절반은 이 변형으로:** [-1*] rank(ts_decay_linear(ts_corr(group_neutralize(A, sector), B, d1), d2)) — A,B 는 서로 다른 **위생래퍼** 필드, d1≈4~6, d2≈6~10. (Kakushadze 101 / 통과 알파 다수의 공통 골격.)
- **2팩터 최소 · 결합 전 각각 표준화:** 스케일 다른 raw 를 직접 +/− 금지. rank(A)*rank(B) 또는 add(ts_zscore(A,252), ts_zscore(B,252), filter=true). **단일팩터 raw 알파 금지(Sharpe ~0.4 천장).**
- **부호:** 대부분 raw 팩터는 역방향(mean-reversion)이 적중률 최고 — momentum/correlation 코어엔 -1* 를 우선 시도. 비슷한 과거 알파가 음수 Sharpe 였으면 버리지 말고 **부호만 뒤집어라**.
- **목표 분포(실제 production median, 과조준 금지):** Sharpe ~2.2(범위 1.9~2.5) · Turnover ~0.48 · 보유 ~2일 · pairwise corr ~15%. Sharpe 4 를 노리지 마라. **Turnover 12.5% 미만으로 과스무딩 금지** — Fitness 바닥 max(Turnover,0.125) 라 그 아래선 회전을 더 줄여도 점수에 무의미하고 신호만 죽인다.

[복잡도 예산 — 과적합 방지]
서로 다른 base 필드 ≤ 6개, 식 길이 과도 금지, 깊은 중첩 금지(≤6단). 설계 원칙: 비율 > 곱 > 합
(rank(A/(B+0.000001)) 가 rank(A)*rank(B) 보다 일반화 잘 됨). 엄격한 == 등호 대신 밴드(±0.1*ts_std_dev) 사용.
단일 연산자 알파(rank(close) 류)는 Sharpe 거의 0 — 최소 2개 차원(가격모멘텀+거래량/변동성)을 결합하라.

[10가지 가설 패밀리 — 배치를 여기에 골고루 분산]
1) PV 모멘텀/리버전: vwap/close, CLV=((close-low)-(high-close))/(high-low), 장기수익률에 delay 준 모멘텀, trade_when(거래량 급증, 신호, -1).
2) 펀더멘털 가치: operating_income/cap(OEY), EBITDA/EV, assets·equity·debt·liabilities 비율, fnd6_* — 분기데이터는 거의 항상 ts_backfill 로 감쌈.
3) 애널리스트 기대: anl4_bvps_mean·anl4_netdebt_mean·anl4_adjusted_netincome_ft·anl4_afv4_eps_mean/high/low, *_rank_derivative, actual_*_value_quarterly 와 애널리스트 추정의 괴리.
4) 현금흐름/수익성 質: cashflow_op/cap, cashflow_efficiency_rank_derivative, ts_corr(cash, cashflow, 252), dividend 추이.
5) 옵션 IV 센티멘트: implied_volatility_call_{만기}-implied_volatility_put_{만기}, IV vs historical_volatility, implied_volatility_mean_skew_*, call_breakeven_*-put_breakeven_*, pcr_vol_*.
6) 뉴스/소셜 센티멘트: scl12_buzz·snt_buzz 대 volume(ts_regression), vec_avg(nws12_*) 로 벡터필드 집계 후 ts_sum/rank.
7) 이벤트 드리븐: abnormal_return_earnings_release, 실적발표 전후 반응.
8) 마이크로구조: volume/adv20, ts_mean(volume,N1)/ts_mean(volume,N2), volume*vwap 흐름.
9) 변동성 레짐 전환(삼항): cap < ts_mean(cap,60) ? 한 신호 : 다른 신호; 고변동성 구간 거래제한 등.
10) 크로스-데이터셋 결합: add(zscore(sigA), ts_zscore(sigB,252), zscore(sigC), filter=True) 처럼 서로 다른 패밀리의 표준화 신호를 합성.

[핵심 구조 관용구 — 적극 활용 (지금까지 안 써서 알파가 빈약했다)]
- ts_backfill(field, N): 펀더멘털·애널리스트·옵션 등 희소/분기 필드의 결측을 과거값으로 채움. 이런 필드엔 **거의 필수** (N=20~250).
- add(a, b, c, ..., filter=True): 여러 표준화 신호를 NaN-안전하게 합성. 다신호 결합의 기본기.
- group_zscore(x, group) / group_rank(x, group) / group_neutralize(x, group): group ∈ sector|industry|subindustry|market. ⚠ group 은 따옴표 없는 bare 식별자다 — group_neutralize(x, sector) (O), group_neutralize(x, 'SECTOR') (X, "Got invalid input at index 1" 에러).
- 커스텀 그룹: bucket(rank(x), range="0,1,0.1") 로 10분위 그룹 생성 → group_neutralize. 이중중립화는 final = g1*10 + g2 한 번에 (group_neutralize 중첩 금지).
- trade_when(entry, alpha, exit): 조건부 진입/유지, exit=-1 이면 청산 없이 모멘텀 유지.
- 삼항 레짐: condition ? signal_if_true : signal_if_false. (returns>0 ? 1 : 0 식 카운팅도 가능)
- vec_avg / vec_sum: 벡터 타입 필드(뉴스 등)를 행렬로 집계해야 다른 연산자에 넣을 수 있음.
- 비선형/직교화: signed_power, winsorize, sign, scale, ts_av_diff, ts_decay_linear, ts_regression(y,x,d).
- 턴오버 억제: hump(x, hump=0.03) 로 작은 포지션 변화를 무시 → 턴오버 직접 감소(특히 delay=0/고회전 알파에 효과적). ⚠ hump 임계값은 반드시 named(hump=)로! positional hump(x, 0.03) 은 입력 2개로 해석돼 에러. 그 외 ts_decay_linear/ts_mean 평활, trade_when(entry, alpha, -1) 로 포지션 유지.

[검증된 데이터필드 팔레트 — 실제 통과 알파/공식 커리큘럼에서 확인됨 (CSV 미수록도 있음)]
- 가격/거래량: close, open, high, low, returns, volume, adv20, vwap, cap.
- 펀더멘털: operating_income, assets, equity, debt, liabilities, cashflow_op, cashflow, cash, dividend, total_assets_reported_value, fnd6_drc, fnd6_newa1v1300_lct, mdl177_vra_qsa_efficiency.
- 애널리스트(matrix): anl4_bvps_mean, anl4_netdebt_mean, anl4_adjusted_netincome_ft, anl4_afv4_eps_mean/high/low, analyst_revision_rank_derivative, cashflow_efficiency_rank_derivative, relative_valuation_rank_derivative, actual_sales_value_quarterly, actual_eps_value_quarterly, abnormal_return_earnings_release.
- 옵션/변동성: implied_volatility_call_{30,60,120,180,360}, implied_volatility_put_{...}, implied_volatility_mean_{N}, implied_volatility_mean_skew_150, historical_volatility_{N}, call_breakeven_120, put_breakeven_120, pcr_vol_120, beta_last_*_days_spy.
- 뉴스/소셜(주로 vector → vec_avg 필요): nws12_prez_result2, nws12_afterhsz_si, scl12_buzz, snt_buzz, pv13_custretsig_retsig.
- 그룹: sector, industry, subindustry, market.

[안전 규칙 — 점수 낭비를 피하기 위한 가이드 (창의성 제한이 아님)]
A) 다음은 사용자 티어에서 확정적으로 'inaccessible'/에러라 점수만 버린다. 피하라: parkinson_volatility(전형태), hl_volatility, ts_returns, realized_volatility, turnover_volatility, ts_decay_exp, ts_median, ts_skewness, ts_kurtosis, ts_co_skewness, ts_co_kurtosis, ts_partial_corr. (분포통계는 ts_std_dev/ts_zscore/ts_rank/ts_arg_max/ts_arg_min 으로 대체.)
B) 과학적 표기법 금지(`1e-6` 등) → `0.000001` 같은 소수로.
C) vector 타입 필드는 **반드시 vec_avg/vec_sum 등 vec_* 로 감싸** 행렬로 바꾼 뒤 사용 (raw 사용 시 'does not support event inputs' 에러). vec_* 로 감싸면 자유롭게 활용 가능.
D) 코드 안 **줄바꿈/탭/주석 문자 금지**. 단, `;` 로 구분한 다중 문장(중간변수 할당)은 한 줄 안에서 적극 권장: `a=...; b=...; add(a,b,filter=true)`.
E) 스케일 다른 raw 값 직접 +/− 금지 → rank/zscore/ts_rank/scale 로 표준화 후 결합. (단 같은 스케일끼리, 예: implied_volatility_call − implied_volatility_put 는 직접 빼도 됨.)
F) 0 분모 가능성 있으면 `+ 0.000001` 더해 안전 처리.

[Sim Settings — 알파마다 다양한 시뮬 조건 시도]
WQB Settings 항목도 통과 여부에 직접 영향을 준다. 각 알파의 가설에 맞는 settings 를 추천하고, 한 배치가 모두 같은 settings 면 안 된다.
추천 가능한 키 (모두 optional, 생략 시 default 사용):
  - region: USA (default) — 사용자 티어상 USA 만 가능. 바꾸지 마라.
  - universe: TOP3000 (default) | TOP1000 | TOP500 | TOP200
        TOP200/500 은 변동성 큰 / 단순 반전 알파, TOP3000 은 cross-sectional rank 류에 유리.
  - delay: 0 | 1 (default 1). delay 0 도 사용 가능하니 일부 알파에 섞어 탈상관하라. 단 delay 0 은 일부 datafield 가 미제공이라 sim 이 실패할 수 있으니, close/open/high/low/volume/vwap/returns 같은 보편 PV 필드 위주 알파에만 0 을 추천.
  - neutralization: NONE | MARKET | INDUSTRY (default) | SUBINDUSTRY | SECTOR
        **데이터타입별 권장**: 가격/거래량→NONE 또는 MARKET, 펀더멘털→INDUSTRY, 애널리스트→INDUSTRY, 뉴스/소셜→SUBINDUSTRY, 옵션→MARKET 또는 SECTOR. 상대 mispricing 에 베팅하고 섹터 beta 를 제거 → Sub-universe Sharpe·Weight 통과에 직접 기여. 코드에 이미 group_neutralize(...) 를 썼다면 NONE/MARKET (이중 중립화 회피).
  - decay: 0~10 정수 (필요하면 그 이상도 가능). 턴오버 높은 알파는 5~10, 일반 rank 류는 0~3. 같은 신호도 decay 만 바꾸면 self-corr 가 떨어지니 배치 안에서 decay 를 적극 분산하라.
  - truncation: **0.08~0.13 기본 권장** (통과 알파들이 수렴하는 값). 0.01 은 hump 저턴오버 특수케이스에만 — 평소 0.01 은 너무 빡빡해 오히려 해롭다.
  - pasteurization: ON (default) | OFF
  - nan_handling: OFF (default) | ON — 결측 많은 fundamental/option 데이터 알파엔 ON 을 적극 섞어 탈상관.

[출력 형식 — 반드시 준수]
JSON 만 출력. 코드 블록(```), 사족 절대 금지. 정확히 8개 객체:
[
  {"code": "<한 줄 알파 수식>",
   "desc": "<한국어 1줄 요약, 60자 이내 — 어떤 가설/데이터/구조인지>",
   "settings": {"universe":"TOP3000", "neutralization":"INDUSTRY", "decay":6, "truncation":0.1}},
  ...총 8개...
]
settings 는 일부 키만 적어도 됨 (생략 키는 default).
8개 알파의 가설 · 데이터소스 · 신호구조 · settings 가 서로 충분히 달라야 한다. 비슷한 알파 반복은 실패다.
🚨 출력 가드(엄수): JSON 배열만. markdown 코드펜스·따옴표 래핑·"분석 결과"/"개선된 알파" 류 사족 절대 금지.
✅ 올바른 응답 시작: [{"code": "...", ...
❌ 잘못: 다음은 제안 알파입니다: [{..."""


_CSV_CACHE: dict[str, tuple[float, str]] = {}
_PROMPT_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_PROMPT_CACHE_TTL_SEC = 3600


class GeminiQuotaError(RuntimeError):
    pass


def _read_csv_text(path: str, max_chars: int = 8000) -> str:
    try:
        mtime = os.path.getmtime(path)
        cached = _CSV_CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()[:max_chars]
        _CSV_CACHE[path] = (mtime, text)
        return text
    except Exception:
        return ''


def _csv_signature() -> str:
    parts = []
    for p in (OPERATORS_CSV, DATAFIELDS_CSV):
        try:
            parts.append(f'{os.path.basename(p)}:{int(os.path.getmtime(p))}')
        except Exception:
            parts.append(f'{os.path.basename(p)}:0')
    sys_hash = hashlib.sha256(SYSTEM_INSTRUCTION.encode('utf-8')).hexdigest()[:8]
    parts.append(f'sysprompt:{sys_hash}')
    return '|'.join(parts)


def _api_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:10]


def _prompt_cache_key(model: str, api_key: str) -> tuple:
    # SYSTEM_INSTRUCTION 해시 포함 → 프롬프트 본문이 바뀌면 캐시 자동 무효화.
    si = hashlib.sha1(SYSTEM_INSTRUCTION.encode('utf-8')).hexdigest()[:8]
    return (model, _csv_signature(), _api_key_hash(api_key), si)


def _get_or_create_prompt_cache(client, model: str, api_key: str,
                                 log_fn: Callable | None = None) -> str | None:
    key = _prompt_cache_key(model, api_key)
    now = time.time()
    cached = _PROMPT_CACHE.get(key)
    if cached and (now - cached[0]) < _PROMPT_CACHE_TTL_SEC:
        return cached[1]

    operators = _read_csv_text(OPERATORS_CSV)
    datafields = datafield_palette.build_palette(
        region='USA',
        delay=None,
        seed=datafield_palette._next_rotation(),
    )
    if not datafields:
        datafields = _read_csv_text(DATAFIELDS_CSV)
    csv_block = (
        '===== brain_operators.csv =====\n' + operators +
        '\n\n===== IQC_brain_datafields.csv =====\n' + datafields
    )
    try:
        cache = client.caches.create(
            model=model,
            config=genai_types.CreateCachedContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                contents=[genai_types.Content(
                    role='user',
                    parts=[genai_types.Part(text=csv_block)],
                )],
                ttl='3600s',
            ),
        )
        name = cache.name if hasattr(cache, 'name') else None
        if not name:
            return None
        _PROMPT_CACHE[key] = (now, name)
        if log_fn:
            log_fn(f'   prompt cache 생성: {name} ({model})')
        return name
    except Exception as e:
        if log_fn:
            log_fn(f'   ⚠ prompt cache 실패 (영향 없음): {str(e)[:120]}')
        return None


_RESPONSE_SCHEMA = genai_types.Schema(
    type=genai_types.Type.ARRAY,
    items=genai_types.Schema(
        type=genai_types.Type.OBJECT,
        properties={
            'code': genai_types.Schema(type=genai_types.Type.STRING),
            'desc': genai_types.Schema(type=genai_types.Type.STRING),
            # Sim settings — optional. 모든 키가 STRING (Number 항목도 문자열 → 후처리에서
            # 그대로 WQB UI 에 입력). Schema 가 모두 optional 이도록 required 에서 제외.
            'settings': genai_types.Schema(
                type=genai_types.Type.OBJECT,
                properties={
                    'region': genai_types.Schema(type=genai_types.Type.STRING),
                    'universe': genai_types.Schema(type=genai_types.Type.STRING),
                    'delay': genai_types.Schema(type=genai_types.Type.STRING),
                    'neutralization': genai_types.Schema(type=genai_types.Type.STRING),
                    'decay': genai_types.Schema(type=genai_types.Type.STRING),
                    'truncation': genai_types.Schema(type=genai_types.Type.STRING),
                    'pasteurization': genai_types.Schema(type=genai_types.Type.STRING),
                    'nan_handling': genai_types.Schema(type=genai_types.Type.STRING),
                },
            ),
        },
        required=['code', 'desc'],
    ),
)


_VALID_SETTING_KEYS = frozenset({
    'region', 'universe', 'delay', 'neutralization', 'decay',
    'truncation', 'pasteurization', 'nan_handling', 'unit_handling',
})


def _normalize_settings(raw) -> dict:
    """Gemini 가 추천한 settings 를 WQB UI 입력에 맞게 정규화.

    - 알 수 없는 키 제거
    - 값을 문자열로 변환 (숫자도 stringify 해서 input 으로 fill)
    - 빈/None 값 제거
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        kk = k.strip().lower()
        if kk not in _VALID_SETTING_KEYS:
            continue
        if v is None:
            continue
        sv = str(v).strip()
        if not sv:
            continue
        out[kk] = sv
    return out


def _compute_signal_stats(feedback: list[dict]) -> dict[str, dict[str, Any]]:
    if not feedback:
        return {}
    patterns = {
        'ts_rank_252': re.compile(r'\bts_rank\([^)]+,\s*252\)'),
        'historical_vol_분모': re.compile(r'/\s*rank\(historical_volatility_'),
        'ts_std_dev_분모': re.compile(r'/\s*rank\(ts_std_dev\('),
        'group_neutralize_sector': re.compile(r'group_neutralize\([^,]+,\s*sector\)'),
        'adv20_보정': re.compile(r'\brank\(adv20\)'),
        'returns_단기모멘텀': re.compile(r'\bts_mean\(returns,\s*[1-9]\)'),
        'returns_중기모멘텀': re.compile(r'\bts_mean\(returns,\s*(?:1[0-9]|20|22)\b'),
        'returns_장기리버설': re.compile(r'-?\s*ts_mean\(returns,\s*(?:60|120|252)'),
        'eps_or_close': re.compile(r'\b(?:actual_eps|anl4_afv4_eps_)\w+\s*/\s*close\b'),
    }
    stats: dict[str, dict[str, Any]] = {}
    n = len(feedback)
    if n == 0:
        return {}
    for name, rx in patterns.items():
        with_p, without_p = [], []
        for f in feedback:
            code = f.get('code') or ''
            p = int(f.get('pass_count') or 0)
            (with_p if rx.search(code) else without_p).append(p)
        if not with_p or not without_p:
            continue
        wa = sum(with_p) / len(with_p)
        woa = sum(without_p) / len(without_p)
        stats[name] = {'with_avg': round(wa, 2), 'without_avg': round(woa, 2),
                        'lift': round(wa - woa, 2),
                        'n_with': len(with_p), 'n_without': len(without_p)}
    return stats


def _parse_metric(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace('%', '').replace('‱', '')
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _select_mutation_seeds(feedback: list[dict], top_k: int = 3) -> list[dict]:
    if not feedback:
        return []
    near = [f for f in feedback if 5 <= int(f.get('pass_count') or 0) <= 6]
    if near:
        near.sort(key=lambda f: -int(f.get('pass_count') or 0))
        return near[:top_k]
    with_sh = []
    for f in feedback:
        sh = _parse_metric((f.get('metrics') or {}).get('sharpe'))
        if sh is not None and sh >= 0.5:
            with_sh.append((sh, f))
    if with_sh:
        with_sh.sort(key=lambda x: -x[0])
        return [f for _, f in with_sh[:top_k]]
    cands = []
    for f in feedback:
        if int(f.get('pass_count') or 0) < 3:
            continue
        sh = _parse_metric((f.get('metrics') or {}).get('sharpe')) or -10.0
        cands.append((sh, f))
    if cands:
        cands.sort(key=lambda x: -x[0])
        return [f for _, f in cands[:top_k]]
    return []


def _build_dynamic_section(round_num: int, feedback: list[dict],
                            errors: list[dict]) -> str:
    parts: list[str] = []
    # 시뮬 stuck 회피 — 통계상 timeout 비율이 높은 operator 들 (관측 결과 8-12배):
    parts.append('===== ⚠ 시뮬 timeout 회피 — 다음 operator 신중 사용 =====')
    parts.append('아래 operator 들은 WQB sim 에서 stuck/timeout 비율이 높음 (관측: trade_when 8x,')
    parts.append('ts_decay_linear 12x, quantile 6x). 강력한 신호가 필요한 경우에만 사용하고,')
    parts.append('가능하면 다음으로 대체:')
    parts.append('  - trade_when(...) → if_else(...) 또는 단순 sign() * signal')
    parts.append('  - ts_decay_linear(x, n) → ts_mean(x, n) 또는 ts_zscore(x, n)')
    parts.append('  - quantile(x, n) → ts_rank(x, n)')
    parts.append('')
    if len(feedback) >= 4:
        stats = _compute_signal_stats(feedback)
        if stats:
            parts.append('===== 학습된 패턴 통계 (포함 시 평균 PASS lift, n=발생 알파 수) =====')
            for name, s in sorted(stats.items(), key=lambda kv: -abs(kv[1]['lift'])):
                lift = s['lift']
                arrow = '↑' if lift > 0 else ('↓' if lift < 0 else '·')
                parts.append(
                    f"- {name}: 사용시 PASS={s['with_avg']} / 미사용 PASS={s['without_avg']} "
                    f"({arrow}{lift:+.2f}, n={s['n_with']}/{s['n_with']+s['n_without']})"
                )
            parts.append('  ↳ lift > 0 인 패턴은 적극 활용, lift < -0.5 인 패턴은 회피.')
            parts.append('')

    seeds = _select_mutation_seeds(feedback, top_k=3)
    if seeds:
        parts.append('===== 돌연변이 시드 (가장 promising 한 직전 알파들 — 1~2개의 큰 변형을 시도) =====')
        for f in seeds:
            m = f.get('metrics') or {}
            sh = _parse_metric(m.get('sharpe'))
            ft = _parse_metric(m.get('fitness'))
            ret = _parse_metric(m.get('returns'))
            metric_parts = []
            if sh is not None: metric_parts.append(f'Sharpe={sh:.2f}')
            if ft is not None: metric_parts.append(f'Fitness={ft:.2f}')
            if ret is not None: metric_parts.append(f'Returns={ret}%')
            metric_str = (f'  [{", ".join(metric_parts)}]') if metric_parts else ''
            parts.append(
                f"- (PASS {f.get('pass_count','?')}/11){metric_str} {f.get('code','')}\n"
                f"    실패 항목: {(', '.join(f.get('fail_items') or []))[:200]}"
            )
        parts.append('  ↳ 약한 변형(윈도우 ±5)이 아니라 큰 변형 시도: archetype 자체를 바꾸거나, 분모 제거,')
        parts.append('     신호 +/- 부호 반전, 곱셈 도입(rank * sign), trade_when 으로 조건부 신호화 등.')
        parts.append('')

    if feedback:
        recent_fb = feedback[-30:]
        parts.append('===== 최근 30회 시뮬레이션 결과 — 코드 + PASS/FAIL + Sharpe/Fitness =====')
        for fb in reversed(recent_fb):
            m = fb.get('metrics') or {}
            sh = _parse_metric(m.get('sharpe'))
            ft = _parse_metric(m.get('fitness'))
            ret = _parse_metric(m.get('returns'))
            metric_parts = []
            if sh is not None: metric_parts.append(f'Sharpe={sh:.2f}')
            if ft is not None: metric_parts.append(f'Fitness={ft:.2f}')
            if ret is not None: metric_parts.append(f'Ret={ret}%')
            metric_str = (' | ' + ', '.join(metric_parts)) if metric_parts else ''
            parts.append(
                f"- 라운드 {fb.get('round','?')} #{fb.get('idx','?')}: "
                f"PASS {fb.get('pass_count',0)}/11, FAIL {fb.get('fail_count',0)}{metric_str}\n"
                f"  코드: {fb.get('code','')}\n"
                f"  ✓성공: {(', '.join(fb.get('pass_items') or []) or '(없음)')[:240]}\n"
                f"  ✗실패: {(', '.join(fb.get('fail_items') or []) or '(없음)')[:240]}"
            )
        parts.append('')

    if errors:
        parts.append('===== 회피해야 할 오류 패턴 (반드시 회피) =====')
        for d in errors[:30]:
            ids = d.get('identifiers') or []
            ids_str = ', '.join(sorted(ids)[:30])
            parts.append(
                f"- ({d.get('count',0)}회 발생) {d.get('pattern','')}\n"
                f"  사용 금지 식별자: {ids_str}"
            )
        parts.append('')

    parts.append('위 학습 자료를 바탕으로 PASS 7개 이상을 노리는 8개 알파를 JSON 으로만 출력하라.')
    return '\n'.join(parts)


def _build_building_blocks_section(seeds: list[dict]) -> str:
    """smilee 정신: 이전 라운드의 best 알파를 'building block' 으로 명시 → Gemini 가 이를
    내부 변수로 사용하거나 변형해 합성하도록 유도. brute-force 가 아닌 *진화적* 탐색.
    """
    if not seeds:
        return ''
    parts = [
        '',
        '===== 🌱 이전 best 알파 (building block 으로 활용) =====',
        '아래는 본 사용자의 직전 라운드들에서 PASS 가 가장 많았던 알파들이다.',
        '이를 그대로 복제하지 말고 — *재료*로 활용해서 새로운 변형을 만들어라:',
        '  · 같은 datafield 조합에 다른 operator 적용',
        '  · 같은 operator 구조에 다른 시간 윈도우 (lookback)',
        '  · 두 building block 을 결합 (rank(A) - rank(B), trade_when(condition, A, B), if_else)',
        '  · group_neutralize 의 group 만 변경 (sector ↔ industry ↔ subindustry)',
        '8개 알파 중 절반 정도는 이 building block 의 변형/조합, 나머지는 새로운 시도가 좋다.',
    ]
    for s in seeds[:5]:
        sh = s.get('_sharpe', 0.0)
        pc = s.get('pass_count', 0)
        rd = s.get('round_num', '?')
        idx = s.get('idx', '?')
        code = (s.get('code') or '').strip()
        if len(code) > 200:
            code = code[:200] + '…'
        parts.append(f'- R{rd}#{idx} (PASS={pc}, Sharpe={sh:.2f}): {code}')
    return '\n'.join(parts)


def _build_preference_section(stats: dict) -> str:
    """smilee 의 ops_picking_prob 정신: 본 사용자의 알파에서 PASS 가 잘 나온 operator
    / datafield 통계 → Gemini 가 그것을 더 자주 사용하도록 가이드.
    """
    if not stats or (not stats.get('top_ops') and not stats.get('top_fields')):
        return ''
    parts = [
        '',
        '===== 📊 본 사용자 학습 통계 (선호 operator / datafield) =====',
    ]
    if stats.get('top_ops'):
        parts.append('직전 알파들에서 PASS 평균이 높았던 operator (avg_pass, n_used):')
        for name, avg, n in stats['top_ops']:
            parts.append(f'  · {name}: avg_pass={avg}, n={n}')
    if stats.get('top_fields'):
        parts.append('직전 알파들에서 PASS 평균이 높았던 datafield (avg_pass, n_used):')
        for name, avg, n in stats['top_fields']:
            parts.append(f'  · {name}: avg_pass={avg}, n={n}')
    parts.append('이 통계는 권장사항. 다양성도 중요하니 단일 operator/field 에 8개 모두 몰리지 마라.')
    return '\n'.join(parts)


def _common_sections(round_num: int, feedback: list[dict], errors: list[dict],
                      avoid_codes: list[str] | None,
                      submitted_codes: list[str] | None,
                      seeds: list[dict] | None,
                      pref_stats: dict | None) -> list[str]:
    """full/cached 프롬프트가 공유하는 6개 섹션 (순서 고정) — 단일 진실.
    여기 섹션을 추가/수정하면 두 빌더 모두 자동 반영됨 (한쪽 누락 버그 방지)."""
    return [
        _build_challenging_section(),
        _build_dynamic_section(round_num, feedback, errors),
        _build_building_blocks_section(seeds or []),
        _build_preference_section(pref_stats or {}),
        _build_submitted_anticorr_section(submitted_codes or []),
        _build_avoid_codes_section(avoid_codes or []),
    ]


def _append_extra(base: str, research_notes: str, seeds_section: str) -> str:
    extra = []
    if research_notes:
        extra.append('\n\n[이번 라운드 연구노트 — 신선한 가설 시드]\n' + research_notes)
    if seeds_section:
        extra.append('\n\n' + seeds_section)
    return base + ''.join(extra)


def _build_user_prompt_full(round_num: int, feedback: list[dict],
                             errors: list[dict],
                             avoid_codes: list[str] | None = None,
                             submitted_codes: list[str] | None = None,
                             seeds: list[dict] | None = None,
                             pref_stats: dict | None = None,
                             forced_delay: 'str | None' = None,
                             slot_settings: list | None = None,
                             seeds_section: str = '',
                             research_notes: str = '') -> str:
    operators = _read_csv_text(OPERATORS_CSV)
    datafields = datafield_palette.build_palette(
        region='USA',
        delay=forced_delay,
        seed=round_num,
    )
    if not datafields:
        datafields = _read_csv_text(DATAFIELDS_CSV)
    parts = [
        f"라운드 #{round_num} — 8개 알파를 새로 생성하라.",
        "",
        "===== brain_operators.csv =====",
        operators,
        "",
        "===== IQC_brain_datafields.csv =====",
        datafields,
        "",
        *_common_sections(round_num, feedback, errors, avoid_codes,
                          submitted_codes, seeds, pref_stats),
        _format_slot_settings(slot_settings),
    ]
    result = '\n'.join(parts)
    return _append_extra(result, research_notes, seeds_section)


def _build_user_prompt_cached(round_num: int, feedback: list[dict],
                               errors: list[dict],
                               avoid_codes: list[str] | None = None,
                               submitted_codes: list[str] | None = None,
                               seeds: list[dict] | None = None,
                               pref_stats: dict | None = None,
                               slot_settings: list | None = None,
                               seeds_section: str = '',
                               research_notes: str = '') -> str:
    # 원본과 byte-동일: 기존 `A + '\n' + B + ...` == `'\n'.join([A,B,...])`.
    base = f"라운드 #{round_num} — 8개 알파를 새로 생성하라.\n\n" + '\n'.join(
        _common_sections(round_num, feedback, errors, avoid_codes,
                         submitted_codes, seeds, pref_stats))
    slot_block = _format_slot_settings(slot_settings)
    if slot_block:
        base += '\n' + slot_block
    return _append_extra(base, research_notes, seeds_section)


def _build_challenging_section() -> str:
    """캐시 히트 누적 시 동일 패턴 반복 방지용 — 도전적/다양한 archetype 적극 권장.

    이 섹션은 prompt 의 가장 앞쪽 (dynamic_section 직전) 에 배치 → Gemini 가 가장 먼저 읽음.
    """
    return '\n'.join([
        '',
        '===== 🔥 다양성 / 도전적 전략 의무사항 =====',
        '최근 라운드 캐시 히트가 누적되고 있다. 같은 패턴을 반복 생성 중이라는 강한 신호다.',
        '8개 알파가 서로 다른 가설/데이터소스/구조를 갖도록 분산하라 (같은 패턴 3개 초과 금지). 예:',
        '  • 자주 안 쓰는 dataset 적극 활용 — anl4 (analyst), fnd6 (fundamental), news18/socialmedia12, mdf 등',
        '  • 비표준 universe / group_* 함수로 sector/industry 슬라이스 신호화',
        '  • 다중 신호 결합 — alpha = 0.6 * sigA + 0.4 * sigB - 0.3 * sigC',
        '  • 비전통 archetype — term-structure, volatility regime, 이벤트(earnings/news) 모멘텀',
        '  • 비선형 transform — winsorize, signed_power(_, p), scale_down, sign 결합',
        '  • 직전 라운드들에서 PASS 났던 알파 구조를 그대로 다시 만들지 마라',
        '',
        '안전하고 보편적인 변형 (price/return 단일 datafield + 표준 window 변경) 반복 금지.',
        '각 알파마다 다른 가설/다른 데이터 소스/다른 시간 스케일을 시도하라.',
    ])


def _format_slot_settings(slot_settings: list) -> str:
    """Format a bandit slot_settings list into a soft-nudge directive block.

    Returns an empty string when slot_settings is None or empty.
    This is a pure function — tested independently.
    """
    if not slot_settings:
        return ''
    lines = [
        '',
        '[이번 라운드 settings 분산 가이드 — 학습된 성과 기반 권장값이다. '
        '가설에 더 맞는 settings가 있으면 그걸 우선하되, '
        '특별한 이유가 없으면 아래 분포를 따라 배치 전반에 settings를 분산하라:]',
    ]
    for i, slot in enumerate(slot_settings, start=1):
        uni  = slot.get('universe', 'TOP3000')
        neut = slot.get('neutralization', 'INDUSTRY')
        dcy  = slot.get('decay', 4)
        lines.append(f'  알파{i}: universe={uni}, neutralization={neut}, decay≈{dcy}')
    lines.append('')
    return '\n'.join(lines)


def _build_avoid_codes_section(codes: list[str]) -> str:
    """캐시 hit 으로 처리되는 코드 리스트 — 정확히 같으면 시뮬 자체를 안 함.
    따라서 새 알파 생성 시 이 코드들과 한 글자라도 같지 않게 만들어야 함."""
    if not codes:
        return ''
    parts = [
        '',
        '===== ⚠ 절대 다시 만들지 마라 — 이미 시뮬한 알파 (cache hit 으로 폐기됨) =====',
        '아래 코드와 정확히 동일하면 시뮬 자체가 안 일어나고 같은 결과가 재사용됨.',
        '아래 패턴을 반복하지 말고 의미 있게 변형 (다른 operator / datafield / 윈도우 / 결합 방식) 시도:',
    ]
    # 너무 길면 prompt 폭주 — 최대 80개, 각 코드 200자로 제한.
    for c in codes[:80]:
        s = (c or '').strip()
        if not s:
            continue
        if len(s) > 200:
            s = s[:200] + '…'
        parts.append(f'- {s}')
    parts.append('')
    return '\n'.join(parts)


def _build_submitted_anticorr_section(codes: list[str]) -> str:
    """이미 WQB 에 제출 성공한 알파 코드 — 새 알파는 이들과 self-correlation 이
    높지 않도록 의도적으로 다른 archetype / dataset / aggregation 을 사용해야 함.
    Submit 시점에 self-correlation 검사가 새로 돌아 0.7 초과 시 reject 되기 때문."""
    if not codes:
        return ''
    parts = [
        '',
        '===== 🚀 이미 제출된 알파 — Self-Correlation 충돌 회피 =====',
        f'아래 {len(codes)}개 알파는 본인 계정으로 제출 성공 상태. 이들과 ',
        'self-correlation 이 0.7 미만이어야 새 알파도 제출 가능 (WQB 가 Submit 시점에',
        '검사). 다음 전략을 의식적으로 활용해 충분히 다른 신호를 만들어라:',
        '  • 다른 archetype 으로 전환 (예: 가격기반→펀더멘털, 평균회귀→모멘텀)',
        '  • 다른 dataset/datafield 사용 (anl4 / fnd6 / news / soc 등)',
        '  • 다른 aggregation 시간 윈도우 (단기 5d ↔ 장기 250d)',
        '  • 다른 universe / 섹터 슬라이스',
        '  • cross-sectional vs time-series operator 변경',
        '  • 약한 신호 결합 (alpha = sigA - 0.5 * sigB) 으로 직교화',
        '',
        '제출된 알파 코드:',
    ]
    for c in codes[:30]:
        s = (c or '').strip()
        if not s:
            continue
        if len(s) > 200:
            s = s[:200] + '…'
        parts.append(f'- {s}')
    parts.append('')
    return '\n'.join(parts)


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'```\s*$', '', text)
    s = text.find('[')
    e = text.rfind(']')
    if s != -1 and e != -1 and e > s:
        return text[s:e + 1]
    return text


_FORBIDDEN_SUBSTRINGS = (
    'parkinson_volatility', 'hl_volatility', 'ts_returns', 'realized_volatility',
    'turnover_volatility', 'ts_decay_exp',
    'ts_median', 'ts_skewness', 'ts_kurtosis', 'ts_co_skewness',
    'ts_co_kurtosis', 'ts_partial_corr',
    # round1 idx=10 에서 'inaccessible operator log_diff' 에러 발생 → 추가
    'log_diff',
    # 2026-06-07 라이브: 이 tier 에서 inaccessible ('unknown operator regression_neut')
    'regression_neut',
)
_SCIENTIFIC_NOTATION_RX = re.compile(r'\d+\.?\d*[eE][+-]?\d+')


def _alpha_violations(code: str) -> list[str]:
    bad = []
    lower = code.lower()
    for tok in _FORBIDDEN_SUBSTRINGS:
        if tok in lower:
            bad.append(f'forbidden token "{tok}"')
    if _SCIENTIFIC_NOTATION_RX.search(code):
        bad.append('scientific notation (e.g. 1e-6)')
    return bad


def _parse_strategies(raw: str) -> list[dict]:
    cleaned = _strip_code_fences(raw)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError('expected a JSON array of strategies')
    out = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        code = (item.get('code') or '').strip()
        desc = (item.get('desc') or '').strip()
        code = re.sub(r'\s+', ' ', code).strip()
        if not code:
            continue
        settings = _normalize_settings(item.get('settings'))
        out.append({'idx': i + 1, 'code': code, 'desc': desc, 'settings': settings})
    if len(out) < 1:
        raise ValueError('no valid strategies parsed')
    return out


def _filter_by_lint(strategies: list[dict], log_fn: Callable | None = None,
                    forced_delay: str | None = None) -> list[dict]:
    try:
        from . import alpha_lint
    except Exception:
        return strategies
    from . import alpha_repair
    clean: list[dict] = []
    rejected: list[tuple[int, str, list[str]]] = []
    for s in strategies:
        fixed, applied = alpha_repair.repair(s['code'], delay=forced_delay)
        if applied:
            s = dict(s)                      # 입력 dict 변형 방지 — 수정본은 사본에만 반영
            s['code'] = fixed
            if log_fn:
                log_fn(f"  #{s['idx']} 자동수정({', '.join(applied)}): {s['code'][:100]}")
        bads = _alpha_violations(s['code'])
        if not bads:
            bads = alpha_lint.validate_alpha(s['code'])
        if bads:
            rejected.append((s['idx'], s['code'], bads))
        else:
            clean.append(s)
    if rejected and log_fn:
        for idx, code, bads in rejected:
            log_fn(f'⚠ #{idx} lint 거부 ({"; ".join(bads)[:200]}): {code[:120]}')
    return clean


def _delay_directive(forced_delay: str | None) -> str:
    """이 라운드에 강제된 delay 를 user prompt 에 알린다(동적 섹션 — 캐시된
    SYSTEM_INSTRUCTION 을 건드리지 않아 context cache 무효화 없음).
    delay=0 은 보편 PV 필드만 제공하므로 그 쪽으로 필드 선택을 유도해 sim 실패를 줄인다.
    """
    if forced_delay is None:
        return ''
    if str(forced_delay) == '0':
        return ('\n\n[DELAY 강제 = 0 — 이번 라운드 전 알파 / delay=0 전용 통과 플레이북]\n'
                'settings.delay 는 시스템이 0 으로 덮어쓴다(적지 마라). delay=0 에서는 '
                'close/open/high/low/volume/vwap/returns/cap/adv20 같은 보편 Price-Volume 필드만 '
                '제공되고 fundamental/analyst/institutional/option 등 상당수 datafield 는 미제공이라 '
                'sim 이 ERROR 로 죽는다. 필드는 PV 로 좁혀라.\n'
                '★ delay=0 은 오늘 데이터로 오늘 포지션을 잡아 신호가 빠르고 노이즈가 커서 '
                '**턴오버 폭증 → Fitness·Turnover·Sub-universe Sharpe 항목이 한꺼번에 FAIL** 하는 것이 '
                '6/7pass 를 못 넘는 주원인이다. 따라서 delay=0 알파는 반드시 강하게 스무딩하라:\n'
                '  1) decay 를 크게 — delay=0 은 decay 15~40 권장(배치 안에서 분산). 일반 PV rank 도 최소 8 이상.\n'
                '  2) 코드 안에서 hump 로 작은 리밸런싱을 무시해 턴오버를 직접 깎아라 (delay=0 의 핵심 도구). '
                '⚠ hump 은 입력이 정확히 1개뿐이다 — 임계값은 반드시 named 파라미터로 써라: hump(signal, hump=0.03). '
                '절대 positional 로 쓰지 마라: hump(signal, 0.03) 은 "Invalid number of inputs" 에러로 무조건 실패한다. '
                '또는 ts_decay_linear(signal, 5~20) 로 신호 자체를 평활.\n'
                '  3) ts_mean(sig, 5~20) 으로 raw PV 의 일중 노이즈를 미리 눌러라. 1~2일 초단기 반전 raw 신호는 '
                '턴오버만 터지고 거의 FAIL 이니 피하라.\n'
                '  4) trade_when(entry, alpha, -1) 로 포지션을 유지(exit=-1)해 불필요한 회전을 줄여라.\n'
                '  5) neutralization(SECTOR/INDUSTRY/SUBINDUSTRY)으로 시장노이즈를 제거해 Sharpe·Sub-universe '
                'Sharpe 를 끌어올려라(delay=0 에서 거의 필수).\n'
                '★ 탈상관(self-corr<0.7) — delay=0 은 필드가 PV 로 묶여 데이터셋 다양성이 없으니 남은 레버는 '
                'settings·구조뿐이다. 8개 알파를 **(universe × neutralization) 서로 다른 칸**에 강제 분산하라 '
                '(TOP200/500/1000/3000 × MARKET/SECTOR/INDUSTRY/SUBINDUSTRY). 또한 기존 제출풀은 delay=1 '
                '신호라, "어제 반전을 오늘 실행" 류는 그 풀과 겹친다 — delay=1 이 물리적으로 못 잡는 **당일 '
                '인트라데이 반응**(오늘 open→현재가/vwap, 당일 volume 급증→당일 포지션)을 노려야 진짜 직교한다.\n'
                '\n'
                '⛔ [필드 사용 규칙 — 위반 시 sim ERROR] 위에 제공된 datafield 팔레트(IQC_brain_datafields)는 '
                '전부 delay=1 전용 데이터셋(fundamental/analyst/news/option/institutional 등)이라 delay=0 '
                '시뮬에서 쓰면 거의 다 ERROR 로 죽는다. **그 팔레트는 무시하고, 아래 delay=0 PV 필드만 써라:**\n'
                '  open, high, low, close, vwap, volume, returns, cap, adv20, sharesout, dividend\n'
                '  (그룹/중립화용: market, sector, industry, subindustry — group_rank/group_neutralize 에만)\n'
                '\n'
                '🎯 [구조적 다양성 의무 — Sharpe edge 의 핵심] delay=0 은 필드가 ~10개뿐이라 edge 는 '
                '**같은 필드를 얼마나 다른 구조로 조합하느냐**에서 나온다. 8개 알파를 아래 서로 다른 '
                'archetype 칸에 분산하라(같은 반전 신호 10번 복제 금지 — 이게 지금 PASS=5 천장의 주원인):\n'
                '  1) 오버나이트 갭 반전: open 대비 전일 close 갭 (open/ts_delay(close,1)-1) 의 평균회귀\n'
                '  2) 일중 위치 모멘텀: (close-open)/(high-low+ε) — 당일 봉 내 종가 위치\n'
                '  3) VWAP 괴리 회귀: (close-vwap)/vwap — 당일 체결가 대비 괴리\n'
                '  4) 유동성조정 모멘텀: returns * rank(volume/adv20) — 거래량 확증된 추세\n'
                '  5) 거래량 충격: ts_zscore(volume,20) 또는 volume/adv20 서프라이즈로 신호 게이팅\n'
                '  6) 변동성 레짐: ts_std(returns,20) 로 신호를 스케일/필터(고변동 구간 축소)\n'
                '  7) 가격-거래량 상관: ts_corr(close,volume,N) / ts_corr(returns,volume,N) 부호\n'
                '  8) 레인지 확장: (high-low)/ts_mean(high-low,20) — 변동성 돌파\n'
                '  9) 横단면 랭크 합성: group_rank(signal, sector) 로 섹터내 상대강도\n'
                ' 10) 다중패밀리 합성: add(zscore(sigA), zscore(sigB), zscore(sigC)) 로 위 2~3개 직교 '
                '신호를 표준화 후 결합 — 단일 신호보다 Sharpe 가 구조적으로 높다(가장 권장).\n'
                '⚠ 한 알파에 최소 2개 이상의 서로 다른 PV 패밀리를 합성하라. 단일 raw 반전은 Sharpe~0.4 에서 막힌다.\n'
                '⚠ 다중문(`;`) 알파에서 **같은 변수명을 재정의하지 마라**(두 번 대입 금지) — `signal = ...; signal = ...` 는 '
                '"Attempted to redefine variable" ERROR 로 죽는다. 중간 변수는 sig1/sig2/sig3 처럼 매번 고유한 '
                '이름을 쓰고(위 sigA/sigB/sigC 는 자리표시일 뿐 실제로는 다른 이름으로 치환), 마지막 줄에서 한 번만 결합하라.\n')
    return ('\n\n[DELAY 강제 = 1 — 이번 라운드 전 알파]\n'
            'settings.delay 는 시스템이 1 로 덮어쓴다(적지 마라). delay=1 전용 '
            'fundamental/analyst 등 모든 datafield 를 자유롭게 사용해도 된다.\n')


def generate_research_notes(*, api_key: str, round_num: int,
                            forced_delay: str | None = None,
                            log_fn: Callable | None = None) -> str:
    """One short Google-grounded call per round → 3 fresh factor hypotheses as text,
    injected into the batch-generation prompt. Returns '' when grounding is disabled
    or on any failure (generation continues ungrounded).

    forced_delay: when '0', restricts to PV-only fields; otherwise allows all
    datafield types (fundamental/analyst/option/news).
    """
    try:
        from . import run_config
        if not run_config.is_grounding_enabled():
            return ''
    except Exception:
        return ''
    if not api_key:
        return ''
    if str(forced_delay) == '0':
        prompt = (
            '최신 퀀트 팩터 연구/논문/리서치에서 WorldQuant Brain delay=0 (USA, Price-Volume 필드만: '
            'close/open/high/low/volume/vwap/returns/adv20/cap) 에 적용할 만한 신선한 알파 가설 3개를 '
            '각 한 줄로. 회전이 낮고 Sharpe 가 높을 만한 구조 위주. 출처/설명 없이 "가설: 식 스케치" 형식만.'
        )
    else:
        prompt = (
            '최신 퀀트 팩터 연구/논문/리서치에서 WorldQuant Brain (USA) 에 적용할 만한 신선한 알파 가설 3개를 '
            '각 한 줄로. 가격/거래량뿐 아니라 fundamental/analyst/option/news 데이터도 활용 가능. '
            '회전이 낮고 Sharpe 가 높을 만한 구조 위주. 출처/설명 없이 "가설: 식 스케치" 형식만.'
        )
    # 연산자 위생 — grounded 모델이 비-WQB 연산자를 노트에 흘리면 Gemini 가 베껴 sim ERROR
    # (관측: indneutralize/IndClass/exp). WQB FASTEXPR 만 쓰도록 명시.
    prompt += (' 식 스케치는 WorldQuant FASTEXPR 연산자만: ts_std_dev(ts_std 아님), '
               'group_neutralize(x, sector) 처럼 bare group(IndClass/indneutralize 금지), '
               'exp/tanh/sigmoid 금지.')
    try:
        client = genai.Client(api_key=api_key)
        tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        # thinking_budget=0: 2.5-flash 의 thinking 토큰이 출력 예산을 잠식해 grounded
        # 응답이 ~30자에서 MAX_TOKENS 로 잘리던 문제 수정(라이브 디버깅으로 규명). thinking
        # 끄고 출력 여유를 주면 가설 3개가 온전히 나온다(thoughts_token=490→0, text 33→600+자).
        cfg = genai_types.GenerateContentConfig(
            tools=tools, temperature=0.9, max_output_tokens=2048,
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        resp = client.models.generate_content(model=_model_chain()[0], contents=prompt, config=cfg)
        text = (resp.text or '').strip()
        if text and log_fn:
            log_fn(f'   (round {round_num} grounding 연구노트 {len(text)}자 수신)')
        return text[:1500]
    except Exception as e:
        if log_fn:
            log_fn(f'   (grounding 실패, 무근거 생성으로 진행: {str(e)[:80]})')
        return ''


def generate_strategies(
    *,
    api_key: str,
    round_num: int,
    feedback: list[dict] | None = None,
    errors: list[dict] | None = None,
    avoid_codes: list[str] | None = None,
    submitted_codes: list[str] | None = None,
    seeds: list[dict] | None = None,
    pref_stats: dict | None = None,
    cache_hit_ratio_hint: float = 0.0,
    max_retries: int | None = 3,
    retry_wait_sec: int = 60,
    forced_delay: str | None = None,
    slot_settings: list | None = None,
    effectiveness_priors: str | None = None,
    log_fn: Callable | None = None,
) -> list[dict]:
    """8개 알파 생성. user_id 별 API 키 받음.

    avoid_codes: 이미 시뮬한 distinct 코드 리스트 — 정확히 동일 코드는 cache hit 으로
                 무시되니 다시 만들지 말라고 Gemini 에게 명시.
    submitted_codes: 본인 계정으로 이미 제출 성공한 알파 코드 — self-correlation 충돌
                 회피하도록 Gemini 가 다른 archetype/dataset 시도하게 가이드.
    cache_hit_ratio_hint: 직전 라운드 cache hit 비율. 0.5 이상이면 다양성 부족 → 온도 부스트.
    max_retries=None : 무한 재시도 (IQC 와 동일).
    """
    if not api_key:
        raise RuntimeError('Gemini API key 없음 (HYFE_IQC user 자격 미설정)')

    client = genai.Client(api_key=api_key)
    chain = _model_chain()

    # 캐시 hit ratio 에 따라 temperature 적극 부스팅.
    # 0.05 부터 이미 부스트 시작 (캐시히트 너무 많이 나오는 현 상황 대응).
    base_temp = 0.90  # 0.85 → 0.90 상향
    if cache_hit_ratio_hint >= 0.5:
        temperature = 1.25
    elif cache_hit_ratio_hint >= 0.3:
        temperature = 1.15
    elif cache_hit_ratio_hint >= 0.15:
        temperature = 1.05
    elif cache_hit_ratio_hint >= 0.05:
        temperature = 0.98
    else:
        temperature = base_temp
    if log_fn and temperature > base_temp:
        log_fn(f'   (cache_hit_ratio={cache_hit_ratio_hint:.0%} — temperature={temperature} 로 다양성 부스트)')

    import random as _rnd
    _seeds_list = alpha_seeds.sample_seeds(5, rng=_rnd.Random(round_num))
    seeds_section = alpha_seeds.render_seeds_section(_seeds_list)
    research_notes = generate_research_notes(api_key=api_key, round_num=round_num,
                                             forced_delay=forced_delay, log_fn=log_fn)

    attempt = 0
    while True:
        attempt += 1
        last_err = None
        for m in chain:
            try:
                cache_name = _get_or_create_prompt_cache(client, m, api_key, log_fn=log_fn)
                if cache_name:
                    user_prompt = _build_user_prompt_cached(round_num, feedback or [], errors or [], avoid_codes or [], submitted_codes or [], seeds or [], pref_stats or {}, slot_settings=slot_settings, seeds_section=seeds_section, research_notes=research_notes)
                    cfg = genai_types.GenerateContentConfig(
                        cached_content=cache_name,
                        response_mime_type='application/json',
                        response_schema=_RESPONSE_SCHEMA,
                        temperature=temperature,
                        max_output_tokens=8192,
                    )
                else:
                    user_prompt = _build_user_prompt_full(round_num, feedback or [], errors or [], avoid_codes or [], submitted_codes or [], seeds or [], pref_stats or {}, forced_delay=forced_delay, slot_settings=slot_settings, seeds_section=seeds_section, research_notes=research_notes)
                    cfg = genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type='application/json',
                        response_schema=_RESPONSE_SCHEMA,
                        temperature=temperature,
                        max_output_tokens=8192,
                    )
                if effectiveness_priors:
                    user_prompt += effectiveness_priors
                user_prompt += _delay_directive(forced_delay)
                resp = client.models.generate_content(
                    model=m, contents=user_prompt, config=cfg,
                )
                text = (resp.text or '').strip()
                if not text:
                    last_err = GeminiQuotaError(f'{m}: empty response')
                    continue
                strategies = _parse_strategies(text)
                if log_fn and m != MODEL:
                    log_fn(f'   (모델 폴백 사용: {m})')
                clean = _filter_by_lint(strategies, log_fn=log_fn, forced_delay=forced_delay)
                for new_idx, s in enumerate(clean, start=1):
                    s['idx'] = new_idx
                if len(clean) < 8 and log_fn:
                    log_fn(f'⚠ Gemini 가 {len(strategies)}개 중 {len(clean)}개만 lint 통과 — 그대로 진행')
                if not clean:
                    last_err = ValueError(f'all {len(strategies)} strategies failed lint')
                    continue
                return clean
            except (json.JSONDecodeError, ValueError) as parse_err:
                last_err = parse_err
                if log_fn:
                    log_fn(f'⚠ {m}: JSON 파싱 실패 — 다음 모델: {parse_err}')
                continue
            except Exception as e:
                last_err = e
                err_str = str(e)
                if 'cached_content' in err_str.lower() or 'cache' in err_str.lower():
                    _PROMPT_CACHE.pop(_prompt_cache_key(m, api_key), None)
                if log_fn:
                    log_fn(f'⚠ {m}: 호출 실패 — 다음 모델: {err_str[:120]}')
                continue

        if log_fn:
            log_fn(f'⚠ 전체 모델 체인 실패 (시도 {attempt}). last={str(last_err)[:120]}')
        if max_retries is not None and attempt >= max_retries:
            raise (last_err or GeminiQuotaError('all model fallbacks failed'))
        time.sleep(retry_wait_sec)


def _build_focused_prompt(round_num: int, phase: int, parent_idx: int,
                           parent_code: str, parent_desc: str, fail_desc: str,
                           parent_pass_items: list[str], parent_fail_items: list[str],
                           focus_kind: str = 'fail',
                           self_corr_value: str = '',
                           submitted_codes: list[str] | None = None) -> str:
    # === correlation 회피 모드 ===
    if focus_kind == 'correlation':
        sc_line = (f"- 측정된 Self-Correlation: {self_corr_value} (cutoff 0.7 초과 → reject)"
                   if self_corr_value
                   else "- Self-Correlation cutoff 0.7 초과 → reject (정확한 값 미수집)")
        parts = [
            f"라운드 #{round_num}-{parent_idx}-{phase} (focused — Self-Correlation 회피) — 8개 직교화 변형 알파를 생성하라.",
            "",
            "===== 🚫 거절된 부모 알파 =====",
            f"- 부모 라운드 #{round_num} idx #{parent_idx}",
            f"- 코드: {parent_code}",
            f"- 설명: {parent_desc or '(없음)'}",
            f"- IS 테스트는 모두 PASS (7-8개 전부 통과) — 통계적으로 우수.",
            sc_line,
            "",
            "===== 미션 =====",
            "본 알파는 통계적으로 우수하지만, 너의 계정에 이미 제출된 다른 알파와 신호 유사도가 너무 높아 거절됨.",
            "Self-correlation 을 0.7 미만으로 떨어뜨리려면 **핵심 신호의 구조 자체를 바꿔야** 한다.",
            "",
            "❌ 절대 하지 말 것:",
            "  - 부모 코드의 window/decay 값만 살짝 바꾸기 (같은 신호 family → corr 그대로 높음)",
            "  - 같은 datafield 에서 비슷한 통계량 (rank vs zscore 의 단순 교체)",
            "  - 같은 archetype 안에서 미세 조정",
            "",
            "✅ 8개 모두 **서로 다른 직교화 차원** 으로 만들어라. 다음 중 분산되게:",
            "  1. **다른 datafield/dataset 으로 동일 의도 재구현** — pv → fundamental(fnd6), anl4 → news/soc/mdf, returns → volume-derived",
            "  2. **다른 시간 윈도우** — 단기(5~10d) ↔ 장기(120~250d) (다른 진동 주파수)",
            "  3. **archetype 전환** — mean-reversion ↔ momentum ↔ event-driven ↔ micro-structure",
            "  4. **cross-sectional ↔ time-series operator 교체** — rank ↔ ts_rank, zscore ↔ ts_zscore, scale ↔ ts_scale",
            "  5. **명시적 직교화** — `alpha = parent_like_signal - 0.5 * (market_factor or momentum_factor)` 등",
            "  6. **다른 universe 슬라이스** — group_rank/group_zscore 로 sector/industry/subindustry 단위 재정의",
            "  7. **neutralization 축 변경** — industry → subindustry → market → none",
            "  8. **비선형 transform 추가** — winsorize, signed_power, scale_down, sign 결합",
            "  9. **신호 결합** — 약한 신호 2~3개를 가중 합성 (alpha = 0.6*sigA + 0.4*sigB - 0.3*sigC)",
            "  10. **이벤트/희소 시그널** — earnings_revision, news momentum, social sentiment 가 있다면 활용",
            "",
            "각 alpha 의 desc 에는 부모 대비 어떤 차원으로 직교화했는지 명시 (예: 'archetype 전환: mean-reversion → 이벤트 모멘텀, anl4_eps_estimate 사용').",
        ]
        if submitted_codes:
            parts.append("")
            parts.append("===== 이미 제출된 알파 — 이들과 모두 corr<0.7 되도록 =====")
            for c in (submitted_codes or [])[:20]:
                s = (c or '').strip()
                if not s:
                    continue
                if len(s) > 180:
                    s = s[:180] + '…'
                parts.append(f'- {s}')
        return '\n'.join(parts)

    # === 기본 'fail' 모드 — PASS=6 FAIL=1 의 단일 실패 테스트만 개선 ===
    parts = [
        f"라운드 #{round_num}-{parent_idx}-{phase} (focused sub-round — fail 개선) — 8개 변형 알파를 생성하라.",
        "",
        "===== 🎯 개선 대상 부모 알파 =====",
        f"- 부모 라운드 #{round_num} idx #{parent_idx}",
        f"- 코드: {parent_code}",
        f"- 설명: {parent_desc or '(없음)'}",
        f"- PASS 항목: {', '.join(parent_pass_items[:8]) or '(없음)'}",
        f"- FAIL 항목 (개선 대상): {', '.join(parent_fail_items[:4]) or fail_desc or '(미지정)'}",
        "",
        "===== 미션 =====",
        "이 부모 알파는 7개 PASS 중 1개만 FAIL 했다. PASS=6 FAIL=1 (PENDING=1) 상태.",
        f"이 단 하나의 실패한 테스트(\"{fail_desc}\")만 개선해야 한다. 다른 PASS 항목들을 깨뜨리지 마라.",
        "",
        "다음 8개의 변형을 만들되, 각각 명확히 다른 접근으로 실패 테스트를 개선해야 한다:",
        "  1. 부모 코드의 핵심 구조는 유지하되 한두 부분만 의미 있게 바꿔라.",
        "  2. FAIL 한 테스트의 cutoff 와 방향을 의식해서 (예: turnover 가 너무 낮다면 더 활발한 시그널 필요).",
        "  3. window 크기, decay, neutralization, ts_* 함수, rank/zscore wrapping 변경 시도.",
        "  4. 다른 datafield 로 일부 교체. 단 검증된 building block 은 유지.",
        "  5. 8개가 동일한 변형 패턴이면 안 됨. 서로 다른 가설을 8개.",
        "",
        "각 알파의 desc 에는 '부모대비 무엇을 바꿨는지' 와 '왜 그게 실패 테스트를 개선할 것이라 보는지' 명시.",
    ]
    return '\n'.join(parts)


def generate_focused_strategies(
    *,
    api_key: str,
    round_num: int,
    phase: int,
    parent_idx: int,
    parent_code: str,
    parent_desc: str = '',
    fail_desc: str = '',
    parent_pass_items: list[str] | None = None,
    parent_fail_items: list[str] | None = None,
    focus_kind: str = 'fail',
    self_corr_value: str = '',
    submitted_codes: list[str] | None = None,
    max_retries: int | None = 3,
    retry_wait_sec: int = 60,
    forced_delay: str | None = None,
    log_fn: Callable | None = None,
) -> list[dict]:
    """focused sub-round 12 변형 생성.

    focus_kind:
      - 'fail'        : PASS=6 FAIL=1 의 단일 실패 테스트만 개선 (다른 PASS 유지).
      - 'correlation' : PASS=7+ 거절 — 본인 제출 알파와의 self-correlation 직교화.
    """
    if not api_key:
        raise RuntimeError('Gemini API key 없음')

    client = genai.Client(api_key=api_key)
    chain = _model_chain()

    # focused 모드는 베이스 prompt 가 다르므로 cache 안 씀 (미스 거의 보장).
    user_prompt = _build_focused_prompt(
        round_num=round_num, phase=phase,
        parent_idx=parent_idx, parent_code=parent_code,
        parent_desc=parent_desc, fail_desc=fail_desc,
        parent_pass_items=list(parent_pass_items or []),
        parent_fail_items=list(parent_fail_items or []),
        focus_kind=focus_kind,
        self_corr_value=self_corr_value,
        submitted_codes=list(submitted_codes or []),
    )
    user_prompt += _delay_directive(forced_delay)
    # directed-mutation: 부모의 실패 지표를 정확히 겨냥한 개선 지시 주입 (fail 모드만).
    if focus_kind == 'fail':
        try:
            from . import directed_mutation
            _md = directed_mutation.route(parent_fail_items or [], parent_code)
            if _md.get('instruction'):
                user_prompt += '\n\n' + _md['instruction']
                if log_fn:
                    log_fn(f'   (directed-mutation: {_md["strategy"]})')
        except Exception:
            pass
    # correlation 모드는 다양성 극대화 필요 → temperature 상향.
    _focused_temp = 1.10 if focus_kind == 'correlation' else 0.95

    attempt = 0
    while True:
        attempt += 1
        last_err = None
        for m in chain:
            try:
                cfg = genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type='application/json',
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=_focused_temp,
                    max_output_tokens=8192,
                )
                resp = client.models.generate_content(model=m, contents=user_prompt, config=cfg)
                text = (resp.text or '').strip()
                if not text:
                    last_err = GeminiQuotaError(f'{m}: empty response')
                    continue
                strategies = _parse_strategies(text)
                if log_fn and m != MODEL:
                    log_fn(f'   (focused 모델 폴백 사용: {m})')
                clean = _filter_by_lint(strategies, log_fn=log_fn, forced_delay=forced_delay)
                for new_idx, s in enumerate(clean, start=1):
                    s['idx'] = new_idx
                if not clean:
                    last_err = ValueError('all focused strategies failed lint')
                    continue
                return clean
            except (json.JSONDecodeError, ValueError) as parse_err:
                last_err = parse_err
                if log_fn:
                    log_fn(f'⚠ focused {m}: JSON 파싱 실패 — 다음 모델: {parse_err}')
                continue
            except Exception as e:
                last_err = e
                if log_fn:
                    log_fn(f'⚠ focused {m}: 호출 실패 — 다음 모델: {str(e)[:120]}')
                continue
        if log_fn:
            log_fn(f'⚠ focused 전체 모델 체인 실패 (시도 {attempt}). last={str(last_err)[:120]}')
        if max_retries is not None and attempt >= max_retries:
            raise (last_err or GeminiQuotaError('all focused fallbacks failed'))
        time.sleep(retry_wait_sec)


def _build_crossover_prompt(parents: list, submitted_codes: list | None = None) -> str:
    """두 고성과 부모 알파를 창의적으로 융합하는 crossover 프롬프트를 생성한다(순수 함수).

    parents: list of dicts with keys 'code', 'pass_count', 'operators'
    submitted_codes: 이미 WQB 에 제출 성공한 알파 코드 (self-corr 회피용)
    """
    parts = [
        "===== 🧬 교차(Crossover) 알파 생성 =====",
        "아래 두 고성과 부모 알파의 성공 요소(연산자 선택·시간창·정규화·신호 방향·중립화)를",
        "각각 추출한 뒤, 그것들을 **창의적으로 융합**해 새 알파 8개를 만들어라.",
        "각 알파는 두 부모와도, 서로와도 **구조적으로 달라야** 한다(단순 결합·복붙 금지).",
        "비율>곱>합 원칙, 회전 억제(ts_decay_linear/hump), group_neutralize 로 안정화.",
        "delay/settings 도 배치 안에서 분산하라.",
        "",
    ]

    for i, p in enumerate(parents, start=1):
        code = (p.get('code') or '').strip()
        pc = p.get('pass_count', '?')
        ops = p.get('operators') or []
        ops_str = ', '.join(ops[:8]) if ops else '(미지정)'
        parts.append(f"부모{i} (PASS {pc}): {code}")
        parts.append(f"  사용 연산자: {ops_str}")
        parts.append("")

    parts += [
        "===== 융합 미션 =====",
        "1. 각 부모의 핵심 신호 구조(필드, 집계, 정규화, 방향)를 별도로 분석하라.",
        "2. 두 부모의 강점을 결합한 8개의 **새로운** 하이브리드 알파를 생성하라.",
        "3. 각 알파는 부모들과 서로 **구조적으로 달라야** 한다:",
        "   - 단순히 코드를 붙여 넣거나(연결 금지), 부모 코드를 그대로 반환 금지.",
        "   - 새로운 시간창/정규화/연산자 조합으로 동일 경제 가설을 재구현하라.",
        "   - archetype 전환(모멘텀↔리버전↔펀더멘털↔센티멘트)도 적극 시도.",
        "4. 비율>곱>합 설계 원칙을 우선 적용.",
        "5. 회전 억제: ts_decay_linear(signal, 5~10) 또는 hump(x, hump=0.03) 활용.",
        "6. group_neutralize 로 섹터/산업 편향 제거.",
        "7. settings(universe/neutralization/decay/delay)를 8개 안에서 골고루 분산.",
        "",
        "각 알파의 desc 에는 '부모1/부모2 에서 어떤 요소를 가져왔는지' 와",
        "'왜 그 조합이 Sharpe/Fitness 를 높일 것이라 보는지' 명시.",
    ]

    if submitted_codes:
        parts.append("")
        parts.append("===== 🚫 이미 제출된 알파 — self-corr < 0.7 유지 =====")
        parts.append("아래 코드들과 신호 유사도가 0.7 미만이 되도록 archetype/dataset/구조를 충분히 다르게 설계하라.")
        for c in (submitted_codes or [])[:20]:
            s = (c or '').strip()
            if not s:
                continue
            if len(s) > 180:
                s = s[:180] + '…'
            parts.append(f'- {s}')

    return '\n'.join(parts)


def generate_crossover_strategies(
    *,
    api_key: str,
    round_num: int,
    parents: list,
    submitted_codes: list | None = None,
    max_retries: int | None = 3,
    forced_delay: str | None = None,
    log_fn: Callable | None = None,
) -> list[dict]:
    """두 고성과 '생존자' 알파를 교차(crossover)해 구조적으로 다른 하이브리드 알파 8개를 생성.

    parents: list of dicts with keys 'code' (str), 'pass_count' (int), 'operators' (list).
             부모가 2개 미만이면 generate_strategies 로 폴백.
    submitted_codes: 이미 WQB 에 제출 성공한 알파 코드 (self-corr 회피용).
    """
    if not api_key:
        raise RuntimeError('Gemini API key 없음')

    if len(parents) < 2:
        if log_fn:
            log_fn('🧬 crossover: 부모 2개 미만 → generate_strategies 폴백')
        return generate_strategies(
            api_key=api_key,
            round_num=round_num,
            forced_delay=forced_delay,
            log_fn=log_fn,
        )

    if log_fn:
        ids = ' × '.join(f"#{p.get('pass_count', '?')}" for p in parents[:2])
        log_fn(f'🧬 crossover 생성 (부모 {ids})')

    client = genai.Client(api_key=api_key)
    chain = _model_chain()

    # crossover 프롬프트는 매 호출마다 고유하므로 prompt cache 미사용.
    user_prompt = _build_crossover_prompt(parents, submitted_codes=submitted_codes)
    user_prompt += _delay_directive(forced_delay)

    attempt = 0
    while True:
        attempt += 1
        last_err = None
        for m in chain:
            try:
                cfg = genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    response_mime_type='application/json',
                    response_schema=_RESPONSE_SCHEMA,
                    temperature=1.05,
                    max_output_tokens=8192,
                )
                resp = client.models.generate_content(model=m, contents=user_prompt, config=cfg)
                text = (resp.text or '').strip()
                if not text:
                    last_err = GeminiQuotaError(f'{m}: empty response')
                    continue
                strategies = _parse_strategies(text)
                if log_fn and m != MODEL:
                    log_fn(f'   (crossover 모델 폴백 사용: {m})')
                clean = _filter_by_lint(strategies, log_fn=log_fn, forced_delay=forced_delay)
                for new_idx, s in enumerate(clean, start=1):
                    s['idx'] = new_idx
                if not clean:
                    last_err = ValueError('all crossover strategies failed lint')
                    continue
                return clean
            except (json.JSONDecodeError, ValueError) as parse_err:
                last_err = parse_err
                if log_fn:
                    log_fn(f'⚠ crossover {m}: JSON 파싱 실패 — 다음 모델: {parse_err}')
                continue
            except Exception as e:
                last_err = e
                if log_fn:
                    log_fn(f'⚠ crossover {m}: 호출 실패 — 다음 모델: {str(e)[:120]}')
                continue
        if log_fn:
            log_fn(f'⚠ crossover 전체 모델 체인 실패 (시도 {attempt}). last={str(last_err)[:120]}')
        if max_retries is not None and attempt >= max_retries:
            raise (last_err or GeminiQuotaError('all crossover fallbacks failed'))
        time.sleep(60)
