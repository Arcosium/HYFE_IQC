"""Task 1: DB 마이그레이션 v3→v4 (account_type) 테스트.

격리 메커니즘: HYFE_DB_PATH 환경변수로 tmp_path 아래 임시 DB 를 가리키게 한 뒤
importlib.reload(db) → db.init() 를 호출해 마이그레이션을 실행.
실제 data/hyfe_iqc.db 는 절대 건드리지 않는다.

SQLite 버전 메모: 이 서버의 SQLite=3.34.1 (<3.35.0) 이라 ALTER TABLE DROP COLUMN
을 지원하지 않는다. test_migration_backfills_existing_user 는 DROP COLUMN 대신
v3 스키마(account_type 없음)를 직접 생성하는 방식으로 v3→v4 업그레이드를 시뮬레이션.
"""
import importlib
import os
import sqlite3
import tempfile

import pytest


def _fresh_db(tmp_path, monkeypatch):
    dbfile = str(tmp_path / 'iqc.db')
    monkeypatch.setenv('HYFE_DB_PATH', dbfile)  # db.py 가 이 env 로 경로 결정
    import server.db as db
    importlib.reload(db)
    db._INITIALIZED = False
    db.init()
    return db, dbfile


def test_account_type_column_and_default(tmp_path, monkeypatch):
    db, _ = _fresh_db(tmp_path, monkeypatch)
    uid = db.upsert_user('a@b.com', 'pw', 'gkey')
    assert db.get_account_type(uid) == 'standard'


def test_set_and_get_account_type(tmp_path, monkeypatch):
    db, _ = _fresh_db(tmp_path, monkeypatch)
    uid = db.upsert_user('a@b.com', 'pw', 'gkey', account_type='research_consultant')
    assert db.get_account_type(uid) == 'research_consultant'
    db.set_account_type(uid, 'standard')
    assert db.get_account_type(uid) == 'standard'


def test_migration_backfills_existing_user(tmp_path, monkeypatch):
    """v3 스키마(account_type 컬럼 없음)로 DB 를 생성한 뒤,
    init() 가 v4 로 ALTER + 백필하는지 확인.

    SQLite 3.34.1 은 ALTER TABLE DROP COLUMN 미지원이므로
    처음부터 account_type 없는 최소 users 테이블을 직접 만들고
    user_version=3 으로 세팅해 v3 DB 를 시뮬레이션한다.
    """
    dbfile = str(tmp_path / 'iqc_v3.db')
    monkeypatch.setenv('HYFE_DB_PATH', dbfile)

    # v3 스키마: account_type 컬럼 없이 users 테이블만 생성
    with sqlite3.connect(dbfile) as c:
        c.execute('''
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wqb_username TEXT UNIQUE NOT NULL,
                wqb_password_enc TEXT NOT NULL,
                gemini_api_key_enc TEXT NOT NULL,
                last_round_num INTEGER NOT NULL DEFAULT 0,
                running INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL,
                last_validated_at REAL NOT NULL DEFAULT 0,
                submittable_list_created INTEGER NOT NULL DEFAULT 0
            )
        ''')
        c.execute("PRAGMA user_version=3")

    import server.db as db
    importlib.reload(db)
    db._INITIALIZED = False
    db.init()

    with sqlite3.connect(dbfile) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
    assert 'account_type' in cols
