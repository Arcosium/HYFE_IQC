"""Unit tests for server.bandit — pure module, no IO, deterministic given rng.

Run: python3.11 -m pytest tests/test_bandit.py -v
"""

import random

import pytest

from server import bandit
from server.bandit import (
    DECAY_BUCKET_VALUE,
    DIMENSIONS,
    arm_key,
    arm_keys_for_assignment,
    best_value,
    decay_to_bucket,
    select_slots,
)

# ─────────────────────────────────────────────────────────────────────────────
# arm_key
# ─────────────────────────────────────────────────────────────────────────────

def test_arm_key_format():
    assert arm_key('universe', 'TOP500') == 'universe:TOP500'


def test_arm_key_other_dimensions():
    assert arm_key('neutralization', 'MARKET') == 'neutralization:MARKET'
    assert arm_key('decay_bucket', 'mid') == 'decay_bucket:mid'


# ─────────────────────────────────────────────────────────────────────────────
# best_value
# ─────────────────────────────────────────────────────────────────────────────

def test_best_value_picks_highest_mean():
    stats = {
        'universe:TOP3000': 0.1,
        'universe:TOP1000': 0.5,
        'universe:TOP500':  0.9,
        'universe:TOP200':  0.3,
    }
    assert best_value('universe', stats) == 'TOP500'


def test_best_value_unseen_returns_first_default():
    # No stats at all → first value in DIMENSIONS['universe']
    assert best_value('universe', {}) == DIMENSIONS['universe'][0]


def test_best_value_partially_unseen_picks_among_seen():
    # Only TOP200 has a stat; others unseen → TOP200 wins
    stats = {'universe:TOP200': 0.7}
    assert best_value('universe', stats) == 'TOP200'


def test_best_value_tie_stable_first():
    # All equal → should return first seen with that mean (first value in list)
    stats = {
        'universe:TOP3000': 0.5,
        'universe:TOP1000': 0.5,
        'universe:TOP500':  0.5,
        'universe:TOP200':  0.5,
    }
    # best_value iterates DIMENSIONS[dim] in order; TOP3000 is first, so it wins ties
    assert best_value('universe', stats) == DIMENSIONS['universe'][0]


def test_best_value_neutralization():
    stats = {
        'neutralization:NONE':        0.1,
        'neutralization:MARKET':      0.2,
        'neutralization:INDUSTRY':    0.8,
        'neutralization:SUBINDUSTRY': 0.5,
        'neutralization:SECTOR':      0.3,
    }
    assert best_value('neutralization', stats) == 'INDUSTRY'


def test_best_value_unknown_dimension_raises():
    with pytest.raises(KeyError):
        best_value('nonexistent_dim', {})


# ─────────────────────────────────────────────────────────────────────────────
# select_slots — shape / values
# ─────────────────────────────────────────────────────────────────────────────

def test_select_slots_returns_correct_count():
    result = select_slots({}, n_slots=10, explore_slots=3, rng=random.Random(0))
    assert len(result) == 10


def test_select_slots_all_keys_present():
    result = select_slots({}, n_slots=5, rng=random.Random(42))
    for assignment in result:
        assert 'universe' in assignment
        assert 'neutralization' in assignment
        assert 'decay_bucket' in assignment
        assert 'decay' in assignment


def test_select_slots_universe_values_valid():
    result = select_slots({}, n_slots=20, rng=random.Random(1))
    for assignment in result:
        assert assignment['universe'] in DIMENSIONS['universe']


def test_select_slots_neutralization_values_valid():
    result = select_slots({}, n_slots=20, rng=random.Random(1))
    for assignment in result:
        assert assignment['neutralization'] in DIMENSIONS['neutralization']


def test_select_slots_decay_values_valid():
    result = select_slots({}, n_slots=20, rng=random.Random(1))
    valid_decay_values = set(DECAY_BUCKET_VALUE.values())
    for assignment in result:
        assert assignment['decay'] in valid_decay_values


def test_select_slots_decay_matches_bucket():
    result = select_slots({}, n_slots=20, rng=random.Random(7))
    for assignment in result:
        assert assignment['decay'] == DECAY_BUCKET_VALUE[assignment['decay_bucket']]


