"""자율 이데이션 — GA 가 스스로 막히면 로컬 LLM 에게 새 아이디어를 청한다.

배경 (2026-07-14 진단): GA 는 유전자 조합의 **국소 탐색**이라, 시드 풀에 없는 구조는
영원히 못 만든다. 실제로 6월 LLM 시대엔 Sharpe 3.77(레짐 조건부 + CLV + hump)이 나왔는데
LLM 생성 경로를 걷어낸 뒤로는 1.0 근처에서 정체됐다. 문법(C1)과 팔레트(C2)를 넓혀도
'그 조합을 떠올리는' 주체가 없으면 탐색은 무작위에 기댈 수밖에 없다.

`pipeline.py` 와의 차이:
  - pipeline = **사용자가 요청**한 주제를 웹 리서치(Arachne)해서 가설을 세운다.
  - 여기 = 요청이 없어도 주기적으로, **GA 자신의 상태를 근거로** 가설을 세운다.
    웹을 타지 않으므로 빠르고 안 죽는다. 근거는 다음 넷이다:
      1. 지금 엘리트가 무엇인가 (무엇이 통했나)
      2. 무엇이 계속 FAIL 하나 (무엇이 병목인가)
      3. 어떤 정향변이가 먹히나 (directive_stats)
      4. 어떤 family 를 안 써봤나 (탐색 공백)

산출물은 pipeline 과 동일하게 `strategy_specs` 에 pending 으로 쌓이고, 워커가 다음
라운드에 원본 그대로 시뮬한다(변이 없이 — 아이디어의 성적을 먼저 잰다). 그 다음
라운드부터는 elite_seeds 를 통해 평범한 GA 재료가 된다.

fail-open: LLM 이 죽거나 후보가 전부 검증 탈락해도 워커는 평소 GA 를 계속 돈다.
킬스위치: IQC_AUTO_IDEATE=0.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from . import db as _db
from . import genome_models as gm
from . import ideation, strategy_spec

LOG = logging.getLogger('genomicwqb.auto_ideation')

AUTO_IDEATE_ON = os.environ.get('IQC_AUTO_IDEATE', '1') != '0'
EVERY_N_ROUNDS = int(os.environ.get('IQC_AUTO_IDEATE_EVERY', '25'))
"""몇 라운드마다 한 번 아이디어를 청할지. LLM 호출이 분 단위로 걸리므로 자주 돌면
시뮬 예산을 잡아먹는다. 25 ≈ 200 알파에 한 번."""

N_HYPOTHESES = int(os.environ.get('IQC_AUTO_N_HYPOTHESES', '3'))
N_SPECS_PER_HYPOTHESIS = int(os.environ.get('IQC_AUTO_N_SPECS_PER_HYPO', '2'))

_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def should_run(user_id: int, round_num: int) -> bool:
    """이번 라운드에 자율 이데이션을 돌릴 때인가.

    조건: 켜져 있고 · 주기가 됐고 · 이미 안 돌고 있고 · 소비 안 된 스펙이 없을 것.
    (스펙이 남아 있는데 또 만들면 큐만 쌓이고 GA 가 굶는다.)
    """
    if not AUTO_IDEATE_ON or EVERY_N_ROUNDS <= 0:
        return False
    if round_num <= 0 or round_num % EVERY_N_ROUNDS != 0:
        return False
    with _LOCK:
        if user_id in _RUNNING:
            return False
    try:
        if _db.pending_specs(user_id, limit=1):
            return False
    except Exception:
        return False
    return True


def start(user_id: int, round_num: int) -> bool:
    """백그라운드 스레드로 이데이션을 띄운다. 이미 돌고 있으면 False."""
    with _LOCK:
        if user_id in _RUNNING:
            return False
        _RUNNING.add(user_id)
    t = threading.Thread(target=_run, args=(user_id, round_num),
                         name=f'iqc-auto-ideate-{user_id}', daemon=True)
    t.start()
    return True


# ── 근거 블록 — GA 자신의 상태 ────────────────────────────────────────────────

def _fmt_pct(n: int, total: int) -> str:
    return f'{(100.0 * n / total):.0f}%' if total else '0%'


def build_state_evidence(user_id: int) -> str:
    """GA 의 현재 상태를 LLM 이 읽을 근거 블록으로. 실패하면 빈 문자열(fail-open)."""
    parts: list[str] = []

    # 1) 지금 엘리트가 무엇인가
    try:
        seeds = _db.elite_seeds(user_id, top_n=5)
        if seeds:
            lines = []
            for s in seeds:
                g = s.get('genome') or {}
                m = s.get('metrics') or {}
                lines.append(
                    f"  - Sharpe {m.get('sharpe', '?')} / Fitness {m.get('fitness', '?')} "
                    f"/ Turnover {m.get('turnover', '?')} / Returns {m.get('returns', '?')}\n"
                    f"    유전자: family={g.get('family')} fields={list(g.get('fields') or [])} "
                    f"combine={g.get('combine')} sign={g.get('sign')} "
                    f"regime={g.get('regime')} hump={g.get('hump')} "
                    f"decay={g.get('decay')} universe={g.get('universe')} "
                    f"neutralization={g.get('neutralization')}")
            parts.append('[현재 엘리트 알파 — 지금까지 무엇이 통했나]\n' + '\n'.join(lines))
    except Exception as e:
        LOG.warning('엘리트 근거 실패: %s', e)

    # 2) 무엇이 계속 FAIL 하나
    try:
        counts = _db.recent_fail_counts(user_id, limit=400)
        if counts:
            total = max(counts.values())
            lines = [f'  - {name}: {n}건 ({_fmt_pct(n, total)})'
                     for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:8]]
            parts.append('[최근 400 알파의 FAIL 사유 — 이게 병목이다]\n' + '\n'.join(lines))
    except Exception as e:
        LOG.warning('fail 근거 실패: %s', e)

    # 3) 어떤 정향변이가 먹히나
    try:
        stats = _db.directive_stats(user_id)
        if stats:
            rows = []
            for (cat, d), st in sorted(stats.items(), key=lambda kv: -(kv[1].get('n') or 0))[:8]:
                n = int(st.get('n') or 0)
                w = int(st.get('wins') or 0)
                if n >= 3:
                    rows.append(f'  - {cat} 문제에 {d} 적용 → 성공 {w}/{n} ({_fmt_pct(w, n)})')
            if rows:
                parts.append('[정향변이 성적 — 어떤 조정이 어떤 문제를 고쳤나]\n' + '\n'.join(rows))
    except Exception as e:
        LOG.warning('directive 근거 실패: %s', e)

    # 4) 탐색 공백 — 안 써본 family
    try:
        used = _db.recent_family_counts(user_id, limit=400)
        gaps = [f for f in gm.BaseGenomeModel.families if used.get(f, 0) < 5]
        if gaps:
            parts.append('[거의 안 써본 데이터 패밀리 — 탈상관·미개척 영역]\n  '
                         + ', '.join(gaps))
    except Exception as e:
        LOG.warning('family 근거 실패: %s', e)

    return '\n\n'.join(parts)


_QUERY = """이 계정의 알파 탐색이 Sharpe 1.0 부근에서 정체돼 있다. 아래 [수집 근거] 는
웹 자료가 아니라 **이 탐색 시스템의 실제 상태**다 — 지금 무엇이 통하고 있고, 무엇이
계속 실패하며, 어떤 영역을 안 써봤는지가 전부 들어 있다.

