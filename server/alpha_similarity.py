"""알파 코드 간 유사도 검사 — Gemini 가 self-correlation 으로 reject 될 가능성 높은
알파를 사전 차단. 출처: zhutoutoutousan/worldquant-miner generation_two/core/template_similarity.py
포팅 + 단순화.

3가지 메트릭 가중 평균:
  - string similarity (SequenceMatcher)
  - operator Jaccard
  - field Jaccard

임계값 이상이면 '너무 비슷한 알파' 로 판정.
"""
from __future__ import annotations
import re
from difflib import SequenceMatcher

from . import operator_catalog

_OP_PAT = re.compile(r'\b([a-z_][a-z0-9_]*)\s*\(', re.IGNORECASE)
_TOKEN_PAT = re.compile(r'\b([a-z][a-z0-9_]{2,})\b', re.IGNORECASE)


def _normalize(code: str) -> str:
    """공백/대소문자 정규화."""
    return re.sub(r'\s+', ' ', (code or '').strip().lower())


def extract_operators(code: str) -> set[str]:
    """식에서 operator 이름 추출."""
    return {m.group(1).lower() for m in _OP_PAT.finditer(code or '')
            if operator_catalog.is_operator(m.group(1))}


def extract_fields(code: str) -> set[str]:
    """식에서 datafield identifier 추출 (operator 제외)."""
    fields: set[str] = set()
    for m in _TOKEN_PAT.finditer(code or ''):
        tok = m.group(1).lower()
        if operator_catalog.is_operator(tok) or len(tok) < 4:
            continue
        fields.add(tok)
    return fields


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    i = len(a & b)
    u = len(a | b)
    return i / u if u else 0.0


def similarity(code1: str, code2: str,
               weight_str: float = 0.4,
               weight_op: float = 0.3,
               weight_field: float = 0.3) -> float:
    """3 메트릭 가중 평균. 0 (다름) ~ 1 (동일).

    - string similarity: 식 자체 token 순서까지 같은지
    - operator Jaccard: 같은 operator 조합 사용?
    - field Jaccard: 같은 datafield 사용?
    """
    n1 = _normalize(code1)
    n2 = _normalize(code2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    s_str = SequenceMatcher(None, n1, n2).ratio()
    s_op = jaccard(extract_operators(code1), extract_operators(code2))
    s_fld = jaccard(extract_fields(code1), extract_fields(code2))
    return weight_str * s_str + weight_op * s_op + weight_field * s_fld


def too_similar_to_any(code: str, others: list[str], threshold: float = 0.7) -> tuple[bool, float, str]:
    """code 가 others 중 어느 하나라도 threshold 초과로 비슷하면 (True, max_sim, matched_code).
    아니면 (False, max_sim, '').
    """
    if not others:
        return (False, 0.0, '')
    best = 0.0
    best_other = ''
    for o in others:
        s = similarity(code, o)
        if s > best:
            best = s
            best_other = o
    return (best >= threshold, best, best_other if best >= threshold else '')
