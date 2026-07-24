"""하우스 RC 계정으로 /data-fields·/operators 실시간 조회 → data/live_datafields.csv.
실패는 절대 생성 흐름을 막지 않는다(호출부가 폴백)."""
from __future__ import annotations
import csv, logging, os, tempfile, time
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
# /data-fields 는 연속 호출에 429 를 준다(실측: 3페이지째부터). 문서의 "scripts do not
# lay excessive load on the server" 지침에 맞춰 페이지 사이를 쉬고 429 는 백오프한다.
_PAGE_SLEEP_S = float(os.environ.get('IQC_DATAFIELDS_PAGE_SLEEP_S', '1.5'))
_PAGE_RETRY_MAX = 5
_PAGE_RETRY_BASE_S = 5.0
# 부분 수집으로 기존 팔레트를 덮어쓰지 않기 위한 하한. datafield_palette 는 라이브 CSV 가
# 비어있지만 않으면 그쪽을 쓰므로, 300행짜리를 쓰면 팔레트가 통째로 퇴화한다(2026-07-21 실측).
_MIN_ROWS_TO_WRITE = int(os.environ.get('IQC_DATAFIELDS_MIN_ROWS', '3000'))


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


def _fetch_page(client, region, universe, delay, offset):
    """한 페이지. 429 면 지수 백오프로 재시도. 끝내 실패하면 None."""
    wait = _PAGE_RETRY_BASE_S
    for _ in range(_PAGE_RETRY_MAX):
        r = client.session.get(f'{wqb_api.BASE}/data-fields',
                               params={'region': region, 'universe': universe,
                                       'delay': delay, 'instrumentType': 'EQUITY',
                                       'limit': 50, 'offset': offset})
        if r.ok:
            return r.json()
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


def refresh(now_ts: float | None = None,
            grid=(('USA', 'TOP3000', 1), ('USA', 'TOP3000', 0))) -> bool:
    """하우스 계정으로 grid 별 /data-fields 페이지네이션 수집 → 라이브 CSV. 성공 True.

    ⚠ 20000행짜리 수집이라 **하루 1회**(_TTL_SEC)만 돌아야 하고, 페이지 사이를 쉬어야
    한다. 부분 수집은 쓰지 않는다 — 팔레트가 퇴화하기 때문(_MIN_ROWS_TO_WRITE).
    """
    try:
        c = _house_client()
        if not c or not c.authenticate():
            LOG.warning('house RC client 미가용 — 라이브 데이터 새로고침 skip')
            return False
        all_rows = []
        for region, universe, delay in grid:
            offset = 0
            while True:
                j = _fetch_page(c, region, universe, delay, offset)
                if j is None:
                    LOG.warning('datafields %s/%s/D%s offset=%d 중단', region, universe,
                                delay, offset)
                    break
                res = j.get('results') or []
                if not res:
                    break
                all_rows += map_datafields(res, region, universe, delay)
                offset += 50
                if offset >= int(j.get('count') or 0):
                    break
                time.sleep(_PAGE_SLEEP_S)
        # operators 는 region 무관·1회 수집 — 같은 세션으로 함께 새로고침(best-effort).
        try:
            refresh_operators(c)
        except Exception as e:
            LOG.warning('operators 새로고침 skip: %s', e)
        if len(all_rows) >= _MIN_ROWS_TO_WRITE:
            write_live_csv(all_rows)
            if now_ts is not None:
                _last_refresh['ts'] = now_ts
            LOG.info('live datafields 새로고침: %d rows', len(all_rows))
            return True
        if all_rows:
            # 부분 수집 — 기존 CSV 를 지키고 다음 TTL 때 다시 시도한다.
            LOG.warning('datafields 부분 수집 %d행 < %d — 기존 팔레트 보존(쓰지 않음)',
                        len(all_rows), _MIN_ROWS_TO_WRITE)
            if now_ts is not None:
                _last_refresh['ts'] = now_ts   # 즉시 재시도해 429 를 키우지 않는다
    except Exception as e:
        LOG.warning('refresh 실패(폴백 유지): %s', e)
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
    last = max(_last_refresh['ts'], _csv_age_ts())
    if now_ts - last >= _TTL_SEC:
        return refresh(now_ts=now_ts)
    return False
