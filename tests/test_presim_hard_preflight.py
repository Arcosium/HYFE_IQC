# tests/test_presim_hard_preflight.py
# #2 시뮬 전 하드 프리플라이트 — field 존재 + operator arity. 확실히 불법일 때만 컷.
import server.presim_gate as pg
import server.operator_catalog as oc
import server.datafield_palette as dp


def _palette(n=60, extra=()):
    return set(extra) | {f'f{i}' for i in range(n)}


# ── field 존재 ──

def test_field_check_drops_unknown_field(monkeypatch):
    monkeypatch.setattr(dp, 'known_field_names', lambda: _palette(extra=('close', 'volume', 'returns')))
    kept, dropped = pg.screen([{'idx': 1, 'code': 'rank(clse) - ts_mean(returns, 5)'}], existing_codes=[])
    assert len(kept) == 0 and len(dropped) == 1
    assert 'clse' in dropped[0]['reason']


def test_field_check_keeps_curated_pv_field(monkeypatch):
    # 팔레트에 pv 필드 없음(실제와 동일) — curated 화이트리스트로 통과해야.
    monkeypatch.setattr(dp, 'known_field_names', lambda: _palette())
    kept, dropped = pg.screen([{'idx': 1, 'code': 'rank(close) - ts_mean(returns, 5)'}], existing_codes=[])
    assert len(kept) == 1 and len(dropped) == 0


def test_field_check_keeps_group_identifier(monkeypatch):
    monkeypatch.setattr(dp, 'known_field_names', lambda: _palette())
    kept, dropped = pg.screen([{'idx': 1, 'code': 'group_neutralize(rank(close), subindustry)'}], existing_codes=[])
    assert len(kept) == 1 and len(dropped) == 0


def test_field_check_skips_when_palette_sparse(monkeypatch):
    monkeypatch.setattr(dp, 'known_field_names', lambda: {'close'})   # < _MIN_PALETTE
    kept, _ = pg.screen([{'idx': 1, 'code': 'rank(clse)'}], existing_codes=[])
    assert len(kept) == 1


def test_field_check_skips_when_palette_none(monkeypatch):
    monkeypatch.setattr(dp, 'known_field_names', lambda: None)
    kept, _ = pg.screen([{'idx': 1, 'code': 'rank(clse)'}], existing_codes=[])
    assert len(kept) == 1


def test_field_check_off_via_opts(monkeypatch):
    monkeypatch.setattr(dp, 'known_field_names', lambda: _palette())
    kept, _ = pg.screen([{'idx': 1, 'code': 'rank(clse)'}], existing_codes=[], opts={'field_check': False})
    assert len(kept) == 1


# ── operator arity (라이브 시그니처 주입) ──

def _inject_ops(monkeypatch, tmp_path, csv_body):
    live = tmp_path / 'live_operators.csv'
    live.write_text('name,category,scope,definition,description,min_args,max_args,required_named\n' + csv_body)
    monkeypatch.setattr(oc, 'LIVE_OPERATORS_CSV', str(live))
    oc._CACHE = None


def test_arity_check_drops_too_many_args(monkeypatch, tmp_path):
    _inject_ops(monkeypatch, tmp_path, 'rank,Cross,REGULAR,"rank(x)",d,1,1,\n')
    try:
        kept, dropped = pg.screen([{'idx': 1, 'code': 'rank(close, volume)'}], existing_codes=[], opts={'field_check': False})
        assert len(dropped) == 1 and 'arity' in dropped[0]['reason']
    finally:
        oc._CACHE = None


def test_arity_check_keeps_valid(monkeypatch, tmp_path):
    _inject_ops(monkeypatch, tmp_path,
                'rank,Cross,REGULAR,"rank(x)",d,1,1,\nts_mean,TS,REGULAR,"ts_mean(x, d)",d,2,2,\n')
    try:
        kept, dropped = pg.screen([{'idx': 1, 'code': 'rank(close) - ts_mean(returns, 5)'}],
                                  existing_codes=[], opts={'field_check': False})
        assert len(kept) == 1 and len(dropped) == 0
    finally:
        oc._CACHE = None


def test_arity_inert_without_signatures(monkeypatch, tmp_path):
    monkeypatch.setattr(oc, 'LIVE_OPERATORS_CSV', str(tmp_path / 'none.csv'))
    oc._CACHE = None
    try:
        kept, _ = pg.screen([{'idx': 1, 'code': 'rank(close, volume, extra_thing)'}],
                            existing_codes=[], opts={'field_check': False})
        assert len(kept) == 1   # 시그니처 미상 → arity skip
    finally:
        oc._CACHE = None
