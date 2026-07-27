# tests/test_iqc_account_rules.py
# 비-컨설턴트는 IQC 규칙을 따른다 — 리전 USA 고정, delay 0/1 선택 가능 (2026-07-27).
from server import constraint_spec as cs


def test_non_consultant_is_pinned_to_usa():
    """전역 조건이 GLB 를 가리켜도 일반 계정은 그 리전으로 경쟁할 수 없다."""
    glb = cs.parse("region=GLB & delay=1 & universe=TOPDIV3000")
    assert glb.region == 'GLB'
    out = glb.for_account('standard')
    assert out.region == 'USA'
    assert 'IQC' in out.label


def test_non_consultant_keeps_delay_choice():
    """delay 는 IQC 에서도 0/1 둘 다 고를 수 있다 — 건드리지 않는다."""
    for d in ('0', '1'):
        out = cs.parse(f"region=USA & delay={d}").for_account('standard')
        assert out.region == 'USA' and out.delay == d


def test_foreign_universe_is_dropped_when_region_is_forced():
    """TOPDIV3000 은 GLB 유니버스다 — USA 로 갈아탈 때 들고 가면 시뮬이 죽는다."""
    out = cs.parse("region=GLB & universe=TOPDIV3000").for_account('standard')
    assert out.region == 'USA' and out.universe is None


def test_usa_universe_survives():
    out = cs.parse("region=USA & universe=TOP1000").for_account('standard')
    assert out.universe == 'TOP1000'


def test_consultant_constraint_is_untouched():
    glb = cs.parse("region=GLB & delay=1 & universe=TOPDIV3000")
    assert glb.for_account('research_consultant') is glb


def test_empty_constraint_still_pins_region_for_non_consultant():
    """조건이 아예 없어도 일반 계정은 USA 로 가둔다 (워커가 빈 조건에서 출발)."""
    out = cs.ConstraintSpec().for_account('standard')
    assert out.region == 'USA' and not out.is_empty()


# ── 중립화 제한 (2026-07-27 실계정 실측) ────────────────────────────────────
# 일반 계정은 그룹 중립화 5종만 쓸 수 있다. 리스크 중립화를 넣으면 WQB 가
# "Neutralization X is not available." 로 400 을 준다 — 시뮬이 접수조차 안 된다.

def test_consultant_only_neutralizations_are_stripped():
    c = cs.parse("region=USA & neutralization in (crowding, fast, industry)")
    out = c.for_account('standard')
    assert 'INDUSTRY' in out.neutralizations
    assert not ({'CROWDING', 'FAST'} & set(out.neutralizations))


def test_all_consultant_neutralizations_means_no_constraint_not_empty_pool():
    """전부 컨설턴트 전용이면 제약을 푼다 — 빈 목록을 남기면 고를 게 없어진다."""
    c = cs.parse("region=GLB & neutralization in (slow, fast, crowding)")
    out = c.for_account('standard')
    assert out.neutralizations == ()
    assert out.region == 'USA'


def test_standard_genome_never_emits_a_risk_neutralization():
    """유전자 풀에서 빼야 교차·변이로도 다시 안 생긴다."""
    from server import genome_models as gm
    gm.set_account_datasets(None)
    gm.set_constraint(None)
    rows = gm.generate_population(account_type='standard', round_num=7,
                                  forced_delay='1', n=12)
    bad = [r['settings']['neutralization'] for r in rows
           if r['settings']['neutralization'] in gm.RISK_NEUTRALIZATIONS]
    assert not bad, f'일반 계정이 못 쓰는 중립화 발현: {bad}'


# ── 계정 등급별 데이터필드 제한 (2026-07-27 실측: RC 297 데이터셋 vs 일반 21) ──

def test_account_dataset_allowlist_narrows_the_palette():
    """팔레트는 하우스 RC 계정으로 긁는다 — 일반 계정에 없는 필드를 내보내면
    시뮬이 'Invalid data field …' 로 죽는다."""
    from server import datafield_palette as dp
    wide = dp.family_pools(delay=1, region='USA', universe='TOP3000')
    narrow = dp.family_pools(delay=1, region='USA', universe='TOP3000',
                             datasets={'fundamental6'})
    n_wide = sum(len(v) for v in wide.values())
    n_narrow = sum(len(v) for v in narrow.values())
    assert n_wide > 0
    assert n_narrow < n_wide, '데이터셋 허용목록이 팔레트를 좁히지 못했다'


def test_dataset_allowlist_is_part_of_the_pool_cache_key():
    """캐시 키에 안 넣으면 첫 호출 결과가 다른 계정에까지 새어 나간다."""
    from server import datafield_palette as dp
    a = dp.family_pools(delay=1, region='USA', universe='TOP3000', datasets={'fundamental6'})
    b = dp.family_pools(delay=1, region='USA', universe='TOP3000', datasets={'analyst4'})
    assert a != b or (not a and not b)
