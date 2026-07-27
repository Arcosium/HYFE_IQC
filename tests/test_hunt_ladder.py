# tests/test_hunt_ladder.py
# 사냥 사다리(2026-07-27 GLB 판단과정 이식): 부호반전·사후감쇠·RAM중립화 처방.
from server import alpha_ast, hunt_ladder


def test_sign_flip_when_strong_negative():
    m = {'sharpe': '-0.98', 'turnover': '1.18', 'fitness': '-0.13'}
    out = hunt_ladder.remedies('ts_rank(clv, 5)', m, {}, ['LOW_SHARPE', 'LOW_FITNESS'], n=1)
    assert out and '부호 반전' in out[0]['desc']
    assert out[0]['code'].startswith('-1 * (')          # 반전 적용
    assert alpha_ast.parse(out[0]['code'])


def test_decay_linear_is_offered_for_high_turnover():
    m = {'sharpe': '1.01', 'turnover': '1.18', 'fitness': '0.14'}
    out = hunt_ladder.remedies('-ts_rank(clv, 5)', m, {}, ['HIGH_TURNOVER', 'LOW_FITNESS'],
                               n=4)
    codes = [o['code'] for o in out]
    # 그날의 승자 처방: ts_decay_linear(...,20) 이 반드시 포함돼야 한다
    assert any(c.startswith('ts_decay_linear(-ts_rank(clv, 5), 20)') for c in codes)
    assert any('hump(' in c for c in codes)
    for o in out:
        assert alpha_ast.parse(o['code'])
        assert o['origin'] == 'hunt'


def test_window_is_never_rescaled():
    """창 확대는 신호를 죽인다 — 사다리는 창을 건드리지 않는다."""
    out = hunt_ladder.remedies('-ts_rank(clv, 5)',
                               {'sharpe': '1.01', 'turnover': '1.2'}, {},
                               ['HIGH_TURNOVER'], n=4)
    for o in out:
        assert 'ts_rank(clv, 5)' in o['code']            # 원래 창 5 그대로


def test_ram_neutralization_variant():
    out = hunt_ladder.remedies('-ts_rank(clv, 5)',
                               {'sharpe': '1.05', 'turnover': '0.9'},
                               {'neutralization': 'FAST'}, ['HIGH_TURNOVER'],
                               n=6, ht_ram_warning=True)
    assert any(o['settings'].get('neutralization') == 'REVERSION_AND_MOMENTUM'
               for o in out)


def test_weak_or_structural_failures_get_nothing():
    # 신호가 약하면 처방 안 함
    assert hunt_ladder.remedies('rank(x)', {'sharpe': '0.2', 'turnover': '1.0'}, {},
                                ['LOW_SHARPE'], n=3) == []
    # 상관·서브유니버스 같은 구조적 실패는 사다리 대상이 아니다
    d = hunt_ladder.diagnose({'sharpe': '1.2', 'turnover': '0.5'},
                             ['PROD_CORRELATION'])
    assert d['remediable'] is False


def test_double_flip_is_not_stacked():
    out = hunt_ladder.remedies('-1 * (ts_rank(clv, 5))',
                               {'sharpe': '-1.1', 'turnover': '0.5'}, {},
                               ['LOW_SHARPE'], n=1)
    assert out and not out[0]['code'].startswith('-1 * (-1')


# ── 상관 방어 (2026-07-27 사장 지적) ─────────────────────────────────────────

def test_ladder_skips_correlation_rejected_fieldset(tmp_path, monkeypatch):
    """상관 거절을 **실제로 맞은** 필드셋만 처방에서 뺀다 (2026-07-27 사장 결정).
    단순 제출 이력만으로는 빼지 않는다 — 거절은 예산을 안 쓰므로 형제도 시도 가치가 있다."""
    from server import db
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'hl.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    rid = db.start_round(uid, 1)

    def mk(idx, code, sharpe, submitted, fields):
        return {'idx': idx, 'code': code, 'desc': code, 'pass_count': 1,
                'pass_items': [], 'fail_count': 1,
                'fail_items': [{'name': 'HIGH_TURNOVER'}], 'error_count': 0,
                'pending_count': 0, 'submitted': submitted, 'submit_status': '',
                'error_text': '',
                'metrics': {'sharpe': str(sharpe), 'turnover': '1.2'},
                'self_corr': None, 'settings': {}, 'delay': '1', 'is_status': {},
                'mode': '', 'cached': False, 'phase': 0, 'generation': 0,
                'genome': {'model': 'm', 'family': 'pv', 'fields': fields}}

    # 상관 거절을 맞은 신호(clv) + 신규 신호(other)
    _rej = mk(0, 'ts_rank(clv_x, 5)', 1.1, False, ['clv_x'])
    _rej['submit_status'] = 'rejected:SELF_CORRELATION (http_403)'
    db.insert_alpha(uid, rid, 1, _rej)
    db.insert_alpha(uid, rid, 1, mk(1, '-ts_rank(clv_x, 5)', 1.2, False, ['clv_x']))
    db.insert_alpha(uid, rid, 1, mk(2, '-ts_zscore(other_y, 5)', 1.0, False, ['other_y']))

    pool = db.hunt_ladder_pool(uid)
    codes = [r['code'] for r in pool]
    assert '-ts_zscore(other_y, 5)' in codes          # 새 신호는 처방 대상
    assert all('clv_x' not in c for c in codes)       # 상관 거절 필드셋은 제외
    db._INITIALIZED = False


def test_pp_selfcorr_gate_off_by_default_but_configurable(tmp_path, monkeypatch):
    """기본은 **예방적 차단 없음**(일단 시도) — 켜면 상한대로 막는다 (사장 결정)."""
    from server import db, worker as w
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'pp.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    monkeypatch.setattr(w.run_config, 'get_submit_hold_until', lambda: 0.0)
    wk = w.Worker.__new__(w.Worker)
    wk.user_id = uid
    wk._corr_fs_hold = set()
    m = {'sharpe': '1.2', 'fitness': '0.3', 'themes': 'GLB High Turnover Theme'}
    # 기본값(0=끔): 상관이 높아도 예방적으로 막지 않는다 — 거절은 예산 미소모
    assert w.PP_SELFCORR_MAX == 0
    _, reason0 = wk._submit_gate(m, 0.62, fail_items=[])
    assert not reason0.startswith('pp_selfcorr')
    # 켜면 상한대로 차단
    monkeypatch.setattr(w, 'PP_SELFCORR_MAX', 0.5)
    ok, reason = wk._submit_gate(m, 0.62, fail_items=[])
    assert ok is False and reason.startswith('pp_selfcorr')
    # 상관이 낮으면 이 게이트는 막지 않는다 (다른 게이트 판단은 이 테스트 밖)
    _, reason2 = wk._submit_gate(m, 0.31, fail_items=[])
    assert not reason2.startswith('pp_selfcorr')
    # 테마 미매칭 알파에는 PP 컷을 적용하지 않는다 (일반 0.7 컷은 별도 경로)
    _, reason3 = wk._submit_gate({'sharpe': '1.2'}, 0.62, fail_items=[])
    assert not reason3.startswith('pp_selfcorr')
    db._INITIALIZED = False
