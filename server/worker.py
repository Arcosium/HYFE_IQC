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
import re
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
# WQB 하드캡과 동일한 4건 (2026-07-27 사장 지시로 3→4 환원). 초과분은
# submit_queue(kind=budget)로 넘겨 다음 날 자동 드레인한다.
# 리셋 경계는 **UTC 자정 = KST 09:00** (db.day_start_ts).
DAILY_SUBMIT_BUDGET = int(os.environ.get('IQC_DAILY_SUBMIT_BUDGET', '4'))
# 대기 큐 제출 확인 주기 — 제출은 라운드와 **별개 경로**다(전용 티커 스레드).
# 예산 리셋(미 동부 자정 = KST 13:00) 직후에 바로 나가야 하므로 촘촘히 본다.
# 큐가 비어 있으면 인덱스 조회 한 번(수 μs)으로 끝나 비용이 사실상 없다.
_DRAIN_TICK_S = float(os.environ.get('IQC_DRAIN_TICK_S', '60'))

# ── 결정론 레이어 (2026-07-26, WQB AAF·smilee 이식) — LLM 비용 0 ─────────────
# ① 재조합(combine_layer): 탐색 라운드에서 검증된 IS 알파 둘을 결합해 후보 추가.
# ② 개선(improve_layer): focus 라운드 phase 1 에서 부모의 회전율 등급에 맞는
#    lookback 재스케일·trade_when·decay 변형 추가. 0 = 해당 레이어 OFF.
COMBINE_LAYER_N = int(os.environ.get('IQC_COMBINE_PER_ROUND', '2'))
IMPROVE_LAYER_N = int(os.environ.get('IQC_IMPROVE_PER_FOCUS', '3'))
# ♻ 신규성 압력 (2026-07-26 라이브 진단): crossover 의 92%·sweep 75% 가 이미 시뮬한
# (code,settings) 재생산이었다 — 캐시는 쿼터만 아끼고 라운드 슬롯은 낭비한다.
# 캐시 히트 예정 후보를 근처 신규 변형(alpha_mutate)으로 교체해 슬롯당 학습량을 살린다.
NOVELTY_REWRITE = os.environ.get('IQC_NOVELTY_REWRITE', '1') != '0'
# 🚑 HT 구제 (2026-07-26 라이브 진단): Sharpe>=1.58 인데 fitness<1.0·turnover>0.4
# 로 죽은 알파가 24h 에 59건 — focus(라운드당 1개)가 못 캐는 광맥. 탐색 라운드마다
# 이 풀에서 부모 1개를 뽑아 회전 절감 변형을 N개 주입한다. 0 = OFF.
HT_RESCUE_PER_ROUND = int(os.environ.get('IQC_HT_RESCUE_PER_ROUND', '2'))
# 🧭 사냥 사다리 (2026-07-27 GLB 사냥 판단과정 이식) — 직전 라운드에서 |Sharpe| 가
# 충분히 큰데 부호·회전율·Fitness 로만 막힌 알파에 표준 처방(부호반전·사후감쇠·RAM
# 중립화)을 즉시 건다. 그날 제출권에 든 유일한 알파가 이 처방에서 나왔다. 0 = OFF.
HUNT_LADDER_PER_ROUND = int(os.environ.get('IQC_HUNT_LADDER_PER_ROUND', '3'))
# 라운드당 유전체 후보 수. **동시 슬롯 수보다 많아야** 빈 슬롯이 안 생긴다
# (2026-07-27 사장 지시). 스레드 풀은 max_workers=슬롯수 로 돌기 때문에, 후보가 슬롯과
# 같으면(옛 n=8) 시뮬 하나가 끝나도 집어 갈 다음 후보가 없어 그 슬롯이 라운드 끝까지
# 논다 — sim 이 ~20분이라 이 낭비가 크다. 후보를 더 주면 끝나는 즉시 다음 것이 들어간다.
ALPHAS_PER_ROUND = int(os.environ.get('IQC_ALPHAS_PER_ROUND', '14'))
# ⚠ 외부 예시 알파를 시드로 주입하는 레이어를 만들었다가 걷어냈다 (2026-07-27 사장 판단).
# 남의 식은 남들도 쓴다 — zscore(rsk70_..._anlystsn) 이 실제로 PROD_CORRELATION 으로
# 거절당했고, 슬롯을 남의 식으로 채우면 그만큼 탐색이 좁아진다. 숨은 필드 발견이라는
# 진짜 가치는 팔레트 알파벳 캡 수정이 이미 대신한다(GLB 가시 필드 10000 → 29343).
# Power Pool self-correlation **예방적** 상한 — 기본 0 = 끔 (2026-07-27 사장 결정).
# 배경: 형제 알파는 PP 적격 컷(self-corr<0.5)에 걸릴 위험이 있다. 하지만 **거절은
#   일일 예산을 소모하지 않으므로 시도 자체는 공짜**고, 통과하면 제출 수가 늘어난다.
#   그래서 미리 막지 않고 **일단 보낸다**. WQB 가 실제로 CORRELATION 으로 거절하면
#   그때 _submit_gate 의 family_corr_wall 이 같은 필드셋 형제를 24h 잠근다
#   (= 헛발질 반복은 막되, 첫 시도는 항상 해 본다).
#   0 보다 큰 값을 넣으면 다시 예방적 차단이 켜진다.
PP_SELFCORR_MAX = float(os.environ.get('IQC_PP_SELFCORR_MAX', '0'))

# ③ Yield Score (ACE) — arm 배분 점수에 '시뮬 1건당 게이트 통과율'을 섞는 비중.
#   score = mean + w·(pass_sum+1)/(visits+2)  (라플라스 스무딩 — 냉시작 arm 은
#   중립 0.5 근처에서 출발). 0 = 순수 mean(기존 동작).
YIELD_WEIGHT = float(os.environ.get('IQC_YIELD_WEIGHT', '0.15'))

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


def _theme_retry_worthwhile(metrics) -> bool:
    """테마 미충족으로 거절된 알파를 '다음 테마 주간에 다시 낼 만한가'.

    테마가 바뀌어도 알파 자체가 컷을 못 넘으면 그때도 떨어진다. criteria.submittability
    는 표준 경로와 고회전(HT) 경로 중 **가까운 쪽**을 재므로, 그 값이 1.0(둘 중 하나는
    충족)일 때만 보관한다.

    2026-07-27 실측 근거: 보관돼 있던 34건 중 Fitness>=1.0 인 것은 4건뿐이었고
    나머지는 테마와 무관하게 떨어질 것들이었다 — 목록만 어지럽혔다.
    """
    try:
        return _criteria.submittability(metrics or {},
                                        delay=_criteria.delay_of(metrics or {})) >= 1.0
    except Exception:
        return True          # 판정 불가면 보관한다 — 잘못 버리는 쪽이 더 나쁘다


def _stamp_region(strategies, region: str | None) -> int:
    """라운드 탐색 조건의 리전을 후보 settings 에 채운다(비어 있을 때만). 채운 개수 반환.

    ⚠ 2026-07-28 실측 버그. GA 후보는 region 을 실어 오지만 **레이어 주입 후보**
    (재조합·사냥사다리·HT구제·개선)는 부모 알파의 universe/neutralization/decay/
    truncation 만 물려받고 region 을 안 실었다. 그러면 wqb_api._full_settings 의
    기본값 'USA' 로 떨어져, GLB 유니버스에 USA 가 붙는다:

        400 {"settings":{"universe":["Universe TOPDIV3000 is not available
                                      for instrument type EQUITY and region USA."]}}

    한 라운드에서 4개가 이렇게 조용히 죽었다(응답 본문을 안 읽어 '제출 응답 없음'
    으로만 보였다). 레이어마다 고치면 다음에 새 레이어가 또 빠뜨리므로, 후보가
    전부 지나는 길목에서 한 번 채운다 — 캐시 지문(settings_fingerprint)을 만들기
    **전에** 불러야 지문과 실제 제출 설정이 어긋나지 않는다.
    """
    if not region:
        return 0
    n = 0
    for s in strategies:
        st = s.setdefault('settings', {})
        if not st.get('region'):
            st['region'] = region
            n += 1
    return n


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


