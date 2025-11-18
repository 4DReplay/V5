
/*
 * oms-actions.js — Unified with chipMsg(scope, mode, text)
 *
 * chipMsg API:
 *
 * Scope: 1=system, 2=camera
 * Mode : 1=restart, 2=connect
 */

(function (global) {
  'use strict';

  // ---- Core prefix helpers ----
  const CHIP_KEYS = { system: 'oms_sys_progress', camera: 'oms_cam_progress' };
  const CHIP_CHS = { system: 'oms-progress-system', camera: 'oms-progress-camera' };
  const PROG_LS_KEY = 'oms_ra_progress'; // dashboard / oms-system 이 보고 있는 기존 키

  // -------------------------------------------------------------------
  // CONNECT 결과를 전역 공유하는 Summary 저장소 (system/dashboard 모두 사용)
  // -------------------------------------------------------------------
  global.OMS = global.OMS || {};
  global.OMS.ConnectSummary = global.OMS.ConnectSummary || {
    connected: {},   // { EMd:true, SCd:true ... }
    lastUpdate: 0
  };
  
  // -------------------------------------------------------------------
  // ---- Global Message (chipMsg)
  // -------------------------------------------------------------------
  function _scopeName(scope) { return (scope === 2 || scope === 'camera') ? 'camera' : 'system'; }
  function _modeName(mode) { return (mode === 2 || mode === 'connect') ? 'connect' : 'restart'; }
  function _prefix(scope, mode) { return `[${_scopeName(scope)}][${_modeName(mode)}]`; }
  function _prefixed(scope, mode, text) {
    const p = _prefix(scope, mode);
    const body = String(text ?? '');
    if (body.startsWith(p + ' ') || body === p) return body;
    return `${p} ${body}`.trim();
  }
  function _bc(name) { try { return new BroadcastChannel(name); } catch { return null; } }
  function _paintChip(text) {
    const el = document.getElementById('progressChip');
    if (!el) return;
    const msg = (text && String(text).trim()) || (el.textContent.trim() || 'Working…');
    el.style.visibility = 'visible';
    if (el.textContent !== msg) el.textContent = msg;
  }
  function _emit(scopeName, text, priority = 1) {
    const payload = {
      scope: scopeName,
      text: String(text || ''),
      prio: Number(priority) || 1,
      ts: Date.now(),
      origin: (window.__OMS_ORIGIN__ ||= Math.random().toString(36).slice(2)),
      seq: (window.__OMS_SEQ__ = (window.__OMS_SEQ__ | 0) + 1)
    };
    // debug
    console.log("broadcast message:",text)
    // 1) system/camera 전용 키
    try {
      localStorage.setItem(CHIP_KEYS[scopeName] || CHIP_KEYS.system,
        JSON.stringify(payload));
    } catch { }

    // 2) dashboard / oms-system 이 보고 있는 공통 키
    try {
      localStorage.setItem(PROG_LS_KEY, payload.text);
    } catch { }

    // 3) system/camera 전용 BroadcastChannel (loop 없음)
    try {
      const bc = _bc(CHIP_CHS[scopeName] || CHIP_CHS.system);
      if (bc) bc.postMessage(payload);
    } catch { }

    // system restart 관련 메시지라면, Lock 상태도 함께 갱신
    const scopeStr = String(scopeName);
    if (scopeStr === 'system' || scopeStr === '1') {
      updateRestartLockFromMessage(payload.text);
    }
    return payload;
  }
  function chipMsg(scope, mode, text, priority = 1) {
    const sname = _scopeName(scope);
    const msg = _prefixed(sname, mode, text);
    // debug          
    console.log("chipMsg:", text)
    _paintChip(msg);    
    return _emit(sname, msg, priority);
  }
  // Expose globally
  global.chipMsg = chipMsg;

  // ========================================================================
  // Actions / API helpers
  // ========================================================================
  const W = global;
  W.OMS = W.OMS || {};
  const NS = (W.OMS.Actions = W.OMS.Actions || {});
  const EXPLICIT_PREFIX = (typeof window !== 'undefined' && (window.__OMS_API_PREFIX__ || '')) || '';
  const PROXY_PREFIX = (() => {
    try { const m = location.pathname.match(/^\/proxy\/([^\/]+)/); return m ? `/proxy/${encodeURIComponent(decodeURIComponent(m[1]))}` : ''; }
    catch { return ''; }
  })();
  const API_BASE = EXPLICIT_PREFIX || PROXY_PREFIX || '';
  async function api(path, init = {}) {
    const url = (typeof path === 'string' && path.startsWith('/')) ? (API_BASE + path) : path;
    const { timeoutMs, ...rest } = init || {};
    const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    const id = (ctrl && timeoutMs && Number.isFinite(timeoutMs)) ? setTimeout(() => { try { ctrl.abort(); } catch { } }, timeoutMs) : null;
    try {
      const res = await fetch(url, { cache: 'no-store', ...(rest || {}), ...(ctrl ? { signal: ctrl.signal } : {}) });
      const ct = res.headers.get('content-type') || '';
      const body = ct.includes('application/json') ? await res.json().catch(() => ({})) : await res.text().catch(() => '');
      if (!res.ok) { const msg = (body && (body.error || body.message)) || `HTTP ${res.status}`; const e = new Error(msg); e.status = res.status; e.url = String(url); e.body = body; throw e; }
      return body;
    } finally { if (id) clearTimeout(id); }
  }
  NS.api = api;

  // ========================================================================
  // Restart monitors
  // ========================================================================
  function updateRestartLockFromMessage(msgRaw) {
    const msg = String(msgRaw || "").trim();
    const lower = msg.toLowerCase();

    // system/restart 메시지가 아니면 무시 (대시보드와 동일 컨셉)
    if (!lower.startsWith("[system][restart]")) {
      return;
    }

    // preparing 이면 Lock ON
    if (lower.includes("preparing")) {
      RA_LOCK = true;
      broadcastLock();
      return;
    }

    // [Finished] / finished / done / complete / Restart-All 요약 메시지면 Lock OFF
    if (
      lower.includes("finished") ||                      // [Finished] 명시
      /\b(finished|done|complete)\b/.test(lower) ||        // 일반 완료 문구
      lower.includes("restart-all done") ||                // 요약 메시지 (성공)
      lower.includes("restart-all failed")                 // 요약 메시지 (실패)
    ) {
      
      chipMsg(1, 1, 'updateRestartLockFromMessage-finished');   // [system][restart] Restarting…
      NS.stopRestartMonitors();
      
      try { stopLive && stopLive(); } catch { }
      return;
    }
  }

  // ========================================================================
  // System connect [oms/sys-connect]
  // ========================================================================
  async function getSwitchIpList() {
    try {
      const res = await fetch("/oms/state", { cache: "no-store" });
      const data = await res.json();

      // cameras 배열에서 SCdIP만 추출
      if (!data.cameras || !Array.isArray(data.cameras)) return [];

      const unique = new Set();

      data.cameras.forEach(cam => {
        if (cam.SCdIP) unique.add(cam.SCdIP);
      });

      return [...unique].map(ip => ({ ip }));
    } catch (e) {
      console.error("Failed to load /oms/state", e);
      return [];
    }
  }
  async function sendSwitchCommand(section3) {
    const switchList = await getSwitchIpList();
    if (switchList.length === 0) {
      chipMsg(2, 1, "No switch detected");
      return;
    }

    const payload = {
      host: "127.0.0.1",
      port: 19765,
      timeout: 15,
      message: {
        Section1: "Switch",
        Section2: "Operation",
        Section3: section3,
        SendState: "request",
        From: "4DDM",
        To: "SCd",
        Action: "run",
        Token: "oms_" + Date.now(),
        Switches: switchList
      }
    };
    chipMsg(2,1,'Send Message to Switch')
    const res = await fetch("/oms/mtd-connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    });

    const data = await res.json();
    console.log("SCd response:", data);

    const results = data.Switches || [];
    const okList = results.filter(x => x.errorMsg === "SUCCESS");
    const failList = results.filter(x => x.errorMsg !== "SUCCESS");

    // chip message
    chipMsg(2, 1, `${section3}:Done (${okList.length}:Success)`);

    // -----------------------------
    // 🆕 SUCCESS 메시지 박스 표시
    // -----------------------------
    let msg = `Command: ${section3}\n\n`;

    if (okList.length > 0) {
      msg += `Success:\n`;
      okList.forEach(sw => {
        msg += ` - ${sw.ip}\n`;
      });
      msg += `\n`;
    }

    if (failList.length > 0) {
      msg += `Failed:\n`;
      failList.forEach(sw => {
        msg += ` - ${sw.ip} (${sw.errorMsg})\n`;
      });
    }
    // 메시지 박스 띄우기
    alert(msg);
    return data;
  }

  NS.mtdConnect = async function (extra = {}) {
    // explicit MTd message passthrough 
    if (extra && typeof extra.mtdMessage === 'object') {
      chipMsg(1, 2, 'MTd message sending…');
      const host = extra.mtd_host || location.hostname;
      const port = Number(extra.mtd_port || 19765);
      const url = (typeof API_BASE === 'string' ? API_BASE : '') + '/oms/mtd-connect';
      const body = { host, port, message: extra.mtdMessage };
      const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), cache: 'no-store' });
      const ct = res.headers.get('content-type') || '';
      const resp = ct.includes('application/json') ? await res.json().catch(() => ({})) : await res.text().catch(() => '');
      if (!res.ok) throw new Error((resp && resp.error) || `HTTP ${res.status}`);
      chipMsg(1, 2, 'MTd message sent');
      return resp;
    }

    chipMsg(1, 2, 'Connect… preparing');

    let dmpdip     = extra?.dmpdip     || '';
    let daemon_map = extra?.daemon_map || undefined;
    let mtd_host   = extra?.mtd_host   || '';
    let mtd_port   = Number(extra?.mtd_port || 19765);
    const dry_run  = !!extra?.dry_run;

    let statusExtra = null;
    let stateSnap   = null;

    // ====================================================
    // 0단계: Process List 에서 DaemonList 선 구축
    // ====================================================
    try {
      const pRes = await api('/oms/process-list');
      const list =
        Array.isArray(pRes?.processes) ? pRes.processes :
        Array.isArray(pRes)            ? pRes :
        [];

      if (!daemon_map || typeof daemon_map !== 'object') {
        daemon_map = {};
      }

      const EXCLUDE = new Set(["MMc", "PreSd", "PostSd", "VPd"]);

      for (const p of list) {
        const name =
          p.name ||
          p.proc_name ||
          p.ProcessName ||
          p.process_name ||
          '';
        const ip =
          p.ip ||
          p.host ||
          p.ipaddr ||
          p.ip_addr ||
          '';

        // 제외대상 제외
        if (!name || !ip) continue;
        if (EXCLUDE.has(name)) continue;

        daemon_map[name] = String(ip).trim();
      }

      // DMPDIP 기본값 처리
      if (!dmpdip) {
        const first = list.find(p =>
          (p.ip || p.host || p.ipaddr || p.ip_addr)
        );
        if (first) {
          dmpdip =
            String(first.ip || first.host || first.ipaddr || first.ip_addr || '')
              .trim();
        }
      }
    } catch (e) {
      // 실패시 기존 fallback 로직으로 진행
    }

    // ====================================================
    // 1차: /oms/status 적용
    // ====================================================
    try {
      const st = await api('/oms/status');
      const ex = st?.extra || {};
      statusExtra = ex;
      // daemon_map 이 이미 Process List에서 생성되었다면 절대 덮어쓰지 않음
      if (!daemon_map || Object.keys(daemon_map).length === 0) {
        daemon_map = ex.daemon_map || daemon_map;
      }
      if (!dmpdip)     dmpdip     = ex.dmpdip     || '';
      if (!mtd_host)   mtd_host   = ex.mtd_host   || '';
      if (!mtd_port && ex.mtd_port) mtd_port = Number(ex.mtd_port);
    } catch { }

    // ====================================================
    // 2차: /oms/state 보완
    // ====================================================
    try {
      const s2 = await api('/oms/state');
      stateSnap = s2;
      if (!daemon_map || Object.keys(daemon_map).length === 0) {
        daemon_map = s2?.daemon_map || s2?.extra?.daemon_map || daemon_map;
      }
      if (!dmpdip)      {dmpdip = s2?.dmpdip || s2?.extra?.dmpdip || dmpdip || '';}
      if (!mtd_host)    {mtd_host = s2?.mtd_host || s2?.extra?.mtd_host || mtd_host || '';}
      if (!mtd_port && s2?.mtd_port) {mtd_port = Number(s2.mtd_port);}
    } catch { }

    // ====================================================
    // EMd 보정 (필수)
    // ====================================================
    if (!daemon_map || typeof daemon_map !== 'object') {
      daemon_map = {};
    }

    if (!daemon_map.EMd || !String(daemon_map.EMd).trim()) {
      const emdFromStatus = statusExtra?.daemon_map?.EMd;
      const emdFromState  =
        stateSnap?.daemon_map?.EMd ||
        stateSnap?.extra?.daemon_map?.EMd;
      const emdFromHost   = mtd_host && String(mtd_host).trim();
      const candidate = (emdFromStatus || emdFromState || emdFromHost || '127.0.0.1');
      daemon_map.EMd = candidate;
    }

    // 기본값 보정
    mtd_host ||= location.hostname;
    dmpdip   ||= (function () {
      const h = (location.hostname || '').trim();
      if (!h || h === 'localhost' || h.startsWith('127.')) return '127.0.0.1';
      return h;
    })();
    if (!Number.isFinite(mtd_port) || mtd_port <= 0) mtd_port = 19765;

    // ====================================================
    // 최종 payload 전송
    // ====================================================
    const payload = {
      dmpdip,
      daemon_map,       // == DaemonList
      mtd_host,
      mtd_port,
      dry_run,
      trace: true,
      return_partial: true
    };

    try {      
      let res = await api('/oms/sys-connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res || res.ok !== true) {
        throw new Error((res && res.error) || 'sys-connect failed');
      }

      // ==========================================================
      // 추가: NOK 처리된 Daemon 만 1회 재시도
      // ==========================================================
      try {
        const dl = res.response?.DaemonList || {};
        const retryMap = {};
        
        for (const [name, info] of Object.entries(dl)) {
          if (info && info.Status === "NOK") {
            retryMap[name] = info.IP || daemon_map[name];
          }
        }

        // 재시도 대상이 있으면 한번만 더 실행
        if (Object.keys(retryMap).length > 0) {
          chipMsg(1, 2, 'Retrying failed daemon(s)…');

          const retryPayload = {
            dmpdip,
            daemon_map: retryMap,
            mtd_host,
            mtd_port,
            dry_run,
            trace: true,
            return_partial: true
          };

          const retryRes = await api('/oms/sys-connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(retryPayload)
          });

          // retry 결과를 res에 병합
          if (retryRes?.response?.DaemonList) {
            for (const [name, info] of Object.entries(retryRes.response.DaemonList)) {
              res.response.DaemonList[name] = info;
            }
          }
        }
        // ---------------------------------------------------------
        // CONNECT Summary 업데이트 (OK인 프로세스만 CONNECTED 기록)
        // ---------------------------------------------------------
        try {
          const dl = res?.response?.DaemonList || {};
          const summary = global.OMS.ConnectSummary;
          summary.lastUpdate = Date.now();
          for (const [name, info] of Object.entries(dl)) {
            if (info?.Status === "OK") {
              summary.connected[name] = true;   // 연결됨
            } else {
              summary.connected[name] = false;  // 연결 실패
            }
          }
          // BroadcastChannel 로 dashboard/system 에 즉시 반영
          const bc = new BroadcastChannel('oms-connect-summary');
          bc.postMessage(summary);
          bc.close();
        } catch (e) {
          console.warn("ConnectSummary update failed:", e);
        }
      } catch (err) {
        console.warn("Retry connect failed:", err);
      }


      chipMsg(1, 2, 'Connect done');
      return res;
    } catch (e) {
      const msg = String(e?.message || e || '');
      chipMsg(1, 2, 'Connect failed: ' + msg);
      throw e;
    }
  };

  NS.connect    = NS.mtdConnect;
  NS.sysConnect = NS.mtdConnect;
  NS.reconnect  = NS.mtdConnect;

  // ========================================================================
  // System Page UI (minimal – hooks retained)
  // ========================================================================
  NS.hooks = { reloadNow: null, render: null };
  NS.mountPage = function ({ hooks } = {}) {
    if (hooks?.reloadNow) NS.hooks.reloadNow = hooks.reloadNow;
    if (hooks?.render) NS.hooks.render = hooks.render;
    const btnSysConnect   = document.getElementById('btnSysConnect');
    const btnSysRestart   = document.getElementById('btnSysRestart');    
    if (btnSysConnect)    btnSysConnect.addEventListener('click', () => NS.sysConnect().catch(e => console.warn(e)));
    if (btnSysRestart)    btnSysRestart.addEventListener('click', () => NS.sysRestart().catch(e => alert('Restart-All failed: ' + (e?.message || e))));    
  };

  // ========================================================================
  // Camera Command
  // ========================================================================
  window.OMS = window.OMS || {};
  OMS.Actions = OMS.Actions || {};
  // 공용 Busy 표시용 헬퍼
  OMS.Actions.setBusy = function (msg) {
    const el = document.getElementById("busy");
    if (el) el.textContent = msg || "";
  };
  
  // ========================================================================
  // 🔹 Connect Cameras 전체 워크플로우 트리거
  // ========================================================================
  OMS.Actions.cameraConnectAll = async function () {
    try {
      OMS.Actions.setBusy("Connecting cameras...");
      console.log("Connecting cameras..")

      const res = await fetch("/oms/cam-connect/all", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
        cache: "no-store",
      });

      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }

      const data = await res.json();

      if (data && data.ok === true) {
        OMS.Actions.setBusy("Cameras connected.");
      } else {

        // 🔥 여기 추가
        // MTd/CCd timeout 또는 카메라 0 → Needs / Check System
        chipMsg(2, 2, "Needs Check System");  
        OMS.Actions.setBusy("Needs Check System");

        console.error("connect-all error:", data);
      }

      if (typeof window.fetchCameraState === "function") {
        window.fetchCameraState();
      }

    } catch (err) {
      console.error("cameraConnectAll failed:", err);

      // 네트워크 오류도 동일하게 처리
      chipMsg(2, 2, "Needs Check System");
      OMS.Actions.setBusy("Needs Check System");
    } finally {
      setTimeout(() => OMS.Actions.setBusy(""), 1500);
    }
  };
  OMS.Actions.cameraRebootAll = async function () {
    chipMsg(2, 1, `Camera Restarting ...`);
    console.log("cameraRebootAll")
    return sendSwitchCommand("Reboot");
  };
  OMS.Actions.cameraStartAll = async function () {
    chipMsg(2, 1, `Camera Starting ...`);
    return sendSwitchCommand("On");
  };
  OMS.Actions.cameraStopAll = async function () {
    chipMsg(2, 1, `Camera Stopping ...`);
    return sendSwitchCommand("Off");
  };
})(window);

