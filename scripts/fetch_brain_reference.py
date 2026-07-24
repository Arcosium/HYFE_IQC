#!/usr/bin/env python3
"""BRAIN 레퍼런스(연산자·데이터셋·카테고리·대회·내 성적표)를 REST 로 받아 저장한다.

발견 경위 (2026-07-22)
---------------------
Playwright 로 플랫폼 화면(/data, /competitions, /genius, /learn/operators, /events)을
한 번 열어 네트워크를 들여다보고 본문 엔드포인트를 확보했다. **정찰만 브라우저로 하고
수집은 REST 로** 한다 — 이 스크립트는 브라우저를 쓰지 않는다.

무엇을 왜 모으나
----------------
- `/operators`        알파 문법의 단일 진실. arity·named param 까지 있어 프리플라이트에 쓴다.
- `/data-sets`        데이터셋별 설명·커버리지·**pyramidMultiplier**·themes·valueScore.
                      "커버리지 높은데 alphaCount 낮은 데이터셋" 이 노다지다 —
                      2026-07-21 에 option6(사용 알파 9개)에서 Sharpe 2.18 이 나왔다.
- `/data-categories`  분류 체계 + 카테고리별 valueScore.
- `/competitions` `/events`  마감이 걸린 트랙(Power Pool 등).
- `/users/self/consultant/summary`  **Genius 타이브레이커 점수판** — 알파수·피라미드수·
                      연산자/필드 평균·최대연속시뮬·커뮤니티활동. 승급 전략의 입력값이다.

⚠ 세션 안전 규칙 — 저장된 세션 쿠키만 재사용한다. `POST /authentication` 을 때리거나
  persona inquiry 를 재해결하면 생체인증 세션이 죽는다.

사용법
------
    python3.12 scripts/fetch_brain_reference.py
    python3.12 scripts/fetch_brain_reference.py --grids USA:1:TOP1000 GLB:1:TOPDIV3000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db, wqb_api  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, 'docs', 'brain_reference')
USER_ID = int(os.environ.get('IQC_LEARN_USER_ID', '2'))
PAGE_SLEEP_S = 0.8

# 우리가 실제로 도는 그리드 + 다음 테마 대비용.
DEFAULT_GRIDS = ('USA:1:TOP1000', 'USA:1:TOP3000', 'USA:0:TOP3000', 'GLB:1:TOPDIV3000')


def _get(c, path, params=None, tries=3):
    for i in range(tries):
        try:
            r = c.session.get(f'{wqb_api.BASE}{path}', params=params,
                              headers={'Accept': wqb_api._API_ACCEPT}, timeout=45)
            if r.status_code == 429:
                time.sleep(8 * (i + 1))
                continue
            if r.ok:
                return r.json()
            return None
        except Exception:
            time.sleep(3)
    return None


def _paged(c, path, params=None, page=50, cap=2000):
    """count/results 페이지네이션. 리스트를 그대로 주는 엔드포인트도 처리한다."""
    params = dict(params or {})
    out, off = [], 0
    while off < cap:
        params.update({'limit': page, 'offset': off})
        j = _get(c, path, params)
        if j is None:
            break
        if isinstance(j, list):
            return j
        res = j.get('results') or []
        out += res
        if not res or len(out) >= int(j.get('count') or 0):
            break
        off += page
        time.sleep(PAGE_SLEEP_S)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--grids', nargs='*', default=list(DEFAULT_GRIDS),
                    help='REGION:DELAY:UNIVERSE 형태')
    args = ap.parse_args()

    cr = db.get_user_credentials(USER_ID)
    c = wqb_api.WqbApiClient(cr[0], cr[1])
    if not c._load_session() or not c._session_valid():
        print('세션 없음/만료 — 대시보드에서 인증 후 다시 실행하라.', file=sys.stderr)
        return 2
    os.makedirs(OUT_DIR, exist_ok=True)

    def save(name, obj, note=''):
        path = os.path.join(OUT_DIR, name)
        with open(path, 'w') as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
        n = len(obj) if isinstance(obj, (list, dict)) else '?'
        print(f'  ✓ {name:34s} {n} {note}')

    print('BRAIN 레퍼런스 수집')
    ops = _get(c, '/operators')
    if ops:
        save('operators.json', ops, '연산자')
    cats = _get(c, '/data-categories')
    if cats:
        save('data_categories.json', cats, '카테고리')
    comps = _paged(c, '/competitions')
    save('competitions.json', comps, '대회')
    evs = _paged(c, '/events', {'order': '-start'}, page=100, cap=500)
    save('events.json', evs, '이벤트')
    summ = _get(c, '/users/self/consultant/summary')
    if summ:
        save('consultant_summary.json', summ, '내 Genius 성적표')

    all_ds = {}
    for grid in args.grids:
        try:
            region, delay, universe = grid.split(':')
        except ValueError:
            print(f'  ⚠ 그리드 형식 오류 무시: {grid}')
            continue
        rows = _paged(c, '/data-sets', {'instrumentType': 'EQUITY', 'region': region,
                                        'delay': int(delay), 'universe': universe},
                      page=50, cap=1000)
        all_ds[grid] = rows
        print(f'  ✓ data-sets {grid:22s} {len(rows)} 개')
        time.sleep(PAGE_SLEEP_S)
    save('datasets_by_grid.json', all_ds, '그리드')

    # ── 필드 → 데이터셋 매핑 (데이터셋별 조회) ──────────────────────────────
    # ⚠ 왜 데이터셋마다 따로 부르나 — /data-fields 는 count 를 10000 으로 캡하고
    #   필드 id 알파벳순으로 준다. D1 은 필드가 그보다 훨씬 많아서 뒤쪽 글자로 시작하는
    #   데이터셋(shortinterest36·us_short_sale·order_flow_imb…)이 통째로 빠진다.
    #   그 결과 2026-07-21 최고 알파를 만든 필드들이 classify_family 에서 None 으로
    #   떨어져 **GA 에 아예 보이지 않았다**. dataset.id 로 걸면 캡을 우회한다.
    field_ds = {}
    seen_ds = set()
    for grid, rows in all_ds.items():
        try:
            region, delay, universe = grid.split(':')
        except ValueError:
            continue
        for d in rows:
            dsid = d.get('id')
            key = (dsid, delay)
            if not dsid or key in seen_ds:
                continue
            seen_ds.add(key)
            j = _get(c, '/data-fields',
                     {'instrumentType': 'EQUITY', 'region': region, 'delay': int(delay),
                      'universe': universe, 'dataset.id': dsid, 'limit': 50, 'offset': 0})
            for f in ((j or {}).get('results') or []):
                fid = str(f.get('id') or '').strip().lower()
                if fid:
                    field_ds.setdefault(fid, dsid)
            time.sleep(0.35)
        print(f'  … {grid} 필드매핑 누적 {len(field_ds)}')
    save('field_dataset.json', field_ds, '필드→데이터셋')

    # 사람·LLM 이 읽을 마크다운 요약 — RAG 색인의 재료가 된다.
    md = [f'# BRAIN 레퍼런스 요약 (수집 {time.strftime("%Y-%m-%d %H:%M")})\n']
    if ops:
        md.append(f'## 연산자 ({len(ops)}개)\n')
        for o in ops:
            md.append(f"- **{o.get('name')}** [{o.get('category')}] "
                      f"`{o.get('definition')}` — {(o.get('description') or '')[:220]}")
        md.append('')
    for grid, rows in all_ds.items():
        md.append(f'\n## 데이터셋 — {grid} ({len(rows)}개)\n')
        # 커버리지 높은데 alphaCount 낮은 것 = 미개척. 그게 우리가 찾는 것이다.
        ranked = sorted(rows, key=lambda d: (-(d.get('coverage') or 0),
                                             (d.get('alphaCount') or 0)))
        for d in ranked:
            cid = d.get('category')
            cid = cid.get('id') if isinstance(cid, dict) else cid
            md.append(f"- **{d.get('id')}** [{cid}] cov={d.get('coverage')} "
                      f"fields={d.get('fieldCount')} alphas={d.get('alphaCount')} "
                      f"pyramid×{d.get('pyramidMultiplier')} — {d.get('name')}: "
                      f"{(d.get('description') or '')[:260]}")
    with open(os.path.join(OUT_DIR, 'REFERENCE.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    print(f'  ✓ REFERENCE.md  {sum(len(v) for v in all_ds.values()) + len(ops or [])} 항목')
    print(f'\n출력: {OUT_DIR}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
