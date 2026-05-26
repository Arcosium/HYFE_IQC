"""오프라인 알파 검증기 — 시뮬에 보내기 전에 컴파일/런타임 에러를 미리 차단.

검증 항목:
  1. 과학적 표기법 (1e-6, 2E+5 등) → WQB 컴파일러 거부.
  2. 괄호 균형 — `(` 와 `)` 짝.
  3. 한 줄 강제 — 줄바꿈/탭 없음 (`;` 다중문장은 허용).
  4. 길이 sanity — 5 자 미만/3000 자 초과는 거부.
  5. raw vector 필드 — vec_* 래퍼 없이 쓴 vector 타입 필드만 차단.

식별자 '화이트리스트'(CSV 미수록=거부) 정책은 폐기했다. CSV 는 일부 목록일 뿐이라
ts_backfill·add·operating_income 같은 실재 이름까지 막아 알파를 획일화시켰다. 새 이름은
시뮬에 보내 그 에러를 학습 캐시에 쌓는 편이 다양성에 유리하다 (확정 불능 연산자는
gemini_strategist._FORBIDDEN_SUBSTRINGS 에서 이미 차단).

검증은 빠르게 (수 ms) 끝나야 한다. 정규식 토큰화 + 집합 lookup 기반.

반환: list[str]  (위반 사항 목록; 비어 있으면 통과)
"""

from __future__ import annotations

import csv
import os
import re

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATAFIELDS_CSV = os.path.join(_THIS_DIR, 'IQC_brain_datafields.csv')

# 정규식: 식별자 (영문자/언더스코어 시작, 영숫자/언더스코어 연속).
_IDENT_RX = re.compile(r'\b[A-Za-z_][A-Za-z0-9_]*\b')
# 과학적 표기법: 1e-6, 2.5E+3, 1e6 등.
_SCI_RX = re.compile(r'\b\d+\.?\d*[eE][+-]?\d+\b')

# type 컬럼이 'vector' 인 datafield 집합의 mtime 캐시 (파일 안 바뀌면 재파싱 안 함).
_VECTOR_FIELDS_CACHE: tuple[float, set[str]] | None = None


def vector_datafields() -> set[str]:
    """type 컬럼이 'vector' 인 datafield 의 집합. raw 로 쓰면 ts_*/산술 연산자에서
    'does not support event inputs' 에러 → vec_avg/vec_sum 등 vec_* 로 감싸야 한다."""
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


def validate_alpha(code: str) -> list[str]:
    """알파 코드 한 줄을 검증. 위반 사항 목록 반환 (비어 있으면 통과)."""
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

    # 식별자 검사: 화이트리스트 거부는 폐기 (위 docstring 참고). raw vector 필드만 차단 —
    # vector 타입은 vec_avg/vec_sum 등 vec_* 로 감싸야 행렬 연산이 되며, raw 로 쓰면
    # 'does not support event inputs' 에러가 확실하다. 코드에 vec_ 래퍼가 전혀 없을 때만 차단.
    vec_fields = vector_datafields()
    if vec_fields and 'vec_' not in code.lower():
        raw_vec = sorted(i for i in set(_IDENT_RX.findall(code)) if i in vec_fields)
        if raw_vec:
            issues.append('raw vector-type datafield (vec_* 로 감싸야 함): '
                          + ', '.join(raw_vec[:6]))

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
