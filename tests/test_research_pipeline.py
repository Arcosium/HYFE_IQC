"""리서치 → 가설 → 전략스펙 → GA 시딩 파이프라인.

Arachne 와 LLM 은 전부 목(mock)이다 — 이 테스트는 네트워크를 타지 않는다.
검증 대상은 '배선' 이다: 근거가 없어도 안 죽는가, 할루시네이션 유전자를 걸러내는가,
스펙이 GA 의 초기 개체로 무손실 진입하는가, 소진되면 평소 GA 로 돌아가는가.

Run: python3 -m pytest tests/test_research_pipeline.py -v
"""
import json

import pytest

from server import db, genome_models as gm, ideation, research, strategy_spec


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'rs.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('rs@test.com', 'pw', 'FAKE')
    yield uid
    db._INITIALIZED = False


# ── research.py — Arachne 클라이언트 (fail-open 이 생명) ──────────────────────

def test_gather_is_fail_open_when_arachne_is_down(monkeypatch):
    def _boom(*a, **kw):
        raise OSError('connection refused')
    monkeypatch.setattr(research, '_post', _boom)
    assert research.gather('아무거나') == {}


def test_gather_ignores_empty_query():
    assert research.gather('') == {}
    assert research.gather('   ') == {}


def test_format_evidence_numbers_globally_and_keeps_extras():
    data = {
        'results': [
            {'title': 'A', 'url': 'https://a.com', 'content': 'AAA 본문'},
            {'title': 'B', 'url': 'https://b.com', 'content': 'BBB 본문'},
        ],
        'extra': [{'title': 'C', 'url': 'https://c.com', 'snippet': 'CCC 요약'}],
    }
    block, entries, nxt = research.format_evidence(data, start_idx=5)
    assert '[출처5] A' in block and '[출처6] B' in block
    assert '[참고7] C' in block, 'extra 를 버리면 안 된다 (스니펫만이라도 싣는다)'
    assert nxt == 8
    assert [e['n'] for e in entries] == [5, 6, 7]


def test_format_evidence_skips_bodyless_results():
    data = {'results': [{'title': 'X', 'url': 'https://x.com', 'content': ''}]}
    block, entries, nxt = research.format_evidence(data, start_idx=1)
    assert block == '' and entries == [] and nxt == 1


def test_build_research_evidence_dedups_urls_across_aspects(monkeypatch):
    """각도마다 같은 URL 이 잡히면 번호만 늘고 정보는 안 는다."""
    hit = {'results': [{'title': 'dup', 'url': 'https://same.com', 'content': '본문'}]}
    monkeypatch.setattr(research, 'gather', lambda q, **kw: json.loads(json.dumps(hit)))
    ev, srcs = research.build_research_evidence('질의')
    assert len(srcs) == 1, f'중복 URL 이 {len(srcs)}번 실렸다'


def test_build_research_evidence_survives_total_arachne_failure(monkeypatch):
    monkeypatch.setattr(research, 'gather', lambda q, **kw: {})
    ev, srcs = research.build_research_evidence('질의')
    assert ev == '' and srcs == []


# ── ideation.py — LLM 응답 파싱 (추론모델이 형식을 자주 어긴다) ───────────────

def test_parse_handles_clean_array():
    out = ideation.parse_json_array('[{"a":1},{"b":2}]')
    assert out == [{'a': 1}, {'b': 2}]


def test_parse_handles_code_fence_and_preamble():
    txt = '알겠습니다. 다음과 같습니다:\n```json\n[{"a":1}]\n```\n도움이 되었길!'
    assert ideation.parse_json_array(txt) == [{'a': 1}]


def test_parse_handles_bare_object_instead_of_array():
    """모델이 배열을 안 쓰고 객체 하나만 줘도 후보를 버리지 않는다."""
    assert ideation.parse_json_array('{"title":"x"}') == [{'title': 'x'}]


def test_parse_handles_truncated_array():
    """추론모델이 배열을 끝맺지 않고 잘려도, 완성된 객체는 건진다."""
    out = ideation.parse_json_array('[{"a":1},{"b":2},{"c":')
    assert out == [{'a': 1}, {'b': 2}]


