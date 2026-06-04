"""Tests for server/focus_priority.py — closeness-to-pass scoring.

Run with: python3.11 -m pytest tests/test_focus_priority.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from server import focus_priority
from server.focus_priority import closeness_score


class TestClosenessScore(unittest.TestCase):

    # ------------------------------------------------------------------
    # Basic parsing
    # ------------------------------------------------------------------

    def test_parses_below_cutoff(self):
        """'Fitness of 0.91 is below cutoff of 1.' should parse correctly."""
        score = closeness_score(['Fitness of 0.91 is below cutoff of 1.'])
        # gap = abs(1 - 0.91) / 1.0 = 0.09 => score = -0.09
        self.assertAlmostEqual(score, -0.09, places=5)

    def test_parses_sharpe_below_cutoff(self):
        """'Sharpe of 1.10 is below cutoff of 1.25.' should parse correctly."""
        score = closeness_score(['Sharpe of 1.10 is below cutoff of 1.25.'])
        expected_gap = abs(1.25 - 1.10) / 1.25  # 0.12
        self.assertAlmostEqual(score, -expected_gap, places=5)

    def test_parses_above_cutoff(self):
        """'Turnover of 0.80 is above cutoff of 0.70.' should parse correctly."""
        score = closeness_score(['Turnover of 0.80 is above cutoff of 0.70.'])
        expected_gap = abs(0.70 - 0.80) / 0.70
        self.assertAlmostEqual(score, -expected_gap, places=5)

    def test_near_miss_scores_higher_than_far_miss(self):
        """Near-miss (Sharpe 1.20 vs 1.25) should score HIGHER than far-miss (Fitness 0.3 vs 1.0)."""
        near_miss = closeness_score(['Sharpe of 1.20 is below cutoff of 1.25.'])
        far_miss = closeness_score(['Fitness of 0.3 is below cutoff of 1.0.'])
        self.assertGreater(near_miss, far_miss,
                           f'Near-miss {near_miss} should > far-miss {far_miss}')

    def test_near_miss_negative_but_close_to_zero(self):
        """Near-miss should produce a score closer to 0 than -0.5."""
        near_miss = closeness_score(['Sharpe of 1.20 is below cutoff of 1.25.'])
        self.assertGreater(near_miss, -0.5)
        self.assertLess(near_miss, 0.0)

    def test_far_miss_more_negative(self):
        far_miss = closeness_score(['Fitness of 0.3 is below cutoff of 1.0.'])
        self.assertLess(far_miss, -0.5)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_list(self):
        """Empty fail_items => neutral (large negative sentinel, sorts last)."""
        score = closeness_score([])
        # Neutral sentinel is negative to rank below any measurable gap entry.
        self.assertLess(score, 0.0)

    def test_none_input(self):
        """None => neutral (large negative sentinel, sorts last)."""
        score = closeness_score(None)
        self.assertLess(score, 0.0)

    def test_unparseable_string(self):
        """Strings that don't match the pattern => neutral (large negative sentinel)."""
        score = closeness_score(['This is a random failure message'])
        self.assertLess(score, 0.0)

    def test_mixed_parseable_and_unparseable(self):
        """Only parseable items contribute; others ignored."""
        items = [
            'Some random text',
            'Fitness of 0.91 is below cutoff of 1.',
        ]
        score = closeness_score(items)
        # Should equal just the Fitness item's contribution
        expected = closeness_score(['Fitness of 0.91 is below cutoff of 1.'])
        self.assertAlmostEqual(score, expected, places=5)

    def test_multiple_fail_items_sum_gaps(self):
        """Multiple fail items: gaps accumulate (score more negative)."""
        single = closeness_score(['Sharpe of 1.20 is below cutoff of 1.25.'])
        double = closeness_score([
            'Sharpe of 1.20 is below cutoff of 1.25.',
            'Fitness of 0.91 is below cutoff of 1.',
        ])
        self.assertLess(double, single,
                        'Two fail items should give lower (more negative) score than one')

    def test_integer_only_input(self):
        """Non-string items are coerced to str; purely numeric items => neutral (no pattern match)."""
        score = closeness_score([42, 99])
        self.assertLess(score, 0.0)

    def test_case_insensitive(self):
        """Parsing should be case-insensitive."""
        upper = closeness_score(['FITNESS OF 0.91 IS BELOW CUTOFF OF 1.'])
        lower = closeness_score(['fitness of 0.91 is below cutoff of 1.'])
        self.assertAlmostEqual(upper, lower, places=5)

    def test_return_type_is_float(self):
        score = closeness_score(['Sharpe of 1.10 is below cutoff of 1.25.'])
        self.assertIsInstance(score, float)

    def test_neutral_sorts_last(self):
        """Neutral entries (no parseable items) score lower than any measurable near-miss."""
        neutral = closeness_score([])
        near_miss = closeness_score(['Sharpe of 1.24 is below cutoff of 1.25.'])
        far_miss = closeness_score(['Fitness of 0.01 is below cutoff of 1.0.'])
        # Neutral must be below even the farthest miss
        self.assertLess(neutral, far_miss)
        self.assertLess(neutral, near_miss)


