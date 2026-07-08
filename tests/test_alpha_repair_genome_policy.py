from server import alpha_lint, alpha_repair


def test_repair_strips_unsupported_filter_named_argument_and_typos():
    code = 'add(rank(close),rank(open),filter=true)+signd_power(rank(volume),2)'
    fixed, actions = alpha_repair.repair(code, delay=1)
    assert 'filter=' not in fixed
    assert 'signed_power' in fixed
    assert 'drop_filter_attr' in actions
    assert 'common_typo' in actions


def test_lint_rejects_redefined_variables():
    issues = alpha_lint.validate_alpha('s1=rank(close); s1=rank(open); s1')
    assert any('redefined variables' in i for i in issues)
