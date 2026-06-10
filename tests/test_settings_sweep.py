from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import settings_sweep


class TestSweepCandidates(unittest.TestCase):
    CODE = 'group_neutralize(ts_zscore(close - vwap, 20), sector)'

    def test_returns_n_candidates(self):
        out = settings_sweep.sweep_candidates(self.CODE, {}, n=3, seed=0)
        self.assertEqual(len(out), 3)

    def test_same_code_distinct_settings(self):
        out = settings_sweep.sweep_candidates(self.CODE, {}, n=4, seed=0)
        for d in out:
            self.assertEqual(d['code'], self.CODE)
            self.assertIn('universe', d['settings'])
            self.assertIn('neutralization', d['settings'])
            self.assertTrue(d['sweep'])
        combos = {(d['settings']['universe'], d['settings']['neutralization']) for d in out}
        self.assertEqual(len(combos), 4)  # all distinct settings combos

    def test_skips_parent_own_combo(self):
        # parent already ran TOP1000×SUBINDUSTRY → sweep must not repeat it.
        parent = {'universe': 'TOP1000', 'neutralization': 'SUBINDUSTRY'}
        out = settings_sweep.sweep_candidates(self.CODE, parent, n=8, seed=0)
        combos = {(d['settings']['universe'], d['settings']['neutralization']) for d in out}
        self.assertNotIn(('TOP1000', 'SUBINDUSTRY'), combos)

    def test_grid_combos_distinct_after_sanitize(self):
        # 작은 universe×세밀 중립화는 worker 가 SECTOR 로 접으므로, 그리드가 그런 조합을
        # 만들면 안 된다(스윕 2개가 동일 effective settings 로 붕괴 = 낭비).
        out = settings_sweep.sweep_candidates(self.CODE, {}, n=8, seed=0)
        for d in out:
            u, nz = d['settings']['universe'], d['settings']['neutralization']
            if u in ('TOP200', 'TOP500'):
                self.assertNotIn(nz, ('INDUSTRY', 'SUBINDUSTRY'),
                                 f'{u}×{nz} 는 sanitize 후 SECTOR 로 붕괴')

    def test_inherits_smoothing_settings(self):
        parent = {'universe': 'TOP3000', 'neutralization': 'INDUSTRY',
                  'decay': '25', 'truncation': '0.02'}
        out = settings_sweep.sweep_candidates(self.CODE, parent, n=2, seed=0)
        for d in out:
            self.assertEqual(d['settings'].get('decay'), '25')
            self.assertEqual(d['settings'].get('truncation'), '0.02')

    def test_does_not_set_delay(self):
        # delay 는 라운드가 강제하므로 sweep 이 적으면 안 됨.
        out = settings_sweep.sweep_candidates(self.CODE, {}, n=3, seed=0)
        for d in out:
            self.assertNotIn('delay', d['settings'])

    def test_idx_starts_at_start_idx(self):
        out = settings_sweep.sweep_candidates(self.CODE, {}, n=3, seed=0, start_idx=101)
        self.assertEqual([d['idx'] for d in out], [101, 102, 103])

    def test_seed_rotates_grid(self):
        a = settings_sweep.sweep_candidates(self.CODE, {}, n=2, seed=0)
        b = settings_sweep.sweep_candidates(self.CODE, {}, n=2, seed=3)
        self.assertNotEqual(
            [(d['settings']['universe'], d['settings']['neutralization']) for d in a],
            [(d['settings']['universe'], d['settings']['neutralization']) for d in b])

    def test_deterministic(self):
        a = settings_sweep.sweep_candidates(self.CODE, {}, n=4, seed=2)
        b = settings_sweep.sweep_candidates(self.CODE, {}, n=4, seed=2)
        self.assertEqual(a, b)

    def test_empty_code_returns_empty(self):
        self.assertEqual(settings_sweep.sweep_candidates('', {}, n=3), [])
        self.assertEqual(settings_sweep.sweep_candidates(None, {}, n=3), [])

    def test_zero_n_returns_empty(self):
        self.assertEqual(settings_sweep.sweep_candidates(self.CODE, {}, n=0), [])

    def test_never_raises_on_garbage_settings(self):
        out = settings_sweep.sweep_candidates(self.CODE, {'universe': None}, n=2, seed=0)
        self.assertEqual(len(out), 2)


if __name__ == '__main__':
    unittest.main()
