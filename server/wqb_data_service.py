"""하우스 RC 계정으로 /data-fields·/operators 실시간 조회 → data/live_datafields.csv.
실패는 절대 생성 흐름을 막지 않는다(호출부가 폴백)."""
from __future__ import annotations
import csv, json, logging, os, tempfile, threading, time
from . import db as _db
from . import wqb_api

LOG = logging.getLogger('genomicwqb.wqb_data')
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(os.path.dirname(_THIS_DIR), 'data')
LIVE_CSV_PATH = os.path.join(_DATA_DIR, 'live_datafields.csv')
LIVE_OPERATORS_CSV = os.path.join(_DATA_DIR, 'live_operators.csv')
CSV_COLUMNS = ['name', 'category', 'coverage', 'description', 'type',
               'date_coverage_pct', 'alphas', 'region', 'universe', 'delay']
# operator_catalog 가 arity/named-params 를 읽어 #2 프리플라이트(arity 검증)에 쓴다.
OP_CSV_COLUMNS = ['name', 'category', 'scope', 'definition', 'description',
                  'min_args', 'max_args', 'required_named']
HOUSE_RC_USERNAME = os.environ.get('HYFE_HOUSE_RC_USERNAME', 'platinumcasillas@gmail.com')
# 데이터필드 팔레트는 하루 한 번이면 충분하다 (필드 목록은 그보다 자주 안 바뀐다).
# 6시간이었을 땐 워커가 하루 4번 20000행을 긁어 429 를 유발했다.
_TTL_SEC = float(os.environ.get('IQC_DATAFIELDS_TTL_S', str(24 * 3600)))
_last_refresh = {'ts': 0.0}
_REFRESH_LOCK = threading.Lock()      # 동시 수집 방지 (워커 라운드 × theme_sync)
# /data-fields 는 연속 호출에 429 를 준다(실측: 3페이지째부터). 문서의 "scripts do not
# lay excessive load on the server" 지침에 맞춰 페이지 사이를 쉬고 429 는 백오프한다.
_PAGE_SLEEP_S = float(os.environ.get('IQC_DATAFIELDS_PAGE_SLEEP_S', '1.5'))
_PAGE_RETRY_MAX = 5
_PAGE_RETRY_BASE_S = 5.0
_PAGE_LIMIT = 50                      # /data-fields·/data-sets 의 하드 상한 (100 은 400)
# 부분 수집으로 기존 팔레트를 덮어쓰지 않기 위한 관문. 그리드별로 "기대 필드수
# (dataset fieldCount 합)" 대비 이만큼은 받아야 CSV 를 쓴다. 총행수 하한만 보던 옛
# 방식은 한 리전이 통째로 비어도 다른 리전 행이 채워서 통과해버렸다(2026-07-27).
_MIN_GRID_COVERAGE = float(os.environ.get('IQC_DATAFIELDS_MIN_COVERAGE', '0.9'))


class _AuthExpired(RuntimeError):
    """Stop a whole palette refresh after the house session expires."""


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


def _split_top_args(s: str) -> list[str]:
    """최상위 콤마 기준 인자 분할 (괄호 깊이 무시)."""
    args, depth, cur = [], 0, ''
    for ch in s:
        if ch in '([{':
            depth += 1; cur += ch
        elif ch in ')]}':
            depth -= 1; cur += ch
        elif ch == ',' and depth == 0:
            args.append(cur.strip()); cur = ''
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


def parse_operator_signature(definition: str):
    """WQB operator definition(예: 'ts_rank(x, d)', 'hump(x, hump=0.01)') 에서
    (min_args, max_args, required_named_csv) 추출. 파싱 불가 → (None, None, '').
      - min_args: '=' 없고 '...' 아닌 위치인자 수(필수 최소)
      - max_args: 가변('...')이면 None, 아니면 전체 인자 수
      - required_named: '=' 로 표기된 named param 이름 (advisory)"""
    try:
        d = (definition or '').strip()
        i = d.find('(')
        if i < 0:
            return (None, None, '')
        inner = d[i + 1:]
        j = inner.rfind(')')
        if j >= 0:
            inner = inner[:j]
        args = _split_top_args(inner)
        if not args or (len(args) == 1 and not args[0]):
            return (0, 0, '')
        variadic = any('...' in a for a in args)
        named = [a.split('=')[0].strip() for a in args if '=' in a and a.split('=')[0].strip()]
        min_args = sum(1 for a in args if '=' not in a and '...' not in a)
        max_args = None if variadic else len(args)
        return (min_args, max_args, ','.join(named))
    except Exception:
        return (None, None, '')


