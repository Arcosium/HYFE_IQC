"""유전자 공간 축소 — 부트캠프 5주차 "유전자가 너무 많다" 지적 반영.

절단은 실측(2026-07-21)에서 0.05~0.15 결과가 동일했다. 이미 스윕 축에서는 뺐는데
무작위 슬롯에서는 계속 굴리고 있었다. 감쇠도 회전율이 목표에 닿으면 더 볼 것이 없다.
"""
import random

from server import genome_models as gm


def _model():
    return gm.BaseGenomeModel(round_num=1)


def test_random_genomes_do_not_explore_truncation():
    m = _model()
    seen = {m._genome(slot, random.Random(slot)).truncation for slot in range(1, 30)}
    assert seen == {m.truncations[0]}, '무작위 슬롯이 절단을 여전히 굴린다'


def test_random_genomes_still_explore_core_genes():
    """핵심 유전자(필드·변환)는 그대로 탐색해야 한다 — 줄일 대상이 아니다."""
    m = _model()
    gs = [m._genome(slot, random.Random(slot)) for slot in range(1, 30)]
    assert len({g.transform_a for g in gs}) > 1
    assert len({g.fields for g in gs}) > 1


def test_sweep_axis_moves_to_core_gene_when_decay_is_settled():
    """회전율이 이미 목표면 감쇠를 맹목 순회하지 않고 핵심 유전자를 돌린다."""
    m = _model()
    parent = m._genome(1, random.Random(0))
    m.parent_metrics = {}                      # 회전율 미측정 → 감쇠 조정 근거 없음
    kids = [m._sweep(parent, slot=1, attempt=a) for a in (1, 2, 3)]
    assert all(k.decay == parent.decay for k in kids), '감쇠를 맹목으로 흔들고 있다'
    assert {k.transform_a for k in kids} != {parent.transform_a}


def test_sweep_still_fixes_decay_when_turnover_is_off_target():
    """회전율이 어긋나 있으면 감쇠는 여전히 **필요한 값으로** 움직인다."""
    m = _model()
    parent = m._genome(1, random.Random(0))
    parent = gm.Genome(**{**parent.__dict__, 'decay': 0})
    m.parent_metrics = {'turnover': 1.0}       # 과회전 — 감쇠를 올려야 한다
    kid = m._sweep(parent, slot=1, attempt=1)
    assert kid.decay > 0


def test_neutralization_sweep_axis_is_untouched():
    """중립화 스윕은 유지 — 강의가 prod 상관을 움직이는 손잡이로 지목한 축이다."""
    m = _model()
    parent = m._genome(1, random.Random(0))
    m.parent_metrics = {}
    kid = m._sweep(parent, slot=0, attempt=1)
    assert kid.neutralization != parent.neutralization
