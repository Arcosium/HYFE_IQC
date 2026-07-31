# tests/test_poll_tail_cut_started.py
# 2026-07-31: 꼬리 절단이 **실행 중** 시뮬을 죽이던 버그.
# WQB 는 실행 중엔 status 없이 progress 만 준다(status 는 완료 때만) — status 로만
# '시작'을 판정하면 progress=0.35 로 돌던 시뮬이 '대기열 미시작'으로 오판된다.
# 7/29~31 실측: tail cut 13건 전부 progress 0.1~0.35 (진짜 미시작 절단 0건).
import threading

from server import wqb_api


class _Resp:
    def __init__(self, code, body=None):
        self.status_code, self.ok, self._b = code, 200 <= code < 300, body or {}
        self.headers = {}

    def json(self):
        return self._b


def _client(responses):
    """responses 를 순서대로 반환, 다 쓰면 마지막 것을 무한 반복."""
    def _get(_s, *a, **k):
        return responses.pop(0) if len(responses) > 1 else responses[0]
    c = wqb_api.WqbApiClient.__new__(wqb_api.WqbApiClient)
    c.session = type('S', (), {'get': _get})()
    c.cancel = lambda url: None
    return c


def _fake_clock(monkeypatch, step=120.0):
    """sleep 마다 step 초 흐르는 가짜 시계. (monotonic, sleep) 반환."""
    clock = {'t': 0.0}
    monkeypatch.setattr(wqb_api._time, 'monotonic', lambda: clock['t'])
    return lambda s: clock.__setitem__('t', clock['t'] + step)


def _set_event():
    ev = threading.Event()
    ev.set()
    return ev


def test_running_sim_with_progress_survives_tail_cut(monkeypatch):
    """progress>0 = 이미 시작한 시뮬 — 형제가 다 끝났어도 자르면 안 된다."""
    sleep = _fake_clock(monkeypatch)  # 3턴이면 유예(600s)를 훌쩍 넘긴다
    c = _client([_Resp(200, {'progress': 0.1}),
                 _Resp(200, {'progress': 0.35}),
                 _Resp(200, {'progress': 0.35}),
                 _Resp(200, {'progress': 0.35}),
                 _Resp(200, {'status': 'COMPLETE', 'alpha': 'A1'})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=sleep,
                 abort_event=_set_event())
    assert (out['status'], out['alpha']) == ('COMPLETE', 'A1')


def test_never_started_sim_is_tail_cut(monkeypatch):
    """진짜 대기열 미시작(빈 본문, progress 없음)은 유예 후 잘라야 한다."""
    sleep = _fake_clock(monkeypatch)
    c = _client([_Resp(200, {})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=sleep,
                 abort_event=_set_event())
    assert out['status'] == 'TIMEOUT' and 'tail cut' in out['message']


def test_progress_zero_still_counts_as_queued(monkeypatch):
    """progress=0.0 은 시작 근거가 못 된다 — 대기열로 보고 자른다."""
    sleep = _fake_clock(monkeypatch)
    c = _client([_Resp(200, {'progress': 0.0})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=sleep,
                 abort_event=_set_event())
    assert out['status'] == 'TIMEOUT' and 'tail cut' in out['message']


def test_fresh_submission_not_cut_before_min_poll_age(monkeypatch):
    """긴 슬롯 대기 끝에 접수된 시뮬 — tail_event 가 이미 켜져 있어도 폴링 나이가
    유예(600s) 미만이면 자르지 않는다(progress 를 읽어 볼 기회를 준다)."""
    sleep = _fake_clock(monkeypatch, step=100.0)
    # 5턴(500s)까지 대기열, 6턴째 progress 등장 → 유예 덕에 살아남아 COMPLETE
    c = _client([_Resp(200, {})] * 5
                + [_Resp(200, {'progress': 0.1}),
                   _Resp(200, {'status': 'COMPLETE', 'alpha': 'A2'})])
    out = c.poll('u', deadline_s=3600, interval_s=0, sleep=sleep,
                 abort_event=_set_event())
    assert (out['status'], out['alpha']) == ('COMPLETE', 'A2')
