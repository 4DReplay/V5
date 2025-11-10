/* OMs Connect Sequence Runner (bundle-all + expect-batch)
 * (via /oms/mtd-connect)
 * - window.initOmsSystem(options?) to initialize
 * - options: { apiBase?: string }
 *
 * Key behaviors:
 *  1) Step 1: Connect EMd only.
 *  2) Step 2: Connect all daemons (sanitized map: exclude PreSd/PostSd/VPd/MMc, map MMd->SPd).
 *  3) Step 3: CCd Select(get) via EMd, parse to cameras + PreSd units.
 *  4) Step 4: PreSd connect(set) via PCd in ONE batched call.
 *  5) Step 5: Collect versions:
 *      - Version for each OK daemon (MMd queried via SPd) + MTd always.
 *      - PreSd versions via Expect(ips,count,wait_sec) in one token, with polling fallback and per-IP fallback.
 *  * State is upserted via /oms/state/upsert (best-effort).
 *  * Safe global logger: top-level functions can log without depending on UI initialization.
 */

/* ---------- global-safe logger (bound later by UI) ---------- */
(function installGlobalLogger() {
  if (!window.__omsAppendLog) {
    window.__omsAppendLog = function (obj) {
      try {
        if (typeof obj === "string") console.log(obj);
        else console.log("[OMS]", obj);
      } catch {}
    };
  }
})();

/* ---------- small utils ---------- */
function makeToken(){
  const d = new Date();
  const hh = String(d.getHours()).padStart(2,"0");
  const mm = String(d.getMinutes()).padStart(2,"0");
  return `${hh}${mm}_${Date.now()}_${Math.random().toString(36).slice(2,5)}`;
}
function guessMgmtIP(fallback){
  const h = (location.hostname || "").trim();
  if (!h || h === "localhost" || h === "127.0.0.1") return fallback || "127.0.0.1";
  return h;
}
// Extract IP only from node text / host field
function ipOf(node){
  // node.host may look like "10.82.104.210:19776 (8/9)"
  const s = (node?.host || node || "").toString();
  const m = s.match(/\b\d{1,3}(?:\.\d{1,3}){3}\b/);
  return m ? m[0] : s;
}

// Resolve version from a mixed structure (supports PreSd by-ip maps)
function resolveVersion(procName, nodeIp, versions){
  const ent = versions?.[procName];
  if (!ent) {
    if (procName === "PreSd" && window.__omsLastState?.presd_versions?.[nodeIp]?.version){
      return window.__omsLastState.presd_versions[nodeIp].version;
    }
    return "-";
  }
  if (procName !== "PreSd") return ent.version || "-";

  if (ent[nodeIp]?.version) return ent[nodeIp].version;
  if (ent.byIp?.[nodeIp]?.version) return ent.byIp[nodeIp].version;

  return ent.version || "-";
}

// DaemonList sanitizer: drop PreSd/PostSd/VPd/MMc; map MMd -> SPd (query target)
function sanitizeDaemonList(list){
  const out = {};
  for (const [k,v] of Object.entries(list||{})){
    if (k === "MMc" || k === "PreSd" || k === "PostSd" || k === "VPd") continue;
    if (k === "MMd") { out["SPd"] = v; continue; }
    out[k] = v;
  }
  return out;
}

function parseJsonLoose(text){
  text = String(text||"");
  // strip /* */ comments
  text = text.replace(/\/\*[\s\S]*?\*\//g, "");
  // strip // comments
  text = text.split("\n").map(line=>{
    const i=line.indexOf("//");
    return i>=0 ? line.slice(0,i) : line;
  }).join("\n");
  // remove dangling commas
  text = text.replace(/,\s*([}\]])/g, "$1");
  return JSON.parse(text);
}

/* ---------- fetch wrappers with improved error surfacing ---------- */
async function httpJson(path, init={}){
  const res = await fetch(path, { cache:"no-store", ...init });
  const ct = res.headers.get("content-type") || "";
  const isJson = ct.includes("application/json");
  if (!res.ok){
    let msg = `HTTP ${res.status}`;
    try{
      msg = isJson ? (await res.json()).error || msg : (await res.text()) || msg;
    }catch{}
    const err = new Error(msg);
    err.status = res.status;
    throw err;
  }
  return isJson ? res.json() : res.text();
}

