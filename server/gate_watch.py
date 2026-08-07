"""gate_watch — 제출 게이트를 **실측으로** 배운다 (2026-08-05 사장 지시).

왜 필요한가
-----------
`criteria.is_blocking` 은 하드코딩된 규칙이다. WQB 가 집행을 바꾸면 규칙도 따라가야 하는데,
답은 이미 우리 DB 안에 있다 — 거절 응답과 성공 이력이 그것이다. 새 API 호출은 없다.

  · 소프트 = 제출에 **성공한** 알파의 fail_items 에 있던 체크. FAIL 인데도 통과했으니 **증명**이다.
  · 하드 = 거절 사유(`submit_status`)에만 등장하고 소프트 증거가 없는 체크.

⚠ 거절 사유에 이름이 있다는 것 자체는 증거가 아니다. WQB 는 403 본문에 그 알파의 FAIL 을
전부 싣기 때문에, 흔한 FAIL 은 원인이 아니어도 늘 이름이 오른다. 그래서 **소프트가 이긴다**
(2026-08-07 수정 — 자세한 근거는 아래 observe 안의 주석).

⚠ 이 모듈은 원래 "8/3 부터 LOW_FITNESS 가 하드로 바뀌었다"는 진단 위에 만들어졌는데,
그 진단은 8/6 에 오진으로 확정돼 철회됐다. LOW_FITNESS 단독 사유 403 은 전 기간 0건이고
제출 성공작 22건의 fitness 는 전부 컷 1.0 미만이다. 모듈의 값어치는 남되 전제는 사라졌다.
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
    # ⚖ 증거의 무게가 비대칭이다 (2026-08-07 수정).
    #   · "FAIL 인데도 제출됐다" = 그 체크가 안 막는다는 **증명**이다.
    #   · "거절 사유 목록에 이름이 있었다" = 증명이 아니다. WQB 는 403 본문에 그 알파의
    #     FAIL 을 **전부** 싣는다. 흔한 FAIL 은 원인이 아니어도 무조건 이름이 오른다.
    # 그래서 소프트가 하드를 이긴다. 최근순 비교로 두면 거절이 성공보다 5~20배 많아
    # 어떤 이름이든 마지막 관측이 거절이 되고, soft 집합이 **영구히 빈다**
    # (실측: 거절 135 · 성공 23 → soft=[], LOW_FITNESS 가 하드로 굳어 있었다.
    #  그 알파들은 fitness 0.26~0.86 로 22건 전원 제출에 성공한 부류다).
    # 집행이 진짜 바뀌면 그 체크를 달고 성공하는 알파가 끊기고, WINDOW_S 안에서
    # 소프트 증거가 만료돼 자동으로 하드로 넘어간다. 늦게 틀리는 대신 안전하게 틀린다
    # — 소프트로 틀리면 공짜 거절 한 번이고, 하드로 틀리면 제출 기회를 통째로 버린다.
    soft = set(soft_at)
    hard = set(hard_at) - soft
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
