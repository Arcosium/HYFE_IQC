"""라운드 delay 결정 (run_config) + Gemini delay 지시문 단위 테스트.

실행: python3.12 -m pytest tests/test_delay_mode.py

⚠ 2026-07-22: 별도의 'delay 테스트 모드'(0/1/mix 토글, `/api/delay_mode`)는 제거됐다.
delay 는 **탐색 조건 하나**가 정한다 — 같은 값을 두 곳에서 정하면 조건을 걸어놓고도
다른 delay 로 라운드가 도는 사고가 난다(실제로 조건 delay=1 인데 토글은 '0' 이었다).
"""
import pytest

from server import run_config
from server import constraint_spec
from server import gemini_strategist


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    # 실제 data/run_config.json 을 건드리지 않도록 임시 경로로 격리.
    monkeypatch.setattr(run_config, '_CONFIG_PATH', str(tmp_path / 'run_config.json'))
    return run_config


def test_delay_mode_api_is_gone():
    """옛 토글이 되살아나면(두 진실) 조건과 어긋난 delay 로 시뮬을 버리게 된다."""
    for gone in ('get_delay_mode', 'set_delay_mode', 'resolve_round_delay',
                 'VALID_DELAY_MODES', 'DEFAULT_DELAY_MODE'):
        assert not hasattr(run_config, gone), f'{gone} 이 아직 살아 있다'


def test_default_delay_when_no_constraint(isolated_config):
    assert isolated_config.round_delay(None) == isolated_config.DEFAULT_DELAY


@pytest.mark.parametrize('delay', ['0', '1'])
def test_constraint_decides_delay(isolated_config, delay):
    spec = constraint_spec.parse(f'region=USA & delay={delay} & universe=TOP1000')
    assert spec.delay == delay
    assert isolated_config.round_delay(spec) == delay


def test_constraint_without_delay_falls_back(isolated_config):
    spec = constraint_spec.parse('region=USA & universe=TOP3000')
    assert spec.delay is None
    assert isolated_config.round_delay(spec) == isolated_config.DEFAULT_DELAY


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
