#!/usr/bin/env python3
"""BRAIN Learn 문서 전체를 REST 로 받아 `docs/brain_learn/` 에 마크다운으로 저장한다.

발견 경위 (2026-07-21)
---------------------
Learn 문서는 SPA 가 클라이언트에서 렌더링해 HTML 을 긁어도 껍데기(2.4KB)만 나온다.
Playwright 로 **한 번** 네트워크를 들여다봐서 본문 엔드포인트를 찾아냈다:

    GET /tutorials?limit=50            → 튜토리얼 목록 + 각 튜토리얼의 pages[]
    GET /tutorial-pages/{page_id}      → 그 페이지의 본문 (content = 블록 배열)

즉 **정찰만 브라우저로 하고 수집은 REST 로** 한다. 이 스크립트는 브라우저를 쓰지 않는다.

⚠ 세션 안전 규칙 — 저장된 세션 쿠키만 재사용한다. `POST /authentication` 을 때리거나
  persona inquiry 를 재해결하면 생체인증 세션이 죽는다(BIOMETRICS_THROTTLED 재무장).

사용법
------
    python3.12 scripts/fetch_brain_learn.py            # 전체 수집
    python3.12 scripts/fetch_brain_learn.py --list     # 목록만 출력
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db, wqb_api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'docs', 'brain_learn')
PAGE_SLEEP_S = 0.4          # /data-fields 처럼 429 를 받지 않도록 여유를 둔다
USER_ID = int(os.environ.get('IQC_LEARN_USER_ID', '2'))


def _md(raw_html: str) -> str:
    """문서용 HTML → 읽을 수 있는 마크다운. 완벽한 변환이 목적이 아니라
    LLM·사람이 읽고 근거로 삼을 수 있는 평문을 만드는 게 목적이다."""
    s = raw_html or ''
    s = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', s, flags=re.S | re.I)
    # 표는 구조가 의미를 가지므로 셀 경계를 파이프로 보존한다
    s = re.sub(r'</t[dh]>\s*<t[dh][^>]*>', ' | ', s, flags=re.I)
    s = re.sub(r'<tr[^>]*>', '\n| ', s, flags=re.I)
    s = re.sub(r'</tr>', ' |', s, flags=re.I)
    for lvl in range(1, 7):
        s = re.sub(rf'<h{lvl}[^>]*>(.*?)</h{lvl}>', rf'\n\n{"#" * lvl} \1\n', s, flags=re.S | re.I)
    s = re.sub(r'<li[^>]*>(.*?)</li>', r'\n- \1', s, flags=re.S | re.I)
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.I)
    s = re.sub(r'</p>', '\n\n', s, flags=re.I)
    s = re.sub(r'<(strong|b)[^>]*>(.*?)</\1>', r'**\2**', s, flags=re.S | re.I)
    s = re.sub(r'<(em|i)[^>]*>(.*?)</\1>', r'*\2*', s, flags=re.S | re.I)
    s = re.sub(r'<code[^>]*>(.*?)</code>', r'`\1`', s, flags=re.S | re.I)
    s = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', s, flags=re.S | re.I)
    s = re.sub(r'<img[^>]*src="([^"]*)"[^>]*>', r'\n![이미지](\1)\n', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    s = html.unescape(s)
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def _render(content) -> str:
    """content 는 블록 배열이다. TEXT 는 HTML, 그 외 타입은 라벨을 붙여 보존한다."""
    if isinstance(content, str):
        return _md(content)
    parts = []
    for blk in (content or []):
        if not isinstance(blk, dict):
            parts.append(str(blk))
            continue
        t = str(blk.get('type') or '').upper()
        v = blk.get('value')
        if t == 'TEXT':
            parts.append(_md(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)))
        elif isinstance(v, str) and v.strip():
            parts.append(f'> [{t}] {v.strip()}')
        elif v is not None:
            parts.append(f'> [{t}] {json.dumps(v, ensure_ascii=False)}')
    return '\n\n'.join(x for x in parts if x)


def _slug(s: str) -> str:
    return re.sub(r'[^a-zA-Z0-9._-]+', '-', str(s)).strip('-')[:80]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help='목록만 출력하고 끝낸다')
    args = ap.parse_args()

    cr = db.get_user_credentials(USER_ID)
    c = wqb_api.WqbApiClient(cr[0], cr[1])
    if not c._load_session() or not c._session_valid():
        print('세션이 없거나 만료됐다. 대시보드에서 인증을 마친 뒤 다시 실행하라.', file=sys.stderr)
        return 2

    r = c.session.get(f'{wqb_api.BASE}/tutorials', params={'limit': 50},
                      headers={'Accept': wqb_api._API_ACCEPT}, timeout=45)
    r.raise_for_status()
    tutorials = r.json()['results']
    total = sum(len(t.get('pages') or []) for t in tutorials)
    print(f'튜토리얼 {len(tutorials)}개 / 페이지 {total}개')
    if args.list:
        for t in tutorials:
            print(f"  [{t.get('category')}] {t['id']} — {len(t.get('pages') or [])}p")
            for p in (t.get('pages') or []):
                print(f"      {p['id']:44s} {p.get('title')}")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    index, done, failed = [], 0, []
    for t in tutorials:
        cat, tid = t.get('category') or 'Uncategorized', t['id']
        for seq, p in enumerate(t.get('pages') or [], 1):
            pid = p['id']
            try:
                pr = c.session.get(f'{wqb_api.BASE}/tutorial-pages/{pid}',
                                   headers={'Accept': wqb_api._API_ACCEPT}, timeout=45)
                if pr.status_code == 429:
                    time.sleep(10)
                    pr = c.session.get(f'{wqb_api.BASE}/tutorial-pages/{pid}',
                                       headers={'Accept': wqb_api._API_ACCEPT}, timeout=45)
                pr.raise_for_status()
                j = pr.json()
            except Exception as e:
                failed.append((pid, str(e)))
                print(f'  ✗ {pid}: {e}')
                continue

            body = _render(j.get('content'))
            fname = f'{_slug(tid)}__{seq:02d}__{_slug(pid)}.md'
            with open(os.path.join(OUT_DIR, fname), 'w') as fh:
                fh.write(f"# {j.get('title')}\n\n"
                         f"- 튜토리얼: `{tid}` ({cat})\n"
                         f"- 페이지 ID: `{pid}`\n"
                         f"- 최종수정: {j.get('lastModified')}\n"
                         f"- 분량: {j.get('duration')}\n\n---\n\n{body}\n")
            index.append({'tutorial': tid, 'category': cat, 'page': pid,
                          'title': j.get('title'), 'file': fname,
                          'chars': len(body), 'lastModified': j.get('lastModified')})
            done += 1
            print(f'  ✓ [{cat}] {str(j.get("title"))[:52]:54s} {len(body):6d}자')
            time.sleep(PAGE_SLEEP_S)

    with open(os.path.join(OUT_DIR, 'INDEX.json'), 'w') as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, 'INDEX.md'), 'w') as fh:
        fh.write('# BRAIN Learn 문서 색인\n\n')
        fh.write(f'수집 {done}/{total} 페이지 · 총 {sum(x["chars"] for x in index):,}자\n\n')
        cur = None
        for x in index:
            if x['category'] != cur:
                cur = x['category']
                fh.write(f'\n## {cur}\n\n')
            fh.write(f'- [{x["title"]}]({x["file"]}) — `{x["page"]}` ({x["chars"]:,}자)\n')
    print(f'\n완료 {done}/{total} · 실패 {len(failed)} · 출력 {OUT_DIR}')
    for pid, e in failed:
        print(f'  실패: {pid} — {e}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
