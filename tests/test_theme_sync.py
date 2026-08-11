# tests/test_theme_sync.py
# Power Pool 주간 테마 자동 동기화: 문서 파서(실측 텍스트, 오타 날짜행 포함).
import datetime as dt

from server import constraint_spec, theme_sync

# 2026-07-27 실측 스크랩 (7월 문서 — '27 29 29' 오타 행 그대로)
ARTICLE = """Current month Power Pool Themes
 MA37901
20 days ago Updated ~1 minute read
July
Mo\tTu\tWe\tTh\tFr\tSa\tSu
29\t30\t1 July\t2\t3\t4\t5

region=USA & delay=1 & universe=TOP1000 & neutralization in (slow, fast, slow and fast, ram, statistical, crowding) & datasets not in ['pv1']

6\t7\t8\t9\t10\t11\t12

region=USA & delay=1 & universe=TOP1000 & neutralization in (slow, fast, slow and fast, ram, statistical, crowding) & datasets not in ['pv1']

13\t14\t15\t16\t17\t18\t19

region=USA & delay=1 & universe=TOP1000 & High Turnover returns ratio test PASS & datasets not in ['pv1']

20\t21\t22\t23\t24\t25\t26

region=USA & delay=1 & universe=TOP1000 & High Turnover returns ratio test PASS & datasets not in ['pv1']

27\t29\t29\t30\t31\t1 August\t2

region=GLB & delay=1 & universe=TOPDIV3000 and neutralization in (slow, fast, slow and fast, ram, crowding) and datasets not in ['pv1', 'model110']
"""

NOW = dt.datetime(2026, 7, 27, 5, 0, tzinfo=dt.timezone.utc)


def test_parse_week_themes_dates_and_order():
    weeks = theme_sync.parse_week_themes(ARTICLE, now_utc=NOW)
    assert [w[0] for w in weeks] == [
        dt.date(2026, 6, 29), dt.date(2026, 7, 6), dt.date(2026, 7, 13),
        dt.date(2026, 7, 20), dt.date(2026, 7, 27)]
    assert 'High Turnover' in weeks[2][1]
    assert weeks[4][1].startswith('region=GLB')


def test_current_theme_selects_this_week():
    t = theme_sync.current_theme(ARTICLE, now_utc=NOW)
    assert t.startswith('region=GLB & delay=1 & universe=TOPDIV3000')
    # 지난주 (7/26 일요일) 이었다면 HT 테마
    last_week = dt.datetime(2026, 7, 26, 23, 0, tzinfo=dt.timezone.utc)
    assert 'High Turnover' in theme_sync.current_theme(ARTICLE, now_utc=last_week)


def test_monday_boundary_utc():
    # 월요일 00:00 UTC(= KST 월 09:00) 직전/직후 경계
    just_before = dt.datetime(2026, 7, 26, 23, 59, tzinfo=dt.timezone.utc)
    just_after = dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc)
    assert 'High Turnover' in theme_sync.current_theme(ARTICLE, now_utc=just_before)
    assert theme_sync.current_theme(ARTICLE, now_utc=just_after).startswith('region=GLB')


def test_garbage_returns_empty():
    assert theme_sync.parse_week_themes('no themes here', now_utc=NOW) == []
    assert theme_sync.current_theme('', now_utc=NOW) is None


def test_manual_dataset_overlay_follows_new_week():
    """추가 금지 데이터셋 때문에 주간 자동 갱신 전체가 멈추면 안 된다."""
    old = ("region=GLB & delay=1 & universe=TOPDIV3000 & "
           "neutralization in (slow, fast, slow and fast, ram, crowding) & "
           "datasets not in ['pv1', 'model110']")
    current = ("region=GLB & delay=1 & universe=TOPDIV3000 & "
               "neutralization in (slow, fast, slow and fast, ram, crowding) & "
               "datasets not in ['pv1', 'model110', 'institutions18']")
    new = ("region=GLB & delay=1 & universe=TOPDIV3000 & "
           "High Turnover returns ratio test PASS & "
           "datasets not in ['model110']")

    merged = theme_sync._merge_manual_overlay(current, old, new)
    spec = constraint_spec.parse(merged)
    assert spec.required_checks == ('HT_HIGH_TURNOVER_RETURNS_RATIO',)
    assert spec.neutralizations == ()
    assert spec.excluded_datasets == frozenset({'model110', 'institutions18'})


def test_manual_scope_override_is_not_replaced():
    old = 'region=GLB & delay=1 & universe=TOPDIV3000'
    current = 'region=USA & delay=1 & universe=TOP1000'
    new = 'region=GLB & delay=1 & universe=TOPDIV3000'
    assert theme_sync._merge_manual_overlay(current, old, new) is None
