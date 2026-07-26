# tests/test_combine_layer.py
# 재조합 레이어(AAF·smilee 이식): 검증 알파 둘의 결합 후보 생성 + db.combine_pool.
import random

import pytest

from server import alpha_ast, combine_layer, db


def _row(id_, code, sharpe, turnover='0.10', **over):
    r = {
        'id': id_, 'code': code, 'code_hash': f'h{id_}',
        'metrics': {'sharpe': str(sharpe), 'turnover': turnover},
        'sharpe': float(sharpe), 'universe': 'TOP3000',
        'neutralization': 'INDUSTRY', 'decay': '4', 'truncation': '0.08',
        'self_corr': None,
    }
    r.update(over)
    return r


POOL = [
    _row(1, 'rank(ts_delta(anl4_foo, 20))', 2.1),
    _row(2, 'ts_zscore(anl4_bar, 63)', 1.6),
    _row(3, '-1 * ts_corr(rank(close), rank(volume), 10)', 1.2, turnover='0.30'),
]


def test_candidates_shape_and_parse():
    out = combine_layer.candidates(POOL, n=2, rng=random.Random(42))
    assert 1 <= len(out) <= 2
    for i, c in enumerate(out):
        assert c['idx'] == combine_layer.IDX_BASE + i
        assert c['origin'] == 'combine'
        assert alpha_ast.parse(c['code'])          # 결합식이 파싱 가능해야 한다
        assert c['settings'].get('universe') == 'TOP3000'
        assert c['parent_alpha_id'] in (1, 2, 3)


def test_candidates_contains_both_parents():
    out = combine_layer.candidates(POOL[:2], n=1, rng=random.Random(0))
    assert len(out) == 1
    code = out[0]['code']
    assert 'anl4_foo' in code and 'anl4_bar' in code


def test_deterministic_with_seed():
    a = combine_layer.candidates(POOL, n=2, rng=random.Random(7))
    b = combine_layer.candidates(POOL, n=2, rng=random.Random(7))
    assert [c['code'] for c in a] == [c['code'] for c in b]


def test_empty_and_single_pool():
    assert combine_layer.candidates([], n=2) == []
    assert combine_layer.candidates(POOL[:1], n=2) == []


def test_affinity_prefers_same_dataset():
    a = combine_layer._profile(POOL[0])
    same = combine_layer._profile(POOL[1])       # 같은 anl4 데이터셋
    diff = combine_layer._profile(POOL[2])       # pv, 회전율도 다름
    assert combine_layer._affinity(a, same) > combine_layer._affinity(a, diff)


# ── db.combine_pool 통합 ─────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'cp.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)
    yield uid, rid
    db._INITIALIZED = False


def _alpha(idx, code, sharpe, error_text=''):
    return {
        'idx': idx, 'code': code, 'desc': code, 'pass_count': 3,
        'pass_items': [], 'fail_count': 0, 'fail_items': [], 'error_count': 0,
        'pending_count': 0, 'submitted': False, 'submit_status': '',
        'error_text': error_text,
        'metrics': {'sharpe': str(sharpe), 'turnover': '0.1'},
        'self_corr': None, 'settings': {'universe': 'TOP3000'}, 'delay': '1',
        'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': 0, 'genome': None,
    }


def test_combine_pool_filters_and_dedups(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(0, 'rank(close)', 2.0))
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(close)', 1.4))       # 같은 코드 → dedup
    db.insert_alpha(uid, rid, 1, _alpha(2, 'rank(volume)', 0.4))      # sharpe 미달
    db.insert_alpha(uid, rid, 1, _alpha(3, 'rank(vwap)', 3.0, error_text='boom'))
    pool = db.combine_pool(uid)
    codes = [r['code'] for r in pool]
    assert codes == ['rank(close)']
    assert float(pool[0]['sharpe']) == 2.0        # dedup 은 최고 sharpe 를 남긴다
    assert pool[0]['metrics']['turnover'] == '0.1'


def test_combine_pool_genomeless_rows_included(isolated_db):
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _alpha(0, 'zscore(anl4_x)', 1.5))
    assert len(db.combine_pool(uid)) == 1         # genome 없어도 재료가 된다
