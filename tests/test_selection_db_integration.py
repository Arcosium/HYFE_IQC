# tests/test_selection_db_integration.py
# elite_seeds 가 (a) 유전체 없는 행을 배제하고 (b) 연속 적합도로 뽑으며
# (c) IQC_SELECTION_MODE 로 정렬층을 바꾸는지(무변경 폴백 포함).
import pytest
from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'sel.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield uid_and_round(tmp_db)
    db._INITIALIZED = False


def uid_and_round(tmp_db):
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)
    return uid, rid


def _genome(generation=0, **over):
    g = {
        'model': 'rc-api-genome', 'family': 'pv',
        'fields': ('close', 'low', 'volume'), 'transform_a': 'rank',
        'transform_b': 'ts_rank', 'combine': 'spread', 'sign': 1,
        'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP3000',
        'neutralization': 'INDUSTRY', 'decay': 4, 'truncation': 0.08,
        'nan_handling': 'OFF', 'decay_style': 'mean', 'generation': generation,
    }
    g.update(over)
    return g


def _mk(idx, code, sharpe, fitness, turnover, self_corr, *, generation=0):
    return {
        'idx': idx, 'code': code, 'desc': code, 'pass_count': 6,
        'pass_items': [], 'fail_count': 0, 'fail_items': [], 'error_count': 0,
        'pending_count': 0, 'submitted': False, 'submit_status': '', 'error_text': '',
        'metrics': {'sharpe': str(sharpe), 'fitness': str(fitness), 'turnover': str(turnover)},
        'self_corr': str(self_corr), 'settings': {'universe': 'TOP3000'}, 'delay': '1',
        'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'generation': generation, 'genome': _genome(generation),
    }


def _seed_ab(uid, rid):
    # A: 높은 sharpe 지만 turnover/self_corr 나쁨. B: sharpe 조금 낮지만 turnover/corr/fitness 우수.
    db.insert_alpha(uid, rid, 1, _mk(0, 'CODE_A', 3.0, 0.5, 0.9, 0.65))
    db.insert_alpha(uid, rid, 1, _mk(1, 'CODE_B', 2.5, 2.0, 0.05, 0.05))


def test_elite_seeds_prefers_multiobjective_winner(isolated_db):
    """기본 정렬이 이제 selection_score — sharpe 만 높고 turnover/self-corr 가 나쁜 CODE_A 를
    CODE_B 가 이긴다. 예전 `ORDER BY pass_count DESC, sharpe` 는 A 를 골랐다."""
    uid, rid = isolated_db
    _seed_ab(uid, rid)
    out = db.elite_seeds(uid, top_n=2)
    assert [a['code'] for a in out] == ['CODE_B', 'CODE_A']
    assert out[0]['_score'] > out[1]['_score']


def test_elite_seeds_skips_alphas_without_a_genome(isolated_db):
    """유전체가 없는 행(레거시 Gemini 화석)은 pass_count 가 아무리 높아도 시드가 아니다 —
    코드에서 역추출한 유전체는 부모를 복제조차 못 한다."""
    uid, rid = isolated_db
    fossil = _mk(0, 'FOSSIL', 4.0, 3.0, 0.02, 0.0)
    fossil['genome'] = None          # 렌더러 산이 아님 → genome NULL
    fossil['pass_count'] = 11
    db.insert_alpha(uid, rid, 1, fossil)
    db.insert_alpha(uid, rid, 1, _mk(1, 'CODE_B', 0.5, 0.2, 0.3, 0.05))

    out = db.elite_seeds(uid, top_n=5)
    assert [a['code'] for a in out] == ['CODE_B']


def test_elite_seeds_skips_alphas_with_no_metrics(isolated_db):
    """시뮬 지표가 하나도 없는 알파는 turnover 결측이 만점으로 읽혀 상위에 올라오면 안 된다."""
    uid, rid = isolated_db
    dead = _mk(0, 'DEAD', '', '', '', '')
    dead['metrics'] = {'_delay': '1'}
    dead['pass_count'] = 0
    db.insert_alpha(uid, rid, 1, dead)
    db.insert_alpha(uid, rid, 1, _mk(1, 'ALIVE', 0.5, 0.2, 0.3, 0.05))

    out = db.elite_seeds(uid, top_n=5)
    assert [a['code'] for a in out] == ['ALIVE']


