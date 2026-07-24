#!/usr/bin/env python3
"""alphas.submitted 오기록 정정 — 기본은 **dry-run**(아무것도 안 바꾼다).

배경
----
`db.effectively_submitted()` 가 브라우저 시대 추정("상태값이 비어있지 않고 self-corr
거절이 아니면 제출됨")을 쓰는 바람에, 제출한 적 없는 알파가 `submitted=1` 로 기록됐다.
2026-07-21 실측: alphas.submitted=1 이 **6657행**인데 WQB 실제 제출은 **23건**.
대부분 `rejected:...(http_403)` — WQB 가 거절한 것을 제출로 센 것이다.

코드는 이미 고쳤다(미제출 접두사 명시 + 기동 마이그레이션 제외). 이 스크립트는 그
정의를 **과거 행에 소급 적용**한다. 판단 기준은 오직 `submit_status` 문자열이며,
REST API 는 성공 시 정확히 'submitted' 를 준다.

사용법
------
    python3.12 scripts/fix_submitted_flag.py               # dry-run (기본)
    python3.12 scripts/fix_submitted_flag.py --apply       # 백업 뜨고 실제 정정
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import db as _db  # noqa: E402

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'data', 'hyfe_iqc.db')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='실제로 UPDATE 한다 (기본은 dry-run)')
    ap.add_argument('--db', default=DB_PATH)
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, submitted, submit_status FROM alphas WHERE submitted=1").fetchall()
    wrong = [r for r in rows
             if not _db.effectively_submitted(r['submitted'], r['submit_status'])]

    by_reason: dict[str, int] = {}
    for r in wrong:
        key = (r['submit_status'] or '').strip()[:30]
        by_reason[key] = by_reason.get(key, 0) + 1

    print(f'submitted=1 인 행: {len(rows)}')
    print(f'그중 실제로는 미제출: {len(wrong)}')
    for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1])[:10]:
        print(f'  {v:6d}  {k}')
    print(f'\n정정 후 남을 제출 성공: {len(rows) - len(wrong)}')

    if not args.apply:
        print('\n[dry-run] 아무것도 바꾸지 않았다. 실제 정정은 --apply')
        return 0
    if not wrong:
        print('정정할 행 없음')
        return 0

    # ⚠ WAL 모드라 shutil.copy2 로 .db 만 복사하면 **WAL 에 있는 최신 커밋이 빠진**
    #   불완전한 백업이 된다(워커가 계속 쓰는 중이면 특히). SQLite 온라인 백업 API 는
    #   WAL 을 포함한 일관 스냅샷을 만든다.
    backup = f'{args.db}.bak_submitfix_{int(time.time())}'
    with sqlite3.connect(backup) as bck:
        con.backup(bck)
    print(f'\n백업(온라인, WAL 포함): {backup} '
          f'({os.path.getsize(backup) / 1e6:.1f}MB)')
    # 워커가 동시에 쓰는 중이므로 잠금 대기를 넉넉히 준다.
    con.execute('PRAGMA busy_timeout=30000')
    con.executemany('UPDATE alphas SET submitted=0 WHERE id=?',
                    [(r['id'],) for r in wrong])
    con.commit()
    print(f'정정 완료: {len(wrong)}행 → submitted=0')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
