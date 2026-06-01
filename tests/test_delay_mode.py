"""delay 테스트 모드 (run_config) + Gemini delay 지시문 단위 테스트.

실행: python3.11 -m pytest tests/test_delay_mode.py
"""
import importlib

import pytest

from server import run_config
from server import gemini_strategist


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    # 실제 data/run_config.json 을 건드리지 않도록 임시 경로로 격리.
    monkeypatch.setattr(run_config, '_CONFIG_PATH', str(tmp_path / 'run_config.json'))
    return run_config


def test_default_is_mix(isolated_config):
    # 파일이 없으면 default 'mix'.
    assert isolated_config.get_delay_mode() == 'mix'


@pytest.mark.parametrize('mode', ['0', '1', 'mix'])
def test_set_get_roundtrip(isolated_config, mode):
    isolated_config.set_delay_mode(mode)
    assert isolated_config.get_delay_mode() == mode


def test_set_invalid_raises(isolated_config):
    with pytest.raises(ValueError):
        isolated_config.set_delay_mode('2')
    with pytest.raises(ValueError):
        isolated_config.set_delay_mode('')


def test_corrupt_file_falls_back_to_default(isolated_config):
    with open(isolated_config._CONFIG_PATH, 'w', encoding='utf-8') as f:
        f.write('not json {{{')
    assert isolated_config.get_delay_mode() == 'mix'


def test_resolve_fixed_modes(isolated_config):
    assert isolated_config.resolve_round_delay('0') == '0'
    assert isolated_config.resolve_round_delay('1') == '1'


def test_resolve_mix_is_zero_or_one(isolated_config):
    seen = {isolated_config.resolve_round_delay('mix') for _ in range(50)}
    assert seen <= {'0', '1'}
    assert seen  # 비어있지 않음


def test_delay_directive_none_is_empty():
    assert gemini_strategist._delay_directive(None) == ''


def test_delay_directive_zero_mentions_pv_and_zero():
    d = gemini_strategist._delay_directive('0')
    assert 'delay=0' in d.lower() or 'delay 강제 = 0' in d.lower()
    # delay=0 호환 PV 필드 유도 문구가 들어가야 한다.
    assert 'vwap' in d.lower() and 'volume' in d.lower()


def test_delay_directive_one_allows_fundamental():
    d = gemini_strategist._delay_directive('1')
    assert '1' in d
    assert 'fundamental' in d.lower()
