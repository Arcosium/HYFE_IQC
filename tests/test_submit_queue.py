# tests/test_submit_queue.py
# 제출 대기 큐(2026-07-27): 테마 보류·예산 초과 큐잉 db 헬퍼.
import pytest

from server import db


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    tmp_db = str(tmp_path / 'sq.db')
    monkeypatch.setattr(db, 'DB_PATH', tmp_db)
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def test_add_list_dedup(isolated_db):
    uid = isolated_db
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='theme',
                               code='rank(x)', note='PURE',
                               metrics={'sharpe': '1.5'}) is True
    # 같은 (user, wid, kind) 중복은 무시
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='theme') is False
    # 같은 wid 라도 kind 다르면 별개
    assert db.submit_queue_add(uid, wqb_alpha_id='A1', kind='budget') is True
    # wid 없으면 거부
    assert db.submit_queue_add(uid, wqb_alpha_id='', kind='theme') is False
    rows = db.submit_queue_list(uid)
    assert len(rows) == 2
    assert rows[-1]['metrics']['sharpe'] == '1.5'


def test_mark_and_next_pending(isolated_db):
    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='B1', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='B2', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='T1', kind='theme')
    nxt = db.submit_queue_next_pending(uid, kind='budget')
    assert nxt['wqb_alpha_id'] == 'B1'          # 오래된 것부터
    db.submit_queue_mark(nxt['id'], 'submitted', 'ok')
    assert db.submit_queue_get(nxt['id'])['status'] == 'submitted'
    assert db.submit_queue_next_pending(uid, kind='budget')['wqb_alpha_id'] == 'B2'
    # theme 은 자동 드레인 대상이 아니다 — budget 조회에 안 섞임
    db.submit_queue_mark(db.submit_queue_next_pending(uid, 'budget')['id'], 'rejected')
    assert db.submit_queue_next_pending(uid, kind='budget') is None


def test_drain_fills_daily_budget_and_respects_list_mode(isolated_db, monkeypatch):
    """예산 리셋(KST 13:00) 뒤 대기분은 **한도까지 연속** 자동 제출한다.
    단 제출 방식이 '목록에 추가'(list)면 한 건도 자동 제출하지 않는다."""
    from server import worker as w

    uid = isolated_db
    for wid in ('Q1', 'Q2', 'Q3', 'Q4', 'Q5'):
        db.submit_queue_add(uid, wqb_alpha_id=wid, kind='budget')

    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = __import__('threading').Event()
    monkeypatch.setattr(w, '_db', db)
    monkeypatch.setattr(w, 'DAILY_SUBMIT_BUDGET', 4)
    sent = []
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: len(sent))
    monkeypatch.setattr(w.Worker, '_submit_gate',
                        lambda self, *a, **k: (True, ''))
    monkeypatch.setattr(w.Worker, '_log', lambda self, *a, **k: None)
    monkeypatch.setattr(w.Worker, '_log_quiet', lambda self, *a, **k: None)

    class FakeClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): return True
        def set_alpha_description(self, *a, **k): pass
        def submit_alpha(self, wid, **k):
            sent.append(wid)
            return True, 'submitted'

    from server import wqb_api
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeClient)

    db.set_submit_mode(uid, 'list')
    wk._drain_submit_queue(1, 'u', 'p')
    assert sent == [], '목록 모드인데 자동 제출됐다'

    db.set_submit_mode(uid, 'auto')
    wk._drain_submit_queue(1, 'u', 'p')
    assert sent == ['Q1', 'Q2', 'Q3', 'Q4'], f'한도까지 안 비웠다: {sent}'
    assert db.submit_queue_next_pending(uid, kind='budget')['wqb_alpha_id'] == 'Q5'


