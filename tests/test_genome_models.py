import re

from server import genome_models


def test_non_rc_population_uses_standard_genome_contract():
    rows = genome_models.generate_population(
        account_type='standard', round_num=3, forced_delay='1', n=8
    )
    assert len(rows) == 8
    assert all(r['desc'].startswith('standard-playwright-genome') for r in rows)
    assert all(r['settings']['delay'] == '1' for r in rows)
    assert all('filter=' not in r['code'] for r in rows)
    assert all(not re.search(r'(?:^|;)\s*[A-Za-z_]\w*\s*=', r['code']) for r in rows)


def test_rc_delay_zero_population_is_pv_only_and_api_safe():
    rows = genome_models.generate_population(
        account_type='research_consultant', round_num=4, forced_delay='0', n=8
    )
    assert len(rows) == 8
    forbidden = ('anl4_', 'implied_volatility', 'historical_volatility', 'pcr_', 'nws', 'scl', 'snt')
    for r in rows:
        assert r['desc'].startswith('rc-api-genome')
        assert r['settings']['delay'] == '0'
        assert not any(tok in r['code'] for tok in forbidden), r['code']
        assert 'filter=' not in r['code']


def test_shared_datasets_are_backed_by_local_datafields_csv():
    from pathlib import Path
    csv = Path(genome_models.__file__).with_name('IQC_brain_datafields.csv')
    known = set()
    for line in csv.read_text().splitlines()[1:]:
        if line.strip():
            known.add(line.split(',', 1)[0].strip())
    platform_fields = {'close', 'open', 'high', 'low', 'vwap', 'volume', 'returns', 'adv20', 'cap'}
    missing = []
    for family, fields in genome_models.SHARED_DATASETS.items():
        for field in fields:
            if field not in known and field not in platform_fields:
                missing.append((family, field))
    assert missing == []


def _parent(**over):
    base = {
        'model': 'seed', 'family': 'fundamental',
        'fields': ('assets', 'equity', 'debt'),
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum',
        'sign': 1, 'lookback_a': 20, 'lookback_b': 60,
        'universe': 'TOP1000', 'neutralization': 'SECTOR',
        'decay': 4, 'truncation': 0.08, 'nan_handling': 'ON', 'generation': 2,
    }
    base.update(over)
    return base


def test_focus_population_is_directed_mutation_of_parent():
    """focus 라운드: 전 슬롯이 부모 유전체의 변이/교차여야 한다 (랜덤 재생성 금지).
    turnover-high fail → 'smooth' 정향변이: decay 강화 + 부모 시그널 유전자 보존."""
    parent = _parent()
    rows = genome_models.generate_population(
        account_type='standard', round_num=7, forced_delay='1', n=8,
        parent_genome=parent, fail_items=['Turnover(58.3>40)'],
    )
    assert len(rows) == 8
    assert all(r['origin'] == 'mutate' for r in rows)
    assert all(r['genome']['generation'] == parent['generation'] + 1 for r in rows)
    # 정향변이 유전자 공간이 작아 일부 슬롯은 일반 변이로 강등될 수 있다 —
    # 대다수(6/8+)는 smooth 지시(decay 강화 + 시그널 유전자 계승)를 따라야 한다.
    smooth = [r for r in rows
              if r['genome']['decay'] >= 8
              and tuple(r['genome']['fields']) == parent['fields']]
    assert len(smooth) >= 6, [r['genome'] for r in rows]


def test_focus_population_mixes_parent_seed_crossover_when_seeds_exist():
    parent = _parent()
    seeds = [_parent(family='pv', fields=('close', 'high', 'low'),
                     universe='TOP500', generation=0),
             _parent(family='option',
                     fields=('implied_volatility_call_120',
                             'implied_volatility_put_120',
                             'historical_volatility_120'),
                     generation=1)]
    rows = genome_models.generate_population(
        account_type='standard', round_num=9, forced_delay='1', n=8,
        parent_genome=parent, fail_items=['Sharpe(0.8<1.25)'], seed_genomes=seeds,
    )
    origins = [r['origin'] for r in rows]
    assert origins.count('crossover') == 2
    assert origins.count('mutate') == 6


