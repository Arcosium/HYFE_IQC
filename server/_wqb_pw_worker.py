
import os, sys, json, time, traceback, re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

PROFILE = os.environ['IQC_PROFILE_DIR']
USERNAME = os.environ.get('WQB_USERNAME', '')
PASSWORD = os.environ.get('WQB_PASSWORD', '')
SIMULATE_URL = 'https://platform.worldquantbrain.com/simulate'
SIM_MAX_WAIT_SEC = int(os.environ.get('IQC_SIM_MAX_WAIT', '480'))
POLL_INTERVAL_SEC = int(os.environ.get('IQC_POLL_INTERVAL', '20'))
PASS_THRESHOLD = int(os.environ.get('IQC_PASS_THRESHOLD', '5'))

def _parse_num(v):
    if v is None: return None
    try:
        s = str(v).strip()
        unit = 1.0
        if s.endswith('%'):
            unit = 1.0/100; s = s[:-1]
        elif s.endswith('‱'):
            unit = 1.0/10000; s = s[:-1]
        elif s.lower().endswith('bp'):
            unit = 1.0/10000; s = s[:-2]
        s = s.replace(',', '').strip()
        return float(s) * unit
    except (ValueError, TypeError):
        return None

def _pick(metrics, *keys):
    for k in keys:
        v = metrics.get(k)
        if v not in (None, ''):
            return v
    return None

def _count_pass(metrics):
    if not metrics:
        return 0
    n = 0
    sharpe = _parse_num(metrics.get('sharpe'))
    if sharpe is not None and sharpe >= 1.25: n += 1
    fitness = _parse_num(metrics.get('fitness'))
    if fitness is not None and fitness >= 1.0: n += 1
    returns = _parse_num(metrics.get('returns'))
    if returns is not None and returns >= 0.05: n += 1
    turnover = _parse_num(metrics.get('turnover'))
    if turnover is not None and turnover <= 0.7: n += 1
    drawdown = _parse_num(metrics.get('drawdown'))
    if drawdown is not None and drawdown >= -0.3: n += 1
    margin = _parse_num(metrics.get('margin'))
    if margin is not None and margin > 0.0: n += 1
    sub_sh = _parse_num(_pick(metrics, 'subuniverse_sharpe', 'sub_universe_sharpe',
                              'sub_sharpe', 'subsharpe'))
    if sub_sh is not None and sub_sh >= 1.0: n += 1
    corr = _parse_num(_pick(metrics, 'self_correlation', 'correlation',
                            'is_correlation', 'selfcorrelation'))
    if corr is not None and corr < 0.7: n += 1
    return n


# 제출 판정 규칙 — server/db.py 의 genuine_selfcorr_reject 와 동일.
# 알파가 '제출 안 됨' 인 유일한 경우: Submit 후 self-corr 검사가 돌아
# 7 Pass / 1 Fail 로 바뀌고 구체 수치(0.7 이상)가 잡힌 거절.
# 그 외(무응답·Cannot submit·예외·수치 없는 above cutoff 등)는 제출 간주.
def _genuine_selfcorr_reject(submit_status):
    s = (submit_status or '').strip()
    if not s.lower().startswith('rejected:'):
        return False
    body = s[len('rejected:'):]
    if 'correlation' not in body.lower():
        return False
    return re.search(r'\d+\.\d+', body) is not None

_INPUT = json.loads(sys.stdin.read())
# stdin 은 [{idx, code, settings}] 형식을 우선 지원 — 워커가 알파 idx + sim settings 를
# 알리고 싶을 때. 호환성: code 문자열 리스트도 받음 (idx 는 1..N, settings 는 {}).
if _INPUT and isinstance(_INPUT[0], dict):
    formulas = [s.get('code', '') for s in _INPUT]
    indices = [int(s.get('idx') or (i+1)) for i, s in enumerate(_INPUT)]
    settings_list = [(s.get('settings') or {}) for s in _INPUT]
else:
    formulas = list(_INPUT)
    indices = list(range(1, len(formulas) + 1))
    settings_list = [{} for _ in formulas]
N = len(formulas)

def log(msg):
    print(f'[pw] {msg}', flush=True)

