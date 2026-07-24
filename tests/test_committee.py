"""전략 위원회(committee) — 순수 로직(검증·소비 헬퍼) 단위 테스트.

LLM 호출은 테스트하지 않는다 (로컬 모델 의존). 대상은:
  - sanitize_policy: LLM 출력 불신 원칙 — 무효 값 드롭, clamp
  - slots_from_policy: bandit.select_slots 와 동일한 형식 보장 (decay 포함)
  - order_seeds_with_pairs: 교차쌍 인접 배치 + fail-open
  - active_policy: 라운드 신선도 판정

실행: python3 -m pytest tests/test_committee.py
"""
import json
import random

import pytest

from server import bandit, committee


@pytest.fixture
def isolated_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(committee, 'POLICY_PATH', str(tmp_path / 'policy.json'))
    return committee


def _valid_raw():
    return {
        'slot_settings': [
            {'universe': 'TOP1000', 'neutralization': 'STATISTICAL',
             'family': 'model', 'combine': 'resid', 'decay_bucket': 'mid'},
        ],
        'seed_pairs': [[11, 22]],
        'explore_slots': 2,
        'valid_rounds': 12,
        'notes': 'test',
    }


def test_sanitize_drops_invalid_values():
    raw = _valid_raw()
    raw['slot_settings'].append({'universe': 'NOPE', 'neutralization': 'ALSO_NOPE'})
    raw['seed_pairs'] += [[1, 1], 'garbage', [0, 5]]
    p = committee.sanitize_policy(raw, user_id=2, round_num=100)
    assert p is not None
    assert len(p['slot_settings']) == 1          # 무효 슬롯은 통째로 폐기
    assert p['seed_pairs'] == [[11, 22]]         # a==b, 비정수, 0 은 드롭


def test_sanitize_clamps_explore_and_valid_rounds():
    raw = _valid_raw()
    raw['explore_slots'] = 99
    raw['valid_rounds'] = 999
    p = committee.sanitize_policy(raw, user_id=2, round_num=100)
    assert p['explore_slots'] == 4
    assert p['valid_rounds'] == 48


def test_sanitize_empty_returns_none():
    assert committee.sanitize_policy({}, user_id=2, round_num=1) is None
    assert committee.sanitize_policy(
        {'slot_settings': [{'universe': 'X'}]}, user_id=2, round_num=1) is None


def test_sanitize_case_normalization():
    p = committee.sanitize_policy(
        {'slot_settings': [{'universe': 'top1000', 'neutralization': 'statistical',
                            'family': 'MODEL', 'combine': 'RESID'}]},
        user_id=2, round_num=1)
    s = p['slot_settings'][0]
    assert s['universe'] == 'TOP1000'
    assert s['neutralization'] == 'STATISTICAL'
    assert s['family'] == 'model'
    assert s['combine'] == 'resid'


def test_slots_from_policy_shape_matches_bandit():
    p = committee.sanitize_policy(_valid_raw(), user_id=2, round_num=100)
    slots = committee.slots_from_policy(p, n_slots=8, stats={},
                                        rng=random.Random(7))
    assert len(slots) == 8
    for s in slots:
        # bandit.select_slots 계약: 전 차원 + decay(int) 가 채워져 있어야 한다.
        for dim in ('universe', 'neutralization', 'decay_bucket', 'family', 'combine'):
            assert s[dim] in bandit.DIMENSIONS[dim]
        assert s['decay'] == bandit.DECAY_BUCKET_VALUE[s['decay_bucket']]
    # 정책 슬롯이 맨 앞에 온다.
    assert slots[0]['neutralization'] == 'STATISTICAL'
    assert slots[0]['combine'] == 'resid'


def test_order_seeds_with_pairs_adjacency():
    pool = [{'id': i, 'genome': {}} for i in (1, 2, 3, 4, 5, 6, 7)]
    fallback = pool[:5]
    policy = {'seed_pairs': [[6, 3]]}
    out = committee.order_seeds_with_pairs(pool, policy, fallback)
    assert [d['id'] for d in out[:2]] == [6, 3]   # 쌍이 인접 앞자리
    assert len(out) <= 6
    assert len({d['id'] for d in out}) == len(out)  # 중복 없음


