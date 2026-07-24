"""세션 keep-alive — 얼굴 인증 회피 로직. 전부 목(mock), WQB 호출 0회.

핵심 검증:
  1. 만료 시각을 JWT 에서 읽는다 (.meta 가 썩어도 정확) — 3일 정지 버그의 근본 수정.
  2. refresh_token 은 **쿠키를 지우지 않는다** (지우면 그게 얼굴 인증을 부른다).
  3. persona 가 와도 살아있는 세션을 버리지 않는다.
  4. 토큰 1개당 갱신 1회 (BIOMETRICS_THROTTLED 재무장 방지).

Run: python3 -m pytest tests/test_session_keepalive.py -v
"""
import base64
import json
import time

import pytest

from server import wqb_api


def _jwt(exp_epoch: float, amr=('pwd', 'face')) -> str:
    def _seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')
    return f"{_seg({'alg': 'HS256'})}.{_seg({'exp': exp_epoch, 'amr': list(amr)})}.sig"


class _FakeResp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


@pytest.fixture
def client(tmp_path):
    sf = str(tmp_path / 'sess.pkl')
    c = wqb_api.WqbApiClient('u@e.com', 'pw', session_file=sf)
    return c


def _set_cookie(client, token):
    client.session.cookies.update({'t': token})


# ── JWT 만료 파싱 (.meta 독립) ────────────────────────────────────────────────

def test_expiry_read_from_jwt_without_network(client):
    exp = time.time() + 4 * 3600
    _set_cookie(client, _jwt(exp))
    got = client._expiry_from_jwt()
    assert abs(got - exp) < 1
    assert client.auth_methods() == ['pwd', 'face']


def test_ensure_expiry_prefers_jwt_over_stale_meta(client, monkeypatch):
    """핵심 회귀: .meta 가 3일 전 값이어도 JWT 가 진실이라 정확히 학습한다."""
    client._expiry_epoch = time.time() - 3 * 86400   # 썩은 .meta 로드 흉내
    fresh = time.time() + 4 * 3600
    _set_cookie(client, _jwt(fresh))

    def _no_net(*a, **kw):
        raise AssertionError('JWT 로 알 수 있으면 네트워크를 타면 안 된다')
    monkeypatch.setattr(client.session, 'get', _no_net)
    client._ensure_expiry()
    assert abs(client._expiry_epoch - fresh) < 1


def test_expiry_stale_detection(client):
    client._expiry_epoch = time.time() - 10
    assert client._expiry_stale() is True
    client._expiry_epoch = time.time() + 3600
    assert client._expiry_stale() is False


def test_seconds_to_expiry(client):
    _set_cookie(client, _jwt(time.time() + 1800))
    assert 1700 < client.seconds_to_expiry() < 1900


# ── refresh_token — 쿠키 보존이 생명 ─────────────────────────────────────────

def test_refresh_keeps_cookies_on_success(client, monkeypatch):
    exp0 = time.time() + 20 * 60
    _set_cookie(client, _jwt(exp0))
    client._expiry_epoch = exp0
    exp1 = time.time() + 4 * 3600
    posted = {}

    cleared = {'called': False}
    monkeypatch.setattr(client, '_clear_session_cookies',
                        lambda: cleared.update(called=True))

    def _post(url, **kw):
        posted['url'] = url
        _set_cookie(client, _jwt(exp1))   # WQB 가 새 토큰 쿠키를 내려준다
        return _FakeResp(201, {'user': {'id': 1}})
    monkeypatch.setattr(client.session, 'post', _post)

    res = client.refresh_token()
    assert res == 'refreshed'
    assert cleared['called'] is False, '쿠키를 지우면 그게 얼굴 인증을 부른다'
    assert '/authentication' in posted['url']
    assert abs(client._expiry_epoch - exp1) < 2


def test_refresh_on_persona_does_not_clear_session(client, monkeypatch):
    """가설이 틀려 persona 가 와도, 살아있는 세션을 절대 버리지 않는다.

    그리고 저장되는 pending 은 **알림용 마커**여야 한다(source='refresh', cookies={}).
    예전엔 세션 jar 통째(살아있는 JWT `t`)로 저장했는데, 그 challenge 를 해석하면 죽은
    hosted 세션이 나와 사용자가 열 때마다 'session expired' 가 떴다(2026-07-16 사장 보고).
    """
    _set_cookie(client, _jwt(time.time() + 20 * 60))
    client._expiry_epoch = time.time() + 20 * 60
    cleared = {'called': False}
    monkeypatch.setattr(client, '_clear_session_cookies',
                        lambda: cleared.update(called=True))
    monkeypatch.setattr(client.session, 'post',
                        lambda *a, **kw: _FakeResp(
                            401, {'inquiry': 'INQ123'},
                            headers={'WWW-Authenticate': 'persona'}))
    saved = {}
    monkeypatch.setattr(client, '_save_pending',
                        lambda url, cookies=None, source=None:
                        saved.update(url=url, cookies=cookies, source=source))
    res = client.refresh_token()
    assert res == 'persona'
    assert cleared['called'] is False
    assert 't' in client.session.cookies.get_dict(), '쿠키가 살아있어야 워커가 만료까지 돈다'
    assert saved.get('source') == 'refresh', '선제갱신 challenge 는 마커로 저장해야 한다'
    assert saved.get('cookies') == {}, '살아있는 JWT 를 pending 에 실으면 해석이 죽은 세션을 낳는다'


