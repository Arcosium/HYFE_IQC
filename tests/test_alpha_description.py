"""Power Pool 설명 생성기 — 형식·최소 길이·fail-soft 검증.

실행: python3 -m pytest tests/test_alpha_description.py
"""
from server import alpha_description as ad


_CODE = ('rank(vector_neut(ts_zscore(winsorize(ts_backfill(fieldA,120),std=4),20),'
         'ts_av_diff(winsorize(ts_backfill(fieldB,120),std=4),60)))')
_GENOME = {'family': 'shortinterest', 'fields': ['fieldA', 'fieldB', 'fieldC'],
           'combine': 'resid', 'sign': -1, 'neutralization': 'STATISTICAL',
           'regime': 'OFF'}


def test_build_has_required_sections_and_length():
    d = ad.build(_CODE, genome=_GENOME)
    # 문서 요건: 100자 이상 + Idea/Rationale 템플릿.
    assert len(d) >= 100
    assert d.startswith('Idea: ')
    assert 'Rationale for data used:' in d
    assert 'Rationale for operators used:' in d
    # 유전자가 서술에 반영된다.
    assert 'short interest' in d
    assert 'orthogonal' in d               # resid 결합
    assert 'mean reversion' in d           # sign=-1
    assert 'statistical risk factors' in d # STATISTICAL 중립화


def test_build_lists_operators_from_code():
    d = ad.build(_CODE, genome=_GENOME)
    for op in ('vector_neut()', 'ts_zscore()', 'winsorize()', 'ts_backfill()'):
        assert op in d


def test_build_without_genome_still_valid():
    d = ad.build('rank(ts_delta(close,5))', genome=None, settings=None)
    assert len(d) >= 100
    assert d.startswith('Idea: ')
    assert 'Rationale for operators used:' in d


def test_build_never_raises_on_garbage():
    assert len(ad.build('', genome={'sign': 'x'}, settings=None)) >= 100
    assert len(ad.build(None, genome=None)) >= 100
