"""focus_priority — Closeness-to-pass scoring for the focus queue.

Convention
----------
closeness_score(fail_items) -> float

  Higher means CLOSER to passing — use this as a sort key with reverse=True
  to get near-misses first.

  Formula: score = -sum(relative_gaps)   when at least one item parses
           score = NEUTRAL_SCORE         when no items parse (neutral sentinel)

  For each fail item that can be parsed as a dict with numeric ``value`` and
  ``cutoff`` keys, or as a string matching:
    'of VALUE is [below|above] cutoff of CUTOFF'
  we compute:
    relative_gap = abs(cutoff - value) / max(abs(cutoff), 1e-9)

  Near-miss example:  Sharpe 1.20 vs cutoff 1.25 → gap ≈ 0.04 → score ≈ -0.04
  Far-miss  example:  Fitness 0.30 vs cutoff 1.0  → gap = 0.70 → score = -0.70
  Neutral   (no parseable items)                  → score = -1e9 (sorts last)

Usage in worker.py (safe, additive — can't crash a round):

    from server.focus_priority import closeness_score
    focus_queue = sorted(
        focus_queue,
        key=lambda e: closeness_score(e.get('parent_fail_items') or []),
        reverse=True,
    )
"""

from __future__ import annotations

import re

# Regex: captures VALUE and CUTOFF from WQB fail strings such as
#   'Fitness of 0.91 is below cutoff of 1.'
#   'Sharpe of 1.10 is below cutoff of 1.25.'
#   'Sub-universe Sharpe of 0.78 is below cutoff of 1.0.'
NEUTRAL_SCORE: float = -1e9
"""Sentinel returned when no fail items are parseable; sorts last in a reverse sort."""

HOPELESS_SCORE: float = -1e6
"""Sentinel for un-fixable fails (IS_LADDER_SHARPE 등). NEUTRAL 보다 크게 잡아
worker 의 far-miss 필터(`NEUTRAL < score < FLOOR`)에 걸려 focus 큐에서 제외된다 —
NEUTRAL 로 주면 '파싱불가는 자르지 않는다' 예외로 오히려 통과해 버린다."""

# Regex: captures VALUE and CUTOFF from WQB fail strings such as
#   'Fitness of 0.91 is below cutoff of 1.'
#   'Sharpe of 1.10 is below cutoff of 1.25.'
#   'Sub-universe Sharpe of 0.78 is below cutoff of 1.0.'
#   'Turnover of 27.24% is above cutoff of 1%.'   (trailing % stripped)
_FAIL_RX = re.compile(
    r'of\s+(\d+(?:\.\d+)?)%?\s+is\s+(?:below|above)\s+cutoff\s+of\s+(\d+(?:\.\d+)?)%?',
    re.IGNORECASE,
)

