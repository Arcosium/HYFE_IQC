"""Test the bandit slot_settings prompt injection in gemini_strategist.

Tests _format_slot_settings (the pure helper) — present / absent block,
content correctness, and None/empty guard.
Also smoke-tests that generate_strategies accepts slot_settings kwarg.
"""
import pytest


def test_format_slot_settings_contains_universe_and_neutralization():
    from server.gemini_strategist import _format_slot_settings

    slot_settings = [
        {'universe': 'TOP500', 'neutralization': 'SUBINDUSTRY', 'decay': 4},
        {'universe': 'TOP3000', 'neutralization': 'MARKET', 'decay': 1},
    ]
    block = _format_slot_settings(slot_settings)
    assert 'TOP500' in block
    assert 'SUBINDUSTRY' in block
    assert 'TOP3000' in block
    assert 'MARKET' in block


def test_format_slot_settings_contains_decay():
    from server.gemini_strategist import _format_slot_settings

    slot_settings = [{'universe': 'TOP200', 'neutralization': 'NONE', 'decay': 8}]
    block = _format_slot_settings(slot_settings)
    assert '8' in block


def test_format_slot_settings_none_returns_empty():
    from server.gemini_strategist import _format_slot_settings

    assert _format_slot_settings(None) == ''


def test_format_slot_settings_empty_list_returns_empty():
    from server.gemini_strategist import _format_slot_settings

    assert _format_slot_settings([]) == ''


def test_format_slot_settings_all_slots_listed():
    from server.gemini_strategist import _format_slot_settings

    slots = [
        {'universe': 'TOP1000', 'neutralization': 'INDUSTRY', 'decay': 4},
        {'universe': 'TOP500',  'neutralization': 'SECTOR',   'decay': 8},
        {'universe': 'TOP200',  'neutralization': 'MARKET',   'decay': 1},
    ]
    block = _format_slot_settings(slots)
    # All three universe values must appear
    for s in slots:
        assert s['universe'] in block
        assert s['neutralization'] in block


def test_format_slot_settings_block_absent_when_none():
    """When slot_settings=None, the block should not inject any universe keyword."""
    from server.gemini_strategist import _format_slot_settings

    block = _format_slot_settings(None)
    assert 'universe=' not in block


def test_build_user_prompt_cached_slot_settings_present():
    """_build_user_prompt_cached must include slot block when provided."""
    from server.gemini_strategist import _build_user_prompt_cached

    slots = [{'universe': 'TOP500', 'neutralization': 'SUBINDUSTRY', 'decay': 4}]
    prompt = _build_user_prompt_cached(
        round_num=1, feedback=[], errors=[],
        avoid_codes=[], submitted_codes=[],
        seeds=[], pref_stats={},
        slot_settings=slots,
    )
    assert 'TOP500' in prompt
    assert 'SUBINDUSTRY' in prompt


def test_build_user_prompt_cached_slot_settings_none():
    """_build_user_prompt_cached with slot_settings=None must NOT include block."""
    from server.gemini_strategist import _build_user_prompt_cached

    prompt = _build_user_prompt_cached(
        round_num=1, feedback=[], errors=[],
        avoid_codes=[], submitted_codes=[],
        seeds=[], pref_stats={},
        slot_settings=None,
    )
    assert 'universe=' not in prompt


def test_generate_strategies_accepts_slot_settings_kwarg():
    """generate_strategies must accept slot_settings as a keyword argument
    (signature test — does not call the real Gemini API)."""
    import inspect
    from server.gemini_strategist import generate_strategies

    sig = inspect.signature(generate_strategies)
    assert 'slot_settings' in sig.parameters
