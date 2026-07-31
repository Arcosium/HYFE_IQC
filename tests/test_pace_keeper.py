# tests/test_pace_keeper.py
# 페이스 계기판 — 국면 판정(광맥/미달/기근)과 ε 하한·사람 호출.
# 생성량엔 손대지 않는다(2026-07-31 사장: "라운드당 14개면 충분") —
# 신선 후보의 질은 genome_models.STRONG_TEMPLATES 가 상시 담당.
# '하루'는 WQB 쿼터 리셋(13:00 KST) 경계로 센다.
import time

from server import pace_keeper as pk


def _at(hour, minute=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


def _day0(now):
    lt = time.localtime(now)
    d0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                      pk.DAY_BOUNDARY_HOUR, 0, 0, 0, 0, -1))
    return d0 if d0 <= now else d0 - 86400.0


def _fix_counts(monkeypatch, now, today, week):
    d0 = _day0(now)

    def fake(user_id, since_ts):
        return today if since_ts >= d0 - 1 else week
    monkeypatch.setattr(pk._db, 'pass_count_since', fake)


def test_on_pace_is_level_0(monkeypatch):
    now = _at(18)
    _fix_counts(monkeypatch, now, today=3, week=25)
    assert pk.pace(2, now=now)['level'] == 0


def test_morning_famine_is_level_2(monkeypatch):
    """7/31 아침 실황 — 11시에 오늘(13시 경계 기준) 1개뿐, 리셋까지 2시간.
    자정 기준이면 '정상'으로 오판되던 바로 그 구간이다."""
    now = _at(11)
    _fix_counts(monkeypatch, now, today=1, week=40)
    p = pk.pace(2, now=now)
    assert p['level'] == 2 and p['day_deficit'] > 1.5


def test_slightly_behind_is_level_1(monkeypatch):
    """경계 6시간 뒤(19시)에 0개 — 주간은 넉넉. 탐색 강화만."""
    now = _at(19)
    _fix_counts(monkeypatch, now, today=0, week=30)
    assert pk.pace(2, now=now)['level'] == 1


def test_fresh_day_zero_is_not_famine(monkeypatch):
    """리셋 30분 뒤 0개는 정상이다 — 하루 목표는 경과 시간에 비례."""
    now = _at(13, 30)
    _fix_counts(monkeypatch, now, today=0, week=30)
    assert pk.pace(2, now=now)['level'] == 0


def test_weekly_shortfall_alone_is_level_1(monkeypatch):
    """오늘은 그럭저럭인데 최근 7일 합이 목표 미달 — 기근 전조, 탐색 강화."""
    now = _at(14)
    _fix_counts(monkeypatch, now, today=1, week=10)
    assert pk.pace(2, now=now)['level'] == 1


def test_famine_raises_epsilon_floor_and_vein_does_not(monkeypatch):
    monkeypatch.setattr(pk._db, 'error_count_like', lambda *a: 0)
    monkeypatch.setattr(pk, 'pace', lambda uid, now=None: {
        'level': 2, 'today': 0, 'week': 5, 'day_deficit': 2.0, 'week_deficit': 20})
    assert pk.maybe_intervene(2)['epsilon_boost'] == pk.EPSILON_FLOOR
    monkeypatch.setattr(pk, 'pace', lambda uid, now=None: {
        'level': 0, 'today': 4, 'week': 40, 'day_deficit': -2.0, 'week_deficit': -15})
    assert pk.maybe_intervene(2)['epsilon_boost'] is None   # 광맥 국면 — 착취 유지


def test_auth_dead_calls_human(monkeypatch):
    monkeypatch.setattr(pk, 'pace', lambda uid, now=None: {
        'level': 0, 'today': 4, 'week': 40, 'day_deficit': -2.0, 'week_deficit': -15})
    monkeypatch.setattr(pk._db, 'error_count_like', lambda uid, ts, pat: 6)
    msgs = []
    pk.maybe_intervene(2, log_fn=msgs.append)
    assert any('생체 인증' in m for m in msgs)
