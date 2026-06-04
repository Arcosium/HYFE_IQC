"""시뮬 전 결정적 자동수정 pre-pass.

lint 의 drop-only 정책을 보완 — 사소한 기계적 결함을 1회 수정해 구조적으로 새로운
아이디어를 typo 하나로 버리지 않는다. 의미 보존: 명시된 안전 케이스만 수정하고
모호하면 손대지 않는다 (멱등).
"""
from __future__ import annotations

import re

from . import operator_catalog

# delay-aware missing-lookback 기본 윈도우 (delay0 은 턴오버 억제 위해 큰 값).
LOOKBACK_DELAY0 = 22
LOOKBACK_DELAY1 = 10

_REGION_PREFIX_RX = re.compile(
    r'\b(?:USA|EUR|EUROPE|CHN|CHINA|ASI|AMR|GLB|JPN|KOR|TWN|HKG)\.([A-Za-z_]\w*)')
_LEADING_OP_RX = re.compile(r'^\s*[+*]\s*')
_OP_CALL_RX = re.compile(r'\b([a-z_][a-z0-9_]*)\s*\(')
_HUMP_CALL_RX = re.compile(r'\bhump\s*\(')
_NUMERIC_ARG_RX = re.compile(r'^\s*[-+]?(?:\d+\.?\d*|\.\d+)\s*$')
_GROUP_CALL_RX = re.compile(r'\bgroup_[a-z_]+\s*\(')
_QUOTED_IDENT_RX = re.compile(r'''^(\s*)(['"])([A-Za-z_]\w*)\2(\s*)$''')


def _strip_region_prefix(code: str) -> str:
    return _REGION_PREFIX_RX.sub(r'\1', code)


def _collapse_doubled_ops(code: str) -> str:
    def repl(m):
        name = m.group(1)
        n = len(name)
        if n % 2 == 0:
            half = name[: n // 2]
            if name == half + half and operator_catalog.is_operator(half):
                return half + '('
        return m.group(0)
    return _OP_CALL_RX.sub(repl, code)


def _fix_hump_positional(code: str) -> str:
    # WQB hump 은 입력 '정확히 1개' + named param 'hump='. positional 2nd arg(숫자)는
    # 'Invalid number of inputs : 2, should be exactly 1 input(s)' 에러로 100% 실패.
    # hump(x, 0.03) → hump(x, hump=0.03). 2nd top-level arg 가 숫자 literal 이고 아직
    # named(=) 가 아닐 때만 수정 (보수적·멱등). 비숫자 인자는 손대지 않는다.
    result = code
    for m in reversed(list(_HUMP_CALL_RX.finditer(code))):
        open_idx = m.end() - 1            # '(' 위치
        depth = 0
        in_str = False
        close_idx = -1
        commas: list[int] = []
        j = open_idx
        while j < len(result):
            ch = result[j]
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        close_idx = j
                        break
                elif ch == ',' and depth == 1:
                    commas.append(j)
            j += 1
        if close_idx == -1 or len(commas) != 1:
            continue                       # top-level arg 2개(콤마 1개)일 때만
        arg2 = result[commas[0] + 1:close_idx]
        if '=' in arg2 or not _NUMERIC_ARG_RX.match(arg2):
            continue                       # 이미 named 이거나 숫자 literal 이 아니면 skip
        fixed_arg2 = arg2.replace(arg2.strip(), 'hump=' + arg2.strip(), 1)
        result = result[:commas[0] + 1] + fixed_arg2 + result[close_idx:]
    return result


def _fix_quoted_group(code: str) -> str:
    # WQB group 연산자(group_neutralize/group_zscore/group_rank/...)는 그룹을 bare
    # identifier(sector/industry/subindustry/market 등)로 받는다. Gemini 가 가끔 따옴표
    # 문자열로 쓰면('SECTOR') 'Got invalid input at index 1, must be an expression' 에러.
    # 마지막 top-level 인자가 따옴표로 감싼 식별자면 따옴표만 벗긴다 (보수적·멱등).
    result = code
    for m in reversed(list(_GROUP_CALL_RX.finditer(code))):
        open_idx = m.end() - 1
        depth = 0
        in_str = ''
        close_idx = -1
        last_comma = -1
        j = open_idx
        while j < len(result):
            ch = result[j]
            if in_str:
                if ch == in_str:
                    in_str = ''
            elif ch in ('"', "'"):
                in_str = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    close_idx = j
                    break
            elif ch == ',' and depth == 1:
                last_comma = j
            j += 1
        if close_idx == -1 or last_comma == -1:
            continue                       # 그룹 인자(2nd+) 없는 형태는 skip
        grp_arg = result[last_comma + 1:close_idx]
        qm = _QUOTED_IDENT_RX.match(grp_arg)
        if not qm:
            continue                       # 따옴표 식별자가 아니면 손대지 않음
        unquoted = qm.group(1) + qm.group(3) + qm.group(4)   # 앞뒤 공백 보존, 따옴표만 제거
        result = result[:last_comma + 1] + unquoted + result[close_idx:]
    return result


def _add_missing_lookback(code: str, window: int) -> str:
    result = code
    # 끝에서 앞으로 처리해 삽입 후 인덱스가 어긋나지 않게 한다.
    for m in reversed(list(_OP_CALL_RX.finditer(code))):
        if not operator_catalog.needs_lookback(m.group(1)):
            continue
        open_idx = m.end() - 1            # '(' 위치 (원본 code 기준 인덱스)
        # 불변식: matches 를 역순 처리하므로 삽입은 항상 이 open_idx 의 '오른쪽'에서만
        # 일어난다 → open_idx 및 그 왼쪽 바이트는 절대 밀리지 않아 result 재스캔이 안전.
        # (좌→우로 바꾸거나 open_idx 왼쪽에 삽입하면 이 안전성이 깨지니 주의.)
        depth = 0
        in_str = False
        close_idx = -1
        has_top_comma = False
        j = open_idx
        while j < len(result):
            ch = result[j]
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        close_idx = j
                        break
                elif ch == ',' and depth == 1:
                    has_top_comma = True
            j += 1
        if close_idx == -1 or has_top_comma:
            continue
        if not result[open_idx + 1:close_idx].strip():
            continue                       # op() 빈 인자는 미수정
        result = result[:close_idx] + f',{window}' + result[close_idx:]
    return result


def repair(code: str, *, delay) -> tuple[str, list[str]]:
    """(repaired_code, applied_fix_labels). 안전 수정만, 멱등."""
    if not isinstance(code, str) or not code.strip():
        return (code, [])
    applied: list[str] = []
    out = code

    s = _strip_region_prefix(out)
    if s != out:
        applied.append('region_prefix'); out = s
    s = _collapse_doubled_ops(out)
    if s != out:
        applied.append('doubled_op'); out = s
    s = _LEADING_OP_RX.sub('', out)
    if s != out:
        applied.append('leading_op'); out = s
    s = _fix_hump_positional(out)
    if s != out:
        applied.append('hump_named'); out = s
    s = _fix_quoted_group(out)
    if s != out:
        applied.append('unquote_group'); out = s
    window = LOOKBACK_DELAY0 if str(delay) == '0' else LOOKBACK_DELAY1
    s = _add_missing_lookback(out, window)
    if s != out:
        applied.append('missing_lookback'); out = s

    return (out, applied)
