# Alpha Search P5 — SC-Saturation Rotation Plan

> subagent-driven. TDD. python3.11. NO git commit. Deploy = restart.

**Goal:** Proactively steer generation away from operator families that the submitted pool already over-uses — the documented cause of the self-correlation (≤0.7) wall ("operator-family + rank(Y) saturates at ~3-5 ACTIVE alphas; window/neutralization tweaks can't break it — you must rotate operator family"). Live data confirms: user2's 50 submitted alphas over-use group_neutralize(37), ts_zscore(35), ts_delta(29).

**Scope decision (no new DB table):** P2's structural decorrelation already drops near-duplicates reactively. P5 adds the PROACTIVE signal: tell the generator which signal-defining operators are saturated so it diversifies. The spec's rules/findings/failures KB is already served by the existing `errors`/`feedback`/`seeds` machinery, so we do NOT add a redundant KB table — P5 = the SC-saturation rotation lever (the spec's stated self-corr-wall solution), derived from existing `alphas` data.

**Architecture:** New pure `server/knowledge_base.py` (`saturated_operators`, `render_saturation_warning`). Worker computes it from the submitted pool and injects the warning into the generation prompt via the existing `effectiveness_priors` append (NO gemini_strategist signature change). Fail-open.

---

## File structure
| File | Responsibility | Action |
|---|---|---|
| `server/knowledge_base.py` | pure `saturated_operators`, `render_saturation_warning` | Create |
| `server/worker.py` | compute from `list_submitted_alphas`, append warning to `_priors` | Modify |
| `tests/test_knowledge_base.py` | saturation logic | Create |

---

## Task 1: `knowledge_base.py` (pure)
- `SIGNAL_OPS: frozenset` — signal-defining operators (exclude ubiquitous wrappers rank/add/scale/abs/sign): includes `ts_corr, ts_zscore, ts_delta, ts_mean, ts_sum, ts_std_dev, ts_decay_linear, ts_rank, signed_power, group_neutralize, trade_when, ts_av_diff, ts_min, ts_max, hump, winsorize, ts_delay, regression_neut, ts_regression, vec_avg, ts_arg_max, ts_arg_min`.
- `saturated_operators(codes, frac=0.25, floor=4) -> list[tuple[str,int]]`: count, across the DISTINCT alpha codes, how many use each SIGNAL_OP (via `alpha_ast.operators_used`); return `(op, count)` for ops used in `>= max(floor, ceil(frac*len(codes)))` distinct alphas, sorted by count desc. `[]` on empty/failure. Never raises.
- `render_saturation_warning(saturated) -> str`: '' if empty; else a soft prompt block: `'[SC-포화 경고 — 제출풀 편중 회피]\n제출풀이 다음 신호 연산자에 편중돼 있어 비슷한 알파는 self-corr>0.7 로 막힌다(창/중립화 조정으론 못 깸). 가능하면 이들을 덜 쓰고 다른 연산자 패밀리/데이터셋을 써라: {op(n), ...}'`.

Tests: counts on a small code list, threshold (frac/floor), excludes wrappers (rank not flagged even if ubiquitous), empty→[]/'' , never raises on garbage.

## Task 2: worker wiring
In `_run_one_round`, after `submitted_codes` is assembled (and after the bandit `_priors` block), compute the saturation warning from the FULL submitted pool (NOT the possibly-cleared `submitted_codes` var — non-focus non-bandit rounds clear it):
```python
            try:
                from . import knowledge_base
                _sub_for_sat = [a.get('code','') for a in _db.list_submitted_alphas(self.user_id, limit=50)]
                _sat_warn = knowledge_base.render_saturation_warning(
                    knowledge_base.saturated_operators(_sub_for_sat))
                if _sat_warn:
                    _priors = (_priors or '') + '\n\n' + _sat_warn
            except Exception:
                pass
```
Place it right after the `if _bandit_on and not is_focus: ... else: _slot_settings=None; _priors=''` block, so `_priors` exists. `_priors` is already passed as `effectiveness_priors=_priors` into `generate_strategies` and appended to the prompt — so no gemini change needed. (Focus rounds also benefit if `_priors` is threaded; if focus path doesn't use `_priors`, at minimum the EXPLORE path gets it — acceptable for P5.)

## Task 3: deploy (P4+P5) + validate
- Full suite green; restart; watch an EXPLORE round log/prompt — confirm no errors; optionally confirm the saturation warning is built (unit-tested). Report.

## Self-review
- No new table; derived from existing submitted alphas. Pure + fail-open. Complements P2 (reactive) with proactive steering. ✅