def test_drain_runs_on_a_timer_not_only_at_round_start(isolated_db, monkeypatch):
    """예산 리셋이 라운드 중간에 걸려도 큐가 그 라운드 끝까지 묵으면 안 된다.

    2026-07-29 실측: 13:00 에 예산이 열렸는데 12:31 시작 라운드 때문에 45분을 기다렸다.
    티커 스레드가 주기적으로 드레인을 돌린다.
    """
    import threading
    from server import worker as w

    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='T1', kind='budget')
    db.set_submit_mode(uid, 'auto')

    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = threading.Event()
    drained = threading.Event()
    monkeypatch.setattr(w, '_DRAIN_TICK_S', 0.01)
    monkeypatch.setattr(w._db, 'get_user_credentials', lambda _uid: ('u', 'p', None))
    monkeypatch.setattr(w.Worker, '_drain_submit_queue',
                        lambda self, rn, u, p: drained.set())

    t = threading.Thread(target=wk._drain_ticker, daemon=True)
    t.start()
    assert drained.wait(3), '티커가 드레인을 부르지 않았다'
    wk._stop_event.set()
    t.join(timeout=3)


def test_drain_skips_wqb_budget_lookup_when_queue_is_empty(isolated_db, monkeypatch):
    """큐가 비면 WQB 실측 조회까지 가면 안 된다 — 5분마다 헛 API 호출이 된다."""
    from server import worker as w

    uid = isolated_db
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = __import__('threading').Event()
    called = []
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: called.append(1) or 0)
    assert wk._drain_one(0, 'u', 'p') is None
    assert not called, '빈 큐인데 예산 조회를 했다'


def test_success_removes_from_queue_rejection_stays(isolated_db, monkeypatch):
    """성공은 목록에서 없애고, 거절은 남긴다 (2026-07-29 사장 지시).

    목록은 '아직 낼 것' 만 보여야 한다. 제출 기록은 제출 내역에 따로 남으므로
    큐에서 지워도 이력이 사라지지 않는다. 거절은 사람이 보고 판단할 몫이라 남긴다.
    """
    import threading
    from server import worker as w

    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='OK1', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='NO1', kind='budget')
    db.set_submit_mode(uid, 'auto')

    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = threading.Event()
    monkeypatch.setattr(w, 'DAILY_SUBMIT_BUDGET', 4)
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: 0)
    monkeypatch.setattr(w.Worker, '_submit_gate', lambda self, *a, **k: (True, ''))
    monkeypatch.setattr(w.Worker, '_log', lambda self, *a, **k: None)
    monkeypatch.setattr(w.Worker, '_log_quiet', lambda self, *a, **k: None)

    class FakeClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): return True
        def set_alpha_description(self, *a, **k): pass
        def submit_alpha(self, wid, **k):
            return (True, 'submitted') if wid == 'OK1' else (False, 'rejected:LOW_FITNESS')

    from server import wqb_api
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeClient)

    wk._drain_submit_queue(0, 'u', 'p')

    left = {r['wqb_alpha_id']: r['status'] for r in db.submit_queue_list(uid, limit=20)}
    assert 'OK1' not in left, '제출 성공 건이 목록에 남아 있다'
    assert left.get('NO1') == 'rejected', '거절 건이 사라졌거나 상태가 틀리다'
    assert db.submit_queue_next_pending(uid, kind='budget') is None, '거절 건을 자동 재시도하면 안 된다'


