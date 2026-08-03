# tests/test_region_exists_no_swap.py
# _apply_constraint 의 리전 존재 검사는 캡 없는 전체 집합으로 — 캡 걸린 풀(계열당
# 상한·계열분류·커버리지 컷)로 판정하면 실존 필드가 몰래 치환된다
# (2026-08-03 실측: 전략스펙 resvol→srisk, divyild→indmom 둔갑 시뮬).
from server import genome_models as gm


class _C:
    universe = None
    excluded_datasets = ()


def _wire(monkeypatch, full):
    monkeypatch.setattr(gm, '_ACTIVE_CONSTRAINT', _C())
    monkeypatch.setattr(gm, '_REGION_DATASETS', {'model': ('f1', 'f2', 'f3')})
    monkeypatch.setattr(gm, '_REGION_FULL_FIELDS', frozenset(full))
    monkeypatch.setattr(gm, '_CONSTRAINT_BANNED_FIELDS', frozenset())
    monkeypatch.setattr(gm, '_allowed_neutralizations', lambda: ['CROWDING'])


def test_existing_but_uncapped_field_survives(monkeypatch):
    _wire(monkeypatch, {'fresh_axis', 'neutf', 'suppf', 'f1', 'f2', 'f3'})
    d = {'family': 'model', 'fields': ('fresh_axis', 'neutf', 'suppf'),
         'neutralization': 'CROWDING', 'decay': 4, 'transform_a': 'ts_zscore'}
    gm._apply_constraint(d)
    assert d['fields'] == ('fresh_axis', 'neutf', 'suppf')


def test_truly_missing_field_still_swapped(monkeypatch):
    _wire(monkeypatch, {'f1', 'f2', 'f3'})
    d = {'family': 'model', 'fields': ('ghost_field', 'f1', 'f2'),
         'neutralization': 'CROWDING', 'decay': 4, 'transform_a': 'ts_zscore'}
    gm._apply_constraint(d)
    assert 'ghost_field' not in d['fields']
    assert all(f in {'f1', 'f2', 'f3'} for f in d['fields'])