def map_operators(api_results) -> list[dict]:
    rows = []
    for op in api_results or []:
        name = str(op.get('name') or '').strip()
        if not name:
            continue
        definition = str(op.get('definition') or op.get('expression') or '')
        scope = op.get('scope')
        scope_s = ('|'.join(scope) if isinstance(scope, (list, tuple)) else str(scope or ''))
        mn, mx, named = parse_operator_signature(definition)
        rows.append({
            'name': name,
            'category': str(op.get('category') or ''),
            'scope': scope_s,
            'definition': definition,
            'description': str(op.get('description') or '')[:300],
            'min_args': '' if mn is None else str(mn),
            'max_args': '' if mx is None else str(mx),
            'required_named': named,
        })
    return rows


def write_live_operators_csv(rows, path=LIVE_OPERATORS_CSV) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', newline='') as fh:
            w = csv.DictWriter(fh, fieldnames=OP_CSV_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, '') for k in OP_CSV_COLUMNS})
        os.replace(tmp, path)  # 원자적
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def refresh_operators(client) -> bool:
    """하우스 계정 세션으로 /operators 1회 수집 → live_operators.csv. 성공 True.
    실패는 절대 생성 흐름을 막지 않는다(operator_catalog 가 정적 CSV+seed 로 폴백)."""
    try:
        r = client.session.get(f'{wqb_api.BASE}/operators')
        if not r.ok:
            return False
        j = r.json()
        results = j if isinstance(j, list) else (j.get('results') or j.get('operators') or [])
        rows = map_operators(results)
        if rows:
            write_live_operators_csv(rows)
            LOG.info('live operators 새로고침: %d ops', len(rows))
            return True
    except Exception as e:
        LOG.warning('refresh_operators 실패(폴백 유지): %s', e)
    return False


def _house_client():
    uid = _db.get_user_id_by_username(HOUSE_RC_USERNAME)
    if not uid:
        return None
    creds = _db.get_user_credentials(uid)
    if not creds:
        return None
    u, p, _ = creds
    return wqb_api.WqbApiClient(u, p)


def _get_paged(client, path, params):
    """한 페이지. 429 면 지수 백오프로 재시도. 끝내 실패하면 None."""
    wait = _PAGE_RETRY_BASE_S
    for _ in range(_PAGE_RETRY_MAX):
        r = client.session.get(f'{wqb_api.BASE}{path}', params=params)
        if r.ok:
            return r.json()
        if r.status_code in (401, 403):
            # Returning None only stops the current dataset, so the outer loop
            # would keep issuing one unauthorized request per dataset for
            # minutes.  Authentication loss invalidates the entire refresh.
            raise _AuthExpired(f'http_{r.status_code}')
        if r.status_code != 429:
            return None
        ra = r.headers.get('Retry-After')
        try:
            wait = float(ra) if ra else wait
        except (TypeError, ValueError):
            pass
        time.sleep(min(wait, 120.0))
        wait = min(wait * 2, 120.0)
    return None


def _fetch_page(client, region, universe, delay, offset, dataset_id):
    """dataset_id 는 필수다 — 전체 조회는 10000 캡에 걸려 쓸 수 없다(_collect_grid 주석)."""
    return _get_paged(client, '/data-fields',
                      {'region': region, 'universe': universe, 'delay': delay,
                       'instrumentType': 'EQUITY', 'limit': _PAGE_LIMIT,
                       'offset': offset, 'dataset.id': dataset_id})


