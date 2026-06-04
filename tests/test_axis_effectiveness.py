"""Tests for db.axis_effectiveness, db.operator_effectiveness, db.round_reward_trend.

Uses a tmp SQLite DB (monkeypatches db.DB_PATH) to avoid touching production data.
"""
import sqlite3
import time

import pytest

from server import db


# ─── Fixture ─────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'ae_test.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    yield tmp_path, tmp_db
    db._INITIALIZED = False


def _user(tmp_db: str) -> int:
    return db.upsert_user('ae_test_user', 'pw', 'FAKE_GEMINI_KEY')


def _make_alpha(uid, rid, round_num, idx, code, *,
                universe='TOP3000', neutralization='INDUSTRY',
                decay=4, region='USA',
                pass_count=0, fail_count=0, error_count=0,
                sharpe=None, self_corr=None):
    """Insert a minimal alpha row via db.insert_alpha."""
    metrics = {}
    if sharpe is not None:
        metrics['sharpe'] = str(sharpe)
    alpha = {
        'idx': idx,
        'code': code,
        'desc': '',
        'pass_items': [],
        'fail_items': [],
        'error_text': '',
        'metrics': metrics,
        'mode': '',
        'cached': False,
        'submitted': False,
        'submit_status': '',
        'error_count': error_count,
        'pending_count': 0,
        'phase': 0,
        'is_status': {},
        'settings': {
            'universe': universe,
            'neutralization': neutralization,
            'decay': str(decay),
            'region': region,
        },
        'delay': '1',
        # override counts directly (is_status empty, so these win)
        'pass_count': pass_count,
        'fail_count': fail_count,
    }
    if self_corr is not None:
        alpha['self_corr'] = str(self_corr)
    db.insert_alpha(uid, rid, round_num, alpha)


# ─────────────────────────────────────────────────────────────────────────────
# axis_effectiveness — universe
# ─────────────────────────────────────────────────────────────────────────────

