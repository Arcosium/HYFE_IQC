# Evolutionary Alpha Search — Design Spec

**Date:** 2026-06-07
**Status:** Draft for review
**Approach:** A — evolutionary "brain" layered on the existing, battle-tested WQB I/O rails (round loop, `_wqb_pw_worker`, cache, lint, repair, stuck-sim handling preserved).
**Mandate:** Full redesign of the *alpha-generation logic* ("알파 생성 로직 전면 재설계"), 1–2 week budget, delay=0 alphas urgently needed before submission deadline.

---

## 1. Goals & success criteria

Produce WQB alphas that PASS the IS gate and become submittable (starred), prioritised by:

1. **High Sharpe** (competition cut: **≥ 2.0**)
2. **Low turnover** (gate 1–70%; we want the low end without over-smoothing to <1%)
3. **Return-to-drawdown** (good margin/returns vs drawdown)
4. **Low self-correlation** (≤ 0.7 vs already-submitted/starred pool)

Plus hard gates: Fitness ≥ 1.3, Weight well-distributed (no >10% concentration), Sub-universe Sharpe above cutoff.

**Success = measurably higher PASS≥7 rate and ≥1 starred alpha per day during the build**, validated live each phase. Today PASS≥7 rate ≈ 0 (round 560 produced 1000+ scored alphas, best 6/7).

### Non-goals (YAGNI)
- No local backtest engine / IC oracle (we have **no price-panel data** — `data/` is SQLite only). WQB sim is the *only* ground-truth evaluator. The cheap pre-sim gate is **structural** (AST/lint/novelty), not statistical.
- No rewrite of WQB I/O, Playwright worker, round persistence, cache, or stuck-sim handling.
- No multi-tab / network-interception (separate roadmap; out of scope here).

---

## 2. Key constraints (drive every decision)

- **WQB is the only oracle**, slot-limited, ~25–40 min per round of 10. → Be **sim-frugal**: maximise candidate quality *before* spending a slot; refine near-misses *deeply* rather than regenerating blind.
- **delay=0** restricts datafields to PV (close/open/high/low/volume/vwap/returns/cap/adv20) and explodes turnover → smoothing is mandatory (existing delay0 playbook stays).
- **Server-import modules** (`gemini_strategist.py`, `alpha_*`, `worker.py`, new modules) require `sudo systemctl restart hyfe-iqc.service` to deploy. `_wqb_pw_worker.py` is re-read each round (no restart). [[clean-code-refactor-2026-05-19]]
- Python: server = `/usr/bin/python3` = 3.9 (PEP604 `X | Y` at runtime in annotations needs care — use `from __future__ import annotations`), worker subprocess = 3.11; tests run on **python3.11**.

---

## 3. Architecture — round becomes a step in a persistent search state machine

Today a round is memoryless: generate 10 → sim → score → shallow 3-phase focus. New round = one step driven by a **MetaStrategySelector** reading persisted search state.

```
 per-user persistent search state (DB):
   survivor_pool   : best/near-miss alphas (PASS or high reward) for exploit/crossover
   trajectory      : recent round best-scores (slope, diversity, consecutive declines)
   knowledge_base  : rules / findings / failures (+ SC-saturation counters)
   focus_entries   : near-misses awaiting directed mutation (replaces raw focus_queue)

 round step:
   1. MetaStrategySelector.pick(state) -> mode ∈ {EXPLORE, REFINE, RECOMBINE, SIMPLIFY}
   2. build candidate batch for that mode:
        EXPLORE   : grounded hypothesis research -> generate (101 seeds + KB + family rotation)
        REFINE    : directed_mutation(near_miss, failing_metrics) -> focused generate
        RECOMBINE : crossover(two survivors from different families) -> focused generate
        SIMPLIFY  : simplify over-complex survivor
   3. PRE-SIM GATE (0 slots): lint + complexity budget + AST decorrelation + anti-repeat
        -> drop or request rewrite
   4. simulate survivors on WQB (existing _wqb_pw_worker, unchanged)
   5. score (reward.py) + classify failing metric per alpha
   6. update state: survivor_pool, trajectory, KB(findings/failures), SC-saturation, focus_entries
```

The MetaStrategySelector and all per-mode builders are **pure functions** over the persisted state (testable like `focus_priority.advance_focus_queue`). `worker._run_one_round` orchestrates I/O around them.

