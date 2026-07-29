"""WQB 백엔드 Strategy — ApiBackend가 공식 API 경로로 simulate_batch 계약 구현.
RC(Research Consultant) 는 동시 시뮬을 지원하므로 ThreadPool 로 알파를 병렬 실행한다.
"""
from __future__ import annotations
import logging
import os
import random as _rnd
import threading
import time as _time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as _fwait
from . import criteria as _criteria
from . import wqb_api

LOG = logging.getLogger('genomicwqb.wqb_backend')

PASS_FIELDS = ('pass', 'fail', 'error', 'pending')

# worker.py 와 동일 env var — 순환 import 없이 단일 진실 소스 유지.
# 기본값 7 → 1 (2026-07-21): worker.PASS_THRESHOLD 주석의 근거 참조.
PASS_THRESHOLD = int(os.environ.get('HYFE_IQC_PASS_THRESHOLD', '1'))

# RC 동시 시뮬 한도. 리서치(rocky-d/wqb, worldquant-miner): consultant=8 안전기본/10 상한,
# 일반(pre-consultant)=최대 5. 1..10 로 clamp. (env 로 튜닝, 재시작 반영)
_CONCURRENCY_DEFAULT = 8
# 429(CONCURRENT_SIMULATION_LIMIT_EXCEEDED)=슬롯 한도. 실패가 아니라 "슬롯 빌 때까지 대기".
# 재시도 예산(deadline)은 **sim 1건 소요시간보다 길어야** 한다 — 슬롯은 앞선 sim 이
# 끝나야 비기 때문이다. 예산 < sim시간이면 대기자는 슬롯이 열리기도 전에 전부 포기한다.
#
# ⚠ 2026-07-27 실측으로 그 일이 실제로 벌어지고 있었다. 600s 는 sim 이 ~130s 이던 시절
#   값인데, GLB·TOPDIV3000·10년 IS 시뮬은 **약 1,200s** 다(r3: 성공 2건에 라운드 1,243s).
#   그래서 라운드마다 8개 중 2~6개가 600s 를 태우고 429 로 죽었다 — 슬롯이 모자란 게
#   아니라 **기다리다 만 것**이다. 폴링 마감(_POLL_DEADLINE_S)과 같은 눈금으로 올린다.
#   2026-07-28: 폴링 마감을 3600 으로 올리며 같이 올린다 — 눈금이 어긋나면 슬롯 대기자가
#   sim 이 끝나기도 전에 포기해 후보를 버린다(아래 테스트가 이 관계를 지킨다).
_RL_DEADLINE_S = float(os.environ.get('HYFE_IQC_RC_RL_DEADLINE_S', '3600'))
_RL_BACKOFF_S = float(os.environ.get('HYFE_IQC_RC_RL_BACKOFF_S', '8'))
# 백오프 상한 = **슬롯이 빈 뒤 그걸 알아채기까지의 최대 지연**이다. 45s 였을 땐 슬롯
# 하나가 빌 때마다 평균 20초 넘게 놀았다. 대기자가 여럿이라 한꺼번에 몰리지 않도록
# 지터를 섞는다(thundering herd 방지). 2026-07-27.
_RL_BACKOFF_CAP_S = float(os.environ.get('HYFE_IQC_RC_RL_BACKOFF_CAP_S', '15'))

# ── 꼬리 절단 (2026-07-29) ──────────────────────────────────────────────────
# r11 #7 실측: 형제 17건이 09:38 에 다 끝났는데 #7 은 **한 번도 시작 못 한 채**
# 폴링 마감(3600s)을 꼬박 태우고 10:12 에 TIMEOUT 했다 — 라운드가 34분 더 서 있었다.
# 마감을 줄이면 정상적으로 늦게 끝나는 시뮬까지 죽인다(그래서 7/28 에 60분으로 올렸다).
# 대신 **형제가 다 끝난 뒤**에도 대기열에서 시작조차 못 한 건만 유예 후 버린다:
# 슬롯이 다 비었는데 큐에서 안 나오면 그 접수는 죽은 것이다.
# ⚠ 이미 돌기 시작한(status 를 받은) 시뮬은 절대 자르지 않는다 — poll() 이 그 판단을 한다.
_TAIL_GRACE_S = float(os.environ.get('HYFE_IQC_TAIL_GRACE_S', '600'))
# 남은 건수가 이 이하일 때부터 '꼬리'로 본다(=슬롯이 넉넉히 비었다).
_TAIL_MAX_PENDING = int(os.environ.get('HYFE_IQC_TAIL_MAX_PENDING', '2'))


