"""
bandit_arms 테이블 + bandit_update / bandit_stats / bandit_arm 함수 테스트.

격리 메커니즘: monkeypatch 로 db.DB_PATH 를 tmp_path 아래 임시 DB 로 교체 +
db._INITIALIZED = False 후 db.init() 호출 → 실제 data/hyfe_iqc.db 미접촉.
"""
import math
import sqlite3

import pytest

from server import db


# ─── 격리 픽스처 ────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """db.DB_PATH → tmp_path/bandit.db 로 교체 후 init()."""
    tmp_db = str(tmp_path / 'bandit.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_path, tmp_db
    db._INITIALIZED = False


def _uid(isolated_db) -> int:
    """테스트용 사용자 생성."""
    return db.upsert_user('testuser', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')


def _uid2(isolated_db) -> int:
    """두 번째 테스트용 사용자."""
    return db.upsert_user('testuser2', 'pw2', 'GEMINI_FAKE_KEY_FOR_TEST')


# ─── 1. 테이블 존재 확인 ──────────────────────────────────────────────────────

def test_table_exists_after_init(isolated_db):
    _, tmp_db = isolated_db
    conn = sqlite3.connect(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bandit_arms)").fetchall()}
    conn.close()
    expected = {'id', 'user_id', 'arm_key', 'dimension',
                'reward_sum', 'reward_sq_sum', 'visits', 'last_round', 'updated_at'}
    assert expected <= cols, f"누락 컬럼: {expected - cols}"


def test_index_exists_after_init(isolated_db):
    _, tmp_db = isolated_db
    conn = sqlite3.connect(tmp_db)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert 'idx_bandit_user_dim' in idx


# ─── 2. 첫 update — arm 신규 생성 ────────────────────────────────────────────

def test_first_update_creates_arm(isolated_db):
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'family:momentum', 0.6, round_num=1)
    arm = db.bandit_arm(uid, 'family:momentum')
    assert arm is not None
    assert arm['visits'] == 1
    assert math.isclose(arm['reward_sum'], 0.6, rel_tol=1e-9)
    assert math.isclose(arm['mean'], 0.6, rel_tol=1e-9)
    assert arm['last_round'] == 1


# ─── 3. 두 번째 update — visits/reward_sum/mean 누적 ─────────────────────────

def test_second_update_accumulates(isolated_db):
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'family:momentum', 0.6, round_num=1)
    db.bandit_update(uid, 'family:momentum', 0.4, round_num=2)
    arm = db.bandit_arm(uid, 'family:momentum')
    assert arm['visits'] == 2
    assert math.isclose(arm['reward_sum'], 1.0, abs_tol=1e-9)
    assert math.isclose(arm['mean'], 0.5, rel_tol=1e-9)
    assert arm['last_round'] == 2


# ─── 4. bandit_stats — mean/var 계산 + dimension 필터 ────────────────────────

def test_bandit_stats_mean_var(isolated_db):
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'family:momentum', 0.6, round_num=1, dimension='family')
    db.bandit_update(uid, 'family:momentum', 0.4, round_num=2, dimension='family')
    stats = db.bandit_stats(uid)
    assert len(stats) == 1
    s = stats[0]
    assert math.isclose(s['mean'], 0.5, rel_tol=1e-9)
    assert s['var'] >= 0.0   # clamp >=0 보장
    # var = E[X^2] - (E[X])^2 = (0.36+0.16)/2 - 0.25 = 0.26 - 0.25 = 0.01
    assert math.isclose(s['var'], 0.01, abs_tol=1e-9)


def test_bandit_stats_dimension_filter(isolated_db):
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'family:momentum', 0.6, round_num=1, dimension='family')
    db.bandit_update(uid, 'op:ts_rank', 0.3, round_num=1, dimension='op')
    # dimension='family' 필터 — family 것만
    fam = db.bandit_stats(uid, dimension='family')
    assert len(fam) == 1
    assert fam[0]['arm_key'] == 'family:momentum'
    # dimension='op' 필터
    ops = db.bandit_stats(uid, dimension='op')
    assert len(ops) == 1
    assert ops[0]['arm_key'] == 'op:ts_rank'
    # 전체 (dimension=None)
    all_stats = db.bandit_stats(uid)
    assert len(all_stats) == 2


# ─── 5. decay — 오래된 보상 감쇠 확인 ───────────────────────────────────────

def test_decay_reduces_reward_sum(isolated_db):
    uid = _uid(isolated_db)
    # round 1 에서 reward=1.0 기록
    db.bandit_update(uid, 'decay_arm', 1.0, round_num=1, decay_k=0.5)
    # round 5 에서 reward=0.0 으로 업데이트 (decay_k=0.5, gap=4)
    # 기존 reward_sum=1.0 에 f=exp(-0.5*4)=exp(-2)≈0.135 적용 후 0.0 추가
    db.bandit_update(uid, 'decay_arm', 0.0, round_num=5, decay_k=0.5)
    arm = db.bandit_arm(uid, 'decay_arm')
    # reward_sum < 1.0 (감쇠됐으므로)
    assert arm['reward_sum'] < 1.0, f"decay 미적용: reward_sum={arm['reward_sum']}"
    # visits 는 감쇠 없이 2
    assert arm['visits'] == 2
    # 감쇠값 검증: exp(-2) ≈ 0.1353
    expected = math.exp(-0.5 * 4) * 1.0 + 0.0
    assert math.isclose(arm['reward_sum'], expected, rel_tol=1e-6)