def _fetch_datasets(client, region, universe, delay) -> list[tuple]:
    """그 그리드의 (dataset_id, fieldCount) 전체. 실패하면 빈 리스트."""
    out, offset = [], 0
    while True:
        j = _get_paged(client, '/data-sets',
                       {'region': region, 'universe': universe, 'delay': delay,
                        'instrumentType': 'EQUITY', 'limit': _PAGE_LIMIT,
                        'offset': offset})
        if j is None:
            return []
        res = j.get('results') or []
        if not res:
            break
        out += [(d.get('id'), int(d.get('fieldCount') or 0)) for d in res if d.get('id')]
        offset += _PAGE_LIMIT
        if offset >= int(j.get('count') or 0):
            break
        time.sleep(_PAGE_SLEEP_S)
    return out


def _collect_grid(client, region, universe, delay) -> tuple:
    """한 그리드의 **전 필드**를 dataset.id 별로 나눠 수집 → (rows, 기대필드수).

    ⚠ 왜 데이터셋별로 쪼개나 — /data-fields 는 전체 조회 시 count 를 10000 으로 캡하고
    필드 id **알파벳순**으로만 준다. offset=10000 은 400 ("Invalid offset. Please use
    filters to narrow down the result."), limit 은 50 이 상한, order=-id 로 역방향을
    받아도 정·역 합쳐 20000 이라 GLB(2만 초과)엔 중간 구멍이 남는다 — 실제로 'g'~'n'
    구간이 통째로 빠져 mdl110_*·rsk70_* 이 한 번도 후보에 오르지 못했다(2026-07-27).
    API 가 에러 메시지로 직접 지시한 대로 dataset.id 필터로 쪼개면 캡에 안 걸린다.
    """
    rows, expect = [], 0
    datasets = _fetch_datasets(client, region, universe, delay)
    if not datasets:
        return [], -1        # 목록 자체를 못 받음 → 결손으로 취급(호출부가 쓰기 중단)
    for ds_id, n_fields in datasets:
        if n_fields <= 0:
            continue
        expect += n_fields
        offset = 0
        while True:
            j = _fetch_page(client, region, universe, delay, offset, dataset_id=ds_id)
            if j is None:
                LOG.warning('datafields %s/%s/D%s dataset=%s offset=%d 중단',
                            region, universe, delay, ds_id, offset)
                break
            res = j.get('results') or []
            if not res:
                break
            rows += map_datafields(res, region, universe, delay)
            offset += _PAGE_LIMIT
            if offset >= int(j.get('count') or 0):
                break
            time.sleep(_PAGE_SLEEP_S)
    return rows, expect


def _load_live_rows() -> list[dict]:
    """현재 라이브 CSV 의 행들. 없거나 못 읽으면 빈 리스트."""
    try:
        with open(LIVE_CSV_PATH, newline='', encoding='utf-8') as fh:
            return list(csv.DictReader(fh))
    except (OSError, csv.Error):
        return []


def _rows_by_grid(rows) -> dict:
    """행 목록 → {(region, universe, delay): [행…]}. 그리드 단위 부분 갱신용."""
    out: dict = {}
    for r in rows or []:
        out.setdefault((str(r.get('region') or ''), str(r.get('universe') or ''),
                        str(r.get('delay') or '')), []).append(r)
    return out


def _constraint_grid_extra() -> tuple:
    """활성 탐색 조건이 USA 밖 리전이면 그 (region, universe, delay) 콤보를 그리드에
    추가한다 (2026-07-27, GLB 테마). 이게 없으면 일일 갱신이 CSV 를 USA 행만으로
    다시 써서 지역 팔레트가 하루 만에 증발한다. 실패 시 빈 튜플(fail-open)."""
    try:
        from . import run_config
        c = run_config.get_constraint()
        if c is None or not c.region or str(c.region).upper() == 'USA':
            return ()
        return ((str(c.region).upper(),
                 str(c.universe or 'TOP3000').upper(),
                 int(c.delay if c.delay is not None else 1)),)
    except Exception:
        return ()


