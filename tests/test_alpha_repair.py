from server import alpha_repair as ar


def test_region_prefix_strip():
    out, applied = ar.repair('rank(USA.close)', delay='1')
    assert out == 'rank(close)'
    assert 'region_prefix' in applied


def test_doubled_operator_collapse():
    out, applied = ar.repair('rankrank(close)', delay='1')
    assert out == 'rank(close)'
    assert 'doubled_op' in applied


def test_leading_operator_strip():
    out, applied = ar.repair('+ts_mean(close,5)', delay='1')
    assert out == 'ts_mean(close,5)'
    assert 'leading_op' in applied


def test_missing_lookback_delay0():
    out, applied = ar.repair('ts_mean(close)', delay='0')
    assert out == 'ts_mean(close,22)'
    assert 'missing_lookback' in applied


def test_missing_lookback_delay1():
    out, _ = ar.repair('ts_mean(close)', delay='1')
    assert out == 'ts_mean(close,10)'


def test_missing_lookback_respects_nested_arg():
    # 단일 인자가 중첩 함수(내부 콤마는 depth>1)면 윈도우를 붙인다.
    out, _ = ar.repair('ts_mean(add(close,open))', delay='1')
    assert out == 'ts_mean(add(close,open),10)'


def test_no_lookback_when_window_present():
    out, applied = ar.repair('ts_mean(close,5)', delay='1')
    assert out == 'ts_mean(close,5)'
    assert 'missing_lookback' not in applied


def test_non_ts_operator_untouched():
    out, applied = ar.repair('rank(close)', delay='0')
    assert out == 'rank(close)'
    assert applied == []


def test_idempotent():
    once, _ = ar.repair('+ts_mean(USA.close)', delay='0')
    twice, applied2 = ar.repair(once, delay='0')
    assert once == twice
    assert applied2 == []


def test_doubled_collapse_only_for_real_operators():
    # 'abc' 는 연산자가 아니므로 abcabc( 는 collapse 되면 안 된다 (의미 변경 방지).
    out, applied = ar.repair('abcabc(close)', delay='1')
    assert out == 'abcabc(close)'
    assert 'doubled_op' not in applied
