"""모바일 제출 내역과 알파 상세 화면의 정적 UI 계약."""
from pathlib import Path


MOBILE = (Path(__file__).resolve().parents[1] / 'static' / 'mobile.html').read_text(
    encoding='utf-8')


def test_mobile_submit_history_shows_expression_instead_of_result_reason():
    assert '<th colspan="2">알파 식</th>' in MOBILE
    assert '<th>결과</th>' not in MOBILE
    assert '<th>사유</th>' not in MOBILE
    assert "_td(a.code || '—', 'alpha-expr')" in MOBILE


def test_mobile_submit_row_opens_desktop_style_alpha_detail():
    assert 'id="alpha-dlg"' in MOBILE
    assert "makeAlphaRow(tr, a.code)" in MOBILE
    assert "'/api/alpha?code='" in MOBILE
    for label in ('알파 ID', 'WQB ID', '알파 식', 'Self-corr', 'PASS', 'FAIL'):
        assert label in MOBILE


def test_mobile_search_constraint_card_is_removed():
    for removed in ('id="constraint-text"', 'id="btn-constraint-save"',
                    'id="btn-constraint-clear"', 'id="constraint-status"'):
        assert removed not in MOBILE
    assert 'initConstraint' not in MOBILE
