# tests/test_datafield_palette_live.py
import importlib, os
import server.datafield_palette as dp

def _write_csv(path, names):
    import csv
    cols = ['name','category','coverage','description','type','date_coverage_pct','alphas','region','universe','delay']
    with open(path, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader()
        for n in names:
            w.writerow({'name': n, 'category': 'matrix', 'coverage': '100', 'description': 'd',
                        'type': 'Matrix', 'date_coverage_pct': '100', 'alphas': '5',
                        'region': 'USA', 'universe': 'TOP3000', 'delay': '1'})

def test_live_csv_preferred(tmp_path, monkeypatch):
    live = str(tmp_path / 'live_datafields.csv'); _write_csv(live, ['LIVE_FIELD_X'])
    monkeypatch.setattr(dp, '_LIVE_CSV_PATH', live)
    p = dp._default_datafields_path()
    assert p == live
    out = dp.build_palette(region='USA', delay=1, universe='TOP3000', n=1)
    assert 'LIVE_FIELD_X' in out

def test_falls_back_to_static_when_no_live(tmp_path, monkeypatch):
    monkeypatch.setattr(dp, '_LIVE_CSV_PATH', str(tmp_path / 'nope.csv'))
    assert dp._default_datafields_path() == dp.DATAFIELDS_CSV
