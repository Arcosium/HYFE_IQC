/* HYFE_IQC 프론트엔드 — Evolution Observatory SPA.
   fetch 기반 API + EventSource SSE + SVG 진화 시각화(외부 라이브러리 없음).

   서버 계약은 기존 그대로 사용한다:
   - /api/status (5s poll + SSE status 이벤트) — ga{seed_pool,focus_queue,bandit} 포함
   - /api/logs + /api/logs/stream — backlog replay 후 SSE 잇기, clear 지점 존중
   - /api/m_submits — 제출 시도 표 (모바일과 동일 소스·비우기)
   - /api/recent_alphas?limit=N — 진화 궤적/세대 데이터 (desc 의 [rand|mut gN|xo gN] 태그 파싱)
   - /api/m_status — submitted/unsubmitted 카운트 타일
*/

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  const screens = {
    login: $('#screen-login'),
    dashboard: $('#screen-dashboard'),
  };
  const statusBox = $('#login-status');
  const logPane = $('#log-pane');
  const userInfo = $('#user-info');
  const userName = $('#user-name');
  const btnStart = $('#btn-start');
  const btnPause = $('#btn-pause');
  const btnLogout = $('#btn-logout');
  const btnResearch = $('#btn-research');
  if (btnResearch) btnResearch.addEventListener('click', () => startResearch());
  const rsQuery = $('#rs-query');
  if (rsQuery) rsQuery.addEventListener('keydown', (e) => {
    // IME 조합 중 Enter 는 한글 확정이지 제출이 아니다 (isComposing 가드).
    if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); startResearch(); }
  });

  // 카드 접기/펼치기 — .card-block.collapsible[data-fold-key] 전부에 적용 (2026-07-23 일반화).
  // 상태는 localStorage 로 유지(모바일과 키 공유). data-fold-default="closed" 면 기본 접힘.
  // ⚠ 2026-07-27 — 진화 분석 탭은 전부 펼친 채로 시작하도록 바꿨다(사장 지시). 그런데
  //   기본값이 '접힘' 이던 카드는 첫 렌더에서 localStorage 에 '1' 을 **써 버렸기 때문에**,
  //   HTML 기본값만 바꿔서는 기존 사용자 화면이 그대로 접혀 있다. 저장된 값을 한 번만
  //   지워 준다(이후엔 사용자가 직접 접은 상태가 정상적으로 유지된다).
  (function migrateEvoFoldDefaults() {
    try {
      if (localStorage.getItem('fold-evo-expanded-v1')) return;
      for (const k of ['pipeline', 'population', 'scatter', 'directive',
                       'genladder', 'hp', 'bandit']) {
        localStorage.removeItem('fold-' + k);
      }
      localStorage.setItem('fold-evo-expanded-v1', '1');
    } catch (e) {}
  })();

  (function initCollapsibles() {
    document.querySelectorAll('.card-block.collapsible[data-fold-key]').forEach((card) => {
      const head = card.querySelector('.card-head');
      if (!head) return;
      const key = 'fold-' + card.dataset.foldKey;
      const setCollapsed = (c) => {
        card.classList.toggle('collapsed', c);
        head.setAttribute('aria-expanded', String(!c));
        try { localStorage.setItem(key, c ? '1' : '0'); } catch (e) {}
        // 접혀 있는 동안 그린 SVG 차트는 폭이 0 이다 — 펼칠 때 다시 그리게 알린다.
        if (!c) { try { window.dispatchEvent(new Event('resize')); } catch (e) {} }
      };
      let start = card.dataset.foldDefault === 'closed';
      try {
        const saved = localStorage.getItem(key)
          // 구버전 키(rs-collapsed) 이관 — 리서치 카드의 기존 사용자 상태를 보존한다.
          || (card.dataset.foldKey === 'rs' ? localStorage.getItem('rs-collapsed') : null);
        if (saved === '1') start = true;
        else if (saved === '0') start = false;
      } catch (e) {}
      card.classList.add('fold-ready');
      setCollapsed(start);
      const toggle = () => setCollapsed(!card.classList.contains('collapsed'));
      head.addEventListener('click', (e) => {
        // 헤더 안의 컨트롤(토글·셀렉트 등) 클릭은 접기가 아니다.
        if (e.target.closest('button, select, input, label, a')) return;
        toggle();
      });
      head.addEventListener('keydown', (e) => {
        if (e.target !== head) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
      });
    });
  })();
  const btnClearLog = $('#btn-clear-log');
  const autoscrollEl = $('#autoscroll');
  const stateText = $('#state-text');
  const stateRound = $('#state-round');
  const stateCompleted = $('#state-completed');
  const stateErrors = $('#state-errors');

  let evtSource = null;
  let lastLogId = 0;
  const renderedLogIds = new Set();
  let statusTimer = null;
  let bestTimer = null;
  let evoTimer = null;
  let learnTimer = null;
  let researchTimer = null;

  // ── 화면 전환 ────────────────────────────────────────────
  function showScreen(name) {
    Object.entries(screens).forEach(([k, el]) => {
      if (k === name) el.removeAttribute('hidden');
      else el.setAttribute('hidden', '');
    });
    const topLive = $('#top-live');
    if (topLive) {
      if (name === 'dashboard') topLive.removeAttribute('hidden');
      else topLive.setAttribute('hidden', '');
    }
  }

  function setStatus(kind, message) {
    statusBox.className = 'status ' + (kind || 'empty');
    statusBox.textContent = message || '';
  }

  // ── API 헬퍼 ─────────────────────────────────────────────
  async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const r = await fetch(path, { credentials: 'same-origin', ...opts, headers });
    let data = null;
    try { data = await r.json(); } catch (_) { data = { ok: false, reason: 'parse_error', detail: 'JSON parse failed' }; }
    return { ok: r.ok, status: r.status, data };
  }

  // ── 로그인 ───────────────────────────────────────────────
  async function tryAutoLogin() {
    const r = await api('/api/me');
    if (r.ok && r.data && r.data.ok) {
      onLoggedIn(r.data);
      return true;
    }
    return false;
  }

  // ── 탭 토글 ──────────────────────────────────────────────
  const tabLoginBtn = $('#tab-login-btn');
  const tabRegisterBtn = $('#tab-register-btn');
  const formLogin = $('#form-login');
  const formRegister = $('#form-register');

  function showLoginTab() {
    formLogin.removeAttribute('hidden');
    formRegister.setAttribute('hidden', '');
    tabLoginBtn.classList.add('active');
    tabRegisterBtn.classList.remove('active');
    setStatus('empty', '');
  }

  function showRegisterTab() {
    formRegister.removeAttribute('hidden');
    formLogin.setAttribute('hidden', '');
    tabRegisterBtn.classList.add('active');
    tabLoginBtn.classList.remove('active');
    setStatus('empty', '');
  }

  if (tabLoginBtn) tabLoginBtn.addEventListener('click', showLoginTab);
  if (tabRegisterBtn) tabRegisterBtn.addEventListener('click', showRegisterTab);

  // 검증 성공 시 보관할 사용자 정보 — 버튼 클릭 시 onLoggedIn 에 전달.
  let _pendingMe = null;
  const btnGoDashboard = $('#btn-go-dashboard');

  $('#form-login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const wqb_username = $('#wqb_username').value.trim();
    const wqb_password = $('#wqb_password').value;
    const remember = !!($('#remember_device') && $('#remember_device').checked);
    const btn = $('#btn-login');
    btn.disabled = true;
    btn.textContent = '로그인 중...';
    setStatus('info', '자격증명 확인 중...');
    btnGoDashboard.setAttribute('hidden', '');
    try {
      const r = await api('/api/login', {
        method: 'POST',
        body: JSON.stringify({ wqb_username, wqb_password, remember }),
      });
      if (r.ok && r.data && r.data.ok) {
        // 자동 전환 대신 명시적 버튼을 띄움 — 사용자가 직접 클릭해서 이동.
        setStatus('empty', '');
        _pendingMe = r.data;
        btnGoDashboard.removeAttribute('hidden');
        btnGoDashboard.focus();
      } else {
        const reason = (r.data && r.data.reason) || 'unknown';
        const detail = (r.data && r.data.detail) || '알 수 없는 오류';
        if (r.status === 404 && reason === 'not_registered') {
          setStatus('info', '가입되지 않은 계정입니다 — 회원가입 탭으로 이동하세요.');
          showRegisterTab();
        } else {
          setStatus('error', `[${reason}] ${reasonLabel(reason)} — ${detail}`);
        }
      }
    } catch (err) {
      setStatus('error', '네트워크 오류: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '로그인';
    }
  });

  // ── 회원가입 제출 ─────────────────────────────────────────
  $('#form-register').addEventListener('submit', async (e) => {
    e.preventDefault();
    const wqb_username = $('#reg_wqb_username').value.trim();
    const wqb_password = $('#reg_wqb_password').value;
    const remember = !!($('#reg_remember_device') && $('#reg_remember_device').checked);
    const btn = $('#btn-register');
    btn.disabled = true;
    btn.textContent = '가입 중... (최대 90초)';
    setStatus('info', 'WQB 자격증명 검증 중. 신규 가입은 30~60초 걸릴 수 있습니다.');
    btnGoDashboard.setAttribute('hidden', '');
    try {
      const r = await api('/api/register', {
        method: 'POST',
        // account_type 은 보내지 않는다 — 서버가 WQB permissions 로 측정한다(2026-07-27).
        body: JSON.stringify({ wqb_username, wqb_password, remember }),
      });
      if (r.ok && r.data && r.data.ok) {
        setStatus('empty', '');
        _pendingMe = r.data;
        btnGoDashboard.removeAttribute('hidden');
        btnGoDashboard.focus();
      } else {
        const reason = (r.data && r.data.reason) || 'unknown';
        const detail = (r.data && r.data.detail) || '알 수 없는 오류';
        if (r.status === 409 && reason === 'already_registered') {
          setStatus('info', '이미 가입됨 — 로그인하세요.');
          showLoginTab();
        } else {
          setStatus('error', `[${reason}] ${reasonLabel(reason)} — ${detail}`);
        }
      }
    } catch (err) {
      setStatus('error', '네트워크 오류: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '회원가입';
    }
  });

  btnGoDashboard.addEventListener('click', () => {
    btnGoDashboard.setAttribute('hidden', '');
    const meData = _pendingMe || {};
    _pendingMe = null;
    try {
      onLoggedIn(meData);
    } catch (err) {
      console.error('onLoggedIn 예외', err);
      showScreen('dashboard');
      setStatus('error', '대시보드 초기화 중 오류 — 새로고침해 주세요. (' + err.message + ')');
    }
  });

  function reasonLabel(reason) {
    return ({
      wqb_credentials: 'WQB 아이디 또는 비밀번호가 잘못되었습니다',
      wqb_unreachable: 'WQB 사이트 접속 실패 (서버 또는 네트워크 문제)',
      wqb_captcha: 'WQB 사이트가 봇 챌린지를 표시했습니다 (잠시 후 재시도)',
      wqb_auth_required: 'WQB 가 새 디바이스 인증을 요구합니다 — platform.worldquantbrain.com 에서 한 번 수동 로그인 후 인증을 마친 뒤 재시도하세요',
      playwright_setup: '브라우저 자동화 시작 실패 (서버 환경 문제)',
      missing_fields: '입력값 누락',
      unauthorized: '인증 실패',
    })[reason] || reason;
  }

  async function onLoggedIn(meData) {
    // 화면 전환은 가장 먼저 — 이후 단계가 실패해도 대시보드는 보이도록.
    showScreen('dashboard');
    userInfo.removeAttribute('hidden');
    userName.textContent = (meData && meData.wqb_username) || `user #${(meData && meData.user_id) || '?'}`;

    // ⚠ 순수 DOM 배선은 **네트워크 호출보다 먼저** 붙인다 (2026-07-27). 화면은 위에서
    //   이미 보이는데 탭 리스너가 아래쪽 await 들(status·log tail·큐 조회) 뒤에 붙던
    //   탓에, 들어오자마자 누른 **첫 탭 클릭이 통째로 씹혔다**(실측: 첫 클릭만 무반응).
    try { initMainTabs(); } catch (e) { console.error('initMainTabs', e); }
    try { initSubmitMode(); } catch (e) { console.error('initSubmitMode', e); }
    try { initHelp(); } catch (e) { console.error('initHelp', e); }
    try { initSubmitQueueTools(); } catch (e) { console.error('initSubmitQueueTools', e); }

    // SSE 시작 전 status 한 번 받아 화면을 그린 뒤, 로그는 **마지막 1500줄만**
    // tail 로 한 번에 받아 그리고 SSE 를 잇는다. (구 방식은 비우기 지점부터 전체
    // backlog 재생 — 로그 10만 행 시점에 GET 60번 + 3만 줄 렌더로 로딩이 수십 초
    // 걸렸고, DOM 캡 5000줄이라 대부분은 그리자마자 버려졌다. 2026-07-26.)
    try {
      const r0 = await api('/api/status');
      if (r0.ok && r0.data && r0.data.ok) {
        lastLogId = Number(r0.data.last_cleared_log_id || 0);
        applyStatus(r0.data);
        try { await replayTail(1500); }
        catch (e) { console.error('log tail replay', e); }
      }
    } catch (e) { console.error('initial status fetch', e); }

    // 폴링/스트림은 각각 독립적으로 try — 하나가 실패해도 나머지는 살린다.
    try { startStreaming(); } catch (e) { console.error('startStreaming init', e); }
    try { refreshBest(); } catch (e) { console.error('refreshBest init', e); }
    try { refreshEvolution(); } catch (e) { console.error('refreshEvolution init', e); }
    try { refreshLearning(); } catch (e) { console.error('refreshLearning init', e); }
    try { refreshResearch(); } catch (e) { console.error('refreshResearch init', e); }
    try { initConstraint(); } catch (e) { console.error('initConstraint', e); }
    try { refreshSubmitQueue(); } catch (e) { console.error('refreshSubmitQueue init', e); }
    setInterval(refreshSubmitQueue, 30000);
    try { refreshSubmitHistory(); } catch (e) { console.error('refreshSubmitHistory init', e); }
    setInterval(refreshSubmitHistory, 30000);
    try { checkPersonaStatus(); } catch (e) { console.error('checkPersonaStatus init', e); }
    if (statusTimer) clearInterval(statusTimer);
    if (bestTimer) clearInterval(bestTimer);
    if (evoTimer) clearInterval(evoTimer);
    if (learnTimer) clearInterval(learnTimer);
    statusTimer = setInterval(refreshStatus, 5000);
    bestTimer = setInterval(refreshBest, 30000);
    evoTimer = setInterval(refreshEvolution, 30000);
    learnTimer = setInterval(refreshLearning, 30000);
    researchTimer = setInterval(refreshResearch, 5000);
  }

  // ── 로그아웃 ─────────────────────────────────────────────
  btnLogout.addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST' });
    stopStreaming();
    if (statusTimer) clearInterval(statusTimer);
    if (bestTimer) clearInterval(bestTimer);
    if (evoTimer) clearInterval(evoTimer);
    if (learnTimer) clearInterval(learnTimer);
    if (researchTimer) clearInterval(researchTimer);
    userInfo.setAttribute('hidden', '');
    showScreen('login');
    setStatus('empty', '');
  });

  // ── 워커 제어 ────────────────────────────────────────────
  btnStart.addEventListener('click', async () => {
    btnStart.disabled = true;
    const r = await api('/api/start', { method: 'POST' });
    btnStart.disabled = false;
    if (r.data && r.data.ok) {
      refreshStatus();
    } else {
      alert('시작 실패: ' + JSON.stringify(r.data));
    }
  });

  btnPause.addEventListener('click', async () => {
    btnPause.disabled = true;
    const r = await api('/api/pause', { method: 'POST' });
    btnPause.disabled = false;
    if (r.data && r.data.ok) {
      refreshStatus();
    } else {
      alert('일시정지 실패: ' + JSON.stringify(r.data));
    }
  });

  // delay 는 아래 '탐색 조건' 이 정한다 (`delay=0|1`). 별도의 delay 토글은
  // 같은 값을 두 곳에서 정하는 구조라 2026-07-22 제거했다.

  // ── 탐색 조건 (Power Pool 주간 테마 등) ───────────────────
  // 기한이 있는 조건이 대부분이라 **끄기가 걸기만큼 중요하다**. 주가 바뀌면 즉시
  // 풀 수 있어야 낡은 조건으로 라운드를 낭비하지 않는다.
  const cText = $('#constraint-text');
  const cStatus = $('#constraint-status');
  const cSave = $('#btn-constraint-save');
  const cClear = $('#btn-constraint-clear');
  function renderConstraint(d) {
    if (!cStatus) return;
    if (!d || !d.active) {
      cStatus.textContent = '걸린 조건 없음 (무제약 탐색)';
      return;
    }
    let msg = '적용중: ' + d.summary + ' (다음 라운드부터)';
    if (d.unparsed && d.unparsed.length) msg += ' · 미해석: ' + d.unparsed.join('; ');
    cStatus.textContent = msg;
  }
  async function initConstraint() {
    if (!cText) return;
    const r = await api('/api/constraint');
    if (r.ok && r.data && r.data.ok) {
      cText.value = r.data.text || '';
      renderConstraint(r.data);
    }
    if (cSave) cSave.addEventListener('click', async () => {
      cStatus.textContent = '저장 중…';
      const res = await api('/api/constraint', {
        method: 'POST', body: JSON.stringify({ text: cText.value }),
      });
      if (res.ok && res.data && res.data.ok) renderConstraint(res.data);
      else cStatus.textContent = '저장 실패: ' + ((res.data && res.data.message) || '조건을 해석하지 못했습니다');
    });
    if (cClear) cClear.addEventListener('click', async () => {
      cStatus.textContent = '해제 중…';
      const res = await api('/api/constraint', { method: 'DELETE' });
      if (res.ok && res.data && res.data.ok) {
        cText.value = '';
        renderConstraint(res.data);
      } else {
        cStatus.textContent = '해제 실패';
      }
    });
  }

  // ── 상태 폴링 ────────────────────────────────────────────
  async function refreshStatus() {
    const r = await api('/api/status');
    if (!(r.ok && r.data && r.data.ok)) {
      if (r.status === 401) {
        // 세션 만료 — 로그인 화면으로.
        userInfo.setAttribute('hidden', '');
        showScreen('login');
        if (statusTimer) clearInterval(statusTimer);
        if (evoTimer) clearInterval(evoTimer);
        stopStreaming();
      }
      return;
    }
    applyStatus(r.data);
  }

  function applyStatus(s) {
    const running = !!s.running;
    const paused = !!s.paused;
    const alive = !!s.thread_alive;
    let label;
    if (running && alive && !paused) label = '실행 중';
    else if (running && paused) label = '일시정지 (요청됨)';
    else if (alive) label = '워커 살아있음';
    else label = 'idle';
    stateText.textContent = label + ` (${s.current_status || 'idle'})`;
    stateRound.textContent = s.current_round != null ? `#${s.current_round}` : '—';
    stateCompleted.textContent = String(s.last_round_num || 0);
    stateErrors.textContent = String(s.errors_count || 0);

    btnStart.disabled = running && alive && !paused;
    btnPause.disabled = !alive || paused;

    // 상단 run pill + 라운드
    const pill = $('#run-pill');
    const pillText = $('#run-pill-text');
    if (pill && pillText) {
      pill.classList.remove('is-running', 'is-paused');
      if (running && alive && !paused) { pill.classList.add('is-running'); pillText.textContent = 'EVOLVING'; }
      else if (running && paused) { pill.classList.add('is-paused'); pillText.textContent = 'PAUSED'; }
      else if (alive) { pillText.textContent = 'ALIVE'; }
      else { pillText.textContent = 'IDLE'; }
    }
    const topRound = $('#top-round-num');
    if (topRound) topRound.textContent = s.current_round != null ? String(s.current_round) : '—';

    // 파이프라인 애니메이션 on/off
    const pl = $('#pipeline');
    if (pl) pl.classList.toggle('is-running', running && alive && !paused);

    // 제출 방식 — 상태 폴링으로 따라간다(다른 기기/탭에서 바꿔도 화면이 어긋나지 않게).
    if (s.submit_mode) { try { paintSubmitMode(s.submit_mode); } catch (e) {} }
    // 현재 계정 유형 표시 — RC 면 전환 안내/버튼 숨김.
    const _isRc = s.account_type === 'research_consultant';
    if (!_helpAudPinned && s.account_type) {
      try {
        paintHelpAudience(_isRc ? 'rc' : 'std');
        const _note = $('#help-auto-note');
        if (_note) _note.hidden = false;
      } catch (e) {}
    }
    const _cur = document.getElementById('account-type-current');
    if (_cur) _cur.textContent = _isRc ? 'Research Consultant' : '일반(Standard)';
    const _prompt = document.getElementById('account-upgrade-prompt');
    if (_prompt) _prompt.hidden = _isRc;
    const _upBtn = document.getElementById('btn-upgrade-rc');
    if (_upBtn) _upBtn.hidden = _isRc;
    // RC 계정만 biometric 인증 대상 — 재인증 진입점을 상시 노출한다.
    const _pOpenBtn = document.getElementById('btn-persona-open');
    if (_pOpenBtn) _pOpenBtn.hidden = !_isRc;

    const _modeSummary = document.getElementById('mode-summary');
    if (_modeSummary) {
      _modeSummary.textContent =
        (s.genome_model || (_isRc ? 'rc-api-genome' : 'standard-genome'))
        + ' · ' + (s.backtester_mode || 'WQB API');
    }

    // 타일 + 파이프라인 라이브 카운트
    const seedN = Number((s.ga && s.ga.seed_pool) || 0);
    const focusN = Number((s.ga && s.ga.focus_queue) || 0);
    const banditOn = !!(s.ga && s.ga.bandit);
    setText('#tile-rounds', String(s.last_round_num || 0));
    setText('#tile-focus', String(focusN));
    setText('#pl-seed-count', String(seedN));
    setText('#pl-focus-count', String(focusN));
    setText('#pl-rand-sub', '신규 유전체 생성 · 밴딧 ' + (banditOn ? 'ON' : 'OFF'));
    setText('#pl-sim-sub', s.backtester_mode || 'WQB API concurrent');
    setText('#pl-gate-sub', _isRc ? '완료 알파마다 제출 시도' : '7 basic PASS + self-corr ≤ 0.7 저장');
    setText('#pl-sim-count', s.current_round != null ? ('R' + s.current_round) : '—');
    setText('#pl-submit-sub', _isRc ? '직렬화 + 429 재시도' : 'Submit 리스트 추가');
    renderPreflightStat(s);   // #7 프리플라이트/리페어 계측 (#2/#4 가 status.ga 에 실어줌)
  }

  function setText(sel, txt) {
    const el = $(sel);
    if (el) el.textContent = txt;
  }

  // ── 로그 tail ────────────────────────────────────────────
  // 초기 로딩: 비우기 지점 이후의 마지막 n 줄만 1 GET 으로 받아 그린다.
  async function replayTail(n) {
    const r = await api('/api/logs?tail=' + encodeURIComponent(n));
    if (!(r.ok && r.data && r.data.ok)) return;
    for (const row of (r.data.logs || [])) {
      if (appendLog(row) && row.id > lastLogId) lastLogId = row.id;
    }
  }

  // ── 로그 스트림 ──────────────────────────────────────────
  let _evoRefreshDebounce = null;
  function startStreaming() {
    stopStreaming();
    try {
      evtSource = new EventSource('/api/logs/stream?since=' + encodeURIComponent(lastLogId));
    } catch (e) {
      console.error('SSE 연결 실패', e);
      return;
    }
    evtSource.addEventListener('log', (ev) => {
      try {
        const row = JSON.parse(ev.data);
        if (appendLog(row) && row.id > lastLogId) lastLogId = row.id;
        // 라운드가 끝나면 진화 차트를 곧 갱신 (2s 디바운스 — 연속 이벤트 합침).
        if (row.level === 'round_end') {
          if (_evoRefreshDebounce) clearTimeout(_evoRefreshDebounce);
          _evoRefreshDebounce = setTimeout(() => { refreshEvolution(); }, 2000);
        }
      } catch (e) { /* skip */ }
    });
    evtSource.addEventListener('status', (ev) => {
      try { applyStatus(JSON.parse(ev.data)); } catch (e) {}
    });
    evtSource.addEventListener('error', () => {
      // 서버가 닫으면 자동 재연결까진 시도 — 5초 대기 후 수동 재연결.
      stopStreaming();
      setTimeout(startStreaming, 5000);
    });
  }

  function stopStreaming() {
    if (evtSource) {
      try { evtSource.close(); } catch (_) {}
      evtSource = null;
    }
  }

  // ── 피드 렌더러 ──────────────────────────────────────────
  // 일반 줄: [시각 | 메시지] 2컬럼(행잉 인덴트). XSS 안전 — textContent 만 사용.
  // round_start/round_end 줄: ═══ 장식을 벗겨 칩+요약 구분선으로 재구성.
  function fmtLogTime(ts) {
    // 고정폭 HH:MM:SS — ko-KR 의 "15시 39분 8초" 는 열이 깨진다.
    return ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '';
  }

  function buildLogLine(row) {
    const line = document.createElement('div');
    line.className = 'log-line ' + classifyLine(row.line || '', row.level || '');
    const ts = document.createElement('span');
    ts.className = 'ts';
    ts.textContent = fmtLogTime(row.ts);
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = row.line || '';
    line.appendChild(ts);
    line.appendChild(msg);
    return line;
  }

  function buildLogDivider(row) {
    const root = document.createElement('div');
    root.className = 'log-divider round-color-' + (Math.abs(Number(row.round_num) || 0) % 6);
    const raw = String(row.line || '');
    const t = raw.replace(/═+/g, '').trim();
    // "ROUND 113 done — 시도 8 / PASS≥7 0 / 오류 3 / 캐시히트 0" | "ROUND 114 시작"
    const m = /^ROUND\s+(\S+)\s*([^—]*?)\s*(?:—\s*(.*))?$/.exec(t);
    const chipText = m ? ('ROUND ' + m[1] + (m[2] ? ' ' + m[2].trim() : '')) : (t || '—');
    const sumText = m && m[3] ? m[3].replace(/\s*\/\s*/g, ' · ') : '';
    const l1 = document.createElement('span');
    l1.className = 'ld-line';
    const chip = document.createElement('span');
    chip.className = 'ld-chip';
    chip.textContent = chipText;
    root.appendChild(l1);
    root.appendChild(chip);
    if (sumText) {
      const sum = document.createElement('span');
      sum.className = 'ld-sum';
      sum.textContent = sumText;
      root.appendChild(sum);
    }
    const l2 = document.createElement('span');
    l2.className = 'ld-line';
    root.appendChild(l2);
    root.title = fmtLogTime(row.ts) + '  ' + raw;
    return root;
  }

  function appendLog(row) {
    const id = Number(row && row.id);
    if (Number.isFinite(id) && id > 0) {
      if (renderedLogIds.has(id)) return false;
      renderedLogIds.add(id);
    }
    const isDivider = row.level === 'round_end' || row.level === 'round_start';
    const el = isDivider ? buildLogDivider(row) : buildLogLine(row);
    if (Number.isFinite(id) && id > 0) el.dataset.logId = String(id);
    logPane.appendChild(el);
    if (autoscrollEl.checked) logPane.scrollTop = logPane.scrollHeight;
    while (logPane.childElementCount > 5000) {
      const first = logPane.firstChild;
      const firstId = first && first.dataset ? Number(first.dataset.logId) : NaN;
      if (Number.isFinite(firstId) && firstId > 0) renderedLogIds.delete(firstId);
      logPane.removeChild(first);
    }
    return true;
  }

  function classifyLine(text, level) {
    const lvl = (level || '').toLowerCase();
    if (lvl === 'pass') return 'lvl-pass';
    if (lvl === 'warn') return 'lvl-warn';
    if (lvl === 'error') return 'lvl-error';
    const t = text || '';
    if (/🏆|best 발견/.test(t)) return 'lvl-pass';
    // 텍스트 휴리스틱 경고(개별 fail 결과 등)는 색만 warn — 핵심만 모드에선 숨긴다.
    if (/⚠|🛑|ERROR|error|예외|실패|fail/i.test(t)) return 'lvl-warn lvl-info';
    return 'lvl-info';   // 핵심만 필터의 숨김 대상
  }

  // 핵심만 토글 — info 줄 숨김 (CSS 클래스로만 처리, 재렌더 없음).
  const coreOnlyEl = $('#core-only');
  if (coreOnlyEl) {
    coreOnlyEl.addEventListener('change', () => {
      logPane.classList.toggle('core-only', coreOnlyEl.checked);
      if (autoscrollEl.checked) logPane.scrollTop = logPane.scrollHeight;
    });
  }

  btnClearLog.addEventListener('click', async () => {
    // 화면만 지움 — 서버에 저장된 로그/오류/알파 데이터는 건드리지 않는다.
    // 다만 사용자별 "비우기 지점"을 latest 로 이동해 둬서, 재접속/새로고침 시
    // 비우기 이전 로그가 다시 그려지지 않게 한다.
    logPane.innerHTML = '';
    renderedLogIds.clear();
    try {
      const r = await api('/api/logs/clear', { method: 'POST' });
      if (r.ok && r.data && r.data.ok) {
        lastLogId = Number(r.data.last_cleared_log_id || lastLogId);
        stopStreaming();
        startStreaming();
      }
    } catch (_) {}
  });

  // ── 제출/미추가 카운트 (타일 + 파이프라인) — 모바일 경량 status 재사용 ──
  async function refreshBest() {
    try {
      const r2 = await api('/api/m_status');
      if (r2.ok && r2.data && r2.data.ok) {
        setText('#tile-submitted', String(r2.data.submitted_count || 0));
        setText('#tile-unsubmitted', String(r2.data.unsubmitted_count || 0));
        setText('#pl-submit-count', String(r2.data.submitted_count || 0));
      }
    } catch (_) {}
  }

  // ═══════════════════════════════════════════════════════════════════════
  // 진화 시각화 — /api/recent_alphas 를 라운드(세대)로 묶어 SVG 로 그린다.
  // desc 태그 규약: genome_models._desc → "... [rand]" | "... [mut g2]" | "... [xo g1]"
  // ═══════════════════════════════════════════════════════════════════════

  // 종이/잉크 원장 테마 — 스펙 12번 차트 레시피 5색을 그대로 사용(순서·의미 고정).
  const ORIGIN_COLOR = {
    rand: '#1C1914', mut: '#8C6D3B', xo: '#3F6B45', etc: '#6E675A',
  };
  const ORIGIN_LABEL = { rand: '탐색', mut: '변이', xo: '교차', etc: '개편 전' };

  let _evoAlphas = [];      // 최신 fetch 원본 (id ASC)
  let _evoMetric = 'sharpe';
  let _evoLimit = 400;
  let _evoDots = [];        // 히트테스트용 [{x,y,a,label}]
  let _lbSort = 'sharpe';           // 리더보드 정렬 키
  let _lbPage = 0;                  // 리더보드 쪽 번호 (0-based)
  let _scatterDots = [];            // Sharpe×Turnover 산점도 히트테스트

  // ── 게이트/품질 상수 (기준선·색상) ──
  const GATE_SHARPE = 1.25;   // 제출 하한(보편 D1). D0 은 더 높지만 표시 기준선은 1.25/1.5 로 통일.
  const GATE_SHARPE_HI = 1.5;
  const TURNOVER_CAP = 0.7;   // reward.py turnover_cap 과 동일
  const SELF_CORR_GATE = 0.7; // 제출 self-corr 컷오프

  function numOrNull(v) { const f = Number(v); return isFinite(f) ? f : null; }
  function turnoverOf(a) { let t = numOrNull(a.turnover); if (t == null && a.metrics) t = numOrNull(a.metrics.turnover); return t; }
  function selfCorrOf(a) { let c = numOrNull(a.self_corr); if (c == null && a.metrics) c = numOrNull(a.metrics.self_corr); return c; }
  function fitnessOf(a) { let f = numOrNull(a.fitness); if (f == null && a.metrics) f = numOrNull(a.metrics.fitness); return f; }
  // Sharpe 품질 색상 계층 (ok ≥1.5 · gold ≥1.0 · red-deep else). 오리진 색과 구분되는 톤.
  function sharpeColor(s) { if (s == null) return '#6E675A'; if (s >= GATE_SHARPE_HI) return '#3F6B45'; if (s >= 1.0) return '#8C6D3B'; return '#8C2F1A'; }

  function parseOrigin(desc) {
    const m = /\[(rand|mut|xo)(?:\s*g(\d+))?\]/.exec(desc || '');
    if (!m) return { op: 'etc', gen: 0 };
    return { op: m[1], gen: Number(m[2] || 0) };
  }

  // ── 계보 ──────────────────────────────────────────────────────────────
  // parent_alpha_id 체인의 뿌리 = 하나의 진화 갈래. 뿌리마다 색을 달리해 갈래를 분간한다.
  let _byId = new Map(), _rootCache = new Map();

  function indexLineage(alphas) {
    _byId = new Map(alphas.map((a) => [Number(a.id), a]));
    _rootCache = new Map();
  }

  function rootOf(id) {
    if (_rootCache.has(id)) return _rootCache.get(id);
    let cur = id;
    for (let hop = 0; hop < 500; hop++) {   // 순환 방어 — 데이터가 깨져도 멈추지 않게
      const p = Number((_byId.get(cur) || {}).parent_alpha_id || 0);
      if (!p || !_byId.has(p)) break;
      cur = p;
    }
    _rootCache.set(id, cur);
    return cur;
  }

  // 골든앵글 hue — 이웃 계보끼리 색이 붙지 않는다. 채도·명도는 잉크 톤에 맞춰 낮춘다.
  function lineageColor(root) {
    return `hsl(${Math.round((root * 137.508) % 360)} 38% 42%)`;
  }

  function parseGenes(desc) {
    // "{model} {family}: {combine}/{ta}+{tb} {universe}x{neut} [tag]"
    const m = /^\S+\s+(\w+):\s*(\w+)\/([\w]+)\+([\w]+)\s+(\w+)x(\w+)/.exec(desc || '');
    if (!m) return null;
    return { family: m[1], combine: m[2], ta: m[3], tb: m[4], universe: m[5], neut: m[6] };
  }

  function sharpeOf(a) {
    if (a.sharpe != null && isFinite(Number(a.sharpe))) return Number(a.sharpe);
    const s = a.metrics && a.metrics.sharpe;
    const v = parseFloat(s);
    return isFinite(v) ? v : null;
  }

  function groupRounds(alphas) {
    // round_id 로 묶는다 (focus sub-round 는 round_num 이 같아도 round_id 가 다름).
    const map = new Map();
    for (const a of alphas) {
      const key = a.round_id != null ? String(a.round_id) : `${a.round_num}/${a.phase || 0}`;
      if (!map.has(key)) {
        map.set(key, {
          key,
          roundNum: Number(a.round_num || 0),
          phase: Number(a.phase || 0),
          alphas: [],
        });
      }
      map.get(key).alphas.push(a);
    }
    const groups = [...map.values()];
    groups.sort((g1, g2) => Number(g1.key) - Number(g2.key));
    // limit 절단으로 가장 오래된 그룹은 일부만 fetch 됐을 수 있음 — 버린다.
    if (groups.length > 1) groups.shift();
    for (const g of groups) {
      g.label = g.phase > 0 ? `${g.roundNum}·f${g.phase}` : String(g.roundNum);
      g.alphas.sort((x, y) => Number(x.idx || 0) - Number(y.idx || 0));
    }
    return groups;
  }

  async function refreshEvolution() {
    const r = await api('/api/recent_alphas?limit=' + _evoLimit);
    if (!(r.ok && r.data && r.data.ok)) return;
    const rows = (r.data.alphas || []).slice();
    rows.reverse();   // 서버는 id DESC — 시간순 ASC 로.
    _evoAlphas = rows;
    indexLineage(rows);
    renderEvolution();
  }

  function svgEl(tag, attrs) {
    const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function renderEvolution() {
    const svg = $('#evo-chart');
    const comp = $('#evo-comp');
    const empty = $('#evo-empty');
    if (!svg || !comp) return;
    svg.replaceChildren();
    comp.replaceChildren();
    _evoDots = [];

    const groups = groupRounds(_evoAlphas);
    const hasData = groups.length > 0;
    if (empty) empty.hidden = hasData;
    renderPipelineComposition(groups);
    // #7 성과 분석 패널 — 하나가 실패해도 진화 차트를 깨뜨리지 않게 격리.
    try { renderScatter(_evoAlphas); } catch (e) { console.error('renderScatter', e); }
    try { renderLeaderboard(_evoAlphas, _lbSort); } catch (e) { console.error('renderLeaderboard', e); }
    if (!hasData) return;

    const W = svg.clientWidth || 900;
    const H = svg.clientHeight || 350;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const mL = 46, mR = 14, mT = 14, mB = 22;
    const plotW = W - mL - mR, plotH = H - mT - mB;
    const n = groups.length;
    const bw = plotW / n;

    // ── y 도메인 ──
    const metric = _evoMetric;
    const val = (a) => metric === 'sharpe' ? sharpeOf(a)
                                           : (a.pass_count != null ? Number(a.pass_count) : null);
    let lo, hi;
    if (metric === 'sharpe') {
      let mn = 0, mx = 1;
      for (const g of groups) for (const a of g.alphas) {
        const v = val(a);
        if (v == null) continue;
        if (v < mn) mn = v;
        if (v > mx) mx = v;
      }
      lo = Math.max(-4, Math.floor(mn * 2) / 2);
      hi = Math.min(4, Math.ceil(mx * 2) / 2);
      if (hi - lo < 1) hi = lo + 1;
    } else {
      lo = 0;
      hi = 7;
      for (const g of groups) for (const a of g.alphas) {
        const v = val(a);
        if (v != null && v > hi) hi = v;
      }
    }
    const y = (v) => mT + plotH - ((v - lo) / (hi - lo)) * plotH;
    const xOf = (gi) => mL + (gi + 0.5) * bw;

    // ── 그리드 + y축 눈금 ──
    const ticks = 5;
    for (let t = 0; t <= ticks; t++) {
      const v = lo + (hi - lo) * (t / ticks);
      const yy = y(v);
      svg.appendChild(svgEl('line', {
        x1: mL, x2: W - mR, y1: yy, y2: yy,
        stroke: 'rgba(28,25,20,0.1)', 'stroke-width': 1,
      }));
      const lbl = svgEl('text', {
        x: mL - 8, y: yy + 3.5, 'text-anchor': 'end',
        fill: '#6E675A', 'font-size': 10, 'font-family': 'IBM Plex Mono, monospace',
      });
      lbl.textContent = metric === 'sharpe' ? v.toFixed(1) : String(Math.round(v));
      svg.appendChild(lbl);
    }
    // 0 기준선 (sharpe)
    if (metric === 'sharpe' && lo < 0 && hi > 0) {
      svg.appendChild(svgEl('line', {
        x1: mL, x2: W - mR, y1: y(0), y2: y(0),
        stroke: 'rgba(28,25,20,0.3)', 'stroke-width': 1, 'stroke-dasharray': '3 4',
      }));
    }
    // 제출 게이트 기준선 (sharpe 1.25 gold / 1.5 ok) — "얼마나 통과선에 근접했나"를 한눈에.
    if (metric === 'sharpe') {
      for (const [gv, col] of [[1.25, 'rgba(140,109,59,0.5)'], [1.5, 'rgba(63,107,69,0.5)']]) {
        if (gv > lo && gv < hi) {
          svg.appendChild(svgEl('line', {
            x1: mL, x2: W - mR, y1: y(gv), y2: y(gv),
            stroke: col, 'stroke-width': 1, 'stroke-dasharray': '2 3',
          }));
          const t = svgEl('text', {
            x: W - mR - 2, y: y(gv) - 3, 'text-anchor': 'end',
            fill: col.replace(/,[^,]+\)$/, ',0.9)'), 'font-size': 9, 'font-family': 'IBM Plex Mono, monospace',
          });
          t.textContent = gv.toFixed(2);
          svg.appendChild(t);
        }
      }
    }

    // ── x축 라벨 (겹침 방지 간격) ──
    const step = Math.max(1, Math.ceil(n / 12));
    for (let i = 0; i < n; i += step) {
      const g = groups[i];
      if (g.phase > 0) continue;    // focus sub-round 라벨은 생략 (툴팁으로 확인)
      const lbl = svgEl('text', {
        x: xOf(i), y: H - 6, 'text-anchor': 'middle',
        fill: '#6E675A', 'font-size': 10, 'font-family': 'IBM Plex Mono, monospace',
      });
      lbl.textContent = 'R' + g.roundNum;
      svg.appendChild(lbl);
    }

    // ── 알파 산점 + 계보 선 ──
    // 엣지는 점 **아래** 레이어여야 점을 가리지 않는다 → g 두 개를 먼저 깔고 채운다.
    const gEdges = svgEl('g', { 'stroke-linecap': 'round' });
    const gDots = svgEl('g', {});
    svg.appendChild(gEdges);
    svg.appendChild(gDots);

    const pos = new Map();     // alpha.id → [x, y]
    groups.forEach((g, gi) => {
      const cnt = g.alphas.length;
      g.alphas.forEach((a, ai) => {
        const v = val(a);
        if (v == null) return;
        const o = parseOrigin(a.desc);
        const jitter = cnt > 1 ? ((ai / (cnt - 1)) - 0.5) * Math.min(bw * 0.55, 26) : 0;
        const cx = xOf(gi) + jitter;
        const cy = y(Math.max(lo, Math.min(hi, v)));
        const submitted = !!a.submitted;
        const c = svgEl('circle', {
          cx: cx.toFixed(1), cy: cy.toFixed(1),
          r: submitted ? 4.5 : 3.2,
          fill: ORIGIN_COLOR[o.op] || ORIGIN_COLOR.etc,
          'fill-opacity': submitted ? 1 : 0.82,
          stroke: submitted ? '#1C1914' : '#F5F1E8',
          'stroke-width': submitted ? 1.6 : 1,
        });
        gDots.appendChild(c);
        if (a.id != null) pos.set(Number(a.id), [cx, cy]);
        _evoDots.push({ x: cx, y: cy, a, o, label: g.label });
      });
    });

    // 부모→자식 엣지만 그린다. 신규(rand·부모 화면 밖)는 선 없는 점 = 새 계보의 시작.
    // 엣지만 그려도 체인(1-1→2-1→3-1)은 저절로 이어져 보인다 — 별도 추적 불필요.
    for (const d of _evoDots) {
      const p = pos.get(Number(d.a.parent_alpha_id || 0));
      if (!p) continue;
      gEdges.appendChild(svgEl('line', {
        x1: p[0].toFixed(1), y1: p[1].toFixed(1),
        x2: d.x.toFixed(1), y2: d.y.toFixed(1),
        stroke: lineageColor(rootOf(Number(d.a.id))),
        'stroke-width': 1.4, opacity: 0.6,
      }));
    }

    // ── 세대 구성 스트립 (스캐터와 x 정렬) ──
    const cW = comp.clientWidth || W;
    const cH = comp.clientHeight || 44;
    comp.setAttribute('viewBox', `0 0 ${cW} ${cH}`);
    const cbw = (cW - mL - mR) / n;
    const barW = Math.max(2, cbw - 2);
    groups.forEach((g, gi) => {
      const counts = { rand: 0, mut: 0, xo: 0, etc: 0 };
      for (const a of g.alphas) counts[parseOrigin(a.desc).op]++;
      const total = g.alphas.length || 1;
      let yCursor = cH - 4;
      for (const op of ['rand', 'mut', 'xo', 'etc']) {
        if (!counts[op]) continue;
        const h = (counts[op] / total) * (cH - 8);
        yCursor -= h;
        comp.appendChild(svgEl('rect', {
          x: (mL + gi * cbw + 1).toFixed(1), y: yCursor.toFixed(1),
          width: barW.toFixed(1), height: Math.max(1, h - 1).toFixed(1),
          fill: ORIGIN_COLOR[op], 'fill-opacity': 0.75,
        }));
      }
    });
  }

  // ── 툴팁 (nearest-dot 히트테스트) ──
  (function initEvoTooltip() {
    const wrap = $('#evo-wrap');
    const tip = $('#evo-tip');
    const svg = $('#evo-chart');
    if (!wrap || !tip || !svg) return;
    function onMove(ev) {
      if (!_evoDots.length) { tip.style.display = 'none'; return; }
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left;
      const my = ev.clientY - rect.top;
      let best = null, bd = 18 * 18;
      for (const d of _evoDots) {
        const dx = d.x - mx, dy = d.y - my;
        const dist = dx * dx + dy * dy;
        if (dist < bd) { bd = dist; best = d; }
      }
      if (!best) { tip.style.display = 'none'; return; }
      renderTip(tip, best);
      const wrapRect = wrap.getBoundingClientRect();
      const px = best.x + (rect.left - wrapRect.left);
      const py = best.y + (rect.top - wrapRect.top);
      tip.style.display = 'block';
      const tw = tip.offsetWidth || 220;
      tip.style.left = Math.max(4, Math.min(px + 14, wrapRect.width - tw - 8)) + 'px';
      tip.style.top = Math.max(4, py - tip.offsetHeight - 12) + 'px';
    }
    wrap.addEventListener('mousemove', onMove);
    wrap.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  })();

  function renderTip(tip, d) {
    tip.replaceChildren();
    const a = d.a;
    const head = document.createElement('div');
    head.className = 'tt-head';
    head.textContent = `R${d.label} #${a.idx} · ${ORIGIN_LABEL[d.o.op]}` + (d.o.gen ? ` g${d.o.gen}` : '');
    tip.appendChild(head);
    const sh = sharpeOf(a);
    const fit = a.fitness != null ? a.fitness : (a.metrics && parseFloat(a.metrics.fitness));
    const row1 = document.createElement('div');
    row1.className = 'tt-row';
    row1.append('Sharpe ');
    const b1 = document.createElement('b');
    b1.textContent = sh != null ? sh.toFixed(2) : '—';
    row1.appendChild(b1);
    row1.append('  ·  Fitness ');
    const b2 = document.createElement('b');
    b2.textContent = (fit != null && isFinite(fit)) ? Number(fit).toFixed(2) : '—';
    row1.appendChild(b2);
    tip.appendChild(row1);
    const row2 = document.createElement('div');
    row2.className = 'tt-row';
    row2.append(`PASS ${a.pass_count || 0} · FAIL ${a.fail_count || 0}`);
    if (a.submitted) row2.append('  ·  제출됨');
    else if (a.cached) row2.append('  ·  캐시');
    tip.appendChild(row2);
    const genes = document.createElement('div');
    genes.className = 'tt-genes';
    genes.textContent = a.desc || a.code || '';
    tip.appendChild(genes);
  }


  // ── 파이프라인의 라운드 구성 카운트 (최신 라운드 기준) ──
  function renderPipelineComposition(groups) {
    if (!groups.length) return;
    const g = groups[groups.length - 1];
    const counts = { rand: 0, mut: 0, xo: 0, etc: 0 };
    let gatePass = 0;
    for (const a of g.alphas) {
      counts[parseOrigin(a.desc).op]++;
      // 게이트 통과 = 전항목 PASS (fail 0 + pass 1개 이상).
      if (Number(a.pass_count || 0) > 0 && Number(a.fail_count || 0) === 0) gatePass++;
    }
    setText('#pl-rand-count', String(counts.rand));
    setText('#pl-xo-count', `${counts.xo}+${counts.mut}`);
    setText('#pl-gate-count', `${gatePass}/${g.alphas.length}`);
  }

  // ═══════════════════════════════════════════════════════════════════════
  // #7 성과 분석 — Sharpe×Turnover 파레토 · 설정별 평균 · 리더보드
  //   전부 무의존 SVG/DOM. 데이터는 /api/recent_alphas 의 최상위 컬럼
  //   (sharpe·turnover·self_corr·universe·neutralization·decay) 을 그대로 사용.
  // ═══════════════════════════════════════════════════════════════════════

  function svgText(x, y, txt, opts) {
    const el = svgEl('text', Object.assign({
      x, y, fill: '#6E675A', 'font-size': 10, 'font-family': 'IBM Plex Mono, monospace',
    }, opts || {}));
    el.textContent = txt;
    return el;
  }

  // 산점도 축 — y×x 조합을 고를 수 있다(2026-07-29 사장 지시). lower = 낮을수록 좋음.
  const AXES = {
    sharpe:   { label: 'Sharpe',   get: sharpeOf,   pct: false, lower: false, gates: [GATE_SHARPE, GATE_SHARPE_HI] },
    turnover: { label: 'Turnover', get: turnoverOf, pct: true,  lower: true,  gates: [TURNOVER_CAP] },
    fitness:  { label: 'Fitness',  get: fitnessOf,  pct: false, lower: false, gates: [1.0] },
  };
  let _scatterPair = 'sharpe:turnover';    // 'y:x'

  // y × x 산점도 + 파레토 프론트(각 축의 좋은 방향) + 게이트 기준선.
  function renderScatter(alphas) {
    const svg = $('#scatter-chart');
    const empty = $('#scatter-empty');
    if (!svg) return;
    svg.replaceChildren();
    _scatterDots = [];
    const [yk, xk] = _scatterPair.split(':');
    const Y = AXES[yk] || AXES.sharpe, X = AXES[xk] || AXES.turnover;
    const title = $('#scatter-title');
    if (title) title.textContent = `${Y.label} × ${X.label}`;
    const pts = [];
    for (const a of alphas) {
      const yv = Y.get(a), xv = X.get(a);
      if (yv == null || xv == null) continue;
      pts.push({ a, xv, yv, o: parseOrigin(a.desc), submitted: !!a.submitted });
    }
    if (empty) empty.hidden = pts.length > 0;
    if (!pts.length) { svg.removeAttribute('viewBox'); return; }

    const W = svg.clientWidth || 440, H = svg.clientHeight || 300;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    const mL = 44, mR = 12, mT = 12, mB = 30;
    const plotW = W - mL - mR, plotH = H - mT - mB;
    let maxX = X.pct ? 0.8 : 2, minY = 0, maxY = 2;
    for (const p of pts) { if (p.xv > maxX) maxX = p.xv; if (p.yv < minY) minY = p.yv; if (p.yv > maxY) maxY = p.yv; }
    maxX = Math.min(maxX * 1.05, X.pct ? 2.0 : 10);   // 이상치 클램프
    maxY = Math.ceil(maxY * 2) / 2; minY = Math.floor(minY * 2) / 2;
    if (maxY - minY < 1) maxY = minY + 1;
    const xOf = (v) => mL + Math.min(Math.max(v, 0), maxX) / maxX * plotW;
    const yOf = (v) => mT + plotH - (Math.max(minY, Math.min(maxY, v)) - minY) / (maxY - minY) * plotH;
    const fmt = (ax, v) => ax.pct ? (v * 100).toFixed(0) + '%' : v.toFixed(2);

    // 그리드 + y 눈금
    for (let k = 0; k <= 4; k++) {
      const v = minY + (maxY - minY) * (k / 4), yy = yOf(v);
      svg.appendChild(svgEl('line', { x1: mL, x2: W - mR, y1: yy, y2: yy, stroke: 'rgba(28,25,20,0.1)', 'stroke-width': 1 }));
      svg.appendChild(svgText(mL - 6, yy + 3.5, Y.pct ? (v * 100).toFixed(0) + '%' : v.toFixed(1), { 'text-anchor': 'end' }));
    }
    // x 눈금
    for (let k = 0; k <= 4; k++) {
      const v = maxX * (k / 4), xx = xOf(v);
      svg.appendChild(svgText(xx, H - 8, X.pct ? (v * 100).toFixed(0) + '%' : v.toFixed(1), { 'text-anchor': 'middle' }));
    }
    svg.appendChild(svgText(mL, H - 18, X.label + ' →', { 'text-anchor': 'start', fill: '#6E675A' }));
    // 게이트 기준선 — y 축은 수평, x 축은 수직.
    const GCOL = ['rgba(140,109,59,0.5)', 'rgba(63,107,69,0.5)'];
    Y.gates.forEach((gv, i) => {
      if (gv >= minY && gv <= maxY) {
        svg.appendChild(svgEl('line', { x1: mL, x2: W - mR, y1: yOf(gv), y2: yOf(gv), stroke: GCOL[i] || GCOL[0], 'stroke-width': 1, 'stroke-dasharray': '3 4' }));
      }
    });
    X.gates.forEach((gv) => {
      if (gv <= maxX) {
        svg.appendChild(svgEl('line', { x1: xOf(gv), x2: xOf(gv), y1: mT, y2: mT + plotH, stroke: 'rgba(140,47,26,0.45)', 'stroke-width': 1, 'stroke-dasharray': '3 4' }));
      }
    });

    // 파레토 프론트: x 를 좋은 방향으로 훑으며 y 최고 기록 갱신점만 남긴다.
    const sorted = pts.slice().sort((p, q) =>
      (X.lower ? p.xv - q.xv : q.xv - p.xv) || (Y.lower ? p.yv - q.yv : q.yv - p.yv));
    const front = [];
    let best = Y.lower ? Infinity : -Infinity;
    for (const p of sorted) {
      if (Y.lower ? p.yv < best : p.yv > best) { front.push(p); best = p.yv; }
    }
    if (front.length > 1) {
      const d = front.map((p, i) => (i ? 'L' : 'M') + xOf(p.xv).toFixed(1) + ' ' + yOf(p.yv).toFixed(1)).join(' ');
      svg.appendChild(svgEl('path', { d, fill: 'none', stroke: '#A63A22', 'stroke-width': 1.4, 'stroke-opacity': 0.6 }));
    }
    const frontSet = new Set(front.map((p) => p.a));

    for (const p of pts) {
      const x = xOf(p.xv), y = yOf(p.yv);
      const onFront = frontSet.has(p.a);
      const col = ORIGIN_COLOR[p.o.op] || ORIGIN_COLOR.etc;
      if (onFront) {
        const r = p.submitted ? 5 : 4;
        svg.appendChild(svgEl('path', { d: `M ${x} ${y - r} L ${x + r} ${y} L ${x} ${y + r} L ${x - r} ${y} Z`, fill: col, stroke: '#A63A22', 'stroke-width': 1.3 }));
      } else {
        svg.appendChild(svgEl('circle', { cx: x.toFixed(1), cy: y.toFixed(1), r: p.submitted ? 4 : 2.8, fill: col, 'fill-opacity': 0.8, stroke: p.submitted ? '#1C1914' : '#F5F1E8', 'stroke-width': p.submitted ? 1.4 : 0.8 }));
      }
      _scatterDots.push({ x, y, a: p.a, o: p.o, front: onFront,
                          text: `${Y.label} ${fmt(Y, p.yv)} · ${X.label} ${fmt(X, p.xv)}` });
    }
  }

  // 설정 축(neutralization|universe|decay) 별 평균 Sharpe 수평 막대(품질 색상, 내림차순).

  // ── 전략 리서치 — /api/research (논문 수집 → 가설 → 타입드 유전체 후보) ──────
  const RS_STEP_ORDER = ['gathering', 'ideating', 'concretizing', 'ready'];

  async function startResearch() {
    const input = $('#rs-query');
    const q = (input.value || '').trim();
    if (!q) { input.focus(); return; }
    const btn = $('#btn-research');
    btn.disabled = true;
    const r = await api('/api/research', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q }),
    });
    btn.disabled = false;
    if (!(r.ok && r.data && r.data.ok)) {
      const reason = (r.data && r.data.detail) || '리서치 시작 실패';
      const body = $('#rs-body');
      body.hidden = false;
      body.innerHTML = `<div class="rs-error">리서치 실패 — ${reason}</div>`;
      return;
    }
    refreshResearch();
  }

  async function refreshResearch() {
    const r = await api('/api/research/status');
    if (!(r.ok && r.data && r.data.ok)) return;
    renderResearch(r.data);
  }

  function renderResearch(d) {
    const run = d.run;
    const empty = $('#rs-empty');
    const steps = $('#rs-steps');
    const body = $('#rs-body');
    const counts = d.specs || {};
    const pend = counts.pending || 0;
    const seeded = counts.seeded || 0;
    setText('#rs-spec-counts', (pend || seeded)
      ? `전략 후보 — 대기 ${pend} · 시뮬 완료 ${seeded}` : '');

    if (!run) { empty.hidden = false; steps.hidden = true; body.hidden = true; return; }
    empty.hidden = true;
    steps.hidden = false;
    body.hidden = false;

    const status = run.status || 'pending';
    const doneIdx = RS_STEP_ORDER.indexOf(status);
    steps.querySelectorAll('.rs-step').forEach((el) => {
      const i = RS_STEP_ORDER.indexOf(el.dataset.step);
      el.classList.toggle('active', i === doneIdx && status !== 'ready');
      el.classList.toggle('done', status === 'ready' ? true : (i >= 0 && i < doneIdx));
    });

    const parts = [];
    parts.push(`<div class="rs-query">“${escapeHtml(run.query || '')}”` +
      (status === 'error' ? ` <span class="rs-error">— 실패: ${escapeHtml(run.error || '')}</span>` : '') +
      `</div>`);

    const srcs = run.sources || [];
    if (srcs.length) {
      const items = srcs.slice(0, 8).map((sx) =>
        `<a class="rs-src" href="${escapeHtml(sx.url)}" target="_blank" rel="noopener"
            title="${escapeHtml(sx.title || '')}">[${sx.n}] ${escapeHtml((sx.title || '').slice(0, 42))}</a>`
      ).join('');
      parts.push(`<div class="rs-sources"><span class="rs-label">근거 ${srcs.length}건</span>${items}</div>`);
    }

    for (const h of (d.hypotheses || [])) {
      const cite = (h.citations && h.citations.length)
        ? `<span class="rs-cite">출처 ${h.citations.join(', ')}</span>`
        : `<span class="rs-cite rs-nocite">근거 없음</span>`;
      const specs = (h.specs || []).map((sp) => {
        const g = sp.genome || {};
        const badge = sp.status === 'seeded' ? '<span class="rs-badge done">시뮬됨</span>'
          : sp.status === 'exhausted' ? '<span class="rs-badge">중복</span>'
          : '<span class="rs-badge pend">대기</span>';
        const knobs = [
          `delay ${sp.delay}`, `decay ${g.decay}`,
          g.trade_when && g.trade_when !== 'OFF' ? `진입 ${g.trade_when}` : null,
          g.winsor_std ? `winsor ${g.winsor_std}` : null,
          g.universe, g.neutralization,
        ].filter(Boolean).join(' · ');
        return `<div class="rs-spec">${badge}
          <div class="rs-spec-why">${escapeHtml(sp.why || '')}</div>
          <code class="rs-spec-code">${escapeHtml(sp.code || '')}</code>
          <div class="rs-spec-knobs">${escapeHtml(knobs)}</div></div>`;
      }).join('');
      parts.push(`<div class="rs-hypo">
        <div class="rs-hypo-head">${escapeHtml(h.title || '')} ${cite}</div>
        <div class="rs-hypo-why">${escapeHtml(h.rationale || '')}</div>
        ${specs}</div>`);
    }
    body.innerHTML = parts.join('');
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  // ── 학습 현황 — /api/learning (정향변이 성공률 행렬 + 밴딧 arm) ──────────
  const DL_CAT_LABEL = {
    turnover_high: 'Turnover 초과', turnover_low: 'Turnover 과소',
    sub_universe: 'Sub-universe Sharpe', correlation: 'Self-correlation',
    concentration: 'Weight 집중', signal: '신호 미달(Sharpe·Fitness…)',
  };
  const DL_DIR_LABEL = {
    smooth: '스무딩(decay·lookback↑)', sharpen: '민감도↑(decay↓)',
    concentration: '분산(중립화·truncation)', universe: '유니버스 확대',
    decorrelate: '탈상관(패밀리 교체)', signal: '신호 유전자 변이',
  };
  const BANDIT_DIM_LABEL = {
    universe: '유니버스', neutralization: '중립화', decay_bucket: 'DECAY',
    family: '패밀리', combine: '결합',
  };

  async function refreshLearning() {
    const r = await api('/api/learning');
    if (!(r.ok && r.data && r.data.ok)) return;
    renderDirectiveLearning(r.data.directives || []);
  }

  function renderDirectiveLearning(rows) {
    const table = $('#dl-table');
    const empty = $('#dl-empty');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.replaceChildren();
    const sorted = rows.slice().sort((a, b) => (b.n - a.n) || (b.win_rate - a.win_rate));
    if (empty) empty.hidden = sorted.length > 0;
    table.hidden = sorted.length === 0;
    for (const d of sorted.slice(0, 18)) {
      const tr = document.createElement('tr');
      const rate = Number(d.win_rate || 0);
      const rateColor = rate >= 0.5 ? '#3F6B45' : rate >= 0.25 ? '#8C6D3B' : '#8C2F1A';
      tr.innerHTML = `
        <td>${DL_CAT_LABEL[d.category] || d.category}</td>
        <td>${DL_DIR_LABEL[d.directive] || d.directive}</td>
        <td class="num">${d.n}</td>
        <td class="num">${d.wins}</td>
        <td class="num" style="color:${rateColor}">${(rate * 100).toFixed(0)}%</td>`;
      tbody.appendChild(tr);
    }
  }


  // ── 4탭 내비 (2026-07-27) — 운영 / 진화 분석 / 제출 대기 / 사용설명서 ──────
  function initMainTabs() {
    const nav = $('#main-tabs');
    if (!nav) return;
    const pages = { ops: $('#page-ops'), evo: $('#page-evo'), queue: $('#page-queue'),
                    help: $('#page-help') };
    function show(key) {
      for (const [k, el] of Object.entries(pages)) if (el) el.hidden = k !== key;
      nav.querySelectorAll('button').forEach((b) =>
        b.classList.toggle('active', b.dataset.page === key));
      try { localStorage.setItem('gwqbMainTab', key); } catch (e) {}
      // 숨김 상태에서 그린 canvas 는 폭 0 — 탭이 보일 때 다시 그린다.
      if (key === 'evo') {
        try { refreshEvolution(); refreshLearning(); refreshBest(); } catch (e) {}
      }
      if (key === 'queue') { try { refreshSubmitQueue(); } catch (e) {} }
      if (key === 'ops') {
        try {
          if (logPane && autoscrollEl && autoscrollEl.checked)
            logPane.scrollTop = logPane.scrollHeight;
        } catch (e) {}
      }
    }
    nav.querySelectorAll('button').forEach((b) =>
      b.addEventListener('click', () => show(b.dataset.page)));
    let saved = 'ops';
    try { saved = localStorage.getItem('gwqbMainTab') || 'ops'; } catch (e) {}
    show(pages[saved] ? saved : 'ops');
  }

  // ── 제출 내역 (운영 탭) — 최근 제출 시도, 성공 강조 ───────────────────────
  async function refreshSubmitHistory() {
    const r = await api('/api/m_submits?limit=50');
    if (!(r.ok && r.data && r.data.ok)) return;
    // 성공한 제출만 — 서버가 이미 걸러 보내지만, 옛 캐시 응답에도 안전하게 (2026-07-27).
    const rows = (r.data.attempts || []).filter((a) => a.submitted);
    const table = $('#sh-table');
    const empty = $('#sh-empty');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.replaceChildren();
    if (empty) empty.style.display = rows.length ? 'none' : '';
    table.style.display = rows.length ? '' : 'none';
    for (const a of rows) {
      const tr = document.createElement('tr');
      const td = (txt, cls) => { const c = document.createElement('td'); if (cls) c.className = cls; c.textContent = txt; return c; };
      const t = a.ts ? new Date(a.ts * 1000) : null;
      tr.appendChild(td(t ? `${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')} ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}` : '—', 'dim'));
      tr.appendChild(td(`r${a.round_num}${a.idx ? '-#' + a.idx : ''}`, 'dim'));
      const st = String(a.submit_status || '');
      const ok = !!a.submitted;
      const res = td(ok ? '✅ 제출' : '⛔ 거절');
      if (ok) res.style.fontWeight = '700';
      tr.appendChild(res);
      tr.appendChild(td(st.slice(0, 70), 'dim'));
      tbody.appendChild(tr);
    }
  }

  // ── 사용설명서 (2026-07-27) — 컨설턴트 / 일반 계정 두 벌 ────────────────────
  // 계정 종류마다 리전 제약·제출 기준·자동화 범위가 달라서, 한 화면에 섞으면
  // 자기한테 해당 없는 규칙을 자기 규칙으로 읽는다. 로그인한 계정 쪽을 먼저 편다.
  let _helpAudPinned = false;      // 사용자가 직접 고르면 상태 폴링이 덮지 않는다

  function paintHelpAudience(aud) {
    const seg = $('#help-audience');
    if (!seg) return;
    seg.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.aud === aud;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    document.querySelectorAll('.help-body[data-aud]').forEach((el) => {
      el.hidden = el.dataset.aud !== aud;
    });
  }

  function initHelp() {
    const seg = $('#help-audience');
    if (!seg) return;
    seg.addEventListener('click', (ev) => {
      const btn = ev.target.closest('button[data-aud]');
      if (!btn) return;
      _helpAudPinned = true;
      const note = $('#help-auto-note');
      if (note) note.hidden = true;
      paintHelpAudience(btn.dataset.aud);
    });
    paintHelpAudience('rc');
  }

  // ── 제출 방식 토글 (2026-07-27) — 자동 제출 / 목록에 추가 ──────────────────
  // 설명은 헤더의 title 툴팁이 맡는다 — 카드 헤더에 버튼 2개만 두라는 지시(2026-07-27).
  function paintSubmitMode(mode) {
    const seg = $('#submit-mode');
    if (!seg) return;
    seg.querySelectorAll('button').forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
  }

  function initSubmitMode() {
    const seg = $('#submit-mode');
    if (!seg) return;
    seg.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('button[data-mode]');
      if (!btn || btn.classList.contains('active')) return;
      const mode = btn.dataset.mode;
      paintSubmitMode(mode);                       // 낙관적 반영 — 즉시 응답감
      const r = await api('/api/submit_mode', {
        method: 'POST', body: JSON.stringify({ submit_mode: mode }),
      });
      // 서버가 거절하면 서버 값이 진실이다. 제출 여부를 화면이 잘못 말하면 안 된다.
      paintSubmitMode((r.data && r.data.submit_mode) || 'auto');
    });
  }

  // ── 제출 대기 큐 (2026-07-27) — 테마 보류(수동 제출 버튼) + 예산 초과 대기 ──
  async function refreshSubmitQueue() {
    const r = await api('/api/submit_queue');
    if (!(r.ok && r.data && r.data.ok)) return;
    const rows = r.data.rows || [];
    const table = $('#sq-table');
    const empty = $('#sq-empty');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.replaceChildren();
    if (empty) empty.style.display = rows.length ? 'none' : '';
    table.style.display = rows.length ? '' : 'none';
    const num = (v) => { const f = parseFloat(v); return Number.isFinite(f) ? f.toFixed(2) : '—'; };
    for (const row of rows) {
      const tr = document.createElement('tr');
      const td = (txt, cls) => { const c = document.createElement('td'); if (cls) c.className = cls; c.textContent = txt; return c; };
      // 선택 삭제 (2026-07-28 사장 지시). 제출 진행 중인 행은 지우면 결과를 적을 자리가 없다.
      const pick = document.createElement('td');
      pick.className = 'sq-pick';
      if (row.status !== 'submitting') {
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'sq-cb';
        cb.value = String(row.id);
        cb.setAttribute('aria-label', `${row.wqb_alpha_id} 선택`);
        cb.addEventListener('change', syncSqDelete);
        pick.appendChild(cb);
      }
      tr.appendChild(pick);
      tr.appendChild(td(row.kind === 'theme' ? '🎯 테마' : '📆 예산', 'dim'));
      tr.appendChild(td(row.wqb_alpha_id));
      const m = row.metrics || {};
      tr.appendChild(td(num(m.sharpe), 'num'));
      tr.appendChild(td(num(m.fitness), 'num'));
      tr.appendChild(td(num(m.turnover), 'num'));
      const stLabel = { pending: '대기', submitting: '제출 중…', submitted: '✅ 제출됨',
                        rejected: '거절됨' }[row.status] || row.status;
      tr.appendChild(td(stLabel));
      tr.appendChild(td((row.note || '').slice(0, 60), 'dim'));
      const act = document.createElement('td');
      if (row.status === 'pending' || row.status === 'rejected') {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'mini-btn';
        btn.textContent = '제출';
        btn.addEventListener('click', async () => {
          btn.disabled = true; btn.textContent = '…';
          await api('/api/submit_queue/submit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ id: row.id }),
          });
          refreshSubmitQueue();
        });
        act.appendChild(btn);
      }
      tr.appendChild(act);
      tbody.appendChild(tr);
    }
    const all = $('#sq-all');
    if (all) all.checked = false;
    syncSqDelete();
  }

  function sqChecked() {
    return Array.from(document.querySelectorAll('.sq-cb:checked')).map((c) => Number(c.value));
  }

  function syncSqDelete() {
    const btn = $('#sq-del');
    if (!btn) return;
    const n = sqChecked().length;
    btn.disabled = !n;
    btn.textContent = n ? `선택 삭제 (${n})` : '선택 삭제';
  }

  function initSubmitQueueTools() {
    const all = $('#sq-all');
    if (all) {
      all.addEventListener('change', () => {
        document.querySelectorAll('.sq-cb').forEach((c) => { c.checked = all.checked; });
        syncSqDelete();
      });
    }
    const btn = $('#sq-del');
    if (!btn) return;
    btn.addEventListener('click', async () => {
      const ids = sqChecked();
      if (!ids.length) return;
      // 되돌릴 수 없다 — 지우면 그 알파는 이 큐로 다시 안 돌아온다.
      if (!confirm(`${ids.length}건을 대기 목록에서 삭제합니다. 되돌릴 수 없습니다.`)) return;
      btn.disabled = true;
      await api('/api/submit_queue/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      refreshSubmitQueue();
    });
  }

  // 필드 필터 — ">1", ">=1.2", "<0.7", "1.5"(= >=1.5). 못 읽으면 null(무시).
  function parseFilter(txt) {
    const m = /^\s*(>=|<=|>|<|=)?\s*(-?\d*\.?\d+)\s*$/.exec(txt || '');
    if (!m) return null;
    const op = m[1] || '>=', v = parseFloat(m[2]);
    return (x) => x != null && (op === '>' ? x > v : op === '<' ? x < v
                              : op === '<=' ? x <= v : op === '=' ? x === v : x >= v);
  }

  // 화면 표기와 같은 단위로 받는다 — turnover 는 %, 나머지는 원값.
  const LB_FIELD = {
    sharpe: (a) => sharpeOf(a),
    fitness: (a) => fitnessOf(a),
    turnover: (a) => { const t = turnoverOf(a); return t == null ? null : t * 100; },
    self_corr: (a) => selfCorrOf(a),
    pass_count: (a) => Number(a.pass_count || 0),
  };

  function lbFilters() {
    const out = [];
    document.querySelectorAll('#lb-table input[data-lbf]').forEach((el) => {
      const txt = (el.value || '').trim();
      const f = txt ? parseFilter(txt) : null;
      el.classList.toggle('bad', !!txt && !f);
      if (f) { const get = LB_FIELD[el.dataset.lbf]; if (get) out.push((a) => f(get(a))); }
    });
    return out;
  }

  // 리더보드 — 정렬키(sharpe|fitness|turnover↓) 상위 15개씩 최대 10쪽(=150개).
  // Sharpe 가 없는 행은 시뮬이 깨진 오류 알파라 아예 빼고, 필드 필터를 AND 로 건다.
  const LB_PAGE = 15, LB_MAX_PAGES = 10;

  function renderLeaderboard(alphas, sortKey) {
    const table = $('#lb-table');
    const empty = $('#lb-empty');
    if (!table) return;
    const tbody = table.querySelector('tbody');
    tbody.replaceChildren();
    const keyf = { sharpe: sharpeOf, fitness: fitnessOf, turnover: turnoverOf }[sortKey] || sharpeOf;
    const asc = (sortKey === 'turnover');                              // 낮을수록 좋음
    const filters = lbFilters();
    const rows = alphas.filter((a) => sharpeOf(a) != null && keyf(a) != null
                                      && filters.every((f) => f(a)));
    rows.sort((a, b) => { const va = keyf(a), vb = keyf(b); return asc ? va - vb : vb - va; });
    const pages = Math.min(LB_MAX_PAGES, Math.max(1, Math.ceil(rows.length / LB_PAGE)));
    if (_lbPage >= pages) _lbPage = 0;         // 필터가 좁아져 쪽수가 줄면 첫 쪽으로
    const from = _lbPage * LB_PAGE;
    const top = rows.slice(from, from + LB_PAGE);
    if (empty) empty.style.display = top.length ? 'none' : '';
    table.style.display = top.length ? '' : 'none';
    renderLbPager(pages, rows.length);
    const td = (cls, txt) => { const c = document.createElement('td'); if (cls) c.className = cls; if (txt != null) c.textContent = txt; return c; };
    top.forEach((a, i) => {
      const o = parseOrigin(a.desc), genes = parseGenes(a.desc);
      const tr = document.createElement('tr');
      tr.className = 'lb-row';
      tr.tabIndex = 0;
      tr.title = '클릭 — 알파 상세';
      tr.addEventListener('click', () => openAlphaDetail(a));
      tr.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); openAlphaDetail(a); }
      });
      tr.appendChild(td('num dim', String(from + i + 1)));
      const c1 = document.createElement('td');
      const badge = document.createElement('span'); badge.className = 'origin-badge ' + o.op;
      badge.textContent = o.op === 'rand' ? 'RAND' : o.op === 'mut' ? 'MUT' : o.op === 'xo' ? 'XO' : 'ETC';
      c1.appendChild(badge); tr.appendChild(c1);
      const sh = sharpeOf(a);
      const c2 = td('num', sh != null ? sh.toFixed(2) : '—'); c2.style.color = sharpeColor(sh); c2.style.fontWeight = '600'; tr.appendChild(c2);
      const fi = fitnessOf(a); tr.appendChild(td('num', fi != null ? fi.toFixed(2) : '—'));
      const tv = turnoverOf(a); const c4 = td('num', tv != null ? (tv * 100).toFixed(0) + '%' : '—'); if (tv != null && tv > TURNOVER_CAP) c4.style.color = '#8C2F1A'; tr.appendChild(c4);
      const cv = selfCorrOf(a); const c5 = td('num', cv != null ? cv.toFixed(2) : '—'); if (cv != null && cv > SELF_CORR_GATE) c5.style.color = '#8C2F1A'; tr.appendChild(c5);
      tr.appendChild(td('num', `${a.pass_count || 0}/${(Number(a.pass_count || 0) + Number(a.fail_count || 0))}`));
      const univ = a.universe || (genes && genes.universe) || '—';
      const neut = a.neutralization || (genes && genes.neut) || '—';
      const c7 = td('dim', `${univ}×${neut}` + (a.decay != null ? ` d${a.decay}` : '')); c7.title = a.code || ''; tr.appendChild(c7);
      tr.appendChild(td(null, a.submitted ? '제출' : (a.cached ? '캐시' : '—')));
      tbody.appendChild(tr);
    });
  }

  // 쪽 번호 1~10 — 필터 통과 행이 15개 이하면 아예 감춘다.
  function renderLbPager(pages, total) {
    const el = $('#lb-pager');
    if (!el) return;
    el.replaceChildren();
    el.hidden = pages <= 1;
    if (pages <= 1) return;
    for (let p = 0; p < pages; p++) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = String(p + 1);
      b.className = p === _lbPage ? 'active' : '';
      b.addEventListener('click', () => { _lbPage = p; renderLeaderboard(_evoAlphas, _lbSort); });
      el.appendChild(b);
    }
    const n = document.createElement('span');
    n.className = 'micro';
    n.textContent = `${Math.min(total, pages * LB_PAGE)} / ${total}건`;
    el.appendChild(n);
  }

  // ═══════════════════════════════════════════════════════════════════════
  // 알파 상세 — 리더보드 행 클릭. 원본은 이미 /api/recent_alphas 에 다 실려 있어
  // (SELECT * → id·code·pass_items·fail_items·metrics) 추가 조회가 필요 없다.
  // ═══════════════════════════════════════════════════════════════════════

  const itemName = (x) => (typeof x === 'string' ? x : (x && (x.name || x.title)) || String(x));

  // 체크 이름 → 실측/컷오프. LOW_/HIGH_/IS_ 접두를 떼고 metrics 키에 맞춰 본다
  // (LOW_SUB_UNIVERSE_SHARPE → sub_universe_sharpe[_check] / _cutoff). 없으면 배지만.
  function checkValues(a, name) {
    const m = a.metrics || {};
    const k = String(name || '').replace(/^(LOW|HIGH|IS)_/, '').toLowerCase();
    return {
      v: m[k] != null ? m[k] : m[k + '_check'],
      c: m[k + '_cutoff'] != null ? m[k + '_cutoff'] : m[k + '_check_cutoff'],
    };
  }

  let _adAlpha = null;

  function openAlphaDetail(a) {
    const dlg = $('#alpha-dlg');
    const body = $('#ad-body');
    if (!dlg || !body) return;
    _adAlpha = a;
    const o = parseOrigin(a.desc);
    const wid = (a.metrics || {}).wqb_alpha_id || '';
    setText('#ad-title', `R${a.round_num || a.round || 0}${a.phase ? '·f' + a.phase : ''} #${a.idx || 0}`
                        + `  ·  ${ORIGIN_LABEL[o.op]}${o.gen ? ' g' + o.gen : ''}`);
    body.replaceChildren();

    const dl = document.createElement('dl');
    dl.className = 'ad-meta';
    const meta = [
      ['알파 ID', String(a.id ?? '—')],
      ['WQB ID', wid || '— (시뮬 실패/캐시)'],
      ['부모', a.parent_alpha_id ? `#${a.parent_alpha_id}` : '— (신규 계보)'],
      ['세대', String(a.generation ?? 0)],
      ['설정', `${a.universe || '—'} × ${a.neutralization || '—'}`
               + (a.decay != null ? ` · decay ${a.decay}` : '')
               + (a.delay != null ? ` · D${a.delay}` : '')],
      ['상태', a.submitted ? '제출됨' : (a.submit_status || (a.cached ? '캐시' : '미제출'))],
    ];
    for (const [k, v] of meta) {
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      dl.append(dt, dd);
    }
    body.appendChild(dl);

    const sh = sharpeOf(a), fi = fitnessOf(a), tv = turnoverOf(a), cv = selfCorrOf(a);
    const stat = document.createElement('div');
    stat.className = 'ad-stats';
    for (const [k, v] of [['Sharpe', sh != null ? sh.toFixed(2) : '—'],
                          ['Fitness', fi != null ? fi.toFixed(2) : '—'],
                          ['Turnover', tv != null ? (tv * 100).toFixed(1) + '%' : '—'],
                          ['Self-corr', cv != null ? cv.toFixed(2) : '—']]) {
      const s = document.createElement('span');
      const lab = document.createElement('em'); lab.textContent = k;
      const val = document.createElement('b'); val.textContent = v;
      s.append(lab, val);
      stat.appendChild(s);
    }
    body.appendChild(stat);

    const codeH = document.createElement('h4'); codeH.textContent = '알파 식';
    const pre = document.createElement('pre'); pre.className = 'ad-code'; pre.textContent = a.code || '—';
    body.append(codeH, pre);

    for (const [title, items, cls] of [['PASS', a.pass_items || [], 'pass'],
                                       ['FAIL', a.fail_items || [], 'fail']]) {
      const h = document.createElement('h4');
      h.textContent = `${title} (${items.length})`;
      const box = document.createElement('div');
      box.className = 'ad-checks';
      if (!items.length) {
        const e = document.createElement('span'); e.className = 'micro'; e.textContent = '— 없음';
        box.appendChild(e);
      }
      for (const it of items) {
        const name = itemName(it);
        const { v, c } = checkValues(a, name);
        const chip = document.createElement('span');
        chip.className = 'ad-chip ' + cls;
        chip.textContent = name + (v != null ? ` ${v}` : '') + (c != null ? ` / 컷 ${c}` : '');
        box.appendChild(chip);
      }
      body.append(h, box);
    }

    if (a.error_text) {
      const h = document.createElement('h4'); h.textContent = '오류';
      const p = document.createElement('pre'); p.className = 'ad-code err'; p.textContent = a.error_text;
      body.append(h, p);
    }

    const btn = $('#ad-queue');
    if (btn) {
      btn.disabled = !wid;
      btn.textContent = '제출 대기 목록에 추가';
    }
    setText('#ad-msg', wid ? '' : 'WQB alpha id 가 없어 제출할 수 없습니다.');
    dlg.showModal();
  }

  (function initAlphaDialog() {
    const dlg = $('#alpha-dlg');
    if (!dlg) return;
    const close = $('#ad-close');
    if (close) close.addEventListener('click', () => dlg.close());
    // 백드롭 클릭으로 닫기 — <dialog> 는 backdrop 클릭도 dialog 를 타깃으로 준다.
    dlg.addEventListener('click', (ev) => { if (ev.target === dlg) dlg.close(); });
    const btn = $('#ad-queue');
    if (btn) {
      btn.addEventListener('click', async () => {
        if (!_adAlpha || !_adAlpha.id) return;
        btn.disabled = true;
        setText('#ad-msg', '추가 중…');
        const r = await api('/api/submit_queue/add', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ alpha_pk: Number(_adAlpha.id) }),
        });
        if (r.ok && r.data && r.data.ok) {
          setText('#ad-msg', r.data.added ? '대기 목록에 추가했습니다.'
                                          : `이미 대기 목록에 있습니다 (${r.data.status}).`);
          try { refreshSubmitQueue(); } catch (_) {}
        } else {
          setText('#ad-msg', (r.data && r.data.detail) || '추가에 실패했습니다.');
          btn.disabled = false;
        }
      });
    }
  })();

  // 프리플라이트/리페어 계측 — #2/#4 가 status.ga 에 실어주면 표시(없으면 빈 문자열).
  function renderPreflightStat(s) {
    const el = $('#preflight-stat');
    if (!el) return;
    const ga = (s && s.ga) || {};
    const parts = [];
    if (ga.presim_dropped != null) parts.push(`프리플라이트 컷 ${ga.presim_dropped}`);
    if (ga.repaired != null) parts.push(`리페어 ${ga.repaired}`);
    if (ga.requeued != null) parts.push(`재큐 ${ga.requeued}`);
    el.textContent = parts.join(' · ');
  }

  // 산점도 툴팁 (nearest-dot)
  (function initScatterTooltip() {
    const wrap = $('#scatter-wrap');
    const tip = $('#scatter-tip');
    const svg = $('#scatter-chart');
    if (!wrap || !tip || !svg) return;
    wrap.addEventListener('mousemove', (ev) => {
      if (!_scatterDots.length) { tip.style.display = 'none'; return; }
      const rect = svg.getBoundingClientRect();
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      let best = null, bd = 16 * 16;
      for (const d of _scatterDots) { const dx = d.x - mx, dy = d.y - my, dist = dx * dx + dy * dy; if (dist < bd) { bd = dist; best = d; } }
      if (!best) { tip.style.display = 'none'; return; }
      tip.replaceChildren();
      const h = document.createElement('div'); h.className = 'tt-head';
      h.textContent = `${ORIGIN_LABEL[best.o.op]}${best.o.gen ? ' g' + best.o.gen : ''}${best.front ? ' · ◆파레토' : ''}`;
      tip.appendChild(h);
      const r1 = document.createElement('div'); r1.className = 'tt-row';
      r1.textContent = best.text;
      tip.appendChild(r1);
      const g = document.createElement('div'); g.className = 'tt-genes'; g.textContent = best.a.desc || best.a.code || ''; tip.appendChild(g);
      const wrapRect = wrap.getBoundingClientRect();
      tip.style.display = 'block';
      const tw = tip.offsetWidth || 200;
      tip.style.left = Math.max(4, Math.min(best.x + (rect.left - wrapRect.left) + 12, wrapRect.width - tw - 8)) + 'px';
      tip.style.top = Math.max(4, best.y + (rect.top - wrapRect.top) - tip.offsetHeight - 10) + 'px';
    });
    wrap.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
  })();

  // ── 차트 컨트롤 ──
  (function initEvoControls() {
    const seg = $('#evo-metric');
    if (seg) {
      seg.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button[data-metric]');
        if (!btn) return;
        _evoMetric = btn.dataset.metric;
        seg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        renderEvolution();
      });
    }
    const range = $('#evo-range');
    if (range) {
      range.addEventListener('change', () => {
        _evoLimit = Number(range.value) || 400;
        refreshEvolution();
      });
    }
    const axes = $('#scatter-axes');
    if (axes) {
      axes.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button[data-pair]');
        if (!btn) return;
        _scatterPair = btn.dataset.pair;
        axes.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        try { renderScatter(_evoAlphas); } catch (e) { console.error('renderScatter', e); }
      });
    }
    document.querySelectorAll('#lb-table input[data-lbf]').forEach((el) => {
      el.addEventListener('input', () => { _lbPage = 0; renderLeaderboard(_evoAlphas, _lbSort); });
    });
    const lbSeg = $('#lb-sort');
    if (lbSeg) {
      lbSeg.addEventListener('click', (ev) => {
        const btn = ev.target.closest('button[data-sort]');
        if (!btn) return;
        _lbSort = btn.dataset.sort;
        _lbPage = 0;
        lbSeg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        renderLeaderboard(_evoAlphas, _lbSort);
      });
    }
    let resizeT = null;
    window.addEventListener('resize', () => {
      if (resizeT) clearTimeout(resizeT);
      resizeT = setTimeout(renderEvolution, 200);
    });
  })();

  // ── Persona 배너 ─────────────────────────────────────────
  const _personaBanner = document.getElementById('persona-banner');
  const _personaLink   = document.getElementById('persona-link');
  const _personaStatus = document.getElementById('persona-status');
  const _btnPersonaComplete = document.getElementById('btn-persona-complete');
  const _btnPersonaRenew = document.getElementById('btn-persona-renew');

  let _personaInquiry = '';

  async function checkPersonaStatus() {
    try {
      const r = await api('/api/account/wqb-persona-status');
      const d = r && r.data;
      if (d && d.rate_limited) {
        // 429 throttle: ⚠ 새로고침하면 재인증 POST 가 나가 throttle 가 재무장된다.
        // 절대 "새로고침"을 권하지 말 것. 가만히 기다리면 풀린다.
        if (_personaStatus) _personaStatus.textContent =
          'WQB 인증 throttle — 새로고침하지 마세요. 잠시 기다리면 자동 해제됩니다.';
        if (_btnPersonaComplete) _btnPersonaComplete.disabled = true;
        if (_personaLink) _personaLink.setAttribute('aria-disabled', 'true');
        if (_personaBanner) _personaBanner.removeAttribute('hidden');
      } else if (d && d.persona_required) {
        // 링크는 미리 받지 않는다 — 발급이 inquiry 를 재개시켜 열려 있는 인증 페이지를
        // 죽인다. 사용자가 누르는 순간 /wqb-persona-link 로 받는다.
        _personaInquiry = d.inquiry || '';
        if (_personaLink) _personaLink.removeAttribute('aria-disabled');
        // authenticated=true 면 만료 전 선인증 창(만료 30분 전 알림) — 지금 인증하면 끊김이 없다.
        if (_personaStatus) _personaStatus.textContent = d.authenticated
          ? '세션이 아직 유효합니다 — 지금 인증해 두면 만료 시 끊김 없이 이어집니다.'
          : '';
        if (_btnPersonaComplete) _btnPersonaComplete.disabled = false;
        if (_personaBanner) _personaBanner.removeAttribute('hidden');
      } else {
        _personaInquiry = '';
        if (_personaBanner) _personaBanner.setAttribute('hidden', '');
      }
      // RC 전환 버튼: 이미 research_consultant 이면 숨김.
      if (_btnUpRc) {
        _btnUpRc.hidden = (d && d.account_type === 'research_consultant');
      }
    } catch (e) {
      console.error('checkPersonaStatus 실패', e);
    }
  }

  // 인증 링크는 **누르는 그 순간** 발급받는다(미리 받아두면 inquiry 재개로 죽는다).
  // force=true — 저장된 challenge 를 버리고 새로 발급. 열어 둔 인증 페이지가
  // 'session expired' 로 죽었을 때의 유일한 탈출구다(직전 링크는 무효가 된다).
  async function requestPersonaLink(force) {
    if (_personaLink) _personaLink.setAttribute('aria-disabled', 'true');
    if (_btnPersonaRenew) _btnPersonaRenew.disabled = true;
    if (_personaStatus) _personaStatus.textContent =
      force ? '새 인증 링크 발급 중… (이전 링크는 무효가 됩니다)' : '인증 링크 발급 중…';
    let d = {};
    try {
      const r = await api('/api/account/wqb-persona-link', {
        method: 'POST',
        body: JSON.stringify(force ? { force: true } : {}),
      });
      d = (r && r.data) || {};
    } catch (e) {
      console.error('persona-link 실패', e);
    }
    if (_personaLink) _personaLink.removeAttribute('aria-disabled');
    if (_btnPersonaRenew) _btnPersonaRenew.disabled = false;
    if (d.authenticated) {
      if (_personaStatus) _personaStatus.textContent = '이미 인증되어 있습니다.';
      if (_personaBanner) _personaBanner.setAttribute('hidden', '');
      return;
    }
    const url = d.persona_url || '';
    if (!url.includes('withpersona.com')) {
      if (_personaStatus) _personaStatus.textContent =
        d.detail || '링크를 준비 중입니다 — 잠시 후 다시 눌러주세요.';
      return;
    }
    if (d.inquiry) _personaInquiry = d.inquiry;
    if (_personaStatus) _personaStatus.textContent =
      (force ? '새 인증 링크를 열었습니다. ' : '') + '인증을 마치면 자동으로 저장하고 창을 닫습니다.';
    // noopener 를 주면 window.open 이 null 을 돌려줘 창을 닫아 줄 수 없다 —
    // 인증이 끝나면 우리가 닫아야 하므로 핸들을 받는다.
    const w = window.open(url, 'wqb_persona');
    if (!w) { location.href = url; return; }
    watchPersonaCompletion(w);
  }

  // 인증 완료 자동 감지 (2026-07-29 사장 지시) — 창 안은 cross-origin 이라 못 읽는다.
  // 대신 complete 엔드포인트를 주기적으로 두드린다: 미완료면 WQB 가 403 을 주고
  // 아무 일도 일어나지 않으며(부작용 없음), 완료되는 순간 세션이 저장된다.
  let _personaWatch = null;
  function watchPersonaCompletion(win) {
    if (_personaWatch) clearInterval(_personaWatch);
    const deadline = Date.now() + 15 * 60 * 1000;
    _personaWatch = setInterval(async () => {
      if (Date.now() > deadline || (win && win.closed)) {
        clearInterval(_personaWatch); _personaWatch = null;
        return;
      }
      let ok = false;
      try {
        const r = await api('/api/account/wqb-persona-complete', {
          method: 'POST', body: JSON.stringify({ inquiry: _personaInquiry }),
        });
        ok = !!(r.ok && r.data && r.data.ok);
      } catch (e) { /* 네트워크 흔들림 — 다음 턴에 다시 */ }
      if (!ok) return;
      clearInterval(_personaWatch); _personaWatch = null;
      try { if (win && !win.closed) win.close(); } catch (e) {}
      if (_personaStatus) _personaStatus.textContent = '인증 완료 — 세션 저장됨 (자동)';
      if (_personaBanner) _personaBanner.setAttribute('hidden', '');
    }, 8000);
  }

  if (_personaLink) {
    _personaLink.addEventListener('click', (ev) => {
      ev.preventDefault();   // href 는 항상 '#' — 링크는 지금 발급받는다.
      if (_personaLink.getAttribute('aria-disabled') === 'true') return;
      requestPersonaLink(false);
    });
  }

  if (_btnPersonaRenew) {
    _btnPersonaRenew.addEventListener('click', () => {
      if (_btnPersonaRenew.disabled) return;
      if (!confirm('새 인증 링크를 발급합니다.\n이미 열어 둔 인증 페이지는 무효가 됩니다. 계속할까요?')) return;
      requestPersonaLink(true);
    });
  }

  // 배너가 숨겨진 상태(세션이 살아 있다고 표시되지만 실제로는 인증이 필요한 경우)에서도
  // 사장이 스스로 재인증에 들어갈 수 있도록 하는 상시 진입점. 여기서는 배너만 펼친다
  // — WQB 로 나가는 호출은 사용자가 배너 안의 버튼을 누를 때만 발생한다.
  const _btnPersonaOpen = document.getElementById('btn-persona-open');
  if (_btnPersonaOpen) {
    _btnPersonaOpen.addEventListener('click', () => {
      if (!_personaBanner) return;
      _personaBanner.removeAttribute('hidden');
      if (_btnPersonaComplete) _btnPersonaComplete.disabled = false;
      if (_personaLink) _personaLink.removeAttribute('aria-disabled');
      if (_personaStatus && !_personaStatus.textContent) _personaStatus.textContent =
        '인증 페이지를 열어 완료한 뒤 세션을 저장하세요. 링크가 만료됐으면 재발급을 누르세요.';
      _personaBanner.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  }

  if (_btnPersonaComplete) {
    _btnPersonaComplete.addEventListener('click', async () => {
      _btnPersonaComplete.disabled = true;
      if (_personaStatus) _personaStatus.textContent = '저장 중…';
      try {
        const r = await api('/api/account/wqb-persona-complete', {
          method: 'POST',
          body: JSON.stringify({ inquiry: _personaInquiry }),
        });
        if (r.ok && r.data && r.data.ok) {
          if (_personaStatus) _personaStatus.textContent = '인증 완료 — 세션 저장됨';
          if (_personaBanner) _personaBanner.setAttribute('hidden', '');
        } else {
          const msg = r.data && r.data.ok === false
            ? 'biometric 인증이 아직 완료되지 않았습니다 — 브라우저에서 완료 후 다시 눌러주세요.'
            : ((r.data && (r.data.detail || r.data.reason)) || '알 수 없는 오류');
          if (_personaStatus) _personaStatus.textContent = '오류: ' + msg;
          _btnPersonaComplete.disabled = false;
        }
      } catch (e) {
        if (_personaStatus) _personaStatus.textContent = '네트워크 오류: ' + (e && e.message ? e.message : e);
        _btnPersonaComplete.disabled = false;
      }
    });
  }

  // ── RC 전환 버튼 ─────────────────────────────────────────
  const _btnUpRc = document.getElementById('btn-upgrade-rc');
  if (_btnUpRc) _btnUpRc.addEventListener('click', async () => {
    try {
      const r = await api('/api/account/upgrade-to-rc', { method: 'POST' });
      const ok = r && r.data && r.data.ok;
      alert(ok ? 'Research Consultant로 전환되었습니다.'
               : ('전환 실패: ' + ((r && r.data && (r.data.detail || r.data.reason)) || '오류')));
    } catch (e) {
      alert('전환 실패: ' + (e && e.message ? e.message : e));
    }
  });

  // ── 부팅 ─────────────────────────────────────────────────
  tryAutoLogin();
})();
