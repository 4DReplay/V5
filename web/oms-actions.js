/*
 * oms-actions.js
  */

(function (global) {
  'use strict';

  // ---- Core prefix helpers ----
  const CHIP_KEYS = { system: 'oms_sys_progress', camera: 'oms_cam_progress' };
  const CHIP_CHS = { system: 'oms-progress-system', camera: 'oms-progress-camera' };

  // -------------------------------------------------------------------
  // CONNECT 결과 저장
  // -------------------------------------------------------------------
  global.OMS = global.OMS || {};
  global.OMS.Actions = global.OMS.Actions || {};   // ← 존재 안 하면 생성
  global.OMS.ConnectSummary = global.OMS.ConnectSummary || {
    connected: {},
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

  // -------------------------------------------------------------------
  // 🔥 수정됨: 메시지 기반 restart 인식 제거
  // -------------------------------------------------------------------
  function _emit(text, priority = 1) {
    const payload = {
      text: String(text || ''),
      prio: Number(priority) || 1,
      ts: Date.now(),
      origin: (window.__OMS_ORIGIN__ ||= Math.random().toString(36).slice(2)),
      seq: (window.__OMS_SEQ__ = (window.__OMS_SEQ__ | 0) + 1)
    };

    console.log("broadcast message:", text);
    // 🔥 항상 system key 로 저장
    try {
      localStorage.setItem(CHIP_KEYS.system, JSON.stringify(payload));
    } catch {}
    // 🔥 항상 system channel 로 broadcast
    try {
      const bc = _bc(CHIP_CHS.system);
      if (bc) bc.postMessage(payload);
    } catch {}
    return payload;
  }
  
  // ========================================================================
  // API helpers
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
  window.API_BASE = API_BASE;

  async function api(path, init = {}) {
    const url = (typeof path === 'string' && path.startsWith('/')) ? (API_BASE + path) : path;
    const { timeoutMs, ...rest } = init || {};
    const ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    const id = (ctrl && timeoutMs && Number.isFinite(timeoutMs)) ? setTimeout(() => { try { ctrl.abort(); } catch { } }, timeoutMs) : null;

    try {
      const res = await fetch(url, { cache: 'no-store', ...(rest || {}), ...(ctrl ? { signal: ctrl.signal } : {}) });
      const ct = res.headers.get('content-type') || '';
      const body = ct.includes('application/json') ? await res.json().catch(() => ({})) : await res.text().catch(() => '');
      if (!res.ok) {
        const msg = (body && (body.error || body.message)) || `HTTP ${res.status}`;
        const e = new Error(msg);
        e.status = res.status;
        e.url = String(url);
        e.body = body;
        throw e;
      }
      return body;
    } finally { if (id) clearTimeout(id); }
  }

  NS.api = api;

  // ------------------------------
  // Common System Restart (shared)
  // ------------------------------
  NS.sysRestart = async function () {
      try {
          const res = await fetch("/oms/system/restart/all", {
              method: "POST",
              headers: { "Content-Type": "application/json" }
          });

          if (!res.ok) throw new Error("System restart failed");

          _emit("[system][restart] begin");
      } catch (err) {
          console.error("sysRestart() failed:", err);
          throw err;
      }
  };

  // ========================================================================
  // System connect [oms/system/connect]
  // ========================================================================
  NS.sysConnect = async function (extra = {}) {
    if (extra && typeof extra.mtdMessage === 'object') {
      const host = extra.mtd_host || location.hostname;
      const port = Number(extra.mtd_port || 19765);
      const url = API_BASE + '/oms/mtd-query';
      const body = { host, port, message: extra.mtdMessage };

      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        cache: 'no-store'
      });

      const ct = res.headers.get('content-type') || '';
      const resp = ct.includes('application/json') ? await res.json().catch(() => ({})) : await res.text().catch(() => '');

      if (!res.ok) throw new Error((resp && resp.error) || `HTTP ${res.status}`);      
      return resp;
    }

    let dmpdip     = extra?.dmpdip     || '';
    let daemon_map = extra?.daemon_map || undefined;
    let mtd_host   = extra?.mtd_host   || '';
    let mtd_port   = Number(extra?.mtd_port || 19765);

    let statusExtra = null;
    let stateSnap   = null;

    try {
      const pRes = await api('/oms/system/process-list');
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

        if (!name || !ip) continue;
        if (EXCLUDE.has(name)) continue;

        daemon_map[name] = String(ip).trim();
      }

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
    } catch (e) {}

    try {
      const st = await api('/oms/system/state');
      const ex = st?.extra || {};
      statusExtra = ex;

      if (!daemon_map || Object.keys(daemon_map).length === 0) {
        daemon_map = ex.daemon_map || daemon_map;
      }
      if (!dmpdip)     dmpdip     = ex.dmpdip     || '';
      if (!mtd_host)   mtd_host   = ex.mtd_host   || '';
      if (!mtd_port && ex.mtd_port) mtd_port = Number(ex.mtd_port);
    } catch {}

    try {
      const s2 = await api('/oms/camera/state');
      stateSnap = s2;

      if (!daemon_map || Object.keys(daemon_map).length === 0) {
        daemon_map = s2?.daemon_map || s2?.extra?.daemon_map || daemon_map;
      }
      if (!dmpdip)      dmpdip = s2?.dmpdip || s2?.extra?.dmpdip || dmpdip || '';
      if (!mtd_host)    mtd_host = s2?.mtd_host || s2?.extra?.mtd_host || mtd_host || '';
      if (!mtd_port && s2?.mtd_port) { mtd_port = Number(s2.mtd_port); }
    } catch {}

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

    mtd_host ||= location.hostname;
    dmpdip   ||= (function () {
      const h = (location.hostname || '').trim();
      if (!h || h === 'localhost' || h.startsWith('127.')) return '127.0.0.1';
      return h;
    })();

    if (!Number.isFinite(mtd_port) || mtd_port <= 0) mtd_port = 19765;

    const payload = {
      dmpdip,
      daemon_map,
      mtd_host,
      mtd_port,
      trace: true,
      return_partial: true
    };

    try {      
      let res = await api('/oms/system/connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res || res.ok !== true) {
        throw new Error((res && res.error) || 'system/connect failed');
      }

      try {
        const dl = res.response?.DaemonList || {};
        const retryMap = {};
        
        for (const [name, info] of Object.entries(dl)) {
          if (info && info.Status === "NOK") {
            retryMap[name] = info.IP || daemon_map[name];
          }
        }

        if (Object.keys(retryMap).length > 0) {
          const retryPayload = {
            dmpdip,
            daemon_map: retryMap,
            mtd_host,
            mtd_port,            
            trace: true,
            return_partial: true
          };

          const retryRes = await api('/oms/system/connect', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(retryPayload)
          });

          if (retryRes?.response?.DaemonList) {
            for (const [name, info] of Object.entries(retryRes.response.DaemonList)) {
              res.response.DaemonList[name] = info;
            }
          }
        }

        try {
          const dl = res?.response?.DaemonList || {};
          const summary = global.OMS.ConnectSummary;
          summary.lastUpdate = Date.now();

          for (const [name, info] of Object.entries(dl)) {
            if (info?.Status === "OK") {
              summary.connected[name] = true;
            } else {
              summary.connected[name] = false;
            }
          }

          const bc = new BroadcastChannel('oms-connect-summary');
          bc.postMessage(summary);
          bc.close();
        } catch (e) {
          console.warn("ConnectSummary update failed:", e);
        }
      } catch (err) {
        console.warn("Retry connect failed:", err);
      }

      return res;

    } catch (e) {
      const msg = String(e?.message || e || '');      
      throw e;
    }
  };

  // ========================================================================
  // System Page UI
  // ========================================================================
  NS.hooks = { reloadNow: null, render: null };
  NS.mountPage = function ({ hooks } = {}) {
    if (hooks?.reloadNow) NS.hooks.reloadNow = hooks.reloadNow;
    if (hooks?.render) NS.hooks.render = hooks.render;

    const btnSysConnect   = document.getElementById('btnSysConnect');
    const btnSysRestart   = document.getElementById('btnSysRestart');    

    if (btnSysConnect)
      btnSysConnect.addEventListener('click', () => NS.sysConnect().catch(e => console.warn(e)));

    if (btnSysRestart)
      btnSysRestart.addEventListener('click', () => NS.sysRestart().catch(e => alert('Restart-All failed: ' + (e?.message || e))));
  };

  // ========================================================================
  // Camera Command
  // ========================================================================
  window.OMS = window.OMS || {};
  OMS.Actions = OMS.Actions || {};
  OMS.Actions.setBusy = function (msg) {
    const el = document.getElementById("busy");
    if (el) el.textContent = msg || "";
  };
  
  // ========================================================================
  // 🔹 Connect Cameras 전체 워크플로우
  // ========================================================================
  OMS.Actions.cameraConnectAll = async function () {
    try {
      OMS.Actions.setBusy("Connecting cameras...");
      console.log("Connecting cameras..")

      const res = await fetch(API_BASE + "/oms/camera/connect/all", {
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
        OMS.Actions.setBusy("Check System");
        console.error("connect-all error:", data);
      }

      if (typeof window.fetchCameraState === "function") {
        window.fetchCameraState();
      }

    } catch (err) {
      console.error("cameraConnectAll failed:", err);
      OMS.Actions.setBusy("Check System");
    } finally {
      setTimeout(() => OMS.Actions.setBusy(""), 1500);
    }
  };

  OMS.Actions.cameraRebootAll = async function () {
    return fetch("/oms/camera/action/reboot", {
      method: "POST"
    }).then(r => r.json());
  };
  OMS.Actions.cameraStartAll = async function () {
    return fetch("/oms/camera/action/start", {
      method: "POST"
    }).then(r => r.json());
  };
  OMS.Actions.cameraStopAll = async function () {
    return fetch("/oms/camera/action/stop", {
      method: "POST"
    }).then(r => r.json());
  };

  // ========================================================================
  // Auto Focus
  // ========================================================================
  OMS.Actions.autoFocus = async function (ip = null) {
    try {
      const payload = ip ? { ip } : {};

      const res = await fetch(API_BASE + "/oms/camera/action/autofocus", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store"
      });

      const data = await res.json();

      if (!data.ok) {
        const detail = data.detail || data.error || data;
        alert(
          "Auto Focus Fail\n\n" +
          JSON.stringify(detail, null, 2)
        );
        return;
      }

      const result = data.detail || {};
      const counts = result.detail || {};

      alert(
        `Auto Focus Finish (Camera: ${ip ? ip : "All"})\n\n` +
        `Success: ${counts.ok_count}\n` +
        `Fail: ${counts.fail_count}`
      );

    } catch (err) {
      alert("Auto Focus Request Fail: " + err);
      console.error("AutoFocus error:", err);
    }
  };

})(window);

