"""theme_sync — Power Pool 주간 테마 자동 동기화 (2026-07-27 사장 지시).

WQB 지원 문서(Current month Power Pool Themes)가 주간 테마를 **우리 constraint
문법 그대로** 게시한다. 매주 월요일 00:00 UTC(= KST 월 09:00) 경계로 바뀌므로,
워커가 라운드 시작 전에 이 모듈을 불러 현재 주 테마를 탐색 조건으로 건다.

접근 경로 (실측 2026-07-27):
  - 문서는 로그인 필요(SSO→platform biometric 벽) + Cloudflare 봇차단(requests 403).
  - **유일하게 뚫리는 조합**: persona 인증된 REST 세션의 쿠키 `t` 를 Playwright
    브라우저 컨텍스트에 주입 → SSO 통과, biometric 안 뜸.

안전장치:
  - 사용자가 수동으로 다른 조건을 걸어놨으면 **덮어쓰지 않는다** — 마지막으로
    자동 적용한 테마 텍스트(run_config)와 현재 조건이 다르면 사용자 커스텀으로
    간주하고 물러난다.
  - fetch 실패는 조용히 무시(현 조건 유지). 킬스위치 IQC_THEME_SYNC=0.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time

LOG = logging.getLogger('genomicwqb.theme_sync')

ARTICLE_URL = ('https://support.worldquantbrain.com/hc/en-us/articles/'
               '38927747787031-Current-month-Power-Pool-Themes')
ENABLED = os.environ.get('IQC_THEME_SYNC', '1') != '0'
_TTL_S = float(os.environ.get('IQC_THEME_SYNC_TTL_S', str(6 * 3600)))

_MONTHS = {m.lower(): i + 1 for i, m in enumerate(
    ('January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'))}

# 모듈 상태 — 재시작 시 리셋(첫 라운드에 재확인). {'ts': epoch, 'week': iso월요일}
_last_check = {'ts': 0.0, 'week': ''}
# maybe_sync 호출자의 user_id — 테마 적용 직후 플레이북을 그 사용자로 돌린다.
_playbook_uid = 0


def _norm(text) -> str:
    return ' '.join(str(text or '').split())


def monday_of(d: _dt.date) -> _dt.date:
    return d - _dt.timedelta(days=d.weekday())


def parse_week_themes(text: str, now_utc=None) -> list[tuple]:
    """문서 텍스트 → [(월요일 date, 테마 문자열)] (주 순서대로).

    형식(실측): 월 헤딩("July") → 요일 헤더 → 날짜행("27 29 29 30 31 1 August 2")
    → 빈 줄 → 'region=' 로 시작하는 테마행. 날짜행 첫 숫자 = 그 주 월요일 일자.
    날짜행엔 오타가 실재하므로(7월 문서의 '29 29'), 요일 검증이 되는 행 하나를
    앵커로 잡고 나머지는 ±7일 산술로 만든다.
    """
    now = now_utc or _dt.datetime.now(_dt.timezone.utc)
    lines = [ln.strip() for ln in str(text or '').splitlines()]
    base_month = None
    for ln in lines:
        if ln.lower() in _MONTHS:
            base_month = _MONTHS[ln.lower()]
            break
    if base_month is None:
        base_month = now.month

    rows = []            # (first_day:int|None, theme:str)
    pending_day = None
    for ln in lines:
        m = re.match(r'^(\d{1,2})\b', ln)
        if m and 'region=' not in ln:
            pending_day = int(m.group(1))
            continue
        if 'region=' in ln:
            rows.append((pending_day, _norm(ln)))
            pending_day = None
    if not rows:
        return []

    year = now.year
    # 앵커: 요일이 실제 월요일로 검증되는 첫 행. 월 후보 = base_month 와 그 전달
    # (첫 주가 전달에 걸칠 수 있다). 연말 경계는 후보에 ±1 개월만 두면 충분하다.
    anchor_idx, anchor_date = None, None
    for i, (day, _t) in enumerate(rows):
        if day is None:
            continue
        for mo_off in (0, -1, 1):
            mo = base_month + mo_off
            yr = year + (0 if 1 <= mo <= 12 else (1 if mo > 12 else -1))
            mo = ((mo - 1) % 12) + 1
            try:
                cand = _dt.date(yr, mo, day)
            except ValueError:
                continue
            if cand.weekday() == 0 and abs((cand - _dt.date(year, base_month, 15)).days) < 45:
                anchor_idx, anchor_date = i, cand
                break
        if anchor_date is not None:
            break
    if anchor_date is None:
        return []
    return [(anchor_date + _dt.timedelta(days=7 * (i - anchor_idx)), t)
            for i, (_d, t) in enumerate(rows)]


def current_theme(text: str, now_utc=None) -> str | None:
    """지금(UTC) 속한 주의 테마 문자열. 문서에 없으면 None."""
    now = now_utc or _dt.datetime.now(_dt.timezone.utc)
    today = now.date()
    for mon, theme in parse_week_themes(text, now_utc=now):
        if mon <= today < mon + _dt.timedelta(days=7):
            return theme
    return None


def fetch_article_text(username: str, password: str) -> str | None:
    """persona 인증된 REST 세션 쿠키를 Playwright 에 주입해 문서 본문을 긁는다."""
    try:
        from . import wqb_api
        from playwright.sync_api import sync_playwright
    except Exception as e:
        LOG.warning('theme fetch 의존성 없음: %s', e)
        return None
    try:
        client = wqb_api.WqbApiClient(username, password)
        if not client.authenticate():
            LOG.warning('theme fetch — REST 인증 실패')
            return None
        cookies = [{'name': ck.name, 'value': ck.value,
                    'domain': '.worldquantbrain.com', 'path': '/'}
                   for ck in client.session.cookies]
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                ctx = b.new_context(user_agent=(
                    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                    '(KHTML, like Gecko) Chrome/126.0 Safari/537.36'))
                ctx.add_cookies(cookies)
                pg = ctx.new_page()
                pg.goto(ARTICLE_URL, timeout=45000, wait_until='domcontentloaded')
                pg.wait_for_timeout(2500)
                for sel in ("button:has-text('Accept All')",
                            "button:has-text('Accept')"):
                    try:
                        pg.locator(sel).first.click(timeout=1500)
                        break
                    except Exception:
                        pass
                try:
                    body = pg.locator('article, .article-body').first.inner_text(
                        timeout=10000)
                except Exception:
                    body = pg.inner_text('body')
                if 'region=' not in body:
                    LOG.warning('theme fetch — 본문에 테마 없음 (url=%s)', pg.url)
                    return None
                return body
            finally:
                b.close()
    except Exception as e:
        LOG.warning('theme fetch 실패: %s', e)
        return None


_SCOPE_RE = re.compile(r'\b([A-Z]{3})/D(\d)\b')


def observed_theme(user_id: int) -> tuple[str, str, str]:
    """최근 알파의 WQB 체크에서 관측한 **활성 Power Pool 테마** → (이름, region, delay).

    지원문서는 월초에 늦는다(2026-08-04 실측: 8월 표 미게시, 마지막 행이 7/27 주).
    문서만 원천으로 두면 새 테마가 걸려도 못 알아채고 옛 조건으로 한 달을 간다.
    알파마다 붙는 MATCHES_THEMES 수확이 두 번째 원천이다 — 이름에 스코프가 들어 있다
    ('GLB/D1 Power Pool Aug`26').
    """
    try:
        from . import theme_playbook
        names = theme_playbook.active_themes(int(user_id or 0)).get('all') or []
    except Exception as e:
        LOG.warning('활성 테마 관측 실패: %s', e)
        return '', '', ''
    for nm in names:
        if 'power pool' not in nm.lower():
            continue
        m = _SCOPE_RE.search(nm)
        return nm, (m.group(1) if m else ''), (m.group(2) if m else '')
    return '', '', ''


def note_observed_theme(user_id: int, log_fn=None) -> str | None:
    """활성 테마 이름이 바뀌었으면 기록하고 플레이북을 기동한다. 바뀐 이름을 반환.

    문서 경로(maybe_sync)와 독립이다 — 문서가 늦어도 이쪽이 새 테마를 잡는다.
    조건 텍스트는 건드리지 않는다. 스코프가 현재 조건과 어긋나면 **경고만** 남긴다.
    ponytail: 스코프 자동 교정은 안 한다 — 리전이 바뀌면 유니버스도 같이 무효라
    (TOPDIV3000 은 GLB 전용) 반쪽 교정이 시뮬을 전멸시킨다. 오탐 없는 자동 교정이
    필요해지면 문서 파싱 결과와 대조해 전체 조건을 갈아끼우는 쪽으로 올린다.
    """
    from . import run_config
    name, region, delay = observed_theme(user_id)
    if not name or name == run_config.get_theme_active_name():
        return None
    run_config.set_theme_active_name(name)
    LOG.info('활성 테마 실측 변경: %s', name)
    if log_fn:
        log_fn(f'🔔 활성 Power Pool 테마 = {name}')
    spec = None
    try:
        spec = run_config.get_constraint()
    except Exception:
        pass
    cur_r = str(getattr(spec, 'region', '') or '').upper()
    cur_d = str(getattr(spec, 'delay', '') or '')
    if region and cur_r and region != cur_r or delay and cur_d and delay != cur_d:
        msg = (f'⚠ 탐색 조건({cur_r}/D{cur_d})이 활성 테마({region}/D{delay})와 다릅니다 '
               f'— 이대로면 이번 테마엔 한 건도 못 넣습니다.')
        LOG.warning(msg)
        if log_fn:
            log_fn(msg)
    try:                                   # 새 테마 → 공략 방침 자동 수립
        from . import theme_playbook
        theme_playbook.start_background(int(user_id or 0))
    except Exception as e:
        LOG.warning('테마 플레이북 기동 실패(무시): %s', e)
    return name


def maybe_sync(username: str, password: str, user_id: int = 0) -> str | None:
    """필요 시 테마를 확인·적용한다. 조건이 **바뀌었으면** 새 텍스트를 반환.

    호출 비용 게이트: 주간 경계(월요일 00:00 UTC)를 넘겼으면 즉시, 아니면 TTL(6h).
    """
    if not ENABLED:
        return None
    global _playbook_uid
    _playbook_uid = int(user_id or 0)
    now = time.time()
    week_key = monday_of(_dt.datetime.now(_dt.timezone.utc).date()).isoformat()
    if _last_check['week'] == week_key and now - _last_check['ts'] < _TTL_S:
        return None
    _last_check.update(ts=now, week=week_key)

    from . import run_config
    cur = _norm(run_config.get_constraint_text())
    last_applied = _norm(run_config.get_theme_last_applied())
    if cur and last_applied and cur != last_applied:
        LOG.info('theme sync 보류 — 수동 조건 활성 중 (자동 덮어쓰기 금지)')
        return None

    text = fetch_article_text(username, password)
    if not text:
        return None
    theme = current_theme(text)
    if not theme:
        # 문서 미갱신(월초 8월 표 미게시 등) — 6h TTL 을 다 기다리면 새 테마 적용이
        # 반나절 늦는다. 45분 뒤 재확인하도록 마지막 체크 시각을 당겨 놓는다.
        _last_check['ts'] = now - _TTL_S + 45 * 60
        LOG.warning('theme sync — 이번 주 테마를 문서에서 못 찾음 (45분 뒤 재확인)')
        return None
    if _norm(theme) == cur:
        run_config.set_theme_last_applied(theme)   # 수동으로 같은 값 넣은 경우 귀속
        return None
    run_config.set_constraint_text(theme)
    run_config.set_theme_last_applied(theme)
    LOG.info('theme sync 적용: %s', theme[:120])
    _kick_palette_refresh_if_needed(theme)
    # 새 테마 → 로컬 LLM 이 스스로 전략을 세워 첫 큐(strategy_specs)에 넣는다
    # (2026-07-27 사장 지시 — Claude 없이도 같은 판단이 돌게).
    try:
        from . import theme_playbook
        theme_playbook.start_background(_playbook_uid or 0)
    except Exception as e:
        LOG.warning('테마 플레이북 기동 실패(무시): %s', e)
    return theme


def _kick_palette_refresh_if_needed(theme_text: str) -> None:
    """새 테마가 USA 밖 리전인데 그 리전 팔레트가 비어 있으면 데이터필드 수집을
    백그라운드로 즉시 기동한다 — 없으면 일일 TTL(최대 24h)까지 전 라운드가
    'unknown variable' 로 전멸한다 (2026-07-27 GLB 전환 실측)."""
    try:
        from . import constraint_spec, datafield_palette, wqb_data_service
        spec = constraint_spec.parse(theme_text)
        if not spec.region or str(spec.region).upper() == 'USA':
            return
        pools = datafield_palette.family_pools(
            delay=(spec.delay if spec.delay is not None else 1),
            region=spec.region, universe=spec.universe)
        if pools:
            return
        import threading
        threading.Thread(
            target=lambda: wqb_data_service.refresh(now_ts=time.time()),
            daemon=True, name='theme-palette-refresh').start()
        LOG.info('새 리전 팔레트 수집 기동: %s/%s', spec.region, spec.universe)
    except Exception as e:
        LOG.warning('팔레트 수집 기동 실패(무시): %s', e)
