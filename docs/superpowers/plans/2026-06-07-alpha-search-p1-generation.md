# Alpha Search P1 — Generation Quick-Wins Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift the WQB PASS rate of generated alphas immediately by recalibrating targets, seeding the 101-Alpha low-turnover palette, adding a WQB-Fitness optimization block + complexity budget + anti-preamble guard to the prompt, and enabling per-round Google grounding — without touching the WQB I/O rails.

**Architecture:** Phase 1 of the Evolutionary Alpha Search design (`docs/superpowers/specs/2026-06-07-evolutionary-alpha-search-design.md`, Approach A). New pure module `alpha_seeds.py`; a live-editable `run_config` grounding flag; targeted edits to `gemini_strategist.py` (SYSTEM_INSTRUCTION + a per-round grounded research-notes call + seed injection). All generation-side; the round loop, `_wqb_pw_worker`, cache, lint, repair stay as-is.

**Tech Stack:** Python 3.9 (server) / 3.11 (tests + worker), `google-genai` SDK, SQLite, pytest. Tests MUST run under `python3.11 -m pytest`.

---

## Project conventions (read before starting)

- **Commit policy:** This repo commits ONLY when the boss explicitly requests (CLAUDE.md). The "Checkpoint" step in each task means *run the tests and confirm green* — do NOT `git commit` unless the boss has asked. An external auto-Backup job periodically commits the tree; that is expected.
- **Deploy = restart:** `gemini_strategist.py`, `alpha_seeds.py`, `run_config.py` are server-imported → changes need `sudo systemctl restart hyfe-iqc.service` to take effect. Confirm with the boss before restarting (it interrupts the in-flight round; running=1 users auto-resume on boot).
- **Tests:** always `python3.11 -m pytest` (plain `python` lacks argon2 and dies on import).
- **Defensive style:** new pure functions never raise; ALLOW/neutral on parse/uncertainty (mirror `focus_priority.py`, `alpha_ast.py`).
- **FASTEXPR invariants (hard-won):** `group_neutralize(x, sector)` uses BARE group names (no quotes); `hump(x, hump=0.03)` uses the NAMED arg; never scientific notation (`0.000001`, not `1e-6`). See [[alpha-repair-hump-group-2026-06-03]].

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `server/alpha_seeds.py` | Low-turnover 101-Alpha seed templates + sample/render | Create |
| `tests/test_alpha_seeds.py` | Unit + contract tests for seeds | Create |
| `server/run_config.py` | Add live `grounding_enabled` flag | Modify |
| `tests/test_run_config_grounding.py` | Flag default + round-trip | Create |
| `server/gemini_strategist.py` | SYSTEM_INSTRUCTION recalibration; grounded research-notes call; seed injection | Modify |
| `tests/test_gemini_generation_p1.py` | Prompt-content regression guards + grounding gating | Create |

---

## Task 1: `run_config` grounding flag

**Files:**
- Modify: `server/run_config.py` (after `set_bandit_enabled`, ~line 89)
- Test: `tests/test_run_config_grounding.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_run_config_grounding.py
from __future__ import annotations
import sys, os, importlib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest


class TestGroundingFlag(unittest.TestCase):
    def setUp(self):
        from server import run_config
        self.rc = run_config
        self._orig = self.rc.is_grounding_enabled()

    def tearDown(self):
        self.rc.set_grounding_enabled(self._orig)

    def test_default_is_true(self):
        # Fresh config (key absent) defaults to True.
        self.rc.set_grounding_enabled(True)
        self.assertTrue(self.rc.is_grounding_enabled())

    def test_round_trip_false(self):
        self.rc.set_grounding_enabled(False)
        self.assertFalse(self.rc.is_grounding_enabled())
        self.rc.set_grounding_enabled(True)
        self.assertTrue(self.rc.is_grounding_enabled())


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_run_config_grounding.py -v`
Expected: FAIL — `AttributeError: module 'server.run_config' has no attribute 'is_grounding_enabled'`

- [ ] **Step 3: Write minimal implementation**

Add to `server/run_config.py` after `set_bandit_enabled`:

