# tests/test_family_corr_economy.py
# ④ 패밀리 상관검사 절약: corr 거절 필드셋 조회 + 오늘 제출 필드셋 dedup.
import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'fam.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)
    yield uid, rid
    db._INITIALIZED = False


def _alpha(idx, code, *, fields, submit_status='', submitted=False):
    return {
        'idx': idx, 'code': code, 'desc': code, 'pass_count': 3,
        'pass_items': [], 'fail_count': 0, 'fail_items': [], 'error_count': 0,
        'pending_count': 0, 'submitted': submitted, 'submit_status': submit_status,
        'error_text': '', 'metrics': {'sharpe': '1.5', 'turnover': '0.1'},
        'self_corr': None, 'settings': {'universe': 'TOP3000'}, 'delay': '1',
        'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': 0,
        'genome': {'model': 'rc-api-genome', 'family': 'pv', 'fields': fields},
    }


def test_corr_rejected_fieldsets_one_strike(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(
        0, 'rank(a1)', fields=['mdl177_x', 'mdl177_y'],
        submit_status='rejected:SELF_CORRELATION (http_403)'))
    db.insert_alpha(uid, rid, 1, _alpha(
        1, 'rank(a2)', fields=['anl4_z'],
        submit_status='rejected:LOW_SHARPE (http_403)'))
    # 상관 사유만 + 1회로도 잡힌다 (패밀리 대표 1회 거절 = 형제 보류).
    # 2026-08-03 부터 유전체 fields 와 코드 추출 **두 표현을 모두** 센다 — 유전체가
    # fields 를 안 담는 알파가 많아 한쪽만 보면 벽이 무력화된다(db.rejected_fieldsets).
    # 그래서 코드 `rank(a1)` 에서 뽑힌 {'a1'} 도 같이 나온다. 잡아야 할 쪽만 확인한다.
    walls = db.rejected_fieldsets(uid, min_count=1, reason_contains='CORRELATION')
    assert frozenset({'mdl177_x', 'mdl177_y'}) in walls
    assert frozenset({'anl4_z'}) not in walls      # 상관 아닌 사유는 안 센다
    # 기존 호출(3회 문턱)은 그대로 빈 결과 — 하위호환.
    assert db.rejected_fieldsets(uid) == []


def test_submitted_fieldsets_today(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(
        0, 'rank(b1)', fields=['anl4_a', 'anl4_b'], submitted=True))
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(b2)', fields=['opt6_c']))
    today = db.submitted_fieldsets_today(uid)
    assert today == [frozenset({'anl4_a', 'anl4_b'})]
