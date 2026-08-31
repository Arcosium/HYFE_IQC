import re

from server import genome_models


def test_non_rc_population_uses_standard_genome_contract():
    rows = genome_models.generate_population(
        account_type='standard', round_num=3, forced_delay='1', n=8
    )
    assert len(rows) == 8
    assert all(r['desc'].startswith('standard-genome') for r in rows)
    assert all(r['settings']['delay'] == '1' for r in rows)
    assert all('filter=' not in r['code'] for r in rows)
    assert all(not re.search(r'(?:^|;)\s*[A-Za-z_]\w*\s*=', r['code']) for r in rows)


def test_rc_delay_zero_population_uses_only_d0_valid_fields(monkeypatch):
    """D0 알파는 **D0 에 실재하는 필드만** 쓴다.

    ⚠ 2026-07-21 계약 변경: 예전엔 'D0 = pv 전용' 이었다. 근거였던 "D0 에선 fundamental/
      analyst 가 ERROR" 는 라이브 실측으로 반증됐다(option6 D0 131필드·fundamental2 766필드,
      셋 다 정상 시뮬). 그 제약이 USA/D0/OPTION 피라미드 1.7배를 구조적으로 막고 있었다.
      이제 지켜야 할 계약은 'pv 만' 이 아니라 '**D0 팔레트 안에서만**' 이다.
    """
    palette = {'pv': genome_models.SHARED_DATASETS['pv'],
               'option': ('opt6_20div', 'opt6_ivspyratio', 'opt6_fcstr2imp')}
    monkeypatch.setattr(genome_models, 'D0_DATASETS', palette)
    allowed = genome_models.d0_allowed_fields()
    rows = genome_models.generate_population(
        account_type='research_consultant', round_num=4, forced_delay='0', n=8
    )
    assert len(rows) == 8
    for r in rows:
        assert r['desc'].startswith('rc-api-genome')
        assert r['settings']['delay'] == '0'
        assert all(f in allowed for f in r['genome']['fields']), r['genome']
        assert 'filter=' not in r['code']


def test_rc_delay_zero_without_palette_stays_pv_only(monkeypatch):
    """D0 팔레트를 모르면 옛 안전 동작(pv 전용)을 유지한다 — 라운드 전멸 방지."""
    monkeypatch.setattr(genome_models, 'D0_DATASETS', {})
    rows = genome_models.generate_population(
        account_type='research_consultant', round_num=4, forced_delay='0', n=8
    )
    forbidden = ('anl4_', 'implied_volatility', 'historical_volatility', 'pcr_', 'nws', 'scl', 'snt')
    for r in rows:
        assert not any(tok in r['code'] for tok in forbidden), r['code']


def test_shared_datasets_are_backed_by_local_datafields_csv():
    """유전체가 고르는 모든 필드는 실재해야 한다 — 할루시네이션 필드는 시뮬에서 죽는다.

    예외는 둘뿐이다:
      - platform_fields: close/open/… 은 WQB 보편 필드라 /data-fields 목록에 없다.
      - SYNTHETIC_FIELDS(syn_*): 필드가 아니라 **식**이다. render() 가 pv 원시필드로
        전개하므로 CSV 에 있을 수가 없다 (있으면 오히려 이름 충돌 버그다).
    """
    # 팔레트가 실제로 읽는 소스와 같은 것을 본다 (라이브 ∪ 정적) — 정적 CSV 만 보면
    # 라이브 수집 이후 환경에서 오탐이 난다.
    from server import datafield_palette as _dp
    known = {(r.get('name') or '').strip() for r in _dp._all_rows()}
    # ⚠ CSV 만으로는 부족하다 — /data-fields 는 count 를 10000 으로 캡하고 필드 id
    #   알파벳순으로 주므로 shortinterest36·us_short_sale·option6 처럼 뒤쪽 글자로
    #   시작하는 데이터셋이 통째로 빠진다. 데이터셋별 직접 수집 매핑(field_dataset.json)이
    #   더 정확한 진실 소스다. 합집합으로 봐야 실재 필드를 오탐하지 않는다.
    known |= {k for k in _dp.field_dataset_map()}
    known |= {k.lower() for k in list(known)}
    platform_fields = {'close', 'open', 'high', 'low', 'vwap', 'volume', 'returns', 'adv20', 'cap'}
    synthetic = set(genome_models.SYNTHETIC_FIELDS)
    # 합성 팔레트가 실재 필드명을 가로채면 안 된다.
    assert not (synthetic & known), '합성 팩터 이름이 실재 datafield 와 충돌한다'
    missing = []
    for family, fields in genome_models.SHARED_DATASETS.items():
        for field in fields:
            if field not in known and field not in platform_fields and field not in synthetic:
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
    # 2026-07-22 — 앞 SWEEP_SLOTS 칸은 '노브 스윕'이다. 전 슬롯이 **부모 파생**이라는
    # 원래 계약(랜덤 재생성 금지)은 그대로다.
    assert all(r['origin'] in ('mutate', 'sweep') for r in rows)
    assert all(r['genome']['generation'] == parent['generation'] + 1 for r in rows)
    # 정향변이 유전자 공간이 작아 일부 슬롯은 일반 변이로 강등될 수 있다 —
    # 변이 슬롯의 대다수는 smooth 지시(decay 강화 + 시그널 유전자 계승)를 따라야 한다.
    muts = [r for r in rows if r['origin'] == 'mutate']
    smooth = [r for r in muts
              if r['genome']['decay'] >= 8
              and tuple(r['genome']['fields']) == parent['fields']]
    assert len(smooth) >= len(muts) - 2, [r['genome'] for r in muts]


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
    # 2026-07-22 — 앞 SWEEP_SLOTS 칸은 '노브 스윕'(부모에서 한 축만 바꾼 변형)이다.
    # 포커스 라운드가 **전부 부모 파생**이라는 원래 계약은 그대로다(random 0).
    assert origins.count('random') == 0
    assert origins.count('crossover') == 2
    assert origins.count('sweep') == genome_models.SWEEP_SLOTS
    assert origins.count('mutate') == 8 - 2 - genome_models.SWEEP_SLOTS


def test_focus_escape_drops_parent_crossover_tail():
    """상관벽 탈출은 부모 식을 보존하는 교차·국소 슬롯을 남기지 않는다."""
    parent = _parent(family='risk', fields=('close', 'vwap', 'volume'))
    seeds = [_parent(family='pv', fields=('open', 'high', 'low'))]
    rows = genome_models.generate_population(
        account_type='standard', round_num=10, forced_delay='1', n=12,
        parent_genome=parent, seed_genomes=seeds, search_mode='escape',
    )
    origins = [r['origin'] for r in rows]
    assert origins.count('escape') == 9
    assert origins.count('random') == 3
    assert origins.count('crossover') == 0
    assert origins.count('local') == 0


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


def test_rc_delay_zero_constraint_survives_mutation_and_crossover(monkeypatch):
    """delay=0 RC: 부모/seed 가 D0 에 없는 필드를 갖고 있어도 자식은 D0 유효 필드만 쓴다.

    (변이·교차를 거친 뒤에도 _constrain 이 다시 걸리는지 보는 테스트라, 계약이
     'pv 만' → 'D0 팔레트 안' 으로 바뀐 뒤에도 의미는 그대로다.)
    """
    monkeypatch.setattr(genome_models, 'D0_DATASETS',
                        {'pv': genome_models.SHARED_DATASETS['pv']})
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
