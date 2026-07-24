"""2026-07-14 '샤프 1.0 정체' 개편에서 드러난 버그들의 회귀 테스트.

전부 **조용한** 버그였다 — 예외도 로그도 없이 잘못된 값을 계속 쓰고 있었다.
"""
import math

import pytest

from server import genome_models as gm
from server import mutation_learn as ml
from server import reward, selection


# ── 1. 퍼센트 문자열 지표를 0.0 으로 읽던 버그 ────────────────────────────────

def test_percent_metrics_are_parsed_not_zeroed():
    """브라우저 시대 알파는 지표를 '9.45%' 로 저장한다. 순진한 float() 는 ValueError →
    조용히 0.0. 명예의 전당이 그 옛 행을 시드로 되살리는 지금은 곧바로 점수 왜곡이다."""
    assert reward._f('9.45%') == pytest.approx(0.0945)
    assert reward._f('43.54%') == pytest.approx(0.4354)
    assert reward._f('232.36‱') == pytest.approx(0.023236)
    assert reward._f('0.0972') == pytest.approx(0.0972)      # REST API 포맷도 그대로
    assert reward._f('') == 0.0 and reward._f(None) == 0.0

    # 6월 Sharpe 3.77 알파(퍼센트 포맷)가 0 점 취급되지 않는다.
    june = {'sharpe': '3.77', 'fitness': '7.04',
            'turnover': '9.45%', 'returns': '43.54%'}
    assert reward.selection_score(june, pass_count=6, fail_count=1) > 0.7

    # selection 의 목적벡터도 같은 파서를 써야 한다 (turnover 가 '미측정' 으로 둔갑 금지).
    obj = selection.obj_vector({'metrics': june})
    assert obj[0] == pytest.approx(3.77)
    assert obj[2] > 0.0, 'turnover 9.45% 가 파싱됐다면 route 축이 0 보다 크다'


# ── 2. turnover 를 바닥(12.5%) 밑으로 낮추면 계속 가산점을 주던 버그 ──────────

def test_turnover_gradient_points_at_high_turnover_band():
    """**2026-07-21 방향 전환** — 회전율은 이제 낮을수록 좋은 게 아니다.

    이 테스트는 원래 정반대를 못박고 있었다(`turnover 40% < turnover 3%`). 근거는
    WQB Fitness 의 분모가 max(turnover, 0.125) 라는 것이었고 그 자체는 지금도 맞다.
    하지만 제출 규칙이 바뀌면서 **회전율 20% 미만은 고회전 분류를 못 얻고**, 분류를
    못 얻으면 D0 Sharpe 2.69 / Fitness 1.5 를 정면으로 뚫어야 한다(사실상 불가).
    그래서 '저회전 최적화' 는 제출 불가능한 알파를 만드는 지시가 됐다.
    """
    base = {'sharpe': 1.0, 'fitness': 0.6, 'returns': 0.05}
    kw = dict(pass_count=4, fail_count=4)
    s_3pct = reward.selection_score({**base, 'turnover': 0.03}, **kw)
    s_40pct = reward.selection_score({**base, 'turnover': 0.40}, **kw)
    assert s_40pct > s_3pct, '고회전 대역(20~70%)이 저회전보다 높아야 한다'

    # 회전율 **항 자체**는 대역 안(20~65%)에서 평평하다 — 불필요한 회전을 유도하지 않는다.
    assert math.isclose(reward._turnover_term(0.40), reward._turnover_term(0.55), rel_tol=1e-9)

    # 다만 **총점**은 대역 하단이 유리하다(2026-07-22 margin 항 신설). 같은 수익률이면
    # 회전율이 낮을수록 후비용 마진이 크기 때문이다: 강등선 = 수익률 > 0.1512 × 회전율.
    # 어제 실측 결론 '대역 하단(20~30%)이 두 번 싸다' 가 보상에 반영된 것.
    s_55pct = reward.selection_score({**base, 'turnover': 0.55}, **kw)
    assert s_40pct > s_55pct, '같은 수익률이면 낮은 회전율이 후비용 마진에서 유리해야 한다'

    # 70% 초과는 HIGH_TURNOVER FAIL(제출 차단) → 대역 안보다 낮아야 한다.
    s_75pct = reward.selection_score({**base, 'turnover': 0.75}, **kw)
    assert s_75pct < s_40pct


