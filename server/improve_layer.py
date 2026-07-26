"""improve_layer — 회전율 조건부 결정론 개선 레이어 (WQB AAF Alpha-improvement 이식).

focus 부모의 실측 turnover 등급에 맞춰 AAF 슬라이드의 주기성 그리드를 적용한다:

  - LT  (<0.20): 갱신이 느린 신호 — lookback 을 연 단위(252/504/756)로 재스케일
  - MID (<0.40): 20/63/252/504 전 대역 시도
  - HT  (>=0.40): 20/63 단기 + trade_when(volume>adv20, x, -1) 회전 게이트
                  + decay 증폭(설정만 변경, 코드 동일)

창 재스케일은 지배 창(코드 내 최대 정수 창)을 그리드 목표값으로 놓고 나머지
정수 창을 같은 비율로 늘여 다중 스케일 구조를 보존한다.

순수 모듈: IO/DB 없음, 예외 안 던짐. Gemini 비용 0. 후보는 워커의 기존
파이프라인(repair→lint→hygiene→캐시→시뮬→DB)을 그대로 통과한다.
"""
from __future__ import annotations

import re

from .reward import _f

# 개선 후보의 idx 대역 — 유전체(1..8)·재조합(31+)과 겹치지 않게 41+.
IDX_BASE = 41

# AAF: 회전율 등급별 주기성 그리드 (거래일).
GRIDS = {
    'LT': (252, 504, 756),
    'MID': (63, 252, 504, 20),
    'HT': (20, 63),
}
LT_MAX = 0.20
MID_MAX = 0.40

# 창으로 간주하는 정수 범위. 5 미만은 지수/스케일 상수일 가능성이 높아 안 건드린다.
_WIN_MIN, _WIN_MAX = 5, 756
# ponytail: 정수 토큰 일괄 재스케일 — ts 창이 아닌 계수(예: `20 * scale(x)`)도 함께
# 스케일된다. 문제가 되면 alpha_ast 로 ts_* 연산자 인자만 겨냥하도록 올릴 것.
_INT_RX = re.compile(r'(?<![\w.])(\d{1,4})(?![\w.])')


def rescale_windows(code: str, target: int) -> str | None:
    """코드의 지배 창을 target 으로 놓고 모든 창 정수를 비례 재스케일.

    창이 없거나 결과가 원본과 같으면 None.
    """
    ints = [int(m) for m in _INT_RX.findall(code) if _WIN_MIN <= int(m) <= _WIN_MAX]
    if not ints:
        return None
    ratio = float(target) / max(ints)

    def _sub(m: re.Match) -> str:
        v = int(m.group(1))
        if not (_WIN_MIN <= v <= _WIN_MAX):
            return m.group(1)
        return str(max(2, min(_WIN_MAX, round(v * ratio))))

    new = _INT_RX.sub(_sub, code)
    return new if new != code else None


def turnover_class(turnover) -> str:
    t = _f(turnover)
    if t < LT_MAX:
        return 'LT'
    if t < MID_MAX:
        return 'MID'
    return 'HT'


def variants(parent_code: str, parent_settings: dict | None,
             parent_metrics: dict | None, *, n: int = 3, rng=None) -> list[dict]:
    """부모 알파의 결정론 개선 후보 전략 dict ≤ n 개.

    각 dict 은 워커 strategies 항목과 같은 모양:
    {idx, code, desc, settings, origin='improve'}.
    parent_alpha_id 는 호출부(focus 큐가 안다)가 채운다. 절대 예외를 안 던진다.
    """
    try:
        code = str(parent_code or '').strip()
        if not code or n <= 0:
            return []
        settings = dict(parent_settings or {})
        klass = turnover_class((parent_metrics or {}).get('turnover'))

        cands: list[tuple[str, str, dict]] = []   # (code, desc, settings)
        for g in GRIDS[klass]:
            new = rescale_windows(code, g)
            if new:
                cands.append((new, f'🔧 개선[{klass}·창→{g}d]: 부모 창 비례 재스케일',
                              dict(settings)))
        if klass == 'HT':
            cands.append((f'trade_when(volume > adv20, {code}, -1)',
                          '🔧 개선[HT·trade_when]: 고거래량 이벤트 게이트로 회전 절감',
                          dict(settings)))
            try:
                cur_decay = int(float(str(settings.get('decay') or 0)))
            except (TypeError, ValueError):
                cur_decay = 0
            s2 = dict(settings)
            s2['decay'] = str(min(15, max(8, cur_decay * 2)))
            cands.append((code,
                          f'🔧 개선[HT·decay→{s2["decay"]}]: 감쇠 증폭으로 회전 절감',
                          s2))

        # 중복 제거(코드+settings 동일) 후 n 개로 절단. 그리드가 n 보다 크면 rng 로
        # 앞부분을 섞어 부모마다 다른 부분집합이 시도되게 한다.
        seen: set[tuple] = set()
        uniq = []
        for c, d, s in cands:
            key = (c, tuple(sorted(s.items())))
            if key in seen:
                continue
            seen.add(key)
            uniq.append((c, d, s))
        if rng is not None and len(uniq) > n:
            rng.shuffle(uniq)
        return [{
            'idx': IDX_BASE + i,
            'code': c,
            'desc': d,
            'settings': s,
            'origin': 'improve',
        } for i, (c, d, s) in enumerate(uniq[:n])]
    except Exception:
        return []
