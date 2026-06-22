# tests/test_worker.py
import server.worker as worker


def test_round_label_base_round():
    """탐색(base) 라운드 = 정수 라벨 그대로 (phase 0)."""
    assert worker._round_label(3, 0, 0) == '3'
    assert worker._round_label(701, 0, 0) == '701'


def test_round_label_focus_includes_parent_alpha_and_depth():
    """focus 라운드 = {base}-{부모알파}-{개선깊이} (예: 2-2-3)."""
    assert worker._round_label(2, 2, 3) == '2-2-3'
    assert worker._round_label(701, 5, 1) == '701-5-1'
    # phase(깊이) 가 0 이면 base 취급 — 부모 정보 없음.
    assert worker._round_label(4, 7, 0) == '4'