이걸 읽고 **지금 병목을 실제로 뚫을** 알파 가설을 세워라. 이미 엘리트에 있는 신호의
변주가 아니라, 구조적으로 다른 메커니즘이어야 한다. 특히 FAIL 사유 1위를 정면으로
겨냥하는 가설을 최소 하나는 포함하라."""


def _log(user_id: int, line: str) -> None:
    try:
        _db.append_log(user_id, 0, line, level='info')
    except Exception:
        pass


def _run(user_id: int, round_num: int) -> None:
    try:
        account_type = 'research_consultant'
        try:
            account_type = _db.get_account_type(user_id)
        except Exception:
            pass

        evidence = build_state_evidence(user_id)
        if not evidence:
            LOG.info('uid=%s 상태 근거가 비어 자율 이데이션 생략', user_id)
            return

        _log(user_id, f'🧠 자율 이데이션 (라운드 {round_num}) — GA 상태를 근거로 '
                      f'새 전략 가설 {N_HYPOTHESES}개 구상 중...')
        run_id = _db.create_research_run(user_id, f'[자율] 라운드 {round_num} 정체 돌파')
        _db.update_research_run(run_id, status='ideating', evidence=evidence, sources=[])

        hypos = ideation.propose_hypotheses(_QUERY, evidence, n=N_HYPOTHESES)
        if not hypos:
            _db.update_research_run(run_id, status='error', error='가설 생성 실패')
            _log(user_id, '⚠ 자율 이데이션 — LLM 가설 생성 실패 (GA 는 그대로 계속)')
            return

        _db.update_research_run(run_id, status='concretizing')
        total = 0
        for h in hypos:
            hid = _db.insert_hypothesis(run_id, user_id, h)
            _log(user_id, f'   💡 {h["title"]}')
            try:
                specs = strategy_spec.concretize(
                    h, evidence, k=N_SPECS_PER_HYPOTHESIS, account_type=account_type)
            except Exception as e:
                LOG.warning('concretize 실패: %s', e)
                specs = []
            for s in specs:
                _db.insert_spec(hid, user_id, genome=s['genome'], code=s['code'],
                                settings=s['settings'], delay=s['delay'],
                                why=s.get('why', ''))
                total += 1

        if total == 0:
            _db.update_research_run(run_id, status='error', error='후보 전부 검증 탈락')
            _log(user_id, '⚠ 자율 이데이션 — 후보가 전부 검증 탈락 (GA 는 그대로 계속)')
            return
        _db.update_research_run(run_id, status='ready')
        _log(user_id, f'✅ 자율 이데이션 — 새 전략 후보 {total}개 준비. '
                      f'다음 라운드에 원본 그대로 측정한다.')
    except Exception as e:
        LOG.exception('자율 이데이션 실패')
        _log(user_id, f'⚠ 자율 이데이션 예외(무시): {e}')
    finally:
        with _LOCK:
            _RUNNING.discard(user_id)
