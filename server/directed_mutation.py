"""directed_mutation — map a near-miss alpha's FAILING IS metrics to a targeted
mutation directive injected into the focused-generation prompt. Replaces blind
re-generation with metric-specific guidance. Pure; never raises."""
from __future__ import annotations

import re

# 'Sharpe of 1.55 is below cutoff of 2.'  /  'Turnover of 95.0% is above cutoff of 70%.'
_RX = re.compile(
    r'([A-Za-z][\w\- ]*?)\s+of\s+(-?\d+(?:\.\d+)?)%?\s+is\s+(below|above)\s+cutoff\s+of\s+(\d+(?:\.\d+)?)%?',
    re.IGNORECASE,
)


def route(fail_items, code: str = '') -> dict:
    """Return {'strategy': str, 'instruction': str}. instruction is '' when no
    actionable failing metric is recognised. Never raises."""
    try:
        items = [str(x) for x in (fail_items or [])]
    except Exception:
        return {'strategy': 'generic', 'instruction': ''}

    strategies: list = []
    lines: list = []

    for it in items:
        try:
            m = _RX.search(it)
        except Exception:
            m = None
        if m:
            metric = m.group(1).strip().lower()
            try:
                value = float(m.group(2))
            except Exception:
                value = 0.0
            direction = m.group(3).lower()
            if 'sub-universe' in metric or 'sub universe' in metric or 'subuniverse' in metric:
                strategies.append('subuniv')
                lines.append('• Sub-universe Sharpe 미달 — group_neutralize(x, sector|industry) 로 '
                             '그룹중립화해 하위유니버스 안정성을 높여라.')
            elif 'sharpe' in metric:
                if value < 0:
                    strategies.append('sign_flip')
                    lines.append('• Sharpe 가 음수다 — 신호 방향이 반대다. 식 전체 앞에 -1 * 를 붙여 부호를 뒤집어라.')
                else:
                    strategies.append('raise_sharpe')
                    lines.append('• Sharpe 가 컷 미달 — 직교 신호를 add(zscore(a), zscore(b), filter=true) 로 '
                                 '더하거나 rank(ts_rank(x, 40)) 이중랭크로 안정화하고 group_neutralize 로 '
                                 '시장노이즈를 제거해 Sharpe 를 올려라.')
            elif 'fitness' in metric:
                strategies.append('raise_fitness')
                lines.append('• Fitness 미달 — Fitness=Sharpe×√(|Ret|/max(Turnover,0.125)). 회전을 '
                             'ts_decay_linear 로 낮추거나 2개 이상 차원을 결합해 |수익|을 키워라.')
            elif 'turnover' in metric:
                if direction == 'above':
                    strategies.append('cut_turnover')
                    lines.append('• Turnover 가 너무 높다 — 최종 신호를 ts_decay_linear(rank(x), 8~15) 로 감싸고 '
                                 'trade_when(저변동 조건, x, -1) 게이트 + hump(x, hump=0.03) 으로 회전을 직접 줄여라.')
                else:
                    strategies.append('raise_turnover')
                    lines.append('• Turnover 가 너무 낮다(과다 스무딩) — decay/hump/ts_mean 평활을 줄여 신호가 '
                                 '죽지 않게 하되 1% 이상 유지하라.')
            elif 'weight' in metric or 'concentration' in metric:
                strategies.append('spread_weight')
                lines.append('• Weight 집중(>10%) — 바깥에 rank()/winsorize 를 씌우고 truncation 을 낮추거나 '
                             'group_neutralize 로 포지션을 분산하라.')
            elif 'correlation' in metric:
                strategies.append('rotate_family')
                lines.append('• Self-correlation 벽 — 같은 연산자패밀리는 ~3-5개 후 포화한다. 창/중립화 조정으론 '
                             '못 깬다. 연산자 패밀리 자체를 바꿔라(예: corr류→decay/regression류, rank류→trade_when류).')
            else:
                continue
        else:
            low = it.lower()
            if 'weight' in low and ('distribut' in low or 'concentrat' in low or '집중' in low):
                strategies.append('spread_weight')
                lines.append('• Weight 집중(>10%) — 바깥에 rank()/winsorize 를 씌우고 truncation 을 낮추거나 '
                             'group_neutralize 로 포지션을 분산하라.')
            elif 'self-correlation' in low or 'self correlation' in low or 'correlation' in low:
                strategies.append('rotate_family')
                lines.append('• Self-correlation 벽 — 같은 연산자패밀리는 ~3-5개 후 포화한다. 창/중립화 조정으론 '
                             '못 깬다. 연산자 패밀리 자체를 바꿔라(예: corr류→decay/regression류, rank류→trade_when류).')

    if not lines:
        return {'strategy': 'generic', 'instruction': ''}
    header = '[지표 기반 개선 지시 — 부모의 실패 지표를 정확히 겨냥하라]'
    return {'strategy': '+'.join(strategies), 'instruction': header + '\n' + '\n'.join(lines)}
