"""Power Pool 제출용 알파 설명(Idea + Rationale) 생성기 — 결정론, LLM 무관 (2026-07-23).

왜 필요한가
-----------
"Getting Started with Power Pool Alphas" 문서:
  "To submit a Power Pool Alpha, it is **mandatory** to describe the idea in at least
   100 characters ... Using the template of **Idea and Rationale**. Otherwise the Alpha
   will not be eligible for Power Pool."
지금까지 머신은 알파 설명을 아예 설정하지 않았다 — 즉 제출에 성공해도 Power Pool
자격이 없는 상태였다. 이 모듈이 유전체·코드에서 문서 예시와 같은 형식의 영어 설명을
결정론적으로 조립하고, wqb_backend 가 제출 직전에 PATCH 로 심는다.

LLM 을 쓰지 않는 이유: 제출 경로는 지연·실패에 민감하고(제출 락 안), 유전체는 이미
구조화돼 있어 템플릿 조립만으로 문서 예시 수준의 설명이 나온다. 항상 100자를 넘긴다.
"""
from __future__ import annotations

import re

# ── 유전자 → 영어 서술 사전 ──────────────────────────────────────────────────

_FAMILY_TEXT = {
    'pv': 'price/volume microstructure data',
    'fundamental': 'fundamental financial statement data',
    'analyst': 'analyst estimate and revision data',
    'option': 'option-market implied information',
    'news': 'news flow and sentiment data',
    'model': 'pre-computed factor model scores',
    'shortinterest': 'short interest / securities lending data',
    'imbalance': 'order-flow imbalance data',
}

_COMBINE_TEXT = {
    'spread': 'the spread between two normalized signals',
    'sum': 'the weighted combination of two complementary signals',
    'triple': 'the combination of three complementary signals',
    'product': 'the interaction (product) of two signals, so the position is largest '
               'when both agree',
    'ratio': 'one signal scaled by the magnitude of another',
    'corr': 'the rolling correlation between two signals, capturing regime alignment',
    'resid': 'the component of one signal that is orthogonal to another '
             '(vector-neutralized residual), isolating information the second signal '
             'does not already carry',
}

_SIGN_TEXT = {
    1: 'The alpha follows the signal (continuation).',
    -1: 'The alpha fades the signal, betting on mean reversion.',
}

_OP_TEXT = {
    'rank': 'rank(): converts raw values into cross-sectional ranks so positions are '
            'comparable across stocks and robust to outliers',
    'ts_rank': 'ts_rank(): ranks the current value within its own recent history, '
               'measuring how extreme today is for that stock',
    'ts_zscore': 'ts_zscore(): standardizes the value against its rolling mean and '
                 'stdev, producing a stationary surprise measure',
    'ts_delta': 'ts_delta(): change versus N days ago — favors changes over levels',
    'ts_mean': 'ts_mean(): rolling average, used for smoothing and turnover control',
    'ts_av_diff': 'ts_av_diff(): deviation from the rolling average, a short-horizon '
                  'surprise measure',
    'ts_corr': 'ts_corr(): rolling correlation between the two inputs',
    'ts_decay_linear': 'ts_decay_linear(): linearly-weighted smoothing that favors '
                       'recent observations while damping turnover',
    'ts_std_dev': 'ts_std_dev(): rolling volatility of the input',
    'winsorize': 'winsorize(): clips extreme outliers so a few names cannot dominate '
                 'the position',
    'ts_backfill': 'ts_backfill(): fills sparse observations with the last valid '
                   'value, keeping coverage stable',
    'group_neutralize': 'group_neutralize(): demeans the signal within its group so '
                        'the bet is purely relative, not a group-level tilt',
    'group_rank': 'group_rank(): ranks the signal within its group',
    'group_zscore': 'group_zscore(): standardizes the signal within its group',
    'vec_avg': 'vec_avg(): averages the vector field per day',
    'vec_sum': 'vec_sum(): sums the vector field per day',
    'vector_neut': 'vector_neut(): removes the projection of one signal on another, '
                   'keeping only the orthogonal residual (decorrelation)',
    'hump': 'hump(): suppresses small signal changes to cut unnecessary rebalancing',
    'trade_when': 'trade_when(): holds positions only while the entry condition is '
                  'true, controlling turnover and regime exposure',
    'abs': 'abs(): magnitude of the input',
    'add': 'add(): sums the component signals',
    'if_else': 'conditional operator: separates regimes so the alpha only bets when '
               'the condition holds',
}

