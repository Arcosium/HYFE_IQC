from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import directed_mutation as dm

class TestRoute(unittest.TestCase):
    def test_sharpe_below_positive_raises(self):
        r = dm.route(['Sharpe of 1.55 is below cutoff of 2.'])
        self.assertIn('raise_sharpe', r['strategy'])
        self.assertTrue(r['instruction'])
    def test_sharpe_negative_sign_flip(self):
        r = dm.route(['Sharpe of -1.13 is below cutoff of 2.'])
        self.assertIn('sign_flip', r['strategy'])
        self.assertIn('-1', r['instruction'])
    def test_turnover_high_cut(self):
        r = dm.route(['Turnover of 95.0% is above cutoff of 70%.'])
        self.assertIn('cut_turnover', r['strategy'])
        self.assertIn('ts_decay_linear', r['instruction'])
    def test_turnover_low_raise(self):
        r = dm.route(['Turnover of 0.50% is below cutoff of 1%.'])
        self.assertIn('raise_turnover', r['strategy'])
    def test_fitness_below(self):
        r = dm.route(['Fitness of 0.91 is below cutoff of 1.3.'])
        self.assertIn('raise_fitness', r['strategy'])
    def test_subuniverse_sharpe(self):
        r = dm.route(['Sub-universe Sharpe of 0.50 is below cutoff of 0.82.'])
        self.assertIn('subuniv', r['strategy'])
        self.assertIn('group_neutralize', r['instruction'])
    def test_weight_concentration_keyword(self):
        r = dm.route(['Weight is not well distributed over instruments.'])
        self.assertIn('spread_weight', r['strategy'])
    def test_weight_concentration_numeric(self):
        r = dm.route(['Weight concentration of 50% is above cutoff of 10%.'])
        self.assertIn('spread_weight', r['strategy'])
    def test_self_correlation(self):
        r = dm.route(['Self-correlation of 0.94 is above cutoff of 0.7.'])
        self.assertIn('rotate_family', r['strategy'])
    def test_multiple_fails_combined(self):
        r = dm.route(['Sharpe of 1.55 is below cutoff of 2.',
                      'Turnover of 90% is above cutoff of 70%.'])
        self.assertIn('raise_sharpe', r['strategy'])
        self.assertIn('cut_turnover', r['strategy'])
    def test_empty_is_generic(self):
        r = dm.route([])
        self.assertEqual(r['instruction'], '')
    def test_never_raises(self):
        for bad in [None, [123], ['garbage'], 'notalist']:
            r = dm.route(bad)
            self.assertIn('strategy', r); self.assertIn('instruction', r)

if __name__ == '__main__':
    unittest.main()
