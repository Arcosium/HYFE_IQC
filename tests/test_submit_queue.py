# tests/test_submit_queue.py
# 제출 대기 큐(2026-07-27): 테마 보류·예산 초과 큐잉 db 헬퍼.
import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'sq.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def test_add_list_dedup(isolated_db):
    uid = isolated_db
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='theme',
                               code='rank(x)', note='PURE',
                               metrics={'sharpe': '1.5'}) is True
    # 같은 (user, wid, kind) 중복은 무시
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='theme') is False
    # 같은 wid 라도 kind 다르면 별개
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='budget') is True
    # wid 없으면 거부
    assert db.submit_queue_add(uid, wqb_alpha_id='', kind='theme') is False
    rows = db.submit_queue_list(uid)
    assert len(rows) == 2
    assert rows[-1]['metrics']['sharpe'] == '1.5'


def test_mark_and_next_pending(isolated_db):
    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='B1', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='B2', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='T1', kind='theme')
    nxt = db.submit_queue_next_pending(uid, kind='budget')
    assert nxt['wqb_alpha_id'] == 'B1'          # 오래된 것부터
    db.submit_queue_mark(nxt['id'], 'submitted', 'ok')
    assert db.submit_queue_get(nxt['id'])['status'] == 'submitted'
    assert db.submit_queue_next_pending(uid, kind='budget')['wqb_alpha_id'] == 'B2'
    # theme 은 자동 드레인 대상이 아니다 — budget 조회에 안 섞임
    db.submit_queue_mark(db.submit_queue_next_pending(uid, 'budget')['id'], 'rejected')
    assert db.submit_queue_next_pending(uid, kind='budget') is None
