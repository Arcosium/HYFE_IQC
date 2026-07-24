"""D0(delay=0) 필드 팔레트 회귀 테스트.

배경: 2026-07-21 까지 GA 는 'D0 면 무조건 pv' 로 강제하고 있었다. 근거는 옛 주석의
"delay=0 에선 fundamental/analyst 필드가 ERROR" 였는데, 라이브 실측으로 반증됐다 —
option6 는 D0 에 131 필드, fundamental2 는 766 필드가 있고, 둘 다 D0 시뮬이 정상 완료된다
(2026-07-21 알파 0mMkeNY8·N1RWgPVp·0mMkeNEr). 그 제약이 **USA/D0/OPTION 피라미드
1.7배(전 항목 최고 배수)** 를 구조적으로 막고 있었다.

이 테스트가 지키는 계약은 두 가지다:
  1) D0 팔레트를 **모를 때**(라이브 CSV 미수집)는 예전처럼 pv 로만 간다 — 모르는 채
     D1 필드를 쓰면 라운드가 통째로 ERROR 난다.
  2) D0 팔레트를 **알 때**는 family 를 존중하고 그 팔레트 안에서만 고른다.
"""
import random

import pytest

from server import genome_models as gm


@pytest.fixture()
def no_d0_palette(monkeypatch):
    monkeypatch.setattr(gm, 'D0_DATASETS', {})
    return None


@pytest.fixture()
def with_d0_palette(monkeypatch):
    palette = {
        'pv': gm.SHARED_DATASETS['pv'],
        'option': ('opt6_20div', 'opt6_ivspyratio', 'opt6_fcstr2imp', 'opt6_divyield'),
        'fundamental': ('accrued_liabilities_total', 'assets', 'equity', 'cash'),
    }
    monkeypatch.setattr(gm, 'D0_DATASETS', palette)
    return palette


def test_without_palette_d0_falls_back_to_pv(no_d0_palette):
    """팔레트를 모르면 옛 안전 동작(pv 전용)을 유지한다."""
    assert gm.d0_allowed_fields() is None
    picked = gm._pick_fields(random.Random(1), 'option', set(), '0')
    assert all(f in gm.SHARED_DATASETS['pv'] for f in picked)


def test_with_palette_d0_uses_family_fields(with_d0_palette):
    """팔레트를 알면 D0 에서도 option 계열 필드를 고른다 (피라미드 1.7배의 열쇠)."""
    picked = gm._pick_fields(random.Random(1), 'option', set(), '0')
    assert all(f in with_d0_palette['option'] for f in picked)
    assert gm.d0_allowed_fields() is not None
    assert 'opt6_20div' in gm.d0_allowed_fields()


def test_constrain_keeps_non_pv_family_at_d0(with_d0_palette):
    """_constrain 이 D0 라는 이유만으로 family 를 pv 로 깎아내리면 안 된다."""
    model = gm.ResearchConsultantGenomeModel(round_num=1, forced_delay='0')
    g = gm._coerce_genome({
        'model': 'x', 'family': 'option',
        'fields': with_d0_palette['option'][:3],
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum', 'sign': 1,
        'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP3000',
        'neutralization': 'SECTOR', 'decay': 0, 'truncation': 0.08,
    })
    out = model._constrain(g, random.Random(3))
    assert out.family == 'option'
    assert all(f in with_d0_palette['option'] for f in out.fields)


def test_constrain_replaces_fields_invalid_at_d0(with_d0_palette):
    """D0 에 없는 필드는 갈아끼운다 (family 는 가능하면 유지)."""
    model = gm.ResearchConsultantGenomeModel(round_num=1, forced_delay='0')
    g = gm._coerce_genome({
        'model': 'x', 'family': 'option',
        'fields': ('nonexistent_d1_only_field', 'another_bogus', 'third_bogus'),
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum', 'sign': 1,
        'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP3000',
        'neutralization': 'SECTOR', 'decay': 0, 'truncation': 0.08,
    })
    out = model._constrain(g, random.Random(5))
    assert all(f in gm.d0_allowed_fields() for f in out.fields)


def test_constrain_downgrades_unknown_family_to_pv(with_d0_palette):
    """D0 팔레트에 없는 family(news 등)는 pv 로 내린다 — 임의 필드로 ERROR 내지 않는다."""
    model = gm.ResearchConsultantGenomeModel(round_num=1, forced_delay='0')
    g = gm._coerce_genome({
        'model': 'x', 'family': 'news', 'fields': ('bogus1', 'bogus2', 'bogus3'),
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum', 'sign': 1,
        'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP3000',
        'neutralization': 'SECTOR', 'decay': 0, 'truncation': 0.08,
    })
    out = model._constrain(g, random.Random(7))
    assert out.family == 'pv'
    assert all(f in gm.SHARED_DATASETS['pv'] for f in out.fields)


def test_d1_is_unaffected_by_d0_palette(with_d0_palette):
    """D0 팔레트 도입이 D1 경로를 건드리면 안 된다."""
    picked = gm._pick_fields(random.Random(1), 'analyst', set(), '1')
    assert all(f in gm.SHARED_DATASETS['analyst'] for f in picked)
