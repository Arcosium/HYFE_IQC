# tests/test_gate_watch.py
# 제출 게이트를 실측으로 배운다. 핵심은 **증거의 비대칭**이다 — "FAIL 인데도 제출됐다"는
# 증명이지만 "거절 사유에 이름이 있었다"는 증명이 아니다(WQB 는 403 본문에 FAIL 을 전부 싣는다).
# 2026-08-07 정정: 원래는 최근 관측이 이기게 해뒀는데, 거절이 성공보다 5~20배 잦아서
# soft 집합이 영구히 비고 LOW_FITNESS 가 하드로 굳었다. 그 알파들은 실제로는 22건 전원
# 제출에 성공한 부류다(fitness 0.26~0.86).
import time

import pytest

from server import criteria, gate_watch, run_config

NOW = 1786000000.0


@pytest.fixture
def wired(monkeypatch):
    state = {'rows': [], 'profile': {}}
    monkeypatch.setattr(gate_watch._db, 'rejection_and_success_checks',
                        lambda uid, since: state['rows'])
    monkeypatch.setattr(run_config, 'get_gate_profile', lambda: state['profile'])
    monkeypatch.setattr(run_config, 'set_gate_profile',
                        lambda p: state.update(profile=dict(p)) or p)
    monkeypatch.setattr(gate_watch.run_config, 'get_gate_profile', lambda: state['profile'])
    monkeypatch.setattr(gate_watch.run_config, 'set_gate_profile',
                        lambda p: state.update(profile=dict(p)) or p)
    return state


def _rejected(ts, *names):
    return (ts, 0, 'rejected:' + '; '.join(f'{n}(0.5 vs 1.0)' for n in names) + ' (http_403)', [])


def _submitted(ts, *names):
    return (ts, 1, 'submitted', [{'name': n} for n in names])


def test_rejection_names_are_hard_without_counter_evidence(wired):
    """반증이 없으면 거절 사유는 하드로 본다 — 모르는 규칙은 막는 쪽이 안전하다."""
    wired['rows'] = [_rejected(NOW, 'LOW_FITNESS', 'IS_LADDER_SHARPE')]
    obs = gate_watch.observe(2)
    assert 'LOW_FITNESS' in obs['hard'] and 'IS_LADDER_SHARPE' in obs['hard']


def test_fail_on_a_submitted_alpha_is_soft(wired):
    """FAIL 인데도 제출됐다면 그 체크는 막지 않는다 — 7월의 LOW_FITNESS 가 그랬다."""
    wired['rows'] = [_submitted(NOW, 'LOW_FITNESS')]
    assert gate_watch.observe(2)['soft'] == ['LOW_FITNESS']


def test_soft_proof_beats_rejection_co_occurrence(wired):
    """더 최근 거절에 이름이 끼어 있어도, 그걸 달고 제출된 적이 있으면 소프트다.

    403 본문은 그 알파의 FAIL 을 전부 싣는다 — 이름이 올랐다는 사실만으로는 원인이
    아니다. 반면 "FAIL 인데도 OS 에 올랐다"는 안 막는다는 증명이다.
    """
    wired['rows'] = [_submitted(NOW - 86400, 'LOW_FITNESS'),
                     _rejected(NOW, 'LOW_FITNESS', 'IS_LADDER_SHARPE')]
    obs = gate_watch.observe(2)
    assert obs['soft'] == ['LOW_FITNESS']
    assert obs['hard'] == ['IS_LADDER_SHARPE']      # 반증 없는 쪽만 하드로 남는다


def test_soft_set_survives_a_flood_of_rejections(wired):
    """거절이 성공보다 훨씬 잦아도 소프트 집합이 비면 안 된다 (2026-08-07 회귀)."""
    wired['rows'] = ([_submitted(NOW - 10 * 86400, 'LOW_FITNESS')]
                     + [_rejected(NOW - i, 'LOW_FITNESS') for i in range(20)])
    obs = gate_watch.observe(2)
    assert obs['soft'] == ['LOW_FITNESS'] and obs['hard'] == []


def test_sync_reports_the_change(wired):
    wired['profile'] = {'hard': ['IS_LADDER_SHARPE'], 'soft': ['LOW_FITNESS']}
    wired['rows'] = [_rejected(NOW, 'LOW_FITNESS', 'IS_LADDER_SHARPE')]
    msgs = []
    change = gate_watch.sync(2, log_fn=msgs.append)
    assert change and change['added'] == ['LOW_FITNESS']
    assert any('게이트 변화' in m for m in msgs)


def test_first_observation_is_not_a_change(wired):
    wired['rows'] = [_rejected(NOW, 'LOW_FITNESS')]
    assert gate_watch.sync(2) is None


def test_criteria_prefers_measured_over_hardcoded(wired):
    """실측이 하드코딩을 이긴다 — HT_ 접두어는 원래 비차단이지만 실측이 차단이라면 차단이다."""
    wired['profile'] = {'hard': ['HT_TURNOVER'], 'soft': ['PROD_CORRELATION']}
    assert criteria.is_blocking('HT_TURNOVER') is True
    assert criteria.is_blocking('PROD_CORRELATION') is False
    assert criteria.is_blocking('LOW_2Y_SHARPE') is True     # 미관측 → 하드코딩 폴백
