# Alpha Search P3 — Directed Mutation Plan

> subagent-driven-development. TDD. Tests on `python3.11 -m pytest`. NO git commit. Deploy = restart.

**Goal:** Replace blind focus re-generation with **metric-targeted** mutation: read a near-miss alpha's FAILING IS metrics and inject a specific fix directive (turnover→decay/trade_when; Sharpe<2→orthogonal/double-rank/neutralize; Sharpe<0→sign flip; weight-conc→outer rank/winsorize; sub-uni→group_neutralize; self-corr→rotate operator family) into the focused-generation prompt. This is the biggest lever to convert PASS-6 near-misses into PASS-7.

**Architecture:** New pure `server/directed_mutation.py` (`route(fail_items, code) -> {strategy, instruction}`). Wire into `gemini_strategist.generate_focused_strategies` — compute the directive from the `parent_fail_items` it already receives and append to the focused prompt. NO worker change needed (the worker already passes parent_fail_items into generate_focused_strategies).

---

## File structure
| File | Responsibility | Action |
|---|---|---|
| `server/directed_mutation.py` | pure metric→directive router | Create |
| `tests/test_directed_mutation.py` | router branches | Create |
| `server/gemini_strategist.py` | compute+inject directive in `generate_focused_strategies` | Modify |
| `tests/test_directed_mutation.py` | also assert focused prompt carries the directive | (same file) |

---

## Task 1: `directed_mutation.py` router (full code in implementer dispatch)
Interface: `route(fail_items: list[str], code: str = '') -> dict` returning `{'strategy': str, 'instruction': str}`; `instruction == ''` when no actionable failure is recognised. Pure; never raises. Parses each fail string with a regex `'<metric> of <value>%? is below|above cutoff of <cutoff>%?'`; keyword fallback for Weight-distribution and Self-correlation items. Branch table:

| failing metric | strategy | directive (Korean, FASTEXPR-valid) |
|---|---|---|
| Sharpe below, value ≥ 0 | raise_sharpe | add orthogonal `add(zscore(a),zscore(b),filter=true)` / `rank(ts_rank(x,40))` / `group_neutralize` |
| Sharpe below, value < 0 | sign_flip | prepend `-1 *` |
| Sub-universe Sharpe below | subuniv | `group_neutralize(x, sector|industry)` |
| Fitness below | raise_fitness | cut turnover via decay / raise returns / combine ≥2 dims |
| Turnover above | cut_turnover | `ts_decay_linear(rank(x), 8~15)` + `trade_when(저변동, x, -1)` + `hump(x, hump=0.03)` |
| Turnover below | raise_turnover | reduce decay/hump/ts_mean smoothing (keep ≥1%) |
| Weight conc / not distributed | spread_weight | outer `rank()`/`winsorize`, lower truncation, `group_neutralize` |
| self-correlation | rotate_family | SC-saturation: change operator family (corr→decay, rank→trade_when/regression) |

Tests: one per branch (assert `strategy` tag + a keyword in `instruction`), empty→generic/'', garbage→never raises.

## Task 2: inject into `generate_focused_strategies`
- Add (no new param needed) inside `generate_focused_strategies`, after `_build_focused_prompt(...)` + `_delay_directive` (around line 1101): when `focus_kind == 'fail'`, compute `from . import directed_mutation; _md = directed_mutation.route(parent_fail_items or [], parent_code)`; if `_md['instruction']`: `user_prompt += '\n\n' + _md['instruction']` and log `(directed-mutation: {_md['strategy']})`.
- Test: call `gemini_strategist._build_focused_prompt(...)` is unaffected; assert that a helper or the route output is appended — simplest: unit-test `directed_mutation.route` (Task 1) thoroughly, plus one integration test that monkeypatches nothing but checks `directed_mutation.route(['Sharpe of 1.55 is below cutoff of 2.'])['instruction']` is non-empty and contains a FASTEXPR token. (Full generate path needs an API key, so don't call it in tests.)

## Task 3: deploy (with P2) + validate
- Full suite green; restart; watch a focus round — confirm a `directed-mutation:` log line appears and the focused alphas reflect the directive (e.g. Sharpe-fail round → alphas add orthogonal signals / neutralization). Report.

## Self-review
- Coverage: router → T1; injection → T2; deploy → T3. ✅ Pure + fail-open + tested.