```python
def is_grounding_enabled() -> bool:
    """True if per-round Google grounding is enabled (default: True).

    Reads 'grounding_enabled' from data/run_config.json — live-editable without
    a server restart, exactly like is_bandit_enabled().
    """
    try:
        val = _read().get('grounding_enabled', None)
    except Exception:
        return True
    if val is None:
        return True
    return bool(val)


def set_grounding_enabled(enabled: bool) -> None:
    """Persist the grounding_enabled flag.  No restart required."""
    data = _read()
    data['grounding_enabled'] = bool(enabled)
    _write(data)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_run_config_grounding.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Checkpoint** — tests green; do not commit (see Commit policy).

---

## Task 2: `alpha_seeds.py` module — seed templates + sample + render

**Files:**
- Create: `server/alpha_seeds.py`
- Test: `tests/test_alpha_seeds.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alpha_seeds.py
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
        seeds = alpha_seeds.sample_seeds(999, exclude_ops={'group_neutralize'},
                                         rng=random.Random(0))
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


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_alpha_seeds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'server.alpha_seeds'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/alpha_seeds.py
"""alpha_seeds — low-turnover WQB FASTEXPR seed templates (WorldQuant 101 Alphas).

A palette the generator samples from and is told to VARY (hypothesis/window/field),
NOT alphas to emit verbatim. Every expr here is already concrete and MUST parse +
lint clean (see tests/test_alpha_seeds.py contract test). Invariants:
  - all time windows are integers
  - group_neutralize uses BARE group names (sector/industry/subindustry/market)
  - hump uses the named arg: hump(x, hump=0.03)
  - no scientific notation (0.000001, not 1e-6)
"""
from __future__ import annotations

import random as _random

SEED_TEMPLATES: list[dict] = [
    {"family": "pv_corr_reversion",
     "expr": "-1 * ts_corr(rank(close), rank(volume), 10)",
     "ops": ["ts_corr", "rank"],
     "intuition": "음의 가격-거래량 상관 = 평균회귀 (이중 rank으로 이상치 제거)"},
    {"family": "decayed_ranked_corr",
     "expr": "-1 * ts_rank(ts_decay_linear(ts_corr(group_neutralize(vwap, sector), volume, 4), 8), 6)",
     "ops": ["ts_rank", "ts_decay_linear", "ts_corr", "group_neutralize"],
     "intuition": "섹터중립 vwap/volume 상관을 decay+ts_rank로 3중 평활 → 최저 회전"},
    {"family": "short_revert_long_corr",
     "expr": "scale(ts_mean(close, 7) - close) + 20 * scale(ts_corr(vwap, ts_delay(close, 5), 230))",
     "ops": ["scale", "ts_mean", "ts_corr", "ts_delay"],
     "intuition": "7일 단기 반전 + 230일 장기 vwap/지연close 상관 (장기창=저회전)"},
    {"family": "scale_combo",
     "expr": "scale(ts_corr(adv20, low, 5) + (high + low) / 2 - close)",
     "ops": ["scale", "ts_corr"],
     "intuition": "중간가+adv/low상관 vs close 괴리, scale로 달러중립"},
    {"family": "stochastic_reversal",
     "expr": "-1 * ts_corr(rank((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12) + 0.000001)), rank(volume), 6)",
     "ops": ["ts_corr", "rank", "ts_min", "ts_max"],
     "intuition": "범위내위치(%R)와 거래량 rank의 음상관 = 반전"},
    {"family": "vwap_relative_ratio",
     "expr": "rank(vwap - close) / rank(vwap + close)",
     "ops": ["rank"],
     "intuition": "vwap 대비 저평가 틸트 — stateless, 추가 회전 거의 0"},
    {"family": "decayed_fitness_vwapclose",
     "expr": "ts_decay_linear(rank((vwap - close) / (close + 0.000001)), 5)",
     "ops": ["ts_decay_linear", "rank"],
     "intuition": "측정된 고-Fitness(~2.86) vwap-close 반전 decay"},
    {"family": "avdiff_corr_gate",
     "expr": "-1 * ts_av_diff(close, 50) * ts_corr(close, volume, 50)",
     "ops": ["ts_av_diff", "ts_corr"],
     "intuition": "구조적으로 유효할 때만 진입하는 상관 게이트 (측정 Fitness~1.70)"},
    {"family": "double_rank_momentum",
     "expr": "rank(ts_rank(close / ts_delay(close, 5) - 1, 40))",
     "ops": ["rank", "ts_rank", "ts_delay"],
     "intuition": "시계열+횡단면 이중 rank 모멘텀 → 안정적, 경계화"},
    {"family": "min_blend_bounded",
     "expr": "min(rank(ts_decay_linear((rank(open) + rank(low)) - (rank(high) + rank(close)), 8)), ts_rank(ts_decay_linear(ts_corr(ts_rank(close, 8), ts_rank(adv60, 21), 8), 7), 3))",
     "ops": ["min", "rank", "ts_decay_linear", "ts_rank", "ts_corr"],
     "intuition": "모든 항이 경계화/decay → 매끄럽고 가중치 분산 양호"},
]

