#!/usr/bin/env python3
"""라이브 datafield 팔레트 갱신 — **delay 0 과 1 을 모두** 수집한다.

왜 필요한가
-----------
정적 CSV(`server/IQC_brain_datafields.csv`, 5200행)는 **전부 delay=1** 이다. 그래서 GA 는
D0 필드 팔레트를 가진 적이 없고, genome_models 는 그 공백을 'D0 면 무조건 pv' 라는
하드 제약으로 메우고 있었다. 실제로는 D0 에도 option6 131개·fundamental2 766개 등
수천 개 필드가 있다(2026-07-21 실측). 그 제약이 USA/D0/OPTION 피라미드 1.7배(전 항목
최고 배수)를 구조적으로 막고 있었다.

사용법
------
    python3.12 scripts/refresh_datafields.py            # 기본 그리드
    python3.12 scripts/refresh_datafields.py --dry-run  # 수집만 하고 안 씀

인증은 **저장된 세션 쿠키를 재사용**한다 (POST /authentication 을 때리지 않는다 —
persona 재무장 위험 회피, memory: genomicwqb-rename-and-mobile 의 불변식 참조).
세션이 만료됐으면 대시보드에서 갱신한 뒤 다시 실행하면 된다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import wqb_api, wqb_data_service as wds  # noqa: E402

# (region, universe, delay) — D0/D1 양쪽. universe 는 최대 유니버스 하나면 충분하다
# (필드 가용성은 유니버스보다 delay 에 크게 좌우된다).
GRID = (('USA', 'TOP3000', 1), ('USA', 'TOP3000', 0))
PAGE = 50
MAX_PAGES = 400          # 안전장치 (20000행)
# /data-fields 는 연속 호출에 429 를 준다 (실측: 3페이지째부터). 문서의
# "scripts do not lay excessive load on the server" 지침에 맞춰 천천히 긁는다.
PAGE_SLEEP_S = 1.5
RETRY_MAX = 6
RETRY_BASE_S = 5.0


def session_for(email: str) -> requests.Session:
    s = requests.Session()
    path = wqb_api._default_session_file(email)
    with open(path) as f:
        s.cookies.update(json.load(f))
    return s


def _get_page(s, region, universe, delay, offset):
    """한 페이지. 429 면 지수 백오프로 재시도. 끝내 실패하면 None."""
    delay_s = RETRY_BASE_S
    for attempt in range(RETRY_MAX):
        r = s.get(f'{wqb_api.BASE}/data-fields',
                  params={'region': region, 'universe': universe, 'delay': delay,
                          'instrumentType': 'EQUITY', 'limit': PAGE, 'offset': offset},
                  headers={'Accept': wqb_api._API_ACCEPT}, timeout=60)
        if r.ok:
            return r.json()
        if r.status_code != 429:
            print(f'  !! offset={offset} → HTTP {r.status_code}')
            return None
        ra = r.headers.get('Retry-After')
        wait = float(ra) if ra and str(ra).replace('.', '').isdigit() else delay_s
        print(f'  .. 429 offset={offset} — {wait:.0f}s 대기 (재시도 {attempt + 1}/{RETRY_MAX})')
        time.sleep(wait)
        delay_s = min(delay_s * 2, 120.0)
    return None


def fetch(s: requests.Session, region: str, universe: str, delay: int) -> list[dict]:
    rows, offset = [], 0
    for _ in range(MAX_PAGES):
        j = _get_page(s, region, universe, delay, offset)
        if j is None:
            print(f'  !! {region}/{universe}/D{delay}: offset={offset} 에서 중단 '
                  f'({len(rows)}행 수집)')
            break
        res = j.get('results') or []
        if not res:
            break
        rows += wds.map_datafields(res, region, universe, delay)
        offset += PAGE
        if offset >= int(j.get('count') or 0):
            break
        time.sleep(PAGE_SLEEP_S)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--email', default=wds.HOUSE_RC_USERNAME)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--min-rows', type=int, default=3000,
                    help='이보다 적게 수집되면 쓰지 않는다 (부분 수집 퇴화 방지)')
    args = ap.parse_args()

    s = session_for(args.email)
    all_rows: list[dict] = []
    for region, universe, delay in GRID:
        rows = fetch(s, region, universe, delay)
        by_ds: dict[str, int] = {}
        for r in rows:
            by_ds[r['category']] = by_ds.get(r['category'], 0) + 1
        top = sorted(by_ds.items(), key=lambda kv: -kv[1])[:6]
        print(f'{region}/{universe}/D{delay}: {len(rows)}행  상위 데이터셋 {top}')
        all_rows += rows

    if not all_rows:
        print('수집 실패 — 세션 만료 가능성 (대시보드에서 갱신 후 재시도)')
        return 1
    # ⚠ 부분 수집을 그대로 쓰면 **팔레트가 퇴화한다** — datafield_palette 는 라이브 CSV 가
    #   비어있지만 않으면 무조건 그쪽을 쓰기 때문이다(정적 CSV 5200행 → 300행 사고 재발 방지).
    if len(all_rows) < args.min_rows:
        print(f'수집 {len(all_rows)}행 < 최소 {args.min_rows}행 — 기존 팔레트 보존을 위해 '
              f'쓰지 않는다. 잠시 후 다시 실행할 것.')
        return 2
    d0 = sum(1 for r in all_rows if r['delay'] == '0')
    print(f'\n총 {len(all_rows)}행 (D0 {d0} / D1 {len(all_rows) - d0})')
    if args.dry_run:
        print('--dry-run: 쓰지 않음')
        return 0
    wds.write_live_csv(all_rows)
    print(f'→ {wds.LIVE_CSV_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
