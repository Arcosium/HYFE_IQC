"""2026-07-21 WQB 컨설턴트 제출 기준 개편의 회귀 테스트.

이 파일의 상수들은 **라이브 실측**에서 왔다 (같은 계정·같은 날·USA D0 TOP3000
SUBINDUSTRY, 시뮬 5건 + 기존 알파 조회). 문서에 안 적힌 규칙(강등 조건)이 있어서
실측을 그대로 고정해 둔다 — 나중에 규칙이 또 바뀌면 여기가 먼저 깨져야 한다.
"""
import pytest

from server import criteria as c
from server import reward


# ── 라이브 실측 5건 — (지표, 표준컷이 강등됐는가) ────────────────────────────
# 전부 HIGH_TURNOVER 분류를 받았는데도 강등은 3건뿐이었다. 가르는 것은 후비용 Sharpe.
LIVE = [
    ('np8k1q38', dict(sharpe='1.63', turnover='0.6003', ht_turnover='0.6003',
                      ht_returns_ratio='0.8746', ht_pnl_horizon='5',
                      ht_after_cost_sharpe='0.35'), True),
    ('gJ9qkKWv', dict(sharpe='1.53', turnover='0.5401', ht_turnover='0.5401',
                      ht_returns_ratio='0.853', ht_pnl_horizon='6',
                      ht_after_cost_sharpe='0.41'), True),
    ('QP9rLo5Q', dict(sharpe='1.12', turnover='0.3995', ht_turnover='0.3995',
                      ht_returns_ratio='0.8435', ht_pnl_horizon='6',
                      ht_after_cost_sharpe='0.21'), True),
    ('E5eo9OLG', dict(sharpe='1.34', turnover='0.6197', ht_turnover='0.6197',
                      ht_returns_ratio='0.8616', ht_pnl_horizon='5',
                      ht_after_cost_sharpe='-0.15'), False),
    ('RR1Zn0Ng', dict(sharpe='-0.44', turnover='0.6148', ht_turnover='0.6148',
                      ht_returns_ratio='1.0207', ht_after_cost_sharpe='-2.1'), False),
]


@pytest.mark.parametrize('name,metrics,waived', LIVE)
def test_waiver_prediction_matches_live(name, metrics, waived):
    """강등 예측이 실측 5/5 와 일치해야 한다.

    ⚠ Sharpe 는 판별자가 아니다 — 1.34 는 떨어지고 1.12 는 통과했다. 후비용 Sharpe
      부호만이 5건 전부를 가른다. (Sharpe 하한 가설로 되돌리면 이 테스트가 깨진다.)
    """
    st = c.ht_status(metrics)
    assert st['eligible'] is True, '5건 모두 HIGH_TURNOVER 분류는 받았다'
    assert st['waiver_likely'] is waived, (name, st)


def test_low_turnover_alpha_is_not_eligible():
    """회전율 20% 미만이면 분류 자체가 안 된다 (라이브 kqZkEgmO, 16.67%)."""
    m = dict(sharpe='0.36', turnover='0.1667', ht_turnover='0.1667',
             ht_returns_ratio='0.6962', ht_pnl_horizon='11')
    st = c.ht_status(m)
    assert st['eligible'] is False
    assert 'turnover' in st['gaps']


def test_route_gradient_points_up_from_low_turnover():
    """저회전 알파의 route 는 회전율을 올릴수록 커져야 한다(GA 가 갈 방향).

    수익률도 회전율에 비례해 올려 잡는다(마진 일정) — 회전율만 올리고 수익률이 그대로면
    비용에 먹혀 후비용이 음수가 되므로, 그건 GA 가 가야 할 방향이 아니다.
    """
    def route(to, sharpe='1.2'):
        return c.submittability(dict(sharpe=sharpe, turnover=str(to),
                                     returns=str(0.20 * to),   # 마진 ≈ 4bp 고정
                                     ht_turnover=str(to), ht_returns_ratio='0.85'))
    assert route(0.05) < route(0.12) < route(0.22)


