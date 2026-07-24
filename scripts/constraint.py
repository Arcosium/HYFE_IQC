#!/usr/bin/env python3
"""탐색 조건 걸기/보기/해제 — 워커가 다음 라운드부터 이 조건 안에서만 알파를 만든다.

왜 있나
-------
Power Pool 테마는 매주 필터가 바뀐다. "이 조건에 맞는 알파를 찾아라" 가 상시 요구인데,
매번 사람이 읽고 손으로 헌팅하면 재현이 안 된다. 조건을 걸어두면 GA 가 도는 내내
조건 밖 알파를 **애초에 만들지 않는다** — 사후 필터링보다 훨씬 싸다(시뮬 한 건이 쿼터다).

**재시작 불필요** — 워커가 라운드마다 run_config 를 새로 읽는다.

사용법
------
    python3.12 scripts/constraint.py                       # 현재 조건 보기
    python3.12 scripts/constraint.py --show-examples       # 문법 예시
    python3.12 scripts/constraint.py --set "region=USA & delay=1 & universe=TOP1000 \
        & High Turnover returns ratio test PASS & datasets not in ['pv1']"
    python3.12 scripts/constraint.py --set "USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존"
    python3.12 scripts/constraint.py --check "…"           # 저장 없이 파싱만 확인
    python3.12 scripts/constraint.py --clear               # 해제
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import constraint_spec, run_config  # noqa: E402


def _report(spec, raw: str) -> None:
    print(f'원문 : {raw}')
    print(f'해석 : {spec.describe()}')
    if spec.region:
        print(f'  region          {spec.region}')
    if spec.delay is not None:
        print(f'  delay           {spec.delay}')
    if spec.universe:
        print(f'  universe        {spec.universe}')
    if spec.neutralizations:
        print(f'  neutralization  {", ".join(spec.neutralizations)}')
    if spec.excluded_datasets:
        print(f'  제외 데이터셋    {", ".join(sorted(spec.excluded_datasets))}')
        try:
            from server import datafield_palette as dfp
            n = len(dfp.fields_of_excluded_datasets(spec.excluded_datasets))
            print(f'                  → 금지 필드 {n}개')
        except Exception:
            pass
    if spec.required_checks:
        print(f'  요구 체크        {", ".join(spec.required_checks)}')
    if spec.unparsed:
        print('\n⚠ 해석하지 못한 절 — 이 조건은 적용되지 않는다:')
        for u in spec.unparsed:
            print(f'    {u}')
        print('  (문법을 확인하거나 --show-examples 참고)')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--set', dest='set_text', help='조건 걸기 (필터 문법 또는 자연어)')
    ap.add_argument('--check', dest='check_text', help='저장하지 않고 파싱만 확인')
    ap.add_argument('--clear', action='store_true', help='조건 해제')
    ap.add_argument('--show-examples', action='store_true', help='문법 예시 출력')
    args = ap.parse_args()

    if args.show_examples:
        print('문법 예시:\n')
        for name, text in constraint_spec.EXAMPLES.items():
            print(f'[{name}]\n  {text}\n')
        print('자연어도 된다:\n  "USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존 통과하는 알파"')
        return 0

    if args.clear:
        run_config.set_constraint_text('')
        print('탐색 조건 해제됨 — 다음 라운드부터 무제약으로 탐색한다.')
        return 0

    if args.check_text:
        _report(constraint_spec.parse(args.check_text), args.check_text)
        print('\n(확인만 했다. 실제로 걸려면 --set)')
        return 0

    if args.set_text:
        spec = constraint_spec.parse(args.set_text)
        if spec.is_empty():
            print('⚠ 아무 조건도 해석되지 않았다. 저장하지 않는다.', file=sys.stderr)
            _report(spec, args.set_text)
            return 1
        run_config.set_constraint_text(args.set_text)
        print('탐색 조건 저장됨 — 워커가 다음 라운드부터 적용한다(재시작 불필요).\n')
        _report(spec, args.set_text)
        return 0

    raw = run_config.get_constraint_text()
    if not raw:
        print('걸린 탐색 조건 없음 (무제약 탐색 중).')
        print('조건을 걸려면: scripts/constraint.py --set "…"')
        return 0
    _report(constraint_spec.parse(raw), raw)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
