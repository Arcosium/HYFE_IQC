# Alpha Search P2 — Pre-Sim Gate (AST decorrelation + complexity) Plan

> **For agentic workers:** subagent-driven-development. TDD per task. Tests on `python3.11 -m pytest`. NO git commit (repo policy). Deploy = `sudo systemctl restart hyfe-iqc.service` (gemini/worker/alpha_ast are server-imported).

**Goal:** Spend zero WQB simulation slots on candidates that are structural near-duplicates of already-submitted/starred alphas or that blow the complexity budget — attacking self-correlation (≤0.7) and over-fitting before simulation. Prerequisite: fix the `alpha_ast` parser so `-1 * expr` (most real alphas) actually parses.

**Architecture:** Extend `server/alpha_ast.py` (parser + new structural/complexity functions), add a pure `server/presim_gate.py`, wire it into `server/worker.py` between lint and simulation. All new logic pure + unit-tested; worker change is thin.

---

## File structure

| File | Responsibility | Action |
|---|---|---|
| `server/alpha_ast.py` | parser unary-minus support; `largest_common_subtree`, `structural_overlap`, `symbol_length`, `base_feature_count`, `free_const_ratio` | Modify |
| `server/alpha_seeds.py` | revert 4 seeds `expr * -1` → idiomatic `-1 * expr` (parser now handles it) | Modify |
| `server/presim_gate.py` | pure `screen(candidates, existing_codes, opts) -> (kept, dropped)` | Create |
| `server/worker.py` | call `presim_gate.screen` after lint, before simulate; log drops | Modify |
| `tests/test_alpha_ast_unary.py` | parser unary-minus + structural/complexity fns | Create |
| `tests/test_presim_gate.py` | screen logic | Create |

---

## Task 0: Parser unary-minus support (+ revert seeds)

**Why:** `alpha_ast.parse('-1 * ts_corr(...)')` returns `None` today (parser drops a leading `-`). Most real alphas start with `-1 *`, so decorrelation/complexity would be blind to them. See [[alpha-ast-unary-minus-gap]].

**Files:** Modify `server/alpha_ast.py`, `server/alpha_seeds.py`; Create `tests/test_alpha_ast_unary.py`.

- [ ] **Step 1 — failing test** `tests/test_alpha_ast_unary.py`:
```python
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import unittest
from server import alpha_ast

class TestUnaryMinus(unittest.TestCase):
    def test_leading_neg_mul_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-1 * ts_corr(rank(close), rank(volume), 10)'))
    def test_neg_call_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-rank(close)'))
    def test_neg_number_parses(self):
        self.assertIsNotNone(alpha_ast.parse('-1'))
    def test_fields_and_ops_under_negation(self):
        self.assertIn('close', alpha_ast.fields_used('-rank(close)'))
        self.assertIn('rank', alpha_ast.operators_used('-rank(close)'))
    def test_unary_distinct_from_positive(self):
        # negation should produce a structurally different tree than the bare atom
        self.assertNotEqual(alpha_ast.parse('-rank(close)'), alpha_ast.parse('rank(close)'))
    def test_plus_unary_is_noop(self):
        self.assertEqual(alpha_ast.parse('+rank(close)'), alpha_ast.parse('rank(close)'))
```

- [ ] **Step 2 — run, expect FAIL** (`test_leading_neg_mul_parses` etc.): `python3.11 -m pytest tests/test_alpha_ast_unary.py -v`

- [ ] **Step 3 — implement in `server/alpha_ast.py`:**
  In `_parse_atom`, BEFORE the final "anything else — skip" branch, add unary handling:
```python
        # unary +/- : consume sign, parse the following atom
        if t['kind'] == 'OP' and t['value'] in ('-', '+'):
            self._consume()
            operand = self._parse_atom()
            if operand is None:
                return None
            if t['value'] == '+':
                return operand
            return {'type': 'unary', 'op': '-', 'operand': operand}
```
  In `_children_of`, add: `if node.get('type') == 'unary': op = node.get('operand'); return [op] if isinstance(op, dict) else []`.
  In `_collect_field_enclosing_op`, add a branch: `if ntype == 'unary': _collect_field_enclosing_op(node.get('operand'), parent_call_name, result); return`.
  In `_node_depth`: unary already routes through `_children_of`; ensure a unary with a call child still counts depth correctly (it will, via child_max). No extra change needed, but verify the test.
  Update the "Node shapes" docstring to list `unary: {'type':'unary','op':'-','operand':Node}`.

- [ ] **Step 4 — run, expect PASS.** Also run `python3.11 -m pytest tests/test_alpha_ast.py -v` (existing AST tests) → PASS (no regression).

