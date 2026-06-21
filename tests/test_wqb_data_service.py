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
    assert set(['name','category','coverage','description','type','date_coverage_pct',
                'alphas','region','universe','delay']).issubset(rd[0].keys())
