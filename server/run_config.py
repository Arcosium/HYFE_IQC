"""라이브 런타임 설정 — UI(대시보드)에서 바꾸고 워커가 매 라운드 새로 읽는다.

현재 항목: delay 테스트 모드.
  '0'   → 모든 라운드 delay=0 강제
  '1'   → 모든 라운드 delay=1 강제
  'mix' → 라운드마다 0/1 랜덤 (한 라운드 전체는 단일 delay)

`data/run_config.json` 에 저장 → 서버 재시작 없이 반영(워커가 라운드 시작 때 get).
모듈 import 한 server.py 코드 변경분만 재시작 필요하고, 이 값 자체는 파일이라 즉시.
"""

from __future__ import annotations

import json
import os
import random
import threading

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.abspath(os.path.join(_THIS_DIR, '..', 'data', 'run_config.json'))

_LOCK = threading.Lock()

VALID_DELAY_MODES = ('0', '1', 'mix')
DEFAULT_DELAY_MODE = 'mix'


def _read() -> dict:
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
    tmp = _CONFIG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CONFIG_PATH)


def get_delay_mode() -> str:
    """'0' | '1' | 'mix' — 잘못된/없는 값이면 default('mix')."""
    with _LOCK:
        mode = str(_read().get('delay_mode', '')).strip().lower()
    return mode if mode in VALID_DELAY_MODES else DEFAULT_DELAY_MODE


def set_delay_mode(mode: str) -> str:
    """mode 검증 후 저장. 반환=저장된 값. 잘못된 값이면 ValueError."""
    mode = str(mode or '').strip().lower()
    if mode not in VALID_DELAY_MODES:
        raise ValueError(f'invalid delay_mode: {mode!r} (allowed: {VALID_DELAY_MODES})')
    with _LOCK:
        data = _read()
        data['delay_mode'] = mode
        _write(data)
    return mode


def is_bandit_enabled() -> bool:
    """True if the bandit learning-loop is enabled (default: True).

    Reads 'bandit_enabled' from data/run_config.json — live-editable without
    a server restart, exactly like get_delay_mode().
    Absent / non-bool values default to True so the loop ships ON.
    """
    with _LOCK:
        val = _read().get('bandit_enabled', None)
    if val is None:
        return True
    # Accept both JSON bool (True/False) and string representations.
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ('false', '0', 'no', 'off')


def set_bandit_enabled(enabled: bool) -> None:
    """Persist the bandit_enabled flag.  No restart required."""
    with _LOCK:
        data = _read()
        data['bandit_enabled'] = bool(enabled)
        _write(data)


def resolve_round_delay(mode: str | None = None) -> str:
    """이 라운드에 강제할 delay 값('0'|'1') 을 산출.
    '0'/'1' 은 그대로, 'mix' 는 라운드 단위로 0/1 랜덤.
    """
    mode = (mode if mode is not None else get_delay_mode())
    if mode == '0':
        return '0'
    if mode == '1':
        return '1'
    return random.choice(('0', '1'))   # mix — 라운드별 랜덤
