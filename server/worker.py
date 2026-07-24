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
from . import genome_models
from . import wqb_backend
from . import run_config
from . import wqb_data_service
from . import settings_fp as _settings_fp
from . import alpha_ast as _alpha_ast
from . import alpha_repair as _alpha_repair
from . import alpha_lint as _alpha_lint
from . import criteria as _criteria
from .focus_priority import closeness_score as _closeness_score
from .focus_priority import advance_focus_queue as _advance_focus_queue
from .focus_priority import NEUTRAL_SCORE as _NEUTRAL_SCORE

LOG = logging.getLogger('genomicwqb.worker')

# PASS_THRESHOLD — 최소 통과 항목 수.
# ⚠ 2026-07-21: 기본값을 7 → 1 로 내렸다. 제출 규칙 개편으로 고회전(HTVR) 분류를 얻은
#   알파는 표준 컷(LOW_SHARPE/LOW_FITNESS/LOW_2Y_SHARPE/CLUSTER_TEST)이 전부 WARNING
#   으로 강등되면서 **차단 PASS 가 4개까지 줄어든다**. 7 을 요구하면 실제로 제출 가능한
#   알파가 'fail' 로 기록되고 보상 0 을 받는다 — 라이브 gJ9qkKWv 가 정확히 그 경우였다
#   (FAIL 0 / PASS 4 / PENDING 6 → 제출 가능한데 시스템은 실패로 셈).
#   진짜 게이트는 갯수가 아니라 **차단 FAIL 0** 이다 (_is_best_alpha 참조).
PASS_THRESHOLD = int(os.environ.get('HYFE_IQC_PASS_THRESHOLD', '1'))

# FOCUS_MIN_SCORE — 한 알파가 focus(directed-mutation) 정제 대상이 되는 최소 적합도.
# 과거엔 `pass_count >= 5` 라는 이산 게이트였는데, pass_count 는 최대 ~7 인 계단함수라
# 자식(라이브 최대 4)이 절대 통과하지 못했다 → focus 가 사실상 죽고 g2 가 태어날 통로가
# 막혔다(2026-07-11 진단). 이제 연속 reward.selection_score 로 판정한다.
# 라이브 분포 기준 p90≈0.29 라 0.30 은 대략 상위 10% 를 뜻한다.
FOCUS_MIN_SCORE = float(os.environ.get('HYFE_IQC_FOCUS_MIN_SCORE', '0.30'))
# 라운드당 후보 수. 1 = '그 라운드의 최고 알파만' — 점수 분포가 위로 이동해도 focus 가
# 예산을 잠식하지 않게 하는 상대적(rank-based) 규칙이라 절대 문턱보다 안정적이다.
FOCUS_MAX_PER_ROUND = int(os.environ.get('HYFE_IQC_FOCUS_MAX_PER_ROUND', '1'))

# focus 부모의 Sharpe 절대 하한 (2026-07-23, 사장 지시). 근거는 부트캠프 강의의 시드
# 원칙 그대로다: "Sharpe 0.5 짜리를 피팅으로 2까지 끌어올리면 오버피팅이다 — 유의미한
# 시드(≥1.0)만 디벨롭하라." 실제로 7/23 라이브에서 Sharpe 0.1 짜리 부모가 focus 큐를
# 차지하고 있었다(선정 기준이 제출적합도뿐이라 신호 하한이 없었다).
# 부호는 안 본다 — sign 유전자 뒤집기가 공짜라 |Sharpe| 가 신호의 세기다.
FOCUS_MIN_SHARPE = float(os.environ.get('HYFE_IQC_FOCUS_MIN_SHARPE', '1.0'))

# ── 일일 제출 예산 (2026-07-21 신설) ──────────────────────────────────────────
# WQB 컨설턴트는 하루 4건까지만 제출할 수 있다(Power Pool 문서 "Max 4 alpha
# submissions in a day"). 0 = 게이트 끔(구 '무조건 제출' 동작).
DAILY_SUBMIT_BUDGET = int(os.environ.get('IQC_DAILY_SUBMIT_BUDGET', '4'))
# 그 4칸을 쓸 최소 가치(reward.submission_value).
# ⚠ **일일 예산은 이월되지 않는다** — 안 쓰면 그냥 사라진다. 그래서 문턱이 높을 때의
#   손해(하루 0건 제출)가 낮을 때의 손해(평범한 알파 제출)보다 크다.
#   게다가 이 문턱은 **2차 필터**다: 여기 도달한 알파는 이미 차단 FAIL 0 = 제출 가능이고,
#   후비용 Sharpe > 0 이라 비용을 내고도 남는 신호다. 쓰레기는 1차 게이트가 이미 막았다.
#   보정 근거(2026-07-21 실측 지표로 계산한 submission_value):
#       Sharpe 1.53 → 0.310 · 1.12 → 0.254 · 0.8 → 0.210 · 0.5 → 0.170 · 0.4 → 0.157
#   0.25 로 두면 Sharpe 1.1 미만이 전부 막혀 하루 0건이 되기 쉽다 → 0.15 로 시작하고
#   라이브 분포가 쌓이면 올린다.
SUBMIT_MIN_VALUE = float(os.environ.get('IQC_SUBMIT_MIN_VALUE', '0.15'))

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

# #4 가이드 리페어 — 시뮬 실패 에러를 표적 수리해 라운드당 알파별 1회 재큐(재시뮬). 기본 on.
GUIDED_REPAIR = os.environ.get('IQC_GUIDED_REPAIR', '1') != '0'

# 밴딧 보상 시간감쇠 계수 — bandit_update 의 exp(-k·Δround). 0.02 ≈ 반감기 ~35라운드:
# 전략/시장 국면이 바뀌면 옛 arm 통계가 서서히 잊혀 최근 관측이 이긴다. 0 = 순수 누적.
BANDIT_DECAY_K = float(os.environ.get('IQC_BANDIT_DECAY_K', '0.02'))

# 정향변이 온라인 학습 — focus 라운드의 변이 축을 규칙 대신 (fail category × directive)
# 누적 성공률의 Thompson sampling 으로 고른다. 관측이 없으면 사전확률=기존 규칙과 동등.
LEARNED_DIRECTIVES = os.environ.get('IQC_LEARNED_DIRECTIVES', '1') != '0'
_REPAIR_POOL_CACHE = {'ts': 0.0, 'pool': None}


