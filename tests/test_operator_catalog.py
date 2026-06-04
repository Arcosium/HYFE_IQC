from server import operator_catalog as oc


def test_seed_ops_always_recognized():
    # CSV 유무와 무관하게 기존 _KNOWN_OPS 핵심은 연산자로 인식 (무회귀 보장).
    for op in ('rank', 'ts_mean', 'add', 'group_neutralize', 'trade_when'):
        assert oc.is_operator(op), op


def test_ts_prefix_needs_lookback():
    assert oc.needs_lookback('ts_mean')
    assert oc.needs_lookback('ts_rank')
    assert not oc.needs_lookback('rank')
    assert not oc.needs_lookback('add')


def test_unknown_token_not_operator():
    assert not oc.is_operator('close')
    assert not oc.is_operator('anl4_some_field')


def test_operator_names_superset_of_seed():
    names = oc.operator_names()
    assert {'rank', 'add', 'ts_delta'} <= names


def test_case_insensitive():
    assert oc.is_operator('TS_MEAN')
    assert oc.needs_lookback('TS_MEAN')


def test_csv_missing_falls_back_to_seed(monkeypatch):
    from server import operator_catalog as oc2
    monkeypatch.setattr(oc2, 'OPERATORS_CSV', '/nonexistent/path/brain_operators.csv')
    oc2._CACHE = None  # 캐시 무효화 → 재로드 시 CSV 없음 경로
    names = oc2.operator_names()
    assert {'rank', 'add', 'ts_mean'} <= names      # 시드는 여전히 인식
    oc2._CACHE = None  # 다른 테스트 오염 방지 위해 캐시 리셋
