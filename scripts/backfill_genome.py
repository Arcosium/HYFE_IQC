#!/usr/bin/env python3
"""alphas.genome 일회성 백필 — 렌더러 산(産) 알파에 한정.

왜 전부가 아니라 렌더러 산출물만인가
------------------------------------
`genome_from_alpha()` 는 손실 압축이다. renderer 가 만든 코드(`rank(...)` 단일 표현식)는
유전자가 그대로 드러나 있어 거의 정확히 복원되지만, 레거시 Gemini 다중문 알파
(`cf=...; vr=...; ts_decay_linear(...)`)는 3필드/2변환/1결합 템플릿으로 찌그러진다 —
pass=11 짜리 3팩터 알파가 2팩터로 줄고 winsorize·ts_decay_linear 유전자는 사라진다.
그런 '화석'을 시드로 쓰면 자식이 부모를 복제조차 못 하므로 구조적으로 부모보다 나쁘다.
그래서 화석은 백필하지 않는다 → `genome IS NULL` → `elite_seeds()` 가 자동 배제한다.

판별은 desc 접두사로 한다. renderer 는 `_desc()` 에서 항상
`"{model_name} {family}: ..."` 로 시작하는 desc 를 만든다.

멱등: 이미 genome 이 있는 행은 건드리지 않는다.

    python3 scripts/backfill_genome.py            # dry-run (기본)
    python3 scripts/backfill_genome.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import genome_models  # noqa: E402
from server.db import DB_PATH  # noqa: E402

RENDERER_PREFIXES = ('rc-api-genome ', 'standard-playwright-genome ')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='실제로 쓴다 (기본은 dry-run)')
    ap.add_argument('--db', default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute('PRAGMA table_info(alphas)')}
    if 'genome' not in cols:
        print('❌ alphas.genome 컬럼이 없다 — 서버를 한 번 띄워 db.init() 마이그레이션을 돌려라.')
        return 1

    where = ' OR '.join("desc LIKE ?" for _ in RENDERER_PREFIXES)
    rows = conn.execute(
        f'SELECT id, code, desc, generation, universe, neutralization, decay, truncation '
        f'FROM alphas WHERE genome IS NULL AND ({where})',
        tuple(p + '%' for p in RENDERER_PREFIXES),
    ).fetchall()

    total = conn.execute('SELECT COUNT(*) FROM alphas').fetchone()[0]
    print(f'전체 알파 {total} · 백필 대상(렌더러 산, genome 없음) {len(rows)}')

    updates: list[tuple[str, int]] = []
    failed = 0
    for r in rows:
        try:
            g = genome_models.genome_from_alpha(
                r['code'],
                settings={k: r[k] for k in
                          ('universe', 'neutralization', 'decay', 'truncation')},
                generation=int(r['generation'] or 0),
            )
            updates.append((json.dumps(g, ensure_ascii=False), r['id']))
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f'  ⚠ id={r["id"]} 역추출 실패: {e}')

    gens: dict[int, int] = {}
    for payload, _ in updates:
        g = json.loads(payload)
        gens[g['generation']] = gens.get(g['generation'], 0) + 1
    print(f'복원 성공 {len(updates)} · 실패 {failed}')
    print(f'세대 분포: {dict(sorted(gens.items()))}')

    fossils = conn.execute(
        'SELECT COUNT(*) FROM alphas WHERE genome IS NULL AND pass_count >= 5'
    ).fetchone()[0]
    print(f'유전체 없는 pass>=5 화석 {fossils}개 — 시드 풀에서 영구 배제된다')

    if not args.apply:
        print('\n(dry-run — 아무것도 쓰지 않았다. --apply 로 실행)')
        return 0

    with conn:
        conn.executemany('UPDATE alphas SET genome=? WHERE id=?', updates)
    print(f'\n✅ {len(updates)} 행에 genome 을 기록했다')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
