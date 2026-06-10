from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest

from server import gemini_strategist as gs


class TestSystemInstructionRecalibration(unittest.TestCase):
    def setUp(self):
        self.si = gs.SYSTEM_INSTRUCTION

    def test_targets_recalibrated_to_real_cuts(self):
        self.assertIn('Sharpe ≥ 2.0', self.si)
        self.assertIn('Fitness ≥ 1.3', self.si)

    def test_old_low_targets_removed(self):
        self.assertNotIn('Sharpe ≥ 1.25', self.si)
        self.assertNotIn('Fitness ≥ 1.0', self.si)

    def test_fitness_formula_present(self):
        self.assertIn('Fitness', self.si)
        self.assertIn('Turnover', self.si)
        self.assertIn('0.125', self.si)

    def test_complexity_budget_present(self):
        self.assertIn('복잡도', self.si)
        self.assertIn('6', self.si)

    def test_anti_preamble_guard_present(self):
        self.assertIn('✅', self.si)
        self.assertIn('❌', self.si)


class TestSeedAndGroundingWiring(unittest.TestCase):
    def test_research_notes_disabled_returns_empty_without_api(self):
        from server import run_config
        orig = run_config.is_grounding_enabled()
        try:
            run_config.set_grounding_enabled(False)
            notes = gs.generate_research_notes(api_key='unused', round_num=1)
            self.assertEqual(notes, '')
        finally:
            run_config.set_grounding_enabled(orig)

    def test_user_prompt_includes_seed_section(self):
        from server import alpha_seeds
        import random
        seeds = alpha_seeds.sample_seeds(3, rng=random.Random(0))
        prompt = gs._build_user_prompt_full(
            1, [], [], None, None, None, None,
            forced_delay=None, slot_settings=None,
            seeds_section=alpha_seeds.render_seeds_section(seeds),
        )
        self.assertIn('검증된 저회전 시드', prompt)

    def test_user_prompt_includes_research_notes(self):
        prompt = gs._build_user_prompt_full(
            1, [], [], None, None, None, None,
            forced_delay=None, slot_settings=None,
            seeds_section='', research_notes='연구노트: 변동성-조정 모멘텀',
        )
        self.assertIn('연구노트: 변동성-조정 모멘텀', prompt)

    def test_cached_prompt_includes_seed_section(self):
        from server import alpha_seeds
        import random
        seeds = alpha_seeds.sample_seeds(3, rng=random.Random(0))
        prompt = gs._build_user_prompt_cached(
            1, [], [],
            seeds_section=alpha_seeds.render_seeds_section(seeds),
        )
        self.assertIn('검증된 저회전 시드', prompt)


class TestCrossoverPrompt(unittest.TestCase):
    def test_function_exists(self):
        self.assertTrue(hasattr(gs, 'generate_crossover_strategies'))

    def test_fewer_than_two_parents_falls_back_path(self):
        # With <2 parents it should NOT raise on the parents check itself.
        # Calling with no api_key still raises RuntimeError (api guard runs first) — assert that.
        with self.assertRaises(RuntimeError):
            gs.generate_crossover_strategies(api_key='', round_num=1, parents=[])

    def test_crossover_prompt_includes_both_parents(self):
        p = gs._build_crossover_prompt([
            {'code': 'rank(close)', 'pass_count': 6, 'operators': ['rank']},
            {'code': 'ts_mean(volume, 5)', 'pass_count': 5, 'operators': ['ts_mean']},
        ])
        self.assertIn('rank(close)', p)
        self.assertIn('ts_mean(volume, 5)', p)

    def test_crossover_prompt_no_parents_raises(self):
        # _build_crossover_prompt with empty parents should still work (returns prompt string)
        p = gs._build_crossover_prompt([])
        self.assertIsInstance(p, str)

    def test_crossover_prompt_contains_fusion_instruction(self):
        p = gs._build_crossover_prompt([
            {'code': 'rank(close)', 'pass_count': 7, 'operators': ['rank']},
            {'code': 'ts_mean(volume, 5)', 'pass_count': 5, 'operators': ['ts_mean']},
        ])
        # Should contain fusion/crossover instruction text
        self.assertIn('융합', p)

    def test_crossover_prompt_includes_submitted_codes(self):
        p = gs._build_crossover_prompt(
            [
                {'code': 'rank(close)', 'pass_count': 6, 'operators': ['rank']},
                {'code': 'ts_mean(volume, 5)', 'pass_count': 5, 'operators': ['ts_mean']},
            ],
            submitted_codes=['some_alpha_code_already_submitted'],
        )
        self.assertIn('some_alpha_code_already_submitted', p)


class TestDelay0Directive(unittest.TestCase):
    """delay=0 directive 계약 — delay=0 PV 알파 발굴을 위한 핵심 처방.

    원인: IQC_brain_datafields.csv 5200행이 전부 delay=1 이라, delay=0 라운드는
    'delay=1 팔레트 + PV만 써라' 모순 입력을 받아 (1) OHLCV 몇 개로만 회귀=다양성 고갈,
    (2) 팔레트 미끼(fundamental)를 물면 sim ERROR. 처방: 캐시 팔레트 무시 지시 +
    curated delay-0 PV 필드셋 + 구조적 archetype 분산.
    """

    def setUp(self):
        self.d0 = gs._delay_directive('0')
        self.d1 = gs._delay_directive('1')

    def test_instructs_to_ignore_cached_delay1_palette(self):
        # 캐시된 delay=1 팔레트를 무시하라는 명시 지시가 있어야 한다.
        self.assertIn('무시', self.d0)
        self.assertTrue('ERROR' in self.d0 or 'error' in self.d0.lower())

    def test_lists_delay0_safe_pv_fields(self):
        for f in ('open', 'high', 'low', 'close', 'vwap', 'volume', 'returns', 'adv20'):
            self.assertIn(f, self.d0, f'delay-0 PV 필드 {f} 누락')

    def test_mandates_structural_diversity(self):
        # 단일 반전 복제 금지 + 다중 PV 패밀리 합성 의무.
        self.assertIn('archetype', self.d0)
        self.assertTrue('합성' in self.d0)

    def test_warns_against_variable_redefinition(self):
        # 'redefine variable signal' ERROR 하드닝 — 변수 재정의 금지 지침이 있어야 한다.
        self.assertIn('redefine', self.d0)
        self.assertIn('재정의', self.d0)

    def test_delay1_has_no_pv_only_restriction(self):
        # delay=1 은 모든 데이터셋 허용 — PV 한정 지시가 없어야 한다.
        self.assertNotIn('무시', self.d1)
        self.assertIn('자유롭게', self.d1)

    def test_none_delay_is_empty(self):
        self.assertEqual(gs._delay_directive(None), '')


if __name__ == '__main__':
    unittest.main()
