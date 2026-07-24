"""탐색 조건 파싱·준수판정·GA 주입 — 2026-07-22 신설.

배경: Power Pool 주간 테마가 "region=USA & delay=1 & universe=TOP1000 &
High Turnover returns ratio test PASS & datasets not in ['pv1']" 같은 필터로 온다.
이 조건 밖 알파는 **아무리 좋아도 쓸모가 없다**. 조건이 GA 생성 단계에서 확실히
걸리는지가 이 파일의 관심사다.
"""
import random

import pytest

from server import constraint_spec as cs
from server import genome_models as gm


@pytest.fixture(autouse=True)
def _clear_constraint():
    """조건은 모듈 전역이라 테스트가 서로 오염시킨다 — 매번 반드시 푼다."""
    gm.set_constraint(None)
    yield
    gm.set_constraint(None)


# ── 파싱 ────────────────────────────────────────────────────────────────────

def test_필터문법_전항목_파싱():
    s = cs.parse(cs.EXAMPLES['usa_d1_top1000_htvr'])
    assert s.region == 'USA'
    assert s.delay == '1'
    assert s.universe == 'TOP1000'
    assert s.excluded_datasets == frozenset({'pv1'})
    assert s.required_checks == ('HT_HIGH_TURNOVER_RETURNS_RATIO',)
    assert not s.unparsed


def test_and_구분자와_복수_제외_데이터셋():
    """GLB 테마는 `&` 대신 `and` 를 쓰고 데이터셋을 2개 제외한다."""
    s = cs.parse(cs.EXAMPLES['glb_d1_topdiv3000'])
    assert s.region == 'GLB'
    assert s.universe == 'TOPDIV3000'
    assert s.excluded_datasets == frozenset({'pv1', 'model110'})
    assert 'SLOW_AND_FAST' in s.neutralizations
    assert 'REVERSION_AND_MOMENTUM' in s.neutralizations   # 'ram' 별칭
    # "slow and fast" 가 "slow"/"fast" 로 쪼개져도 안 되지만, 각각도 있어야 한다.
    assert {'SLOW', 'FAST'} <= set(s.neutralizations)


def test_한국어_자연어():
    s = cs.parse('USA 딜레이1 TOP1000에서 pv1 제외하고 고회전 수익보존 통과하는 알파')
    assert (s.region, s.delay, s.universe) == ('USA', '1', 'TOP1000')
    assert s.excluded_datasets == frozenset({'pv1'})
    assert s.required_checks == ('HT_HIGH_TURNOVER_RETURNS_RATIO',)


def test_빈조건은_is_empty():
    assert cs.parse('').is_empty()
    assert cs.parse('   ').is_empty()


def test_해석못한_절은_버리지_않고_남긴다():
    """조용히 무시하면 조건을 놓친 채 '충족' 이라 착각한다 — 제출 예산이 비싸다."""
    s = cs.parse('region=USA & 뭔가 알 수 없는 요구사항 zzz')
    assert s.region == 'USA'
    assert s.unparsed and 'zzz' in s.unparsed[0]


# ── 준수 판정 ────────────────────────────────────────────────────────────────

def test_준수_판정_통과():
    s = cs.parse(cs.EXAMPLES['usa_d1_top1000_htvr'])
    ok, why = s.compliant(
        settings={'region': 'USA', 'delay': 1, 'universe': 'TOP1000',
                  'neutralization': 'STATISTICAL'},
        datasets=['option6', 'us_short_sale'],
        checks={'HT_HIGH_TURNOVER_RETURNS_RATIO': 'PASS'})
    assert ok and why == []


def test_금지_데이터셋_사용시_불충족():
    s = cs.parse(cs.EXAMPLES['usa_d1_top1000_htvr'])
    ok, why = s.compliant(settings={'region': 'USA', 'delay': 1, 'universe': 'TOP1000'},
                          datasets=['pv1'],
                          checks={'HT_HIGH_TURNOVER_RETURNS_RATIO': 'PASS'})
    assert not ok
    assert any('pv1' in r for r in why)


