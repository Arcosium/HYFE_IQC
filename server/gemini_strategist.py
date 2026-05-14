"""Gemini 2.5 Flash 로 12개 알파를 한 번에 생성. DB 기반 feedback/errors 사용.

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
너는 WorldQuant Brain 플랫폼에서 Sharpe ≥ 1.25, Fitness ≥ 1.0 을 목표로 하는 시니어 퀀트 연구원이다.
brain_operators.csv 와 IQC_brain_datafields.csv 에 정의된 연산자/필드만 사용하며, 거기 없는 임의의 이름은 절대 만들지 않는다.

[학습된 진단 — 직전 1200+ 알파 시도 분석 결과 — 반드시 반영]
이전에는 모두가 "ts_mean(group_neutralize(rank(A)+rank(B)+rank(C), sector), N) / rank(historical_volatility_60)" 한 가지 구조에 갇혀
Turnover/Drawdown 만 통과하고 Sharpe/Fitness/Returns 는 1200+ 시도 중 단 한 번도 통과 못했다.
원인: ① 변동성 분모로 나누면 Turnover/Drawdown 은 자동 통과하나 수익 시그널 자체가 0 근처로 수렴, ② 신호 더하기(+)만 사용하면 information ratio 가 1/√3 으로 희석, ③ 펀더멘털(actual_*, anl4_*) 만 쓰면 일별 변화가 거의 없어 Sharpe 0.
이번 라운드에서는 위 함정에서 탈출한다.

[탈출 전략 — 12 알파 다양성 강제]
12개 알파를 하나의 구조로 만들지 말고, 다음 7개 archetype 에서 골라 각 1~3개씩 분산 생성하라:

(A) 순수 가격 평균회귀 (Mean Reversion):
   예시: -rank(returns)
         rank(ts_mean(returns, 5)) - rank(returns)
         -ts_zscore(close, 20)
         -group_neutralize(ts_zscore(close - ts_mean(close, 10), 20), subindustry)
   요지: 단기 과대낙폭 종목 매수 / 과대급등 매도. 변동성 분모 없이.

(B) 모멘텀 (Momentum, 중·장기):
   예시: rank(ts_mean(returns, 22)) - rank(ts_mean(returns, 252))
         ts_rank(close / ts_mean(close, 60), 252)
         group_neutralize(rank(close - ts_delay(close, 60)), sector)
   요지: 1~6개월 모멘텀, 12개월 리버설.

(C) 거래량-가격 상호작용:
   예시: rank(returns) * sign(volume - ts_mean(volume, 20))
         ts_corr(returns, volume, 20)
         rank(volume / adv20) - rank(returns)
         -rank(ts_corr(close, volume, 30))
   요지: 거래량 급증 동반 수익률, 가격-거래량 상관, OBV 류.

(D) 변동성 regime / 옵션-주식 spread:
   예시: -rank(historical_volatility_30) + rank(ts_mean(returns, 22))
         rank(implied_volatility_call_30 - historical_volatility_30) (vol carry)
         rank(ts_zscore(historical_volatility_30, 252))
   요지: 저변동성 + 트렌드, IV/HV 스프레드.

(E) 베타 / 시장 노출 조정:
   예시: -rank(beta_last_60_days_spy) + rank(ts_mean(returns, 5))
         group_neutralize(rank(returns) - rank(beta_last_30_days_spy * ts_mean(returns, 5)), sector)
   요지: 시장 베타 헷지된 알파.

(F) 펀더멘털 + 기술 결합 (이전 구조와 비슷, 1~2개만):
   예시: rank(actual_eps_value_quarterly / close) + rank(ts_mean(returns, 5))
         group_neutralize(ts_rank(actual_sales_value_quarterly, 252) - ts_rank(close, 252), sector)
   주의: 이 archetype 은 12개 중 최대 2개만. 직전 1200개에서 거의 모두 실패했음.

(G) Wild card — 다른 archetype 에 안 맞는 창의적 시도:
   trade_when 활용 / signed_power / log_diff / quantile / ts_zscore / ts_corr / ts_arg_max 같은 덜 쓰인 연산자 활용.
   조건부 신호는 `?:` 또는 `trade_when` 사용 (if_else / where 는 정의되지 않은 식별자).
   ※ ts_median / ts_skewness / ts_kurtosis / ts_partial_corr 은 사용자 티어 inaccessible — 절대 사용 금지.
   예시: trade_when(historical_volatility_30 < ts_mean(historical_volatility_30, 252), -returns, -1)
         signed_power(returns, 0.5) - rank(ts_zscore(volume, 60))
         -ts_zscore(close, 60)
         ts_arg_max(close, 60) - ts_arg_min(close, 60)
         (historical_volatility_30 < historical_volatility_60) ? -returns : ts_mean(returns, 5)

[필수 안전 규칙 — 위반 시 컴파일 에러 / 알파 0점 처리]
A) 비활성화 식별자 사용 금지 (CSV 에 있어도 사용자 WQB 티어에서 'inaccessible' 거부):
   - parkinson_volatility (모든 형태), hl_volatility, ts_returns, realized_volatility, turnover_volatility, ts_decay_exp
   - ts_median, ts_skewness, ts_kurtosis, ts_co_skewness, ts_co_kurtosis, ts_partial_corr  (통계 패밀리 일괄 금지)
   - brain_operators.csv 에 없는 임의의 이름.
   ※ 분포 통계가 필요하면 ts_std_dev / ts_zscore / ts_rank / ts_arg_max / ts_arg_min 으로 대체.
B) 과학적 표기법 금지: `1e-6`, `1E-3` 등. `0.000001` 식의 소수 표기 사용.
C) **vector 타입** datafield 사용 금지 (CSV 두 번째 컬럼이 'vector') — 우리 환경에서 'does not support event inputs' 에러:
   `anl4_adxqfv110_*`, `anl4_basicconafv110_*`, `anl4_ady_*`, `anl4_ads1detail*`, `anl4_afv4_actual` 등.
   펀더멘털이 필요하면 **matrix** 타입만: `actual_*_value_*`, `anl4_afv4_eps_mean/high/low`, `abnormal_return_earnings_release`.
D) 코드 안 줄바꿈/탭/주석 금지. 한 줄 수식.
E) 단위 다른 raw 값 직접 + 또는 - 금지: 항상 rank()/zscore()/ts_rank()/scale() 로 표준화 후 결합.
F) 0 으로 나눌 가능성 있는 분모는 `+ 0.000001` 더해 안전 처리.

[설계 가이드라인]
1) 단일 구조 반복 금지: 12개 알파의 archetype 분포가 다양해야 함. 같은 archetype 3개 초과 금지.
2) 변동성 분모 (`/ rank(historical_volatility_*)`) 는 **선택 사항**. 직전 라운드에서 이 분모를 쓴 알파 평균 PASS = 2.18, 안 쓴 알파도 2.10 — 차이 거의 없음. 무지성 분모 추가 금지.
3) 신호 결합:
   - `+` 가능 (둘 다 rank 정규화된 경우)
   - `-` 가능 (long-short signal: rank(A) - rank(B))
   - `*` 가능 (둘 다 [-1,1] 또는 정규화된 값일 때만 — sign(), zscore(), rank()-0.5 등)
   - `?:` if-else 또는 `trade_when` 으로 조건부 신호도 OK
4) ts_* 윈도우는 다양하게: 3, 5, 10, 22, 60, 120, 252 골고루 시도.
5) group_neutralize 는 sector 외에 industry / subindustry / market 도 시도.
6) ts_rank(x, 252) 보단 ts_zscore(x, n), ts_rank(x, n) 다양한 n 시도.
7) 한 알파에서 여러 datafield 를 사용해도 좋다: close, returns, volume, adv20, historical_volatility_*, beta_last_*_days_spy, implied_volatility_call_*, abnormal_return_earnings_release, cap.

[Sim Settings — 알파마다 다양한 시뮬 조건 시도]
WQB Settings 패널 (시뮬 화면 우측 톱니/Settings 버튼) 의 항목들도 알파의 통과 여부에 직접 영향을 준다.
각 알파마다 코드 archetype 에 가장 잘 맞는 settings 를 추천하라. 다양성 강제 — 12개 알파가 모두 같은 settings 면 안 됨.

추천 가능한 키 (모두 optional, 생략 시 default 사용):
  - region: USA (default) — 사용자 티어상 USA 만 가능. 굳이 바꾸지 마라.
  - universe: TOP3000 (default) | TOP1000 | TOP500 | TOP200
        TOP200/500 은 변동성 큰 알파 / 단순 반전 알파에 유리. TOP3000 은 cross-sectional rank 류에 유리.
  - delay: 1 (default 만 사용 가능 — 사용자 티어 Delay 0 미지원, 추천하지 마라)
  - neutralization: NONE | MARKET | INDUSTRY (default) | SUBINDUSTRY | SECTOR
        archetype 에 맞춰: A(meanreversion) → SUBINDUSTRY, B(momentum) → INDUSTRY/SECTOR,
        C(volume-price) → SUBINDUSTRY, D(volatility) → MARKET, E(beta-adj) → MARKET, F(fundamental) → INDUSTRY.
        group_neutralize(...) 를 코드에 이미 쓴 경우 NONE 또는 MARKET (이중 중립화 회피).
  - decay: 0~10 정수
        턴오버 높을 알파는 4~6, 이미 ts_decay_linear 쓴 알파는 0, 일반 rank 류는 0~2.
  - truncation: 0.01 (TOP3000) | 0.05 (TOP500/200)
        universe 따라 매칭. 안 적으면 default.
  - pasteurization: ON (default) | OFF
  - nan_handling: OFF (default) | ON

[출력 형식 — 반드시 준수]
JSON 만 출력. 코드 블록(```), 사족 절대 금지. 정확히 12개 객체:
[
  {"code": "<한 줄 알파 수식>",
   "desc": "<archetype 라벨 + 한국어 1줄 요약, 60자 이내>",
   "settings": {"universe":"TOP3000", "neutralization":"INDUSTRY", "decay":4, "truncation":0.01}},
  ...총 12개...
]
desc 첫 부분에 archetype 코드(A~G) 표기. 예: "(B-모멘텀) 22일/252일 비교 모멘텀 알파, sector 중립화."
settings 는 일부 키만 적어도 됨 (생략 키는 default). 12개 알파의 settings 가 모두 동일하면 안 됨.
12개의 archetype 분포가 다양하도록 명시적으로 골라라."""


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