/**
 * Restart Stabilizer — OPTIONAL
 * (필요 없다면 그대로 유지해도 동작 문제 없음)
 */
(function () {
  const STABLE_REQUIRED = 3;
  const STABILIZE_TIMEOUT = 20000;

  window.OMS = window.OMS || {};
  window.OMS.Actions = window.OMS.Actions || {};

  window.OMS.Actions.monitorRestartLifecycle = function (onState, onStable) {
    let stableCount = 0;
    let stopped = false;
    let timeoutId = null;

    const evt = new EventSource(API_BASE + "/oms/system/restart/stream");

    const finish = (reason, last) => {
      if (stopped) return;
      stopped = true;
      try { evt.close(); } catch (e) {}
      clearTimeout(timeoutId);
      onStable({ reason, lastState: last });
    };

    evt.onmessage = (e) => {
      let s = null;
      try { s = JSON.parse(e.data); } catch (_) {}
      if (!s) return;
      onState(s);

      if (s.state === "running") {
        stableCount = 0;
        return;
      }
    };

    async function pollStatus() {
      if (stopped) return;

      let st = null;
      try {
        const res = await fetch(API_BASE + "/oms/system/state", { cache: "no-store" });
        st = await res.json();
      } catch (e) {
        stableCount = 0;
        return setTimeout(pollStatus, 1000);
      }

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

    timeoutId = setTimeout(() => finish("timeout", null), STABILIZE_TIMEOUT);
    setTimeout(pollStatus, 700);
  };
  // --- 신규 버전 (camera/state 기반) ---
  async function initSwitchDetailsFromStatus() {
    try {
      const res = await fetch("/oms/camera/state", { cache: "no-store" });
      const state = await res.json();
      const tbl = document.getElementById("tblSwitch");
      if (!tbl) return;
      const list = state.switches || [];
      tbl.innerHTML = "";
      if (list.length === 0) {
        tbl.innerHTML = '<tr><td colspan="4" class="muted">no data</td></tr>';
        return;
      }
      list.forEach(sw => {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${sw.IP}</td>
          <td>${sw.Brand || "-"}</td>
          <td>${sw.Model || "-"}</td>
          <td><button class="btn-secondary" onclick="OMS.Actions.switchReboot('${sw.IP}')">Reboot</button></td>
        `;
        tbl.appendChild(tr);
      });
    } catch (err) {
      console.error("initSwitchDetailsFromStatus failed", err);
    }
  };
  // --- 신규 camera/state 기반 카메라 테이블 초기화 ---
  async function initCameraDetailsFromStatus () {
    try {
      const res = await fetch("/oms/camera/state", { cache: "no-store" });
      const state = await res.json();
      const cameras = state.cameras || [];

      const thead = document.getElementById("theadUnified");
      const tbody = document.getElementById("tblUnified");
      if (!thead || !tbody) return;

      // 🔸 info 컬럼 정의
      const INFO_COLUMNS = [
        ["col-fw",       "FW"],
        ["col-format",   "Format"],
        ["col-codec",    "Codec"],
        ["col-bitrate",  "Bitrate"],
        ["col-gop",      "GOP"],
        ["col-aperture", "Aperture"],
        ["col-shutter",  "Shutter"],
        ["col-iso",      "ISO"],
        ["col-wb",       "WB"]
      ];

      // 🔥 테이블 헤더 (Status를 IP 오른쪽으로 이동)
      let headerHtml = `
        <tr>
          <th>#</th>
          <th>IP</th>
          <th>Status</th>
          <th>Model</th>
          <th>PreSd IP</th>
          <th>SCd IP</th>
      `;

      INFO_COLUMNS.forEach(([cls, name]) => {
        headerHtml += `<th>${name}</th>`;
      });

      headerHtml += `</tr>`;
      thead.innerHTML = headerHtml;

      // tbody 초기화
      tbody.innerHTML = "";

      if (cameras.length === 0) {
        tbody.innerHTML = '<tr><td class="muted">no data</td></tr>';
        return;
      }

      // 🔥 row 생성
      cameras.forEach(cam => {
        const tr = document.createElement("tr");
        tr.setAttribute("data-ip", cam.IP);

        let rowHtml = `
          <td>${cam.Index}</td>
          <td>${cam.IP}</td>
          <td class="col-status"><span class="pill off">OFF</span></td>
          <td>${cam.CameraModel || "-"}</td>
          <td>${cam.PreSdIP || "-"}</td>
          <td>${cam.SCdIP || "-"}</td>
        `;

        // 🔥 info 셀 생성
        INFO_COLUMNS.forEach(([cls, name]) => {
          rowHtml += `<td class="${cls}">-</td>`;
        });

        tr.innerHTML = rowHtml;
        tbody.appendChild(tr);
      });

    } catch (err) {
      console.error("initCameraDetailsFromStatus failed", err);
    }
  }

  window.OMSActionsReady = function () {
    return window.__OMS_ACTIONS_LOAD__ || Promise.resolve();
  };

  window.OMS.Actions.initCameraDetailsFromStatus = initCameraDetailsFromStatus;
  window.OMS.Actions.initSwitchDetailsFromStatus = initSwitchDetailsFromStatus;

})();

