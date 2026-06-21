"""WorldQuant BRAIN 공식 API 클라이언트 (Research Consultant 경로).
계약은 _wqb_pw_worker.py 에서 검증된 형태를 그대로 미러링한다."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import requests
from requests.auth import HTTPBasicAuth

LOG = logging.getLogger('hyfe.wqb_api')
BASE = 'https://api.worldquantbrain.com'


def _default_session_file(email: str) -> str:
    h = hashlib.sha1((email or '').encode('utf-8')).hexdigest()[:16]
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'wqb_sessions')
    return os.path.join(d, f'{h}.pkl')


class WqbApiClient:
    def __init__(self, email: str, password: str, session=None, session_file=None):
        self.email = email; self.password = password
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(email, password)
        # session_file semantics: None → default per-account path (worker/prod);
        #   False → persistence DISABLED (unit tests / no-persist); str → that path.
        if session_file is None:
            self.session_file = _default_session_file(email)
        elif session_file is False:
            self.session_file = None  # disabled
        else:
            self.session_file = session_file
        self._authed = False

    def _save_session(self) -> bool:
        try:
            if not self.session_file:
                return False  # persistence disabled
            ck = self.session.cookies
            d = ck.get_dict() if hasattr(ck, 'get_dict') else dict(ck)
            if not d:
                return False
            os.makedirs(os.path.dirname(self.session_file), mode=0o700, exist_ok=True)
            try:
                os.chmod(os.path.dirname(self.session_file), 0o700)
            except OSError:
                pass
            tmp = self.session_file + '.tmp'
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(d, f)
            os.replace(tmp, self.session_file)
            return True
        except Exception as e:
            LOG.warning('session save err: %s', e); return False

    def _load_session(self) -> bool:
        try:
            if not self.session_file or not os.path.exists(self.session_file):
                return False
            with open(self.session_file, 'r') as f:
                d = json.load(f)
            if not isinstance(d, dict) or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in d.items()):
                return False
            if not d:
                return False
            self.session.cookies.update(d)
            return True
        except Exception as e:
            LOG.warning('session load err: %s', e); return False

    def _session_valid(self) -> bool:
        try:
            r = self.session.get(f'{BASE}/authentication', timeout=15)
            return r.ok and isinstance(r.json(), dict) and ('user' in r.json())
        except Exception:
            return False

    def authenticate(self) -> bool:
        # 1) reuse persisted session — no /authentication POST → no biometric
        if self._load_session() and self._session_valid():
            self._authed = True; return True
        # 2) fresh Basic Auth (persona handling added in Task 2)
        try:
            r = self.session.post(f'{BASE}/authentication', timeout=30)
        except Exception as e:
            LOG.warning('authenticate network err: %s', e); return False
        body = r.json() if (r.headers.get('Content-Type', '').startswith('application/json')) else {}
        if r.status_code in (200, 201) and isinstance(body, dict) and 'user' in body:
            self._save_session(); self._authed = True; return True
        if r.status_code in (200, 201):  # success without explicit user body
            self._save_session(); self._authed = True; return True
        LOG.warning('authenticate failed: HTTP %s', r.status_code)
        return False

    def _ensure_auth(self) -> bool:
        return self._authed or self.authenticate()

    def harvest_alpha(self, alpha_id: str) -> dict | None:
        """GET /alphas/{id} → {metrics, is_status}. _api_harvest_alpha 미러."""
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}')
            if not r.ok:
                return None
            data = r.json()
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        isf = data.get('is') or {}
        checks = isf.get('checks') or []
        out = {'pass': [], 'fail': [], 'error': [], 'pending': []}
        for ch in checks:
            res = str(ch.get('result') or '').upper()
            nm = ch.get('name')
            item = {'name': nm, 'value': ch.get('value'), 'cutoff': ch.get('limit'),
                    'result': res,
                    'desc': f"{nm}: {ch.get('result')} (value={ch.get('value')}, limit={ch.get('limit')})"}
            bucket = {'PASS': 'pass', 'FAIL': 'fail', 'PENDING': 'pending', 'ERROR': 'error'}.get(res)
            if bucket:
                out[bucket].append(item)
        metrics = {}
        for k in ('sharpe', 'fitness', 'returns', 'turnover', 'drawdown', 'margin'):
            if isf.get(k) is not None:
                metrics[k] = str(isf[k])
        return {'metrics': metrics, 'is_status': out}

    def submit_simulation(self, expr: str, settings: dict) -> str | None:
        if not self._ensure_auth():
            return None
        body = {'type': 'REGULAR', 'settings': self._full_settings(settings), 'regular': expr}
        try:
            r = self.session.post(f'{BASE}/simulations', json=body)
        except Exception as e:
            LOG.warning('submit network err: %s', e); return None
        if r.status_code == 429:  # CONCURRENT_SIMULATION_LIMIT_EXCEEDED
            return 'RATE_LIMITED'
        if r.status_code not in (200, 201):
            return None
        return r.headers.get('Location') or r.headers.get('location')

    @staticmethod
    def _full_settings(s: dict) -> dict:
        # UI 기본값 채움. Task 2 스모크로 키/기본값 확정 후 필요시 조정.
        return {
            'instrumentType': 'EQUITY',
            'region': s.get('region', 'USA'),
            'universe': s.get('universe', 'TOP3000'),
            'delay': int(s.get('delay', 1)),
            'decay': int(s.get('decay', 0)),
            'neutralization': s.get('neutralization', 'INDUSTRY'),
            'truncation': float(s.get('truncation', 0.08)),
            'pasteurization': s.get('pasteurization', 'ON'),
            'unitHandling': s.get('unitHandling', 'VERIFY'),
            'nanHandling': s.get('nanHandling', 'OFF'),
            'language': 'FASTEXPR',
            'visualization': False,
        }

    def poll(self, progress_url: str, stop_event=None, deadline_s: int = 720,
             interval_s: float = 5.0, sleep=None) -> dict:
        import time as _t
        sleep = sleep or _t.sleep
        loops = max(1, int(deadline_s / max(interval_s, 0.001)))
        last = {'status': None, 'alpha': None, 'message': None, 'progress': None}
        for _ in range(loops):
            if stop_event is not None and stop_event.is_set():
                self.cancel(progress_url)
                return {'status': 'CANCELLED', 'alpha': None, 'message': '', 'progress': last.get('progress')}
            try:
                r = self.session.get(progress_url)
                j = r.json() if r.ok else {}
            except Exception as e:
                j = {'message': str(e)}
            last = {'status': j.get('status'), 'alpha': j.get('alpha'),
                    'message': j.get('message'), 'progress': j.get('progress')}
            if last['status'] in ('COMPLETE', 'ERROR', 'FAIL', 'WARNING'):
                return last
            sleep(interval_s)
        # deadline 초과 — 슬롯 반환
        self.cancel(progress_url)
        return {'status': 'TIMEOUT', 'alpha': None, 'message': 'poll deadline', 'progress': last.get('progress')}

    def cancel(self, progress_url: str) -> None:
        if not progress_url:
            return
        try:
            self.session.delete(progress_url)  # COMPLETE면 400 — 무해
        except Exception:
            pass

    def read_self_correlation(self, alpha_id: str) -> float | None:
        # Task 2 스모크로 경로/키 확정. 일반형: records 의 max.
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}/correlations/self')
            if not r.ok:
                return None
            j = r.json()
        except Exception:
            return None
        return _extract_max_correlation(j)


def _extract_max_correlation(j) -> float | None:
    """correlation 응답에서 max self-correlation 추출. 응답 형태가 여러 가지라 방어적."""
    if not isinstance(j, dict):
        return None
    # 1) {'max': 0.4} 류
    if isinstance(j.get('max'), (int, float)):
        return float(j['max'])
    # 2) {'records': [[...,corr], ...], 'schema': {...}} 류 — 모든 수치의 max
    recs = j.get('records')
    if isinstance(recs, list) and recs:
        vals = []
        for row in recs:
            if isinstance(row, (list, tuple)):
                vals += [x for x in row if isinstance(x, (int, float))]
        if vals:
            return max(vals)
    return None
