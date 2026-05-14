"""HYFE_IQC 의 WQB 시뮬레이션 — IQC 의 Playwright 자동화를 멀티유저로 확장.

차이점:
  - 자격증명 / Gemini key 가 db.User 에서 user_id 단위로 들어옴 (env var 폴백 없음).
  - 프로필 디렉터리는 username 해시로 격리.
  - subprocess.Popen 핸들을 호출자에게 반환 → pause 시 즉시 kill.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import logging
from typing import Any, Callable

LOG = logging.getLogger('hyfe.wqb')

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
IQC_PYTHON = os.environ.get('IQC_PY') or '/usr/bin/python3.11'
if not os.path.exists(IQC_PYTHON):
    IQC_PYTHON = sys.executable or 'python3'

PLAYWRIGHT_SIM_POLL_INTERVAL_SEC = 20
PLAYWRIGHT_SIM_MAX_WAIT_SEC = 18 * 60  # 10 → 18 분 (WQB 서버 느린 시간대 / 무거운 알파 대응)
# subprocess 전체 타임아웃 — 한 라운드의 모든 알파를 1탭 순차로 돌릴 시간 예산.
# 알파 1개당 SIM_MAX_WAIT_SEC + setup overhead 의 보수 추정. simulate_batch 에서
# 알파 수에 비례해 예산을 추가한다.
PLAYWRIGHT_BATCH_TIMEOUT_SEC = 30 * 60
# 시뮬 슬롯 수 — 사용자 요청으로 1탭 순차 고정. WQB tier 가 동시 sim 을 사실상 제한해
# 병렬 모드가 거의 매번 sequential 로 fallback 됐고, 로그/타이밍/상태 추적이 복잡해서
# 라운드 한 사이클 동안 단일 탭에서 알파를 차례로 돌리도록 단순화. 필요 시 환경변수로
# override 가능. IQC_FORCE_SEQUENTIAL 은 후방 호환을 위해 남겨둠.
PLAYWRIGHT_PARALLEL_SLOTS = int(os.environ.get('IQC_PARALLEL_SLOTS', '1'))


def user_profile_dir(username: str) -> str:
    h = hashlib.sha1(username.encode('utf-8')).hexdigest()[:10]
    return os.path.expanduser(f'~/.hyfe_iqc_browser_{h}')


def _playwright_available() -> tuple[bool, str]:
    if not os.path.exists(IQC_PYTHON):
        return False, f'IQC Python 인터프리터 없음: {IQC_PYTHON}'
    try:
        r = subprocess.run(
            [IQC_PYTHON, '-c', 'import playwright.sync_api as p; p.sync_playwright'],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            return True, ''
        return False, ((r.stderr or r.stdout or '')[:300] or f'returncode={r.returncode}')
    except Exception as e:
        return False, str(e)[:300]


def _iter_chrome_pids_for_profile(profile_dir: str) -> list[int]:
    """주어진 profile_dir 을 user-data-dir 로 쓰는 chrome/chromium 프로세스 PID 목록.

    /proc/<pid>/cmdline 만 읽기 때문에 외부 프로세스/shell 호출 없음 (linux 전용).
    """
    needle = f'--user-data-dir={profile_dir}'.encode('utf-8')
    pids: list[int] = []
    proc_root = '/proc'
    try:
        names = os.listdir(proc_root)
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        cmd_path = os.path.join(proc_root, name, 'cmdline')
        try:
            with open(cmd_path, 'rb') as f:
                cmdline = f.read()
        except (OSError, IOError):
            continue
        if not cmdline:
            continue
        # cmdline 인자는 NUL 로 구분 — chrome/chromium 어떤 이름이든 user-data-dir 만 일치하면 우리 것.
        if needle in cmdline and (b'chrome' in cmdline or b'chromium' in cmdline):
            try:
                pids.append(int(name))
            except ValueError:
                continue
    return pids


def _cleanup_browser_state(profile_dir: str) -> None:
    """이전 배치가 남긴 chromium 프로세스/lock 정리.

    /proc 파싱으로 PID 를 모은 뒤 SIGKILL — shell=True / awk 의존 없이 동일 효과.
    """
    try:
        for pid in _iter_chrome_pids_for_profile(profile_dir):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        time.sleep(0.5)
    except Exception:
        pass
    if os.path.isdir(profile_dir):
        for fn in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            try:
                p = os.path.join(profile_dir, fn)
                if os.path.exists(p) or os.path.islink(p):
                    os.remove(p)
            except Exception:
                pass


def _build_playwright_script() -> str:
    """IQC 의 검증된 스크립트와 동일. 입력은 stdin JSON, 출력은 stdout RESULT_JSON 줄."""
    return r"""
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

