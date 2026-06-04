"""Tests for server/alpha_ast.py — tolerant WQB alpha expression parser.

Run with: python3.11 -m pytest tests/test_alpha_ast.py -v
"""
from __future__ import annotations

import sys
import os
import unittest
from unittest.mock import patch

# Ensure project root on path so `server` package is importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import alpha_ast
from server import alpha_lint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_vecfield():
    """Return the first vector field name from the real CSV, or None."""
    vf = alpha_lint.vector_datafields()
    if vf:
        return sorted(vf)[0]
    return None


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestTokenize(unittest.TestCase):

    def test_basic_call(self):
        toks = alpha_ast.tokenize('rank(close)')
        kinds = [t['kind'] for t in toks]
        self.assertEqual(kinds, ['NAME', 'LPAREN', 'NAME', 'RPAREN'])
        self.assertEqual(toks[0]['value'], 'rank')
        self.assertEqual(toks[2]['value'], 'close')

    def test_number(self):
        toks = alpha_ast.tokenize('ts_mean(close,5)')
        num_tok = next(t for t in toks if t['kind'] == 'NUM')
        self.assertEqual(num_tok['value'], '5')

    def test_decimal_number(self):
        toks = alpha_ast.tokenize('winsorize(close, 0.05)')
        nums = [t for t in toks if t['kind'] == 'NUM']
        self.assertEqual(len(nums), 1)
        self.assertEqual(nums[0]['value'], '0.05')

    def test_operator_symbols(self):
        toks = alpha_ast.tokenize('a+b*c')
        ops = [t for t in toks if t['kind'] == 'OP']
        self.assertEqual(len(ops), 2)

    def test_string_token(self):
        toks = alpha_ast.tokenize('"hello world"')
        self.assertEqual(len(toks), 1)
        self.assertEqual(toks[0]['kind'], 'STR')

    def test_empty(self):
        self.assertEqual(alpha_ast.tokenize(''), [])

    def test_whitespace_skipped(self):
        toks = alpha_ast.tokenize('rank ( close )')
        # whitespace not emitted
        self.assertFalse(any(t['kind'] == 'WS' for t in toks))

    def test_non_string_input(self):
        # Should not raise
        result = alpha_ast.tokenize(None)
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Parser — basic structure
# ---------------------------------------------------------------------------

class TestParse(unittest.TestCase):

    def test_simple_call_structure(self):
        node = alpha_ast.parse('rank(close)')
        self.assertIsNotNone(node)
        self.assertEqual(node['type'], 'call')
        self.assertEqual(node['name'], 'rank')
        self.assertEqual(len(node['args']), 1)
        arg = node['args'][0]
        self.assertEqual(arg['type'], 'name')
        self.assertEqual(arg['value'], 'close')

    def test_nested_call(self):
        node = alpha_ast.parse('rank(ts_delta(close,5))')
        self.assertIsNotNone(node)
        self.assertEqual(node['type'], 'call')
        self.assertEqual(node['name'], 'rank')
        inner = node['args'][0]
        self.assertEqual(inner['type'], 'call')
        self.assertEqual(inner['name'], 'ts_delta')
        self.assertEqual(len(inner['args']), 2)

    def test_empty_string(self):
        self.assertIsNone(alpha_ast.parse(''))

    def test_whitespace_only(self):
        self.assertIsNone(alpha_ast.parse('   '))

    def test_none_input(self):
        # Must not raise
        self.assertIsNone(alpha_ast.parse(None))

    def test_number_literal(self):
        node = alpha_ast.parse('42')
        self.assertIsNotNone(node)
        self.assertEqual(node['type'], 'num')
        self.assertEqual(node['value'], '42')

    def test_case_insensitive_name(self):
        node = alpha_ast.parse('RANK(Close)')
        self.assertIsNotNone(node)
        self.assertEqual(node['name'], 'rank')
        self.assertEqual(node['args'][0]['value'], 'close')

    def test_arithmetic_group(self):
        # a+b should produce a group (or similar) — should not crash
        node = alpha_ast.parse('a+b')
        self.assertIsNotNone(node)

    def test_multi_statement_semicolon(self):
        # semicolons separate statements — should not crash
        node = alpha_ast.parse('rank(close); ts_mean(open,5)')
        self.assertIsNotNone(node)

    def test_weird_input_no_crash(self):
        for expr in ['(', ')', '((((', '!!!', ',,,,', '']:
            try:
                alpha_ast.parse(expr)  # must not raise
            except Exception as e:
                self.fail(f"parse({expr!r}) raised {e}")


