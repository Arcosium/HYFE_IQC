"""Ranked, rotating, settings-scoped datafield palette for Gemini alpha generation.

Replaces the naive `_read_csv_text(DATAFIELDS_CSV, max_chars=8000)` call that
truncated to the first ~80 alphabetical rows of a 5201-row CSV.

Design:
  Three diversified buckets (de-duplicated, total ≈ n fields):
    1. ~40% highest coverage  — reliable, clean-simulating fields.
    2. ~30% "undiscovered gems" — low alphas AND coverage >= floor
                                   (low-popularity / decorrelated).
    3. ~30% rotating window   — stable sort over remainder, slice offset by
                                 `seed` mod len(remainder) so each round
                                 surfaces different long-tail fields.

Filtering:
  - Always filter by region (case-insensitive match).
  - If delay given, filter by delay==str(delay); if result < n, RELAX (drop
    delay filter) so palette is never starved.
  - If universe given, prefer rows matching universe; relax similarly.

Output:
  Compact text block — one line per field:
    "<name> | <category> | cov=<coverage> | pop=<alphas>"
  Plus a 1-line header.  Bounded to n+1 lines total.

Cache:
  CSV is parsed once per mtime (same pattern as operator_catalog.py).
  The module-level counter `_rotation_counter` provides a monotonically
  incrementing seed when the caller has no round_num available.

Robustness:
  Any CSV parse/IO error returns '' — caller falls back to old behaviour.
  Never raises.
"""
from __future__ import annotations

import csv
import os
import threading
from typing import Any

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATAFIELDS_CSV = os.path.join(_THIS_DIR, 'IQC_brain_datafields.csv')

# path+mtime-keyed cache: dict mapping (path, mtime_or_None) -> list[dict]
# Keying on path prevents a test-injected file with the same mtime as the
# real CSV from returning wrong rows.
_CSV_CACHE: dict[tuple[str, float | None], list[dict]] = {}
_CSV_LOCK = threading.Lock()

# Module-level monotonic counter — fallback seed when caller has no round_num.
_rotation_counter = 0
_rotation_lock = threading.Lock()


def _next_rotation() -> int:
    """Atomically increment and return the module-level rotation counter."""
    global _rotation_counter
    with _rotation_lock:
        _rotation_counter += 1
        return _rotation_counter


def _load_csv(path: str = DATAFIELDS_CSV) -> list[dict]:
    """Parse the CSV, returning a list of row dicts.  Mtime+path-cached, thread-safe."""
    try:
        mtime: float | None = os.path.getmtime(path)
    except OSError:
        mtime = None

    cache_key = (path, mtime)
    with _CSV_LOCK:
        if cache_key in _CSV_CACHE:
            return _CSV_CACHE[cache_key]

        if mtime is None:
            # File does not exist — store empty sentinel so we don't keep retrying
            _CSV_CACHE[cache_key] = []
            return []

        rows: list[dict] = []
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    rows.append(row)
        except Exception:
            _CSV_CACHE[cache_key] = []
            return []

        _CSV_CACHE[cache_key] = rows
        return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _apply_region_filter(rows: list[dict], region: str) -> list[dict]:
    region_lc = region.lower()
    return [r for r in rows if r.get('region', '').lower() == region_lc]


def _apply_delay_filter(rows: list[dict], delay: str) -> list[dict]:
    return [r for r in rows if str(r.get('delay', '')) == delay]


def _apply_universe_filter(rows: list[dict], universe: str) -> list[dict]:
    uni_lc = universe.lower()
    return [r for r in rows if r.get('universe', '').lower() == uni_lc]


