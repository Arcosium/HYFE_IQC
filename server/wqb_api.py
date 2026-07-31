"""WorldQuant BRAIN 공식 API 클라이언트.

인증은 이메일+비밀번호 HTTP Basic → JWT 쿠키('t'). **별도 API 키는 없다.**
(RC 전용이 아니다 — 계정 능력은 auth.probe_wqb_backend 가 실측한다.)
시뮬 백엔드는 이 API 단일이다 (2026-07-13 Playwright/브라우저 경로 제거).
"""
from __future__ import annotations
import base64
import hashlib
import json
import logging
import os
import threading
import time as _time
import requests
from requests.auth import HTTPBasicAuth

from . import criteria as _criteria

LOG = logging.getLogger('genomicwqb.wqb_api')
BASE = 'https://api.worldquantbrain.com'

# (connect, read) 타임아웃 — 시뮬 경로 requests 가 WQB 소켓에서 무한 대기하는 것을 차단.
# read=45s 는 정상 응답엔 충분하고, 매달린 연결은 끊어 poll 루프가 전진하게 한다.
_HTTP_TIMEOUT = (10, 45)
_API_ACCEPT = 'application/json;version=2.0'
# 180 → 480 (2026-07-23): 제출 확인은 SELF/PROD correlation 계산을 기다리는 구간이라
# 3분을 자주 넘긴다. 7/23 라이브에서 전 체크 PASS 알파(Sharpe 1.59)가 180초 타임아웃으로
# 유실됐다. 제출은 하루 4건뿐이라 스레드가 몇 분 더 기다리는 비용은 무시할 만하다.
_SUBMIT_ALPHA_DEADLINE_S = float(os.environ.get('HYFE_IQC_ALPHA_SUBMIT_DEADLINE_S', '480'))
# 일일 시뮬 쿼터 잔여가 이 밑이면 경고 로그 (WQB 플랫폼도 1000 에서 경고한다).
_SIM_QUOTA_WARN = float(os.environ.get('IQC_SIM_QUOTA_WARN', '1000'))

# ── 시뮬 폴링 (2026-07-21 재조정) ────────────────────────────────────────────
# 라이브 에러의 75%(400건 중 299건)가 'sim TIMEOUT: poll deadline' 이었다 — WQB 가
# 실패시킨 게 아니라 우리가 720초에 포기한 것이다. D0·TOP3000·10년 IS 시뮬은 대기열까지
# 포함하면 12분을 넘기는 게 정상이라, 마감을 올리고 정체 감지로 보완한다.
#
# 2026-07-28 재조정 30분 → 60분. 실측(r5 ph3): 라운드 꼬리에 접수된 4개가 **30분 내내
# progress=None**(= WQB 대기열에서 시작조차 안 됨) 이다 마감에 걸려 통째로 버려졌고,
# 그 한 판 처리량이 16.8 → 8.8 완료/시간으로 반토막 났다. 대기열에 오래 앉는 건
# 고장이 아니다 — 진짜 멈춘 시뮬은 아래 _POLL_STALL_S 가 따로 잡는다. 기다리는 스레드는
# 어차피 자기 후보 말고 할 일이 없으니, 포기해서 후보를 버리는 것보다 기다리는 게 싸다.
_POLL_DEADLINE_S = float(os.environ.get('IQC_SIM_POLL_DEADLINE_S', '3600'))
# 진행이 이 시간 동안 전혀 없으면 마감 전이라도 포기(슬롯 반환). 대기열에서 오래 머무는
# 것과 진짜 멈춘 것을 구분하기 위한 값이라 마감의 절반 정도로 넉넉히 잡는다.
_POLL_STALL_S = float(os.environ.get('IQC_SIM_POLL_STALL_S', '900'))
# Retry-After 가 없을 때의 기본 간격. 있으면 그쪽이 이긴다(BRAIN API 문서의 계약).
_POLL_INTERVAL_S = float(os.environ.get('IQC_SIM_POLL_INTERVAL_S', '5'))
_POLL_RETRY_AFTER_MAX_S = 60.0
# 꼬리 절단(abort_event)을 존중하기 전 최소 폴링 시간. 긴 슬롯 대기(수십 분) 끝에
# 접수된 시뮬은 tail_event 가 이미 켜진 채 폴링을 시작한다 — 유예 없이는 progress 를
# 한 번 읽어 보기도 전에 갓 접수된 시뮬을 즉시 버리게 된다(2026-07-31).
_TAIL_MIN_POLL_S = float(os.environ.get('IQC_SIM_TAIL_MIN_POLL_S', '600'))


def _clamp_retry_after(v: float, floor_s: float) -> float:
    """Retry-After 를 [floor, 60s] 로 clamp. 서버가 0/음수/과대값을 줘도 폴링이
    폭주하거나 멈추지 않게 한다."""
    if v <= 0:
        return floor_s
    return max(floor_s, min(v, _POLL_RETRY_AFTER_MAX_S))

# 선제갱신 임계: 토큰 만료까지 남은 시간이 이보다 크면 세션을 신뢰하고 네트워크 검증을
# 건너뛴다(fast-path). 15분 여유는 한 라운드 시뮬이 만료를 넘겨 401 로 실패하는 것을 막는다.
_REAUTH_THRESHOLD_S = float(os.environ.get('HYFE_IQC_REAUTH_THRESHOLD_S', str(15 * 60)))
# 상대(초 잔여) vs 절대(epoch) 만료값 구분 임계 — 10년(초). 이보다 작으면 '지금+잔여초'.
_REL_EXPIRY_CUTOFF = 3.15e8

# 미완료 persona challenge 를 '살아있다'고 보는 기간. 이 안에서는 authenticate() 가 새
# challenge 를 발급하지 않고 기존 것을 재사용한다 — POST /authentication 은 매번 새 inquiry 를
# 만들고 WQB 는 직전 inquiry 를 폐기하므로, 사용자가 그 링크로 인증하는 도중이면 페이지가 죽는다.
_PERSONA_PENDING_TTL_S = float(os.environ.get('IQC_PERSONA_PENDING_TTL_S', str(30 * 60)))

# ── 선제 갱신 (바이오 인증 회피의 핵심) ───────────────────────────────────────
# WQB 의 JWT 는 발급 후 **정확히 4시간** 살고(실측), 클레임에 amr:['pwd','face'] 가 박힌다.
# 지금까지는 토큰이 **죽은 뒤에야** 재인증을 시도했다 — 죽은 세션에서 Basic 으로 새로
# 로그인하면 WQB 는 당연히 얼굴 인증(Persona)을 다시 요구한다. 즉 기존 설계는 4시간마다
# 최악의 경로를 밟도록 보장돼 있었다.
#
# 가설: **아직 살아있는 세션**으로 재인증하면(쿠키를 지우지 않은 채 POST /authentication)
# WQB 는 이미 face 검증된 세션임을 알고 새 토큰을 그냥 내준다. 그렇다면 만료 전에 계속
# 갱신하는 것만으로 얼굴 인증이 사실상 0회가 된다.
# 이 가설이 틀리면(=persona 가 또 오면) 손해는 없다: 살아있는 쿠키를 지우지 않으므로
# 세션은 그대로 유지되고, 어차피 만료 때 해야 했을 인증을 조금 일찍 알게 될 뿐이다.
# 토큰 1개당 갱신 시도는 **1회로 제한**한다(BIOMETRICS_THROTTLED 재무장 방지).
_REFRESH_LEAD_S = float(os.environ.get('IQC_REFRESH_LEAD_S', str(30 * 60)))
SESSION_KEEPALIVE = os.environ.get('IQC_SESSION_KEEPALIVE', '1') != '0'


def _jwt_claims(token: str) -> dict:
    """JWT payload 를 검증 없이 디코드 (만료·인증수단 확인용, 네트워크 0회).

    서명 검증은 하지 않는다 — 우리는 토큰을 신뢰할지 판단하는 게 아니라 **언제 죽는지**
    알고 싶을 뿐이고, 그 값이 틀려도 최악은 불필요한 재검증 1회다.
    """
    try:
        seg = str(token or '').split('.')[1]
        pad = seg + '=' * (-len(seg) % 4)
        obj = json.loads(base64.urlsafe_b64decode(pad))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}

# challenge 발급(POST /authentication)을 계정별로 직렬화한다. 데이터 새로고침 스레드와
# 워커가 동시에 인증하면 2초 사이에 inquiry 가 두 번 갈려 먼저 것이 즉시 무효가 된다
# (실측 2026-07-10 08:53:08/10, 15:18:06/08).
_MINT_LOCKS: dict[str, threading.Lock] = {}
_MINT_LOCKS_GUARD = threading.Lock()


