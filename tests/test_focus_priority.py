"""Tests for server/focus_priority.py — closeness-to-pass scoring.

Run with: python3.11 -m pytest tests/test_focus_priority.py -v
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

from server import focus_priority
from server.focus_priority import closeness_score, advance_focus_queue, NEUTRAL_SCORE


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


    def test_closeness_score_dict_items(self):
        near = closeness_score([{'name': 'Sharpe', 'value': 1.20, 'cutoff': 1.25}])
        far = closeness_score([{'name': 'Fitness', 'value': 0.30, 'cutoff': 1.0}])
        assert near > far                      # near-miss closer to pass (less negative)
        assert near != NEUTRAL_SCORE and far != NEUTRAL_SCORE

    def test_closeness_score_dict_missing_values_is_neutral(self):
        assert closeness_score([{'name': 'X', 'value': None, 'cutoff': None}]) == NEUTRAL_SCORE


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


class TestAdvanceFocusQueue(unittest.TestCase):
    """Regression guard for the round-560 infinite loop.

    The focus queue is SELECTED by closeness_score (sorted), so the processed
    entry is not necessarily queue[0] (FIFO front). The pop must remove the entry
    that was actually processed, matched by (round_num, phase, parent_idx),
    wherever it sits — otherwise the highest-closeness entry is re-selected
    forever and the round never advances.
    """

    def _q560(self) -> list[dict]:
        # Real FIFO order from the live bug: idx1 phases first (Sharpe 1.54),
        # then idx8 phases (Sharpe 1.55). Closeness ranks idx8 ABOVE idx1
        # (1.55 is nearer cutoff 2 than 1.54), so selection picks idx8 even
        # though idx1 is the FIFO front — exactly the divergence that stuck 560.
        def mk(ph, idx, sharpe):
            return {
                'parent_round_num': 560, 'phase': ph, 'parent_idx': idx,
                'parent_fail_items': [f'Sharpe of {sharpe} is below cutoff of 2.'],
            }
        return [mk(1, 1, 1.54), mk(2, 1, 1.54), mk(3, 1, 1.54),
                mk(1, 8, 1.55), mk(2, 8, 1.55), mk(3, 8, 1.55)]

    def test_selection_picks_idx8_not_fifo_front(self):
        """Sanity: closeness selection diverges from FIFO front (the bug precondition)."""
        q = self._q560()
        sorted_q = sorted(q, key=lambda e: closeness_score(e['parent_fail_items']),
                          reverse=True)
        self.assertEqual((sorted_q[0]['phase'], sorted_q[0]['parent_idx']), (1, 8))
        self.assertEqual((q[0]['phase'], q[0]['parent_idx']), (1, 1))  # FIFO front differs

    def test_removes_processed_entry_behind_fifo_front(self):
        """done on the selected idx8 entry must remove IT, not require FIFO front match."""
        q = self._q560()
        new_q, action = advance_focus_queue(q, 560, 1, 8, 'done')
        self.assertEqual(action, 'removed')
        self.assertEqual(len(new_q), 5)
        self.assertFalse(any(e['parent_idx'] == 8 and e['phase'] == 1 for e in new_q))

    def test_drains_to_empty_so_round_can_advance(self):
        """Each selected entry is consumed on done → queue empties → round advances."""
        q = self._q560()
        for ph, idx in [(1, 8), (2, 8), (3, 8), (1, 1), (2, 1), (3, 1)]:
            q, action = advance_focus_queue(q, 560, ph, idx, 'done')
            self.assertEqual(action, 'removed')
        self.assertEqual(q, [])

    def test_error_retries_then_gives_up_at_max_attempts(self):
        """A persistently-erroring entry retries up to max_attempts, then is dropped."""
        q = self._q560()
        for _ in range(4):
            q, action = advance_focus_queue(q, 560, 1, 8, 'error', max_attempts=5)
            self.assertEqual(action, 'retry')
        q, action = advance_focus_queue(q, 560, 1, 8, 'error', max_attempts=5)
        self.assertEqual(action, 'giveup')
        self.assertFalse(any(e['parent_idx'] == 8 and e['phase'] == 1 for e in q))

    def test_done_removes_even_when_fifo_front(self):
        q = self._q560()
        new_q, action = advance_focus_queue(q, 560, 1, 1, 'done')
        self.assertEqual(action, 'removed')
        self.assertEqual(len(new_q), 5)

    def test_nomatch_leaves_queue_untouched(self):
        q = self._q560()
        new_q, action = advance_focus_queue(q, 999, 1, 1, 'done')
        self.assertEqual(action, 'nomatch')
        self.assertEqual(len(new_q), 6)

    def test_pure_does_not_mutate_input(self):
        q = self._q560()
        snapshot = [dict(e) for e in q]
        advance_focus_queue(q, 560, 1, 8, 'done')
        self.assertEqual(q, snapshot)

    def test_empty_queue_is_safe(self):
        new_q, action = advance_focus_queue([], 560, 1, 8, 'done')
        self.assertEqual(action, 'nomatch')
        self.assertEqual(new_q, [])


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


class TestFocusClosenessFloor(unittest.TestCase):
    """Design contract for worker.FOCUS_CLOSENESS_FLOOR (기본 -0.8).

    worker 가 focus 큐 진입을 closeness_score >= FLOOR 로 게이팅한다. delay=0 은
    Sharpe 통과가 본래 어려워 hopeless 부모(예: Sharpe 0.07/Fitness 0.01)가 많은데,
    그런 부모를 directed-mutation 으로 5배 끌어올리는 건 불가능하므로 차단하고 예산을
    탐색으로 돌려야 한다. 이 테스트가 깨지면 worker 기본값과 동기화가 어긋난 것."""

    FLOOR = -0.8  # worker.FOCUS_CLOSENESS_FLOOR 기본값과 일치해야 함

    def test_hopeless_delay0_parent_is_below_floor(self):
        # delay=0 흔한 실패 형태 — 두 하드체크가 동시에 멀리 떨어짐.
        hopeless = closeness_score([
            'Sharpe of 0.07 is below cutoff of 2.',
            'Fitness of 0.01 is below cutoff of 1.3.',
        ])
        self.assertLess(hopeless, self.FLOOR,
                        f'hopeless parent {hopeless} should be cut by floor {self.FLOOR}')

    def test_near_miss_parent_is_above_floor(self):
        # 통과 임박 — 두 하드체크 모두 컷오프 근처.
        near = closeness_score([
            'Sharpe of 1.7 is below cutoff of 2.',
            'Fitness of 1.1 is below cutoff of 1.3.',
        ])
        self.assertGreaterEqual(near, self.FLOOR,
                                f'near-miss parent {near} should pass floor {self.FLOOR}')

    def test_single_far_metric_is_cut(self):
        # Sharpe 만 멀어도(0.4 vs 2.0) gap≈0.8 이라 경계선 — 0.3 처럼 더 멀면 확실히 차단.
        far = closeness_score(['Sharpe of 0.3 is below cutoff of 2.'])
        self.assertLess(far, self.FLOOR)


def test_submit_rejection_reasons_map_to_improvement_axes():
    """제출 거절도 '무엇을 고칠지' 를 말해 준다 — 그 사유가 변이 축으로 이어져야 한다.

    2026-07-29: 고회전 알파가 LOW_GLB_EMEA_SHARPE 등으로 거절되면 IS 체크는 FAIL 0 이라
    focus 후보에서 탈락했다(라운드마다 제일 좋은 후보를 버림). 이제 거절 사유를 실어
    보내며, 아래는 그 사유가 실제로 축을 고르게 하는지 고정한다.
    """
    from server.mutation_learn import categorize

    rej = 'LOW_SHARPE(1.5 vs 1.58); LOW_FITNESS(0.46 vs 1.0); LOW_GLB_EMEA_SHARPE(0.69 vs 1)'
    cats = categorize([x.strip() for x in rej.split(';')])
    assert 'signal' in cats and 'fitness' in cats, cats

    rej2 = 'LOW_SUB_UNIVERSE_SHARPE(0.4 vs 1); PROD_CORRELATION(0.8)'
    cats2 = categorize([x.strip() for x in rej2.split(';')])
    assert cats2 == ['sub_universe', 'correlation'], cats2
