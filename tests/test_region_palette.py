# tests/test_region_palette.py
# GLB 테마(2026-07-27): 지역 필드 팔레트 강제 — USA 필드의 'unknown variable' 전멸 방지.
import pytest

from server import constraint_spec, datafield_palette, genome_models


@pytest.fixture(autouse=True)
def _reset_constraint():
    yield
    genome_models.set_constraint(None)


def test_family_pools_region_filter(monkeypatch):
    rows = [
        {'name': 'glb_fnd_x1', 'region': 'GLB', 'universe': 'TOPDIV3000', 'delay': '1',
         'coverage': '90', 'alphas': '5'},
        {'name': 'usa_fnd_y1', 'region': 'USA', 'universe': 'TOP3000', 'delay': '1',
         'coverage': '90', 'alphas': '5'},
    ]
    monkeypatch.setattr(datafield_palette, '_all_rows', lambda: rows)
    monkeypatch.setattr(datafield_palette, 'classify_family', lambda n: 'fundamental')
    datafield_palette._POOL_CACHE.clear()
    glb = datafield_palette.family_pools(delay=1, region='GLB', universe='TOPDIV3000')
    assert glb == {'fundamental': ['glb_fnd_x1']}
    datafield_palette._POOL_CACHE.clear()
    usa = datafield_palette.family_pools(delay=1, region='USA', universe='TOP3000')
    assert usa == {'fundamental': ['usa_fnd_y1']}


def test_apply_constraint_swaps_to_region_fields(monkeypatch):
    spec = constraint_spec.parse(
        "region=GLB & delay=1 & universe=TOPDIV3000 and neutralization in "
        "(slow, fast, ram) and datasets not in ['pv1']")
    # 팔레트 로드를 가짜 풀로 대체
    monkeypatch.setattr(
        datafield_palette, 'family_pools',
        lambda **kw: {'fundamental': ['glb_f1', 'glb_f2', 'glb_f3', 'glb_f4']})
    genome_models.set_constraint(spec)
    assert genome_models.region_allowed_fields() == frozenset(
        {'glb_f1', 'glb_f2', 'glb_f3', 'glb_f4'})
    d = {'family': 'model', 'fields': ('mdl177_usa_only', 'afinn_x', 'shrt36_y'),
         'universe': 'TOP3000', 'neutralization': 'INDUSTRY', 'decay': 4,
         'transform_a': 'rank'}
    genome_models._apply_constraint(d)
    assert d['universe'] == 'TOPDIV3000'
    assert d['neutralization'] in ('SLOW', 'FAST', 'REVERSION_AND_MOMENTUM')
    assert all(f.startswith('glb_') for f in d['fields'])       # 전면 교체
    assert len(set(d['fields'])) == 3
    # 결정론: 같은 입력이면 같은 결과 (dedup 키 안정성)
    d2 = {'family': 'model', 'fields': ('mdl177_usa_only', 'afinn_x', 'shrt36_y'),
          'universe': 'TOP3000', 'neutralization': 'INDUSTRY', 'decay': 4,
          'transform_a': 'rank'}
    genome_models._apply_constraint(d2)
    assert d2['fields'] == d['fields']


def test_usa_constraint_keeps_fields(monkeypatch):
    spec = constraint_spec.parse('region=USA & delay=1 & universe=TOP1000')
    genome_models.set_constraint(spec)
    assert genome_models.region_allowed_fields() is None        # USA 는 팔레트 미강제
    d = {'family': 'pv', 'fields': ('close', 'volume', 'vwap'),
         'universe': 'TOP3000', 'neutralization': 'INDUSTRY', 'decay': 4,
         'transform_a': 'rank'}
    genome_models._apply_constraint(d)
    assert d['fields'] == ('close', 'volume', 'vwap')
