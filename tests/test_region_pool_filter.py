# tests/test_region_pool_filter.py
# 리전 전환(2026-07-27 GLB): 재조합·HT구제 풀이 조건 리전 알파만 재료로 쓰는지.
import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'rg.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)
    yield uid, rid
    db._INITIALIZED = False


def _alpha(idx, code, sharpe, region, *, fitness=0.5, turnover=0.5):
    return {
        'idx': idx, 'code': code, 'desc': code, 'pass_count': 3,
        'pass_items': [], 'fail_count': 0, 'fail_items': [], 'error_count': 0,
        'pending_count': 0, 'submitted': False, 'submit_status': '', 'error_text': '',
        'metrics': {'sharpe': str(sharpe), 'fitness': str(fitness),
                    'turnover': str(turnover)},
        'self_corr': None, 'settings': {'universe': 'TOP1000', 'region': region},
        'delay': '1', 'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': 0, 'genome': None,
    }


def test_combine_pool_region_filter(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(0, 'rank(usa_f)', 2.0, 'USA'))
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(glb_f)', 1.8, 'GLB'))
    assert [r['code'] for r in db.combine_pool(uid, region='GLB')] == ['rank(glb_f)']
    assert [r['code'] for r in db.combine_pool(uid, region='USA')] == ['rank(usa_f)']
    # region 미지정이면 기존 동작(전부)
    assert len(db.combine_pool(uid)) == 2


def test_ht_rescue_pool_region_filter(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(0, 'rank(usa_h)', 2.0, 'USA',
                                        fitness=0.6, turnover=0.8))
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(glb_h)', 1.9, 'GLB',
                                        fitness=0.6, turnover=0.8))
    assert [r['code'] for r in db.ht_rescue_pool(uid, region='GLB')] == ['rank(glb_h)']
    assert len(db.ht_rescue_pool(uid)) == 2
