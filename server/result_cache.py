"""user_id 별 알파 코드 → 시뮬 결과 캐시. DB 의 alphas 테이블 조회.

동일 user 가 동일 코드를 다음 라운드에 또 요청하면 시뮬 건너뛰고 캐시 결과 반환.
"""

from __future__ import annotations

from typing import Any

from . import db as _db


def lookup(user_id: int, code: str, settings_fp: str | None = None) -> dict[str, Any] | None:
    return _db.lookup_alpha_by_hash(user_id, _db.code_hash(code), settings_fp)


def materialize(strategy: dict, cached: dict, current_round: int) -> dict:
    # cache 는 cross-user — submitted 같은 사용자별 WQB 상태는 가져오지 않는다.
    # (다른 사용자가 본인 계정으로 제출했어도 본인 계정으로 자동 제출된 게 아님.)
    return {
        'idx': strategy['idx'],
        'code': strategy['code'],
        'desc': strategy.get('desc', ''),
        'pass_count': int(cached.get('pass_count') or 0),
        'pass_items': list(cached.get('pass_items') or []),
        'fail_count': int(cached.get('fail_count') or 0),
        'fail_items': list(cached.get('fail_items') or []),
        'error_count': int(cached.get('error_count') or 0),
        'pending_count': int(cached.get('pending_count') or 0),
        'submitted': False,
        'submit_status': '',
        'error_text': str(cached.get('error_text') or ''),
        'mode': f"cache:{cached.get('mode','?')}",
        'metrics': dict(cached.get('metrics') or {}),
        'cached': True,
        'cached_from_round': int(cached.get('round') or cached.get('round_num') or 0),
    }
