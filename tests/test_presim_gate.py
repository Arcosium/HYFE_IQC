from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import presim_gate

class TestScreen(unittest.TestCase):
    def test_keeps_novel_candidate(self):
        kept, dropped = presim_gate.screen(
            [{'idx': 1, 'code': 'rank(close) - ts_mean(returns, 5)'}],
            existing_codes=['ts_corr(rank(high), rank(volume), 10)'])
        self.assertEqual(len(kept), 1); self.assertEqual(dropped, [])
    def test_drops_near_duplicate(self):
        existing = ['ts_corr(rank(close), rank(volume), 10)']
        kept, dropped = presim_gate.screen(
            [{'idx': 1, 'code': 'ts_corr(rank(close), rank(volume), 20)'}],
            existing_codes=existing)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn('overlap', dropped[0]['reason'].lower())
    def test_drops_overcomplex(self):
        big = 'rank(' * 20 + 'close' + ')' * 20
        kept, dropped = presim_gate.screen([{'idx': 1, 'code': big}], existing_codes=[],
                                           opts={'max_symbol_length': 30})
        self.assertEqual(kept, []); self.assertEqual(len(dropped), 1)
    def test_empty_existing_keeps_all(self):
        cands = [{'idx': i, 'code': 'rank(close)'} for i in range(3)]
        kept, dropped = presim_gate.screen(cands, existing_codes=[])
        self.assertEqual(len(kept), 3)
    def test_never_raises_on_garbage(self):
        kept, dropped = presim_gate.screen([{'idx': 1, 'code': '((('}], existing_codes=['close'])
        self.assertEqual(len(kept) + len(dropped), 1)


class TestOverlapDropDisabled(unittest.TestCase):
    """focus 라운드는 overlap_drop=0 으로 구조적 탈상관을 꺼야 한다 — focus 는 부모를
    의도적으로 변형하므로 near-dup 이 정상이고, 끄지 않으면 생성의 50-80% 가 버려진다."""

    EXISTING = ['ts_corr(rank(close), rank(volume), 10)']
    NEAR_DUP = {'idx': 1, 'code': 'ts_corr(rank(close), rank(volume), 20)'}

    def test_overlap_zero_disables_structural_drop(self):
        kept, dropped = presim_gate.screen(
            [self.NEAR_DUP], existing_codes=self.EXISTING,
            opts={'overlap_drop': 0})
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_overlap_none_disables_structural_drop(self):
        kept, dropped = presim_gate.screen(
            [self.NEAR_DUP], existing_codes=self.EXISTING,
            opts={'overlap_drop': None})
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_complexity_cap_still_applies_when_overlap_off(self):
        # overlap 을 꺼도 복잡도 백스톱은 살아 있어야 한다(pathological 알파 차단).
        big = 'rank(' * 20 + 'close' + ')' * 20
        kept, dropped = presim_gate.screen(
            [{'idx': 1, 'code': big}], existing_codes=self.EXISTING,
            opts={'overlap_drop': 0, 'max_symbol_length': 30})
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_default_still_drops_near_duplicate(self):
        # opts 미지정(탐색 라운드)이면 기존 동작 그대로 — 회귀 가드.
        kept, dropped = presim_gate.screen(
            [self.NEAR_DUP], existing_codes=self.EXISTING)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)


if __name__ == '__main__':
    unittest.main()
