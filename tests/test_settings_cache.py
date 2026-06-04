"""
Tasks 5+6: insert_alpha settings 컬럼 저장 + lookup_alpha_by_hash settings_fp 필터 테스트.

격리 메커니즘: monkeypatch.setattr(db, 'DB_PATH', ...) + db._INITIALIZED = False + db.init()
실제 data/hyfe_iqc.db 는 절대 건드리지 않는다.
"""
from __future__ import annotations

import pytest

from server import db
from server import settings_fp as sf


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """db.DB_PATH → tmp_path/t.db 로 교체하고 init() 전 상태로 되돌린다."""
    tmp_db = str(tmp_path / 't.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_db
    db._INITIALIZED = False


@pytest.fixture
def user_and_round(isolated_db):
    """FK 충족을 위한 user + round 행 생성. (uid, round_id, round_num) 반환."""
    uid = db.upsert_user('testuser@wq.com', 'pw123', 'gemini-key-test')
    round_num = 1
    round_id = db.start_round(uid, round_num)
    return uid, round_id, round_num


# ── 1. 동일 settings → 캐시 히트 ──────────────────────────────────────────────
def test_cache_hit_same_settings(user_and_round):
    uid, round_id, round_num = user_and_round

    code = 'rank(close / open)'
    settings = {'universe': 'TOP500'}
    delay = '1'

    alpha = {
        'code': code,
        'settings': settings,
        'delay': delay,
        'pass_count': 7,
        'idx': 0,
    }
    db.insert_alpha(uid, round_id, round_num, alpha)

    fp = sf.settings_fingerprint(sf.effective_settings(settings, delay))
    result = db.lookup_alpha_by_hash(uid, db.code_hash(code), fp)

    assert result is not None, "동일 settings_fp 로 조회 시 캐시 히트여야 함"
    assert result['pass_count'] == 7
    assert result['settings_fp'] == fp
    assert result['universe'] == 'TOP500'


# ── 2. 다른 settings → 캐시 미스 ─────────────────────────────────────────────
def test_cache_miss_different_settings(user_and_round):
    uid, round_id, round_num = user_and_round

    code = 'rank(close / open)'
    settings = {'universe': 'TOP500'}
    delay = '1'

    alpha = {
        'code': code,
        'settings': settings,
        'delay': delay,
        'pass_count': 7,
        'idx': 0,
    }
    db.insert_alpha(uid, round_id, round_num, alpha)

    # 다른 universe 로 계산한 fingerprint
    other_fp = sf.settings_fingerprint(sf.effective_settings({'universe': 'TOP3000'}, delay))
    result = db.lookup_alpha_by_hash(uid, db.code_hash(code), other_fp)

    assert result is None, "다른 settings_fp 로 조회하면 None 이어야 함 (캐시 미스)"


# ── 3. legacy 행 (settings_fp=NULL) — fp 지정 시 미스, 미지정 시 히트 ────────
def test_legacy_row_no_fp_is_miss(user_and_round):
    uid, round_id, round_num = user_and_round

    code = 'rank(volume)'

    # 'settings' / 'delay' 키를 넣지 않는 레거시 호출 → settings_fp=NULL
    alpha = {
        'code': code,
        'pass_count': 5,
        'idx': 1,
    }
    db.insert_alpha(uid, round_id, round_num, alpha)

    # 어떤 non-null fp 로 조회해도 미스
    some_fp = sf.settings_fingerprint(sf.effective_settings({}, '1'))
    result_with_fp = db.lookup_alpha_by_hash(uid, db.code_hash(code), some_fp)
    assert result_with_fp is None, (
        "legacy 행(settings_fp=NULL)은 fp 지정 조회에서 None 이어야 함"
    )

    # settings_fp 생략(레거시 동작)하면 히트
    result_legacy = db.lookup_alpha_by_hash(uid, db.code_hash(code))
    assert result_legacy is not None, (
        "legacy 행은 settings_fp 미지정 조회(레거시 동작)에서 히트해야 함"
    )
    assert result_legacy['pass_count'] == 5


# ── 4. result_cache.lookup — settings_fp 매칭/미스 ────────────────────────────
def test_result_cache_lookup_threads_fp(tmp_path, monkeypatch):
    """result_cache.lookup 이 settings_fp 를 기준으로 히트/미스를 구분한다."""
    tmp_db = str(tmp_path / 'rc.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()

    from server import result_cache, settings_fp as sf
    uid = db.upsert_user('rc@wq.com', 'pw', 'k')
    rid = db.start_round(uid, 1)
    code = 'rank(volume)'
    db.insert_alpha(uid, rid, 1, {'idx': 1, 'code': code, 'pass_count': 5,
                                  'settings': {'universe': 'TOP200'}, 'delay': '1'})
    fp = sf.settings_fingerprint(sf.effective_settings({'universe': 'TOP200'}, '1'))
    assert result_cache.lookup(uid, code, fp) is not None
    other = sf.settings_fingerprint(sf.effective_settings({'universe': 'TOP3000'}, '1'))
    assert result_cache.lookup(uid, code, other) is None

    db._INITIALIZED = False