---

## 4. Module design (isolation & interfaces)

Convention: **N** new, **E** extend, **R** rewrite-section. Each module states *what it does / how to use / depends on*.

### 4.1 N `server/alpha_seeds.py` — low-turnover seed library
- **What:** the WorldQuant 101-Alphas low-turnover families, translated to WQB FASTEXPR, as parametric templates + a few measured high-Fitness templates. A palette the generator samples from, NOT alphas to emit verbatim.
- **Use:** `sample_seeds(n, families=None, exclude_ops=set()) -> list[SeedTemplate]`; rendered into the generation prompt as "관용구/시드" guidance.
- **Depends on:** nothing (pure data + render).
- **Content (initial):** templates P1–P10 from the 101-Alphas research, e.g.
  - P1 neg PV-corr: `-1 * ts_corr(rank(<PRICE>), rank(volume), <D∈{5,6,10}>)`
  - P2 decayed-ranked corr (group-neutral): `-1 * ts_rank(ts_decay_linear(ts_corr(group_neutralize(vwap, <GROUP>), volume, <d1>), <d2>), <d3>)`
  - P3 short-revert + long-corr (Alpha#32): `scale(ts_mean(close,7) - close) + 20 * scale(ts_corr(vwap, ts_delay(close,5), 230))`
  - P5 scale-combo (Alpha#28): `scale(ts_corr(adv20, low, 5) + (high+low)/2 - close)`
  - P7 stochastic reversal (Alpha#55): `-1 * ts_corr(rank((close - ts_min(low,12))/(ts_max(high,12)-ts_min(low,12))), rank(volume), 6)`
  - vwap-relative ratio (Alpha#42): `rank(vwap - close) / rank(vwap + close)`
  - measured high-Fitness (QuantGPT): `ts_decay_linear(rank((vwap-close)/close), 5)` (~2.86), `-ts_av_diff(close,50) * ts_corr(close,volume,50)` (~1.70)
- **Notes:** integer-ize all windows; **bare** group names in `group_neutralize` (no quotes); wrap any 1-day-delta seed in `ts_decay_linear`/`hump(x, hump=0.03)` for delay=0.

### 4.2 R `server/gemini_strategist.py` — SYSTEM_INSTRUCTION + grounding
- **Recalibrate targets:** line 46 "Sharpe ≥ 1.25, Fitness ≥ 1.0" → **"Sharpe ≥ 2.0, Fitness ≥ 1.3"** (the real cut). This is the single highest-leverage one-line fix.
- **Add WQB-Fitness block:** `Fitness = Sharpe × √(|Returns| / max(Turnover, 0.125))`; lever list in priority order (decay_linear wrap, rank, ts_zscore, double-rank, `-ts_av_diff(x,50)*ts_corr(x,y,50)`, trade_when gate); measured high-Fitness templates.
- **Complexity budget:** ≤6 distinct base fields, symbol length ≤ ~200 chars, prefer `ratio > 곱 > 합` (`rank(A/(B+0.000001))` over `rank(A)*rank(B)`), nesting depth ≤ ~6, band-not-equality.
- **Anti-preamble output guard:** explicit ✅/❌ examples; JSON-only, no markdown/prose. (Hardens existing JSON contract.)
- **Google grounding (per round, generation call only):** enable the Gemini `google_search` tool on the round's `generate_strategies` call so it can pull current factor ideas from papers/reports into the batch. Cost control: grounding applies to the **single batch-generation call per round** (not per-alpha, not to refinement calls). Grounding disables context-cache → the system instruction is sent inline that call; acceptable at one call/round. A short "research notes" step may precede generation (one grounded call → hypotheses → injected into the batch prompt).
- **Depends on:** `alpha_seeds`, `knowledge_base` (for KB injection), genai SDK grounding types.

### 4.3 E `server/alpha_ast.py` — structural decorrelation + complexity
- **Add:** `largest_common_subtree(codeA, codeB) -> int` (size of max shared subtree; **commutative-aware** for `+,*,&,|,==,!=`), `structural_overlap(code, others) -> (max_size, which)`, and complexity metrics `symbol_length(code)`, `base_feature_count(code)` (distinct `$`/datafield leaves), `free_const_ratio(code)`.
- **Use (pre-sim gate):** reject/flag a candidate whose `largest_common_subtree` vs any submitted/starred alpha exceeds a threshold (start 6–8 nodes) → attacks self-corr at **0 sim cost**; reject if complexity budget exceeded.
- **Depends on:** existing tolerant parser (`parse`, `_walk`). Reuses Node tree; never raises (ALLOW on parse failure, consistent with current philosophy).

### 4.4 N `server/directed_mutation.py` — metric→fix router (the biggest PASS lever)
- **What:** pure function mapping a near-miss's **failing IS metrics** to a concrete mutation directive (instruction text + suggested operators/settings) injected into a focused generation call. Replaces blind 3-phase regeneration.
- **Use:** `route(metrics, fail_items, code) -> MutationDirective(strategy, instruction, ops_to_add, settings_hint)`.
- **Routing table (initial):**

  | failing condition | directive |
  |---|---|
  | Turnover > 70% (or weight churn) | wrap final signal `ts_decay_linear(rank(x), 8)`; add `trade_when(entry, x, -1)`; raise decay |
  | Turnover < 1% (over-smoothed) | reduce decay/hump; drop excess `ts_mean` smoothing |
  | Sharpe < 2 | add orthogonal signal `add(zscore(a), zscore(b), filter=true)`; double-rank `rank(ts_rank(x,40))`; neutralize |
  | Sharpe < 0 | prepend `-1 *` (sign flip) |
  | Fitness < 1.3 | lift returns and/or cut turnover (decay); combine ≥2 dims |
  | Weight concentration > 10% | outer `rank()`/`winsorize`; lower truncation; `group_neutralize` |
  | Sub-universe Sharpe low | `group_neutralize(x, sector|industry)` |
  | self-corr > 0.7 | **rotate operator family** (SC-saturation law); change neutralization/universe |

- **Depends on:** `reward`/metric parsing, `alpha_seeds` (replacement ops), `knowledge_base` (SC-saturation). Pure & testable.

### 4.5 N `server/alpha_search.py` — meta-strategy + trajectory + survivor pool
- **What:** pure controller logic. `pick_mode(trajectory, survivors, focus) -> mode`; trajectory metrics `diversity` (CV of recent best-scores), `convergence` (normalized slope), `consecutive_declines`; survivor-pool selection for exploit/crossover (elitism: refine from **best-so-far**, not latest).
- **Decision tree (initial, from QuantGPT meta-evolution):**
  - depth/complexity too high → SIMPLIFY
  - have near-miss (PASS 5–6, one metric short) → REFINE (directed mutation)
  - plateaued (consecutive_declines ≥ 2) → RECOMBINE
  - early & weak, or diversity collapsed → EXPLORE
  - else → REFINE/EXPLORE by score band
- **Depends on:** state read from `db`. Pure & testable.

### 4.6 N `server/knowledge_base.py` — rules/findings/failures + SC-saturation
- **What:** structured, persistent memory the generator reads at round start and writes at round end. Three buckets: **rules** (hard platform constraints), **findings** (validated signal structures, e.g. "fundamental ratio orthogonal add lifted Fitness 1.07→1.47"), **failures** (dead ends). Plus **SC-saturation counter**: `(operator_family, outer_op) -> count of self-corr-PASS`; when ≥ ~4, mark saturated → EXPLORE must rotate to a new family.
- **Use:** `load_for_prompt(user_id) -> str`; `record_finding/record_failure/bump_saturation(...)`; `saturated_families(user_id) -> set`.
- **Depends on:** `db` (new table). [[alpha-diversity-over-safety]] — errors are learning signal, denylist not whitelist.

### 4.7 E `server/worker.py` — orchestrate the step
- Replace the `is_focus`/`focus_queue` branch in `_run_one_round` with: `mode = alpha_search.pick_mode(...)` → per-mode batch build → pre-sim gate → existing sim path → state update. Keep all WQB sim plumbing, logging, cache, finish_round. The `advance_focus_queue` fix stays for any residual queue use; focus entries migrate into the survivor/near-miss structure.

### 4.8 E `server/db.py` — state tables
- Add tables/columns: `survivor_pool`, `trajectory` (per-round best score + mode), `knowledge_base` (bucket, key, body, ts), `sc_saturation` (family, outer_op, count). Gate with `PRAGMA user_version` migration (existing pattern). Reuse `alphas`/`rounds` where possible.

---

## 5. Data flow (one REFINE round, concrete)

1. `pick_mode` sees a near-miss (Sharpe 1.55, all else PASS) → REFINE.
2. `directed_mutation.route` → "Sharpe<2: add orthogonal signal + double-rank; keep low turnover."
3. focused `generate_strategies` (KB injected, **no grounding**, cached system instruction) → 10 variants.
4. pre-sim gate: drop 3 over-correlated/over-complex → 7 survive.
5. WQB sim 7 → score → one hits Sharpe 2.1, all PASS → starred; others recorded.
6. state update: starred alpha → survivor_pool + submitted pool; bump SC-saturation for its family; record finding.

---

## 6. Error handling
- Every pure module is **defensive** (never raises; ALLOW/neutral on parse/parse-failure), mirroring `focus_priority`/`alpha_ast`.
- Grounding call failure → fall back to non-grounded generation (existing model-fallback chain).
- Directed-mutation with unparseable metrics → neutral directive (generic "improve weakest metric").
- DB migration failures are gated and logged; never crash the round.

---

## 7. Testing strategy
- Unit tests (python3.11) per pure module, following `test_focus_priority.py` style:
  - `alpha_seeds`: render/sample, all templates parse via `alpha_ast.parse`, bare-group + named-hump invariants.
  - `alpha_ast` additions: `largest_common_subtree` (commutative cases, identical, disjoint), complexity metrics.
  - `directed_mutation.route`: each failing-metric branch returns expected strategy; neutral on junk.
  - `alpha_search.pick_mode`: decision-tree branches; trajectory metric math.
  - `knowledge_base`: bucket round-trip, SC-saturation threshold.
- Contract test: every `alpha_seeds` template + every directed-mutation example expression passes `alpha_lint` and `alpha_ast.validate` (no FASTEXPR syntax regressions).
- Live validation per phase: deploy (restart), watch one round, confirm error rate and PASS distribution in DB.

---

## 8. Phased rollout (each phase independently shippable + live-validated + tested)

| Phase | Scope | Est. | Primary lever |
|---|---|---|---|
| **P1** | Generation quick-wins: target recalibration (2.0/1.3), `alpha_seeds` palette, WQB-Fitness block, anti-preamble guard, complexity budget, **grounding ON** | 1–2d | PASS rate ↑, deadline de-risk |
| **P2** | Pre-sim gate: `alpha_ast` decorrelation + complexity + structural dedup wired into worker | 2–3d | wasted sims ↓, self-corr ↓ |
| **P3** | `directed_mutation` refinement engine replacing blind focus | 3–4d | near-miss PASS (biggest) |
| **P4** | `alpha_search` meta-strategy + survivor pool + crossover | 3–4d | search efficiency, diversity |
| **P5** | `knowledge_base` (rules/findings/failures) + SC-saturation rotation | 2–3d | self-corr wall, anti-regression |

P1 ships within ~2 days to protect the deadline; later phases layer on without destabilising the live WQB path.

---

## 9. Open questions / risks
- **Grounding cost:** per-round grounded generation disables context cache; monitor token spend; if excessive, demote to a per-round *research-notes* call only.
- **SC-saturation threshold** (~4) and **subtree-overlap threshold** (6–8) are starting guesses — tune from live data in P2/P5.
- **DB migration** on a live 17 MB DB — must be additive + gated; back up before P4/P5 schema changes.
- delay=0 over-smoothing floor (Turnover <1%) must stay guarded in directed_mutation (don't let the turnover-reduction branch push below 1%).

---

## 10. Provenance (research sources)
- **Harvey-Sun/World_Quant_Alphas** — 101 formulas verbatim + low-turnover templates P1–P10 + FASTEXPR translation → `alpha_seeds`.
- **Miasyster/QuantGPT** — WQB-Fitness block, metric-driven directed-mutation router, trajectory meta-strategy, anti-preamble guard, rules/findings/failures KB, SC-saturation law → §4.2/4.4/4.5/4.6.
- **QuantaAlpha/QuantaAlpha** — AST largest-common-subtree decorrelation, complexity/parsimony budget, mutation-as-orthogonality, crossover with diversity-scored selection → §4.3/4.5.
- **koreal6803/finlab-ai** — `signal × gate` + `sustain(n)` turnover-reduction idioms, lookahead/PIT discipline for delay=0, IC/IC-IR & Shapley as *future* offline analytics (deferred — no local data) → idioms into §4.1/4.2.
