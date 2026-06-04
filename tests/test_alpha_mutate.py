"""Tests for server/alpha_mutate.py — evolutionary mutation engine.

Run with: python3.11 -m pytest tests/test_alpha_mutate.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from server import alpha_mutate


class TestNumericParamVariants(unittest.TestCase):

    def test_basic_window_variants(self):
        """ts_mean(close, 20) should produce variants with different window values."""
        variants = alpha_mutate.numeric_param_variants('ts_mean(close, 20)')
        self.assertTrue(len(variants) > 0, 'Expected at least one variant')
        # Some variant should have a window != 20 (e.g. 16, 24, 30)
        original_n = '20'
        self.assertTrue(
            any(original_n not in v.replace('close', '') for v in variants)
            or any('16' in v or '24' in v or '30' in v for v in variants),
            f'Expected changed window in variants, got: {variants}',
        )

    def test_no_mangle_identifier_embedded_number(self):
        """The '20' in 'adv20' is part of an identifier — must NOT be mutated."""
        variants = alpha_mutate.numeric_param_variants('rank(adv20)')
        # No variant should have a broken identifier like 'adv16' or 'adv24'
        for v in variants:
            self.assertIn('adv20', v,
                          f'adv20 was mangled in variant: {v!r}')

    def test_adv20_in_larger_expression(self):
        """adv20 stays intact in a realistic multi-param expression."""
        code = 'rank(divide(close, adv20))'
        variants = alpha_mutate.numeric_param_variants(code)
        for v in variants:
            self.assertIn('adv20', v,
                          f'adv20 was mangled in: {v!r}')

    def test_caps_at_max_variants(self):
        # Many param sites: should not exceed max_variants
        code = 'add(ts_mean(close,20), ts_std_dev(volume,60))'
        for cap in [1, 3, 6]:
            variants = alpha_mutate.numeric_param_variants(code, max_variants=cap)
            self.assertLessEqual(len(variants), cap)

    def test_no_duplicate_variants(self):
        variants = alpha_mutate.numeric_param_variants('ts_mean(close, 20)')
        self.assertEqual(len(variants), len(set(variants)))

    def test_none_input(self):
        self.assertEqual(alpha_mutate.numeric_param_variants(None), [])  # type: ignore[arg-type]

    def test_empty_string(self):
        self.assertEqual(alpha_mutate.numeric_param_variants(''), [])

    def test_no_params(self):
        # A code with no numeric params
        self.assertEqual(alpha_mutate.numeric_param_variants('rank(close)'), [])

    def test_float_param_variants(self):
        """A float param should produce variants at 0.9x and 1.1x."""
        code = 'winsorize(close, 0.05)'
        variants = alpha_mutate.numeric_param_variants(code)
        # Should produce at least one variant with a different float
        self.assertTrue(len(variants) > 0, 'Expected float variants')
        for v in variants:
            self.assertNotEqual(v, code)

    def test_single_param(self):
        """ts_rank(close,10): produces variants with different windows."""
        variants = alpha_mutate.numeric_param_variants('ts_rank(close,10)')
        self.assertTrue(len(variants) > 0)
        # Must contain '10' in identifier positions but different in args
        for v in variants:
            self.assertNotEqual(v, 'ts_rank(close,10)')

    def test_all_variants_differ_from_original(self):
        code = 'ts_mean(close, 20)'
        variants = alpha_mutate.numeric_param_variants(code)
        for v in variants:
            self.assertNotEqual(v, code)


class TestNegate(unittest.TestCase):

    def test_basic_negate(self):
        result = alpha_mutate.negate('rank(close)')
        self.assertEqual(result, 'subtract(0, (rank(close)))')

    def test_negate_complex(self):
        code = 'ts_mean(close, 20)'
        result = alpha_mutate.negate(code)
        self.assertEqual(result, f'subtract(0, ({code}))')

    def test_negate_strips_whitespace(self):
        result = alpha_mutate.negate('  rank(close)  ')
        self.assertEqual(result, 'subtract(0, (rank(close)))')

    def test_negate_empty(self):
        self.assertEqual(alpha_mutate.negate(''), '')

    def test_negate_none(self):
        self.assertEqual(alpha_mutate.negate(None), '')  # type: ignore[arg-type]


class TestSwapVariants(unittest.TestCase):

    def test_ts_delta_to_ts_rank(self):
        """swap_variants on a ts_delta code should include a ts_rank variant."""
        variants = alpha_mutate.swap_variants('ts_delta(close,5)')
        codes = list(variants)
        self.assertTrue(
            any('ts_rank' in v for v in codes),
            f'Expected ts_rank variant, got: {codes}',
        )

    def test_ts_mean_to_ts_zscore(self):
        variants = alpha_mutate.swap_variants('rank(ts_mean(close,20))')
        self.assertTrue(any('ts_zscore' in v for v in variants),
                        f'Expected ts_zscore variant, got: {variants}')

    def test_adv20_to_adv60(self):
        variants = alpha_mutate.swap_variants('rank(divide(close, adv20))')
        self.assertTrue(any('adv60' in v for v in variants),
                        f'Expected adv60 variant, got: {variants}')

    def test_adv20_to_adv120(self):
        variants = alpha_mutate.swap_variants('rank(divide(close, adv20))')
        self.assertTrue(any('adv120' in v for v in variants),
                        f'Expected adv120 variant, got: {variants}')

    def test_no_partial_match(self):
        """adv200 is NOT adv20 — should not be swapped."""
        code = 'rank(adv200)'
        variants = alpha_mutate.swap_variants(code)
        for v in variants:
            # adv200 should survive unchanged in the result
            self.assertIn('adv200', v,
                          f'adv200 was incorrectly altered in: {v!r}')

    def test_no_duplicate_variants(self):
        variants = alpha_mutate.swap_variants('ts_delta(close,5)')
        self.assertEqual(len(variants), len(set(variants)))

    def test_all_variants_differ_from_original(self):
        code = 'ts_delta(close,5)'
        for v in alpha_mutate.swap_variants(code):
            self.assertNotEqual(v, code)

    def test_empty_input(self):
        self.assertEqual(alpha_mutate.swap_variants(''), [])

    def test_none_input(self):
        self.assertEqual(alpha_mutate.swap_variants(None), [])  # type: ignore[arg-type]

    def test_no_swappable_tokens(self):
        # Code with no tokens in any swap group
        self.assertEqual(alpha_mutate.swap_variants('rank(close)'), [])


class TestMutate(unittest.TestCase):

    def test_returns_list_of_strings(self):
        result = alpha_mutate.mutate('ts_mean(close,20)')
        self.assertIsInstance(result, list)
        for v in result:
            self.assertIsInstance(v, str)

    def test_caps_at_max_variants(self):
        for cap in [1, 4, 8, 16]:
            result = alpha_mutate.mutate('ts_mean(close,20)', max_variants=cap)
            self.assertLessEqual(len(result), cap,
                                 f'Expected <= {cap} variants, got {len(result)}')

    def test_dedupes(self):
        result = alpha_mutate.mutate('ts_mean(close,20)')
        self.assertEqual(len(result), len(set(result)))

    def test_deterministic(self):
        code = 'ts_mean(close,20)'
        self.assertEqual(
            alpha_mutate.mutate(code),
            alpha_mutate.mutate(code),
        )

    def test_deterministic_complex(self):
        code = 'rank(divide(ts_delta(close,5), adv20))'
        self.assertEqual(
            alpha_mutate.mutate(code, max_variants=16),
            alpha_mutate.mutate(code, max_variants=16),
        )

    def test_returns_empty_for_empty_string(self):
        self.assertEqual(alpha_mutate.mutate(''), [])

    def test_returns_empty_for_none(self):
        self.assertEqual(alpha_mutate.mutate(None), [])  # type: ignore[arg-type]

    def test_no_variant_equals_original(self):
        code = 'ts_mean(close,20)'
        for v in alpha_mutate.mutate(code):
            self.assertNotEqual(v, code)

    def test_includes_negation_by_default(self):
        code = 'rank(close)'
        result = alpha_mutate.mutate(code)
        neg = alpha_mutate.negate(code)
        self.assertIn(neg, result,
                      f'Expected negation {neg!r} in mutate result')

    def test_no_negation_when_disabled(self):
        code = 'rank(close)'
        result = alpha_mutate.mutate(code, include_negation=False)
        neg = alpha_mutate.negate(code)
        self.assertNotIn(neg, result)

    def test_numeric_variants_precede_swaps_precede_negation(self):
        """Ordering: numeric first, then swaps, then negation (at end)."""
        code = 'ts_delta(close,10)'
        result = alpha_mutate.mutate(code, max_variants=20, include_negation=True)
        neg = alpha_mutate.negate(code)
        if neg in result:
            neg_idx = result.index(neg)
            # Negation should be last (or at least after any numeric/swap variants)
            self.assertEqual(neg_idx, len(result) - 1,
                             'Negation should be the last variant')

    def test_sample_output_nonempty(self):
        """Integration smoke test — mutate a real-world code produces variants."""
        result = alpha_mutate.mutate('rank(ts_mean(close,20))')
        self.assertGreater(len(result), 0)

    def test_adv20_not_mangled_in_mutate(self):
        """Identifier-embedded 20 in adv20 stays intact across mutate()."""
        code = 'rank(divide(close, adv20))'
        for v in alpha_mutate.mutate(code, max_variants=20):
            # adv20 may become adv60/adv120 (swap) but not adv16/adv24 etc.
            import re
            adv_tokens = re.findall(r'\badv\d+\b', v)
            for tok in adv_tokens:
                self.assertIn(tok, {'adv20', 'adv60', 'adv120'},
                              f'Unexpected adv token {tok!r} in variant: {v!r}')
