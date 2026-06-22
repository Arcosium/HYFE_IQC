"""사용자별 백그라운드 워커 — IQC 라운드를 무한 루프 실행, pause 즉시 중단.

설계:
  - 한 user 당 최대 1 thread. WorkerRegistry 가 (user_id → Worker) 보관.
  - Worker 는 paused 플래그가 set 이면 즉시 종료 (현재 batch 의 subprocess 도 kill).
  - pause/resume 은 DB 의 users.paused 와 메모리 _paused_event 동기화로 구현.
  - 라운드 간 1초 딜레이 (브라우저 cooldown).
"""

from __future__ import annotations

import json as _json
import os
import signal
import threading
import time
import traceback
import logging
from typing import Any

from . import db as _db
from . import result_cache
from . import gemini_strategist
from . import wqb_browser
from . import run_config
from . import wqb_data_service
from . import settings_fp as _settings_fp
from . import alpha_ast as _alpha_ast
from .focus_priority import closeness_score as _closeness_score
from .focus_priority import advance_focus_queue as _advance_focus_queue
from .focus_priority import NEUTRAL_SCORE as _NEUTRAL_SCORE

LOG = logging.getLogger('hyfe.worker')

# PASS_THRESHOLD — 한 알파의 통과 항목 수 임계값. WQB 의 IS Testing 항목 수는
# 사용자 tier 에 따라 다름 (보통 6-11). 우리가 추출 가능한 metric 은 6개 (Sharpe/
# Fitness/Returns/Turnover/Drawdown/Margin) 라 실 max 는 6. 환경변수로 override.
PASS_THRESHOLD = int(os.environ.get('HYFE_IQC_PASS_THRESHOLD', '7'))

# FOCUS_MIN_PASS — 한 알파가 focus(directed-mutation) 정제 대상이 되는 최소 PASS 수.
# 6 이었으나 라이브 검증상 새 생성기가 pass-5 에서 정체 → focus 가 0 건 enqueue 되어
# P3 directed-mutation 이 영영 안 돌았다. 5 로 낮춰 pass-5 near-miss 를 지표기반 정제한다.
# (라운드당 후보는 FOCUS_MAX_PER_ROUND 로 캡해 큐 폭주 방지.)
FOCUS_MIN_PASS = int(os.environ.get('HYFE_IQC_FOCUS_MIN_PASS', '5'))
FOCUS_MAX_PER_ROUND = int(os.environ.get('HYFE_IQC_FOCUS_MAX_PER_ROUND', '2'))

# focus 라운드의 presim 구조적 overlap 임계값. focus 는 부모를 의도적으로 변형하므로
# 부모/형제와 닮는 게 정상인데, 전역 presim_gate(임계 5)가 그걸 near-dup 으로 보고
# 라이브 50~80% 를 드롭해 Gemini 생성을 통째로 낭비했다. 0 = focus 에서 overlap 드롭 OFF
# (정확 중복은 code_hash dedup 이 이미 잡음, 복잡도 캡은 유지). 탐색 라운드는 영향 없음.
FOCUS_OVERLAP_DROP = int(os.environ.get('HYFE_IQC_FOCUS_OVERLAP_DROP', '0'))


def _round_label(round_num: int, parent_idx: int, phase: int) -> str:
    """계층 라운드 라벨 = {base}-{부모알파}-{개선깊이}.
    탐색(base, phase 0) 은 정수 그대로('3'), focus 는 '2-2-3' (round 2 의 알파 #2 를 깊이 3 개선)."""
    if phase and phase > 0:
        return f'{round_num}-{parent_idx}-{phase}'
    return str(round_num)

# focus 진입 절대 하한선 — closeness_score(통과까지의 상대 gap 합의 음수) 가 이 값보다
# 낮은(=통과에서 너무 먼) 부모는 directed-mutation 으로 정제해도 가망이 없으므로 큐에
# 넣지 않고 예산을 탐색으로 돌린다. delay=0 은 Sharpe 통과가 본래 어려워 hopeless 부모가
# 많아 이 게이트가 특히 중요. 예: Sharpe 0.07(gap≈0.97)+Fitness 0.01(gap≈0.99)→약 -1.96 (차단),
# Sharpe 1.7(gap≈0.15)+Fitness 1.1(gap≈0.15)→약 -0.30 (통과). -1e8 이하는 사실상 OFF.
FOCUS_CLOSENESS_FLOOR = float(os.environ.get('HYFE_IQC_FOCUS_CLOSENESS_FLOOR', '-0.8'))

# focus 라운드마다 부모의 '정확한 공식'을 (universe × neutralization) 그리드로 재시뮬하는
# settings 스윕 개수. delay=0 은 필드가 PV 로 묶여 settings 가 사실상 유일한 추가 Sharpe
# 레버라, LLM 추측 대신 결정적으로 훑는다(Gemini 호출 0, 기존 조합은 캐시히트=공짜).
# 0 = 비활성화. 시뮬 비용(delay=0 개당 ~3분)을 고려해 기본 3.
FOCUS_SWEEP_N = int(os.environ.get('HYFE_IQC_FOCUS_SWEEP_N', '3'))

# 서킷 브레이커 — _run_one_round 가 연속 이만큼 예외나면 워커를 자동 중단.
# (기존엔 무한 재시도라 같은 버그로 영원히 spin 했음.)
_MAX_CONSEC_FAILS = 5

_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict[int, 'Worker'] = {}


def get_or_create(user_id: int) -> 'Worker':
    with _REGISTRY_LOCK:
        w = _REGISTRY.get(user_id)
        if w is not None and w.is_alive():
            return w
        w = Worker(user_id)
        _REGISTRY[user_id] = w
        return w


def get(user_id: int) -> 'Worker | None':
    with _REGISTRY_LOCK:
        return _REGISTRY.get(user_id)


def cleanup_dead() -> None:
    with _REGISTRY_LOCK:
        dead = [uid for uid, w in _REGISTRY.items() if not w.is_alive()]
        for uid in dead:
            _REGISTRY.pop(uid, None)