def test_route_penalises_turnover_raised_without_returns():
    """회전율만 올리고 수익률이 그대로면 route 가 **떨어져야** 한다.

    고회전 문서의 "Artificial turnover" 경고를 정량적으로 못박는 테스트다:
    수익률 8% 를 유지한 채 회전율을 25%→60% 로 올리면 마진이 6.3bp→2.6bp 로 무너져
    후비용이 음수가 된다 = 강등 불가.
    """
    def route(to):
        return c.submittability(dict(sharpe='1.2', turnover=str(to), returns='0.08',
                                     ht_turnover=str(to), ht_returns_ratio='0.85'))
    assert route(0.60) < route(0.25)
    assert c.ht_status(dict(sharpe='1.2', turnover='0.60', returns='0.08',
                            ht_turnover='0.60', ht_returns_ratio='0.85'))['waiver_likely'] is False


def test_lower_band_is_cheaper_than_upper_band():
    """**대역 하단(20~30%)이 상단보다 싸다** — 후비용 Sharpe 요구가 회전율에 비례하므로.

    이게 전략의 핵심 결론이다: 회전율을 60% 까지 밀 이유가 없다. 같은 Sharpe 1.0 이면
    22% 에선 강등되고 55% 에선 안 된다.
    """
    # 수익률 4% 고정 — 회전율이 오를수록 마진이 얇아져 비용에 먹힌다.
    def waived(to):
        return c.ht_status(dict(sharpe='1.0', turnover=str(to), returns='0.04',
                                ht_turnover=str(to),
                                ht_returns_ratio='0.85'))['waiver_likely']
    assert waived(0.22) is True
    assert waived(0.25) is True
    assert waived(0.55) is False


def test_cutoffs_are_delay_aware():
    """D0 는 D1 의 1.7배(Sharpe)·1.5배(Fitness). 구 코드는 두 값을 섞어 하드코딩했다."""
    assert c.cutoffs('0') == {'sharpe': 2.69, 'fitness': 1.5, 'ladder_fail': 2.69}
    assert c.cutoffs('1')['sharpe'] == 1.58
    assert c.cutoffs(None)['sharpe'] == 1.58, '알 수 없으면 느슨한 쪽(D1)'


@pytest.mark.parametrize('universe,sharpe,expected_cutoff', [
    ('TOP3000', 2.11, 0.91),   # 라이브 d5ReNWXK
    ('TOP3000', 1.53, 0.66),   # 라이브 gJ9qkKWv
    ('TOP1000', 0.21, 0.11),   # 라이브 78ngpva1
    ('TOP500', 0.17, 0.08),    # 라이브 vRvaQ2Wa
])
def test_sub_universe_cutoff_matches_live(universe, sharpe, expected_cutoff):
    """0.75·sqrt(sub/alpha)·sharpe — 라이브 cutoff 를 소수 둘째자리까지 재현한다."""
    got = c.sub_universe_cutoff(sharpe, universe)
    assert got == pytest.approx(expected_cutoff, abs=0.01)


def test_top200_has_no_sub_universe_test():
    assert c.sub_universe_cutoff(1.0, 'TOP200') is None


def test_theme_multiplier_combination_rule():
    """복수 테마 = sum − count + 1 ("Multiplier Rules" 문서의 예: 3x + 5x → 7x)."""
    assert c.combine_theme_multipliers([3.0, 5.0]) == 7.0
    assert c.combine_theme_multipliers([2.0]) == 2.0
    assert c.combine_theme_multipliers([]) == 1.0


def test_cluster_test_never_blocks_submission():
    """CLUSTER_TEST 는 분류 전용 — FAIL 이어도 제출을 막지 않는다(문서 명시)."""
    assert c.is_blocking('CLUSTER_TEST') is False
    assert c.is_blocking('HT_AFTER_COST_SHARPE') is False
    assert c.is_blocking('MATCHES_THEMES') is False
    assert c.is_blocking('LOW_SHARPE') is True
    assert c.is_blocking('SOME_NEW_CHECK_WE_HAVE_NEVER_SEEN') is True, '모르면 차단으로 본다'


def test_reward_rewards_waived_alpha_over_unwaived():
    """강등된(=제출 가능) 알파가 안 된 알파보다 높은 보상을 받아야 한다."""
    waived = dict(LIVE[2][1]); waived['pyramid_multiplier'] = '1.6'
    unwaived = dict(LIVE[3][1]); unwaived['pyramid_multiplier'] = '1.6'
    r_ok = reward.compute_reward(waived, pass_count=4, fail_count=0)
    r_no = reward.compute_reward(unwaived, pass_count=4, fail_count=3)
    assert r_ok > 0.0 and r_no == 0.0
    # 게이트를 뺀 연속 점수에서도 순서가 유지돼야 한다(선택압).
    assert (reward.selection_score(waived, pass_count=4, fail_count=0)
            > reward.selection_score(unwaived, pass_count=4, fail_count=3))


