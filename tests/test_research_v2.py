import sqlite3

import pytest

from server import db, genome_models, research_v2, run_config, selection


def test_canonical_identity_is_hygiene_first_and_settings_aware():
    raw = 'rank(close - vwap)'
    hygienic = research_v2.canonicalize(raw, {'neutralization': 'MARKET'}, 1)
    same = research_v2.canonicalize(hygienic['code'], {'neutralization': 'MARKET'}, 1)
    other = research_v2.canonicalize(raw, {'neutralization': 'SECTOR'}, 1)
    assert hygienic['canonical_key'] == same['canonical_key']
    assert hygienic['canonical_key'] != other['canonical_key']
    assert 'ts_backfill' in hygienic['code']


def test_submit_403_prod_corr_is_promoted_to_metrics_and_fail_items():
    result = research_v2.promote_submit_evidence({
        'metrics': {'sharpe': '1.7'},
        'is_status': {'pass': [], 'fail': []},
        'submit_status': 'rejected:PROD_CORRELATION(0.834 vs 0.7) (http_403)',
    })
    assert result['metrics']['prod_correlation'] == '0.834'
    assert result['is_status']['fail'][0]['name'] == 'PROD_CORRELATION'


def test_one_corr_wall_does_not_switch_the_whole_policy_to_escape():
    recent = [{'submit_status': 'rejected:PROD_CORRELATION(0.8 vs 0.7)'}] * 8
    recent += [{'submit_status': 'rejected:LOW_FITNESS'}] * 2
    mode, reason = research_v2.choose_search_mode(11, recent)
    assert mode == 'exploit'
    assert 'local refinement' in reason


def test_focus_dataset_corr_wall_overrides_global_exploit():
    risk = 'rsk70_mfm2_gemtrd_srtindcnt'
    recent = [
        {'code': f'rank({risk})',
         'metrics': {'prod_correlation': '0.82'}, 'submit_status': ''},
        {'code': f'ts_rank({risk},20)',
         'metrics': {},
         'submit_status': 'rejected:PROD_CORRELATION(0.91 vs 0.7)'},
    ]
    recent += [{'code': f'rank(close+{i})', 'metrics': {'sharpe': '0.5'}}
               for i in range(20)]
    mode, reason = research_v2.choose_search_mode(
        891, recent, focus_code=f'ts_zscore({risk},60)')
    assert mode == 'escape'
    assert 'exact-lineage quarantine' in reason


def test_lineage_policy_quarantines_only_exact_rejected_dataset_key():
    risk = 'rsk70_mfm2_gemtrd_srtindcnt'
    recent = [
        {'code': f'rank({risk})', 'metrics': {'sharpe': '2.1', 'fitness': '1.2'},
         'submit_status': 'rejected:PROD_CORRELATION(0.82 vs 0.7)'},
        {'code': f'ts_rank({risk},20)', 'metrics': {'sharpe': '2.0', 'fitness': '1.1'},
         'submit_status': 'rejected:PROD_CORRELATION(0.79 vs 0.7)'},
        {'code': f'rank({risk}+close)',
         'metrics': {'sharpe': '1.7', 'fitness': '1.05', 'prod_correlation': '0.62'}},
        {'code': 'rank(close)',
         'metrics': {'sharpe': '1.72', 'fitness': '1.49',
                     'glb_amer_sharpe': '1.25', 'glb_emea_sharpe': '0.76',
                     'glb_apac_sharpe': '1.02'}},
    ]
    policy = research_v2.build_lineage_policy(recent)
    assert 'risk70' in policy['quarantined']
    assert 'pv1+risk70' not in policy['quarantined']
    assert 'pv1' in policy['near_miss_keys']


def test_seed_selection_prefers_non_quarantined_near_miss_and_caps_probe():
    policy = {'quarantined': ['risk'], 'dataset_stats': {},
              'near_miss_keys': ['pv1'], 'near_miss_count': 1}
    rows = [
        {'id': 1, 'code': 'rank(risk_a)', 'genome': {'family': 'risk'},
         'metrics': {'sharpe': '3.0', 'fitness': '2.0'}},
        {'id': 2, 'code': 'rank(risk_b)', 'genome': {'family': 'risk'},
         'metrics': {'sharpe': '2.9', 'fitness': '2.0'}},
        {'id': 3, 'code': 'rank(close)', 'genome': {'family': 'pv'},
         'metrics': {'sharpe': '1.7', 'fitness': '0.9'}},
        {'id': 4, 'code': 'rank(model_a)', 'genome': {'family': 'model'},
         'metrics': {'sharpe': '1.6', 'fitness': '1.1'}},
    ]
    selected = research_v2.select_seed_rows(rows, policy, top_n=4)
    assert selected[0]['id'] == 3
    assert sum(research_v2.dataset_key(row) == 'risk' for row in selected) == 1