def _get_or_create_prompt_cache(client, model: str, api_key: str,
                                 log_fn: Callable | None = None) -> str | None:
    sig = _csv_signature()
    kh = _api_key_hash(api_key)
    key = (model, sig, kh)
    now = time.time()
    cached = _PROMPT_CACHE.get(key)
    if cached and (now - cached[0]) < _PROMPT_CACHE_TTL_SEC:
        return cached[1]

    operators = _read_csv_text(OPERATORS_CSV)
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

    parts.append('위 학습 자료를 바탕으로 PASS 7개 이상을 노리는 12개 알파를 JSON 으로만 출력하라.')
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
        '12개 알파 중 절반 정도는 이 building block 의 변형/조합, 나머지는 새로운 시도가 좋다.',
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
    parts.append('이 통계는 권장사항. 다양성도 중요하니 단일 operator/field 에 12개 모두 몰리지 마라.')
    return '\n'.join(parts)


def _build_user_prompt_full(round_num: int, feedback: list[dict],
                             errors: list[dict],
                             avoid_codes: list[str] | None = None,
                             submitted_codes: list[str] | None = None,
                             seeds: list[dict] | None = None,
                             pref_stats: dict | None = None) -> str:
    operators = _read_csv_text(OPERATORS_CSV)
    datafields = _read_csv_text(DATAFIELDS_CSV)
    parts = [
        f"라운드 #{round_num} — 12개 알파를 새로 생성하라.",
        "",
        "===== brain_operators.csv =====",
        operators,
        "",
        "===== IQC_brain_datafields.csv =====",
        datafields,
        "",
        _build_challenging_section(),
        _build_dynamic_section(round_num, feedback, errors),
        _build_building_blocks_section(seeds or []),
        _build_preference_section(pref_stats or {}),
        _build_submitted_anticorr_section(submitted_codes or []),
        _build_avoid_codes_section(avoid_codes or []),
    ]
    return '\n'.join(parts)