def js_dismiss_overlays(page):
    # 신규 사용자: cookie consent, EU GDPR, 첫 로그인 welcome tour, sidebar onboarding 등
    # 다양한 modal/banner 를 한 번에 dismiss. 최소 2회 호출 권장 (modal 이 chain 으로 뜸).
    page.evaluate(r'''() => {
        // 단어 시작이 아니라 단어 단위로 매칭 (예: "Accept all cookies" 도 잡음).
        const POSITIVE = /\b(Skip|Got it|Exit|Continue|Close|Dismiss|OK|Okay|Accept|Agree|Allow|Done|Next|Later|Confirm|확인|동의|허용|닫기|건너뛰기|나중에|시작|계속|취소)\b/i;
        const COOKIE = /\b(Accept (all|cookies)|I (accept|agree)|Allow all|Reject all|Only necessary|쿠키 허용|모두 허용|필수만)\b/i;
        const candidates = [
            ...document.querySelectorAll('button, a[role="button"], [role="button"]')
        ];
        let clicked = 0;
        for (const el of candidates) {
            try {
                if (el.offsetParent === null) continue;
                const txt = ((el.innerText || el.textContent || '') + ' ' +
                             (el.getAttribute('aria-label') || '')).trim();
                if (!txt) continue;
                if (COOKIE.test(txt) || POSITIVE.test(txt)) {
                    el.click();
                    clicked++;
                }
            } catch(e) {}
        }
        // role=dialog 의 close 버튼도 시도.
        [...document.querySelectorAll('[role="dialog"] button[aria-label*="lose" i], [role="dialog"] [class*="close" i]')]
            .forEach(b => { try { if (b.offsetParent !== null) { b.click(); clicked++; } } catch(e) {} });
        return clicked;
    }''')


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
        log(f'step: set_editor_text done')
        return True
    except Exception as e:
        log(f'step: set_editor_text EXCEPTION: {e}')
        return False