- [ ] **Step 5 — revert seeds to idiomatic form** in `server/alpha_seeds.py`: change the 4 `... * -1` exprs back to `-1 * ...`:
  - `pv_corr_reversion` → `-1 * ts_corr(rank(close), rank(volume), 10)`
  - `decayed_ranked_corr` → `-1 * ts_rank(ts_decay_linear(ts_corr(group_neutralize(vwap, sector), volume, 4), 8), 6)`
  - `stochastic_reversal` → `-1 * ts_corr(rank((close - ts_min(low, 12)) / (ts_max(high, 12) - ts_min(low, 12) + 0.000001)), rank(volume), 6)`
  - `avdiff_corr_gate` → `-1 * ts_av_diff(close, 50) * ts_corr(close, volume, 50)`

- [ ] **Step 6 — run `python3.11 -m pytest tests/test_alpha_seeds.py -v`** → the contract test (`assertIsNotNone(parse(expr))`) must still PASS with the idiomatic `-1 *` forms (proving the parser fix). Then `python3.11 -m pytest -q` → no regressions.

- [ ] **Step 7 — Checkpoint** (no commit).

---

## Task 1: Complexity metrics in `alpha_ast.py`

**Files:** Modify `server/alpha_ast.py`; append to `tests/test_alpha_ast_unary.py`.

- [ ] **Step 1 — failing test** (append):
```python
class TestComplexityMetrics(unittest.TestCase):
    def test_symbol_length(self):
        self.assertEqual(alpha_ast.symbol_length('rank(close)'), len('rank(close)'))
    def test_base_feature_count(self):
        # distinct datafield leaves (fields_used) — close, volume = 2
        self.assertEqual(alpha_ast.base_feature_count('ts_corr(rank(close), rank(volume), 10)'), 2)
    def test_base_feature_count_dedup(self):
        self.assertEqual(alpha_ast.base_feature_count('close - ts_mean(close, 5)'), 1)
    def test_free_const_ratio_range(self):
        r = alpha_ast.free_const_ratio('rank(close) + 0.5')
        self.assertGreater(r, 0.0); self.assertLessEqual(r, 1.0)
    def test_free_const_ratio_no_consts(self):
        self.assertEqual(alpha_ast.free_const_ratio('rank(close)'), 0.0)
    def test_metrics_safe_on_garbage(self):
        # never raise
        self.assertIsInstance(alpha_ast.symbol_length(None), int)
        self.assertIsInstance(alpha_ast.base_feature_count('((('), int)
        self.assertIsInstance(alpha_ast.free_const_ratio(''), float)
```

- [ ] **Step 2 — run, expect FAIL.**

- [ ] **Step 3 — implement** (add to `alpha_ast.py`):
```python
def symbol_length(code) -> int:
    """Character length of the expression (proxy for complexity). 0 on non-str."""
    return len(code) if isinstance(code, str) else 0

def base_feature_count(code) -> int:
    """Number of DISTINCT base datafields used (== len(fields_used)). 0 on failure."""
    try:
        return len(fields_used(code))
    except Exception:
        return 0

def free_const_ratio(code) -> float:
    """Fraction of tree nodes that are numeric literals (free constants) — a proxy
    for over-parameterisation. 0.0 on failure / empty tree."""
    try:
        tree = parse(code)
        if tree is None:
            return 0.0
        nodes = list(_walk(tree))
        if not nodes:
            return 0.0
        nums = sum(1 for n in nodes if n.get('type') == 'num')
        return nums / len(nodes)
    except Exception:
        return 0.0
```

- [ ] **Step 4 — run, expect PASS.** Checkpoint.

---

## Task 2: `largest_common_subtree` + `structural_overlap`

**Files:** Modify `server/alpha_ast.py`; append to `tests/test_alpha_ast_unary.py`.

Algorithm: a node "shape" ignores leaf VALUES but keeps STRUCTURE + operator names (so two `ts_corr(rank(_), rank(_), _)` match even with different windows/fields — that is the decorrelation signal). Compute, for the two trees, the size (node count) of the largest subtree that is structurally identical between them. Commutative-aware for `+,*,&,|` and for commutative call args where order shouldn't matter is OUT OF SCOPE for v1 — keep it simple: structural equality is recursive type+name match with children compared in order, EXCEPT wrap the comparison so a top-level commutative group matches regardless of child order. Keep v1 tractable; the goal is catching near-identical structures, not perfect graph isomorphism.