/* ---------- packet builders ---------- */
function pkt_mtd_connect_run(dmpdip, daemonMap){
  const dm = sanitizeDaemonList(daemonMap||{});
  return {
    DaemonList: dm,
    Section1: "mtd", Section2: "connect", Section3: "",
    SendState: "request", From: "4DOMS", To: "MTd",
    Token: makeToken(), Action: "run", DMPDIP: dmpdip
  };
}
function pkt_ccd_select_get(dmpdip){
  return {
    Section1:"CCd", Section2:"Select", Section3:"",
    SendState:"request", From:"4DOMS", To:"EMd",
    Token: makeToken(), Action:"get", DMPDIP: dmpdip
  };
}
function pkt_pcd_connect_set(dmpdip, presdUnits){
  // One-shot batched connect(set) including ALL PreSd targets
  return {
    PreSd: presdUnits, PostSd: [], VPd: [],
    Section1:"pcd", Section2:"daemonlist", Section3:"connect",
    SendState:"request", From:"4DOMS", To:"PCd",
    Token: makeToken(), Action:"set", DMPDIP: dmpdip
  };
}
function pkt_camera_add_set(dmpdip, cameras){
  return {
    Cameras: cameras.map(c => ({ IPAddress: c.IP, Model: c.CameraModel || "" })),
    Section1:"Camera", Section2:"Information", Section3:"AddCamera",
    SendState:"request", From:"4DOMS", To:"CCd",
    Token: makeToken(), Action:"set", DMPDIP: dmpdip
  };
}
function pkt_camera_connect_run(dmpdip){
  return {
    Section1:"Camera", Section2:"Operation", Section3:"Connect",
    SendState:"request", From:"4DOMS", To:"CCd",
    Token: makeToken(), Action:"run", DMPDIP: dmpdip
  };
}
// Switch 모델 정보 조회 (여러 IP를 한 번에)
function pkt_switch_model_get(dmpdip, switchIps){
  const list = (switchIps||[]).filter(Boolean).map(ip => ({ ip }));
  return {
    Section1:"Switch", Section2:"Information", Section3:"Model",
    SendState:"request", From:"4DOMS", To:"SCd",
    Token: makeToken(), Action:"get", DMPDIP: dmpdip,
    Switches: list
  };
}

