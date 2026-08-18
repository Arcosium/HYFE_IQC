# tests/test_discovery_and_decorrelate.py
# 2026-08-18 사장 지시로 고친 세 가지. 근거는 08-17~18 라이브 실측(알파 300여 건)이다.
import random

from server import genome_models as gm
from server import submit_push as sp
from server import wqb_api


def _row(name, ds, cov, used):
    return {'name': name, 'category': ds, 'coverage': cov, 'alphas': used,
            'type': 'MATRIX', 'region': 'GLB', 'universe': 'TOPDIV3000', 'delay': '1'}


def test_cross_dataset_partner_is_found_when_same_dataset_has_none():
    """한 재료로는 조건 다섯 개를 못 넘는다 — 데이터셋을 가로지르는 짝을 허용해야 한다.

    08-17~18 에 통과한 알파 3건은 전부 EMEA 되는 데이터셋 × 래더 되는 데이터셋이었다.
    """
    rows = [_row('lonely_field', 'ds_alone', 99, 500),
            _row('partner_a', 'ds_other', 99, 400),
            _row('partner_b', 'ds_other2', 98, 600)]
    got = sp._cross_dataset_partners('lonely_field', rows)
    assert got is not None
    assert 'lonely_field' not in got
    assert len(set(got)) == 2


def test_cross_partner_skips_dead_and_crowded_fields():
    """사용 0~1 은 신호가 없고(24종 전부 샤프 0.5 이하), 3000 이상은 붐빈다."""
    rows = [_row('lonely_field', 'ds_alone', 99, 500),
            _row('dead', 'ds_x', 99, 1),          # 아무도 안 쓴다 = 신호 없음
            _row('crowded', 'ds_y', 99, 9000),    # 포화
            _row('thin', 'ds_z', 50, 400)]        # 커버리지 미달
    assert sp._cross_dataset_partners('lonely_field', rows) is None


def test_discovery_rejects_zero_usage_fields():
    """발굴이 저사용 오름차순이라, 구간 필터가 없으면 죽은 필드가 웨이브를 다 먹는다."""
    assert sp.SIGNAL_USAGE_MIN >= 100
    assert sp.SIGNAL_USAGE_MAX <= 5000


def test_decorrelate_raises_decay():
    """상관은 decay 로만 실제로 움직였다 — 부모보다 **올리는 쪽**이어야 한다.

    실측: 합성 decay 12→24 로 첫 제출, srisk 조립 24→36 으로 둘째 제출.
    """
    base = gm.BaseGenomeModel(round_num=1)
    parent = base._genome(1, random.Random(0))
    parent = gm.Genome(**{**parent.__dict__, 'decay': 4})
    raised = 0
    for seed in range(40):
        m = gm.BaseGenomeModel(round_num=2)
        m.fail_items = ['PROD_CORRELATION(0.84 vs 0.7)']
        m.parent_metrics = {}
        child = m._mutate(parent, random.Random(seed), directed=True)
        if m._last_directive == 'decorrelate':
            raised += 1
            assert child.decay >= 16, f'decay 가 안 올랐다: {child.decay}'
    assert raised, 'decorrelate 지시가 한 번도 안 뽑혔다 — 매핑을 확인하라'


def test_prod_correlation_maps_to_the_same_axis():
    """PROD 상관도 self 상관과 같은 축으로 가야 어제 통한 경로가 재현된다."""
    from server import mutation_learn
    assert mutation_learn.categorize(['PROD_CORRELATION(0.84 vs 0.7)']) == ['correlation']
    assert mutation_learn.RULE_DIRECTIVE['correlation'] == 'decorrelate'


def test_submit_deadline_outlives_the_wqb_battery():
    """480초로는 08-17 하루에만 10회 넘게 끊겼다 — 판정을 못 받으면 되쏴야 한다."""
    assert wqb_api._SUBMIT_ALPHA_DEADLINE_S >= 900