# ---------------------------------------------------------------------------
# operators_used
# ---------------------------------------------------------------------------

class TestOperatorsUsed(unittest.TestCase):

    def test_basic(self):
        result = alpha_ast.operators_used('rank(ts_delta(close,5))')
        self.assertIn('rank', result)
        self.assertIn('ts_delta', result)

    def test_unknown_not_included(self):
        result = alpha_ast.operators_used('some_unknown_op(weird_thing)')
        # some_unknown_op is not in operator catalog — should not appear
        self.assertNotIn('some_unknown_op', result)

    def test_empty(self):
        self.assertEqual(alpha_ast.operators_used(''), set())

    def test_none(self):
        self.assertEqual(alpha_ast.operators_used(None), set())


# ---------------------------------------------------------------------------
# fields_used
# ---------------------------------------------------------------------------

class TestFieldsUsed(unittest.TestCase):

    def test_close_is_field(self):
        result = alpha_ast.fields_used('rank(ts_delta(close,5))')
        self.assertIn('close', result)

    def test_operators_not_included(self):
        result = alpha_ast.fields_used('rank(close)')
        self.assertNotIn('rank', result)

    def test_single_char_excluded(self):
        # Tokenizer does produce the 'x' NAME token (that is correct tokenizer behavior).
        # But fields_used must exclude single-character names.
        result = alpha_ast.fields_used('f(x)')
        self.assertNotIn('x', result)

    def test_empty(self):
        self.assertEqual(alpha_ast.fields_used(''), set())


# ---------------------------------------------------------------------------
# max_depth
# ---------------------------------------------------------------------------

class TestMaxDepth(unittest.TestCase):

    def test_depth_zero_for_empty(self):
        self.assertEqual(alpha_ast.max_depth(''), 0)

    def test_depth_one(self):
        d = alpha_ast.max_depth('rank(close)')
        self.assertEqual(d, 1)

    def test_depth_two(self):
        d = alpha_ast.max_depth('rank(ts_delta(close,5))')
        # rank wraps ts_delta which wraps close → depth 2
        self.assertEqual(d, 2)

    def test_depth_three(self):
        d = alpha_ast.max_depth('rank(ts_mean(ts_delta(close,5),10))')
        self.assertEqual(d, 3)

    def test_does_not_crash(self):
        alpha_ast.max_depth(None)


# ---------------------------------------------------------------------------
# outermost_operator
# ---------------------------------------------------------------------------

class TestOutermostOperator(unittest.TestCase):

    def test_single_call(self):
        result = alpha_ast.outermost_operator('rank(close)')
        self.assertEqual(result, 'rank')

    def test_arithmetic_is_none(self):
        # top-level arithmetic: not a single call → None
        result = alpha_ast.outermost_operator('add(a,b)+1')
        self.assertIsNone(result)

    def test_unknown_op_is_none(self):
        # unknown identifier at top level is NOT a known operator
        result = alpha_ast.outermost_operator('some_unknown_op(a)')
        self.assertIsNone(result)

    def test_empty_is_none(self):
        self.assertIsNone(alpha_ast.outermost_operator(''))

    def test_none_is_none(self):
        self.assertIsNone(alpha_ast.outermost_operator(None))