def test_payout_multiplier_is_pyramid_times_theme():
    m = {'pyramid_multiplier': '1.6', 'theme_multiplier': '2.0'}
    assert c.payout_multiplier(m) == pytest.approx(3.2)
    assert c.payout_multiplier({}) == 1.0


# ── 단일 데이터셋(ATOM) 알파의 완화 규정 ─────────────────────────────────────

def test_single_dataset_detection_from_classification():
    """분류는 WQB 가 준 classification id 로만 판정한다(우리 family 로 세지 않는다)."""
    assert c.is_single_dataset({'classification_ids': 'DATA_USAGE:SINGLE_DATA_SET'}) is True
    assert c.is_single_dataset(
        {'classification_ids': 'DATA_USAGE:SINGLE_DATA_SET,HIGH_TURNOVER:HIGH_TURNOVER'}) is True
    assert c.is_single_dataset({'classification_ids': 'CLUSTER:CLUSTER'}) is False
    assert c.is_single_dataset({}) is False


@pytest.mark.parametrize('delay,turnover,expect_fail,expect_pass', [
    # 회전율 30% 이상 → 할인 없음. 문서의 D1 2.38 / D0 3.96 이 그대로 PASS 문턱.
    ('1', 0.54, 1.59, 2.38),
    ('0', 0.54, 2.69, 3.96),
    # 회전율 30% 미만 → PASS 문턱만 0.85 배 (FAIL 문턱은 할인되지 않는다).
    ('1', 0.25, 1.59, 2.38 * 0.85),
    ('0', 0.25, 2.69, 3.96 * 0.85),
])
def test_two_year_thresholds(delay, turnover, expect_fail, expect_pass):
    fail, passing = c.two_year_thresholds(delay, turnover, single_dataset=True)
    assert fail == pytest.approx(expect_fail)
    assert passing == pytest.approx(expect_pass)


def test_low_turnover_discount_favours_the_ht_band_floor():
    """회전율 20~30% 대역이 2Y 문턱에서도 싸다 — 후비용 조건과 같은 방향.

    이게 '대역 하단이 두 번 싸다' 는 근거다: 후비용 요구치가 낮고, 2Y 문턱도 15% 깎인다.
    """
    _, pass_25 = c.two_year_thresholds('0', 0.25, True)
    _, pass_55 = c.two_year_thresholds('0', 0.55, True)
    assert pass_25 < pass_55


def test_stability_target_prefers_live_fail_cutoff_but_aims_at_pass():
    """실측 컷(=FAIL 문턱)이 있어도 목표는 PASS 문턱이어야 한다.

    라이브 gJ9qkKWv 는 sharpe_2y_cutoff=2.69 를 받았는데, 그건 '이 밑이면 즉사' 선이지
    통과선이 아니다. 2.69 를 목표로 삼으면 통과 못 할 알파를 통과했다고 착각한다.
    """
    m = {'_delay': '0', 'turnover': '0.54', 'sharpe_2y': '0.68',
         'sharpe_2y_cutoff': '2.69', 'classification_ids': 'DATA_USAGE:SINGLE_DATA_SET'}
    assert c.stability_target(m) == pytest.approx(3.96)


def test_reward_stability_uses_delay_and_turnover_aware_target():
    """같은 2Y Sharpe 라도 D0 는 D1 보다 낮게 평가돼야 한다(문턱이 1.66배 높으므로)."""
    base = dict(sharpe='1.2', fitness='0.6', turnover='0.45', returns='0.08',
                sharpe_2y='2.0', classification_ids='DATA_USAGE:SINGLE_DATA_SET')
    d1 = reward.selection_score({**base, '_delay': '1'}, pass_count=4, fail_count=0)
    d0 = reward.selection_score({**base, '_delay': '0'}, pass_count=4, fail_count=0)
    assert d1 > d0


# ── 거래비용 모델 (마진 기반, 2026-07-21 역산으로 확정) ──────────────────────