_NEUT_TEXT = {
    'STATISTICAL': 'statistical risk factors (principal components)',
    'CROWDING': 'crowding risk factors',
    'REVERSION_AND_MOMENTUM': 'reversion and momentum risk factors',
    'FAST': 'fast-horizon risk factors',
    'SLOW': 'slow-horizon risk factors',
    'SLOW_AND_FAST': 'slow and fast risk factors',
    'MARKET': 'the market',
    'INDUSTRY': 'industry groups',
    'SUBINDUSTRY': 'sub-industry groups',
    'SECTOR': 'sectors',
    'NONE': None,
}


def _field_descriptions() -> dict:
    """팔레트 CSV 의 필드 → description 매핑 (없으면 빈 dict — fail-soft)."""
    try:
        from . import datafield_palette as _dp
        rows = _dp._all_rows()          # 모듈 내부 캐시 재사용 (읽기 전용)
        return {str(r.get('name') or '').strip(): str(r.get('description') or '').strip()
                for r in rows if r.get('name')}
    except Exception:
        return {}


def _ops_used(code: str) -> list[str]:
    """코드에 등장한 연산자(우리가 서술문을 가진 것만), 등장 순서 유지·중복 제거."""
    seen: list[str] = []
    for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', code or ''):
        op = m.group(1).lower()
        if op in _OP_TEXT and op not in seen:
            seen.append(op)
    if '?' in (code or '') and 'if_else' not in seen:
        seen.append('if_else')
    return seen


def build(code: str, genome: dict | None = None, settings: dict | None = None) -> str:
    """알파 한 개의 Power Pool 형식 설명 (영어, 항상 100자 이상).

    형식은 문서 예시 그대로: Idea / Rationale for data used / Rationale for
    operators used. 유전체가 없으면(레거시/스펙 외 경로) 코드만으로 조립한다.
    """
    g = dict(genome or {})
    st = dict(settings or {})
    fam = str(g.get('family') or '').strip().lower()
    combine = str(g.get('combine') or '').strip().lower()
    try:
        sign = -1 if int(g.get('sign') or 1) < 0 else 1
    except (TypeError, ValueError):
        sign = 1
    neut = str(g.get('neutralization') or st.get('neutralization') or '').strip().upper()
    fields = [str(f) for f in (g.get('fields') or []) if f][:3]

    # ── Idea ────────────────────────────────────────────────────────────────
    idea_bits: list[str] = []
    fam_txt = _FAMILY_TEXT.get(fam)
    cmb_txt = _COMBINE_TEXT.get(combine)
    if cmb_txt and fam_txt:
        idea_bits.append(f'This alpha trades {cmb_txt}, built from {fam_txt}.')
    elif fam_txt:
        idea_bits.append(f'This alpha extracts a cross-sectional signal from {fam_txt}.')
    else:
        idea_bits.append('This alpha extracts a cross-sectional relative-value signal '
                         'from the listed data fields.')
    idea_bits.append(_SIGN_TEXT[sign])
    regime = str(g.get('regime') or 'OFF')
    if regime != 'OFF':
        idea_bits.append(f'Positions are only taken in the "{regime}" market regime, '
                         'where the signal has historically been most informative.')
    neut_txt = _NEUT_TEXT.get(neut)
    if neut_txt:
        idea_bits.append(f'Positions are neutralized against {neut_txt} to isolate '
                         'the idiosyncratic component.')
    idea = ' '.join(idea_bits)

    # ── Rationale for data used ─────────────────────────────────────────────
    descs = _field_descriptions()
    data_lines: list[str] = []
    for f in fields:
        d = (descs.get(f) or '').strip()
        if d:
            if len(d) > 160:
                d = d[:157].rstrip() + '...'
            data_lines.append(f'{f}: {d}')
        else:
            data_lines.append(f'{f}: input field for the signal above')
    if not data_lines:
        data_lines.append('Fields referenced in the expression provide the raw '
                          'signal inputs.')
    data_txt = ' | '.join(data_lines)

    # ── Rationale for operators used ────────────────────────────────────────
    op_lines = [_OP_TEXT[o] for o in _ops_used(code)[:6]]
    if not op_lines:
        op_lines.append('Standard cross-sectional operators normalize the signal '
                        'and control turnover.')
    ops_txt = ' | '.join(op_lines)

    out = (f'Idea: {idea}\n'
           f'Rationale for data used: {data_txt}\n'
           f'Rationale for operators used: {ops_txt}')
    if len(out) < 100:          # 안전망 — 위 조립으로는 사실상 도달 불가
        out += ('\nThe combination of normalization, outlier control and group '
                'neutralization keeps the position well diversified.')
    return out[:4000]
