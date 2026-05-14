"""오프라인 알파 검증기 — 시뮬에 보내기 전에 컴파일/런타임 에러를 미리 차단.

검증 항목:
  1. 과학적 표기법 (1e-6, 2E+5 등) → WQB 컴파일러 거부.
  2. 식별자 화이트리스트 — operators.csv ∪ datafields.csv 에 있는 이름만 허용.
     단, 산술/비교/논리 연산자(`+ - * / ^ ?:` 등) 와 숫자 리터럴, sector/industry 같은
     group 키워드는 예외.
  3. 괄호 균형 — `(` 와 `)` 짝.
  4. 한 줄 강제 — 줄바꿈/탭 없음.
  5. 길이 sanity — 5 자 미만/3000 자 초과는 거부.

검증은 빠르게 (수 ms) 끝나야 한다. 정규식 토큰화 + 집합 lookup 기반.

반환: list[str]  (위반 사항 목록; 비어 있으면 통과)
"""

from __future__ import annotations

import csv
import os
import re
from typing import Iterable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
OPERATORS_CSV = os.path.join(_THIS_DIR, 'brain_operators.csv')
DATAFIELDS_CSV = os.path.join(_THIS_DIR, 'IQC_brain_datafields.csv')

# group_neutralize 의 두 번째 인자, group_rank 등에 쓰이는 그룹 키워드 — 식별자처럼 보이지만 합법.
_GROUP_KEYWORDS = {
    'sector', 'industry', 'subindustry', 'market',
    'country', 'exchange', 'pv13_h_min2_3000_sector',  # 알려진 group expression
}

# 자주 등장하는 숫자형/코드형 키워드 (시간 윈도우 등 인자) — 기본 예외.
# 식별자처럼 보이지만 사실 키워드성. 안전하게 통과시킨다.
_BUILTIN_TOKENS = {
    'true', 'false', 'nan', 'null',
}

# WQB 가 모든 알파에 기본 노출하는 의사 datafield (CSV 에는 없을 수 있음).
# 이들은 각 종목의 일별 시계열로 항상 사용 가능한 'OHLCV+' 표준 필드.
_WQB_BUILTIN_FIELDS = {
    'returns',          # 일간 수익률 (close[t]/close[t-1] - 1)
    'open', 'high', 'low', 'close', 'volume', 'vwap',
    'adv5', 'adv10', 'adv20', 'adv30', 'adv60', 'adv120', 'adv180',
    'cap',              # 시가총액
    'shares_outstanding', 'shares_outstanding_basic',
    'mdv',              # median daily volume
}

# 정규식: 식별자 (영문자/언더스코어 시작, 영숫자/언더스코어 연속).
_IDENT_RX = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*\b')
# 과학적 표기법: 1e-6, 2.5E+3, 1e6 등.
_SCI_RX = re.compile(r'\b\d+\.?\d*[eE][+-]?\d+\b')

# 운영 환경에서 ts_* / 산술 연산자가 거부하는 datafield 들 — CSV 의 type 컬럼이
# 'vector' 인 항목들이 여기 해당. 라운드별 1~3개 알파가 "Operator X does not support
# event inputs" 에러로 떨어지는 주범 (예: anl4_adxqfv110_pu, anl4_basicconafv110_*).
# CSV 의 두 번째 컬럼에서 동적으로 읽어들인다.


# CSV mtime 기반 캐시 — 파일이 바뀌지 않으면 재파싱하지 않는다.
_OPS_CACHE: tuple[float, set[str]] | None = None
_FIELDS_CACHE: tuple[float, set[str]] | None = None


def _read_first_column(path: str) -> set[str]:
    out: set[str] = set()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            for row in reader:
                if row and row[0]:
                    out.add(row[0].strip())
    except Exception:
        pass
    return out


def known_operators() -> set[str]:
    global _OPS_CACHE
    try:
        mt = os.path.getmtime(OPERATORS_CSV)
    except OSError:
        return set()
    if _OPS_CACHE and _OPS_CACHE[0] == mt:
        return _OPS_CACHE[1]
    raw = _read_first_column(OPERATORS_CSV)
    # 기호류 (`+`, `-`, `*`, `/`, `<=` 등) 는 식별자 정규식에 안 잡히므로 별도 처리 불필요.
    # 영숫자형 operator 만 필터.
    ops = {x for x in raw if _IDENT_RX.fullmatch(x or '')}
    _OPS_CACHE = (mt, ops)
    return ops


