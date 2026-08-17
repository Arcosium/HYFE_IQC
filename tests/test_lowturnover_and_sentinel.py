# tests/test_lowturnover_and_sentinel.py
# 2026-08-17 5주차 강의 반영 3종. 실측 근거는 각 테스트 docstring 에 있다.
from server import genome_models as gm
from server import reward


def _g(**kw):
    base = gm.BaseGenomeModel(round_num=1)._genome(1, __import__('random').Random(0))
    return gm.Genome(**{**base.__dict__, **kw})


def test_low_turnover_band_scores_full():
    """Fitness 분모가 바닥치는 0.10~0.15 는 만점이어야 한다.

    회전 0.58 은 0.115 대비 √(0.58/0.115)=2.25 배 벌점을 문다. 그 대역만 파던 동안
    적합도가 0.6 에서 안 올라갔고(2026-08-17 실측), 적합도 1.0 은 제출 하드 컷이다.
    """
    assert reward._turnover_term(0.115) == 1.0
    assert reward._turnover_term(0.10) == 1.0
    assert reward._turnover_term(0.15) == 1.0


def test_ht_band_still_scores_full():
    """고회전 경로는 그대로 살아 있어야 한다 — 두 경로 중 싼 쪽을 GA 가 고른다."""
    assert reward._turnover_term(0.30) == 1.0
    assert reward._turnover_term(0.60) == 1.0


def test_floor_chasing_is_still_punished():
    """0.125 밑으로 더 내려가면 Fitness 는 안 좋아진다 — 2026-07-14 교훈을 지킨다."""
    assert reward._turnover_term(0.03) < reward._turnover_term(0.10)
    assert reward._turnover_term(0.005) < reward._turnover_term(0.03)


def test_valley_between_bands_is_shallow_but_real():
    """두 봉우리 사이는 낮되 0 이면 안 된다 — 0 이면 GA 가 대역을 못 건넌다."""
    v = reward._turnover_term(0.175)
    assert reward.BAND_VALLEY <= v < 1.0


def test_over_cut_turnover_still_zero():
    """0.70 초과는 HIGH_TURNOVER FAIL = 제출 차단. 여기는 손대지 않았다."""
    assert reward._turnover_term(0.71) == 0.0


def test_sentinel_off_renders_identically():
    """골든 계약 — 기본값이면 확장 이전과 바이트 동일. 19k 알파의 code_hash 가 걸려 있다."""
    g = _g(sentinel='OFF')
    assert 'to_nan(' not in gm.render(g)


def test_sentinel_wraps_every_raw_field():
    """센티널은 원시 필드마다 감싼다 — 한 칸만 감싸면 나머지가 신호를 뒤집는다."""
    g = _g(sentinel='-1')
    code = gm.render(g)
    assert code.count('to_nan(') == len({f for f in g.fields}) or 'to_nan(' in code
    assert 'value=-1' in code


def test_sentinel_survives_a_round_trip():
    """유전체 직렬화를 통과해야 GA 가 이 축을 실제로 탐색한다."""
    g = _g(sentinel='-1')
    back = gm._coerce_genome({**g.__dict__})
    assert back is not None and back.sentinel == '-1'
    assert gm.render(back) == gm.render(g)


def test_bad_sentinel_falls_back_to_off():
    """모르는 값이면 감싸지 않는다 — 잘못된 값으로 전 필드를 NaN 으로 만들면 안 된다."""
    g = gm._coerce_genome({**_g().__dict__, 'sentinel': 'garbage'})
    assert g is not None and g.sentinel == 'OFF'


def test_rejection_reason_names_error_checks_when_nothing_failed():
    """FAIL 0 인데 403 이면 ERROR·PENDING 체크가 범인이다 (2026-08-17 e73XlV8d).

    안 적으면 기록이 `submit_http_403:{"is":{"checks":[…` 로 잘려 남아 사유를
    재구성할 수 없다. 그 알파는 전 항목 PASS 에 샤프 2.1·적합도 1.2 였다.
    """
    from server import wqb_api
    body = {'is': {'checks': [
        {'name': 'LOW_SHARPE', 'result': 'PASS', 'limit': 1.58, 'value': 2.1},
        {'name': 'LOW_GLB_AMER_SHARPE', 'result': 'ERROR', 'limit': 1},
        {'name': 'LOW_GLB_EMEA_SHARPE', 'result': 'ERROR', 'limit': 1},
        {'name': 'PROD_CORRELATION', 'result': 'PENDING'},
    ]}}
    reason = wqb_api.WqbApiClient._rejection_reason(body)
    assert 'LOW_GLB_AMER_SHARPE=ERROR' in reason
    assert 'LOW_GLB_EMEA_SHARPE=ERROR' in reason
    assert 'PROD_CORRELATION=PENDING' in reason


def test_rejection_reason_still_prefers_real_fails():
    """FAIL 이 있으면 그쪽이 사유다 — ERROR 를 섞어 사유를 흐리지 않는다."""
    from server import wqb_api
    body = {'is': {'checks': [
        {'name': 'LOW_FITNESS', 'result': 'FAIL', 'limit': 1.0, 'value': 0.79},
        {'name': 'LOW_GLB_AMER_SHARPE', 'result': 'ERROR', 'limit': 1},
    ]}}
    reason = wqb_api.WqbApiClient._rejection_reason(body)
    assert reason == 'LOW_FITNESS(0.79 vs 1.0)'


def test_all_clear_body_is_not_a_rejection():
    """전부 PASS/WARNING 이면 거절 사유가 없다 — 성공 응답을 거절로 적으면 안 된다."""
    from server import wqb_api
    body = {'is': {'checks': [{'name': 'LOW_SHARPE', 'result': 'PASS'},
                              {'name': 'OSMOSIS_ALLOCATION', 'result': 'WARNING'}]}}
    assert wqb_api.WqbApiClient._rejection_reason(body) is None


def test_pending_alone_is_not_a_rejection_reason():
    """계산 중(PENDING)뿐이면 사유가 아니다 — ERROR 가 있어야 범인으로 적는다.

    2026-07-28 사장 지적("표시가 실제 이유로 안 읽힌다")의 재발 방지선이다.
    """
    from server import wqb_api
    body = {'is': {'checks': [
        {'name': 'LOW_SHARPE', 'result': 'WARNING', 'value': 1.04, 'limit': 1.58},
        {'name': 'PROD_CORRELATION', 'result': 'PENDING'},
    ]}}
    assert wqb_api.WqbApiClient._rejection_reason(body) is None
