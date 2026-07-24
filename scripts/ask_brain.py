#!/usr/bin/env python3
"""BRAIN 공식 문서에 묻는다 — 규칙을 역산하지 말고 찾아보라.

2026-07-21 발굴에서 결정적이었던 사실들이 전부 문서에 적혀 있었는데 우리는 모르고
라이브 A/B 로 역산하고 있었다(IS Ladder 문턱표·CHN 컷·Genius 타이브레이커·
Multi-Simulation 이 순차 실행이라는 사실). 이제 묻고 답한다.

사용법
------
    python3.12 scripts/ask_brain.py "IS ladder thresholds"
    python3.12 scripts/ask_brain.py "고회전 알파 제출 조건" -k 3
    python3.12 scripts/ask_brain.py --rebuild        # 문서가 바뀌었을 때 색인 재생성

색인 재료: docs/brain_learn/ (Learn 77편) + docs/brain_reference/REFERENCE.md
  → scripts/fetch_brain_learn.py · scripts/fetch_brain_reference.py 로 수집한다.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import brain_rag  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('query', nargs='*', help='질의 (한/영)')
    ap.add_argument('-k', type=int, default=4, help='반환 개수')
    ap.add_argument('--full', action='store_true', help='청크 전문 출력')
    ap.add_argument('--rebuild', action='store_true', help='색인 재생성')
    args = ap.parse_args()

    if args.rebuild:
        idx = brain_rag.build_index()
        print(f"청크 {len(idx['chunks'])} · 임베딩 {idx['embedded']} · 차원 {idx['dim']}")
        if not args.query:
            return 0

    q = ' '.join(args.query).strip()
    if not q:
        print('질의를 달라. 예: ask_brain.py "IS ladder thresholds"', file=sys.stderr)
        return 1

    idx = brain_rag.load_index()
    if not idx:
        print('색인이 없다 — 먼저 --rebuild 로 만들라.', file=sys.stderr)
        return 2
    if not idx.get('embedded'):
        print('⚠ 임베딩 없는 색인 — 키워드 검색으로 동작한다(arcembed 확인).', file=sys.stderr)

    hits = brain_rag.search(q, k=args.k)
    if not hits:
        print('맞는 대목 없음.')
        return 0
    for i, h in enumerate(hits, 1):
        print(f"\n[{i}] {h['title']}  (유사도 {h['score']:.3f})")
        print(f"    출처 {h['source']}")
        text = h['text'] if args.full else h['text'][:900]
        for line in text.splitlines():
            print(f'    {line}')
        if not args.full and len(h['text']) > 900:
            print('    … (--full 로 전문)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
