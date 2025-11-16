
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
    // debug
    console.log("_paintChip",text)
    
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
    console.log("_emit",text)

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

  // ---- Single public API ----
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

  // ─────────────────────────────────────────────────────
  // MTd HTTP 프록시 래퍼 + 디버그 로그
  // ─────────────────────────────────────────────────────
  async function mtdSend(host, port, message, timeoutSec = 12) {
    const h = host;
    const p = Number(port) || 19765;
    const payload = {
      host: h,
      port: p,
      timeout: timeoutSec,
      message,
    };

    // 1) 보내기 직전 로그
    pushMtdDebug('send', {
      host: h,
      port: p,
      timeoutSec,
      message,
    });

    let res;
    try {
      res = await api('/oms/mtd-connect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        // MTd 타임아웃보다 살짝 길게
        timeoutMs: (timeoutSec + 5) * 1000,
      });
    } catch (e) {
      // 2) HTTP 레벨 / 네트워크 에러 로그
      pushMtdDebug('error', {
        host: h,
        port: p,
        timeoutSec,
        error: String(e && e.message ? e.message : e),
        status: e && e.status,
        url: e && e.url,
        body: e && e.body,
      });
      throw e;
    }

    // res 는 /oms/mtd-connect 응답 JSON (ok, tag, response …)
    const response = (res && res.response != null) ? res.response : res;

    // 3) 성공 응답 로그
    pushMtdDebug('recv', {
      host: h,
      port: p,
      timeoutSec,
      response,
      tag: res && res.tag,
    });

    return response;
  }
  // ─────────────────────────────────────────────────────
  // MTd 메시지 로컬 디버그 로그 (브라우저 localStorage)
  // ─────────────────────────────────────────────────────
  function pushMtdDebug(direction, payload) {
    try {
      const key = 'oms_mtd_debug_log';
      const now = new Date().toISOString();
      let list;
      try {
        const raw = localStorage.getItem(key);
        list = raw ? JSON.parse(raw) : [];
        if (!Array.isArray(list)) list = [];
      } catch {
        list = [];
      }

      list.push({
        ts: now,
        dir: direction,  // 'send' | 'recv' | 'error'
        ...payload,
      });

      // 로그가 너무 커지지 않도록 최근 200개만 유지
      const MAX_LEN = 200;
      if (list.length > MAX_LEN) {
        list.splice(0, list.length - MAX_LEN);
      }

      localStorage.setItem(key, JSON.stringify(list));
    } catch (e) {
      // 디버그 로깅 실패는 서비스에 영향 주지 않도록 그냥 경고만
      console.warn('pushMtdDebug failed', e);
    }
  }
  
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

  // Keep a small listener to mirror messages across tabs
  try {
    const bc = new BroadcastChannel('oms-progress');
    bc.onmessage = (e) => { const t = (e && e.data && e.data.text) || ''; if (typeof t === 'string' && t.length) chipMsg(1, 1, t); };    
  } catch { }

  // ========================================================================
  // Camera Page (only message plumbing changes here)
  // ========================================================================
  (function cameraWiring() {
    const BTN_RUN = document.getElementById('btnRun');
    if (BTN_RUN) {
      BTN_RUN.addEventListener('click', () => {
        // camera connect sequence progress messages use [camera][connect]
        chipMsg(2, 2, 'Start');
      }, false);
    }
  })();

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