class Worker(threading.Thread):
    """user_id 별 IQC 라운드 무한 실행."""

    def __init__(self, user_id: int):
        super().__init__(daemon=True, name=f'hyfe-worker-{user_id}')
        self.user_id = user_id
        self._stop_event = threading.Event()         # pause/stop 신호
        self._batch_proc_holder: dict[str, Any] = {} # 현재 배치 subprocess 보관
        self._lock = threading.Lock()
        # PASS 알파 (IS Testing Status PASS≥7 AND FAIL=0) 는 그 자리에서 'Submit Alpha'
        # 버튼 활성화를 확인하고, 활성화되어 있으면 클릭해서 알파를 제출한다.

    # ── 외부 제어 ─────────────────────────────────────────────
    def request_pause(self) -> None:
        """pause 요청. 현재 진행 중인 batch 가 있으면 subprocess 도 즉시 kill."""
        self._stop_event.set()
        with self._lock:
            proc = self._batch_proc_holder.get('proc')
        if proc is not None:
            try:
                # 새 프로세스 그룹으로 띄웠으므로 그룹 전체 SIGKILL.
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        _db.set_user_running(self.user_id, running=True, paused=True)

    def request_resume(self) -> None:
        """일시정지 해제 — Worker 가 종료된 상태일 수 있으므로 새 인스턴스로 시작 필요.
        호출자(routes 의 /start)가 get_or_create 후 .start() 다시 호출."""
        self._stop_event.clear()
        with self._lock:
            self._batch_proc_holder['proc'] = None
        _db.set_user_running(self.user_id, running=True, paused=False)

    def is_paused(self) -> bool:
        return self._stop_event.is_set()

    # ── 메인 루프 ─────────────────────────────────────────────
    def run(self) -> None:
        try:
            self._main_loop()
        finally:
            _db.set_user_running(self.user_id, running=False, paused=False)
            with _REGISTRY_LOCK:
                if _REGISTRY.get(self.user_id) is self:
                    _REGISTRY.pop(self.user_id, None)

    def _main_loop(self) -> None:
        _db.set_user_running(self.user_id, running=True, paused=False)
        consec_fails = 0
        while not self._stop_event.is_set():
            try:
                wqb_data_service.maybe_refresh(time.time())
            except Exception:
                pass
            try:
                self._run_one_round()
                consec_fails = 0          # 성공 → 카운터 리셋
            except Exception as e:
                consec_fails += 1
                LOG.exception('worker round exception')
                self._log(0, f'⚠ 워커 라운드 예외 ({consec_fails}/{_MAX_CONSEC_FAILS}): {e}')
                if consec_fails >= _MAX_CONSEC_FAILS:
                    self._log(0, f'⛔ 연속 {consec_fails}회 실패 — 워커 자동 중단 '
                                 f'(무한 루프 방지). 원인 수정 후 다시 시작하세요.')
                    break
                # 잠시 쉬고 다음 라운드.
                if self._stop_event.wait(timeout=10):
                    break
                continue
            # 라운드 간 짧은 cooldown.
            if self._stop_event.wait(timeout=1.5):
                break

    # ── 단일 라운드 ───────────────────────────────────────────
    def _run_one_round(self) -> None:
        if self._stop_event.is_set():
            return
        creds = _db.get_user_credentials(self.user_id)
        if not creds:
            self._log(0, '⚠ 자격증명 조회 실패 (user 가 삭제됐을 수 있음) — 워커 종료')
            self._stop_event.set()
            return
        username, password = creds
        account_type = _db.get_account_type(self.user_id)

        u = _db.get_user(self.user_id)
        # focus 큐 우선 — PASS=6 알파에 대한 sub-round 가 대기중이면 그것을 먼저 실행.
        focus_queue = _db.get_focus_queue(self.user_id)
        # Near-miss priority: sort by closeness_score (near-pass first).
        # Safe: entries with unparseable fail_items fall to neutral (0.0);
        # a sort error will be caught by the outer try/except and the round
        # will continue with whatever ordering was in place.
        try:
            focus_queue = sorted(
                focus_queue,
                key=lambda e: _closeness_score(e.get('parent_fail_items') or []),
                reverse=True,
            )
        except Exception:
            pass  # fallback to original FIFO order
        focus_entry = focus_queue[0] if focus_queue else None
        is_focus = bool(focus_entry)

        if is_focus:
            round_num = int(focus_entry.get('parent_round_num') or 0)
            phase = int(focus_entry.get('phase') or 1)
            parent_idx = int(focus_entry.get('parent_idx') or 0)
            fail_desc = str(focus_entry.get('fail_desc') or '')
            parent_code = str(focus_entry.get('parent_code') or '')
            parent_desc = str(focus_entry.get('parent_desc') or '')
            parent_pass_items = list(focus_entry.get('parent_pass_items') or [])
            parent_fail_items = list(focus_entry.get('parent_fail_items') or [])
            focus_kind = str(focus_entry.get('focus_kind') or 'fail')
            self_corr_value = str(focus_entry.get('self_corr_value') or '')
            parent_settings = dict(focus_entry.get('parent_settings') or {})
            round_id = _db.start_round(
                self.user_id, round_num,
                phase=phase, parent_idx=parent_idx, focus_fail=fail_desc,
            )
            kind_tag = '🚫 corr 회피' if focus_kind == 'correlation' else '🔧 fail 개선'
            self._log(round_num,
                      f'═══ ROUND {_round_label(round_num, parent_idx, phase)} 시작 ({kind_tag}, on #{parent_idx}, fix: {fail_desc[:60]}) ═══',
                      level='round_start')
        else:
            round_num = int((u or {}).get('last_round_num') or 0) + 1
            phase = 0
            round_id = _db.start_round(self.user_id, round_num)
            self._log(round_num, f'═══ ROUND {round_num} 시작 ═══', level='round_start')
            parent_idx = 0
            fail_desc = ''
            parent_code = ''
            parent_desc = ''
            parent_pass_items = []
            parent_fail_items = []
            focus_kind = 'fail'
            self_corr_value = ''
            parent_settings = {}

        feedback = _db.list_feedback(self.user_id)
        errors = _db.list_error_patterns(self.user_id, limit=100)
        # 캐시 히트 다수 발생 시 Gemini 가 같은 코드를 또 만들고 있다는 뜻 — 회피 가이드 시드.
        avoid_codes = _db.list_recent_distinct_codes(limit=80)
        # 이미 제출된 알파 + Submit 거절/무응답으로 끝난 알파 코드 — Gemini 가
        # self-correlation 충돌 회피하도록 가이드 & 사전 유사도 필터의 비교 기준.
        # 거절된 영역(예: 0.94 self-corr)을 다시 만들어봐야 또 거절되므로 미리 피한다.
        submitted_codes = [a.get('code', '') for a in _db.list_submitted_alphas(self.user_id, limit=30)]
        submitted_codes += _db.list_rejected_alpha_codes(self.user_id, limit=40)
        submitted_codes = list(dict.fromkeys(c for c in submitted_codes if c))
        # 직전 라운드 cache hit ratio — Gemini temperature 부스팅에 활용.
        prev_cache_ratio = self._prev_cache_hit_ratio()
        # smilee 정신: 이전 best 알파 → building block, operator/datafield 통계 → preference.
        try:
            seeds = _db.best_alphas_for_seeding(self.user_id, top_n=5, min_pass_count=5)
            pref_stats = _db.operator_preference_stats(self.user_id, lookback_alphas=200)
        except Exception as e:
            self._log_quiet(round_num, f'⚠ seeds/pref_stats 조회 실패: {e}')
            seeds, pref_stats = [], {}

        # 탐색(비-focus) 라운드: 기존 30-알파 history / seeds / preference / 제출코드를 주입하지
        # 않는다. 오류 캐시만 학습해 archetype 틀에 갇히지 않고 최대한 다양한 알파를 생성한다.
        # (제출 자체를 하지 않으므로 submitted 기반 사전 유사도 필터도 자동 비활성화된다.
        #  avoid_codes 만 유지해 '글자까지 동일한' 중복 생성=cache-hit 낭비를 막는다.)
        # 단, 밴딧이 활성화된 경우에는 feedback/seeds/pref_stats/submitted_codes 를 유지해
        # 학습 신호가 생성 단계로 흘러들어갈 수 있도록 한다.
        _bandit_on = run_config.is_bandit_enabled()
        if not is_focus and not _bandit_on:
            feedback = []
            seeds = []
            pref_stats = {}
            submitted_codes = []

        new_feedback: list[dict] = []
        pass_total = 0
        err_total = 0
        cache_hit_total = 0

        # delay 테스트 모드 (UI/run_config) — 생성 전에 이번 라운드의 강제 delay 를 확정해
        # Gemini 프롬프트(필드 선택 유도)와 시뮬 settings 양쪽에 동일 값을 넘긴다.
        delay_mode = run_config.get_delay_mode()
        forced_delay = run_config.resolve_round_delay(delay_mode)
        if delay_mode == 'mix':
            self._log(round_num, f'  Delay 모드=혼합(랜덤) → 이번 라운드 delay={forced_delay}')
        else:
            self._log(round_num, f'  Delay 모드=고정 → delay={forced_delay}')

        # 밴딧 arm 선택 — 비-focus 라운드에서만 실행, 밴딧 ON 시에만.
        # select_slots 결과를 generate_strategies 에 전달해 settings 분산을 소프트 가이드.
        if _bandit_on and not is_focus:
            import random as _random
            from . import bandit as _bandit
            from . import retrospect as _retrospect
            # 적응형 epsilon: 최근 라운드 보상 트렌드로 탐색/착취 비율 자동 조정.
            try:
                _trend = _db.round_reward_trend(self.user_id)
                _epsilon = _retrospect.adaptive_epsilon(_trend)
            except Exception:
                _epsilon = 0.2
            _stats = {a['arm_key']: a['mean'] for a in _db.bandit_stats(self.user_id)}
            _slot_settings = _bandit.select_slots(
                _stats, n_slots=10, epsilon=_epsilon, explore_slots=3,
                rng=_random.Random(round_num),
            )
            # 데이터 기반 prior 생성 — 생성 프롬프트에 소프트 가이드로 주입.
            try:
                _axis_res = {
                    a: _db.axis_effectiveness(self.user_id, a)
                    for a in ('universe', 'neutralization', 'decay')
                }
                _op_res = _db.operator_effectiveness(self.user_id)
                _priors = _retrospect.format_effectiveness_priors(_axis_res, _op_res)
            except Exception:
                _priors = ''
            try:
                _db.update_round_config(round_id, delay_mode=delay_mode,
                                        explore_exploit='3/7',
                                        injected_arms=_json.dumps(_slot_settings))
            except Exception:
                pass
        else:
            _slot_settings = None
            _priors = ''

        # P5 SC-포화: 제출풀이 과다 사용한 신호 연산자를 생성 프롬프트에 경고로 주입,
        # 비슷한 알파의 self-corr>0.7 벽을 회피하도록 다른 연산자 패밀리로 유도. fail-open.
        try:
            from . import knowledge_base
            _sub_for_sat = [a.get('code', '') for a in _db.list_submitted_alphas(self.user_id, limit=50)]
            _sat_warn = knowledge_base.render_saturation_warning(
                knowledge_base.saturated_operators(_sub_for_sat))
            if _sat_warn:
                _priors = (_priors or '') + '\n\n' + _sat_warn
                self._log_quiet(round_num, f'   (SC-포화 경고 주입: {len(_sat_warn)}자)')
        except Exception as _e:
            self._log_quiet(round_num, f'⚠ SC-포화 계산 예외(무시): {_e}')

        try:
            if is_focus:
                kind_label = 'correlation 회피' if focus_kind == 'correlation' else 'fail 개선'
                self._log(round_num, f'1) Gemini focused 8 알파 생성 [{kind_label}] (부모 #{parent_idx} 의 \"{fail_desc[:50]}\")...')
                # correlation 모드는 직교화 가이드 위해 제출/거절 알파 코드를 함께 전달
                # (submitted_codes 는 위에서 이미 제출+거절 합본으로 만들어 둠).
                _submitted_for_corr = list(submitted_codes) if focus_kind == 'correlation' else []
                strategies = gemini_strategist.generate_focused_strategies(
                    round_num=round_num,
                    phase=phase,
                    parent_idx=parent_idx,
                    parent_code=parent_code,
                    parent_desc=parent_desc,
                    fail_desc=fail_desc,
                    parent_pass_items=parent_pass_items,
                    parent_fail_items=parent_fail_items,
                    focus_kind=focus_kind,
                    self_corr_value=self_corr_value,
                    submitted_codes=_submitted_for_corr,
                    forced_delay=forced_delay,
                    log_fn=lambda line: self._log_quiet(round_num, line),
                )
                # (c) settings 스윕 — 부모의 '정확한 공식'을 (universe×neutralization)
                # 그리드로 결정적 재시뮬해 delay=0 의 유일한 추가 Sharpe 레버를 훑는다.
                # Gemini 호출 0; 기존 조합은 아래 캐시 단계서 cache-hit 으로 공짜 처리.
                # idx 101+ 로 부여해 Gemini 알파(1~10)와 충돌 회피. fail-open.
                if FOCUS_SWEEP_N > 0 and focus_kind != 'correlation' and parent_code.strip():
                    try:
                        from . import settings_sweep
                        _sweep = settings_sweep.sweep_candidates(
                            parent_code, parent_settings,
                            n=FOCUS_SWEEP_N, seed=round_num + phase, start_idx=101)
                        if _sweep:
                            strategies = list(strategies or []) + _sweep
                            self._log(round_num,
                                      f'  ⚙ settings 스윕 {len(_sweep)}개 주입 — 부모 #{parent_idx} '
                                      f'동일 공식 × 다른 universe/neutralization (Gemini 호출 없음)')
                    except Exception as _e:
                        self._log_quiet(round_num, f'⚠ settings 스윕 예외(무시): {_e}')
            else:
                # P4 메타전략: 정체(연속 하락) + survivor>=2 이면 RECOMBINE(두 survivor 융합),
                # 아니면 기존 EXPLORE. 어떤 예외든 EXPLORE 로 폴백(fail-open).
                strategies = None
                try:
                    from . import alpha_search
                    _scores = _db.recent_round_scores(self.user_id, n=8)
                    _survivors = _db.survivor_alphas(self.user_id, n=6, min_pass=5)
                    _mode = alpha_search.pick_mode(
                        _scores, has_near_miss=False, survivor_count=len(_survivors))
                    if _mode == 'RECOMBINE' and len(_survivors) >= 2:
                        _p1 = _survivors[0]
                        _p2 = max(
                            _survivors[1:],
                            key=lambda s: len(set(_p1.get('operators') or [])
                                              ^ set(s.get('operators') or [])))
                        self._log(round_num,
                                  f'1) 🧬 RECOMBINE — survivor 2개 융합 '
                                  f'(PASS {_p1.get("pass_count")} × {_p2.get("pass_count")})')
                        strategies = gemini_strategist.generate_crossover_strategies(
                            round_num=round_num, parents=[_p1, _p2],
                            submitted_codes=submitted_codes, forced_delay=forced_delay,
                            log_fn=lambda line: self._log_quiet(round_num, line))
                except Exception as _e:
                    self._log_quiet(round_num, f'⚠ RECOMBINE 경로 예외, EXPLORE 폴백: {_e}')
                    strategies = None
                if not strategies:
                    self._log(round_num, '1) Gemini 8 알파 생성 호출 (탐색: 오류캐시만, history 없음)...')
                    strategies = gemini_strategist.generate_strategies(
                        round_num=round_num,
                        feedback=feedback,
                        errors=errors,
                        avoid_codes=avoid_codes,
                        submitted_codes=submitted_codes,
                        seeds=seeds,
                        pref_stats=pref_stats,
                        cache_hit_ratio_hint=prev_cache_ratio,
                        forced_delay=forced_delay,
                        slot_settings=_slot_settings,
                        effectiveness_priors=_priors,
                        log_fn=lambda line: self._log_quiet(round_num, line),
                    )

            if self._stop_event.is_set():
                _db.finish_round(round_id, self.user_id, round_num,
                                  status='paused', pass_count=0, err_count=0,
                                  cache_hits=0, summary='Gemini 후 pause 요청')
                return

            # 사전 유사도 검사 — 이미 제출된 알파와 string/operator/field 가중평균 0.7 이상이면
            # WQB 가 self-correlation 으로 reject 할 가능성 높음. 시뮬 보내기 전 차단.
            # (출처: zhutoutoutousan/worldquant-miner template_similarity.py 포팅)
            if submitted_codes:
                from . import alpha_similarity as _sim
                kept: list[dict] = []
                rejected_pre = 0
                for s in strategies:
                    too_sim, score, matched = _sim.too_similar_to_any(
                        s['code'], submitted_codes, threshold=0.7)
                    if too_sim:
                        rejected_pre += 1
                    else:
                        kept.append(s)
                if rejected_pre > 0:
                    self._log(round_num,
                              f'  ⚠ 사전 유사도 검사: {rejected_pre}/{len(strategies)} 알파가 '
                              f'기제출 알파와 너무 유사 (>=0.7) → 시뮬 안 함 (self-corr reject 회피)')
                strategies = kept
                if not strategies:
                    _db.finish_round(round_id, self.user_id, round_num,
                                      status='done', pass_count=0, err_count=0,
                                      cache_hits=0,
                                      summary='Gemini 알파 모두 기제출과 유사도 0.7+ → 다음 라운드')
                    # focus 모드면 큐 pop 안 하면 동일 부모 무한 반복 위험 — 강제 pop.
                    if is_focus:
                        try:
                            new_q = _db.get_focus_queue(self.user_id)
                            if new_q and new_q[0].get('parent_round_num') == round_num \
                                    and int(new_q[0].get('phase') or 0) == phase:
                                _db.set_focus_queue(self.user_id, new_q[1:])
                                self._log(round_num, '  focus 큐 강제 pop (전부 유사도 거부)')
                        except Exception:
                            pass
                    return

            # 구조적 탈상관 + 복잡도 사전게이트 (Jaccard 유사도 필터 다음 단계).
            # 기제출/거절 알파와 AST 서브트리가 겹치는 near-dup, 과복잡 알파를 시뮬 전에 제거.
            try:
                from . import presim_gate
                # focus 라운드는 구조적 overlap 드롭을 끈다(FOCUS_OVERLAP_DROP, 기본 0=OFF) —
                # focus 는 부모를 일부러 변형하므로 닮는 게 정상이고, 끄지 않으면 생성의 50~80%
                # 가 'near-duplicate' 로 버려져 Gemini 호출이 낭비된다. 탐색 라운드는 기본 임계 유지.
                _gate_opts = {'overlap_drop': FOCUS_OVERLAP_DROP} if is_focus else None
                _kept, _dropped = presim_gate.screen(
                    strategies, existing_codes=(submitted_codes or [])[:60],
                    opts=_gate_opts)
                for _d in _dropped:
                    self._log(round_num,
                              f'  ⊘ #{_d.get("idx")} 사전게이트 드롭: {_d.get("reason")}')
                if _kept:
                    strategies = _kept
                elif _dropped:
                    # 전부 드롭되면 시뮬 0개가 되므로 전량 통과시키고 경고(임계값 점검).
                    self._log(round_num,
                              '  ⚠ 사전게이트가 전부 드롭 — 전량 통과(threshold 점검 필요)')
            except Exception as _e:
                self._log_quiet(round_num, f'⚠ 사전게이트 예외(무시하고 진행): {_e}')

            _db.update_round_status(round_id, 'simulating')

            # 필드 위생 자동 래핑 — presim 게이트(신호 복잡도 평가) 이후·캐시/시뮬 이전.
            # LLM 이 winsorize(ts_backfill(F,120),std=4) 를 안 붙여도 코드 레벨에서 결정론적
            # 보장(Sharpe~0.2 차단). 멱등이라 Gemini 가 직접 감쌌어도 이중래핑 안 됨. 래핑된
            # 코드를 이후 code_hash/cache/simulate/저장에 일관되게 사용한다.
            for _s in strategies:
                try:
                    _hy = _alpha_ast.apply_field_hygiene(_s['code'])
                    if _hy != _s['code']:
                        _s['code'] = _hy
                except Exception:
                    pass

            # 캐시 hit 분리 (settings-aware 키: code_hash + settings_fingerprint).
            cached_results: list[dict] = []
            to_simulate: list[dict] = []
            seen: set[str] = set()
            settings_by_idx: dict[int, dict] = {
                int(s['idx']): (s.get('settings') or {}) for s in strategies
            }
            for s in strategies:
                eff = _settings_fp.effective_settings(s.get('settings') or {}, forced_delay)
                fp = _settings_fp.settings_fingerprint(eff)
                h = _db.code_hash(s['code'])
                key = f'{h}:{fp}'
                if key in seen:
                    continue
                seen.add(key)
                cached = result_cache.lookup(self.user_id, s['code'], fp)
                if cached:
                    cached_results.append(result_cache.materialize(s, cached, round_num))
                else:
                    to_simulate.append(s)
            cache_hit_total = len(cached_results)

            # 라운드의 모든 시뮬 대상 알파를 한 subprocess 에 넘긴다 — 그 안에서 wqb_browser
            # 가 1개 탭에서 알파를 순차 실행한다 (배치/슬롯 개념 없음, 라운드 한 사이클이 한 흐름).
            all_results: list[dict] = list(cached_results)
            do_simulate = bool(to_simulate)

            if do_simulate and not self._stop_event.is_set():
                batch = to_simulate
                _sim_mode = ('RC API 동시' if account_type == 'research_consultant'
                             else '1탭 순차')
                self._log(round_num,
                          f'  ── 라운드 시뮬 시작 ({_sim_mode}) — 알파 {len(batch)}개 '
                          f'#{[s_["idx"] for s_ in batch]}')
                # 어떤 전략을 테스트하는지 (idx + desc) 로그에 한 줄씩 노출.
                for s_ in batch:
                    desc_short = (s_.get('desc') or '').strip()
                    if len(desc_short) > 90:
                        desc_short = desc_short[:90] + '…'
                    self._log(round_num, f'      #{s_["idx"]} → {desc_short or "(설명 없음)"}')
                with self._lock:
                    self._batch_proc_holder['proc'] = None
                # 알파 한 개가 끝날 때마다 partial_fn 으로 즉시 결과를 흘려보낸다.
                _seen_idx: set[int] = set()
                def _on_partial(obj: dict, _round_num=round_num):
                    s_idx = int(obj.get('idx') or 0)
                    if s_idx in _seen_idx:
                        return
                    _seen_idx.add(s_idx)
                    status = obj.get('status') or ''
                    err_t = (obj.get('error_text') or '').strip()
                    if status == 'error':
                        snippet = err_t[:80] + ('…' if len(err_t) > 80 else '')
                        self._log(_round_num, f'      #{s_idx} ⚠ 오류 — {snippet}',
                                  level='warn')
                        return
                    is_status = obj.get('is_status') or {}
                    metrics = obj.get('metrics') or {}
                    submit_status = (obj.get('submit_status') or '').strip()
                    line = _format_alpha_result(s_idx, status, metrics, is_status,
                                                submit_status=submit_status)
                    self._log(_round_num, line,
                              level='pass' if status == 'pass' else 'info')
                    # ★ 제출 시도가 있었으면 (submit_status 가 있거나 submitted) 라운드
                    #   종료를 기다리지 않고 즉시 기록 — 모바일이 실시간 열람.
                    #   어떤 예외도 워커 흐름을 절대 중단시키지 않는다.
                    if submit_status or obj.get('submitted'):
                        try:
                            _code = ''
                            for _b in batch:
                                if int(_b.get('idx') or 0) == s_idx:
                                    _code = _b.get('code', '')
                                    break
                            _isr = obj.get('is_status') or {}
                            _db.record_submit_attempt(
                                self.user_id, _round_num, s_idx, _code,
                                bool(obj.get('submitted')), submit_status,
                                len(_isr.get('pass', []) or []),
                                len(_isr.get('fail', []) or []))
                        except Exception as _e:
                            self._log_quiet(_round_num,
                                            f'⚠ submit_attempt 기록 실패: {_e}')

                try:
                    results = wqb_browser.simulate_batch(
                        batch,
                        wqb_username=username, wqb_password=password,
                        account_type=account_type,
                        stop_event=self._stop_event,
                        log_fn=None,  # [pw]/[playwright] 로그가 UI 로 흘러들어오지 않도록 끔
                        proc_holder=self._batch_proc_holder,
                        partial_fn=_on_partial,
                        forced_delay=forced_delay,
                    )
                except Exception as e:
                    results = [{
                        'idx': s_['idx'], 'code': s_['code'], 'desc': s_.get('desc', ''),
                        'pass_count': 0, 'pass_items': [],
                        'fail_count': 0, 'fail_items': [],
                        'submitted': False, 'submit_status': '',
                        'error_text': f'시뮬 예외: {e}',
                        'mode': 'error',
                    } for s_ in batch]

                aborted = False
                if self._stop_event.is_set():
                    self._log(round_num, '  ⏸ pause 처리됨 — 시뮬 결과 폐기')
                    aborted = True

                # WQB 새 디바이스 인증 등 사용자 액션이 필요한 영구 에러 — 즉시 라운드 abort + 워커 종료.
                if not aborted and results and any(
                        _is_auth_required(r.get('error_text') or '') for r in results):
                    self._log(round_num, '  🛑 WQB 가 새 디바이스/2FA 인증 요구 — 자동화 불가, 워커 종료')
                    all_results.extend(results)
                    self._stop_event.set()
                    aborted = True

                # setup 에러 전부면 (= 브라우저/로그인 자체가 깨짐) 1회 재시도 — subprocess 가
                # 새 브라우저를 다시 띄운다.
                if not aborted and results and all(
                        _is_setup_error(r.get('error_text') or '') for r in results):
                    self._log(round_num, '  ⚠ 시뮬 전체 setup 에러 — 브라우저 재시작 후 재시도')
                    try:
                        retry = wqb_browser.simulate_batch(
                            batch,
                            wqb_username=username, wqb_password=password,
                            account_type=account_type,
                            stop_event=self._stop_event,
                            log_fn=None,
                            proc_holder=self._batch_proc_holder,
                        )
                        if not all(_is_setup_error(r.get('error_text') or '') for r in retry):
                            results = retry
                            self._log(round_num, '  ✓ 재시도 성공')
                        else:
                            self._log(round_num, '  ⚠ 재시도도 setup 에러 — 라운드 결과 그대로 진행')
                    except Exception:
                        pass

                if not aborted:
                    # 라운드 시뮬 결과 — 한 줄 요약. 알파별 줄은 partial_fn 스트림이 이미 송출함.
                    # partial 미수신된 알파 (예: subprocess KILL) 만 여기서 보충.
                    r_pass = sum(1 for r in results
                                 if int(r.get('pass_count') or 0) >= PASS_THRESHOLD)
                    r_err = sum(1 for r in results if r.get('error_text'))
                    self._log(round_num,
                              f'  ── 시뮬 결과 — '
                              f'알파 {len(results)} / PASS≥{PASS_THRESHOLD} {r_pass} / 오류 {r_err}')
                    for r in results:
                        if int(r.get('idx') or 0) in _seen_idx:
                            continue
                        err_t = (r.get('error_text') or '').strip()
                        metrics = r.get('metrics') or {}
                        is_status = r.get('is_status') or {}
                        if err_t:
                            snippet = err_t[:80] + ('…' if len(err_t) > 80 else '')
                            self._log(round_num, f'      #{r["idx"]} ⚠ 오류 — {snippet}',
                                      level='warn')
                        else:
                            p_n = len(is_status.get('pass', []) or [])
                            f_n = len(is_status.get('fail', []) or [])
                            e_n = len(is_status.get('error', []) or [])
                            is_pass = (p_n >= PASS_THRESHOLD and f_n == 0 and e_n == 0) \
                                      if (p_n + f_n + e_n) > 0 \
                                      else (int(r.get('pass_count') or 0) >= PASS_THRESHOLD)
                            status = 'pass' if is_pass else 'fail'
                            sub_st = (r.get('submit_status') or '').strip()
                            line = _format_alpha_result(int(r['idx']), status, metrics, is_status,
                                                        submit_status=sub_st)
                            self._log(round_num, line,
                                      level='pass' if is_pass else 'info')

                    all_results.extend(results)

            # 결과 저장 + feedback / errors 누적 (UI 로그는 PASS 만 노출).
            for r in all_results:
                # delay-aware 캐시용 stamp — 갓 시뮬한 결과엔 이번 라운드 강제 delay 를,
                # 캐시 재사용 결과(cached)엔 원본 _delay 를 그대로 보존한다.
                _metrics = dict(r.get('metrics') or {})
                if not r.get('cached'):
                    _metrics['_delay'] = str(forced_delay)
                alpha_entry = {
                    'idx': r['idx'],
                    'code': r['code'],
                    'desc': r.get('desc', ''),
                    'pass_count': int(r.get('pass_count') or 0),
                    'pass_items': r.get('pass_items') or [],
                    'fail_count': int(r.get('fail_count') or 0),
                    'fail_items': r.get('fail_items') or [],
                    'error_count': int(r.get('error_count') or 0),
                    'pending_count': int(r.get('pending_count') or 0),
                    'submitted': r.get('submitted', False),
                    'submit_status': r.get('submit_status', ''),
                    'error_text': r.get('error_text', ''),
                    'metrics': _metrics,
                    'is_status': r.get('is_status') or {},
                    'mode': r.get('mode', ''),
                    'cached': bool(r.get('cached')),
                    'phase': phase,
                    'settings': settings_by_idx.get(int(r['idx']), {}),
                    'delay': forced_delay,
                    'self_corr': r.get('self_corr'),
                    'generation': r.get('generation', 0),
                    'parent_alpha_id': r.get('parent_alpha_id'),
                }
                _db.insert_alpha(self.user_id, round_id, round_num, alpha_entry)

                # 밴딧 보상 업데이트 — 비-focus 라운드, 밴딧 ON 시에만. per-alpha flush.
                if _bandit_on and not is_focus:
                    try:
                        from . import reward as _reward, bandit as _bandit
                        _m = dict(r.get('metrics') or {})
                        _rwd = _reward.compute_reward(
                            _m,
                            pass_count=int(r.get('pass_count') or 0),
                            fail_count=int(r.get('fail_count') or 0),
                            error_count=int(r.get('error_count') or 0),
                            self_corr=r.get('self_corr'))
                        _set = settings_by_idx.get(int(r['idx']), {}) or {}
                        _assign = {
                            'universe': (_set.get('universe') or 'TOP3000'),
                            'neutralization': (_set.get('neutralization') or 'INDUSTRY'),
                            'decay_bucket': _bandit.decay_to_bucket(_set.get('decay', 0)),
                        }
                        for _ak in _bandit.arm_keys_for_assignment(_assign):
                            _dim = _ak.split(':', 1)[0]
                            _db.bandit_update(self.user_id, _ak, _rwd, round_num,
                                              dimension=_dim)
                    except Exception as _e:
                        self._log_quiet(round_num, f'⚠ bandit update 실패: {_e}')

                if r.get('error_text'):
                    err_total += 1
                    _db.upsert_error(self.user_id, round_num, r['code'], r['error_text'][:600])

                fb_payload = {
                    'round': round_num, 'idx': r['idx'],
                    'code': r['code'], 'desc': r.get('desc', ''),
                    'pass_count': int(r.get('pass_count') or 0),
                    'fail_count': int(r.get('fail_count') or 0),
                    'pass_items': (r.get('pass_items') or [])[:8],
                    'fail_items': (r.get('fail_items') or [])[:8],
                    'metrics': r.get('metrics') or {},
                }
                _db.append_feedback(self.user_id, round_num, fb_payload)

                pc = int(r.get('pass_count') or 0)
                fc = int(r.get('fail_count') or 0)
                total = pc + fc
                # IS Testing Status 가 있으면 그쪽 권위 — fail=0 AND error=0 AND pass>=threshold.
                ist_r = r.get('is_status') or {}
                p_n = len(ist_r.get('pass', []) or [])
                f_n = len(ist_r.get('fail', []) or [])
                e_n = len(ist_r.get('error', []) or [])
                sub_status = (r.get('submit_status') or '').strip()
                # Submit 시점 self-correlation 거절 → 8개 IS 테스트 중 7개(실질 테스트) 전부
                # 통과한 케이스. is_status 에 self-corr FAIL 1개가 들어가 있어도 "PASS≥7 best"
                # 로 계속 인정 (제출만 못 했을 뿐).
                is_best = _is_best_alpha(r)   # 단일 진실: 위 _is_best_alpha 헬퍼
                if is_best:
                    pass_total += 1
                    if r.get('submitted'):
                        submit_tag = ' · 🚀 알파 제출 완료'
                    elif sub_status.startswith('rejected:'):
                        submit_tag = f' · ⛔ 제출 거절 ({sub_status[len("rejected:"):][:48]})'
                    elif sub_status == 'disabled':
                        submit_tag = ' · ⛔ Submit 버튼 비활성 (제출 조건 미충족)'
                    elif sub_status == 'not_found':
                        submit_tag = ' · ⚠ Submit 버튼 못 찾음'
                    elif sub_status.startswith('fail:'):
                        submit_tag = f' · ⚠ 제출 실패 ({sub_status[5:][:30]})'
                    else:
                        submit_tag = ''
                    _denom = (p_n + f_n) if (p_n + f_n) else (total or '?')
                    self._log(round_num,
                              f'    #{r["idx"]} 🏆 PASS {p_n or pc}/{_denom} — best 발견!{submit_tag}',
                              level='pass')

            status = 'paused' if self._stop_event.is_set() else 'done'
            label = _round_label(round_num, parent_idx, phase)
            summary = (f'═══ ROUND {label} {status} — 시도 {len(all_results)} / '
                       f'PASS≥{PASS_THRESHOLD} {pass_total} / 오류 {err_total} / '
                       f'캐시히트 {cache_hit_total} ═══')
            # 라운드 끝 — 색깔 다르게 입히기 위해 level=round_end 로 보냄. 클라이언트가
            # round_num 을 6 컬러팔레트에 매핑.
            self._log(round_num, summary, level='round_end')
            _db.finish_round(round_id, self.user_id, round_num,
                              status=status, pass_count=pass_total, err_count=err_total,
                              cache_hits=cache_hit_total, summary=summary)

            # focus 큐 관리.
            # paused/인터럽트 라운드는 큐를 건드리지 않는다 (재개 시 같은 항목 이어서 처리).
            if status == 'paused' or self._stop_event.is_set():
                pass
            elif is_focus:
                # 방금 처리한 entry 를 (round_num, phase, parent_idx) 로 매칭 제거한다.
                # ⚠ 선택은 closeness 정렬 기준(near-miss 우선)이라 FIFO 맨 앞과 다를 수 있으므로
                #    'FIFO 맨 앞'이 아니라 '실제 처리한' 항목을 제거해야 한다.
                #    (이 불일치가 round-560 무한루프의 원인이었다 — selection=idx8, pop=idx1.)
                #    status!='done' 인 실패 라운드는 attempts 를 세고 N회 연속 시 강제 포기한다.
                try:
                    cur_q = _db.get_focus_queue(self.user_id)
                    new_q, action = _advance_focus_queue(
                        cur_q, round_num, phase, parent_idx, status,
                        max_attempts=_MAX_CONSEC_FAILS,
                    )
                    if action == 'removed':
                        _db.set_focus_queue(self.user_id, new_q)
                        self._log(round_num,
                                  f'  focus 큐 #{parent_idx} (phase {phase}) 처리 완료 (남은 항목 {len(new_q)})')
                    elif action == 'giveup':
                        _db.set_focus_queue(self.user_id, new_q)
                        self._log(round_num,
                                  f'  ⚠ focus 큐 #{parent_idx} (phase {phase}) '
                                  f'{_MAX_CONSEC_FAILS}회 연속 실패 → 강제 포기 (남은 항목 {len(new_q)})',
                                  level='pass')
                    elif action == 'retry':
                        # attempts 카운트만 영속화 — 다음 라운드 재시도.
                        _db.set_focus_queue(self.user_id, new_q)
                    # action == 'nomatch': 큐가 외부에서 바뀐 경우 — 그대로 둔다.
                except Exception as e:
                    self._log_quiet(round_num, f'⚠ focus 큐 갱신 실패: {e}')
            elif status == 'done':
                # 메인 라운드 종료 — PASS>=6 이고 미통과 항목(FAIL/ERROR)이 남은 알파마다
                # FAIL 사유를 넣어 3 라운드(phase 1·2·3)씩 개선 변형을 큐에 추가한다.
                def _fail_descs_of(r: dict) -> list[str]:
                    ist = r.get('is_status') or {}
                    return [
                        str(it.get('desc') or it.get('name') or '')
                        for it in (list(ist.get('fail') or [])
                                   + list(ist.get('error') or []))
                    ]

                focus_candidates: list[dict] = []
                _far_skipped = 0
                for r in all_results:
                    _kind, _ = _classify_focus(r)
                    if not _kind:
                        continue
                    # 절대 closeness 하한선 — 통과까지 너무 먼(예: Sharpe 0.07) 부모는
                    # directed-mutation 으로 5배 끌어올리는 게 사실상 불가능하므로 큐에 넣지
                    # 않고 예산을 탐색으로 돌린다. delay=0 은 hopeless 부모가 많아 특히 중요.
                    try:
                        _cs = _closeness_score(_fail_descs_of(r))
                    except Exception:
                        _cs = 0.0  # 점수 계산 실패 시 보수적으로 통과시킴
                    # NEUTRAL(파싱불가)은 자르지 않는다 — gap 을 측정조차 못한 후보를 조용히
                    # 드롭하면 안 되므로(no silent cap), 측정 가능한 far-miss 만 차단한다.
                    if _cs > _NEUTRAL_SCORE and _cs < FOCUS_CLOSENESS_FLOOR:
                        _far_skipped += 1
                        continue
                    focus_candidates.append(r)
                if _far_skipped:
                    self._log(round_num,
                              f'  focus 제외: 통과에서 너무 먼 부모 {_far_skipped}개 '
                              f'(closeness < {FOCUS_CLOSENESS_FLOOR}) — 연마 대신 탐색에 예산 회수')

                # 라운드당 focus 후보 폭주 방지(pass-5 는 흔함) — closeness(통과 근접) 상위 N개만.
                if len(focus_candidates) > FOCUS_MAX_PER_ROUND:
                    try:
                        focus_candidates.sort(
                            key=lambda r: _closeness_score(_fail_descs_of(r)),
                            reverse=True,
                        )
                    except Exception:
                        pass
                    focus_candidates = focus_candidates[:FOCUS_MAX_PER_ROUND]

                if focus_candidates:
                    new_q = _db.get_focus_queue(self.user_id)
                    PHASES_PER_PARENT = 3
                    for a in focus_candidates:
                        ist_r = a.get('is_status') or {}
                        f_list = list(ist_r.get('fail') or []) + list(ist_r.get('error') or [])
                        p_list = list(ist_r.get('pass') or [])
                        fail_descs = [
                            str(it.get('desc') or it.get('name') or '').strip()
                            for it in f_list
                        ]
                        pass_descs = [
                            str(it.get('desc') or it.get('name') or '').strip()
                            for it in p_list
                        ]
                        fd = ' / '.join([d for d in fail_descs if d])[:200]
                        for ph in range(1, PHASES_PER_PARENT + 1):
                            new_q.append({
                                'parent_round_num': round_num,
                                'phase': ph,
                                'parent_idx': int(a.get('idx') or 0),
                                'parent_code': str(a.get('code') or ''),
                                'parent_desc': str(a.get('desc') or ''),
                                'fail_desc': fd,
                                'parent_pass_items': pass_descs[:8],
                                'parent_fail_items': fail_descs[:4],
                                'focus_kind': 'fail',
                                'self_corr_value': '',
                                # 부모 settings 를 실어 보내 다음 focus 라운드의 settings 스윕이
                                # 부모의 smoothing(decay 등)을 계승하고 자기 조합은 건너뛰게 한다.
                                'parent_settings': settings_by_idx.get(int(a.get('idx') or 0), {}),
                            })
                    _db.set_focus_queue(self.user_id, new_q)
                    self._log(round_num,
                              f'  🎯 PASS≥{FOCUS_MIN_PASS} focus 후보 {len(focus_candidates)}개 — '
                              f'각 {PHASES_PER_PARENT} 라운드씩 FAIL 개선 변형 생성 예정 '
                              f'(총 {len(focus_candidates) * PHASES_PER_PARENT} sub-round)',
                              level='pass')
        except Exception as e:
            self._log(round_num, f'⚠ 라운드 예외: {e}')
            _db.finish_round(round_id, self.user_id, round_num,
                              status='error', pass_count=pass_total,
                              err_count=err_total, cache_hits=cache_hit_total,
                              summary=f'예외: {str(e)[:300]}')

    def _prev_cache_hit_ratio(self) -> float:
        """직전 라운드의 cache_hits / (시도 알파 수). 1.0 에 가까우면 거의 모든 알파가 캐시에서
        해결됨 → Gemini 가 같은 코드를 또 생성한 것. temperature 부스트로 다양성 강제."""
        rounds = _db.list_rounds(self.user_id, limit=1)
        if not rounds:
            return 0.0
        r = rounds[0]
        ch = int(r.get('cache_hits') or 0)
        # 한 라운드당 시도 알파 수 ≈ pass_count + err_count + cache_hits 추정.
        # 정확히는 alphas insert 카운트가 맞지만, summary 로 충분.
        attempts = ch + int(r.get('pass_count') or 0) + int(r.get('err_count') or 0)
        if attempts <= 0:
            return 0.0
        return ch / attempts

    # ── 로깅 ──────────────────────────────────────────────────
    def _log(self, round_num: int, line: str, level: str = 'info') -> None:
        try:
            _db.append_log(self.user_id, round_num, line, level=level)
        except Exception:
            pass

    def _log_quiet(self, round_num: int, line: str) -> None:
        """gemini_strategist 등 외부 모듈에서 들어오는 로그를 필터링.

        UI 에는 핵심 워닝 (⚠ / 🛑) 만 노출. 디버그성 진행 상황 (prompt cache, 모델 폴백,
        lint 거부 상세 등) 은 server.log Python logger 로만 보냄.
        """
        s = (line or '').strip()
        if not s:
            return
        # 워닝/오류 표식이 있는 줄만 UI 노출.
        if s[:2] in ('⚠ ', '⚠'):
            self._log(round_num, line)
            return
        if any(s.startswith(c) for c in ('🛑', '✓ ', '✓')):
            self._log(round_num, line)
            return
        # 그 외는 Python logger 로만.
        LOG.info('[round %d] %s', round_num, s[:300])