# ─────────────────────────────────────────────────────────────────────────────
# select_slots — determinism
# ─────────────────────────────────────────────────────────────────────────────

def test_select_slots_deterministic_given_seed():
    result_a = select_slots({}, n_slots=10, explore_slots=3, rng=random.Random(0))
    result_b = select_slots({}, n_slots=10, explore_slots=3, rng=random.Random(0))
    assert result_a == result_b


def test_select_slots_different_seeds_differ():
    result_a = select_slots({}, n_slots=10, rng=random.Random(0))
    result_b = select_slots({}, n_slots=10, rng=random.Random(9999))
    # With high probability, at least one slot differs
    assert result_a != result_b


# ─────────────────────────────────────────────────────────────────────────────
# select_slots — explore vs exploit
# ─────────────────────────────────────────────────────────────────────────────

def test_explore_slots_are_independent_of_stats():
    """With strong stats favouring TOP500, explore slots should still vary."""
    stats = {f'universe:{v}': (0.9 if v == 'TOP500' else 0.0)
             for v in DIMENSIONS['universe']}
    # Run many times — explore slots should NOT always be TOP500
    universes_in_explore = set()
    for seed in range(30):
        result = select_slots(stats, n_slots=5, explore_slots=3,
                              epsilon=0.0, rng=random.Random(seed))
        for slot in result[:3]:
            universes_in_explore.add(slot['universe'])
    # At least 2 distinct universe values should appear across explore slots
    assert len(universes_in_explore) >= 2


def test_exploit_epsilon_zero_picks_best():
    """With epsilon=0 and strong stats, all exploit slots should pick TOP500."""
    stats = {f'universe:{v}': (0.9 if v == 'TOP500' else 0.0)
             for v in DIMENSIONS['universe']}
    result = select_slots(stats, n_slots=10, explore_slots=0,
                          epsilon=0.0, rng=random.Random(0))
    for assignment in result:
        assert assignment['universe'] == 'TOP500'


def test_exploit_epsilon_one_is_all_random():
    """With epsilon=1.0, all slots should explore (random choices)."""
    stats = {f'universe:{v}': (0.9 if v == 'TOP500' else 0.0)
             for v in DIMENSIONS['universe']}
    universes_seen = set()
    for seed in range(40):
        result = select_slots(stats, n_slots=5, explore_slots=0,
                              epsilon=1.0, rng=random.Random(seed))
        for assignment in result:
            universes_seen.add(assignment['universe'])
    assert len(universes_seen) >= 2, 'epsilon=1.0 should produce diverse exploration'


def test_explore_slots_10_3_structure():
    """Full scenario: 10 slots, 3 explore, strong stats for TOP500 & epsilon=0."""
    stats = {f'universe:{v}': (0.9 if v == 'TOP500' else 0.0)
             for v in DIMENSIONS['universe']}
    result = select_slots(stats, n_slots=10, explore_slots=3,
                          epsilon=0.0, rng=random.Random(0))
    assert len(result) == 10
    # Exploit slots (3..9) must all be TOP500
    for assignment in result[3:]:
        assert assignment['universe'] == 'TOP500'


# ─────────────────────────────────────────────────────────────────────────────
# arm_keys_for_assignment
# ─────────────────────────────────────────────────────────────────────────────

def test_arm_keys_for_assignment_returns_three():
    assignment = {
        'universe': 'TOP1000',
        'neutralization': 'SECTOR',
        'decay_bucket': 'mid',
        'decay': 4,
    }
    keys = arm_keys_for_assignment(assignment)
    assert len(keys) == 3


def test_arm_keys_for_assignment_correct_keys():
    assignment = {
        'universe': 'TOP1000',
        'neutralization': 'SECTOR',
        'decay_bucket': 'mid',
        'decay': 4,
    }
    keys = arm_keys_for_assignment(assignment)
    assert 'universe:TOP1000' in keys
    assert 'neutralization:SECTOR' in keys
    assert 'decay_bucket:mid' in keys


def test_arm_keys_for_assignment_dimension_prefix_matches_db():
    """arm_key format must be '{dimension}:{value}' so db.bandit_update is consistent."""
    assignment = {
        'universe': 'TOP500',
        'neutralization': 'MARKET',
        'decay_bucket': 'low',
        'decay': 1,
    }
    keys = arm_keys_for_assignment(assignment)
    for k in keys:
        dim, val = k.split(':', 1)
        assert dim in DIMENSIONS
        assert val in DIMENSIONS[dim]


