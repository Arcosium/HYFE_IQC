from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import alpha_ast

class TestUnaryMinus(unittest.TestCase):
    def test_leading_neg_mul_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-1 * ts_corr(rank(close), rank(volume), 10)'))
    def test_neg_call_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-rank(close)'))
    def test_neg_number_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-1'))
    def test_fields_and_ops_under_negation(self):
        self.assertIn('close', alpha_ast.fields_used('-rank(close)'))
        self.assertIn('rank', alpha_ast.operators_used('-rank(close)'))
    def test_unary_distinct_from_positive(self):
        self.assertNotEqual(alpha_ast.parse('-rank(close)'), alpha_ast.parse('rank(close)'))
    def test_plus_unary_is_noop(self):
        self.assertEqual(alpha_ast.parse('+rank(close)'), alpha_ast.parse('rank(close)'))

class TestComplexityMetrics(unittest.TestCase):
    def test_symbol_length(self):
        self.assertEqual(alpha_ast.symbol_length('rank(close)'), len('rank(close)'))
    def test_base_feature_count(self):
        self.assertEqual(alpha_ast.base_feature_count('ts_corr(rank(close), rank(volume), 10)'), 2)
    def test_base_feature_count_dedup(self):
        self.assertEqual(alpha_ast.base_feature_count('close - ts_mean(close, 5)'), 1)
    def test_free_const_ratio_range(self):
        r = alpha_ast.free_const_ratio('rank(close) + 0.5')
        self.assertGreater(r, 0.0); self.assertLessEqual(r, 1.0)
    def test_free_const_ratio_no_consts(self):
        self.assertEqual(alpha_ast.free_const_ratio('rank(close)'), 0.0)
    def test_metrics_safe_on_garbage(self):
        self.assertIsInstance(alpha_ast.symbol_length(None), int)
        self.assertIsInstance(alpha_ast.base_feature_count('((('), int)
        self.assertIsInstance(alpha_ast.free_const_ratio(''), float)


class TestStructuralOverlap(unittest.TestCase):
    def test_identical_structure_different_windows(self):
        a = 'ts_corr(rank(close), rank(volume), 10)'
        b = 'ts_corr(rank(close), rank(volume), 20)'
        self.assertGreaterEqual(alpha_ast.largest_common_subtree(a, b), 5)
    def test_disjoint_structure_small_overlap(self):
        self.assertLessEqual(alpha_ast.largest_common_subtree('rank(close)', 'ts_mean(volume, 5)'), 1)
    def test_self_overlap_is_full(self):
        a = 'ts_corr(rank(close), rank(volume), 10)'
        self.assertEqual(alpha_ast.largest_common_subtree(a, a),
                         len(list(alpha_ast._walk(alpha_ast.parse(a)))))
    def test_structural_overlap_finds_worst(self):
        code = 'ts_corr(rank(close), rank(volume), 15)'
        others = ['rank(close)', 'ts_corr(rank(close), rank(volume), 10)']
        size, idx = alpha_ast.structural_overlap(code, others)
        self.assertEqual(idx, 1)
        self.assertGreaterEqual(size, 5)
    def test_overlap_safe_on_garbage(self):
        self.assertEqual(alpha_ast.largest_common_subtree('(((', 'close'), 0)
        self.assertEqual(alpha_ast.structural_overlap('close', []), (0, -1))


if __name__ == '__main__':
    unittest.main()
