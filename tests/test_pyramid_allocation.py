# tests/test_pyramid_allocation.py
# 2026-08-20 사장 지시: 피라미드 예약(2026-08-18 B안)은 제출을 보류시키지 않는다.
# "일단 쏴보고 실패하면 그때 넣어라" — 실측 사고: 8/19 유일한 게이트 통과작이
# kind='pyramid' 로 큐에 들어갔는데 그 kind 는 드레인 대상이 아니라 쿼터 0.
# 다변화는 생성 단계(submit_push)에서만 민다. 아래는 복원 방지 tripwire 다.
from server import worker as w


def test_pyramid_defer_stays_removed():
    """게이트 쪽 피라미드 보류를 되살리지 말 것 — 사장 지시(2026-08-20)."""
    assert not hasattr(w, '_pyramid_defer')
    assert not hasattr(w, '_pyramid_saturated')
    assert not hasattr(w, 'PYRAMID_RESERVE')


def test_pyramid_short_survives(monkeypatch):
    """_pyramid_short(HT 필수체크 면제 판정)는 남는다 — 미달 칸 우대는 유지."""
    from server import pyramids as _pyr_mod
    monkeypatch.setattr(_pyr_mod, 'counts', lambda uid, now=None: {'GLB/D1/PV': 21})
    monkeypatch.setattr(_pyr_mod, 'PYRAMID_MIN', 3)
    assert w._pyramid_short(2, {'pyramids': 'GLB/D1/PV,GLB/D1/ANALYST'}) is True
    assert w._pyramid_short(2, {'pyramids': 'GLB/D1/PV'}) is False
    assert w._pyramid_short(2, {'pyramids': ''}) is False