def _build_user_prompt_cached(round_num: int, feedback: list[dict],
                               errors: list[dict],
                               avoid_codes: list[str] | None = None,
                               submitted_codes: list[str] | None = None,
                               seeds: list[dict] | None = None,
                               pref_stats: dict | None = None) -> str:
    return f"라운드 #{round_num} — 12개 알파를 새로 생성하라.\n\n" + \
           _build_challenging_section() + '\n' + \
           _build_dynamic_section(round_num, feedback, errors) + '\n' + \
           _build_building_blocks_section(seeds or []) + '\n' + \
           _build_preference_section(pref_stats or {}) + '\n' + \
           _build_submitted_anticorr_section(submitted_codes or []) + '\n' + \
           _build_avoid_codes_section(avoid_codes or [])


def _build_challenging_section() -> str:
    """캐시 히트 누적 시 동일 패턴 반복 방지용 — 도전적/다양한 archetype 적극 권장.

    이 섹션은 prompt 의 가장 앞쪽 (dynamic_section 직전) 에 배치 → Gemini 가 가장 먼저 읽음.
    """
    return '\n'.join([
        '',
        '===== 🔥 다양성 / 도전적 전략 의무사항 =====',
        '최근 라운드 캐시 히트가 누적되고 있다. 같은 패턴을 반복 생성 중이라는 강한 신호다.',
        '아래 12개 중 최소 6개는 다음 카테고리에서 가져와라 (절대 같은 archetype 8개+ 금지):',
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


def _filter_by_lint(strategies: list[dict], log_fn: Callable | None = None) -> list[dict]:
    try:
        from . import alpha_lint
    except Exception:
        return strategies
    clean: list[dict] = []
    rejected: list[tuple[int, str, list[str]]] = []
    for s in strategies:
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
    log_fn: Callable | None = None,
) -> list[dict]:
    """12개 알파 생성. user_id 별 API 키 받음.

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

    attempt = 0
    while True:
        attempt += 1
        last_err = None
        for m in chain:
            try:
                cache_name = _get_or_create_prompt_cache(client, m, api_key, log_fn=log_fn)
                if cache_name:
                    user_prompt = _build_user_prompt_cached(round_num, feedback or [], errors or [], avoid_codes or [], submitted_codes or [], seeds or [], pref_stats or {})
                    cfg = genai_types.GenerateContentConfig(
                        cached_content=cache_name,
                        response_mime_type='application/json',
                        response_schema=_RESPONSE_SCHEMA,
                        temperature=temperature,
                        max_output_tokens=8192,
                    )
                else:
                    user_prompt = _build_user_prompt_full(round_num, feedback or [], errors or [], avoid_codes or [], submitted_codes or [], seeds or [], pref_stats or {})
                    cfg = genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION,
                        response_mime_type='application/json',
                        response_schema=_RESPONSE_SCHEMA,
                        temperature=temperature,
                        max_output_tokens=8192,
                    )
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
                clean = _filter_by_lint(strategies, log_fn=log_fn)
                for new_idx, s in enumerate(clean, start=1):
                    s['idx'] = new_idx
                if len(clean) < 12 and log_fn:
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
                    _PROMPT_CACHE.pop((m, _csv_signature(), _api_key_hash(api_key)), None)
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
            f"라운드 #{round_num}-{phase} (focused — Self-Correlation 회피) — 12개 직교화 변형 알파를 생성하라.",
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
            "✅ 12개 모두 **서로 다른 직교화 차원** 으로 만들어라. 다음 중 분산되게:",
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
        f"라운드 #{round_num}-{phase} (focused sub-round — fail 개선) — 12개 변형 알파를 생성하라.",
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
        "다음 12개의 변형을 만들되, 각각 명확히 다른 접근으로 실패 테스트를 개선해야 한다:",
        "  1. 부모 코드의 핵심 구조는 유지하되 한두 부분만 의미 있게 바꿔라.",
        "  2. FAIL 한 테스트의 cutoff 와 방향을 의식해서 (예: turnover 가 너무 낮다면 더 활발한 시그널 필요).",
        "  3. window 크기, decay, neutralization, ts_* 함수, rank/zscore wrapping 변경 시도.",
        "  4. 다른 datafield 로 일부 교체. 단 검증된 building block 은 유지.",
        "  5. 12개가 동일한 변형 패턴이면 안 됨. 서로 다른 가설을 12개.",
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
                clean = _filter_by_lint(strategies, log_fn=log_fn)
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