// ────────────────────────────────────────────────
//  Restart Stabilizer (restart-after-state tracker)
//  - 서버 상태스트림(SSE or polling)에서 실제 프로세스가 모두 정상 RUNNING/CONNECTED
//    상태가 될 때까지 감시
//  - 특정 안정화 구간(연속 n번 정상) 충족 시 "onStable" 콜백 호출
// ────────────────────────────────────────────────
(function () {
  const STABLE_REQUIRED = 3;          // 연속 3번 정상 상태면 안정화 된 것으로 판단
  const STABILIZE_TIMEOUT = 20000;    // 최대 20초 대기
  // restart lifecycle monitor
  window.OMS = window.OMS || {};
  window.OMS.Actions = window.OMS.Actions || {};
  /**
   * Start monitoring restart lifecycle
   * @param {Function} onState - every state callback (for UI update)
   * @param {Function} onStable - called when stabilization condition satisfied
   */
  window.OMS.Actions.monitorRestartLifecycle = function (onState, onStable) {
    let stableCount = 0;
    let stopped = false;
    let timeoutId = null;
    // SSE 구독
    const evt = new EventSource((window.__OMS_API_PREFIX__ || "") + "/oms/sys-restart/stream");
    const finish = (reason, last) => {
      if (stopped) return;
      stopped = true;
      try { evt.close(); } catch (e) { }
      clearTimeout(timeoutId);
      onStable({ reason, lastState: last });
    };
    evt.onmessage = (e) => {
      let s = null;
      try { s = JSON.parse(e.data); } catch (_) { }
      if (!s) return;

      // 상태반복 UI 업데이트는 상위(UI)에서 처리
      onState(s);

      // state == done 이라도 실제 프로세스가 살아났는지 확인 필요
      if (s.state === "running") {
        stableCount = 0;
        return;
      }

      // Running 아님 → 서버 측 완료 메시지 등
      // 실제 안정화 판단은 /oms/status 로직에서 수행해야 함
    };

    // 상태 polling + 안정화 체크
    async function pollStatus() {
      if (stopped) return;

      let st = null;
      try {
        const res = await fetch((window.__OMS_API_PREFIX__ || "") + "/oms/status", { cache: "no-store" });
        st = await res.json();
      } catch (e) {
        stableCount = 0;
        return setTimeout(pollStatus, 1000);
      }

      // 각 노드의 running/connected 상태를 모두 정상이라고 판단하면 stableCount++
      const nodes = st.nodes || [];
      let allGood = true;

      for (const n of nodes) {
        const selected = (n.status && n.status.executables || [])
          .filter(x => x && (x.select !== false));

        for (const p of selected) {
          const running = !!p.running;
          const conn = String(p.connection_state || "").toUpperCase();

          if (!running && conn !== "CONNECTED") {
            allGood = false;
            break;
          }
        }
        if (!allGood) break;
      }

      if (allGood) {
        stableCount++;
        if (stableCount >= STABLE_REQUIRED) {
          return finish("stabilized", st);
        }
      } else {
        stableCount = 0;
      }

      setTimeout(pollStatus, 800);
    }

    // timeout guard
    timeoutId = setTimeout(() => finish("timeout", null), STABILIZE_TIMEOUT);

    // start polling
    setTimeout(pollStatus, 700);
  };

})();

