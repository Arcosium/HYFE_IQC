"""gate_watch — 제출 게이트를 **실측으로** 배운다 (2026-08-05 사장 지시).

왜 필요한가
-----------
`criteria.is_blocking` 은 하드코딩된 규칙이다. 7월엔 맞았지만 8/3 이후 틀려졌다.
`LOW_FITNESS` 가 그때까지는 미달이어도 제출을 안 막았는데(성공작 21건의 fitness 가
0.26~0.86, 전부 공식 컷 1.0 미달) 그 뒤로 단독 사유 403 을 내기 시작했다.
우리는 이틀 늦게 알았다 — **거절 응답에 답이 들어 있었는데 읽지 않았다.**

이 모듈은 DB 만으로 그 답을 복원한다. 새 API 호출이 없다.
  · 하드 = 거절 사유(`submit_status`)에 이름이 등장한 체크. WQB 가 그것 때문에 막았다는 뜻이다
  · 소프트 = 제출에 **성공한** 알파의 fail_items 에 있던 체크. FAIL 인데도 통과했다
같은 체크가 양쪽에 나오면 **최근 관측이 이긴다** — 집행이 바뀌는 게 바로 그 모습이다.
"""
from __future__ import annotations

import json
import logging
import re
import time

from . import db as _db
from . import run_config

LOG = logging.getLogger('genomicwqb.gate_watch')

#: 관측 창 — 이보다 오래된 판정은 현재 집행을 대변하지 못한다.
WINDOW_S = float(14 * 86400)
#: 체크 이름 추출 (예: 'rejected:LOW_FITNESS(0.65 vs 1.0); IS_LADDER_SHARPE(...) (http_403)')
_NAME_RX = re.compile(r'[A-Z][A-Z0-9_]{4,}')
#: 사유 문자열에 섞이는 비-체크 토큰 ('rejected:' 접두어·'(http_403)' 꼬리 등)
_NOISE = frozenset({'ERROR', 'FAIL', 'WARNING', 'PASS', 'PENDING', 'NONE',
                    'REJECTED', 'SUBMITTED', 'TRUE', 'FALSE', 'NAME'})


def _names(text) -> set:
    """체크 이름만 뽑는다. 껍데기 토큰(REJECTED·HTTP_403)이 섞이면 게이트 프로파일이 오염된다."""
    return {n for n in _NAME_RX.findall(str(text or '').upper())
            if n not in _NOISE and not n.startswith('HTTP')}


def observe(user_id: int, since_s: float | None = None) -> dict:
    """최근 판정에서 하드/소프트 체크 집합을 복원한다.

    → {'hard': [...], 'soft': [...], 'n_rejected': int, 'n_submitted': int}
    """
    window = WINDOW_S if since_s is None else float(since_s)
    hard_at: dict[str, float] = {}
    soft_at: dict[str, float] = {}
    n_rej = n_sub = 0
    try:
        rows = _db.rejection_and_success_checks(user_id, time.time() - window)
    except Exception as e:
        LOG.warning('게이트 관측 실패: %s', e)
        return {'hard': [], 'soft': [], 'n_rejected': 0, 'n_submitted': 0}
    for ts, submitted, status, fail_items in rows:
        if submitted:
            n_sub += 1
            for n in _names(json.dumps(fail_items, ensure_ascii=False)):
                soft_at[n] = max(soft_at.get(n, 0.0), float(ts or 0))
        elif str(status or '').startswith('rejected:'):
            n_rej += 1
            for n in _names(status):
                hard_at[n] = max(hard_at.get(n, 0.0), float(ts or 0))
    # 양쪽에 나오면 최근 관측이 이긴다 — 집행 변경이 정확히 이 모습이다.
    hard = {n for n, t in hard_at.items() if t >= soft_at.get(n, 0.0)}
    soft = {n for n, t in soft_at.items() if t > hard_at.get(n, 0.0)}
    return {'hard': sorted(hard), 'soft': sorted(soft),
            'n_rejected': n_rej, 'n_submitted': n_sub}


def sync(user_id: int, log_fn=None) -> dict | None:
    """관측 → 저장. 하드 집합이 **바뀌었으면** 그 변화를 반환하고 로그를 남긴다.

    집행 변경을 당일에 잡는 게 목적이다. 8/3 을 이틀 늦게 안 것이 이 모듈을 만든 이유다.
    """
    obs = observe(user_id)
    if not obs['hard'] and not obs['soft']:
        return None
    prev = run_config.get_gate_profile() or {}
    old = set(prev.get('hard') or [])
    new = set(obs['hard'])
    run_config.set_gate_profile({**obs, 'ts': time.time()})
    if not prev:
        return None                                   # 첫 관측은 변화가 아니다
    added, removed = sorted(new - old), sorted(old - new)
    if not added and not removed:
        return None
    msg = '⚖ 제출 게이트 변화 감지 —'
    if added:
        msg += f' 새로 차단: {", ".join(added)}'
    if removed:
        msg += f' 더 이상 차단 안 함: {", ".join(removed)}'
    LOG.warning(msg)
    if log_fn:
        log_fn(msg)
    return {'added': added, 'removed': removed}


def is_blocking_measured(name) -> bool | None:
    """실측 기준 차단 여부. 관측이 없으면 None (호출자가 하드코딩 규칙으로 폴백)."""
    prof = run_config.get_gate_profile() or {}
    nm = str(name or '').strip().upper()
    if not nm:
        return None
    if nm in set(prof.get('hard') or []):
        return True
    if nm in set(prof.get('soft') or []):
        return False
    return None
