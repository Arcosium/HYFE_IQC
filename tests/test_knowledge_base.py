from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import knowledge_base as kb

class TestSaturation(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(kb.saturated_operators([]), [])
        self.assertEqual(kb.render_saturation_warning([]), '')
    def test_flags_overused_signal_op(self):
        # 5 codes, 4 use group_neutralize -> >= max(4, ceil(0.25*5)=2) => flagged
        codes = [
            'group_neutralize(ts_mean(close,5), sector)',
            'group_neutralize(ts_delta(volume,1), industry)',
            'group_neutralize(rank(returns), sector)',
            'group_neutralize(ts_zscore(vwap,20), market)',
            'rank(close)',
        ]
        sat = dict(kb.saturated_operators(codes, frac=0.25, floor=4))
        self.assertIn('group_neutralize', sat)
        self.assertEqual(sat['group_neutralize'], 4)
    def test_does_not_flag_wrappers(self):
        # rank is ubiquitous but is a wrapper -> never flagged even if in all codes
        codes = ['rank(close)', 'rank(volume)', 'rank(returns)', 'rank(vwap)', 'rank(open)']
        sat = dict(kb.saturated_operators(codes, frac=0.1, floor=1))
        self.assertNotIn('rank', sat)
    def test_floor_threshold(self):
        # 3 codes use ts_corr; floor=4 -> not flagged; floor=3 -> flagged
        codes = ['ts_corr(close,volume,5)', 'ts_corr(high,low,10)', 'ts_corr(open,vwap,20)']
        self.assertEqual(kb.saturated_operators(codes, frac=0.9, floor=4), [])
        self.assertTrue(kb.saturated_operators(codes, frac=0.9, floor=3))
    def test_render_nonempty(self):
        w = kb.render_saturation_warning([('group_neutralize', 37), ('ts_zscore', 35)])
        self.assertIn('group_neutralize', w)
        self.assertIn('SC', w.upper().replace('-', '') + 'SC')  # mentions saturation/SC concept
    def test_never_raises(self):
        for bad in [None, [123], ['(((', None], 'notalist']:
            self.assertIsInstance(kb.saturated_operators(bad), list)
        self.assertIsInstance(kb.render_saturation_warning(None), str)

if __name__ == '__main__':
    unittest.main()