- [ ] **Step 1 — failing test** (append):
```python
class TestStructuralOverlap(unittest.TestCase):
    def test_identical_structure_different_windows(self):
        a = 'ts_corr(rank(close), rank(volume), 10)'
        b = 'ts_corr(rank(close), rank(volume), 20)'  # only window differs
        size = alpha_ast.largest_common_subtree(a, b)
        self.assertGreaterEqual(size, 5)  # the whole ts_corr(rank,rank,_) shares
    def test_disjoint_structure_small_overlap(self):
        a = 'rank(close)'
        b = 'ts_mean(volume, 5)'
        self.assertLessEqual(alpha_ast.largest_common_subtree(a, b), 1)
    def test_self_overlap_is_full(self):
        a = 'ts_corr(rank(close), rank(volume), 10)'
        self.assertEqual(alpha_ast.largest_common_subtree(a, a),
                         len(list(alpha_ast._walk(alpha_ast.parse(a)))))
    def test_structural_overlap_finds_worst(self):
        code = 'ts_corr(rank(close), rank(volume), 15)'
        others = ['rank(close)', 'ts_corr(rank(close), rank(volume), 10)']
        size, idx = alpha_ast.structural_overlap(code, others)
        self.assertEqual(idx, 1)  # the ts_corr one is the closer match
        self.assertGreaterEqual(size, 5)
    def test_overlap_safe_on_garbage(self):
        self.assertEqual(alpha_ast.largest_common_subtree('(((', 'close'), 0)
        self.assertEqual(alpha_ast.structural_overlap('close', []), (0, -1))
```

- [ ] **Step 2 — run, expect FAIL.**

- [ ] **Step 3 — implement** in `alpha_ast.py`. Suggested approach (the implementer may refine, keeping tests green):
```python
def _shape_key(node) -> tuple:
    """Structural signature of a node ignoring leaf values but keeping type + op
    name + arity. Leaves (name/num/str) collapse to a single 'LEAF' so different
    fields/windows match structurally."""
    if not isinstance(node, dict):
        return ('LEAF',)
    t = node.get('type')
    if t == 'call':
        return ('call', node.get('name'), len(node.get('args', [])))
    if t == 'unary':
        return ('unary', node.get('op'))
    if t == 'group':
        return ('group', len(node.get('children', [])))
    return ('LEAF',)

def _subtree_size(node) -> int:
    return sum(1 for _ in _walk(node))

def _identical(a, b) -> bool:
    """Recursive structural equality (type+name+arity, children in order)."""
    if _shape_key(a) != _shape_key(b):
        return False
    ca, cb = _children_of(a), _children_of(b)
    if len(ca) != len(cb):
        return False
    return all(_identical(x, y) for x, y in zip(ca, cb))

def largest_common_subtree(code_a, code_b) -> int:
    """Size (node count) of the largest structurally-identical subtree shared by
    the two expressions. 0 on parse failure. Used as a pre-sim decorrelation signal."""
    try:
        ta, tb = parse(code_a), parse(code_b)
        if ta is None or tb is None:
            return 0
        nodes_b = list(_walk(tb))
        best = 0
        for na in _walk(ta):
            for nb in nodes_b:
                if _shape_key(na) == _shape_key(nb) and _identical(na, nb):
                    best = max(best, _subtree_size(na))
        return best
    except Exception:
        return 0

def structural_overlap(code, others):
    """Return (max_overlap_size, index_of_worst) vs a list of existing codes.
    (0, -1) if others is empty or on failure."""
    try:
        best, best_i = 0, -1
        for i, o in enumerate(others or []):
            s = largest_common_subtree(code, o)
            if s > best:
                best, best_i = s, i
        return best, best_i
    except Exception:
        return 0, -1
```
NOTE: `largest_common_subtree` is O(N_a × N_b) per pair — fine for our small expressions (≤ ~40 nodes) and a bounded `others` list. The gate (Task 3) must cap `others` to a recent window (e.g. last 60 submitted+starred) to keep it cheap.

- [ ] **Step 4 — run, expect PASS.** Then full suite `python3.11 -m pytest -q`. Checkpoint.

---

## Task 3: `presim_gate.py` + worker wiring

**Files:** Create `server/presim_gate.py`, `tests/test_presim_gate.py`; Modify `server/worker.py`.

Policy (conservative — diversity over safety, [[alpha-diversity-over-safety]]): only DROP a candidate when it is a near-structural-duplicate of an existing submitted/starred alpha (overlap ≥ threshold AND the candidate is small enough that the overlap dominates it) OR it busts a hard complexity ceiling. Everything dropped is logged with a reason (no silent caps). Default thresholds (tunable): `overlap_drop = 8` nodes, `max_symbol_length = 240`, `max_base_features = 8`, `max_free_const_ratio = 0.5`.

