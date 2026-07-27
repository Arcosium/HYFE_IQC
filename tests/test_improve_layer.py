# tests/test_improve_layer.py
# 개선 레이어(AAF 이식): 회전율 등급별 lookback 재스케일·trade_when·decay 변형.
from server import alpha_ast, improve_layer


def test_turnover_class():
    assert improve_layer.turnover_class('0.05') == 'LT'
    assert improve_layer.turnover_class('9.45%') == 'LT'
    assert improve_layer.turnover_class(0.3) == 'MID'
    assert improve_layer.turnover_class('0.55') == 'HT'


def test_rescale_windows_proportional():
    # 지배 창 252 → 504 로 놓으면 126 도 2배로 늘어난다 (다중 스케일 보존).
    out = improve_layer.rescale_windows('ts_rank(ts_backfill(anl4_x, 252), 126)', 504)
    assert out == 'ts_rank(ts_backfill(anl4_x, 504), 252)'
    # 창이 없으면 None. 결과가 원본과 같아도 None.
    assert improve_layer.rescale_windows('rank(close)', 252) is None
    assert improve_layer.rescale_windows('ts_delta(close, 252)', 252) is None


def test_rescale_ignores_floats_and_small_ints():
    out = improve_layer.rescale_windows('hump(ts_delta(close, 20), hump=0.03) * 2', 40)
    assert '0.03' in out and '* 2' in out and '40' in out


def test_lt_variants():
    v = improve_layer.variants('ts_zscore(anl4_x, 252)', {'universe': 'TOP3000'},
                               {'turnover': '0.05'}, n=3)
    # LT 그리드 252(=원본, skip)/504/756 → 2개
    assert len(v) == 2
    assert {improve_layer.IDX_BASE, improve_layer.IDX_BASE + 1} == {c['idx'] for c in v}
    for c in v:
        assert c['origin'] == 'improve'
        assert alpha_ast.parse(c['code'])
        assert c['settings']['universe'] == 'TOP3000'


def test_ht_variants_prefer_post_smoothing_then_trade_when():
    """2026-07-27 실측 교정: 창 확대는 신호를 죽이고 사후 감쇠는 살린다
    (CLV 5일 S=1.01 → 창10일 0.36 / 사후감쇠 1.07). 감쇠가 **앞**에 와야 한다."""
    v = improve_layer.variants('rank(ts_delta(close, 5))', {'decay': '2'},
                               {'turnover': '0.60'}, n=8)
    codes = [c['code'] for c in v]
    assert codes[0].startswith('ts_decay_linear(')          # 1순위 = 사후 감쇠
    assert any(c.startswith('ts_decay_linear(rank(ts_delta(close, 5)), 20)')
               for c in codes)
    assert any(c.startswith('trade_when(volume > adv20,') for c in codes)
    decay_vars = [c for c in v if c['code'] == 'rank(ts_delta(close, 5))']
    assert decay_vars and decay_vars[0]['settings']['decay'] == '8'
    for c in v:
        assert alpha_ast.parse(c['code'])


def test_empty_code_and_zero_n():
    assert improve_layer.variants('', {}, {}, n=3) == []
    assert improve_layer.variants('rank(close)', {}, {}, n=0) == []
