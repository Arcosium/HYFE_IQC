# tests/test_superalpha.py
# ⑤ 슈퍼알파: selection/combo 표현식 생성(순수부) + superalpha_runs db 헬퍼.
import random

import pytest

from server import criteria, db, superalpha


def test_selection_expression_embeds_filters_and_seed():
    s = superalpha.selection_expression(37)
    assert s.startswith('d = 37;')
    assert f'(turnover < {criteria.SUPERALPHA_TURNOVER_MAX})' in s
    assert f'(turnover > {criteria.SUPERALPHA_TURNOVER_MIN})' in s
    assert 'abs(short_count/long_count-1)' in s
    assert 'sqrt(turnover+d)*10000' in s          # ACE 의사난수 정렬키


def test_combo_grid_shape():
    g = superalpha.combo_grid()
    # stats 5종 × ops 3종 + (-drawdown) × 3 = 18
    assert len(g) == 18
    assert all(c.startswith('stats = generate_stats(alpha);') for c in g)
    # ts 계열엔 창 126, rank 엔 창 없음.
    assert any('ts_rank(stats.pnl,126)' in c for c in g)
    assert any('rank(stats.pnl)' in c and 'ts_' not in c.split(';')[1] for c in g)
    assert any('-stats.drawdown' in c for c in g)


def test_build_candidates_sampling_and_settings():
    out = superalpha.build_candidates(10, n=5, rng=random.Random(1))
    assert len(out) == 5
    sel = out[0]['selection']
    assert all(c['selection'] == sel for c in out)             # 런당 selection 1개
    assert len({c['combo'] for c in out}) == 5                 # combo 는 전부 다름
    st = out[0]['settings']
    assert st['selectionLimit'] == superalpha.SELECTION_LIMIT
    assert st['selectionHandling'] == superalpha.SELECTION_HANDLING


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'sa.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def test_superalpha_run_lifecycle(isolated_db):
    uid = isolated_db
    rid = db.superalpha_start(uid, 37, 'd = 37; ...')
    runs = db.superalpha_runs_list(uid)
    assert runs[0]['id'] == rid and runs[0]['status'] == 'running'
    db.superalpha_finish(rid, 'done', [{'combo': 'rank(stats.pnl)',
                                        'alpha_id': 'abc'}])
    runs = db.superalpha_runs_list(uid)
    assert runs[0]['status'] == 'done'
    assert runs[0]['results'][0]['alpha_id'] == 'abc'