async function upsertState(payload){
  try{
    await httpJson("/oms/state/upsert", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
  }catch(e){
    window.__omsAppendLog({warn:"state upsert failed", detail:String(e?.message||e)});
  }
}

/* ---------- transport wrapper with payload-on-error logging ---------- */
async function mtdSend(host, port, message, timeoutSec=12, logLevel="full"){
  const t0 = performance.now();
  const payload = { host, port:Number(port)||19765, timeout: timeoutSec, message };

  if (logLevel === "full") window.__omsAppendLog({send: payload});
  let res;
  try{
    res = await httpJson("/oms/mtd-connect", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
  } catch (e){
    window.__omsAppendLog({
      mtd_connect_error: String(e?.message||e),
      status: e?.status,
      sent_summary: {
        host, port: Number(port)||19765, timeout: timeoutSec,
        message: {
          Section1: message?.Section1, Section2: message?.Section2, Section3: message?.Section3,
          To: message?.To, From: message?.From, Action: message?.Action,
          DMPDIP: message?.DMPDIP,
          HasDaemonList: !!message?.DaemonList && Object.keys(message.DaemonList).length>0
        }
      }
    });
    throw e;
  }

  const ms = Math.round(performance.now() - t0);
  const resp = res?.response ?? res;

  if (logLevel !== "none") {
    if (logLevel === "full") window.__omsAppendLog({recv: resp, elapsed_ms: ms});
    else if (logLevel === "compact") window.__omsAppendLog({ok:true, elapsed_ms: ms});
  }
  return resp;
}

/* ---------- validators ---------- */
function isValidIp(s){ return /\b\d{1,3}(?:\.\d{1,3}){3}\b/.test(String(s||"")); }
function isValidPort(p){ return Number.isInteger(p) && p>0 && p<65536; }
function ensureStep1Inputs({host, port, dmpdip, dm}){
  if (!host) throw new Error("MTd Host required");
  const p = Number(port);
  if (!isValidPort(p)) throw new Error(`Bad MTd port: ${port}`);
  if (!dm || !dm.EMd) throw new Error("DaemonMap.EMd is required for step1");
  const emdIp = ipOf(dm.EMd);
  if (!isValidIp(emdIp)) throw new Error(`Bad EMd IP: ${emdIp}`);
  if (!isValidIp(dmpdip)) throw new Error(`Bad DMPDIP: ${dmpdip}`);
}

/* ---------- version helpers ---------- */
/* ---------- version helpers (self-contained; no external builder deps) ---------- */
function buildVersionMsg(dmpdip, toName, token){
  return {
    Section1: "Daemon",
    Section2: "Information",
    Section3: "Version",
    SendState: "request",
    From: "4DOMS",
    To: toName,
    Token: token || makeToken(),
    Action: "set",
    DMPDIP: dmpdip
  };
}
async function fetchVersion(host, port, dmpdip, toName){
  const msg = buildVersionMsg(dmpdip, toName);
  const r = await mtdSend(host, port, msg, 8);
  const ver = (r && r.Version && r.Version[toName]) || {};
  return { version: ver.version || "-", date: ver.date || "-" };
}

// Per-IP fallback for PreSd
async function fetchPreSdVersionPerIp(host, port, dmpdip, ip){
  const msg = buildVersionMsg(dmpdip, "PreSd");
  msg.Expect = { ips:[ip], count: 1, wait_sec: 5 };
  const r = await mtdSend(host, port, msg, 8);
  const ver = (r && r.Version && r.Version.PreSd) || {};
  return { version: ver.version || "-", date: ver.date || "-", sender: (r && r.SenderIP) || ip };
}

// Batched collection using Expect(ips,count,wait_sec) with same Token, then token-poll loop and per-IP fallback
async function fetchPreSdVersionsBatched(host, port, dmpdip, ips, waitSec=5, hardTimeoutMs=15000){
  if (!Array.isArray(ips) || ips.length===0) return { results:{}, pending:[], errors:{}, timedOut:false };

  const msg = buildVersionMsg(dmpdip, "PreSd");
  const token = msg.Token; // fixed token for batch + polls
  msg.Expect = { ips, count: Math.min(ips.length, 1), wait_sec: waitSec };

  const pending = new Set(ips);
  const results = Object.create(null);
  const errors  = Object.create(null);

  const deadline = Date.now() + hardTimeoutMs;

  // 1) initial send with Expect
  try {
    const r = await mtdSend(host, port, msg, Math.max(8, waitSec+3), "compact");
    if (r && r.From === "PreSd" && r.Token === token){
      const ip = r.SenderIP;
      if (ip){
        const ver = (r.Version && r.Version.PreSd) || {};
        results[ip] = { version: ver.version || "-", date: ver.date || "-" };
        pending.delete(ip);
      }
      if (r.ResultCode && r.ResultCode !== 1000 && r.SenderIP){
        errors[r.SenderIP] = { ResultCode: r.ResultCode, ErrorMsg: r.ErrorMsg };
      }
    }
  } catch (e) {
    window.__omsAppendLog({ warn: "initial batch send failed", err: String(e?.message||e) });
  }

  // 2) token-poll loop (lightweight)
  const poll = buildVersionMsg(dmpdip, "PreSd", token);

  while (pending.size > 0 && Date.now() < deadline) {
    try{
      const r = await mtdSend(host, port, poll, Math.max(8, waitSec+3), "none");
      if (r && r.From === "PreSd" && r.Token === token){
        const ip = r.SenderIP;
        if (ip && pending.has(ip)){
          const ver = (r.Version && r.Version.PreSd) || {};
          results[ip] = { version: ver.version || "-", date: ver.date || "-" };
          pending.delete(ip);
          if (r.ResultCode && r.ResultCode !== 1000){
            errors[ip] = { ResultCode: r.ResultCode, ErrorMsg: r.ErrorMsg };
          }
        }
      }
    }catch(e){
      // ignore and continue polling briefly
    }
    await new Promise(res=>setTimeout(res, 200));
  }

  return { results, pending: Array.from(pending), errors, timedOut: pending.size>0 };
}

/* ---------- parser for CCd Select response ---------- */
function parse_ccd_select(resp){
  const ra = Array.isArray(resp?.ResultArray) ? resp.ResultArray : [];

  if (ra.length){
    const cameras = ra
      .map(r => ({ Index: r.cam_idx ?? r.id, IP: r.ip, CameraModel: r.model || "" }))
      .filter(c => !!c.IP);

    const grouped = {};
    const switchSet = new Set();
    for (const r of ra){
      const pid = r.PreSd_id || r.presd_id || r.PreSd || "";
      if (!pid) continue;
      if (!grouped[pid]) grouped[pid] = { IP: pid, Mode: "replay", Cameras: [] };
      grouped[pid].Cameras.push({ Index: r.cam_idx ?? r.id, IP: r.ip, CameraModel: r.model || "" });
      if (r.SCd_id) switchSet.add(r.SCd_id);
    }
    const presd_units = Object.values(grouped);
    return { cameras, presd_units, switch_ips: Array.from(switchSet) };
  }

  const cams_src = resp?.Cameras || resp?.CameraList || resp?.CameraInfo || [];
  const pres_src = resp?.PreSd || resp?.PreSdList || [];
  const cameras = cams_src
    .map(c=>({ Index:c.Index, IP:(c.IP||c.IPAddress), CameraModel:(c.CameraModel||c.Model)||"" }))
    .filter(c=>!!c.IP);
  const presd_units = [];
  const switch_ips = [];
  for (const u of pres_src){
    const ent = {
      IP: u.IP, Mode: u.Mode || "replay",
      Cameras: (u.Cameras||[]).map(cc=>({ Index: cc.Index, IP:cc.IP, CameraModel: cc.CameraModel||"" }))
    };
    if (ent.IP) presd_units.push(ent);
  }
  return { cameras, presd_units, switch_ips };
}

/* ---------- main initializer / UI binding ---------- */
window.initOmsSystem = function initOmsSystem(options){
  const apiBase = (options && options.apiBase) || "";

  const EL = {
    host:  document.getElementById("seqHost"),
    port:  document.getElementById("seqPort"),
    mgmt:  document.getElementById("seqMgmtIP"),
    dm:    document.getElementById("seqDaemonMap"),
    autoToken: document.getElementById("seqAutoToken"),
    sanitize:  document.getElementById("seqSanitize"),
    dry:       document.getElementById("seqDryRun"),
    start:     document.getElementById("btnSeqStart"),
    abort:     document.getElementById("btnSeqAbort"),
    status:    document.getElementById("seqStatus"),
    log:       document.getElementById("seqLog"),
    copy:      document.getElementById("btnCopySeqLog"),
    save:      document.getElementById("btnSaveSeqLog"),
    fillFromList: document.getElementById("btnSeqFillFromList"),
  };

  let _aborted = false;
  function setSeqStatus(text, tone=""){ EL.status.textContent = text; EL.status.className = "pill " + (tone||""); }

  // Bind UI logger to global-safe hook
  function __bindUiLogger(){
    function appendLog(obj){
      const prev = (EL.log.textContent||"").trim();
      let line;
      try {
        if (typeof obj === "string") {
          line = obj;
        } else {
          const seen = new WeakSet();
          line = JSON.stringify(obj, function(key, value){
            if (typeof value === "bigint") return value.toString();
            if (typeof value === "function") return `[Function ${value.name||"anonymous"}]`;
            if (typeof value === "object" && value !== null) {
              if (seen.has(value)) return "[Circular]";
              seen.add(value);
            }
            return value;
          }, 2);
        }
      } catch {
        line = String(obj);
      }
      EL.log.textContent = (prev && prev !== "-" ? prev+"\n" : "") + line;
      EL.log.scrollTop = EL.log.scrollHeight;
    }
    window.__omsAppendLog = appendLog;
    return appendLog;
  }
  const appendLog = __bindUiLogger();

  EL.copy.onclick = ()=> navigator.clipboard.writeText(EL.log.textContent||"");
  EL.save.onclick = ()=>{
    const blob = new Blob([EL.log.textContent||""], {type:"application/json"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `connect_seq_${Date.now()}.log`;
    a.click(); URL.revokeObjectURL(a.href);
  };

  // Auto-fill DaemonMap from /oms/status (first winners per daemon type)
  EL.fillFromList.onclick = async ()=>{
    try{
      const j = await httpJson("/oms/status");
      const nodes = Array.isArray(j.nodes) ? j.nodes : [];

      const WANT = {
        EMd: ["EMd"],
        CCd: ["CCd"],
        SCd: ["SCd"],
        GCd: ["GCd"],
        PCd: ["PCd"],
        SPd: ["SPd","MMd"],
        AId: ["AId"],
      };

      const firstWin = Object.fromEntries(Object.keys(WANT).map(k => [k, undefined]));

      const toProcList = (status)=>{
        if (!status) return [];
        if (status.data && typeof status.data === "object") return Object.values(status.data);
        if (Array.isArray(status.processes))   return status.processes;
        if (Array.isArray(status.executables)) return status.executables;
        return [];
      };

      for (const n of nodes){
        const all = toProcList(n.status);
        const procs = document.getElementById("cbOnlySelected")?.checked
          ? all.filter(p=>p && p.select === true)
          : all;

        for (const p of procs){
          if (!p || !p.name) continue;
          for (const key of Object.keys(WANT)){
            if (firstWin[key]) continue;
            if (WANT[key].includes(p.name)) {
              firstWin[key] = n.host;
            }
          }
        }
      }

      const dm = {};
      for (const k of Object.keys(firstWin)){
        if (firstWin[k]) dm[k] = firstWin[k];
      }

      EL.dm.value = JSON.stringify(dm, null, 2);
      if (dm.EMd) EL.mgmt.value = dm.EMd;

    }catch(e){
      appendLog({error:"fillFromList failed", detail:String(e?.message||e)});
    }
  };

  async function runSequence(){
    _aborted = false;
    EL.log.textContent = "-";
    setSeqStatus("Running...");

    const host = (EL.host.value||"").trim();
    const port = Number(EL.port.value||19765);
    if (!host){ setSeqStatus("No host","pill-bad"); appendLog("ERR: MTd Host required"); return; }
    if (!isValidPort(port)){ setSeqStatus("Failed","pill-bad"); appendLog({error:`Bad MTd port: ${EL.port.value}`}); return; }

    let dm;
    try{ dm = parseJsonLoose(EL.dm.value); }
    catch(e){ setSeqStatus("Bad DaemonMap","pill-bad"); appendLog("ERR: DaemonMap JSON invalid"); return; }

    if (EL.sanitize.checked) dm = sanitizeDaemonList(dm);
    const dmpdip = (EL.mgmt.value||"").trim() || guessMgmtIP(host);

    let r1 = null, r2 = null, r3 = null, r4 = null;
    let msg1 = null, msg2 = null, msg3 = null, msg4 = null;

    try{
      // Step1: EMd connect(run)
      if (_aborted) throw new Error("aborted");
      appendLog("== step1: EMd connect(run) ==");
      if (!EL.dry.checked){
        try{ ensureStep1Inputs({host, port, dmpdip, dm}); }
        catch(e){ setSeqStatus("Failed","pill-bad"); appendLog({error:String(e?.message||e)}); return; }

        msg1 = pkt_mtd_connect_run(dmpdip, { EMd: dm.EMd });
        r1 = await mtdSend(host, port, msg1, 15);
        if (Number(r1?.ResultCode) !== 1000) throw new Error("step1 failed");
      } else { appendLog({dry:"skip step1"}); }
      await upsertState({ dmpdip, connected_daemons: { MTd: true }, mtd_host: host, mtd_port: port });

      // Step2: connect(run) for all daemons
      if (_aborted) throw new Error("aborted");
      appendLog("== step2: all daemons connect(run) ==");
      if (!EL.dry.checked){
        msg2 = pkt_mtd_connect_run(dmpdip, dm);
        r2 = await mtdSend(host, port, msg2, 18);
        if (Number(r2?.ResultCode) !== 1000) throw new Error("step2 failed");
      } else { appendLog({dry:"skip step2"}); }
      if (r2 && r2.DaemonList){
        const cd = {};
        for (const [name, obj] of Object.entries(r2.DaemonList)){
          if (obj && String(obj.Status).toUpperCase()==="OK") cd[name] = true;
        }
        if (cd.SPd) { cd.MMd = true; cd.MMcs = "ALL"; }
        await upsertState({ dmpdip, connected_daemons: cd, daemon_map: dm });
      }

      // Step3: CCd Select(get) via EMd
      if (_aborted) throw new Error("aborted");
      appendLog("== step3: CCd Select(get) via EMd ==");
      let cameras = [], presd_units = [], switch_ips = [];
      if (!EL.dry.checked){
        msg3 = pkt_ccd_select_get(dmpdip);
        r3 = await mtdSend(host, port, msg3, 12);
        ({ cameras, presd_units, switch_ips } = parse_ccd_select(r3));
        appendLog({parsed: {cameras, presd_units, switch_ips}});
      } else { appendLog({dry:"skip step3 (no parsed data)"}); }
      await upsertState({ dmpdip, cameras, presd: presd_units, switch_ips });

      // Step4: PreSd connect(set) via PCd (BATched ONCE)
      if (_aborted) throw new Error("aborted");
      appendLog("== step4: PreSd connect(set) via PCd ==");
      if (!EL.dry.checked){
        msg4 = pkt_pcd_connect_set(dmpdip, presd_units);
        r4 = await mtdSend(host, port, msg4, 12);
        if (Number(r4?.ResultCode) !== 1000) appendLog("warn: step4 non-1000");
      } else { appendLog({dry:"skip step4"}); }
      const presd_ips = Array.isArray(r4?.PreSd)
        ? r4.PreSd.filter(e => Number(e?.ResultCode) === 1000).map(e => e.IP)
        : [];
      await upsertState({ dmpdip, connected_daemons: { PreSd: true }, presd_ips });

      // Step5: Version collection (only for OK daemons)
      appendLog("== step5: Versions ==");
      const versions = {}; // collect here

      // 1) Build OK set from step2 response
      const okMap = {};
      if (r2 && r2.DaemonList) {
        for (const [name, obj] of Object.entries(r2.DaemonList)) {
          if (obj && String(obj.Status).toUpperCase() === "OK") okMap[name] = true;
        }
      }
      // 2) Query targets: OK only; replace MMd -> SPd; always include MTd
      const wantSet = new Set();
      for (const name of Object.keys(okMap)) {
        if (name === "MMd") { wantSet.add("SPd"); continue; }
        wantSet.add(name);
      }
      if (dm.AId && okMap.AId) wantSet.add("AId"); // only if actually OK
      wantSet.add("MTd"); // MTd may not be in DaemonList, but query anyway
      const wantUnique = Array.from(wantSet);

      for (const toName of wantUnique){
        try{
          const v = await fetchVersion(host, port, dmpdip, toName);
          appendLog({version: {[toName]: v}});
          versions[toName === "SPd" ? "MMd" : toName] = v;
        }catch(e){ appendLog({warn:`version failed: ${toName}`, err:String(e?.message||e)}); }
      }

      // PreSd: batch via Expect, then token-poll, then per-IP fallback
      const presd_versions = {};
      if (presd_ips.length){
        for (const ip of presd_ips) presd_versions[ip] = { version: "-", date: "-" };

        const batch = await fetchPreSdVersionsBatched(host, port, dmpdip, presd_ips, /*waitSec*/3, /*hardTimeoutMs*/7000);
        for (const [ip, v] of Object.entries(batch.results)){
          appendLog({version: { PreSd: { [ip]: v }}});
          presd_versions[ip] = { version: v.version, date: v.date };
        }
        if (batch.pending.length){
          for (const ip of batch.pending){
            try{
              const sv = await fetchPreSdVersionPerIp(host, port, dmpdip, ip);
              appendLog({version: { PreSd: { [ip]: sv }}});
              presd_versions[ip] = { version: sv.version, date: sv.date };
            }catch(e){ appendLog({ warn:`version failed: PreSd ${ip}`, err:String(e?.message||e)}); }
          }
          const unresolved = Object.entries(presd_versions)
            .filter(([_,v]) => (v.version||"-") === "-")
            .map(([ip]) => ip);
          if (unresolved.length){
            appendLog({ warn: "PreSd pending after batch+fallback", pending: unresolved, errors: batch.errors, timedOut: true });
          }
        }
      }
      versions.PreSd = presd_versions;

      await upsertState({ dmpdip, versions, presd_versions });
      try { await (window.reloadNow?.() || Promise.resolve()); } catch {}
      setSeqStatus("Done","pill-ok");

    }catch(e){
      if (String(e?.message||e) === "aborted"){
        setSeqStatus("Aborted","pill-warn"); appendLog("== aborted ==");
      } else {
        setSeqStatus("Failed","pill-bad"); appendLog({error:String(e?.message||e)});
      }
    }
  }

  EL.start.onclick = ()=> runSequence();
  EL.abort.onclick = ()=>{ _aborted = true; };

  EL.host.value ||= location.hostname || "127.0.0.1";
  EL.mgmt.value ||= guessMgmtIP(EL.host.value);
};
