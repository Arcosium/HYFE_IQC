# tests/test_wqb_data_service.py
import csv, os
import server.wqb_data_service as ds

def test_map_datafields():
    api = [{'id': 'close', 'description': 'Close', 'type': 'MATRIX', 'coverage': 1.0, 'alphaCount': 12,
            'dataset': {'id': 'pv1'}}]
    rows = ds.map_datafields(api, region='USA', universe='TOP3000', delay=1)
    r = rows[0]
    assert r['name'] == 'close' and r['region'] == 'USA' and r['delay'] == '1'
    assert r['alphas'] == '12' and int(r['coverage']) == 100 and r['category']

def test_write_live_csv_atomic_and_header(tmp_path):
    rows = ds.map_datafields([{'id': 'x', 'description': 'd', 'type': 'MATRIX',
                               'coverage': 0.5, 'alphaCount': 3, 'dataset': {'id': 'pv'}}],
                             region='USA', universe='TOP3000', delay=1)
    p = str(tmp_path / 'live.csv')
    ds.write_live_csv(rows, p)
    with open(p, newline='') as fh:
        rd = list(csv.DictReader(fh))
    assert rd[0]['name'] == 'x'
    assert list(rd[0].keys()) == ds.CSV_COLUMNS


def test_refresh_never_raises_on_house_client_exception(monkeypatch):
    """_house_client() 이 예외를 던져도 refresh()는 절대 raise하지 않고 False를 반환해야 한다."""
    def boom():
        raise RuntimeError('DB locked')
    monkeypatch.setattr(ds, '_house_client', boom)
    result = ds.refresh()
    assert result is False


# ── 알파벳 10000 캡 우회 (2026-07-27) ────────────────────────────────────────
# /data-fields 전체 조회는 count 를 10000 으로 캡하고 id 알파벳순으로만 준다.
# 그래서 GLB 29343개 중 'f' 이후 19343개가 통째로 안 보였다(mdl110_*·rsk70_* 포함).
# dataset.id 로 쪼개면 캡에 안 걸린다 — 그게 실제로 되는지 검증한다.

class _FakeDatasetApi:
    """dataset.id 필터가 있을 때만 결과를 주는 가짜 API (실 API 의 캡을 흉내)."""
    CAP = 10

    def __init__(self, datasets):
        self.datasets = datasets            # {ds_id: [field_id, ...]}
        self.calls = []

    def _page(self, items, offset, limit):
        return {'count': len(items), 'results': items[offset:offset + limit]}

    def get(self, url, params=None, **kw):
        params = params or {}
        self.calls.append(params)
        off, lim = int(params.get('offset', 0)), int(params.get('limit', 50))
        if url.endswith('/data-sets'):
            rows = [{'id': k, 'fieldCount': len(v)} for k, v in sorted(self.datasets.items())]
            body = self._page(rows, off, lim)
        else:
            ds_id = params.get('dataset.id')
            if ds_id is None:                       # 전체 조회 = 캡 걸린 알파벳 앞부분만
                allf = sorted(f for v in self.datasets.values() for f in v)[:self.CAP]
                body = self._page([{'id': f, 'dataset': {'id': '?'}} for f in allf], off, lim)
            else:
                body = self._page([{'id': f, 'dataset': {'id': ds_id}}
                                   for f in self.datasets.get(ds_id, [])], off, lim)
        return type('R', (), {'ok': True, 'status_code': 200, 'headers': {},
                              'json': lambda self, b=body: b})()


def _fake_client(api):
    return type('C', (), {'session': api})()


def test_collect_grid_beats_the_alphabet_cap(monkeypatch):
    monkeypatch.setattr(ds, '_PAGE_SLEEP_S', 0)
    # 캡(10) 을 넘는 25개 필드를 3개 데이터셋에 나눠 둔다. 'z'로 시작하는 것은
    # 알파벳 캡 뒤에 있어 전체 조회로는 절대 안 나온다.
    api = _FakeDatasetApi({
        'aaa': [f'a_{i:02d}' for i in range(12)],
        'mmm': [f'mdl110_{i:02d}' for i in range(8)],
        'zzz': [f'z_{i:02d}' for i in range(5)],
    })
    rows, expect = ds._collect_grid(_fake_client(api), 'GLB', 'TOPDIV3000', 1)
    names = {r['name'] for r in rows}
    assert expect == 25 and len(rows) == 25
    assert any(n.startswith('mdl110_') for n in names)   # 캡 구간
    assert any(n.startswith('z_') for n in names)        # 캡 뒤 구간
    assert all(r['region'] == 'GLB' for r in rows)


