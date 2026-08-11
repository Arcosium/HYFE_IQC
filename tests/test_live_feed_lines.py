# tests/test_live_feed_lines.py
# 라이브 피드 가독성 (2026-07-27 사장 지적): 제출을 안 한 **진짜 이유**가 보여야 하고,
# 라운드 요약은 체크 개수만이 아니라 품질·제출 결과를 말해야 한다.
from server import worker as w


# ── 제출 생략 사유 ──────────────────────────────────────────────────────────
# 예전엔 submit_skipped:* 를 전부 '(pause)' 로 뭉개, 예산 소진·목록 모드로 안 낸 것까지
# 일시정지처럼 보였다. 사유가 이미 문자열에 있는데 버리고 있었다.

def test_budget_exhaustion_is_not_reported_as_pause():
    out = w._skip_reason_ko('submit_skipped:daily_budget(4/4)→queued')
    assert '일시정지' not in out
    assert '예산' in out and '4/4' in out and '대기 큐' in out


def test_list_mode_reason():
    out = w._skip_reason_ko('submit_skipped:submit_mode=list→queued')
    assert '목록' in out and '대기 큐' in out


def test_real_pause_still_says_pause():
    assert w._skip_reason_ko('submit_skipped:paused') == '일시정지'
    assert w._skip_reason_ko('submit_skipped:') == '일시정지'


def test_blocking_fail_names_the_checks():
    out = w._skip_reason_ko('submit_skipped:blocking_fail(LOW_SHARPE,LOW_FITNESS)')
    assert 'LOW_SHARPE' in out


def test_unknown_reason_is_passed_through_not_swallowed():
    assert 'something_new' in w._skip_reason_ko('submit_skipped:something_new(1)')


def test_submit_rejection_reason_is_not_truncated():
    reason = 'PURE_POWER_POOL_THEME_' + 'VERY_LONG_DETAIL_' * 8
    out = w._submit_result_tag(False, 'rejected:' + reason)
    assert reason in out
    assert out.endswith(reason + ')')


# ── 라운드 한 줄 요약 ───────────────────────────────────────────────────────

def _r(idx, sharpe=None, submit_status=''):
    return {'idx': idx, 'submit_status': submit_status,
            'metrics': ({'sharpe': str(sharpe)} if sharpe is not None else {})}


def test_headline_reports_best_sharpe_and_submissions():
    out = w._round_headline([
        _r(1, 0.42), _r(3, 1.21, 'submitted'), _r(5, 0.88),
        _r(7, 0.10, 'submit_skipped:daily_budget(4/4)→queued'),
    ])
    assert '최고 S=1.21(#3)' in out
    assert '제출 1' in out
    assert '대기큐 +1' in out


def test_headline_is_empty_when_there_is_nothing_to_say():
    """지표가 없으면 요약 형식을 깨지 않고 빈 문자열."""
    assert w._round_headline([]) == ''
    assert w._round_headline([_r(1)]) == ''


def test_headline_survives_garbage_metrics():
    out = w._round_headline([_r(1, 'n/a'), _r(2, 1.5), None, {}])
    assert '최고 S=1.50' in out


# ── 제출 내역이 스킵에 밀려 사라지지 않는다 (2026-07-27 사장 지적) ───────────
# 오늘 시도 54건 중 50건이 게이트 스킵이라, limit=50 이 스킵으로 다 채워지고
# **성공 제출 1건이 화면에서 사라졌다**. limit 은 '보이는 행' 에 걸려야 한다.

def test_submit_history_limit_counts_visible_rows_not_skips(tmp_path, monkeypatch):
    from server import db
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'sh.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')

    # 실제 제출 1건이 **먼저**, 그 뒤에 스킵 60건 — 옛 동작이면 제출이 잘려 나간다
    db.record_submit_attempt(uid, 1, 1, 'rank(close)', True, 'submitted')
    for i in range(60):
        db.record_submit_attempt(uid, 1, i + 2, f'rank(x{i})', False,
                                 'submit_skipped:blocking_fail(LOW_SHARPE)')

    # 거절 1건도 섞어 둔다 — '제출 내역' 은 성공만 보여야 한다(2026-07-27 사장 지시)
    db.record_submit_attempt(uid, 1, 99, 'rank(y)', False, 'rejected:PROD_CORRELATION')

    rows = db.list_submit_attempts(uid, limit=50)
    assert [r['submit_status'] for r in rows] == ['submitted'], \
        f'성공만 보여야 하는데 다른 것이 섞였다: {[r["submit_status"] for r in rows]}'

    # 감사용으로는 여전히 전부 보인다
    allrows = db.list_submit_attempts(uid, limit=200, scope='all')
    assert len(allrows) == 62
    db._INITIALIZED = False
