"""2026-07-22 회전율 폭주 교정 — 보상·제출게이트·캐시 3종 회귀 테스트.

배경(라이브 실측): 탐색 조건을 USA·D1·TOP1000 으로 건 뒤 GA 상위 알파 6개가 전부
회전율 122~144% 였고 전원 HIGH_TURNOVER FAIL 이었다. 원인은 컷(70%) **위쪽이
평평**했다는 것 — `_turnover_term` 도 `criteria.ht_progress` 도 초과분에서 똑같이
0 이라 71% 와 143% 가 동점인데, 그 사이 sharpe/returns/htclass 항은 회전율이
높을수록 잘 나와서 사실상 오르막이었다.

같은 날 강등에 성공한 5건은 회전율 40~62% · 수익률 7.5~11.4% 였다. 즉 GA 가
가야 할 곳은 대역 하단인데, 보상이 정확히 반대 방향을 가리키고 있었다.
"""
import pytest

from server import criteria as _criteria
from server import reward


# ── ① 회전율 초과 감쇄: 컷 위에서 '내려오는 기울기' 가 있어야 한다 ──────────────

def test_overshoot_factor_is_one_below_cut():
    for t in (0.0, 0.25, 0.5, _criteria.TURNOVER_MAX):
        assert reward._overshoot_factor(t) == 1.0


def test_overshoot_factor_is_strictly_decreasing_above_cut():
    """71% → 143% 로 갈수록 계수가 **엄격히** 줄어야 GA 가 내려올 방향을 안다."""
    hi = _criteria.TURNOVER_MAX
    vals = [reward._overshoot_factor(t) for t in (hi + 0.01, 0.8, 1.0, 1.2, 1.44)]
    assert all(a > b for a, b in zip(vals, vals[1:])), vals
    assert all(0.0 < v < 1.0 for v in vals), vals


def test_runaway_turnover_scores_below_healthy_alpha():
    """같은 Sharpe 라면 회전율 143% 알파가 회전율 50% 알파를 절대 못 이긴다.

    라이브 r620#8 (sh 1.11 · 회전율 143.4% · 수익률 6.11%) vs
    2026-07-21 강등 성공 QP9rLo5Q 형 (sh 1.12 · 회전율 40% · 수익률 7.54%).
    """
    runaway = {'sharpe': '1.11', 'fitness': '0.23', 'turnover': '1.434', 'returns': '0.0611'}
    healthy = {'sharpe': '1.12', 'fitness': '0.30', 'turnover': '0.40', 'returns': '0.0754'}
    # pass_count 는 보상 게이트(MIN_EVALUATED_PASSES)를 열기 위한 것 — 둘에 같은 값을 준다.
    assert (reward.compute_reward(healthy, pass_count=4)
            > reward.compute_reward(runaway, pass_count=4))
    # 선택(부모 자격)에서도 같은 순서여야 한다 — 한쪽만 고치면 폭주 알파가 부모로 남는다.
    assert reward.selection_score(healthy) > reward.selection_score(runaway)


# ── ② 후비용 마진 항: 강등의 유일한 판별자를 직접 겨냥 ──────────────────────────

def test_margin_term_full_at_waiver_line():
    """수익률 = 0.1512 × 회전율 이면 후비용 Sharpe 부호가 넘어간다 → 만점."""
    assert reward._margin_term(0.40, 0.1512 * 0.40) == pytest.approx(1.0)
    assert reward._margin_term(0.40, 0.20) == pytest.approx(1.0)   # 넘겨도 만점(클램프)


def test_margin_term_punishes_raising_turnover_alone():
    """수익률을 그대로 두고 회전율만 올리면 점수가 **내려가야** 한다."""
    r = 0.06
    assert reward._margin_term(0.40, r) > reward._margin_term(0.80, r) > reward._margin_term(1.44, r)


def test_margin_term_matches_yesterdays_waiver_cases():
    """2026-07-21 실측 5건 — 강등된 3건은 1.0, 탈락한 2건은 그 아래여야 한다."""
    waived = [(0.60, 0.1140), (0.54, 0.1109), (0.40, 0.0754)]     # np8k1q38 · gJ9qkKWv · QP9rLo5Q
    failed = [(0.62, 0.0860), (0.615, -0.0242)]                    # E5eo9OLG · RR1Zn0Ng
    for turn, ret in waived:
        assert reward._margin_term(turn, ret) == pytest.approx(1.0), (turn, ret)
    for turn, ret in failed:
        assert reward._margin_term(turn, ret) < 1.0, (turn, ret)


def test_weights_still_sum_to_one():
    assert sum(reward.DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)
    assert 'margin' in reward.DEFAULT_WEIGHTS