def test_모르는_정보는_불충족으로_본다():
    """확인 못 한 걸 통과로 세면 제출 예산을 버린다."""
    s = cs.parse(cs.EXAMPLES['usa_d1_top1000_htvr'])
    ok, why = s.compliant(settings={'region': 'USA', 'delay': 1, 'universe': 'TOP1000'},
                          datasets=None, checks=None)
    assert not ok
    assert any('미확인' in r for r in why)
    assert any('미측정' in r for r in why)


# ── GA 주입 ─────────────────────────────────────────────────────────────────

def _violations(pop, banned):
    import re
    bad_settings, bad_fields = [], []
    for s in pop:
        st = s.get('settings') or {}
        if (str(st.get('universe')) != 'TOP1000' or str(st.get('region')) != 'USA'
                or str(st.get('delay')) != '1'):
            bad_settings.append(st)
        code = s.get('code') or ''
        ids = {t.lower() for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', code)}
        calls = {c.lower() for c in re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', code)}
        hit = (ids - calls) & banned
        if hit:
            bad_fields.append((sorted(hit), code[:80]))
    return bad_settings, bad_fields


def test_조건_걸면_생성_개체가_전부_준수한다():
    gm.set_constraint(cs.parse(cs.EXAMPLES['usa_d1_top1000_htvr']))
    banned = gm.constraint_banned_fields()
    assert 'close' in banned and 'volume' in banned   # 라이브 CSV 가 잘려도 잡혀야 한다
    pop = gm.generate_population(account_type='research_consultant', round_num=1,
                                 forced_delay='1', n=24)
    bad_settings, bad_fields = _violations(pop, banned)
    assert not bad_settings, f'설정 위반: {bad_settings[:3]}'
    assert not bad_fields, f'금지 필드 사용: {bad_fields[:3]}'


def test_합성필드_전개까지_검사한다():
    """syn_clv 는 렌더링될 때 (close-low)-(high-close) 로 전개돼 pv1 을 끌어온다.
    이름만 비교하면 통과해 버린다."""
    gm.set_constraint(cs.parse("datasets not in ['pv1']"))
    assert gm._field_is_banned('syn_clv')
    assert gm._field_is_banned('syn_vwap_dev')
    assert not gm._field_is_banned('opt6_vimtaxp')


def test_조건식_유전자도_끈다():
    """trade_when/regime/group_by 는 pv1 필드를 템플릿에 하드코딩한다."""
    gm.set_constraint(cs.parse("datasets not in ['pv1']"))
    assert gm._gene_uses_banned('trade_when', 'vol_surge')      # volume
    assert gm._gene_uses_banned('regime', 'range_expand')       # high, low
    assert gm._gene_uses_banned('group_by', 'subindustry')
    assert gm._gene_uses_banned('group_by', 'auto', 'INDUSTRY')  # GROUPS → industry
    assert not gm._gene_uses_banned('group_by', 'auto', 'STATISTICAL')
    assert not gm._gene_uses_banned('trade_when', 'OFF')


def test_조건_해제하면_원래대로():
    gm.set_constraint(None)
    assert gm.active_constraint() is None
    assert gm.constraint_banned_fields() == frozenset()
    pop = gm.generate_population(account_type='research_consultant', round_num=1,
                                 forced_delay='1', n=12)
    # 무제약이면 유니버스가 한 값으로 고정되지 않는다.
    assert len({(s.get('settings') or {}).get('universe') for s in pop}) > 1


def test_중립화_제약_적용():
    gm.set_constraint(cs.parse(
        'region=USA & delay=1 & universe=TOP1000 & neutralization in (statistical, crowding)'))
    pop = gm.generate_population(account_type='research_consultant', round_num=2,
                                 forced_delay='1', n=16)
    got = {(s.get('settings') or {}).get('neutralization') for s in pop}
    assert got <= {'STATISTICAL', 'CROWDING'}, got


def test_결정론_같은_유전체는_같은_중립화():
    """_constrain 은 캐시 키(_dedup_key)의 입력이라 호출마다 값이 달라지면 안 된다."""
    gm.set_constraint(cs.parse('neutralization in (statistical, crowding)'))
    d = {'family': 'option', 'fields': ('a', 'b', 'c'), 'decay': 4,
         'neutralization': 'INDUSTRY'}
    outs = []
    for _ in range(5):
        dd = dict(d)
        gm._apply_constraint(dd)
        outs.append(dd['neutralization'])
    assert len(set(outs)) == 1
