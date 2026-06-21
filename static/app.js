/* HYFE_IQC 프론트엔드 — 단일 SPA. fetch 기반 API + EventSource SSE. */

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
  const btnClearLog = $('#btn-clear-log');
  const autoscrollEl = $('#autoscroll');
  const stateText = $('#state-text');
  const stateRound = $('#state-round');
  const stateCompleted = $('#state-completed');
  const stateErrors = $('#state-errors');

  let evtSource = null;
  let lastLogId = 0;
  let statusTimer = null;
  let bestTimer = null;

  // ── 화면 전환 ────────────────────────────────────────────
  function showScreen(name) {
    Object.entries(screens).forEach(([k, el]) => {
      if (k === name) el.removeAttribute('hidden');
      else el.setAttribute('hidden', '');
    });
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

  // 검증 성공 시 보관할 사용자 정보 — 버튼 클릭 시 onLoggedIn 에 전달.
  let _pendingMe = null;
  const btnGoDashboard = $('#btn-go-dashboard');

  $('#form-login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const wqb_username = $('#wqb_username').value.trim();
    const wqb_password = $('#wqb_password').value;
    const gemini_api_key = $('#gemini_api_key').value.trim();
    const remember = !!($('#remember_device') && $('#remember_device').checked);
    const btn = $('#btn-login');
    btn.disabled = true;
    btn.textContent = '검증 중... (최대 90초)';
    setStatus('info', 'WQB + Gemini 검증 중. 신규 로그인은 30~60초 걸릴 수 있습니다.');
    btnGoDashboard.setAttribute('hidden', '');
    try {
      const account_type = (document.querySelector('input[name="account_type"]:checked') || {}).value || 'standard';
      const r = await api('/api/login', {
        method: 'POST',
        body: JSON.stringify({ wqb_username, wqb_password, gemini_api_key, remember, account_type }),
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
        setStatus('error', `[${reason}] ${reasonLabel(reason)} — ${detail}`);
      }
    } catch (err) {
      setStatus('error', '네트워크 오류: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '로그인 검증 시작';
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
      gemini_invalid: 'Gemini API 키가 잘못되었습니다',
      gemini_quota: 'Gemini API 키 쿼터 초과 (다른 키를 시도하세요)',
      gemini_network: 'Gemini API 호출 자체 실패 (네트워크/SDK)',
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

    // SSE 시작 전 status 한 번 받아 비우기 지점 확인 → 그 이후 로그를 backlog 으로
    // 한꺼번에 그린 다음 SSE 를 잇는다. 사용자가 화면 비우기를 누른 적 없으면 0 부터
    // (= 본인 계정의 모든 로그) replay — 새로고침 / 재접속해도 누적 유지.
    try {
      const r0 = await api('/api/status');
      if (r0.ok && r0.data && r0.data.ok) {
        const startId = Number(r0.data.last_cleared_log_id || 0);
        lastLogId = startId;
        applyStatus(r0.data);
        try { await replayBacklog(startId); }
        catch (e) { console.error('backlog replay', e); }
      }
    } catch (e) { console.error('initial status fetch', e); }

    // 폴링/스트림은 각각 독립적으로 try — 하나가 실패해도 나머지는 살린다.
    try { startStreaming(); } catch (e) { console.error('startStreaming init', e); }
    try { refreshBest(); } catch (e) { console.error('refreshBest init', e); }
    try { initDelayMode(); } catch (e) { console.error('initDelayMode', e); }
    if (statusTimer) clearInterval(statusTimer);
    if (bestTimer) clearInterval(bestTimer);
    statusTimer = setInterval(refreshStatus, 5000);
    bestTimer = setInterval(refreshBest, 30000);
  }

  // ── 로그아웃 ─────────────────────────────────────────────
  btnLogout.addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST' });
    stopStreaming();
    if (statusTimer) clearInterval(statusTimer);
    if (bestTimer) clearInterval(bestTimer);
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

  // ── Delay 테스트 모드 ────────────────────────────────────
  const delaySel = $('#delay-mode');
  const delayStatus = $('#delay-mode-status');
  const DELAY_LABELS = { '0': 'Delay=0 만', '1': 'Delay=1 만', 'mix': '혼합(랜덤)' };
  async function initDelayMode() {
    if (!delaySel) return;
    const r = await api('/api/delay_mode');
    if (r.ok && r.data && r.data.ok) {
      delaySel.value = r.data.mode;
      if (delayStatus) delayStatus.textContent = '현재: ' + (DELAY_LABELS[r.data.mode] || r.data.mode);
    }
    delaySel.addEventListener('change', async () => {
      const mode = delaySel.value;
      if (delayStatus) delayStatus.textContent = '저장 중…';
      const res = await api('/api/delay_mode', {
        method: 'POST', body: JSON.stringify({ mode }),
      });
      if (res.ok && res.data && res.data.ok) {
        if (delayStatus) delayStatus.textContent = '저장됨: ' + (DELAY_LABELS[res.data.mode] || res.data.mode) + ' (다음 라운드부터)';
      } else {
        if (delayStatus) delayStatus.textContent = '저장 실패';
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
  }

  // ── 로그 backlog ─────────────────────────────────────────
  // 비우기 지점 이후 로그를 페이지로 끊어 가져와 화면에 추가한다.
  // (한 번의 GET 응답이 너무 커지지 않게 limit=500 페이지네이션.)
  async function replayBacklog(sinceId) {
    let cursor = Number(sinceId || 0);
    let safety = 60; // 최대 30,000 줄까지 — 그 이상은 사용자가 비우면 됨.
    while (safety-- > 0) {
      const r = await api('/api/logs?since=' + encodeURIComponent(cursor) + '&limit=500');
      if (!(r.ok && r.data && r.data.ok)) break;
      const rows = r.data.logs || [];
      if (rows.length === 0) break;
      for (const row of rows) {
        appendLog(row);
        if (row.id > lastLogId) lastLogId = row.id;
      }
      cursor = rows[rows.length - 1].id;
      if (rows.length < 500) break;
    }
  }

  // ── 로그 스트림 ──────────────────────────────────────────
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
        appendLog(row);
        if (row.id > lastLogId) lastLogId = row.id;
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

  function appendLog(row) {
    const line = document.createElement('div');
    const cls = ['log-line', classifyLine(row.line || '', row.level || '')];
    if (row.level === 'round_end' || row.level === 'round_start') {
      // 라운드 끝/시작 줄은 6 컬러팔레트로 round_num 별 다른 색 — ArcAI Daily IQC 스타일.
      cls.push('lvl-round-end');
      cls.push('round-color-' + (Math.abs(Number(row.round_num) || 0) % 6));
    }
    line.className = cls.filter(Boolean).join(' ');
    const t = row.ts ? new Date(row.ts * 1000).toLocaleTimeString('ko-KR', { hour12: false }) : '';
    line.innerHTML =
      `<span class="ts">[${escapeHtml(t)}]</span>` +
      `<span class="rn">R${row.round_num || 0}</span>` +
      escapeHtml(row.line || '');
    logPane.appendChild(line);
    if (autoscrollEl.checked) logPane.scrollTop = logPane.scrollHeight;
    while (logPane.childElementCount > 5000) {
      logPane.removeChild(logPane.firstChild);
    }
  }

  function classifyLine(text, level) {
    const lvl = (level || '').toLowerCase();
    if (lvl === 'pass') return 'lvl-pass';
    if (lvl === 'warn') return 'lvl-warn';
    if (lvl === 'error') return 'lvl-error';
    const t = text || '';
    if (/🏆|best 발견/.test(t)) return 'lvl-pass';
    if (/⚠|🛑|ERROR|error|예외|실패|fail/i.test(t)) return 'lvl-warn';
    return '';
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  btnClearLog.addEventListener('click', async () => {
    // 화면만 지움 — 서버에 저장된 로그/오류/알파 데이터는 건드리지 않는다.
    // 다만 사용자별 "비우기 지점"을 latest 로 이동해 둬서, 재접속/새로고침 시
    // 비우기 이전 로그가 다시 그려지지 않게 한다.
    logPane.innerHTML = '';
    try {
      const r = await api('/api/logs/clear', { method: 'POST' });
      if (r.ok && r.data && r.data.ok) {
        lastLogId = Number(r.data.last_cleared_log_id || lastLogId);
        stopStreaming();
        startStreaming();
      }
    } catch (_) {}
  });

  // ── 알파 제출 시도 표 (모바일 /api/m_submits 와 동일 소스·비우기) ────────
  function fmtTime(ts) {
    if (!ts) return '—';
    try {
      // 뷰어/서버 타임존 무관하게 항상 KST(Asia/Seoul) 로 표기.
      return new Date(ts * 1000).toLocaleTimeString('en-GB', {
        timeZone: 'Asia/Seoul', hour12: false,
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
    } catch (_) {
      const d = new Date((ts + 9 * 3600) * 1000);  // UTC+9 수동 폴백
      const z = (n) => String(n).padStart(2, '0');
      return `${z(d.getUTCHours())}:${z(d.getUTCMinutes())}:${z(d.getUTCSeconds())}`;
    }
  }
  // XSS 안전: innerHTML 미사용 — 동적 값은 textContent/.title 로만.
  function bestCell(text, title) {
    const td = document.createElement('td');
    td.textContent = (text == null ? '' : String(text));
    if (title) td.title = String(title);
    return td;
  }
  function bestBadge(submitted, ss) {
    const span = document.createElement('span');
    let cls = 'unsubmitted', label = '— 미추가', title = ss || '리스트 추가 시도';
    if (submitted) {                                   // 'Submit' 리스트 추가 성공
      cls = 'submitted'; label = '✅ 추가'; title = ss || 'Submit 리스트 추가';
      const m = ss.match(/0?\.\d+/);
      if (m && /corr/i.test(ss)) label = '✅ 추가 · corr ' + m[0];
    } else if (ss.startsWith('skip_star')) {           // all-pass 아님 / corr>0.7 → 미추가
      cls = 'unsubmitted'; label = '— 미추가';
      const m = ss.match(/0?\.\d+/);
      if (m && /corr/i.test(ss)) label = '— 미추가 · corr ' + m[0];
      title = ss.slice(0, 90);
    } else if (ss.startsWith('star_fail')) {           // 리스트 추가 실패
      cls = 'unsubmitted'; label = '⚠ 추가실패'; title = ss;
    } else if (ss.startsWith('rejected:')) {           // (구버전 제출 기록 호환)
      cls = 'unsubmitted'; label = '✗ 거절';
      const body = ss.slice('rejected:'.length).trim();
      const m = body.match(/\d+\.\d+/);
      if (m && /correlation/i.test(body)) label = '✗ 거절 · corr ' + m[0];
      title = body.slice(0, 80);
    } else if (ss.startsWith('fail:')) { cls = 'unsubmitted'; label = '⚠ 실패'; title = ss; }
    else if (ss === 'disabled') { cls = 'unsubmitted'; label = '⛔ 비활성'; title = ss; }
    else if (ss === 'not_found') { cls = 'unsubmitted'; label = '⚠ 못찾음'; title = ss; }
    span.className = 'status-badge ' + cls;
    span.textContent = label;
    span.title = title;
    return span;
  }
  async function refreshBest() {
    const r = await api('/api/m_submits?limit=50');
    if (!(r.ok && r.data && r.data.ok)) return;
    const tbody = $('#best-table tbody');
    const empty = $('#best-empty');
    tbody.replaceChildren();
    const rows = r.data.attempts || [];
    if (rows.length === 0) {
      empty.style.display = '';
      $('#best-table').style.display = 'none';
      return;
    }
    empty.style.display = 'none';
    $('#best-table').style.display = '';
    for (const a of rows) {
      const tr = document.createElement('tr');
      tr.appendChild(bestCell(a.round_num));
      tr.appendChild(bestCell(a.idx));
      tr.appendChild(bestCell(a.pass_count || 0));
      tr.appendChild(bestCell(a.fail_count || 0));
      const bcell = document.createElement('td');
      bcell.appendChild(bestBadge(!!a.submitted, a.submit_status || ''));
      tr.appendChild(bcell);
      tr.appendChild(bestCell(fmtTime(a.ts)));
      tr.appendChild(bestCell((a.code || '').slice(0, 120), a.code || ''));
      tbody.appendChild(tr);
    }
  }

  const btnClearSubmits = $('#btn-clear-submits');
  if (btnClearSubmits) {
    btnClearSubmits.addEventListener('click', async () => {
      btnClearSubmits.disabled = true;
      try {
        await api('/api/m_submits/clear', { method: 'POST' });
      } catch (_) {}
      btnClearSubmits.disabled = false;
      try { refreshBest(); } catch (_) {}
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
