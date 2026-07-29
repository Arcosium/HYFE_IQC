"""고회전 개편의 배관 회귀 테스트 — 수확(harvest)과 일일 제출 예산.

둘 다 '조용히 틀리는' 종류다: 수확이 HT 지표를 버리면 보상은 그 값을 영원히 못 보고,
예산 게이트가 없으면 하루 4칸이 그날 처음 통과한 4개에 소진된다.
"""
import json

import pytest

from server import criteria as c
from server import wqb_api
from server import wqb_backend as wb


# 라이브 GET /alphas/{id} 응답에서 발췌한 checks 블록 (gJ9qkKWv, 2026-07-21).
LIVE_ALPHA = {
    'settings': {'delay': 0, 'region': 'USA', 'universe': 'TOP3000',
                 'neutralization': 'SUBINDUSTRY'},
    'is': {
        'sharpe': 1.53, 'fitness': 0.69, 'turnover': 0.5401, 'returns': 0.1109,
        'drawdown': 0.15, 'margin': 0.00038,
        'riskNeutralized': {'sharpe': 2.81, 'fitness': 1.0, 'turnover': 0.6003},
        'investabilityConstrained': {'sharpe': 1.03, 'turnover': 0.4114},
        'checks': [
            {'name': 'LOW_SHARPE', 'result': 'WARNING', 'limit': 2.69, 'value': 1.53},
            {'name': 'LOW_FITNESS', 'result': 'WARNING', 'limit': 1.5, 'value': 0.69},
            {'name': 'CLUSTER_TEST', 'result': 'WARNING', 'limit': 1.58, 'value': 1.01},
            {'name': 'LOW_TURNOVER', 'result': 'PASS', 'limit': 0.01, 'value': 0.5401},
            {'name': 'HIGH_TURNOVER', 'result': 'PASS', 'limit': 0.7, 'value': 0.5401},
            {'name': 'CONCENTRATED_WEIGHT', 'result': 'PASS'},
            {'name': 'LOW_SUB_UNIVERSE_SHARPE', 'result': 'PASS', 'limit': 0.66, 'value': 0.82},
            {'name': 'SELF_CORRELATION', 'result': 'PENDING'},
            {'name': 'HT_TURNOVER', 'result': 'PASS', 'limit': 0.2, 'value': 0.5401},
            {'name': 'HT_HIGH_TURNOVER_RETURNS_RATIO', 'result': 'PASS',
             'limit': 0.75, 'value': 0.853},
            {'name': 'HT_PNL_REALIZATION_HORIZON', 'result': 'PASS', 'limit': 20, 'value': 6},
            {'name': 'HT_AFTER_COST_SHARPE', 'result': 'WARNING', 'limit': 1.0, 'value': 0.41},
            {'name': 'HT_ORTHOGONAL_RAM_NEUTRALIZATION', 'result': 'WARNING',
             'limit': 'RAM', 'value': 'Subindustry'},
            {'name': 'MATCHES_CLASSIFICATION', 'result': 'PASS', 'value': ['High Turnover']},
            {'name': 'MATCHES_PYRAMID', 'result': 'PASS', 'effective': 1, 'multiplier': 1.6,
             'pyramids': [{'name': 'USA/D0/PV', 'multiplier': 1.6}]},
            {'name': 'MATCHES_THEMES', 'result': 'WARNING',
             'themes': [{'name': 'GLB High Turnover Theme', 'multiplier': 2.0}]},
            {'name': 'OSMOSIS_ALLOCATION', 'result': 'WARNING'},
        ],
    },
}


class _Resp:
    ok = True

    def json(self):
        return LIVE_ALPHA


class _Sess:
    def get(self, *a, **k):
        return _Resp()


@pytest.fixture()
def harvested():
    cl = wqb_api.WqbApiClient.__new__(wqb_api.WqbApiClient)
    cl.session = _Sess()
    return cl.harvest_alpha('x')


def test_harvest_promotes_ht_metrics(harvested):
    """HT 지표가 metrics 로 승격돼야 한다.

    구 코드는 core 가 아닌 체크를 통째로 `continue` 로 버렸다 — 그래서 제출 가능성의
    1차 결정 변수(HT_*)가 보상에 **한 번도** 도달하지 못했다.
    """
    m = harvested['metrics']
    assert m['ht_turnover'] == '0.5401'
    assert m['ht_returns_ratio'] == '0.853'
    assert m['ht_pnl_horizon'] == '6.0'
    assert m['ht_after_cost_sharpe'] == '0.41'
    assert m['cluster_sharpe'] == '1.01'
    # 실측 컷도 함께 — WQB 가 규칙을 또 바꿔도 하드코딩 대신 이 값을 따라간다.
    assert m['sharpe_check_cutoff'] == '2.69'
    assert m['fitness_check_cutoff'] == '1.5'


def test_harvest_promotes_multipliers_and_classification(harvested):
    m = harvested['metrics']
    assert m['pyramid_multiplier'] == '1.6'
    assert m['pyramids'] == 'USA/D0/PV'
    assert m['classifications'] == 'High Turnover'
    # MATCHES_THEMES 가 WARNING = "매칭 안 됨" → 배수를 주면 안 된다.
    assert 'theme_multiplier' not in m
    assert m['themes_unmatched'] == 'GLB High Turnover Theme'


