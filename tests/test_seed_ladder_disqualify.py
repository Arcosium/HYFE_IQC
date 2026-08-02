# tests/test_seed_ladder_disqualify.py
# ladder 사망 알파는 GA 부모(엘리트·명예의전당) 자격 박탈 — sharpe 만 보는
# selection_score 가 밤샘 고샤프 ladder-dead 클러스터를 풀에 눌러앉히던 것 방지
# (8/2 오후 실측: 게이트 시도 106중 91 ladder 실패, 같은 가계 변주만 생산).
from server.db import _hydrate_alpha_row

ROW = {
    'id': 1, 'code': 'rank(x)', 'code_hash': 'h', 'desc': '',
    'pass_count': 5, 'fail_count': 1, 'error_count': 0,
    'metrics': '{"sharpe": 3.0, "fitness": 1.7}',
    'round_num': 1, 'idx': 0, 'universe': 'TOPDIV3000',
    'neutralization': 'CROWDING', 'decay': 4, 'truncation': 0.08,
    'self_corr': None, 'generation': 1,
    'genome': '{"model": "rc-api-genome"}',
    'fail_items': '["LOW_FITNESS"]',
}


def test_ladder_dead_row_disqualified():
    row = dict(ROW, fail_items='["LOW_SHARPE", "IS_LADDER_SHARPE"]')
    assert _hydrate_alpha_row(row) is None


def test_low_2y_row_disqualified():
    # LOW_2Y_SHARPE = 같은 검사의 단일데이터셋 이름 — 함께 실격
    row = dict(ROW, fail_items='["LOW_SUB_UNIVERSE_SHARPE", "LOW_2Y_SHARPE"]')
    assert _hydrate_alpha_row(row) is None


def test_non_ladder_row_survives():
    d = _hydrate_alpha_row(dict(ROW))
    assert d is not None and d['_sharpe'] == 3.0