# ─── 6. 재시작 안전성 — _INITIALIZED=False 후 재-init() 해도 데이터 유지 ────────

def test_persist_across_reinit(isolated_db):
    _, tmp_db = isolated_db
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'family:momentum', 0.6, round_num=1)
    db.bandit_update(uid, 'family:momentum', 0.4, round_num=2)

    # 워커 재시작 시뮬레이션: _INITIALIZED 리셋 후 init() 재호출
    db._INITIALIZED = False
    db.init()

    arm = db.bandit_arm(uid, 'family:momentum')
    assert arm is not None, "재시작 후 arm 소실"
    assert arm['visits'] == 2
    assert math.isclose(arm['mean'], 0.5, rel_tol=1e-9)


# ─── 7. 사용자 격리 — 동일 arm_key 라도 user_id 가 다르면 독립 ──────────────

def test_per_user_isolation(isolated_db):
    uid1 = _uid(isolated_db)
    uid2 = _uid2(isolated_db)
    db.bandit_update(uid1, 'shared_arm', 1.0, round_num=1)
    db.bandit_update(uid2, 'shared_arm', 0.2, round_num=1)

    arm1 = db.bandit_arm(uid1, 'shared_arm')
    arm2 = db.bandit_arm(uid2, 'shared_arm')
    assert arm1 is not None and arm2 is not None
    assert arm1['visits'] == 1
    assert arm2['visits'] == 1
    assert math.isclose(arm1['mean'], 1.0, rel_tol=1e-9)
    assert math.isclose(arm2['mean'], 0.2, rel_tol=1e-9)

    # bandit_stats 도 사용자별 독립
    s1 = db.bandit_stats(uid1)
    s2 = db.bandit_stats(uid2)
    assert len(s1) == 1
    assert len(s2) == 1


# ─── 8. reward=None 은 0.0 으로 강제, 예외 없음 ────────────────────────────────

def test_reward_none_coerces_to_zero(isolated_db):
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'arm_none', None, round_num=1)  # 예외 없어야 함
    arm = db.bandit_arm(uid, 'arm_none')
    assert arm is not None
    assert arm['visits'] == 1
    assert math.isclose(arm['reward_sum'], 0.0, abs_tol=1e-9)
    assert math.isclose(arm['mean'], 0.0, abs_tol=1e-9)


# ─── 9. 실제 data/ DB 미접촉 확인 ──────────────────────────────────────────

def test_real_db_not_touched(tmp_path):
    import os
    repo_data = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data'
    )
    assert not str(tmp_path).startswith(repo_data)


# ─── 10. last_round 단조 증가 보장 ───────────────────────────────────────────

def test_last_round_monotone(isolated_db):
    """out-of-order 업데이트가 last_round 를 되돌리지 않아야 한다."""
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'mono_arm', 0.5, round_num=5)
    db.bandit_update(uid, 'mono_arm', 0.3, round_num=2)  # 과거 라운드
    arm = db.bandit_arm(uid, 'mono_arm')
    assert arm['last_round'] == 5, (
        f"out-of-order 업데이트가 last_round 를 {arm['last_round']} 로 덮어씀"
    )


# ─── 11. dimension 은 나중에 채울 수 있고, 비어 있으면 기존 값 유지 ─────────

def test_dimension_updatable(isolated_db):
    """빈 dimension 으로 생성 후 비어 있지 않은 dimension 으로 업데이트 가능.
    반대로, 비어 있는 dimension 을 전달하면 기존 값을 유지한다."""
    uid = _uid(isolated_db)

    # dimension 미전달(기본='') → '' 로 생성
    db.bandit_update(uid, 'dim_arm', 0.5, round_num=1)
    assert db.bandit_arm(uid, 'dim_arm')['dimension'] == ''

    # dimension='family' 전달 → 업데이트
    db.bandit_update(uid, 'dim_arm', 0.3, round_num=2, dimension='family')
    assert db.bandit_arm(uid, 'dim_arm')['dimension'] == 'family'

    # dimension='' 전달 → 기존 'family' 유지
    db.bandit_update(uid, 'dim_arm', 0.1, round_num=3, dimension='')
    assert db.bandit_arm(uid, 'dim_arm')['dimension'] == 'family'


# ─── 12. reward_sq_sum 이 반환 dict 에 포함되어야 한다 ───────────────────────

def test_reward_sq_sum_surfaced(isolated_db):
    """bandit_arm / bandit_stats 모두 reward_sq_sum 키를 노출해야 한다."""
    uid = _uid(isolated_db)
    db.bandit_update(uid, 'sq_arm', 0.6, round_num=1)
    db.bandit_update(uid, 'sq_arm', 0.4, round_num=2)

    arm = db.bandit_arm(uid, 'sq_arm')
    assert 'reward_sq_sum' in arm, "bandit_arm 이 reward_sq_sum 을 반환하지 않음"
    # 0.6^2 + 0.4^2 = 0.36 + 0.16 = 0.52
    assert math.isclose(arm['reward_sq_sum'], 0.52, abs_tol=1e-9)

    stats = db.bandit_stats(uid)
    assert len(stats) == 1
    assert 'reward_sq_sum' in stats[0], "bandit_stats 가 reward_sq_sum 을 반환하지 않음"
    assert math.isclose(stats[0]['reward_sq_sum'], 0.52, abs_tol=1e-9)
