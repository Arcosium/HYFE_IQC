# tests/test_submit_mode.py
# 제출 모드 토글 (2026-07-27): 'auto' 자동 제출 | 'list' 대기 목록에만.
import pytest

import server.app as app_mod
from server import db, worker as w


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'sm.db'))
    db._INITIALIZED = False
    db.init()
    yield db.upsert_user('u@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    db._INITIALIZED = False


def test_default_is_auto_so_existing_behaviour_is_unchanged(fresh_db):
    assert db.get_submit_mode(fresh_db) == 'auto'


def test_set_and_get_roundtrip(fresh_db):
    assert db.set_submit_mode(fresh_db, 'list') == 'list'
    assert db.get_submit_mode(fresh_db) == 'list'


def test_unknown_mode_falls_back_to_auto(fresh_db):
    """조용히 제출을 멈추는 쪽이 더 나쁘다 — 알 수 없는 값은 auto 로."""
    assert db.set_submit_mode(fresh_db, 'nonsense') == 'auto'
    assert db.get_submit_mode(fresh_db) == 'auto'


def test_list_mode_queues_instead_of_submitting(fresh_db, monkeypatch):
    db.set_submit_mode(fresh_db, 'list')
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = fresh_db
    wk._corr_fs_hold = set()
    m = {'sharpe': '1.5', 'fitness': '1.2', 'wqb_alpha_id': 'ABC12345'}
    ok, reason = wk._submit_gate(m, None, fail_items=[])
    assert ok is False and reason == 'submit_mode=list→queued'
    rows = [r for r in db.submit_queue_list(fresh_db) if r['kind'] == 'manual']
    assert [r['wqb_alpha_id'] for r in rows] == ['ABC12345']


def test_list_mode_does_not_queue_alphas_wqb_would_reject(fresh_db):
    """차단 FAIL 이 있으면 목록에도 안 넣는다 — 사람이 고를 것이 묻힌다."""
    db.set_submit_mode(fresh_db, 'list')
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = fresh_db
    wk._corr_fs_hold = set()
    ok, reason = wk._submit_gate({'wqb_alpha_id': 'BAD00001'}, None,
                                 fail_items=[{'name': 'LOW_SHARPE'}])
    assert ok is False and reason.startswith('blocking_fail')
    assert db.submit_queue_list(fresh_db) == []


def test_manual_queue_is_not_auto_drained(fresh_db, monkeypatch):
    """kind='manual' 은 사용자가 직접 누를 때만 나간다 — 드레인이 집어가면 토글이 무의미."""
    db.submit_queue_add(fresh_db, wqb_alpha_id='M1', kind='manual', note='n', metrics={})
    assert db.submit_queue_next_pending(fresh_db, kind='budget') is None


def test_api_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(app_mod, '_require_user', lambda: (7, None))
    monkeypatch.setattr(app_mod._db, 'set_submit_mode', lambda uid, m: m)
    c = app_mod.app.test_client()
    assert c.post('/api/submit_mode', json={'submit_mode': 'off'}).status_code == 400
    r = c.post('/api/submit_mode', json={'submit_mode': 'list'})
    assert r.status_code == 200 and r.get_json()['submit_mode'] == 'list'


# ── 목록에서 skipped 제외 (2026-07-27 사장 지시) ────────────────────────────

def test_skipped_rows_are_hidden_from_the_queue_list(fresh_db):
    """skipped 는 '더 이상 낼 일 없음' 결론이라 판단할 것들 사이에 섞이면 안 된다."""
    db.submit_queue_add(fresh_db, wqb_alpha_id='KEEP1', kind='budget', note='n', metrics={})
    db.submit_queue_add(fresh_db, wqb_alpha_id='GONE1', kind='budget', note='n', metrics={})
    gone = [r for r in db.submit_queue_list(fresh_db) if r['wqb_alpha_id'] == 'GONE1'][0]
    db.submit_queue_mark(gone['id'], 'skipped', '노선 폐기')

    ids = [r['wqb_alpha_id'] for r in db.submit_queue_list(fresh_db)]
    assert ids == ['KEEP1']
    # 감사용으로는 여전히 볼 수 있어야 한다 (지우는 게 아니라 감추는 것)
    all_ids = {r['wqb_alpha_id'] for r in db.submit_queue_list(fresh_db, include_skipped=True)}
    assert all_ids == {'KEEP1', 'GONE1'}
