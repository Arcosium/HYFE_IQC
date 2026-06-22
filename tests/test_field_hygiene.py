# tests/test_field_hygiene.py — apply_field_hygiene 결정론적 필드 위생 래퍼
import server.alpha_ast as a


def test_wraps_base_pv_fields():
    out = a.apply_field_hygiene('rank(returns * volume)')
    assert 'winsorize(ts_backfill(returns, 120), std=4)' in out
    assert 'winsorize(ts_backfill(volume, 120), std=4)' in out
    assert out.startswith('rank(')


def test_excludes_group_names():
    out = a.apply_field_hygiene('group_neutralize(close, sector)')
    assert 'winsorize(ts_backfill(close, 120), std=4)' in out
    assert 'ts_backfill(sector' not in out          # group 명은 래핑 금지
    assert out.rstrip().endswith('sector)')


def test_excludes_intermediate_vars_and_named_args():
    out = a.apply_field_hygiene(
        'sig_a = rank(close); add(sig_a, ts_zscore(volume, 60), filter=true)')
    assert 'ts_backfill(close' in out and 'ts_backfill(volume' in out
    assert 'ts_backfill(sig_a' not in out           # 중간변수 금지
    assert 'ts_backfill(filter' not in out          # named-arg key 금지
    assert 'filter=true' in out


def test_does_not_wrap_named_arg_keys():
    out = a.apply_field_hygiene('hump(rank(close), hump=0.03)')
    assert 'ts_backfill(close' in out
    assert 'ts_backfill(hump' not in out
    assert 'hump=0.03' in out


def test_idempotent():
    once = a.apply_field_hygiene('rank(close - vwap)')
    twice = a.apply_field_hygiene(once)
    assert once == twice
    assert once.count('ts_backfill(close,') == 1     # 이중 래핑 없음


def test_skips_vector_alphas():
    code = 'rank(vec_avg(nws12_prez_result2))'
    assert a.apply_field_hygiene(code) == code       # vec_ 알파는 그대로


def test_never_raises_on_garbage():
    assert a.apply_field_hygiene('((((') is not None
    assert a.apply_field_hygiene('') == ''


def test_wraps_skeleton_keeps_group():
    # 검증된 스켈레톤: group_neutralize(A, sector) 안의 A 만 래핑, sector 유지
    out = a.apply_field_hygiene('-1 * rank(ts_corr(group_neutralize(operating_income, sector), assets, 5))')
    assert 'ts_backfill(operating_income' in out
    assert 'ts_backfill(assets' in out
    assert 'ts_backfill(sector' not in out