DATASET_META_PATH = os.path.join(_DATA_DIR, 'dataset_meta.json')


def refresh_dataset_meta(client, grid) -> bool:
    """그리드별 /data-sets 를 받아 데이터셋 Value Score·dateUpdated 를 캡처한다
    → data/dataset_meta.json (2026-08-26, WQB Data Explorer 피드백).

    /data-sets 는 그리드당 3~6페이지로 싸다(필드 수집과 별개). Value Score 는
    그리드마다 다를 수 있어 **최댓값**을 남긴다. 실패는 조용히 넘어간다(fail-open —
    없으면 datafield_palette.dataset_value_score 가 0.0 폴백)."""
    meta: dict = {}
    try:
        with open(DATASET_META_PATH, encoding='utf-8') as fh:
            meta = json.load(fh) or {}
    except (OSError, ValueError):
        meta = {}
    changed = False
    for region, universe, delay in grid:
        try:
            for d in _get_paged_all(client, region, universe, delay):
                k = str(d.get('id') or '').strip().lower()
                if not k:
                    continue
                vs = d.get('valueScore')
                prev = meta.get(k, {})
                pv = prev.get('vs')
                cat = d.get('category')
                cat = cat.get('id') if isinstance(cat, dict) else cat
                meta[k] = {
                    'vs': max([x for x in (vs, pv) if x is not None], default=None),
                    'userCount': d.get('userCount'), 'alphaCount': d.get('alphaCount'),
                    'fieldCount': d.get('fieldCount'), 'dateUpdated': d.get('dateUpdated'),
                    'name': (d.get('name') or '')[:60], 'category': cat}
                changed = True
        except Exception as e:
            LOG.warning('dataset_meta %s/%s/D%s 수집 skip: %s', region, universe, delay, e)
    if not changed:
        return False
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_DATA_DIR, suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False)
        os.replace(tmp, DATASET_META_PATH)
        LOG.info('dataset_meta 갱신: %d datasets', len(meta))
        return True
    except OSError as e:
        LOG.warning('dataset_meta 쓰기 실패: %s', e)
        return False


def _get_paged_all(client, region, universe, delay) -> list:
    """그 그리드의 /data-sets 전체 dict 목록 (fieldCount 만이 아니라 원본 필드 보존)."""
    out, offset = [], 0
    while True:
        j = _get_paged(client, '/data-sets',
                       {'region': region, 'universe': universe, 'delay': delay,
                        'instrumentType': 'EQUITY', 'limit': _PAGE_LIMIT, 'offset': offset})
        if j is None:
            break
        res = j.get('results') or []
        out += res
        offset += _PAGE_LIMIT
        if not res or offset >= int(j.get('count') or 0):
            break
        time.sleep(_PAGE_SLEEP_S)
    return out


