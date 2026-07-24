"""변이 귀속(v6) 종단 테스트 — '어떤 조정 → 어떤 지표 개선' 학습의 데이터 기반.

부모 alphas.id → (origin/directive/genes_changed) → 자식 → directive_stats 집계까지,
그리고 밴딧 신규 차원(family/combine)·bandit_reward 조밀화를 못박는다.

Run: python3 -m pytest tests/test_attribution.py -v
"""
import json
import random

import pytest

from server import bandit, db, genome_models, reward


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'attr.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('attr', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def _alpha(idx, code, *, pass_count=6, fail_items=None, directive=None,
           parent_alpha_id=None, origin=None, genes_changed=None, error_text=''):
    fails = list(fail_items or [])
    return {
        'idx': idx, 'code': code, 'desc': f'test #{idx}',
        'pass_count': pass_count, 'pass_items': [],
        'fail_count': len(fails), 'fail_items': fails,
        'error_count': 0, 'pending_count': 0,
        'submitted': False, 'submit_status': '', 'error_text': error_text,
        'metrics': {'sharpe': '1.0', 'fitness': '0.5', 'turnover': '0.3',
                    'returns': '0.05'},
        'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
        'settings': {'universe': 'TOP3000', 'neutralization': 'INDUSTRY',
                     'decay': '4', 'truncation': '0.08'},
        'delay': '1', 'self_corr': None, 'generation': 0, 'genome': None,
        'origin': origin, 'directive': directive,
        'parent_alpha_id': parent_alpha_id, 'genes_changed': genes_changed,
    }


# ── insert_alpha — id 반환 + 귀속 컬럼 영속화 ────────────────────────────────

def test_insert_alpha_returns_row_id(isolated_db):
    uid = isolated_db
    rid = db.start_round(uid, 1)
    id1 = db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(close)'))
    id2 = db.insert_alpha(uid, rid, 1, _alpha(2, 'rank(open)'))
    assert isinstance(id1, int) and isinstance(id2, int)
    assert id2 > id1


def test_attribution_columns_roundtrip(isolated_db):
    uid = isolated_db
    rid = db.start_round(uid, 1)
    pid = db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(close)'))
    db.insert_alpha(uid, rid, 1, _alpha(
        2, 'rank(ts_mean(close,20))', origin='mutate', directive='smooth',
        parent_alpha_id=pid, genes_changed=['decay', 'lookback_a']))
    rows = db.list_recent_alphas(uid, limit=1)
    child = rows[0]
    assert child['origin'] == 'mutate'
    assert child['directive'] == 'smooth'
    assert child['parent_alpha_id'] == pid
    assert json.loads(child['genes_changed']) == ['decay', 'lookback_a']


def test_attribution_columns_null_when_absent(isolated_db):
    """랜덤 탐색 알파(부모 없음)는 귀속 컬럼이 NULL — directive_stats 필터 계약."""
    uid = isolated_db
    rid = db.start_round(uid, 1)
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(close)'))
    row = db.list_recent_alphas(uid, limit=1)[0]
    assert row['directive'] is None
    assert row['parent_alpha_id'] is None


# ── directive_stats — (fail category × directive) 성공률 행렬 ────────────────

def test_directive_stats_aggregates_edges(isolated_db):
    uid = isolated_db
    rid = db.start_round(uid, 1)
    pid = db.insert_alpha(uid, rid, 1, _alpha(
        1, 'rank(close)', pass_count=6, fail_items=['Turnover']))
    # 성공: 표적 해소 + pass_count 개선.
    db.insert_alpha(uid, rid, 1, _alpha(
        2, 'A2', pass_count=7, fail_items=[], directive='smooth',
        parent_alpha_id=pid))
    # 실패: 표적 그대로.
    db.insert_alpha(uid, rid, 1, _alpha(
        3, 'A3', pass_count=6, fail_items=['Turnover'], directive='smooth',
        parent_alpha_id=pid))
    # 실패: 표적은 고쳤지만 전반 후퇴 (pass 6→4).
    db.insert_alpha(uid, rid, 1, _alpha(
        4, 'A4', pass_count=4, fail_items=['Sharpe'], directive='sharpen',
        parent_alpha_id=pid))
    stats = db.directive_stats(uid)
    assert stats[('turnover_high', 'smooth')]['n'] == 2
    assert stats[('turnover_high', 'smooth')]['wins'] == 1
    assert stats[('turnover_high', 'smooth')]['win_rate'] == 0.5
    assert stats[('turnover_high', 'sharpen')] == {'n': 1, 'wins': 0, 'win_rate': 0.0}


def test_directive_stats_ignores_unattributed_rows(isolated_db):
    uid = isolated_db
    rid = db.start_round(uid, 1)
    db.insert_alpha(uid, rid, 1, _alpha(1, 'rank(close)'))          # no directive
    pid = db.insert_alpha(uid, rid, 1, _alpha(2, 'X', fail_items=['Turnover']))
    db.insert_alpha(uid, rid, 1, _alpha(                             # directive, no parent
        3, 'Y', directive='smooth', parent_alpha_id=None))
    assert db.directive_stats(uid) == {}
    db.insert_alpha(uid, rid, 1, _alpha(
        4, 'Z', pass_count=7, directive='smooth', parent_alpha_id=pid))
    assert ('turnover_high', 'smooth') in db.directive_stats(uid)


# ── genome_models — provenance 산출 ──────────────────────────────────────────

def _make_parent():
    pop = genome_models.generate_population(
        account_type='research_consultant', round_num=1, forced_delay='1', n=8)
    return dict(pop[0]['genome'])