def test_harvest_buckets_warning_separately(harvested):
    """WARNING 은 pass 도 fail 도 아니다. 구 코드엔 버킷이 없어 통째로 사라졌다."""
    st = harvested['is_status']
    warn = {i['name'] for i in st['warning']}
    assert {'LOW_SHARPE', 'LOW_FITNESS'} <= warn
    assert st['fail'] == []
    assert {i['name'] for i in st['pass']} == {
        'LOW_TURNOVER', 'HIGH_TURNOVER', 'CONCENTRATED_WEIGHT', 'LOW_SUB_UNIVERSE_SHARPE'}
    # 분류/장부 체크는 pass 카운트를 부풀리면 안 된다.
    assert not any(i['name'].startswith(('HT_', 'MATCHES_')) for i in st['pass'])


def test_harvest_settings_stamp_enables_delay_aware_cutoffs(harvested):
    """settings 의 delay/neutralization 이 metrics 에 실려야 criteria 가 delay 를 안다."""
    m = harvested['metrics']
    assert m['_delay'] == '0'
    assert m['neutralization'] == 'SUBINDUSTRY'
    assert c.cutoffs(m['_delay'])['sharpe'] == 2.69


def test_harvested_alpha_is_submittable_end_to_end(harvested):
    """이 알파는 라이브에서 FAIL 0 이었다 — 파이프라인 끝단도 그렇게 봐야 한다."""
    st = harvested['is_status']
    assert len(st['fail']) == 0
    assert c.ht_status(harvested['metrics'])['waiver_likely'] is True


# ── 일일 제출 예산 ───────────────────────────────────────────────────────────


@pytest.fixture
def no_live_submission_count(monkeypatch):
    """_submitted_today 의 **WQB 실측 조회**를 끊는다 (테스트 격리).

    로컬 집계만 monkeypatch 하면 반쪽이다 — 실측이 살아 있어서 그날 실계정 제출이
    4건이면 예산 게이트 테스트가 환경 때문에 실패한다(2026-07-29 실측).
    """
    from server import wqb_api as _api

    class _NoRemote:
        def __init__(self, *a, **k): pass
        def submissions_on(self, day): return None      # None → 로컬 집계 사용

    monkeypatch.setattr(_api, 'WqbApiClient', _NoRemote)


def test_submit_gate_allows_then_blocks_at_budget(monkeypatch, no_live_submission_count):
    """예산 소진 시 제출이 막혀야 한다 (하루 4건 — Power Pool 문서)."""
    from server import worker as w

    used = {'n': 0}
    monkeypatch.setattr(w._db, 'submitted_today', lambda uid: used['n'])
    # 제출 보류창(2026-07-27)은 이 테스트의 관심사가 아니다 — 라이브 설정에서 격리.
    monkeypatch.setattr(w.run_config, 'get_submit_hold_until', lambda: 0.0)
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = 2
    good = dict(sharpe='1.53', fitness='0.69', turnover='0.54',
                pyramid_multiplier='1.6', ht_turnover='0.54',
                ht_returns_ratio='0.85', ht_after_cost_sharpe='0.41')

    ok, _ = wk._submit_gate(good)
    assert ok is True

    used['n'] = w.DAILY_SUBMIT_BUDGET
    ok, reason = wk._submit_gate(good)
    assert ok is False and 'daily_budget' in reason


def test_submit_gate_fails_open_on_db_error(monkeypatch, no_live_submission_count):
    """게이트 버그가 제출을 통째로 막으면 안 된다 — 불확실하면 제출한다."""
    from server import worker as w

    def boom(uid):
        raise RuntimeError('db down')

    monkeypatch.setattr(w._db, 'submitted_today', boom)
    monkeypatch.setattr(w.run_config, 'get_submit_hold_until', lambda: 0.0)
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = 2
    assert wk._submit_gate({'sharpe': '1.5'})[0] is True


def test_backend_skips_submit_when_gate_refuses():
    """게이트가 거부하면 submit_alpha 를 아예 부르지 않는다."""
    calls = []

    class Client:
        def authenticate(self):
            return True

        def submit_simulation(self, code, settings):
            return 'http://sim'

        def poll(self, url, stop_event=None, **k):
            return {'status': 'COMPLETE', 'alpha': 'a1'}

        def harvest_alpha(self, aid):
            return {'metrics': {'sharpe': '0.1'},
                    'is_status': {'pass': [{'name': 'x'}], 'fail': [], 'error': [],
                                  'pending': [], 'warning': []}}

        def read_self_correlation(self, aid):
            return None

        def submit_alpha(self, aid, stop_event=None):
            calls.append(aid)
            return True, 'submitted'

    be = wb.ApiBackend('e', 'p', client=Client())
    res = be.simulate_batch([{'idx': 1, 'code': 'x', 'desc': '', 'settings': {}}],
                            submit_gate=lambda m, sc: (False, 'daily_budget(4/4)'))
    assert calls == []
    assert res[0]['submitted'] is False
    assert 'daily_budget' in res[0]['submit_status']
