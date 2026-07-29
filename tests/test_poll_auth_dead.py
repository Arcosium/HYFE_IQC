# tests/test_poll_auth_dead.py
# 2026-07-28: 세션이 죽으면(401) 폴링이 마감(3600초)을 꼬박 태우던 것 → 조기 포기.
from server import wqb_api


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self.ok, self._b = code, 200 <= code < 300, body or {}
        self.headers = {}

    def json(self):
        return self._b


def _client(responses):
    c = wqb_api.WqbApiClient.__new__(wqb_api.WqbApiClient)
    c.session = type('S', (), {'get': lambda _s, *a, **k: responses.pop(0)})()
    c.cancel = lambda url: None
    return c


def test_two_consecutive_401_gives_up_early():
    c = _client([_Resp(401), _Resp(401), _Resp(200, {'status': 'COMPLETE'})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)
    assert out['status'] == 'TIMEOUT'
    assert 'auth dead (http_401)' in out['message']


def test_403_also_counts():
    c = _client([_Resp(403), _Resp(403)])
    assert 'http_403' in c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)['message']


def test_single_401_is_tolerated():
    """세션 갱신 레이스로 한 번 튄 401 에 멀쩡한 시뮬을 죽이면 안 된다."""
    c = _client([_Resp(401), _Resp(200, {'status': 'RUNNING', 'progress': 0.1}),
                 _Resp(200, {'status': 'COMPLETE', 'alpha': 'A1'})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)
    assert (out['status'], out['alpha']) == ('COMPLETE', 'A1')


def test_429_queue_waits_are_not_auth_failures():
    """대기열 429 는 회복 가능하다 — 조기 포기 대상이 아니다."""
    c = _client([_Resp(429), _Resp(429), _Resp(429),
                 _Resp(200, {'status': 'COMPLETE', 'alpha': 'A2'})])
    assert c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)['alpha'] == 'A2'


def test_401_rereads_refreshed_session_from_disk():
    """갱신 레이스 — 새 토큰은 이미 디스크(.pkl)에 있고 이 스레드만 옛 쿠키다.

    2026-07-29 실측: 12:06 에 세션 갱신이 **성공**해 있었는데도, 돌고 있던 폴링
    스레드들이 메모리의 옛 쿠키로 401 을 맞아 라운드 17건 중 11건이 통째로 버려졌다.
    디스크에서 다시 읽으면 살아난다(순수 파일 읽기 — 인증 POST 없음 = throttle 무관).
    """
    c = _client([_Resp(401), _Resp(200, {'status': 'COMPLETE', 'alpha': 'A9'})])
    reloads = []
    c._load_session = lambda: (reloads.append(1), True)[1]
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)
    assert (out['status'], out['alpha']) == ('COMPLETE', 'A9')
    assert reloads, '갱신된 세션을 디스크에서 다시 읽지 않았다'


def test_reload_does_not_rescue_a_genuinely_dead_session():
    """다시 읽고도 401 이면 진짜 죽은 것 — 마감(3600초)을 태우지 말고 즉시 포기한다."""
    c = _client([_Resp(401), _Resp(401)])
    c._load_session = lambda: True
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=lambda s: None)
    assert out['status'] == 'TIMEOUT' and 'auth dead' in out['message']
