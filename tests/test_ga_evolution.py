"""GA 가 실제로 '진화' 하는지 — 2026-07-11 정체 사건의 회귀 테스트.

라이브 증상: uid2 가 250라운드를 돌았는데 최고 세대가 g1, 엘리트 풀은 round 66 에서
184라운드째 동결, 제출 1,263건 전량 거절. 원인 네 가지가 맞물린 닫힌 고리였다.

  1. `genome_from_alpha()` 가 generation 을 0 으로 하드코딩 → 시드 왕복마다 세대 리셋.
  2. 엘리트 게이트가 `pass_count >= 5` (이산·포화) → 자식(최대 4)이 영원히 진입 실패.
  3. 시드 유전체를 코드에서 정규식 역추출 → 자식이 부모를 복제조차 못 함.
  4. `compute_reward()` 가 fail>0 이면 0.0 → 모든 자식이 동점, 선택압 소멸.

아래 테스트들은 각각을 못박는다.
"""
import pytest

from server import db, genome_models, reward, worker


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'ga.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('evo', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid
    db._INITIALIZED = False


def _metrics(sharpe: float) -> dict:
    return {'sharpe': str(sharpe), 'fitness': str(round(sharpe * 0.6, 4)),
            'turnover': '0.2', 'returns': '0.05'}


def _insert_pop(uid, round_num, pop, *, sharpe_of):
    rid = db.start_round(uid, round_num)
    for p in pop:
        db.insert_alpha(uid, rid, round_num, {
            'idx': p['idx'], 'code': p['code'], 'desc': p['desc'],
            'pass_count': 4, 'pass_items': [], 'fail_count': 3, 'fail_items': [],
            'error_count': 0, 'pending_count': 0, 'submitted': False,
            'submit_status': '', 'error_text': '',
            'metrics': _metrics(sharpe_of(p)),
            'self_corr': None, 'settings': p['settings'], 'delay': '1',
            'is_status': {}, 'mode': '', 'cached': False, 'phase': 0,
            'generation': p['generation'], 'genome': p['genome'],
        })


def _run_rounds(uid, n_rounds, sharpe_of):
    """탐색 라운드를 n번 돌리고 각 라운드의 최고 세대를 반환."""
    seen_gens = []
    for rnd in range(1, n_rounds + 1):
        seeds = db.elite_seeds(uid, top_n=5)
        pop = genome_models.generate_population(
            account_type='research_consultant', round_num=rnd, forced_delay='1', n=8,
            seed_genomes=[s['genome'] for s in seeds] or None,
        )
        _insert_pop(uid, rnd, pop, sharpe_of=sharpe_of)
        seen_gens.append(max(p['generation'] for p in pop))
    return seen_gens


def test_generation_climbs_past_g1(isolated_db):
    """자식이 부모보다 적합하면 세대가 계속 올라간다. 예전 코드는 영원히 g1 이었다."""
    uid = isolated_db
    # 세대가 높을수록 조금씩 나은 알파 — 정상적인 진화 압력.
    gens = _run_rounds(uid, 6, sharpe_of=lambda p: 0.5 + 0.15 * p['generation'])
    assert gens[0] == 0, '첫 라운드는 시드가 없어 전부 무작위(g0)'
    assert max(gens) >= 4, f'세대가 정체했다: {gens}'
    # 그리고 그 세대가 DB 를 거쳐(=시드 왕복) 살아남았는지.
    assert db.elite_seeds(uid, top_n=1)[0]['genome']['generation'] >= 3


def test_pool_turns_over_when_fitness_is_flat(isolated_db):
    """적합도가 완전히 동률이어도 풀은 옛 행에 눌러앉지 않는다 (round-66 동결 사건).

    세대가 올라가는 것까지 요구하지는 않는다 — 더 적합한 자식이 없으면 세대가 오를
    이유가 없고, 그걸 강제하면 카운터를 위한 카운터가 된다. 요구하는 것은 풀의 갱신이다.
    """
    uid = isolated_db
    _run_rounds(uid, 5, sharpe_of=lambda p: 1.0)
    seeds = db.elite_seeds(uid, top_n=5)
    assert seeds
    assert all(s['round_num'] >= 4 for s in seeds), \
        f'옛 라운드가 풀을 점거했다: {[s["round_num"] for s in seeds]}'


