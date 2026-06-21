"""WorldQuant BRAIN 공식 API 클라이언트 (Research Consultant 경로).
계약은 _wqb_pw_worker.py 에서 검증된 형태를 그대로 미러링한다."""
from __future__ import annotations
import logging
import requests
from requests.auth import HTTPBasicAuth

LOG = logging.getLogger('hyfe.wqb_api')
BASE = 'https://api.worldquantbrain.com'

class WqbApiClient:
    def __init__(self, email: str, password: str, session=None):
        self.email = email; self.password = password
        self.session = session or requests.Session()
        self.session.auth = HTTPBasicAuth(email, password)
        self._authed = False

    def authenticate(self) -> bool:
        try:
            r = self.session.post(BASE + '/authentication')
        except Exception as e:
            LOG.warning('authenticate network err: %s', e); return False
        self._authed = r.status_code in (200, 201)
        return self._authed

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