def shutdown_all() -> int:
    """살아 있는 워커 전부에 종료 요청 + 진행 중 시뮬 취소. 취소한 시뮬 수 반환."""
    with _REGISTRY_LOCK:
        workers = list(_REGISTRY.values())
    for w in workers:
        try:
            w.request_shutdown()
        except Exception:
            pass
    return wqb_backend.cancel_all_inflight()


class Worker(threading.Thread):
    """user_id 별 IQC 라운드 무한 실행."""

    def __init__(self, user_id: int):
        super().__init__(daemon=True, name=f'hyfe-worker-{user_id}')
        self.user_id = user_id
        self._stop_event = threading.Event()         # pause/stop 신호
        self._batch_proc_holder: dict[str, Any] = {} # 현재 배치 subprocess 보관
        self._lock = threading.Lock()
        # ④ 패밀리 상관벽 (2026-07-26, AAF 패밀리 트리 이식) — 이번 세션에서
        # CORRELATION 거절을 맞은 필드셋. DB 는 라운드 끝에야 기록되므로, 같은
        # 라운드 안의 형제가 곧바로 같은 벽에 돌진하는 것은 이 메모리 셋이 막는다.
        self._corr_fs_hold: set[frozenset] = set()
        # 차단 FAIL 이 0 인 알파는 그 자리에서 제출을 시도한다 — 단, 일일 예산 안에서만
        # (_submit_gate 참조).

    # ── 일일 제출 예산 ────────────────────────────────────────────
    def _submit_gate(self, metrics: dict, self_corr=None, fail_items=None,
                     genome=None, code=None) -> tuple[bool, str]:
        """제출할까? (ok, 사유) — wqb_backend 가 **제출 락 안에서** 호출한다.

        WQB 컨설턴트의 제출 한도는 **하루 4건**이다("Max 4 alpha submissions in a day",
        Power Pool 문서).

        **낼 수 있으면 낸다** (2026-07-28 사장 지시). 두 겹만 거른다:
          0) 차단 FAIL: `criteria.is_blocking` FAIL 이 하나라도 있으면 **WQB 가 반드시
             403 으로 거절**한다 — 보낼 이유가 없다
          1) 예산: 오늘 성공 제출이 DAILY_SUBMIT_BUDGET 이상이면 대기 큐로
        예전엔 여기에 품질 문턱(below_value)이 한 겹 더 있었다. 4칸을 아껴 뒀다가 더
        좋은 알파에 쓰자는 것이었는데, 실측이 정반대였다 — 제출 실적 1·1·2·4·2 건으로
        대개 4칸을 못 채우면서 같은 기간 330건을 문턱으로 걸렀다. 예산은 이월되지
        않으니 아끼는 게 곧 버리는 것이었다.
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
        # 같은 식이 **최근** 거절당했으면 또 보내지 않는다. 후보 생성이 결정론이라
        # 재시작·재방문 때 같은 식이 다시 만들어져, 이 검사가 없으면 계속 재제출한다
        # (2026-07-28: 1YzG86aM 이 14분 간격으로 같은 FAIL 5개로 재거절).
        # IS 판정엔 안 걸리고 제출 시점 판정에만 걸리는 알파라 위 blocking 검사로는 못 막는다.
        # ⚠ 영구 차단이 아니다 — Power Pool·테마 조건이 바뀌면 통과할 수 있으므로
        #   db.REJECT_MEMORY_S(기본 24h) 가 지나면 다시 시도한다(사장 지적).
        try:
            prior = _db.code_rejected_before(self.user_id, str(code or ''))
        except Exception:
            prior = None
        if prior:
            return False, f'already_rejected({prior[len("rejected:"):][:60]})'
        # 📋 제출 모드 = 'list' — 자동 제출하지 않고 대기 목록에만 쌓는다(사용자 선택).
        #    차단 FAIL 검사 **뒤**에 둔다: WQB 가 어차피 거절할 알파로 목록을 채우면
        #    사람이 골라야 할 것이 묻힌다. kind='manual' 은 큐 드레인이 건드리지 않는다.
        try:
            if _db.get_submit_mode(self.user_id) == 'list':
                wid = str((metrics or {}).get('wqb_alpha_id') or '')
                if wid:
                    try:
                        _db.submit_queue_add(
                            self.user_id, wqb_alpha_id=wid, kind='manual',
                            note='제출 모드=목록 — 대시보드에서 직접 제출',
                            metrics=dict(metrics or {}))
                    except Exception as e:
                        LOG.warning('submit_queue 추가 실패(무시): %s', e)
                return False, 'submit_mode=list→queued'
        except Exception as e:
            LOG.warning('제출 모드 조회 실패 (자동 제출로 진행): %s', e)
        # 필드셋 쿨다운 (2026-07-24) — 같은 필드 조합이 최근 24h 에 3회+ 거절됐으면
        # 제출을 보류한다. 상관(PROD/PP)은 아이디어=필드 수준 속성이라 중립화·감쇠만
        # 바꾼 형제는 같은 벽에 부딪힌다(실측: 같은 mdl177 3종 변형 19연속 거절).
        # 시뮬·학습은 그대로 — **제출 API 만** 아낀다.
        if genome:
            try:
                fs = frozenset(str(f) for f in (dict(genome).get('fields') or []) if f)
                if fs and fs in set(_db.rejected_fieldsets(self.user_id)):
                    return False, 'fieldset_cooldown(24h)'
                # ④ 패밀리 상관벽 — 상관은 필드셋(아이디어) 수준 속성이라 대표 1회의
                # CORRELATION 거절이면 같은 필드셋 형제 전원을 24h 보류한다(AAF 패밀리
                # 트리의 '대표만 검사' 경제화). 탈상관 focus 자식은 필드를 바꾸므로
                # 다른 필드셋 = 통과. 시뮬·학습은 그대로, **제출 API 만** 아낀다.
                if fs and (fs in self._corr_fs_hold
                           or fs in set(_db.rejected_fieldsets(
                               self.user_id, min_count=1,
                               reason_contains='CORRELATION'))):
                    return False, 'family_corr_wall(24h)'
                # PURE_POWER_POOL_THEME 도 필드셋 수준 속성이다 — 순수 PP 테마
                # 데이터셋 조합은 품질과 무관하게 거절된다(2026-07-26 실측 3건).
                # 같은 필드셋 형제의 제출 시도는 제출 락만 낭비하므로 24h 보류.
                if fs and fs in set(_db.rejected_fieldsets(
                        self.user_id, min_count=1,
                        reason_contains='PURE_POWER_POOL')):
                    return False, 'family_pure_theme_wall(24h)'
                # ④ 일일 패밀리 dedup — 오늘 이미 같은 필드셋을 성공 제출했으면 예산
                # 4칸을 형제가 잠식하지 않게 보류(내일 다시 가능).
                if fs and fs in set(_db.submitted_fieldsets_today(self.user_id)):
                    return False, 'family_dup_today'
            except Exception as e:
                LOG.warning('fieldset cooldown 조회 실패 (제출 계속): %s', e)
        # ⏳ 제출 보류창 (2026-07-27 사장 지시) — 테마 경계(KST 09:00)와 예산
        # 리셋(KST 13:00)이 다른 시계라, 그 사이에 새 테마 알파를 내면 **전날
        # 예산**을 쓴다. 예산이 열릴 때까지 큐에 재워 하루치를 온전히 쓴다.
        try:
            _hold = run_config.get_submit_hold_until()
        except Exception:
            _hold = 0.0
        if _hold and time.time() < _hold:
            wid = str((metrics or {}).get('wqb_alpha_id') or '')
            if wid:
                try:
                    _db.submit_queue_add(
                        self.user_id, wqb_alpha_id=wid, kind='budget',
                        note='예산 리셋 대기 — 보류창 해제 후 자동 제출',
                        metrics=dict(metrics or {}))
                except Exception as e:
                    LOG.warning('submit_queue 추가 실패(무시): %s', e)
            import datetime as _dtm
            _hh = _dtm.datetime.fromtimestamp(_hold).strftime('%H:%M')
            return False, f'submit_hold(~{_hh})→queued'
        # Power Pool 상관 컷 — 테마에 매칭된 알파만 대상(일반 제출은 0.7 컷이 따로 있다).
        # 형제 알파 줄세우기를 막아 상관 풀 오염과 예산 낭비를 동시에 방지한다.
        if PP_SELFCORR_MAX > 0 and str((metrics or {}).get('themes') or '').strip():
            _sc = self_corr if self_corr is not None else (metrics or {}).get('self_correlation')
            try:
                _scv = float(str(_sc)) if _sc not in (None, '') else None
            except (TypeError, ValueError):
                _scv = None
            if _scv is not None and _scv > PP_SELFCORR_MAX:
                return False, f'pp_selfcorr({_scv:.2f}>{PP_SELFCORR_MAX:.2f})'
        if DAILY_SUBMIT_BUDGET <= 0:
            return True, ''
        try:
            used = self._submitted_today()
        except Exception as e:
            LOG.warning('submitted_today 조회 실패 (제출 강행): %s', e)
            return True, ''
        if used >= DAILY_SUBMIT_BUDGET:
            # 예산 초과분은 버리지 않고 대기 큐에 — 다음 날 _drain_submit_queue 가
            # 자동 재시도한다 (2026-07-27 사장 지시).
            wid = str((metrics or {}).get('wqb_alpha_id') or '')
            queued = False
            if wid:
                try:
                    queued = bool(_db.submit_queue_add(
                        self.user_id, wqb_alpha_id=wid, kind='budget',
                        note=f'일일 예산 초과({used}/{DAILY_SUBMIT_BUDGET}) — 익일 자동 재시도',
                        metrics=dict(metrics or {})))
                    if queued:
                        LOG.info('제출 대기 큐 추가(budget): %s', wid)
                except Exception as e:
                    LOG.warning('submit_queue 추가 실패(무시): %s', e)
            else:
                # 큐는 wqb_alpha_id 로 재제출한다 — id 가 없으면 넣어도 못 쓴다.
                LOG.warning('예산 초과인데 wqb_alpha_id 가 없어 대기 큐에 못 넣음')
            # 넣지도 못했으면서 '→queued' 라고 적지 않는다 — 라이브 피드가 거짓말한다.
            tail = '→queued' if queued else '→미보관'
            return False, f'daily_budget({used}/{DAILY_SUBMIT_BUDGET}){tail}'
        # 품질 문턱(below_value)은 2026-07-28 사장 지시로 제거했다. **낼 수 있으면 낸다.**
        # 하루 4칸을 아끼자는 장치였는데 실측이 정반대였다 — 제출 실적이 1·1·2·4·2 건으로
        # 대개 4칸을 못 채우면서 같은 기간 330건을 문턱으로 걸렀다. 안 쓴 예산은 이월되지
        # 않고 사라지니, 아끼는 게 곧 버리는 것이었다. 한도를 채우면 위에서 대기 큐로 간다.
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

    def _account_datasets(self, account_type: str, constraint,
                          username: str, password: str):
        """일반 계정이 접근 가능한 dataset.id 집합 (RC 는 None = 제한 없음).

        하루 1회만 묻는다 — 계정 권한은 그보다 자주 안 바뀌고, /data-sets 페이지네이션이
        공짜가 아니다. 조회 실패 시 **직전 값을 유지**한다(빈 집합을 걸면 팔레트가
        통째로 비어 라운드가 죽는다).
        """
        if account_type == 'research_consultant':
            return None
        cached = getattr(self, '_acct_ds_cache', None)
        if cached and time.time() - cached[0] < 24 * 3600:
            return cached[1]
        region = (getattr(constraint, 'region', None) or 'USA')
        universe = (getattr(constraint, 'universe', None) or 'TOP3000')
        delay = getattr(constraint, 'delay', None)
        try:
            from . import wqb_api as _wqb_api
            client = _wqb_api.WqbApiClient(username, password)
            ids = client.accessible_datasets(
                region, universe, int(delay) if delay is not None else 1)
        except Exception as e:
            self._log_quiet(0, f'⚠ 계정 데이터셋 조회 실패(직전 값 유지): {e}')
            return cached[1] if cached else None
        if not ids:
            return cached[1] if cached else None
        self._acct_ds_cache = (time.time(), ids)
        self._log(0, f'🔐 계정 접근 가능 데이터셋 {len(ids)}종 — 팔레트를 여기로 제한')
        return ids

    def _submitted_today(self) -> int:
        """오늘 제출 수 — **WQB 실측과 우리 집계 중 큰 쪽**.

        우리 집계는 UI 로 직접 낸 것을 모르고(2026-07-28: 2 vs 실측 3), WQB 쪽은
        방금 우리가 낸 것이 아직 안 잡힐 수 있다. 예산을 넘겨 내는 쪽이 훨씬
        나쁘므로(WQB 가 거절해 후보 하나를 버린다) 큰 값을 쓴다.
        조회는 5분 캐시 — 게이트는 라운드마다 여러 번 불린다.
        """
        try:
            local = _db.submitted_today(self.user_id)
        except Exception:
            local = 0
        cached = getattr(self, '_sub_cnt_cache', None)
        today = _db.platform_date()
        if cached and cached[0] == today and time.time() - cached[1] < 300:
            return max(local, cached[2])
        try:
            from . import wqb_api as _wqb_api
            u, p = _db.get_user_credentials(self.user_id)[:2]
            remote = _wqb_api.WqbApiClient(u, p).submissions_on(today)
        except Exception as e:
            self._log_quiet(0, f'⚠ WQB 제출 수 조회 실패(로컬 집계 사용): {e}')
            return local
        if remote is None:
            return local
        self._sub_cnt_cache = (today, time.time(), int(remote))
        if remote > local:
            self._log_quiet(
                0, f'📊 오늘 제출 {remote}건 (우리 집계 {local}건) — 외부 제출 반영')
        return max(local, int(remote))

    def _account_operators(self, username: str, password: str):
        """이 계정이 쓸 수 있는 연산자 집합. 조회 실패면 None(= 제한 없음).

        데이터셋 캐시와 같은 규칙(하루 1회, 실패 시 직전 값 유지)이지만 **RC 도
        건너뛰지 않는다** — 2026-07-28 실측으로 CONSULTANT 계정에서도 vector_proj·
        regression_neut·regression_proj 이 막혀 있었다.
        """
        cached = getattr(self, '_acct_op_cache', None)
        if cached and time.time() - cached[0] < 24 * 3600:
            return cached[1]
        try:
            from . import wqb_api as _wqb_api
            ops = _wqb_api.WqbApiClient(username, password).accessible_operators()
        except Exception as e:
            self._log_quiet(0, f'⚠ 계정 연산자 조회 실패(직전 값 유지): {e}')
            return cached[1] if cached else None
        if not ops:
            return cached[1] if cached else None
        self._acct_op_cache = (time.time(), ops)
        return ops

    def _drain_submit_queue(self, round_num: int, username: str,
                            password: str) -> None:
        """대기 큐를 그날 예산이 찰 때까지 비운다 — **라운드와 무관한 별개 경로**.

        2026-07-29 사장 지시: 제출은 시뮬 라운드와 아무 상관이 없다. 라운드 경계에
        묶여 있으면 예산이 열려도 라운드가 끝날 때까지(40~70분) 묵는다. 그래서 이제
        전용 티커 스레드만 이걸 부른다(라운드 시작 시 호출은 제거).

        각 대기 건을 **한 번씩** 내본다:
          · 성공 → 큐에서 삭제(제출 내역에는 남는다)
          · 거절 → status='rejected' 로 목록에 그대로 둔다(자동 재시도 없음)
        제출 방식이 '목록에 추가'(list)면 아무것도 자동 제출하지 않는다.
        """
        try:
            if _db.get_submit_mode(self.user_id) == 'list':
                return
        except Exception as e:
            LOG.warning('제출 모드 조회 실패 (큐 드레인 생략): %s', e)
            return
        # 한도만큼만 돈다 — 게이트가 보류시키면 _drain_one 이 None 으로 루프를 끊는다.
        # 이번 판에 이미 건드린 행은 다시 집지 않는다 — 일시 거절로 pending 에 되돌린
        # 행을 곧바로 재선택하면 그 한 건이 루프를 독점해 뒤의 대기 건이 굶는다.
        tried: set[int] = set()
        for _ in range(max(1, DAILY_SUBMIT_BUDGET)):
            if self._stop_event.is_set():
                return
            rid = self._drain_one(round_num, username, password, skip=tried)
            if rid is None:
                return
            tried.add(rid)

    @staticmethod
    def _rejection_looks_spurious(client, wqb_alpha_id: str) -> bool:
        """거절 직후 WQB 가 보는 체크에 FAIL 이 하나도 없으면 '다시 내볼 만하다'고 본다.

        제출 판정은 주(week)마다 바뀐다 — 고회전 면제·테마·피라미드 배수가 갈아끼워지고,
        같은 알파가 어떤 주엔 통과하고 어떤 주엔 떨어진다(2026-07-29 사장 판단).
        그래서 한 번의 403 으로 영구 폐기하지 않고 **딱 한 번** 더 내본다.
        거절은 일일 예산을 쓰지 않으므로 재시도 비용은 API 호출 한 번뿐이다.
        판단 불가면 False — 거절을 함부로 무효화하지 않는다(fail-closed).
        """
        try:
            h = client.harvest_alpha(wqb_alpha_id) or {}
            st = h.get('is_status') or {}
            if not st:
                return False
            return not st.get('fail')
        except Exception as e:
            LOG.warning('거절 재확인 실패(거절 유지): %s', e)
            return False

    def _drain_one(self, round_num: int, username: str, password: str,
                   skip=()) -> int | None:
        """대기 큐에서 1건 제출. 시도한 행 id 를 반환(더 볼 게 없으면 None).

        시뮬 쿼터 0 소모 — 영속화된 wqb_alpha_id 로 직접 제출한다.
        kind='theme'(테마 미충족)은 자동 드레인하지 않는다 — 테마가 바뀐 뒤
        사람이 UI 에서 판단해 1건씩 쏜다 (2026-07-27 사장 지시).
        """
        try:
            # 대기 건 조회(DB, 공짜)를 먼저 한다 — 예산 조회는 WQB 실측을 타므로,
            # 큐가 비었는데 주기마다 API 를 두드리는 낭비를 막는다.
            rows = [r for r in _db.submit_queue_list(self.user_id, limit=200)
                    if r.get('kind') == 'budget' and r.get('status') == 'pending'
                    and int(r['id']) not in set(skip)]
            if not rows:
                return None
            row = min(rows, key=lambda r: int(r['id']))      # 오래된 것부터
            if self._submitted_today() >= DAILY_SUBMIT_BUDGET:
                return None
        except Exception:
            return None
        wid = str(row.get('wqb_alpha_id') or '')
        ok_gate, reason = self._submit_gate(row.get('metrics') or {}, None,
                                            fail_items=[], genome=None)
        if not ok_gate:
            if not reason.startswith('daily_budget'):
                _db.submit_queue_mark(row['id'], 'pending', f'게이트 보류: {reason}')
            return None
        # 🔒 선점 — 네트워크에 나가기 **전에** 'submitting' 으로 찍어 남이 못 집게 한다.
        #   2026-07-29 실측: 드레인 두 갈래(라운드 훅 + 티커)가 같은 pending 행을 동시에
        #   집어 같은 알파를 두 번 제출했다(제출은 4분씩 걸려 그동안 계속 pending 이었다).
        #   대시보드의 수동 [제출] 버튼과도 같은 규약을 쓴다(app.py 도 'submitting' 선점).
        _db.submit_queue_mark(row['id'], 'submitting', '자동 제출 진행 중…')
        self._log(round_num, f'  📤 대기 큐 제출 시도 — {wid}')
        try:
            from . import wqb_api as _wqb_api
            client = _wqb_api.WqbApiClient(username, password)
            if not client.authenticate():
                _db.submit_queue_mark(row['id'], 'pending', 'WQB 인증 실패 — 재시도 가능')
                return None
            try:
                from . import alpha_description as _adesc
                if row.get('code'):
                    client.set_alpha_description(
                        wid, _adesc.build(str(row['code']), genome=None, settings={}))
            except Exception:
                pass
            # 제출은 계정당 하나씩 — 라운드 시뮬 스레드의 제출과 섞이면 429 로 서로를
            # 죽인다. 티커 스레드에서도 도니 프로세스 전역 락으로 직렬화한다.
            from . import wqb_backend as _wb
            with _wb.SUBMIT_LOCK:
                ok, st = client.submit_alpha(wid, stop_event=self._stop_event,
                                             deadline_s=600)
        except Exception as e:
            # 선점만 해두고 죽으면 그 행이 영영 잠긴다 — 반드시 되돌린다.
            _db.submit_queue_mark(row['id'], 'pending', f'예외: {str(e)[:120]} — 재시도 가능')
            self._log_quiet(round_num, f'⚠ 대기 큐 제출 예외(무시): {e}')
            return None
        # 성공하면 큐에서 **없앤다** — 목록은 '아직 낼 것' 만 보여야 한다(사장 지시).
        # 기록은 제출 내역(record_submit_attempt)과 alphas 테이블에 그대로 남는다.
        # 거절은 그대로 둔다 — 사람이 보고 판단하며, 자동 재시도는 하지 않는다.
        if ok:
            try:
                _db.submit_queue_delete(self.user_id, [row['id']])
            except Exception as e:
                LOG.warning('큐 삭제 실패(상태만 갱신): %s', e)
                _db.submit_queue_mark(row['id'], 'submitted', st[:200])
        elif ('재시도' not in (row.get('note') or '')
              and self._rejection_looks_spurious(client, wid)):
            # 지금 FAIL 이 0 인데 제출만 막혔다 → 주간 기준이 바뀌면 통과할 수 있다.
            # 노트에 표식을 남겨 **한 번만** 더 낸다(다음 거절은 확정).
            _db.submit_queue_mark(row['id'], 'pending',
                                  f'1회 재시도 대기 ({st[:100]})')
            self._log(round_num, f'  ↩ 대기 큐 — {wid} 는 한 번 더 내본다(주간 기준 변동 대비)')
        else:
            _db.submit_queue_mark(row['id'], 'rejected', st[:200])
        if row.get('alpha_pk'):
            try:
                _db.set_alpha_submit_result(int(row['alpha_pk']), ok, st)
            except Exception:
                pass
        try:
            _db.record_submit_attempt(self.user_id, round_num, 0,
                                      str(row.get('code') or wid), ok, f'[queue] {st}')
        except Exception:
            pass
        self._log(round_num,
                  (f'  🚀 대기 큐 제출 완료 — {wid} (목록에서 제거)' if ok
                   else f'  ⛔ 대기 큐 제출 거절 — {wid}: {st[:70]} (목록에 남김)'),
                  level=('pass' if ok else 'info'))
        # 거절은 예산을 쓰지 않는다 — 다음 대기 건을 이어서 시도한다.
        return int(row['id'])

    # ── 외부 제어 ─────────────────────────────────────────────
    def request_shutdown(self) -> None:
        """프로세스 종료용 중단.

        request_pause 와 딱 하나 다르다 — **paused 플래그를 DB 에 남기지 않는다.**
        남기면 재시작 후 _auto_resume_workers 가 '사용자가 스스로 멈춘 것' 으로 보고
        워커를 안 켠다(list_running_user_ids 가 paused=0 만 고른다).
        """
        self.request_pause(persist=False)

    def request_pause(self, *, persist: bool = True) -> None:
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
        if persist:
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
    def _drain_ticker(self) -> None:
        """예산 리셋을 **라운드 경계까지 기다리지 않는다**.

        드레인이 라운드 시작 시점에만 돌면, 리셋(미 동부 자정 = KST 13:00)이 라운드
        중간에 걸릴 때 큐가 그 라운드가 끝날 때까지 묵는다 — 2026-07-29 실측: 13:00 에
        예산이 열렸는데 12:31 에 시작한 라운드 때문에 45분을 그냥 기다렸다.
        제출은 시뮬 쿼터를 쓰지 않고 SUBMIT_LOCK 으로 직렬화되므로 라운드 중에 내도 안전하다.
        """
        # 재시작으로 중단된 선점('submitting')은 이 프로세스엔 주인이 없다 — 되돌린다.
        # (부팅 직후라 수동 제출이 진행 중일 수 없어 안전하다)
        try:
            for _r in _db.submit_queue_list(self.user_id, limit=200):
                if _r.get('status') == 'submitting':
                    _db.submit_queue_mark(_r['id'], 'pending', '재시작으로 중단 — 재시도 대기')
        except Exception as e:
            LOG.warning('중단된 선점 복구 실패(무시): %s', e)
        while not self._stop_event.wait(timeout=_DRAIN_TICK_S):
            try:
                creds = _db.get_user_credentials(self.user_id)
                if not creds:
                    continue
                u, p = creds[0], creds[1]
                self._drain_submit_queue(0, u, p)
            except Exception as e:
                LOG.warning('드레인 티커 실패(무시): %s', e)

    def run(self) -> None:
        try:
            threading.Thread(target=self._drain_ticker, daemon=True,
                             name=f'hyfe-drain-{self.user_id}').start()
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
                probe = _auth.probe_wqb_backend(username, password)
                if probe.get('backend') == 'api':
                    _db.set_backend(self.user_id, 'api')
                    self._log(0, '🔌 백엔드 능력 탐지: WQB REST API')
                # 같은 응답에 permissions 가 실려 온다 — 공짜로 역할까지 맞춘다.
                measured = probe.get('account_type')
                if measured and measured != account_type:
                    _db.set_account_type(self.user_id, measured)
                    self._log(0, f'👤 계정 종류 동기화: {account_type} → {measured} '
                                 f'(WQB permissions 실측)')
                    account_type = measured
            except Exception as e:
                self._log_quiet(0, f'⚠ 백엔드 탐침 실패(무시): {e}')
        backend = 'api'   # 시뮬은 항상 REST API 로 시도한다(유일 경로)

        # 📅 Power Pool 주간 테마 자동 동기화 (2026-07-27 사장 지시) — 월요일
        # 00:00 UTC(KST 09:00) 경계는 즉시, 평시엔 6h TTL 로 지원 문서를 확인해
        # 탐색 조건을 갱신한다. 수동 조건이 걸려 있으면 모듈이 알아서 물러난다.
        if account_type == 'research_consultant':
            try:
                from . import theme_sync
                _theme = theme_sync.maybe_sync(username, password, self.user_id)
                if _theme:
                    self._log(0, f'📅 Power Pool 테마 자동 적용: {_theme[:110]}')
            except Exception as e:
                self._log_quiet(0, f'⚠ 테마 동기화 실패(무시): {e}')

        u = _db.get_user(self.user_id)

        # ④ 상관벽 메모리 홀드셋은 '같은 라운드 안의 형제' 차단용이다. 라운드가 끝나면
        # DB(alphas.submit_status) 가 24h 윈도우로 이어받으므로 여기서 비운다 —
        # 안 비우면 세션이 길수록 24h 를 넘겨서까지 과차단한다.
        self._corr_fs_hold.clear()

        # 전략스펙 최우선 — 사용자가 리서치로 명시 요청한 아이디어다. focus 큐 뒤에
        # 세우면 (거의 항상 비어있지 않은) 큐에 밀려 영원히 굶는다.
        # 스펙은 1회성이라 소비되면 큐가 비고 워커는 평소 GA 로 되돌아간다.
        try:
            pending_specs = _db.pending_specs(self.user_id, limit=8)
        except Exception as e:
            self._log_quiet(0, f'⚠ 전략스펙 조회 실패: {e}')
            pending_specs = []
        is_spec_round = bool(pending_specs)

        # 리전 전환 감지 (2026-07-27) — 옛 리전 부모로 채워진 focus 큐는 통째로
        # 무효다(그 코드의 필드가 새 리전에 없어 sub-round 가 전부 'unknown
        # variable' 로 죽는다, GLB 전환 실측). **큐를 읽기 전에** 정리해야
        # 이번 라운드가 죽은 부모로 시작하지 않는다.
        try:
            _spec_now = run_config.get_constraint()
            _reg_now = str(getattr(_spec_now, 'region', '') or '').upper()
            if _reg_now and _reg_now != run_config.get_last_region():
                _oldq = _db.get_focus_queue(self.user_id)
                if _oldq:
                    _db.set_focus_queue(self.user_id, [])
                    self._log(0, f'🧹 리전 전환({run_config.get_last_region() or "?"}'
                                 f'→{_reg_now}) — 옛 리전 focus 큐 {len(_oldq)}건 정리 '
                                 f'(필드 부재로 무효)')
                run_config.set_last_region(_reg_now)
        except Exception as e:
            self._log_quiet(0, f'⚠ 리전 전환 정리 실패(무시): {e}')

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
            # 계정 규칙을 덧씌운다 — 비-컨설턴트는 IQC 규칙상 USA 고정이라, 전역 조건이
            # GLB 를 가리켜도 그 리전으로 경쟁할 수 없다(2026-07-27). 조건이 아예 없으면
            # 빈 조건에서 출발해 리전만 박는다.
            from . import constraint_spec as _cspec
            _constraint = (_constraint or _cspec.ConstraintSpec()).for_account(account_type)
            if _constraint.is_empty():
                _constraint = None
            # 계정 등급이 허용하는 데이터셋으로 팔레트를 좁힌다 — set_constraint 가
            # 이 값을 읽어 풀을 만드므로 **먼저** 걸어야 한다.
            genome_models.set_account_datasets(
                self._account_datasets(account_type, _constraint, username, password))
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
        # ⚠ 대기 큐 제출은 여기서 부르지 않는다 — 라운드와 **완전히 별개**로,
        #   _drain_ticker 스레드가 주기적으로 돌린다(2026-07-29 사장 지시).
        #   라운드에 묶으면 예산이 열려도 라운드가 끝날 때까지 큐가 묵는다.

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
                      f'(남은 시드 {len(seed_genomes)}개)')

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
            # Yield Score 블렌딩 (v8) — 통과 없는 시뮬만 쌓는 arm 을 감점하고
            # 쿼터가 고수율 arm 으로 흐르게 한다. epsilon 탐색은 그대로 유지.
            # ⚠ 프라이어는 0.5 가 아니라 **전역 yield** 에 앵커링한다 — 라이브 실측
            #   (2026-07-26) mean 0.02~0.04 · 전역 yield 0.011 스케일에서 라플라스
            #   (p+1)/(v+2)=0.5 프라이어는 콜드 arm 에 mean 의 2~3배 보너스를 줘
            #   착취 순위를 통째로 뒤집는다. 전역 앵커면 콜드 arm 은 중립이다.
            _arms = _db.bandit_stats(self.user_id)
            _tv = sum(int(a['visits']) for a in _arms)
            _g = (sum(int(a.get('pass_sum') or 0) for a in _arms) / _tv) if _tv else 0.0
            _K = 50.0   # 관측 50회쯤부터 arm 실측이 프라이어를 이긴다
            _stats = {
                a['arm_key']: a['mean'] + YIELD_WEIGHT
                * ((int(a.get('pass_sum') or 0) + _K * _g) / (int(a['visits']) + _K))
                for a in _arms
            }
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
                'Research Consultant Genome'
                if account_type == 'research_consultant'
                else 'Standard Genome'
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
                          f'1) 알파 생성 중 [{kind_label} — 정향변이] '
                          f'(model={model_label}, 부모 #{parent_idx}, fix="{fail_desc[:50]}")...')
            else:
                _sg = (f'g{max(int(g.get("generation") or 0) for g in seed_genomes)}'
                       if seed_genomes else '없음')
                _ss = (f'{max(s["_score"] for s in seeds):.3f}' if seeds else '-')
                self._log(round_num,
                          f'1) 알파 생성 중 (model={model_label}, '
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
                n=ALPHAS_PER_ROUND,
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

            # ── 결정론 레이어 후보 주입 (AAF·smilee 이식) — 이하 전 후보는 기존
            #    파이프라인(repair→lint→hygiene→캐시→시뮬→DB→밴딧)을 그대로 통과한다.
            try:
                import random as _rnd_mod
                _det_rng = _rnd_mod.Random(gen_salt or round_num)
                # 리전 필터 — 조건 리전이 바뀌면 옛 리전 알파는 재료로 못 쓴다
                # (필드가 그 리전에 없어 'unknown variable' 로 전멸, 2026-07-27 GLB 실측).
                _creg = str(getattr(_constraint, 'region', '') or '').upper() or None
                if COMBINE_LAYER_N > 0 and not is_focus and not is_spec_round:
                    from . import combine_layer
                    _cpool = _db.combine_pool(self.user_id, region=_creg)
                    _ops = self._account_operators(username, password)
                    _ccands = combine_layer.candidates(
                        _cpool, n=COMBINE_LAYER_N, rng=_det_rng, operators=_ops)
                    if _ccands:
                        strategies.extend(_ccands)
                        _nc = len(combine_layer.usable_combiners(_ops))
                        self._log(round_num,
                                  f'  🧬 재조합 레이어 — 검증 알파 풀 {len(_cpool)}개'
                                  f'에서 결합 후보 {len(_ccands)}개 추가'
                                  + (f' (쓸 수 있는 결합식 {_nc}/{len(combine_layer.COMBINERS)})'
                                     if _ops else ''))
                # 🧭 사냥 사다리 — 부호·회전율·Fitness 로만 막힌 강신호에 표준 처방.
                if HUNT_LADDER_PER_ROUND > 0 and not is_spec_round:
                    from . import hunt_ladder
                    _hl = _db.hunt_ladder_pool(self.user_id, region=_creg)
                    _added = 0
                    for _t in _hl:
                        if _added >= HUNT_LADDER_PER_ROUND:
                            break
                        _tset = {k: str(_t[k]) for k in
                                 ('universe', 'neutralization', 'decay', 'truncation')
                                 if _t.get(k) not in (None, '')}
                        _ram_warn = str((_t.get('metrics') or {})
                                        .get('ht_ram_ok') or '') != '1'
                        _rx = hunt_ladder.remedies(
                            _t['code'], _t.get('metrics') or {}, _tset,
                            _t.get('blocking'),
                            n=HUNT_LADDER_PER_ROUND - _added,
                            ht_ram_warning=_ram_warn)
                        for _v in _rx:
                            _v['idx'] = hunt_ladder.IDX_BASE + _added
                            _v['parent_alpha_id'] = _t['id']
                            _v['desc'] = (f'α#{_t["id"]}(S{float(_t["sharpe"]):.2f}) '
                                          + _v['desc'])
                            strategies.append(_v)
                            _added += 1
                    if _added:
                        self._log(round_num,
                                  f'  🧭 사냥 사다리 — 강신호 차단 알파에 표준 처방 '
                                  f'{_added}개 (부호반전·사후감쇠·RAM중립화)')
                if (HT_RESCUE_PER_ROUND > 0 and not is_focus
                        and not is_spec_round):
                    from . import improve_layer as _improve
                    _hpool = _db.ht_rescue_pool(self.user_id, region=_creg)
                    if _hpool:
                        _hp = _det_rng.choice(_hpool[:10])   # 상위 10 순환
                        _pset = {k: str(_hp[k]) for k in
                                 ('universe', 'neutralization', 'decay', 'truncation')
                                 if _hp.get(k) not in (None, '')}
                        _hvars = _improve.variants(
                            _hp['code'], _pset, {'turnover': _hp.get('turnover')},
                            n=HT_RESCUE_PER_ROUND, rng=_det_rng)
                        for _i, _v in enumerate(_hvars):
                            _v['idx'] = 51 + _i     # 41+ 는 focus 개선 대역
                            _v['origin'] = 'ht_rescue'
                            _v['parent_alpha_id'] = _hp['id']
                            _v['desc'] = (f'🚑 HT구제 α#{_hp["id"]}'
                                          f'(S{float(_hp["sharpe"] or 0):.2f}·'
                                          f'to{float(_hp["turnover"] or 0):.2f}) — '
                                          + _v['desc'])
                        if _hvars:
                            strategies.extend(_hvars)
                            self._log(round_num,
                                      f'  🚑 HT 구제 레이어 — 고샤프·고회전 풀 '
                                      f'{len(_hpool)}개 중 α#{_hp["id"]} 회전 절감 '
                                      f'변형 {len(_hvars)}개 주입')
                if IMPROVE_LAYER_N > 0 and is_focus and phase == 1 and parent_code:
                    from . import improve_layer
                    _ivars = improve_layer.variants(
                        parent_code, parent_settings, parent_metrics,
                        n=IMPROVE_LAYER_N, rng=_det_rng)
                    for _v in _ivars:
                        _v['parent_alpha_id'] = (focus_entry or {}).get('parent_alpha_id')
                    if _ivars:
                        strategies.extend(_ivars)
                        self._log(round_num,
                                  f'  🔧 개선 레이어 — 부모 회전율 등급 '
                                  f'{improve_layer.turnover_class(parent_metrics.get("turnover"))}'
                                  f' 그리드 변형 {len(_ivars)}개 추가')
            except Exception as _e:
                self._log_quiet(round_num, f'⚠ 결정론 레이어 실패(무시): {_e}')

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

            # 리전 채우기 — 지문/시뮬 이전이어야 한다(_stamp_region 주석 참조).
            _n_reg = _stamp_region(
                strategies, str(getattr(_constraint, 'region', '') or '').upper() or None)
            if _n_reg:
                self._log_quiet(round_num, f'리전 미지정 후보 {_n_reg}개에 조건 리전 주입')

            # 캐시 hit 분리 (settings-aware 키: code_hash + settings_fingerprint).
            cached_results: list[dict] = []
            to_simulate: list[dict] = []
            _novelty_n = 0
            seen: set[str] = set()
            # idx → 저장된 alphas.id. **시뮬이 끝나는 즉시** 채워진다.
            alpha_id_by_idx: dict[int, int] = {}
            def _alpha_entry(r: dict) -> dict:
                """시뮬 결과 1건 → alphas 행. 즉시 저장과 라운드 끝 저장이 **같은
                한 곳**을 쓴다 — 두 벌로 두면 반드시 어긋난다."""
                i = int(r.get('idx') or 0)
                _m = dict(r.get('metrics') or {})
                # delay-aware 캐시 stamp — 갓 시뮬한 결과엔 이번 라운드 강제 delay를,
                # 캐시 재사용분엔 원본 _delay 를 그대로 둔다.
                if not r.get('cached'):
                    _m['_delay'] = str(forced_delay)
                _meta = meta_by_idx.get(i) or {}
                return {
                    'idx': i, 'code': r.get('code', ''), 'desc': r.get('desc', ''),
                    'pass_count': int(r.get('pass_count') or 0),
                    'pass_items': r.get('pass_items') or [],
                    'fail_count': int(r.get('fail_count') or 0),
                    'fail_items': r.get('fail_items') or [],
                    'error_count': int(r.get('error_count') or 0),
                    'pending_count': int(r.get('pending_count') or 0),
                    'submitted': r.get('submitted', False),
                    'submit_status': r.get('submit_status', ''),
                    'error_text': r.get('error_text', ''),
                    'metrics': _m,
                    'is_status': r.get('is_status') or {},
                    'mode': r.get('mode', ''),
                    'cached': bool(r.get('cached')),
                    'phase': phase,
                    'settings': settings_by_idx.get(i, {}),
                    'delay': forced_delay,
                    'self_corr': r.get('self_corr'),
                    # 세대(lineage)는 시뮬 결과가 아니라 생성 시 유전체가 안다.
                    'generation': int((genome_by_idx.get(i) or {}).get('generation') or 0),
                    # 귀속(v6) — 부모 알파 id·변이 축·바뀐 유전자.
                    'parent_alpha_id': _meta.get('parent_alpha_id'),
                    'origin': _meta.get('origin'),
                    'directive': _meta.get('directive'),
                    'genes_changed': _meta.get('genes_changed'),
                    # 이 알파를 낳은 LLM 전략스펙 (NULL = 순수 GA 산).
                    'spec_id': _meta.get('spec_id'),
                    # 유전체 원본 — 다음 라운드가 코드에서 역추출하지 않고 이걸 읽는다.
                    'genome': genome_by_idx.get(i),
                }

            def _persist(r: dict) -> None:
                """알파 1건을 즉시 저장. 라운드 끝까지 미루면 그 사이 재시작에
                통째로 날아가고, 캐시도 비어 같은 식을 다시 시뮬한다
                (2026-07-28 실측: 중단된 라운드 2개가 alphas 0건, 캐시히트 0)."""
                i = int(r.get('idx') or 0)
                if i in alpha_id_by_idx or (r.get('error_text') or '').strip():
                    return          # 에러 행은 캐시 대상이 아니라 라운드 끝에서 처리
                try:
                    alpha_id_by_idx[i] = _db.insert_alpha(
                        self.user_id, round_id, round_num, _alpha_entry(r))
                except Exception as _e:
                    self._log_quiet(round_num, f'⚠ 즉시 저장 실패(라운드 끝 재시도): {_e}')

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
                # ♻ 신규성 압력 — 기지 조합이면 근처 신규 변형으로 교체해 시뮬한다.
                #   spec 은 'LLM 산출물 원본 측정'이 목적이라 원본 그대로 둔다(캐시 응답).
                if cached and NOVELTY_REWRITE and (s.get('origin') or '') != 'spec':
                    _nv = _novelty_rewrite(self.user_id, s['code'], fp, seen)
                    if _nv:
                        seen.add(f'{_db.code_hash(_nv)}:{fp}')
                        s['code'] = _nv
                        # ⚠ 유전체는 비운다 — 코드만 바꾸면 genome↔code 대응이 깨져
                        #   엘리트 시딩이 오염된다(2026-07-11 손실 압축 교훈의 역방향).
                        #   genome-less 라도 combine_pool 재료로는 살아남는다.
                        s['genome'] = None
                        genome_by_idx[int(s['idx'])] = {}
                        s['desc'] = '♻ 신규화: ' + (s.get('desc') or '')
                        _novelty_n += 1
                        to_simulate.append(s)
                        continue
                if cached:
                    cached_results.append(result_cache.materialize(s, cached, round_num))
                else:
                    to_simulate.append(s)
            cache_hit_total = len(cached_results)
            if _novelty_n:
                self._log(round_num,
                          f'  ♻ 신규성 압력 — 기지(旣知) 조합 {_novelty_n}개를 '
                          f'근처 신규 변형으로 교체 (슬롯 낭비 방지)')

            # 라운드의 시뮬 대상 알파를 WQB REST API 로 넘긴다 — ApiBackend 가 ThreadPool 로
            # 동시 실행하고(계정 tier 만큼), 결과를 idx 순서로 정렬해 돌려준다.
            all_results: list[dict] = list(cached_results)
            do_simulate = bool(to_simulate)

            if do_simulate and not self._stop_event.is_set():
                batch = to_simulate
                self._log(round_num,
                          f'  ── 라운드 시뮬 시작 #{[s_["idx"] for s_ in batch]}')
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
                    _persist(obj)
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
                    # 테마 미충족 거절 보관 (2026-07-27 사장 지시) — 테마는 주간
                    # 로테이션이라 다음 주 수동 재시도 가치가 있다. UI '제출 대기'
                    # 카드에서 버튼으로 1건씩 재제출한다.
                    # ⚠ 단, **자력으로 제출 컷을 넘을 수 있는 것만** 보관한다
                    #   (2026-07-27 사장 지시). 테마가 바뀌어도 Fitness·Sharpe 가
                    #   모자라면 그때도 떨어진다 — 가망 없는 걸 쌓아 두면 사람이
                    #   골라야 할 것들이 그 사이에 묻힌다(실측: 34건 중 4건만 유효).
                    if (submit_status.startswith('rejected:')
                            and 'PURE_POWER_POOL' in submit_status.upper()
                            and _theme_retry_worthwhile(obj.get('metrics') or {})):
                        try:
                            _m = dict(obj.get('metrics') or {})
                            _wid = str(_m.get('wqb_alpha_id') or '')
                            _code = ''
                            for _b in batch:
                                if int(_b.get('idx') or 0) == s_idx:
                                    _code = _b.get('code', ''); break
                            if _wid and _db.submit_queue_add(
                                    self.user_id, wqb_alpha_id=_wid, kind='theme',
                                    code=_code, note=submit_status[:200], metrics=_m):
                                self._log(_round_num,
                                          f'      📥 #{s_idx} 테마 미충족 — 제출 대기 '
                                          f'큐에 보관 (다음 테마 주간 재시도)')
                        except Exception:
                            pass
                    # ⑤ 제출 거절 → **대기 큐로 회수**. 제출 판정은 주마다 바뀌므로
                    #   (고회전 면제·테마·피라미드 배수 교체) 그 자리에서 버리지 않고
                    #   지금 FAIL 이 0 인 알파는 큐에 넣어 티커가 한 번 더 낸다.
                    #   상관·테마 거절은 각자 전용 경로가 있으므로 제외.
                    if (submit_status.startswith('rejected:')
                            and 'CORRELATION' not in submit_status.upper()
                            and 'PURE_POWER_POOL' not in submit_status.upper()):
                        try:
                            _m = dict(obj.get('metrics') or {})
                            _wid = str(_m.get('wqb_alpha_id') or '')
                            from . import wqb_api as _wapi
                            if _wid and self._rejection_looks_spurious(
                                    _wapi.WqbApiClient(username, password), _wid):
                                _code = ''
                                for _b in batch:
                                    if int(_b.get('idx') or 0) == s_idx:
                                        _code = _b.get('code', ''); break
                                if _db.submit_queue_add(
                                        self.user_id, wqb_alpha_id=_wid, kind='budget',
                                        code=_code, metrics=_m,
                                        note='제출 거절 — 기준 변동 대비 1회 재시도 대기'):
                                    self._log(_round_num,
                                              f'      📥 #{s_idx} 제출 거절 — 대기 큐에서 '
                                              f'한 번 더 시도')
                        except Exception as e:
                            self._log_quiet(_round_num, f'⚠ 거절 회수 실패(무시): {e}')
                    # ④ 상관 거절 즉시 캡처 — DB 기록은 라운드 끝이라, 같은 라운드의
                    # 형제 제출을 막으려면 여기(부분 결과 스트림)에서 잡아야 한다.
                    if (submit_status.startswith('rejected:')
                            and 'CORRELATION' in submit_status.upper()):
                        try:
                            _g = genome_by_idx.get(s_idx) or {}
                            _fs = frozenset(str(f) for f in (_g.get('fields') or []) if f)
                            if _fs:
                                self._corr_fs_hold.add(_fs)
                                self._log(_round_num,
                                          f'      🧱 #{s_idx} 상관벽 필드셋 홀드 — '
                                          f'같은 필드셋 형제는 이번 제출 보류')
                        except Exception:
                            pass

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
            for r in all_results:
                # 저장은 _persist 가 시뮬 종료 즉시 이미 했을 수 있다 — 그 경우 건너뛴다.
                # 여기 남는 건 에러 행과, 즉시 저장이 실패했던 행이다.
                if int(r.get('idx') or 0) not in alpha_id_by_idx:
                    alpha_id_by_idx[int(r['idx'])] = _db.insert_alpha(
                        self.user_id, round_id, round_num, _alpha_entry(r))
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
                        _passed = _is_best_alpha(r)   # yield 분자 — 게이트 통과 여부
                        for _ak in _bandit.arm_keys_for_assignment(_assign):
                            _dim = _ak.split(':', 1)[0]
                            _db.bandit_update(self.user_id, _ak, _rwd, round_num,
                                              dimension=_dim, decay_k=BANDIT_DECAY_K,
                                              passed=_passed)
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
            # 라운드 한 줄 요약 (2026-07-27) — 체크 **개수**만으로는 '잘 돌고 있나' 를
            # 알 수 없다. 이번 라운드 최고 알파와 제출 결과를 같이 적는다.
            summary = (f'═══ ROUND {label} {status} — 시도 {len(all_results)} / '
                       f'PASS≥{PASS_THRESHOLD} {pass_total} / 오류 {err_total} / '
                       f'캐시히트 {cache_hit_total}{_round_headline(all_results)} ═══')
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

                # 제출 거절 부모 — IS 체크는 전부 통과했는데 **제출에서** 죽은 알파.
                # 신호 자체는 검증된 것이라 버리지 않고, 거절 사유를 그대로 개선 방향으로
                # 삼아 연마 큐에 넣는다. _classify_focus 는 FAIL 0 이라 이들을 못 잡는다.
                #
                # 2026-07-23 엔 상관(CORRELATION) 거절만 잡았는데, 2026-07-29 실측으로
                # **고회전 거절이 훨씬 많다**: LOW_GLB_EMEA_SHARPE 등으로 거절된 HT 알파가
                # 라운드마다 나오는데 전부 버려지고 있었다(58k5agYM·E5ezrEqL).
                # 거절 사유가 곧 '무엇을 고쳐야 하는가' 다 — mutation_learn.categorize 가
                # 이름으로 축을 고른다(…SHARPE→signal, …FITNESS→fitness, SUB_…→sub_universe,
                # CORRELATION→correlation). 사장 지시로 모든 거절 사유로 넓혔다.
                _rej_parents = [
                    r for r in all_results
                    if not r.get('cached')
                    and str(r.get('submit_status') or '').startswith('rejected:')
                ][:2]                       # 라운드당 2개 — 예산 보호
                for _cp in _rej_parents:
                    if any(x is _cp for x in focus_candidates):
                        continue
                    focus_candidates.append(_cp)
                    _why = str(_cp.get('submit_status') or '')
                    _axis = ('탈상관' if 'CORRELATION' in _why.upper() else '거절 사유')
                    self._log(round_num,
                              f"  🧭 제출 거절 부모 #{_cp.get('idx')} — {_axis} 연마 "
                              f"대상으로 추가 ({_why[:60]})")

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
                            # 제출 거절 부모 — IS 체크는 전부 PASS 라 fail 항목이 없다.
                            # 거절 사유를 실어야 directed_mutation 이 고칠 축을 고른다.
                            _ss = str(a.get('submit_status') or '')
                            if _ss.startswith('rejected:'):
                                fd = (_ss.split(':', 1)[-1].strip()[:200]
                                      or 'SUBMIT_REJECTED')
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


# 신호에 영향이 없는 노브 — 여기만 바뀐 변형은 '새 실험'이 아니라 **같은 알파**다.
# ts_backfill(field, N) 의 N 은 결측치를 며칠 전 값으로 메울지만 정한다. 결측이 드문
# 필드에선 신호가 한 톨도 안 바뀌는데, 코드 해시는 달라져 캐시를 못 타고 풀 시뮬을 태운다.
# 2026-07-29 실측: 최근 3일 '♻ 신규화' 알파 203건이 앞선 알파와 지표가 **완전히 동일**했다
# (#63: backfill 120→96→144, 셋 다 S=1.50·TO=0.2926). 신규성 압력이 쿼터를 태우고
# 정보는 0을 얻고 있었다 — 사장 지적으로 발견.
_SIGNAL_NEUTRAL_RX = (
    re.compile(r'(ts_backfill\s*\([^,()]+,\s*)\d+(\s*\))'),
)


def _signal_form(code: str) -> str:
    """신호에 무관한 노브를 자리표시자로 지워, 두 코드가 '같은 실험'인지 비교한다."""
    out = str(code or '')
    for rx in _SIGNAL_NEUTRAL_RX:
        out = rx.sub(r'\1N\2', out)
    return out


def _novelty_rewrite(user_id: int, code: str, fp: str, seen: set) -> str | None:
    """이미 시뮬한 (code, settings) 조합을 **아직 안 해본** 근처 변형으로 바꿔본다.

    alpha_mutate 의 수치/연산자 변형을 순서대로 시도해 (code_hash, fp) 가 캐시에도
    이번 라운드(seen)에도 없는 첫 변형을 돌려준다. 없으면 None(캐시 응답 유지).
    순수 변형 + 캐시 조회뿐이라 싸다. 예외는 호출부가 아니라 여기서 삼킨다.

    ⚠ 해시가 다르다고 새 실험이 아니다 — 신호가 안 바뀌는 노브만 건드린 변형은
      건너뛴다(_signal_form). 이게 없으면 캐시를 우회해 같은 알파를 다시 시뮬한다.
    """
    try:
        from . import alpha_mutate as _am
        _base = _signal_form(code)
        for v in _am.mutate(code, max_variants=12, include_negation=False):
            if _signal_form(v) == _base:
                continue                      # 신호가 같은 변형 = 같은 실험
            if f'{_db.code_hash(v)}:{fp}' in seen:
                continue
            if result_cache.lookup(user_id, v, fp) is None:
                return v
    except Exception:
        pass
    return None


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


def _round_headline(results) -> str:
    """라운드 결과 → ' · 최고 S=1.21(#3) · 제출 1 · 대기큐 +2' 같은 꼬리표.

    비어 있거나 지표가 없으면 빈 문자열(요약 형식을 깨지 않는다).
    """
    best_s, best_idx, submitted, queued = None, None, 0, 0
    for r in results or []:
        st = str((r or {}).get('submit_status') or '')
        if st == 'submitted':
            submitted += 1
        elif '→queued' in st or 'submit_mode=list' in st:
            queued += 1
        try:
            s = float(str(((r or {}).get('metrics') or {}).get('sharpe')))
        except (TypeError, ValueError):
            continue
        if best_s is None or s > best_s:
            best_s, best_idx = s, (r or {}).get('idx')
    bits = []
    if best_s is not None:
        bits.append(f'최고 S={best_s:.2f}' + (f'(#{best_idx})' if best_idx else ''))
    if submitted:
        bits.append(f'🚀 제출 {submitted}')
    if queued:
        bits.append(f'📥 대기큐 +{queued}')
    return (' · ' + ' · '.join(bits)) if bits else ''


def _skip_reason_ko(sub_st: str) -> str:
    """'submit_skipped:<reason>' → 사람이 읽는 한 줄.

    게이트가 돌려주는 reason 은 코드용 문자열(`daily_budget(4/4)→queued` 등)이다.
    라이브 피드를 보는 사람이 '왜 안 냈는지'를 바로 알 수 있어야 한다.
    """
    r = sub_st.split(':', 1)[1].strip() if ':' in sub_st else ''
    if not r or r == 'paused':
        return '일시정지'
    if r.startswith('daily_budget'):
        return f'오늘 제출 예산 소진 {r[len("daily_budget"):].split("→")[0].strip("()")} → 대기 큐'
    if r.startswith('submit_mode=list'):
        return '제출 방식이 [목록에 추가] → 대기 큐'
    if r.startswith('submit_hold'):
        return f'예산 리셋 대기 {r[len("submit_hold"):].split("→")[0].strip("()")} → 대기 큐'
    if r.startswith('blocking_fail'):
        return r[len('blocking_fail'):].strip('()')
    if r.startswith('already_rejected'):
        return f'같은 식이 이미 거절됨 — {r[len("already_rejected"):].strip("()")}'
    if r.startswith('fieldset_cooldown'):
        return '같은 필드 조합이 최근 반복 거절돼 24h 보류'
    if r.startswith('family_corr_wall'):
        return '같은 아이디어가 상관으로 거절된 적 있어 24h 보류'
    if r.startswith('family_pure_theme_wall'):
        return '같은 필드 조합이 테마 순수성으로 거절돼 24h 보류'
    if r.startswith('family_dup_today'):
        return '오늘 같은 필드 조합을 이미 제출 — 예산 아끼려 보류'
    if r.startswith('below_value'):
        return f'품질 문턱 미달 {r[len("below_value"):].strip("()")}'
    return r[:60]


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
        # ⚠ 실제 사유는 이미 'submit_skipped:<reason>' 에 담겨 있다. 예전엔 그걸 버리고
        #   무조건 '(pause)' 라고 찍어, 예산 소진·목록 모드로 안 낸 것까지 전부
        #   '일시정지' 로 보였다 (2026-07-27 사장 지적).
        star_note = f'  ⏸ 제출 안 함 — {_skip_reason_ko(sub_st)}'
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
