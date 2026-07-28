# tests/test_queue_done_rows.py
# 2026-07-28 사장 지시 2건:
#   ① 제출 성공하면 대기 목록에서 바로 내린다.
#   ② 이미 제출된 알파를 큐에서 다시 누르면 WQB 가 거절하는데, 그걸 '거절' 로 적으면
#      제출된 알파가 거절 상태로 큐에 눌러앉는다(gJ9ea3ZJ).
import pytest

from server import db
from server.wqb_api import WqbApiClient


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'q.db'))
    db._INITIALIZED = False
    db.init()
    yield db.upsert_user('q@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    db._INITIALIZED = False


def _add(uid, aid):
    return db.submit_queue_add(uid, wqb_alpha_id=aid, kind='budget',
                               code='rank(close)', note='', metrics={})


def test_submitted_row_leaves_the_waiting_list(uid):
    _add(uid, 'DONE1')
    qid = [r['id'] for r in db.submit_queue_list(uid) if r['wqb_alpha_id'] == 'DONE1'][0]
    db.submit_queue_mark(qid, 'submitted', 'submitted')
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == []


def test_pending_and_rejected_stay_because_they_still_need_a_decision(uid):
    _add(uid, 'WAIT1')
    _add(uid, 'REJ1')
    qid = [r['id'] for r in db.submit_queue_list(uid) if r['wqb_alpha_id'] == 'REJ1'][0]
    db.submit_queue_mark(qid, 'rejected', 'rejected:LOW_SHARPE(1.0 vs 1.58)')
    assert {r['wqb_alpha_id'] for r in db.submit_queue_list(uid)} == {'WAIT1', 'REJ1'}


def test_audit_view_still_shows_everything(uid):
    _add(uid, 'DONE1')
    qid = [r['id'] for r in db.submit_queue_list(uid)][0]
    db.submit_queue_mark(qid, 'submitted', 'submitted')
    assert len(db.submit_queue_list(uid, include_skipped=True)) == 1


# ── ② 이미 제출된 알파의 403 은 성공으로 정정된다 ───────────────────────────

class _Resp:
    def __init__(self, code, js):
        self.status_code, self._js, self.headers, self.text = code, js, {}, ''

    def json(self):
        return self._js


def _client(monkeypatch, verify_result):
    c = WqbApiClient.__new__(WqbApiClient)
    c._authed = True
    c._ensure_auth = lambda: True
    c._verify_submitted = lambda aid: verify_result
    body = {'is': {'checks': [
        {'name': 'LOW_SHARPE', 'result': 'FAIL', 'limit': 1.58, 'value': 1.04}]}}
    c.session = type('S', (), {
        'post': lambda *a, **k: _Resp(403, body),
        'get': lambda *a, **k: _Resp(403, body)})()
    return c


def test_403_on_an_already_submitted_alpha_is_reported_as_success(monkeypatch):
    ok, st = _client(monkeypatch, True).submit_alpha('gJ9ea3ZJ')
    assert ok is True and 'submitted' in st


def test_403_on_a_genuinely_failing_alpha_is_still_a_rejection(monkeypatch):
    ok, st = _client(monkeypatch, False).submit_alpha('X1')
    assert ok is False
    assert st.startswith('rejected:LOW_SHARPE(1.04 vs 1.58)') and 'http_403' in st