def _mint_lock(session_file) -> threading.Lock:
    key = session_file or '<no-persist>'
    with _MINT_LOCKS_GUARD:
        lk = _MINT_LOCKS.get(key)
        if lk is None:
            lk = _MINT_LOCKS[key] = threading.Lock()
        return lk


def _default_session_file(email: str) -> str:
    h = hashlib.sha1((email or '').encode('utf-8')).hexdigest()[:16]
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'wqb_sessions')
    return os.path.join(d, f'{h}.pkl')


def _public_persona_url(api_url: str, session=None) -> str | None:
    """Return the browser-facing Persona URL for a WQB persona API URL.

    ⚠ **부작용이 있다 — 폴링 경로에서 부르지 말 것.** `GET /authentication/persona?inquiry=…`
    는 단순 조회가 아니라 Persona inquiry 를 **재개(resume)** 시켜 새 hosted-flow 세션을
    발급받는 호출이고, WQB/Persona 는 그 순간 **직전 세션을 무효화**한다. 사용자가 열어 둔
    인증 페이지가 그 자리에서 죽는다(무한 새로고침 → 'session expired'). 사용자가 링크를
    요청하는 바로 그 순간에만 호출하라 (`/api/account/wqb-persona-link`).

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


def _parse_expiry(v):
    """WQB token.expiry 를 epoch(초)로 관대하게 파싱. 형식 편차 방어:
      - 숫자 & < 10년(초)  → '지금+잔여초'(상대)로 해석
      - 숫자 & >= 컷오프    → 절대 epoch 로 해석
      - ISO8601 문자열      → timestamp
    파싱 불가/비정상은 None(→ 만료 미상, fast-path 미적용, OPTIONS 검증 유지)."""
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            f = float(v)
            if f <= 0:
                return None
            return (_time.time() + f) if f < _REL_EXPIRY_CUTOFF else f
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            try:
                f = float(s)
                if f <= 0:
                    return None
                return (_time.time() + f) if f < _REL_EXPIRY_CUTOFF else f
            except ValueError:
                pass
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
            return dt.timestamp()
    except Exception:
        return None
    return None


def _extract_inquiry_from_url(url: str) -> str:
    try:
        if 'inquiry=' not in url and 'inquiry-id=' not in url:
            return ''
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(url).query)
        return (q.get('inquiry') or q.get('inquiry-id') or [''])[0]
    except Exception:
        return ''


# 차단 게이트로 인정하는 IS check — 단일 진실은 criteria.BLOCKING_CHECKS 다.
# SELF/PROD_CORRELATION 은 제출 전엔 PENDING 버킷으로 빠지므로 카운트에 안 잡히고,
# FAIL 로 확정되면 게이트에 반영돼야 하므로 core 로 둔다.
# ⚠ 2026-07-21: HT_*/MATCHES_*/CLUSTER_TEST/OSMOSIS_ALLOCATION 은 **분류 전용**이라
#   차단하지 않는다(문서 "Failing the Cluster Test does not block submission").
_UNKNOWN_CHECKS_SEEN: set[str] = set()

# IS check 이름 → metrics 키. 이 체크들의 value/cutoff 는 `is` 요약 블록에 없어서
# harvest_alpha 가 따로 승격해 주지 않으면 reward/selection 이 영원히 못 본다.
# (2026-07 개편 후엔 HT_* 지표가 제출 가능성의 1차 결정 변수다 — criteria 참조.)
_CHECK_METRIC_KEY = _criteria.CHECK_METRIC_KEY


def _parse_check_number(v):
    """IS check 의 value/limit → float|None. WQB 는 숫자를 float 로 주지만
    문자열/None/빈값도 방어한다 (계약이 흔들려도 metrics 승격이 죽지 않게)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _harvest_classification(nm_u: str, ch: dict, res: str, out: dict) -> None:
    """분류/배수 체크의 **구조화된 필드**를 metrics 로 승격한다 (value/limit 밖에 있다).

    - MATCHES_PYRAMID: {'effective': 2, 'multiplier': 1.6, 'pyramids': [...]}
    - MATCHES_THEMES : {'themes': [{'name','multiplier'}, ...]} — result=PASS 일 때만
      실제로 매칭된 것이다(WARNING = "these themes do not match").
    - MATCHES_CLASSIFICATION: {'value': ['High Turnover']}
    복수 테마 동시 매칭 배수는 criteria.combine_theme_multipliers (sum−count+1).
    """
    try:
        if nm_u == 'MATCHES_PYRAMID':
            mult = _parse_check_number(ch.get('multiplier'))
            if mult is not None:
                out['pyramid_multiplier'] = str(mult)
            eff = _parse_check_number(ch.get('effective'))
            if eff is not None:
                out['pyramid_effective'] = str(eff)
            names = [str(p.get('name')) for p in (ch.get('pyramids') or [])
                     if isinstance(p, dict) and p.get('name')]
            if names:
                out['pyramids'] = ','.join(names)
        elif nm_u == 'MATCHES_THEMES':
            themes = [t for t in (ch.get('themes') or []) if isinstance(t, dict)]
            names = [str(t.get('name')) for t in themes if t.get('name')]
            if names:
                out['themes' if res == 'PASS' else 'themes_unmatched'] = ','.join(names)
            if res == 'PASS':
                out['theme_multiplier'] = str(_criteria.combine_theme_multipliers(
                    [t.get('multiplier') for t in themes]))
        elif nm_u == 'MATCHES_CLASSIFICATION':
            v = ch.get('value')
            if isinstance(v, list) and v:
                out['classifications'] = ','.join(str(x) for x in v)
        elif nm_u == 'HT_ORTHOGONAL_RAM_NEUTRALIZATION':
            if res == 'PASS':
                out['ht_ram_ok'] = '1'
    except Exception:   # 승격 실패가 수확 전체를 죽이면 안 된다
        pass


def _check_desc(name: str, value, limit, result: str) -> str:
    """IS 체크 1건 → '<이름> of <값> is (below|above) cutoff of <컷> (RESULT)'.

    ⚠ **어법이 계약이다.** directed_mutation._RX 는 이 문장형만 읽는다
    (`'... of <값> is (below|above) cutoff of <컷>'`, 브라우저 스크레이퍼 시절 문구).
    REST 경로가 'LOW_SHARPE: FAIL (value=…, limit=…)' 로 적는 동안 정향변이는
    sharpe/fitness/turnover 실패를 **하나도 인식하지 못하고** generic 으로 떨어졌다
    (2026-07-30). 숫자가 아니면 옛 표기로 폴백한다.
    """
    try:
        v, lim = float(value), float(limit)
    except (TypeError, ValueError):
        return f'{name}: {result} (value={value}, limit={limit})'
    where = 'below' if v < lim else 'above'
    return f'{name} of {v} is {where} cutoff of {lim} ({result})'