# PROD_CORRELATION 값 추출 — 두 표기 모두:
#   'PROD_CORRELATION(0.8428 vs 0.7)'  (거절 사유 요약)
#   'Prod Correlation of 0.84 is above cutoff of 0.7'  (WQB 원문)
_PROD_RX = re.compile(
    r'prod[\s_]?correlation\D*?(\d+(?:\.\d+)?)\s*(?:vs|is\s+above\s+cutoff\s+of)\s+(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

PROD_HOPELESS_MARGIN: float = 0.05
"""컷(0.7)을 이만큼 넘긴 PROD_CORRELATION 은 축 소진 — 형제를 다시 써도 0.8대
유지(α#34879, 8/2 #51=0.8428 실측). decay/회전율 연마로 못 내리므로 손절한다.
컷 근처(<0.05 초과)는 중립화 변경 등으로 내려갈 여지가 있어 살려 둔다."""


def closeness_score(fail_items: object) -> float:
    """Compute closeness score for a list of WQB fail description strings.

    Returns a float:
      - Closer to 0  => near-miss (small gap between value and cutoff).
      - More negative => farther from passing.
      - NEUTRAL_SCORE  => neutral sentinel (no parseable items; sorts last).

    Safe: never raises; accepts any input type.
    """
    if not fail_items:
        return NEUTRAL_SCORE  # neutral sentinel — sorts last

    # Accept any iterable of items; coerce each to str.
    try:
        items = list(fail_items)
    except TypeError:
        return NEUTRAL_SCORE  # neutral sentinel

    # 사다리(IS_LADDER_SHARPE) 실패 = 최근 구간 수익이 죽은 신호 — decay/회전율
    # 연마로 못 고친다. 8/1 밤 실측: ladder≈0 클러스터(S 2.9~3.0) 정제에 라운드
    # 20여 개가 통째로 낭비됐다. 측정 가능한 far-miss 로 분류해 큐에서 손절한다.
    try:
        if any('ladder' in str(it).lower() for it in items):
            return HOPELESS_SCORE
    except Exception:
        pass

    # 소진 축(PROD_CORRELATION 대폭 초과)도 같은 병 — 8/2 오후 #51(0.8428 vs 0.7)
    # 연마에 focus 라운드 3개가 낭비됐다. 컷 근처만 살리고 나머지는 손절.
    try:
        for it in items:
            if isinstance(it, dict):
                if 'prod' in str(it.get('name', '')).lower():
                    v, c = it.get('value'), it.get('cutoff')
                    if (v is not None and c is not None
                            and float(v) - float(c) >= PROD_HOPELESS_MARGIN):
                        return HOPELESS_SCORE
                continue
            m = _PROD_RX.search(str(it))
            if m and float(m.group(1)) - float(m.group(2)) >= PROD_HOPELESS_MARGIN:
                return HOPELESS_SCORE
    except Exception:
        pass

    total_gap = 0.0
    parsed = 0

    for item in items:
        value = None
        cutoff = None
        # 1) dict fail items (API harvest / is_status dicts) with numeric value+cutoff
        if isinstance(item, dict):
            v = item.get('value')
            c = item.get('cutoff')
            try:
                if v is not None and c is not None:
                    value = float(v)
                    cutoff = float(c)
            except (ValueError, TypeError):
                value = cutoff = None
        # 2) fall back to parsing the WQB human-readable string
        if value is None or cutoff is None:
            try:
                text = str(item)
            except Exception:
                continue
            m = _FAIL_RX.search(text)
            if not m:
                continue
            try:
                value = float(m.group(1))
                cutoff = float(m.group(2))
            except (ValueError, IndexError):
                continue
        gap = abs(cutoff - value) / max(abs(cutoff), 1e-9)
        total_gap += gap
        parsed += 1

    if parsed == 0:
        # Neutral: no parseable items.  Use a large negative sentinel so these
        # entries sort AFTER any entry with a measurable gap (near or far), since
        # at least some gap information is better than none.
        return NEUTRAL_SCORE

    return -total_gap  # higher (less negative) => closer to pass


def advance_focus_queue(
    queue: object,
    round_num: int,
    phase: int,
    parent_idx: int,
    status: str,
    max_attempts: int = 5,
) -> tuple[list, str]:
    """Remove the just-processed focus entry from the queue.

    Why this is not a plain ``queue[1:]`` (the round-560 infinite-loop bug)
    ---------------------------------------------------------------------
    The focus queue is *selected* by sorting on ``closeness_score`` (near-miss
    first), so the entry the worker actually processed is **not** necessarily
    ``queue[0]`` (the FIFO front).  Removing by FIFO front therefore fails to
    remove the processed entry whenever the highest-closeness entry sits behind
    another in the queue — and the same entry is re-selected every round forever
    (round 560 stuck on phase 1 / parent_idx 8 for two days).  The fix: match the
    processed entry by ``(round_num, phase, parent_idx)`` wherever it sits and
    remove that one.

    Semantics by ``status``:
      - ``'done'``      : the sub-round for this (parent, phase) is consumed →
                          remove the entry (one phase == one attempt, the
                          original PHASES_PER_PARENT intent).
      - anything else   : treated as a failed attempt (e.g. ``'error'``).
                          Increment the entry's ``attempts`` and remove it only
                          once ``attempts >= max_attempts`` — defense-in-depth so
                          a persistently-erroring entry (it never reaches
                          ``done``, so it would otherwise never be popped) cannot
                          loop forever either.  Callers should NOT pass paused /
                          interrupted rounds here.

    Returns ``(new_queue, action)`` where ``action`` is one of
    ``'removed' | 'giveup' | 'retry' | 'nomatch'``.

    Pure and defensive: never mutates ``queue`` and never raises.
    """
    try:
        q = [dict(e) for e in (queue or [])]
    except Exception:
        return [], 'nomatch'

    def _matches(e: dict) -> bool:
        try:
            return (int(e.get('parent_round_num') or 0) == int(round_num)
                    and int(e.get('phase') or 0) == int(phase)
                    and int(e.get('parent_idx') or 0) == int(parent_idx))
        except Exception:
            return False

    i = next((k for k, e in enumerate(q) if _matches(e)), None)
    if i is None:
        return q, 'nomatch'

    if status == 'done':
        q.pop(i)
        return q, 'removed'

    # Non-done (error/etc.): count an attempt; give up after max_attempts.
    attempts = int(q[i].get('attempts') or 0) + 1
    if attempts >= max(1, int(max_attempts)):
        q.pop(i)
        return q, 'giveup'
    q[i]['attempts'] = attempts
    return q, 'retry'