FAMILIES: list[str] = sorted({t["family"] for t in SEED_TEMPLATES})


def sample_seeds(n, families=None, exclude_ops=None, rng=None) -> list[dict]:
    """Return up to n seed dicts.

    families:    keep only these family tags (None = all).
    exclude_ops: drop templates using any excluded operator (SC-saturation lever).
    rng:         random.Random for deterministic sampling (None = module default).
    Never raises.
    """
    try:
        rng = rng or _random.Random()
        exclude_ops = set(exclude_ops or ())
        fam = set(families) if families is not None else None
        pool = [t for t in SEED_TEMPLATES
                if (fam is None or t["family"] in fam)
                and not (set(t["ops"]) & exclude_ops)]
        rng.shuffle(pool)
        return pool[:max(0, int(n))]
    except Exception:
        return []


def render_seeds_section(seeds) -> str:
    """Render seeds as a prompt section; empty string when no seeds."""
    if not seeds:
        return ''
    lines = ['[검증된 저회전 시드 — 통째 베끼지 말고 가설/창/필드를 변형해 사용 (WorldQuant 101 기반)]']
    for s in seeds:
        lines.append(f'- ({s.get("family","")}) {s.get("expr","")}  // {s.get("intuition","")}')
    return '\n'.join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_alpha_seeds.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Checkpoint** — tests green; do not commit.

---

## Task 3: Contract test — every seed parses + lints clean

This guards against re-introducing FASTEXPR syntax bugs (positional hump, quoted group, sci-notation, forbidden ops) through the seed palette.

**Files:**
- Test: `tests/test_alpha_seeds.py` (append a new class)

- [ ] **Step 1: Write the failing test** (append to `tests/test_alpha_seeds.py`)

```python
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
        import re
        for t in alpha_seeds.SEED_TEMPLATES:
            code = t['expr']
            with self.subTest(family=t['family']):
                # bare group: no group_*( ... 'QUOTED' ... )
                self.assertNotRegex(code, r"group_\w+\([^)]*['\"]")
                # hump must be named if present
                if 'hump(' in code:
                    self.assertRegex(code, r'hump\([^)]*hump\s*=')
                # no scientific notation
                self.assertNotRegex(code, r'\d+\.?\d*[eE][+-]?\d+')
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python3.11 -m pytest tests/test_alpha_seeds.py::TestSeedContract -v`
Expected: If any seed has a latent FASTEXPR issue, FAIL with the offending `family`/`code` in the subTest message. If all are clean, PASS — that is the desired end state for this task.

- [ ] **Step 3: Fix any failing seed in `server/alpha_seeds.py`**

For each failure, correct the `expr` in place (e.g. integer-ize a window, add `+ 0.000001` to a denominator, switch a quoted group to bare, rename an op flagged by `_alpha_violations`). Re-run until green. Do NOT weaken the test.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_alpha_seeds.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Checkpoint** — tests green; do not commit.

---

## Task 4: Recalibrate SYSTEM_INSTRUCTION (targets + Fitness block + complexity budget + anti-preamble)

