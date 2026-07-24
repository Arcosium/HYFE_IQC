"""Tests for run_config.is_bandit_enabled() / set_bandit_enabled().

Isolation: monkeypatch run_config._CONFIG_PATH to a tmp file — same technique
used in test_delay_mode.py — so the real data/run_config.json is never touched.

Run: python3.11 -m pytest tests/test_run_config_bandit.py -v
"""

import json

import pytest

from server import run_config


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a fresh temp file for each test."""
    monkeypatch.setattr(run_config, '_CONFIG_PATH', str(tmp_path / 'run_config.json'))
    return run_config


# ─────────────────────────────────────────────────────────────────────────────
# is_bandit_enabled — defaults
# ─────────────────────────────────────────────────────────────────────────────

def test_bandit_enabled_default_true_when_file_absent(isolated_config):
    """No file at all → default True (learning loop ships ON)."""
    assert isolated_config.is_bandit_enabled() is True


def test_bandit_enabled_default_true_when_key_absent(isolated_config, tmp_path):
    """File exists but 'bandit_enabled' key not present → default True."""
    cfg_path = str(tmp_path / 'run_config.json')
    with open(cfg_path, 'w') as f:
        json.dump({'delay_mode': '0'}, f)
    isolated_config._CONFIG_PATH = cfg_path  # re-point (monkeypatch already in effect)
    import importlib
    # Re-read via public API — _CONFIG_PATH already monkeypatched
    assert isolated_config.is_bandit_enabled() is True


# ─────────────────────────────────────────────────────────────────────────────
# set_bandit_enabled → is_bandit_enabled round-trip
# ─────────────────────────────────────────────────────────────────────────────

def test_set_false_returns_false(isolated_config):
    isolated_config.set_bandit_enabled(False)
    assert isolated_config.is_bandit_enabled() is False


def test_set_true_returns_true(isolated_config):
    isolated_config.set_bandit_enabled(False)
    isolated_config.set_bandit_enabled(True)
    assert isolated_config.is_bandit_enabled() is True


def test_set_does_not_disturb_other_keys(isolated_config):
    """Toggling bandit_enabled must preserve other config values (e.g. constraint)."""
    isolated_config.set_constraint_text('region=USA & delay=0 & universe=TOP3000')
    isolated_config.set_bandit_enabled(False)
    assert isolated_config.get_constraint_text() == 'region=USA & delay=0 & universe=TOP3000'
    assert isolated_config.is_bandit_enabled() is False


# ─────────────────────────────────────────────────────────────────────────────
# JSON-level truth — reads written value correctly
# ─────────────────────────────────────────────────────────────────────────────

def test_json_false_literal_returns_false(isolated_config, tmp_path, monkeypatch):
    """JSON boolean false stored in file → is_bandit_enabled() == False."""
    path = str(tmp_path / 'run_config_b.json')
    monkeypatch.setattr(run_config, '_CONFIG_PATH', path)
    with open(path, 'w') as f:
        json.dump({'bandit_enabled': False}, f)
    assert run_config.is_bandit_enabled() is False


def test_json_true_literal_returns_true(isolated_config, tmp_path, monkeypatch):
    path = str(tmp_path / 'run_config_c.json')
    monkeypatch.setattr(run_config, '_CONFIG_PATH', path)
    with open(path, 'w') as f:
        json.dump({'bandit_enabled': True}, f)
    assert run_config.is_bandit_enabled() is True


def test_corrupt_file_defaults_to_true(isolated_config, tmp_path, monkeypatch):
    """Corrupt JSON → _read() returns {} → default True."""
    path = str(tmp_path / 'run_config_corrupt.json')
    monkeypatch.setattr(run_config, '_CONFIG_PATH', path)
    with open(path, 'w') as f:
        f.write('not json {{{')
    assert run_config.is_bandit_enabled() is True
