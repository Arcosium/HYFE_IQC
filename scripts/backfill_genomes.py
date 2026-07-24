#!/usr/bin/env python3.12
"""레거시 알파(genome IS NULL)의 유전체를 코드에서 역추출해 채운다 — 일회성 백필.

왜: `elite_seeds` 는 `genome IS NOT NULL` 인 행만 시드 후보로 본다. 6월 LLM 시대의
고성과 알파(Sharpe 3.77 / 3.43 …)는 유전체 컬럼이 비어 있어 GA 재료가 될 수 없었고,
7/12 콜드스타트 때 풀에서 통째로 사라졌다. `genome_from_alpha` 가 이제 레짐 조건부·
hump·합성팩터(CLV 등)까지 역추출하므로, 그 유전자를 되찾아 명예의 전당 시드로 되살린다.

⚠ 역추출은 **손실 압축**이다(genome_from_alpha docstring 참조). 완벽한 복제가 목적이
아니라 '그 구조를 GA 가 다시 만질 수 있게 하는 출발점' 이 목적이다.

사용:
    python3.12 scripts/backfill_genomes.py --dry-run          # 무엇이 바뀌는지만 출력
    python3.12 scripts/backfill_genomes.py --min-sharpe 1.0   # 실제 반영
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import genome_models as gm   # noqa: E402

DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          'data', 'hyfe_iqc.db')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', default=DEFAULT_DB)
    ap.add_argument('--min-sharpe', type=float, default=1.0,
                    help='이 Sharpe 이상인 레거시 알파만 백필 (기본 1.0)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        'SELECT id, code, sharpe, universe, neutralization, decay, truncation, '
        'generation, delay FROM alphas '
        "WHERE genome IS NULL AND TRIM(error_text)='' AND sharpe IS NOT NULL "
        'AND sharpe >= ? ORDER BY sharpe DESC',
        (args.min_sharpe,),
    ).fetchall()
    print(f'대상 {len(rows)}행 (genome NULL, sharpe >= {args.min_sharpe})')

    updates: list[tuple[str, int]] = []
    failed = 0
    for r in rows:
        code = (r['code'] or '').strip()
        if not code:
            failed += 1
            continue
        settings = {
            'universe': r['universe'] or 'TOP3000',
            'neutralization': r['neutralization'] or 'INDUSTRY',
            'decay': r['decay'] if r['decay'] is not None else 0,
            'truncation': r['truncation'] if r['truncation'] is not None else 0.08,
        }
        try:
            g = gm.genome_from_alpha(code, settings=settings,
                                     generation=int(r['generation'] or 0))
        except Exception as e:
            print(f'  ⚠ id={r["id"]} 역추출 실패: {e}')
            failed += 1
            continue
        if not g:
            failed += 1
            continue
        updates.append((json.dumps(g, ensure_ascii=False), int(r['id'])))

    print(f'역추출 성공 {len(updates)}행, 실패 {failed}행')

    # 상위 몇 개는 무엇이 복원됐는지 눈으로 확인한다.
    for raw, aid in updates[:5]:
        g = json.loads(raw)
        r = next(x for x in rows if x['id'] == aid)
        print(f'  id={aid} sh={r["sharpe"]:.2f} → fields={g["fields"]} '
              f'combine={g["combine"]} sign={g["sign"]} regime={g["regime"]} '
              f'hump={g["hump"]} group_op={g["group_op"]}')

    if args.dry_run:
        print('\n--dry-run — DB 를 건드리지 않았다.')
        return 0

    conn.executemany('UPDATE alphas SET genome=? WHERE id=? AND genome IS NULL',
                     updates)
    conn.commit()
    print(f'\n✓ {len(updates)}행에 genome 기록 완료')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