def test_two_drainers_cannot_grab_the_same_row(isolated_db, monkeypatch):
    """드레인이 겹쳐 돌아도 같은 알파를 두 번 제출하면 안 된다.

    2026-07-29 실측: 라운드 훅과 티커가 같은 pending 행을 집어 A17rzm9R 를 두 번 냈다
    (제출은 4분 걸려 그동안 계속 pending 이었다). 네트워크 전에 'submitting' 으로 선점한다.
    """
    import threading
    from server import worker as w

    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='ONE', kind='budget')
    db.set_submit_mode(uid, 'auto')

    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = threading.Event()
    monkeypatch.setattr(w, 'DAILY_SUBMIT_BUDGET', 4)
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: 0)
    monkeypatch.setattr(w.Worker, '_submit_gate', lambda self, *a, **k: (True, ''))
    monkeypatch.setattr(w.Worker, '_log', lambda self, *a, **k: None)
    monkeypatch.setattr(w.Worker, '_log_quiet', lambda self, *a, **k: None)

    sent, in_flight = [], threading.Event()

    class SlowClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): return True
        def set_alpha_description(self, *a, **k): pass
        def submit_alpha(self, wid, **k):
            sent.append(wid)
            in_flight.set()
            _time.sleep(0.3)              # 실제로도 수 분 걸린다
            return False, 'rejected:LOW_FITNESS'

    import time as _time
    from server import wqb_api
    monkeypatch.setattr(wqb_api, 'WqbApiClient', SlowClient)

    t = threading.Thread(target=wk._drain_one, args=(0, 'u', 'p'), daemon=True)
    t.start()
    assert in_flight.wait(3), '첫 드레인이 시작되지 않았다'
    # 제출이 아직 진행 중인 사이에 두 번째 드레인이 같은 행을 집으면 안 된다
    assert wk._drain_one(0, 'u', 'p') is None, '진행 중인 행을 다른 드레인이 또 집었다'
    t.join(timeout=5)
    assert sent == ['ONE'], f'같은 알파를 중복 제출했다: {sent}'


def test_rejected_alpha_gets_exactly_one_more_try(isolated_db, monkeypatch):
    """제출 판정은 주마다 바뀐다 — 403 한 번으로 영구 폐기하지 않고 한 번 더 낸다.

    단 **한 번만**. 두 번째 거절은 확정이다(무한 재시도로 WQB 를 두드리지 않는다).
    지금 FAIL 이 남아 있는 알파는 애초에 재시도 대상이 아니다.
    """
    import threading
    from server import worker as w

    uid = isolated_db
    db.submit_queue_add(uid, wqb_alpha_id='WEEKLY', kind='budget')
    db.submit_queue_add(uid, wqb_alpha_id='TRULYBAD', kind='budget')
    db.set_submit_mode(uid, 'auto')

    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._stop_event = threading.Event()
    monkeypatch.setattr(w, 'DAILY_SUBMIT_BUDGET', 4)
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: 0)
    monkeypatch.setattr(w.Worker, '_submit_gate', lambda self, *a, **k: (True, ''))
    monkeypatch.setattr(w.Worker, '_log', lambda self, *a, **k: None)
    monkeypatch.setattr(w.Worker, '_log_quiet', lambda self, *a, **k: None)

    tries = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): return True
        def set_alpha_description(self, *a, **k): pass
        def submit_alpha(self, wid, **k):
            tries.append(wid)
            return False, 'rejected:LOW_SHARPE(1.5 vs 1.58)'
        def harvest_alpha(self, wid):
            fails = [] if wid == 'WEEKLY' else [{'name': 'LOW_SHARPE'}]
            return {'metrics': {}, 'is_status': {'pass': [], 'fail': fails,
                                                 'error': [], 'pending': []}}

    from server import wqb_api
    monkeypatch.setattr(wqb_api, 'WqbApiClient', FakeClient)

    wk._drain_submit_queue(0, 'u', 'p')          # 1회차
    st = {r['wqb_alpha_id']: r['status'] for r in db.submit_queue_list(uid, limit=20)}
    assert st.get('WEEKLY') == 'pending', 'FAIL 0 인데 재시도 기회를 안 줬다'
    assert st.get('TRULYBAD') == 'rejected', 'FAIL 남은 알파까지 되살리면 안 된다'

    wk._drain_submit_queue(0, 'u', 'p')          # 2회차 — 재시도분 소진
    st = {r['wqb_alpha_id']: r['status'] for r in db.submit_queue_list(uid, limit=20)}
    assert st.get('WEEKLY') == 'rejected', '두 번째 거절은 확정이어야 한다'
    assert tries.count('WEEKLY') == 2, f'재시도가 정확히 1회가 아니다: {tries}'


