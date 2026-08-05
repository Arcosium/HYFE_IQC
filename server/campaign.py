"""campaign — 병목이 특정됐을 때 라운드를 우회해 **직접 병렬 시뮬**을 돌린다.

2026-08-05 실측에서 손으로 만든 것을 코드로 남긴다. 그날 라운드 루프(9~20분에 스펙 8개)로는
가설 검증이 너무 느려 API 를 직접 때렸고, 같은 시간에 훨씬 넓은 좌표를 훑었다.

라운드와 다른 점
  · 큐를 안 거친다. 표현식 목록을 그대로 시뮬에 넣는다
  · 429(동시한도)를 **정상 상태로 보고 기다린다**. 워커와 슬롯을 나눠 쓰므로 당연한 응답인데,
    submit_simulation 은 'RATE_LIMITED' 만 돌려주고 재시도를 안 한다(8/5 에 변형 여러 개가
    이것 때문에 통째로 유실됐다)
  · 결과를 관문별 진척도로 정리해 '무엇이 병목인가'를 바로 말해 준다
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import criteria as _criteria

LOG = logging.getLogger('genomicwqb.campaign')

#: 슬롯이 빌 때까지 기다리는 최대 시간 — 워커와 나눠 쓰면 몇 분 대기가 정상이다.
SLOT_WAIT_S = float(20 * 60)
_SLOT_POLL_S = 15.0


def _submit_with_slot_wait(client, expr: str, settings: dict, stop_event=None):
    """429 를 만나면 슬롯이 날 때까지 기다렸다 다시 낸다. 실패하면 None."""
    deadline = time.time() + SLOT_WAIT_S
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            return None
        url = client.submit_simulation(expr, settings)
        if url and url != 'RATE_LIMITED':
            return url
        if url != 'RATE_LIMITED':
            return None                       # 진짜 실패 — 기다려도 소용없다
        time.sleep(_SLOT_POLL_S)
    return None


def run(client, specs, *, workers: int = 8, deadline_s: int = 1400,
        stop_event=None, log_fn=None) -> list[dict]:
    """specs = [(tag, expr, settings), ...] → 결과 dict 목록 (진척도 포함).

    각 결과: tag · alpha · sharpe · fitness · turnover · gates(체크별 진척도) ·
             binding(최대 병목 체크) · submittability · ready(전 관문 통과)
    """
    out: list[dict] = []
    lock = threading.Lock()

    def one(spec):
        tag, expr, settings = spec
        if stop_event is not None and stop_event.is_set():
            return
        try:
            url = _submit_with_slot_wait(client, expr, settings, stop_event)
            if not url:
                with lock:
                    if log_fn:
                        log_fn(f'  ✗ {tag}: 슬롯 확보 실패')
                return
            res = client.poll(url, deadline_s=deadline_s, stop_event=stop_event)
            aid = res.get('alpha')
            if not aid:
                with lock:
                    if log_fn:
                        log_fn(f'  ✗ {tag}: {str(res.get("message"))[:70]}')
                return
            m = (client.harvest_alpha(aid) or {}).get('metrics') or {}
            gates = _criteria.gate_progress(m)
            bname, bval = _criteria.binding_gate(m)
            sub = _criteria.submittability(m)
            rec = {'tag': tag, 'alpha': aid, 'expr': expr,
                   'sharpe': m.get('sharpe'), 'fitness': m.get('fitness'),
                   'turnover': m.get('turnover'), 'gates': gates,
                   'binding': bname, 'binding_progress': bval,
                   'submittability': sub, 'ready': sub >= 1.0}
            with lock:
                out.append(rec)
                if log_fn:
                    mark = '🎯 전관문 통과' if rec['ready'] else f'병목 {bname}'
                    log_fn(f'  {mark} — {tag} α={aid} S={m.get("sharpe")} '
                           f'fit={m.get("fitness")} tvr={m.get("turnover")}')
        except Exception as e:
            with lock:
                LOG.warning('campaign %s 실패: %s', tag, e)
                if log_fn:
                    log_fn(f'  ✗ {tag}: {e}')

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        list(ex.map(one, list(specs)))
    out.sort(key=lambda d: -(d.get('submittability') or 0))
    return out


def bottleneck_report(results) -> str:
    """결과 묶음 → '무엇이 몇 번 병목이었나' 한 문단. 다음 웨이브 방향을 정하는 근거."""
    if not results:
        return '결과 없음'
    tally: dict[str, int] = {}
    for r in results:
        b = r.get('binding') or '(미측정)'
        tally[b] = tally.get(b, 0) + 1
    parts = [f'{k}×{v}' for k, v in sorted(tally.items(), key=lambda kv: -kv[1])]
    best = results[0]
    return (f'{len(results)}건 중 병목 분포: {", ".join(parts)} | '
            f'최고 진척 {best.get("submittability"):.2f} ({best.get("tag")})')
