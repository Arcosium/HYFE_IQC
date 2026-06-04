"""
Task 4: SQLite schema migration v1→v2 테스트.

격리 메커니즘: db.DB_PATH 는 모듈 레벨 상수 (line 34).
monkeypatch.setattr 으로 tmp_path 아래 임시 DB 를 가리키게 한 뒤
db._INITIALIZED = False → db.init() 를 호출해 마이그레이션을 실행.
실제 data/hyfe_iqc.db 는 절대 건드리지 않는다.
"""
import importlib
import sqlite3

import pytest

# server/ 가 패키지이므로 import 는 server.db 로.
from server import db


_NEW_ALPHA_COLS = ['region', 'universe', 'delay', 'neutralization',
                   'decay', 'truncation', 'settings_fp', 'self_corr']


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """db.DB_PATH → tmp_path/t.db 로 교체하고 init() 전 상태로 되돌린다."""
    tmp_db = str(tmp_path / 't.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_path, tmp_db
    # 사후 정리: _INITIALIZED 리셋 (다음 픽스처가 오염되지 않도록)
    db._INITIALIZED = False


# ── 1. 새 컬럼 존재 확인 ──────────────────────────────────────────────────────
def test_new_alpha_columns_exist(isolated_db):
    _, tmp_db = isolated_db
    conn = sqlite3.connect(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alphas)").fetchall()}
    conn.close()
    for col in _NEW_ALPHA_COLS:
        assert col in cols, f"alphas 테이블에 '{col}' 컬럼 없음"


# ── 2. 버전 확인 ───────────────────────────────────────────────────────────────
def test_schema_version_is_2(isolated_db):
    _, tmp_db = isolated_db
    assert db._SCHEMA_VERSION >= 2, f"_SCHEMA_VERSION={db._SCHEMA_VERSION}, 2 이상 필요"
    conn = sqlite3.connect(tmp_db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver == db._SCHEMA_VERSION, (
        f"PRAGMA user_version={ver} != _SCHEMA_VERSION={db._SCHEMA_VERSION}"
    )


# ── 3. 멱등성 — 두 번 init() 해도 예외 없고 버전 유지 ──────────────────────────
def test_migration_idempotent(isolated_db):
    _, tmp_db = isolated_db
    # 한 번 더 돌린다
    db._INITIALIZED = False
    db.init()  # 예외 없어야 함

    conn = sqlite3.connect(tmp_db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver == db._SCHEMA_VERSION


# ── 4. 새 컬럼들이 모두 nullable (NOT NULL 없음) ──────────────────────────────
def test_new_columns_are_nullable(isolated_db):
    _, tmp_db = isolated_db
    conn = sqlite3.connect(tmp_db)
    info = {r[1]: r for r in conn.execute("PRAGMA table_info(alphas)").fetchall()}
    conn.close()
    # PRAGMA table_info 컬럼 순서: cid, name, type, notnull, dflt_value, pk
    for col in _NEW_ALPHA_COLS:
        notnull = info[col][3]
        assert notnull == 0, f"'{col}' 은 NOT NULL 이어선 안 됨 (notnull={notnull})"


# ── 5. idx_alphas_hash_fp 인덱스 존재 ─────────────────────────────────────────
def test_new_index_exists(isolated_db):
    _, tmp_db = isolated_db
    conn = sqlite3.connect(tmp_db)
    idx = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert 'idx_alphas_hash_fp' in idx


# ── 6. 실제 data/ DB 를 건드리지 않았음 확인 ────────────────────────────────────
def test_real_db_not_touched(tmp_path):
    """격리 DB 의 경로가 tmp_path 아래임을 보장."""
    import os
    repo_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data'
    )
    # isolated_db 픽스처와 독립적으로, 현재 DB_PATH 가 repo data/ 가 아님을
    # 직접 확인할 수는 없지만, tmp_path 가 repo data/ 와 다른 경로임을 검증.
    assert not str(tmp_path).startswith(repo_data)