# ---------------------------------------------------------------------------
# validate — balanced parentheses
# ---------------------------------------------------------------------------

class TestValidateBalanced(unittest.TestCase):

    def test_balanced_no_issues(self):
        issues = alpha_ast.validate('rank(close)')
        self.assertFalse(any('unbalanced' in i for i in issues))

    def test_extra_close_paren(self):
        issues = alpha_ast.validate('rank(close))')
        self.assertTrue(any('unbalanced' in i for i in issues))

    def test_unclosed_paren(self):
        issues = alpha_ast.validate('rank(close')
        self.assertTrue(any('unbalanced' in i for i in issues))

    def test_paren_inside_string_not_flagged(self):
        # The '(' and ')' inside the double-quoted string should be ignored
        issues = alpha_ast.validate('rank("a)b")')
        self.assertFalse(any('unbalanced' in i for i in issues),
                         f"Should not flag paren inside string, got: {issues}")

    def test_empty_string_balanced(self):
        # Empty string: depth==0, no unbalanced — or validate returns [] quickly
        issues = alpha_ast.validate('')
        # No unbalanced paren issue for empty code
        self.assertFalse(any('unbalanced' in i for i in issues))

    def test_double_empty_string_token(self):
        # "" — empty double-quoted string: in_str flips twice, net zero
        issues = alpha_ast.validate('rank("")')
        self.assertFalse(any('unbalanced' in i for i in issues))


# ---------------------------------------------------------------------------
# validate — scoped raw-vector check
# ---------------------------------------------------------------------------

class TestValidateScopedVector(unittest.TestCase):
    """Key tests for the scoped raw-vector detection.

    We first try to get a real vector field from the CSV. If none available,
    we monkeypatch alpha_lint.vector_datafields to return {'myvec'}.
    """

    def setUp(self):
        self._real_vecfield = _get_vecfield()
        if self._real_vecfield:
            self.vecfield = self._real_vecfield
            self._patch = None
        else:
            self.vecfield = 'myvec'
            self._patch = patch(
                'server.alpha_lint.vector_datafields',
                return_value={'myvec'}
            )
            self._patch.start()
            # Also patch inside alpha_ast's validate where it imports alpha_lint
            self._patch2 = patch(
                'server.alpha_ast._lint',
                create=True,
            )

    def tearDown(self):
        if self._patch is not None:
            self._patch.stop()

    def test_raw_vector_in_ts_mean_flagged(self):
        """ts_mean(<vecfield>, 5) — vecfield not wrapped in vec_* → FAIL."""
        expr = f'ts_mean({self.vecfield}, 5)'
        issues = alpha_ast.validate(expr)
        self.assertTrue(
            any('raw vector' in i or 'vec_*' in i for i in issues),
            f"Expected raw-vector issue for {expr!r}, got: {issues}"
        )

    def test_vec_avg_wrapping_ok(self):
        """vec_avg(<vecfield>) — directly wrapped in vec_* → OK."""
        expr = f'vec_avg({self.vecfield})'
        issues = alpha_ast.validate(expr)
        self.assertFalse(
            any('raw vector' in i or 'vec_*' in i for i in issues),
            f"Should NOT flag vec_*-wrapped vector field in {expr!r}, got: {issues}"
        )

    def test_vec_sum_nested_in_add_ok(self):
        """add(vec_sum(<vecfield>), close) — vecfield's DIRECT enclosing call is
        vec_sum, which starts with vec_* → OK."""
        expr = f'add(vec_sum({self.vecfield}), close)'
        issues = alpha_ast.validate(expr)
        self.assertFalse(
            any('raw vector' in i or 'vec_*' in i for i in issues),
            f"Should NOT flag field already wrapped by vec_sum in {expr!r}, got: {issues}"
        )

    def test_rank_wrapping_without_vec_flagged(self):
        """rank(<vecfield>) — rank does NOT start with vec_* → FAIL."""
        expr = f'rank({self.vecfield})'
        issues = alpha_ast.validate(expr)
        self.assertTrue(
            any('raw vector' in i or 'vec_*' in i for i in issues),
            f"Expected raw-vector issue for {expr!r}, got: {issues}"
        )

    def test_bare_vecfield_flagged(self):
        """A bare top-level vector field name (no enclosing call at all) → FAIL."""
        expr = self.vecfield
        issues = alpha_ast.validate(expr)
        self.assertTrue(
            any('raw vector' in i or 'vec_*' in i for i in issues),
            f"Expected raw-vector issue for bare {expr!r}, got: {issues}"
        )


