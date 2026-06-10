"""P4 Task 2: recent_round_scores + survivor_alphas read helpers."""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import db


class TestP4Queries(unittest.TestCase):
    def test_recent_round_scores_callable_and_safe(self):
        r = db.recent_round_scores(999999, n=6)  # nonexistent user
        self.assertIsInstance(r, list)
        self.assertEqual(r, [])

    def test_survivor_alphas_callable_and_safe(self):
        r = db.survivor_alphas(999999, n=6, min_pass=5)
        self.assertIsInstance(r, list)
        self.assertEqual(r, [])

    def test_recent_round_scores_returns_floats(self):
        r = db.recent_round_scores(2, n=6)  # real user (may be empty if DB unseeded)
        self.assertIsInstance(r, list)
        for x in r:
            self.assertIsInstance(x, float)

    def test_survivor_alpha_shape(self):
        r = db.survivor_alphas(2, n=3, min_pass=3)
        for d in r:
            self.assertIn('code', d); self.assertIn('pass_count', d); self.assertIn('operators', d)
            self.assertIsInstance(d['operators'], list)


if __name__ == '__main__':
    unittest.main()