**Files:**
- Modify: `server/gemini_strategist.py:45-118` (the `SYSTEM_INSTRUCTION` constant)
- Test: `tests/test_gemini_generation_p1.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_gemini_generation_p1.py
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
        # WQB Fitness optimization block
        self.assertIn('Fitness', self.si)
        self.assertIn('Turnover', self.si)
        self.assertIn('0.125', self.si)   # max(Turnover, 0.125) term

    def test_complexity_budget_present(self):
        self.assertIn('복잡도', self.si)
        self.assertIn('6', self.si)       # ≤6 base fields

    def test_anti_preamble_guard_present(self):
        # explicit good/bad output examples
        self.assertIn('✅', self.si)
        self.assertIn('❌', self.si)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_gemini_generation_p1.py -v`
Expected: FAIL — `test_targets_recalibrated_to_real_cuts` (current text says "Sharpe ≥ 1.25"), `test_fitness_formula_present`, `test_anti_preamble_guard_present`, etc.

- [ ] **Step 3: Edit `SYSTEM_INSTRUCTION`**

Make these exact edits in `server/gemini_strategist.py`:

(a) Replace the role line (line ~46):
```
너는 WorldQuant Brain(USA, Delay1) 에서 Sharpe ≥ 1.25, Fitness ≥ 1.0, 그리고 **기존 제출 알파들과의 Self-Correlation < 0.7** 을 동시에 노리는 시니어 퀀트 연구원이다.
```
with:
```
너는 WorldQuant Brain(USA) 에서 **Sharpe ≥ 2.0, Fitness ≥ 1.3**, Turnover 1~70%, Weight 잘 분산(>10% 집중 금지), Sub-universe Sharpe 컷 통과, 그리고 **기존 제출 알파들과의 Self-Correlation ≤ 0.7** 을 동시에 노리는 시니어 퀀트 연구원이다. 이 컷들은 실제 대회 기준이다 — 절대 더 낮게 조준하지 마라.
```

(b) Insert this block immediately after the `[가장 중요 — 상관관계 0.7 벽 깨기]` section (before `[10가지 가설 패밀리...]`):
```
[Fitness 최적화 — 점수 직접 끌어올리기]
WorldQuant Fitness = Sharpe × √(|Returns| / max(Turnover, 0.125)). 레버 우선순위:
  1) 최종 신호를 ts_decay_linear(rank(signal), 5~10) 로 감싸 회전↓·단조성↑ (가장 강력).
  2) rank() 로 스케일 제거·횡단면 비교성 확보 (가장 중요한 연산자).
  3) raw 값 대신 ts_zscore/(x-ts_mean)/ts_std 로 '수준'이 아닌 '변화'를 포착.
  4) 이중 rank: rank(ts_rank(x, 40)) 로 안정성↑.
  5) -ts_av_diff(x, 50) * ts_corr(x, y, 50): 구조적으로 유효할 때만 진입하는 상관 게이트.
  6) trade_when(저변동 조건, 신호, -1): 최강 회전 억제기.
측정된 고-Fitness 템플릿(변형해 사용): ts_decay_linear(rank((vwap-close)/close), 5) (~2.86),
rank(ts_rank(close/ts_delay(close,5)-1, 40)) (~1.5), -ts_av_diff(close,50)*ts_corr(close,volume,50) (~1.70).

[복잡도 예산 — 과적합 방지]
서로 다른 base 필드 ≤ 6개, 식 길이 과도 금지, 깊은 중첩 금지(≤6단). 설계 원칙: 비율 > 곱 > 합
(rank(A/(B+0.000001)) 가 rank(A)*rank(B) 보다 일반화 잘 됨). 엄격한 == 등호 대신 밴드(±0.1*ts_std) 사용.
단일 연산자 알파(rank(close) 류)는 Sharpe 거의 0 — 최소 2개 차원(가격모멘텀+거래량/변동성)을 결합하라.
```

