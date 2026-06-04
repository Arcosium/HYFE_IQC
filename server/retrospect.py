"""Per-axis effectiveness leaderboards + round-trend retrospection (adaptive exploration).

Pure module — no IO, no DB imports. All functions are deterministic.

Part of the adaptive-exploration feature gated by run_config.is_bandit_enabled().
"""

from __future__ import annotations

import math
from typing import Any


def adaptive_epsilon(trend: float, base: float = 0.2,
                     lo: float = 0.05, hi: float = 0.5,
                     k: float = 2.0) -> float:
    """탐색률 epsilon 을 라운드 성과 추세로 동적 조절.

    trend=0 → base. trend>0(개선 중) → lo 쪽으로 감소(악용↑).
    trend<0(정체/악화) → hi 쪽으로 증가(탐색↑).

    base 를 중립점으로, 양쪽 진폭을 비대칭으로(아래=base-lo, 위=hi-base) 잡아
    각각 lo/hi 에 점근한다. k=tanh 기울기. 결과는 [lo, hi] 로 clamp.

    Pure and deterministic.
    """
    t = math.tanh(trend * k)   # (-1, 1); t>0 improving
    if t >= 0:
        eps = base - (base - lo) * t      # → lo as t → 1
    else:
        eps = base + (hi - base) * (-t)   # → hi as t → -1
    return max(lo, min(hi, eps))


def format_effectiveness_priors(axis_results: dict[str, list[dict[str, Any]]],
                                  operator_results: list[dict[str, Any]]) -> str:
    """Build a concise soft-nudge prompt block from per-axis + operator leaderboards.

    axis_results: {'universe':[...], 'neutralization':[...], 'decay':[...], ...}
    operator_results: [{'operator':str, 'count':int, 'all_pass_rate':float, ...}, ...]

    Wording is explicitly soft (참고용, 강제 아님) per diversity-over-safety policy.
    Returns '' if all inputs are empty.
    """
    parts: list[str] = []

    # Collect non-empty axes
    axis_lines: list[str] = []
    for axis in ('universe', 'neutralization', 'decay', 'region'):
        items = (axis_results or {}).get(axis) or []
        if not items:
            continue
        # Top 3 values with non-zero count, formatted as "VALUE(rate)"
        top = [x for x in items[:3] if x.get('count', 0) >= 1]
        if not top:
            continue
        vals = ', '.join(
            f"{x['value']}(pass율 {x['all_pass_rate']:.2f})"
            for x in top
        )
        axis_lines.append(f'    {axis}: {vals}')

    op_lines: list[str] = []
    if operator_results:
        top_ops = [x for x in operator_results[:3] if x.get('count', 0) >= 2]
        if top_ops:
            vals = ', '.join(
                f"{x['operator']}(pass율 {x['all_pass_rate']:.2f})"
                for x in top_ops
            )
            op_lines.append(f'    outer-op: {vals}')

    if not axis_lines and not op_lines:
        return ''

    parts.append('')
    parts.append('[최근 성과 상위 (데이터 기반 참고용, 강제 아님):]')
    parts.extend(axis_lines)
    parts.extend(op_lines)
    parts.append('')
    return '\n'.join(parts)
