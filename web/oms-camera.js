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

  const T_LIST = document.getElementById("tblList");
  const T_CONN = document.getElementById("tblConn");
  const T_UNI  = document.getElementById("tblUnified");

  // ---------- state ----------
  let LAST_STATUS = {};  // { ip: 'connected'|'video ok'... }
  let LAST_INFO   = [];  // parsed info list
  let LAST_VIDEO  = [];  // parsed video list

  // ---------- utils ----------
  function setBusy(text){ BUSY.textContent = text || ""; }
  async function httpJson(path, init={}) {
    const res = await fetch(path, { cache:"no-store", ...init });
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
  function mapBy(arr, key){ const m={}; for(const x of arr||[]) m[x[key]]=x; return m; }

  async function mtdSend(host, port, message, timeoutSec=12){
    const payload = { host, port: Number(port)||19765, timeout: timeoutSec, message };
    const res = await httpJson("/oms/mtd-connect", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    return res?.response ?? res;
  }

  // ---------- parsers ----------
  function parse_ccd_select(resp){
    const ra = Array.isArray(resp?.ResultArray) ? resp.ResultArray : [];
    if (ra.length){
      const cameras = ra
        .map(r => ({ Index: r.cam_idx ?? r.id, IP: r.ip, CameraModel: r.model || "" }))
        .filter(c => !!c.IP);
      return { cameras };
    }
    const cams_src = resp?.Cameras || resp?.CameraList || resp?.CameraInfo || [];
    const cameras = cams_src
      .map(c=>({ Index:c.Index, IP:(c.IP||c.IPAddress), CameraModel:(c.CameraModel||c.Model||c.ModelName)||"" }))
      .filter(c=>!!c.IP);
    return { cameras };
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

  // ---------- renderers ----------
  function renderCameras(cameras){
    const rows = (cameras||[]).map(c=>{
      const id = `sel_${c.IP.replace(/\./g,'_')}`;
      return `<tr>
        <td style="text-align:center"><input type="checkbox" class="rowSel" id="${id}" data-ip="${c.IP}" checked></td>
        <td>${c.IP}</td>
        <td>${c.CameraModel||""}</td>
      </tr>`;
    }).join("") || `<tr><td colspan="3" class="muted">no cameras</td></tr>`;
    T_LIST.innerHTML = rows;
  }
  function renderConns(map){
    const keys = Object.keys(map||{});
    const rows = keys.map(ip=>`<tr><td>${ip}</td><td>${map[ip]}</td></tr>`).join("")
      || `<tr><td colspan="2" class="muted">no data</td></tr>`;
    T_CONN.innerHTML = rows;
  }
  function renderUnified(statusMap, infoList, videoList){
    const sinfo = mapBy(infoList, "IP");
    const svid  = mapBy(videoList, "IP");

    const ips = Array.from(new Set([
      ...Object.keys(statusMap||{}),
      ...Object.keys(sinfo),
      ...Object.keys(svid)
    ])).sort((a,b)=>a.localeCompare(b, undefined, {numeric:true}));

    if (ips.length === 0){
      T_UNI.innerHTML = `<tr><td colspan="13" class="muted">no data</td></tr>`;
      return;
    }

    const rows = ips.map(ip=>{
      const st = statusMap?.[ip] || "";
      const i  = sinfo[ip] || {};
      const v  = svid[ip]  || {};
      return `<tr>
        <td>${ip}</td>
        <td>${st}</td>
        <td>${i.Model || v.Model || ""}</td>
        <td>${i.FW || ""}</td>
        <td>${v.Format || ""}</td>
        <td>${v.Codec || ""}</td>
        <td>${v.Bitrate || ""}</td>
        <td>${v.GOP || ""}</td>
        <td>${i.WB || ""}</td>
        <td>${i.ISO || ""}</td>
        <td>${i.Shutter || ""}</td>
        <td>${i.Aperture || ""}</td>
        <td>${i.FocusMode || ""}</td>
      </tr>`;
    }).join("");
    T_UNI.innerHTML = rows;
  }
  function clearUnified(){ renderUnified({}, [], []); }

  // ---------- data flows ----------
  async function loadFromState(){
    try{
      const st = await httpJson("/oms/state");
      const dmpdip = st?.dmpdip || st?.DMPDIP || "";
      if (dmpdip) IN_MGMT.value = dmpdip;

      const cams = Array.isArray(st?.cameras) ? st.cameras : [];
      if (cams.length){ renderCameras(cams); return true; }
    }catch{}
    return false;
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
    const { cameras } = parse_ccd_select(r);
    renderCameras(cameras);
    return cameras;
  }

  // ---------- steps 6~9 ----------
  function getSelectedIPs(){
    const ips = [];
    document.querySelectorAll(".rowSel").forEach(ch=>{
      if (ch.checked) ips.push(ch.dataset.ip);
    });
    return ips;
  }
  function statusMapInit(ips){ const m={}; for(const ip of ips) m[ip] = "pending"; return m; }

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
    if (!ips.length){ alert("ì¹´ë©”ë¼ë¥¼ ì„ íƒí•˜ì„¸ìš”."); return; }

    LAST_STATUS = statusMapInit(ips);
    renderConns(LAST_STATUS); clearUnified();

    // 6) AddCamera
    setBusy("Adding cameras (6)...");
    try{
      await step6_addCamera(host, port, dmpdip, ips);
      ips.forEach(ip => LAST_STATUS[ip] = "added");
      renderConns(LAST_STATUS);
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
          if (ip) LAST_STATUS[ip] = (String(c.Status).toUpperCase()==="OK") ? "connected" : (c.Status || "failed");
        });
      } else {
        ips.forEach(ip => LAST_STATUS[ip] = "connected");
      }
      renderConns(LAST_STATUS);
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
      ips.forEach(ip => { if (by[ip]) LAST_STATUS[ip] = "info ok"; });
      renderConns(LAST_STATUS);
      renderUnified(LAST_STATUS, LAST_INFO, LAST_VIDEO);
    }catch{}

    // 9) GetVideoFormat
    setBusy("Fetching video formats (9)...");
    try{
      LAST_VIDEO = await step9_getVideo(host, port, dmpdip, ips);
      const byv = mapBy(LAST_VIDEO, "IP");
      ips.forEach(ip => { if (byv[ip]) LAST_STATUS[ip] = "video ok"; });
      renderConns(LAST_STATUS);
      renderUnified(LAST_STATUS, LAST_INFO, LAST_VIDEO);
    }catch{}

    setBusy("");
  }

  // ---------- interactions ----------
  BTN_REFRESH.addEventListener("click", async ()=>{
    setBusy("loading...");
    renderConns({}); clearUnified();
    try{
      const ok = await loadFromState();
      if (!ok) await fetchListViaSelect();
    }catch(e){
      alert("Refresh failed: " + (e.message||e));
    }finally{
      setBusy("");
    }
  });

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
    BTN_REFRESH.click();
  })();
})();