@pytest.mark.parametrize('name,sharpe,turnover,returns,expected_ac', [
    ('np8k1q38', 1.63, 0.6003, 0.1140, 0.35),
    ('gJ9qkKWv', 1.53, 0.5401, 0.1109, 0.41),
    ('QP9rLo5Q', 1.12, 0.3995, 0.0754, 0.21),
    ('E5eo9OLG', 1.34, 0.6197, 0.0860, -0.15),
    ('RR1Zn0Ng', -0.44, 0.6148, -0.0242, -2.10),   # 마진 음수 — abs() 쓰면 부호가 뒤집힌다
])
def test_after_cost_sharpe_closed_form_matches_live(name, sharpe, turnover, returns, expected_ac):
    """after_cost = sharpe·(margin−3bp)/margin 이 실측 후비용을 ±0.05 로 재현한다.

    이 식이 있으면 회전율 20% 미만이라 HT 체크가 안 도는 알파도 비용을 판단할 수 있다
    (실측은 그 구간에서 아예 안 나온다).
    """
    got = c.after_cost_sharpe({'sharpe': str(sharpe), 'turnover': str(turnover),
                               'returns': str(returns)})
    assert got == pytest.approx(expected_ac, abs=0.05)


def test_margin_identity_matches_live_margin():
    """마진 = 수익률/(504·회전율) — IS 요약의 margin 과 일치해야 한다(라이브 3.80bp)."""
    assert c.margin({'returns': '0.1140', 'turnover': '0.6003'}) == pytest.approx(0.00038, abs=1e-5)
    # 실측 margin 이 있으면 그쪽이 권위.
    assert c.margin({'margin': '0.00042', 'returns': '0.1', 'turnover': '0.5'}) == 0.00042


@pytest.mark.parametrize('turnover,expected', [(0.20, 0.0302), (0.25, 0.0378), (0.60, 0.0907)])
def test_required_returns_is_the_ga_facing_target(turnover, expected):
    """비용을 넘기 위한 최소 수익률 = 0.1512 × 회전율. GA 가 직접 겨냥할 수 있는 형태."""
    assert c.required_returns(turnover) == pytest.approx(expected, abs=0.0005)


def test_after_cost_unknown_is_neutral_not_zero():
    """수익률이 없으면 '모름'(중립 0.5)이지 '나쁨'(0)이 아니다 — 레거시 행 보호."""
    assert c.after_cost_sharpe({'sharpe': '1.2', 'turnover': '0.4'}) is None
    r_unknown = c.ht_progress({'sharpe': '1.2', 'turnover': '0.4',
                               'ht_turnover': '0.4', 'ht_returns_ratio': '0.85'})
    assert 0.0 < r_unknown < 1.0


# ── 제출 여부 기록의 진실성 (2026-07-21) ────────────────────────────────────

def test_effectively_submitted_rejects_non_submissions():
    """제출하지 않은 알파를 '제출됨' 으로 세면 안 된다.

    ⚠ 라이브 실측: alphas.submitted=1 이 6657행인데 WQB 실제 제출은 23건이었다.
      원인은 브라우저 시대 추정 — '상태값이 비어있지 않고 self-corr 거절이 아니면 제출'.
      REST API 는 성공을 정확히 'submitted' 로 알려주므로 그 추정이 필요 없다.
      하루 4건뿐인 제출을 세는 화면이 거짓말을 하면 판단이 통째로 어긋난다.
    """
    from server import db
    not_submitted = [
        'submit_skipped:daily_budget(4/4)',
        'submit_skipped:below_value(0.11<0.15)',
        'submit_error:connection reset',
        'submit_pending_timeout:1.0',
        'rejected:LOW_SHARPE; LOW_FITNESS (http_403)',
        'submit_http_403: {"is":{"checks":[]}}',
        'skip_star: all-pass 아님',
        'fail:no_response_modal_less',
    ]
    for st in not_submitted:
        assert db.effectively_submitted(True, st) is False, st
        assert db.effectively_submitted(False, st) is False, st
    # 진짜 성공만 True.
    assert db.effectively_submitted(True, 'submitted') is True
    # 시도 자체가 없었으면(상태값 없음) 플래그를 따른다 — 기존 계약 유지.
    assert db.effectively_submitted(True, '') is True
    assert db.effectively_submitted(False, '') is False