def test_refresh_pending_marker_is_never_resolved(client, monkeypatch):
    """선제갱신 마커(source='refresh')는 resolve=True 로도 절대 해석되지 않는다 —
    해석(GET /authentication/persona)이 죽은 hosted 세션을 낳기 때문."""
    _set_cookie(client, _jwt(time.time() + 20 * 60))
    monkeypatch.setattr(client.session, 'post',
                        lambda *a, **kw: _FakeResp(
                            401, {'inquiry': 'INQ123'},
                            headers={'WWW-Authenticate': 'persona'}))
    assert client.refresh_token() == 'persona'

    def _no_resolve(*a, **kw):
        raise AssertionError('마커 pending 은 해석(네트워크)하면 안 된다')
    monkeypatch.setattr(wqb_api, '_public_persona_url', _no_resolve)
    monkeypatch.setattr(client.session, 'get', _no_resolve)

    pend = client.pending_persona(resolve=True)
    assert pend is not None
    assert pend.get('persona_url') == '', '마커에서 URL 이 나오면 안 된다'
    assert pend.get('source') == 'refresh'
    assert pend.get('inquiry') == 'INQ123'


def test_mint_challenge_mints_clean_even_with_valid_session(client, monkeypatch):
    """만료 전 선인증의 핵심: 세션이 살아 있어도 mint_challenge() 는 기존 pending(마커)을
    버리고 **쿠키를 비운 맨몸 POST** 로 fresh challenge 를 발급한다 — 만료-후와 동일한
    검증된 경로. 살아있는 세션 파일은 건드리지 않는다."""
    _set_cookie(client, _jwt(time.time() + 20 * 60))
    client._save_pending('https://api.worldquantbrain.com/authentication/persona?inquiry=OLD',
                         cookies={}, source='refresh')

    posted = {'cookies_at_post': None}

    def _post(url, **kw):
        posted['cookies_at_post'] = dict(client.session.cookies.get_dict())
        client.session.cookies.update({'challenge': 'ck1'})   # WQB 가 challenge 쿠키를 내려줌
        return _FakeResp(401, {'inquiry': 'INQ_FRESH'},
                         headers={'WWW-Authenticate': 'persona',
                                  'Content-Type': 'application/json'})
    monkeypatch.setattr(client.session, 'post', _post)

    ok = client.mint_challenge()
    assert ok is False and client.persona_required is True
    assert posted['cookies_at_post'] == {}, '맨몸 POST 여야 한다 — 살아있는 JWT 를 실으면 안 된다'

    pend = client._read_pending()
    assert pend is not None
    assert str(pend.get('source') or '') != 'refresh', 'fresh challenge 는 마커가 아니다'
    assert 'INQ_FRESH' in (pend.get('persona_url') or '')
    assert pend.get('cookies', {}).get('challenge') == 'ck1', \
        'challenge 응답 쿠키가 finalize 바인딩으로 저장돼야 한다'


def test_refresh_once_per_token(client, monkeypatch):
    """같은 토큰으로는 한 번만 시도한다 — /authentication 연타 = biometric throttle."""
    exp = time.time() + 20 * 60
    _set_cookie(client, _jwt(exp))
    client._expiry_epoch = exp
    calls = {'n': 0}

    def _post(*a, **kw):
        calls['n'] += 1
        return _FakeResp(500)
    monkeypatch.setattr(client.session, 'post', _post)
    client.refresh_token()
    client.refresh_token()          # 같은 토큰 — 스킵돼야 한다
    assert calls['n'] == 1


def test_refresh_skips_when_pending_challenge_alive(client, monkeypatch):
    _set_cookie(client, _jwt(time.time() + 20 * 60))
    monkeypatch.setattr(client, '_read_pending', lambda: {'persona_url': 'x'})
    posted = {'n': 0}
    monkeypatch.setattr(client.session, 'post',
                        lambda *a, **kw: posted.update(n=posted['n'] + 1))
    assert client.refresh_token() == 'skipped'
    assert posted['n'] == 0, '인증 진행 중엔 POST 를 하면 그 페이지가 죽는다'


def test_refresh_disabled_by_flag(client, monkeypatch):
    monkeypatch.setattr(wqb_api, 'SESSION_KEEPALIVE', False)
    assert client.refresh_token() == 'skipped'


def test_refresh_resets_attempt_on_new_token(client, monkeypatch):
    """갱신 성공(=새 토큰)하면 다음 토큰에 대해 다시 시도할 수 있어야 한다."""
    _set_cookie(client, _jwt(time.time() + 20 * 60))
    client._expiry_epoch = time.time() + 20 * 60

    def _post(*a, **kw):
        _set_cookie(client, _jwt(time.time() + 4 * 3600))
        return _FakeResp(201, {'user': {'id': 1}})
    monkeypatch.setattr(client.session, 'post', _post)
    assert client.refresh_token() == 'refreshed'
    # 새 토큰이므로 시도 카운터가 리셋됐어야 한다.
    assert client._refresh_attempted_for_current_token() is False


# ── complete_persona 가 만료를 캡처하는가 (.meta 정지 버그의 근본 수정) ────────

def test_complete_persona_captures_expiry(client, monkeypatch):
    fresh = time.time() + 4 * 3600
    monkeypatch.setattr(client, '_read_pending',
                        lambda: {'persona_url': 'https://api.worldquantbrain.com/'
                                                'authentication/persona?inquiry=INQ',
                                 'cookies': {}})

    def _post(url, **kw):
        _set_cookie(client, _jwt(fresh))
        return _FakeResp(201, {'user': {'id': 1}})
    monkeypatch.setattr(client.session, 'post', _post)
    ok = client.complete_persona(inquiry='INQ')
    assert ok is True
    assert client._expiry_epoch is not None
    assert abs(client._expiry_epoch - fresh) < 2, 'complete 후 만료를 학습해야 한다'
