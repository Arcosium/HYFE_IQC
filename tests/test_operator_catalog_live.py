# tests/test_operator_catalog_live.py
# #3 라이브 /operators 카탈로그 — 시그니처 파싱 + operator_catalog arity 병합.
import server.wqb_data_service as wds
import server.operator_catalog as oc


def test_parse_operator_signature_basic():
    assert wds.parse_operator_signature('ts_rank(x, d)') == (2, 2, '')
    assert wds.parse_operator_signature('hump(x, hump=0.01)') == (1, 2, 'hump')
    assert wds.parse_operator_signature('rank(x)') == (1, 1, '')
    assert wds.parse_operator_signature('group_neutralize(x, group)') == (2, 2, '')
    mn, mx, named = wds.parse_operator_signature('add(x, y, ...)')
    assert mn == 2 and mx is None      # 가변 → max None
    assert wds.parse_operator_signature('') == (None, None, '')
    assert wds.parse_operator_signature('foo') == (None, None, '')


def test_map_operators_extracts_arity_and_skips_nameless():
    rows = wds.map_operators([
        {'name': 'ts_rank', 'category': 'Time Series', 'scope': ['REGULAR'],
         'definition': 'ts_rank(x, d)', 'description': 'desc'},
        {'name': '', 'definition': 'ignored()'},   # 이름 없음 → 스킵
    ])
    assert len(rows) == 1
    r = rows[0]
    assert r['name'] == 'ts_rank'
    assert r['min_args'] == '2' and r['max_args'] == '2'
    assert r['scope'] == 'REGULAR'


def test_operator_catalog_merges_live_signatures(tmp_path, monkeypatch):
    live = tmp_path / 'live_operators.csv'
    live.write_text(
        'name,category,scope,definition,description,min_args,max_args,required_named\n'
        'ts_rank,Time Series,REGULAR,"ts_rank(x, d)",desc,2,2,\n'
        'hump,Transform,REGULAR,"hump(x, hump=0.01)",desc,1,2,hump\n'
    )
    monkeypatch.setattr(oc, 'LIVE_OPERATORS_CSV', str(live))
    oc._CACHE = None
    try:
        assert oc.arity('ts_rank') == (2, 2)
        assert oc.arity('hump') == (1, 2)
        assert 'hump' in oc.required_named('hump')
        assert oc.has_signature('ts_rank') is True
        # 라이브에 없는 연산자도 여전히 인식되지만 arity 는 미상(None)
        assert oc.is_operator('rank') is True
        assert oc.arity('rank') == (None, None)
        assert oc.has_signature('rank') is False
    finally:
        oc._CACHE = None   # 다른 테스트 오염 방지


def test_operator_catalog_without_live_has_no_arity(tmp_path, monkeypatch):
    monkeypatch.setattr(oc, 'LIVE_OPERATORS_CSV', str(tmp_path / 'nonexistent.csv'))
    oc._CACHE = None
    try:
        assert oc.has_signature('ts_rank') is False
        assert oc.is_operator('ts_rank') is True   # seed 로 여전히 인식 (무회귀)
    finally:
        oc._CACHE = None
