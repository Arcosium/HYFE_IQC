# tests/test_focus_ladder_cut.py
# 고칠 수 없는 실패는 focus 연마에서 손절한다:
#   IS_LADDER_SHARPE (8/1 밤 라운드 20여 개 낭비) ·
#   PROD_CORRELATION 대폭 초과 = 축 소진 (8/2 #51=0.8428 에 focus 3라운드 낭비)
from server.focus_priority import (HOPELESS_SCORE, NEUTRAL_SCORE,
                                   closeness_score)


def test_ladder_fail_is_hopeless_not_neutral():
    s = closeness_score(['IS Ladder Sharpe of 0.05 is below cutoff of 1.58'])
    assert s == HOPELESS_SCORE
    # worker 필터 창(NEUTRAL < s < FLOOR=-0.8)에 걸려야 큐에서 제외된다.
    assert NEUTRAL_SCORE < HOPELESS_SCORE < -0.8


def test_ladder_name_only_also_cut():
    assert closeness_score(['IS_LADDER_SHARPE']) == HOPELESS_SCORE


def test_near_miss_still_scores():
    s = closeness_score(['Sharpe of 1.20 is below cutoff of 1.25'])
    assert -0.1 < s < 0


def test_prod_far_over_cut_is_hopeless():
    # 거절 요약 표기 — 다른 near-miss 가 섞여 있어도 PROD 초과가 이긴다
    items = ['LOW_GLB_EMEA_SHARPE(0.97 vs 1)', 'PROD_CORRELATION(0.8428 vs 0.7)']
    assert closeness_score(items) == HOPELESS_SCORE
    # WQB 원문 표기
    assert closeness_score(
        ['Prod Correlation of 0.84 is above cutoff of 0.7']) == HOPELESS_SCORE
    # dict 표기
    assert closeness_score(
        [{'name': 'PROD_CORRELATION', 'value': 0.8, 'cutoff': 0.7}]) == HOPELESS_SCORE


def test_prod_near_cut_still_scores():
    s = closeness_score(['PROD_CORRELATION(0.72 vs 0.7)',
                         'Sharpe of 1.20 is below cutoff of 1.25'])
    assert s != HOPELESS_SCORE and -0.2 < s < 0