def refresh(now_ts: float | None = None, grid=None) -> bool:
    """하우스 계정으로 grid 별 /data-fields 페이지네이션 수집 → 라이브 CSV. 성공 True.

    grid=None 이면 USA 기본 콤보 + 활성 조건의 리전 콤보(_constraint_grid_extra).

    ⚠ 그리드당 수만 행짜리 수집이라 **하루 1회**(_TTL_SEC)만 돌아야 하고, 페이지 사이를
    쉬어야 한다. 부분 수집은 쓰지 않는다 — 팔레트가 퇴화하기 때문(_MIN_GRID_COVERAGE).
    ⚠ 오래 걸리므로(그리드당 10분+) 호출부는 **백그라운드 스레드**에서 돌려야 한다.
    """
    if grid is None:
        grid = (('USA', 'TOP3000', 1), ('USA', 'TOP3000', 0)) + _constraint_grid_extra()
    if not _REFRESH_LOCK.acquire(blocking=False):
        # 워커 라운드와 theme_sync 가 동시에 부를 수 있다 — 겹치면 같은 수천 페이지를
        # 두 번 긁어 429 만 키운다.
        LOG.info('datafields 수집 이미 진행 중 — skip')
        return False
    try:
        c = _house_client()
        if not c or not c.authenticate():
            LOG.warning('house RC client 미가용 — 라이브 데이터 새로고침 skip')
            return False
        # operators 는 region 무관·1회 수집 — 필드 수집보다 **먼저**(싸고 독립적이라
        # 필드 쪽이 결손으로 중단돼도 영향을 안 받는다).
        try:
            refresh_operators(c)
        except Exception as e:
            LOG.warning('operators 새로고침 skip: %s', e)
        # 데이터셋 Value Score/dateUpdated 캡처 (싸고 독립적 — 필드 수집 전에).
        try:
            refresh_dataset_meta(c, grid)
        except Exception as e:
            LOG.warning('dataset_meta 새로고침 skip: %s', e)
        # ⚠ 그리드별로 **부분 갱신**한다 (2026-07-27). 예전엔 전체를 새로 쓰고 한
        #   그리드라도 결손이면 통째로 버렸는데, 실제로 GLB 29343행을 20분에 걸쳐
        #   멀쩡히 받아 놓고 뒤이은 USA/D1 이 429 로 111개 데이터셋을 놓치자 GLB 까지
        #   같이 폐기됐다. 성공한 그리드는 반영하고, 결손 그리드는 옛 행을 남긴다.
        kept = _rows_by_grid(_load_live_rows())
        fresh, thin = {}, []
        for region, universe, delay in grid:
            rows, expect = _collect_grid(c, region, universe, delay)
            key = (str(region), str(universe), str(delay))
            if expect < 0 or len(rows) < expect * _MIN_GRID_COVERAGE:
                thin.append(f'{region}/{universe}/D{delay} {len(rows)}/{expect}')
                continue
            fresh[key] = rows
        if thin:
            LOG.warning('datafields 그리드 결손(%s) — 그 그리드는 옛 행 유지', ', '.join(thin))
        if not fresh:
            if now_ts is not None:
                _last_refresh['ts'] = now_ts   # 즉시 재시도하면 429 만 키운다
            return False
        kept.update(fresh)
        all_rows = [r for rows in kept.values() for r in rows]
        write_live_csv(all_rows)
        if now_ts is not None:
            _last_refresh['ts'] = now_ts
        LOG.info('live datafields 새로고침: %d rows (갱신 그리드 %d, 유지 %d)',
                 len(all_rows), len(fresh), len(kept) - len(fresh))
        return True
    except _AuthExpired as e:
        LOG.info('datafields 갱신 중단 — WQB 인증 만료(%s)', e)
    except Exception as e:
        LOG.warning('refresh 실패(폴백 유지): %s', e)
    finally:
        _REFRESH_LOCK.release()
    return False


def _csv_age_ts() -> float:
    """라이브 CSV 의 mtime. 없으면 0.

    _last_refresh 는 프로세스 메모리라 **재시작할 때마다 0 으로 돌아간다** — 그러면
    재시작 직후 첫 라운드가 매번 20000행 수집을 시작한다(재시작이 잦으면 429 폭탄).
    디스크의 CSV 나이를 진실로 삼아 그걸 막는다.
    """
    try:
        return os.path.getmtime(LIVE_CSV_PATH)
    except OSError:
        return 0.0


def maybe_refresh(now_ts: float) -> bool:
    """TTL 만료면 수집을 **백그라운드로** 기동하고 즉시 반환(기동했으면 True).

    ⚠ 워커 라운드 맨 앞에서 불린다 — 인라인으로 돌리면 데이터셋별 수천 페이지 수집이
    끝날 때까지 라운드가 통째로 멈춘다(그리드당 10분+).
    """
    last = max(_last_refresh['ts'], _csv_age_ts())
    if now_ts - last < _TTL_SEC:
        return False
    if _REFRESH_LOCK.locked():
        return False
    _last_refresh['ts'] = now_ts      # 스레드가 끝나기 전 다음 라운드가 또 띄우지 않도록
    threading.Thread(target=lambda: refresh(now_ts=time.time()),
                     daemon=True, name='datafields-refresh').start()
    return True