def test_recency_breaks_ties_against_the_incumbent(isolated_db):
    """동점 타이브레이크는 최신 우선 — 이게 없으면 첫 라운드 알파가 영구 엘리트가 된다."""
    uid = isolated_db
    _run_rounds(uid, 3, sharpe_of=lambda p: 1.0)
    ids = [s['id'] for s in db.elite_seeds(uid, top_n=3)]
    assert ids == sorted(ids, reverse=True)


def test_seed_roundtrip_preserves_the_genome_exactly(isolated_db):
    """DB 왕복이 유전자를 잃지 않는다 (정규식 역추출은 잃는다)."""
    uid = isolated_db
    pop = genome_models.generate_population(
        account_type='research_consultant', round_num=1, forced_delay='1', n=8)
    _insert_pop(uid, 1, pop, sharpe_of=lambda p: 2.0 if p['idx'] == 1 else 0.1)

    seed = db.elite_seeds(uid, top_n=1)[0]
    original = pop[0]['genome']
    restored = seed['genome']
    assert tuple(restored['fields']) == tuple(original['fields'])
    for gene in ('transform_a', 'transform_b', 'combine', 'sign',
                 'lookback_a', 'lookback_b', 'decay', 'decay_style', 'generation'):
        assert restored[gene] == original[gene], gene
    # 복원한 유전체로 렌더하면 원본 코드 그대로여야 한다.
    assert genome_models.render(
        genome_models._coerce_genome(restored)) == pop[0]['code']


def test_regex_reconstruction_loses_genes_on_legacy_alphas():
    """왜 화석을 시드로 쓰면 안 되는지 — 3팩터 Gemini 알파가 2팩터로 찌그러진다."""
    fossil = ('cf=winsorize(ts_backfill(cashflow_op,120),std=4)/'
              '(winsorize(ts_backfill(cap,120),std=4)+0.000001); '
              'vr=winsorize(ts_backfill(volume,120),std=4)/'
              'ts_mean(winsorize(ts_backfill(volume,120),std=4),20); '
              'ts_decay_linear(rank(cf)*rank(vr),8)')
    g = genome_models.genome_from_alpha(fossil, settings={'universe': 'TOP3000'})
    rendered = genome_models.render(genome_models._coerce_genome(g))
    # volume 은 유전체엔 남지만 combine 이 2항이라 코드에 발현되지 못한다.
    assert 'volume' in g['fields']
    assert 'volume' not in rendered
    assert 'winsorize' not in rendered      # 필드 위생 유전자 소실


def test_genome_from_alpha_requires_caller_to_supply_generation():
    """세대는 코드에서 복원 불가 — 하드코딩 0 이 g1 상한의 직접 원인이었다."""
    code = 'rank((rank(close)-ts_rank(low,20)))'
    assert genome_models.genome_from_alpha(code)['generation'] == 0
    assert genome_models.genome_from_alpha(code, generation=7)['generation'] == 7


# ── 선택압: 자식들이 전부 0.0 이 되면 안 된다 ────────────────────────────────

_CHILD_STRONG = {'sharpe': '1.11', 'fitness': '0.59', 'turnover': '0.30', 'returns': '0.10'}
_CHILD_WEAK = {'sharpe': '0.02', 'fitness': '0.01', 'turnover': '0.30', 'returns': '0.00'}