def _select_diverse(pool: list[dict], n: int, seed: int) -> list[dict]:
    """Return up to n diverse fields from pool using three-bucket strategy.

    Bucket 1 (~40%): highest coverage — reliable, clean-simulating fields.
    Bucket 2 (~30%): undiscovered gems — low alphas (popularity) AND
                     coverage >= floor — decorrelation lever.
    Bucket 3 (~30%): rotating window — stable-sorted pool sliced at
                     offset = seed % len(pool), wrapping around.
    All buckets de-duplicate by field name; later buckets fill unused quota.
    """
    if not pool:
        return []

    n = max(1, n)
    seen: set[str] = set()
    result: list[dict] = []

    def add_up_to(rows: list[dict], target: int) -> None:
        """Add unique rows from `rows` until len(result) == target (or rows exhausted)."""
        for r in rows:
            if len(result) >= target:
                return
            name = r.get('name', '')
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(r)

    # ---- Bucket 1: ~40% highest coverage --------------------------------
    b1_target = max(1, round(n * 0.40))
    by_coverage = sorted(pool, key=lambda r: _safe_int(r.get('coverage', 0)), reverse=True)
    add_up_to(by_coverage, b1_target)

    # ---- Bucket 2: ~30% undiscovered gems --------------------------------
    # coverage >= floor ensures the field actually simulates; low alphas = under-mined.
    coverage_floor = 20
    gems = [
        r for r in pool
        if _safe_int(r.get('coverage', 0)) >= coverage_floor
    ]
    # Sort: least popular first, then by coverage descending (prefer reliable low-pop)
    gems_sorted = sorted(
        gems,
        key=lambda r: (_safe_int(r.get('alphas', 0)),
                       -_safe_int(r.get('coverage', 0)))
    )
    b2_target = b1_target + max(1, round(n * 0.30))
    add_up_to(gems_sorted, b2_target)

    # ---- Bucket 3: rotating window over the whole pool ------------------
    # Stable sort by name guarantees deterministic ordering; the offset rotates
    # each round (via seed) so different long-tail fields are exposed each time.
    pool_sorted = sorted(pool, key=lambda r: r.get('name', ''))
    offset = seed % len(pool_sorted) if pool_sorted else 0
    rotated = pool_sorted[offset:] + pool_sorted[:offset]
    add_up_to(rotated, n)  # fill any remaining slots up to n

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_palette(
    region: str = 'USA',
    delay: 'str | int | None' = None,
    universe: 'str | None' = None,
    n: int = 55,
    seed: int = 0,
    _csv_path: str = DATAFIELDS_CSV,   # injectable for tests
) -> str:
    """Build a compact, diverse datafield palette for a Gemini alpha prompt.

    Parameters
    ----------
    region:   Filter rows by region (case-insensitive).  Default 'USA'.
    delay:    If given, also filter by delay==str(delay).  Relaxed if < n rows.
    universe: If given, prefer rows matching universe.    Relaxed if < n rows.
    n:        Number of fields to include.  Default 55.
    seed:     Rotating offset for bucket 3 (long-tail window).
              Pass round_num here so different rounds see different fields.

    Returns
    -------
    A compact text block bounded to n+1 lines (header + n fields),
    or '' on any error (caller falls back gracefully).
    """
    try:
        rows = _load_csv(_csv_path)
        if not rows:
            return ''

        # ---- Step 1: region filter (mandatory) --------------------------
        pool = _apply_region_filter(rows, region)
        if not pool:
            # If region matched nothing, use full set as last resort
            pool = list(rows)

        # ---- Step 2: universe filter (optional, relax if starved) -------
        universe_relaxed = False
        if universe is not None:
            uni_pool = _apply_universe_filter(pool, universe)
            if len(uni_pool) >= n:
                pool = uni_pool
            else:
                # relax — keep broader pool
                universe_relaxed = True

        # ---- Step 3: delay filter (optional, relax if starved) ----------
        delay_str: str | None = None
        delay_relaxed = False
        if delay is not None:
            delay_str = str(delay)
            delay_pool = _apply_delay_filter(pool, delay_str)
            if len(delay_pool) >= n:
                pool = delay_pool
            else:
                # relax — keep broader pool (palette never starved)
                delay_relaxed = True

        # ---- Step 4: select diverse set ---------------------------------
        selected = _select_diverse(pool, n, seed)

        if not selected:
            return ''

        # ---- Step 5: format output (compact, token-efficient) -----------
        scope_parts = [f'region={region}']
        if delay_str is not None:
            delay_token = f'delay={delay_str}(relaxed)' if delay_relaxed else f'delay={delay_str}'
            scope_parts.append(delay_token)
        if universe is not None:
            uni_token = f'universe={universe}(relaxed)' if universe_relaxed else f'universe={universe}'
            scope_parts.append(uni_token)
        scope_parts.append(f'n={len(selected)}')
        header = f'# datafields palette ({", ".join(scope_parts)})'

        lines = [header]
        for r in selected:
            name = r.get('name', '')
            category = r.get('category', '')
            cov = r.get('coverage', '')
            pop = r.get('alphas', '')
            lines.append(f'{name} | {category} | cov={cov} | pop={pop}')

        return '\n'.join(lines)

    except Exception:
        return ''
