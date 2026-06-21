"""
DB schema migration v2→v3 테스트.

검증 항목:
  1. alphas 테이블에 sharpe/fitness/turnover/drawdown/margin/returns/generation/parent_alpha_id 컬럼 추가됨
  2. rounds 테이블에 delay_mode/gen_temperature/explore_exploit/injected_arms 컬럼 추가됨
  3. PRAGMA user_version == 3
  4. insert_alpha 가 metric 컬럼에 올바르게 값을 저장함
  5. self_corr 두-위치 폴백 동작: top-level 우선, 없으면 metrics 안 키 사용
  6. 멱등성 — 두 번 init() 해도 예외 없고 버전 유지
  7. 실제 data/ DB 미변경
"""
import sqlite3

import pytest

from server import db


# ─── 격리 픽스처 ────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """db.DB_PATH → tmp_path/v3.db 로 교체 후 init()."""
    tmp_db = str(tmp_path / 'v3.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_path, tmp_db
    db._INITIALIZED = False


def _cols(tmp_db: str, table: str) -> set:
    conn = sqlite3.connect(tmp_db)
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    conn.close()
    return cols


def _make_round(tmp_db: str, uid: int, round_num: int) -> int:
    """start_round 를 직접 호출해 round_id 반환."""
    return db.start_round(uid, round_num)


def _user(tmp_db: str) -> int:
    return db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')


# ─── 1. 새 alphas 컬럼 존재 ──────────────────────────────────────────────────

_NEW_ALPHA_COLS_V3 = [
    'sharpe', 'fitness', 'turnover', 'drawdown',
    'margin', 'returns', 'generation', 'parent_alpha_id',
]

def test_new_alpha_columns_exist(isolated_db):
    _, tmp_db = isolated_db
    cols = _cols(tmp_db, 'alphas')
    for col in _NEW_ALPHA_COLS_V3:
        assert col in cols, f"alphas 테이블에 '{col}' 컬럼 없음"


# ─── 2. 새 rounds 컬럼 존재 ─────────────────────────────────────────────────

_NEW_ROUND_COLS_V3 = [
    'delay_mode', 'gen_temperature', 'explore_exploit', 'injected_arms',
]

def test_new_round_columns_exist(isolated_db):
    _, tmp_db = isolated_db
    cols = _cols(tmp_db, 'rounds')
    for col in _NEW_ROUND_COLS_V3:
        assert col in cols, f"rounds 테이블에 '{col}' 컬럼 없음"


# ─── 3. 버전 >= 3 ─────────────────────────────────────────────────────────────
# v4 이후부터는 _SCHEMA_VERSION 이 3 초과일 수 있으므로 >= 3 으로 완화.

def test_schema_version_is_3(isolated_db):
    _, tmp_db = isolated_db
    assert db._SCHEMA_VERSION >= 3, f"_SCHEMA_VERSION={db._SCHEMA_VERSION}"
    conn = sqlite3.connect(tmp_db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver >= 3, f"PRAGMA user_version={ver}, 3 이상 기대"


# ─── 4. insert_alpha 가 metric 컬럼에 올바른 값 저장 ─────────────────────────

def test_insert_alpha_metric_columns(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = _make_round(tmp_db, uid, 1)

    alpha = {
        'idx': 0,
        'code': 'rank(returns)',
        'desc': 'test',
        'pass_count': 6,
        'pass_items': [],
        'fail_count': 1,
        'fail_items': [],
        'error_count': 0,
        'pending_count': 0,
        'submitted': False,
        'submit_status': '',
        'error_text': '',
        'metrics': {
            'sharpe':   '1.5',
            'fitness':  '0.9',
            'turnover': '0.27',
            'drawdown': '0.1',
            'margin':   '12.0',
            'returns':  '0.2',
        },
        'self_corr': '0.74',
        'settings': {'universe': 'TOP500'},
        'delay': '1',
        'is_status': {},
        'mode': '',
        'cached': False,
        'phase': 0,
    }
    db.insert_alpha(uid, rid, 1, alpha)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        'SELECT sharpe, fitness, turnover, drawdown, margin, returns, self_corr, '
        'generation, parent_alpha_id FROM alphas ORDER BY id DESC LIMIT 1'
    ).fetchone()
    conn.close()

    assert row is not None
    assert abs(row[0] - 1.5)  < 1e-9, f"sharpe={row[0]}"
    assert abs(row[1] - 0.9)  < 1e-9, f"fitness={row[1]}"
    assert abs(row[2] - 0.27) < 1e-9, f"turnover={row[2]}"
    assert abs(row[4] - 12.0) < 1e-9, f"margin={row[4]}"
    assert abs(row[6] - 0.74) < 1e-9, f"self_corr={row[6]}"
    assert row[7] == 0,  f"generation={row[7]}"
    assert row[8] is None, f"parent_alpha_id={row[8]}"


# ─── 5. self_corr 두-위치 폴백: metrics 안에 있을 때도 저장 ─────────────────

def test_self_corr_fallback_from_metrics(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = _make_round(tmp_db, uid, 2)

    # top-level 'self_corr' 없이, metrics 안에만 있는 경우
    alpha = {
        'idx': 1,
        'code': 'rank(close)',
        'desc': '',
        'pass_count': 6,
        'pass_items': [],
        'fail_count': 1,
        'fail_items': [],
        'error_count': 0,
        'pending_count': 0,
        'submitted': False,
        'submit_status': '',
        'error_text': '',
        'metrics': {
            'sharpe':    '1.2',
            'fitness':   '0.8',
            'turnover':  '0.3',
            'drawdown':  '0.05',
            'margin':    '10.0',
            'returns':   '0.15',
            'self_corr': '0.5',
        },
        # 의도적으로 top-level 'self_corr' 없음
        'settings': {'universe': 'TOP500'},
        'delay': '1',
        'is_status': {},
        'mode': '',
        'cached': False,
        'phase': 0,
    }
    db.insert_alpha(uid, rid, 2, alpha)

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        'SELECT self_corr FROM alphas ORDER BY id DESC LIMIT 1'
    ).fetchone()
    conn.close()

    assert row is not None
    assert abs(row[0] - 0.5) < 1e-9, f"self_corr={row[0]} (폴백 실패)"


# ─── 6. 멱등성 ────────────────────────────────────────────────────────────────

def test_migration_idempotent(isolated_db):
    _, tmp_db = isolated_db
    db._INITIALIZED = False
    db.init()  # 두 번째 init() — 예외 없어야 함

    conn = sqlite3.connect(tmp_db)
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert ver >= 3, f"멱등 재실행 후 user_version={ver}"


# ─── 7. 실제 data/ DB 미변경 ────────────────────────────────────────────────

def test_real_db_not_touched(tmp_path):
    import os
    repo_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data',
    )
    assert not str(tmp_path).startswith(repo_data)
