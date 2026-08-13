# tests/test_drain_no_starvation.py
# 대기 큐 드레인은 '보류된 앞줄'에 굶으면 안 된다.
# 2026-08-13 실측 사고 2건:
#   ① _drain_one 이 게이트 보류에 None 을 돌려줘 호출측 루프가 끊겼다 —
#      q94 한 건이 60초마다 다시 집히며 뒤 15건을 40분간 막았다.
#   ② 루프 상한이 일일 예산(4)이라, 영구 보류 4건이 매 틱 4칸을 다 먹어
#      q101~109 가 20분간 한 번도 안 불렸다. 보류는 제출을 안 쓴다.
from server import worker as w


class _Drainer:
    """_drain_submit_queue 만 떼어 쓰기 위한 최소 스텁."""

    def __init__(self, outcomes):
        self.outcomes = dict(outcomes)      # row id → 'hold' | 'done'
        self.seen = []
        self.user_id = 2
        self._stop_event = _Never()
        self._drain = w.Worker._drain_submit_queue.__get__(self, w.Worker)

    def _drain_one(self, round_num, username, password, skip=()):
        rows = [i for i in sorted(self.outcomes) if i not in set(skip)]
        if not rows:
            return None
        rid = rows[0]
        self.seen.append(rid)
        if self.outcomes[rid] == 'done':
            del self.outcomes[rid]          # 성공/거절은 큐에서 내려간다
        return rid


class _Never:
    @staticmethod
    def is_set():
        return False


def _wire(monkeypatch, drainer):
    monkeypatch.setattr(w._db, 'get_submit_mode', lambda uid: 'auto')
    return drainer


def test_held_rows_do_not_starve_the_rest(monkeypatch):
    """앞줄 4건이 영구 보류여도 뒤 행까지 전부 훑는다."""
    d = _wire(monkeypatch, _Drainer({1: 'hold', 2: 'hold', 3: 'hold', 4: 'hold',
                                     5: 'done', 6: 'done'}))
    d._drain(0, 'u', 'p')
    assert d.seen == [1, 2, 3, 4, 5, 6]


def test_budget_is_not_the_scan_limit(monkeypatch):
    """상한을 예산(4)에 묶으면 5번째 행이 영영 안 불린다 — 그게 이 사고였다."""
    d = _wire(monkeypatch, _Drainer({i: 'hold' for i in range(1, 10)}))
    d._drain(0, 'u', 'p')
    assert len(d.seen) == 9 > w.DAILY_SUBMIT_BUDGET


def test_stops_when_drain_one_reports_closed(monkeypatch):
    """_drain_one 이 None 이면(예산 닫힘) 즉시 멈춘다 — 무한 재조회 금지."""
    d = _wire(monkeypatch, _Drainer({}))
    d._drain(0, 'u', 'p')
    assert d.seen == []