class TestFocusQueueSorting(unittest.TestCase):
    """Integration test: sorting a small focus queue by closeness puts near-miss first."""

    def _make_entry(self, fail_items: list[str]) -> dict:
        return {
            'parent_round_num': 1,
            'phase': 1,
            'parent_idx': 0,
            'parent_code': 'rank(close)',
            'parent_fail_items': fail_items,
            'focus_kind': 'fail',
        }

    def test_near_miss_first_in_sorted_queue(self):
        near_miss = self._make_entry(['Sharpe of 1.20 is below cutoff of 1.25.'])
        far_miss = self._make_entry(['Fitness of 0.30 is below cutoff of 1.0.'])
        neutral = self._make_entry([])

        queue = [far_miss, neutral, near_miss]
        sorted_q = sorted(
            queue,
            key=lambda e: closeness_score(e.get('parent_fail_items') or []),
            reverse=True,
        )
        self.assertIs(sorted_q[0], near_miss,
                      'Near-miss entry should be first after sorting')

    def test_far_miss_after_near_miss(self):
        near_miss = self._make_entry(['Sharpe of 1.22 is below cutoff of 1.25.'])
        far_miss = self._make_entry(['Fitness of 0.10 is below cutoff of 1.0.'])

        queue = [far_miss, near_miss]
        sorted_q = sorted(
            queue,
            key=lambda e: closeness_score(e.get('parent_fail_items') or []),
            reverse=True,
        )
        self.assertIs(sorted_q[0], near_miss)
        self.assertIs(sorted_q[1], far_miss)

    def test_neutral_entry_does_not_crash(self):
        """Entry with None/missing parent_fail_items sorts without crashing."""
        entries = [
            {'parent_fail_items': None},
            {'parent_fail_items': ['Sharpe of 1.20 is below cutoff of 1.25.']},
            {},
        ]
        # Should not raise
        sorted_q = sorted(
            entries,
            key=lambda e: closeness_score(e.get('parent_fail_items') or []),
            reverse=True,
        )
        self.assertEqual(len(sorted_q), 3)

    def test_all_same_score_stable(self):
        """Entries with identical scores preserve relative order (Python sort is stable)."""
        e1 = self._make_entry([])
        e2 = self._make_entry([])
        sorted_q = sorted(
            [e1, e2],
            key=lambda e: closeness_score(e.get('parent_fail_items') or []),
            reverse=True,
        )
        self.assertIs(sorted_q[0], e1)
        self.assertIs(sorted_q[1], e2)


class TestRealWQBFailFormat(unittest.TestCase):
    """Contract test: locks the exact desc format emitted by _wqb_pw_worker IS-test scrape.

    If this breaks, the closeness parser is silently returning NEUTRAL_SCORE for
    real fail strings, causing focus prioritisation to degrade without any error.
    """

    def test_closeness_parses_real_wqb_fail_format(self):
        # 스크레이퍼(_wqb_pw_worker IS-test) 가 내는 실제 desc 포맷 계약 — 이게 깨지면
        # focus 우선순위가 조용히 neutral 로 떨어지므로 회귀 가드.
        near = focus_priority.closeness_score(['Sharpe of 1.20 is below cutoff of 1.25.'])
        far  = focus_priority.closeness_score(['Fitness of 0.30 is below cutoff of 1.'])
        assert near > far                      # 근접 미스가 더 높은 우선순위
        assert near > focus_priority.NEUTRAL_SCORE
        # 퍼센트 포함 라인도 파싱돼야 함 (값/컷오프에 % 가 붙는 경우)
        pct = focus_priority.closeness_score(['Turnover of 27.24% is above cutoff of 1%.'])
        assert pct > focus_priority.NEUTRAL_SCORE   # 파싱 성공(neutral 아님)
        # 포맷이 전혀 안 맞으면 neutral
        assert focus_priority.closeness_score(['some unparseable label']) == focus_priority.NEUTRAL_SCORE
