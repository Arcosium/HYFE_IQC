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
    """임베디드 PW 워커(server/_wqb_pw_worker.py)를 그대로 반환.

    예전엔 ~166KB raw 리터럴을 이 함수가 직접 들고 있어 lint/AST/diff 가
    불가능했음. 이제 실파일이라 정적분석 가능 — 내용은 byte-identical 이라
    서브프로세스 동작은 완전히 동일하다. (파일은 import 하지 말 것: stdin 을
    읽는 독립 실행 스크립트다.)"""
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      '_wqb_pw_worker.py')
    with open(_p, encoding='utf-8') as _f:
        return _f.read()


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
    # 디버그 플래그 (subprocess 안에서 우상단 스크린샷 dump).
    if os.environ.get('IQC_DEBUG_TOPRIGHT'):
        env['IQC_DEBUG_TOPRIGHT'] = os.environ['IQC_DEBUG_TOPRIGHT']
    # 한 호출에 라운드 전체 알파를 넘기므로 subprocess 데드라인을 알파 수에 비례하게 늘린다.
    # 첫 wave (= 슬롯 수) 는 기본 타임아웃 안에, 그 뒤로 알파 1개당 ~5분 추가 예산.
    _n_alpha = max(1, len(payload_in))
    _extra_alpha = max(0, _n_alpha - PLAYWRIGHT_PARALLEL_SLOTS)
    batch_timeout_sec = PLAYWRIGHT_BATCH_TIMEOUT_SEC + _extra_alpha * 300
    tmp = os.path.expanduser('~/.hyfe_iqc_tmp')
    os.makedirs(tmp, exist_ok=True)
    env['TMPDIR'] = tmp
    env['XDG_RUNTIME_DIR'] = env.get('XDG_RUNTIME_DIR') or tmp

    # 스크립트가 Linux MAX_ARG_STRLEN (4096*32 = 131072 bytes) 를 넘기 시작해서
    # `python -c <script>` 호출이 E2BIG 으로 실패함. 임시 파일에 쓰고 path 로 호출.
    # mode 0o600 으로 권한 좁힘 (다른 user 가 read 못 하게 — credentials 는 env 로만 들어가지만
    # 스크립트 자체에 prompt cache 같은 민감 정보가 없어도 보수적으로 락).
    import tempfile as _tempfile
    try:
        with _tempfile.NamedTemporaryFile(
                mode='w', dir=tmp, prefix='wqb_pw_', suffix='.py',
                delete=False, encoding='utf-8') as _sf:
            _sf.write(script)
            script_path = _sf.name
        try:
            os.chmod(script_path, 0o600)
        except Exception:
            pass
    except Exception as e:
        return _failed_batch(strategies, f'subprocess_spawn: write script tempfile: {e}')

    started = time.time()
    try:
        proc = subprocess.Popen(
            [IQC_PYTHON, script_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
    except Exception as e:
        try: os.unlink(script_path)
        except Exception: pass
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
                      'apply_settings', 'step:',
                      'toggle_tutorial_checkbox', 'init_script:',
                      'js_dismiss_introjs', 'js_dismiss_overlays',
                      '_ensure_results_mode',
                      'fail_dump:', '_ensure_simulator_view')

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
        try: os.unlink(script_path)
        except Exception: pass
        return _failed_batch(strategies, f'playwright_setup timeout after {batch_timeout_sec}s')

    t_out.join(timeout=3)
    t_err.join(timeout=3)
    stdout = ''.join(stdout_buf)
    stderr = ''.join(stderr_buf)
    if proc_holder is not None:
        proc_holder['proc'] = None
    try: os.unlink(script_path)
    except Exception: pass

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
