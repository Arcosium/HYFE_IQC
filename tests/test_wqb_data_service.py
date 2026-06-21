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