def test_exploration_population_uses_elite_seeds():
    seeds = [_parent(generation=0),
             _parent(family='pv', fields=('close', 'vwap', 'volume'),
                     universe='TOP500', generation=1)]
    rows = genome_models.generate_population(
        account_type='standard', round_num=11, forced_delay='1', n=8,
        seed_genomes=seeds,
    )
    origins = [r['origin'] for r in rows]
    assert origins.count('random') >= 4               # 탐색 절반 보장
    assert origins.count('crossover') + origins.count('mutate') >= 2


def test_rc_delay_zero_constraint_survives_mutation_and_crossover():
    """delay=0 RC: 부모/seed 가 fundamental 유전자를 갖고 있어도 자식은 PV 만 쓴다."""
    parent = _parent()  # fundamental 부모
    seeds = [_parent(family='analyst',
                     fields=('anl4_bvps_mean', 'anl4_netdebt_mean',
                             'anl4_afv4_eps_mean'))]
    rows = genome_models.generate_population(
        account_type='research_consultant', round_num=13, forced_delay='0', n=8,
        parent_genome=parent, fail_items=['Fitness(0.2<1.0)'], seed_genomes=seeds,
    )
    pv = set(genome_models.SHARED_DATASETS['pv'])
    for r in rows:
        assert all(f in pv for f in r['genome']['fields']), r['genome']
        assert r['settings']['delay'] == '0'
        assert r['genome']['neutralization'] != 'NONE'
        assert r['genome']['decay'] <= 8


def test_bandit_arms_apply_to_random_slots_in_order():
    arms = [
        {'universe': 'TOP200', 'neutralization': 'MARKET', 'decay': 6},
        {'universe': 'TOP500', 'neutralization': 'SUBINDUSTRY', 'decay': 6},
    ]
    rows = genome_models.generate_population(
        account_type='standard', round_num=17, forced_delay='1', n=8,
        slot_settings=arms,
    )
    assert all(r['origin'] == 'random' for r in rows)
    for i, arm in enumerate(arms):
        g = rows[i]['genome']
        assert g['universe'] == arm['universe']
        assert g['neutralization'] == arm['neutralization']
        assert g['decay'] == 6
        assert rows[i]['settings']['universe'] == arm['universe']


def test_generation_is_deterministic_for_same_inputs():
    kw = dict(account_type='standard', round_num=21, forced_delay='1', n=8,
              seed_genomes=[_parent()])
    a = genome_models.generate_population(**kw)
    b = genome_models.generate_population(**kw)
    assert [r['code'] for r in a] == [r['code'] for r in b]
    # salt 가 다르면 (RC 재시도 시나리오) 다른 세대가 나온다.
    c = genome_models.generate_population(**kw, salt=12345)
    assert [r['code'] for r in c] != [r['code'] for r in a]


def test_genome_from_alpha_roundtrips_renderer_output():
    rows = genome_models.generate_population(
        account_type='standard', round_num=23, forced_delay='1', n=4)
    r = rows[0]
    back = genome_models.genome_from_alpha(r['code'], settings=r['settings'])
    g = r['genome']
    # 3번째 필드는 combine='triple' 일 때만 코드에 발현된다 — 발현된 유전자만
    # 역추출을 보장할 수 있다.
    expressed = {f for f in g['fields'] if f in r['code']}
    assert expressed and expressed <= set(back['fields'])
    assert back['universe'] == g['universe']
    assert back['neutralization'] == g['neutralization']
    assert back['decay'] == g['decay']


def test_directed_mutation_decorrelates_on_selfcorr_fail():
    parent = _parent()
    rows = genome_models.generate_population(
        account_type='standard', round_num=29, forced_delay='1', n=8,
        parent_genome=parent,
        fail_items=['Self-correlation of 0.9415 is above cutoff of 0.7'],
    )
    # 탈상관 변이: 부모 패밀리(fundamental)와 다른 패밀리로 이동한 자식이 있어야 한다.
    assert any(r['genome']['family'] != 'fundamental' for r in rows)
