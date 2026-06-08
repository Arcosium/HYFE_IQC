"""knowledge_base — SC-saturation detection. The submitted-alpha pool over-uses
certain signal-defining operators; alphas reusing them hit self-corr>0.7
(window/neutralization tweaks can't break it — must rotate the operator family).
This flags those operators so the generator can diversify. Pure; never raises."""
from __future__ import annotations

import math

from . import alpha_ast

# Signal-defining operators (exclude ubiquitous wrappers rank/add/scale/abs/sign/...).
SIGNAL_OPS = frozenset({
    'ts_corr', 'ts_zscore', 'ts_delta', 'ts_mean', 'ts_sum', 'ts_std_dev',
    'ts_decay_linear', 'ts_rank', 'signed_power', 'group_neutralize', 'trade_when',
    'ts_av_diff', 'ts_min', 'ts_max', 'hump', 'winsorize', 'ts_delay',
    'ts_regression', 'vec_avg', 'ts_arg_max', 'ts_arg_min',
    'group_zscore', 'group_rank', 'ts_covariance',
})


def saturated_operators(codes, frac: float = 0.25, floor: int = 4):
    """Return [(op, count), ...] for SIGNAL_OPS used in >= max(floor, ceil(frac*N))
    distinct codes, sorted by count desc. [] on empty / failure. Never raises."""
    try:
        items = [c for c in (codes or []) if isinstance(c, str) and c.strip()]
        n = len(items)
        if n == 0:
            return []
        threshold = max(int(floor), int(math.ceil(float(frac) * n)))
        counts: dict[str, int] = {}
        for code in items:
            try:
                ops = alpha_ast.operators_used(code)
            except Exception:
                ops = set()
            for op in ops:
                if op in SIGNAL_OPS:
                    counts[op] = counts.get(op, 0) + 1
        sat = [(op, c) for op, c in counts.items() if c >= threshold]
        sat.sort(key=lambda t: (-t[1], t[0]))
        return sat
    except Exception:
        return []


def render_saturation_warning(saturated) -> str:
    """Render the saturation list as a soft prompt block. '' when empty."""
    try:
        sat = list(saturated or [])
    except Exception:
        return ''
    if not sat:
        return ''
    ops = ', '.join(f'{op}({c})' for op, c in sat)
    return ('[SC-포화 경고 — 제출풀 편중 회피]\n'
            '제출풀이 다음 신호 연산자에 편중돼 있어, 이들을 핵심으로 쓰는 알파는 self-corr>0.7 로 '
            '막힌다(창/중립화 조정으론 못 깬다 — 연산자 패밀리를 바꿔야 한다). 가능하면 이들 사용을 줄이고 '
            f'덜 쓴 연산자/데이터셋/구조로 직교화하라: {ops}')
