"""WQB 백엔드 Strategy — ApiBackend가 공식 API 경로로 simulate_batch 계약 구현.
RC(Research Consultant) 는 동시 시뮬을 지원하므로 ThreadPool 로 알파를 병렬 실행한다.
"""
from __future__ import annotations
import logging
import os
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from . import wqb_api

LOG = logging.getLogger('hyfe.wqb_backend')

PASS_FIELDS = ('pass', 'fail', 'error', 'pending')

# worker.py:36 과 동일 env var — 순환 import 없이 단일 진실 소스 유지
PASS_THRESHOLD = int(os.environ.get('HYFE_IQC_PASS_THRESHOLD', '7'))

# RC 동시 시뮬 한도. 리서치(rocky-d/wqb, worldquant-miner): consultant=8 안전기본/10 상한,
# 일반(pre-consultant)=최대 5. 1..10 로 clamp. (env 로 튜닝, 재시작 반영)
_CONCURRENCY_DEFAULT = 8
# 429(CONCURRENT_SIMULATION_LIMIT_EXCEEDED)=슬롯 한도. 실패가 아니라 "슬롯 빌 때까지 대기".
# sim 한 개가 슬롯을 비우는 데 ~130s+ 걸리므로 재시도 예산(deadline)은 그보다 충분히 길어야
# 8개를 던져도 가용 슬롯 수만큼 웨이브로 처리된다. (예산 < sim시간이면 영원히 슬롯 못 잡고 포기)
_RL_DEADLINE_S = float(os.environ.get('HYFE_IQC_RC_RL_DEADLINE_S', '600'))
_RL_BACKOFF_S = float(os.environ.get('HYFE_IQC_RC_RL_BACKOFF_S', '8'))
_RL_BACKOFF_CAP_S = float(os.environ.get('HYFE_IQC_RC_RL_BACKOFF_CAP_S', '45'))


def _default_concurrency() -> int:
    try:
        return max(1, min(10, int(os.environ.get('HYFE_IQC_RC_CONCURRENCY', str(_CONCURRENCY_DEFAULT)))))
    except (TypeError, ValueError):
        return _CONCURRENCY_DEFAULT


class ApiBackend:
    def __init__(self, username: str, password: str, client=None, concurrency=None):
        self.username = username
        self.password = password
        self._client = client or wqb_api.WqbApiClient(username, password)
        self.concurrency = (_default_concurrency() if concurrency is None
                            else max(1, min(10, int(concurrency))))
        # WQB 는 계정당 제출을 한 번에 하나만 처리한다 — 동시 시뮬 스레드들이 완료
        # 직후 각자 submit 을 때리면 첫 번째만 성공하고 나머지는 429 로 죽는다.
        # 제출은 이 락으로 직렬화한다 ("무조건 제출 시도" 정책의 실효성 보장).
        self._submit_lock = threading.Lock()

    def simulate_batch(self, batch, *, wqb_username=None, wqb_password=None,
                       log_fn=None, proc_holder=None, partial_fn=None,
                       forced_delay=None, stop_event=None) -> list[dict]:
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
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix='wqb-sim') as ex:
            futs = {ex.submit(self._run_one, s, forced_delay, _safe_partial, stop_event): s
                    for s in batch}
            for fut in as_completed(futs):
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
        while True:
            if stop_event is not None and stop_event.is_set():
                return None
            url = self._client.submit_simulation(code, settings)
            if url != 'RATE_LIMITED':
                return url
            if _time.monotonic() - start >= _RL_DEADLINE_S:
                return 'RATE_LIMITED'
            _time.sleep(delay)
            delay = min(delay * 1.5, _RL_BACKOFF_CAP_S)

    def _run_one(self, s, forced_delay, partial_fn, stop_event) -> dict:
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

        pr = self._client.poll(url, stop_event=stop_event)
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
                partial_fn({
                    'idx': idx, 'status': mode, 'error_text': '',
                    'is_status': is_status, 'metrics': metrics,
                    'submit_status': submit_status, 'submitted': bool(submit_ok),
                })
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