# ── ③ 제출 게이트: 차단 FAIL 이면 WQB 로 보내지 않는다 ────────────────────────

class _Gate:
    """worker.Worker._submit_gate 만 떼어 쓰기 위한 최소 스텁."""
    user_id = 2

    def __init__(self):
        from server.worker import Worker
        self._gate = Worker._submit_gate.__get__(self, Worker)

    def __call__(self, metrics, self_corr=None, fail_items=None):
        return self._gate(metrics, self_corr, fail_items=fail_items)


def test_submit_gate_sends_blocking_fail_for_ground_truth():
    """거절은 쿼터를 쓰지 않으므로 WQB 응답을 직접 학습한다."""
    gate = _Gate()
    ok, reason = gate({'sharpe': '1.11'},
                      fail_items=[{'name': 'LOW_SHARPE'}, {'name': 'HIGH_TURNOVER'}])
    assert ok is True and reason == ''


def test_submit_gate_accepts_plain_string_fail_items():
    gate = _Gate()
    ok, reason = gate({}, fail_items=['HIGH_TURNOVER'])
    assert ok is True and reason == ''


def test_submit_gate_ignores_non_blocking_checks():
    """HT_*/CLUSTER_TEST 등 분류 체크는 FAIL 이어도 제출을 막지 않는다."""
    gate = _Gate()
    ok, reason = gate({}, fail_items=[{'name': 'CLUSTER_TEST'}, {'name': 'HT_TURNOVER'}])
    assert 'blocking_fail' not in reason


def test_backend_passes_fail_items_to_gate():
    """배관 회귀 — 백엔드가 fail 목록을 게이트에 넘기지 않으면 ①이 무력해진다."""
    from server.wqb_backend import ApiBackend
    seen = {}

    def gate(metrics, self_corr, fail_items=None):
        seen['fail_items'] = fail_items
        return False, 'stub'

    ok, reason = ApiBackend._check_submit_gate(gate, {}, None, [{'name': 'LOW_SHARPE'}])
    assert ok is False and reason == 'stub'
    assert seen['fail_items'] == [{'name': 'LOW_SHARPE'}]


def test_backend_tolerates_old_gate_signature():
    """게이트 교체 중에도 제출이 통째로 막히면 안 된다(구 시그니처 폴백)."""
    from server.wqb_backend import ApiBackend

    def old_gate(metrics, self_corr):
        return True, ''

    ok, _ = ApiBackend._check_submit_gate(old_gate, {}, None, [{'name': 'LOW_SHARPE'}])
    assert ok is True


# ── ④ 선택 경로 2 (NSGA-II 목적벡터) — 보상만 고치면 여기로 폭주 부모가 산다 ──────

def test_obj_vector_damps_runaway_sharpe():
    """회전율 143% · Sharpe 1.11 은 **제출 불가**다 — sharpe 축을 그대로 넣으면
    아무에게도 지배당하지 않아 파레토 1층에 영구히 남는다(r623 실측 원인)."""
    from server import selection
    runaway = {'metrics': {'sharpe': '1.11', 'fitness': '0.23',
                           'turnover': '1.434', 'returns': '0.0611'}}
    healthy = {'metrics': {'sharpe': '0.94', 'fitness': '0.30',
                           'turnover': '0.389', 'returns': '0.0439'}}
    v_run = selection.obj_vector(runaway)
    v_ok = selection.obj_vector(healthy)
    assert v_run[0] < v_ok[0], f'폭주 알파의 sharpe 축이 아직 높다: {v_run} vs {v_ok}'
    assert v_run[1] < v_ok[1], 'fitness 축도 감쇄돼야 한다'


def test_obj_vector_does_not_reward_negative_sharpe_for_churning():
    """감쇄를 음수에 곱하면 Sharpe −0.46 이 −0.07 로 '좋아진다'. 그 뒤집힘 금지."""
    from server import selection
    bad_low = selection.obj_vector({'metrics': {'sharpe': '-0.46', 'turnover': '0.30'}})
    bad_high = selection.obj_vector({'metrics': {'sharpe': '-0.46', 'turnover': '1.09'}})
    assert bad_high[0] <= bad_low[0], '회전율을 올려 음수 Sharpe 가 개선되면 안 된다'


def test_obj_vector_untouched_inside_the_band():
    """대역 안(≤70%)에서는 목적벡터가 예전과 동일해야 한다(불필요한 거동 변화 금지)."""
    from server import selection
    d = {'metrics': {'sharpe': '1.20', 'fitness': '0.80', 'turnover': '0.45'}}
    v = selection.obj_vector(d)
    assert v[0] == pytest.approx(1.20) and v[1] == pytest.approx(0.80)
