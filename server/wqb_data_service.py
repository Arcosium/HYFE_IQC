"""하우스 RC 계정으로 /data-fields·/operators 실시간 조회 → data/live_datafields.csv.
실패는 절대 생성 흐름을 막지 않는다(호출부가 폴백)."""
from __future__ import annotations
import csv, logging, os, tempfile
from . import db as _db
from . import wqb_api

LOG = logging.getLogger('hyfe.wqb_data')
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_THIS_DIR), 'data')
LIVE_CSV_PATH = os.path.join(_DATA_DIR, 'live_datafields.csv')
CSV_COLUMNS = ['name', 'category', 'coverage', 'description', 'type',
               'date_coverage_pct', 'alphas', 'region', 'universe', 'delay']
HOUSE_RC_USERNAME = os.environ.get('HYFE_HOUSE_RC_USERNAME', 'platinumcasillas@gmail.com')
_TTL_SEC = 6 * 3600
_last_refresh = {'ts': 0.0}


def map_datafields(api_results, region, universe, delay) -> list[dict]:
    rows = []
    for d in api_results or []:
        cov = d.get('coverage')
        cov_pct = int(round(cov * 100)) if isinstance(cov, (int, float)) and cov <= 1.0 else int(cov or 0)
        typ = str(d.get('type') or '')
        rows.append({
            'name': d.get('id', ''),
            'category': (d.get('dataset') or {}).get('id') or typ.lower(),
            'coverage': str(cov_pct),
            'description': d.get('description', ''),
            'type': typ.title() if typ else '',
            'date_coverage_pct': str(cov_pct),
            'alphas': str(d.get('alphaCount', d.get('userCount', 0)) or 0),
            'region': region, 'universe': universe, 'delay': str(delay),
        })
    return rows


def write_live_csv(rows, path=LIVE_CSV_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, '') for k in CSV_COLUMNS})
        os.replace(tmp, path)  # 원자적
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _house_client():
    uid = _db.get_user_id_by_username(HOUSE_RC_USERNAME)
    if not uid:
        return None
    creds = _db.get_user_credentials(uid)
    if not creds:
        return None
    u, p, _ = creds
    return wqb_api.WqbApiClient(u, p)


def refresh(now_ts: float | None = None,
            grid=(('USA', 'TOP3000', 1), ('USA', 'TOP3000', 0))) -> bool:
    """하우스 계정으로 grid 별 /data-fields 페이지네이션 수집 → 라이브 CSV. 성공 True."""
    try:
        c = _house_client()
        if not c or not c.authenticate():
            LOG.warning('house RC client 미가용 — 라이브 데이터 새로고침 skip')
            return False
        all_rows = []
        for region, universe, delay in grid:
            offset = 0
            while True:
                r = c.session.get(f'{wqb_api.BASE}/data-fields',
                                  params={'region': region, 'universe': universe,
                                          'delay': delay, 'limit': 50, 'offset': offset})
                if not r.ok:
                    break
                j = r.json()
                res = j.get('results') or []
                all_rows += map_datafields(res, region, universe, delay)
                offset += 50
                if offset >= int(j.get('count') or 0) or not res:
                    break
        if all_rows:
            write_live_csv(all_rows)
            if now_ts is not None:
                _last_refresh['ts'] = now_ts
            LOG.info('live datafields 새로고침: %d rows', len(all_rows))
            return True
    except Exception as e:
        LOG.warning('refresh 실패(폴백 유지): %s', e)
    return False


def maybe_refresh(now_ts: float) -> bool:
    if now_ts - _last_refresh['ts'] >= _TTL_SEC:
        return refresh(now_ts=now_ts)
    return False