def test_elite_seeds_dedups_identical_codes(isolated_db):
    """같은 코드가 풀을 통째로 차지하면 교차가 자기복제로 붕괴한다."""
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _mk(0, 'SAME', 1.0, 0.5, 0.2, 0.0))
    db.insert_alpha(uid, rid, 1, _mk(1, 'SAME', 2.0, 0.9, 0.1, 0.0))   # 더 좋은 쪽이 남는다
    db.insert_alpha(uid, rid, 1, _mk(2, 'OTHER', 0.4, 0.2, 0.3, 0.0))

    out = db.elite_seeds(uid, top_n=5)
    assert [a['code'] for a in out] == ['SAME', 'OTHER']
    assert out[0]['metrics']['sharpe'] == '2.0'


def test_elite_seeds_honours_recency_window(isolated_db):
    """최근성 윈도우가 없으면 풀이 과거의 화석에서 굳는다 (round 66 동결 사건).

    명예의 전당(hall_of_fame)은 **일부러** 이 윈도우를 넘어서므로 여기선 끄고 검증한다
    — 윈도우 의미론과 HOF 는 별개 기능이고, HOF 는 아래 전용 테스트가 못박는다.
    """
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _mk(0, 'OLD_STRONG', 3.0, 2.0, 0.05, 0.0))
    db.insert_alpha(uid, rid, 2, _mk(1, 'NEW_WEAK', 0.6, 0.3, 0.3, 0.0))

    assert [a['code'] for a in db.elite_seeds(uid, top_n=5, hall_of_fame=0)] \
        == ['OLD_STRONG', 'NEW_WEAK']
    # 윈도우를 1 로 좁히면 최신 1건만 후보다.
    assert [a['code'] for a in db.elite_seeds(uid, top_n=5, window=1, hall_of_fame=0)] \
        == ['NEW_WEAK']


def test_hall_of_fame_rescues_out_of_window_champions(isolated_db):
    """윈도우 밖으로 밀려난 역대 최고 유전체가 시드 슬롯으로 되돌아온다.

    이게 없으면 6월의 Sharpe 3.77 알파처럼 '풀에서 영구 소멸' 한다 — ELITE_WINDOW 가
    화석화를 막는 대가로 챔피언까지 같이 버리던 문제(2026-07-14).
    """
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _mk(0, 'CHAMPION', 3.5, 2.0, 0.10, 0.0))
    for i in range(1, 6):
        db.insert_alpha(uid, rid, 2, _mk(i, f'RECENT_{i}', 0.5, 0.2, 0.3, 0.0))

    # 윈도우가 좁아 CHAMPION 은 최근 후보가 아니다.
    win_only = [a['code'] for a in db.elite_seeds(uid, top_n=4, window=3, hall_of_fame=0)]
    assert 'CHAMPION' not in win_only

    # HOF 를 켜면 되돌아온다 — 단, 슬롯은 top_n 의 절반까지만.
    with_hof = db.elite_seeds(uid, top_n=4, window=3, hall_of_fame=2)
    codes = [a['code'] for a in with_hof]
    assert 'CHAMPION' in codes, codes
    assert sum(1 for c in codes if c.startswith('RECENT')) >= 2, codes

    # 직접 조회도 챔피언을 1순위로 준다.
    assert db.hall_of_fame_seeds(uid, top_n=1)[0]['code'] == 'CHAMPION'


def test_elite_seeds_carries_true_generation(isolated_db):
    """세대는 유전체 JSON 이 권위 — 시드를 거쳐도 0 으로 리셋되지 않는다."""
    uid, rid = isolated_db
    db.insert_alpha(uid, rid, 1, _mk(0, 'G3', 1.0, 0.5, 0.2, 0.0, generation=3))
    out = db.elite_seeds(uid, top_n=1)
    assert out[0]['genome']['generation'] == 3


def test_selection_mode_reorders_and_falls_back(isolated_db, monkeypatch):
    uid, rid = isolated_db
    _seed_ab(uid, rid)

    monkeypatch.setenv('IQC_SELECTION_MODE', 'percentile')   # 다목적 백분위 → B 승격
    pct = db.elite_seeds(uid, top_n=2)
    assert pct and pct[0]['code'] == 'CODE_B'

    monkeypatch.setenv('IQC_SELECTION_MODE', 'nsga2')        # 비지배정렬도 안전 동작
    nsga = db.elite_seeds(uid, top_n=2)
    assert {a['code'] for a in nsga} == {'CODE_A', 'CODE_B'}

    monkeypatch.setenv('IQC_SELECTION_MODE', 'garbage')      # 알 수 없는 모드 → score 폴백
    out = db.elite_seeds(uid, top_n=2)
    assert [a['code'] for a in out] == ['CODE_B', 'CODE_A']
