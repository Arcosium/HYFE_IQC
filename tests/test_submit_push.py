# tests/test_submit_push.py
# 자동 제출 푸시 — "급하게 말 안 해도 알아서"(2026-08-02 사장 지시).
# 집중 창(13:00 경계 직전 3h = 10:00~) 안에서 제출 미달 + 탄약 없음 + 쿨다운
# 경과 → 검증 골격 × 신선 축 ±양부호 장전. 축 소진(제출된 코드에 등장)·
# 사망(골격 시도 최고 S<1)은 자동 제외, 마르면 카탈로그에서 발굴.
import time

from server import submit_push as sp


SEASON = 'rsk70_mfm2_gemtrd_season'
STREV = 'rsk70_mfm2_gemtrd_strevrsl'
NEUT = sp.NEUT_FIELD


def _at(hour, minute=0):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))


IN_WINDOW = _at(11)      # 10:00~13:00 창 안
OUT_WINDOW = _at(15)     # 창 밖


def _skeleton_code(field):
    return (f'rank(winsorize(vector_neut(ts_zscore({field},20),'
            f'ts_zscore({NEUT},20)),std=4))')


def _wire(monkeypatch, *, submitted=0, pending=False, last_ts=None, rows=(),
          short=('RISK', 'FUNDAMENTAL')):
    # 피라미드 미달 칸은 DB 실측이라 테스트에선 고정한다. 기본값에 RISK 를 두어
    # 큐레이션 축(전부 risk70)이 살아 있는 상태를 재현한다.
    monkeypatch.setattr(sp, '_short_cats', lambda uid: {c: 3 for c in short})
    # 발굴 축의 짝은 카탈로그 조회다 — 가짜 필드명엔 짝이 없으니 테스트에선 고정한다.
    monkeypatch.setattr(sp, '_same_dataset_partners',
                        lambda field, rows: ('nfd_neutx', 'nfd_thirdy'))
    monkeypatch.setattr(sp._db, 'submitted_count_since', lambda uid, ts: submitted)
    monkeypatch.setattr(sp._db, 'pending_specs',
                        lambda uid, limit=8: [{'id': 1}] if pending else [])
    monkeypatch.setattr(sp._db, 'last_hypothesis_ts', lambda uid, pfx: last_ts)
    monkeypatch.setattr(sp._db, 'code_sharpe_submitted_since', lambda uid, ts: list(rows))
    # 상관 완화 레인은 maybe_seed 앞단에 붙어 있다 — 비워 두지 않으면 라이브 DB 를
    # 읽어 무관한 테스트가 그날의 거절 이력에 따라 깨진다.
    monkeypatch.setattr(sp._db, 'prod_corr_rejected', lambda uid, since: [])
    monkeypatch.setattr(sp._db, 'latest_run_id', lambda uid: 21)
    inserted = []
    monkeypatch.setattr(sp._db, 'insert_hypothesis', lambda rid, uid, h: 999)
    monkeypatch.setattr(sp._db, 'insert_spec',
                        lambda hid, uid, **kw: inserted.append(kw) or len(inserted))
    return inserted


def test_outside_window_seeds_small_wave_from_other_datasets(monkeypatch):
    """창 밖 상시 다변화 — 카탈로그 발굴 축(=다른 데이터셋)만, 소형으로."""
    inserted = _wire(monkeypatch)
    monkeypatch.setattr(sp, '_discover_axes',
                        lambda exclude, n, uid=None: ['fnd6_newfieldx', 'mdl99_newfieldy'][:n])
    monkeypatch.setattr('server.alpha_lint.validate_alpha', lambda code: [])
    n = sp.maybe_seed(2, now=OUT_WINDOW)
    assert n == len(inserted) == 2 * sp.OFF_WINDOW_AXES < 2 * sp.AXES_PER_WAVE
    codes = ' '.join(s['code'] for s in inserted)
    assert 'fnd6_newfieldx' in codes
    assert not any(a in codes.replace(NEUT, '') for a in sp.AXES)  # rsk70 축 재탕 금지


def test_outside_window_already_tried_field_not_refired(monkeypatch):
    """발굴 축엔 소진/사망 테이블이 없다 — 최근 코드에 있으면 다시 쏘지 않는다."""
    inserted = _wire(monkeypatch, rows=[(_skeleton_code('fnd6_newfieldx'), 0.3, 0)])
    monkeypatch.setattr(sp, '_discover_axes',
                        lambda exclude, n, uid=None: ['fnd6_newfieldx', 'mdl99_newfieldy'][:n])
    monkeypatch.setattr('server.alpha_lint.validate_alpha', lambda code: [])
    sp.maybe_seed(2, now=OUT_WINDOW)
    codes = ' '.join(s['code'] for s in inserted)
    assert 'fnd6_newfieldx' not in codes and 'mdl99_newfieldy' in codes


def test_outside_window_no_fresh_dataset_is_silent(monkeypatch):
    """창 밖엔 사람을 부르지 않는다 — 45분마다 '고갈' 경보가 울리면 안 된다."""
    _wire(monkeypatch)
    monkeypatch.setattr(sp, '_discover_axes', lambda exclude, n, uid=None: [])
    msgs = []
    assert sp.maybe_seed(2, log_fn=msgs.append, now=OUT_WINDOW) == 0
    assert not msgs