def test_lineage_profile_accepts_db_json_genome_string():
    profile = research_v2.lineage_profile(
        'rank(unknown_field)', '{"family":"option"}')
    assert profile['dataset_key'] == 'option'


def test_round_concentration_caps_vector_neut_archetype():
    strategies = [
        {'idx': i + 1,
         'code': f'vector_neut(rank(field_{i}), rank(other_{i}))',
         'genome': {'family': 'model'}} for i in range(10)]
    strategies += [
        {'idx': 11 + i, 'code': f'rank(distinct_{i})',
         'genome': {'family': 'analyst'}} for i in range(4)]
    kept, dropped = research_v2.concentration_filter(strategies, min_keep=1)
    assert sum(1 for s in kept
               if (s['_v2_lineage']['expression_key'] == 'vector_neut')) <= 4
    assert dropped


def test_concentration_counts_dataset_inside_mixed_dataset_key():
    risk = 'rsk70_mfm2_gemtrd_srtindcnt'
    strategies = [
        {'idx': 1, 'code': f'rank({risk})'},
        {'idx': 2, 'code': f'ts_rank({risk},20)'},
        {'idx': 3, 'code': f'ts_zscore({risk},60)'},
        {'idx': 4, 'code': f'rank({risk}+close)'},
        {'idx': 5, 'code': 'rank(close)'},
        {'idx': 6, 'code': 'ts_rank(returns,20)'},
        {'idx': 7, 'code': 'ts_zscore(volume,60)'},
        {'idx': 8, 'code': 'rank(vwap)'},
        {'idx': 9, 'code': 'ts_delta(open,5)'},
        {'idx': 10, 'code': 'ts_mean(high,20)'},
    ]
    kept, dropped = research_v2.concentration_filter(strategies, min_keep=1)
    risk_kept = [s for s in kept if 'risk70' in s['_v2_lineage']['dataset_key']]
    assert len(risk_kept) <= 3
    assert any('risk70' in s['_v2_lineage']['dataset_key'] for s in dropped)


def test_concentration_prioritizes_recently_unused_dataset_coverage():
    risk = 'rsk70_mfm2_gemtrd_srtindcnt'
    strategies = [
        {'idx': 1, 'code': f'rank({risk})'},
        {'idx': 2, 'code': f'ts_rank({risk},20)'},
        {'idx': 3, 'code': 'rank(close)'},
        {'idx': 4, 'code': 'ts_rank(returns,20)'},
        {'idx': 5, 'code': 'rank(opt6_divyield)'},
        {'idx': 6, 'code': 'ts_rank(ern4_fcsterneffct,20)'},
    ]
    recent = [{'code': f'rank({risk})'} for _ in range(20)]
    kept, _ = research_v2.concentration_filter(
        strategies, min_keep=1, dataset_share=0.34,
        expression_share=0.34, recent=recent)
    keys = [s['_v2_lineage']['dataset_key'] for s in kept]
    assert keys[0] != 'risk70'
    assert len(set(keys[:3])) == 3


def test_concentration_restores_minimum_without_unbound_need():
    strategies = [
        {'idx': i, 'code': f'vector_neut(rank(field_{i}),rank(other_{i}))',
         'genome': {'family': 'model'}}
        for i in range(1, 9)
    ]
    kept, dropped = research_v2.concentration_filter(
        strategies, min_keep=6, dataset_share=0.10, expression_share=0.10)
    assert len(kept) == 6
    assert len(dropped) == 2


