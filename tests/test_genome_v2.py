"""Genome v2 유전자 확장 — 하위호환 불변식 + 신규 유전자 발현 검증.

핵심 계약(이게 깨지면 라이브가 죽는다):
  기본값 유전체의 render() 산출물은 확장 **이전과 바이트 단위로 동일**하다.
  같지 않으면 19k 기존 알파의 code_hash(=결과 캐시 키)와 _dedup_key 가 전부 무효화되고,
  이미 시뮬한 알파를 처음부터 다시 돌리게 된다.

Run: python3 -m pytest tests/test_genome_v2.py -v
"""
import itertools
import json
from pathlib import Path

import pytest

from server import alpha_ast, alpha_lint, genome_models as gm, presim_gate

_GOLDEN = Path(__file__).with_name('fixtures_golden_render.json')


def _mk(**over) -> gm.Genome:
    base = dict(
        model='rc-api-genome', family='pv', fields=('close', 'open', 'volume'),
        transform_a='rank', transform_b='ts_zscore', combine='sum', sign=1,
        lookback_a=20, lookback_b=60, universe='TOP3000', neutralization='INDUSTRY',
        decay=4, truncation=0.08, nan_handling='OFF', decay_style='mean', generation=0)
    base.update(over)
    return gm.Genome(**base)


# ── 하위호환: 기본값이면 확장 이전과 동일한 산출물 ────────────────────────────

def test_golden_render_is_byte_identical():
    """확장 전에 떠 둔 550건(라이브 유전체 100 + 합성 격자 450)의 render/dedup 재현."""
    cases = json.loads(_GOLDEN.read_text(encoding='utf-8'))
    assert len(cases) >= 500
    for c in cases:
        g = gm._coerce_genome(c['genome'])
        assert g is not None
        assert gm.render(g) == c['render'], c['genome']
        assert gm.BaseGenomeModel._dedup_key(g) == c['dedup']


def test_legacy_genome_dict_coerces_to_neutral_defaults():
    """v2 유전자가 통째로 없는 레거시 dict → 무해한 기본값."""
    legacy = {
        'model': 'seed', 'family': 'pv', 'fields': ['close', 'open', 'volume'],
        'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'sum',
        'sign': 1, 'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP3000',
        'neutralization': 'INDUSTRY', 'decay': 4, 'truncation': 0.08,
        'generation': 3,
    }
    g = gm._coerce_genome(legacy)
    assert (g.trade_when, g.group_op, g.group_by, g.winsor_std, g.weight_scheme) \
        == ('OFF', 'neutralize', 'auto', 0, '1:1')
    # 기본값 조합은 group_neutralize 만 발현 — 확장 이전 동작 그대로.
    assert gm.render(g) == 'rank(group_neutralize(add(rank(close),ts_zscore(open,60)),industry))'
    assert g.generation == 3, '세대는 보존돼야 한다'


def test_garbage_v2_values_fall_back_to_defaults():
    g = gm._coerce_genome({
        **dict(gm._coerce_genome({'model': 's', 'family': 'pv'}).__dict__),
        'trade_when': 'ROCKET', 'group_op': '???', 'group_by': 'galaxy',
        'winsor_std': 99, 'weight_scheme': '7:7',
    })
    assert (g.trade_when, g.group_op, g.group_by, g.winsor_std, g.weight_scheme) \
        == ('OFF', 'neutralize', 'auto', 0, '1:1')


# ── 신규 유전자 발현 ─────────────────────────────────────────────────────────

def test_trade_when_wraps_and_holds_nothing_outside_condition():
    g = _mk(trade_when='vol_calm')
    code = gm.render(g)
    assert code.startswith('trade_when(')
    assert code.endswith(',-1)'), '조건 밖에서는 미보유(-1)'
    assert gm._TRADE_WHEN_CONDS['vol_calm'] in code


def test_group_op_and_group_by_are_independent_of_neutralization():
    """group_by 를 명시하면 neutralization 이 MARKET(=그룹 없음)이어도 그룹연산이 산다."""
    g = _mk(group_op='rank', group_by='sector', neutralization='MARKET')
    assert gm.render(g) == 'rank(group_rank(add(rank(close),ts_zscore(open,60)),sector))'


def test_group_op_none_disables_grouping():
    g = _mk(group_op='none')
    assert 'group_' not in gm.render(g)


def test_winsorize_and_weight_scheme():
    g = _mk(winsor_std=4, weight_scheme='2:1')
    code = gm.render(g)
    assert 'winsorize(' in code and 'std=4' in code
    assert 'add(2*(rank(close)),1*(ts_zscore(open,60)))' in code