def _extract_self_corr_value(fail_items: list[dict]) -> str:
    """is_status['fail'] 안의 self-correlation 항목에서 실측값 (예: '0.9415') 을 뽑는다.

    `_scrape_is_testing_status` 가 'Self-correlation of 0.9415 is above cutoff of 0.7' 형식을
    {name:'Self-correlation', value:'0.9415', cutoff:'0.7'} 로 파싱해 두므로 그대로 활용.
    매치 없으면 빈 문자열 반환.
    """
    import re as _re
    for it in fail_items or []:
        nm = (it.get('name') or '').lower()
        if 'correlation' in nm or 'self-corr' in nm or 'self corr' in nm:
            v = (it.get('value') or '').strip()
            if v:
                return v
            desc = (it.get('desc') or '')
            m = _re.search(r'(\d+\.\d+)', desc)
            if m:
                return m.group(1)
    return ''


def _is_counts(r: dict) -> tuple[int, int, int, int]:
    """결과 r 의 is_status 에서 (pass, fail, error, pending) 갯수. 없으면 0."""
    ist = r.get('is_status') or {}
    return (len(ist.get('pass', []) or []), len(ist.get('fail', []) or []),
            len(ist.get('error', []) or []), len(ist.get('pending', []) or []))


def _is_best_alpha(r: dict) -> bool:
    """라운드 요약의 'best' 판정 (인라인 로직과 동일):
    IS 권위 있으면 PASS>=T AND (FAIL=0&ERR=0 또는 self-corr 거절뿐),
    IS 없으면 pass_count>=T."""
    p_n, f_n, e_n, _ = _is_counts(r)
    sub_status = (r.get('submit_status') or '').strip()
    ist_r = r.get('is_status') or {}
    _fail0_name = ((ist_r.get('fail') or [{}])[0].get('name') or '').lower()
    only_selfcorr_fail = (f_n == 1 and e_n == 0 and 'correlation' in _fail0_name)
    _rej = sub_status.startswith('rejected') or sub_status.startswith('fail:no_response')
    if (p_n + f_n + e_n) > 0:
        return (p_n >= PASS_THRESHOLD
                and ((f_n == 0 and e_n == 0) or (only_selfcorr_fail and _rej)))
    return int(r.get('pass_count') or 0) >= PASS_THRESHOLD


