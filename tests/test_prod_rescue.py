"""prod 상관 완화 레인 — 문턱 근접만 중립화 스윕하고 0.8 이상은 손대지 않는다.

⚠ 스펙 경로는 `code` 컬럼이 아니라 **유전체를 다시 render** 해서 시뮬한다. 그래서 변주는
   반드시 유전체로 표현돼야 한다 — raw code 를 넣으면 기본 유전체 하나로 뭉개진다
   (2026-08-14 라이브 실측: 스펙 13개 → 후보 1개).
"""
from server import genome_models as gm
from server import submit_push as sp


def test_prod_corr_of_reads_both_formats():
    assert sp.prod_corr_of('rejected:LOW_FITNESS(0.94 vs 1.0); '
                           'PROD_CORRELATION(0.7358 vs 0.7) (http_403)') == 0.7358
    assert sp.prod_corr_of('submit_skipped:prod_corr(0.8867>0.7)') == 0.8867
    assert sp.prod_corr_of('rejected:LOW_SHARPE(1.2 vs 1.58)') is None
    assert sp.prod_corr_of(None) is None


def test_json_genome_tolerates_junk():
    assert sp._json_genome('{"a": 1}') == {'a': 1}
    assert sp._json_genome({'a': 1}) == {'a': 1}
    assert sp._json_genome('') is None
    assert sp._json_genome('not json') is None
    assert sp._json_genome('[1,2]') is None


def _genome_dict(neut='SUBINDUSTRY'):
    g = gm.BaseGenomeModel(round_num=1)._genome(1, __import__('random').Random(0))
    return dict({**g.__dict__, 'neutralization': neut})


def _row(status, neut='SUBINDUSTRY', genome=None):
    import json
    gd = _genome_dict(neut) if genome is None else genome
    return {'code': f'rank(x_{neut})', 'sharpe': 1.8, 'universe': 'TOPDIV3000', 'delay': 1,
            'neutralization': neut, 'decay': 6, 'truncation': 0.1, 'status': status,
            'ts': 0.0, 'genome': json.dumps(gd) if gd is not None else ''}


def _wire(monkeypatch, rows, *, pending=(), last=None, tried=()):
    seen = {'specs': []}
    monkeypatch.setattr(sp._db, 'pending_specs', lambda uid, limit=8: list(pending))
    monkeypatch.setattr(sp._db, 'last_hypothesis_ts', lambda uid, t: last)
    monkeypatch.setattr(sp._db, 'prod_corr_rejected', lambda uid, since: rows)
    monkeypatch.setattr(sp._db, 'get_alpha_by_code',
                        lambda uid, code: {'id': 1} if code in tried else None)
    monkeypatch.setattr(sp._db, 'latest_run_id', lambda uid: 1)
    monkeypatch.setattr(sp._db, 'insert_hypothesis', lambda rid, uid, h: 7)
    monkeypatch.setattr(sp._db, 'insert_spec',
                        lambda hid, uid, **kw: seen['specs'].append(kw) or 1)
    return seen


def test_rescues_only_near_threshold(monkeypatch):
    rows = [_row('rejected:PROD_CORRELATION(0.9621 vs 0.7)', 'INDUSTRY'),   # 가망 없음
            _row('rejected:PROD_CORRELATION(0.7358 vs 0.7)', 'CROWDING'),   # 구제 대상
            _row('rejected:PROD_CORRELATION(0.6100 vs 0.7)', 'SECTOR')]     # 이미 통과분
    seen = _wire(monkeypatch, rows)
    n = sp.maybe_rescue(2, now=1_000_000.0)
    assert n > 0
    neuts = {s['settings']['neutralization'] for s in seen['specs']}
    assert 'CROWDING' not in neuts          # 원본과 같은 중립화는 변주가 아니다
    assert neuts <= set(sp.RESCUE_NEUTS)


def test_specs_carry_a_real_genome_not_a_stub(monkeypatch):
    """스펙이 render 가능한 완전 유전체를 실어야 한다 — 스텁이면 전부 한 후보로 뭉개진다."""
    seen = _wire(monkeypatch, [_row('rejected:PROD_CORRELATION(0.7358 vs 0.7)')])
    sp.maybe_rescue(2, now=1_000_000.0)
    assert seen['specs']
    codes = set()
    for s in seen['specs']:
        g = gm._coerce_genome(s['genome'])
        assert g is not None, '유전체가 render 불가'
        assert s['code'] == gm.render(g)
        codes.add(gm.BaseGenomeModel._dedup_key(g))
    assert len(codes) == len(seen['specs']), 'dedup 키가 겹쳐 같은 후보로 뭉개진다'


def test_alpha_without_genome_is_skipped(monkeypatch):
    rows = [_row('rejected:PROD_CORRELATION(0.7358 vs 0.7)', genome=None)]
    rows[0]['genome'] = ''
    msgs = []
    _wire(monkeypatch, rows)
    assert sp.maybe_rescue(2, log_fn=msgs.append, now=1_000_000.0) == 0
    assert msgs and '유전체' in msgs[0]


def test_already_simulated_variants_are_not_reloaded(monkeypatch):
    """부모는 거절 상태로 계속 남는다 — 이미 돌린 변주를 걸러야 웨이브마다 재탕하지 않는다."""
    rows = [_row('rejected:PROD_CORRELATION(0.7358 vs 0.7)')]
    seen = _wire(monkeypatch, rows)
    sp.maybe_rescue(2, now=1_000_000.0)
    first = [s['code'] for s in seen['specs']]
    assert first
    seen2 = _wire(monkeypatch, rows, tried=set(first))
    assert sp.maybe_rescue(2, now=1_000_000.0) == 0
    assert seen2['specs'] == []


def test_exhausted_parent_yields_slot_to_the_next_one(monkeypatch):
    """앞 부모가 다 소진돼도 뒷 부모까지 내려가야 한다 — 아니면 레인이 조용히 굶는다."""
    rows = [_row('rejected:PROD_CORRELATION(0.7358 vs 0.7)', 'SUBINDUSTRY'),
            _row('rejected:PROD_CORRELATION(0.7412 vs 0.7)', 'MARKET')]
    rows[1]['code'] = 'rank(x_other)'          # 부모 dedup 은 code 로 한다
    seen = _wire(monkeypatch, rows)
    sp.maybe_rescue(2, now=1_000_000.0)
    spent = {s['code'] for s in seen['specs'] if '0.7358' in s['why']}
    assert spent
    seen2 = _wire(monkeypatch, rows, tried=spent)
    assert sp.maybe_rescue(2, now=1_000_000.0) > 0
    assert all('0.7412' in s['why'] for s in seen2['specs'])


def test_cooldown_and_pending_block(monkeypatch):
    rows = [_row('rejected:PROD_CORRELATION(0.7358 vs 0.7)')]
    _wire(monkeypatch, rows, pending=[{'id': 1}])
    assert sp.maybe_rescue(2, now=1_000_000.0) == 0
    _wire(monkeypatch, rows, last=1_000_000.0 - 10)
    assert sp.maybe_rescue(2, now=1_000_000.0) == 0