# ---------------------------------------------------------------------------
# validate — tolerance (unknown identifiers NOT flagged)
# ---------------------------------------------------------------------------

class TestValidateTolerance(unittest.TestCase):

    def test_unknown_op_and_field_allowed(self):
        issues = alpha_ast.validate('some_unknown_op(weird_thing)')
        # Must NOT flag unknown identifiers
        self.assertFalse(issues, f"Expected no issues for unknown op/field, got: {issues}")

    def test_deeply_nested_allowed(self):
        expr = 'op1(op2(op3(op4(close))))'
        # No vec fields involved, should produce no issues
        issues = alpha_ast.validate(expr)
        self.assertFalse(any('vec' in i for i in issues))

    def test_none_no_crash(self):
        result = alpha_ast.validate(None)
        self.assertIsInstance(result, list)

    def test_parse_failure_returns_empty(self):
        # Completely garbage — parser should swallow and return []
        result = alpha_ast.validate('!@#$%^&*()')
        self.assertIsInstance(result, list)


# ---------------------------------------------------------------------------
# lint integration
# ---------------------------------------------------------------------------

class TestLintIntegration(unittest.TestCase):
    """Verify that alpha_lint.validate_alpha uses AST scoped vector check."""

    def test_vec_wrapped_passes(self):
        """A vector field wrapped in vec_avg should pass lint."""
        vecfield = _get_vecfield()
        if not vecfield:
            self.skipTest('No vector fields in CSV')
        expr = f'vec_avg({vecfield})'
        issues = alpha_lint.validate_alpha(expr)
        vec_issues = [i for i in issues if 'vec_*' in i or 'raw vector' in i]
        self.assertFalse(vec_issues,
                         f"vec_*-wrapped field should pass, got issues: {issues}")

    def test_raw_vec_fails_lint(self):
        """A vector field used raw (no vec_* wrapper) should fail lint."""
        vecfield = _get_vecfield()
        if not vecfield:
            self.skipTest('No vector fields in CSV')
        expr = f'ts_mean({vecfield}, 5)'
        issues = alpha_lint.validate_alpha(expr)
        vec_issues = [i for i in issues if 'vec_*' in i or 'raw vector' in i]
        self.assertTrue(vec_issues,
                        f"Raw vector field should fail lint, got issues: {issues}")

    def test_vec_wrapped_inside_rank_passes(self):
        """rank(vec_avg(vecfield)) — vecfield wrapped by vec_avg even though
        top-level is rank → should pass (vec_avg is the direct enclosing call)."""
        vecfield = _get_vecfield()
        if not vecfield:
            self.skipTest('No vector fields in CSV')
        expr = f'rank(vec_avg({vecfield}))'
        issues = alpha_lint.validate_alpha(expr)
        vec_issues = [i for i in issues if 'vec_*' in i or 'raw vector' in i]
        self.assertFalse(vec_issues,
                         f"Properly wrapped vec field should pass, got issues: {issues}")

    def test_import_ok(self):
        """Smoke: both modules importable together."""
        import importlib
        m1 = importlib.import_module('server.alpha_ast')
        m2 = importlib.import_module('server.alpha_lint')
        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)


if __name__ == '__main__':
    unittest.main()
