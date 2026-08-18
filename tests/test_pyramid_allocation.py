# tests/test_pyramid_allocation.py
# 2026-08-18 사장 지적: 알파는 느는데 피라미드가 안 는다.
# 실측 — 최근 14일 제출 27건 중 **21건이 GLB/D1/PV 한 칸**. 칸은 3건이면 차는데
# 거기 21건을 쌓는 동안 피라미드는 그대로였고 상관만 올라 계보가 제출 불능이 됐다.
# 막지는 않고(A안) 앞 칸만 예약하는 B안으로 간다.
import pytest

from server import worker as w


@pytest.fixture
def wk(monkeypatch):
    obj = object.__new__(w.Worker)
    obj.user_id = 2
    monkeypatch.setattr(w, 'PYRAMID_RESERVE', 2)
    return obj


def _pyr(monkeypatch, have: dict, minimum: int = 3):
    """실제 모듈의 counts 를 갈아 끼운다.

    ⚠ sys.modules 를 바꾸는 방식은 쓰지 않는다 — 다른 테스트가 이미 임포트해 둔
      `server.pyramids` 참조에는 안 먹어서 단독 실행만 통과하고 전체에선 깨진다
      (2026-08-18 실측).
    """
    from server import pyramids as _pyr_mod
    monkeypatch.setattr(_pyr_mod, 'counts', lambda uid, now=None: have)
    monkeypatch.setattr(_pyr_mod, 'PYRAMID_MIN', minimum)
    return _pyr_mod


def test_saturated_cell_is_deferred_while_slots_are_reserved(monkeypatch, wk):
    """앞 2칸은 미개척 칸 몫 — 이미 3건 찬 칸의 알파는 그동안 뒤로 민다."""
    _pyr(monkeypatch, {'GLB/D1/PV': 21})
    m = {'pyramids': 'GLB/D1/PV'}
    assert w._pyramid_defer(2, m, used=0, now=0) is True
    assert w._pyramid_defer(2, m, used=1, now=0) is True


def test_reserve_opens_after_the_first_slots(monkeypatch, wk):
    """예약을 넘기면 포화 칸도 낸다 — 쿼터를 통째로 버리지 않는다(B안의 핵심)."""
    _pyr(monkeypatch, {'GLB/D1/PV': 21})
    assert w._pyramid_defer(2, {'pyramids': 'GLB/D1/PV'}, used=2, now=0) is False


def test_unexplored_cell_is_never_deferred(monkeypatch, wk):
    """미달 칸이 하나라도 있으면 그대로 낸다 — 그게 우리가 원하는 알파다."""
    _pyr(monkeypatch, {'GLB/D1/PV': 21})
    m = {'pyramids': 'GLB/D1/PV,GLB/D1/ANALYST'}
    assert w._pyramid_defer(2, m, used=0, now=0) is False


def test_unknown_cell_is_never_deferred(monkeypatch, wk):
    """칸을 모르면 미루지 않는다 — 새 데이터셋이 통째로 늦어지면 안 된다."""
    _pyr(monkeypatch, {'GLB/D1/PV': 21})
    assert w._pyramid_defer(2, {'pyramids': ''}, used=0, now=0) is False


def test_reserve_is_released_near_the_day_boundary(monkeypatch, wk):
    """마감이 가까우면 예약을 푼다 — 미개척 알파가 안 나온 날 쿼터를 버리지 않는다."""
    _pyr(monkeypatch, {'GLB/D1/PV': 21})
    monkeypatch.setattr('server.submit_push._day0', lambda now: now - 86400.0 + 600.0)
    assert w._pyramid_defer(2, {'pyramids': 'GLB/D1/PV'}, used=0, now=1_000_000.0) is False


def test_saturation_needs_every_cell_full(monkeypatch, wk):
    """칸이 여럿이면 **전부** 차야 포화다 — 하나라도 비면 그 알파가 그 칸을 연다."""
    _pyr(monkeypatch, {'GLB/D1/PV': 5, 'GLB/D1/RISK': 1})
    assert w._pyramid_saturated(2, {'pyramids': 'GLB/D1/PV,GLB/D1/RISK'}) is False
    assert w._pyramid_saturated(2, {'pyramids': 'GLB/D1/PV'}) is True
