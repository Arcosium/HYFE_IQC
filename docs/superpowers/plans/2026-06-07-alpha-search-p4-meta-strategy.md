# Alpha Search P4 — Trajectory Meta-Strategy + Survivor Crossover Plan

> subagent-driven. TDD. python3.11. NO git commit. Deploy = restart.

**Goal:** Make round-mode selection trajectory-aware and add RECOMBINE (crossover of two surviving near-miss alphas from different operator families). Today the mode is implicit: focus_queue non-empty → REFINE (focus), else → EXPLORE (main). P4 adds, on EXPLORE rounds, a data-driven choice to RECOMBINE two survivors when the search has plateaued — fusing proven structure into new, decorrelated alphas.

**Design principle — NO new DB tables / migration:** derive trajectory metrics and the survivor pool from the existing `rounds` and `alphas` tables via queries. Lower risk on the live 17 MB DB.

**Architecture:** New pure `server/alpha_search.py` (trajectory metrics + `pick_mode`). New `gemini_strategist.generate_crossover_strategies` (LLM fuses 2 parent codes). Thin worker hook: on an EXPLORE (non-focus) round, if `pick_mode` says RECOMBINE and ≥2 survivors exist, run crossover instead of a plain main round. Everything additive + fail-open.

---

## File structure
| File | Responsibility | Action |
|---|---|---|
| `server/alpha_search.py` | pure `trajectory_metrics`, `pick_mode` | Create |
| `server/db.py` | `recent_round_scores(user_id, n)`, `survivor_alphas(user_id, n, min_pass)` queries | Modify |
| `server/gemini_strategist.py` | `generate_crossover_strategies(parents, ...)` | Modify |
| `server/worker.py` | EXPLORE round: consult `pick_mode`; RECOMBINE branch | Modify |
| `tests/test_alpha_search.py` | trajectory + pick_mode | Create |

---

## Task 1: `alpha_search.py` (pure)
Interface:
- `trajectory_metrics(scores: list[float]) -> dict` returning `{'diversity','convergence','consecutive_declines','best','n'}`. `diversity` = coefficient of variation (std/mean, 0 if mean 0); `convergence` = normalized slope sign of recent scores (−1..1); `consecutive_declines` = count of trailing strictly-decreasing steps. All pure, never raise.
- `pick_mode(scores, *, has_near_miss, survivor_count, max_depth_seen=0) -> str` returning one of `'EXPLORE'|'REFINE'|'RECOMBINE'|'SIMPLIFY'`. Decision tree:
  - `has_near_miss` (a queued focus entry exists) → `'REFINE'` (existing focus path; directed-mutation already wired).
  - else (an EXPLORE-eligible round):
    - `consecutive_declines >= 2 and survivor_count >= 2` → `'RECOMBINE'`
    - `max_depth_seen > 8` → `'SIMPLIFY'`  (reserved; worker may treat like EXPLORE w/ a simplify hint for now)
    - else → `'EXPLORE'`
Tests: trajectory math (rising/falling/flat scores), each pick_mode branch, empty/garbage safety.

## Task 2: `db.py` queries (no schema change)
- `recent_round_scores(user_id, n=6) -> list[float]`: for the last n DONE rounds, the round's best alpha reward proxy = `MAX(pass_count)` (or a reward column if present). Pure SQL read.
- `survivor_alphas(user_id, n=6, min_pass=5) -> list[dict]`: distinct recent alphas with `pass_count >= min_pass`, newest first, each `{code, pass_count, operators}` (operators via `alpha_ast.operators_used`). For crossover parent selection (prefer different operator families).
Tests: covered indirectly; add a small `tests/test_db_p4.py` using a temp DB if practical, else assert callable + safe on empty.

## Task 3: `generate_crossover_strategies` in gemini_strategist
- Signature mirrors `generate_focused_strategies` but takes `parents: list[dict]` (2 survivor codes+desc). Builds a prompt: "두 고성과 알파의 성공요소(연산자/창/정규화/신호방향)를 추출해 **구조적으로 다른** 새 하이브리드 알파 10개로 융합하라. 두 부모와도, 서로도 달라야 한다." Includes both parent codes. Reuses the JSON response schema, lint filter, model-fallback loop (copy the structure from generate_focused_strategies). Pure-ish (one LLM call); fail to `generate_strategies` semantics on error.
- Test: assert the prompt builder includes both parent codes (no API call in test).

## Task 4: worker wiring
- In `_run_one_round`, for a NON-focus round (the `else` branch, phase 0), BEFORE generating: compute `scores = _db.recent_round_scores(user_id)`, `survivors = _db.survivor_alphas(user_id)`, `mode = alpha_search.pick_mode(scores, has_near_miss=False, survivor_count=len(survivors))`.
  - If `mode == 'RECOMBINE'` and `len(survivors) >= 2`: pick 2 survivors with the most different operator sets; call `gemini_strategist.generate_crossover_strategies(parents=...)`; log `🧬 RECOMBINE (survivors #..)`. Else fall through to the normal `generate_strategies` EXPLORE path.
  - Wrap in try/except → on any error, fall back to normal EXPLORE (fail-open).
- Focus rounds (REFINE) are unchanged (already have directed-mutation).

## Task 5: deploy + validate (with P5)
- Full suite green; restart; watch rounds for a `🧬 RECOMBINE` line (only fires after a plateau with ≥2 survivors) and confirm no errors/regression. Report.

## Self-review
- No DB migration (queries only) → low live risk. Every new path fail-open. pick_mode falls back to EXPLORE. ✅
