# tests/test_code_settings_rejected.py
# 같은 식이라도 **설정이 다르면 다른 실험**이다 (2026-08-04 실측: 같은 code_hash 가
# SLOW_AND_FAST 에서 S=2.91, CROWDING 에서 S=0.81 — 3.5배). 시뮬 전 중복 차단은
# code_hash 와 settings_fp 가 **둘 다** 같을 때만 걸려야 한다.
import pytest

from server import db
from server import settings_fp as _sfp

CODE = 'rank(ts_mean(close, 20))'
SAF = {'neutralization': 'SLOW_AND_FAST', 'universe': 'TOPDIV3000', 'decay': '2'}
CROWD = {'neutralization': 'CROWDING', 'universe': 'TOPDIV3000', 'decay': '2'}


def _fp(settings):
    return _sfp.settings_fingerprint(_sfp.effective_settings(settings, '1'))


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """⚠ 라이브 DB 를 절대 건드리지 않는다 — 실 DB 로 돌리면 계정 데이터가 변형된다."""
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'dup.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid, db.start_round(uid, 1)
    db._INITIALIZED = False


def _reject(uid, rid, settings):
    db.insert_alpha(uid, rid, 1, {
        'idx': 1, 'code': CODE, 'desc': '', 'pass_items': [], 'fail_items': [],
        'error_text': '', 'metrics': {}, 'self_corr': None, 'settings': settings,
        'delay': '1', 'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': 0, 'genome': {},
        'submit_status': 'rejected:LOW_SHARPE(1.24 vs 1.58) (http_403)',
    })


def test_same_code_same_settings_is_blocked(isolated_db):
    uid, rid = isolated_db
    _reject(uid, rid, SAF)
    assert db.code_settings_rejected_before(uid, CODE, _fp(SAF))


def test_same_code_other_neutralization_passes(isolated_db):
    """CROWDING 으로 거절된 식을 SLOW_AND_FAST 로 돌리는 건 새 실험 — 막으면 안 된다."""
    uid, rid = isolated_db
    _reject(uid, rid, CROWD)
    assert _fp(SAF) != _fp(CROWD)
    assert db.code_settings_rejected_before(uid, CODE, _fp(SAF)) is None


def test_window_expires(isolated_db):
    uid, rid = isolated_db
    _reject(uid, rid, SAF)
    assert db.code_settings_rejected_before(uid, CODE, _fp(SAF), since_s=0.0) is None


def test_missing_args_are_noop(isolated_db):
    uid, _ = isolated_db
    assert db.code_settings_rejected_before(uid, CODE, '') is None
    assert db.code_settings_rejected_before(uid, '', _fp(SAF)) is None