def test_weight_scheme_only_applies_to_sum_and_spread():
    """product/ratio/corr 은 스케일이 상쇄되거나 의미가 달라 가중을 발현하지 않는다."""
    for cmb in ('product', 'ratio', 'corr', 'triple'):
        code = gm.render(_mk(combine=cmb, weight_scheme='3:1'))
        assert '3*(' not in code, cmb


# ── 전 조합이 FASTEXPR 로 유효한가 (lint · AST · presim) ──────────────────────

def test_every_v2_combination_is_lint_clean_and_gate_safe():
    checked = 0
    for tw, gop, gby, ws, wsch, cmb in itertools.product(
            gm.TRADE_WHEN_KINDS, gm.GROUP_OPS, gm.GROUP_BYS, gm.WINSOR_STDS,
            tuple(gm.WEIGHT_SCHEMES), gm.BaseGenomeModel.combines):
        code = gm.render(_mk(trade_when=tw, group_op=gop, group_by=gby,
                             winsor_std=ws, weight_scheme=wsch, combine=cmb))
        assert alpha_lint.validate_alpha(code) == [], code
        assert alpha_ast.parse(code) is not None, code
        kept, dropped = presim_gate.screen(
            [{'idx': 1, 'code': code, 'settings': {}}], existing_codes=[])
        assert not dropped, (code, dropped)
        checked += 1
    # 5 trade_when × 4 group_op × 5 group_by × 4 winsor × 4 weight × 7 combine
    # (2026-07-23 'resid' 결합 추가로 9600 → 11200)
    assert checked == 11200


def test_named_arg_keys_are_not_datafields():
    """`std=4` 의 'std' 가 필드로 잡히면 presim 팔레트 검사가 알파를 통째로 드롭한다.
    (`filter=True`·`hump=` 같은 기존 관용구도 같은 함정 — alpha_ast 에서 근본 차단.)"""
    code = 'rank(winsorize(add(close,open,filter=True),std=4))'
    fields = alpha_ast.fields_used(code)
    assert 'std' not in fields
    assert 'filter' not in fields
    assert {'close', 'open'} <= fields


def test_group_names_are_not_datafields():
    fields = alpha_ast.fields_used('rank(group_neutralize(close,subindustry))')
    assert 'subindustry' not in fields
    assert 'close' in fields


# ── GA 연산자가 신규 유전자를 다루는가 ────────────────────────────────────────

def test_crossover_inherits_v2_genes():
    a = _mk(trade_when='vol_calm', winsor_std=4, weight_scheme='2:1', group_by='sector')
    b = _mk(trade_when='OFF', winsor_std=0, weight_scheme='1:1', group_by='auto')
    model = gm.ResearchConsultantGenomeModel(round_num=1, forced_delay='1')
    import random
    seen = set()
    for s in range(40):
        c = model._crossover(a, b, random.Random(s))
        seen.add((c.trade_when, c.winsor_std, c.weight_scheme, c.group_by))
    assert len(seen) > 1, '교차가 v2 유전자를 섞지 않는다 (전부 한쪽만 상속)'


def test_directed_mutation_uses_new_levers():
    """turnover 초과 → trade_when 조건부 진입이 실제로 켜진다 (가장 강한 turnover 레버)."""
    import random
    parent = _mk(trade_when='OFF')
    model = gm.ResearchConsultantGenomeModel(
        round_num=1, forced_delay='1', fail_items=['Turnover(0.9>0.7)'])
    kinds = {model._mutate(parent, random.Random(s)).trade_when for s in range(30)}
    assert kinds - {'OFF'}, 'smooth 지시가 trade_when 을 한 번도 켜지 않았다'


def test_concentration_directive_turns_on_winsorize():
    import random
    parent = _mk(winsor_std=0)
    model = gm.ResearchConsultantGenomeModel(
        round_num=1, forced_delay='1', fail_items=['Weight Concentration'])
    stds = {model._mutate(parent, random.Random(s)).winsor_std for s in range(30)}
    assert stds - {0}, 'concentration 지시가 winsorize 를 켜지 않았다'


def test_population_still_deterministic_with_v2_genes():
    kw = dict(account_type='research_consultant', round_num=7, forced_delay='1', n=8)
    a = gm.generate_population(**kw)
    b = gm.generate_population(**kw)
    assert [x['code'] for x in a] == [x['code'] for x in b]
    assert [x['genome']['trade_when'] for x in a] == [x['genome']['trade_when'] for x in b]


def test_rc_model_constrains_winsor_std():
    import random
    model = gm.ResearchConsultantGenomeModel(round_num=1, forced_delay='1')
    g = model._constrain(_mk(winsor_std=5), random.Random(0))
    assert g.winsor_std in (0, 4)
