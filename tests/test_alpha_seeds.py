from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import random
import unittest

from server import alpha_seeds


class TestSampleRender(unittest.TestCase):
    def test_templates_nonempty_and_shaped(self):
        self.assertGreaterEqual(len(alpha_seeds.SEED_TEMPLATES), 8)
        for t in alpha_seeds.SEED_TEMPLATES:
            self.assertIn('family', t)
            self.assertIn('expr', t)
            self.assertIn('ops', t)
            self.assertIn('intuition', t)
            self.assertIsInstance(t['ops'], list)

    def test_sample_returns_n(self):
        seeds = alpha_seeds.sample_seeds(3, rng=random.Random(0))
        self.assertEqual(len(seeds), 3)

    def test_sample_caps_at_pool_size(self):
        seeds = alpha_seeds.sample_seeds(999, rng=random.Random(0))
        self.assertEqual(len(seeds), len(alpha_seeds.SEED_TEMPLATES))

    def test_sample_deterministic_with_rng(self):
        a = alpha_seeds.sample_seeds(4, rng=random.Random(42))
        b = alpha_seeds.sample_seeds(4, rng=random.Random(42))
        self.assertEqual([s['expr'] for s in a], [s['expr'] for s in b])

    def test_exclude_ops_filters(self):
        seeds = alpha_seeds.sample_seeds(999, exclude_ops={'group_neutralize'}, rng=random.Random(0))
        for s in seeds:
            self.assertNotIn('group_neutralize', s['ops'])

    def test_families_filter(self):
        fam = alpha_seeds.FAMILIES[0]
        seeds = alpha_seeds.sample_seeds(999, families=[fam], rng=random.Random(0))
        for s in seeds:
            self.assertEqual(s['family'], fam)

    def test_render_empty(self):
        self.assertEqual(alpha_seeds.render_seeds_section([]), '')

    def test_render_contains_exprs(self):
        seeds = alpha_seeds.sample_seeds(2, rng=random.Random(1))
        out = alpha_seeds.render_seeds_section(seeds)
        for s in seeds:
            self.assertIn(s['expr'], out)


class TestSeedContract(unittest.TestCase):
    """Every seed expression must be valid FASTEXPR: parses, no provable AST issue,
    no forbidden token / sci-notation, bare group, named hump."""

    def test_all_seeds_parse_and_lint_clean(self):
        from server import alpha_ast, alpha_lint
        from server.gemini_strategist import _alpha_violations
        for t in alpha_seeds.SEED_TEMPLATES:
            code = t['expr']
            with self.subTest(family=t['family']):
                self.assertIsNotNone(alpha_ast.parse(code), f'parse None: {code}')
                self.assertEqual(alpha_ast.validate(code), [], f'AST issue: {code}')
                self.assertEqual(_alpha_violations(code), [], f'violation: {code}')
                self.assertEqual(alpha_lint.validate_alpha(code), [], f'lint: {code}')

    def test_no_quoted_group_no_positional_hump_no_scinote(self):
        for t in alpha_seeds.SEED_TEMPLATES:
            code = t['expr']
            with self.subTest(family=t['family']):
                self.assertNotRegex(code, r"group_\w+\([^)]*['\"]")
                if 'hump(' in code:
                    self.assertRegex(code, r'hump\([^)]*hump\s*=')
                self.assertNotRegex(code, r'\d+\.?\d*[eE][+-]?\d+')


if __name__ == '__main__':
    unittest.main()
