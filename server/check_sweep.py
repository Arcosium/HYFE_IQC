"""check_sweep — 쿼터를 쓰지 않는 `GET /alphas/{id}/check` 로 **오늘 기준 판정**을 받아 온다.

2026-08-05 실측 근거
  · 거절은 제출 쿼터를 한 건도 쓰지 않는다(그날 20건 넘게 쏘고 REGULAR_SUBMISSION 0/4).
  · 체크 엔드포인트는 제출조차 아니라 더 싸다.
  · 그런데 우리는 어제 판정을 그대로 믿고 있었다. 기준은 바뀌고(8/3 LOW_FITNESS),
    PROD_CORRELATION 은 형제가 OS 에 오를 때마다 **오늘 값이 달라진다**.

용도 둘
  1. gate_watch 의 관측 원천 보강
  2. 어제 미달이던 알파가 기준·상관 변동으로 오늘 통과 가능해졌는지 탐지 → 있으면 제출
"""
from __future__ import annotations

import logging
import time

from . import criteria as _criteria
from . import db as _db

LOG = logging.getLogger('genomicwqb.check_sweep')

BASE = 'https://api.worldquantbrain.com'
#: 한 번에 훑는 알파 수 — 체크는 싸지만 폴링이 있으므로 상한을 둔다.
TOP_N = 12
POLL_TRIES = 12


def _check_one(client, wid: str) -> dict | None:
    """제출 없이 오늘 기준 체크. Retry-After 를 존중한다."""
    for _ in range(POLL_TRIES):
        try:
            r = client.session.get(f'{BASE}/alphas/{wid}/check', timeout=30)
        except Exception as e:
            LOG.warning('check %s 네트워크 오류: %s', wid, e)
            return None
        ra = r.headers.get('Retry-After')
        if r.status_code == 200 and not ra:
            try:
                return r.json()
            except ValueError:
                return None
        time.sleep(float(ra or 2))
    return None


def sweep(client, user_id: int, *, top_n: int = TOP_N, log_fn=None) -> list[dict]:
    """미제출 상위 알파의 오늘 판정을 받아 온다. → [{wid, fails, blocking, ready}]

    반환의 ready=True 는 '지금 내면 통과할 가능성이 높다'는 뜻이다 — 호출자가 제출한다.
    """
    try:
        cands = _db.unsubmitted_check_candidates(user_id, limit=int(top_n))
    except Exception as e:
        LOG.warning('체크 후보 조회 실패: %s', e)
        return []
    out = []
    for wid, sharpe, fitness in cands:
        body = _check_one(client, wid)
        if not body:
            continue
        checks = (body.get('is') or {}).get('checks') or []
        fails = [c for c in checks if str(c.get('result', '')).upper() == 'FAIL']
        blocking = [str(c.get('name')) for c in fails
                    if _criteria.is_blocking(c.get('name'))]
        rec = {'wid': wid, 'sharpe': sharpe, 'fitness': fitness,
               'fails': [str(c.get('name')) for c in fails],
               'blocking': blocking, 'ready': not blocking}
        out.append(rec)
        if rec['ready'] and log_fn:
            log_fn(f'  ✅ 오늘 기준 통과 가능 — {wid} (S={sharpe} fit={fitness})')
    if log_fn and out:
        n_ok = sum(1 for r in out if r['ready'])
        log_fn(f'  🔎 무료 체크 {len(out)}건 — 오늘 제출 가능 {n_ok}건 '
               f'(쿼터 소모 없음)')
    return out
