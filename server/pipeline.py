"""리서치 → 가설 → 전략스펙 파이프라인 오케스트레이터.

사용자가 요청 1건을 넣으면 백그라운드 스레드가 3단계를 돌고, 산출물(전략스펙)은
`strategy_specs` 테이블에 pending 으로 쌓인다. 워커는 다음 라운드에 그걸 초기 개체로
소비하고, 이후는 기존 GA 가 이어받는다.

**요청이 없으면 이 모듈은 아무것도 하지 않는다** — 워커는 지금처럼 무작위 GA 를 돈다.

단계마다 fail-open 이다: Arachne 가 죽으면 근거 없이 가설을 세우고, LLM 이 죽으면
스펙 0개로 끝난다. 어느 경우에도 워커/GA 는 멈추지 않는다.
"""
from __future__ import annotations

import logging
import threading

from . import db as _db
from . import ideation, research, strategy_spec

LOG = logging.getLogger('genomicwqb.pipeline')

# 가설 수 · 가설당 후보 수. 8 슬롯짜리 라운드 하나를 채우는 것이 목표(4×2=8).
N_HYPOTHESES = int(__import__('os').environ.get('IQC_N_HYPOTHESES', '4'))
N_SPECS_PER_HYPOTHESIS = int(__import__('os').environ.get('IQC_N_SPECS_PER_HYPO', '2'))

_RUNNING: dict[int, bool] = {}
_LOCK = threading.Lock()


def is_running(user_id: int) -> bool:
    with _LOCK:
        return bool(_RUNNING.get(user_id))


def start(user_id: int, query: str) -> int:
    """리서치 런을 만들고 백그라운드 스레드를 띄운다. 반환: run_id.

    같은 사용자의 파이프라인이 이미 돌고 있으면 RuntimeError.
    """
    q = str(query or '').strip()
    if not q:
        raise ValueError('빈 요청')
    with _LOCK:
        if _RUNNING.get(user_id):
            raise RuntimeError('이미 리서치가 진행 중입니다')
        _RUNNING[user_id] = True
    run_id = _db.create_research_run(user_id, q)
    t = threading.Thread(target=_run, args=(user_id, run_id, q),
                         name=f'iqc-pipeline-{user_id}', daemon=True)
    t.start()
    return run_id


def _log(user_id: int, line: str) -> None:
    try:
        _db.append_log(user_id, 0, line, level='info')
    except Exception:
        pass


def _run(user_id: int, run_id: int, query: str) -> None:
    account_type = 'research_consultant'
    try:
        account_type = _db.get_account_type(user_id)
    except Exception:
        pass
    try:
        # ── 1) 근거 수집 (Arachne) ────────────────────────────────────────
        _db.update_research_run(run_id, status='gathering')
        _log(user_id, f'🔍 리서치 시작 — "{query[:60]}"')

        def _on_aspect(label, n):
            _log(user_id, f'   · {label}: 출처 {n}건')

        evidence, sources = research.build_research_evidence(
            query, on_progress=_on_aspect)
        _db.update_research_run(run_id, evidence=evidence, sources=sources)
        if not evidence:
            _log(user_id, '⚠ 웹 근거를 못 모았습니다 (Arachne 응답 없음) — '
                          'LLM 사전지식만으로 가설을 세웁니다')
        else:
            _log(user_id, f'📚 근거 {len(sources)}건 수집 완료')

        # ── 2) 가설 생성 (LLM) ────────────────────────────────────────────
        _db.update_research_run(run_id, status='ideating')
        _log(user_id, f'💡 전략 가설 {N_HYPOTHESES}개 생성 중...')
        hypos = ideation.propose_hypotheses(query, evidence, n=N_HYPOTHESES)
        if not hypos:
            _db.update_research_run(run_id, status='error',
                                    error='가설 생성 실패 (LLM 응답 파싱 불가)')
            _log(user_id, '⚠ 가설 생성 실패 — 요청을 더 구체적으로 적어 다시 시도하세요')
            return
        hypo_ids: list[tuple[int, dict]] = []
        for h in hypos:
            hid = _db.insert_hypothesis(run_id, user_id, h)
            hypo_ids.append((hid, h))
            cite = (f" [출처 {', '.join(str(c) for c in h['citations'])}]"
                    if h.get('citations') else ' [근거 없음]')
            _log(user_id, f'   💡 {h["title"]}{cite}')

        # ── 3) 전략 후보 구체화 (LLM → 타입드 유전체 → 검증) ──────────────
        _db.update_research_run(run_id, status='concretizing')
        _log(user_id, f'🧬 가설당 전략 후보 {N_SPECS_PER_HYPOTHESIS}개 구체화 중...')
        total = 0
        for hid, h in hypo_ids:
            specs = strategy_spec.concretize(
                h, evidence, k=N_SPECS_PER_HYPOTHESIS, account_type=account_type)
            for s in specs:
                _db.insert_spec(hid, user_id, genome=s['genome'], code=s['code'],
                                settings=s['settings'], delay=s['delay'],
                                why=s.get('why', ''))
                total += 1
            if not specs:
                _log(user_id, f'   ⚠ "{h["title"][:30]}" — 후보가 전부 검증 탈락')
        if total == 0:
            _db.update_research_run(run_id, status='error',
                                    error='모든 전략 후보가 검증(lint/팔레트)에 탈락')
            _log(user_id, '⚠ 유효한 전략 후보를 만들지 못했습니다 — 무작위 GA 를 계속합니다')
            return

        _db.update_research_run(run_id, status='ready')
        _log(user_id, f'✅ 전략 후보 {total}개 준비 완료 — '
                      f'다음 라운드부터 이 후보들로 GA 를 시작합니다', )
    except Exception as e:
        LOG.exception('pipeline 실패')
        try:
            _db.update_research_run(run_id, status='error', error=f'{type(e).__name__}: {e}')
        except Exception:
            pass
        _log(user_id, f'⚠ 리서치 파이프라인 예외: {e}')
    finally:
        with _LOCK:
            _RUNNING.pop(user_id, None)