def test_order_seeds_with_pairs_fail_open():
    fallback = [{'id': 1}, {'id': 2}]
    # 풀에 없는 id 쌍 → fallback 그대로
    out = committee.order_seeds_with_pairs(
        [{'id': 9}], {'seed_pairs': [[100, 200]]}, fallback)
    assert out == fallback


def test_active_policy_freshness(isolated_policy):
    p = committee.sanitize_policy(_valid_raw(), user_id=2, round_num=100)
    committee._write_policy_file(p)
    assert isolated_policy.active_policy(2, 100) is not None
    assert isolated_policy.active_policy(2, 100 + p['valid_rounds']) is not None
    assert isolated_policy.active_policy(2, 100 + p['valid_rounds'] + 1) is None
    assert isolated_policy.active_policy(3, 101) is None     # 다른 사용자
    assert isolated_policy.active_policy(2, 99) is None      # 과거 라운드(시계 역행)


def test_active_policy_missing_file(isolated_policy):
    assert isolated_policy.active_policy(2, 1) is None


def test_bandit_neutralization_covers_genome_full_set():
    """bandit 차원이 유전체 전집합과 다시 어긋나면(하드코딩 회귀) 즉시 잡는다 —
    STATISTICAL 이 '통계는 쌓이는데 선택은 불가능한 팔' 이던 2026-07-23 사고의 재발 방지."""
    from server import genome_models as gm
    assert set(bandit.DIMENSIONS['neutralization']) == set(gm.NEUTRALIZATIONS)
    assert set(bandit.DIMENSIONS['combine']) == set(gm.BaseGenomeModel.combines)
    assert 'resid' in bandit.DIMENSIONS['combine']


def test_parse_json_object_prefers_outer_object():
    """`{"slots": [...]}` 에서 내부 배열이 아니라 바깥 객체를 잡아야 한다 —
    parse_json_array 를 쓰다 껍데기 키가 증발한 2026-07-23 라이브 버그의 회귀 테스트."""
    raw = '```json\n{"slots": [{"universe": "TOP1000"}], "why": "x"}\n```'
    o = committee._parse_json_object(raw)
    assert o.get('why') == 'x'
    assert o.get('slots') == [{'universe': 'TOP1000'}]


def test_parse_json_object_with_preamble():
    raw = '설명입니다.\n{"slot_settings": [], "seed_pairs": [[1, 2]]}\n끝.'
    o = committee._parse_json_object(raw)
    assert o.get('seed_pairs') == [[1, 2]]
    assert committee._parse_json_object('') == {}
    assert committee._parse_json_object('json 아님') == {}


def test_submit_gate_fieldset_cooldown(monkeypatch):
    """같은 필드셋이 24h 내 3회+ 거절됐으면 제출 보류 — 2026-07-24 19연속 거절 회귀."""
    from server import worker, db as _db
    w = worker.Worker.__new__(worker.Worker)   # 스레드 시작 없이 게이트만
    w.user_id = 2
    cool = frozenset({'f1', 'f2', 'f3'})
    monkeypatch.setattr(_db, 'rejected_fieldsets', lambda uid, **k: [cool])
    monkeypatch.setattr(_db, 'submitted_today', lambda uid: 0)
    ok, reason = w._submit_gate({'sharpe': 2.0}, None, fail_items=[],
                                genome={'fields': ['f2', 'f1', 'f3']})
    assert not ok and reason == 'fieldset_cooldown(24h)'
    # 다른 필드셋은 통과 경로로 계속 (예산/가치 게이트로 넘어간다)
    ok2, reason2 = w._submit_gate({'sharpe': 2.0}, None, fail_items=[],
                                  genome={'fields': ['x', 'y', 'z']})
    assert reason2 != 'fieldset_cooldown(24h)'