def test_submit_term_keeps_pulling_past_the_local_gate():
    """로컬 게이트(Sharpe 1.25)를 넘긴 뒤에도 실제 제출컷까지 계속 당겨야 한다."""
    kw = dict(pass_count=8, fail_count=0)
    at_gate = reward.selection_score(
        {'sharpe': 1.30, 'fitness': 0.7, 'turnover': 0.2, 'returns': 0.05}, **kw)
    at_cut = reward.selection_score(
        {'sharpe': 1.60, 'fitness': 1.05, 'turnover': 0.2, 'returns': 0.09}, **kw)
    assert at_cut > at_gate + 0.05, '게이트 통과 후 선택압이 포화됐다'


# ── 3. 정향변이 학습이 전 축 0승으로 죽어 있던 버그 ──────────────────────────

def test_directive_learning_credits_partial_progress():
    """'FAIL 이 사라져야 승리' 는 게이트에서 먼 알파에겐 도달 불가능한 기준이다.
    라이브에서 LOW_SHARPE/LOW_FITNESS 가 ~100% FAIL 이라 **모든 축이 영원히 0승**이었다.
    표적 지표가 유의미하게 나아지면 부분 전진도 승리로 센다."""
    parent = {'fail_items': ['LOW_SHARPE'], 'pass_count': 4,
              'metrics': {'sharpe': '0.30'}}
    # 자식은 여전히 LOW_SHARPE 지만 Sharpe 가 0.30 → 0.90 으로 확실히 전진했다.
    child = {'fail_items': ['LOW_SHARPE'], 'pass_count': 4, 'directive': 'concentration',
             'metrics': {'sharpe': '0.90'}}
    obs = ml.outcome_observations(parent, child)
    assert obs == [('signal', 'concentration', True)], obs

    # 후퇴한 자식은 승리가 아니다.
    worse = {**child, 'metrics': {'sharpe': '0.10'}}
    assert ml.outcome_observations(parent, worse) == [('signal', 'concentration', False)]

    # 노이즈 수준(<2%)의 변화는 승리로 세지 않는다.
    noise = {**child, 'metrics': {'sharpe': '0.301'}}
    assert ml.outcome_observations(parent, noise) == [('signal', 'concentration', False)]


def test_fitness_and_stability_are_their_own_categories():
    """Fitness 와 2Y Sharpe 는 'signal'(무작위 변이)에 삼켜지면 안 된다 — 각각
    returns 를 올리는 축(boost)과 시간 안정성 축(robustify)이 따로 받는다."""
    assert ml.categorize(['LOW_FITNESS']) == ['fitness']
    assert ml.categorize(['LOW_2Y_SHARPE']) == ['stability']
    assert ml.RULE_DIRECTIVE['fitness'] == 'boost'
    assert ml.RULE_DIRECTIVE['stability'] == 'robustify'
    for d in ('boost', 'robustify'):
        assert d in ml.DIRECTIVES


# ── 4. NSGA-II crowding 이 축퇴 축에 phantom 경계를 만들던 버그 ───────────────

def test_degenerate_objective_creates_no_phantom_boundary():
    """목적값이 전부 같은 축에는 경계가 없다. 그런데도 inf 를 찍으면 '임의의 두 개체'가
    무한 우선순위를 얻어, 지표가 평평한 구간에서 옛 화석이 영구 엘리트가 된다."""
    flat = [[1.0, 0.5], [1.0, 0.5], [1.0, 0.5]]
    assert selection.crowding_distance(flat) == [0.0, 0.0, 0.0]

    # 그 결과 nsga2 는 타이브레이크(=최신 우선)를 실제로 쓸 수 있다.
    recs = [{'id': i, 'metrics': {'sharpe': '1.0', 'fitness': '0.5', 'turnover': '0.2'},
             'self_corr': '0.0'} for i in (1, 23, 24)]
    order = selection.order_seed_records(recs, mode='nsga2')
    assert [recs[i]['id'] for i in order] == [24, 23, 1]


