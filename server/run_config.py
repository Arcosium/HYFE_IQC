"""라이브 런타임 설정 — UI(대시보드)에서 바꾸고 워커가 매 라운드 새로 읽는다.

현재 항목: 탐색 조건(constraint) · 밴딧 on/off · 그라운딩 on/off.

`data/run_config.json` 에 저장 → 서버 재시작 없이 반영(워커가 라운드 시작 때 get).
모듈 import 한 server.py 코드 변경분만 재시작 필요하고, 이 값 자체는 파일이라 즉시.

⚠ delay 는 **탐색 조건이 정한다** (`constraint` 의 `delay=0|1`). 예전엔 별도의
'delay 테스트 모드'(0/1/mix 토글)가 있었지만, 같은 값을 두 곳에서 정하면 어느 쪽이
이겼는지가 로그로만 드러나 조건을 걸어도 다른 delay 로 도는 사고가 난다
(2026-07-22 사장 지시로 제거). 조건이 delay 를 안 정하면 DEFAULT_DELAY 를 쓴다.
"""

from __future__ import annotations

import json
import os
import threading

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.abspath(os.path.join(_THIS_DIR, '..', 'data', 'run_config.json'))

_LOCK = threading.Lock()

# 탐색 조건이 delay 를 지정하지 않을 때 쓰는 값.
# 2026-07-23 '1' 로 변경(사장 지시). 근거: D0 표준컷(Sharpe 2.69/Fitness 1.5)은 현재
# 머신의 Sharpe 천장(~2.2) 위라 구조적으로 못 넘고, HT 강등 경로(마진>3bp)도 대부분
# 미달인 반면, D1 컷(1.58/1.0)은 7/23 실측으로 전 체크 PASS 알파가 실제로 나왔다
# (TOP1000·INDUSTRY·model 계열, PROD_CORRELATION 만 남음). D0 이 필요하면 탐색 조건에
# `delay=0` 을 명시한다 — 조건이 지정하면 언제나 조건이 이긴다(round_delay 참조).
DEFAULT_DELAY = '1'


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


# ── 탐색 조건 (Power Pool 주간 테마·대회 필터·즉석 지시) ─────────────────────
# 필터 문법이든 자연어든 **원문 그대로** 저장한다. 파싱은 읽는 쪽(constraint_spec)이
# 한다 — 파싱 결과를 저장하면 파서를 고칠 때마다 저장된 조건이 낡는다.
#   "region=USA & delay=1 & universe=TOP1000 & datasets not in ['pv1']"
#   "USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존 통과하는 알파"

def get_constraint_text() -> str:
    """현재 걸린 탐색 조건 원문. 없으면 빈 문자열."""
    with _LOCK:
        return str(_read().get('constraint', '') or '').strip()


def set_constraint_text(text) -> str:
    """탐색 조건을 건다. 빈 문자열/None 이면 해제. 저장된 원문을 돌려준다."""
    val = str(text or '').strip()
    with _LOCK:
        data = _read()
        if val:
            data['constraint'] = val
        else:
            data.pop('constraint', None)
        _write(data)
    return val


def get_constraint():
    """현재 조건을 파싱한 ConstraintSpec. 조건이 없으면 None."""
    from . import constraint_spec
    raw = get_constraint_text()
    if not raw:
        return None
    spec = constraint_spec.parse(raw)
    return None if spec.is_empty() else spec


def round_delay(constraint=None) -> str:
    """이 라운드에 강제할 delay('0'|'1'). 조건이 정하면 그 값, 아니면 DEFAULT_DELAY.

    라운드는 delay 를 하나만 가진다 — 필드 팔레트(D0/D1 가용 데이터셋)와 프롬프트가
    delay 에 묶여 있어서, 한 라운드 안에 섞으면 조건 밖 알파를 만들어 시뮬을 버린다.
    """
    spec = constraint if constraint is not None else get_constraint()
    d = getattr(spec, 'delay', None)
    return str(d) if d is not None else DEFAULT_DELAY


def is_bandit_enabled() -> bool:
    """True if the bandit learning-loop is enabled (default: True).

    Reads 'bandit_enabled' from data/run_config.json — live-editable without
    a server restart, exactly like get_constraint_text().
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


def is_grounding_enabled() -> bool:
    """True if per-round Google grounding is enabled (default: True).

    Reads 'grounding_enabled' from data/run_config.json — live-editable without
    a server restart, exactly like is_bandit_enabled().
    Absent / non-bool values default to True so grounding ships ON.
    """
    with _LOCK:
        val = _read().get('grounding_enabled', None)
    if val is None:
        return True
    # Accept both JSON bool (True/False) and string representations.
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() not in ('false', '0', 'no', 'off')


def set_grounding_enabled(enabled: bool) -> None:
    """Persist the grounding_enabled flag.  No restart required."""
    with _LOCK:
        data = _read()
        data['grounding_enabled'] = bool(enabled)
        _write(data)
