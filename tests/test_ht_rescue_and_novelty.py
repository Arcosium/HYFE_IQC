# tests/test_ht_rescue_and_novelty.py
# A. HT 구제 풀(db.ht_rescue_pool) + B. 신규성 압력(worker._novelty_rewrite).
import pytest

from server import db, worker


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'ht.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)
    yield uid, rid
    db._INITIALIZED = False


def _alpha(idx, code, sharpe, fitness, turnover):
    return {
        'idx': idx, 'code': code, 'desc': code, 'pass_count': 3,
        'pass_items': [], 'fail_count': 1, 'fail_items': ['LOW_FITNESS'],
        'error_count': 0, 'pending_count': 0, 'submitted': False,
        'submit_status': '', 'error_text': '',
        'metrics': {'sharpe': str(sharpe), 'fitness': str(fitness),
                    'turnover': str(turnover)},
        'self_corr': None, 'settings': {'universe': 'TOP1000'}, 'delay': '1',
        'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': 0, 'genome': None,
    }


def test_ht_rescue_pool_targets_high_sharpe_high_turnover(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(0, 'rank(a)', 1.9, 0.6, 0.55))   # 구제 대상
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(b)', 1.9, 1.2, 0.55))   # fitness 통과 → 제외
    db.insert_alpha(uid, rid, 1, _alpha(2, 'rank(c)', 1.9, 0.6, 0.10))   # 저회전 → 제외
    db.insert_alpha(uid, rid, 1, _alpha(3, 'rank(d)', 1.2, 0.6, 0.55))   # 샤프 미달 → 제외
    db.insert_alpha(uid, rid, 1, _alpha(4, 'rank(a)', 1.7, 0.5, 0.50))   # 같은 코드 → dedup
    pool = db.ht_rescue_pool(uid)
    assert [r['code'] for r in pool] == ['rank(a)']
    assert float(pool[0]['sharpe']) == 1.9        # dedup 은 최고 sharpe 유지
    assert pool[0]['universe'] == 'TOP1000'       # 부모 settings 상속 재료


def test_novelty_rewrite_finds_unseen_variant(monkeypatch):
    monkeypatch.setattr(worker.result_cache, 'lookup', lambda *a, **k: None)
    out = worker._novelty_rewrite(1, 'ts_rank(close, 20)', 'fp', set())
    assert out and out != 'ts_rank(close, 20)'


def test_novelty_rewrite_gives_up_when_all_known(monkeypatch):
    monkeypatch.setattr(worker.result_cache, 'lookup',
                        lambda *a, **k: {'pass_count': 1})   # 전부 기지 조합
    assert worker._novelty_rewrite(1, 'ts_rank(close, 20)', 'fp', set()) is None
