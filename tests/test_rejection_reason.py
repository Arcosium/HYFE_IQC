# tests/test_rejection_reason.py
# 2026-07-28 사장 지적: 제출 대기의 거절 사유가 실제 이유로 안 읽힌다.
# gJ9ea3ZJ 노트는 `rejected:LOW_SHARPE; LOW_FITNESS; LOW_GLB_EMEA_SHARPE (http_403)`
# 인데 지금 그 알파를 조회하면 셋 다 WARNING — 값·기준을 안 남겨 대조가 불가능했다.
from server.wqb_api import WqbApiClient as C


def _body(*checks):
    return {'is': {'checks': list(checks)}}


def test_fail_reason_carries_value_and_limit():
    r = C._rejection_reason(_body(
        {'name': 'LOW_SHARPE', 'result': 'FAIL', 'value': 1.04, 'limit': 1.58}))
    assert r == 'LOW_SHARPE(1.04 vs 1.58)'


def test_warnings_are_never_reported_as_the_reason():
    """WARNING 은 거절 사유가 아니다 — 섞이면 사장이 본 그 혼란이 재발한다."""
    assert C._rejection_reason(_body(
        {'name': 'LOW_SHARPE', 'result': 'WARNING', 'value': 1.04, 'limit': 1.58},
        {'name': 'LOW_FITNESS', 'result': 'WARNING', 'value': 0.29, 'limit': 1.0},
        {'name': 'PROD_CORRELATION', 'result': 'PENDING'},
        {'name': 'HIGH_TURNOVER', 'result': 'PASS', 'value': 0.34, 'limit': 0.7},
    )) is None


def test_name_only_when_wqb_gives_no_numbers():
    r = C._rejection_reason(_body({'name': 'PROD_CORRELATION', 'result': 'FAIL'}))
    assert r == 'PROD_CORRELATION'


def test_every_fail_survives_not_just_the_first_three():
    """앞 3개만 남기면 결정적인 체크가 잘린다 — WQB 순서상 진짜 이유가 뒤에 온다."""
    r = C._rejection_reason(_body(
        {'name': 'LOW_SHARPE', 'result': 'FAIL'},
        {'name': 'LOW_FITNESS', 'result': 'FAIL'},
        {'name': 'LOW_GLB_EMEA_SHARPE', 'result': 'FAIL'},
        {'name': 'REGULAR_SUBMISSION', 'result': 'FAIL'},
    ))
    assert 'REGULAR_SUBMISSION' in r, f'결정적인 체크가 잘렸다: {r}'
    assert r.startswith('LOW_SHARPE'), '순서는 WQB 가 준 그대로'


def test_garbage_bodies_do_not_raise():
    for junk in (None, '', 42, [], {}, {'is': None}, {'is': {'checks': None}}):
        assert C._rejection_reason(junk) is None
