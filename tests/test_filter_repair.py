from server import gemini_strategist as gs


def test_filter_autofix_region_prefix_then_passes():
    strategies = [{'idx': 1, 'code': 'rank(USA.close)', 'desc': '', 'settings': {}}]
    out = gs._filter_by_lint(strategies, log_fn=None, forced_delay='1')
    assert len(out) == 1
    assert out[0]['code'] == 'rank(close)'


def test_filter_autofix_missing_lookback_delay0():
    strategies = [{'idx': 1, 'code': 'ts_mean(close)', 'desc': '', 'settings': {}}]
    out = gs._filter_by_lint(strategies, log_fn=None, forced_delay='0')
    assert out and out[0]['code'] == 'ts_mean(close,22)'
