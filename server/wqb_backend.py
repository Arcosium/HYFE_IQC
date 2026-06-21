"""WQB 백엔드 Strategy — ApiBackend가 공식 API 경로로 simulate_batch 계약 구현.
BrowserBackend + dispatcher는 Task 6에서 추가된다.
"""
from __future__ import annotations
import logging
import os
from . import wqb_api

LOG = logging.getLogger('hyfe.wqb_backend')

PASS_FIELDS = ('pass', 'fail', 'error', 'pending')

# worker.py:36 과 동일 env var — 순환 import 없이 단일 진실 소스 유지
PASS_THRESHOLD = int(os.environ.get('HYFE_IQC_PASS_THRESHOLD', '7'))


class ApiBackend:
    def __init__(self, username: str, password: str, client=None):
        self.username = username
        self.password = password
        self._client = client or wqb_api.WqbApiClient(username, password)

    def simulate_batch(self, batch, *, wqb_username=None, wqb_password=None,
                       log_fn=None, proc_holder=None, partial_fn=None,
                       forced_delay=None, stop_event=None) -> list[dict]:
        results = []
        if not self._client.authenticate():
            msg = ('WQB biometric(Persona) 인증 필요 — 대시보드에서 완료'
                   if getattr(self._client, 'persona_required', False)
                   else 'WQB API 인증 실패 (RC 자격증명/권한 확인)')
            return [self._err(s, msg) for s in batch]
        for s in batch:
            if stop_event is not None and stop_event.is_set():
                break
            results.append(self._run_one(s, forced_delay, partial_fn, stop_event))
        return results

    def _run_one(self, s, forced_delay, partial_fn, stop_event) -> dict:
        idx = int(s.get('idx') or 0)
        code = s.get('code', '')
        desc = s.get('desc', '')
        settings = dict(s.get('settings') or {})
        if forced_delay is not None:
            settings['delay'] = forced_delay

        url = self._client.submit_simulation(code, settings)
        if url == 'RATE_LIMITED':
            return self._err(s, 'CONCURRENT_SIMULATION_LIMIT_EXCEEDED (429)')
        if not url:
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

        p_n = len(is_status.get('pass', []))
        f_n = len(is_status.get('fail', []))
        e_n = len(is_status.get('error', []))
        is_pass = (p_n >= PASS_THRESHOLD and f_n == 0 and e_n == 0)
        mode = 'pass' if is_pass else 'fail'

        out = {
            'idx': idx, 'code': code, 'desc': desc,
            'pass_count': p_n, 'pass_items': is_status.get('pass', []),
            'fail_count': f_n, 'fail_items': is_status.get('fail', []),
            'submitted': False, 'submit_status': '', 'error_text': '',
            'metrics': metrics, 'is_status': is_status, 'mode': mode,
        }

        if partial_fn:
            try:
                partial_fn({
                    'idx': idx, 'status': mode, 'error_text': '',
                    'is_status': is_status, 'metrics': metrics,
                    'submit_status': '', 'submitted': False,
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