def test_parse_returns_empty_on_garbage():
    assert ideation.parse_json_array('죄송합니다 만들 수 없습니다') == []
    assert ideation.parse_json_array('') == []


def test_propose_hypotheses_forces_empty_citations_without_evidence(monkeypatch):
    """근거가 없으면 인용은 반드시 빈 배열 — 없는 출처를 인용한 척하면 안 된다."""
    monkeypatch.setattr(ideation, '_llm', lambda *a, **kw: json.dumps([
        {'title': 'T', 'rationale': 'R', 'citations': [1, 2], 'family_hint': 'pv'}]))
    out = ideation.propose_hypotheses('질의', evidence='', n=2)
    assert out[0]['citations'] == []
    out2 = ideation.propose_hypotheses('질의', evidence='[출처1] ...', n=2)
    assert out2[0]['citations'] == [1, 2]


def test_propose_hypotheses_rejects_unknown_family(monkeypatch):
    monkeypatch.setattr(ideation, '_llm', lambda *a, **kw: json.dumps([
        {'title': 'T', 'rationale': 'R', 'citations': [], 'family_hint': 'crypto'}]))
    assert ideation.propose_hypotheses('q', 'ev')[0]['family_hint'] == ''


# ── strategy_spec.py — 타입드 유전체 검증 (할루시네이션 차단) ─────────────────

_GOOD = {
    'family': 'fundamental', 'fields': ['equity', 'debt', 'assets'],
    'transform_a': 'rank', 'transform_b': 'ts_zscore', 'combine': 'ratio',
    'sign': -1, 'lookback_a': 20, 'lookback_b': 60, 'universe': 'TOP1000',
    'neutralization': 'INDUSTRY', 'decay': 8, 'decay_style': 'linear',
    'truncation': 0.08, 'trade_when': 'vol_calm', 'group_op': 'neutralize',
    'group_by': 'auto', 'winsor_std': 4, 'weight_scheme': '1:1',
}


def test_valid_genome_renders_and_lints():
    built = strategy_spec.validate_and_build(_GOOD, delay=1)
    assert built is not None
    assert built['code'].startswith('trade_when(')
    assert 'winsorize(' in built['code']
    assert built['settings']['universe'] == 'TOP1000'
    # 유전체가 무손실로 살아있어야 GA 가 그대로 교차/변이할 수 있다.
    assert built['genome']['trade_when'] == 'vol_calm'


def test_hallucinated_datafield_is_rejected():
    """LLM 이 없는 필드를 지어내면 폐기한다 — 조용히 pv 로 갈아끼우면 가설이 증발한다."""
    bad = {**_GOOD, 'fields': ['equity', 'debt', 'roic_magic_score']}
    assert strategy_spec.validate_and_build(bad, delay=1) is None


def test_duplicate_fields_rejected():
    assert strategy_spec.validate_and_build(
        {**_GOOD, 'fields': ['equity', 'equity', 'debt']}, delay=1) is None


def test_delay_zero_requires_pv_fields():
    assert strategy_spec.validate_and_build(_GOOD, delay=0) is None
    pv = {**_GOOD, 'family': 'pv', 'fields': ['close', 'open', 'volume']}
    assert strategy_spec.validate_and_build(pv, delay=0) is not None


def test_garbage_genome_rejected():
    assert strategy_spec.validate_and_build({}, delay=1) is None
    assert strategy_spec.validate_and_build({'fields': 'not-a-list'}, delay=1) is None
    assert strategy_spec.validate_and_build(None, delay=1) is None


def test_concretize_retries_and_dedups(monkeypatch):
    """모델이 한 번에 1개만 줘도 재요청해 채우고, 같은 수식은 후보로 안 센다."""
    calls = []

    def _fake(messages, **kw):
        calls.append(messages)
        if len(calls) == 1:
            return json.dumps([{'why': 'first', 'delay': 1, 'genome': _GOOD}])
        alt = {**_GOOD, 'combine': 'spread', 'trade_when': 'OFF'}
        return json.dumps([
            {'why': 'dup', 'delay': 1, 'genome': _GOOD},        # 중복 → 버려짐
            {'why': 'second', 'delay': 1, 'genome': alt},
        ])
    monkeypatch.setattr(strategy_spec, '_llm', _fake)
    out = strategy_spec.concretize({'title': 'H', 'rationale': 'R'}, '', k=2)
    assert len(out) == 2
    assert len(calls) == 2, '부족한데 재요청을 안 했다'
    assert out[0]['code'] != out[1]['code']


