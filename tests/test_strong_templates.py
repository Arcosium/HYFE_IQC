# tests/test_strong_templates.py
# 무작위 슬롯 절반에 강신호 골격(STRONG_TEMPLATES) — 균등 무작위 변환×결합
# 조합은 대부분 잡음이라 신선 필드 프로브가 광맥을 못 뚫던 문제의 보완.
# (2026-07-31 — 별도 시딩 스트림 대신 기존 라운드 14개의 질을 올린다)
import dataclasses
import random

from server import genome_models as gm


def test_template_keys_are_genome_fields():
    names = {f.name for f in dataclasses.fields(gm.Genome)}
    for tpl in gm.STRONG_TEMPLATES:
        assert set(tpl) <= names, tpl


def test_half_of_random_slots_wear_a_template():
    model = gm.ResearchConsultantGenomeModel(round_num=3, forced_delay='1')
    hits = 0
    for slot in range(1, 41):
        g = model._genome(slot, random.Random(slot))
        combo = {k: getattr(g, k) for k in
                 ('transform_a', 'transform_b', 'combine', 'sign',
                  'lookback_a', 'lookback_b', 'decay')}
        if any(all(combo.get(k) == v for k, v in tpl.items() if k in combo)
               for tpl in gm.STRONG_TEMPLATES):
            hits += 1
    # 확률 0.5 × 40슬롯 — 결정론 시드라 항상 같은 값이지만, 우연 일치 여유를 두고
    # '절반 근처'만 확인한다. 0 이면 오버레이가 죽은 것이다.
    assert hits >= 8, hits


def test_pure_random_half_still_explores():
    """골격을 안 입은 절반은 여전히 순수 무작위여야 한다 — 신조합 탐침 유지."""
    model = gm.ResearchConsultantGenomeModel(round_num=3, forced_delay='1')
    combos = set()
    for slot in range(1, 61):
        g = model._genome(slot, random.Random(slot * 7919))
        combos.add((g.transform_a, g.combine, g.lookback_a))
    # 골격 8종만 나온다면 다양성이 죽은 것 — 그보다 훨씬 많아야 한다.
    assert len(combos) > len(gm.STRONG_TEMPLATES)