- [ ] **Step 1 — failing test** `tests/test_presim_gate.py`:
```python
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
            [{'idx': 1, 'code': 'ts_corr(rank(close), rank(volume), 20)'}],  # only window differs
            existing_codes=existing)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn('corr', dropped[0]['reason'].lower() + 'x')  # reason mentions overlap; loose check
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
```

- [ ] **Step 2 — run, expect FAIL.**

- [ ] **Step 3 — implement `server/presim_gate.py`:**
```python
"""presim_gate — structural decorrelation + complexity screen run BEFORE spending
a WQB simulation slot. Pure; never raises. Conservative: drop only clear near-
duplicates / budget-busters, and report a reason for every drop (no silent caps)."""
from __future__ import annotations
from . import alpha_ast

_DEFAULTS = {
    'overlap_drop': 8,
    'max_symbol_length': 240,
    'max_base_features': 8,
    'max_free_const_ratio': 0.5,
}

def screen(candidates, existing_codes=None, opts=None):
    """Return (kept, dropped). dropped items are {'idx','code','reason'}."""
    o = dict(_DEFAULTS); o.update(opts or {})
    existing = [c for c in (existing_codes or []) if isinstance(c, str) and c.strip()]
    kept, dropped = [], []
    for c in candidates or []:
        code = (c.get('code') or '') if isinstance(c, dict) else ''
        reason = _drop_reason(code, existing, o)
        if reason:
            d = dict(c); d['reason'] = reason; dropped.append(d)
        else:
            kept.append(c)
    return kept, dropped

def _drop_reason(code, existing, o):
    try:
        sl = alpha_ast.symbol_length(code)
        if sl > o['max_symbol_length']:
            return f'over-complex: symbol_length {sl} > {o["max_symbol_length"]}'
        bf = alpha_ast.base_feature_count(code)
        if bf > o['max_base_features']:
            return f'too many base fields: {bf} > {o["max_base_features"]}'
        fc = alpha_ast.free_const_ratio(code)
        if fc > o['max_free_const_ratio']:
            return f'over-parameterised: const ratio {fc:.2f} > {o["max_free_const_ratio"]}'
        if existing:
            size, idx = alpha_ast.structural_overlap(code, existing)
            if size >= o['overlap_drop']:
                return f'structural near-duplicate (overlap {size}) of existing #{idx}'
        return None
    except Exception:
        return None  # ALLOW on uncertainty
```

- [ ] **Step 4 — run, expect PASS.**

- [ ] **Step 5 — wire into `server/worker.py`.** In `_run_one_round`, AFTER strategies are generated + lint-filtered and BEFORE the to_simulate/cache split, screen against the user's submitted+starred codes:
  - Build `existing = submitted_codes` (already assembled earlier in the round as submitted+rejected codes — reuse it; cap to last ~60).
  - `from . import presim_gate`; `kept, dropped = presim_gate.screen(clean_strategies, existing_codes=existing)`.
  - For each dropped: `self._log(round_num, f'  ⊘ #{d["idx"]} 사전게이트 드롭: {d["reason"]}')` (no silent caps — log every drop).
  - Proceed to simulate only `kept`. Renumber idx on kept like the existing code does.
  - SAFETY: if screening would drop ALL candidates (e.g. threshold too aggressive), keep at least the least-overlapping one OR keep all and log a warning — never simulate zero alphas in a round. Implement: `if kept == [] and dropped: kept = [min-by-...]` OR simply `if not kept: kept = clean_strategies` with a warning log. Choose the simpler: if `not kept`, restore all and log `⚠ 사전게이트가 전부 드롭 — 전량 통과(threshold 점검)`.
  Read the surrounding worker code first to place this correctly relative to the existing focus/lint/cache flow.

- [ ] **Step 6 — run full suite** `python3.11 -m pytest -q` → no regressions. Add a worker-level note; the gate is covered by `test_presim_gate.py` (pure). Checkpoint.

---

## Task 4: Deploy + live validation
- [ ] `python3.11 -m pytest -q` green; confirm with boss; `sudo systemctl restart hyfe-iqc.service`; verify auto-resume + no Traceback.
- [ ] Watch ≥1 round: confirm `사전게이트 드롭` lines appear (or none, if no near-dups), error rate not worse, and that a round still simulates a non-empty set. Report drop counts + any issue.

## Self-review (author)
- Spec coverage: parser fix (prereq) → T0; complexity metrics → T1; AST decorrelation → T2; gate wiring → T3; deploy → T4. ✅
- No silent caps: every gate drop logged; zero-kept safety restores all. ✅
- Defensive: every new fn returns neutral on failure. ✅