# ─────────────────────────────────────────────────────────────────────────────
# decay_to_bucket
# ─────────────────────────────────────────────────────────────────────────────

def test_decay_to_bucket_low():
    """0, 1, 2 all map to 'low'."""
    for v in (0, 1, 2):
        assert decay_to_bucket(v) == 'low', f'expected low for {v}'


def test_decay_to_bucket_mid():
    """3, 4, 6 map to 'mid'."""
    for v in (3, 4, 6):
        assert decay_to_bucket(v) == 'mid', f'expected mid for {v}'


def test_decay_to_bucket_high():
    """7, 8, 10 map to 'high'."""
    for v in (7, 8, 10):
        assert decay_to_bucket(v) == 'high', f'expected high for {v}'


def test_decay_to_bucket_none_returns_low():
    assert decay_to_bucket(None) == 'low'


def test_decay_to_bucket_string_x_returns_low():
    assert decay_to_bucket('x') == 'low'


def test_decay_to_bucket_string_int():
    """String integers should be coerced correctly."""
    assert decay_to_bucket('2') == 'low'
    assert decay_to_bucket('5') == 'mid'
    assert decay_to_bucket('9') == 'high'


def test_decay_to_bucket_boundary_3():
    assert decay_to_bucket(3) == 'mid'


def test_decay_to_bucket_boundary_6():
    assert decay_to_bucket(6) == 'mid'


def test_decay_to_bucket_boundary_7():
    assert decay_to_bucket(7) == 'high'


# ─────────────────────────────────────────────────────────────────────────────
# _has_signal
# ─────────────────────────────────────────────────────────────────────────────

def test_has_signal_empty_stats_is_false():
    from server.bandit import _has_signal
    assert _has_signal('universe', {}) is False


def test_has_signal_one_arm_is_false():
    from server.bandit import _has_signal
    assert _has_signal('universe', {'universe:TOP500': 0.5}) is False


def test_has_signal_full_tie_is_false():
    from server.bandit import _has_signal
    stats = {f'universe:{v}': 0.5 for v in DIMENSIONS['universe']}
    assert _has_signal('universe', stats) is False


def test_has_signal_two_differing_arms_is_true():
    from server.bandit import _has_signal
    stats = {'universe:TOP500': 0.9, 'universe:TOP3000': 0.1}
    assert _has_signal('universe', stats) is True


# ─────────────────────────────────────────────────────────────────────────────
# select_slots — cold-start diversity & signal exploitation (new)
# ─────────────────────────────────────────────────────────────────────────────

def test_exploit_cold_start_is_diverse():
    """With empty stats, epsilon=0, explore_slots=0, exploit slots must NOT all
    collapse to the same default config — cold-start random must produce >1
    distinct universe value across 20 slots.
    """
    result = select_slots({}, n_slots=20, explore_slots=0,
                          epsilon=0.0, rng=random.Random(0))
    universes = {a['universe'] for a in result}
    assert len(universes) > 1, (
        f'Cold-start exploit should be diverse, got only: {universes}'
    )


def test_exploit_uses_signal_when_present():
    """With strong signal for TOP500 (universe) and no signal for neutralization,
    epsilon=0: universe should be predominantly TOP500 (signal exploited) while
    neutralization should show >1 distinct value (random because no signal).
    """
    stats = {
        'universe:TOP500':  0.9,
        'universe:TOP3000': 0.0,
    }
    result = select_slots(stats, n_slots=30, explore_slots=0,
                          epsilon=0.0, rng=random.Random(0))

    universes = [a['universe'] for a in result]
    neutralizations = {a['neutralization'] for a in result}

    # Signal present → should predominantly pick TOP500
    top500_count = universes.count('TOP500')
    assert top500_count >= 28, (
        f'Expected >=28/30 TOP500 when signal present, got {top500_count}'
    )
    # No signal for neutralization → cold-start random → diverse
    assert len(neutralizations) > 1, (
        f'Expected diverse neutralization with no signal, got: {neutralizations}'
    )