def _repair_field_pool():
    """field 스냅 후보 = 라이브+정적 팔레트 필드 ∪ genome curated 필드. 10분 캐시."""
    now = time.time()
    if _REPAIR_POOL_CACHE['pool'] is not None and (now - _REPAIR_POOL_CACHE['ts']) < 600:
        return _REPAIR_POOL_CACHE['pool']
    pool: set[str] = set()
    try:
        from . import datafield_palette
        names = datafield_palette.known_field_names()
        if names:
            pool |= set(names)
    except Exception:
        pass
    try:
        for fam in genome_models.SHARED_DATASETS.values():
            for f in fam:
                pool.add(str(f).lower())
    except Exception:
        pass
    result = sorted(pool)
    _REPAIR_POOL_CACHE.update(ts=now, pool=result)
    return result


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
        # 차단 FAIL 이 0 인 알파는 그 자리에서 제출을 시도한다 — 단, 일일 예산 안에서만
        # (_submit_gate 참조).

    # ── 일일 제출 예산 ────────────────────────────────────────────
    def _submit_gate(self, metrics: dict, self_corr=None, fail_items=None,
                     genome=None) -> tuple[bool, str]:
        """제출할까? (ok, 사유) — wqb_backend 가 **제출 락 안에서** 호출한다.

        WQB 컨설턴트의 제출 한도는 **하루 4건**이다("Max 4 alpha submissions in a day",
        Power Pool 문서). 2026-07 규칙 개편으로 고회전 알파는 Sharpe 1.1 로도 제출
        가능해졌기 때문에, 예전처럼 '완료되면 무조건 제출' 하면 그날 예산 4칸이 **그날
        가장 먼저 통과한 4개**에 소진된다. 뒤에 훨씬 좋은 알파가 나와도 못 낸다.

        그래서 세 겹으로 거른다:
          0) 차단 FAIL: `criteria.is_blocking` FAIL 이 하나라도 있으면 **WQB 가 반드시
             403 으로 거절**한다 — 보낼 이유가 없다
          1) 예산: 오늘 성공 제출이 DAILY_SUBMIT_BUDGET 이상이면 중단
          2) 품질: reward.submission_value 가 SUBMIT_MIN_VALUE 미만이면 보류
        1·2 는 env 로 조정 가능하고, DAILY_SUBMIT_BUDGET=0 이면 그 둘이 꺼진다.
        0 은 끄지 않는다 — 거절이 확정된 요청은 어떤 설정에서도 낭비다.

        ⚠ 0 이 없어서 2026-07-22 라이브에서 FAIL 5개짜리 알파를 계속 제출해
        `rejected:LOW_SHARPE; LOW_FITNESS; HIGH_TURNOVER (http_403)` 를 반복했다.
        """
        # fail_items 는 harvest 의 dict({'name':…}) 리스트일 수도, 저장된 이름
        # 문자열 리스트일 수도 있다 — 둘 다 받는다.
        names = [str((f.get('name') if isinstance(f, dict) else f) or '').strip()
                 for f in (fail_items or [])]
        blocking = [n for n in names if n and _criteria.is_blocking(n)]
        if blocking:
            return False, f'blocking_fail({",".join(blocking[:3])})'
        # 필드셋 쿨다운 (2026-07-24) — 같은 필드 조합이 최근 24h 에 3회+ 거절됐으면
        # 제출을 보류한다. 상관(PROD/PP)은 아이디어=필드 수준 속성이라 중립화·감쇠만
        # 바꾼 형제는 같은 벽에 부딪힌다(실측: 같은 mdl177 3종 변형 19연속 거절).
        # 시뮬·학습은 그대로 — **제출 API 만** 아낀다.
        if genome:
            try:
                fs = frozenset(str(f) for f in (dict(genome).get('fields') or []) if f)
                if fs and fs in set(_db.rejected_fieldsets(self.user_id)):
                    return False, 'fieldset_cooldown(24h)'
            except Exception as e:
                LOG.warning('fieldset cooldown 조회 실패 (제출 계속): %s', e)
        if DAILY_SUBMIT_BUDGET <= 0:
            return True, ''
        try:
            used = _db.submitted_today(self.user_id)
        except Exception as e:
            LOG.warning('submitted_today 조회 실패 (제출 강행): %s', e)
            return True, ''
        if used >= DAILY_SUBMIT_BUDGET:
            return False, f'daily_budget({used}/{DAILY_SUBMIT_BUDGET})'
        try:
            from . import reward as _reward
            val = _reward.submission_value(metrics or {}, self_corr=self_corr)
        except Exception:
            return True, ''
        if val < SUBMIT_MIN_VALUE:
            return False, f'below_value({val:.2f}<{SUBMIT_MIN_VALUE:.2f})'
        return True, ''

    def _retry_stuck_submits(self, round_num: int, username: str,
                             password: str) -> None:
        """일시 장애(pending_timeout/5xx/429)로 끊긴 '차단 FAIL 0' 제출을 재시도한다.

        2026-07-23 신설 — 그날 전 체크 PASS 알파(Sharpe 1.59)가 제출 확인 타임아웃 후
        재시도 없이 유실됐다. 재시뮬이 아니라 지표에 영속화된 WQB 알파 id
        (wqb_backend 가 심는 metrics['wqb_alpha_id'])로 곧장 재제출하므로 시뮬 쿼터
        소모가 0 이다. 라운드당 1건 — 제출은 하루 4건 예산이라 서두를 이유가 없다.
        id 가 없는 레거시 행(7/23 이전)은 재시도 불가라 건너뛴다.
        """
        try:
            cands = _db.stuck_submits(self.user_id)
        except Exception:
            return
        for c in cands:
            wid = str((c.get('metrics') or {}).get('wqb_alpha_id') or '')
            if not wid:
                continue
            ok_gate, reason = self._submit_gate(c.get('metrics') or {}, None,
                                                fail_items=[],
                                                genome=c.get('genome'))
            if not ok_gate:
                self._log_quiet(round_num, f"⏭ 제출 재시도 보류 (#{c['id']}): {reason}")
                return
            self._log(round_num,
                      f"  🔁 끊긴 제출 재시도 — 알파 #{c['id']} "
                      f"({str(c.get('submit_status') or '')[:40]})")
            try:
                from . import wqb_api as _wqb_api
                client = _wqb_api.WqbApiClient(username, password)
                if not client.authenticate():
                    self._log_quiet(round_num,
                                    '⚠ 제출 재시도 — 인증 실패 (다음 라운드에 다시)')
                    return
                # Power Pool 설명 — 재시도 경로도 본 제출과 같은 요건을 지킨다.
                try:
                    from . import alpha_description as _adesc
                    client.set_alpha_description(
                        wid, _adesc.build(str(c.get('code') or ''),
                                          genome=c.get('genome'),
                                          settings=c.get('metrics') or {}))
                except Exception:
                    pass
                ok, st = client.submit_alpha(wid, stop_event=self._stop_event,
                                             deadline_s=600)
            except Exception as e:
                self._log_quiet(round_num, f'⚠ 제출 재시도 예외(무시): {e}')
                return
            try:
                _db.set_alpha_submit_result(c['id'], ok, st)
            except Exception:
                pass
            self._log(round_num,
                      ('  🚀 재시도 제출 성공!' if ok
                       else f'  📝 재시도 결과: {str(st)[:80]}'),
                      level=('pass' if ok else 'info'))
            return                     # 라운드당 1건만

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

    def _wait_for_reauth(self, username: str, password: str) -> bool:
        """생체인증(biometric)/세션 만료로 시뮬이 막혔을 때 워커를 죽이지 않고 조용히 대기한다.

        기존엔 이 지점에서 워커를 종료해, 사용자가 얼굴 인증 후 '진화 실행'을 **다시**
        눌러야 했다(4시간마다 반복 → 성가심의 핵심). 이제는 여기서 대기하며 **로컬 JWT
        만료만** 반복 확인한다(WQB 로 나가는 호출 0건 — 429 폭주 없음). 사용자가 재인증해
        새 토큰이 발급되면(만료시각이 실패 시점보다 늦어짐) 그걸 감지해 자동 재개한다.

        재인증 판정을 '만료가 미래'가 아니라 '만료가 실패 시점보다 늦어짐'으로 두는 이유:
        WQB 가 세션이 살아있는 채로 persona 재검증을 요구할 수 있는데, 그때 '미래면 재개'
        로 두면 같은 토큰으로 재개→또 실패를 반복(플랩)한다. 새 토큰은 만료가 ~4h 뒤로
        점프하므로 exp0 보다 확실히 커진다.

        반환: True=재인증 감지(이어서 재개) · False=사용자가 대기 중 워커를 멈춤.
        """
        from . import wqb_api

        def _jwt_expiry() -> float | None:
            try:
                c = wqb_api.WqbApiClient(username, password)
                if not c._load_session():
                    return None
                return c._expiry_from_jwt()      # 순수 로컬 JWT 디코드(네트워크 없음)
            except Exception:
                return None

        exp0 = _jwt_expiry()                      # 실패 시점의 만료(기준선)
        self._log(0, '⏸ WQB 세션 만료(biometric) — 재인증 대기 중. 대시보드/앱에서 '
                     '얼굴 인증을 완료하면 진화를 자동으로 이어서 재개합니다. '
                     '(다른 계정·작업엔 영향 없음)')
        try:
            _db.set_user_running(self.user_id, running=True, paused=True)
        except Exception:
            pass
        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=30):
                break
            exp = _jwt_expiry()
            if exp is not None and (exp - time.time()) > 60 \
                    and (exp0 is None or exp > exp0 + 30):
                self._log(0, '▶ WQB 재인증 감지 — 진화를 이어서 재개합니다.')
                try:
                    _db.set_user_running(self.user_id, running=True, paused=False)
                except Exception:
                    pass
                return True
        return False

    # ── 단일 라운드 ───────────────────────────────────────────
    def _run_one_round(self) -> None:
        if self._stop_event.is_set():
            return
        creds = _db.get_user_credentials(self.user_id)
        if not creds:
            self._log(0, '⚠ 자격증명 조회 실패 (user 가 삭제됐을 수 있음) — 워커 종료')
            self._stop_event.set()
            return
        username, password, api_key = creds
        account_type = _db.get_account_type(self.user_id)
        # 시뮬 백엔드는 이제 WQB REST API 단일이다 (2026-07-13 Playwright 경로 제거).
        # users.backend 는 세션 keep-alive 가 'api' 계정을 고르는 데 쓰이므로, 미탐침('')
        # 계정은 여기서 1회 탐침해 저장한다(POST /authentication 1회 — 5/min 한도 안).
        backend = _db.get_backend(self.user_id)
        if backend != 'api':
            try:
                from . import auth as _auth
                if _auth.probe_wqb_backend(username, password).get('backend') == 'api':
                    _db.set_backend(self.user_id, 'api')
                    self._log(0, '🔌 백엔드 능력 탐지: WQB REST API')
            except Exception as e:
                self._log_quiet(0, f'⚠ 백엔드 탐침 실패(무시): {e}')
        backend = 'api'   # 시뮬은 항상 REST API 로 시도한다(유일 경로)

        u = _db.get_user(self.user_id)

        # 전략스펙 최우선 — 사용자가 리서치로 명시 요청한 아이디어다. focus 큐 뒤에
        # 세우면 (거의 항상 비어있지 않은) 큐에 밀려 영원히 굶는다.
        # 스펙은 1회성이라 소비되면 큐가 비고 워커는 평소 GA 로 되돌아간다.
        try:
            pending_specs = _db.pending_specs(self.user_id, limit=8)
        except Exception as e:
            self._log_quiet(0, f'⚠ 전략스펙 조회 실패: {e}')
            pending_specs = []
        is_spec_round = bool(pending_specs)

        # focus 큐 — PASS=6 알파에 대한 sub-round 가 대기중이면 그것을 먼저 실행.
        focus_queue = [] if is_spec_round else _db.get_focus_queue(self.user_id)
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
            parent_metrics = dict(focus_entry.get('parent_metrics') or {})
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
            if is_spec_round:
                self._log(round_num,
                          f'═══ ROUND {round_num} 시작 (🧪 전략스펙 {len(pending_specs)}개 '
                          f'— LLM 리서치 산출물 원본 측정) ═══', level='round_start')
            else:
                self._log(round_num, f'═══ ROUND {round_num} 시작 ═══', level='round_start')
            parent_idx = 0
            fail_desc = ''
            parent_code = ''
            parent_desc = ''
            parent_pass_items = []
            parent_fail_items = []
            parent_metrics = {}
            focus_kind = 'fail'
            self_corr_value = ''
            parent_settings = {}

        errors = _db.list_error_patterns(self.user_id, limit=100)
        # 이미 제출된 알파 + Submit 거절/무응답으로 끝난 알파 코드 — Non-RC 사전 유사도
        # 필터의 비교 기준. 거절된 영역(예: 0.94 self-corr)을 다시 만들어봐야 또
        # 거절되므로 미리 피한다.
        submitted_codes = [a.get('code', '') for a in _db.list_submitted_alphas(self.user_id, limit=30)]
        submitted_codes += _db.list_rejected_alpha_codes(self.user_id, limit=40)
        submitted_codes = list(dict.fromkeys(c for c in submitted_codes if c))
        # 탐색 조건 (Power Pool 주간 테마 등) — 생성 **전에** 심어야 이번 라운드의
        # 유전체가 전부 조건 안에서 만들어진다. 매 라운드 다시 읽으므로 재시작 없이
        # 대시보드에서 조건을 갈아끼울 수 있다.
        _constraint = None
        try:
            _constraint = run_config.get_constraint()
            genome_models.set_constraint(_constraint)
        except Exception as e:
            genome_models.set_constraint(None)
            self._log(round_num, f'  ⚠ 탐색 조건 적용 실패(무제약으로 진행): {e}')
        if _constraint is not None:
            self._log(round_num, f'  탐색 조건 = {_constraint.describe()}')
            if _constraint.unparsed:
                self._log(round_num,
                          f'  ⚠ 조건 중 해석 못 한 절: {"; ".join(_constraint.unparsed)}')

        # 끊긴 제출 회수 (2026-07-23) — 시뮬 전에 가볍게 확인한다 (쿼터 0 소모).
        if account_type == 'research_consultant' and not self._stop_event.is_set():
            try:
                self._retry_stuck_submits(round_num, username, password)
            except Exception as e:
                self._log_quiet(round_num, f'⚠ 제출 재시도 블록 실패(무시): {e}')

        # GA 엘리트 seed 풀 — 최근 윈도우에서 selection_score 상위 유전체를 그대로 가져온다.
        # ⚠ 코드에서 유전체를 역추출하지 않는다. 그건 손실 압축이라 자식이 부모를 복제조차
        #    못 해 구조적으로 부모보다 나쁜 자식만 나온다(2026-07-11 진단).
        try:
            seeds = _db.elite_seeds(self.user_id, top_n=5)
        except Exception as e:
            self._log_quiet(round_num, f'⚠ seeds 조회 실패: {e}')
            seeds = []
        # 위원회 교차쌍 (2026-07-23) — 탈상관 심사역이 고른 (X,Y) 쌍을 시드 앞자리에
        # **인접 배치**한다. _plan_ga 의 교차는 seeds[j]×seeds[j+1] 이라 인접 = 실제
        # 교차 지시가 된다. 모든 쌍을 시뮬하는 O(n²) 대신 LLM 이 후보를 좁히는 구조.
        try:
            from . import committee as _committee
            _cpolicy = _committee.active_policy(self.user_id, round_num)
            if _cpolicy and _cpolicy.get('seed_pairs'):
                _pool = _db.elite_seeds(self.user_id, top_n=12)
                _reordered = _committee.order_seeds_with_pairs(
                    _pool, _cpolicy, fallback=seeds)
                if _reordered is not seeds and len(_reordered) >= 2:
                    seeds = _reordered
                    self._log(round_num,
                              f'  🏛 위원회 교차쌍 {len(_cpolicy["seed_pairs"])}쌍 — '
                              f'시드 앞자리에 인접 배치')
        except Exception as e:
            self._log_quiet(round_num, f'⚠ 위원회 교차쌍 적용 실패(무시): {e}')
        seed_genomes: list[dict] = []
        seed_alpha_ids: list[int | None] = []
        _dropped_seeds = 0
        for sd in seeds:
            if isinstance(sd.get('genome'), dict):
                # ⚠ 조건과 구조적으로 안 맞는 시드는 **버린다**(고치지 않는다).
                #   필드를 강제 교체하면 원본 구조가 깨져 부모보다 나쁜 자식만 나온다.
                #   2026-07-22 실측: pv1 금지 주에 pv1 엘리트(Sharpe 3.77)를 개조해
                #   Sharpe 0.2 짜리만 양산하고 있었다.
                if genome_models.violates_constraint(sd['genome']):
                    _dropped_seeds += 1
                    continue
                seed_genomes.append(sd['genome'])
                # 시드의 alphas.id — 이 시드에서 나온 자식의 parent_alpha_id 귀속용.
                seed_alpha_ids.append(sd.get('id'))
        if _dropped_seeds:
            self._log(round_num,
                      f'  ⚠ 조건과 안 맞는 시드 {_dropped_seeds}개 제외 '
                      f'(남은 시드 {len(seed_genomes)}개) — 개조하면 원본 구조가 깨진다')

        _bandit_on = run_config.is_bandit_enabled()
        pass_total = 0
        err_total = 0
        cache_hit_total = 0

        # 이번 라운드의 delay 를 생성 전에 확정해 프롬프트(필드 선택 유도)와 시뮬
        # settings 양쪽에 같은 값을 넘긴다. 출처는 **탐색 조건 하나**다 — 조건이
        # 안 정하면 run_config.DEFAULT_DELAY(D1). 예전의 별도 delay 테스트 토글은
        # 같은 값을 두 곳에서 정해 사고를 냈으므로 제거했다(2026-07-22).
        forced_delay = run_config.round_delay(_constraint)
        if is_spec_round:
            # 스펙은 delay(=latency)를 전략의 일부로 지정한다. 라운드는 delay 하나만
            # 가질 수 있으므로 다수결로 정하고, 소수파 스펙은 다음 라운드로 미룬다
            # (delay 를 임의로 바꿔 시뮬하면 LLM 이 설계한 전략이 아니게 된다).
            _votes: dict[str, int] = {}
            for _s in pending_specs:
                _d = str(_s.get('delay') if _s.get('delay') is not None else forced_delay)
                _votes[_d] = _votes.get(_d, 0) + 1
            forced_delay = max(_votes, key=lambda k: _votes[k])
            _kept = [_s for _s in pending_specs
                     if str(_s.get('delay') if _s.get('delay') is not None
                            else forced_delay) == forced_delay]
            if len(_kept) < len(pending_specs):
                self._log(round_num,
                          f'  스펙 {len(pending_specs) - len(_kept)}개는 delay 가 달라 '
                          f'다음 라운드로 이월 (이번 라운드 delay={forced_delay})')
            pending_specs = _kept
            self._log(round_num, f'  Delay = 스펙 지정값 {forced_delay}')
        elif _constraint is not None and _constraint.delay is not None:
            self._log(round_num, f'  Delay = 탐색 조건 지정값 {forced_delay}')
        else:
            self._log(round_num, f'  Delay = 기본값 {forced_delay} (조건 미지정)')

        # 밴딧 arm 선택 — 비-focus 라운드에서만 실행, 밴딧 ON 시에만.
        # 선택된 arm 은 generate_population 의 slot_settings 로 넘어가 '무작위 탐색'
        # 슬롯의 settings 유전자에 실제로 주입된다 (선택→플레이→보상 루프 복원).
        if _bandit_on and not is_focus and not is_spec_round:
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
            # 위원회 정책 (2026-07-23) — 유효한 정책이 있으면 슬롯 배정의 주도권을 LLM
            # 위원회에 넘긴다(서치스페이스를 사람이 아니라 AI 가 정한다는 방침).
            # 정책이 없거나 낡았으면 기존 epsilon-greedy 그대로 (fail-open).
            _policy = None
            try:
                from . import committee as _committee
                _policy = _committee.active_policy(self.user_id, round_num)
            except Exception as _e:
                self._log_quiet(round_num, f'⚠ 위원회 정책 조회 실패(무시): {_e}')
            if _policy:
                _slot_settings = _committee.slots_from_policy(
                    _policy, n_slots=8, stats=_stats, rng=_random.Random(round_num))
                self._log(round_num,
                          f"  🏛 위원회 정책 적용 (r{_policy.get('round')} 확정, "
                          f"슬롯 {len(_policy.get('slot_settings') or [])}개 지정)")
            else:
                _slot_settings = _bandit.select_slots(
                    _stats, n_slots=8, epsilon=_epsilon, explore_slots=3,
                    rng=_random.Random(round_num),
                )
            try:
                _db.update_round_config(round_id, delay_mode=str(forced_delay),
                                        explore_exploit='3/7',
                                        injected_arms=_json.dumps(_slot_settings))
            except Exception:
                pass
        else:
            _slot_settings = None

        try:
            model_label = (
                'Research Consultant API Genome'
                if account_type == 'research_consultant'
                else 'Non-RC Playwright Genome'
            )
            # focus 라운드의 부모 유전체 — 큐에 저장된 정확한 유전체 우선, 없으면
            # (레거시 큐 항목) 부모 code+settings 에서 역추출한다.
            parent_genome = None
            if is_focus:
                parent_genome = (focus_entry or {}).get('parent_genome')
                if not parent_genome and parent_code:
                    # 레거시 큐 항목 폴백 — 유전체가 없으니 역추출한다. 세대는 코드에서
                    # 복원되지 않으므로 큐가 실어 온 값을 넘긴다(없으면 0).
                    try:
                        parent_genome = genome_models.genome_from_alpha(
                            parent_code, settings=parent_settings,
                            generation=int((focus_entry or {}).get('parent_generation') or 0))
                    except Exception:
                        parent_genome = None

            if is_focus:
                kind_label = 'correlation 회피' if focus_kind == 'correlation' else 'fail 개선'
                self._log(round_num,
                          f'1) 8 Genome 알파 생성 중 [{kind_label} — 정향변이] '
                          f'(model={model_label}, 부모 #{parent_idx}, fix="{fail_desc[:50]}")...')
            else:
                _sg = (f'g{max(int(g.get("generation") or 0) for g in seed_genomes)}'
                       if seed_genomes else '없음')
                _ss = (f'{max(s["_score"] for s in seeds):.3f}' if seeds else '-')
                self._log(round_num,
                          f'1) 8 Genome 알파 생성 중 (model={model_label}, '
                          f'seed {len(seed_genomes)}개 교차/변이 + 탐색 — '
                          f'최고세대 {_sg}, 최고점수 {_ss})...')

            # RC 는 결과 캐시를 우회하므로 결정론 유지 시 paused/error 라운드 재시도가
            # 동일 8개를 재시뮬·재제출한다 → round_id(시도마다 고유)를 salt 로 섞는다.
            # Non-RC 는 결정론 유지가 캐시 히트로 이득이라 salt=0.
            gen_salt = round_id if account_type == 'research_consultant' else 0

            # 정향변이 온라인 학습 관측 행렬 — focus 라운드에서만 필요하다
            # (탐색 라운드는 fail_items 가 없어 정향 경로를 타지 않는다).
            _dstats = None
            if LEARNED_DIRECTIVES and is_focus:
                try:
                    _dstats = _db.directive_stats(self.user_id)
                except Exception as _e:
                    self._log_quiet(round_num, f'⚠ directive_stats 조회 실패: {_e}')
                    _dstats = None

            strategies = genome_models.generate_population(
                account_type=account_type,
                round_num=(round_num * 1000) + (phase * 100) + int(parent_idx or 0),
                forced_delay=forced_delay,
                errors=errors,
                n=8,
                parent_genome=parent_genome,
                fail_items=(parent_fail_items if is_focus else None),
                parent_metrics=(parent_metrics if is_focus else None),
                seed_genomes=seed_genomes,
                slot_settings=(_slot_settings
                               if (_bandit_on and not is_focus and not is_spec_round)
                               else None),
                salt=gen_salt,
                parent_alpha_id=((focus_entry or {}).get('parent_alpha_id')
                                 if is_focus else None),
                seed_alpha_ids=seed_alpha_ids,
                directive_stats=_dstats,
                spec_genomes=[s['genome'] for s in pending_specs] or None,
                spec_ids=[s['id'] for s in pending_specs] or None,
            )
            if not strategies:
                raise RuntimeError('Genome generated no strategies')

            _origins = {'random': 0, 'mutate': 0, 'crossover': 0, 'spec': 0}
            for _s in strategies:
                _origins[_s.get('origin') or 'random'] = _origins.get(_s.get('origin') or 'random', 0) + 1
            self._log(round_num,
                      f'  세대 구성 — '
                      + (f'전략스펙 {_origins.get("spec", 0)} · ' if _origins.get('spec') else '')
                      + f'탐색 {_origins.get("random", 0)} · '
                      f'변이 {_origins.get("mutate", 0)} · 교차 {_origins.get("crossover", 0)}'
                      + (f' (밴딧 arm {min(len(_slot_settings), _origins.get("random", 0))}개 주입)'
                         if _slot_settings else ''))

            if is_focus:
                _dcomp: dict[str, int] = {}
                for _s in strategies:
                    _d = _s.get('directive')
                    if _d:
                        _dcomp[_d] = _dcomp.get(_d, 0) + 1
                if _dcomp:
                    _mode_tag = '학습가중 TS' if _dstats is not None else '규칙기반'
                    self._log(round_num,
                              '  정향변이 축 — '
                              + ' · '.join(f'{k} {v}' for k, v in sorted(_dcomp.items()))
                              + f' [{_mode_tag}]')

            # 사전 오류 방지 배선 — renderer 산출물에도 repair(오타/filter=)와 lint 를
            # 통과시킨다. RC 는 "무조건 제출 시도" 정책이라 lint 경고여도 드롭하지 않고,
            # Non-RC 만 확정 컴파일 에러 후보를 시뮬 전에 걸러 슬롯을 아낀다.
            _lint_kept: list[dict] = []
            for _s in strategies:
                try:
                    _fixed, _actions = _alpha_repair.repair(_s['code'], delay=forced_delay)
                    if _actions:
                        _s['code'] = _fixed
                        self._log_quiet(round_num,
                                        f'⚠ #{_s["idx"]} repair 적용: {",".join(_actions)}')
                    _issues = _alpha_lint.validate_alpha(_s['code'])
                except Exception:
                    _issues = []
                if _issues and account_type != 'research_consultant':
                    self._log(round_num,
                              f'  ⊘ #{_s["idx"]} lint 거부: {"; ".join(_issues)[:80]}')
                    continue
                if _issues:
                    self._log(round_num,
                              f'  ⚠ #{_s["idx"]} lint 경고 (RC 정책상 계속): '
                              f'{"; ".join(_issues)[:80]}')
                _lint_kept.append(_s)
            if _lint_kept:
                strategies = _lint_kept
            elif strategies:
                self._log(round_num, '  ⚠ lint 가 전부 거부 — 전량 통과(renderer 점검 필요)')

            # idx → 유전체 매핑 — focus 큐 상속과 세대(lineage) 기록에 사용.
            genome_by_idx: dict[int, dict] = {
                int(s['idx']): (s.get('genome') or {}) for s in strategies
            }
            # idx → 귀속 메타 — 어떤 부모(alphas.id)에 어떤 변이 축을 적용해 나온
            # 후보인지. 시뮬 결과 r 은 이걸 모르므로 생성 시점에 떠 둔다.
            meta_by_idx: dict[int, dict] = {
                int(s['idx']): {
                    'origin': s.get('origin'),
                    'directive': s.get('directive'),
                    'parent_alpha_id': s.get('parent_alpha_id'),
                    'genes_changed': s.get('genes_changed'),
                    'spec_id': s.get('spec_id'),
                } for s in strategies
            }

            if self._stop_event.is_set():
                _db.finish_round(round_id, self.user_id, round_num,
                                  status='paused', pass_count=0, err_count=0,
                                  cache_hits=0, summary='알파 생성 후 pause 요청')
                return

            # 사전 유사도 검사 — 이미 제출된 알파와 string/operator/field 가중평균 0.7 이상이면
            # WQB 가 self-correlation 으로 reject 할 가능성 높음. 시뮬 보내기 전 차단.
            # (출처: zhutoutoutousan/worldquant-miner template_similarity.py 포팅)
            if submitted_codes and account_type != 'research_consultant':
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
                                      summary='생성 알파 모두 기제출과 유사도 0.7+ → 다음 라운드')
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
            # RC는 "생성 후보를 API 백테스트 후 무조건 제출 시도" 정책이 우선이므로
            # 사전 드롭 게이트를 타지 않는다. Non-RC만 self-corr 절약 목적으로 사용한다.
            if account_type != 'research_consultant':
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
                # ⚠ 예전엔 RC 계정만 캐시를 통째로 건너뛰었다(브라우저 시대의 잔재 —
                #   스크레이핑 결과를 못 믿던 시절 규칙). 그 결과 2026-07-22 실측으로
                #   **시뮬의 21~23% 가 같은 코드 재실행**이었다(한 코드가 최대 13회).
                #   일일 5000건 쿼터를 그만큼 버린 것이라 RC 도 캐시를 쓴다. 키는
                #   code_hash + settings_fp 라 설정이 다르면 히트하지 않는다.
                cached = result_cache.lookup(self.user_id, s['code'], fp)
                if cached and str(cached.get('error_text') or '').strip():
                    # 에러 행은 캐시하지 않는다 — `sim TIMEOUT: poll deadline` 처럼
                    # **우리 쪽 사정**으로 실패한 행이 섞여 있어(2026-07-21 에러의 75%),
                    # 캐시로 굳히면 멀쩡한 알파가 영구히 죽은 것으로 남는다.
                    cached = None
                if cached:
                    cached_results.append(result_cache.materialize(s, cached, round_num))
                else:
                    to_simulate.append(s)
            cache_hit_total = len(cached_results)

            # 라운드의 시뮬 대상 알파를 WQB REST API 로 넘긴다 — ApiBackend 가 ThreadPool 로
            # 동시 실행하고(계정 tier 만큼), 결과를 idx 순서로 정렬해 돌려준다.
            all_results: list[dict] = list(cached_results)
            do_simulate = bool(to_simulate)

            if do_simulate and not self._stop_event.is_set():
                batch = to_simulate
                _sim_mode = 'WQB API 동시'
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
                    results = wqb_backend.simulate_batch(
                        batch,
                        wqb_username=username, wqb_password=password,
                        account_type=account_type, backend=backend,
                        stop_event=self._stop_event,
                        log_fn=None,  # [pw]/[playwright] 로그가 UI 로 흘러들어오지 않도록 끔
                        proc_holder=self._batch_proc_holder,
                        partial_fn=_on_partial,
                        forced_delay=forced_delay,
                        submit_gate=self._submit_gate,
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

                # WQB 세션 만료/biometric/2FA — 워커를 죽이지 않고 조용히 대기하다,
                # 사용자가 재인증하면 자동으로 이어서 재개한다(수동 '진화 실행' 재클릭 불필요).
                if not aborted and results and any(
                        _is_auth_required(r.get('error_text') or '') for r in results):
                    all_results.extend(results)
                    aborted = True
                    if not self._wait_for_reauth(username, password):
                        self._stop_event.set()   # 사용자가 대기 중 워커를 멈춤

                # setup 에러 전부면 (= 브라우저/로그인 자체가 깨짐) 1회 재시도 — subprocess 가
                # 새 브라우저를 다시 띄운다.
                if not aborted and results and all(
                        _is_setup_error(r.get('error_text') or '') for r in results):
                    self._log(round_num, '  ⚠ 시뮬 전체 setup 에러 — 브라우저 재시작 후 재시도')
                    try:
                        retry = wqb_backend.simulate_batch(
                            batch,
                            wqb_username=username, wqb_password=password,
                            account_type=account_type, backend=backend,
                            stop_event=self._stop_event,
                            log_fn=None,
                            proc_holder=self._batch_proc_holder,
                            partial_fn=_on_partial,
                            forced_delay=forced_delay,
                        submit_gate=self._submit_gate,
                        )
                        if not all(_is_setup_error(r.get('error_text') or '') for r in retry):
                            results = retry
                            self._log(round_num, '  ✓ 재시도 성공')
                        else:
                            self._log(round_num, '  ⚠ 재시도도 setup 에러 — 라운드 결과 그대로 진행')
                    except Exception:
                        pass

                # ── #4 가이드 리페어: 시뮬 실패 에러를 표적 수리해 라운드당 알파별 1회 재큐 ──
                #   재시뮬은 setup-error 재시도(위)와 동일 패턴. 무한루프 방지: 재큐 결과에
                #   _repaired=True 를 달아 다시 리페어하지 않는다(재큐 pass 는 1회).
                if not aborted and GUIDED_REPAIR and results and not self._stop_event.is_set():
                    try:
                        _pool = _repair_field_pool()
                        _repaired_batch = []
                        _repair_meta = {}   # idx -> (label, results_index)
                        for _ri, _r in enumerate(results):
                            _et = (_r.get('error_text') or '').strip()
                            if not _et or _r.get('_repaired'):
                                continue
                            if _is_setup_error(_et) or _is_auth_required(_et):
                                continue   # 인프라 에러는 리페어 대상 아님
                            _newcode, _label = _alpha_repair.repair_from_error(
                                _r.get('code', ''), _et, field_pool=_pool)
                            if _newcode and _newcode != _r.get('code'):
                                _idx = int(_r.get('idx') or 0)
                                _repaired_batch.append({
                                    'idx': _idx, 'code': _newcode,
                                    'desc': _r.get('desc', ''),
                                    'settings': settings_by_idx.get(_idx, {}),
                                })
                                _repair_meta[_idx] = (_label, _ri)
                                self._log(round_num, f'  🔧 #{_idx} 가이드 리페어 → {_label} · 재큐')
                        if _repaired_batch:
                            _retry = wqb_backend.simulate_batch(
                                _repaired_batch,
                                wqb_username=username, wqb_password=password,
                                account_type=account_type, backend=backend,
                                stop_event=self._stop_event,
                                log_fn=None,
                                proc_holder=self._batch_proc_holder,
                                partial_fn=None,   # 재큐 결과는 아래에서 직접 요약 로그
                                forced_delay=forced_delay,
                            submit_gate=self._submit_gate,
                            )
                            for _rr in _retry or []:
                                _rr['_repaired'] = True
                                _idx = int(_rr.get('idx') or 0)
                                if _idx not in _repair_meta:
                                    continue
                                _label, _ri = _repair_meta[_idx]
                                _rr['repair_label'] = _label
                                results[_ri] = _rr   # 원 실패 결과를 리페어 결과로 교체
                                _ok = not (_rr.get('error_text') or '').strip()
                                self._log(round_num,
                                          f'  🔧 #{_idx} 리페어 재시뮬 {"성공" if _ok else "여전히 실패"}')
                            self._repaired_total = getattr(self, '_repaired_total', 0) + len(_repaired_batch)
                    except Exception as _e:
                        self._log_quiet(round_num, f'⚠ 가이드 리페어 예외(무시): {_e}')

                if not aborted:
                    # 라운드 시뮬 결과 — 한 줄 요약. 알파별 줄은 partial_fn 스트림이 이미 송출함.
                    # partial 미수신된 알파 (예: subprocess KILL) 만 여기서 보충.
                    r_pass = sum(1 for r in results if _is_best_alpha(r))
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
            alpha_id_by_idx: dict[int, int] = {}   # idx → 방금 저장한 alphas.id
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
                    # 세대(lineage)는 시뮬 결과가 아니라 생성 시 유전체가 안다.
                    'generation': int((genome_by_idx.get(int(r['idx'])) or {})
                                      .get('generation') or 0),
                    # 귀속(v6) — 부모 알파 id·변이 축·바뀐 유전자. 시뮬 결과 r 이
                    # 아니라 생성 계획(meta_by_idx)이 안다.
                    'parent_alpha_id': (meta_by_idx.get(int(r['idx'])) or {})
                                       .get('parent_alpha_id'),
                    'origin': (meta_by_idx.get(int(r['idx'])) or {}).get('origin'),
                    'directive': (meta_by_idx.get(int(r['idx'])) or {}).get('directive'),
                    'genes_changed': (meta_by_idx.get(int(r['idx'])) or {})
                                     .get('genes_changed'),
                    # 이 알파를 낳은 LLM 전략스펙 (NULL = 순수 GA 산).
                    'spec_id': (meta_by_idx.get(int(r['idx'])) or {}).get('spec_id'),
                    # 유전체 원본을 그대로 영속화 — 다음 라운드가 코드에서 역추출하지 않고
                    # 이걸 읽는다. 이게 세대를 잇는 유일한 통로다.
                    'genome': genome_by_idx.get(int(r['idx'])),
                }
                alpha_id_by_idx[int(r['idx'])] = _db.insert_alpha(
                    self.user_id, round_id, round_num, alpha_entry)
                _sid = (meta_by_idx.get(int(r['idx'])) or {}).get('spec_id')
                if _sid:
                    try:
                        _db.attach_spec_alpha(_sid, alpha_id_by_idx[int(r['idx'])])
                    except Exception as _e:
                        self._log_quiet(round_num, f'⚠ spec-alpha 연결 실패: {_e}')

                # 밴딧 보상 업데이트 — 비-focus 라운드, 밴딧 ON 시에만. per-alpha flush.
                if _bandit_on and not is_focus:
                    try:
                        from . import reward as _reward, bandit as _bandit
                        _m = dict(r.get('metrics') or {})
                        # bandit_reward = 게이트 보상 + 소량의 연속 점수 — 제출 가능
                        # 알파가 없는 구간에서도 arm 간 구분 신호가 흐른다(희소성 완화).
                        _rwd = _reward.bandit_reward(
                            _m,
                            pass_count=int(r.get('pass_count') or 0),
                            fail_count=int(r.get('fail_count') or 0),
                            error_count=int(r.get('error_count') or 0),
                            self_corr=r.get('self_corr'))
                        _set = settings_by_idx.get(int(r['idx']), {}) or {}
                        _gn = genome_by_idx.get(int(r['idx'])) or {}
                        _assign = {
                            'universe': (_set.get('universe') or 'TOP3000'),
                            'neutralization': (_set.get('neutralization') or 'INDUSTRY'),
                            'decay_bucket': _bandit.decay_to_bucket(_set.get('decay', 0)),
                            # 구조 유전자 차원 — 실제 발현된 유전체 값으로 크레딧.
                            'family': _gn.get('family'),
                            'combine': _gn.get('combine'),
                        }
                        for _ak in _bandit.arm_keys_for_assignment(_assign):
                            _dim = _ak.split(':', 1)[0]
                            _db.bandit_update(self.user_id, _ak, _rwd, round_num,
                                              dimension=_dim, decay_k=BANDIT_DECAY_K)
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

            # 자율 이데이션 — GA 는 국소 탐색이라 시드 풀에 없는 구조는 영원히 못 만든다.
            # 주기적으로 로컬 LLM 에게 'GA 의 현재 상태'를 보여 주고 새 구조를 청한다.
            # 백그라운드 스레드라 라운드를 막지 않고, 실패해도 GA 는 그대로 돈다.
            # (focus 라운드는 부모 연마 중이라 건너뛴다 — 탐색 라운드에서만.)
            if status == 'done' and not is_focus and not is_spec_round:
                try:
                    from . import auto_ideation
                    if auto_ideation.should_run(self.user_id, round_num):
                        auto_ideation.start(self.user_id, round_num)
                except Exception as e:
                    self._log_quiet(round_num, f'⚠ 자율 이데이션 기동 실패(무시): {e}')
                # 전략 위원회 (2026-07-23) — 다중 LLM 에이전트가 밴딧 통계·구역 실측·
                # 거절 사유를 읽고 다음 라운드들의 슬롯 정책과 교차쌍을 정한다.
                # 백그라운드라 라운드를 막지 않고, 실패해도 밴딧이 평소대로 돈다.
                try:
                    from . import committee as _committee
                    if _committee.should_run(self.user_id, round_num):
                        _committee.start(self.user_id, round_num)
                except Exception as e:
                    self._log_quiet(round_num, f'⚠ 위원회 기동 실패(무시): {e}')

            # 전략스펙 소진 처리 — 완료된 라운드에서만. (paused 면 pending 으로 남겨
            # 재개 후 다시 시도한다.) dedup 으로 슬롯에 못 들어간 스펙 = 이미 시뮬한
            # 조합이므로 'exhausted' — 그대로 두면 매 라운드 같은 스펙을 재시도한다.
            if is_spec_round and status == 'done':
                try:
                    _used = {m.get('spec_id') for m in meta_by_idx.values()
                             if m.get('spec_id')}
                    _all = {int(s['id']) for s in pending_specs}
                    if _used:
                        _db.mark_specs(sorted(_used), 'seeded', seeded_round=round_num)
                    _dropped = _all - _used
                    if _dropped:
                        _db.mark_specs(sorted(_dropped), 'exhausted',
                                       seeded_round=round_num)
                        self._log(round_num,
                                  f'  스펙 {len(_dropped)}개는 이미 시뮬한 조합이라 소진 처리')
                    _left = len(_db.pending_specs(self.user_id, limit=99))
                    self._log(round_num,
                              f'  🧪 전략스펙 {len(_used)}개 시뮬 완료 — '
                              + (f'남은 스펙 {_left}개' if _left
                                 else '전부 소진, 다음 라운드부터 GA 가 이어받습니다'),
                              level='pass')
                except Exception as e:
                    self._log_quiet(round_num, f'⚠ 스펙 상태 갱신 실패: {e}')

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
                # 메인 라운드 종료 — 적합도가 FOCUS_MIN_SCORE 이상이면서 미통과 항목
                # (FAIL/ERROR)이 남은 알파마다 FAIL 사유를 넣어 3 라운드(phase 1·2·3)씩
                # 개선 변형을 큐에 추가한다. (focus 라운드에서는 enqueue 하지 않는다 —
                # 위 elif 분기가 먼저 잡으므로 큐가 무한히 자라지 않는다.)
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

                # 라운드당 focus 후보 폭주 방지 — 적합도 상위 N개만. closeness 는 이미
                # 위에서 하한선(far-miss 차단)으로 썼고, 정렬은 연속 적합도로 한다.
                # (closeness 로 정렬하면 '컷오프에 가깝지만 신호가 없는' 알파가 이긴다.)
                if len(focus_candidates) > FOCUS_MAX_PER_ROUND:
                    _dropped = len(focus_candidates) - FOCUS_MAX_PER_ROUND
                    try:
                        focus_candidates.sort(key=focus_score, reverse=True)
                    except Exception:
                        pass
                    focus_candidates = focus_candidates[:FOCUS_MAX_PER_ROUND]
                    self._log(round_num,
                              f'  focus 후보 {_dropped}개 보류 — 라운드당 상위 '
                              f'{FOCUS_MAX_PER_ROUND}개만 연마(예산 보호)')

                # PROD/SELF_CORRELATION 거절 부모 (2026-07-23) — 체크는 전부 통과했는데
                # 상관 벽에서만 죽은 알파. 신호 자체는 검증된 것이므로 버리지 않고
                # **탈상관 방향**(중립화 교체·resid·필드 교체)으로 연마 큐에 넣는다.
                # fail_desc 의 'correlation' 을 directed_mutation 이 인식해 그쪽 축을
                # 고른다. _classify_focus 는 FAIL 0 이라 이들을 못 잡는다 — 별도 수집.
                _corr_parents = [
                    r for r in all_results
                    if not r.get('cached')
                    and str(r.get('submit_status') or '').startswith('rejected:')
                    and 'CORRELATION' in str(r.get('submit_status') or '').upper()
                ][:1]                       # 라운드당 1개 — 예산 보호
                for _cp in _corr_parents:
                    if any(x is _cp for x in focus_candidates):
                        continue
                    focus_candidates.append(_cp)
                    self._log(round_num,
                              f"  🧭 상관 거절 부모 #{_cp.get('idx')} — 탈상관 연마 "
                              f"대상으로 추가 ({str(_cp.get('submit_status'))[:60]})")

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
                        if not fd:
                            # 상관 거절 부모 — IS 체크는 전부 PASS 라 fail 항목이 없다.
                            # 거절 사유를 실어야 directed_mutation 이 탈상관 축을 고른다.
                            _ss = str(a.get('submit_status') or '')
                            if 'CORRELATION' in _ss.upper():
                                fd = (_ss.split(':', 1)[-1].strip()[:200]
                                      or 'PROD_CORRELATION')
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
                                # 부모의 실측 지표 — 고회전(HTVR) 관문 미달은 FAIL 이
                                # 아니라 WARNING 이라 fail_items 에 안 나타난다. 정향변이가
                                # 'churn' 축을 고르려면 이 지표가 있어야 한다.
                                'parent_metrics': dict(a.get('metrics') or {}),
                                'focus_kind': 'fail',
                                'self_corr_value': '',
                                # 부모 settings 를 실어 보내 다음 focus 라운드의 정향변이가
                                # 부모의 smoothing(decay 등)을 계승하게 한다.
                                'parent_settings': settings_by_idx.get(int(a.get('idx') or 0), {}),
                                # 정확한 부모 유전체 — focus 라운드 정향변이의 출발점.
                                'parent_genome': genome_by_idx.get(int(a.get('idx') or 0)),
                                # 유전체가 없는 레거시 폴백 경로가 세대를 잇도록.
                                'parent_generation': int(
                                    (genome_by_idx.get(int(a.get('idx') or 0)) or {})
                                    .get('generation') or 0),
                                # 부모의 alphas.id — focus 자식의 귀속 엣지
                                # (parent→directive→child→Δ지표) 를 잇는 열쇠.
                                'parent_alpha_id': alpha_id_by_idx.get(
                                    int(a.get('idx') or 0)),
                            })
                    _db.set_focus_queue(self.user_id, new_q)
                    _best = max((focus_score(a) for a in focus_candidates), default=0.0)
                    self._log(round_num,
                              f'  🎯 focus 후보 {len(focus_candidates)}개 '
                              f'(적합도≥{FOCUS_MIN_SCORE}, 최고 {_best:.3f}) — '
                              f'각 {PHASES_PER_PARENT} 라운드씩 FAIL 개선 변형 생성 예정 '
                              f'(총 {len(focus_candidates) * PHASES_PER_PARENT} sub-round)',
                              level='pass')
        except Exception as e:
            self._log(round_num, f'⚠ 라운드 예외: {e}')
            _db.finish_round(round_id, self.user_id, round_num,
                              status='error', pass_count=pass_total,
                              err_count=err_total, cache_hits=cache_hit_total,
                              summary=f'예외: {str(e)[:300]}')

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
        # 로컬 LLM에는 prompt cache가 없으므로 이 메시지는 정상 폴백이다.
        if 'prompt cache 실패' in s and 'no prompt cache' in s:
            LOG.info('[round %d] %s', round_num, s[:300])
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