def _is_core_check(name: str) -> bool:
    """Return True for IS checks that gate submission (FAIL = 제출 차단).

    WQB API responses also include bookkeeping/classification checks such as
    HT_*, MATCHES_*, CLUSTER_TEST, OSMOSIS_ALLOCATION; counting them inflates
    PASS totals and makes RC logs disagree with the real acceptance gate.
    처음 보는 이름은 (안전하게) core 로 세되 이름을 1회 로깅해 검증 근거를 남긴다.
    """
    nm = str(name or '').strip().upper()
    if not nm:
        return False
    if nm in _criteria.BLOCKING_CHECKS:
        return True
    if not _criteria.is_blocking(nm):
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
        # 429(동시 시뮬 한도) 응답을 프로세스당 한 번만 통째로 남긴다. **한도가 몇 개인지는
        # 공개 문서에 없고**(2026-07-28 확인: IQC 가이드라인·컨설턴트 페이지 모두 무언급),
        # WQB 가 이 응답에서 직접 말해 준다 — 여태 body 를 안 읽고 버렸다.
        self._rl_body_logged = False
        self.persona_required = False
        self.persona_url = None
        self.last_auth_status_code = None
        self.last_auth_body = None
        # 토큰 만료 epoch(초). None=미상. 선제갱신 fast-path 와 사이드카(.meta) 로 캐시.
        self._expiry_epoch = None
        # 직전 _session_valid 가 OPTIONS(프로덕션 경로)로 유효 판정했는지 — 만료 미상일 때만
        # 1회 GET 로 만료를 학습(_ensure_expiry)하기 위한 플래그. GET-폴백 경로는 이미 만료를
        # 그 응답 body 에서 잡으므로 중복 GET 을 하지 않는다.
        self._valid_via_options = False

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

    # ── 만료(token.expiry) 캐시 사이드카 ─────────────────────────────────────
    def _meta_file(self):
        return (self.session_file + '.meta') if self.session_file else None

    def _save_meta(self, *, refresh_attempt_for: float | None = ...):
        """만료 사이드카 저장. refresh_attempt_for 를 넘기면 그 값도 함께 기록/삭제한다
        (기본값 ... = '건드리지 않음' — None 은 '지운다' 는 뜻이라 구분해야 한다)."""
        try:
            mf = self._meta_file()
            if not mf:
                return
            d = {}
            if os.path.exists(mf):
                try:
                    with open(mf, 'r') as f:
                        d = json.load(f) or {}
                except Exception:
                    d = {}
            if not isinstance(d, dict):
                d = {}
            if self._expiry_epoch is not None:
                d['expiry'] = self._expiry_epoch
            if refresh_attempt_for is not ...:
                if refresh_attempt_for is None:
                    d.pop('refresh_attempt_for', None)
                else:
                    d['refresh_attempt_for'] = float(refresh_attempt_for)
            d['written_at'] = _time.time()
            if not d:
                return
            os.makedirs(os.path.dirname(mf), mode=0o700, exist_ok=True)
            fd = os.open(mf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as f:
                json.dump(d, f)
        except Exception as e:
            LOG.warning('meta save err: %s', e)

    def _load_meta(self):
        try:
            mf = self._meta_file()
            if not mf or not os.path.exists(mf):
                return
            with open(mf, 'r') as f:
                d = json.load(f)
            exp = _parse_expiry((d or {}).get('expiry')) if isinstance(d, dict) else None
            if exp is not None:
                self._expiry_epoch = exp
        except Exception:
            pass

    # ── 만료 시각의 진실은 JWT 안에 있다 ────────────────────────────────────
    # 과거엔 응답 body 의 token.expiry 만 캐시했는데, complete_persona(얼굴 인증 완료)
    # 경로가 그걸 캡처하지 않아 .meta 가 **3일째 옛 만료값에 얼어붙어 있었다**(실측).
    # 그 결과 클라이언트는 자기 토큰이 언제 죽는지 몰랐고, 선제 갱신 자체가 불가능했다.
    # 쿠키 't' 는 JWT 라 exp 클레임을 네트워크 0회로 읽을 수 있다 — 이게 1차 진실이다.
    def _expiry_from_jwt(self) -> float | None:
        try:
            ck = self.session.cookies
            tok = (ck.get_dict() if hasattr(ck, 'get_dict') else dict(ck)).get('t')
            exp = _jwt_claims(tok).get('exp')
            return float(exp) if isinstance(exp, (int, float)) else None
        except Exception:
            return None

    def auth_methods(self) -> list:
        """현재 토큰이 어떤 수단으로 발급됐는지 (예: ['pwd','face']). 진단용."""
        try:
            ck = self.session.cookies
            tok = (ck.get_dict() if hasattr(ck, 'get_dict') else dict(ck)).get('t')
            return list(_jwt_claims(tok).get('amr') or [])
        except Exception:
            return []

    def _capture_expiry(self, body=None):
        """만료 캐시 갱신: JWT(권위) → 없으면 응답 body 의 token.expiry(폴백)."""
        exp = self._expiry_from_jwt()
        if exp is None:
            try:
                tok = (body or {}).get('token') if isinstance(body, dict) else None
                exp = _parse_expiry((tok or {}).get('expiry')) if isinstance(tok, dict) else None
            except Exception:
                exp = None
        if exp is not None:
            self._expiry_epoch = exp
            self._save_meta()

    def _not_near_expiry(self) -> bool:
        return (self._expiry_epoch is not None
                and (self._expiry_epoch - _time.time()) > _REAUTH_THRESHOLD_S)

    def _expiry_stale(self) -> bool:
        """캐시된 만료가 이미 지났는가 — 지났으면 다시 학습해야 한다.
        (과거엔 이 판정이 없어 '이미 만료된 값' 을 영원히 재사용했다.)"""
        return (self._expiry_epoch is not None
                and self._expiry_epoch <= _time.time() + 60)

    def seconds_to_expiry(self) -> float | None:
        exp = self._expiry_epoch if self._expiry_epoch is not None else self._expiry_from_jwt()
        return None if exp is None else (exp - _time.time())

    def _ensure_expiry(self, force: bool = False):
        """만료를 학습한다. 우선 JWT 에서(네트워크 0회), 그래도 모르면 GET 1회.

        GET /authentication 은 POST 와 달리 challenge 를 새로 만들지 않는다 → biometric 무관.
        force=True 는 '캐시를 믿지 말고 다시 학습' 이라는 뜻이지 'JWT 를 건너뛰라' 가 아니다.
        JWT 는 언제나 가장 싼 진실이므로 force 여부와 무관하게 먼저 본다.
        best-effort: 실패해도 무시.
        """
        exp = self._expiry_from_jwt()
        if exp is not None:
            self._expiry_epoch = exp
            self._save_meta()
            return
        if not force and self._expiry_epoch is not None and not self._expiry_stale():
            return
        try:
            r = self.session.get(f'{BASE}/authentication', timeout=15)
            if getattr(r, 'ok', False):
                self._capture_expiry(r.json())
        except Exception:
            pass

    def _session_valid(self) -> bool:
        """세션 유효성 판정. 우선 OPTIONS /simulations 로 **인증 엔드포인트를 건드리지 않고**
        확인한다(2xx=유효, 401=만료). Persona/biometric throttle 재무장 위험이 없다.
        OPTIONS 미지원/애매(그 외 코드)면 GET /authentication 으로 폴백하고, 그 김에
        token.expiry 를 캐시한다."""
        self._valid_via_options = False
        try:
            r = self.session.options(f'{BASE}/simulations', timeout=15)
            code = getattr(r, 'status_code', None)
            if code is not None:
                if 200 <= code < 300:
                    self._valid_via_options = True
                    return True
                if code == 401:
                    return False
                # 그 외(403/5xx 등)는 애매 → GET 폴백으로 확정
        except Exception:
            pass  # options 미지원/일시오류 → GET 폴백
        try:
            r = self.session.get(f'{BASE}/authentication', timeout=15)
            if not r.ok:
                return False
            body = r.json()
            if not isinstance(body, dict):
                return False
            self._capture_expiry(body)
            return body.get('user') is not None
        except Exception:
            return False

    def authenticate(self) -> bool:
        # 0) 선제갱신 fast-path: 인메모리로 이미 인증됐고 만료까지 여유가 크면 네트워크 0.
        #    (클라이언트가 사이클간 재사용될 때 OPTIONS 호출조차 건너뛴다.)
        if self._authed and self._not_near_expiry():
            return True
        # 1) reuse persisted session — OPTIONS 검증(인증 엔드포인트 무접촉 → no biometric).
        #    만료는 JWT 에서 직접 읽는다(네트워크 0회, .meta 가 썩어도 무관).
        if self._load_session():
            self._load_meta()
            if self._session_valid():
                self._authed = True
                if self._expiry_epoch is None or self._expiry_stale():
                    self._ensure_expiry()
                return True
        # 2) fresh Basic Auth — 여기 오면 세션이 완전히 죽은 것이고, WQB 는 이 시점에
        #    얼굴 인증(Persona)을 요구한다. session_keeper 의 선제 갱신이 제대로 돌면
        #    정상 운영 중에는 이 경로에 오지 않는 것이 목표다.
        # 계정별 락 — 두 스레드가 동시에 challenge 를 발급하면 뒤엣것이 앞엣것을 폐기한다.
        with _mint_lock(self.session_file):
            return self._authenticate_locked()

    # ── 선제 갱신: 살아있는 세션으로 토큰을 연장한다 ─────────────────────────
    def refresh_token(self) -> str:
        """만료 전에 토큰을 갱신한다. 반환: refreshed|persona|invalid|skipped|error.

        `_authenticate_locked` 와의 결정적 차이는 **쿠키를 지우지 않는다**는 것이다.
        그쪽은 _clear_session_cookies() 로 세션을 버리고 맨몸 Basic 로그인을 하는데,
        WQB 입장에선 그게 '새 기기의 첫 로그인' 이라 얼굴 인증을 요구한다.
        여기서는 face 로 이미 검증된 살아있는 세션을 그대로 들고 재인증을 청한다.

        안전장치:
          - 살아있는 challenge 가 있으면 손대지 않는다(사용자가 인증 중일 수 있다).
          - 토큰 1개당 1회만 시도한다(.meta 에 기록) → /authentication 연타로 인한
            BIOMETRICS_THROTTLED 재무장이 원천 불가능.
          - persona 가 오더라도 **살아있는 쿠키를 지우지 않는다** → 워커는 만료까지
            하던 일을 계속한다. 손해가 없다.
        """
        if not SESSION_KEEPALIVE:
            return 'skipped'
        with _mint_lock(self.session_file):
            if self._read_pending() is not None:
                return 'skipped'          # 인증 진행 중 — 건드리면 그 페이지가 죽는다
            if self._refresh_attempted_for_current_token():
                return 'skipped'          # 이 토큰으로는 이미 시도했다
            self._mark_refresh_attempt()
            try:
                # 쿠키 유지! (Basic 자격증명은 session.auth 에 그대로 붙어 있다)
                r = self.session.post(f'{BASE}/authentication',
                                      headers={'Accept': _API_ACCEPT},
                                      timeout=_HTTP_TIMEOUT)
            except Exception as e:
                LOG.warning('refresh 실패(네트워크): %s', e)
                return 'error'
            body = {}
            try:
                body = r.json()
            except Exception:
                body = {}
            code = getattr(r, 'status_code', 0)
            if code in (200, 201):
                self._save_session()
                self._capture_expiry(body)
                self._clear_refresh_attempt()   # 새 토큰 → 갱신 시도 카운터 리셋
                self._authed = True
                left = self.seconds_to_expiry()
                LOG.info('토큰 선제 갱신 성공 — 만료까지 %s분, amr=%s',
                         int((left or 0) / 60), self.auth_methods())
                return 'refreshed'
            if code == 410:
                # '이미 인증됨' 애매 응답 — 세션이 실제로 유효한지로 확정한다.
                self._ensure_expiry(force=True)
                return 'refreshed' if not self._expiry_stale() else 'invalid'
            purl = self._extract_persona_url(r, body)
            if purl:
                # 가설이 이 계정/상태에선 틀렸다 — WQB 가 갱신에도 얼굴을 요구한다.
                # ⚠ 살아있는 쿠키를 절대 지우지 않는다. 세션은 만료까지 그대로 쓴다.
                # ⚠ 이 challenge 는 **알림용 마커로만** 저장한다(source='refresh', cookies={}).
                #   예전엔 세션 jar 통째(JWT 포함)로 저장했는데, 그 challenge 를 해석하면
                #   죽은 hosted 세션이 나와 사용자가 열 때마다 'session expired' 가 떴다
                #   (2026-07-16 사장 보고). 실제 인증용 challenge 는 사용자가 링크를 누르는
                #   순간 mint_challenge() 가 만료-후 경로와 동일하게 깨끗이 발급한다.
                self._save_pending(purl, cookies={}, source='refresh')
                LOG.info('선제 갱신에도 persona 요구 — 세션은 만료까지 유지, 사용자 인증 필요')
                return 'persona'
            LOG.info('refresh 응답 HTTP %s — 갱신 불가', code)
            return 'invalid'

    # 토큰별 갱신 시도 기록 — .meta 에 넣어 프로세스 재시작에도 살아남는다.
    def _refresh_attempted_for_current_token(self) -> bool:
        try:
            mf = self._meta_file()
            if not mf or not os.path.exists(mf):
                return False
            with open(mf, 'r') as f:
                d = json.load(f)
            exp = self._expiry_from_jwt() or self._expiry_epoch
            # 같은 토큰(=같은 만료시각)에 대한 시도만 유효하다.
            return (isinstance(d, dict) and d.get('refresh_attempt_for') is not None
                    and exp is not None
                    and abs(float(d['refresh_attempt_for']) - float(exp)) < 1.0)
        except Exception:
            return False

    def _mark_refresh_attempt(self) -> None:
        exp = self._expiry_from_jwt() or self._expiry_epoch
        if exp is not None:
            self._save_meta(refresh_attempt_for=exp)

    def _clear_refresh_attempt(self) -> None:
        self._save_meta(refresh_attempt_for=None)

    def mint_challenge(self) -> bool:
        """세션 유효 여부와 **무관하게** 깨끗한 persona challenge 를 지금 발급한다.

        용도 = 만료 전 선인증: authenticate() 는 세션이 살아 있으면 발급 없이 True 를
        반환하므로 만료 전에 정상 challenge 를 얻을 방법이 없었다. 이 메서드는 기존
        pending(선제갱신 마커 포함)을 버리고 `_authenticate_locked` 의 검증된 경로
        (쿠키 비우고 맨몸 POST → challenge 쿠키가 바인딩된 fresh inquiry)를 강제로 탄다.
        ⚠ POST /authentication 1회가 나간다 — **사용자가 명시적으로 요청한 순간에만**
        호출하라(폴링 금지, biometric throttle). 살아있는 세션 파일(.pkl)은 건드리지
        않으므로 워커는 만료까지 하던 일을 계속한다.

        반환 계약은 authenticate() 와 동일: True=인증 완료(persona 불필요),
        False+persona_required=True → 새 challenge 가 .pending 에 저장됨.
        """
        with _mint_lock(self.session_file):
            self._clear_pending()
            return self._authenticate_locked()

    def _authenticate_locked(self) -> bool:
        # ⚠ 미완료 challenge 가 이미 있으면 새로 발급하지 않는다. POST /authentication 은
        #    매번 새 inquiry 를 만들고 WQB 는 직전 inquiry 를 폐기하므로, 사용자가 그 링크로
        #    인증하는 중이었다면 페이지가 그 순간 죽는다. TTL 이 지난 challenge 만 갈아끼운다.
        pend = self._read_pending()
        if pend is not None:
            age = self._pending_age_s()
            if age is None or age < _PERSONA_PENDING_TTL_S:
                self.persona_required = True
                self.persona_url = pend.get('persona_url')
                LOG.info('미완료 persona challenge 재사용(age=%s초) — 새 발급 생략',
                         int(age) if age is not None else '?')
                return False
            LOG.info('persona challenge 가 %.0f분 경과 — 폐기하고 새로 발급', age / 60)
            self._clear_pending()
        # ⚠ stale-persona-URL 무한반복 버그 수정(2026-07-08): 무효한 쿠키(만료된 .pkl 세션이든
        #    pending challenge 쿠키든)를 든 채로 POST /authentication 을 때리면, WQB 가 그 쿠키에
        #    묶인 '오래된 outstanding persona inquiry' 를 그대로 재발급한다 → 만료된 같은 biometric
        #    URL 을 영원히 되돌려줘 사용자가 절대 통과 못 한다. 여기까지 왔다는 건 살아있는 세션도
        #    살아있는 challenge 도 없다는 뜻이므로, 무조건 비우고 '깨끗한' 재인증을 한다.
        #    (실측: .pkl 제거 시 inquiry ID 가 바뀐다.)
        self._clear_session_cookies()
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
            self._capture_expiry(body)   # POST 성공 body 의 token.expiry 캐시(선제갱신용)
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

    def _save_pending(self, persona_url, cookies=None, source=None):
        """미완료 challenge 를 `.pending` 에 기록한다.

        cookies=None(기본) — 현재 세션 jar 전체를 저장. **challenge 를 만든 요청의 쿠키**가
        finalize 바인딩이므로, `_authenticate_locked`(쿠키 비우고 맨몸 POST) 경로에선 이게 맞다.
        source='refresh' — 선제 갱신(refresh_token)이 만든 **알림용 마커**. 이 challenge 는
        살아있는 세션 쿠키(JWT `t`) 아래에서 발급돼, 나중에 링크를 해석하면 이미 죽은
        hosted-flow 세션이 나온다(2026-07-16 실측: 열자마자 'session expired' 페이지).
        그래서 마커는 해석 대상이 아니며(pending_persona 가 거부), 사용자가 링크를 누르는
        순간 mint_challenge() 로 깨끗한 challenge 를 그 자리에서 새로 발급한다.
        """
        self.persona_url = persona_url; self.persona_required = True  # always signal, even if disabled
        try:
            pf = self._pending_file()
            if not pf:
                return  # persistence disabled — still signal above
            if cookies is None:
                ck = self.session.cookies
                cookies = ck.get_dict() if hasattr(ck, 'get_dict') else dict(ck)
            payload = {'cookies': cookies, 'persona_url': persona_url}
            if source:
                payload['source'] = str(source)
            os.makedirs(os.path.dirname(pf), mode=0o700, exist_ok=True)
            try:
                os.chmod(os.path.dirname(pf), 0o700)
            except OSError:
                pass
            fd = os.open(pf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
            with os.fdopen(fd, 'w') as f:
                json.dump(payload, f)
        except Exception as e:
            LOG.warning('pending save err: %s', e)

    def _read_pending(self):
        """`.pending` 를 **네트워크·세션 부작용 없이** 그대로 읽는다. 없으면 None."""
        try:
            pf = self._pending_file()
            if not pf or not os.path.exists(pf):
                return None
            with open(pf, 'r') as f:
                pend = json.load(f)
            if not isinstance(pend, dict) or not (pend.get('persona_url') or ''):
                return None
            return pend
        except Exception as e:
            LOG.warning('pending read err: %s', e)
            return None

    def _pending_age_s(self):
        """`.pending` 이 만들어진 뒤 흐른 초. 파일이 없거나 stat 실패면 None."""
        try:
            pf = self._pending_file()
            if not pf or not os.path.exists(pf):
                return None
            return max(0.0, _time.time() - os.path.getmtime(pf))
        except OSError:
            return None

    def pending_persona(self, resolve: bool = True):
        """저장된 미완료 persona challenge 를 반환. 없으면 None.

        반환: {'persona_url': str, 'inquiry': str}

        `resolve=False` — `.pending` 파일만 읽는다. **WQB 로 나가는 호출이 0건**이고
        `persona_url` 은 항상 ''. 반복 호출되는 경로(60초 폴링, 대시보드 진입)는 반드시
        이 모드를 써야 한다.

        `resolve=True` — 브라우저용 Persona 링크를 새로 발급받는다(`_public_persona_url`).
        ⚠ 이건 **부작용이 있는 호출**이다: WQB 가 inquiry 를 재개하며 새 hosted-flow 세션을
        만들고 직전 세션을 무효화한다. 사용자가 열어 둔 인증 페이지가 그 순간 죽는다
        (사장 보고 2026-07-10 "무한 새로고침 후 세션 만료" 의 원인). 사용자가 링크를
        요청하는 그 순간에만 써라.

        어느 모드든 절대 POST /authentication 을 하지 않는다 — 매 조회마다 POST 하면
        WQB biometric throttle 가 영원히 재무장되기 때문이다.
        """
        try:
            pend = self._read_pending()
            if pend is None:
                return None
            pu = pend.get('persona_url') or ''
            if 'withpersona.com' in pu:
                # Legacy pending files stored the short-lived Persona public URL.
                # Once that link expires we cannot refresh it because the WQB API
                # challenge URL was lost, so discard it and let authenticate()
                # mint a fresh challenge.
                self._clear_pending()
                return None
            inquiry = _extract_inquiry_from_url(pu)
            source = str(pend.get('source') or '')
            if not resolve:
                return {'persona_url': '', 'inquiry': inquiry, 'source': source}
            if source == 'refresh':
                # 선제 갱신 마커 — 해석하면 죽은 hosted 세션이 나온다('session expired',
                # 2026-07-16 실측). 해석하지 말고 그대로 알린다. 링크 발급 경로
                # (/wqb-persona-link)는 이 표식을 보고 mint_challenge() 로 갈아탄다.
                return {'persona_url': '', 'inquiry': inquiry, 'source': source}
            cookies = pend.get('cookies')
            if isinstance(cookies, dict) and cookies:
                self.session.cookies.update(cookies)
            public_url = _public_persona_url(pu, session=self.session)
            if public_url is None:
                # 일시 실패(네트워크 등) — pending 을 지우면 다음 조회가 새 challenge
                # POST 를 유발해 throttle 재무장 루프가 된다. challenge 는 유지하고
                # URL 만 빈 값으로 반환 → UI 가 "링크 준비 중" 을 띄우고 재시도한다.
                return {'persona_url': '', 'inquiry': inquiry}
            if not _is_public_persona_url(public_url):
                # Never expose the WQB API challenge URL to the browser. Opening
                # it directly shows the "Details:Gone" white page for stale or
                # already-finalized inquiries; dropping it lets the status
                # endpoint mint a fresh challenge.
                self._clear_pending()
                return None
            return {'persona_url': public_url,
                    'inquiry': inquiry or _extract_inquiry_from_url(public_url)}
        except Exception as e:
            LOG.warning('pending_persona read err: %s', e)
            return None

    def _persona_finalized(self) -> bool:
        """finalize 확정 후 공통 마무리 — 세션 저장·pending 정리·만료 갱신.
        ⚠ 만료 갱신을 빼먹으면 .meta 가 옛 값에 얼어붙어 선제 갱신이 불가능해진다
        (실측 2026-07-12)."""
        self._save_session()
        self._clear_pending()
        self.persona_required = False
        self._authed = True
        self._ensure_expiry(force=True)
        self._clear_refresh_attempt()
        return True

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
            try:
                # ⚠ finalize 는 느릴 수 있다 — WQB 가 Persona inquiry 상태를 서버측에서
                #   확인한다. 30초 타임아웃이 성공한 finalize 를 실패로 오판했었다
                #   (2026-07-31 실측: read timeout 후 세션은 이미 인증돼 있었음).
                r = self.session.post(f'{BASE}/authentication/persona',
                                      json={'inquiry': inquiry}, timeout=90)
            except (requests.Timeout, requests.ConnectionError) as e:
                # finalize 가 서버측에 이미 착지했을 수 있다 — 세션 유효성으로 확정.
                LOG.warning('complete_persona finalize 네트워크 오류(%s) — 세션 유효성으로 확정', e)
                return self._persona_finalized() if self._session_valid() else False
            if r.status_code in (200, 201):
                # finalize succeeded — session is now authenticated; persist its cookies
                return self._persona_finalized()
            if r.status_code == 410:
                # WQB returns 410 Gone for finalized or expired inquiries. Success still
                # requires the same session to pass GET /authentication.
                LOG.info('complete_persona 410 Gone — verifying current WQB session')
                if self._session_valid():
                    return self._persona_finalized()
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

    def _clear_session_cookies(self):
        """무효(만료) 세션 쿠키를 비운다 — WQB 가 낡은 쿠키에 묶인 오래된 persona inquiry 를
        재발급하지 않고 fresh inquiry 를 주도록. 파일(.pkl)은 남기고 in-memory 만 비운다
        (다음 성공 인증이 _save_session 으로 덮어씀). 어떤 쿠키자 구현이든 방어적으로 처리."""
        try:
            self.session.cookies.clear()
        except Exception:
            try:
                self.session.cookies = requests.cookies.RequestsCookieJar()
            except Exception:
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
        out = {'pass': [], 'fail': [], 'error': [], 'pending': [], 'warning': []}
        check_metrics: dict[str, str] = {}
        for ch in checks:
            res = str(ch.get('result') or '').upper()
            nm = ch.get('name')
            nm_u = str(nm or '').strip().upper()
            # ⚠ 승격은 **분류 체크에도** 해야 한다. HT_*/CLUSTER_TEST 는 차단하지 않지만
            #   그 값이야말로 2026-07 개편 이후 제출 가능성의 1차 결정 변수다.
            #   (구 코드는 core 가 아니면 `continue` 로 통째로 버려서 HT 지표가 영영
            #    보상에 도달하지 못했다.)
            mkey = _CHECK_METRIC_KEY.get(nm_u)
            if mkey:
                v = _parse_check_number(ch.get('value'))
                if v is not None:
                    check_metrics[mkey] = str(v)
                lim = _parse_check_number(ch.get('limit'))
                if lim is not None:
                    check_metrics[f'{mkey}_cutoff'] = str(lim)
            _harvest_classification(nm_u, ch, res, check_metrics)
            if not _is_core_check(nm):
                continue
            # value/cutoff 는 브라우저 스크레이퍼와 동일하게 **문자열** 계약을 지킨다.
            # (워커 _short_metric_label/_extract_self_corr_value 가 `.strip()` 호출 → raw float 면 크래시)
            v = ch.get('value'); lim = ch.get('limit')
            item = {'name': nm,
                    'value': '' if v is None else str(v),
                    'cutoff': '' if lim is None else str(lim),
                    'result': res,
                    'desc': _check_desc(str(nm), ch.get('value'), ch.get('limit'), res)}
            # WARNING 은 **비차단**이다 — 2026-07 개편에서 HT 분류를 얻은 알파의 표준
            # 컷(LOW_SHARPE 등)이 여기로 강등된다. 예전엔 버킷이 없어 통째로 버려졌고,
            # 그래서 '제출 가능한데 pass 도 fail 도 아닌' 알파가 보상 0 을 받았다.
            bucket = {'PASS': 'pass', 'FAIL': 'fail', 'PENDING': 'pending',
                      'ERROR': 'error', 'WARNING': 'warning'}.get(res)
            if bucket:
                out[bucket].append(item)
        metrics = {}
        for k in ('sharpe', 'fitness', 'returns', 'turnover', 'drawdown', 'margin'):
            if isf.get(k) is not None:
                metrics[k] = str(isf[k])
        # 투자가능성 제약/리스크 중립화 하위 성과 — HT Investable/Orthogonal 판정 재료.
        for sub_key, prefix in (('investabilityConstrained', 'ic_'), ('riskNeutralized', 'rn_')):
            sub = isf.get(sub_key)
            if isinstance(sub, dict):
                for k in ('sharpe', 'fitness', 'turnover', 'returns'):
                    if sub.get(k) is not None:
                        metrics[f'{prefix}{k}'] = str(sub[k])
        # 최상위 classifications — MATCHES_CLASSIFICATION 체크와 **다른 곳**이다.
        # 단일데이터셋(DATA_USAGE:SINGLE_DATA_SET) 판정이 여기에만 있고, 그 알파는
        # IS Ladder 대신 2년 컷 하나만 보므로 안정성 목표치가 달라진다(criteria 참조).
        try:
            cids = [str(c.get('id')) for c in (data.get('classifications') or [])
                    if isinstance(c, dict) and c.get('id')]
            if cids:
                metrics['classification_ids'] = ','.join(cids)
        except Exception:
            pass
        st = data.get('settings') or {}
        for k, mk in (('delay', '_delay'), ('region', 'region'),
                      ('universe', 'universe'), ('neutralization', 'neutralization')):
            if st.get(k) is not None:
                metrics[mk] = str(st[k])
        # 체크 유래 지표는 요약 지표를 덮어쓰지 않는다(요약이 권위).
        for k, v in check_metrics.items():
            metrics.setdefault(k, v)
        return {'metrics': metrics, 'is_status': out}

    def set_alpha_description(self, alpha_id: str, description: str) -> bool:
        """알파의 regular.description 을 PATCH 로 설정한다 (Power Pool 필수 요건).

        문서("Getting Started with Power Pool Alphas"): 100자 이상 Idea/Rationale
        형식 설명이 없으면 Power Pool 자격이 없다. 계약은 ACE lib
        set_alpha_properties 와 동일: PATCH /alphas/{id} {"regular": {"description": …}}.
        실패해도 제출 자체는 막지 않는다(fail-soft) — 설명 없는 제출은 Regular 로는
        유효하기 때문. 성공 여부만 돌려준다.
        """
        if not alpha_id or not str(description or '').strip():
            return False
        if not self._ensure_auth():
            return False
        try:
            r = self.session.patch(
                f'{BASE}/alphas/{alpha_id}',
                json={'regular': {'description': str(description)[:4000]}},
                headers={'Accept': _API_ACCEPT}, timeout=_HTTP_TIMEOUT)
            if 200 <= r.status_code < 300:
                return True
            LOG.warning('alpha %s description PATCH %s: %s',
                        alpha_id, r.status_code, (r.text or '')[:120])
            return False
        except Exception as e:
            LOG.warning('alpha %s description PATCH err: %s', alpha_id, e)
            return False

    @staticmethod
    def _rejection_reason(body_j) -> str | None:
        """응답의 is.checks 에서 **FAIL 만** 골라 사람이 읽을 사유로. 없으면 None.

        ⚠ 이름만 적으면 안 된다(2026-07-28 사장 지적). gJ9ea3ZJ 는 노트가
        `rejected:LOW_SHARPE; LOW_FITNESS; LOW_GLB_EMEA_SHARPE (http_403)` 인데
        **지금 그 알파를 조회하면 셋 다 WARNING** 이다 — 제출 시점 평가와 조회 시점
        평가가 다르기 때문이다. 그때 WQB 가 준 값·기준을 같이 남겨야 나중에 대조가
        되고, "표시가 틀렸다" 로 읽히지 않는다.
        """
        if not isinstance(body_j, dict):
            return None
        checks = ((body_j.get('is') or {}).get('checks')) or []
        failed = [c for c in checks if str(c.get('result') or '').upper() == 'FAIL']
        if not failed:
            return None
        # ⚠ 앞 3개만 자르면 **결정적인 체크가 잘려 나간다**(2026-07-28). WQB 체크 순서상
        #   LOW_SHARPE·LOW_FITNESS·LOW_GLB_* 가 맨 앞이고 REGULAR_SUBMISSION·
        #   POWER_POOL_CORRELATION 처럼 진짜로 막는 것은 한참 뒤에 온다 — 잘린 자리에
        #   답이 있었다. FAIL 은 애초에 몇 개 안 되니 전부 남긴다(노트 컬럼은 300자).
        out = []
        for c in failed:
            name = str(c.get('name') or 'FAIL')
            val, lim = c.get('value'), c.get('limit')
            out.append(f'{name}({val} vs {lim})'
                       if val is not None and lim is not None else name)
        return '; '.join(out)

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
                    # 접수는 됐는데 우리가 기다림을 포기하는 경우 — 포기 선언 전에
                    # 알파 stage 실측으로 성공 여부를 확정한다 (2026-07-25 유실 교훈).
                    if self._verify_submitted(alpha_id):
                        return True, 'submitted (pending_timeout 후 stage=OS 확인)'
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
                reason = self._rejection_reason(body)
                if reason:
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
            # 거절 응답은 하루 4건뿐이라 통째로 남겨도 로그가 붐비지 않는다. 이름만
            # 저장하면 나중에 왜 거절됐는지 재구성할 길이 없다(위 _rejection_reason 참조).
            if r.status_code not in (404,):
                # 800자로 자르니 checks 배열 한가운데서 끊겨 JSON 파싱이 안 됐다
                # (2026-07-28 첫 실사용). 하루 몇 건뿐이라 통째로 남긴다.
                LOG.warning('alpha submit http_%s %s — body=%s', r.status_code, alpha_id,
                            (getattr(r, 'text', '') or '')[:4000])
            reason = self._rejection_reason(body_j)
            if reason:
                # **이미 제출된 알파도 여기로 떨어진다.** 사장이 BRAIN UI 로 직접 낸 뒤
                # 우리 큐에서 버튼을 누르면 WQB 가 거절하는데, 그걸 '거절' 로 적으면
                # 제출된 알파가 대기 큐에 거절 상태로 눌러앉는다(2026-07-28 gJ9ea3ZJ).
                # 거절은 드무니(하루 몇 건) 실측 GET 한 번이 싸다.
                if self._verify_submitted(alpha_id):
                    return True, 'submitted (이미 제출됨 — stage=OS 확인)'
                return False, f'rejected:{reason} (http_{r.status_code})'
            # ⚠ 404 맹점 (2026-07-27 진단): 제출이 **완료되면** /alphas/{id}/submit
            #   리소스가 사라져 폴링 GET 이 404 를 받는다. 7/23·7/25 실제 제출 성공
            #   2건이 'submit_http_404' 실패로 기록돼 통계·게이트에서 증발했다.
            #   포기 선언 전에 알파 stage 를 실측해 성공을 확정한다.
            if r.status_code == 404 and method == 'GET' \
                    and self._verify_submitted(alpha_id):
                return True, 'submitted (submit 리소스 소멸 → stage=OS 확인)'
            body = (getattr(r, 'text', '') or '')[:200].replace(chr(10), ' ').strip()
            suffix = f': {body}' if body else ''
            return False, f'submit_http_{r.status_code}{suffix}'

    def _verify_submitted(self, alpha_id: str) -> bool:
        """GET /alphas/{id} 로 제출 완료(stage OS / dateSubmitted 有)를 실측 확인."""
        try:
            r = self.session.get(f'{BASE}/alphas/{alpha_id}', timeout=_HTTP_TIMEOUT,
                                 headers={'Accept': _API_ACCEPT})
            if not r.ok:
                return False
            j = r.json()
            return (str(j.get('stage') or '').upper() == 'OS'
                    or bool(j.get('dateSubmitted')))
        except Exception:
            return False

    def submit_simulation(self, expr: str, settings: dict,
                          _retry_auth: bool = False, _retry_net: bool = False) -> str | None:
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
            # 슬롯 대기(수 분~수십 분) 동안 논 keep-alive 를 서버가 끊으면 첫 POST 가
            # RemoteDisconnected 로 죽는다 — 새 커넥션으로 1회만 재시도한다.
            # (시뮬 접수라 만에 하나 중복 생성돼도 슬롯 1개 손해로 그친다.)
            if not _retry_net:
                LOG.warning('submit network err: %s — 1회 재시도', e)
                return self.submit_simulation(expr, settings,
                                              _retry_auth=_retry_auth, _retry_net=True)
            LOG.warning('submit network err: %s', e); return None
        self._capture_sim_quota(r)
        if r.status_code == 429:  # CONCURRENT_SIMULATION_LIMIT_EXCEEDED
            if not self._rl_body_logged:
                self._rl_body_logged = True
                LOG.warning('429 동시한도 응답 — body=%s / limit-headers=%s',
                            (getattr(r, 'text', '') or '')[:300],
                            {k: v for k, v in (r.headers or {}).items()
                             if 'limit' in k.lower() or 'retry' in k.lower()})
            return 'RATE_LIMITED'
        if r.status_code in (401, 403) and not _retry_auth:
            # 갱신 레이스 — 새 토큰은 디스크에 있고 이 스레드만 옛 쿠키다(poll 과 동일 함정).
            # 디스크에서 다시 읽고 딱 한 번 재시도한다(인증 POST 없음 → throttle 위험 0).
            if self._load_session():
                LOG.info('submit %s — 갱신된 세션을 다시 읽고 1회 재시도', r.status_code)
                return self.submit_simulation(expr, settings, _retry_auth=True)
        if r.status_code not in (200, 201):
            # 여태 아무 말 없이 None 을 뱉었다 — 워커엔 '제출 응답 없음' 으로만 보여
            # **왜** 죽었는지 알 길이 없었다(2026-07-28 실측: 한 라운드 18개 중 4개가
            # 여기서 조용히 증발, 전부 합성 코드). 형제 submit_super_simulation 은
            # 처음부터 body 를 남긴다 — 정규 경로만 빠져 있었다.
            LOG.warning('submit http_%s: %s', r.status_code,
                        (getattr(r, 'text', '') or '')[:200])
            return None
        return r.headers.get('Location') or r.headers.get('location')

    def submissions_on(self, date_str: str) -> int | None:
        """WQB 가 세는 그 날짜의 제출 수. 조회 실패면 None(호출부가 폴백).

        ⚠ 우리 DB 집계(submit_attempts)는 **우리가 낸 것만** 센다. 사장이 BRAIN UI 에서
        직접 내면 모르고, 그만큼 예산을 남았다고 착각해 초과 제출을 시도한다
        (2026-07-28: 우리 집계 2건 vs WQB 실측 3건).
        """
        if not self._ensure_auth():
            return None
        try:
            r = self.session.get(f'{BASE}/users/self/activities/submissions',
                                 timeout=_HTTP_TIMEOUT, headers={'Accept': _API_ACCEPT})
        except Exception as e:
            LOG.warning('제출 활동 조회 실패: %s', e)
            return None
        if not r.ok:
            return None
        try:
            recs = ((r.json().get('records') or {}).get('records')) or []
        except Exception:
            return None
        for row in recs:
            try:
                if str(row[0]) == date_str:
                    return int(row[1])
            except (IndexError, TypeError, ValueError):
                continue
        return 0        # 기록에 없으면 그날은 0건 (조회는 성공했다)

    def accessible_operators(self) -> set:
        """이 **계정이 쓸 수 있는** 연산자 이름 집합. 실패하면 빈 집합(= 제한 없음 취급).

        2026-07-28 실측(CONSULTANT 계정): 82개. vector_neut 은 되지만 vector_proj·
        regression_neut·regression_proj 은 안 된다 — 컨설턴트라고 전부 열리는 게
        아니라서 계정 종류로 넘겨짚으면 안 된다.
        """
        if not self._ensure_auth():
            return set()
        try:
            r = self.session.get(f'{BASE}/operators', timeout=_HTTP_TIMEOUT,
                                 headers={'Accept': _API_ACCEPT})
        except Exception as e:
            LOG.warning('operators 조회 실패: %s', e)
            return set()
        if not r.ok:
            return set()
        try:
            j = r.json()
        except Exception:
            return set()
        items = j if isinstance(j, list) else (j.get('results') or [])
        return {str(x.get('name')) for x in items
                if isinstance(x, dict) and x.get('name')}

    def accessible_datasets(self, region: str, universe: str, delay) -> set:
        """이 **계정이 실제로 접근 가능한** dataset.id 집합. 실패하면 빈 집합.

        팔레트는 하우스 RC 계정으로 긁으므로 일반 계정에는 없는 필드가 섞인다
        (2026-07-27 실측 USA/TOP3000: RC 297 데이터셋 vs 일반 21). 그대로 쓰면
        시뮬이 'Invalid data field …' 로 죽는다.
        """
        if not self._ensure_auth():
            return set()
        out: set = set()
        offset = 0
        while True:
            try:
                r = self.session.get(
                    f'{BASE}/data-sets', timeout=_HTTP_TIMEOUT,
                    params={'region': region, 'universe': universe, 'delay': delay,
                            'instrumentType': 'EQUITY', 'limit': 50, 'offset': offset})
            except Exception as e:
                LOG.warning('data-sets 조회 실패: %s', e)
                return out
            if not r.ok:
                return out
            j = r.json()
            res = j.get('results') or []
            if not res:
                return out
            out |= {str(d.get('id')) for d in res if d.get('id')}
            offset += 50
            if offset >= int(j.get('count') or 0):
                return out

    def submit_super_simulation(self, selection: str, combo: str,
                                settings: dict) -> str | None:
        """type=SUPER 시뮬 제출 → progress Location (⑤ 슈퍼알파, ACE body 형식).

        settings 는 superalpha.default_settings() 가 만든 **완성본**을 그대로 쓴다
        (REGULAR 용 _full_settings 와 달리 selectionHandling/selectionLimit 포함).
        반환 규약은 submit_simulation 과 동일: Location / 'RATE_LIMITED' / None.
        """
        if not self._ensure_auth():
            return None
        body = {'type': 'SUPER', 'settings': dict(settings),
                'selection': selection, 'combo': combo}
        try:
            r = self.session.post(
                f'{BASE}/simulations', json=body, timeout=_HTTP_TIMEOUT,
                headers={'Content-Type': 'application/json',
                         'Access-Control-Request-Headers': 'Location',
                         'Accept': _API_ACCEPT})
        except Exception as e:
            LOG.warning('super submit network err: %s', e); return None
        self._capture_sim_quota(r)
        if r.status_code == 429:
            return 'RATE_LIMITED'
        if r.status_code not in (200, 201):
            body = (getattr(r, 'text', '') or '')
            LOG.warning('super submit http_%s: %s', r.status_code, body[:200])
            # 권한 미보유는 **재시도해도 소용없는 계정 상태**다 — 일반 실패와 구분해
            #   호출부가 사람에게 "WQB 에 super simulation 권한을 요청하라" 고 말할 수
            #   있게 한다. 실측(2026-07-27, CONSULTANT 권한 보유 계정):
            #   400 {"type":["Not permissioned for super simulations"]}
            #   — CONSULTANT 만으로는 안 되고 별도 권한이 필요하다.
            if r.status_code == 400 and 'not permissioned' in body.lower():
                return 'NOT_PERMISSIONED'
            return None
        return r.headers.get('Location') or r.headers.get('location')

    def _capture_sim_quota(self, r) -> None:
        """POST /simulations 응답의 **일일 시뮬 쿼터** 헤더를 기록한다.

        WQB 는 계정별 일일 시뮬 한도를 두고(중복 시뮬도 차감된다) 남은 수량을
        `X-Ratelimit-Remaining` 으로 알려준다. 여태 이 코드는 헤더를 읽지 않아
        한도 소진을 **감지조차 못 했다** — 소진되면 그날 남은 라운드가 통째로
        실패로 기록된다. 워커가 sim_quota() 를 보고 스스로 멈출 수 있게 남긴다.
        """
        try:
            h = r.headers or {}
            lim = h.get('X-Ratelimit-Limit') or h.get('x-ratelimit-limit')
            rem = h.get('X-Ratelimit-Remaining') or h.get('x-ratelimit-remaining')
            rst = h.get('X-Ratelimit-Reset') or h.get('x-ratelimit-reset')
            if rem is None and lim is None:
                return
            q = {'limit': _parse_check_number(lim), 'remaining': _parse_check_number(rem),
                 'reset_s': _parse_check_number(rst), 'ts': _time.time()}
            self._sim_quota = q
            if q['remaining'] is not None and q['remaining'] <= _SIM_QUOTA_WARN:
                LOG.warning('일일 시뮬 쿼터 잔여 %s / %s (리셋 %ss)',
                            q['remaining'], q['limit'], q['reset_s'])
        except Exception:
            pass

    def sim_quota(self) -> dict | None:
        """마지막으로 관측한 일일 시뮬 쿼터. 한 번도 POST 하지 않았으면 None."""
        return getattr(self, '_sim_quota', None)

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

    def poll(self, progress_url: str, stop_event=None, deadline_s: int = None,
             interval_s: float = None, sleep=None, abort_event=None) -> dict:
        """시뮬 완료까지 폴링. (status, alpha, message, progress) 반환.

        2026-07-21 개편 — 라이브 에러의 **75%가 여기서 났다**(최근 400건 중 299건이
        'sim TIMEOUT: poll deadline'). WQB 가 낸 에러가 아니라 우리가 720초 만에
        포기한 것이다. 세 가지를 고쳤다:

        1. **마감 720초 → 1800초** (IQC_SIM_POLL_DEADLINE_S). D0·TOP3000·10년 IS 시뮬은
           대기열까지 포함하면 12분을 넘기는 게 정상이다.
        2. **Retry-After 존중.** BRAIN API 문서가 명시한 계약인데 무시하고 5초 고정으로
           긁고 있었다 — 30분×5초면 알파당 360회, 8개 동시면 라운드당 2880회 GET 이라
           "scripts do not lay excessive load on the server" 지침에도 어긋난다.
        3. **정체(stall) 감지.** 진행률이 IQC_SIM_POLL_STALL_S(기본 900초) 동안 한 번도
           안 움직이면 마감 전이라도 포기하고 슬롯을 반환한다 — 진짜 멈춘 시뮬에
           30분을 낭비하지 않기 위해서다.

        `abort_event` (2026-07-29) — 배치의 **꼬리 절단** 신호. 형제 시뮬이 전부 끝나
        슬롯이 비었는데도 이 시뮬이 아직 대기열에서 **시작조차 못 했으면**(status 를
        한 번도 못 받았으면) 죽은 접수로 보고 마감 전에 포기한다. 마감(3600초)은 그대로
        두고 이 경우만 일찍 잡는다 — r11 #7 이 형제가 다 끝난 뒤 34분을 더 태웠다.
        """
        sleep = sleep or _time.sleep
        deadline_s = _POLL_DEADLINE_S if deadline_s is None else float(deadline_s)
        base_interval = _POLL_INTERVAL_S if interval_s is None else float(interval_s)
        # deadline 은 **벽시계**(monotonic) 기준이어야 한다. 루프-카운트 방식은 단일 GET 이
        # 매달리면 영원히 한 바퀴를 못 돌아 deadline 이 도달 불가능해진다(라이브 무한 행 회귀).
        start = _time.monotonic()
        last = {'status': None, 'alpha': None, 'message': None, 'progress': None}
        last_change = start
        prev_mark = None
        n_auth = 0              # 연속 401/403 횟수
        saw_status = False      # status 또는 progress>0 을 받았는가(= 시뮬이 시작됐는가)
        n_blind = 0             # status 를 못 읽은 턴 수 (대기열 + 429/5xx)
        while True:
            now = _time.monotonic()
            if now - start >= deadline_s:
                reason = 'poll deadline'
                break
            # 꼬리 절단 — 형제가 다 끝났는데 아직 시작조차 못 한 접수는 버린다.
            # 이미 돌기 시작한(status/progress 를 받은) 시뮬은 절대 건드리지 않고,
            # 갓 접수돼 폴링 나이가 _TAIL_MIN_POLL_S 미만인 시뮬도 봐준다.
            if (not saw_status and abort_event is not None and abort_event.is_set()
                    and now - start >= _TAIL_MIN_POLL_S):
                reason = 'tail cut (형제 완료 후에도 대기열에서 미시작)'
                break
            if stop_event is not None and stop_event.is_set():
                self.cancel(progress_url)
                return {'status': 'CANCELLED', 'alpha': None, 'message': '', 'progress': last.get('progress')}
            wait = base_interval
            readable = False        # 이번 턴에 서버 상태를 실제로 읽었는가
            try:
                r = self.session.get(progress_url, timeout=_HTTP_TIMEOUT,
                                     headers={'Accept': _API_ACCEPT})
                j = r.json() if r.ok else {}
                readable = bool(r.ok)
                # 세션이 죽으면(401/403) 폴링은 회복되지 않는다 — 재인증은 사용자 생체
                # 인증이 필요해서 이 루프 안에서 일어날 수 없다. 그런데 못 읽는 턴은
                # 아래에서 '대기열'로 보고 넘어가므로, 마감(3600초)을 꼬박 태우고 나서야
                # TIMEOUT 이 뜬다(2026-07-28 실측: 인증 사망 라운드 1건이 97분).
                # 세션 갱신 레이스로 한 번 튀는 건 봐주고, 두 번 연속이면 포기한다.
                if r.status_code in (401, 403):
                    n_auth += 1
                    # ⚠ 죽은 게 아니라 **갱신 레이스**일 수 있다. session_keeper 가 방금
                    #   새 토큰을 디스크(.pkl)에 썼어도, 이미 돌고 있는 폴링 스레드는
                    #   메모리에 옛 쿠키를 들고 있어 401 을 맞는다.
                    #   2026-07-29 실측: 12:06 에 갱신이 **성공**해 있었는데도 라운드
                    #   17건 중 11건이 401 로 통째로 버려졌다.
                    #   _load_session 은 순수 디스크 읽기다 — 인증 POST 를 하지 않으므로
                    #   biometric throttle 위험이 없다. 한 번만 다시 읽고 재시도한다.
                    if n_auth == 1 and self._load_session():
                        LOG.info('poll 401 — 갱신된 세션을 디스크에서 다시 읽고 재시도')
                        sleep(base_interval)
                        continue
                    if n_auth >= 2:
                        reason = f'auth dead (http_{r.status_code})'
                        break
                else:
                    n_auth = 0
                ra = r.headers.get('Retry-After') or r.headers.get('retry-after')
                if ra:
                    try:
                        wait = _clamp_retry_after(float(ra), base_interval)
                    except (TypeError, ValueError):
                        pass
                elif not r.ok:
                    # 429/5xx 인데 Retry-After 가 없으면 우리가 알아서 물러난다.
                    wait = max(base_interval, min(30.0, base_interval * 4))
            except Exception as e:
                j = {'message': str(e)}
            last = {'status': j.get('status'), 'alpha': j.get('alpha'),
                    'message': j.get('message'), 'progress': j.get('progress')}
            if last['status'] in ('COMPLETE', 'ERROR', 'FAIL', 'WARNING'):
                # 옛 마감(=지금 마감의 절반)을 넘겨 끝난 건은 INFO 로 올린다 — 마감을
                # 올린 게 실제로 후보를 살렸는지, 꼬리가 어디까지 늘어지는지 보려면
                # 이 숫자가 필요하다. 평상시 건은 DEBUG 라 로그가 붐비지 않는다.
                _el = now - start
                (LOG.info if _el > deadline_s * 0.5 else LOG.debug)(
                    'sim 완료 %.0fs status=%s', _el, last['status'])
                return last
            # ⚠ **응답을 못 읽은 턴은 정체로 세면 안 된다.** 8워커가 5초마다 긁으면 폴링
            #   자체가 429 를 맞는데, 그때 j={} 라 status=None 이 되어 (None, None) 이
            #   계속 같은 mark 로 찍힌다 → 멀쩡히 돌던 시뮬을 900초에 죽였다.
            #   (2026-07-21 실측: 최근 25건 중 4건이 전부 `status=None` 정체로 오판됐다.)
            if last['status'] is not None:
                saw_status = True
            # ⚠ WQB 는 **실행 중에도 status 없이 progress 만** 준다(status 는 완료 때만).
            #   status 로만 '시작'을 판정하면 실행 중 시뮬이 전부 '대기열 미시작'으로
            #   보인다 → 꼬리 절단이 돌던 시뮬만 죽였다(7/29~31 실측: tail cut 13건
            #   전부 progress 0.1~0.35, 진짜 미시작을 자른 적은 0건).
            try:
                if float(last['progress'] or 0) > 0:
                    saw_status = True
            except (TypeError, ValueError):
                pass
            if not readable or last['status'] is None:
                n_blind += 1
                # ⚠ status 가 아예 없는 200 응답은 **대기열**이다(정체가 아니다).
                #   WQB 는 큐에 있는 동안 Retry-After 와 함께 빈 본문을 준다.
                #   2026-07-22 실측: 이걸 정체로 세어 946초 만에 멀쩡한 시뮬을 죽였다
                #   (라운드 8건 중 2건). 상태를 못 읽는 동안은 전체 마감(1800초)에만 맡긴다.
                sleep(wait)
                continue
            mark = (last['status'], last['progress'])
            if mark != prev_mark:
                prev_mark, last_change = mark, now
            elif now - last_change >= _POLL_STALL_S:
                reason = f'no progress for {_POLL_STALL_S:.0f}s (status={last["status"]})'
                break
            sleep(wait)
        # 포기 — 슬롯 반환. 얼마나 기다렸는지 남겨야 마감값을 실측으로 조정할 수 있다.
        elapsed = _time.monotonic() - start
        # started=False 면 WQB 대기열에서 시작조차 못 한 접수다 — 마감값이 아니라
        # 꼬리 절단/동시 슬롯 쪽을 봐야 한다는 신호라 give-up 로그에 같이 남긴다.
        LOG.warning('sim 폴링 포기 %.0fs — %s (progress=%s, started=%s, blind_turns=%d)',
                    elapsed, reason, last.get('progress'), saw_status, n_blind)
        self.cancel(progress_url)
        return {'status': 'TIMEOUT', 'alpha': None,
                'message': f'{reason} after {elapsed:.0f}s', 'progress': last.get('progress')}

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