def test_axis_effectiveness_universe_sorted_by_pass_rate(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # 2 all-pass (pass_count=7, fail=0, error=0) for TOP500
    _make_alpha(uid, rid, 1, 1, 'rank(returns)', universe='TOP500',
                pass_count=7, fail_count=0, error_count=0)
    _make_alpha(uid, rid, 1, 2, 'rank(close)', universe='TOP500',
                pass_count=7, fail_count=0, error_count=0)
    # 0 all-pass for TOP3000 (fail_count>0)
    _make_alpha(uid, rid, 1, 3, 'rank(volume)', universe='TOP3000',
                pass_count=7, fail_count=1, error_count=0)
    _make_alpha(uid, rid, 1, 4, 'rank(cap)', universe='TOP3000',
                pass_count=6, fail_count=0, error_count=0)

    results = db.axis_effectiveness(uid, 'universe', lookback_rounds=20)

    assert len(results) >= 2
    # TOP500 should be first (rate = 1.0)
    top = results[0]
    assert top['value'] == 'TOP500'
    assert top['all_pass_rate'] == pytest.approx(1.0)
    assert top['count'] == 2

    # TOP3000 rate should be 0.0 (no all_pass)
    top3000 = next(r for r in results if r['value'] == 'TOP3000')
    assert top3000['all_pass_rate'] == pytest.approx(0.0)

    # sorted descending by all_pass_rate
    rates = [r['all_pass_rate'] for r in results]
    assert rates == sorted(rates, reverse=True)


def test_axis_effectiveness_neutralization(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    _make_alpha(uid, rid, 1, 1, 'rank(returns)', neutralization='SUBINDUSTRY',
                pass_count=7, fail_count=0, error_count=0)
    _make_alpha(uid, rid, 1, 2, 'rank(close)', neutralization='SUBINDUSTRY',
                pass_count=7, fail_count=0, error_count=0)
    _make_alpha(uid, rid, 1, 3, 'rank(volume)', neutralization='MARKET',
                pass_count=5, fail_count=1, error_count=0)

    results = db.axis_effectiveness(uid, 'neutralization', lookback_rounds=20)
    assert any(r['value'] == 'SUBINDUSTRY' for r in results)
    sub = next(r for r in results if r['value'] == 'SUBINDUSTRY')
    assert sub['all_pass_rate'] == pytest.approx(1.0)
    assert sub['count'] == 2


def test_axis_effectiveness_decay_buckets(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # decay=1 → 'low', 2 all-pass
    _make_alpha(uid, rid, 1, 1, 'rank(returns)', decay=1,
                pass_count=7, fail_count=0, error_count=0)
    _make_alpha(uid, rid, 1, 2, 'rank(close)', decay=2,
                pass_count=7, fail_count=0, error_count=0)
    # decay=4 → 'mid', 0 all-pass
    _make_alpha(uid, rid, 1, 3, 'rank(volume)', decay=4,
                pass_count=6, fail_count=1, error_count=0)

    results = db.axis_effectiveness(uid, 'decay', lookback_rounds=20)
    assert any(r['value'] == 'low' for r in results)
    low_bkt = next(r for r in results if r['value'] == 'low')
    assert low_bkt['all_pass_rate'] == pytest.approx(1.0)
    assert low_bkt['count'] == 2


def test_axis_effectiveness_tolerates_null_rows(isolated_db):
    """Rows where the axis column is NULL (legacy) must be ignored."""
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # Insert alpha with no settings → universe/neutralization/decay = NULL
    alpha_no_settings = {
        'idx': 1,
        'code': 'rank(returns)',
        'desc': '',
        'pass_items': [],
        'fail_items': [],
        'error_text': '',
        'metrics': {},
        'mode': '',
        'cached': False,
        'submitted': False,
        'submit_status': '',
        'error_count': 0,
        'pending_count': 0,
        'phase': 0,
        'is_status': {},
        'pass_count': 7,
        'fail_count': 0,
    }
    db.insert_alpha(uid, rid, 1, alpha_no_settings)

    # Should return empty list (no rows with non-NULL universe)
    results = db.axis_effectiveness(uid, 'universe', lookback_rounds=20)
    assert results == []


def test_axis_effectiveness_avg_sharpe_and_self_corr(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    _make_alpha(uid, rid, 1, 1, 'rank(returns)', universe='TOP1000',
                pass_count=7, fail_count=0, sharpe=1.5, self_corr=0.3)
    _make_alpha(uid, rid, 1, 2, 'rank(close)', universe='TOP1000',
                pass_count=7, fail_count=0, sharpe=1.1, self_corr=0.5)

    results = db.axis_effectiveness(uid, 'universe', lookback_rounds=20)
    top = next(r for r in results if r['value'] == 'TOP1000')
    assert top['avg_sharpe'] == pytest.approx(1.3)
    assert top['avg_self_corr'] == pytest.approx(0.4)


def test_axis_effectiveness_invalid_axis_returns_empty(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    results = db.axis_effectiveness(uid, 'invalid_axis', lookback_rounds=20)
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# operator_effectiveness
# ─────────────────────────────────────────────────────────────────────────────

def test_operator_effectiveness_groups_by_outermost_op(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # 3 x rank (2 all-pass)
    _make_alpha(uid, rid, 1, 1, 'rank(returns)', pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 2, 'rank(close)', pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 3, 'rank(volume)', pass_count=5, fail_count=1)

    # 2 x ts_rank (0 all-pass)
    _make_alpha(uid, rid, 1, 4, 'ts_rank(returns, 20)', pass_count=6, fail_count=1)
    _make_alpha(uid, rid, 1, 5, 'ts_rank(close, 10)', pass_count=5, fail_count=1)

    results = db.operator_effectiveness(uid, lookback_alphas=200, top_n=8)

    assert len(results) >= 2

    rank_row = next((r for r in results if r['operator'] == 'rank'), None)
    assert rank_row is not None
    assert rank_row['count'] == 3
    assert rank_row['all_pass_rate'] == pytest.approx(2 / 3)

    tsrank_row = next((r for r in results if r['operator'] == 'ts_rank'), None)
    assert tsrank_row is not None
    assert tsrank_row['all_pass_rate'] == pytest.approx(0.0)


def test_operator_effectiveness_sorted_by_pass_rate_then_avg(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # group_neutralize: 2 all-pass (rate=1.0)
    _make_alpha(uid, rid, 1, 1, 'group_neutralize(returns, sector)',
                pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 2, 'group_neutralize(close, industry)',
                pass_count=7, fail_count=0)

    # rank: partial pass (rate < 1.0)
    _make_alpha(uid, rid, 1, 3, 'rank(returns)', pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 4, 'rank(close)', pass_count=3, fail_count=1)

    results = db.operator_effectiveness(uid, lookback_alphas=200, top_n=8)
    assert len(results) >= 2
    # group_neutralize should rank first
    assert results[0]['operator'] == 'group_neutralize'


def test_operator_effectiveness_min_count_2(isolated_db):
    """Operators appearing only once should be excluded."""
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # Only 1 occurrence — should be excluded
    _make_alpha(uid, rid, 1, 1, 'zscore(returns)', pass_count=7, fail_count=0)
    # 2 occurrences — should be included
    _make_alpha(uid, rid, 1, 2, 'rank(returns)', pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 3, 'rank(close)', pass_count=7, fail_count=0)

    results = db.operator_effectiveness(uid, lookback_alphas=200, top_n=8)
    ops = {r['operator'] for r in results}
    assert 'rank' in ops
    assert 'zscore' not in ops


def test_operator_effectiveness_top_n_limit(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # Insert 6 distinct operators with >=2 occurrences each
    ops = ['rank', 'ts_rank', 'zscore', 'group_neutralize', 'signed_power', 'scale']
    for i, op in enumerate(ops):
        for j in range(2):
            _make_alpha(uid, rid, 1, i * 10 + j,
                        f'{op}(returns)', pass_count=5, fail_count=0)

    results = db.operator_effectiveness(uid, lookback_alphas=200, top_n=3)
    assert len(results) <= 3


def test_operator_effectiveness_skips_no_outermost_op(isolated_db):
    """Code with no single outermost operator (e.g. arithmetic) should be skipped."""
    _, tmp_db = isolated_db
    uid = _user(tmp_db)
    rid = db.start_round(uid, 1)

    # Plain arithmetic — outermost_operator returns None
    _make_alpha(uid, rid, 1, 1, 'returns + volume', pass_count=7, fail_count=0)
    _make_alpha(uid, rid, 1, 2, 'close - open', pass_count=7, fail_count=0)

    results = db.operator_effectiveness(uid, lookback_alphas=200, top_n=8)
    # Both should be skipped (no outermost op), so result is empty
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# round_reward_trend
# ─────────────────────────────────────────────────────────────────────────────

def _insert_round_alphas(uid, round_num, avg_pass, n=3):
    """Insert n alphas with the given average pass_count for a round."""
    rid = db.start_round(uid, round_num)
    for i in range(n):
        _make_alpha(uid, rid, round_num, i + 1, f'rank(returns_{round_num}_{i})',
                    pass_count=avg_pass, fail_count=0)
    return rid


def test_round_reward_trend_positive_for_improving(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    # Rounds with increasing avg pass_count: 2, 4, 6, 8
    for rn, avg_pc in enumerate([2, 4, 6, 8], start=1):
        _insert_round_alphas(uid, rn, avg_pc)

    slope = db.round_reward_trend(uid, window=10)
    assert slope > 0, f"Expected positive slope for improving rounds, got {slope}"


def test_round_reward_trend_negative_for_declining(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    # Rounds with decreasing avg pass_count: 8, 6, 4, 2
    for rn, avg_pc in enumerate([8, 6, 4, 2], start=1):
        _insert_round_alphas(uid, rn, avg_pc)

    slope = db.round_reward_trend(uid, window=10)
    assert slope < 0, f"Expected negative slope for declining rounds, got {slope}"


def test_round_reward_trend_zero_for_flat(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    # All rounds have same avg pass_count
    for rn in range(1, 5):
        _insert_round_alphas(uid, rn, avg_pass=5)

    slope = db.round_reward_trend(uid, window=10)
    assert abs(slope) < 1e-9, f"Expected ~0 slope for flat rounds, got {slope}"


def test_round_reward_trend_returns_zero_for_single_round(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    _insert_round_alphas(uid, 1, avg_pass=7)

    slope = db.round_reward_trend(uid, window=10)
    assert slope == 0.0


def test_round_reward_trend_returns_zero_for_no_data(isolated_db):
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    slope = db.round_reward_trend(uid, window=10)
    assert slope == 0.0


def test_round_reward_trend_window_limits_lookback(isolated_db):
    """window=3 should only use last 3 rounds."""
    _, tmp_db = isolated_db
    uid = _user(tmp_db)

    # Rounds 1-3: decreasing (8,6,4), Rounds 4-6: increasing (2,5,8)
    for rn, avg_pc in [(1, 8), (2, 6), (3, 4), (4, 2), (5, 5), (6, 8)]:
        _insert_round_alphas(uid, rn, avg_pc)

    # With window=3, only rounds 4,5,6 are used → increasing → positive slope
    slope = db.round_reward_trend(uid, window=3)
    assert slope > 0, f"Expected positive slope using last 3 improving rounds, got {slope}"