// Load switch info from /oms/status and render into Switch Details table
(function () {
  const W = window;
  const m = location.pathname.match(/^\/proxy\/([^/]+)/);
  const API_PREFIX = m ? "/proxy/" + encodeURIComponent(m[1]) : "";
  async function loadSwitchesFromStatus() {
    try {
      const res = await fetch(API_PREFIX + "/oms/status", {
        cache: "no-store",
      });
      if (!res.ok) {
        throw new Error("HTTP " + res.status);
      }
      const data = await res.json();
      const extra = (data && data.extra) || {};
      const switches = extra.switches || [];
      renderSwitchTable(Array.isArray(switches) ? switches : []);
    } catch (err) {
      console.warn("[OMS] Failed to load switches:", err);
      renderSwitchTable([]);
    }
  }
  // ✅ 전역에서 쓸 수 있게 export
  if (typeof window !== "undefined") {
    window.initSwitchDetailsFromStatus = loadSwitchesFromStatus;
  }

  function renderSwitchTable(list) {
    const tbody = document.getElementById("tblSwitch");
    if (!tbody) return;

    if (!list.length) {
      tbody.innerHTML =
        '<tr><td colspan="4" class="muted">no data</td></tr>';
      return;
    }

    // Switch IP 목록을 기반으로 row + Restart 버튼 생성
    tbody.innerHTML = list
      .map((sw) => {
        const ip = sw.IP || sw.ip || "";
        const brand = sw.Brand || sw.brand || "";
        const model = sw.Model || sw.model || "";

        return `
          <tr data-ip="${ip}">
            <td>${ip}</td>
            <td>${brand}</td>
            <td>${model}</td>
            <td>
              <button type="button"
                      class="btn-secondary btn-sm"
                      data-ip="${ip}"
                      onclick="window.OMS && window.OMS.Actions && window.OMS.Actions.restartCameraSwitch && window.OMS.Actions.restartCameraSwitch('${ip}')">
                🔁 Restart
              </button>
            </td>
          </tr>
        `;
      })
      .join("");

    // 상단 "Restart Cameras" 버튼 → 모든 Switch IP에 대해 stub 호출
    const btnAll = document.getElementById("btnCamAllReboot");
    if (btnAll) {
      btnAll.onclick = function () {
        const ips = list
          .map((sw) => sw.IP || sw.ip || "")
          .filter((v, idx, arr) => v && arr.indexOf(v) === idx);

        if (
          W.OMS &&
          W.OMS.Actions &&
          typeof W.OMS.Actions.restartAllCameras === "function"
        ) {
          W.OMS.Actions.restartAllCameras(ips);
        } else {
          console.warn("OMS.Actions.restartAllCameras stub not found");
        }
      };
    }
  }
  // 다른 스크립트(oms-camera.html bootstrap)에서 직접 호출 가능하게 export
  W.initSwitchDetailsFromStatus = loadSwitchesFromStatus;
  // Run on page load (페이지 로드시 자동으로 한 번 호출)
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadSwitchesFromStatus);
  } else {
    loadSwitchesFromStatus();
  }

  // ─────────────────────────────────────────────
  // Camera Details 초기화 (Status는 일단 Unknown)
  // ─────────────────────────────────────────────
  function initCameraDetailsFromStatus() {
    try {
      const thead = document.getElementById("theadUnified");
      const tbody = document.getElementById("tblUnified");

      if (!thead || !tbody) {
        console.warn("[OMS][Camera] camera table elements not found");
        return;
      }

      // 헤더 고정: Index | IP | Model | PreSd ip | Switch ip | Status
      thead.innerHTML = `
        <tr>
          <th style="width:80px">Index</th>
          <th>IP</th>
          <th>Model</th>
          <th>PreSd IP</th>
          <th>Switch IP</th>
          <th style="width:120px">Status</th>
        </tr>
      `;

      // 기본값: 로딩 중 표기
      tbody.innerHTML = `
        <tr>
          <td colspan="6" class="muted">loading ...</td>
        </tr>
      `;

      fetch(API_PREFIX + "/oms/status", {
        cache: "no-store",
      })
        .then((res) => {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.json();
        })
        .then((js) => {
          // /oms/status 결과에서 cameras 추출
          const cameras =
            (js && js.extra && js.extra.cameras) ||
            js.cameras ||
            [];

          if (!Array.isArray(cameras) || cameras.length === 0) {
            tbody.innerHTML = `
              <tr>
                <td colspan="6" class="muted">no camera data</td>
              </tr>
            `;
            return;
          }

          // cam Index 순으로 정렬
          const sorted = [...cameras].sort((a, b) => {
            const ia = Number(a.Index || 0);
            const ib = Number(b.Index || 0);
            return ia - ib;
          });

          let html = "";
          for (const cam of sorted) {
            const idx    = cam.Index ?? "";
            const ip     = cam.IP ?? "";
            const model  = cam.CameraModel ?? "";
            const presd  = cam.PreSdIP ?? cam.PreSd_id ?? "";
            const swip   = cam.SCdIP ?? cam.SCd_id ?? cam.SCdIP ?? "";
            const status = "Unknown"; // 아직 Connection 정보를 모름

            html += `
              <tr>
                <td>${idx}</td>
                <td>${ip}</td>
                <td>${model}</td>
                <td>${presd}</td>
                <td>${swip}</td>
                <td><span class="badge badge-muted">${status}</span></td>
              </tr>
            `;
          }
          tbody.innerHTML = html;
        })
        .catch((err) => {
          console.error("[OMS][Camera] initCameraDetailsFromStatus failed:", err);
          tbody.innerHTML = `
            <tr>
              <td colspan="6" class="muted">failed to load camera list</td>
            </tr>
          `;
        });
    } catch (e) {
      console.error("[OMS][Camera] initCameraDetailsFromStatus error:", e);
    }
  }
  // 전역 네임스페이스에 붙여두기 (다른 페이지에서도 재사용 가능)
  window.OMS.Actions = window.OMS.Actions || {};
  window.OMS.Actions.initCameraDetailsFromStatus = initCameraDetailsFromStatus;
  // 페이지 진입 시 Camera Details도 자동 초기화
  (function autoInitCameraDetails() {
    function run() {
      // 이 페이지(oms-camera.html)에서만 동작하게 최소한으로 가드
      const hasTable = document.getElementById("tblUnified");
      if (!hasTable) return;
      try {
        initCameraDetailsFromStatus();
      } catch (e) {
        console.error("[OMS][Camera] autoInitCameraDetails failed:", e);
      }
    }

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", run);
    } else {
      run();
    }
  })();
})();