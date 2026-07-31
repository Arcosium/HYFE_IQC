"""페이스 계기판 — 발굴 페이스를 스스로 재고, 필요한 두 가지에만 쓴다.

2026-07-31 사장 지시 요약: "말 안 해도 일 3개·주 25개", 이어서 "조건 걸지 말고
항상 최대로", 이어서 "라운드당 14개면 생성은 충분하다". 그래서 이 모듈은
생성량에 일절 손대지 않는다 — 신선 후보의 질은 genome_models.STRONG_TEMPLATES
(무작위 슬롯 절반에 강신호 골격, 상시)가 맡는다. 여기 남은 것은 둘뿐:

  · 탐색/착취 모드 전환 — 광맥이 터진 날은 착취가, 마른 날은 탐색이 최대
    페이스다. 기근이면 밴딧 epsilon 하한을 올린다(스로틀 아님 — 방향 전환).
  · 사람 호출 — 생체 인증 사망처럼 기계가 못 푸는 문제는 크게 알린다.

'하루'는 WQB 쿼터 리셋(13:00 KST) 경계로 센다 — 자정 기준이면 아침 기근
(예: 11시에 1개, 리셋까지 2시간)이 '정상'으로 오판된다. worker 가 라운드
시작 시 maybe_intervene() 한 번 호출하는 것이 전부다.
"""
from __future__ import annotations

import logging
import os
import time as _time

from . import db as _db

LOG = logging.getLogger('genomicwqb.pace_keeper')

DAILY_TARGET = int(os.environ.get('IQC_PACE_DAILY_TARGET', '3'))
WEEKLY_TARGET = int(os.environ.get('IQC_PACE_WEEKLY_TARGET', '25'))
EPSILON_FLOOR = float(os.environ.get('IQC_PACE_EPSILON_FLOOR', '0.35'))
DAY_BOUNDARY_HOUR = int(os.environ.get('IQC_PACE_DAY_BOUNDARY_HOUR', '13'))


def pace(user_id: int, now: float | None = None) -> dict:
    """오늘(13시 경계)/최근 7일 통과 수 → 국면 레벨(0 광맥 / 1 미달 / 2 기근).
    하루 목표는 경과 시간에 비례해 잰다 — 경계 직후부터 3개를 요구하면 매일
    첫 몇 시간이 '기근'으로 오판된다."""
    now = _time.time() if now is None else now
    lt = _time.localtime(now)
    day0 = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                         DAY_BOUNDARY_HOUR, 0, 0, 0, 0, -1))
    if day0 > now:
        day0 -= 86400.0
    today = _db.pass_count_since(user_id, day0)
    week = _db.pass_count_since(user_id, day0 - 6 * 86400)
    frac = max(0.0, min(1.0, (now - day0) / 86400.0))
    day_deficit = DAILY_TARGET * frac - today
    week_deficit = WEEKLY_TARGET - week
    if today >= DAILY_TARGET and week >= WEEKLY_TARGET:
        level = 0
    elif day_deficit >= 1.5 or (week_deficit > 0 and day_deficit > 0.5):
        level = 2
    elif day_deficit > 0.5 or week_deficit > 0:
        level = 1
    else:
        level = 0
    return {'today': today, 'week': week, 'level': level,
            'day_deficit': round(day_deficit, 2), 'week_deficit': week_deficit}


def auth_dead_count(user_id: int, window_s: float = 3 * 3600) -> int:
    """최근 window 동안 인증 사망으로 죽은 시뮬 수 — 기계가 못 푸는 문제."""
    since = _time.time() - window_s
    return (_db.error_count_like(user_id, since, '%biometric%')
            + _db.error_count_like(user_id, since, '%auth dead%'))


def maybe_intervene(user_id: int, account_type: str = 'research_consultant',
                    log_fn=None) -> dict:
    """라운드 시작 훅 — 계기판 로그 + 기근 시 ε 하한 + 인증 사망 사람 호출."""
    p = pace(user_id)
    out = {'epsilon_boost': None, **p}
    if p['level'] >= 1:
        out['epsilon_boost'] = EPSILON_FLOOR
        if log_fn:
            log_fn(f'🤖 페이스 L{p["level"]} — 오늘 {p["today"]}/{DAILY_TARGET} · '
                   f'7일 {p["week"]}/{WEEKLY_TARGET} → 탐색 강화 ε≥{EPSILON_FLOOR}')
    dead = auth_dead_count(user_id)
    if dead >= 5 and log_fn:
        log_fn(f'🚨 최근 3시간 인증 사망 {dead}건이 페이스를 깎았습니다 — '
               f'대시보드에서 생체 인증이 필요합니다.')
    return out
