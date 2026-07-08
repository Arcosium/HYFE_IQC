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
_API_ACCEPT = 'application/json;version=2.0'
_SUBMIT_ALPHA_DEADLINE_S = float(os.environ.get('HYFE_IQC_ALPHA_SUBMIT_DEADLINE_S', '180'))


def _default_session_file(email: str) -> str:
    h = hashlib.sha1((email or '').encode('utf-8')).hexdigest()[:16]
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'wqb_sessions')
    return os.path.join(d, f'{h}.pkl')


def _public_persona_url(api_url: str, session=None) -> str | None:
    """Return the browser-facing Persona URL for a WQB persona API URL.

    반환 계약:
      - public withpersona URL: 해석 성공.
      - api_url 그대로: WQB 가 **확정적으로** 리다이렉트를 주지 않음 (stale challenge).
      - None: 네트워크 등 **일시** 실패 — 호출자는 pending 을 삭제하면 안 된다
        (삭제하면 다음 상태조회가 POST /authentication 을 다시 때려 biometric
        throttle 이 재무장되는 루프로 돌아간다).
    """
    transient = False
    if session is not None:
        try:
            rr = session.get(api_url, timeout=10, allow_redirects=False)
            loc = rr.headers.get('Location')
            if rr.status_code in (301, 302, 303, 307, 308) and loc and 'withpersona.com' in loc:
                return loc
        except Exception:
            transient = True
    try:
        from . import auth as _auth
        resolved = _auth._resolve_persona_url(api_url)
        if _is_public_persona_url(resolved):
            return resolved
    except Exception:
        transient = True
    return None if transient else api_url


def _is_public_persona_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith('https://') and 'withpersona.com' in url


def _extract_inquiry_from_url(url: str) -> str:
    try:
        if 'inquiry=' not in url and 'inquiry-id=' not in url:
            return ''
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        return (q.get('inquiry') or q.get('inquiry-id') or [''])[0]
    except Exception:
        return ''


# 7개 기본 게이트로 인정하는 IS check allowlist. SELF/PROD_CORRELATION 은 제출 전엔
# PENDING 버킷으로 빠지므로 카운트에 안 잡히고, FAIL 로 확정되면 게이트에 반영돼야
# 하므로 core 로 둔다.
_CORE_IS_CHECKS = frozenset({
    'LOW_SHARPE', 'LOW_FITNESS', 'LOW_TURNOVER', 'HIGH_TURNOVER',
    'LOW_SUB_UNIVERSE_SHARPE', 'CONCENTRATED_WEIGHT', 'LOW_2Y_SHARPE',
    'IS_LADDER_SHARPE', 'SELF_CORRELATION', 'PROD_CORRELATION', 'UNITS',
})
_UNKNOWN_CHECKS_SEEN: set[str] = set()


