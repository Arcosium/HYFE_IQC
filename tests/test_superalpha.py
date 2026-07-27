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


# ── 권한 미보유 처리 (2026-07-27 실측) ──────────────────────────────────────
# CONSULTANT 권한만 있는 계정에서 실제로 받은 응답:
#   400 {"type":["Not permissioned for super simulations"]}
# super simulation 은 CONSULTANT 와 **별개 권한**이다.

class _NotPermissionedClient:
    """submit 마다 권한 거절을 돌려주는 가짜 클라이언트."""

    def __init__(self):
        self.calls = 0

    def authenticate(self):
        return True

    def submit_super_simulation(self, selection, combo, settings):
        self.calls += 1
        return 'NOT_PERMISSIONED'


def test_run_stops_immediately_when_account_lacks_permission(isolated_db, monkeypatch):
    """남은 후보를 더 돌려도 전부 같은 400 이다 — 첫 건에서 끝내고 사유를 남긴다."""
    uid = isolated_db
    fake = _NotPermissionedClient()
    import server.wqb_api as wqb_api
    monkeypatch.setattr(wqb_api, 'WqbApiClient', lambda u, p: fake)

    rid = superalpha.run(uid, 'u', 'p', seed_plus=10, n=6)
    assert fake.calls == 1, '권한 거절인데 후보 6개를 다 시도했다'
    row = db.superalpha_runs_list(uid)[0]
    assert row['id'] == rid and row['status'] == 'error'
    assert 'super simulation 권한' in (row['error'] or '')