_VECTOR_FIELDS_CACHE: tuple[float, set[str]] | None = None


def known_datafields() -> set[str]:
    global _FIELDS_CACHE
    try:
        mt = os.path.getmtime(DATAFIELDS_CSV)
    except OSError:
        return set()
    if _FIELDS_CACHE and _FIELDS_CACHE[0] == mt:
        return _FIELDS_CACHE[1]
    fields = _read_first_column(DATAFIELDS_CSV)
    _FIELDS_CACHE = (mt, fields)
    return fields


def vector_datafields() -> set[str]:
    """type 컬럼이 'vector' 인 datafield 의 집합. 이들은 ts_* / 산술 연산자에서
    'does not support event inputs' 에러를 낸다 — 알파에서 사용 금지."""
    global _VECTOR_FIELDS_CACHE
    try:
        mt = os.path.getmtime(DATAFIELDS_CSV)
    except OSError:
        return set()
    if _VECTOR_FIELDS_CACHE and _VECTOR_FIELDS_CACHE[0] == mt:
        return _VECTOR_FIELDS_CACHE[1]
    vec: set[str] = set()
    try:
        with open(DATAFIELDS_CSV, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 2 and row[0] and row[1].strip().lower() == 'vector':
                    vec.add(row[0].strip())
    except Exception:
        pass
    _VECTOR_FIELDS_CACHE = (mt, vec)
    return vec


def _balanced_parens(code: str) -> bool:
    depth = 0
    in_str = False
    for ch in code:
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def validate_alpha(code: str, *, allowed_extra: Iterable[str] = ()) -> list[str]:
    """알파 코드 한 줄을 검증. 위반 사항 목록 반환 (비어 있으면 통과).

    `allowed_extra`: 호출자가 추가로 허용하고 싶은 식별자. 보통 비어 있음.
    """
    issues: list[str] = []
    if not isinstance(code, str):
        return ['code is not a string']
    code = code.strip()
    if not code:
        return ['empty code']
    if len(code) < 5:
        issues.append(f'too short ({len(code)} chars)')
    if len(code) > 3000:
        issues.append(f'too long ({len(code)} chars)')
    if '\n' in code or '\r' in code or '\t' in code:
        issues.append('contains newline/tab — must be a single line')

    # 과학적 표기법 검출
    sci = _SCI_RX.findall(code)
    if sci:
        issues.append(f'scientific notation not allowed: {", ".join(sorted(set(sci))[:3])}')

    # 괄호 균형
    if not _balanced_parens(code):
        issues.append('unbalanced parentheses')

    # 식별자 화이트리스트 검사
    ops = known_operators()
    fields = known_datafields()
    vec_fields = vector_datafields()
    vec_hits: list[str] = []
    if ops or fields:  # CSV 가 비어 있으면 검사 스킵 (잘못된 거부 방지)
        idents = set(_IDENT_RX.findall(code))
        unknown: list[str] = []
        for ident in idents:
            if ident in _BUILTIN_TOKENS or ident in _GROUP_KEYWORDS:
                continue
            if ident in _WQB_BUILTIN_FIELDS:
                continue
            if ident in vec_fields:
                vec_hits.append(ident)
                continue
            if ident in ops or ident in fields:
                continue
            if ident in allowed_extra:
                continue
            # 숫자처럼 시작하는 게 아니라 _ 또는 알파로 시작하는데, 둘 다에 없으면 unknown.
            unknown.append(ident)
        if unknown:
            # 가장 흔히 잘못 쓰는 것들을 먼저 보여준다.
            unknown_sorted = sorted(set(unknown))[:8]
            issues.append(f'unknown identifiers: {", ".join(unknown_sorted)}')
    if vec_hits:
        issues.append(
            'vector-type datafield (ts_*/산술 연산자에서 event-input 에러): '
            + ', '.join(sorted(set(vec_hits))[:6])
        )

    return issues


def validate_strategies(strategies: list[dict]) -> tuple[list[dict], list[tuple[dict, list[str]]]]:
    """전략 묶음을 한 번에 검증. (passed, rejected) 튜플 반환.

    rejected[i] = (strategy_dict, list_of_issues).
    """
    passed: list[dict] = []
    rejected: list[tuple[dict, list[str]]] = []
    for s in strategies:
        code = s.get('code') if isinstance(s, dict) else None
        issues = validate_alpha(code or '')
        if issues:
            rejected.append((s, issues))
        else:
            passed.append(s)
    return passed, rejected
