# tests/test_pyramids.py
# 피라미드 칸(REGION/DELAY/CATEGORY)은 3건 이상이라야 하나가 달성된다.
# 2026-08-13 실측 분포: PV 16 · Risk 16 · Model 10 — 55건 중 33건이 이미 달성한
# 칸에 덧쌓였고, 그 대가로 PROD_CORRELATION 이 0.9대로 올라 계보가 막혔다.
import json
import time as _t

import pytest

from server import db, pyramids


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', str(tmp_path / 'pyr.db'))
    db._INITIALIZED = False
    db.init()
    uid = db.upsert_user('u', 'p', 'GEMINI_FAKE_KEY_FOR_TEST')
    pyramids._MAP_CACHE.clear()
    pyramids._MAP_TS = 0.0
    yield uid
    db._INITIALIZED = False
    pyramids._MAP_CACHE.clear()
    pyramids._MAP_TS = 0.0


_SEQ = [0]


def _submit(uid, pyr, ts=None):
    """제출(OS)된 알파 1행 — 피라미드 칸만 다르게."""
    _SEQ[0] += 1
    rid = db.start_round(uid, _SEQ[0])
    pk = db.insert_alpha(uid, rid, _SEQ[0], {
        'code': f'rank(f{_SEQ[0]})', 'idx': _SEQ[0],
        'metrics': {'pyramids': pyr}})
    conn = db._connect()
    conn.execute('UPDATE alphas SET submitted=1, ts=? WHERE id=?',
                 (ts if ts is not None else _t.time(), pk))
    conn.commit(); conn.close()


def test_counts_use_full_cell_name(isolated_db):
    """GLB/D1/RISK 와 USA/D1/RISK 는 다른 칸이다 — 카테고리만 세면 안 된다."""
    uid = isolated_db
    _submit(uid, 'GLB/D1/RISK'); _submit(uid, 'GLB/D1/RISK'); _submit(uid, 'USA/D1/RISK')
    c = pyramids.counts(uid)
    assert c['GLB/D1/RISK'] == 2 and c['USA/D1/RISK'] == 1


def test_shortfall_drops_filled_cells(isolated_db):
    uid = isolated_db
    for _ in range(3):
        _submit(uid, 'GLB/D1/RISK')
    _submit(uid, 'GLB/D1/OPTION')
    short = pyramids.shortfall(uid, 'GLB', '1')
    assert 'RISK' not in short              # 3건 = 달성 → 더 쌓을 이유 없음
    assert short['OPTION'] == 2             # 2발 남음
    assert short['FUNDAMENTAL'] == 3        # 미개척


def test_previous_quarter_does_not_count(isolated_db):
    """피라미드는 분기마다 리셋된다 — 지난 분기 제출을 세면 칸이 찬 줄 안다."""
    uid = isolated_db
    for _ in range(3):
        _submit(uid, 'GLB/D1/RISK', ts=pyramids.quarter_start() - 86400)
    assert pyramids.counts(uid)['GLB/D1/RISK'] == 0
    assert pyramids.shortfall(uid, 'GLB', '1')['RISK'] == 3


def test_category_map_learned_from_wqb_answers(isolated_db, monkeypatch):
    """사상은 손으로 적지 않는다 — 단일 데이터셋 알파의 (코드, pyramids) 쌍에서 배운다."""
    monkeypatch.setattr(db, 'code_pyramid_pairs',
                        lambda: [('rank(fnd23_x)', 'GLB/D1/FUNDAMENTAL')] * 3)
    monkeypatch.setattr('server.datafield_palette.field_dataset_map',
                        lambda: {'fnd23_x': 'fundamental23'})
    assert pyramids.dataset_category('fundamental23') == 'FUNDAMENTAL'
    # 배운 적 없는 데이터셋은 접두 폴백
    assert pyramids.dataset_category('analyst99') == 'ANALYST'
    assert pyramids.dataset_category('zzz_unknown') == ''


def test_mixed_dataset_samples_are_ignored(isolated_db, monkeypatch):
    """데이터셋이 섞인 코드는 어느 쪽 공로인지 모른다 — 학습에서 뺀다."""
    monkeypatch.setattr(db, 'code_pyramid_pairs',
                        lambda: [('rank(fnd23_x + rsk_y)', 'GLB/D1/RISK')] * 5)
    monkeypatch.setattr('server.datafield_palette.field_dataset_map',
                        lambda: {'fnd23_x': 'fundamental23', 'rsk_y': 'risk70'})
    assert pyramids.dataset_category('fundamental23') == 'FUNDAMENTAL'   # 폴백만
