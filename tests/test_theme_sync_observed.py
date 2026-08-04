# tests/test_theme_sync_observed.py
# 지원문서는 월초에 늦는다(2026-08-04 실측: 8월 표 미게시). 문서만 원천으로 두면
# 새 테마가 걸려도 못 알아채고 옛 조건으로 한 달을 간다 — API 실측이 두 번째 원천.
# ⚠ run_config 는 라이브 data/run_config.json 을 만진다. 반드시 monkeypatch 로 막을 것.
import pytest

from server import run_config, theme_sync


class _Spec:
    def __init__(self, region, delay):
        self.region, self.delay = region, delay


@pytest.fixture
def wired(monkeypatch):
    state = {'name': '', 'themes': [], 'spec': _Spec('GLB', '1'), 'playbook': 0}
    monkeypatch.setattr(run_config, 'get_theme_active_name', lambda: state['name'])
    monkeypatch.setattr(run_config, 'set_theme_active_name',
                        lambda v: state.update(name=v) or v)
    monkeypatch.setattr(run_config, 'get_constraint', lambda: state['spec'])
    monkeypatch.setattr('server.theme_playbook.active_themes',
                        lambda uid: {'all': state['themes'], 'matched': {}})
    monkeypatch.setattr('server.theme_playbook.start_background',
                        lambda uid: state.update(playbook=state['playbook'] + 1))
    return state


def test_parses_scope_from_theme_name(wired):
    wired['themes'] = ['GLB High Turnover Theme', 'GLB/D1 Power Pool Aug`26']
    assert theme_sync.observed_theme(2) == ('GLB/D1 Power Pool Aug`26', 'GLB', '1')


def test_new_theme_logs_and_kicks_playbook(wired):
    wired['themes'] = ['GLB/D1 Power Pool Aug`26']
    msgs = []
    assert theme_sync.note_observed_theme(2, log_fn=msgs.append)
    assert wired['name'] == 'GLB/D1 Power Pool Aug`26' and wired['playbook'] == 1
    assert any('Power Pool Aug' in m for m in msgs)
    # 같은 테마 재관측 — 매 라운드 플레이북을 다시 돌리면 안 된다
    assert theme_sync.note_observed_theme(2, log_fn=msgs.append) is None
    assert wired['playbook'] == 1


def test_scope_mismatch_warns(wired):
    """리전이 어긋나면 이번 테마엔 한 건도 못 넣는다 — 조용히 넘어가면 안 된다."""
    wired['themes'] = ['USA/D1 Power Pool Sep`26']
    msgs = []
    theme_sync.note_observed_theme(2, log_fn=msgs.append)
    assert any('USA/D1' in m and 'GLB' in m for m in msgs)


def test_no_observation_is_noop(wired):
    assert theme_sync.note_observed_theme(2) is None
    assert wired['playbook'] == 0