def test_selection_score_discriminates_children_that_reward_cannot():
    """compute_reward 는 all-pass 게이트 때문에 둘 다 0.0 — 그래서 선택에 못 쓴다."""
    kw = dict(pass_count=4, fail_count=3, error_count=0)
    assert reward.compute_reward(_CHILD_STRONG, **kw) == 0.0
    assert reward.compute_reward(_CHILD_WEAK, **kw) == 0.0

    strong = reward.selection_score(_CHILD_STRONG, **kw)
    weak = reward.selection_score(_CHILD_WEAK, pass_count=3, fail_count=4, error_count=0)
    # 마진이 2배 → 1.4배로 좁아진 건 2026-07-21 가중치 재배분의 의도된 결과다:
    # 두 자식은 회전율이 같아(30%) route/turnover 항이 동일하고, 차이는 sharpe/fitness/
    # returns(합 0.34)에서만 난다. 개편 전엔 이 셋이 0.59 를 차지했다. 제출 가능성이
    # Sharpe 보다 중요해진 만큼 순수 Sharpe 격차의 지배력이 준 것이고, 원래 이 테스트가
    # 막으려던 것(자식 전원 동점 → 선택압 소멸)은 그대로 막힌다.
    assert strong > weak * 1.4, (strong, weak)


def test_selection_score_zeroes_alphas_without_metrics():
    """turnover 결측을 '완벽' 으로 읽어 죽은 알파가 상위 시드가 되면 안 된다."""
    assert reward.selection_score({'_delay': '1'}, pass_count=0, fail_count=0) == 0.0
    assert reward.selection_score({}, pass_count=7, fail_count=0) == 0.0


def test_selection_score_keeps_unsubmittable_parents_usable():
    """self-corr > 0.7 은 제출 불가지만 부모로는 쓸 만하다 — 0.0 으로 죽이지 않는다."""
    kw = dict(pass_count=7, fail_count=0, error_count=0)
    assert reward.compute_reward(_CHILD_STRONG, self_corr=0.9, **kw) == 0.0
    assert reward.selection_score(_CHILD_STRONG, self_corr=0.9, **kw) > 0.0
    # 그래도 깨끗한 부모보다는 낮아야 한다.
    assert (reward.selection_score(_CHILD_STRONG, self_corr=0.9, **kw)
            < reward.selection_score(_CHILD_STRONG, self_corr=0.1, **kw))


# ── focus 게이트 ────────────────────────────────────────────────────────────

def _result(metrics, pass_count, fail_count, **over):
    r = {'metrics': metrics, 'pass_count': pass_count, 'fail_count': fail_count,
         'error_count': 0, 'is_status': {}, 'cached': False, 'self_corr': None}
    r.update(over)
    return r


def test_focus_fires_on_a_strong_child_that_pass_count_would_have_rejected():
    """pass_count=4 짜리 자식은 예전 게이트(>=5)에 영원히 걸렸다. 이제 적합도로 판정한다."""
    r = _result(_CHILD_STRONG, pass_count=4, fail_count=3)
    assert worker.focus_score(r) >= worker.FOCUS_MIN_SCORE
    assert worker._classify_focus(r) == ('fail', '')


def test_focus_skips_weak_and_already_passing_and_cached():
    assert worker._classify_focus(_result(_CHILD_WEAK, 3, 4)) == (None, '')
    assert worker._classify_focus(_result(_CHILD_STRONG, 7, 0)) == (None, '')   # 연마할 게 없다
    assert worker._classify_focus(
        _result(_CHILD_STRONG, 4, 3, cached=True)) == (None, '')


def test_rc_genome_expresses_its_third_field():
    """`triple` 이 빠져 있어 RC 유전체는 fields[2] 를 한 번도 쓰지 못했다."""
    assert 'triple' in genome_models.ResearchConsultantGenomeModel.combines
    pop = genome_models.generate_population(
        account_type='research_consultant', round_num=11, forced_delay='1', n=8)
    triples = [p for p in pop if p['genome']['combine'] == 'triple']
    assert triples, '표본에 triple 이 하나도 없다 — 유전자 풀 확인'
    for p in triples:
        # 합성 팩터(syn_*)는 이름이 아니라 식으로 전개된다 → 렌더된 표현으로 확인한다.
        expr = genome_models._field_expr(p['genome']['fields'][2])
        assert expr in p['code'], (p['genome']['fields'][2], p['code'])