def _is_core_check(name: str) -> bool:
    """Return True for IS checks that count toward the seven basic gates.

    WQB API responses also include bookkeeping/classification checks such as
    HT_* and MATCHES_*; counting them inflates PASS totals and makes RC logs
    disagree with the real acceptance gate. 알려진 core 는 allowlist 로 확정하고,
    처음 보는 이름은 (호환 위해) core 로 세되 이름을 1회 로깅해 임계값 검증
    근거를 남긴다.
    """
    nm = str(name or '').strip().upper()
    if not nm:
        return False
    if nm in _CORE_IS_CHECKS:
        return True
    if nm.startswith('HT_') or nm.startswith('MATCHES_'):
        return False
    if nm not in _UNKNOWN_CHECKS_SEEN:
        _UNKNOWN_CHECKS_SEEN.add(nm)
        LOG.info('unknown IS check name (counted as core): %s', nm)
    return True


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
        self.last_auth_status_code = None
        self.last_auth_body = None

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
        """Return True only when WQB confirms this session is authenticated."""
        try:
            r = self.session.get(f'{BASE}/authentication', timeout=15)
            if not r.ok:
                return False
            body = r.json()
            return isinstance(body, dict) and body.get('user') is not None
        except Exception:
            return False

    def authenticate(self) -> bool:
        # 1) reuse persisted session — no /authentication POST → no biometric
        if self._load_session() and self._session_valid():
            self._authed = True; return True
        # 2) fresh Basic Auth (persona handling added in Task 2)
        # /authentication 호출 (POST — 새 세션/ Persona 요청).
        # WQB API 이 엔드포인트가 410 Gone을 반환할 수 있음:
        #   - 이미 biometric 완료된 계정이 다시 POST 할 때. 이는 "이미 인증됨"을 의미.
        try:
            r = self.session.post(f'{BASE}/authentication', timeout=30)
        except Exception as e:
            LOG.warning('authenticate network err: %s', e); return False
        self.last_auth_status_code = r.status_code
        body = r.json() if (r.headers.get('Content-Type', '').startswith('application/json')) else {}
        self.last_auth_body = body
        
        # 410 Gone is ambiguous: the biometric inquiry may already be finalized,
        # or it may be stale. Only treat it as authenticated after a real session check.
        if r.status_code == 410:
            LOG.info('authenticate 410 Gone — verifying current WQB session')
            if self._session_valid():
                self._save_session()
                self._clear_pending()
                self.persona_required = False
                self._authed = True
                return True
            LOG.warning('authenticate 410 Gone but session verification failed; clearing stale pending persona')
            self._clear_pending()
            self.persona_required = False
            self._authed = False
            return False

        
        if r.status_code in (200, 201):  # success without explicit user body
            self._save_session(); self._authed = True; return True
        persona = self._extract_persona_url(r, body, session=self.session)
        if persona:
            self._save_pending(persona)
            LOG.warning('WQB persona/biometric required: %s', persona)
            return False
        LOG.warning('authenticate failed: HTTP %s', r.status_code)
        return False

    @staticmethod
    def _extract_persona_url(resp, body, session=None):
        url = None
        try:
            from urllib.parse import urljoin
            if resp.status_code == 401 and (resp.headers.get('WWW-Authenticate') or '').lower() == 'persona':
                loc = resp.headers.get('Location')
                if loc:
                    url = urljoin(f'{BASE}/authentication/', loc)
            inq = (body or {}).get('inquiry') if isinstance(body, dict) else None
            if not url and inq:
                url = f'{BASE}/authentication/persona?inquiry={inq}'
        except Exception:
            pass
        return url

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

    def pending_persona(self):
        """저장된 미완료 persona challenge 를 **네트워크 호출 없이** 읽어 반환한다.

        반환: {'persona_url': str, 'inquiry': str} 또는 None.
        상태조회(passive)는 이 메서드만 쓰고 절대 POST /authentication 을 하지 않는다 —
        매 조회마다 POST 하면 WQB biometric throttle 가 영원히 재무장되기 때문이다.
        """
        try:
            pf = self._pending_file()
            if not pf or not os.path.exists(pf):
                return None
            with open(pf, 'r') as f:
                pend = json.load(f)
            cookies = (pend or {}).get('cookies')
            if isinstance(cookies, dict) and cookies:
                self.session.cookies.update(cookies)
            pu = (pend or {}).get('persona_url') or ''
            if not pu:
                return None
            if 'withpersona.com' in pu:
                # Legacy pending files stored the short-lived Persona public URL.
                # Once that link expires we cannot refresh it because the WQB API
                # challenge URL was lost, so discard it and let authenticate()
                # mint a fresh challenge.
                self._clear_pending()
                return None
            public_url = _public_persona_url(pu, session=self.session)
            if public_url is None:
                # 일시 실패(네트워크 등) — pending 을 지우면 다음 조회가 새 challenge
                # POST 를 유발해 throttle 재무장 루프가 된다. challenge 는 유지하고
                # URL 만 빈 값으로 반환 → UI 가 "링크 준비 중" 을 띄우고 재시도한다.
                inquiry = _extract_inquiry_from_url(pu)
                return {'persona_url': '', 'inquiry': inquiry}
            if not _is_public_persona_url(public_url):
                # Never expose the WQB API challenge URL to the browser. Opening
                # it directly shows the "Details:Gone" white page for stale or
                # already-finalized inquiries; dropping it lets the status
                # endpoint mint a fresh challenge.
                self._clear_pending()
                return None
            inquiry = _extract_inquiry_from_url(pu) or _extract_inquiry_from_url(public_url)
            return {'persona_url': public_url, 'inquiry': inquiry}
        except Exception as e:
            LOG.warning('pending_persona read err: %s', e)
            return None

    def complete_persona(self, inquiry=None) -> bool:
        try:
            # Always restore the cookies from the challenge-creating request.
            # WQB binds Persona finalization to that session even when the
            # browser sends the inquiry back explicitly.
            pf = self._pending_file()
            if pf and os.path.exists(pf):
                with open(pf, 'r') as f:
                    pend = json.load(f)
                cookies = (pend or {}).get('cookies')
                if isinstance(cookies, dict) and cookies:
                    self.session.cookies.update(cookies)
                pu = (pend or {}).get('persona_url') or ''
                if not inquiry:
                    inquiry = _extract_inquiry_from_url(pu) or None
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
            if r.status_code == 410:
                # WQB returns 410 Gone for finalized or expired inquiries. Success still
                # requires the same session to pass GET /authentication.
                LOG.info('complete_persona 410 Gone — verifying current WQB session')
                if self._session_valid():
                    self._save_session()
                    self.persona_required = False
                    self._clear_pending()
                    self._authed = True
                    return True
                LOG.warning('complete_persona 410 Gone but session verification failed; clearing stale pending persona')
                self.persona_required = False
                self._authed = False
                self._clear_pending()
                return False
            # 403 INQUIRY_INCOMPLETE (biometric not done yet）or other → not authenticated
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
            r = self.session.get(f'{BASE}/alphas/{alpha_id}', timeout=_HTTP_TIMEOUT,
                                 headers={'Accept': _API_ACCEPT})
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
            if not _is_core_check(nm):
                continue
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

    def submit_alpha(self, alpha_id: str, stop_event=None,
                     deadline_s: float | None = None) -> tuple[bool, str]:
        """Submit an alpha using the official UI/API contract.

        The BRAIN frontend POSTs /alphas/{id}/submit first. A 2xx response with
        Retry-After is not final; it polls the same endpoint with GET until the
        header disappears, then reads the JSON body.

        429 는 계정당 제출이 한 번에 하나라는 슬롯 신호일 수 있으므로 즉시 포기하지
        않고 Retry-After(없으면 지수 백오프)를 존중하며 deadline 안에서 재시도한다.
        stop_event 가 set 되면 폴링/재시도를 즉시 중단한다 (pause 반응성).
        """
        if not alpha_id:
            return False, 'submit_error:missing_alpha_id'
        if not self._ensure_auth():
            return False, 'submit_error:not_authenticated'

        deadline = _SUBMIT_ALPHA_DEADLINE_S if deadline_s is None else float(deadline_s)
        url = f'{BASE}/alphas/{alpha_id}/submit'
        method = 'POST'
        start = _time.monotonic()
        headers = {'Accept': _API_ACCEPT}
        backoff_429 = 5.0
        while True:
            if stop_event is not None and stop_event.is_set():
                return False, 'submit_skipped:paused'
            try:
                if method == 'POST':
                    r = self.session.post(url, headers=headers, timeout=_HTTP_TIMEOUT)
                else:
                    r = self.session.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
            except Exception as e:
                LOG.warning('alpha submit network err: %s', e)
                return False, f'submit_error:{e}'

            retry_after = r.headers.get('Retry-After') or r.headers.get('retry-after')
            if 200 <= r.status_code < 300 and retry_after:
                if _time.monotonic() - start >= deadline:
                    return False, f'submit_pending_timeout:{retry_after}'
                try:
                    sleep_s = max(0.5, min(30.0, float(retry_after)))
                except (TypeError, ValueError):
                    sleep_s = 3.0
                _time.sleep(sleep_s)
                method = 'GET'
                continue

            if 200 <= r.status_code < 300:
                try:
                    body = r.json()
                except Exception:
                    body = None
                checks = []
                if isinstance(body, dict):
                    checks = (((body.get('is') or {}).get('checks')) or [])
                failed = [c for c in checks if str(c.get('result') or '').upper() == 'FAIL']
                if failed:
                    reason = '; '.join(str(c.get('name') or c.get('result') or 'FAIL') for c in failed[:3])
                    return False, f'rejected:{reason}'
                return True, 'submitted'

            if r.status_code == 429:
                # 제출 슬롯 대기 — deadline 안에서 인내심 재시도. POST 가 아직 접수되지
                # 않은 상태이므로 method 는 그대로 유지한다 (POST 면 POST 재시도).
                if _time.monotonic() - start >= deadline:
                    return False, 'submit_http_429: too_many_requests'
                try:
                    sleep_s = max(1.0, min(60.0, float(retry_after))) if retry_after else backoff_429
                except (TypeError, ValueError):
                    sleep_s = backoff_429
                backoff_429 = min(backoff_429 * 1.5, 45.0)
                _time.sleep(sleep_s)
                continue
            # WQB 는 제출 체크 미달 알파의 submit 을 403 + is.checks JSON 으로 거절한다
            # (2026-07-03 R84 라이브 관찰). raw JSON 을 그대로 저장하면 UI 가 지저분하고
            # _is_best_alpha 의 rejected 분기도 못 탄다 — FAIL 체크명으로 분류한다.
            try:
                body_j = r.json()
            except Exception:
                body_j = None
            if isinstance(body_j, dict):
                checks = (((body_j.get('is') or {}).get('checks')) or [])
                failed = [c for c in checks if str(c.get('result') or '').upper() == 'FAIL']
                if failed:
                    reason = '; '.join(str(c.get('name') or 'FAIL') for c in failed[:3])
                    return False, f'rejected:{reason} (http_{r.status_code})'
            body = (getattr(r, 'text', '') or '')[:200].replace(chr(10), ' ').strip()
            suffix = f': {body}' if body else ''
            return False, f'submit_http_{r.status_code}{suffix}'

    def submit_simulation(self, expr: str, settings: dict) -> str | None:
        if not self._ensure_auth():
            return None
        body = {'type': 'REGULAR', 'settings': self._full_settings(settings), 'regular': expr}
        try:
            r = self.session.post(
                f'{BASE}/simulations', json=body, timeout=_HTTP_TIMEOUT,
                headers={'Content-Type': 'application/json',
                         'Access-Control-Request-Headers': 'Location',
                         'Accept': _API_ACCEPT})
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
            'unitHandling': s.get('unitHandling', s.get('unit_handling', 'VERIFY')),
            'nanHandling': s.get('nanHandling', s.get('nan_handling', 'OFF')),
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
                r = self.session.get(progress_url, timeout=_HTTP_TIMEOUT,
                                     headers={'Accept': _API_ACCEPT})
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
            self.session.delete(progress_url, timeout=_HTTP_TIMEOUT,
                                headers={'Accept': _API_ACCEPT})  # COMPLETE면 400 — 무해
        except Exception:
            pass

    def read_self_correlation(self, alpha_id: str, deadline_s: float = 60.0) -> float | None:
        # Task 2 스모크로 경로/키 확정. 일반형: records 의 max.
        # 갓 완료된 알파는 계산 중이라 200 + Retry-After(본문 없음)를 주므로
        # 헤더가 사라질 때까지 deadline 안에서 짧게 폴링한다.
        if not alpha_id:
            return None
        start = _time.monotonic()
        while True:
            try:
                r = self.session.get(f'{BASE}/alphas/{alpha_id}/correlations/self',
                                     timeout=_HTTP_TIMEOUT,
                                     headers={'Accept': _API_ACCEPT})
            except Exception:
                return None
            if not r.ok:
                return None
            retry_after = r.headers.get('Retry-After') or r.headers.get('retry-after')
            if retry_after and _time.monotonic() - start < deadline_s:
                try:
                    sleep_s = max(0.5, min(15.0, float(retry_after)))
                except (TypeError, ValueError):
                    sleep_s = 3.0
                _time.sleep(sleep_s)
                continue
            try:
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