def test_quarantined_exact_lineage_uses_at_most_ten_percent_of_round():
    risk = 'rsk70_mfm2_gemtrd_srtindcnt'
    strategies = [
        {'idx': i, 'code': f'ts_rank({risk},{i + 2})'} for i in range(10)
    ] + [
        {'idx': 10 + i, 'code': f'rank(field_{i})',
         'genome': {'family': f'family_{i}'}} for i in range(10)
    ]
    kept, _ = research_v2.concentration_filter(
        strategies, min_keep=8, dataset_share=1.0, expression_share=1.0,
        policy={'quarantined': ['risk70']})
    assert sum(s['_v2_lineage']['dataset_key'] == 'risk70' for s in kept) <= 1


def test_absolute_quality_observation_ignores_warning_downgrade():
    weak = {
        '_delay': '1', 'region': 'GLB',
        'sharpe': '1.05', 'fitness': '0.71',
        'glb_amer_sharpe': '0.97', 'glb_emea_sharpe': '-0.73',
        'glb_apac_sharpe': '0.47',
    }
    assert research_v2.absolute_quality_observations(weak) == [
        'LOW_SHARPE', 'LOW_FITNESS', 'LOW_GLB_AMER_SHARPE',
        'LOW_GLB_EMEA_SHARPE', 'LOW_GLB_APAC_SHARPE',
    ]
    strong = dict(weak, sharpe='1.8', fitness='1.1',
                  glb_amer_sharpe='1.1', glb_emea_sharpe='1.2',
                  glb_apac_sharpe='1.3')
    assert research_v2.absolute_quality_observations(strong) == []


def test_v2_prunes_old_and_multi_phase_focus_debt():
    queue = [
        {'parent_round_num': 887, 'phase': 1, 'parent_idx': 1},
        {'parent_round_num': 888, 'phase': 1, 'parent_idx': 2},
        {'parent_round_num': 888, 'phase': 2, 'parent_idx': 2},
        {'parent_round_num': 888, 'phase': 1, 'parent_idx': 2},
        {'parent_round_num': 888, 'phase': 1, 'parent_idx': 3},
    ]
    kept, dropped = research_v2.prune_focus_queue(queue, 888)
    assert [(x['parent_idx'], x['phase']) for x in kept] == [(2, 1), (3, 1)]
    assert dropped == 3


def test_exploit_generation_changes_at_most_two_axes():
    seed = genome_models.generate_population(
        account_type='research_consultant', round_num=1, forced_delay='1', n=1)[0]
    children = genome_models.generate_population(
        account_type='research_consultant', round_num=2, forced_delay='1', n=10,
        seed_genomes=[seed['genome']], search_mode='exploit')
    local = [c for c in children if c['origin'] in ('local', 'sweep')]
    assert local
    assert all(1 <= len(c['genes_changed']) <= 2 for c in local)


@pytest.fixture
def isolated_v2_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'v2.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('v2-user', 'pw', 'GEMINI_FAKE_KEY_FOR_TEST')
    yield uid, str(tmp_path / 'v2.db')
    db._INITIALIZED = False


def test_evidence_spine_registers_one_canonical_identity(isolated_v2_db):
    uid, db_path = isolated_v2_db
    rid = db.start_round(uid, 1)
    can = research_v2.canonicalize('rank(close-vwap)', {'neutralization': 'MARKET'}, 1)
    lin = research_v2.lineage_profile(can['code'], {'family': 'pv'})
    first = db.v2_register_candidate(
        uid, rid, 1, 1, canonical=can, lineage=lin, search_mode='exploit',
        policy_version=research_v2.POLICY_VERSION)
    second = db.v2_register_candidate(
        uid, rid, 1, 2, canonical=can, lineage=lin, search_mode='exploit',
        policy_version=research_v2.POLICY_VERSION)
    assert not first['duplicate'] and second['duplicate']
    with sqlite3.connect(db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM canonical_alphas').fetchone()[0] == 1
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {'alpha_experiments', 'wqb_snapshots', 'evidence_cards',
            'policy_versions'} <= tables


def test_v2_portfolio_vector_adds_region_and_prod_axes(monkeypatch):
    monkeypatch.setattr(run_config, 'is_architecture_v2_enabled', lambda: True)
    row = {'metrics': {'sharpe': '1.8', 'fitness': '1.1', 'turnover': '0.3',
                       'glb_amer_sharpe': '1.0', 'glb_emea_sharpe': '1.2',
                       'glb_apac_sharpe': '1.4', 'prod_correlation': '0.62'}}
    vector = selection.obj_vector(row)
    assert vector[-2:] == [1.0, -0.62]