def test_missing_2y_sharpe_does_not_double_count_sharpe():
    """2Y Sharpe 결측을 sharpe 로 메우면 sharpe 가 두 축에 들어가 파레토 지배가 왜곡된다
    (turnover/self-corr 가 나쁜 고-Sharpe 알파가 그 덕에 승격했다). 아무도 측정값이
    없으면 축 자체를 넣지 않는다."""
    recs = [{'id': 1, 'metrics': {'sharpe': '3.0', 'fitness': '0.5', 'turnover': '0.6'},
             'self_corr': '0.6'},
            {'id': 2, 'metrics': {'sharpe': '2.5', 'fitness': '1.5', 'turnover': '0.15'},
             'self_corr': '0.05'}]
    objs, has_2y = selection._obj_matrix(recs)
    assert has_2y is False
    assert all(len(o) == 4 for o in objs), '측정값이 없는데 2y 축이 생겼다'

    # 하나라도 측정되면 축이 생기고, 결측 행은 중립값(중앙값)을 받는다.
    recs[1]['metrics']['sharpe_2y'] = '1.4'
    objs, has_2y = selection._obj_matrix(recs)
    assert has_2y is True
    assert objs[0][4] == pytest.approx(1.4), '결측 행은 측정값의 중앙값(중립)을 받아야 한다'


# ── 5. 문법 확장 — 6월 챔피언 알파를 표현할 수 있는가 ────────────────────────

def test_grammar_can_express_the_june_champion():
    """Sharpe 3.77 알파 = 레짐 조건부 + CLV + hump + 서브인더스트리 중립화.
    확장 전 문법으로는 **표현 자체가 불가능**했다."""
    g = gm._coerce_genome({
        'family': 'pv', 'fields': ('syn_clv', 'syn_range', 'close'),
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'spread',
        'sign': -1, 'neutralization': 'SUBINDUSTRY', 'decay': 15,
        'decay_style': 'linear', 'regime': 'range_expand', 'hump': 0.055,
    })
    code = gm.render(g)
    assert '?' in code and ':0)' in code, '레짐 조건부가 렌더되지 않았다'
    assert 'hump(' in code and 'hump=0.055' in code
    assert 'group_neutralize' in code and 'subindustry' in code
    assert '(close-low)-(high-close)' in code, 'CLV 합성팩터가 전개되지 않았다'


def test_june_champion_roundtrips_back_into_a_genome():
    """백필(A1)이 성립하려면 원본 코드에서 그 유전자들이 되돌아와야 한다."""
    june = ('_clv = ((close - low) - (high - close)) / (high - low + 0.000001); '
            '_daily_range = high - low; '
            '_range_vol = ts_std_dev(_daily_range, 20) / (ts_mean(_daily_range, 20) + 0.000001); '
            '_sig = _range_vol > 1.3 ? -1.0 * rank(_clv) : 0.0; '
            'hump(group_neutralize(ts_decay_linear(_sig, 15), subindustry), hump=0.055)')
    g = gm.genome_from_alpha(june, {'universe': 'TOP200',
                                    'neutralization': 'SUBINDUSTRY', 'decay': '18'})
    assert 'syn_clv' in g['fields'], 'CLV 가 원시필드로 분해돼 구조를 잃었다'
    assert g['regime'] == 'range_expand'
    assert g['hump'] == 0.055
    assert g['sign'] == -1, "'-1.0*' 를 못 읽어 부호가 뒤집혔다"
    assert g['group_op'] == 'neutralize'


def test_default_genes_keep_render_byte_identical():
    """v3 유전자의 기본값은 render() 산출물을 바꾸면 안 된다 — 기존 2만 알파의
    code_hash(캐시 키)와 dedup 이 살아 있어야 한다."""
    base = {'family': 'pv', 'fields': ('close', 'open', 'high'),
            'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum',
            'sign': 1, 'lookback_a': 20, 'lookback_b': 60,
            'universe': 'TOP3000', 'neutralization': 'INDUSTRY', 'decay': 0}
    g = gm._coerce_genome(base)
    assert g.transform_c == 'ts_zscore' and g.lookback_c == 0
    assert g.regime == 'OFF' and g.hump == 0.0
    code = gm.render(g)
    assert '?' not in code and 'hump(' not in code
    assert code == 'rank(group_neutralize(add(rank(close),ts_zscore(open,60)),industry))'
