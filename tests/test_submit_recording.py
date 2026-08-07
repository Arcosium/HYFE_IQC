# tests/test_submit_recording.py
# 2026-08-07 사장 지적: "재확인 시 제출 완료라고 뜬 것도 제출 카운트도 안 올라가고
# 제출 내역에도 안 뜬다. 실제 들어가 보면 하나 제출됐다."
# 원인 — 제출 성사를 alphas 행에만 적고 submit_attempts 를 빠뜨린 경로가 둘 있었다
# (끊긴 제출 재시도 · 무료체크 발 제출). 카운트도 내역도 submit_attempts.submitted=1
# 만 세므로, 알파 상세만 '제출됨' 이 되고 나머지는 전부 침묵했다.
# 실측 유실 건: 8/6 23:08 재시도로 성사된 vRNxL9Lz.
import threading

import pytest

from server import db
from server import worker as w


@pytest.fixture
def wk(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 's.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('s@x.com', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    obj = w.Worker.__new__(w.Worker)
    obj.user_id = uid
    obj._stop_event = threading.Event()
    obj._lock = threading.Lock()
    obj._corr_fs_hold = set()
    yield obj
    db._INITIALIZED = False


def test_recorded_submit_shows_up_in_count_and_history(wk):
    wk._record_submit(7, True, 'submitted (이미 제출됨 — stage=OS 확인)',
                      code='rank(close)', wid='vRNxL9Lz', tag='[retry] ')
    assert db.submitted_today(wk.user_id) == 1
    assert db.submitted_count(wk.user_id) == 1
    hist = db.list_submit_attempts(wk.user_id)
    assert len(hist) == 1 and hist[0]['code'] == 'rank(close)'
    assert hist[0]['submit_status'].startswith('[retry] ')


def test_failed_submit_does_not_inflate_the_count(wk):
    wk._record_submit(7, False, 'submit_pending_timeout:1.0', wid='vRNxL9Lz')
    assert db.submitted_today(wk.user_id) == 0
    assert db.list_submit_attempts(wk.user_id) == []          # 내역은 성공만
    assert db.list_submit_attempts(wk.user_id, scope='all')   # 감사용엔 남는다


def test_retry_path_records_the_recovered_submission(wk, monkeypatch):
    """끊긴 제출 회수가 성사되면 카운트에 잡혀야 한다 (유실된 그 경로)."""
    import server.wqb_api as api
    monkeypatch.setattr(w._db, 'stuck_submits', lambda uid: [
        {'id': 0, 'code': 'rank(close)', 'genome': None,
         'submit_status': 'submit_pending_timeout:1.0',
         'metrics': {'wqb_alpha_id': 'vRNxL9Lz'}}])
    monkeypatch.setattr(w.Worker, '_submit_gate', lambda self, *a, **k: (True, ''))
    monkeypatch.setattr(api.WqbApiClient, '__init__', lambda self, u, p: None)
    monkeypatch.setattr(api.WqbApiClient, 'authenticate', lambda self: True)
    monkeypatch.setattr(api.WqbApiClient, 'set_alpha_description',
                        lambda self, wid, d: None)
    monkeypatch.setattr(api.WqbApiClient, 'submit_alpha',
                        lambda self, wid, **k: (True, 'submitted (stage=OS 확인)'))
    monkeypatch.setattr(w.Worker, '_log', lambda self, *a, **k: None)
    monkeypatch.setattr(w.Worker, '_log_quiet', lambda self, *a, **k: None)

    wk._retry_stuck_submits(7, 'u', 'p')
    assert db.submitted_today(wk.user_id) == 1

# POST 생략(이미 OS)은 tests/test_wqb_api.py::test_submit_alpha_skips_post_when_already_os.


# ── 진화 궤적: 세대 이어붙이기 (2026-08-07 사장 지적 "진화 궤적도 이상한데") ──
# combine·ht_rescue·hunt·improve 는 유전체 없이 코드만 만드는 레이어다. 유전체만
# 보면 이 넷의 자식이 전부 g0 으로 찍혀 계보가 매 라운드 리셋된 것처럼 보인다.

def test_generation_comes_from_the_genome_when_it_has_one():
    assert w._child_generation({'generation': 7}, 3) == 7


def test_generation_falls_back_to_parent_plus_one():
    assert w._child_generation(None, 11) == 12
    assert w._child_generation({}, 0) == 1        # 부모가 g0 이어도 자식은 g1


def test_generation_is_zero_without_genome_or_parent():
    assert w._child_generation(None, None) == 0
