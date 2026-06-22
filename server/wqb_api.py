"""WorldQuant BRAIN 공식 API 클라이언트 (Research Consultant 경로).
계약은 _wqb_pw_worker.py 에서 검증된 형태를 그대로 미러링한다."""
from __future__ import annotations
import hashlib
import json
import logging
import os
import time as _time
import requests
from requests.auth import HTTPBasicAuth

LOG = logging.getLogger('hyfe.wqb_api')
BASE = 'https://api.worldquantbrain.com'

# (connect, read) 타임아웃 — 시뮬 경로 requests 가 WQB 소켓에서 무한 대기하는 것을 차단.
# read=45s 는 정상 응답엔 충분하고, 매달린 연결은 끊어 poll 루프가 전진하게 한다.
_HTTP_TIMEOUT = (10, 45)


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
        self.persona_required = False
        self.persona_url = None

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
        persona = self._extract_persona_url(r, body)
        if persona:
            self._save_pending(persona)
            LOG.warning('WQB persona/biometric required: %s', persona)
            return False
        LOG.warning('authenticate failed: HTTP %s', r.status_code)
        return False

    @staticmethod
    def _extract_persona_url(resp, body):
        try:
            from urllib.parse import urljoin
            if resp.status_code == 401 and (resp.headers.get('WWW-Authenticate') or '').lower() == 'persona':
                loc = resp.headers.get('Location')
                if loc:
                    return urljoin(f'{BASE}/authentication', loc)
            inq = (body or {}).get('inquiry') if isinstance(body, dict) else None
            if inq:
                return f'{BASE}/authentication/persona?inquiry={inq}'
        except Exception:
            pass
        return None

    def _pending_file(self):
        return (self.session_file + '.pending') if self.session_file else None

    def _save_pending(self, persona_url):
        self.persona_url = persona_url; self.persona_required = True  # always signal, even if disabled
        try:
            pf = self._pending_file()
            if not pf:
                return  # persistence disabled — still signal above
            ck = self.session.cookies
            d = ck.get_dict() if hasattr(ck, 'get_dict') else dict(ck)
            os.makedirs(os.path.dirname(pf), mode=0o700, exist_ok=True)
            try:
                os.chmod(os.path.dirname(pf), 0o700)
            except OSError:
                pass
            fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
            with os.fdopen(fd, 'w') as f:
                json.dump({'cookies': d, 'persona_url': persona_url}, f)
        except Exception as e:
            LOG.warning('pending save err: %s', e)

    def complete_persona(self, inquiry=None) -> bool:
        try:
            # backward-compat: if no inquiry passed, try the pending file's saved inquiry/url
            if not inquiry:
                pf = self._pending_file()
                if pf and os.path.exists(pf):
                    with open(pf, 'r') as f:
                        pend = json.load(f)
                    pu = (pend or {}).get('persona_url') or ''
                    if 'inquiry=' in pu:
                        from urllib.parse import urlparse, parse_qs
                        q = parse_qs(urlparse(pu).query)
                        inquiry = (q.get('inquiry') or q.get('inquiry-id') or [None])[0]
            if not inquiry:
                return False
            r = self.session.post(f'{BASE}/authentication/persona', json={'inquiry': inquiry}, timeout=30)
            if r.status_code in (200, 201):
                # finalize succeeded — session is now authenticated; persist its cookies
                self._save_session()
                self._clear_pending()
                self.persona_required = False
                self._authed = True
                return True
            # 403 INQUIRY_INCOMPLETE (biometric not done yet) or other → not authenticated
            LOG.warning('complete_persona finalize HTTP %s', r.status_code)
            return False
        except Exception as e:
            LOG.warning('complete_persona err: %s', e)
            return False

    def _clear_pending(self):
        try:
            pf = self._pending_file()
            if pf and os.path.exists(pf):
                os.remove(pf)
        except OSError:
            pass

    def _ensure_auth(self) -> bool:
        return self._authed or self.authenticate()

    def harvest_alpha(self, alpha_id: str) -> dict | None:
        """GET /alphas/{id} → {metrics, is_status}. _api_harvest_alpha 미러."""
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}', timeout=_HTTP_TIMEOUT)
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
            # value/cutoff 는 브라우저 스크레이퍼와 동일하게 **문자열** 계약을 지킨다.
            # (워커 _short_metric_label/_extract_self_corr_value 가 `.strip()` 호출 → raw float 면 크래시)
            v = ch.get('value'); lim = ch.get('limit')
            item = {'name': nm,
                    'value': '' if v is None else str(v),
                    'cutoff': '' if lim is None else str(lim),
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
            r = self.session.post(f'{BASE}/simulations', json=body, timeout=_HTTP_TIMEOUT)
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
        sleep = sleep or _time.sleep
        # deadline 은 **벽시계**(monotonic) 기준이어야 한다. 루프-카운트 방식은 단일 GET 이
        # 매달리면 영원히 한 바퀴를 못 돌아 deadline 이 도달 불가능해진다(라이브 무한 행 회귀).
        start = _time.monotonic()
        last = {'status': None, 'alpha': None, 'message': None, 'progress': None}
        while _time.monotonic() - start < deadline_s:
            if stop_event is not None and stop_event.is_set():
                self.cancel(progress_url)
                return {'status': 'CANCELLED', 'alpha': None, 'message': '', 'progress': last.get('progress')}
            try:
                r = self.session.get(progress_url, timeout=_HTTP_TIMEOUT)
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
            self.session.delete(progress_url, timeout=_HTTP_TIMEOUT)  # COMPLETE면 400 — 무해
        except Exception:
            pass

    def read_self_correlation(self, alpha_id: str) -> float | None:
        # Task 2 스모크로 경로/키 확정. 일반형: records 의 max.
        if not alpha_id:
            return None
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}/correlations/self', timeout=_HTTP_TIMEOUT)
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
