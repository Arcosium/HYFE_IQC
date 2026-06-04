from server import alpha_similarity as sim


def test_known_op_not_counted_as_field():
    # 카탈로그가 인식하는 operator 는 field 로 새지 않는다.
    ops = sim.extract_operators('ts_mean(close,5)')
    assert 'ts_mean' in ops
    flds = sim.extract_fields('ts_mean(close,5)')
    assert 'ts_mean' not in flds
    assert 'close' in flds