def _check_counts(r: dict) -> tuple[int, int, int]:
    """(pass, fail, error) — IS Testing Status 가 있으면 그쪽이 권위, 없으면 스칼라 필드."""
    p_n, f_n, e_n, pn_n = _is_counts(r)
    if (p_n + f_n + e_n + pn_n) > 0:
        return (p_n, f_n, e_n)
    return (int(r.get('pass_count') or 0),
            int(r.get('fail_count') or 0),
            int(r.get('error_count') or 0))


def focus_score(r: dict) -> float:
    """알파 한 개의 연속 적합도 — focus 후보 선정·정렬의 단일 기준. 절대 예외를 던지지 않는다."""
    pc, fc, ec = _check_counts(r)
    try:
        from . import reward as _reward
        return _reward.selection_score(
            r.get('metrics') or {}, pass_count=pc, fail_count=fc,
            error_count=ec, self_corr=r.get('self_corr'))
    except Exception:
        return 0.0


def _classify_focus(r: dict) -> tuple[str | None, str]:
    """focus 큐 후보 분류 → (kind|None, self_corr_value).

    아직 통과 못한 항목(FAIL/ERROR)이 남아 있고, 연마할 가치가 있을 만큼
    적합도(focus_score)가 높으면 'fail' 개선 대상이다.
    (self-correlation 은 별표 단계에서 Correlation 상자의 Maximum 으로 직접 확인하므로
     제출-거절 기반 'correlation' kind 는 더 이상 사용하지 않는다.)"""
    if r.get('cached'):
        return (None, '')
    _pc, fc, ec = _check_counts(r)
    if (fc + ec) < 1:
        return (None, '')          # 이미 전부 통과 — 연마할 항목이 없다
    if focus_score(r) < FOCUS_MIN_SCORE:
        return (None, '')
    # Sharpe 절대 하한 — 신호가 없는 부모는 연마해도 오버피팅만 나온다 (강의의 시드 원칙).
    _sh = _criteria._f((r.get('metrics') or {}).get('sharpe'))
    if _sh is None or abs(_sh) < FOCUS_MIN_SHARPE:
        return (None, '')
    return ('fail', '')


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

    # 별표(저장, Non-RC) / 제출(RC) 상태 + self-correlation 값 표기.
    star_note = ''
    if sub_st.startswith('starred'):
        star_note = f'  ⭐ 별표 저장 ({sub_st[len("starred"):].strip().strip("()") or "self-corr ?"})'
    elif sub_st.startswith('skip_star'):
        star_note = f'  ☆ 미저장 — {sub_st[len("skip_star:"):].strip()}'
    elif sub_st.startswith('star_fail'):
        star_note = f'  ⚠ 리스트 제출 실패 ({sub_st[len("star_fail"):].strip().strip("()")})'
    elif sub_st == 'submitted':                      # RC 공식 API 제출 성공
        star_note = '  🚀 제출 성공'
    elif sub_st.startswith('submit_http_429'):
        star_note = '  ⏳ 제출 대기열 초과(429) — deadline 내 슬롯 미확보'
    elif sub_st.startswith('submit_pending_timeout'):
        star_note = '  ⏳ 제출 결과 확인 시간 초과 (WQB 쪽에서 계속 진행 중일 수 있음)'
    elif sub_st.startswith('submit_skipped'):
        star_note = '  ⏸ 제출 생략 (pause)'
    elif sub_st.startswith('submit_http_'):
        star_note = f'  ⚠ 제출 HTTP 오류 ({sub_st[len("submit_http_"):][:40]})'
    elif sub_st.startswith('submit_error'):
        star_note = f'  ⚠ 제출 오류 ({sub_st.split(":", 1)[-1].strip()[:40]})'
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
    from . import wqb_backend as _wqb
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
        # RC(공식 API) 세션 만료/biometric — 재인증(대시보드) 필요. 워커가 멈춰야
        # /authentication 을 연타(5/min 429 폭주)하며 Gemini 를 낭비하지 않는다.
        # ⚠ 'concurrent_simulation_limit'(슬롯 429)는 인증문제가 아니므로 포함하지 않는다.
        'biometric', 'persona', 'rc 자격증명',
    )
    return any(s in t for s in sigs)
