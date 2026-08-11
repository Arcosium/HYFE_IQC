# tests/test_theme_playbook.py
# 테마 플레이북(2026-07-27): 테마 브리핑 구성 + 리전 팔레트 검증 통과.
import pytest

from server import constraint_spec, genome_models, strategy_spec, theme_playbook


@pytest.fixture(autouse=True)
def _reset():
    yield
    genome_models.set_constraint(None)


def test_brief_includes_constraint_themes_and_implications(monkeypatch):
    spec = constraint_spec.parse(
        "region=GLB & delay=1 & universe=TOPDIV3000 and neutralization in (slow, fast)")
    monkeypatch.setattr(theme_playbook.run_config, 'get_constraint', lambda: spec)
    monkeypatch.setattr(theme_playbook._db, 'recent_metrics', lambda *a, **k: [
        {'themes': 'GLB High Turnover Theme,GLB/D1/MODEL Pyramid Theme',
         'themes_unmatched': 'USA/D1 Power Pool July`26 2',
         'theme_multiplier': '2.4', 'sharpe': '1.9', 'turnover': '0.55',
         '_submitted': True},
        {'themes': 'GLB High Turnover Theme', 'sharpe': '1.1', 'turnover': '0.42',
         '_submitted': False},
    ])
    b = theme_playbook.brief(2)
    assert 'TOPDIV3000' in b                      # 조건
    assert 'GLB High Turnover Theme' in b         # 활성 테마
    assert '2.4' in b                             # 배수
    assert '0.2~0.7' in b                         # 고회전 함의(회전율 대역)
    assert '제출 성공 1건' in b                    # 실측 프로파일
    assert 'Pyramid' in b                         # 피라미드 배수 안내


def test_brief_without_theme_data_says_so(monkeypatch):
    monkeypatch.setattr(theme_playbook.run_config, 'get_constraint', lambda: None)
    monkeypatch.setattr(theme_playbook._db, 'recent_metrics', lambda *a, **k: [])
    b = theme_playbook.brief(2)
    assert '아직 테마 관측 데이터가 없다' in b    # 추측을 지어내지 않는다


def test_validate_and_build_accepts_region_fields(monkeypatch):
    """리전 전환 후 GLB 필드로 만든 스펙이 폐기되지 않아야 한다 (2026-07-27 블로커)."""
    monkeypatch.setattr(genome_models, '_REGION_DATASETS',
                        {'model': ('glb_m1', 'glb_m2', 'glb_m3', 'glb_m4')}, raising=False)
    assert 'glb_m1' in strategy_spec._palette_block()
    known = {f for fam in strategy_spec._active_palette().values() for f in fam}
    assert 'glb_m1' in known and 'close' not in known


def test_seed_specs_is_fail_open(monkeypatch):
    monkeypatch.setattr(theme_playbook.run_config, 'get_constraint', lambda: None)
    monkeypatch.setattr(theme_playbook._db, 'recent_metrics', lambda *a, **k: [])
    import server.strategy_spec as ss
    monkeypatch.setattr(ss, 'concretize', lambda *a, **k: [])   # LLM 실패 상황
    assert theme_playbook.seed_specs(2) == 0                    # 예외 없이 0


def test_primary_theme_prefers_current_power_pool_over_general_matches():
    spec = constraint_spec.parse('region=GLB & delay=1 & universe=TOPDIV3000')
    themes = {
        'matched': {'GLB High Turnover Theme': None,
                    'GLB/D1/PV Pyramid Theme': None},
        'all': ['GLB High Turnover Theme', 'GLB/D1/PV Pyramid Theme',
                "GLB/D1 Power Pool Aug'26 2"],
    }
    assert theme_playbook.primary_theme(themes, spec) == "GLB/D1 Power Pool Aug'26 2"