def test_outside_window_cooldown_is_longer(monkeypatch):
    """창 밖은 라운드 2~3회에 1번만 — 매 라운드 장전하면 focus(연마)가 굶는다."""
    _wire(monkeypatch, last_ts=OUT_WINDOW - sp.COOLDOWN_S - 60)
    monkeypatch.setattr(sp, '_discover_axes', lambda exclude, n, uid=None: ['fnd6_newfieldx'][:n])
    assert sp.maybe_seed(2, now=OUT_WINDOW) == 0


def test_target_met_does_nothing(monkeypatch):
    _wire(monkeypatch, submitted=sp.DAILY_SUBMIT_TARGET)
    assert sp.maybe_seed(2, now=IN_WINDOW) == 0


def test_pending_ammo_blocks_double_load(monkeypatch):
    _wire(monkeypatch, pending=True)
    assert sp.maybe_seed(2, now=IN_WINDOW) == 0


def test_seeds_fresh_axes_both_signs(monkeypatch):
    inserted = _wire(monkeypatch, rows=[
        (_skeleton_code(SEASON), 1.06, 1),      # 제출됨 → 소진
        (_skeleton_code(STREV), 0.4, 0),        # 시도·S<1 → 사망
    ])
    n = sp.maybe_seed(2, now=IN_WINDOW)
    assert n == len(inserted) == 2 * sp.AXES_PER_WAVE
    codes = ' '.join(s['code'] for s in inserted)
    assert SEASON not in codes and STREV not in codes
    signs = {s['why'][-2:] for s in inserted}
    assert signs == {'+1', '-1'}                # 축당 양부호


def test_filled_risk_cell_retires_curated_axes(monkeypatch):
    """큐레이션 축은 전부 risk70 이다 — RISK 칸이 차면 더 쏠 이유가 없다.

    2026-08-13 실측: 그 칸에 16건을 쌓는 동안 피라미드는 하나도 안 늘고
    PROD_CORRELATION 만 0.9대로 올라 계보 전체가 제출 불능이 됐다.
    """
    inserted = _wire(monkeypatch, short=('FUNDAMENTAL',))     # RISK 는 이미 달성
    monkeypatch.setattr(sp, '_discover_axes',
                        lambda exclude, n, uid=None: ['fnd6_newfieldx'][:n])
    monkeypatch.setattr('server.alpha_lint.validate_alpha', lambda code: [])
    sp.maybe_seed(2, now=IN_WINDOW)
    codes = ' '.join(s['code'] for s in inserted)
    assert 'fnd6_newfieldx' in codes
    assert not any(a in codes.replace(NEUT, '') for a in sp.AXES)


def test_axis_without_same_dataset_partner_is_skipped(monkeypatch):
    """짝을 못 찾으면 안 쏜다 — rsk70 을 섞으면 WQB 가 risk 칸으로 셈해 다변화가 무효."""
    inserted = _wire(monkeypatch, short=('FUNDAMENTAL',))
    monkeypatch.setattr(sp, '_same_dataset_partners', lambda field, rows: None)
    monkeypatch.setattr(sp, '_discover_axes',
                        lambda exclude, n, uid=None: ['fnd6_newfieldx'][:n])
    monkeypatch.setattr('server.alpha_lint.validate_alpha', lambda code: [])
    msgs = []
    assert sp.maybe_seed(2, log_fn=msgs.append, now=IN_WINDOW) == 0
    assert inserted == []
    assert any('건너뜀' in m for m in msgs)


def test_cooldown_blocks(monkeypatch):
    _wire(monkeypatch, last_ts=IN_WINDOW - 60)
    assert sp.maybe_seed(2, now=IN_WINDOW) == 0


def test_exhausted_curated_axes_discovers_from_catalog(monkeypatch):
    """큐레이션 축 전멸 → 사람 호출이 아니라 카탈로그 발굴로 이어져야 한다."""
    rows = [(_skeleton_code(f), 0.1, 0) for f in sp.AXES]
    inserted = _wire(monkeypatch, rows=rows)
    monkeypatch.setattr(sp, '_discover_axes',
                        lambda exclude, n, uid=None: ['fnd6_newfieldx', 'mdl99_newfieldy'][:n])
    # 가짜 필드명은 실제 lint 를 통과 못 하므로 우회 — 프로덕션에선 카탈로그 출신이라 유효
    monkeypatch.setattr('server.alpha_lint.validate_alpha', lambda code: [])
    msgs = []
    n = sp.maybe_seed(2, log_fn=msgs.append, now=IN_WINDOW)
    assert n == len(inserted) > 0
    assert any('발굴' in m for m in msgs)
    assert 'fnd6_newfieldx' in ' '.join(s['code'] for s in inserted)


def test_catalog_also_empty_calls_human(monkeypatch):
    rows = [(_skeleton_code(f), 0.1, 0) for f in sp.AXES]
    _wire(monkeypatch, rows=rows)
    monkeypatch.setattr(sp, '_discover_axes', lambda exclude, n, uid=None: [])
    msgs = []
    assert sp.maybe_seed(2, log_fn=msgs.append, now=IN_WINDOW) == 0
    assert any('고갈' in m for m in msgs)