def test_refresh_keeps_old_palette_when_a_grid_comes_up_short(monkeypatch, tmp_path):
    """그리드 하나가 결손이면 CSV 를 덮어쓰지 않는다 — 조용한 팔레트 퇴화 방지."""
    monkeypatch.setattr(ds, '_PAGE_SLEEP_S', 0)
    monkeypatch.setattr(ds, '_house_client',
                        lambda: type('C', (), {'session': None,
                                               'authenticate': lambda self: True})())
    monkeypatch.setattr(ds, 'refresh_operators', lambda c: True)
    monkeypatch.setattr(ds, '_collect_grid', lambda *a: ([{'name': 'x'}] * 10, 1000))
    wrote = []
    monkeypatch.setattr(ds, 'write_live_csv', lambda rows, *a: wrote.append(rows))
    assert ds.refresh(grid=(('GLB', 'TOPDIV3000', 1),)) is False
    assert wrote == []


def test_one_bad_grid_does_not_discard_a_good_one(monkeypatch):
    """실측 사고(2026-07-27): GLB 를 20분 걸려 다 받아 놓고 뒤이은 USA/D1 이 429 로
    무너지자 GLB 까지 함께 폐기됐다. 성공 그리드는 반영, 결손 그리드는 옛 행 유지."""
    monkeypatch.setattr(ds, '_PAGE_SLEEP_S', 0)
    monkeypatch.setattr(ds, '_house_client',
                        lambda: type('C', (), {'session': None,
                                               'authenticate': lambda self: True})())
    monkeypatch.setattr(ds, 'refresh_operators', lambda c: True)
    # 기존 CSV 에는 두 그리드가 이미 있다
    monkeypatch.setattr(ds, '_load_live_rows', lambda: [
        {'name': 'old_glb', 'region': 'GLB', 'universe': 'TOPDIV3000', 'delay': '1'},
        {'name': 'old_usa', 'region': 'USA', 'universe': 'TOP3000', 'delay': '1'}])

    def collect(c, region, universe, delay):
        if region == 'GLB':
            return ([{'name': 'new_glb', 'region': 'GLB', 'universe': 'TOPDIV3000',
                      'delay': '1'}], 1)
        return ([], -1)                      # USA 는 데이터셋 목록부터 실패

    monkeypatch.setattr(ds, '_collect_grid', collect)
    wrote = []
    monkeypatch.setattr(ds, 'write_live_csv', lambda rows, *a: wrote.append(rows))
    assert ds.refresh(grid=(('GLB', 'TOPDIV3000', 1), ('USA', 'TOP3000', 1))) is True
    names = {r['name'] for r in wrote[0]}
    assert names == {'new_glb', 'old_usa'}   # GLB 갱신, USA 는 옛 행 보존


def test_dataset_list_failure_counts_as_thin_not_success(monkeypatch):
    """/data-sets 자체가 실패하면 필드 0개다 — '기대치 0 이니 100% 달성'으로 새면 안 된다."""
    monkeypatch.setattr(ds, '_PAGE_SLEEP_S', 0)
    monkeypatch.setattr(ds, '_fetch_datasets', lambda *a: [])
    rows, expect = ds._collect_grid(None, 'GLB', 'TOPDIV3000', 1)
    assert rows == [] and expect < 0

    monkeypatch.setattr(ds, '_house_client',
                        lambda: type('C', (), {'session': None,
                                               'authenticate': lambda self: True})())
    monkeypatch.setattr(ds, 'refresh_operators', lambda c: True)
    wrote = []
    monkeypatch.setattr(ds, 'write_live_csv', lambda rows, *a: wrote.append(rows))
    assert ds.refresh(grid=(('GLB', 'TOPDIV3000', 1),)) is False
    assert wrote == []


def test_maybe_refresh_does_not_block_the_round(monkeypatch):
    """워커 라운드 맨 앞에서 불리므로 인라인 수집은 금지 — 스레드로 띄우고 즉시 반환."""
    monkeypatch.setattr(ds, '_csv_age_ts', lambda: 0.0)
    monkeypatch.setattr(ds, '_last_refresh', {'ts': 0.0})
    started = []
    monkeypatch.setattr(ds, 'refresh', lambda **kw: started.append(1))
    assert ds.maybe_refresh(ds._TTL_SEC + 1) is True
    import time as _t
    for _ in range(100):                      # 데몬 스레드가 뜰 때까지만 짧게 대기
        if started:
            break
        _t.sleep(0.01)
    assert started == [1]
    # 두 번째 호출은 방금 찍은 타임스탬프 때문에 다시 띄우지 않는다
    assert ds.maybe_refresh(ds._TTL_SEC + 2) is False