# WQB 는 계정당 제출을 한 번에 하나만 처리한다. 제출은 **두 갈래**로 나간다 —
# 라운드 안의 시뮬 완료 직후(sim 스레드) + 대기 큐 드레인(워커/티커 스레드).
# 그래서 락이 ApiBackend 인스턴스에 있으면 무력하다(라운드마다 새 인스턴스 = 새 락).
# 프로세스 전역 락 하나로 모든 제출을 직렬화한다 (2026-07-29).
SUBMIT_LOCK = threading.Lock()


# ── 진행 중 시뮬 추적 (종료 시 취소용) ──────────────────────────────────────
# ⚠ 2026-07-28 실측. 서비스가 죽으면 폴링 스레드도 같이 죽는데, **WQB 쪽 시뮬은 계속
#   돈다** — 취소를 안 보내기 때문이다. 그 유령들이 동시 슬롯을 물고 있어서, 재시작
#   직후 라운드는 빈 슬롯 1개로 시작했다(스레드 8개 중 7개가 t=0 부터 대기, 첫 결과가
#   30분 뒤). stop_event 만으로는 느리다 — 폴링 스레드가 Retry-After 로 최대 60초
#   자고 있어 그때까지 취소가 안 나간다. 그래서 URL 을 들고 있다가 직접 DELETE 한다.
_INFLIGHT: dict[str, object] = {}
_INFLIGHT_LOCK = threading.Lock()


def _track_inflight(url: str, client) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT[url] = client


