"""Pure bandit-selection module — no IO, no DB imports.

Per-dimension independent epsilon-greedy bandits for selecting alpha settings.
The arm space stays small (per-dimension, not cross-product) for fast convergence
under low sim throughput.

arm_key convention: '{dimension}:{value}'  (matches db.bandit_update fields)

Usage:
    from server import bandit

    # Build stats_by_key from db.bandit_stats() output:
    stats = {row['arm_key']: row['mean'] for row in db.bandit_stats(user_id)}

    assignments = bandit.select_slots(stats, n_slots=10, explore_slots=3)
    # → list of 10 dicts, each {'universe':..., 'neutralization':..., 'decay':<int>, 'decay_bucket':...}

    # After alpha completes, get arm_keys to credit:
    for k in bandit.arm_keys_for_assignment(assignment):
        db.bandit_update(user_id, k, reward, round_num, dimension=k.split(':')[0])
"""

from __future__ import annotations

import random as _random_module
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Dimension definitions
# ─────────────────────────────────────────────────────────────────────────────

DIMENSIONS: dict[str, list[str]] = {
    'universe':      ['TOP3000', 'TOP1000', 'TOP500', 'TOP200'],
    'neutralization': ['NONE', 'MARKET', 'INDUSTRY', 'SUBINDUSTRY', 'SECTOR'],
    'decay_bucket':  ['low', 'mid', 'high'],
}

DECAY_BUCKET_VALUE: dict[str, int] = {
    'low': 1,
    'mid': 4,
    'high': 8,
}

# ─────────────────────────────────────────────────────────────────────────────
# Core helpers
# ─────────────────────────────────────────────────────────────────────────────

def arm_key(dimension: str, value: str) -> str:
    """Return the canonical arm key string: '{dimension}:{value}'."""
    return f'{dimension}:{value}'


def best_value(dimension: str, stats_by_key: dict[str, float]) -> str:
    """Return the value with the highest mean for *dimension*.

    stats_by_key: {arm_key: mean}
    Ties / unseen arms → first value in DIMENSIONS[dimension] (stable default).
    Raises KeyError if dimension is unknown.
    """
    values = DIMENSIONS[dimension]
    best_val = values[0]          # stable default
    best_mean: float | None = None

    for v in values:
        k = arm_key(dimension, v)
        if k in stats_by_key:
            m = stats_by_key[k]
            if best_mean is None or m > best_mean:
                best_mean = m
                best_val = v

    return best_val


def _has_signal(dimension: str, stats_by_key: dict[str, float]) -> bool:
    """Return True iff *dimension* has at least two arms present in stats_by_key
    with differing means — i.e. there is genuine exploitable information.

    False when:
    - no arms for the dimension appear in stats_by_key (cold start), OR
    - only one arm is present (can't distinguish), OR
    - all present arms share the same mean (tie — no preference signal).
    """
    values = DIMENSIONS[dimension]
    seen_means: list[float] = []
    for v in values:
        k = arm_key(dimension, v)
        if k in stats_by_key:
            seen_means.append(stats_by_key[k])
    if len(seen_means) < 2:
        return False
    return len(set(seen_means)) > 1


def select_slots(
    stats_by_key: dict[str, float],
    *,
    n_slots: int,
    epsilon: float = 0.2,
    explore_slots: int = 0,
    rng: _random_module.Random | None = None,
) -> list[dict[str, Any]]:
    """Return n_slots assignment dicts, each with keys:
        'universe', 'neutralization', 'decay_bucket', 'decay' (int).

    The first *explore_slots* are EXPLORE: each dimension value chosen
    uniformly at random — ensures a round never collapses onto one config.

    Remaining slots are EXPLOIT: per dimension, with prob (1-epsilon) pick
    best_value(dimension, stats), else a random value (epsilon-greedy).

    rng: a random.Random instance for deterministic testing.
         Defaults to the module-level random functions (non-deterministic).
    """
    _rng: _random_module.Random = rng if rng is not None else _random_module  # type: ignore[assignment]

    assignments: list[dict[str, Any]] = []

    for slot_idx in range(n_slots):
        assignment: dict[str, Any] = {}
        is_explore = slot_idx < explore_slots

        for dim in ('universe', 'neutralization', 'decay_bucket'):
            values = DIMENSIONS[dim]
            if is_explore:
                chosen = _rng.choice(values)
            else:
                if not _has_signal(dim, stats_by_key):
                    # Cold start (no arms seen) or full tie: pick uniformly at
                    # random so the batch stays diverse until data accumulates.
                    chosen = _rng.choice(values)
                elif _rng.random() < epsilon:
                    chosen = _rng.choice(values)
                else:
                    chosen = best_value(dim, stats_by_key)
            assignment[dim] = chosen

        assignment['decay'] = DECAY_BUCKET_VALUE[assignment['decay_bucket']]
        assignments.append(assignment)

    return assignments


def decay_to_bucket(decay) -> str:
    """Map a decay value back to its bucket label.

    int(decay) <= 2           → 'low'
    3 <= int(decay) <= 6      → 'mid'
    int(decay) >= 7           → 'high'
    non-int / None / invalid  → 'low'

    Inverse of DECAY_BUCKET_VALUE for crediting an alpha's actual decay
    to the right arm after simulation.
    """
    try:
        d = int(decay)
    except (TypeError, ValueError):
        return 'low'
    if d <= 2:
        return 'low'
    if d <= 6:
        return 'mid'
    return 'high'


def arm_keys_for_assignment(assignment: dict[str, Any]) -> list[str]:
    """Return the 3 arm_keys (universe / neutralization / decay_bucket) implied
    by an assignment dict, so the caller can call db.bandit_update on each after
    the alpha's reward is known (credit assignment per dimension).
    """
    return [
        arm_key('universe',      assignment['universe']),
        arm_key('neutralization', assignment['neutralization']),
        arm_key('decay_bucket',  assignment['decay_bucket']),
    ]