def click_simulate(page):
    # Simulate 버튼이 'editor-simulate-button-text--disabled-example' 등 disabled 클래스로
    # 잠겨있으면 새 sim 못 시작. monaco editor 에 'space + backspace' 입력해서 *editing 상태*
    # 트리거 → React 가 disabled 풀고 enabled 로 전환.
    try:
        is_disabled_example = page.evaluate(r'''() => {
            const btn = document.querySelector('button.editor-simulate-button-text, button[class*="editor-simulate-button"]');
            if (!btn) return false;
            const cls = (btn.className || '').toString();
            return /disabled-example|--disabled\b/.test(cls) || btn.disabled === true;
        }''')
        if is_disabled_example:
            log('step: click_simulate detected disabled-example, nudging editor')
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
                progress_visible: !!document.querySelector('[class*="progress"], [class*="loading"], [class*="spinner"]'),
            };
        }''')
        log(f'step: click_simulate post sim_buttons={post.get("sim_buttons")} running={post.get("running_detected")} progress={post.get("progress_visible")} error={post.get("error_detected")}')
        post_running = bool(post.get('running_detected'))
    except Exception as e:
        log(f'step: click_simulate post diag exception: {e}')
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
        const running = !!document.querySelector('.editor-tabs__tab-dot--running, [class*="--running"]');
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
                log(f'submit_alpha: confirm step FAILED — {confirm}')
                try:
                    page.keyboard.press('Escape')
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                return ('fail:confirm_button_not_found',
                        f'modal 떴으나 confirm 버튼 매칭 실패: {confirm}')

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
        # modal/page-sweep 으로 confirm 한 케이스 → 일반적으로 success (WQB 가 silent submit).
        # 순수 modal-less 케이스 → 1차 click 이 effective 였는지 불확실 → 보수적으로 fail.
        if modal_after_first or confirm.get('match') == 'page_sweep':
            log('submit_alpha: confirm 클릭됨 + 150s 내 명시 신호 없음 — submitted 추정')
            return ('submitted', f'confirm={confirm!r} (no explicit signal in 150s)')
        log('submit_alpha: modal-less + no signal in 150s — fail 보수적 처리')
        return ('fail:no_response_modal_less',
                f'1차 click 후 modal/toast/IS변화 모두 없음 (150s polled). label={info.get("label","")!r}')
    except Exception as e:
        return ('fail:exception', str(e)[:150])


def _rescrape_submit_outcome(page, retries=8, interval_ms=5000):
    # Submit 클릭 후 IS Tests 패널을 재시도 폴링 — WQB 의 self-correlation 검사가
    # 끝나길 기다림. PASS=8 → 'success' / FAIL·ERROR 항목 등장 → 'reject' (self-corr 값 추출).
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
    if state_obj.get('error_text'):
        return True
    # IS Tests 패널 출현 = sim 종료. metrics 변화 detect 못 해도 done 판정.
    if state_obj.get('is_tests_visible'):
        return True
    m = state_obj.get('metrics') or {}
    if not ('sharpe' in m and 'fitness' in m):
        return False
    if not before_metrics:
        return True
    for k in m:
        if before_metrics.get(k) != m.get(k):
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
            else:
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

        def _submit_if_pass(tab_label, fi):
            # PASS 슬롯이면 해당 탭으로 가 Submit Alpha 시도 + 거절/무응답 시 IS 재스크랩.
            if not _is_pass_slot(fi):
                return
            click_tab(page, tab_label)
            page.wait_for_timeout(700)
            sub_status, sub_detail = _try_submit_alpha(page)
            log(f'idx{_ix(fi)} submit status={sub_status} detail={sub_detail}')
            results[fi]['submit_status'] = sub_status
            results[fi]['submitted'] = (sub_status == 'submitted')
            page.wait_for_timeout(600)
            if sub_status.startswith('rejected') or sub_status.startswith('fail:no_response'):
                ro = _rescrape_submit_outcome(page, retries=8, interval_ms=5000)
                if ro.get('ist') is not None and (ro.get('verdict') in ('reject', 'success', 'success_implied')):
                    results[fi]['is_status'] = ro['ist']
                if ro.get('verdict') in ('success', 'success_implied'):
                    results[fi]['submit_status'] = 'submitted'
                    results[fi]['submitted'] = True
                elif ro.get('verdict') == 'reject':
                    results[fi]['submitted'] = False
                    scv = ro.get('self_corr') or ''
                    cur_ss = results[fi].get('submit_status') or ''
                    if ro.get('is_selfcorr') and 'self-corr' not in cur_ss.lower():
                        results[fi]['submit_status'] = (
                            f'rejected:Self-correlation {scv} > 0.7' if scv
                            else 'rejected:Self-correlation above cutoff')
                    elif not cur_ss.startswith('rejected'):
                        _nm = ''
                        for _e in (ro.get('ist') or {}).get('fail', []) or []:
                            _nm = (_e.get('name') or '').strip()
                            if _nm:
                                break
                        results[fi]['submit_status'] = f'rejected:post-submit fail{": "+_nm if _nm else ""}'

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
                        log(f'step: SEQ idx{_ix(fi)} trivial_quit (no result {age}s, panel=False)')
                        results[fi]['summary_metrics'] = {}
                        results[fi]['is_status'] = {'pass': [], 'fail': [], 'error': [], 'pending': [],
                                                    'raw': '(panel never showed; sim likely produced no result)'}
                        done = True
                        break
                if not done and not results[fi].get('error_text'):
                    log(f'step: SEQ idx{_ix(fi)} FAIL sim wait timeout ({SIM_MAX_WAIT_SEC}s)')
                    results[fi]['error_text'] = f'sim wait timeout ({SIM_MAX_WAIT_SEC}s)'
                _submit_if_pass(seq_label, fi)
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
                        _submit_if_pass(tabs[w]['label'], fi)
                        _emit_done(fi)
                        _fill_slot(w)
                        continue
                    # trivial-quit — 6분 무결과 + metrics 안 변함 + panel 없음 → signal 0 알파.
                    if sim_age > 6 * 60 and metrics_unchanged and not panel_seen:
                        log(f'parallel: slot{w} idx{_ix(fi)} trivial_quit (no result {sim_age:.0f}s)')
                        results[fi]['summary_metrics'] = {}
                        results[fi]['is_status'] = {'pass': [], 'fail': [], 'error': [], 'pending': [],
                                                    'raw': '(panel never showed; sim likely produced no result)'}
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
    err = f'playwright_setup: {type(e).__name__}: {e}'
    for r in results:
        if not r.get('error_text'):
            r['error_text'] = err

print('RESULT_JSON:', json.dumps(results, ensure_ascii=False), flush=True)
"""


_PASS_THRESHOLDS = {
    'sharpe': 1.25,
    'fitness': 1.0,
    'returns': 0.05,
    'turnover_max': 0.7,
    'drawdown_min': -0.3,
    'margin': 0.0,
    # WQB IS Tests 8 항목 중 위 6 + 아래 2 = 총 8개. 7개 이상 통과해야 submittable.
    'subuniverse_sharpe': 1.0,
    'self_correlation_max': 0.7,
}


def _parse_metric_number(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    # 단위 처리: %, ‱ (per myriad = 1/10000), bp (basis point) 등.
    unit = 1.0
    if s.endswith('%'):
        unit = 1.0 / 100
        s = s[:-1]
    elif s.endswith('‱'):
        unit = 1.0 / 10000
        s = s[:-1]
    elif s.lower().endswith('bp'):
        unit = 1.0 / 10000
        s = s[:-2]
    s = s.replace(',', '').strip()
    try:
        return float(s) * unit
    except ValueError:
        return None


def _derive_pass_fail(metrics: dict) -> tuple[list[str], list[str]]:
    """WQB IS Tests 8 항목 중 통과/탈락 분리. 7 이상 PASS = submittable.

    1. Sharpe          ≥ 1.25
    2. Fitness         ≥ 1.0
    3. Returns         ≥ 5%
    4. Turnover        ≤ 70%
    5. Drawdown        ≥ -30% (max DD 30% 이하)
    6. Margin          > 0
    7. Sub-universe Sharpe (sub-portfolio 일관성)  ≥ 1.0
    8. Self-correlation (다른 알파와의 유사도)      < 0.7
    """
    passes, fails = [], []
    sharpe = _parse_metric_number(metrics.get('sharpe'))
    fitness = _parse_metric_number(metrics.get('fitness'))
    returns = _parse_metric_number(metrics.get('returns'))
    turnover = _parse_metric_number(metrics.get('turnover'))
    drawdown = _parse_metric_number(metrics.get('drawdown'))
    margin = _parse_metric_number(metrics.get('margin'))
    # 키 별칭 — 'sub_sharpe', 'subuniverse_sharpe', 'sub_universe_sharpe', 'sub-sharpe' 모두 처리.
    sub_sharpe = _parse_metric_number(
        metrics.get('subuniverse_sharpe') or metrics.get('sub_universe_sharpe')
        or metrics.get('sub_sharpe') or metrics.get('subsharpe')
    )
    self_corr = _parse_metric_number(
        metrics.get('self_correlation') or metrics.get('correlation')
        or metrics.get('is_correlation') or metrics.get('selfcorrelation')
    )

    def check(name, ok):
        if ok is None: return
        (passes if ok else fails).append(name)
    check('Sharpe', None if sharpe is None else sharpe >= _PASS_THRESHOLDS['sharpe'])
    check('Fitness', None if fitness is None else fitness >= _PASS_THRESHOLDS['fitness'])
    check('Returns', None if returns is None else returns >= _PASS_THRESHOLDS['returns'])
    check('Turnover', None if turnover is None else turnover <= _PASS_THRESHOLDS['turnover_max'])
    check('Drawdown', None if drawdown is None else drawdown >= _PASS_THRESHOLDS['drawdown_min'])
    check('Margin', None if margin is None else margin > _PASS_THRESHOLDS['margin'])
    check('Sub-Sharpe', None if sub_sharpe is None else sub_sharpe >= _PASS_THRESHOLDS['subuniverse_sharpe'])
    check('Self-Correlation', None if self_corr is None else self_corr < _PASS_THRESHOLDS['self_correlation_max'])
    return passes, fails


def _parse_result_json(stdout: str) -> list[dict] | None:
    if not stdout:
        return None
    starts = []
    pos = 0
    while True:
        i = stdout.find('RESULT_JSON:', pos)
        if i < 0:
            break
        starts.append(i)
        pos = i + 12
    for start in reversed(starts):
        tail = stdout[start + len('RESULT_JSON:'):]
        lb = tail.find('[')
        if lb < 0:
            continue
        depth = 0
        end = -1
        in_str = False
        esc = False
        for j, ch in enumerate(tail[lb:], start=lb):
            if esc:
                esc = False; continue
            if ch == '\\':
                esc = True; continue
            if ch == '"':
                in_str = not in_str; continue
            if in_str:
                continue
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    end = j; break
        if end < 0:
            continue
        try:
            obj = json.loads(tail[lb:end + 1])
            if isinstance(obj, list):
                return obj
        except Exception:
            continue
    return None


def _strategy_fail(s: dict, err: str) -> dict:
    return {
        'idx': s['idx'], 'code': s['code'], 'desc': s.get('desc', ''),
        'pass_count': 0, 'pass_items': [],
        'fail_count': 0, 'fail_items': [],
        'submitted': False, 'submit_status': '',
        'error_text': err[:1500], 'mode': 'error',
    }


def _failed_batch(strategies: list[dict], err: str) -> list[dict]:
    return [_strategy_fail(s, err) for s in strategies]


# ─────────────────────────────────────────────────────────────────────────────
# 메인 진입점 — 워커가 호출. proc_holder 에 Popen 을 저장 → pause 시 kill.
# ─────────────────────────────────────────────────────────────────────────────

def simulate_batch(
    strategies: list[dict], *,
    wqb_username: str, wqb_password: str,
    log_fn: Callable[[str], None] | None = None,
    proc_holder: dict[str, Any] | None = None,
    partial_fn: Callable[[dict], None] | None = None,
) -> list[dict]:
    """한 라운드의 알파들을 Playwright 직접 자동화로 시뮬 (1탭 순차 진행).

    PASS (IS Testing Status PASS≥7 AND FAIL=0) 인 알파는 그 자리에서 'Submit Alpha'
    버튼이 활성화되었는지 확인하고, 활성화되어 있으면 클릭해서 알파를 제출한다.
    완료된 알파별로 partial_fn 콜백이 즉시 호출되어 UI 가 라운드 종료를 기다리지 않는다.

    proc_holder: dict — 호출자가 미리 만들어 전달. 호출 도중 proc_holder['proc'] 에
                       subprocess.Popen 인스턴스가 들어감. 호출자가 pause 시 그걸
                       proc.kill() 하여 즉시 중단 가능.
    """
    if not strategies:
        return []

    pw_ok, reason = _playwright_available()
    if not pw_ok:
        return _failed_batch(strategies, f'playwright_unavailable: {reason}')

    profile_dir = user_profile_dir(wqb_username)
    os.makedirs(profile_dir, exist_ok=True)
    _cleanup_browser_state(profile_dir)

    # idx 도 함께 — 서브프로세스가 partial 알릴 때 정확한 idx 매핑.
    # settings 는 라운드 단위로 통일 — WQB Settings 패널이 전역이라 알파별 다른 settings
    # 가 효과 없고 마지막으로 적용된 settings 만 살아남음 (진행 중 sim invalidate).
    # 따라서 한 호출(라운드)의 모든 알파는 첫 알파의 settings 사용.
    batch_settings: dict = {}
    for s in strategies:
        cand = s.get('settings') or {}
        if cand:
            batch_settings = cand
            break
    payload_in = [{'idx': int(s.get('idx') or (i + 1)),
                   'code': s.get('code', ''),
                   'settings': batch_settings}
                  for i, s in enumerate(strategies)]
    formulas = [p['code'] for p in payload_in]  # 후속 _strategy_fail 등에서 사용

    script = _build_playwright_script()
    env = os.environ.copy()
    env['IQC_PROFILE_DIR'] = profile_dir
    env['WQB_USERNAME'] = wqb_username
    env['WQB_PASSWORD'] = wqb_password
    env['IQC_SIM_MAX_WAIT'] = str(PLAYWRIGHT_SIM_MAX_WAIT_SEC)
    env['IQC_POLL_INTERVAL'] = str(PLAYWRIGHT_SIM_POLL_INTERVAL_SEC)
    env['IQC_PARALLEL_SLOTS'] = str(PLAYWRIGHT_PARALLEL_SLOTS)
    # PASS_THRESHOLD 는 worker.py 의 환경변수에서 읽음. Default 7 (8개 IS Tests 중 7개 통과).
    env['IQC_PASS_THRESHOLD'] = os.environ.get('HYFE_IQC_PASS_THRESHOLD', '7')
    # 한 호출에 라운드 전체 알파를 넘기므로 subprocess 데드라인을 알파 수에 비례하게 늘린다.
    # 첫 wave (= 슬롯 수) 는 기본 타임아웃 안에, 그 뒤로 알파 1개당 ~5분 추가 예산.
    _n_alpha = max(1, len(payload_in))
    _extra_alpha = max(0, _n_alpha - PLAYWRIGHT_PARALLEL_SLOTS)
    batch_timeout_sec = PLAYWRIGHT_BATCH_TIMEOUT_SEC + _extra_alpha * 300
    tmp = os.path.expanduser('~/.hyfe_iqc_tmp')
    os.makedirs(tmp, exist_ok=True)
    env['TMPDIR'] = tmp
    env['XDG_RUNTIME_DIR'] = env.get('XDG_RUNTIME_DIR') or tmp

    started = time.time()
    try:
        proc = subprocess.Popen(
            [IQC_PYTHON, '-c', script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
    except Exception as e:
        return _failed_batch(strategies, f'subprocess_spawn: {e}')

    if proc_holder is not None:
        proc_holder['proc'] = proc

    # stdout/stderr 를 별도 thread 로 line 단위 streaming — [pw] 로그가 worker 로 즉시 흐름.
    stdout_buf: list[str] = []
    stderr_buf: list[str] = []

    # 라이브 디버그용 — 특정 키워드 포함된 [pw] 라인은 즉시 LOG 에 forward.
    _LIVE_KEYWORDS = ('click_new_tab', 'tab-add finished', 'matched tag=',
                      'sequential mode', 'parallel mode',
                      'submit_alpha', 'submit status',
                      'is_tests scrape', 'is_tests body', 'is_tests anomaly',
                      'show_test_results: clicked', 'show_test_results: panel',
                      'show_test_results: tutorial exit',
                      'alt_panel_trigger', 'early_quit', 'trivial_quit',
                      'metrics_under',
                      'menu_candidates', 'trigger_outer',
                      'apply_settings', 'step:')

    def _reader(stream, buf, is_err: bool):
        # 라인 buffer 로만 모음 — UI 로그가 [pw]/[pw err] 트레이스로 도배되는 걸 막기 위해
        # log_fn 으로 forward 하지 않음. 단,
        #   - '[partial] ' 접두 라인은 슬롯 완료 알림 → partial_fn 즉시 dispatch
        #   - 디버그 키워드 매칭 라인은 server.log 에 즉시 LOG.info (배치 종료 안 기다림)
        try:
            for line in iter(stream.readline, ''):
                if not line:
                    break
                buf.append(line)
                if not is_err:
                    if partial_fn is not None and line.startswith('[partial] '):
                        try:
                            obj = json.loads(line[len('[partial] '):].strip())
                            partial_fn(obj)
                        except Exception:
                            pass
                    elif line.startswith('[pw] ') and any(kw in line for kw in _LIVE_KEYWORDS):
                        LOG.info('  pw_live: %s', line.rstrip()[:400])
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_buf, False), daemon=True)
    t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_buf, True), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.stdin.write(json.dumps(payload_in))
        proc.stdin.close()
    except Exception:
        try:
            proc.stdin.close()
        except Exception:
            pass

    try:
        proc.wait(timeout=batch_timeout_sec)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        t_out.join(timeout=2)
        t_err.join(timeout=2)
        if proc_holder is not None:
            proc_holder['proc'] = None
        return _failed_batch(strategies, f'playwright_setup timeout after {batch_timeout_sec}s')

    t_out.join(timeout=3)
    t_err.join(timeout=3)
    stdout = ''.join(stdout_buf)
    stderr = ''.join(stderr_buf)
    if proc_holder is not None:
        proc_holder['proc'] = None

    elapsed = int(time.time() - started)

    if proc.returncode is None:
        # pause 등으로 외부에서 kill 된 경우.
        return _failed_batch(strategies, 'paused: subprocess killed mid-batch')
    if proc.returncode < 0:
        return _failed_batch(strategies, f'paused: subprocess died with signal {-proc.returncode}')

    LOG.info('playwright round done in %ds (return=%d)', elapsed, proc.returncode)
    # NOTE: 디버그 라인은 _reader 에서 _LIVE_KEYWORDS 매칭 시 `pw_live:` 로 이미 흘렸음.
    # 종료 후 stdout 전체를 한번 더 dump 하면 모든 step 이 두 번 찍히므로 제거.

    parsed = _parse_result_json(stdout or '')
    if parsed is None:
        tail = (stderr or '')[-1500:] or (stdout or '')[-1500:]
        return _failed_batch(strategies, f'playwright_setup: RESULT_JSON 파싱 실패. {tail}')

    out = []
    for pos, s in enumerate(strategies):
        slot_no = pos + 1
        match = next(
            (p for p in parsed if int(p.get('slot') or p.get('sim') or 0) == slot_no), None,
        )
        if not match and pos < len(parsed):
            match = parsed[pos]
        if not match:
            out.append(_strategy_fail(s, 'playwright: no result for slot'))
            continue
        pass_items = list(match.get('pass_items') or [])
        fail_items = list(match.get('fail_items') or [])
        metrics = dict(match.get('summary_metrics') or {})
        if (not pass_items and not fail_items) and metrics:
            pass_items, fail_items = _derive_pass_fail(metrics)
        # IS Testing Status 패널 결과가 있으면 PASS/FAIL 항목을 그쪽 권위 데이터로 교체.
        is_status = match.get('is_status') or {}
        if is_status.get('pass') or is_status.get('fail'):
            pass_items = [(p.get('name') or p.get('desc') or '').strip()
                          for p in (is_status.get('pass') or [])]
            fail_items = [(f.get('name') or f.get('desc') or '').strip()
                          for f in (is_status.get('fail') or [])]
            pass_count = len(pass_items)
            fail_count = len(fail_items)
        else:
            pass_count = int(match.get('pass_count') or len(pass_items))
            fail_count = int(match.get('fail_count') or len(fail_items))
        out.append({
            'idx': s['idx'],
            'code': s['code'],
            'desc': s.get('desc', ''),
            'pass_count': pass_count,
            'pass_items': pass_items,
            'fail_count': fail_count,
            'fail_items': fail_items,
            'submitted': bool(match.get('submitted')),
            'submit_status': str(match.get('submit_status') or ''),
            'error_text': str(match.get('error_text') or ''),
            'mode': 'playwright',
            'metrics': metrics,
            'is_status': is_status,
        })
    return out
