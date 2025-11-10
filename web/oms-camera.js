// /web/oms-camera.js â€” Connect (6~9) and unified summary table
(function(){
  // ---------- DOM ----------
  const IN_HOST = document.getElementById("camHost");
  const IN_PORT = document.getElementById("camPort");
  const IN_MGMT = document.getElementById("camMgmt");
  const BTN_REFRESH = document.getElementById("btnRefresh");
  const BTN_RUN = document.getElementById("btnRun");
  const SEL_ALL = document.getElementById("selAll");
  const META = document.getElementById("meta");
  const BUSY = document.getElementById("busy");
  const POLL_CHIP = document.getElementById("pollChip");

  const THEAD_UNI = document.getElementById("theadUnified");
  const T_UNI  = document.getElementById("tblUnified");
  // ✅ 누락된 스위치 테이블 바디 참조 추가
  const T_SWITCH = document.getElementById("tblSwitch");
  const BTN_SW_ALL_REBOOT = document.getElementById("btnSwAllReboot");

  // ---------- state ----------
  let LAST_STATUS = {};  // { ip: 'connected'|'video ok'... }
  let LAST_INFO   = [];  // parsed info list
  let LAST_VIDEO  = [];  // parsed video list
  let LAST_SWITCH_LIST = []; // [{ip, Brand, Model}]

  // ⬇️ 추가: CONNECT 세션 토큰
  let CURRENT_CONNECT_TOKEN = 0;        // 최신 Connect 시퀀스 토큰
  const CONNECT_TOKEN = new Map();      // ip -> token (이 토큰이 있어야 connected 등급 유지)
  const PING_ALIVE = new Map();         // ip -> true | false | null
  let ALLOW_UPGRADE = false             // Connect 시퀀스 중에만 연결 승격 허용
  const OFF_LATCH = new Set();          // ⬅️ OFF가 한 번이라도 난 ip는 여기 등록 (재연결 전까지 CONNECTED 금지)

  // 카메라 상태 모니터링
  const CAM_STATUS = new Map();   // ip -> 'running' | 'disconnected' | 'unknown'
  let CAM_MONITOR_TIMER = null;   // setInterval 핸들
  const ALL_CAM_IPS = new Set();  // 표에 보이는 모든 카메라 ip (항상 모니터링)
  const CAM_POLL_MS = 3000;
  const LS_KEY = 'oms__camera_snapshot';   // 로컬 백업 키
  let BC = null;                           // BroadcastChannel 핸들
  // ping 플래핑 방지용 실패 카운터
  const PING_FAILS = new Map();
  const MAX_FAILS = 2;   // 연속 2회 실패 시 OFF

  // ---------- proxy-safe base (assets와 동일한 방식) ----------
  // e.g., /proxy/DMS-1/oms-camera.html  =>  PROXY_PREFIX=/proxy/DMS-1
  const _m = location.pathname.match(/^\/proxy\/([^/]+)/);
  const PROXY_PREFIX = _m ? "/proxy/" + encodeURIComponent(_m[1]) : "";
  const API = (p) => (p.startsWith("/") ? PROXY_PREFIX + p : p);


  // ---------- utils ----------
  function setBusy(text){
     BUSY.textContent = text || ""; 
  }
  function debounce(fn, ms=400){
    let t=null; return (...args)=>{ clearTimeout(t); t=setTimeout(()=>fn(...args), ms); };
  }  
  function setPollMsg(text){
  // 폴링 상태칩 메시지 유틸(칩이 없으면 무시)
    if (!POLL_CHIP) return;
    POLL_CHIP.textContent = text || "poll -";
  }
  async function httpJson(path, init={}) {
    const url = API(path);
    const res = await fetch(url, { cache:"no-store", ...init });
    const ct = res.headers.get("content-type") || "";
    const isJson = ct.includes("application/json");
    if (!res.ok){
      let msg = `HTTP ${res.status}`;
      try{ msg = isJson ? (await res.json()).error || msg : (await res.text()) || msg; }catch{}
      throw new Error(msg);
    }
    return isJson ? res.json() : res.text();
  }
  function makeToken(){
    const d=new Date(); const hh=String(d.getHours()).padStart(2,"0"); const mm=String(d.getMinutes()).padStart(2,"0");
    return `${hh}${mm}_${Date.now()}_${Math.random().toString(36).slice(2,5)}`;
  }
  function guessMgmtIP(host){
    const h = (host||"").trim();
    if (!h || h==="localhost" || h==="127.0.0.1") return "";
    return h;
  }
  function sanitizeMgmt(dmpdip, host){
    const ip = (dmpdip||"").trim();
    if (!ip || ip==="127.0.0.1" || ip==="localhost") {
      const g = guessMgmtIP(host);
      return g || ip || host;
    }
    return ip;
  }
  function mapBy(arr, key){
     const m={}; for(const x of arr||[]) m[x[key]]=x; return m; 
  }
  async function mtdSend(host, port, message, timeoutSec=12){
    const payload = { host, port: Number(port)||19765, timeout: timeoutSec, message };
    const res = await httpJson("/oms/mtd-connect", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    return res?.response ?? res;
  }
  function resetAfterReboot(ipsHint) {
  // ---- Restart 이후: 토큰/상태 리셋하고 핑 기반으로만 표시
    ALLOW_UPGRADE = false;           // 재시작 후엔 업그레이드 차단
    CONNECT_TOKEN.clear();           // 모든 CONNECT 토큰 제거
    const targets = Array.isArray(ipsHint) && ipsHint.length
      ? ipsHint
      : Array.from(ALL_CAM_IPS);
    // 과거 status 문자열도 정리 (connected → on/off/unknown)
    for (const ip of targets) {
      const p = PING_ALIVE.get(ip);  // true | false | null
      LAST_STATUS[ip] = (p === true) ? 'on' : (p === false) ? 'off' : 'unknown';
      setCamStatus(ip);              // 최종 표시 반영 (토큰 없음 → connected 불가)
    }
    persistDebounced();
  }

  // ---------- shared state persist/restore ----------
  // 현재 테이블/메모리 상태를 공용 /oms/state 로 저장
  async function persistUnifiedSnapshot(){
    try{
      // 카메라 목록(IP/Model) — Details 테이블에서 수집
      const cams = [];
      T_UNI?.querySelectorAll('tr[data-ip]').forEach(tr=>{
        const ip = tr.getAttribute('data-ip');
        const model = tr.getAttribute('data-model') || '';
        if (ip) cams.push({ IP: ip, CameraModel: model });
      });
      const snapshot  = {
        cameras: cams,                     // Camera List
        camera_status: LAST_STATUS || {},  // raw status map (off|connected|video ok...)
        camera_info: LAST_INFO || [],      // GetCameraInfo 결과
        camera_video: LAST_VIDEO || [],    // GetVideoFormat 결과
        connected_ips: Array.from(CONNECT_TOKEN.keys()),   // ⬅️ 추가
        off_latch_ips: Array.from(OFF_LATCH.values ? OFF_LATCH : new Set(OFF_LATCH)), // ⬅️ 추가 
        updated_at: Date.now()
      };
      // 1) 서버 저장 (여러 엔드포인트 순차 시도)
      const endpoints = ['/oms/state','/oms/state/save','/oms/save-state'];
      for (const ep of endpoints){
        try{
          const r = await fetch(API(ep), {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(snapshot)
          });
          if (r.ok) break;
        }catch{}
      }
      // 2) localStorage 백업 저장
      try{ localStorage.setItem(LS_KEY, JSON.stringify(snapshot)); }catch{}
      // 3) BroadcastChannel 전파 (다른 탭/페이지 실시간 반영)
      try{
        BC ||= new BroadcastChannel('oms-state');
        BC.postMessage({ type:'camera_snapshot', ...snapshot });
      }catch{}
    }catch(e){
      console.warn('[persistUnifiedSnapshot] save failed:', e?.message||e);
    }
  }
  const persistDebounced = debounce(persistUnifiedSnapshot, 600);

  
  // ---------- parsers ----------
  function parse_ccd_select(resp){
    const ra = Array.isArray(resp?.ResultArray) ? resp.ResultArray : [];
    if (ra.length){
      const cameras = ra
        .map(r => ({ Index: r.cam_idx ?? r.id, IP: r.ip, CameraModel: r.model || "" }))
        .filter(c => !!c.IP);
      const sw = new Set();
      for (const r of ra){ if (r.SCd_id) sw.add(r.SCd_id); }
      return { cameras, switch_ips: Array.from(sw) };
    }  
    const cams_src = resp?.Cameras || resp?.CameraList || resp?.CameraInfo || [];
    const cameras = cams_src
      .map(c=>({ Index:c.Index, IP:(c.IP||c.IPAddress), CameraModel:(c.CameraModel||c.Model||c.ModelName)||"" }))
      .filter(c=>!!c.IP);
    return { cameras, switch_ips: [] };
  }
  function parse_camera_info(resp){
    const arr = Array.isArray(resp?.Cameras) ? resp.Cameras : [];
    return arr.map(r=>({
      IP: r.IPAddress || r.IP || r.ip,
      Model: r.ModelName || r.Model || r.CameraModel || "",
      UUID: r.UUID || "",
      FW: r.FirmwareVersion || r.FW || "",
      WB: r.WhiteBalance || r.WB || "",
      ISO: r.ISO || "",
      Shutter: r.ShutterSpeed || r.Shutter || "",
      Aperture: r.Aperture ?? "",
      FocusMode: r.FocusMode || ""
    })).filter(x=>!!x.IP);
  }
  function parse_video_format(resp){
    const arr = Array.isArray(resp?.Cameras) ? resp.Cameras : [];
    return arr.map(r=>({
      IP: r.IPAddress || r.IP || r.ip,
      Model: r.ModelName || r.Model || r.CameraModel || "",
      Stream: r.StreamType || r.Stream || "",
      Format: r.VideoFormatMain || r.Format || "",
      Codec: r.Codec || "",
      Bitrate: r.VideoBitrateMain || r.Bitrate || "",
      GOP: (r.VideoGopMain ?? r.VideoGop ?? "")
    })).filter(x=>!!x.IP);
  }


  // ---- Switch helpers ----
  function asSwitchArray(v){
    if (!v) return [];
    if (Array.isArray(v)) return v;
    if (Array.isArray(v.list)) return v.list;      // { list: [...] }
    if (Array.isArray(v.switches)) return v.switches;
    if (Array.isArray(v.items)) return v.items;
    return [];
  }
  function renderSwitch(list){
     if (!T_SWITCH) return;

    const arr = asSwitchArray(list);
    LAST_SWITCH_LIST = arr.map(s => ({
      ip: s.ip || s.IP || "",
      Brand: (s.Brand || "").trim(),
      Model: String(s.Model || "").replace(/\\r?\\n/g,"").trim()
    })).filter(x => x.ip);

    if (LAST_SWITCH_LIST.length === 0){
      T_SWITCH.innerHTML = `<tr><td colspan="4" class="muted">no data</td></tr>`;
      return;
    }
    T_SWITCH.innerHTML = LAST_SWITCH_LIST.map(s => `
      <tr data-ip="${s.ip}">
        <td>${s.ip}</td>
        <td>${s.Brand || '-'}</td>
        <td>${s.Model || '-'}</td>
        <td style="white-space:nowrap">
           <button class="btn-secondary sw-op" data-op="On"     data-ip="${s.ip}">⚡ Enable Port</button>
           <button class="btn-secondary sw-op" data-op="Off"    data-ip="${s.ip}">⏻ Diable Port</button>
           <button class="btn-secondary sw-op" data-op="Reboot" data-ip="${s.ip}">⟳ Restart Port</button>
        </td>
      </tr>
    `).join('');
  }
  window.renderSwitchInfo = function(list){ renderSwitch(list); };

  // ---- Ping
  function recomputeState(ip){
    const hasConn = CONNECT_TOKEN.has(ip) && !OFF_LATCH.has(ip); // ⬅️ 래치가 걸려있으면 토큰이 있어도 무효
    const p = PING_ALIVE.get(ip); // true | false | null

    if (hasConn) return 'connected';
    if (p === true)  return 'on';
    if (p === false) return 'off';

    // unknown 유지 (기존 상태가 있으면 그걸 유지)
    return normalizeState(CAM_STATUS.get(ip) || 'unknown');
  }
  function setCamStatus(ip){
    const final = recomputeState(ip);
    CAM_STATUS.set(ip, final);

    const cell = T_UNI?.querySelector(`.cam-status[data-ip="${ip}"]`);
    if (cell) cell.innerHTML = statusBadge(final);

    persistDebounced();
  }
  async function initialPingAll(cameras){
    const tasks = (cameras||[]).map(async c=>{
      const alive = await pingIp(c.IP);
      // 초기 핑에도 우선순위 규칙 적용
      applyPingResult(c.IP, alive);
      ALL_CAM_IPS.add(c.IP);
    });
    await Promise.all(tasks);
    ensureMonitorTimer();
    persistDebounced();
  }
  function applyPingResult(ip, alive){
    if (alive === true){
      PING_FAILS.set(ip, 0);
      PING_ALIVE.set(ip, true);
      // connected 토큰이 있으면 connected 유지, 없으면 on
    } else if (alive === false){
      CONNECT_TOKEN.delete(ip);                 // off 관측 시 토큰 박탈
      OFF_LATCH.add(ip);                        // ⬅️ 래치 설정
      PING_FAILS.set(ip, (PING_FAILS.get(ip)||0) + 1);
      PING_ALIVE.set(ip, false);
      persistDebounced(); // ⬅️ 다른 탭들도 바로 ‘connected’ 해제되도록
    } else {
      const n = (PING_FAILS.get(ip)||0) + 1;
      PING_FAILS.set(ip, n);
      if (n >= MAX_FAILS){
        CONNECT_TOKEN.delete(ip);
        OFF_LATCH.add(ip);                      // ⬅️ 연속 측정불가 임계 시에도 래치
        PING_ALIVE.set(ip, false);
        persistDebounced();
      } else {
        PING_ALIVE.set(ip, null);               // 측정불가는 상태 유지 의도
      }
    }
    setCamStatus(ip);                            // ← 최종 상태 산출/반영
  }    
  async function pingIp(ip){
  // 간단 핑 (백엔드에 /oms/ping?ip=1.2.3.4 가 있다고 가정; 없으면 항상 unknown)
    try{
      // ping은 소프트 실패(null)와 하드 실패(false)를 구분한다.
      const url = API(`/oms/ping?ip=${encodeURIComponent(ip)}&t=${Date.now()}`);
      const res = await fetch(url, { cache:"no-store" });
      if (res.ok){
        const ct = res.headers.get("content-type") || "";
        if (ct.includes("application/json")){
          const r = await res.json();
          if (r && typeof r === "object"){
            if ("error" in r && r.ok === false) return null; // "not found" 등은 플랩 방지
            if ("alive" in r) return !!r.alive;
            if ("ok" in r)    return !!r.ok;
          }
          return null;
        } else {
          const s = (await res.text()).trim().toLowerCase();
          if (s === "ok" || s === "alive" || s === "true" || s === "1") return true;
          if (s === "false" || s === "0" || s === "dead") return false;
          return null;
        }
      }
      // 백엔드가 404/5xx면 프런트 폴백으로 시도
      return await imgPing(ip);

    }catch{
      // 네트워크 예외 시에도 폴백 시도
      try { return await imgPing(ip); } catch { return null; }
    }
  }  
  function imgPing(ip){
  // 브라우저 이미지 핑(폴백). onload → true, onerror/timeout → false
    return new Promise((resolve)=>{
      const img = new Image();
      const timer = setTimeout(()=>{ cleanup(); resolve(false); }, 1500);
      function cleanup(){ img.onload = img.onerror = null; clearTimeout(timer); }
      img.onload = ()=>{ cleanup(); resolve(true); };
      img.onerror = ()=>{ cleanup(); resolve(false); };
      const proto = location.protocol === 'https:' ? 'http:' : location.protocol; // 로컬은 http 가정
      img.src = `${proto}//${ip}/favicon.ico?_=${Date.now()}`;
    });
  }


  // --- Status
  function ensureMonitorTimer(){
    if (CAM_MONITOR_TIMER) return;
      // 시작 시 즉시 한 번 상태 메시지 갱신
      setPollMsg(`poll ${Math.round(CAM_POLL_MS/1000)}s`);
      CAM_MONITOR_TIMER = setInterval(async ()=>{
      const ips = Array.from(ALL_CAM_IPS);
      if (!ips.length) return;
      // 병렬로 핑
      const tasks = ips.map(async ip => {
        const alive = await pingIp(ip).catch(()=>null);
        // 우선순위 규칙으로 상태 반영
        applyPingResult(ip, alive);
      });
      await Promise.all(tasks);
      // 폴링 대상 수를 함께 보여주기
      setPollMsg(`poll ${Math.round(CAM_POLL_MS/1000)}s • ${ips.length} cam`);
    }, CAM_POLL_MS);
  }
  function statusBadge(state){
    const s = String(state||'').toLowerCase();
    if (s === 'connected') return '<span class="pill connected">CONNECTED</span>';
    if (s === 'on'  || s === 'running') return '<span class="pill on">ON</span>';
    if (s === 'off' || s === 'disconnected' || s === 'not connected')
      return '<span class="pill off">OFF</span>';
    return '<span class="pill off">UNKNOWN</span>';
  }
  function normalizeState(v){
    const s = String(v||'').toLowerCase();
    if (s.includes('video ok') || s === 'connected' || s === 'ok') return 'connected';
    if (s === 'on' || s === 'running') return 'on';
    if (s === 'off' || s === 'disconnected' || s === 'not connected' || s === 'failed') return 'off';
    return 'unknown';
  }
  function renderConns(map){
    const entries = Object.entries(map || {});
    if (!entries.length) return;

    for (const [ip, raw] of entries) {
      const s = String(raw || '').toLowerCase();
      const connected =
        s.includes('video ok') ||
        s.includes('connected') ||
        s.includes('info ok') ||
        s === 'ok';

      // 재시작 이후엔 업그레이드 금지, Connect 시퀀스 안에서만 허용
      if (connected && ALLOW_UPGRADE && !OFF_LATCH.has(ip)) {
        CONNECT_TOKEN.set(ip, CURRENT_CONNECT_TOKEN);
      }
      // ❌ 기존처럼 else에서 off로 내리지 않음
      setCamStatus(ip);                                // 토큰/핑 종합 반영
    }
    ensureMonitorTimer();
    persistDebounced();
  }  
  function renderDetailsPre(cameras){
  // --- Details 테이블 렌더링 (pre/post 단계별로 헤더와 행을 다르게) ---
    if (!Array.isArray(cameras) || cameras.length===0){
      THEAD_UNI.innerHTML = '';
      T_UNI.innerHTML = `<tr><td class="muted">no data</td></tr>`;
      return;
    }
    THEAD_UNI.innerHTML = `
      <tr>
        <th style="width:50px">Select</th>
        <th>IP</th>
        <th>Status</th>
        <th>Model</th>
      </tr>`;
    const rows = cameras.map(c=>{
      const ip = c.IP;
      const st = recomputeState(ip);
      const model = c.CameraModel || c.Model || '';
      const chkId = `sel_${ip.replace(/\./g,'_')}`;
      return `
        <tr data-ip="${ip}" data-model="${model}">
          <td style="text-align:center">
            <input type="checkbox" class="rowSel" id="${chkId}" data-ip="${ip}" checked>
          </td>
          <td class="cell-ip">${ip}</td>
          <td class="cam-status" data-ip="${ip}">${statusBadge(st)}</td>
          <td class="cell-model">${model}</td>
        </tr>`;
    }).join('');
    T_UNI.innerHTML = rows;
  }
  function renderDetailsPost(statusMap, infoList, videoList){
    const sinfo = mapBy(infoList, "IP");
    const svid  = mapBy(videoList, "IP");
    const ips = Array.from(new Set([
      ...Object.keys(statusMap||{}),
      ...Object.keys(sinfo),
      ...Object.keys(svid)
    ])).sort((a,b)=>a.localeCompare(b, undefined, {numeric:true}));

    if (ips.length===0){
      THEAD_UNI.innerHTML = '';
      T_UNI.innerHTML = `<tr><td class="muted">no data</td></tr>`;
      return;
    }
    THEAD_UNI.innerHTML = `
      <tr>
        <th style="width:50px">Select</th>
        <th>IP</th>
        <th>Status</th>
        <th>Model</th>
        <th>FW</th>
        <th>Format</th><th>Codec</th><th>Bitrate</th><th>GOP</th>
        <th>WB</th><th>ISO</th><th>Shutter</th><th>Aperture</th><th>FocusMode</th>
      </tr>`;

    const rows = ips.map(ip=>{
      const i = sinfo[ip] || {};
      const v = svid[ip]  || {};
      const model = i.Model || v.Model || '';

      // 최종 상태는 recomputeState로 재산출 (표시 일관)
      const final = recomputeState(ip);
      const badge = statusBadge(final);

      return `
        <tr data-ip="${ip}" data-model="${model}">
          <td style="text-align:center"><input type="checkbox" class="rowSel" data-ip="${ip}" checked></td>
          <td class="cell-ip">${ip}</td>
          <td class="cam-status" data-ip="${ip}">${badge}</td>
          <td class="cell-model">${model||''}</td>
          <td>${i.FW||''}</td>
          <td>${v.Format||''}</td>
          <td>${v.Codec||''}</td>
          <td>${v.Bitrate||''}</td>
          <td>${v.GOP||''}</td>
          <td>${i.WB||''}</td>
          <td>${i.ISO||''}</td>
          <td>${i.Shutter||''}</td>
          <td>${i.Aperture||''}</td>
          <td>${i.FocusMode||''}</td>
        </tr>`;
    }).join('');
    T_UNI.innerHTML = rows;
  }
  
  // ---------- data flows ----------
  async function loadFromState(){
    try{
      const st = await httpJson("/oms/state");
      const dmpdip = st?.dmpdip || st?.DMPDIP || "";
      if (dmpdip) IN_MGMT.value = dmpdip;

      // ▼ 추가: switch_info 먼저 렌더 (있으면 바로 표시)
      renderSwitch(st && (st.switch_info ?? st.switchInfo ?? st.switches));

      // 카메라 공용 스냅샷 복원 (서버 우선, 없으면 localStorage 백업 사용)
      let cams  = Array.isArray(st?.cameras) ? st.cameras : [];
      let sMap  = st?.camera_status || {};
      let info  = Array.isArray(st?.camera_info) ? st.camera_info : [];
      let video = Array.isArray(st?.camera_video) ? st.camera_video : [];
      // ⬇️ 토큰/래치 복원
      if (Array.isArray(st?.connected_ips)) {
        CONNECT_TOKEN.clear();
        st.connected_ips.forEach(ip => CONNECT_TOKEN.set(ip, st.updated_at || Date.now()));
      }
      if (Array.isArray(st?.off_latch_ips)) {
        OFF_LATCH.clear();
        st.off_latch_ips.forEach(ip => OFF_LATCH.add(ip));
      }
      if (!cams.length){
        try{
          const raw = localStorage.getItem(LS_KEY);
          if (raw){
            const snap = JSON.parse(raw);
            cams  = Array.isArray(snap?.cameras) ? snap.cameras : cams;
            sMap  = snap?.camera_status || sMap;
            info  = Array.isArray(snap?.camera_info) ? snap.camera_info : info;
            video = Array.isArray(snap?.camera_video) ? snap.camera_video : video;
          }
        }catch{}
      }
      if (cams.length){
        // pre/post 자동 판단: 상세정보가 있으면 post, 아니면 pre
        if ((info?.length||0)===0 && (video?.length||0)===0){
          renderDetailsPre(cams);
        }else{
          // post 모드 렌더
          LAST_STATUS = sMap; LAST_INFO = info; LAST_VIDEO = video;
          renderDetailsPost(LAST_STATUS, LAST_INFO, LAST_VIDEO);
        }
        // 상태맵 복원
        LAST_STATUS = sMap;
        renderConns(LAST_STATUS);
        // 핑 모니터링 시작
        initialPingAll(cams).catch(()=>{});
        return true;
      }
    }catch{}
    return false;
  }  
  (function subscribeSwitchBroadcast(){
  // (선택) 러너가 BroadcastChannel('oms-state')로 업데이트를 브로드캐스트하면 실시간 반영
    try{
      BC = new BroadcastChannel('oms-state');
      BC.onmessage = (ev)=>{
        const d = ev?.data || {};
        // 다른 페이지에서 저장한 카메라 스냅샷 실시간 반영
        if (d.type === 'camera_snapshot' && Array.isArray(d.cameras)){
          LAST_STATUS = d.camera_status || {};
          LAST_INFO   = Array.isArray(d.camera_info)  ? d.camera_info  : [];
          LAST_VIDEO  = Array.isArray(d.camera_video) ? d.camera_video : [];
          // ⬇️ 토큰/래치 동기화
          if (Array.isArray(d.connected_ips)) {
            CONNECT_TOKEN.clear();
            d.connected_ips.forEach(ip => CONNECT_TOKEN.set(ip, d.updated_at || Date.now()));
          }
          if (Array.isArray(d.off_latch_ips)) {
            OFF_LATCH.clear();
            d.off_latch_ips.forEach(ip => OFF_LATCH.add(ip));
          }        
          if ((LAST_INFO.length||0)===0 && (LAST_VIDEO.length||0)===0){
            renderDetailsPre(d.cameras);
          }else{
            renderDetailsPost(LAST_STATUS, LAST_INFO, LAST_VIDEO);
          }
          renderConns(LAST_STATUS);
          return;
        }
        if ('switch_info' in d) renderSwitch(d.switch_info);
        else if ('switchInfo' in d) renderSwitch(d.switchInfo);
        else if ('switches' in d) renderSwitch(d.switches);
      };
    }catch{}
  })();  
  async function fetchSwitchModels(host, port, dmpdip, switchIps){
  // SCd에 스위치 모델 조회
    if (!Array.isArray(switchIps) || switchIps.length === 0){
      renderSwitch([]); return [];
    }
    const msg = {
      Section1: "Switch",
      Section2: "Information",
      Section3: "Model",
      SendState: "request",
      From: "4DOMS",
      To: "SCd",
      Token: makeToken(),
      Action: "get",
      DMPDIP: dmpdip,
      Switches: switchIps.map(ip => ({ ip }))
    };
    const r = await mtdSend(host, port, msg, 10);
    const list = Array.isArray(r?.Switches) ? r.Switches : [];
    renderSwitch(list);
    return list;
  }

  // ---- Switch 전체/개별 On/Off/Reboot (포트 구분 없음) ----
  async function sendSwitchOp(host, port, dmpdip, op, ips){
    const Switches = ips.map(ip => ({ ip }));
    const msg = {
      Section1:"Switch",
      Section2:"Operation",
      Section3: op,               // "On" | "Off" | "Reboot"
      SendState:"request",
      From:"4DOMS",
      To:"SCd",
      Token: makeToken(),
      Action:"run",
      DMPDIP: dmpdip,
      Switches
    };
    const r = await mtdSend(host, port, msg, 12);
    const arr = Array.isArray(r?.Switches) ? r.Switches : [];
    // 간단 피드백
    const fails = arr.filter(s => Number(s.resultCode) !== 1000);
    if (fails.length){
      const msgTxt = fails.map(s => `${s.ip}: ${s.resultCode} ${s.errorMsg||""}`.trim()).join("\\n");
      alert(`[${op}] some switches failed:\\n` + msgTxt);
    } else {
      alert(`[${op}] OK for ${arr.length} switch(es).`);
    }
    return r;
  }
  async function fetchListViaSelect(){
    const host = (IN_HOST.value||"").trim();
    const port = Number(IN_PORT.value||19765);
    const dmpdip = sanitizeMgmt(IN_MGMT.value, host);

    const msg = {
      Section1:"CCd", Section2:"Select", Section3:"",
      SendState:"request", From:"4DOMS", To:"EMd",
      Token: makeToken(), Action:"get", DMPDIP: dmpdip
    };
    const r = await mtdSend(host, port, msg, 12);
    const { cameras, switch_ips } = parse_ccd_select(r);
    renderDetailsPre(cameras);
    initialPingAll(cameras).catch(()=>{});
    // 카메라 표를 그릴 때 Connections도 함께 초기화 (Not Connected)
    if (Array.isArray(cameras) && cameras.length){
      LAST_STATUS = statusMapInit(cameras.map(c=>c.IP));
      renderConns(LAST_STATUS);
    }    
    try { await fetchSwitchModels(host, port, dmpdip, switch_ips); } catch {}
    // 선택 결과 저장
    persistDebounced();
    return cameras;
  }

  // ---------- steps 6~9 ----------
  function getSelectedIPs(){
    const ips = [];
    T_UNI?.querySelectorAll(".rowSel").forEach(ch=>{
      if (ch.checked) ips.push(ch.dataset.ip);
    });
    return ips;
  }
  function statusMapInit(ips){
    // 초기 표시는 Not Connected로
    const m={}; for(const ip of ips) m[ip] = "off";
    return m;
  }
  async function step6_addCamera(host, port, dmpdip, ips){
    const cameras = ips.map(ip => ({ IPAddress: ip, Model: "BGH1" }));
    const msg = {
      Cameras: cameras,
      Section1:"Camera", Section2:"Information", Section3:"AddCamera",
      SendState:"request", From:"4DOMS", To:"CCd",
      Token: makeToken(), Action:"set", DMPDIP: dmpdip
    };
    return mtdSend(host, port, msg, 10);
  }
  async function step7_connect(host, port, dmpdip){
    const msg = {
      Section1:"Camera", Section2:"Operation", Section3:"Connect",
      SendState:"request", From:"4DOMS", To:"CCd",
      Token: makeToken(), Action:"run", DMPDIP: dmpdip
    };
    return mtdSend(host, port, msg, 12);
  }
  async function step8_getInfo(host, port, dmpdip, ips){
    const msg = {
      Cameras: ips.slice(),
      Section1:"Camera", Section2:"Information", Section3:"GetCameraInfo",
      SendState:"request", From:"4DOMS", To:"CCd",
      Token: makeToken(), Action:"get", DMPDIP: dmpdip
    };
    const r = await mtdSend(host, port, msg, 12);
    return parse_camera_info(r);
  }
  async function step9_getVideo(host, port, dmpdip, ips){
    const msg = {
      Cameras: ips.slice(),
      Section1:"Camera", Section2:"Information", Section3:"GetVideoFormat",
      SendState:"request", From:"4DOMS", To:"CCd",
      Token: makeToken(), Action:"get", DMPDIP: dmpdip
    };
    const r = await mtdSend(host, port, msg, 12);
    return parse_video_format(r);
  }
  async function runConnectSequence(){
    const host = (IN_HOST.value||"").trim();
    const port = Number(IN_PORT.value||19765);
    const dmpdip = sanitizeMgmt(IN_MGMT.value, host);

    const ips = getSelectedIPs();
    if (!ips.length){ alert("Please select at least one camera."); return; }

    // ⬇️ 새 Connect 세션 시작: 신규 토큰 발급
    CURRENT_CONNECT_TOKEN = Date.now();
    ALLOW_UPGRADE = true;  // ← 이때부터 연결 업그레이드 허용
    LAST_STATUS = statusMapInit(ips);
    renderConns(LAST_STATUS);

    // 6) AddCamera
    setBusy("Adding cameras (6)...");
    try{
      await step6_addCamera(host, port, dmpdip, ips);
      ips.forEach(ip => LAST_STATUS[ip] = "added");
      renderConns(LAST_STATUS);
      persistDebounced();
    }catch(e){
      ips.forEach(ip => LAST_STATUS[ip] = "add failed");
      renderConns(LAST_STATUS);
      throw e;
    }

    // 7) Connect
    setBusy("Connecting (7)...");
    try{
      const r7 = await step7_connect(host, port, dmpdip);
      if (Array.isArray(r7?.Cameras)){
        r7.Cameras.forEach(c=>{
          const ip = c.IPAddress || c.IP || c.ip;
          if (ip){
            const ok = String(c.Status).toUpperCase()==="OK";
            LAST_STATUS[ip] = ok ? "Connected" : (c.Status || "failed");
            if (ok) {
              OFF_LATCH.delete(ip);                    // ⬅️ 성공한 IP만 래치 해제
              CONNECT_TOKEN.set(ip, CURRENT_CONNECT_TOKEN);
              setCamStatus(ip);
              persistDebounced();  // ⬅️ 즉시 다른 탭에 반영
            }
          }
        });
      } else {
        ips.forEach(ip => {
          OFF_LATCH.delete(ip);                         // ⬅️ 시퀀스 성공으로 간주
          CONNECT_TOKEN.set(ip, CURRENT_CONNECT_TOKEN);
          LAST_STATUS[ip] = "connected";
          setCamStatus(ip);
        });
      }
      renderConns(LAST_STATUS);
      persistDebounced();
    }catch(e){
      ips.forEach(ip => LAST_STATUS[ip] = "connect failed");
      renderConns(LAST_STATUS);
      throw e;
    }

    // 8) GetCameraInfo
    setBusy("Fetching camera info (8)...");
    try{
      LAST_INFO = await step8_getInfo(host, port, dmpdip, ips);
      const by = mapBy(LAST_INFO, "IP");
      ips.forEach(ip => {
        if (by[ip]) {
          OFF_LATCH.delete(ip);                          // ⬅️ 성공 응답이 있는 IP만
          CONNECT_TOKEN.set(ip, CURRENT_CONNECT_TOKEN);
          setCamStatus(ip);
        }
      });
      renderConns(LAST_STATUS);
      renderDetailsPost(LAST_STATUS, LAST_INFO, LAST_VIDEO);
      persistDebounced();
    }catch{}

    // 9) GetVideoFormat
    setBusy("Fetching video formats (9)...");
    try{
      LAST_VIDEO = await step9_getVideo(host, port, dmpdip, ips);
      const byv = mapBy(LAST_VIDEO, "IP");
      ips.forEach(ip => {
        if (byv[ip]) {
          OFF_LATCH.delete(ip);                          // ⬅️ 성공 응답이 있는 IP만
          CONNECT_TOKEN.set(ip, CURRENT_CONNECT_TOKEN);
          setCamStatus(ip);
        }
      });
      renderConns(LAST_STATUS);
      // 연결 정보 일부라도 확보되면 post 모드로 업그레이드
      renderDetailsPost(LAST_STATUS, LAST_INFO, LAST_VIDEO);
      persistDebounced();
    }catch{}
    setBusy("");
    ALLOW_UPGRADE = false;    
  }
  // ---------- interactions ----------
  BTN_REFRESH.addEventListener("click", async ()=>{
    setBusy("loading...");
    if (T_SWITCH) T_SWITCH.innerHTML = `<tr><td colspan="4" class="muted">loading...</td></tr>`;
    try{
      const ok = await loadFromState();
      if (!ok) await fetchListViaSelect();
    }catch(e){
      alert("Refresh failed: " + (e.message||e));
    }finally{
      setBusy("");
    }
  });
  // 개별 행 버튼 위임
  if (T_SWITCH){
    T_SWITCH.addEventListener("click", async (ev)=>{
      const btn = ev.target.closest(".sw-op");
      if (!btn) return;
      const op = btn.dataset.op;
      const ip = btn.dataset.ip;
      if (!op || !ip) return;
      btn.disabled = true;
      try{
        const host = (IN_HOST.value||"").trim();
        const port = Number(IN_PORT.value||19765);
        const dmpdip = sanitizeMgmt(IN_MGMT.value, host);
        await sendSwitchOp(host, port, dmpdip, op, [ip]);
        if (op === "Reboot") {
          // 모든 카메라를 모르니 안전하게 전부 래치 (또는 매핑 있으면 해당 포트/카메라만)
          Array.from(ALL_CAM_IPS).forEach(ip => { CONNECT_TOKEN.delete(ip); OFF_LATCH.add(ip); setCamStatus(ip); });
        }
      }catch(e){
        alert(`${op} failed: ` + (e?.message||e));
      }finally{
        btn.disabled = false;
      }
    });
  }

  // 제목 우측: 모든 스위치 Reboot
  if (BTN_SW_ALL_REBOOT){
    BTN_SW_ALL_REBOOT.addEventListener("click", async ()=>{
      if (!LAST_SWITCH_LIST.length) { alert("No switches."); return; }
      const ips = LAST_SWITCH_LIST.map(s=>s.ip);
      BTN_SW_ALL_REBOOT.disabled = true;
      try{
        const host = (IN_HOST.value||"").trim();
        const port = Number(IN_PORT.value||19765);
        const dmpdip = sanitizeMgmt(IN_MGMT.value, host);
        await sendSwitchOp(host, port, dmpdip, "Reboot", ips);
        Array.from(ALL_CAM_IPS).forEach(ip => { CONNECT_TOKEN.delete(ip); OFF_LATCH.add(ip); setCamStatus(ip); });
      }catch(e){
        alert("Reboot failed: " + (e?.message||e));
      }finally{
        BTN_SW_ALL_REBOOT.disabled = false;
      }
    });
  }
  SEL_ALL.addEventListener("change", ()=>{
    document.querySelectorAll(".rowSel").forEach(ch=>{ ch.checked = SEL_ALL.checked; });
  });
  BTN_RUN.addEventListener("click", ()=>{
    runConnectSequence().catch(e=>{
      setBusy("");
      alert("Camera connect sequence failed: " + (e?.message||e));
    });
  });

  // ---------- init ----------
  (function init(){
    META.textContent = `DMPDIP: ${IN_MGMT.value || "-"}`;
    if (!IN_HOST.value) IN_HOST.value = location.hostname || "127.0.0.1";
    if (!IN_MGMT.value) IN_MGMT.value = guessMgmtIP(IN_HOST.value) || IN_HOST.value;
    setPollMsg("poll -");
    BTN_REFRESH.click();
  })();
})();