# ── 큐 행은 code 를 실어야 한다 (2026-07-30 사장 지적) ───────────────────────

def test_gate_queued_rows_carry_the_code(isolated_db, monkeypatch):
    """게이트가 큐에 넣는 3경로(list·보류창·예산초과) 모두 code 를 실어야 한다.

    UI '제출 대기' 행 클릭 → 알파 상세는 alpha_pk 아니면 code 로만 찾는다.
    게이트 시점엔 alphas 행이 아직 없어 pk 가 없으므로, code 가 빠지면
    그 행은 영영 클릭이 죽는다 (실측: q47~49 가 code='' 로 들어갔다).
    """
    import time as _t

    from server import run_config, worker as w

    uid = isolated_db
    code = 'rank(-1 * ts_delta(close, 5))'

    def gate(wid):
        wk = w.Worker.__new__(w.Worker)
        wk.user_id = uid
        wk._corr_fs_hold = set()
        return wk._submit_gate({'wqb_alpha_id': wid}, None, fail_items=[], code=code)

    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: 0.0)
    monkeypatch.setattr(w, 'DAILY_SUBMIT_BUDGET', 4)
    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: 0)

    db.set_submit_mode(uid, 'list')
    assert gate('QLIST')[1] == 'submit_mode=list→queued'
    db.set_submit_mode(uid, 'auto')

    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: _t.time() + 3600)
    assert gate('QHOLD')[1].startswith('submit_hold')
    monkeypatch.setattr(run_config, 'get_submit_hold_until', lambda: 0.0)

    monkeypatch.setattr(w.Worker, '_submitted_today', lambda self: 4)
    assert gate('QBUDGET')[1].startswith('daily_budget')

    got = {r['wqb_alpha_id']: r['code'] for r in db.submit_queue_list(uid, limit=20)}
    assert set(got) == {'QLIST', 'QHOLD', 'QBUDGET'}, f'큐잉 자체가 안 됐다: {got}'
    for wid, c in got.items():
        assert c == code, f'{wid} 행에 code 가 안 실렸다: {c!r}'


def test_queue_rejection_reason_lands_on_the_alpha(isolated_db):
    """큐에서 거절되면 사유가 알파 행에 남아야 한다 — 상세의 '상태' 가 그걸 읽는다.

    큐 행은 성공하면 삭제되고 pk 도 없을 수 있으니, code 로 알파를 찾아 기록한다.
    pk 만 보던 동안 상세는 'submit_skipped:…→queued' 에 멈춰 있었다(2026-07-30).
    """
    uid = isolated_db
    code = 'rank(ts_std_dev(returns, 20))'
    rid = db.start_round(uid, 1)
    db.insert_alpha(uid, rid, 1, {'code': code, 'idx': 1, 'metrics': {'sharpe': '1.2'}})
    a = db.get_alpha_by_code(uid, code)
    assert a and a['submit_status'] in (None, '', 'submit_skipped:'), a['submit_status']

    reason = 'rejected:LOW_SHARPE(1.2 vs 1.58); LOW_FITNESS(0.28 vs 1.0) (http_403)'
    db.set_alpha_submit_result(0, False, reason, user_id=uid, code=code)
    assert db.get_alpha_by_code(uid, code)['submit_status'] == reason

    # 남의 알파는 절대 건드리지 않는다
    other = db.upsert_user('o@x.com', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    db.set_alpha_submit_result(0, False, 'rejected:HACK', user_id=other, code=code)
    assert db.get_alpha_by_code(uid, code)['submit_status'] == reason
    # pk·code 둘 다 없으면 조용히 아무것도 안 한다
    db.set_alpha_submit_result(0, False, 'rejected:NOPE', user_id=uid, code='')
    assert db.get_alpha_by_code(uid, code)['submit_status'] == reason