def test_focus_children_carry_directive_and_parent_id():
    parent = _make_parent()
    pop = genome_models.generate_population(
        account_type='research_consultant', round_num=2, forced_delay='1', n=8,
        parent_genome=parent, fail_items=['Turnover(0.9>0.7)'],
        parent_alpha_id=777)
    mutates = [p for p in pop if p['origin'] == 'mutate']
    assert mutates
    for p in mutates:
        assert p['parent_alpha_id'] == 777
        assert p['directive'] in (None,) + genome_models._mutation_learn.DIRECTIVES
        assert p['genes_changed'], '변이 자식은 바뀐 유전자가 1개 이상이어야 한다'


def test_exploration_children_inherit_seed_alpha_ids():
    seeds = [_make_parent(), _make_parent()]
    seeds[1]['family'] = 'pv'
    pop = genome_models.generate_population(
        account_type='research_consultant', round_num=3, forced_delay='1', n=8,
        seed_genomes=seeds, seed_alpha_ids=[11, 22])
    ga_children = [p for p in pop if p['origin'] in ('mutate', 'crossover')]
    assert ga_children
    assert all(p['parent_alpha_id'] in (11, 22) for p in ga_children)
    randoms = [p for p in pop if p['origin'] == 'random']
    assert all(p['parent_alpha_id'] is None and p['directive'] is None
               for p in randoms)


def test_learned_directive_selection_is_deterministic():
    parent = _make_parent()
    stats = {('turnover_high', 'signal'): {'n': 50, 'wins': 45}}
    kw = dict(account_type='research_consultant', round_num=4, forced_delay='1',
              n=8, parent_genome=parent, fail_items=['Turnover(0.9>0.7)'],
              directive_stats=stats)
    a = genome_models.generate_population(**kw)
    b = genome_models.generate_population(**kw)
    assert [p['directive'] for p in a] == [p['directive'] for p in b]
    assert [p['code'] for p in a] == [p['code'] for p in b]


def test_learned_stats_override_rule_choice():
    """관측이 규칙(smooth)을 반박하면 자식 세대의 축 구성이 signal 쪽으로 기운다."""
    parent = _make_parent()
    stats = {
        ('turnover_high', 'smooth'): {'n': 80, 'wins': 2},
        ('turnover_high', 'signal'): {'n': 80, 'wins': 70},
    }
    picked = []
    for rnd in range(10, 26):
        pop = genome_models.generate_population(
            account_type='research_consultant', round_num=rnd, forced_delay='1',
            n=8, parent_genome=parent, fail_items=['Turnover(0.9>0.7)'],
            directive_stats=stats)
        picked += [p['directive'] for p in pop if p['directive']]
    assert picked
    assert picked.count('signal') > picked.count('smooth')


# ── 밴딧 신규 차원 (family / combine) ────────────────────────────────────────

def test_select_slots_include_structural_dimensions():
    slots = bandit.select_slots({}, n_slots=8, explore_slots=3,
                                rng=random.Random(0))
    for s in slots:
        assert s['family'] in bandit.DIMENSIONS['family']
        assert s['combine'] in bandit.DIMENSIONS['combine']


def test_arm_keys_include_family_and_combine_when_present():
    assign = {'universe': 'TOP3000', 'neutralization': 'INDUSTRY',
              'decay_bucket': 'mid', 'decay': 4,
              'family': 'option', 'combine': 'ratio'}
    keys = bandit.arm_keys_for_assignment(assign)
    assert 'family:option' in keys
    assert 'combine:ratio' in keys
    assert len(keys) == 5


def test_arm_keys_legacy_three_key_contract_preserved():
    assign = {'universe': 'TOP3000', 'neutralization': 'INDUSTRY',
              'decay_bucket': 'mid', 'decay': 4}
    assert len(bandit.arm_keys_for_assignment(assign)) == 3


def test_apply_arm_injects_family_with_consistent_fields():
    parent = _make_parent()
    g = genome_models._coerce_genome(parent)
    model = genome_models.ResearchConsultantGenomeModel(round_num=1, forced_delay='1')
    rng = random.Random(0)
    out = model._apply_arm(g, {'family': 'option', 'combine': 'ratio'}, rng)
    assert out.family == 'option'
    assert all(f in genome_models.SHARED_DATASETS['option'] for f in out.fields)
    assert out.combine == 'ratio'


# ── bandit_reward — 희소성 완화 ──────────────────────────────────────────────

def test_bandit_reward_dense_signal_for_near_miss():
    """all-pass 미달(게이트 보상 0)이어도 근접 알파는 0 보다 큰 보상을 받는다."""
    m = {'sharpe': '1.2', 'fitness': '0.9', 'turnover': '0.3', 'returns': '0.06'}
    gated = reward.compute_reward(m, pass_count=6, fail_count=1)
    dense = reward.bandit_reward(m, pass_count=6, fail_count=1)
    assert gated == 0.0
    assert dense > 0.0


def test_bandit_reward_submittable_still_dominates():
    m_good = {'sharpe': '2.0', 'fitness': '1.5', 'turnover': '0.2', 'returns': '0.1'}
    m_near = {'sharpe': '2.0', 'fitness': '1.5', 'turnover': '0.2', 'returns': '0.1'}
    submittable = reward.bandit_reward(m_good, pass_count=8, fail_count=0)
    near_miss = reward.bandit_reward(m_near, pass_count=7, fail_count=1)
    assert submittable > near_miss
