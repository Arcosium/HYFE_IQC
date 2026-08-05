# tests/test_gate_watch.py
# 제출 게이트를 실측으로 배운다. 2026-08-03 에 LOW_FITNESS 가 소프트→하드로 바뀐 걸
# 이틀 늦게 알아챈 사고의 대응 — 거절 응답에 답이 있었는데 읽지 않았다.
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


def test_rejection_names_are_hard(wired):
    wired['rows'] = [_rejected(NOW, 'LOW_FITNESS', 'IS_LADDER_SHARPE')]
    obs = gate_watch.observe(2)
    assert 'LOW_FITNESS' in obs['hard'] and 'IS_LADDER_SHARPE' in obs['hard']


def test_fail_on_a_submitted_alpha_is_soft(wired):
    """FAIL 인데도 제출됐다면 그 체크는 막지 않는다 — 7월의 LOW_FITNESS 가 그랬다."""
    wired['rows'] = [_submitted(NOW, 'LOW_FITNESS')]
    assert gate_watch.observe(2)['soft'] == ['LOW_FITNESS']


def test_recent_observation_wins(wired):
    """소프트였다가 하드가 되는 게 집행 변경의 모습이다 — 최근 관측을 따른다."""
    wired['rows'] = [_submitted(NOW - 86400, 'LOW_FITNESS'),
                     _rejected(NOW, 'LOW_FITNESS')]
    obs = gate_watch.observe(2)
    assert obs['hard'] == ['LOW_FITNESS'] and obs['soft'] == []


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