# ── 실패 진단용 dump ─────────────────────────────────────────────────────────
# click_tab / click_simulate 가 모든 fallback 다 시도하고도 실패한 시점에 호출.
# /home/opc/.hyfe_iqc_tmp/wqb_fail_<tag>_<ts>.{png,html,json} 에 떨궈서 사람이
# 사후 분석 가능하게 한다. (subprocess 환경이라 ~/path 직접 쓰기 OK)
# IQC_MAX_FAIL_DUMPS (기본 20) 초과 시 가장 오래된 파일부터 정리.
_FAIL_DUMP_DIR = os.path.join(os.path.expanduser('~'), '.hyfe_iqc_tmp')
_FAIL_DUMP_SEEN = {'n': 0}
def _dump_failure(page, tag):
    try:
        _FAIL_DUMP_SEEN['n'] += 1
        cap = int(os.environ.get('IQC_MAX_FAIL_DUMPS', '20'))
        if _FAIL_DUMP_SEEN['n'] > cap:
            return
        os.makedirs(_FAIL_DUMP_DIR, exist_ok=True)
        ts = time.strftime('%H%M%S')
        safe_tag = ''.join(c if (c.isalnum() or c in '_-') else '_' for c in tag)[:40]
        base = os.path.join(_FAIL_DUMP_DIR, f'wqb_fail_{safe_tag}_{ts}')
        png = base + '.png'
        try: page.screenshot(path=png, full_page=False, timeout=4000)
        except Exception as e: png = f'(screenshot err: {str(e)[:60]})'
        meta = {}
        try:
            meta = page.evaluate(r'''() => ({
                url: location.href,
                title: document.title,
                body_text_head: (document.body && document.body.innerText || '').slice(0, 600),
                editor_checkbox_count: document.querySelectorAll('.editor__checkbox').length,
                editor_checkbox_labels: [...document.querySelectorAll('.editor__checkbox')]
                    .map(e => ((e.innerText||'').trim() + (/--selected/.test(e.className||'') ? '*' : ''))),
                tab_count: document.querySelectorAll('.editor-tabs__tab-element').length,
                tab_labels: [...document.querySelectorAll('.editor-tabs__tab-text')].map(t => (t.innerText||'').trim()).slice(0, 12),
                sim_btn_class: (document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]')||{}).className || null,
                monaco_count: document.querySelectorAll('.monaco-editor').length,
                introjs_visible: document.querySelectorAll('[class*="introjs-"]:not(.introjs-hidden)').length,
            })''')
        except Exception as e:
            meta = {'evaluate_err': str(e)[:120]}
        try:
            with open(base + '.json', 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception: pass
        log(f'fail_dump: tag={safe_tag!r} png={png} meta_url={meta.get("url","?")[:80]!r} '
            f'modes={meta.get("editor_checkbox_labels","?")} tabs={meta.get("tab_labels","?")} '
            f'btn={(meta.get("sim_btn_class") or "None")[:80]!r}')
    except Exception as e:
        log(f'fail_dump exception: {e}')


def _read_self_correlation(page):
    """Correlation 상자를 활성화하고 Self-Correlation 행의 'V' 아래 화살표를 눌러
    self-correlation 계산을 트리거한 뒤, ~30s 내에 뜨는 Maximum 값을 float 로 반환한다.
    값 미수집(계산 안 됨/'-')이면 None.

    셀렉터 출처: 라이브 실험(2026-05-21) — 화살표 `.correlation__content-status-arrow--down`,
    값 라벨 `.correlation__content-status*title` ('Maximum') + 형제값. ('Self Correlation'
    아래 Maximum=최대 self-corr, Minimum=최소.)"""
    try:
        # 0) 혹시 Tutorial 모드면 완전 종료 — 'Exit tutorial mode' → confirm 'Exit'.
        #    Tutorial 모드는 Correlation 상자를 가린다. 평소(이미 종료됨)엔 no-op.
        for _ in range(2):
            did = page.evaluate(r'''() => {
                let acted=false;
                for(const b of [...document.querySelectorAll('button,[role="button"],a')]){
                  if(b.offsetParent===null) continue;
                  if(/^exit tutorial mode$/i.test((b.innerText||'').trim())){ try{b.click();}catch(e){} acted=true; }
                }
                return acted;
            }''')
            if not did:
                break
            page.wait_for_timeout(700)
            page.evaluate(r'''() => {
                for(const b of [...document.querySelectorAll('button,[role="button"],a')]){
                  if(b.offsetParent===null) continue;
                  if(/^exit$/i.test((b.innerText||'').trim())){ try{b.click();}catch(e){} return; }
                }
            }''')
            page.wait_for_timeout(1200)
        # 1) Correlation 섹션 활성화.
        page.evaluate(r'''() => {
            const c=document.querySelector('.alphas-details__actions-item--correlation');
            if(c){ try{c.click();}catch(e){} }
        }''')
        page.wait_for_timeout(1000)
        # 2) 클릭 전 'Last Run' 값 캡처 — 같은 탭을 알파마다 재사용하므로 직전 알파의
        #    self-corr 잔상값을 그대로 읽는 stale-read 위험이 있다. 화살표 클릭 후 Last Run 이
        #    바뀌어야(=현재 알파 fresh 계산 완료) Maximum 을 신뢰한다.
        prev_run = page.evaluate(r'''() => { const m=(document.body.innerText||'').match(/Last Run:\s*([^\n]*)/i); return m?m[1].trim():''; }''') or ''
        # 3) Self-Correlation 행의 down-arrow 클릭 → 계산 트리거.
        clicked = page.evaluate(r'''() => {
            function ownText(el){let t='';for(const n of el.childNodes){if(n.nodeType===3)t+=n.textContent;}return t.trim();}
            const sc=[...document.querySelectorAll('*')].find(el=>/^self[-\s]?correlation$/i.test(ownText(el)));
            const arrows=[...document.querySelectorAll('[class*="correlation__content-status-arrow"]')];
            if(!arrows.length) return false;
            let target=arrows[0];
            if(sc){const r=sc.getBoundingClientRect();let bd=1e9;
              for(const a of arrows){const ar=a.getBoundingClientRect();const d=Math.abs(ar.y-r.y);if(d<bd){bd=d;target=a;}}}
            try{target.click();}catch(e){}
            try{target.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));}catch(e){}
            return true;
        }''')
        if not clicked:
            log('_read_self_correlation: correlation arrow 없음')
            return None
        # 4) ~60s 폴링 — Last Run 갱신(fresh) + Maximum 값 등장 동시 충족 시에만 채택.
        for _ in range(15):
            page.wait_for_timeout(4000)
            st = page.evaluate(r'''() => {
                let mx='';
                const titles=[...document.querySelectorAll('[class*="correlation__content-status"][class*="title"]')];
                for(const t of titles){
                  if(/^maximum$/i.test((t.innerText||'').trim())){
                    let sib=t.nextElementSibling, v='';
                    while(sib && !v){ v=(sib.innerText||'').trim(); sib=sib.nextElementSibling; }
                    if(!v && t.parentElement) v=(t.parentElement.innerText||'').replace(/maximum/i,'').trim();
                    mx=v; break;
                  }
                }
                if(!mx){ const m=(document.body.innerText||'').match(/Maximum\s*\n\s*([-+]?\d*\.?\d+)/i); if(m) mx=m[1]; }
                const lr=(document.body.innerText||'').match(/Last Run:\s*([^\n]*)/i);
                return {mx:(mx||'').trim(), run:lr?lr[1].trim():''};
            }''')
            mx = (st or {}).get('mx', '')
            run = (st or {}).get('run', '')
            fresh = bool(run) and run not in ('-', '—') and run != prev_run
            if fresh and mx and mx not in ('-', '—'):
                m = re.search(r'[-+]?\d*\.?\d+', mx)
                if m:
                    try:
                        return float(m.group(0))
                    except ValueError:
                        pass
        log(f'_read_self_correlation: fresh Maximum ~60s 내 미수집 (prev_run={prev_run!r})')
        return None
    except Exception as e:
        log(f'_read_self_correlation err: {e}')
        return None


def _add_alpha_to_list(page, list_name='Submit'):
    """알파 상세 페이지를 새 탭으로 열고('Open alpha details in new tab'), 우상단
    'Add Alpha to a List'(`button.alpha__header-add`) 를 눌러 Semantic-UI 모달을 띄운 뒤,
    'List name' 드롭다운(`.alphaAdd__content-dropdown`)에서 기존 'Submit' 리스트를
    선택하고 'Add Alpha to List' 버튼을 눌러 그 리스트에 추가한다. 성공 시 True.

    (기존엔 우상단 별표 `.alpha__header-star` 로 favorite 저장했으나, 사용자 요청으로
    'Submit' 리스트 추가로 교체. downstream(submitted/submit_status) 의미는 그대로.)

    셀렉터 출처: 라이브 실험(2026-05-24) — 상세 페이지(/alpha/<id>) 헤더 우상단
    'Add Alpha to a List' 버튼 → 모달 `.ui.modal` → 드롭다운 `.alphaAdd__content-dropdown`
    의 `.menu .item`('Submit') → 버튼 'Add Alpha to List'. 추가 성공 시 모달이 닫힌다.
    'Add Alpha and View List' 는 새 탭으로 리스트를 여니 피하고 'Add Alpha to List' 사용."""
    detail = None
    try:
        ctx = page.context
        with ctx.expect_page(timeout=15000) as np_info:
            page.evaluate(r'''() => {
                function ownText(el){let t='';for(const n of el.childNodes){if(n.nodeType===3)t+=n.textContent;}return t.trim();}
                const el=[...document.querySelectorAll('*')].find(e=>/^open alpha details in new tab$/i.test(ownText(e)));
                if(el){ const b=el.closest('button,a,[role="button"]')||el; try{b.click();}catch(e){} }
            }''')
        detail = np_info.value
        detail.wait_for_load_state('domcontentloaded')
        # 액션 헤더('Add Alpha to a List' 버튼) 는 SPA 렌더 직후 약간 늦게 뜬다.
        try:
            detail.wait_for_selector('button.alpha__header-add', timeout=15000)
        except Exception:
            detail.wait_for_timeout(3000)
        # ★ promo/onboarding modal('Apply to be a Research Consultant' 등) 을 add-to-list
        #   모달 '열기 전' 에 제거한다. js_dismiss_overlays 는 .ui.modal.transition.visible.active
        #   를 강제 remove 하므로, add-to-list 모달을 연 뒤 부르면 우리 모달까지 날아간다.
        js_dismiss_overlays(detail)
        detail.wait_for_timeout(600)
        # 1) 'Add Alpha to a List' 클릭 → Semantic-UI 모달 오픈.
        opened = bool(detail.evaluate(r'''() => {
            const b=document.querySelector('button.alpha__header-add');
            if(!b) return false;
            try{b.click();}catch(e){}
            try{b.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));}catch(e){}
            return true;
        }'''))
        if not opened:
            log('_add_alpha_to_list: button.alpha__header-add 없음')
            return False
        try:
            detail.wait_for_selector('.alphaAdd__content-dropdown', timeout=8000)
        except Exception:
            detail.wait_for_timeout(1500)
        # 2) 'List name' 드롭다운 열기 (Semantic-UI: click 으로 .menu 펼침).
        detail.evaluate(r'''() => {
            const dd=document.querySelector('.alphaAdd__content-dropdown');
            if(dd){ try{dd.click();}catch(e){}
                dd.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                dd.dispatchEvent(new MouseEvent('click',{bubbles:true})); }
        }''')
        detail.wait_for_timeout(800)
        # 3) 기존 'Submit' 리스트 항목 선택. (없으면 실패 — 새 리스트 생성은 하지 않음.)
        sel = detail.evaluate(r'''(name) => {
            const dd=document.querySelector('.alphaAdd__content-dropdown');
            if(!dd) return 'no-dropdown';
            const items=[...dd.querySelectorAll('.menu .item,[role="option"]')];
            const want=items.find(e=>(e.innerText||e.textContent||'').trim().toLowerCase()===name.toLowerCase())
                     || items.find(e=>new RegExp(name,'i').test(e.innerText||''));
            if(!want) return 'no-item:'+items.map(e=>(e.innerText||'').trim()).join('|').slice(0,80);
            try{want.click();}catch(e){}
            want.dispatchEvent(new MouseEvent('click',{bubbles:true}));
            return 'selected';
        }''', list_name)
        log(f'_add_alpha_to_list: select {list_name!r} -> {sel}')
        if sel != 'selected':
            return False
        detail.wait_for_timeout(600)
        # 4) 'Add Alpha to List' 버튼 클릭 (disabled 면 선택 미반영 → 실패).
        added = bool(detail.evaluate(r'''() => {
            const btns=[...document.querySelectorAll('button')].filter(b=>/^add alpha to list$/i.test((b.innerText||'').trim()));
            if(!btns.length) return false;
            const b=btns[0];
            if(b.disabled) return false;
            try{b.click();}catch(e){}
            b.dispatchEvent(new MouseEvent('click',{bubbles:true}));
            return true;
        }'''))
        # 성공 판정: 'Submit' 이 선택된 상태에서 *활성화된* 'Add Alpha to List' 버튼을
        # 눌렀으면(=added) 서버측 추가는 성사된다 — 이게 권위 있는 신호다.
        # 모달 닫힘은 *보조* 신호로만 본다: 이전엔 `.ui.modal.visible.active`(임의 모달)
        # 가 사라졌는지로 판정했는데, 추가 직후 promo/온보딩 모달이 재등장하거나 우리
        # 모달의 닫힘 트랜지션이 고정 대기(1.5s) 안에 안 끝나면 modal_gone=False 가 떠
        # added=True 인데도 star_fail(추가실패)로 오판했다(2026-05-24 R346 idx8 사례).
        # → 우리 add-to-list 모달(`.alphaAdd__content-dropdown`)만 스코프해 grace
        # 재폴링으로 닫힘을 확인하되, 최종 판정은 added 를 우선한다.
        own_modal_open = True
        for _ in range(10):                 # ~5s grace (0.5s × 10)
            detail.wait_for_timeout(500)
            own_modal_open = bool(detail.evaluate(
                '() => !!document.querySelector(".alphaAdd__content-dropdown, .alphaAdd__content")'))
            if not own_modal_open:
                break
        modal_gone = not own_modal_open
        log(f'_add_alpha_to_list: added={added} modal_gone={modal_gone} url={detail.url}')
        return bool(added)
    except Exception as e:
        log(f'_add_alpha_to_list err: {e}')
        return False
    finally:
        try:
            if detail:
                detail.close()
        except Exception:
            pass


# ── 잉여 시뮬 탭 정리 ────────────────────────────────────────────────────────
# SEQUENTIAL 모드는 한 페이지에서 같은 탭 1개를 반복 사용. 3배치 시절 만들어진
# Simulation 2/3 잉여 탭이 페이지에 남아 WQB 의 동시 sim 슬롯을 점유하면
# 새 sim 클릭 시 "You have reached the limit of concurrent simulations" 배너로
# 거부됨. setup 진입 시 첫 탭만 남기고 나머지를 close — 단, sim 진행 중인 탭
# (tab-dot 가 running 클래스) 은 결과 손실 막기 위해 close 하지 않는다.
def close_extra_tabs(page, keep_label='Simulation 1'):
    try:
        info = page.evaluate(r'''(keep) => {
            const out = {before: [], closed: [], skipped_running: [], err: null};
            const tabs = [...document.querySelectorAll('.editor-tabs__tab-element')];
            for (const t of tabs) {
                const txtEl = t.querySelector('.editor-tabs__tab-text');
                const label = txtEl ? (txtEl.innerText||'').trim() : '';
                out.before.push(label);
                if (!label) continue;
                if (label === keep) continue;
                // 이 탭이 sim 돌고 있으면 (tab-dot--running) close 안 함.
                const dots = [...t.querySelectorAll('[class*="tab-dot"]')];
                const dot_classes = dots.map(d => (d.className||'')).join(' ');
                const running = /--running/.test(dot_classes);
                if (running) { out.skipped_running.push(label); continue; }
                // close 'x' 버튼 찾기 — selector 후보 여러 개 시도.
                const close_sels = [
                    '.editor-tabs__tab-close',
                    '[class*="tab-close"]',
                    '[class*="tab__close"]',
                    '[class*="close-icon"]',
                ];
                let closer = null;
                for (const sel of close_sels) {
                    const c = t.querySelector(sel);
                    if (c && c.offsetParent !== null) { closer = c; break; }
                }
                // selector fallback — 탭 내 SVG/icon 중 click 가능한 마지막 자식.
                if (!closer) {
                    const candidates = [...t.querySelectorAll('svg, i, [role="button"]')];
                    closer = candidates.find(c => c.offsetParent !== null) || null;
                }
                if (closer) {
                    try { closer.click(); out.closed.push(label); } catch(e) { out.err = e.message; }
                }
            }
            return out;
        }''', keep_label)
        # 항상 log — close=0 케이스에서도 page 의 탭 상태가 보이도록 (진단성).
        log(f'close_extra_tabs: before={info.get("before")} '
            f'closed={info.get("closed")} skipped_running={info.get("skipped_running")}')
        # close 후 confirm dialog 가 뜨는 경우 ('Are you sure?') — 'Yes' 클릭.
        if info.get('closed'):
            try:
                page.evaluate(r'''() => {
                    const btns = [...document.querySelectorAll('button, [role="button"]')];
                    for (const b of btns) {
                        const t = (b.innerText||'').trim().toLowerCase();
                        if (b.offsetParent !== null && /^(yes|confirm|ok|discard)$/.test(t)) {
                            try { b.click(); return; } catch(e) {}
                        }
                    }
                }''')
                page.wait_for_timeout(500)
            except Exception: pass
    except Exception as e:
        log(f'close_extra_tabs exception: {e}')

# ── simulator view 강제 복귀 ────────────────────────────────────────────────
# show_test_results / settings panel / IS test detail 이 페이지 view 를 바꿔놓고
# 자동 복귀를 안 해서 .editor__checkbox 도 .editor-tabs__tab-element 도 비는
# 케이스가 1배치 시퀀셜 실행에서 빈발. 매 setup 직전 한 번 사전 점검.
def _ensure_simulator_view(page):
    try:
        info = page.evaluate(r'''() => {
            const url = location.href || '';
            const body = (document.body && document.body.innerText) || '';
            const tabs = document.querySelectorAll('.editor-tabs__tab-element').length;
            const monaco = document.querySelectorAll('.monaco-editor').length;
            const modes = document.querySelectorAll('.editor__checkbox').length;
            const app_err = /Application Error Has Occurred|application encountered an error/i.test(body);
            return {url, tabs, monaco, modes, app_err};
        }''')
    except Exception as e:
        log(f'_ensure_simulator_view eval-err: {e}')
        return
    on_sim = ('/simulate' in info.get('url','') or '/simulator' in info.get('url','')
              or '/research/alpha' in info.get('url',''))
    ui_ok = (info.get('tabs', 0) > 0 and info.get('monaco', 0) > 0
             and info.get('modes', 0) > 0)
    app_err = bool(info.get('app_err'))
    if on_sim and ui_ok and not app_err:
        return
    log(f'_ensure_simulator_view: missing UI (tabs={info.get("tabs")} '
        f'monaco={info.get("monaco")} modes={info.get("modes")} app_err={app_err} '
        f'url={info.get("url","")[:80]!r}) — recovering')
    try:
        if app_err:
            # WQB 서버가 'An Application Error Has Occurred' 페이지를 띄운 상태 — goto 만으로는
            # SPA 가 같은 broken state 를 재현할 수 있어 hard reload 로 React tree 자체를 reset.
            page.reload(timeout=20000, wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            # 재확인 후 여전히 simulator UI 안 보이면 마지막으로 goto.
            info2 = page.evaluate(r'''() => ({
                tabs: document.querySelectorAll('.editor-tabs__tab-element').length,
                monaco: document.querySelectorAll('.monaco-editor').length,
                modes: document.querySelectorAll('.editor__checkbox').length,
            })''')
            if not (info2.get('tabs',0) > 0 and info2.get('monaco',0) > 0):
                page.goto('https://platform.worldquantbrain.com/simulate',
                          timeout=20000, wait_until='domcontentloaded')
                page.wait_for_timeout(3000)
        else:
            # 다른 모듈(plugins, research) 로 빠져있을 때 강제 복귀.
            page.goto('https://platform.worldquantbrain.com/simulate',
                      timeout=20000, wait_until='domcontentloaded')
            page.wait_for_timeout(2500)
    except Exception as e:
        log(f'_ensure_simulator_view recover-err: {e}')

def emit_partial(slot, status, error_text='', metrics=None, is_status=None,
                 submitted=False, submit_status=''):
    # 슬롯 1개 완료 시점 단위로 worker 에 stream — UI 가 batch 끝날 때까지 안 기다리고 받음.
    is_status = is_status or {'pass': [], 'fail': [], 'pending': []}
    payload = {
        'slot': slot + 1,
        'idx': indices[slot] if slot < len(indices) else slot + 1,
        'status': status,                   # 'pass' | 'fail' | 'error'
        'error_text': error_text or '',
        'metrics': metrics or {},
        'is_status': is_status,
        'submitted': bool(submitted),
        'submit_status': submit_status or '',  # 'submitted' | 'disabled' | 'not_found' | 'fail:*'
        'pass_count_estimate': _count_pass(metrics or {}),
    }
    print('[partial] ' + json.dumps(payload, ensure_ascii=False), flush=True)

def toggle_tutorial_checkbox(page):
    # WQB 시뮬 페이지 우상단의 'Code / Results / Learn / Data / Tutorial' mode switcher 처리.
    # 라디오 같은 single-select group. 활성 mode 는 `.editor__checkbox--selected` suffix class.
    # 목표: Tutorial mode 비활성 + Results mode 활성. (사용자 매뉴얼 동작: 'Tutorial 해제 후
    # Result 체크'). Results 가 활성화돼야 Simulate 버튼의 --disabled-example 가 풀린다.
    try:
        info = page.evaluate(r'''() => {
            const out = {modes: [], clicked_results: false, clicked_tutorial_off_via: '',
                         results_was_active: false, tutorial_was_active: false};
            const items = [...document.querySelectorAll('.editor__checkbox')];
            for (const el of items) {
                const cls = (el.className || '').toString();
                const label = (el.innerText || el.textContent || '').trim();
                const active = /--selected\b/.test(cls);
                out.modes.push({label, active, cls: cls.slice(0,100)});
                if (/^tutorial$/i.test(label) && active) out.tutorial_was_active = true;
                if (/^results?$/i.test(label) && active) out.results_was_active = true;
            }
            // 1) Tutorial 이 활성이면 다른 mode (Code 또는 Results) 클릭해서 해제.
            //    아래 2) 에서 Results 클릭하므로 자동 해제됨.
            // 2) Results 가 비활성이면 클릭.
            if (!out.results_was_active) {
                const res = items.find(el =>
                    /^results?$/i.test((el.innerText||'').trim()));
                if (res) {
                    try { res.click(); out.clicked_results = true; } catch(e) {}
                }
            }
            return out;
        }''')
        modes_str = ' | '.join(f"{m['label']}{'*' if m['active'] else ''}" for m in info.get('modes', []))
        log(f'toggle_tutorial_checkbox: modes=[{modes_str}] (* = active) | '
            f'tut_was_active={info.get("tutorial_was_active")} '
            f'res_was_active={info.get("results_was_active")} '
            f'clicked_results={info.get("clicked_results")}')
        return info
    except Exception as e:
        log(f'toggle_tutorial_checkbox exception: {e}')
        return {}


def js_dismiss_introjs(page):
    # WQB 신규/잠긴 계정에서 'intro-step-N' 으로 Simulate 버튼이 disabled-example 상태로
    # 잠기는 경우 — 이 상태에서는 클릭/Ctrl+Enter/nudge 모두 React state 를 못 풀어 sim 시작 불가.
    # 근본 해결: intro.js 의 Skip/Done 버튼 클릭 + overlay/clone 제거 + intro-step-* 클래스 제거.
    try:
        info = page.evaluate(r'''() => {
            const out = {clicked: [], removed: 0, classes_stripped: 0, introjs_exited: false};
            // 1) intro.js 표준 셀렉터로 skip/done/close 버튼 클릭.
            const sels = [
                '.introjs-skipbutton',
                '.introjs-donebutton',
                '.introjs-tooltip [class*="skip" i]',
                '.introjs-tooltip [class*="close" i]',
                '.introjs-tooltipbuttons [class*="skip" i]',
                'button[aria-label*="skip tutorial" i]',
                'button[aria-label*="exit tutorial" i]',
            ];
            for (const s of sels) {
                document.querySelectorAll(s).forEach(b => {
                    try {
                        if (b.offsetParent !== null) {
                            b.click(); out.clicked.push(s);
                        }
                    } catch(e) {}
                });
            }
            // 2) intro.js API 직접 호출 (앱이 export 했을 때).
            try {
                if (window.introJs) {
                    const inst = window.introJs();
                    if (inst && typeof inst.exit === 'function') {
                        inst.exit(true);
                        out.introjs_exited = true;
                    }
                }
            } catch(e) {}
            // 3) overlay / clone / helper layer 강제 제거.
            const overlayClasses = [
                '.introjs-overlay', '.introjs-helperLayer', '.introjs-tooltipReferenceLayer',
                '.introjs-tooltip', '.introjs-disableInteraction', '.introjs-fixParent',
            ];
            for (const c of overlayClasses) {
                document.querySelectorAll(c).forEach(el => {
                    try { el.remove(); out.removed++; } catch(e) {}
                });
            }
            // 4) 모든 elem 에서 'intro-step-N' / 'introjs-showElement' 같은 잔존 class 제거.
            //    이 클래스가 남아있으면 Simulate 버튼이 disabled-example 으로 유지됨.
            document.querySelectorAll('[class*="intro-step-"], [class*="introjs-"]').forEach(el => {
                try {
                    const orig = (el.className || '').toString();
                    const cleaned = orig
                        .replace(/\bintro-step-\d+\b/g, '')
                        .replace(/\bintrojs-[a-zA-Z0-9-]+\b/g, '')
                        .replace(/\s+/g, ' ').trim();
                    if (cleaned !== orig) {
                        el.className = cleaned;
                        out.classes_stripped++;
                    }
                } catch(e) {}
            });
            // 5) body 의 introjs-* 클래스 (introjs-showElement 등) 도 제거.
            try {
                document.body.className = (document.body.className || '')
                    .replace(/\bintrojs-[a-zA-Z0-9-]+\b/g, '').replace(/\s+/g,' ').trim();
            } catch(e) {}
            return out;
        }''')
        if info.get('clicked') or info.get('removed') or info.get('classes_stripped') \
                or info.get('introjs_exited'):
            log(f'js_dismiss_introjs: clicked={info.get("clicked")} removed={info.get("removed")} '
                f'classes_stripped={info.get("classes_stripped")} introjs_exited={info.get("introjs_exited")}')
    except Exception as e:
        log(f'js_dismiss_introjs exception: {e}')


def js_dismiss_overlays(page):
    # 신규 사용자: cookie consent, EU GDPR, 첫 로그인 welcome tour, sidebar onboarding 등
    # 다양한 modal/banner 를 한 번에 dismiss. 최소 2회 호출 권장 (modal 이 chain 으로 뜸).
    info = page.evaluate(r'''() => {
        const out = {clicked_labels: [], removed_overlays: 0, removed_dimmer: 0};
        // 단어 시작이 아니라 단어 단위로 매칭 (예: "Accept all cookies" 도 잡음).
        const POSITIVE = /\b(Skip|Got it|Exit|Continue|Close|Dismiss|OK|Okay|Accept|Agree|Allow|Done|Next|Later|Confirm|확인|동의|허용|닫기|건너뛰기|나중에|시작|계속|취소)\b/i;
        const COOKIE = /\b(Accept (all|cookies)|I (accept|agree)|Allow all|Reject all|Only necessary|쿠키 허용|모두 허용|필수만)\b/i;
        // WQB 의 onboarding-popup modal 안의 dismiss 버튼은 더 광범위한 label 가짐.
        const ONBOARD = /\b(Got it|Skip|Skip tour|No thanks|Dismiss|Close|Continue|Next|Start|Begin|Maybe later|Later)\b/i;
        const candidates = [
            ...document.querySelectorAll('button, a[role="button"], [role="button"], [class*="button" i]')
        ];
        for (const el of candidates) {
            try {
                if (el.offsetParent === null) continue;
                // onboarding-popup 내부 button 은 별도 처리 (안전 dismiss 만 click).
                if (el.closest && el.closest('.onboarding-popup, [class*="onboarding"]')) continue;
                const txt = ((el.innerText || el.textContent || '') + ' ' +
                             (el.getAttribute('aria-label') || '')).trim();
                if (!txt) continue;
                if (COOKIE.test(txt) || POSITIVE.test(txt) || ONBOARD.test(txt)) {
                    el.click();
                    out.clicked_labels.push(txt.slice(0, 30));
                }
            } catch(e) {}
        }
        // role=dialog 의 close 버튼도 시도.
        [...document.querySelectorAll('[role="dialog"] button[aria-label*="lose" i], [role="dialog"] [class*="close" i]')]
            .forEach(b => { try { if (b.offsetParent !== null) { b.click(); out.clicked_labels.push('dialog-close'); } } catch(e) {} });
        // WQB onboarding-popup modal — Semantic UI 의 .ui.modal.transition.visible.active
        // 그리고 .ui.page.modals.dimmer.transition.visible.active (전체 페이지 dimmer).
        // dismiss 버튼이 onboarding-popup__modal-section--buttons 안에 있음.
        // ★ SAFETY: 모든 버튼을 click 하면 'Apply to be a Research Consultant' 같은
        //   acquisition 버튼을 실수로 누를 수 있음 (실측 사고). 절대 SAFE 한 dismiss text 만 click.
        const SAFE_DISMISS = /^(skip(?:\s+(?:tour|tutorial|onboarding|for now))?|got it|no thanks|maybe later|later|close|dismiss|cancel|x)$/i;
        const ONBOARD_DANGER = /\b(Apply|Subscribe|Sign\s*up|Sign\s*in|Submit|Send|Buy|Purchase|Enroll|Register|Get\s*started)\b/i;
        const onboardingBtns = [...document.querySelectorAll(
            '.onboarding-popup__modal-section--buttons button, .onboarding-popup__modal--content button, .onboarding-popup button')]
            .filter(b => b.offsetParent !== null);
        for (const b of onboardingBtns) {
            try {
                const t = (b.innerText || '').trim();
                if (ONBOARD_DANGER.test(t)) {
                    out.clicked_labels.push('SKIP-DANGEROUS:' + t.slice(0,30));
                    continue;
                }
                if (SAFE_DISMISS.test(t)) {
                    b.click();
                    out.clicked_labels.push('onboarding:' + t.slice(0,30));
                }
            } catch(e) {}
        }
        // dismiss 가 안 통하는 경우 강제 제거 — onboarding modal + dimmer 둘 다 DOM 에서 빼냄.
        // (사용자 권한 없는 modal/dimmer 는 click 으로 못 닫지만 제거하면 페이지 사용 가능.)
        const overlaySelectors = [
            '.onboarding-popup', '.onboarding-popup__modal',
            '.ui.modal.transition.visible.active',
            '.ui.page.modals',  // Semantic UI 의 dimmer 컨테이너
            '.ui.dimmer.transition.visible.active',
            '[class*="onboarding-popup"]',
            '.modal.transition.visible',
        ];
        for (const sel of overlaySelectors) {
            try {
                document.querySelectorAll(sel).forEach(el => {
                    try { el.remove(); out.removed_overlays++; } catch(e) {}
                });
            } catch(e) {}
        }
        // body 의 dimmable 클래스도 제거 (Semantic UI dimmer 가 body 에 'dimmable dimmed' 추가).
        try {
            const before = document.body.className || '';
            document.body.className = before.replace(/\b(dimmable|dimmed|scrolling)\b/g, '').replace(/\s+/g,' ').trim();
            if (document.body.className !== before) out.removed_dimmer++;
        } catch(e) {}
        return out;
    }''')
    try:
        if isinstance(info, dict) and (info.get('clicked_labels') or info.get('removed_overlays')
                                        or info.get('removed_dimmer')):
            log(f'js_dismiss_overlays: clicked={info.get("clicked_labels")[:6]} '
                f'removed_overlays={info.get("removed_overlays")} '
                f'removed_dimmer={info.get("removed_dimmer")}')
    except Exception:
        pass


def detect_auth_block(page):
    # WQB 가 새 디바이스에서 추가 인증을 요구하는 경우 감지.
    # 반환: 'auth_required' | '' (정상)
    try:
        return page.evaluate(r'''() => {
            const t = (document.body.innerText || '').toLowerCase();
            const URL = (location.href || '').toLowerCase();
            // 2FA / verification code / new device 인증 페이지 패턴.
            const patterns = [
                'verification code', 'verify your identity', 'two-factor',
                'two factor', 'mfa code', 'authenticator', 'security code',
                'new device', 'unrecognized device', 'we sent you',
                '2단계', '인증 코드', '본인 확인', '디바이스',
            ];
            for (const p of patterns) {
                if (t.includes(p)) return 'auth_required';
            }
            if (/\/(verify|2fa|mfa|otp|challenge)/i.test(URL)) return 'auth_required';
            return '';
        }''')
    except Exception:
        return ''

def get_tab_labels(page):
    return page.evaluate(r'''() => {
        const out = [];
        const tabs = [...document.querySelectorAll('.editor-tabs__tab-element')];
        tabs.forEach(el => {
            const text_el = el.querySelector('.editor-tabs__tab-text');
            const txt = text_el ? (text_el.innerText || '').trim() : '';
            if (/^Simulation\s+\d+$/.test(txt) && el.offsetParent !== null) {
                const cls = el.className || '';
                const dot_classes = [...el.querySelectorAll('[class*="tab-dot"]')]
                    .map(d => d.className || '').join(' ');
                const running = /--running|tab-dot--/.test(dot_classes) && !/--idle/.test(dot_classes);
                const has_error = /--error|--fail/.test(cls + ' ' + dot_classes);
                out.push({label: txt, active: cls.includes('--active'),
                          running: running, has_error: has_error});
            }
        });
        return out;
    }''')

def click_tab(page, label):
    try:
        loc = page.locator('.editor-tabs__tab-element').filter(has_text=label).first
        loc.click(timeout=8000)
        log(f'step: click_tab ok (locator) label={label!r}')
        return True
    except Exception as e:
        ok = page.evaluate(r'''(label) => {
            const tabs = [...document.querySelectorAll('.editor-tabs__tab-element')];
            for (const t of tabs) {
                const txt = t.querySelector('.editor-tabs__tab-text');
                if (txt && (txt.innerText || '').trim() === label && t.offsetParent !== null) {
                    const inner = t.querySelector('.editor-tabs__tab-inside-element');
                    (inner || t).click();
                    return true;
                }
            }
            return false;
        }''', label)
        log(f'step: click_tab {"ok" if ok else "FAIL"} (js fallback) label={label!r} err={str(e)[:80]}')
        if not ok:
            _dump_failure(page, f'click_tab_{label}')
        return ok

def click_new_tab(page):
    # WQB 의 '+' 는 dropdown 트리거 → 클릭 시 메뉴가 뜨고 그 메뉴 안에서 'New' / 'Add' / 'Blank'
    # 같은 옵션을 다시 클릭해야 새 시뮬 탭이 생성됨.
    # 1) 드롭다운 트리거 클릭 → 2) 메뉴 옵션 클릭 → 메뉴 외부 클릭으로 닫기.
    info = page.evaluate(r'''() => {
        const all_tabs = [...document.querySelectorAll('.editor-tabs__tab-element')];
        const before_n = all_tabs.length;
        const tab_classes = all_tabs.map(t => (t.className||'').slice(0, 80)).join(' | ');
        // 1) 명시 selector 우선.
        const sels = [
            '.editor-tabs__tab-add',
            '[class*="tab-add"]',
            '[class*="add-tab"]',
            '[class*="tab__add"]',
            '[class*="new-tab"]',
        ];
        let target = null;
        let how = '';
        for (const sel of sels) {
            const list = [...document.querySelectorAll(sel)];
            for (const c of list) {
                if (c.offsetParent !== null) { target = c; how = 'sel:' + sel; break; }
            }
            if (target) break;
        }
        // 2) 시뮬 탭 컨테이너 ancestor 안에서 '+' 텍스트 찾기.
        if (!target) {
            const tabs = document.querySelectorAll('.editor-tabs__tab-element');
            if (tabs.length > 0) {
                let p = tabs[0].parentElement;
                for (let i = 0; i < 4 && p; i++) {
                    const candidates = [...p.querySelectorAll('*')];
                    for (const c of candidates) {
                        if (c.children.length > 0) continue;
                        if (c.offsetParent === null) continue;
                        const t = (c.textContent || '').trim();
                        if (t === '+') { target = c; how = 'plus-text-near-tabs'; break; }
                    }
                    if (target) break;
                    p = p.parentElement;
                }
            }
        }
        // 3) aria-label / title fallback.
        if (!target) {
            const all = [...document.querySelectorAll('button, [role="button"]')];
            for (const c of all) {
                if (c.offsetParent === null) continue;
                const aria = (c.getAttribute('aria-label')||'').toLowerCase();
                const title = (c.getAttribute('title')||'').toLowerCase();
                if (/(add|new).*(tab|sim|simulation)/.test(aria + ' ' + title)) {
                    target = c; how = 'aria:' + (aria||title); break;
                }
            }
        }
        // 4) 마지막 폴백 — 페이지 전체의 '+' 텍스트.
        if (!target) {
            const all = [...document.querySelectorAll('button, [role="button"], div, span')];
            for (const c of all) {
                if (c.children.length > 0) continue;
                if (c.offsetParent === null) continue;
                const t = (c.textContent || '').trim();
                if (t === '+') { target = c; how = 'plus-text-anywhere'; break; }
            }
        }
        if (!target) return {ok: false, how: 'no-match', before_n, tab_classes, dropdown_clicked: false, option_clicked: ''};
        // 1단계: 드롭다운 트리거 클릭. '__button' 노드를 우선 (직접 click target).
        let trigger = target.querySelector('.editor-tabs__new-tab-dropdown-element__button')
                   || target.querySelector('.editor-tabs__new-tab-icon')
                   || target;
        try { trigger.click(); } catch(e) {}
        try { trigger.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true})); } catch(e) {}
        return {ok: true, how, before_n, tab_classes, trigger_outer: (trigger.outerHTML||'').slice(0,200)};
    }''')
    if not info or not info.get('ok'):
        return info or {'ok': False, 'how': 'eval-error', 'before_n': 0}
    # 2단계: 드롭다운 메뉴가 뜨길 잠시 기다림 → 옵션 클릭.
    page.wait_for_timeout(600)
    pick = page.evaluate(r'''() => {
        // 메뉴 옵션 후보 — 메뉴 안의 클릭 가능한 항목.
        // WQB UI 에 '+' dropdown 옵션은 보통 'New Blank Alpha' / 'Clone' / 'Paste' 등.
        // 'New Blank' 류를 우선, 없으면 가장 가벼운 'New' 키워드 매칭.
        const RX_PRIMARY = /\b(new\s+(blank|empty|simulation|alpha)|blank\s+alpha|add\s+(blank|new|simulation))\b/i;
        const RX_FALLBACK = /\b(new|blank|empty|add)\b/i;
        const RX_AVOID = /\b(clone|paste|import|template|sample|tutorial)\b/i;
        const cands = [...document.querySelectorAll(
            '[class*="dropdown"] [role="menuitem"], [class*="dropdown"] li, '
            + '[class*="dropdown"] button, [class*="dropdown"] a, '
            + '[class*="menu"] [role="menuitem"], [class*="menu"] li, '
            + '[class*="menu"] button, [class*="menu"] a, '
            + '[class*="new-tab"] [role="menuitem"], [class*="new-tab"] [class*="option"], '
            + '[class*="new-tab"] [class*="item"]'
        )];
        for (const el of cands) {
            try {
                if (el.offsetParent === null) continue;
                const t = (el.innerText||'').trim();
                if (!t) continue;
                if (RX_PRIMARY.test(t)) {
                    el.click();
                    return {clicked: true, label: t.slice(0,80), match: 'primary'};
                }
            } catch(e) {}
        }
        for (const el of cands) {
            try {
                if (el.offsetParent === null) continue;
                const t = (el.innerText||'').trim();
                if (!t) continue;
                if (RX_AVOID.test(t)) continue;
                if (RX_FALLBACK.test(t)) {
                    el.click();
                    return {clicked: true, label: t.slice(0,80), match: 'fallback'};
                }
            } catch(e) {}
        }
        return {clicked: false, candidates: cands.slice(0,5).map(c => ((c.innerText||'').trim().slice(0,40)))};
    }''')
    info['option_clicked'] = pick.get('label', '') if pick.get('clicked') else ''
    info['option_match'] = pick.get('match', '')
    if not pick.get('clicked'):
        info['menu_candidates'] = pick.get('candidates', [])
    return info

def wait_editor_ready(page, timeout_ms=20000):
    try:
        page.wait_for_function('''() => {
            const ed = document.querySelector('.monaco-editor');
            if (!ed || ed.offsetParent === null) return false;
            return !!ed.querySelector('textarea.inputarea');
        }''', timeout=timeout_ms)
        return True
    except PWTimeout:
        return False

def get_editor_text(page):
    return page.evaluate(r'''() => {
        const ed = document.querySelector('.monaco-editor');
        if (!ed) return '';
        const lines = [...ed.querySelectorAll('.view-line')].map(l => l.innerText || '');
        return lines.join('\n').trim();
    }''')

def set_editor_text(page, formula):
    log(f'step: set_editor_text begin len={len(formula)} preview={formula[:60]!r}')
    try:
        ta = page.locator('.monaco-editor textarea.inputarea').first
        try:
            page.locator('.monaco-editor .view-lines').first.click(timeout=4000)
        except Exception:
            pass
        ta.focus(timeout=5000)

        for _ in range(3):
            page.keyboard.press('Control+A')
            page.keyboard.press('Delete')
            cur = get_editor_text(page)
            if not cur or len(cur) <= 1:
                break
            page.keyboard.press('Control+End')
            page.keyboard.press('Control+Shift+Home')
            page.keyboard.press('Delete')
            cur = get_editor_text(page)
            if not cur or len(cur) <= 1:
                break
            for _ in range(min(len(cur) + 5, 600)):
                page.keyboard.press('Backspace')
            cur = get_editor_text(page)
            if not cur or len(cur) <= 1:
                break

        cur = get_editor_text(page)
        if cur and len(cur) > 2:
            log(f'step: set_editor_text FAIL (not empty after clear): {cur[:60]!r}')
            return False
        page.keyboard.insert_text(formula)
        # WQB Simulate 버튼이 'editor-simulate-button-text--disabled-example' (튜토리얼
        # 예시 코드 상태) 로 잠긴 채라면, 단순 insert_text 만으로는 React 가 "사용자 입력"
        # 으로 인식 못 해 버튼이 안 풀린다. 실제 keystroke (space → backspace) 를 추가로
        # 보내 React state 를 강제로 갱신.
        try:
            page.keyboard.press('End')
            page.keyboard.press(' ')
            page.keyboard.press('Backspace')
        except Exception:
            pass
        # 추가 보강: monaco editor 의 model.setValue() 를 직접 호출 → monaco 가 내부적으로
        # change 이벤트를 발화시켜 React 가 listen 하는 onChange/onDidChangeModelContent 가
        # trigger 된다. 이렇게 해야 React 가 "사용자가 진짜 입력했음" 으로 인식하고
        # --disabled-example state 를 풀어준다. keyboard.insert_text 만으로는 keydown
        # 이벤트만 fire 되고 model 변경 이벤트가 발화 안 될 수 있음.
        # WQB 가 monaco 를 production bundle 에 inline 했을 가능성 있어 window.monaco 가
        # 없을 수 있음 — 폴백으로 native textarea value setter + InputEvent dispatch (React
        # 의 onChange listener 가 input 이벤트만 listen 하는 controlled component pattern).
        try:
            ok = page.evaluate(r'''(formula) => {
                const out = {steps: []};
                // 1) window.monaco 가 있으면 setValue 시도.
                if (window.monaco && window.monaco.editor) {
                    try {
                        const editors = window.monaco.editor.getEditors() || [];
                        for (const ed of editors) {
                            try {
                                const dom = ed.getDomNode();
                                if (!dom || dom.offsetParent === null) continue;
                                ed.setValue(formula);
                                out.steps.push('monaco-setValue');
                                break;
                            } catch(e) { out.steps.push('monaco-err:' + String(e).slice(0,40)); }
                        }
                    } catch(e) { out.steps.push('monaco-getEditors-err'); }
                } else {
                    out.steps.push('no-window-monaco');
                    // monaco namespace 가 다른 곳에 있는지 탐색.
                    const monacoKeys = Object.keys(window).filter(k => /monaco/i.test(k));
                    if (monacoKeys.length) out.monaco_keys = monacoKeys.slice(0, 5);
                }
                // 2) 폴백: 보이는 .monaco-editor 의 textarea 에 native value setter +
                //    InputEvent dispatch. React 가 textarea 의 input 이벤트를 listen
                //    하는 controlled component 패턴일 때 동작.
                const eds = [...document.querySelectorAll('.monaco-editor')]
                    .filter(el => el.offsetParent !== null);
                if (eds.length) {
                    const ta = eds[0].querySelector('textarea.inputarea');
                    if (ta) {
                        try {
                            const proto = window.HTMLTextAreaElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                            if (setter && setter.set) {
                                // 한 글자 추가 → input 이벤트 → 한 글자 제거 → 다시 input 이벤트.
                                // 이렇게 두 번 발화시켜 React 가 dirty change 를 확실히 인식.
                                const orig = ta.value || '';
                                setter.set.call(ta, orig + ' ');
                                ta.dispatchEvent(new InputEvent('input', {
                                    bubbles: true, inputType: 'insertText', data: ' '
                                }));
                                setter.set.call(ta, orig);
                                ta.dispatchEvent(new InputEvent('input', {
                                    bubbles: true, inputType: 'deleteContentBackward', data: null
                                }));
                                out.steps.push('native-setter-fire');
                            } else {
                                out.steps.push('no-native-setter');
                            }
                        } catch(e) { out.steps.push('native-setter-err:' + String(e).slice(0,40)); }
                    } else {
                        out.steps.push('no-textarea');
                    }
                } else {
                    out.steps.push('no-visible-monaco-dom');
                }
                return out;
            }''', formula)
            log(f'step: set_editor_text monaco-fire {ok}')
        except Exception as e:
            log(f'step: set_editor_text monaco-fire exception: {e}')
        log(f'step: set_editor_text done')
        return True
    except Exception as e:
        log(f'step: set_editor_text EXCEPTION: {e}')
        return False

def _ensure_results_mode(page):
    # WQB 우상단 mode switcher 강제: TUTORIAL active 면 끄고 RESULTS active 면 둠.
    # click_simulate 직전마다 호출 — WQB 가 어떤 트리거로 TUTORIAL 을 다시 켜는 race 차단.
    try:
        info = page.evaluate(r'''() => {
            const items = [...document.querySelectorAll('.editor__checkbox')];
            const out = {tut_off: false, res_on: false, before: [], after: []};
            for (const el of items) {
                const lbl = (el.innerText || '').trim();
                const act = /--selected\b/.test((el.className||'').toString());
                out.before.push(lbl + (act?'*':''));
            }
            // 1) TUTORIAL active 면 click 해서 deactivate.
            for (const el of items) {
                if (/^tutorial$/i.test((el.innerText||'').trim())
                        && /--selected\b/.test((el.className||'').toString())) {
                    try { el.click(); out.tut_off = true; } catch(e) {}
                }
            }
            // 2) RESULTS inactive 면 click 해서 activate.
            for (const el of items) {
                if (/^results?$/i.test((el.innerText||'').trim())
                        && !/--selected\b/.test((el.className||'').toString())) {
                    try { el.click(); out.res_on = true; } catch(e) {}
                }
            }
            for (const el of items) {
                const lbl = (el.innerText || '').trim();
                const act = /--selected\b/.test((el.className||'').toString());
                out.after.push(lbl + (act?'*':''));
            }
            return out;
        }''')
        if info.get('tut_off') or info.get('res_on'):
            log(f'_ensure_results_mode: before=[{",".join(info.get("before",[]))}] '
                f'tut_off={info.get("tut_off")} res_on={info.get("res_on")} '
                f'after=[{",".join(info.get("after",[]))}]')
    except Exception as e:
        log(f'_ensure_results_mode exception: {e}')


def click_simulate(page):
    # Simulate 버튼이 'editor-simulate-button-text--disabled-example' 등 disabled 클래스로
    # 잠겨있으면 새 sim 못 시작. monaco editor 에 'space + backspace' 입력해서 *editing 상태*
    # 트리거 → React 가 disabled 풀고 enabled 로 전환.
    # ── 진입 시 already-running 체크 (false-fail 차단) ──
    # WQB 는 sim 중에도 button label='Simulate' 그대로 두고 진행 표시는 우측 패널의
    # '10% / Click here to cancel the simulation' + progress bar 로만 한다. 우리는 그걸
    # extract_state.running 으로 감지하므로, 진입 시 이미 running 이면 새 클릭 안 함.
    try:
        gate = page.evaluate(r'''() => {
            const body = (document.body && document.body.innerText) || '';
            // concurrent-limit 은 WQB 의 *특정* 에러 문구 — body 매치해도 오탐 소스 아님.
            const concurrent_limit = /limit of concurrent simulations|reached the limit of concurrent/i.test(body);

            // ── 'sim 진행 중' 신호는 전부 element-scoped ──
            // (구버전 버그: body innerText 전체에 /simulating|simulations usually take|
            //  cancel the simulation/ 정규식 → WQB static UI 텍스트에 영구 오탐 →
            //  매 idx inflight=True 로 굳어 Simulate 영구 skip → 직전 결과 패널을
            //  새 결과로 재보고. 그래서 body 전체 텍스트 매치 전면 폐기.)

            // 1) 진행 표시줄 element + live % (가장 신뢰도 높음).
            const pb = document.querySelector('[class*="editor-simulate__progress"], [class*="simulate-progress"], [class*="progress-bar"]');
            let pb_pct = false;
            if (pb && pb.offsetParent !== null) {
                const ptxt = (pb.innerText||'') + ' ' + (pb.textContent||'');
                pb_pct = /\d+\s*%/.test(ptxt);
            }
            // 2) --running 탭-dot element 존재.
            const running_dot = !!document.querySelector(
                '.editor-tabs__tab-dot--running, [class*="tab-dot--running"], [class*="--running"]');
            // 3) Simulate 버튼 라벨이 cancel/stop/running 으로 바뀜 (버튼 element 한정).
            const btn_cancel_label = [...document.querySelectorAll(
                    'button.editor-simulate-button-text, button[class*="editor-simulate-button"]')]
                .filter(b => b.offsetParent !== null)
                .some(b => /cancel|stop|running/i.test((b.innerText||'').trim()));
            // 4) 'cancel the simulation' 링크/영역 — element *자체* 텍스트만 검사.
            //    static UI 잔재 오탐 방지: clickable/link-ish + 짧은(<120자) element 한정.
            const CANCEL_RX = /cancel\s+the\s+simulation|\bcancel\s*sim\b|\bstop\s*sim\b/i;
            const visible_cancel_link = [...document.querySelectorAll(
                    'a, button, [role="button"], [class*="cancel"], [class*="editor-simulate"]')]
                .some(el => {
                    if (el.offsetParent === null) return false;
                    const t = (el.innerText || '').trim();
                    return t.length > 0 && t.length < 120 && CANCEL_RX.test(t);
                });

            return {concurrent_limit, pb_pct, running_dot, btn_cancel_label,
                    visible_cancel_link, body_head: body.slice(0, 120)};
        }''')
        pb_pct          = bool(gate.get('pb_pct'))
        running_dot     = bool(gate.get('running_dot'))
        btn_cancel      = bool(gate.get('btn_cancel_label'))
        cancel_link     = bool(gate.get('visible_cancel_link'))
        concurrent_limit = bool(gate.get('concurrent_limit'))
        # ── '시뮬레이션 진행 중' 판정 (정책: Signals + scoped cancel-link) ──
        # 진짜 live element 신호만 사용 — 진행표시줄% / --running 탭dot /
        # 버튼 cancel 라벨 / scoped cancel-the-simulation 링크. body 텍스트 불사용.
        sim_inflight = pb_pct or running_dot or btn_cancel or cancel_link
        # 진단성 — 매 호출 시 신호 breakdown 1줄 log.
        log(f'step: click_simulate gate inflight={sim_inflight} '
            f'pb_pct={pb_pct} running_dot={running_dot} btn_cancel={btn_cancel} '
            f'cancel_link={cancel_link} concurrent_limit={concurrent_limit} '
            f'body_head={gate.get("body_head","")[:60]!r}')
        if concurrent_limit:
            log('step: click_simulate skip — concurrent simulations limit reached')
            # 호출자 (_start_sim) 의 16x1.5s polling 동안 running 으로 인식되어 정상 path.
            return 1
        if sim_inflight:
            log('step: click_simulate skip — sim already in progress')
            return 1
    except Exception as e:
        log(f'step: click_simulate gate-err: {e}')
    # 매 click 직전에 mode-switcher 가 Results 로 잠겼는지 보장 (TUTORIAL 자동 복원 race 차단).
    _ensure_results_mode(page)
    # WQB onboarding-popup modal 이 떠있으면 button 위에 dimmer 가 덮여 클릭이 dimmer 에
    # 흡수됨 (FAIL-diag 에서 elementFromPoint 가 'DIV.ui page modals dimmer ...' 잡힌 패턴).
    # 매 click 직전에 modal/dimmer 강제 제거.
    try: js_dismiss_overlays(page)
    except Exception: pass
    # 후속 sim 실패 패턴: button class 에 'intro-step-N' 가 박혀있어 intro.js 가 click 을
    # 가로채는 케이스 (FAIL-diag 에서 btn_class='intro-step-5 editor-simulate-button-text ...'
    # 패턴). disabled-example 가 detect 안 돼도 intro-step 만 있어도 sim 시작 안 됨.
    # 매 click 직전에 무조건 intro.js exit + button class 에서 intro-step-* 제거.
    try: js_dismiss_introjs(page)
    except Exception: pass
    try:
        page.evaluate(r'''() => {
            const btns = document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
            for (const b of btns) {
                try {
                    const orig = (b.className||'').toString();
                    const cleaned = orig
                        .replace(/\bintro-step-\d+\b/g, '')
                        .replace(/\bintrojs-[a-zA-Z0-9-]+\b/g, '')
                        .replace(/\s+/g, ' ').trim();
                    if (cleaned !== orig) b.className = cleaned;
                } catch(e) {}
            }
        }''')
    except Exception: pass
    try:
        is_disabled_example = page.evaluate(r'''() => {
            const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
            if (!btn) return false;
            const cls = (btn.className || '').toString();
            return /disabled-example|--disabled\b/.test(cls) || btn.disabled === true;
        }''')
        if is_disabled_example:
            log('step: click_simulate detected disabled-example, nudging editor + intro.js exit')
            # 1) intro.js 잔존물이 React state 를 잠그고 있을 수 있으니 매번 강제 종료.
            try:
                js_dismiss_introjs(page)
            except Exception:
                pass
            try:
                page.locator('.monaco-editor textarea.inputarea').first.focus(timeout=2000)
                page.keyboard.press('End')
                page.keyboard.press(' ')
                page.keyboard.press('Backspace')
                page.wait_for_timeout(700)
            except Exception:
                pass
            # 그래도 disabled 면 더 강한 nudge — Ctrl+End → 새 라인 → 백스페이스 → 클릭 본문.
            still_disabled = page.evaluate(r'''() => {
                const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
                if (!btn) return false;
                return /disabled-example|--disabled\b/.test((btn.className||'').toString()) || btn.disabled === true;
            }''')
            if still_disabled:
                log('step: click_simulate still disabled after nudge, deeper trigger')
                try:
                    page.locator('.monaco-editor .view-lines').first.click(timeout=2000)
                    page.wait_for_timeout(300)
                    page.keyboard.press('Control+End')
                    page.keyboard.press('Enter')
                    page.keyboard.press('Backspace')
                    page.keyboard.type(' ')
                    page.keyboard.press('Backspace')
                    page.wait_for_timeout(900)
                except Exception:
                    pass
    except Exception:
        pass

    # 1) playwright locator click — React 의 mouse 이벤트 전체 (mousedown/up/click) 보냄.
    locator_clicked = 0
    try:
        loc = page.locator('button.editor-simulate-button-text, button[class*="editor-simulate-button"]').first
        if loc.is_visible(timeout=2000) and loc.is_enabled(timeout=1000):
            loc.click(timeout=4000)
            locator_clicked = 1
            log(f'step: click_simulate via locator ok')
    except Exception as e:
        log(f'step: click_simulate locator fail: {str(e)[:80]}')

    # 2) JS click fallback (이미 사용했던 방법).
    info = page.evaluate(r'''() => {
        const btns = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"]')];
        let clicked = 0;
        let visible = 0;
        let disabled = 0;
        const before_labels = btns.filter(b => b.offsetParent !== null).map(b => (b.innerText||'').trim().slice(0,30));
        const outer = btns.filter(b => b.offsetParent !== null).map(b => (b.outerHTML||'').slice(0,200));
        btns.forEach(b => {
            if (b.offsetParent !== null) visible++;
            if (b.disabled) disabled++;
            try {
                if (b.offsetParent !== null && !b.disabled) { b.click(); clicked++; }
            } catch(e) {}
        });
        return {clicked, visible, disabled, total: btns.length, before_labels, outer};
    }''')
    log(f'step: click_simulate js clicked={info.get("clicked",0)} visible={info.get("visible",0)} disabled={info.get("disabled",0)} total={info.get("total",0)} labels={info.get("before_labels")}')
    if info.get("clicked", 0) == 0 and info.get("visible", 0) > 0:
        log(f'step: click_simulate diag outerHTML={info.get("outer")}')

    # 3) keyboard shortcut fallback — Ctrl+Enter 가 monaco editor focus 일 때 sim 트리거.
    if locator_clicked == 0 and info.get("clicked", 0) == 0:
        try:
            page.keyboard.press('Control+Enter')
            log(f'step: click_simulate via keyboard Ctrl+Enter')
        except Exception:
            pass

    # 4) Force-fire fallback — disabled-example 가 React state 에 영구 박혀있는 WQB 계정 대응.
    #    버튼의 React onClick handler 를 __reactProps$* 통해 직접 invoke. disabled HTML 속성
    #    + class 도 제거해서 onClick 내부의 if(disabled)return 가드까지 우회. 마지막 수단.
    if locator_clicked == 0 and info.get("clicked", 0) == 0:
        try:
            forced = page.evaluate(r'''() => {
                const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
                if (!btn) return {ok: false, why: 'no-btn'};
                try {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.classList.remove('editor-simulate-button-text--disabled',
                                          'editor-simulate-button-text--disabled-example');
                } catch(e) {}
                // React onClick 핸들러 직접 호출. React 가 button 에 __reactProps$<key> 로
                // 핸들러를 붙여놓음 (production build 도 동일).
                const propsKey = Object.keys(btn).find(k => k.startsWith('__reactProps$'));
                if (propsKey) {
                    const p = btn[propsKey];
                    if (p && typeof p.onClick === 'function') {
                        try {
                            p.onClick({
                                preventDefault: () => {}, stopPropagation: () => {},
                                currentTarget: btn, target: btn, type: 'click', bubbles: true,
                            });
                            return {ok: true, via: 'react-onClick', keys: Object.keys(p).slice(0,8)};
                        } catch(e) { return {ok: false, why: 'react-onClick-err: ' + e.message}; }
                    }
                    return {ok: false, why: 'no-onClick', keys: Object.keys(p||{}).slice(0,8)};
                }
                // 폴백: native click() 그래도 시도.
                try { btn.click(); return {ok: true, via: 'native-click'}; }
                catch(e) { return {ok: false, why: 'native-err: ' + e.message}; }
            }''')
            log(f'step: click_simulate force-fire {forced}')
            if forced and forced.get('ok'):
                # 강제 발사 성공이면 클릭 카운트에 더해주기.
                info['clicked'] = max(info.get('clicked', 0), 1)
        except Exception as e:
            log(f'step: click_simulate force-fire exception: {e}')

    # 진단 — click 후 1초 뒤 sim 버튼이 'Cancel'/'Stop'/'Running' 로 변경됐는지 확인.
    page.wait_for_timeout(1500)
    post_running = False
    try:
        post = page.evaluate(r'''() => {
            const btns = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"], button')];
            const labels = btns.filter(b => b.offsetParent !== null
                && /simulate|cancel|stop|running/i.test((b.innerText||'').trim()))
                .map(b => (b.innerText||'').trim().slice(0,40));
            const body = (document.body.innerText || '');
            // 시뮬 버튼 자체가 'Cancel' / 'Stop' / 'Running' 라벨로 바뀌었으면 sim 시작된 것.
            const btn_started = labels.some(l => /cancel|stop|running/i.test(l));
            return {
                url: location.href.slice(0, 80),
                sim_buttons: labels,
                running_detected: btn_started
                    || /\bcancel sim|stop sim|simulating|sim running/i.test(body),
                error_detected: /session expired|please log in|unauthorized|server error|503|504/i.test(body),
                // 실제 WQB sim 진행 UI = 진행률 위젯에 'N%' 표시 + "Click here to
                // cancel the simulation" 링크. 일반 spinner/loading 클래스(streak
                // bar 등)로 인한 오탐을 피하려 이 두 신호만 신뢰.
                progress_visible: (function () {
                    const pb = [...document.querySelectorAll(
                        '[class*="progress" i], [class*="simulate" i]')]
                        .filter(e => e.offsetParent !== null);
                    const pctHit = pb.some(e => /\d+\s*%/.test(
                        (e.innerText || '') + ' ' + (e.textContent || '')));
                    const cancelHit = /cancel\s+the\s+simulation|cancel\s+the\s+sim\b/i
                        .test(body);
                    return pctHit || cancelHit;
                })(),
            };
        }''')
        log(f'step: click_simulate post sim_buttons={post.get("sim_buttons")} running={post.get("running_detected")} progress={post.get("progress_visible")} error={post.get("error_detected")}')
        # progress_visible(=N% 위젯/취소 링크)도 '진행 중' 의 신뢰 신호.
        # 기존엔 running_detected(버튼 라벨/regex)만 봐서, WQB 의 'N% + Click
        # here to cancel the simulation' UI 를 못 잡고 sim 이 도는데도 FAIL-diag
        # +스크린샷 덤프가 오발(거짓 경보) → 정상 신호로 합산.
        post_running = bool(post.get('running_detected')) or bool(post.get('progress_visible'))
    except Exception as e:
        log(f'step: click_simulate post diag exception: {e}')

    # 5) Post-check 가 running=False 면 DOM 클릭은 됐지만 React 가 sim 을 안 띄운 상태.
    #    Tutorial / example state 가 onClick 안에서 early-return 한 케이스 — React 의
    #    onClick prop 을 __reactProps$* 통해 직접 invoke 해서 disabled-guard 까지 우회.
    #    (이전엔 locator/JS click 둘 다 0 일 때만 force-fire 했는데, "DOM 은 clicked=1 인데
    #    sim 안 시작" 케이스가 빈발 → 매번 한 번 더 보장.)
    if not post_running:
        try:
            js_dismiss_introjs(page)
        except Exception:
            pass
        try:
            _ensure_results_mode(page)
        except Exception:
            pass
        try:
            forced2 = page.evaluate(r'''() => {
                const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
                if (!btn) return {ok: false, why: 'no-btn'};
                try {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.classList.remove('editor-simulate-button-text--disabled',
                                          'editor-simulate-button-text--disabled-example');
                } catch(e) {}
                const propsKey = Object.keys(btn).find(k => k.startsWith('__reactProps$'));
                if (propsKey) {
                    const p = btn[propsKey];
                    if (p && typeof p.onClick === 'function') {
                        try {
                            p.onClick({
                                preventDefault: () => {}, stopPropagation: () => {},
                                currentTarget: btn, target: btn, type: 'click', bubbles: true,
                            });
                            return {ok: true, via: 'react-onClick-recover'};
                        } catch(e) { return {ok: false, why: 'react-err: ' + e.message}; }
                    }
                    return {ok: false, why: 'no-onClick'};
                }
                try { btn.click(); return {ok: true, via: 'native-recover'}; }
                catch(e) { return {ok: false, why: 'native-err: ' + e.message}; }
            }''')
            log(f'step: click_simulate post-recovery force-fire {forced2}')
            if forced2 and forced2.get('ok'):
                page.wait_for_timeout(1500)
                try:
                    post2 = page.evaluate(r'''() => {
                        const btns = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"], button')];
                        const labels = btns.filter(b => b.offsetParent !== null
                            && /simulate|cancel|stop|running/i.test((b.innerText||'').trim()))
                            .map(b => (b.innerText||'').trim().slice(0,40));
                        const body = (document.body.innerText || '');
                        const btn_started = labels.some(l => /cancel|stop|running/i.test(l));
                        return {sim_buttons: labels, running_detected: btn_started
                            || /\bcancel sim|stop sim|simulating|sim running/i.test(body)};
                    }''')
                    post_running = bool(post2.get('running_detected'))
                    log(f'step: click_simulate post-recovery check sim_buttons={post2.get("sim_buttons")} running={post_running}')
                except Exception:
                    pass
        except Exception as e:
            log(f'step: click_simulate post-recovery exception: {e}')

    # 6) Post-recovery 의 force-fire 도 막혔으면 (React props.disabled 가드로 onClick 본문이
    #    early-return) — 마지막 수단: monaco editor 에 focus 후 Ctrl+Enter shortcut.
    #    WQB 는 monaco editor command 로 sim 단축키를 등록하므로, button 의 disabled 가드를
    #    우회한다. 또한 그 전에 monaco.editor.getEditors()[0].setValue() 로 model 변경 이벤트
    #    재발화 → React 가 dirty state 인식 → --disabled-example 풀림.
    if not post_running:
        try:
            page.evaluate(r'''() => {
                if (!window.monaco || !window.monaco.editor) return;
                const eds = window.monaco.editor.getEditors() || [];
                for (const ed of eds) {
                    try {
                        const dom = ed.getDomNode();
                        if (!dom || dom.offsetParent === null) continue;
                        const cur = ed.getValue();
                        // setValue 를 동일한 값으로 다시 호출 → onDidChangeModelContent fire.
                        ed.setValue(cur);
                        ed.focus();
                        const ta = dom.querySelector('textarea.inputarea');
                        if (ta) {
                            try { ta.dispatchEvent(new Event('input', {bubbles: true})); } catch(e) {}
                        }
                        return;
                    } catch(e) {}
                }
            }''')
            page.locator('.monaco-editor textarea.inputarea').first.focus(timeout=2000)
            page.wait_for_timeout(300)
            page.keyboard.press('Control+Enter')
            log('step: click_simulate post-recovery Ctrl+Enter shortcut')
            page.wait_for_timeout(2000)
            try:
                post3 = page.evaluate(r'''() => {
                    const btns = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"], button')];
                    const labels = btns.filter(b => b.offsetParent !== null
                        && /simulate|cancel|stop|running/i.test((b.innerText||'').trim()))
                        .map(b => (b.innerText||'').trim().slice(0,40));
                    const body = (document.body.innerText || '');
                    const btn_started = labels.some(l => /cancel|stop|running/i.test(l));
                    return {sim_buttons: labels, running_detected: btn_started
                        || /\bcancel sim|stop sim|simulating|sim running/i.test(body)};
                }''')
                post_running = bool(post3.get('running_detected'))
                log(f'step: click_simulate post-recovery shortcut check sim_buttons={post3.get("sim_buttons")} running={post_running}')
            except Exception:
                pass
        except Exception as e:
            log(f'step: click_simulate post-recovery shortcut exception: {e}')

    # 6b) Grace 재확인 — WQB React 는 클릭/recover 후 sim 'N%' 진행 UI 를
    #     1~3초 늦게 띄운다. recover-check 가 그 직전에 running=False 로 봐서
    #     FAIL-diag+스크린샷이 오발됐었음(관측: 덤프 2초 뒤 sim_started).
    #     dump 전에 최대 ~6초(3×2s) 진행/실행 신호를 다시 폴링한다.
    if not post_running:
        for _g in range(3):
            page.wait_for_timeout(2000)
            try:
                gp = page.evaluate(r'''() => {
                    const btns = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"], button')];
                    const lbl = btns.filter(b => b.offsetParent !== null
                        && /cancel|stop|running/i.test((b.innerText||'').trim())).length > 0;
                    const body = (document.body.innerText || '');
                    const pb = [...document.querySelectorAll('[class*="progress" i], [class*="simulate" i]')]
                        .filter(e => e.offsetParent !== null);
                    const pct = pb.some(e => /\d+\s*%/.test((e.innerText||'') + ' ' + (e.textContent||'')));
                    const cancelHit = /cancel\s+the\s+simulation|cancel\s+the\s+sim\b/i.test(body);
                    return lbl || pct || cancelHit;
                }''')
            except Exception:
                gp = False
            if gp:
                post_running = True
                log(f'step: click_simulate grace-recheck OK after {(_g+1)*2}s — sim 진행 확인, FAIL-diag/덤프 생략')
                break

    # 7) 모든 path 실패 — 페이지 상태 진단 로그.
    if not post_running:
        try:
            diag = page.evaluate(r'''() => {
                const out = {};
                const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
                if (btn) {
                    out.btn_class = (btn.className||'').toString().slice(0, 200);
                    out.btn_disabled = btn.disabled;
                    out.btn_label = (btn.innerText||'').trim().slice(0, 40);
                    const rect = btn.getBoundingClientRect();
                    out.btn_rect = {x: Math.round(rect.x), y: Math.round(rect.y),
                                    w: Math.round(rect.width), h: Math.round(rect.height)};
                    // 버튼 위에 다른 element 가 가로채는지 확인 (overlay).
                    const cx = rect.left + rect.width/2;
                    const cy = rect.top + rect.height/2;
                    const top = document.elementFromPoint(cx, cy);
                    out.top_at_center = top ? (top.tagName + '.' + (top.className||'').toString().slice(0,80)) : null;
                    out.btn_is_top = top === btn || btn.contains(top);
                }
                // overlay/modal 검사.
                const overlays = [...document.querySelectorAll('[class*="modal" i], [class*="overlay" i], [class*="dialog" i], [class*="introjs" i], [class*="tutorial" i], [class*="onboarding" i]')]
                    .filter(el => el.offsetParent !== null)
                    .map(el => (el.tagName + '.' + (el.className||'').toString().slice(0,60)).slice(0,80));
                out.overlays = overlays.slice(0, 8);
                // 페이지 URL + body innerText 첫 부분.
                out.url = (location.href || '').slice(0, 120);
                // monaco 존재 여부 (sanity).
                out.has_window_monaco = !!(window.monaco && window.monaco.editor);
                out.monaco_doms = document.querySelectorAll('.monaco-editor').length;
                return out;
            }''')
            log(f'step: click_simulate FAIL-diag btn_class={diag.get("btn_class")!r}')
            log(f'step: click_simulate FAIL-diag btn_disabled={diag.get("btn_disabled")} label={diag.get("btn_label")!r} rect={diag.get("btn_rect")}')
            log(f'step: click_simulate FAIL-diag top_at_center={diag.get("top_at_center")!r} btn_is_top={diag.get("btn_is_top")}')
            log(f'step: click_simulate FAIL-diag overlays={diag.get("overlays")}')
            log(f'step: click_simulate FAIL-diag has_window_monaco={diag.get("has_window_monaco")} monaco_doms={diag.get("monaco_doms")}')
        except Exception as e:
            log(f'step: click_simulate FAIL-diag exception: {e}')
        _dump_failure(page, 'click_simulate')

    # Ctrl+Enter 가 실제로 sim 을 시작시켰으면 (locator/JS click 실패해도) 클릭 성공으로 인정.
    # 이전 코드: locator_clicked + info.clicked → Ctrl+Enter 만으로 시작된 경우 0 을 반환해
    # 호출자가 'simulate button not clicked' 오류 처리. 그 결과 첫 알파만 운 좋게 통과하고
    # 나머지 알파가 줄줄이 실패하는 false-negative 발생.
    return locator_clicked + info.get('clicked', 0) + (1 if post_running else 0)

# ─────────────────────────────────────────────────────────────────────────────
# Settings 패널 자동화 — 시뮬 시작 전 Region/Universe/Delay/Neutralization/Decay/
# Truncation/Pasteurization/NaNHandling 등의 항목을 변경해서 다양한 조건으로
# 알파를 테스트할 수 있도록.
# ─────────────────────────────────────────────────────────────────────────────
SETTINGS_LABEL_MAP = {
    'region': 'Region',
    'universe': 'Universe',
    'delay': 'Delay',
    'neutralization': 'Neutralization',
    'decay': 'Decay',
    'truncation': 'Truncation',
    'pasteurization': 'Pasteurization',
    'nan_handling': 'NaN Handling',
    'unit_handling': 'Unit Handling',
}

def _open_settings_panel(page):
    # 현재 활성 sim 탭의 Settings 버튼을 클릭해서 패널 연다.
    # 이미 열려있으면 그대로. 못 찾으면 ''.
    return page.evaluate(r'''() => {
        // 이미 열려있는지 확인 — 'Region' 라벨이 visible 하면 OK.
        const norm = (s) => (s||'').trim().toLowerCase();
        const labels = [...document.querySelectorAll('label, span, div, legend')];
        for (const lb of labels) {
            if (lb.offsetParent === null) continue;
            if (norm(lb.innerText) === 'region') return 'already_open';
        }
        // 'Settings' 버튼 — 시뮬 에디터 우측 또는 헤더 영역.
        const cands = [...document.querySelectorAll('button, [role="button"], [role="tab"]')];
        for (const b of cands) {
            try {
                if (b.offsetParent === null || b.disabled) continue;
                const t = ((b.innerText || b.getAttribute('aria-label') || '') + '').trim();
                if (/^settings?$/i.test(t)) {
                    b.click();
                    return t;
                }
            } catch(e) {}
        }
        // 톱니 아이콘 fallback — title/aria-label 에 settings.
        const icons = [...document.querySelectorAll('[title*="ettings" i], [aria-label*="ettings" i]')];
        for (const ic of icons) {
            try {
                if (ic.offsetParent === null) continue;
                ic.click();
                return ic.getAttribute('title') || ic.getAttribute('aria-label') || 'icon';
            } catch(e) {}
        }
        return '';
    }''')


def _set_setting_field(page, label_text, value):
    # 라벨 텍스트 ('Region', 'Decay' 등) 옆 control 값 변경.
    # 반환: 'native_select' | 'input' | 'custom_dropdown_open' | 'label_not_found' | ''
    # custom_dropdown_open 이면 호출자가 _click_dropdown_option(value) 후속 호출.
    return page.evaluate(r'''
        ([label, value]) => {
            const norm = (s) => (s||'').trim().toLowerCase();
            const tgt = norm(label);
            // 라벨 후보 — span/label/legend/div 중 정확 매치 또는 ':' 포함.
            const all = [...document.querySelectorAll('label, legend, span, div, p')];
            const labels = all.filter(el => {
                if (el.offsetParent === null) return false;
                const tx = norm(el.innerText);
                return tx === tgt || tx === tgt + ':' || tx === tgt + ' ';
            });
            for (const t of labels) {
                let scope = t.parentElement;
                for (let depth = 0; depth < 5 && scope; depth++) {
                    // (a) native <select>
                    const sel = scope.querySelector('select');
                    if (sel && sel.offsetParent !== null) {
                        const opts = [...sel.options];
                        const opt = opts.find(o => norm(o.text) === norm(value) ||
                                                    norm(o.value) === norm(value));
                        if (opt) {
                            sel.value = opt.value;
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            sel.dispatchEvent(new Event('input', {bubbles: true}));
                            return 'native_select';
                        }
                    }
                    // (b) <input> (text/number).
                    const inp = scope.querySelector('input:not([type="checkbox"]):not([type="radio"]):not([type="hidden"])');
                    if (inp && inp.offsetParent !== null) {
                        try {
                            inp.focus();
                            const desc = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
                            if (desc && desc.set) {
                                desc.set.call(inp, String(value));
                            } else {
                                inp.value = String(value);
                            }
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            inp.blur();
                            return 'input';
                        } catch(e) { /* fall through */ }
                    }
                    // (c) Custom dropdown trigger.
                    const trig = scope.querySelector('[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="true"], button[class*="dropdown"], button[class*="select" i], div[class*="dropdown" i][role], div[class*="select" i][role]');
                    if (trig && trig.offsetParent !== null) {
                        try { trig.click(); } catch(e) {}
                        return 'custom_dropdown_open';
                    }
                    scope = scope.parentElement;
                }
            }
            return 'label_not_found';
        }
    ''', [label_text, value])


def _click_dropdown_option(page, value):
    # custom dropdown 이 열린 상태에서 텍스트 매치되는 옵션 클릭.
    # React 호환: mousedown/mouseup/click 모두 dispatch 해서 React 의 onClick 핸들러
    # 가 확실히 발화하도록.
    return page.evaluate(r'''
        (value) => {
            const norm = (s) => (s||'').trim().toLowerCase();
            const want = norm(value);
            const opts = [...document.querySelectorAll('[role="option"], [role="listbox"] *, li, .ant-select-item, .dropdown-item')]
                .filter(el => el.offsetParent !== null);
            function fireClick(o) {
                try {
                    // React 는 mousedown 직후 또는 click 에 반응. 둘 다 보냄.
                    const rect = o.getBoundingClientRect();
                    const x = rect.left + rect.width / 2;
                    const y = rect.top + rect.height / 2;
                    const opts = {bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0};
                    o.dispatchEvent(new MouseEvent('mousedown', opts));
                    o.dispatchEvent(new MouseEvent('mouseup', opts));
                    o.dispatchEvent(new MouseEvent('click', opts));
                    o.click();
                    return true;
                } catch(e) { return false; }
            }
            // 정확 매치 우선, 부분 매치 fallback.
            for (const o of opts) {
                if (norm(o.innerText) === want) {
                    if (fireClick(o)) return 'exact';
                }
            }
            for (const o of opts) {
                const tx = norm(o.innerText);
                if (tx && tx.indexOf(want) !== -1 && tx.length < 60) {
                    if (fireClick(o)) return 'partial';
                }
            }
            return '';
        }
    ''', value)


def _close_settings_panel(page):
    # Apply / Done / Save 버튼 또는 Escape 으로 패널 닫기. RX 광범위화 + React 호환 click.
    closed = page.evaluate(r'''() => {
        const btns = [...document.querySelectorAll('button, [role="button"]')];
        // 정확 매치 우선 (Apply / Save / Done / OK 단독 텍스트), 그 다음 부분 매치 ('Apply Settings' 등).
        const RX_EXACT = /^(apply|save|done|ok|확인|적용|close)$/i;
        const RX_PARTIAL = /^(apply\s+settings?|save\s+changes?|update\s+settings?|적용하기|저장)/i;
        function fireClick(o) {
            try {
                const rect = o.getBoundingClientRect();
                const opts = {bubbles: true, cancelable: true, clientX: rect.left + 5, clientY: rect.top + 5, button: 0};
                o.dispatchEvent(new MouseEvent('mousedown', opts));
                o.dispatchEvent(new MouseEvent('mouseup', opts));
                o.dispatchEvent(new MouseEvent('click', opts));
                o.click();
                return true;
            } catch(e) { return false; }
        }
        for (const b of btns) {
            if (b.offsetParent === null || b.disabled) continue;
            const t = ((b.innerText || '') + '').trim();
            if (RX_EXACT.test(t)) { if (fireClick(b)) return t; }
        }
        for (const b of btns) {
            if (b.offsetParent === null || b.disabled) continue;
            const t = ((b.innerText || '') + '').trim();
            if (RX_PARTIAL.test(t)) { if (fireClick(b)) return t; }
        }
        return '';
    }''')
    if not closed:
        try:
            page.keyboard.press('Escape')
        except Exception:
            pass
    return closed or 'escape'


def _sanitize_settings(settings):
    # 작은 universe (TOP500/TOP200) + 강한 신경화 (SUBINDUSTRY/INDUSTRY) 조합은
    # 그룹 내 종목 수가 너무 적어 시그널이 0 으로 수렴 → sim 결과 안 만들어짐.
    # SECTOR 또는 NONE 으로 완화.
    out = dict(settings)
    uni = (out.get('universe') or '').upper()
    neut = (out.get('neutralization') or '').upper()
    if uni in ('TOP500', 'TOP200') and neut in ('SUBINDUSTRY', 'INDUSTRY'):
        out['neutralization'] = 'SECTOR'
        log(f'apply_settings: sanitize {uni}+{neut} -> SECTOR (그룹 종목 수 부족 회피)')
    # decay / truncation 의 input 자동화는 React state 업데이트 못 받아 WQB 가
    # 'Wrong value for parameter decay' 에러로 sim 거부 → key 자체 제거 (default 사용).
    for k in ('decay', 'truncation'):
        if k in out:
            log(f'apply_settings: sanitize remove {k}={out[k]!r} (input 자동화 호환 안 됨, default 사용)')
            out.pop(k, None)
    return out


def apply_settings(page, settings):
    # settings dict 의 각 항목을 Settings 패널에 적용. 빈 dict 면 no-op.
    # 호출자는 set_editor_text 직후 / click_simulate 직전에 호출.
    # 실패해도 raise 안 함 - 시뮬은 default 로 진행됨.
    if not settings or not isinstance(settings, dict):
        return ''
    settings = _sanitize_settings(settings)
    nonempty = {k: v for k, v in settings.items() if v not in (None, '', [])}
    if not nonempty:
        return ''

    opened = _open_settings_panel(page)
    if not opened:
        log('apply_settings: Settings button not found, skip')
        return 'open_failed'
    if opened != 'already_open':
        page.wait_for_timeout(900)
    log(f'apply_settings: panel ({opened!r}), keys={list(nonempty.keys())}')

    applied = []
    for key, value in nonempty.items():
        label = SETTINGS_LABEL_MAP.get(str(key).lower())
        if not label:
            log(f'apply_settings: skip unknown key={key}')
            continue
        try:
            r = _set_setting_field(page, label, str(value))
        except Exception as e:
            r = f'err:{e}'
        if r == 'custom_dropdown_open':
            page.wait_for_timeout(400)
            picked = _click_dropdown_option(page, str(value))
            r = f'dropdown:{picked or "no_option"}'
            # 옵션 클릭 후 listbox 가 자동 닫힐 시간 (React render). Escape 누르면
            # 패널 자체가 닫히는 부작용이 있어 wait 만 사용.
            page.wait_for_timeout(550)
        applied.append(f'{key}={value!r}->{r}')
        page.wait_for_timeout(300)
    log('apply_settings: ' + ' | '.join(applied))

    closed = _close_settings_panel(page)
    page.wait_for_timeout(500)
    log(f'apply_settings: closed via {closed!r}')
    return 'ok'

def extract_state(page):
    return page.evaluate(r'''() => {
        const r = {};
        // 8개 metric (Sharpe/Fitness/Returns/Turnover/Drawdown/Margin/Sub-Sharpe/Correlation) 외에도
        // WQB UI 가 추가 row 를 보여줄 수 있으니, 이름 → 값 매핑을 generic 하게 추출.
        const KEYS_FALLBACK = ['sharpe','fitness','returns','drawdown','turnover','margin'];
        // 키 정규화: 'IS Sharpe' → 'sharpe', 'Sub-Universe Sharpe' → 'subuniverse_sharpe' 등.
        const norm = (s) => (s||'').toLowerCase()
            .replace(/^is\s+/, '')
            .replace(/^os\s+/, 'os_')
            .replace(/[\-\s]+/g, '_')
            .replace(/[^a-z0-9_]/g, '');
        const VALUE_RX = /([-+]?\d[\d.,]*\s*[%‱]?|n\/a)/i;
        const rows = [...document.querySelectorAll('.summary-metrics-info')]
            .filter(e => !(/--title/.test(e.className || '')));
        for (const row of rows) {
            const txt = (row.innerText || '').trim().replace(/\s+/g, ' ');
            // "Name Value" 형식 — name 은 알파벳/공백/대시, value 는 숫자(+단위).
            const m = txt.match(/^([A-Za-z][A-Za-z0-9\-\s]*?)\s+([-+]?\d[\d.,%‱\s]*)$/);
            if (m) {
                const k = norm(m[1]);
                const v = m[2].replace(/\s+/g, '').trim();
                if (k && v) r[k] = v;
            }
        }
        if (Object.keys(r).length === 0) {
            const block = document.querySelector('.title.sumary__metrics, [class*="sumary__metrics"], [class*="summary__metrics"]');
            if (block) {
                const text = (block.innerText || '').replace(/\n+/g, ' ');
                for (const k of KEYS_FALLBACK) {
                    const re = new RegExp('(?:^|\\s)' + k + '\\s+([-+]?\\d+(?:\\.\\d+)?[%‱]?)', 'i');
                    const m = text.match(re);
                    if (m) r[k] = m[1];
                }
            }
        }
        const bodyText = document.body.innerText;
        if (Object.keys(r).length === 0) {
            const lines = bodyText.split('\n').map(s => s.trim());
            for (let i = 0; i < lines.length - 1; i++) {
                const ln = lines[i];
                const lnMatch = KEYS_FALLBACK.find(k => ln.toLowerCase() === k || ln.toLowerCase() === 'is ' + k);
                if (lnMatch) {
                    const next = lines[i+1] || '';
                    if (/^[-+]?\d/.test(next)) r[lnMatch] = next;
                }
            }
        }
        let compileErr = '';
        const errRX = /(Attempted to use[^"]+"[^"]+"|Unexpected character[^.]*\.|Operator [^"]+ does not support[^.]*\.)/i;
        const m2 = bodyText.match(errRX);
        if (m2) compileErr = m2[0];
        let running = !!document.querySelector('.editor-tabs__tab-dot--running, [class*="--running"]');
        // 보조 시그널 — Simulate 버튼이 Cancel/Stop/Running 라벨로 바뀌었으면 sim 진행 중.
        // 사용자 tier 따라 tab-dot 가 안 보일 수 있어 (--running 안 잡힘) UI 폴백 필수.
        if (!running) {
            const sim_btn_labels = [...document.querySelectorAll('button.editor-simulate-button-text, button[class*="editor-simulate-button"]')]
                .filter(b => b.offsetParent !== null)
                .map(b => (b.innerText||'').trim());
            if (sim_btn_labels.some(l => /cancel|stop|running/i.test(l))) running = true;
        }
        if (!running) {
            // 마지막 폴백 — body 텍스트에 'Cancel Sim' / 'Stop Sim' / 'Simulating' /
            // 'Click here to cancel the simulation' / 'Simulations usually take' 패턴.
            // (WQB 는 sim 중에도 button label 을 'Simulate' 그대로 두고 진행 표시는
            //  우측 패널 안의 cancel-link + progress bar 로만 표시함 — 핵심 폴백.)
            if (/\bcancel\s*sim|stop\s*sim|simulating|sim\s*running|cancel\s+the\s+simulation|simulations?\s+usually\s+take/i.test(bodyText)) running = true;
        }
        if (!running) {
            // progress bar 직접 매치 — `.editor-simulate__progress-bar` 또는 progress 클래스.
            const pb = document.querySelector('[class*="editor-simulate__progress"], [class*="simulate-progress"], [class*="progress-bar"]');
            if (pb && pb.offsetParent !== null) {
                const ptxt = (pb.innerText||'') + ' ' + (pb.textContent||'');
                if (/\d+\s*%/.test(ptxt)) running = true;
            }
        }
        // IS Tests 패널 출현 시그널 — 'X PASS' / 'X FAIL' / 'X PENDING' 헤더가 보이면
        // 이 슬롯의 sim 은 끝나고 결과가 노출된 상태 (metrics 변화 detect 못 해도 done 으로 판정).
        const is_tests_visible = /\b\d+\s+(PASS|FAIL|ERROR|PENDING)\b/.test(bodyText);
        return {metrics: r, error_text: compileErr, running, is_tests_visible};
    }''')

def _click_show_test_results(page):
    # 1) Tutorial 팝업/체크박스 처리 — 'Tutorial' 이 켜져 있으면 끄고, 'Results' 켜기.
    # 2) 'Show test period' / 'Show Test Results' 버튼 클릭 — IS Testing Status 패널 노출
    # 3) 그 안의 'N PASS' / 'N FAIL' / 'N PENDING' 카운터를 accordion 확장 시도 (각 클릭)
    # 주의: 이 버튼은 토글이라 이미 패널이 열려있을 때 다시 클릭하면 닫힘 → 무조건
    #       클릭하지 않고, 페이지에 이미 'X PASS|FAIL|PENDING' 패턴이 보이면 skip.
    try:
        # 0) Tutorial 체크박스 해제 + Results 체크박스 켜기.
        #    WQB 의 시뮬 결과 페이지 상단에 'Tutorial / Results' 토글 체크박스가 있음.
        #    Tutorial 켜져 있으면 IS Tests panel 이 tutorial 가이드로 가려짐 → Tutorial 해제 + Results 체크.
        #    React UI 라 input[type=checkbox] 가 hidden 일 수도 있어 다양한 selector + label 추출.
        tut_info = page.evaluate(r'''() => {
            const out = {tutorial_unchecked: false, results_checked: false, debug_labels: []};

            // 모든 체크박스 후보 — input + role=checkbox/switch + class 기반.
            const cbs = new Set([
                ...document.querySelectorAll('input[type="checkbox"]'),
                ...document.querySelectorAll('[role="checkbox"], [role="switch"]'),
                ...document.querySelectorAll('[class*="checkbox" i]:not(label):not(div[class*="container"])'),
                ...document.querySelectorAll('[aria-checked]'),
            ]);

            function getLabel(cb) {
                // 1. id matching label.
                if (cb.id) {
                    const lbl = document.querySelector(`label[for="${cb.id}"]`);
                    if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
                }
                // 2. ancestor label (max 3 levels up).
                let p = cb.parentElement;
                for (let i = 0; i < 3 && p; i++) {
                    if (p.tagName === 'LABEL') {
                        return (p.innerText || p.textContent || '').trim();
                    }
                    p = p.parentElement;
                }
                // 3. sibling text — next first, then prev.
                const sibs = [cb.nextElementSibling, cb.previousElementSibling];
                for (const s of sibs) {
                    if (s) {
                        const t = (s.innerText || s.textContent || '').trim();
                        if (t && t.length < 50) return t;
                    }
                }
                // 4. parent (closest, brief).
                if (cb.parentElement) {
                    const t = (cb.parentElement.innerText || cb.parentElement.textContent || '').trim();
                    if (t && t.length < 50) return t;
                }
                // 5. aria-label / title.
                return (cb.getAttribute('aria-label') || cb.getAttribute('title') || '').trim();
            }

            function isChecked(cb) {
                if (cb.tagName === 'INPUT' && cb.type === 'checkbox') return cb.checked;
                const ac = cb.getAttribute('aria-checked');
                if (ac === 'true') return true;
                if (ac === 'false') return false;
                if (/(^|\s)(checked|active|selected|on)(\s|$)/i.test(cb.className || '')) return true;
                return false;
            }

            function clickIt(cb) {
                // React 호환 — click() + 명시적 click event + change event.
                try { cb.click(); } catch(e) {}
                try { cb.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true})); } catch(e) {}
                try { cb.dispatchEvent(new Event('change', {bubbles: true})); } catch(e) {}
                // 부모 label 도 click — input 이 hidden 인 경우 label click 으로 토글.
                let p = cb.parentElement;
                for (let i = 0; i < 2 && p; i++) {
                    if (p.tagName === 'LABEL' || /(^|\s)label(\s|$)/i.test(p.className||'')) {
                        try { p.click(); } catch(e) {}
                        break;
                    }
                    p = p.parentElement;
                }
            }

            for (const cb of cbs) {
                if (cb.offsetParent === null && cb.type !== 'checkbox') continue;
                const label = getLabel(cb);
                if (!label) continue;
                out.debug_labels.push(label.slice(0, 30));
                const checked = isChecked(cb);
                if (/^tutorial\b/i.test(label) || /\btutorial\b/i.test(label)) {
                    if (checked) {
                        clickIt(cb);
                        out.tutorial_unchecked = true;
                    }
                }
                if (/^results?\b/i.test(label) || /\btest\s*results?\b/i.test(label)) {
                    if (!checked) {
                        clickIt(cb);
                        out.results_checked = true;
                    }
                }
            }
            return out;
        }''')
        ti = tut_info or {}
        if ti.get('tutorial_unchecked') or ti.get('results_checked'):
            log(f'show_test_results: tutorial fix — uncheck_tut={ti.get("tutorial_unchecked")} check_results={ti.get("results_checked")}')
            page.wait_for_timeout(1500)
        elif ti.get('debug_labels'):
            log(f'show_test_results: tutorial-cb labels seen={ti["debug_labels"][:20]} (no Tutorial/Results match)')

        # 'panel already open' skip 제거 - 이전 알파 panel 잔재가 남아있으면 'X PASS' 매치되어
        # skip 되고 잔재 결과가 scrape 됨 (slot 2/3 의 합 13 PENDING=6 케이스). 매번 trigger 시도.

        # 0a) Tutorial 모드 빠져나오기 — WQB 가 신규 사용자에게 tutorial 보여주는 동안에는
        # IS Tests panel 이 tutorial 안에 숨어있어 panel 진짜 안 뜸.
        # 'Exit tutorial mode' 클릭 → confirm dialog 가 뜨면 'Yes'/'Confirm'/'Exit' 까지 클릭.
        try:
            for _ in range(3):  # 최대 3회 — tutorial 모드가 confirm modal 로 가드됨
                tut_exit = page.evaluate(r'''() => {
                    const RX = /^exit\s*tutorial|exit\s*tutorial\s*mode|^skip\s*tutorial|don'?t\s*show\s*again/i;
                    const cands = [...document.querySelectorAll('button, a, [role="button"]')];
                    for (const el of cands) {
                        if (el.offsetParent === null || el.disabled) continue;
                        const t = (el.innerText || el.getAttribute('aria-label') || '').trim();
                        if (RX.test(t)) { el.click(); return t.slice(0, 40); }
                    }
                    return '';
                }''')
                if not tut_exit:
                    break
                log(f'show_test_results: tutorial exit click ({tut_exit!r})')
                page.wait_for_timeout(1200)
                # confirm dialog 처리 — 'Exit'/'Yes'/'Confirm' 라벨 매칭.
                confirm = page.evaluate(r'''() => {
                    const RX = /^(exit|yes|confirm|ok|proceed|continue)\s*$/i;
                    const dlg = document.querySelector('[role="dialog"]:not([aria-hidden="true"]), .ant-modal:not(.ant-modal-hidden), .modal:not(.hidden)');
                    const root = dlg || document;
                    const btns = [...root.querySelectorAll('button, [role="button"]')];
                    for (const b of btns) {
                        if (b.offsetParent === null || b.disabled) continue;
                        const t = (b.innerText || '').trim();
                        if (RX.test(t)) { b.click(); return t.slice(0,40); }
                    }
                    return '';
                }''')
                if confirm:
                    log(f'show_test_results: tutorial exit confirm ({confirm!r})')
                    page.wait_for_timeout(1500)
                else:
                    page.wait_for_timeout(800)
        except Exception:
            pass

        info = page.evaluate(r'''() => {
            // ★ 'Show all checks' 가 WQB 의 진짜 IS Tests panel trigger.
            // 'Show test period' 는 sim period 표시 토글일 뿐 panel 과 무관.
            // 둘 다 시도하되 'Show all checks' 우선.
            // ★ 'Show test period' 가 진짜 IS Testing Results panel 트리거 (사용자 검증).
            // 'Show all checks' 는 alpha checks 페이지 navigation (panel 안 띄움) → RX_BAD 로.
            const RX_GOOD = /show\s*test\s*period|show\s*test\s*results?|run\s*tests?|view\s*test\s*results?|expand\s*tests?|결과\s*보기|테스트\s*결과/i;
            const RX_BAD = /hide|list|menu|customize|history|drag|rearrange|all\s*checks?/i;
            const cands = [...document.querySelectorAll('button, a, [role="button"]')];
            const debug = [];
            let clicked_label = '';
            for (const el of cands) {
                try {
                    if (el.offsetParent === null || el.disabled) continue;
                    const t = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'')).trim();
                    if (!t) continue;
                    if (RX_BAD.test(t)) continue;
                    if (RX_GOOD.test(t)) {
                        el.click();
                        clicked_label = t.slice(0, 60);
                        debug.push('clicked: ' + clicked_label);
                        break;
                    }
                } catch(e) {}
            }
            return {clicked_label, debug};
        }''')
        clicked_label = (info or {}).get('clicked_label') or ''
        if clicked_label:
            log(f'show_test_results: clicked label={clicked_label!r}')
        else:
            log('show_test_results: no matching button found')
            return False
        page.wait_for_timeout(1500)
        # 카운터 헤더들 클릭해서 accordion 확장.
        page.evaluate(r'''() => {
            const RX_HEAD = /^\d+\s+(PASS|FAIL|PENDING)\s*$/i;
            // textContent 포함 모든 element 검사 (hidden 제외).
            const cands = [...document.querySelectorAll('*')];
            const clicked = [];
            for (const el of cands) {
                if (el.offsetParent === null) continue;
                if (el.children.length > 0) continue;
                const t = (el.textContent||'').trim();
                if (RX_HEAD.test(t)) {
                    // 클릭 가능한 ancestor 찾기.
                    let target = el;
                    for (let i = 0; i < 5 && target; i++) {
                        const tag = (target.tagName||'').toLowerCase();
                        const role = target.getAttribute && target.getAttribute('role');
                        const cls = (target.className||'').toString();
                        if (tag === 'button' || role === 'button'
                                || /clickable|expand|toggle|accordion|cursor/i.test(cls)
                                || (target.onclick !== null)) {
                            try { target.click(); clicked.push(t); } catch(e) {}
                            break;
                        }
                        target = target.parentElement;
                    }
                    // ancestor 못 찾았으면 leaf 직접 click 시도.
                    if (!clicked.includes(t)) {
                        try { el.click(); clicked.push(t); } catch(e) {}
                    }
                }
            }
            return clicked;
        }''')
        page.wait_for_timeout(1500)
        return True
    except Exception:
        return False


def _try_alternative_panel_trigger(page):
    # IS Tests 패널이 안 떴을 때 다양한 trigger 시도 + 진단 정보 dump.
    try:
        info = page.evaluate(r'''() => {
            // 'Test'/'TEST' 단독 제외 — 본인 알파 리스트 카테고리 라벨로 navigation 위험.
            const RX = /(test\s*results?|is\s*tests?|run\s*tests?|tests?\s*status|view\s*tests?|results?\s*tab)/i;
            const RX_BAD = /hide|list|menu|customize|history|drag|rearrange|setting|period|date|^test$|^tests$|alpha\s*list/i;
            const cands = [...document.querySelectorAll('button, a, [role="button"], [role="tab"], summary, h2, h3, h4, [class*="accordion"], [class*="expand"], [class*="tab"]')];
            const clicked = [];
            for (const el of cands) {
                try {
                    if (el.offsetParent === null || el.disabled) continue;
                    const t = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'')).trim();
                    if (!t || t.length > 80) continue;
                    if (RX_BAD.test(t)) continue;
                    if (RX.test(t)) { el.click(); clicked.push(t.slice(0,40)); }
                } catch(e) {}
            }
            // 진단 — 클릭한 게 없으면 page 의 모든 visible button label 을 dump.
            let diag_btns = [];
            if (clicked.length === 0) {
                const all_btns = [...document.querySelectorAll('button, a, [role="button"], [role="tab"]')];
                for (const b of all_btns) {
                    if (b.offsetParent === null || b.disabled) continue;
                    const t = (b.innerText || b.getAttribute('aria-label') || '').trim();
                    if (t && t.length > 0 && t.length < 50) diag_btns.push(t.slice(0,40));
                    if (diag_btns.length >= 30) break;
                }
            }
            // 'X PASS' 류 헤더가 페이지 어디에 있는지 위치 확인.
            const hdr_rx = /\b\d+\s+(PASS|FAIL|ERROR|PENDING)\b/i;
            const all_text_nodes = [...document.querySelectorAll('*')].filter(e => e.children.length === 0);
            let panel_found = '';
            for (const n of all_text_nodes) {
                if (n.offsetParent === null) continue;
                const t = (n.textContent || '').trim();
                if (hdr_rx.test(t)) { panel_found = t.slice(0, 80); break; }
            }
            return {clicked, diag_btns, panel_found};
        }''')
        if info:
            if info.get('clicked'):
                log(f'alt_panel_trigger: clicked={info["clicked"]}')
            if info.get('diag_btns'):
                log(f'alt_panel_trigger: no match — visible buttons={info["diag_btns"]}')
            if info.get('panel_found'):
                log(f'alt_panel_trigger: PASS|FAIL header found at: {info["panel_found"]!r}')
    except Exception:
        pass


def _scrape_is_testing_status(page):
    # 전략: document.body.innerText / textContent 안에서 "IS Testing Status" 가 여러 번
    # 등장할 수 있음 (사이드바 라벨 + 메뉴 customizer + 실제 테스트 패널). 각 occurrence
    # 의 슬라이스를 점수화해 "X PASS / X FAIL / cutoff of / check pending / competitions
    # match" 이 가장 많은 슬라이스를 선택.
    try:
        raw = page.evaluate(r'''() => {
            const innerT = document.body.innerText || '';
            const fullT = document.body.textContent || '';
            const RX_DETAIL = /cutoff of|check pending|check error|competitions match|weight is well distributed|robustness check/gi;
            const RX_HEADER = /\b\d+\s+(PASS|FAIL|ERROR|PENDING)\b/gi;
            const TERMINATORS = /\n(IS Tests Setting|Show Test Results|Settings\b|Submit\b|Properties\b|Code\b|^\d{1,3}$)/im;
            function scoreSlice(s) {
                if (!s) return 0;
                const d = s.match(RX_DETAIL);
                const h = s.match(RX_HEADER);
                return ((d ? d.length : 0) * 10) + ((h ? h.length : 0) * 3);
            }
            function findAllSlices(text) {
                const out = [];
                const rx = /IS Testing Status/gi;
                let m;
                while ((m = rx.exec(text)) !== null) {
                    const after = text.slice(m.index);
                    const endIdx = after.search(TERMINATORS);
                    const slice = (endIdx > 0 && endIdx < 30) ? after.slice(0, 4000)
                                : (endIdx > 0 ? after.slice(0, endIdx) : after.slice(0, 4000));
                    out.push(slice);
                }
                return out;
            }
            let best = '', bestScore = 0;
            for (const t of [innerT, fullT]) {
                for (const s of findAllSlices(t)) {
                    const sc = scoreSlice(s);
                    if (sc > bestScore) { bestScore = sc; best = s; }
                }
            }
            if (bestScore > 0) return best;
            // "IS Testing Status" 라벨이 패널 안에 없을 수 있음 — 페이지 어디에든 'X PASS'
            // / 'X FAIL' / 'X PENDING' 헤더 패턴이 있으면 그 주변 슬라이스 채택.
            for (const t of [innerT, fullT]) {
                const m = /\n\d+\s+(PASS|FAIL|PENDING)\b/m.exec(t);
                if (m) {
                    const start = Math.max(0, m.index - 200);
                    const slice = t.slice(start, m.index + 4000);
                    if (scoreSlice(slice) > 0) return slice;
                }
            }
            // 디버그용 fallback — 첫 occurrence 800자.
            const idx2 = innerT.search(/IS Testing Status/i);
            if (idx2 >= 0) return innerT.slice(idx2, idx2 + 800);
            return '';
        }''') or ''
        if not raw or 'IS Testing Status' not in raw:
            return {'pass': [], 'fail': [], 'pending': [], 'raw': raw[:500]}

        # 헤더 별로 line 분류. WQB IS Testing Status 는 4섹션: PASS / FAIL / ERROR / PENDING.
        # ERROR 는 테스트 자체가 계산 실패한 항목 (Fitness check error, Sub-universe Sharpe
        # check error 등) — pass 가 아니므로 fail 과 동일하게 카운트해서 submit 차단.
        # 전처리: 일부 케이스 (textContent 폴백 / 압축된 DOM) 에서 줄바꿈이 모두 제거되어
        # 한 줄로 합쳐 들어옴. (a) 섹션 헤더 'X PASS|FAIL|ERROR|PENDING' 앞에 줄바꿈 삽입,
        # (b) 마침표 '.' 다음에도 줄바꿈 삽입 (각 cutoff 메시지 분리).
        raw = re.sub(r'(?<!\n)\s*(\d+\s+(?:PASS|FAIL|ERROR|PENDING)\b)', r'\n\1', raw,
                     flags=re.IGNORECASE)
        # 마침표 다음 — 공백 있든 없든 대문자 시작이면 줄바꿈 ('1%.Turnover' 같은 케이스).
        raw = re.sub(r'\.\s*(?=[A-Z])', '.\n', raw)
        # 섹션 헤더가 본문과 같은 줄에 있는 케이스: '3 PASS  Turnover...' → '3 PASS\nTurnover...'
        raw = re.sub(r'(\d+\s+(?:PASS|FAIL|ERROR|PENDING))\s{2,}', r'\1\n', raw,
                     flags=re.IGNORECASE)
        lines = [ln.strip() for ln in raw.split('\n') if ln.strip()]
        section = None  # 'pass' | 'fail' | 'error' | 'pending' | None
        out = {'pass': [], 'fail': [], 'error': [], 'pending': [], 'raw': raw[:1500]}
        # 헤더 매치 - 'X PASS' / 'PASS X' / 단독 'PASS' 모두 허용.
        section_rx = re.compile(
            r'^(?:\d+\s+)?(PASS|FAIL|ERROR|ERRORS|PENDING|WARNING|WARNINGS|NOTE|NOTES|INFO)(?:\s+\d+)?\s*$',
            re.IGNORECASE,
        )
        # 값 패턴 — 끝에 무관한 마침표가 따라오면 떼어냄.
        VAL = r'[-+]?\d[\d,]*(?:\.\d+)?\s*[%‱]?'

        # PASS 전용 관용구 매핑 (값 없는 형태). 키워드 → canonical name.
        PASS_PHRASES = [
            (re.compile(r'^Weight is well distributed', re.I), 'Weight Concentration'),
            (re.compile(r'^These competitions? match', re.I), 'Competitions'),
            (re.compile(r'^Robustness check passed', re.I), 'Robustness'),
        ]

        def _strip_trailing_dot(s: str) -> str:
            return (s or '').rstrip('.').strip()

        # Customize Alpha Details Menu 같은 다른 panel 의 widget 라벨이 PENDING section 안으로
        # 흘러들어 오면 false-positive PENDING entry 생성 (합 != 8 anomaly). 이 키워드 라인 보이면
        # section reset → 더 이상 entry 추가 안 함.
        SECTION_TERMINATORS = re.compile(
            r'^(Customize\s+Alpha|Drag\s+the\s+containers|Chart$|Summary$|Correlation$|'
            r'Testing\s+Status$|Performance\s+Comparison|Properties$|Reset$|Apply$|'
            r'Add\s+Alpha\s+to\s+a\s+List|Open\s+alpha\s+details|Check\s+Submission|'
            r'Submit\s+Alpha$|Last\s+saved|Name$|Category$|Tags$|Color$|Description$|'
            r'Select/add\s+tags|None$|'
            # drag-and-drop 접근성 안내 / properties 잔재 / 위젯 라벨 concatenated.
            r'Press\s+space\s+bar|When\s+dragging|Some\s+screen\s+readers|'
            r'PropertiesLast\s+saved|ChartSummary|TestingStatus)',
            re.IGNORECASE,
        )
        # IS Tests 표준 검사 키워드 화이트리스트 — 8개 표준 검사 + 변형. desc 안에 이 중
        # 하나라도 보이지 않으면 noise 로 간주하고 entry 추가 안 함.
        IS_TESTS_WHITELIST = re.compile(
            r'(sharpe|fitness|return|turnover|drawdown|margin|'
            r'sub[-\s]?universe|self[-\s]?correlation|weight|competition|'
            r'robustness|cutoff|check\s+(pending|error|failed))',
            re.IGNORECASE,
        )

        for ln in lines:
            mh = section_rx.match(ln)
            if mh:
                kind = mh.group(1).upper()
                if kind in ('PASS', 'FAIL', 'PENDING'):
                    section = kind.lower()
                elif kind in ('ERROR', 'ERRORS'):
                    section = 'error'
                else:
                    section = None
                continue
            if section is None or ln == 'IS Testing Status':
                continue
            # 다른 panel/widget 라벨 만나면 section 종료.
            if SECTION_TERMINATORS.match(ln):
                section = None
                continue
            # desc 자체에 IS Tests 키워드 없으면 noise — drop.
            if not IS_TESTS_WHITELIST.search(ln):
                continue
            if len(ln) > 250:
                # 너무 긴 라인 — self-correlation 관련이면 잘라서 계속 (WQB reject 메시지가
                # 길 수 있음), 아니면 noise 로 drop.
                if re.search(r'self[\s-]?correlation', ln, re.IGNORECASE):
                    ln = ln[:250]
                else:
                    continue
            entry = {'desc': ln}
            # 1) "X of Y is above/below cutoff of Z" — 표준 형식
            mv = re.search(
                rf'^([A-Za-z][A-Za-z\-\s]*?)\s+of\s+({VAL})\s+is\s+(above|below)\s+cutoff\s+of\s+({VAL})',
                ln, re.IGNORECASE,
            )
            if mv:
                entry['name'] = mv.group(1).strip()
                entry['value'] = _strip_trailing_dot(mv.group(2))
                entry['direction'] = mv.group(3).lower()
                entry['cutoff'] = _strip_trailing_dot(mv.group(4))
                out[section].append(entry); continue
            # 2) "Weight concentration X% is above cutoff of Y% on DATE" — 위 패턴 변형 (no 'of')
            mv2 = re.search(
                rf'^([A-Za-z][A-Za-z\s]*?)\s+({VAL})\s+is\s+(above|below)\s+cutoff\s+of\s+({VAL})',
                ln, re.IGNORECASE,
            )
            if mv2:
                entry['name'] = mv2.group(1).strip()
                entry['value'] = _strip_trailing_dot(mv2.group(2))
                entry['direction'] = mv2.group(3).lower()
                entry['cutoff'] = _strip_trailing_dot(mv2.group(4))
                out[section].append(entry); continue
            # 3) PASS 전용 관용구 (값 없음).
            handled = False
            for rx, canon in PASS_PHRASES:
                if rx.search(ln):
                    entry['name'] = canon
                    out[section].append(entry); handled = True
                    break
            if handled:
                continue
            # 4) PENDING / ERROR 라인 — 다양한 wording 매치.
            # 'Self-correlation check pending' / 'Fitness check error' / 'X is pending' /
            # 'X pending' / 'X errored' / 'X computing' / 'X running' 등.
            m_chk = re.match(
                r'^([A-Za-z][A-Za-z\-\s]*?)\s+'
                r'(?:check\s+)?(?:is\s+)?(pending|errored|error|running|computing|failed|in\s*progress)\b',
                ln, re.IGNORECASE,
            )
            if m_chk:
                entry['name'] = m_chk.group(1).strip()
                out[section].append(entry); continue
            # 4a) Self-correlation 전용 — 줄 어디든 'self correlation' 이 보이면 매치 + 실측값 추출.
            #     "Self-correlation check pending" / "The self correlation of this alpha ... is 0.94,
            #     above the maximum of 0.7" / "self correlation against submitted alphas is 0.9415
            #     (cutoff 0.7)" 등 다양한 wording 대응.
            if re.search(r'self[\s-]?correlation', ln, re.IGNORECASE):
                entry['name'] = 'Self-correlation'
                _nums = re.findall(r'\d+\.\d+', ln)
                if _nums:
                    entry['value'] = _nums[0]
                    if len(_nums) >= 2:
                        entry['cutoff'] = _nums[1]
                    if re.search(r'\babove\b|exceed|too\s+high|over\s+the\b', ln, re.IGNORECASE):
                        entry['direction'] = 'above'
                    elif re.search(r'\bbelow\b|under\b', ln, re.IGNORECASE):
                        entry['direction'] = 'below'
                out[section].append(entry); continue
            # 4b) Sub-universe Sharpe fallback (cutoff/of 없는 변형).
            m_su = re.match(r'^(maximum\s+)?(sub[-\s]?universe\s+sharpe)\b', ln, re.IGNORECASE)
            if m_su:
                entry['name'] = m_su.group(2).strip()
                _nums = re.findall(r'[-+]?\d+\.\d+', ln)
                if _nums:
                    entry['value'] = _nums[0]
                    if len(_nums) >= 2:
                        entry['cutoff'] = _nums[1]
                out[section].append(entry); continue
            # 5) 기타 — 첫 단어 묶음만 name 으로.
            m_first = re.match(r'^([A-Za-z][A-Za-z\-\s]{1,30}?)\b', ln)
            entry['name'] = m_first.group(1).strip() if m_first else ln[:40]
            out[section].append(entry)
        return out
    except Exception as e:
        return {'pass': [], 'fail': [], 'pending': [], 'raw': f'exception: {e}'}


def _try_submit_alpha(page):
    # 현재 활성 시뮬 탭의 결과 화면에서 Submit Alpha 버튼을 찾아 클릭.
    # 비활성화 (disabled) 상태면 클릭 안 함. 확인 modal 이 뜨면 confirm 도 클릭.
    # 반환: ('submitted' | 'disabled' | 'not_found' | 'fail:<reason>', detail_str).
    #
    # WQB 의 'Customize Alpha Details Menu' 패널 안에는 'Submit Alpha' 라벨의 menu
    # 아이템이 존재 — 이를 클릭하면 menu 항목 토글만 발생하고 제출되지 않음.
    # 따라서 customize 영역 안의 후보는 반드시 제외하고, 1차 click 후 modal 이 떠야만
    # 진짜 Submit 버튼이었던 것으로 본다.
    try:
        # 1) Submit 버튼 탐색 — Customize 메뉴 아이템 제외 + bounding box 캡처.
        info = page.evaluate(r'''() => {
            // "Submit" / "Submit Alpha" / "제출" 정확 매칭. "Submitted" / "Resubmit" 류 제외.
            const RX_GOOD = /^\s*submit(\s+alpha)?\s*$|^\s*제출\s*$/i;
            const RX_BAD = /submitted|resubmit|simulate|cancel|close/i;
            const isInCustomizeMenu = (el) => {
                // 'Customize Alpha Details Menu' 안의 menu item 은 제출 안 시킴 — 제외.
                let p = el;
                for (let i = 0; i < 12 && p; i++) {
                    const role = (p.getAttribute && p.getAttribute('role')) || '';
                    if (role === 'menu' || role === 'menuitem' || role === 'listbox' || role === 'option') return true;
                    const txt = (p.innerText || '').slice(0, 600);
                    if (/customize\s+alpha\s+details/i.test(txt) || /drag\s+the\s+containers/i.test(txt)) {
                        // 단, 페이지 전체 (body) 가 잡혀버리면 의미 없음 — 너무 큰 컨테이너는 skip.
                        if ((p.innerText || '').length < 1500) return true;
                    }
                    p = p.parentElement;
                }
                return false;
            };
            const isDisabled = (el) => {
                if (el.disabled) return true;
                const ad = (el.getAttribute('aria-disabled')||'').toLowerCase();
                if (ad === 'true') return true;
                if (/(^|\s)(disabled|is-disabled|btn-disabled)(\s|$)/i.test(el.className||'')) return true;
                const cs = window.getComputedStyle(el);
                if (cs && (cs.pointerEvents === 'none' || cs.cursor === 'not-allowed')) return true;
                return false;
            };
            const all = [...document.querySelectorAll('button, [role="button"], a[role="button"]')];
            for (const el of all) {
                try {
                    if (el.offsetParent === null) continue;
                    const t = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'')).trim();
                    if (RX_BAD.test(t)) continue;
                    if (!RX_GOOD.test(t)) continue;
                    if (isInCustomizeMenu(el)) continue;
                    const r = el.getBoundingClientRect();
                    return {
                        found: true,
                        label: t.slice(0,80),
                        disabled: isDisabled(el),
                        rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
                    };
                } catch(e) {}
            }
            return {found: false};
        }''')
        if not info or not info.get('found'):
            return ('not_found', 'no submit button (customize menu items 제외됨)')
        if info.get('disabled'):
            return ('disabled', f'label={info.get("label","")!r}')

        first_rect = info.get('rect') or {}
        log(f'submit_alpha: button found, label={info.get("label","")!r} rect={first_rect}')

        # 2) 1차 click — Customize 메뉴 제외 + 위 매칭 버튼 click.
        clicked = page.evaluate(r'''() => {
            const RX_GOOD = /^\s*submit(\s+alpha)?\s*$|^\s*제출\s*$/i;
            const RX_BAD = /submitted|resubmit|simulate|cancel|close/i;
            const isInCustomizeMenu = (el) => {
                let p = el;
                for (let i = 0; i < 12 && p; i++) {
                    const role = (p.getAttribute && p.getAttribute('role')) || '';
                    if (role === 'menu' || role === 'menuitem' || role === 'listbox' || role === 'option') return true;
                    const txt = (p.innerText || '').slice(0, 600);
                    if (/customize\s+alpha\s+details/i.test(txt) || /drag\s+the\s+containers/i.test(txt)) {
                        if ((p.innerText || '').length < 1500) return true;
                    }
                    p = p.parentElement;
                }
                return false;
            };
            const isDisabled = (el) => {
                if (el.disabled) return true;
                const ad = (el.getAttribute('aria-disabled')||'').toLowerCase();
                if (ad === 'true') return true;
                if (/(^|\s)(disabled|is-disabled|btn-disabled)(\s|$)/i.test(el.className||'')) return true;
                const cs = window.getComputedStyle(el);
                if (cs && (cs.pointerEvents === 'none' || cs.cursor === 'not-allowed')) return true;
                return false;
            };
            const all = [...document.querySelectorAll('button, [role="button"], a[role="button"]')];
            for (const el of all) {
                try {
                    if (el.offsetParent === null) continue;
                    const t = ((el.innerText||'') + ' ' + (el.getAttribute('aria-label')||'') + ' ' + (el.getAttribute('title')||'')).trim();
                    if (RX_BAD.test(t)) continue;
                    if (!RX_GOOD.test(t)) continue;
                    if (isInCustomizeMenu(el)) continue;
                    if (isDisabled(el)) continue;
                    el.scrollIntoView({block: 'center', behavior: 'instant'});
                    el.click();
                    return true;
                } catch(e) {}
            }
            return false;
        }''')
        if not clicked:
            return ('fail:click_failed', '')
        page.wait_for_timeout(2000)

        # 3) modal 이 떴는지 확인 — 안 떴으면 1차 click 이 메뉴 아이템 토글이었거나
        #    aria-hidden 으로 무시된 케이스. 보수적으로 fail 처리.
        def _modal_state():
            return page.evaluate(r'''() => {
                const sels = [
                    '[role="dialog"]:not([aria-hidden="true"])',
                    '[role="alertdialog"]:not([aria-hidden="true"])',
                    '.ant-modal:not(.ant-modal-hidden)',
                    '.MuiDialog-root',
                    '[class*="Modal__container"]',
                    '[class*="modal-dialog"]',
                    '[class*="ConfirmDialog"]',
                ];
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) {
                        const t = (el.innerText || '').slice(0, 600);
                        return {open: true, text: t, sel: s};
                    }
                }
                return {open: false};
            }''') or {'open': False}

        modal0 = _modal_state()
        if not modal0.get('open'):
            # 1.5초 더 기다려도 안 뜨면 modal-less submit 흐름일 가능성.
            page.wait_for_timeout(1500)
            modal0 = _modal_state()

        modal_after_first = modal0.get('open', False)
        if modal_after_first:
            log(f'submit_alpha: modal opened sel={modal0.get("sel","")!r} '
                f'snippet={(modal0.get("text") or "")[:120]!r}')
        else:
            # modal 없는 modal-less submit — WQB 가 confirm 없이 즉시 backend 호출하고
            # toast/snackbar 로 결과 알려주는 흐름. 'submitted' 표시는 explicit success
            # 신호 (success toast/redirect/PENDING→PASS 전환) 가 있을 때만 인정.
            log('submit_alpha: no modal after click — modal-less flow, toast 폴링 진입')

        confirm = {'clicked': False, 'label': '', 'match': 'no_modal'}
        if modal_after_first:
            # 4) modal 안에서만 confirm 클릭 — page-level 동일 'Submit Alpha' 버튼 재 click 금지.
            confirm = page.evaluate(r'''(firstRect) => {
                const RX_EXACT = /^(submit|submit\s+alpha|confirm|yes|ok|agree|proceed|i agree|i understand|제출|확인)$/i;
                const RX_LOOSE = /(^|\s)(submit|confirm|i agree|i understand|proceed|agree)(\s|$)/i;
                const RX_BAD = /cancel|close|continue|닫기|취소|never\s+show/i;
                const sels = [
                    '[role="dialog"]:not([aria-hidden="true"])',
                    '[role="alertdialog"]:not([aria-hidden="true"])',
                    '.ant-modal:not(.ant-modal-hidden)',
                    '.MuiDialog-root',
                    '[class*="Modal__container"]',
                    '[class*="modal-dialog"]',
                    '[class*="ConfirmDialog"]',
                ];
                let dlg = null;
                for (const s of sels) {
                    const el = document.querySelector(s);
                    if (el && el.offsetParent !== null) { dlg = el; break; }
                }
                if (!dlg) return {clicked: false, reason: 'no_dialog'};
                const isSameAsFirst = (b) => {
                    if (!firstRect) return false;
                    const r = b.getBoundingClientRect();
                    const dx = Math.abs(Math.round(r.x) - firstRect.x);
                    const dy = Math.abs(Math.round(r.y) - firstRect.y);
                    return dx < 3 && dy < 3;
                };
                const btns = [...dlg.querySelectorAll('button, [role="button"]')];
                for (const b of btns) {
                    if (b.offsetParent === null || b.disabled) continue;
                    const t = (b.innerText || '').trim();
                    if (RX_BAD.test(t)) continue;
                    if (RX_EXACT.test(t)) {
                        if (isSameAsFirst(b)) continue;
                        b.click();
                        return {clicked: true, label: t.slice(0,40), match: 'exact'};
                    }
                }
                for (const b of btns) {
                    if (b.offsetParent === null || b.disabled) continue;
                    const t = (b.innerText || '').trim();
                    if (RX_BAD.test(t)) continue;
                    if (RX_LOOSE.test(t)) {
                        if (isSameAsFirst(b)) continue;
                        b.click();
                        return {clicked: true, label: t.slice(0,40), match: 'loose'};
                    }
                }
                return {clicked: false, reason: 'no_confirm_button_in_modal'};
            }''', first_rect)
            if not confirm or not confirm.get('clicked'):
                # 모달 내부에서 confirm 못 찾음. WQB 는 PASS 7/7 알파에서 confirm 을
                # 모달이 아니라 *페이지 우하단의 초록색 'Submit alpha' 버튼* 으로
                # 노출한다 → 페이지 전역에서 (1차 클릭 버튼은 제외) 그 버튼을
                # 텍스트·초록색·우하단 위치 점수로 골라 클릭하는 fallback.
                log(f'submit_alpha: modal confirm 실패 ({confirm}) — page-level 우하단 fallback')
                pl = page.evaluate(r'''(firstRect) => {
                    const RX_GOOD = /^\s*submit(\s+alpha)?\s*$|^\s*confirm\s*$|^\s*제출\s*$|^\s*확인\s*$/i;
                    const RX_BAD = /cancel|close|continue|simulate|submitted|resubmit|닫기|취소|never\s+show/i;
                    const sameAsFirst = (r) => firstRect
                        && Math.abs(Math.round(r.x) - firstRect.x) < 3
                        && Math.abs(Math.round(r.y) - firstRect.y) < 3;
                    const isGreen = (cs) => {
                        const m = (cs.backgroundColor || '').match(/rgba?\(([^)]+)\)/);
                        if (!m) return false;
                        const p = m[1].split(',').map(s => parseFloat(s));
                        const a = p.length > 3 ? p[3] : 1;
                        if (a < 0.3) return false;
                        return p[1] > 90 && p[1] > p[0] * 1.15 && p[1] > p[2] * 1.15;
                    };
                    const vw = window.innerWidth, vh = window.innerHeight;
                    const cands = [];
                    for (const el of document.querySelectorAll('button, [role="button"], a[role="button"]')) {
                        if (el.offsetParent === null || el.disabled) continue;
                        const t = (el.innerText || '').trim();
                        if (!t || RX_BAD.test(t) || !RX_GOOD.test(t)) continue;
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) continue;
                        if (sameAsFirst(r)) continue;
                        const cs = getComputedStyle(el);
                        if (cs.pointerEvents === 'none' || cs.visibility === 'hidden') continue;
                        const green = isGreen(cs);
                        const score = (r.right / vw) + (r.bottom / vh)
                            + (green ? 2 : 0) + (/^\s*submit\s+alpha\s*$/i.test(t) ? 1 : 0);
                        cands.push({el, t, r, green, bg: cs.backgroundColor, score});
                    }
                    if (!cands.length) return {clicked: false, reason: 'no_pagelevel_submit_btn'};
                    cands.sort((a, b) => b.score - a.score);
                    const pk = cands[0];
                    try { pk.el.scrollIntoView({block: 'center', behavior: 'instant'}); } catch (e) {}
                    pk.el.click();
                    return {clicked: true, label: pk.t.slice(0, 40), match: 'page_bottom_right',
                            green: pk.green, bg: pk.bg,
                            rect: {x: Math.round(pk.r.x), y: Math.round(pk.r.y)},
                            cand_count: cands.length};
                }''', first_rect)
                if pl and pl.get('clicked'):
                    confirm = pl
                    log(f"submit_alpha: page-level confirm clicked label={pl.get('label')!r} "
                        f"green={pl.get('green')} bg={pl.get('bg')!r} rect={pl.get('rect')} "
                        f"cands={pl.get('cand_count')}")
                else:
                    # 모달·페이지 전역 모두 실패 → 스크린샷 + 전체 버튼 인벤토리 덤프
                    # (사용자가 우하단 초록 버튼을 스크린샷으로 검증할 수 있도록).
                    log(f'submit_alpha: page-level fallback 도 실패 — {pl}')
                    try:
                        _dump_failure(page, 'confirm_not_found')
                        binv = page.evaluate(r'''() =>
                            [...document.querySelectorAll('button,[role="button"],a[role="button"]')]
                              .filter(b => b.offsetParent !== null)
                              .map(b => { const r = b.getBoundingClientRect();
                                          const cs = getComputedStyle(b);
                                return {t:(b.innerText||'').trim().slice(0,40),
                                        x:Math.round(r.x), y:Math.round(r.y),
                                        w:Math.round(r.width), h:Math.round(r.height),
                                        bg:cs.backgroundColor, dis:!!b.disabled,
                                        cls:(b.className||'').toString().slice(0,80)}; })
                              .slice(0, 60)''')
                        log(f'submit_alpha: BUTTON_INVENTORY {json.dumps(binv, ensure_ascii=False)[:1800]}')
                    except Exception as _de:
                        log(f'submit_alpha: confirm fail dump err {_de}')
                    try:
                        page.keyboard.press('Escape')
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    return ('fail:confirm_button_not_found',
                            f'modal+page-level 모두 confirm 버튼 매칭 실패: modal={confirm} page={pl}')

            log(f'submit_alpha: confirm clicked label={confirm.get("label","")!r} match={confirm.get("match","")!r}')
            page.wait_for_timeout(2500)
        else:
            # modal-less submit — 1차 click 자체로 backend 호출 트리거.
            # 단, WQB 가 detector 가 못 잡는 class 의 confirm dialog 를 띄웠을 가능성 →
            # 페이지 전역에서 'Submit/Confirm/Yes' 류 버튼이 1차 버튼과 '다른 위치'에 새로
            # 나타났고 그게 작은 컨테이너(dialog/popup) 안이면 그것도 click.
            page.wait_for_timeout(1200)
            sweep = page.evaluate(r'''(firstRect) => {
                const RX_OK = /^(submit|submit\s+alpha|confirm|yes|ok|agree|proceed|i\s+agree|i\s+understand|제출|확인)$/i;
                const RX_BAD = /cancel|close|continue|닫기|취소|never|simulate|customize/i;
                const all = [...document.querySelectorAll('button, [role="button"]')];
                for (const b of all) {
                    try {
                        if (b.offsetParent === null || b.disabled) continue;
                        const t = (b.innerText || '').trim();
                        if (!t || RX_BAD.test(t) || !RX_OK.test(t)) continue;
                        const r = b.getBoundingClientRect();
                        if (firstRect && Math.abs(Math.round(r.x)-firstRect.x)<3 && Math.abs(Math.round(r.y)-firstRect.y)<3) continue;
                        let p = b, container = null;
                        for (let i=0;i<10 && p;i++){
                            const role=(p.getAttribute&&p.getAttribute('role'))||'';
                            const am=(p.getAttribute&&p.getAttribute('aria-modal'))||'';
                            if (role==='dialog'||role==='alertdialog'||am==='true'||/dialog|popup|modal|confirm/i.test(p.className||'')){ container=p; break; }
                            p=p.parentElement;
                        }
                        if (!container) continue;
                        if ((container.innerText||'').length > 2000) continue;
                        b.scrollIntoView({block:'center',behavior:'instant'});
                        b.click();
                        return {clicked:true, label:t.slice(0,40), container:(container.className||'').slice(0,80)};
                    } catch(e){}
                }
                return {clicked:false};
            }''', first_rect)
            if sweep and sweep.get('clicked'):
                log(f'submit_alpha: page-sweep confirm clicked label={sweep.get("label","")!r} container={sweep.get("container","")!r}')
                confirm = {'clicked': True, 'label': sweep.get('label', ''), 'match': 'page_sweep'}
                page.wait_for_timeout(2000)
            else:
                confirm = {'clicked': True, 'label': info.get('label', ''), 'match': 'modal_less'}
                page.wait_for_timeout(1500)

        # 5) modal close 확인.
        modal_open = _modal_state().get('open', False)

        # 5) 결과 신호 폴링 — modal 텍스트 / toast / snackbar / IS Tests 패널 변화.
        #    PASS=7 알파를 Submit 하면 WQB 가 그 시점에 self-correlation 검사를 서버에서 수행.
        #    이 검사는 부하에 따라 수초~3분까지 걸릴 수 있음. 끝나면 'Self-correlation' 항목이
        #    PENDING → (값<0.7) PASS=8 / (값>=0.7) FAIL 로 이동하고 실측값(예 0.9415)이 노출됨.
        #    → 60s → 최대 150s 폴링 (2초 간격 × 75). 30s 마다 진단 로그.
        reject_info = {'rejected': False}
        success_info = {'success': False}
        post_pass = post_fail = post_err = post_pending = []
        MAX_POLL_ITERS = 75  # × 2s = 150s
        for _attempt in range(MAX_POLL_ITERS):
            check = page.evaluate(r'''() => {
                // Toast / dialog 등 작은 element 에서는 broad 패턴.
                const RX_TOAST_REJECT = /(\d+\s+tests?\s+failed|correlation\s+(too|is)\s+high|self[\s-]*correlation\s+(of\s+[\d.]|exceeds|too\s+high|is\s+[\d.]|against)|cannot\s+submit|submission\s+failed|submit\s+failed|alpha\s+(was\s+)?rejected|not\s+submittable|^\s*failed\.?\s*$|\bfailed\b)/i;
                // 성공 toast/메시지.
                const RX_TOAST_SUCCESS = /(successfully\s+submitted|submission\s+(was\s+)?successful|alpha\s+(has\s+been\s+)?submitted|submitted\s+successfully|제출되었|성공적으로\s+제출)/i;
                // Body 전체에는 specific 패턴만 (false positive 방지).
                const RX_BODY_REJECT = /(\d+\s+tests?\s+failed|correlation\s+(too|is)\s+high|self[\s-]*correlation\s+(of\s+[\d.]|exceeds|too\s+high|is\s+[\d.]|against\s+your)|cannot\s+submit|submission\s+failed|submit\s+failed|alpha\s+(was\s+)?rejected|not\s+submittable)/i;
                // self-corr 실측값 추출 — "self correlation ... is 0.9415" / "of 0.9415" / "0.9415, above" 등.
                const grabCorr = (s) => { const m = (s||'').match(/self[\s-]*correlation[^0-9]{0,60}(\d+\.\d+)/i); return m ? m[1] : ((s||'').match(/(\d+\.\d+)/) ? (s||'').match(/(\d+\.\d+)/)[1] : ''); };
                const dlg = document.querySelector('[role="dialog"]:not([aria-hidden="true"]), [role="alertdialog"]:not([aria-hidden="true"]), .ant-modal:not(.ant-modal-hidden), .modal:not(.hidden), [aria-modal="true"]');
                if (dlg && dlg.offsetParent !== null) {
                    const txt = (dlg.innerText || '').slice(0, 1200);
                    const ms = txt.match(RX_TOAST_SUCCESS);
                    if (ms) return {success: true, detail: ms[0].slice(0,80), source: 'dialog'};
                    const mr = txt.match(RX_TOAST_REJECT);
                    if (mr) return {rejected: true, detail: mr[0].slice(0,80), corr: grabCorr(txt), source: 'dialog:'+txt.slice(0,140)};
                }
                const toasts = [...document.querySelectorAll('[class*="toast"], [class*="Toast"], [class*="snackbar"], [class*="Snackbar"], [class*="notification"], [class*="Notification"], [role="alert"], [role="status"], [class*="message"], [class*="banner"], [class*="alert"]')];
                for (const t of toasts) {
                    if (t.offsetParent === null) continue;
                    const txt = t.innerText || '';
                    if (txt.length > 600) continue;
                    const ms = txt.match(RX_TOAST_SUCCESS);
                    if (ms) return {success: true, detail: ms[0].slice(0,80), source: 'toast:'+txt.slice(0,140)};
                    const mr = txt.match(RX_TOAST_REJECT);
                    if (mr) return {rejected: true, detail: mr[0].slice(0,80), corr: grabCorr(txt), source: 'toast:'+txt.slice(0,140)};
                }
                const bodyTxt = (document.body.innerText || '').slice(0, 8000);
                const ms = bodyTxt.match(RX_TOAST_SUCCESS);
                if (ms) return {success: true, detail: ms[0].slice(0,80), source: 'body'};
                const mr = bodyTxt.match(RX_BODY_REJECT);
                if (mr) return {rejected: true, detail: mr[0].slice(0,80), corr: grabCorr(bodyTxt.slice(Math.max(0,bodyTxt.search(RX_BODY_REJECT)-20), bodyTxt.search(RX_BODY_REJECT)+200)), source: 'body'};
                return {};
            }''') or {}
            if check.get('success'):
                success_info = check
                break
            if check.get('rejected'):
                reject_info = check
                break
            # IS Tests 패널 변화: PASS=8 도달 → success. FAIL/ERROR > 0 → reject (self-corr 값 추출).
            try:
                post_ist = _scrape_is_testing_status(page)
                post_pass = post_ist.get('pass') or []
                post_fail = post_ist.get('fail') or []
                post_err = post_ist.get('error') or []
                post_pending = post_ist.get('pending') or []
                if len(post_pass) >= 8 and not post_fail and not post_err:
                    success_info = {'success': True, 'detail': f'PASS={len(post_pass)} all green', 'source': 'is_tests_panel'}
                    break
                if post_fail or post_err:
                    corr_val = ''
                    sc_entry = None
                    for e in (post_fail + post_err):
                        if 'correlation' in (e.get('name') or '').lower():
                            sc_entry = e
                            corr_val = (e.get('value') or '').strip()
                            if not corr_val:
                                m_ = re.search(r'(\d+\.\d+)', e.get('desc') or '')
                                if m_:
                                    corr_val = m_.group(1)
                            break
                    if sc_entry is not None:
                        cutoff_ = (sc_entry.get('cutoff') or '0.7').strip()
                        detail = (f'Self-correlation {corr_val} > {cutoff_}' if corr_val
                                  else 'Self-correlation above cutoff')
                    else:
                        names = [(e.get('name') or '?').strip() for e in (post_fail + post_err)][:3]
                        detail = 'post-submit fail: ' + ', '.join(n for n in names if n)
                    reject_info = {'rejected': True, 'detail': detail[:80], 'corr': corr_val, 'source': 'is_tests_panel'}
                    break
            except Exception:
                pass
            # 30s (15 iters) 마다 진단 로그 — stuck 케이스 디버깅용.
            if _attempt > 0 and _attempt % 15 == 0:
                try:
                    dbg = page.evaluate(r'''() => {
                        const btns = [...document.querySelectorAll('button,[role="button"]')]
                            .filter(b=>b.offsetParent!==null && /submit/i.test((b.innerText||'').trim()))
                            .map(b=>({t:(b.innerText||'').trim().slice(0,30), dis:!!b.disabled, ad:b.getAttribute('aria-disabled')||''}));
                        const toasts = [...document.querySelectorAll('[class*="toast"],[class*="snackbar"],[role="alert"],[role="status"],[class*="notification"]')]
                            .filter(t=>t.offsetParent!==null).map(t=>(t.innerText||'').slice(0,140)).filter(x=>x);
                        return {btns: btns.slice(0,4), toasts: toasts.slice(0,4)};
                    }''') or {}
                    log(f'submit_alpha: poll {_attempt*2}s — btns={dbg.get("btns")} toasts={dbg.get("toasts")} '
                        f'pending={[(e.get("name") or "?") for e in (post_pending or [])][:4]} '
                        f'P={len(post_pass)} F={len(post_fail)} E={len(post_err)}')
                except Exception:
                    pass
            page.wait_for_timeout(2000)

        if reject_info.get('rejected'):
            corr_s = (reject_info.get('corr') or '').strip()
            base = (reject_info.get('detail') or 'rejected').strip()
            if corr_s and corr_s not in base:
                reason = f'{base} (self-corr {corr_s})'[:75]
            else:
                reason = base[:75]
            log(f'submit_alpha: REJECTED — detail={base!r} corr={corr_s!r} src={reject_info.get("source","")[:140]!r}')
            if modal_open or _modal_state().get('open'):
                try:
                    page.keyboard.press('Escape'); page.wait_for_timeout(400)
                except Exception:
                    pass
            return (f'rejected:{reason}', f'confirm={confirm!r} src={reject_info.get("source","")[:140]!r}')

        if success_info.get('success'):
            log(f'submit_alpha: SUCCESS — detail={success_info.get("detail","")!r} src={success_info.get("source","")[:80]!r}')
            return ('submitted', f'confirm={confirm!r} src={success_info.get("source","")[:80]!r}')

        if modal_open or _modal_state().get('open'):
            try:
                page.keyboard.press('Escape'); page.wait_for_timeout(400)
            except Exception:
                pass
            if modal_after_first:
                return ('fail:modal_did_not_close', f'confirm={confirm!r}')

        # 150초 폴링 동안 reject/success 신호 모두 못 잡음.
        # 정책상 '구체 수치 동반 self-corr 거절' 이 아니면 제출 간주이므로
        # modal-less/무응답 케이스도 submitted 로 본다 (rescrape 가 한 번 더
        # 늦은 진짜 self-corr 거절을 확인). 진단용으로 detail 만 남긴다.
        # 'UNCONFIRMED ' detail 접두사 = '명시 success 신호 없이 제출 간주' →
        # 호출부가 지연(1~2분) self-corr 재확인을 한 번 더 돌리게 하는 신호.
        # status 자체는 'submitted' 유지 (SSOT/worker 영향 없음).
        if modal_after_first or confirm.get('match') == 'page_sweep':
            log('submit_alpha: confirm 클릭됨 + 150s 내 명시 신호 없음 — submitted 간주 (지연확인 대상)')
            return ('submitted', f'UNCONFIRMED confirm={confirm!r} (no explicit signal in 150s)')
        log('submit_alpha: modal-less + no signal in 150s — submitted 간주 (지연확인 대상)')
        return ('submitted',
                f'UNCONFIRMED no_response_modal_less: modal/toast/IS변화 모두 없음 (150s polled). '
                f'label={info.get("label","")!r}')
    except Exception as e:
        # 예외도 '구체 수치 self-corr 거절' 이 아니므로 제출 간주 (지연확인 대상).
        log(f'submit_alpha: exception — submitted 간주 (지연확인 대상): {str(e)[:120]}')
        return ('submitted', f'UNCONFIRMED exception(treated submitted): {str(e)[:150]}')


def _rescrape_submit_outcome(page, retries=15, interval_ms=8000):
    # Submit 클릭 후 IS Tests 패널을 재시도 폴링 — WQB 의 self-correlation 검사가
    # 끝나길 기다림 (기본 ~120s = retries 15 × 8s, 최대 1~2분 지연 케이스 커버).
    # PASS=8 → 'success' / FAIL·ERROR 항목 등장 → 'reject' (self-corr 값 추출).
    # 둘 다 안 잡혔지만 패널 스크랩은 됐고 self-corr 거절 신호가 한 번도 안 떴으면 →
    # 'success_implied' (WQB 는 제출 거절 시 반드시 Self-Correlation 항목에 실측값을 노출하므로,
    # 그 신호의 부재 == 사실상 제출 성공. modal-less 라 confirm 모달만 못 본 케이스).
    # 반환: {'ist': dict|None, 'verdict': 'success'|'success_implied'|'reject'|'none',
    #        'self_corr': str, 'is_selfcorr': bool, 'pfn': int, 'ppn': int, 'ppen': int}
    out = {'ist': None, 'verdict': 'none', 'self_corr': '', 'is_selfcorr': False,
           'pfn': 0, 'ppn': 0, 'ppen': 0}
    for _rs in range(retries):
        page.wait_for_timeout(interval_ms)
        try:
            ist = _scrape_is_testing_status(page)
        except Exception as ex_:
            log(f'rescrape submit outcome skipped: {ex_}')
            break
        ppn = len(ist.get('pass', []) or []); pfn = len(ist.get('fail', []) or [])
        pen = len(ist.get('error', []) or []); ppen = len(ist.get('pending', []) or [])
        out['ist'] = ist; out['pfn'] = pfn; out['ppn'] = ppn; out['ppen'] = ppen
        if ppn >= 8 and not pfn and not pen:
            log(f'rescrape({_rs+1}/{retries}): PASS={ppn} all green — 제출 성공으로 정정')
            out['verdict'] = 'success'
            return out
        if (ppn + pfn + pen + ppen) >= 7 and (pfn or pen):
            scv = ''
            is_sc = False
            for e in (ist.get('fail') or []) + (ist.get('error') or []):
                if 'correlation' in (e.get('name') or '').lower():
                    is_sc = True
                    scv = (e.get('value') or '').strip()
                    if not scv:
                        m_ = re.search(r'(\d+\.\d+)', e.get('desc') or '')
                        if m_:
                            scv = m_.group(1)
                    break
            log(f'rescrape({_rs+1}/{retries}): PASS={ppn} FAIL={pfn} ERROR={pen} PENDING={ppen} '
                f'self-corr={scv or ("yes" if is_sc else "no")}')
            out['verdict'] = 'reject'; out['self_corr'] = scv; out['is_selfcorr'] = is_sc
            return out
    # 폴링 끝 — PASS=8 도 self-corr 거절도 못 잡음. 패널 스크랩이 됐고(ist != None) 패널이
    # 정상적으로 채워져 있으면(PASS+PENDING 합이 알파 1개분 ~7~8) self-corr 거절 신호가
    # 없는 것이므로 제출 성공으로 추정. 스크랩 자체가 실패했으면(ist == None) 'none' 유지.
    if out['ist'] is not None and (out['ppn'] + out['ppen']) >= 7 and out['pfn'] == 0:
        log(f"rescrape: {retries}회 폴링 동안 self-corr 거절 신호 없음 "
            f"(PASS={out['ppn']} PENDING={out['ppen']} FAIL={out['pfn']}) — 제출 성공으로 추정")
        out['verdict'] = 'success_implied'
    return out


def collect_full_metrics(page, summary_metrics):
    # 1) 'Show Test Results' 버튼 클릭 → IS Testing Status 패널 노출
    # 2) 패널 텍스트 → PASS/FAIL/PENDING 항목 분류 + 각 항목의 value/cutoff
    # 반환: { 'metrics': summary_metrics 그대로, 'is_status': {pass, fail, pending, raw} }

    # 이전 알파의 panel 잔재 제거 — 'Hide test period' 클릭으로 panel close 후 재오픈.
    # 이전 PENDING/PASS 항목 누적되어 합 != 8 anomaly 유발. close 하면 panel 비고 새 sim
    # 결과로 채워짐.
    try:
        hidden = page.evaluate(r'''() => {
            const btns = [...document.querySelectorAll('button, a, [role="button"]')];
            for (const el of btns) {
                if (el.offsetParent === null) continue;
                const t = (el.innerText || '').trim();
                if (/^Hide\s+test\s+period/i.test(t)) { el.click(); return t.slice(0,40); }
            }
            return '';
        }''')
        if hidden:
            log(f'show_test_results: pre-close panel via {hidden!r}')
            page.wait_for_timeout(700)
    except Exception:
        pass

    clicked = _click_show_test_results(page)
    if clicked:
        page.wait_for_timeout(2000)
    else:
        page.wait_for_timeout(500)
    def _counts(st):
        return (len(st.get('pass', [])), len(st.get('fail', [])),
                len(st.get('error', [])), len(st.get('pending', [])))

    is_status = _scrape_is_testing_status(page)
    p, f, e, pn = _counts(is_status)
    # Retry 1 — panel async 로딩 중. 8초 더 wait.
    if (p + f + e + pn) == 0:
        log('is_tests scrape: empty 1st, retry 8s')
        page.wait_for_timeout(8000)
        is_status = _scrape_is_testing_status(page)
        p, f, e, pn = _counts(is_status)
    # Retry 2 — 'Show test period' 매칭 실패 또는 toggle 닫힘. 다른 trigger 시도.
    if (p + f + e + pn) == 0:
        log('is_tests scrape: empty 2nd, alt trigger + 8s')
        try:
            _try_alternative_panel_trigger(page)
        except Exception:
            pass
        page.wait_for_timeout(8000)
        is_status = _scrape_is_testing_status(page)
        p, f, e, pn = _counts(is_status)
    # Retry 3 — partial render (0 < sum < 8). 패널이 아직 다 안 그려졌을 가능성. 5s 더 wait.
    if 0 < (p + f + e + pn) < 8:
        log(f'is_tests scrape: partial sum={p+f+e+pn}, retry 5s')
        page.wait_for_timeout(5000)
        is_status_2 = _scrape_is_testing_status(page)
        p2, f2, e2, pn2 = _counts(is_status_2)
        if (p2 + f2 + e2 + pn2) > (p + f + e + pn):
            log(f'is_tests scrape: partial recovered PASS={p2} FAIL={f2} ERROR={e2} PENDING={pn2}')
            is_status, p, f, e, pn = is_status_2, p2, f2, e2, pn2
    log(f'is_tests scrape: PASS={p} FAIL={f} ERROR={e} PENDING={pn} (clicked={clicked})')
    if (p + f + e + pn) == 0:
        # 못 찾음 — 디버그.
        log(f'is_tests body snippet: {(is_status.get("raw") or "")[:500]!r}')
    elif (p + f + e + pn) != 8 or pn > 1:
        # 합 != 8 또는 PENDING > 1 인 비정상 — section noise 잡힘. entries desc dump.
        pending_descs = [(it.get('desc') or '')[:80] for it in (is_status.get('pending') or [])]
        pass_descs = [(it.get('desc') or '')[:60] for it in (is_status.get('pass') or [])]
        fail_descs = [(it.get('desc') or '')[:60] for it in (is_status.get('fail') or [])]
        error_descs = [(it.get('desc') or '')[:60] for it in (is_status.get('error') or [])]
        log(f'is_tests anomaly sum={p+f+e+pn} PASS_descs={pass_descs}')
        log(f'is_tests anomaly FAIL_descs={fail_descs}')
        log(f'is_tests anomaly ERROR_descs={error_descs}')
        log(f'is_tests anomaly PENDING_descs={pending_descs}')
    return {'metrics': dict(summary_metrics or {}), 'is_status': is_status}


def is_done_after(before_metrics, state_obj):
    # IS Tests 패널(`N PASS / N FAIL / N PENDING` 헤더) 또는 compile/lint 에러 만
    # 진짜 done 시그널. 같은 페이지 재사용 시 DOM 에 직전 알파의 sharpe/fitness 가 남아
    # state.metrics 에 잡힐 수 있어, metrics 변화 기반 done 판정은 false-positive 위험.
    # 사용자 요구: "IS Testing Panel 미수신이면 억지로 만들지 말고 에러값 내뱉기."
    if state_obj.get('error_text'):
        return True
    if state_obj.get('is_tests_visible'):
        return True
    return False

results = [{'slot': i+1, 'code': formulas[i], 'summary_metrics': {},
            'pass_count': 0, 'pass_items': [], 'fail_count': 0, 'fail_items': [],
            'error_text': '', 'before_metrics': {},
            'submitted': False, 'submit_status': ''} for i in range(N)]

try:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE, headless=True,
            viewport={'width': 1600, 'height': 900},
            args=['--no-sandbox', '--disable-blink-features=AutomationControlled'],
        )
        # 누적 탭 정리 — persistent profile 은 이전 시뮬 탭을 보존. 매 배치 시작 시
        # 여러 탭 다 닫고 첫 페이지만 남겨둠 (메모리/JS heap 압박 + stale state 방지).
        try:
            extra_pages = list(ctx.pages[1:]) if len(ctx.pages) > 1 else []
            for ep in extra_pages:
                try: ep.close()
                except Exception: pass
            if extra_pages:
                log(f'closed {len(extra_pages)} stale tabs at batch start')
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(20000)

        # 모든 navigation 직전에 intro.js 를 중성화 — WQB 가 onboarding 띄울 때
        # introJs() 객체의 메서드들이 다 no-op 이라 tutorial DOM 자체가 안 그려진다.
        # 이미 그려진 페이지에는 효과 없으므로 js_dismiss_introjs 도 병행.
        try:
            ctx.add_init_script(r'''
                (function() {
                    function makeNoopJs() {
                        const inst = {};
                        const noop = function() { return inst; };
                        const methods = ['start','exit','refresh','setOption','setOptions',
                            'goToStep','goToStepNumber','nextStep','previousStep',
                            'onbeforechange','onchange','onafterchange','oncomplete',
                            'onexit','onbeforeexit','onhintclick','onhintsadded',
                            'onhintclose','addHints','hideHint','hideHints','showHint',
                            'showHints','removeHints','clone','introJs'];
                        methods.forEach(function(m) { inst[m] = noop; });
                        return inst;
                    }
                    try {
                        Object.defineProperty(window, 'introJs', {
                            configurable: true, writable: false,
                            value: function() { return makeNoopJs(); }
                        });
                    } catch(e) {
                        try { window.introJs = function() { return makeNoopJs(); }; } catch(e2) {}
                    }
                })();
            ''')
            log('init_script: introJs neutralized')
        except Exception as e:
            log(f'init_script intro neutralize fail: {e}')

        log(f'navigate to {SIMULATE_URL}')
        try:
            page.goto(SIMULATE_URL, wait_until='domcontentloaded', timeout=30000)
        except PWTimeout:
            log('navigate timeout, continuing')
        page.wait_for_timeout(2500)

        # cookie banner 같은 게 로그인 폼 위에 떠있을 수 있으므로 한번 dismiss.
        js_dismiss_overlays(page)
        page.wait_for_timeout(800)

        if page.locator('input[type="password"]').count() > 0:
            log('login form seen')
            try:
                page.locator('input[type="email"], input[name="email"], input[type="text"]').first.fill(USERNAME)
                page.locator('input[type="password"]').first.fill(PASSWORD)
                page.locator('button[type="submit"]').first.click()
                page.wait_for_url('**/simulate**', timeout=30000)
                page.wait_for_timeout(2500)
            except Exception as e:
                log(f'login error: {e}')

        # 신규 디바이스 인증 페이지 감지 — 자동화 불가능, 명시 에러.
        block = detect_auth_block(page)
        if block == 'auth_required':
            log('auth_required: WQB requires verification code (new device / 2FA)')
            raise RuntimeError('playwright_setup_fail: WQB 가 새 디바이스 인증을 요구함. 사용자가 한 번 수동 로그인하여 인증을 마친 뒤 재시도 필요.')

        # welcome modal / sidebar tour / cookie banner 등 dismiss — 두 번 호출 (chain modal).
        js_dismiss_overlays(page)
        page.wait_for_timeout(700)
        js_dismiss_overlays(page)
        page.wait_for_timeout(800)
        # intro.js 튜토리얼 — Simulate 버튼이 'intro-step-N' / '--disabled-example' 로 잠길 수
        # 있으므로 세션 시작 시 무조건 한번 강제 종료.
        js_dismiss_introjs(page)
        page.wait_for_timeout(500)
        js_dismiss_introjs(page)
        page.wait_for_timeout(500)
        # WQB 'Tutorial / Results' 체크박스 토글 — Tutorial OFF / Results ON.
        # 이 체크박스 상태가 풀려야 Simulate 버튼이 --disabled-example 잠금에서 풀린다.
        # (사용자가 명시한 매뉴얼 동작: 'Tutorial 체크박스 해제 후 Result 체크박스 체크')
        # 한번에 안 풀릴 수 있어 setting + UI 안정 후 1회 더.
        toggle_tutorial_checkbox(page)
        page.wait_for_timeout(800)
        toggle_tutorial_checkbox(page)
        page.wait_for_timeout(500)
        # DEBUG: 첫 세션에서만 우상단 영역 스크린샷 + DOM dump (Tutorial 체크박스 찾기용).
        # 환경변수 IQC_DEBUG_TOPRIGHT=1 일 때만 활성화.
        if os.environ.get('IQC_DEBUG_TOPRIGHT') == '1':
            try:
                debug_out = os.path.expanduser('~/.hyfe_iqc_tmp/debug_topright')
                page.screenshot(path=f'{debug_out}_full.png', full_page=False)
                page.screenshot(path=f'{debug_out}_TR.png',
                                clip={'x': 800, 'y': 0, 'width': 800, 'height': 450})
                page.screenshot(path=f'{debug_out}_TR_tight.png',
                                clip={'x': 1100, 'y': 0, 'width': 500, 'height': 250})
                tr_info = page.evaluate(r'''() => {
                    const out = [];
                    const TR_X = 900, TR_Y = 250;
                    const sel = 'button, input, [role="checkbox"], [role="switch"], [aria-checked], label, [class*="checkbox" i], [class*="toggle" i], [class*="switch" i]';
                    for (const el of document.querySelectorAll(sel)) {
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        if (el.offsetParent === null) continue;
                        if (r.left < TR_X || r.top > TR_Y) continue;
                        out.push({
                            tag: el.tagName,
                            text: ((el.innerText || el.textContent || '').trim()).slice(0, 60),
                            aria_label: el.getAttribute('aria-label') || '',
                            aria_checked: el.getAttribute('aria-checked') || '',
                            role: el.getAttribute('role') || '',
                            type: el.getAttribute('type') || '',
                            cls: (el.className || '').toString().slice(0, 150),
                            rect: {x: Math.round(r.left), y: Math.round(r.top),
                                   w: Math.round(r.width), h: Math.round(r.height)},
                            outer: (el.outerHTML || '').slice(0, 400),
                        });
                    }
                    return out;
                }''')
                import json as _json
                with open(f'{debug_out}_TR.json', 'w', encoding='utf-8') as _f:
                    _json.dump(tr_info, _f, ensure_ascii=False, indent=2)
                log(f'DEBUG_TOPRIGHT: dumped {len(tr_info)} elements to {debug_out}_TR.json + screenshots')
            except Exception as _e:
                log(f'DEBUG_TOPRIGHT failed: {_e}')

        # 시뮬 인터페이스가 로드될 때까지 짧게 polling — 빈 페이지 (탭 0 개) 일 때 안전.
        # 시뮬 탭이 적어도 1개 보이거나, '+' 버튼이 보일 때까지 최대 10초 대기.
        try:
            page.wait_for_function('''() => {
                const tabs = document.querySelectorAll('.editor-tabs__tab-element');
                const add = document.querySelector('.editor-tabs__tab-add, [class*="tab-add"]');
                return tabs.length > 0 || !!add;
            }''', timeout=10000)
        except PWTimeout:
            log('simulate UI not ready after 10s — proceeding anyway')

        before_tabs = get_tab_labels(page)
        before_labels = {t['label'] for t in before_tabs}
        log(f'before: {len(before_tabs)} tabs')

        # 1탭 순차가 기본. 환경변수 IQC_PARALLEL_SLOTS 로 > 1 가 들어오면 그 수만큼 탭을
        # 확보하여 rolling pipeline (한 알파 sim 끝나면 그 탭에 다음 알파 투입) 도 가능.
        PARALLEL_SLOTS = int(os.environ.get('IQC_PARALLEL_SLOTS', '1'))
        W_target = max(1, min(PARALLEL_SLOTS, N))
        need_new = max(0, W_target - len(before_tabs))
        added = 0
        attempts = 0
        max_attempts = need_new * 3 + 2
        while added < need_new and attempts < max_attempts:
            info = click_new_tab(page)
            log(f'click_new_tab attempt {attempts+1}: ok={info.get("ok")} how={info.get("how","")} '
                f'option_clicked={info.get("option_clicked","")!r} match={info.get("option_match","")} '
                f'before_n={info.get("before_n",0)}')
            if info.get('ok') and not info.get('option_clicked'):
                # 드롭다운 옵션 못 찾음 → 후보 dump.
                log(f'  menu_candidates={info.get("menu_candidates","")}')
                log(f'  trigger_outer={info.get("trigger_outer","")[:200]!r}')
            page.wait_for_timeout(2000)
            cur = get_tab_labels(page)
            new_only = [t for t in cur if t['label'] not in before_labels]
            added = len(new_only)
            attempts += 1
            if not info.get('ok') and added == 0:
                page.wait_for_timeout(800)
        log(f'tab-add finished: attempts={attempts}, new_tabs={added}, total={len(get_tab_labels(page))}')

        after_tabs = get_tab_labels(page)
        if not after_tabs:
            raise RuntimeError('playwright_setup_fail: no simulation tabs available')
        new_tabs = [t for t in after_tabs if t['label'] not in before_labels]
        if len(new_tabs) >= W_target:
            tabs = new_tabs[:W_target]
        elif len(after_tabs) >= W_target:
            tabs = after_tabs[-W_target:]
        else:
            tabs = after_tabs[:]   # 확보 가능한 만큼만 (tier 가 동시 sim 제한)

        # 1탭 순차가 기본. PARALLEL_SLOTS > 1 이고 충분한 탭이 확보됐을 때만 병렬.
        FORCE_SEQUENTIAL = os.environ.get('IQC_FORCE_SEQUENTIAL', '0') == '1'
        SEQUENTIAL = (PARALLEL_SLOTS <= 1) or FORCE_SEQUENTIAL or len(tabs) < 2 or N < 2
        if SEQUENTIAL:
            seq_label = after_tabs[0]['label']
            W = 1
            log(f'sequential mode: 1 tab × {N} formulas')
        else:
            W = len(tabs)   # 실제 확보된 탭 수로 슬롯 수 확정
            log(f'parallel rolling mode: {W} slots × {N} formulas — tabs={[t["label"] for t in tabs]}')

        def _alnum(s):
            return ''.join(c for c in s if c.isalnum())

        def _ix(fi):
            # 로그용 — formula 위치 fi → 워커가 부여한 알파 idx.
            return indices[fi] if fi < len(indices) else fi + 1

        _settings_done = {'v': False}
        _disable_settings = os.environ.get('IQC_DISABLE_SETTINGS', '0') == '1'

        def _setup_slot(tab_label, fi):
            # 탭 tab_label 을 열어 formulas[fi] 를 에디터에 넣고 검증. 성공 시 True,
            # 실패 시 results[fi]['error_text'] 설정 후 False.
            log(f'step: setup[idx{_ix(fi)}] tab={tab_label!r} formula_len={len(formulas[fi])}')
            # 1배치 시퀀셜에서 show_test_results / results-detail navigate 등으로 페이지
            # 가 simulator view 를 벗어난 채 click_tab 만 반복하면 8초 timeout 만 누적.
            # setup 진입 시 한 번 강제 복귀 — 정상이면 no-op.
            _ensure_simulator_view(page)
            # SEQUENTIAL 1탭 모드에서 잉여 탭(Simulation 2/3 등) 정리 — 잉여 탭이 동시
            # sim 슬롯을 점유하면 새 sim 클릭이 "limit of concurrent simulations" 로 거부됨.
            log(f'step: setup[idx{_ix(fi)}] pre-check SEQUENTIAL={SEQUENTIAL}')
            if SEQUENTIAL:
                try:
                    close_extra_tabs(page, keep_label=tab_label)
                except Exception as e:
                    log(f'step: setup[idx{_ix(fi)}] close_extra_tabs raised: {e}')
            if not click_tab(page, tab_label):
                log(f'step: setup[idx{_ix(fi)}] FAIL tab_click')
                results[fi]['error_text'] = 'tab click failed'
                return False
            page.wait_for_timeout(1200)
            if not wait_editor_ready(page, timeout_ms=20000):
                log(f'step: setup[idx{_ix(fi)}] editor_not_ready retry')
                click_tab(page, tab_label)
                page.wait_for_timeout(1500)
                if not wait_editor_ready(page, timeout_ms=15000):
                    log(f'step: setup[idx{_ix(fi)}] FAIL editor_mount_timeout')
                    results[fi]['error_text'] = 'editor mount timeout'
                    return False
            if not set_editor_text(page, formulas[fi]):
                log(f'step: setup[idx{_ix(fi)}] FAIL set_editor_text')
                results[fi]['error_text'] = 'set editor text failed'
                return False
            page.wait_for_timeout(500)
            cur = get_editor_text(page)
            f_alnum = _alnum(formulas[fi]); cur_alnum = _alnum(cur)
            len_ok = (f_alnum and len(cur_alnum) >= int(len(f_alnum) * 0.9)
                      and len(cur_alnum) <= int(len(f_alnum) * 1.5) + 5)
            content_ok = f_alnum and (f_alnum in cur_alnum)
            if not (len_ok and content_ok):
                log(f'step: setup[idx{_ix(fi)}] FAIL text_verify cur={cur[:120]!r}')
                results[fi]['error_text'] = f'text verify fail: editor has {cur[:200]!r}'
                return False
            log(f'step: setup[idx{_ix(fi)}] text_verified')
            # Settings 는 라운드 전체에서 1회만 적용 (WQB Settings 패널이 전역 — 매번 적용 시
            # 진행 중 sim 이 invalidate). 모든 알파가 동일 settings 사용.
            if not _settings_done['v'] and not _disable_settings:
                try:
                    s_cfg = settings_list[fi] if fi < len(settings_list) else {}
                    if s_cfg:
                        apply_settings(page, s_cfg)
                        page.wait_for_timeout(1500)
                        wait_editor_ready(page, timeout_ms=8000)
                except Exception as e:
                    log(f'apply_settings exception: {e}')
                _settings_done['v'] = True
            elif _disable_settings and not _settings_done['v']:
                log('step: settings_DISABLED (IQC_DISABLE_SETTINGS=1)')
                _settings_done['v'] = True
            return True

        def _start_sim(fi):
            # formulas[fi] 가 에디터에 들어간 상태에서 Simulate 클릭.
            # ★ 탭 재사용 함정: 직전 알파의 IS Tests 패널("7 PASS …")이 화면에 그대로 남아
            #   있을 수 있다. 그 상태로 _sim_started_at 을 찍고 poll 하면 is_done_after 가
            #   곧장 True → 직전 결과를 새 알파 결과로 오인한다. 그래서 "running 표시가
            #   떴다(= 새 sim 이 실제로 돌기 시작)" 를 확인한 뒤에야 시작 시각/before_metrics
            #   를 찍는다. 그 전 화면은 전부 옛 패널로 간주.
            started = False
            click_ok_any = False
            for attempt in range(2):
                if attempt > 0:
                    # attempt 1 실패 후 attempt 2 전 — Tutorial/intro.js 잔존물이 React state
                    # 를 잠갔을 수 있으므로 강제 reset 후 retry. 단순 재클릭만 하면 같은 상태에서
                    # 또 실패한다 (실측 패턴).
                    try: js_dismiss_introjs(page)
                    except Exception: pass
                    try: toggle_tutorial_checkbox(page)
                    except Exception: pass
                    try:
                        # 에디터 dirty state 재트리거 — End → space → backspace.
                        page.locator('.monaco-editor textarea.inputarea').first.focus(timeout=2000)
                        page.keyboard.press('End')
                        page.keyboard.press(' ')
                        page.keyboard.press('Backspace')
                        page.wait_for_timeout(500)
                    except Exception: pass
                ck = click_simulate(page)
                if ck:
                    click_ok_any = True
                # click_simulate 가 0 을 반환해도 WQB UI 가 느려서 running 표시가 뒤늦게
                # 뜨는 케이스가 있으므로 첫 attempt 에서는 무조건 running poll 까지 돌린다.
                # 두 attempt 모두 click 도 안 됐고 running 도 못 봤으면 그때 단념.
                for _ in range(16):   # ~24s 동안 running 확인
                    page.wait_for_timeout(1500)
                    st = extract_state(page)
                    if st.get('error_text'):
                        # compile/lint 에러 — sim 이 곧장 끝난 정당한 경우 (옛 패널 아님).
                        started = True
                        break
                    if st.get('running'):
                        started = True
                        break
                if started:
                    break
                log(f'step: _start_sim[idx{_ix(fi)}] running 표시 안 뜸 (attempt {attempt+1}) — Simulate 재클릭')
                page.wait_for_timeout(1200)
            if not started:
                if not click_ok_any:
                    results[fi]['error_text'] = 'simulate button not clicked'
                    return False
                results[fi]['error_text'] = 'sim did not start (직전 결과 패널이 막고 있을 수 있음)'
                return False
            results[fi]['_sim_started_at'] = time.time()
            results[fi]['before_metrics'] = extract_state(page).get('metrics') or {}
            return True

        def _collect_done(fi, state):
            if state.get('error_text'):
                results[fi]['error_text'] = state['error_text'][:600]
                return
            # ★ panel 없으면 metrics 신뢰 불가 (같은 페이지의 직전 알파 잔재일 수 있음).
            #   summary_metrics / is_status 채우지 말고 error 로 마감.
            if not state.get('is_tests_visible'):
                log(f'step: SEQ idx{_ix(fi)} _collect_done refusing — no IS Testing Panel')
                results[fi]['error_text'] = 'no IS Testing Panel (metrics unreliable — likely stale from previous alpha)'
                results[fi]['summary_metrics'] = {}
                results[fi]['is_status'] = {'pass': [], 'fail': [], 'error': [], 'pending': []}
                return
            full = collect_full_metrics(page, state.get('metrics') or {})
            results[fi]['summary_metrics'] = full['metrics']
            results[fi]['is_status'] = full['is_status']

        def _is_pass_slot(fi):
            if results[fi].get('error_text'):
                return False
            ist = results[fi].get('is_status') or {}
            p_n = len(ist.get('pass', []) or [])
            f_n = len(ist.get('fail', []) or [])
            e_n = len(ist.get('error', []) or [])
            if (p_n + f_n + e_n) == 0:
                return False
            return (p_n >= PASS_THRESHOLD and f_n == 0 and e_n == 0)

        def _corr_check_and_star(tab_label, fi):
            # 새 정책 (제출 폐지): PASS>=6 인 알파만 Correlation 상자의 V-화살표를 눌러
            # Self-Correlation Maximum 을 읽고, IS 전체 PASS(FAIL=0 & ERROR=0) 이면서
            # self-corr<=0.7 이면 알파 상세 페이지 우상단 'Add Alpha to a List' 로
            # 'Submit' 리스트에 추가한다(기존 별표 favorite 저장을 대체).
            # downstream 호환을 위해 'submitted'(=추가됨), 'submit_status'(상태문구) 필드와
            # 기존 토큰 접두사('starred'/'star_fail'/'skip_star') 를 그대로 재사용한다.
            ist = results[fi].get('is_status') or {}
            p_n = len(ist.get('pass', []) or [])
            f_n = len(ist.get('fail', []) or [])
            e_n = len(ist.get('error', []) or [])
            if (p_n + f_n + e_n) == 0:
                return
            if p_n < 6:
                return
            click_tab(page, tab_label)
            page.wait_for_timeout(700)
            sc = _read_self_correlation(page)
            results[fi]['self_corr'] = sc
            all_pass = (f_n == 0 and e_n == 0)
            sc_s = f'{sc:.4f}' if isinstance(sc, float) else '—'
            log(f'idx{_ix(fi)} corr-check: self_corr={sc_s} all_pass={all_pass} (P{p_n}/F{f_n}/E{e_n})')
            # '시도'(submit_attempt) 는 진짜 후보(=IS 전체 PASS)에 대해서만 기록한다.
            # all-pass 아닌 6+PASS 알파는 corr 를 로그로만 노출하고, 시도/카운트에는 넣지 않는다
            # (submit_status 를 비워두면 worker _on_partial 이 record_submit_attempt 를 건너뜀).
            if not all_pass:
                results[fi]['submitted'] = False
                results[fi]['submit_status'] = ''
                log(f'idx{_ix(fi)} Submit 후보 아님(all-pass 아님 F{f_n}/E{e_n}) — self-corr {sc_s}, 시도 미기록')
                return
            if sc is not None and sc <= 0.7:
                ok = _add_alpha_to_list(page)
                results[fi]['submitted'] = ok
                results[fi]['submit_status'] = (f'starred (self-corr {sc_s})' if ok
                                                else f'star_fail (self-corr {sc_s})')
                log(f'idx{_ix(fi)} {"⭐ Submit 리스트 추가 완료" if ok else "Submit 리스트 추가 실패"} (self_corr={sc_s})')
            else:
                why = 'self-corr 미수집' if sc is None else f'self-corr {sc_s}>0.7'
                results[fi]['submitted'] = False
                results[fi]['submit_status'] = f'skip_star: {why}'
                log(f'idx{_ix(fi)} Submit 리스트 추가 스킵 — {why}')

        def _emit_done(fi):
            err = results[fi].get('error_text') or ''
            if err:
                emit_partial(fi, 'error', error_text=err)
            else:
                emit_partial(fi, 'pass' if _is_pass_slot(fi) else 'fail',
                             metrics=results[fi].get('summary_metrics') or {},
                             is_status=results[fi].get('is_status') or {'pass': [], 'fail': [], 'error': [], 'pending': []},
                             submitted=results[fi].get('submitted', False),
                             submit_status=results[fi].get('submit_status', ''))

        # 처리할 formula 큐 (빈 formula 는 즉시 error 마감).
        queue = []
        for i in range(N):
            if formulas[i]:
                queue.append(i)
            else:
                results[i]['error_text'] = results[i].get('error_text') or 'empty formula'
                emit_partial(i, 'error', error_text=results[i]['error_text'])

        # 잔상 방어: sim 시작 후 이 시간 이전에 is_done_after 가 떠도 (error 아니면) 무시.
        # 진짜 WQB sim 이 이만큼 빨리 끝나지 않음 — 그 전 패널은 직전 알파의 잔상.
        MIN_SIM_SEC = int(os.environ.get('IQC_MIN_SIM_SEC', '25'))

        if SEQUENTIAL:
            # 한 탭에서 한 알파씩: setup → simulate → poll until done → 다음.
            SEQ_TRIVIAL_QUIT_SEC = 720
            SHOW_PANEL_EVERY_N_POLLS = 3
            for fi in queue:
                log(f'step: SEQ idx{_ix(fi)} start')
                if not _setup_slot(seq_label, fi):
                    emit_partial(fi, 'error', error_text=results[fi].get('error_text') or 'setup failed')
                    continue
                if not _start_sim(fi):
                    emit_partial(fi, 'error', error_text=results[fi].get('error_text') or 'simulate not clicked')
                    continue
                log(f'step: SEQ idx{_ix(fi)} sim_started, poll every {POLL_INTERVAL_SEC}s up to {SIM_MAX_WAIT_SEC}s')
                deadline = time.time() + SIM_MAX_WAIT_SEC
                t_start = time.time()
                poll_n = 0
                done = False
                while time.time() < deadline:
                    page.wait_for_timeout(POLL_INTERVAL_SEC * 1000)
                    poll_n += 1
                    state = extract_state(page)
                    cur_metrics = state.get('metrics') or {}
                    panel_seen = bool(state.get('is_tests_visible'))
                    age = int(time.time() - t_start)
                    log(f'step: SEQ idx{_ix(fi)} poll#{poll_n} age={age}s panel={panel_seen} metrics_keys={len(cur_metrics)}')
                    if poll_n % SHOW_PANEL_EVERY_N_POLLS == 0 and not panel_seen:
                        try: _click_show_test_results(page)
                        except Exception: pass
                    if is_done_after(results[fi].get('before_metrics') or {}, state) and (
                            age >= MIN_SIM_SEC or state.get('error_text')):
                        _collect_done(fi, state)
                        done = True
                        break
                    if (age > SEQ_TRIVIAL_QUIT_SEC and not panel_seen
                            and cur_metrics == (results[fi].get('before_metrics') or {})):
                        log(f'step: SEQ idx{_ix(fi)} trivial_quit → error (no panel {age}s, metrics unchanged)')
                        results[fi]['error_text'] = (
                            f'panel never showed after {age}s (metrics unchanged) — '
                            'sim likely produced no result')
                        results[fi]['summary_metrics'] = {}
                        results[fi]['is_status'] = {'pass': [], 'fail': [], 'error': [], 'pending': []}
                        done = True
                        break
                if not done and not results[fi].get('error_text'):
                    log(f'step: SEQ idx{_ix(fi)} FAIL sim wait timeout ({SIM_MAX_WAIT_SEC}s)')
                    results[fi]['error_text'] = f'sim wait timeout ({SIM_MAX_WAIT_SEC}s)'
                _corr_check_and_star(seq_label, fi)
                _emit_done(fi)
        else:
            # ── 병렬 rolling window ──────────────────────────────────────────
            # slot_fi[w] = 슬롯(탭) w 가 지금 돌리는 formula index (없으면 None).
            # 한 슬롯의 sim 이 끝나면 그 즉시 큐에서 다음 formula 를 꺼내 같은 탭에 투입.
            slot_fi = [None] * W
            # straggler 인내심 — 큐가 비었고 (= 새로 넣을 알파 없음) busy 슬롯이 하나뿐일 때
            # 그 슬롯에 줄 최대 시간. 그 외 슬롯은 SIM_MAX_WAIT_SEC. (한 무거운 sim 이 전체
            # round 를 끝까지 붙잡지 않도록.) 필요하면 IQC_DRAIN_STRAGGLER_SEC 로 조정.
            DRAIN_STRAGGLER_SEC = int(os.environ.get('IQC_DRAIN_STRAGGLER_SEC', '420'))

            def _fill_slot(w):
                # 큐에서 다음 formula 를 꺼내 슬롯 w 에 setup+simulate. setup 실패하면 그
                # 알파는 error 로 마감하고 다음 후보를 계속 시도. 슬롯이 작업을 시작했으면 True.
                while queue:
                    fi = queue.pop(0)
                    if _setup_slot(tabs[w]['label'], fi) and _start_sim(fi):
                        slot_fi[w] = fi
                        log(f'parallel: slot{w} ← idx{_ix(fi)} (queue left {len(queue)})')
                        return True
                    emit_partial(fi, 'error', error_text=results[fi].get('error_text') or 'setup failed')
                slot_fi[w] = None
                return False

            for w in range(W):
                _fill_slot(w)

            # 전체 데드라인 — 첫 wave 외에 알파 1개당 ~5분 예산. (subprocess 자체 타임아웃은
            # simulate_batch 가 알파 수에 맞춰 더 넉넉히 잡음.)
            overall_deadline = time.time() + SIM_MAX_WAIT_SEC + max(0, len(queue)) * 300
            poll_n = 0
            while any(x is not None for x in slot_fi):
                if time.time() > overall_deadline:
                    log('parallel: overall round deadline hit — force-fail remaining')
                    for w in range(W):
                        fi = slot_fi[w]
                        if fi is not None:
                            if not results[fi].get('error_text'):
                                results[fi]['error_text'] = 'sim wait timeout (round deadline)'
                            _emit_done(fi)
                            slot_fi[w] = None
                    for fi in queue:
                        results[fi]['error_text'] = 'not started (round deadline)'
                        emit_partial(fi, 'error', error_text=results[fi]['error_text'])
                    queue.clear()
                    break
                page.wait_for_timeout(POLL_INTERVAL_SEC * 1000)
                poll_n += 1
                n_busy = sum(1 for x in slot_fi if x is not None)
                for w in range(W):
                    fi = slot_fi[w]
                    if fi is None:
                        # 큐에 남은 게 있으면 (앞선 슬롯 setup 실패 등으로) 채워본다.
                        if queue:
                            _fill_slot(w)
                        continue
                    click_tab(page, tabs[w]['label'])
                    page.wait_for_timeout(800)
                    state = extract_state(page)
                    before_m = results[fi].get('before_metrics') or {}
                    sim_age = time.time() - (results[fi].get('_sim_started_at') or 0)
                    panel_seen = bool(state.get('is_tests_visible'))
                    metrics_unchanged = (state.get('metrics') or {}) == before_m
                    # 가끔 sim 끝났는데 패널 자동 출현 안 함 — 60초마다 직접 trigger.
                    if poll_n % 3 == 0 and not panel_seen and sim_age > 50:
                        try: _click_show_test_results(page)
                        except Exception: pass
                        state = extract_state(page)
                        panel_seen = bool(state.get('is_tests_visible'))
                    if is_done_after(before_m, state) and (sim_age >= MIN_SIM_SEC or state.get('error_text')):
                        _collect_done(fi, state)
                        log(f'parallel: slot{w} idx{_ix(fi)} done (age {sim_age:.0f}s)')
                        _corr_check_and_star(tabs[w]['label'], fi)
                        _emit_done(fi)
                        _fill_slot(w)
                        continue
                    # trivial-quit — 6분 무결과 + metrics 안 변함 + panel 없음 → error 마감.
                    # (panel 없으면 metrics 직전 알파 잔재일 수 있어 결과 신뢰 불가.)
                    if sim_age > 6 * 60 and metrics_unchanged and not panel_seen:
                        log(f'parallel: slot{w} idx{_ix(fi)} trivial_quit → error (no panel {sim_age:.0f}s)')
                        results[fi]['error_text'] = (
                            f'panel never showed after {int(sim_age)}s (metrics unchanged) — '
                            'sim likely produced no result')
                        results[fi]['summary_metrics'] = {}
                        results[fi]['is_status'] = {'pass': [], 'fail': [], 'error': [], 'pending': []}
                        _emit_done(fi)
                        _fill_slot(w)
                        continue
                    # straggler 타임아웃 — 큐 비고 단독 busy 슬롯이면 인내심을 줄인다.
                    limit = DRAIN_STRAGGLER_SEC if (not queue and n_busy == 1) else SIM_MAX_WAIT_SEC
                    if sim_age > limit:
                        log(f'parallel: slot{w} idx{_ix(fi)} timeout (age {sim_age:.0f}s > {limit}s)')
                        results[fi]['error_text'] = f'sim wait timeout ({int(sim_age)}s)'
                        _emit_done(fi)
                        _fill_slot(w)
                        continue

        try: ctx.close()
        except Exception: pass

except Exception as e:
    traceback.print_exc()
    # 임베디드 스크립트라 stderr 트레이스백이 부모 로그/결과에 안 남아 원인추적이
    # 어려웠음 → 실패 프레임 꼬리를 결과 payload 에도 포함(진단성 추가만, 동작 불변).
    _tb_tail = traceback.format_exc()[-800:]
    err = f'playwright_setup: {type(e).__name__}: {e} | tb:{_tb_tail}'
    for r in results:
        if not r.get('error_text'):
            r['error_text'] = err

print('RESULT_JSON:', json.dumps(results, ensure_ascii=False), flush=True)