def _classify_focus(r: dict) -> tuple[str | None, str]:
    """focus 큐 후보 분류 → (kind|None, self_corr_value).

    새 정책 (제출 폐지): PASS>=6 이면서 아직 통과 못한 항목(FAIL/ERROR)이 남아 있으면
    'fail' 개선 focus 대상이다. 이런 알파마다 FAIL 사유를 넣어 3 라운드씩 개선 변형을 생성한다.
    (self-correlation 은 별표 단계에서 Correlation 상자의 Maximum 으로 직접 확인하므로
     제출-거절 기반 'correlation' kind 는 더 이상 사용하지 않는다.)"""
    if r.get('cached'):
        return (None, '')
    p_n, f_n, e_n, pn_n = _is_counts(r)
    if (p_n + f_n + e_n + pn_n) > 0:
        pc, fc, ec = p_n, f_n, e_n
    else:
        pc = int(r.get('pass_count') or 0)
        fc = int(r.get('fail_count') or 0)
        ec = int(r.get('error_count') or 0)
    if pc >= FOCUS_MIN_PASS and (fc + ec) >= 1:
        return ('fail', '')
    return (None, '')


def _short_metric_label(entry: dict) -> str:
    """IS Testing Status 한 항목 → '이름(값 op cutoff)' 짧은 표기.
    예: {'name':'Sharpe','value':'-0.06','direction':'below','cutoff':'1.25'} → 'Sharpe(-0.06<1.25)'
    """
    name = (entry.get('name') or '').strip() or '?'
    v = (entry.get('value') or '').strip()
    cutoff = (entry.get('cutoff') or '').strip()
    direction = (entry.get('direction') or '').strip()
    if v and cutoff and direction:
        op = '>' if direction == 'above' else '<'
        return f'{name}({v}{op}{cutoff})'
    if v:
        return f'{name}({v})'
    return name