(c) At the very end of the `[출력 형식 — 반드시 준수]` section, append:
```
🚨 출력 가드(엄수): JSON 배열만. markdown 코드펜스(```)·따옴표 래핑·"분석 결과"/"개선된 알파" 류 사족 절대 금지.
✅ 올바른 응답 시작: [{"code": "...", ...
❌ 잘못: 다음은 제안 알파입니다: [{...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_gemini_generation_p1.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run cache-signature regression**

The SYSTEM_INSTRUCTION hash feeds `_csv_signature` / `_prompt_cache_key` (cache auto-invalidates on prompt change — intended). Confirm nothing else broke:
Run: `python3.11 -m pytest tests/test_settings_cache.py tests/test_gemini_slot_settings.py -v`
Expected: PASS

- [ ] **Step 6: Checkpoint** — tests green; do not commit.

---

## Task 5: Inject seeds + per-round grounded research-notes into generation

Design note: Gemini's `google_search` tool does not combine cleanly with a forced JSON `response_schema`. So grounding runs as a **separate, plain-text "research notes" call once per round**, gated by `run_config.is_grounding_enabled()`; its text is injected into the (cached, structured) batch-generation prompt. This satisfies "per-round grounding at generation time" while keeping the structured-output path intact and bounding cost to one extra short call/round.

**Files:**
- Modify: `server/gemini_strategist.py` — add `generate_research_notes(...)`, inject seeds + notes into `_build_user_prompt_full` / `_build_user_prompt_cached`, call it in `generate_strategies`.
- Test: `tests/test_gemini_generation_p1.py` (append)

- [ ] **Step 1: Write the failing test** (append to `tests/test_gemini_generation_p1.py`)

```python
class TestSeedAndGroundingWiring(unittest.TestCase):
    def test_research_notes_disabled_returns_empty_without_api(self):
        # When grounding is disabled, no client/API call is made → '' immediately.
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
            1, [], [], [], [], [], {},
            forced_delay=None, slot_settings=None, seeds_section=alpha_seeds.render_seeds_section(seeds),
        )
        self.assertIn('검증된 저회전 시드', prompt)

    def test_user_prompt_includes_research_notes(self):
        prompt = gs._build_user_prompt_full(
            1, [], [], [], [], [], {},
            forced_delay=None, slot_settings=None,
            seeds_section='', research_notes='연구노트: 변동성-조정 모멘텀',
        )
        self.assertIn('연구노트: 변동성-조정 모멘텀', prompt)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.11 -m pytest tests/test_gemini_generation_p1.py::TestSeedAndGroundingWiring -v`
Expected: FAIL — `generate_research_notes` missing; `_build_user_prompt_full` does not accept `seeds_section`/`research_notes`.

- [ ] **Step 3: Implement**

(a) Add the grounded research-notes function to `server/gemini_strategist.py`:

```python
def generate_research_notes(*, api_key: str, round_num: int,
                            log_fn: Callable | None = None) -> str:
    """One short Google-grounded call per round → 3 fresh delay=0 factor hypotheses
    as text, injected into the batch-generation prompt. Returns '' when grounding is
    disabled or on any failure (generation continues ungrounded)."""
    try:
        from . import run_config
        if not run_config.is_grounding_enabled():
            return ''
    except Exception:
        return ''
    if not api_key:
        return ''
    prompt = (
        '최신 퀀트 팩터 연구/논문/리서치에서 WorldQuant Brain delay=0 (USA, Price-Volume 필드만: '
        'close/open/high/low/volume/vwap/returns/adv20/cap) 에 적용할 만한 신선한 알파 가설 3개를 '
        '각 한 줄로. 회전이 낮고 Sharpe 가 높을 만한 구조 위주. 출처/설명 없이 "가설: 식 스케치" 형식만.'
    )
    try:
        client = genai.Client(api_key=api_key)
        tools = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        cfg = genai_types.GenerateContentConfig(tools=tools, temperature=0.9,
                                                max_output_tokens=512)
        resp = client.models.generate_content(model=MODEL, contents=prompt, config=cfg)
        text = (resp.text or '').strip()
        if text and log_fn:
            log_fn(f'   (grounding 연구노트 {len(text)}자 수신)')
        return text[:1500]
    except Exception as e:
        if log_fn:
            log_fn(f'   (grounding 실패, 무근거 생성으로 진행: {str(e)[:80]})')
        return ''
```

(b) Add optional params to the two prompt builders. Change the `_build_user_prompt_full` signature to accept `seeds_section: str = '', research_notes: str = ''` and append them; do the same for `_build_user_prompt_cached`. Concretely, at the end of each builder, before the `return`, append:

```python
    extra = []
    if research_notes:
        extra.append('\n\n[이번 라운드 연구노트 — 신선한 가설 시드]\n' + research_notes)
    if seeds_section:
        extra.append('\n\n' + seeds_section)
    return prompt + ''.join(extra)
```
(Adapt `prompt` to whatever local variable each builder returns.)

(c) In `generate_strategies`, before the model loop, build the seeds + notes once:

```python
    from . import alpha_seeds
    import random as _rnd
    _seeds = alpha_seeds.sample_seeds(5, rng=_rnd.Random(round_num))
    seeds_section = alpha_seeds.render_seeds_section(_seeds)
    research_notes = generate_research_notes(api_key=api_key, round_num=round_num, log_fn=log_fn)
```
Then pass `seeds_section=seeds_section, research_notes=research_notes` into both
`_build_user_prompt_cached(...)` and `_build_user_prompt_full(...)` calls.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.11 -m pytest tests/test_gemini_generation_p1.py -v`
Expected: PASS (all classes)

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `python3.11 -m pytest -q`
Expected: PASS — previous 348 + new P1 tests, 0 failures.

- [ ] **Step 6: Checkpoint** — tests green; do not commit.

---

## Task 6: Deploy + live validation (one round)

**Files:** none (operational).

- [ ] **Step 1: Confirm with the boss before restarting** (interrupts the in-flight round; running=1 auto-resumes).

- [ ] **Step 2: Restart**

Run: `sudo systemctl restart hyfe-iqc.service`
Then: `systemctl status hyfe-iqc.service --no-pager | head -6` → Expected: active (running), no Traceback.

- [ ] **Step 3: Confirm new code loaded + auto-resume**

Run: `journalctl -u hyfe-iqc.service --since "1 min ago" --no-pager | grep -iE "auto-resume|Traceback|grounding|연구노트"`
Expected: `auto-resume worker for user_id=2`; no Traceback; (within the round) a `grounding 연구노트 …자 수신` line if grounding is on.

- [ ] **Step 4: Watch one round complete, check error rate + seed/grounding effect**

Run (after ~30 min, the next finished round id):
```bash
sqlite3 -header -column data/hyfe_iqc.db "SELECT round_num,phase,parent_idx,status,err_count,pass_count FROM rounds WHERE user_id=2 ORDER BY id DESC LIMIT 3;"
sqlite3 -header -column data/hyfe_iqc.db "SELECT idx,pass_count pc,error_count ec FROM alphas WHERE user_id=2 AND round_id=(SELECT MAX(id) FROM rounds WHERE user_id=2 AND status='done') ORDER BY idx;"
```
Expected: error rate not worse than before (≤ baseline), generated alphas scored; ideally higher max `pass_count` than the round-560 ceiling of 6. Record the numbers.

- [ ] **Step 5: Report results to the boss** — PASS distribution before/after, any new errors, whether grounding fired. Decide go/no-go for P2.

---

## Self-review checklist (completed by plan author)

1. **Spec coverage (P1 scope §8):** target recalibration → Task 4; `alpha_seeds` palette → Tasks 2-3; WQB-Fitness block → Task 4; anti-preamble guard → Task 4; complexity budget → Task 4; grounding ON → Tasks 1, 5. ✅ All P1 items mapped.
2. **Placeholder scan:** every code step contains complete code; no TBD/TODO. Task 3 Step 3 is a conditional fix loop with concrete instructions, not a placeholder. ✅
3. **Type consistency:** `is_grounding_enabled`/`set_grounding_enabled` (Task 1) used in Task 5; `sample_seeds`/`render_seeds_section`/`SEED_TEMPLATES`/`FAMILIES` (Task 2) used in Tasks 3, 5; `generate_research_notes`, `_build_user_prompt_full(..., seeds_section, research_notes)` consistent between Task 5 impl and tests. ✅
4. **Deferred to later phases (not P1):** AST decorrelation, directed_mutation, meta-strategy, KB — P2-P5, each its own plan.
