"""로그 한 줄이 PASS/FAIL/WARNING 조건을 **전부** 싣는지 (2026-07-30 사장 지적).

라이브에서 잘려 나오던 것 4가지를 고정한다:
  1) 컷오프 — REST 경로엔 direction 이 없어 'LOW_SHARPE(-0.87)' 로 조건이 사라졌다
  2) WARNING — 파서는 채우는데 로그가 안 읽어 통째로 사라졌다
  3) 차단 FAIL 사유 — 앞 3개만 적어 'N FAIL' 과 개수가 어긋났다
  4) desc 어법 — directed_mutation 이 못 읽는 문장이라 정향변이가 generic 으로 떨어졌다
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import directed_mutation
from server import worker as W
from server.wqb_api import _check_desc

# REST harvest_alpha 가 만드는 실제 항목 모양 — direction 없음, value/cutoff 문자열.
def item(name, value, cutoff, result):
    return {'name': name, 'value': str(value), 'cutoff': str(cutoff), 'result': result,
            'desc': _check_desc(name, value, cutoff, result)}


class TestShortMetricLabel(unittest.TestCase):
    def test_cutoff_shown_without_direction(self):
        self.assertEqual(W._short_metric_label(item('LOW_SHARPE', -0.87, 1.25, 'FAIL')),
                         'LOW_SHARPE(-0.87<1.25)')
        self.assertEqual(W._short_metric_label(item('LOW_SHARPE', 2.4, 1.25, 'PASS')),
                         'LOW_SHARPE(2.4≥1.25)')

    def test_direction_wins_when_present(self):
        e = item('HIGH_TURNOVER', 0.72, 0.7, 'FAIL')
        e['direction'] = 'above'
        self.assertEqual(W._short_metric_label(e), 'HIGH_TURNOVER(0.72>0.7)')

    def test_non_numeric_and_missing_fields(self):
        self.assertEqual(W._short_metric_label({'name': 'CONCENTRATED_WEIGHT'}),
                         'CONCENTRATED_WEIGHT')
        self.assertEqual(W._short_metric_label({'name': 'X', 'value': 'n/a', 'cutoff': '?'}),
                         'X(n/a / 컷 ?)')
        # float 로 오는 경로도 죽지 않는다 (.strip() 크래시 방지)
        self.assertEqual(W._short_metric_label({'name': 'S', 'value': 1.5, 'cutoff': 1.25}),
                         'S(1.5≥1.25)')


class TestFormatAlphaResult(unittest.TestCase):
    def _line(self):
        return W._format_alpha_result(7, 'fail', {}, {
            'pass': [item('LOW_TURNOVER', 0.19, 0.01, 'PASS')],
            'fail': [item('LOW_SHARPE', -0.87, 1.25, 'FAIL'),
                     item('LOW_FITNESS', -0.13, 1.0, 'FAIL')],
            'warning': [item('LOW_2Y_SHARPE', 0.88, 1.25, 'WARNING')],
            'error': [{'name': 'IS_LADDER_SHARPE'}],
        })

    def test_counts_include_warning(self):
        self.assertIn('(1 PASS / 2 FAIL / 1 WARN / 1 ERR)', self._line())

    def test_warning_items_are_shown_with_cutoff(self):
        line = self._line()
        self.assertIn('⚠ WARN: LOW_2Y_SHARPE(0.88<1.25)', line)
        self.assertIn('⚠ ERR: IS_LADDER_SHARPE', line)

    def test_every_condition_and_cutoff_present(self):
        line = self._line()
        for name, cut in (('LOW_TURNOVER', '0.01'), ('LOW_SHARPE', '1.25'),
                          ('LOW_FITNESS', '1.0'), ('LOW_2Y_SHARPE', '1.25')):
            self.assertIn(name, line)
            self.assertIn(cut, line)


class TestSkipReasonNotTruncated(unittest.TestCase):
    NAMES = ['LOW_SHARPE', 'LOW_FITNESS', 'LOW_GLB_AMER_SHARPE', 'LOW_GLB_EMEA_SHARPE',
             'LOW_GLB_APAC_SHARPE', 'LOW_SUB_UNIVERSE_SHARPE', 'IS_LADDER_SHARPE']

    def test_all_blocking_names_survive_to_the_log(self):
        ko = W._skip_reason_ko('submit_skipped:blocking_fail(' + ','.join(self.NAMES) + ')')
        for n in self.NAMES:
            self.assertIn(n, ko, f'{n} 이 사유에서 사라졌다')

    def test_unknown_reason_kept_whole(self):
        long_reason = 'weird_reason(' + 'x' * 200 + ')'
        self.assertEqual(W._skip_reason_ko('submit_skipped:' + long_reason), long_reason)


class TestEllip(unittest.TestCase):
    def test_marks_the_cut(self):
        self.assertEqual(W._ellip('abcdef', 10), 'abcdef')
        self.assertEqual(W._ellip('abcdef', 3), 'abc…')
        self.assertEqual(W._ellip(None, 3), '')


class TestCheckDescDrivesDirectedMutation(unittest.TestCase):
    """desc 어법이 정향변이 파서와 맞물려야 실패 축이 지시로 바뀐다."""

    def test_rx_matches_rest_desc(self):
        d = _check_desc('LOW_SHARPE', -0.87, 1.25, 'FAIL')
        self.assertRegex(d, r'of -0\.87 is below cutoff of 1\.25')
        r = directed_mutation.route([d])
        self.assertNotEqual(r['strategy'], 'generic')
        self.assertIn('sign_flip', r['strategy'])      # 음수 Sharpe → 부호 뒤집기

    def test_turnover_above_routes_to_cut(self):
        r = directed_mutation.route([_check_desc('HIGH_TURNOVER', 0.95, 0.7, 'FAIL')])
        self.assertIn('cut_turnover', r['strategy'])

    def test_non_numeric_falls_back_to_old_shape(self):
        self.assertEqual(_check_desc('CONCENTRATED_WEIGHT', None, None, 'PASS'),
                         'CONCENTRATED_WEIGHT: PASS (value=None, limit=None)')


if __name__ == '__main__':
    unittest.main()
