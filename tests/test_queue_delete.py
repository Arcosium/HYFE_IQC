# tests/test_queue_delete.py
# 2026-07-28 사장 지시: 제출 대기 목록에서 선택 삭제.
import pytest

from server import db


@pytest.fixture
def uid(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'd.db'))
    db._INITIALIZED = False
    db.init()
    yield db.upsert_user('d@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    db._INITIALIZED = False


def _add(uid, aid):
    db.submit_queue_add(uid, wqb_alpha_id=aid, kind='budget',
                        code='rank(close)', note='', metrics={})
    return [r['id'] for r in db.submit_queue_list(uid) if r['wqb_alpha_id'] == aid][0]


def test_selected_rows_are_removed(uid):
    a, b, c = _add(uid, 'A'), _add(uid, 'B'), _add(uid, 'C')
    assert db.submit_queue_delete(uid, [a, c]) == 2
    assert [r['wqb_alpha_id'] for r in db.submit_queue_list(uid)] == ['B']
    assert b  # 남은 행 id 는 유효


def test_other_users_rows_are_untouched(uid, tmp_path):
    """큐는 사용자별 제출 예약이다 — 남의 것을 지우면 그 사람 제출이 사라진다."""
    other = db.upsert_user('other@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    mine = _add(uid, 'MINE')
    db.submit_queue_add(other, wqb_alpha_id='THEIRS', kind='budget',
                        code='rank(open)', note='', metrics={})
    theirs = [r['id'] for r in db.submit_queue_list(other)][0]
    assert db.submit_queue_delete(uid, [theirs]) == 0
    assert len(db.submit_queue_list(other)) == 1
    assert db.submit_queue_delete(uid, [mine]) == 1


def test_unknown_and_garbage_ids_are_ignored(uid):
    a = _add(uid, 'A')
    assert db.submit_queue_delete(uid, [999999]) == 0
    assert db.submit_queue_delete(uid, []) == 0
    assert db.submit_queue_delete(uid, None) == 0
    assert db.submit_queue_delete(uid, ['abc', None]) == 0
    assert db.submit_queue_delete(uid, [a, 999999]) == 1


def test_delete_is_idempotent(uid):
    a = _add(uid, 'A')
    assert db.submit_queue_delete(uid, [a]) == 1
    assert db.submit_queue_delete(uid, [a]) == 0
