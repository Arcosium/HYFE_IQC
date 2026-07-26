# tests/test_yield_score.py
# v8 Yield Score: bandit_arms.pass_sum 마이그레이션 + update/stats 의 yield 노출.
import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'y.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def test_pass_sum_accumulates_and_yield_exposed(isolated_db):
    uid = isolated_db
    db.bandit_update(uid, 'family:pv', 0.3, 1, dimension='family', passed=True)
    db.bandit_update(uid, 'family:pv', 0.0, 2, dimension='family', passed=False)
    db.bandit_update(uid, 'family:pv', 0.5, 3, dimension='family', passed=True)
    arm = db.bandit_arm(uid, 'family:pv')
    assert arm['visits'] == 3
    assert arm['pass_sum'] == 2
    assert arm['yield'] == pytest.approx(2 / 3)
    rows = db.bandit_stats(uid, dimension='family')
    assert rows[0]['pass_sum'] == 2 and rows[0]['yield'] == pytest.approx(2 / 3)


def test_passed_default_false_backcompat(isolated_db):
    uid = isolated_db
    # 구 호출부 시그니처(passed 미전달)도 그대로 동작해야 한다.
    db.bandit_update(uid, 'universe:TOP3000', 0.1, 1, dimension='universe')
    arm = db.bandit_arm(uid, 'universe:TOP3000')
    assert arm['pass_sum'] == 0 and arm['yield'] == 0.0


def test_fresh_db_has_pass_sum_column(isolated_db):
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(bandit_arms)').fetchall()}
    conn.close()
    assert 'pass_sum' in cols