def _format_alpha_result(idx: int, status: str, metrics: dict, is_status: dict | None = None,
                          submit_status: str = '') -> str:
    """슬롯 결과 한 줄 — IS Testing Status 패널 의 항목별 PASS/FAIL/PENDING 그대로 노출.

    is_status 가 있으면 그쪽 권위 데이터 사용 (실제 WQB cutoff 값 표시).
    없으면 (legacy) summary metrics 만으로 6항목 추정.
    submit_status 가 'rejected:*' 이면 거절 사유와 (가능 시) self-correlation 값 표기.
    """
    is_status = is_status or {}
    p_list = is_status.get('pass') or []
    f_list = is_status.get('fail') or []
    e_list = is_status.get('error') or []
    # PENDING(주로 'Self-correlation check pending')은 더 이상 표시/대기하지 않는다 —
    # self-correlation 은 Correlation 상자의 Maximum 으로 직접 읽어 별표 판정에 쓴다.
    total = len(p_list) + len(f_list) + len(e_list)
    sub_st = (submit_status or '').strip()

    # 별표(저장) 상태 + self-correlation 값 표기.
    star_note = ''
    if sub_st.startswith('starred'):
        star_note = f'  ⭐ 별표 저장 ({sub_st[len("starred"):].strip().strip("()") or "self-corr ?"})'
    elif sub_st.startswith('skip_star'):
        star_note = f'  ☆ 미저장 — {sub_st[len("skip_star:"):].strip()}'
    elif sub_st.startswith('star_fail'):
        star_note = f'  ⚠ 리스트 제출 실패 ({sub_st[len("star_fail"):].strip().strip("()")})'
    elif sub_st.startswith('rejected') or sub_st.startswith('fail:'):  # 구버전 제출 기록 호환
        star_note = f'  📝 {sub_st.split(":", 1)[-1].strip()[:80]}'

    if total > 0:
        pass_str = ' '.join(_short_metric_label(e) for e in p_list) or '(없음)'
        fail_str = ' '.join(_short_metric_label(e) for e in f_list) or '(없음)'
        err_str = ' '.join((e.get('name') or '?').strip() for e in e_list)
        head_status = 'PASS' if status == 'pass' else 'fail'
        check = ' ✓' if status == 'pass' else ''
        head = f'      #{idx} → {head_status} ({len(p_list)} PASS / {len(f_list)} FAIL'
        if e_list:
            head += f' / {len(e_list)} ERR'
        head += f'){check}'
        body = f'  ✓ {pass_str}  ✗ {fail_str}'
        if err_str:
            body += f'  ⚠ {err_str}'
        body += star_note
        return head + body

    # Fallback — IS Testing Status 미수신 시 summary metrics 기반 표기 (값 포함).
    from . import wqb_browser as _wqb
    passes, fails = _wqb._derive_pass_fail(metrics or {})
    pc = len(passes)

    def _fmt_legacy(name: str) -> str:
        # 'IS Sharpe' → 'Sharpe(1.32)'; 'Sub-Sharpe' 등은 후보 키들에서 검색.
        short = name.replace('IS ', '').strip()
        candidates = (
            short.lower(),
            short.lower().replace(' ', '_'),
            short.lower().replace('-', '_'),
        )
        v = None
        for k in candidates:
            v = (metrics or {}).get(k)
            if v not in (None, ''):
                break
        return f'{short}({v})' if v not in (None, '') else short

    pass_str = ' '.join(_fmt_legacy(p) for p in passes) or '(없음)'
    fail_str = ' '.join(_fmt_legacy(f) for f in fails) or '(없음)'
    head = f'      #{idx} → {"PASS" if status == "pass" else "fail"} ({pc}/{len(passes)+len(fails) or 8})'
    if status == 'pass':
        head += ' ✓'
    return f'{head}  ✓ {pass_str}  ✗ {fail_str}  (※ IS Testing Status 패널 미수신)'


def _is_setup_error(text: str) -> bool:
    if not text:
        return False
    # auth_required 는 setup 이 아님 (재시도해도 똑같음, 사용자 액션 필요).
    if _is_auth_required(text):
        return False
    sigs = (
        'playwright_setup', 'editor mount timeout', 'tab click failed',
        'set editor text failed', 'text verify fail', 'sim wait timeout',
        'no result returned for this simulation', 'no result for slot',
        'browser timeout', 'RESULT_JSON 파싱 실패',
    )
    return any(s in text for s in sigs)


def _is_auth_required(text: str) -> bool:
    """WQB 가 새 디바이스 인증/2FA 를 요구하는 에러인지."""
    if not text:
        return False
    t = text.lower()
    sigs = (
        '새 디바이스 인증', 'new device', 'verification code',
        'two-factor', '2fa', 'verify your identity', 'mfa',
        'auth_required', 'wqb_auth_required',
    )
    return any(s in t for s in sigs)
