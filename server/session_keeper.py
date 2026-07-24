"""세션 keep-alive 스레드 — 토큰이 죽기 전에 갱신해 얼굴 인증을 없앤다.

배경(실측 2026-07-12): WQB JWT 는 발급 후 **정확히 4시간** 살고, 클레임에
`amr:['pwd','face']` 가 박힌다. 그런데 기존 코드는 토큰이 **죽은 뒤에야** 재인증했다
— 죽은 세션에서 맨몸 Basic 로그인을 하면 WQB 는 얼굴 인증을 다시 요구한다.
즉 4시간마다 얼굴 인증을 하도록 설계가 보장하고 있었던 셈이다.

이 스레드는 만료 30분 전에 (살아있는 세션 그대로) 토큰을 갱신한다. 갱신이 통하면
얼굴 인증은 사실상 0회가 된다. 안 통하면 손해는 없다 — 세션을 지우지 않으므로
워커는 만료까지 하던 일을 계속하고, 인증이 필요하다는 사실을 30분 일찍 알려줄 뿐이다.
"""
from __future__ import annotations

import logging
import threading
import time

from . import db as _db
from . import wqb_api

LOG = logging.getLogger('genomicwqb.session_keeper')

# 얼마나 자주 만료를 들여다볼지. 갱신 자체는 만료 30분 전(_REFRESH_LEAD_S)에만 한 번.
TICK_S = float(__import__('os').environ.get('IQC_KEEPALIVE_TICK_S', '300'))

_thread: threading.Thread | None = None
_stop = threading.Event()


def _candidates() -> list[int]:
    """API 백엔드를 쓰는 모든 사용자 (워커가 안 돌고 있어도 세션은 살려둔다 —
    사용자가 '진화 실행' 을 누른 그 순간 인증이 살아 있어야 하니까)."""
    try:
        return [int(u['id']) for u in _db.list_users()
                if str(u.get('backend') or '') == 'api']
    except Exception:
        return []


def _tick_once() -> None:
    for uid in _candidates():
        if _stop.is_set():
            return
        try:
            creds = _db.get_user_credentials(uid)
            if not creds:
                continue
            username, password, _ = creds
            c = wqb_api.WqbApiClient(username, password)
            if not c._load_session():
                continue                     # 세션 자체가 없다 — 갱신할 게 없다
            c._load_meta()
            if c._expiry_epoch is None or c._expiry_stale():
                c._ensure_expiry()
            left = c.seconds_to_expiry()
            if left is None:
                continue
            if left <= 0:
                continue                     # 이미 죽었다 — authenticate() 가 처리
            if left > wqb_api._REFRESH_LEAD_S:
                continue                     # 아직 여유 — 건드리지 않는다
            if not c._session_valid():
                continue                     # 죽은 세션으로 갱신을 청하면 persona 를 부른다
            res = c.refresh_token()
            if res == 'refreshed':
                LOG.info('uid=%s 토큰 선제 갱신 성공 (얼굴 인증 회피)', uid)
                try:
                    _db.append_log(uid, 0,
                                   '🔐 WQB 세션 선제 갱신 성공 — 재인증 없이 연장됨',
                                   level='info')
                except Exception:
                    pass
            elif res == 'persona':
                LOG.info('uid=%s 선제 갱신에 persona 요구 — 사용자 인증 필요', uid)
                try:
                    _db.append_log(
                        uid, 0,
                        '🔐 WQB 세션이 곧 만료됩니다 — 편하실 때 얼굴 인증을 완료하면 끊김 없이 '
                        '이어집니다. (안 하셔도 만료 시 조용히 대기하다, 재인증하면 자동 재개합니다)',
                        level='info')
                except Exception:
                    pass
        except Exception as e:
            LOG.warning('keepalive uid=%s 실패(무시): %s', uid, e)


def _loop() -> None:
    LOG.info('session keeper 시작 (tick=%ss, lead=%ss)',
             TICK_S, wqb_api._REFRESH_LEAD_S)
    while not _stop.is_set():
        try:
            _tick_once()
        except Exception:
            LOG.exception('keepalive tick 예외')
        if _stop.wait(timeout=TICK_S):
            break


def start() -> threading.Thread | None:
    """앱 기동 시 1회 호출. 비활성화(IQC_SESSION_KEEPALIVE=0)면 아무것도 안 한다."""
    global _thread
    if not wqb_api.SESSION_KEEPALIVE:
        LOG.info('session keeper 비활성 (IQC_SESSION_KEEPALIVE=0)')
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop.clear()
    _thread = threading.Thread(target=_loop, name='iqc-session-keeper', daemon=True)
    _thread.start()
    return _thread


def stop() -> None:
    _stop.set()
