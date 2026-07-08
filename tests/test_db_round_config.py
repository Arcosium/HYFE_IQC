"""Test db.update_round_config — writes bandit/generation config-snapshot
columns to a rounds row without touching other columns.

Isolation: monkeypatches db.DB_PATH to a tmp file, resets _INITIALIZED.
"""
import sqlite3

import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """db.DB_PATH → tmp_path/t.db; init fresh schema."""
    tmp_db = str(tmp_path / 't.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_path, tmp_db
    db._INITIALIZED = False


def _create_user_and_round(tmp_db: str) -> tuple[int, int]:
    """Insert a minimal user + round row; return (user_id, round_id)."""
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    import time
    now = time.time()
    cur = conn.execute(
        'INSERT INTO users (wqb_username, wqb_password_enc, gemini_api_key_enc, '
        'created_at, last_login_at, last_validated_at) VALUES (?,?,?,?,?,?)',
        ('test_user', 'enc_pw', 'enc_key', now, now, now),
    )
    uid = cur.lastrowid
    cur2 = conn.execute(
        'INSERT INTO rounds (user_id, round_num, status, started_at) VALUES (?,?,?,?)',
        (uid, 1, 'generating', now),
    )
    rid = cur2.lastrowid
    conn.commit()
    conn.close()
    return uid, rid


def test_update_round_config_writes_columns(isolated_db):
    """update_round_config must persist delay_mode, explore_exploit, injected_arms."""
    _, tmp_db = isolated_db
    _uid, rid = _create_user_and_round(tmp_db)

    db.update_round_config(rid, delay_mode='mix', explore_exploit='3/7',
                           injected_arms='[]')

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT delay_mode, explore_exploit, injected_arms FROM rounds '
                       'WHERE id=?', (rid,)).fetchone()
    conn.close()

    assert row is not None
    assert row['delay_mode'] == 'mix'
    assert row['explore_exploit'] == '3/7'
    assert row['injected_arms'] == '[]'


def test_update_round_config_partial_update(isolated_db):
    """Only provided keys should be updated; others stay NULL."""
    _, tmp_db = isolated_db
    _uid, rid = _create_user_and_round(tmp_db)

    db.update_round_config(rid, delay_mode='0')

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT delay_mode, explore_exploit FROM rounds WHERE id=?',
                       (rid,)).fetchone()
    conn.close()

    assert row['delay_mode'] == '0'
    # explore_exploit was not set → remains NULL
    assert row['explore_exploit'] is None


def test_update_round_config_unknown_keys_ignored(isolated_db):
    """Unknown column names must be silently dropped (no exception)."""
    _, tmp_db = isolated_db
    _uid, rid = _create_user_and_round(tmp_db)

    # Should not raise even though 'bogus_column' does not exist
    db.update_round_config(rid, bogus_column='x', delay_mode='1')

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT delay_mode FROM rounds WHERE id=?', (rid,)).fetchone()
    conn.close()
    assert row['delay_mode'] == '1'


def test_update_round_config_no_fields_noop(isolated_db):
    """Calling with no recognised fields is a no-op (no exception)."""
    _, tmp_db = isolated_db
    _uid, rid = _create_user_and_round(tmp_db)
    db.update_round_config(rid)  # no kwargs → should be harmless


def test_interrupt_open_rounds_marks_unfinished_rounds(isolated_db):
    _tmp_path, tmp_db = isolated_db
    uid, rid = _create_user_and_round(tmp_db)

    count = db.interrupt_open_rounds('restart cleanup')

    assert count == 1
    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT status, ended_at, summary FROM rounds WHERE id=?', (rid,)).fetchone()
    conn.close()
    assert row['status'] == 'interrupted'
    assert row['ended_at'] is not None
    assert row['summary'] == 'restart cleanup'


def test_get_user_status_focus_label_includes_parent_idx(isolated_db):
    _tmp_path, _tmp_db = isolated_db
    uid, _rid = _create_user_and_round(_tmp_db)
    db.interrupt_open_rounds('clear setup round')
    db.start_round(uid, 65, phase=1, parent_idx=3, focus_fail='LOW_SHARPE')

    status = db.get_user_status(uid)

    assert status['current_round_label'] == '65-3-1'
    assert status['current_phase'] == 1