def _untrack_inflight(url: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.pop(url, None)


def cancel_all_inflight() -> int:
    """진행 중인 시뮬 전부에 DELETE. 취소한 개수 반환. 실패는 삼킨다(종료 경로)."""
    with _INFLIGHT_LOCK:
        items = list(_INFLIGHT.items())
        _INFLIGHT.clear()
    n = 0
    for url, client in items:
        try:
            client.cancel(url)
            n += 1
        except Exception:
            pass
    if n:
        LOG.warning('종료 — 진행 중 시뮬 %d건 취소 (슬롯 반환)', n)
    return n


def _default_concurrency() -> int:
    try:
        return max(1, min(10, int(os.environ.get('HYFE_IQC_RC_CONCURRENCY', str(_CONCURRENCY_DEFAULT)))))
    except (TypeError, ValueError):
        return _CONCURRENCY_DEFAULT


# ── 결과 표시용 메트릭 헬퍼 (구 wqb_browser 에서 이관) ─────────────────────────
# ApiBackend 는 is_status(IS checks)로 pass/fail 을 확정하지만, worker._format_alpha_result
# 는 IS Testing Status 를 못 받았을 때 summary metrics 로 폴백 표기하려고 아래를 쓴다.
# ⚠ 이 표는 IS Testing Status 를 못 받았을 때의 **표시용 폴백**일 뿐이다. 실제 판정은
#   항상 WQB 가 준 checks(criteria.BLOCKING_CHECKS)가 권위다.
#   2026-07-21: sharpe/fitness 는 delay 별로 다르므로 criteria 에서 가져온다
#   (D0 2.69/1.5, D1 1.58/1.0). turnover 상한도 criteria 단일 진실.
_PASS_THRESHOLDS = {
    'sharpe': _criteria.CUTOFFS['1']['sharpe'],
    'fitness': _criteria.CUTOFFS['1']['fitness'],
    'returns': 0.05,
    'turnover_max': _criteria.TURNOVER_MAX,
    'turnover_min': _criteria.TURNOVER_MIN,
    'drawdown_min': -0.3,
    'margin': 0.0,
    'subuniverse_sharpe': 1.0,
    'self_correlation_max': 0.7,
}


def _parse_metric_number(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    # 단위 처리: %, ‱ (per myriad = 1/10000), bp (basis point) 등.
    unit = 1.0
    if s.endswith('%'):
        unit = 1.0 / 100
        s = s[:-1]
    elif s.endswith('‱'):
        unit = 1.0 / 10000
        s = s[:-1]
    elif s.lower().endswith('bp'):
        unit = 1.0 / 10000
        s = s[:-2]
    s = s.replace(',', '').strip()
    try:
        return float(s) * unit
    except ValueError:
        return None


def _derive_pass_fail(metrics: dict) -> tuple[list[str], list[str]]:
    """WQB IS Tests 8 항목 중 통과/탈락 분리. 7 이상 PASS = submittable.

    1. Sharpe          ≥ delay별 컷 (D0 2.69 / D1 1.58)
    2. Fitness         ≥ delay별 컷 (D0 1.5 / D1 1.0)
    3. Returns         ≥ 5%
    4. Turnover        ≤ 70%
    5. Drawdown        ≥ -30% (max DD 30% 이하)
    6. Margin          > 0
    7. Sub-universe Sharpe (sub-portfolio 일관성)  ≥ 1.0
    8. Self-correlation (다른 알파와의 유사도)      < 0.7
    """
    passes, fails = [], []
    sharpe = _parse_metric_number(metrics.get('sharpe'))
    fitness = _parse_metric_number(metrics.get('fitness'))
    returns = _parse_metric_number(metrics.get('returns'))
    turnover = _parse_metric_number(metrics.get('turnover'))
    drawdown = _parse_metric_number(metrics.get('drawdown'))
    margin = _parse_metric_number(metrics.get('margin'))
    # 키 별칭 — 'sub_sharpe', 'subuniverse_sharpe', 'sub_universe_sharpe', 'sub-sharpe' 모두 처리.
    sub_sharpe = _parse_metric_number(
        metrics.get('subuniverse_sharpe') or metrics.get('sub_universe_sharpe')
        or metrics.get('sub_sharpe') or metrics.get('subsharpe')
    )
    self_corr = _parse_metric_number(
        metrics.get('self_correlation') or metrics.get('correlation')
        or metrics.get('is_correlation') or metrics.get('selfcorrelation')
    )

    def check(name, ok):
        if ok is None:
            return
        (passes if ok else fails).append(name)
    cut = _criteria.cutoffs(metrics.get('_delay'))
    check('Sharpe', None if sharpe is None else sharpe >= cut['sharpe'])
    check('Fitness', None if fitness is None else fitness >= cut['fitness'])
    check('Returns', None if returns is None else returns >= _PASS_THRESHOLDS['returns'])
    check('Turnover', None if turnover is None else
          _PASS_THRESHOLDS['turnover_min'] < turnover < _PASS_THRESHOLDS['turnover_max'])
    check('Drawdown', None if drawdown is None else drawdown >= _PASS_THRESHOLDS['drawdown_min'])
    check('Margin', None if margin is None else margin > _PASS_THRESHOLDS['margin'])
    check('Sub-Sharpe', None if sub_sharpe is None else sub_sharpe >= _PASS_THRESHOLDS['subuniverse_sharpe'])
    check('Self-Correlation', None if self_corr is None else self_corr < _PASS_THRESHOLDS['self_correlation_max'])
    return passes, fails


class ApiBackend:
    def __init__(self, username: str, password: str, client=None, concurrency=None):
        self.username = username
        self.password = password
        self._client = client or wqb_api.WqbApiClient(username, password)
        self.concurrency = (_default_concurrency() if concurrency is None
                            else max(1, min(10, int(concurrency))))
        # 제출 직렬화 — 프로세스 전역 락을 쓴다(위 SUBMIT_LOCK 주석 참조).
        # 인스턴스마다 새 락을 만들면 대기 큐 드레인과 라운드 제출이 서로를 못 본다.
        self._submit_lock = SUBMIT_LOCK

    def simulate_batch(self, batch, *, wqb_username=None, wqb_password=None,
                       log_fn=None, proc_holder=None, partial_fn=None,
                       forced_delay=None, stop_event=None,
                       submit_gate=None) -> list[dict]:
        if not self._client.authenticate():
            msg = ('WQB biometric(Persona) 인증 필요 — 대시보드에서 완료'
                   if getattr(self._client, 'persona_required', False)
                   else 'WQB API 인증 실패 (RC 자격증명/권한 확인)')
            return [self._err(s, msg) for s in batch]
        if stop_event is not None and stop_event.is_set():
            return []
        if not batch:
            return []

        # partial_fn 은 여러 워커 스레드에서 호출되므로 lock 으로 직렬화한다
        # (워커 _on_partial 의 _seen_idx/DB 접근이 레이스 없이 안전해진다).
        plock = threading.Lock()

        def _safe_partial(obj):
            if partial_fn is None:
                return
            with plock:
                partial_fn(obj)

        n = max(1, min(self.concurrency, len(batch)))
        out_by_idx: dict[int, dict] = {}
        tail_event = threading.Event()
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix='wqb-sim') as ex:
            futs = {ex.submit(self._run_one, s, forced_delay, _safe_partial, stop_event,
                              submit_gate, tail_event): s
                    for s in batch}
            pending = set(futs)
            while pending:
                # 꼬리(남은 몇 건)만 남으면 유예 시간을 걸고 기다린다 — 그 안에 아무것도
                # 안 끝나면 '시작조차 못 한' 잔여 건을 poll 이 포기하게 한다.
                # out_by_idx 가 비었으면 자르지 않는다: 형제가 하나도 안 끝난 판은
                # '슬롯이 비었다'는 근거가 없어 그냥 느린 것일 수 있다(배치 1~2건 포함).
                tail = (len(pending) <= _TAIL_MAX_PENDING and out_by_idx
                        and not tail_event.is_set())
                done, pending = _fwait(pending, timeout=_TAIL_GRACE_S if tail else None,
                                       return_when=FIRST_COMPLETED)
                if not done and tail:
                    LOG.warning('꼬리 절단 — 잔여 %d건이 %.0fs 동안 무진전, '
                                '대기열 미시작 건 포기(슬롯 반환)', len(pending), _TAIL_GRACE_S)
                    tail_event.set()
                for fut in done:
                    s = futs[fut]
                    try:
                        r = fut.result()
                    except Exception as e:  # 한 알파의 예외가 라운드 전체를 죽이지 않게
                        LOG.warning('run_one err idx=%s: %s', s.get('idx'), e)
                        r = self._err(s, f'sim 예외: {e}')
                    out_by_idx[int(s.get('idx') or 0)] = r
        # 완료 순서가 뒤섞여도 batch(idx) 순서로 정렬해 반환
        return [out_by_idx[int(s.get('idx') or 0)] for s in batch]

    def _submit_with_retry(self, code, settings, stop_event):
        """submit. 429(슬롯 한도)면 슬롯이 빌 때까지 deadline 까지 인내심 재시도.
        끝내 못 잡으면 'RATE_LIMITED', stop 요청 시 None."""
        start = _time.monotonic()
        delay = _RL_BACKOFF_S
        waited = False
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            url = self._client.submit_simulation(code, settings)
            if url != 'RATE_LIMITED':
                # url 이 None 이어도 '접수' 로 찍던 버그 — 제출 실패가 성공 로그를
                # 달고 나와, 라운드 실패를 슬롯 문제로 오독하게 만들었다.
                if waited and url:
                    # 슬롯을 얼마나 기다렸는지 남긴다 — 실효 동시 슬롯 수를 추측이 아니라
                    # 관측으로 알게 해 준다(2026-07-27). 0 이면 여유, 길면 한도에 붙었다.
                    LOG.info('슬롯 대기 %.0fs 후 접수', _time.monotonic() - start)
                return url
            waited = True
            if _time.monotonic() - start >= _RL_DEADLINE_S:
                LOG.warning('슬롯 대기 %.0fs 초과 — 포기(429). 동시 시뮬 한도에 걸려 있다.',
                            _RL_DEADLINE_S)
                return 'RATE_LIMITED'
            # 지터 ±25% — 대기자 여럿이 같은 순간에 몰려 서로를 429 시키지 않게.
            _time.sleep(delay * _rnd.uniform(0.75, 1.25))
            delay = min(delay * 1.5, _RL_BACKOFF_CAP_S)

    @staticmethod
    def _check_submit_gate(submit_gate, metrics, self_corr, fail_items=None,
                           genome=None, code=None):
        """제출 차단/예산/품질 게이트. gate 미지정이면 항상 통과(기존 '무조건 제출' 동작).

        WQB 컨설턴트는 **하루 4건**만 제출할 수 있다(Power Pool 문서). 2026-07 규칙
        개편으로 고회전 알파는 Sharpe 1.1 짜리도 제출 가능해졌기 때문에, 예산을 그날
        가장 먼저 통과한 4개에 흘려보내지 않으려면 여기서 골라야 한다.
        `fail_items` 는 이 알파의 FAIL 체크 이름들 — 차단 FAIL 이 있으면 제출은
        403 이 확정이라 게이트가 보내지 않는다.
        예외는 삼키고 통과시킨다 — 게이트 버그가 제출을 통째로 막으면 안 된다
        ('실주문을 조용히 누락하지 않는다' 와 같은 원칙).
        """
        if submit_gate is None:
            return True, ''
        try:
            ok, reason = submit_gate(metrics or {}, self_corr,
                                     fail_items=list(fail_items or []),
                                     genome=genome, code=code)
        except TypeError:
            # 구 시그니처 호환 — 게이트 교체 중에도 제출은 살린다.
            try:
                ok, reason = submit_gate(metrics or {}, self_corr,
                                         fail_items=list(fail_items or []),
                                         genome=genome)
            except TypeError:
                try:
                    ok, reason = submit_gate(metrics or {}, self_corr,
                                             fail_items=list(fail_items or []))
                except TypeError:
                    ok, reason = submit_gate(metrics or {}, self_corr)
        except Exception as e:
            LOG.warning('submit_gate err (제출 강행): %s', e)
            return True, ''
        return bool(ok), str(reason or '')

    def _run_one(self, s, forced_delay, partial_fn, stop_event, submit_gate=None,
                 tail_event=None) -> dict:
        if stop_event is not None and stop_event.is_set():
            return self._err(s, 'pause로 취소', mode='cancelled')
        idx = int(s.get('idx') or 0)
        code = s.get('code', '')
        desc = s.get('desc', '')
        settings = dict(s.get('settings') or {})
        if forced_delay is not None:
            settings['delay'] = forced_delay

        url = self._submit_with_retry(code, settings, stop_event)
        if url == 'RATE_LIMITED':
            return self._err(s, 'CONCURRENT_SIMULATION_LIMIT_EXCEEDED (429)')
        if not url:
            if stop_event is not None and stop_event.is_set():
                return self._err(s, 'pause로 취소', mode='cancelled')
            return self._err(s, 'simulation 제출 실패 (submit 응답 없음)')

        _track_inflight(url, self._client)
        try:
            pr = self._client.poll(url, stop_event=stop_event, abort_event=tail_event)
        finally:
            _untrack_inflight(url)
        status = pr.get('status')

        if status == 'CANCELLED':
            return self._err(s, 'pause로 취소', mode='cancelled')
        if status in ('ERROR', 'FAIL') or not pr.get('alpha'):
            msg = f"sim {status}: {pr.get('message') or ''}".strip()
            return self._err(s, msg)

        alpha_id = pr['alpha']
        h = self._client.harvest_alpha(alpha_id) or {
            'metrics': {}, 'is_status': {k: [] for k in PASS_FIELDS}
        }
        is_status = h['is_status']
        metrics = h['metrics']
        # WQB 알파 id 를 지표에 영속화 (2026-07-23) — 제출이 일시 장애(타임아웃/5xx)로
        # 끊긴 알파를 나중에 재시뮬 없이 재제출하려면 이 id 가 유일한 열쇠다.
        # (7/23 실측: 전 체크 PASS 알파가 pending_timeout 후 id 가 없어 영영 유실됐다.)
        metrics['wqb_alpha_id'] = str(alpha_id)

        corr = self._client.read_self_correlation(alpha_id)
        if corr is not None:
            metrics['self_correlation'] = str(corr)

        submit_ok = False
        submit_status = ''
        acquired = False
        try:
            # 락 대기 중에도 pause 에 반응해야 하므로 1초 단위로 재시도한다.
            while not acquired:
                acquired = self._submit_lock.acquire(timeout=1.0)
                if not acquired and stop_event is not None and stop_event.is_set():
                    submit_status = 'submit_skipped:paused'
                    break
            # 락을 잡았어도 그 사이 pause 됐으면 제출하지 않는다 (pause 시 라운드
            # 결과는 폐기되므로 제출만 나가면 기록-실제가 어긋난다).
            if acquired and stop_event is not None and stop_event.is_set():
                submit_status = 'submit_skipped:paused'
            elif acquired:
                # 일일 제출 예산 게이트 — **락 안에서** 판정해야 한다. 동시 시뮬 8개가
                # 각자 밖에서 예산을 읽으면 전부 '아직 여유 있음' 을 보고 한도를 넘긴다.
                gate_ok, gate_reason = self._check_submit_gate(
                    submit_gate, metrics, corr, is_status.get('fail'),
                    genome=s.get('genome'), code=code)
                if not gate_ok:
                    submit_status = f'submit_skipped:{gate_reason}'
                else:
                    # Power Pool 필수 요건 (2026-07-23) — 제출 직전에 Idea/Rationale
                    # 형식 설명(100자+)을 PATCH 로 심는다. 설명이 없으면 제출은 되더라도
                    # Power Pool 자격이 없다. 실패해도 제출은 진행(fail-soft).
                    try:
                        from . import alpha_description as _adesc
                        self._client.set_alpha_description(
                            alpha_id,
                            _adesc.build(code, genome=s.get('genome'),
                                         settings=settings))
                    except Exception as e:
                        LOG.warning('description build/patch err (제출 계속): %s', e)
                    submit_ok, submit_status = self._client.submit_alpha(
                        alpha_id, stop_event=stop_event)
        except Exception as e:
            submit_status = f'submit_error:{e}'
        finally:
            if acquired:
                self._submit_lock.release()

        p_n = len(is_status.get('pass', []))
        f_n = len(is_status.get('fail', []))
        e_n = len(is_status.get('error', []))
        pn_n = len(is_status.get('pending', []))
        total_core = p_n + f_n + e_n
        if 0 < total_core < PASS_THRESHOLD and pn_n == 0:
            # core check 총수가 임계보다 적으면 이 계정 tier 에선 PASS>=threshold 가
            # 도달 불가능하다 — 임계 재조정 근거로 남긴다 (게이트 자체는 엄격 유지).
            LOG.warning('alpha %s: core IS checks %d < PASS_THRESHOLD %d — '
                        'HYFE_IQC_PASS_THRESHOLD 재검토 필요', alpha_id, total_core, PASS_THRESHOLD)
        is_pass = (p_n >= PASS_THRESHOLD and f_n == 0 and e_n == 0)
        mode = 'pass' if is_pass else 'fail'

        out = {
            'idx': idx, 'code': code, 'desc': desc,
            'pass_count': p_n, 'pass_items': is_status.get('pass', []),
            'fail_count': f_n, 'fail_items': is_status.get('fail', []),
            'submitted': bool(submit_ok), 'submit_status': submit_status, 'error_text': '',
            'metrics': metrics, 'is_status': is_status, 'mode': mode,
        }

        if partial_fn:
            try:
                # 결과 전체를 넘긴다. 워커가 이걸로 **즉시 DB 에 쓴다** — 예전엔 요약만
                # 줘서 라운드 끝까지 기다려야 했고, 그 사이 재시작하면 끝난 시뮬이
                # 통째로 사라졌다(2026-07-28: 재시작 5회 × 최대 18건, 캐시히트 0).
                partial_fn({**out, 'status': mode})
            except Exception as e:
                LOG.warning('partial_fn err: %s', e)

        return out

    @staticmethod
    def _err(s, msg, mode='error') -> dict:
        return {
            'idx': int(s.get('idx') or 0), 'code': s.get('code', ''), 'desc': s.get('desc', ''),
            'pass_count': 0, 'pass_items': [], 'fail_count': 0, 'fail_items': [],
            'submitted': False, 'submit_status': '', 'error_text': msg,
            'metrics': {}, 'is_status': {k: [] for k in PASS_FIELDS}, 'mode': mode,
        }


def simulate_batch(
    strategies: list[dict], *,
    wqb_username: str, wqb_password: str,
    account_type: str = 'standard',
    backend: str | None = None,
    log_fn=None,
    proc_holder=None,
    partial_fn=None,
    forced_delay=None,
    stop_event=None,
    submit_gate=None,
) -> list[dict]:
    """한 배치를 WQB REST API 로 시뮬한다 (구 wqb_browser.simulate_batch 의 후신).

    Playwright(브라우저) 경로는 2026-07-13 제거됐다 — 일반 계정도 REST API 로
    인증·시뮬됨을 실증(scripts/verify_std_api.py)한 뒤 백엔드를 API 단일로 통일했다.
    `account_type`·`backend` 인자는 구 호출부 호환을 위해 받되 무시한다(항상 ApiBackend).
    시뮬 방식과 무관한 정책(lint/presim/cache 스킵 등)은 worker 가 account_type 으로
    따로 갈린다.
    """
    be = ApiBackend(wqb_username, wqb_password)
    return be.simulate_batch(
        strategies,
        wqb_username=wqb_username, wqb_password=wqb_password,
        log_fn=log_fn, proc_holder=proc_holder, partial_fn=partial_fn,
        forced_delay=forced_delay, stop_event=stop_event, submit_gate=submit_gate,
    )
