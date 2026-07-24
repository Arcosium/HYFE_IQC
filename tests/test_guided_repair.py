# tests/test_guided_repair.py
# #4 가이드 리페어 — 시뮬 실패 에러 → 표적 수리(순수).
import server.alpha_repair as ar


def test_repair_drops_unknown_attribute():
    code = 'ts_backfill(close, 120, filter=true)'
    new, label = ar.repair_from_error(code, 'Unknown attribute "filter" encountered')
    assert new == 'ts_backfill(close, 120)'
    assert label == 'drop_attr:filter'


def test_repair_snaps_nearest_field():
    code = 'rank(clse) - ts_mean(returns, 5)'
    new, label = ar.repair_from_error(code, '"clse" is not a valid field',
                                      field_pool=['close', 'open', 'volume', 'returns'])
    assert new == 'rank(close) - ts_mean(returns, 5)'
    assert label == 'field_snap:clse->close'


def test_repair_field_variant_does_not_exist():
    new, label = ar.repair_from_error('rank(voluem)', 'voluem does not exist',
                                      field_pool=['close', 'volume', 'vwap'])
    assert new == 'rank(volume)'
    assert 'voluem->volume' in label


def test_repair_skips_operator_token():
    # 에러가 연산자('rank')를 지목하면 field 스냅 대상 아님.
    new, _ = ar.repair_from_error('rank(close)', "rank doesn't exist",
                                  field_pool=['close', 'ranking'])
    assert new is None


def test_repair_no_field_pool_skips_snap():
    new, _ = ar.repair_from_error('rank(clse)', '"clse" is not a valid field')
    assert new is None


def test_repair_unrepairable_returns_none():
    new, label = ar.repair_from_error('rank(close)', 'Simulation limit exceeded')
    assert new is None and label == ''


def test_repair_no_close_match_skips():
    new, _ = ar.repair_from_error('rank(xyzzy)', '"xyzzy" is not a valid field',
                                  field_pool=['close', 'volume'])
    assert new is None


def test_repair_empty_inputs():
    assert ar.repair_from_error('', 'err') == (None, '')
    assert ar.repair_from_error('rank(close)', '') == (None, '')