def test_concretize_fails_open_when_llm_dead(monkeypatch):
    monkeypatch.setattr(strategy_spec, '_llm', lambda *a, **kw: '')
    assert strategy_spec.concretize({'title': 'H'}, '', k=2) == []


# ── DB + GA 시딩 ─────────────────────────────────────────────────────────────

def _seed_spec(uid, genome=None, delay=1):
    run_id = db.create_research_run(uid, '질의')
    hid = db.insert_hypothesis(run_id, uid, {'title': 'H', 'citations': [1]})
    g = genome or _GOOD
    built = strategy_spec.validate_and_build(g, delay=delay)
    return db.insert_spec(hid, uid, genome=built['genome'], code=built['code'],
                          settings=built['settings'], delay=delay, why='w'), run_id


def test_pending_specs_roundtrip(isolated_db):
    uid = isolated_db
    sid, _ = _seed_spec(uid)
    pend = db.pending_specs(uid)
    assert [p['id'] for p in pend] == [sid]
    assert pend[0]['genome']['trade_when'] == 'vol_calm'
    db.mark_specs([sid], 'seeded', seeded_round=3)
    assert db.pending_specs(uid) == []
    assert db.spec_counts(uid) == {'seeded': 1}


def test_specs_enter_ga_population_unmutated(isolated_db):
    """스펙은 **변이 없이** 그대로 시뮬돼야 한다 — 원본 성적을 먼저 재야 하니까."""
    uid = isolated_db
    sid, _ = _seed_spec(uid)
    spec = db.pending_specs(uid)[0]
    pop = gm.generate_population(
        account_type='research_consultant', round_num=1, forced_delay='1', n=8,
        spec_genomes=[spec['genome']], spec_ids=[spec['id']])
    specs = [p for p in pop if p['origin'] == 'spec']
    assert len(specs) == 1
    assert specs[0]['spec_id'] == sid
    assert specs[0]['code'] == spec['code'], '스펙 코드가 변형됐다'
    assert specs[0]['genome']['trade_when'] == 'vol_calm'
    # 나머지 슬롯은 평소 GA 로 채워진다 — 예산을 놀리지 않는다.
    assert len(pop) == 8
    assert {p['origin'] for p in pop} >= {'spec', 'random'}


def test_no_specs_means_untouched_random_ga():
    """요청을 안 넣으면 오늘과 완전히 같은 개체군이 나온다 (회귀 방지)."""
    kw = dict(account_type='research_consultant', round_num=5, forced_delay='1', n=8)
    before = gm.generate_population(**kw)
    after = gm.generate_population(**kw, spec_genomes=None, spec_ids=None)
    assert [p['code'] for p in before] == [p['code'] for p in after]
    assert all(p['origin'] != 'spec' for p in after)


def test_spec_alpha_link_and_insert(isolated_db):
    uid = isolated_db
    sid, _ = _seed_spec(uid)
    rid = db.start_round(uid, 1)
    aid = db.insert_alpha(uid, rid, 1, {
        'idx': 1, 'code': 'rank(close)', 'pass_count': 6, 'metrics': {'sharpe': '1.1'},
        'settings': {'universe': 'TOP1000'}, 'delay': '1',
        'origin': 'spec', 'spec_id': sid,
    })
    db.attach_spec_alpha(sid, aid)
    row = db.list_recent_alphas(uid, limit=1)[0]
    assert row['spec_id'] == sid
    assert row['origin'] == 'spec'
    specs = db.list_specs_for_run(_seed_spec(uid)[1] - 1) or []
    assert any(s['alpha_id'] == aid for s in db.list_specs_for_run(1))
