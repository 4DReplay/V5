<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>4DReplay V5 - DMs Config Editor</title>
  <meta http-equiv="Cache-Control" content="no-store" />
  <style>
    :root{
      --bg:#0b0f14; --fg:#eef2f7; --muted:#94a3b8;
      --card:#0b1220; --line:#243045;
      --btn:#1e40af; --btn2:#334155; --warn:#8b0000; --ok:#14532d;
      --th:#0f172a;
    }
    html,body{height:100%}
    body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:24px}
    h2{font-size:28px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
    h2 small{font-size:14px;opacity:.8}
    .toolbar{display:flex;gap:8px;margin:8px 0 16px;align-items:center;flex-wrap:wrap}
    .toolbar small{opacity:.8}
    button{background:var(--btn);color:#fff;border:0;border-radius:8px;padding:8px 12px;cursor:pointer}
    button:hover{opacity:.92}
    .btn-secondary{background:var(--btn2)}
    .btn-ghost{background:transparent;border:1px solid var(--line);color:#e5e7eb}
    .pill{display:inline-block;padding:2px 10px;border-radius:14px;font-size:12px;border:1px solid #374151;background:#0f172a;color:#d1d5db}
    .pill-ok{border-color:#16a34a;color:#a7f3d0;background:#0d3b1a}
    .pill-warn{border-color:#ef4444;color:#fecaca;background:#3b0d0d}
    table{border-collapse:collapse;width:100%;max-width:1280px}
    th,td{border:1px solid var(--line);padding:8px;vertical-align:middle}
    th{background:var(--th);text-align:left}
    input[type="text"]{width:100%;box-sizing:border-box;background:#0b1220;border:1px solid #243045;border-radius:6px;color:#e5e7eb;padding:6px 8px;font:13px/1.4 ui-monospace,monospace}
    input[type="checkbox"]{transform:scale(1.1)}
    .row-actions{display:flex;gap:6px}
    .hint{color:var(--muted);font-size:12px;margin-top:6px}
    .right{margin-left:auto}
  </style>
</head>
<body>
  <h2>
    <span>4DReplay V5 - DMs Config Editor</span>
    <small id="titleHost"></small>
  </h2>

  <div class="toolbar">
    <button id="btnReload" class="btn-secondary">Reload</button>
    <button id="btnSave">Save</button>
    <button id="btnFormat" class="btn-ghost" title="가능하면 서버 /config-format 또는 로컬 포맷으로 정리">Format</button>
    <span class="pill" id="saveStatus">Idle</span>
    <span class="right"></span>
    <small>File: <span id="metaPath">-</span> (<span id="metaSize">-</span> bytes, mtime <span id="metaMtime">-</span>)</small>
    <small>• Agent heartbeat: <span id="hb">-</span>s</small>
  </div>

  <table id="tbl">
    <thead>
      <tr>
        <th style="width:58px">select</th>
        <th style="width:120px">name</th>
        <th>alias</th>
        <th style="width:360px">path</th>
        <th style="width:140px">args (comma)</th>
        <th style="width:90px">auto_restart</th>
        <th style="width:90px">start_on_boot</th>
        <th style="width:120px">actions</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>

  <div class="hint">
    • 체크 해제(<code>select:false</code>)하면 Agent가 다음 tick에 해당 프로세스를 종료합니다.  
    • <code>heartbeat_interval_sec</code>는 Agent 내부 주기이며, 이 화면 폴링 간격도 자동 반영됩니다.
  </div>

<script>
/* ---------- 헤더 호스트 표기 ---------- */
(function setTitleHost(){
  try {
    const host=(window.DASHBOARD_HOST&&String(window.DASHBOARD_HOST).trim())||(location&&location.hostname)||"";
    const el=document.getElementById("titleHost"); if(el&&host) el.textContent=`(${host})`;
  } catch (_) {}
})();

/* ---------- 공통 API ---------- */
async function api(path,method="GET",body=null){
  const opt={method,cache:"no-store",headers:{}};
  if(body!==null){
    if(typeof body==="string"){ opt.headers["Content-Type"]="text/plain; charset=utf-8"; opt.body=body; }
    else { opt.headers["Content-Type"]="application/json"; opt.body=JSON.stringify(body); }
  }
  const r=await fetch(path,opt);
  const ct=r.headers.get("content-type")||"";
  const isJson=ct.includes("application/json");
  if(!r.ok){
    let msg=r.statusText;
    try{ msg=isJson?(await r.json()).error||r.statusText:await r.text(); }catch{}
    throw new Error(msg||`HTTP ${r.status}`);
  }
  return isJson?r.json():r.text();
}

/* ---------- JSON5 best-effort 클리너 (로컬 포맷 백업용) ---------- */
function cleanJson5(text){
  let t=String(text||"");
  t=t.replace(/\/\*[\s\S]*?\*\//g,""); // block comments
  t=t.split(/\r?\n/).map(line=>{
    let i=0,inStr=false,q=null,out="";
    while(i<line.length){
      const ch=line[i];
      if(ch==="\""||ch==="'"){ if(!inStr){inStr=true;q=ch;} else if(q===ch){inStr=false;q=null;} out+=ch; i++; continue; }
      if(!inStr && line[i]==="/" && line[i+1]==="/") break;
      out+=ch; i++;
    }
    return out;
  }).join("\n");
  t=t.replace(/,\s*([}\]])/g,"$1");                // trailing comma
  t=t.replace(/([{\s,])([A-Za-z_]\w*)\s*:/g,'$1"$2":'); // unquoted keys
  return t;
}

/* ---------- DOM refs ---------- */
const TBL_BODY=document.querySelector("#tbl tbody");
const SAVE_STATUS=document.getElementById("saveStatus");
const META_PATH=document.getElementById("metaPath");
const META_SIZE=document.getElementById("metaSize");
const META_MTIME=document.getElementById("metaMtime");
const HB_EL=document.getElementById("hb");
const BTN_SAVE=document.getElementById("btnSave");
const BTN_RELOAD=document.getElementById("btnReload");
const BTN_FORMAT=document.getElementById("btnFormat");

/* ---------- 상태 ---------- */
let CONFIG=null;
let POLL=null;
let POLL_MS=1500;
let BUSY=false;

/* ---------- 서버 연동 ---------- */
async function loadConfigText(){ return api("/config","GET"); }
async function saveConfigText(text){ return api("/config","POST",text); }  // 서버가 POST/PUT 중 POST 사용
async function tryServerFormat(text){
  try{
    const r=await api("/config-format","POST",text);
    if(r && typeof r.text==="string") return r.text;
  }catch{}
  return cleanJson5(text);
}
async function loadMeta(){
  try{
    const m=await api("/config/meta","GET");
    META_PATH.textContent=m.path||"-";
    META_SIZE.textContent=String(m.size ?? "-");
    META_MTIME.textContent=m.mtime||"-";
  }catch{
    META_PATH.textContent="-";
  }
}
async function loadHeartbeat(){
  try{
    const s=await api("/status","GET");
    if(s && typeof s.heartbeat_interval_sec!=="undefined"){
      const hb=Number(s.heartbeat_interval_sec);
      if(!isNaN(hb) && hb>0){
        HB_EL.textContent=hb;
        const next=Math.max(1000,Math.floor(hb*1000));
        if(next!==POLL_MS){ POLL_MS=next; restartPolling(); }
      }
    }
  }catch{ HB_EL.textContent="-"; }
}

/* ---------- 렌더 ---------- */
function row(exec){
  const tr=document.createElement("tr");
  const argsStr=Array.isArray(exec.args)?exec.args.join(","):String(exec.args||"");
  const name = exec.name || "";     // ← 안전하게 이름 뽑기
  tr.innerHTML=`
    <td style="text-align:center"><input type="checkbox" data-k="select" ${exec.select===true?"checked":""}></td>
    <td><input type="text" data-k="name" value="${exec.name||""}" readonly title="name은 고정(충돌 방지)"></td>
    <td><input type="text" data-k="alias" value="${exec.alias||""}"></td>
    <td><input type="text" data-k="path"  value="${exec.path||""}" placeholder="daemon/XXX/XXX.exe"></td>
    <td><input type="text" data-k="args"  value="${argsStr}" placeholder="arg1,arg2"></td>
    <td style="text-align:center"><input type="checkbox" data-k="auto_restart" ${exec.auto_restart!==false?"checked":""}></td>
    <td style="text-align:center"><input type="checkbox" data-k="start_on_boot" ${exec.start_on_boot===true?"checked":""}></td>
    <td class="row-actions">
      <button data-a="logs"  data-name="${exec.name}">Logs</button>
      <button class="btn-secondary" data-a="debug" data-name="${exec.name}">Debug</button>
    </td>
  `;
  tr.dataset.name=exec.name||"";
  return tr;
}
function render(){
  if(!CONFIG) return;
  const arr=Array.isArray(CONFIG.executables)?CONFIG.executables:[];
  TBL_BODY.innerHTML="";
  arr.forEach(e=>TBL_BODY.appendChild(row(e)));
}

/* ---------- 수집 / 저장 ---------- */
function collect(){
  if(!CONFIG) return null;
  const base={...CONFIG};
  const rows=[...TBL_BODY.querySelectorAll("tr")];
  const execs=[];
  rows.forEach(tr=>{
    const get=(k)=>tr.querySelector(`[data-k="${k}"]`);
    const name=get("name").value.trim();
    const alias=get("alias").value.trim();
    const path =get("path").value.trim();
    const args =get("args").value.trim();
    const select= !!get("select").checked;
    const auto_restart= !!get("auto_restart").checked;
    const start_on_boot= !!get("start_on_boot").checked;
    execs.push({
      name, alias, path,
      args: args?args.split(",").map(s=>s.trim()).filter(Boolean):[],
      select, auto_restart, start_on_boot
    });
  });
  base.executables=execs;
  return base;
}



/* ---------- 버튼 동작 ---------- */
BTN_RELOAD.addEventListener("click", async ()=>{
  if(BUSY) return;
  BUSY=true; SAVE_STATUS.textContent="Loading..."; SAVE_STATUS.className="pill";
  try{
    const txt=await loadConfigText();
    const cleaned=cleanJson5(txt);
    CONFIG=JSON.parse(cleaned);
    render();
    await Promise.allSettled([loadMeta(),loadHeartbeat()]);
    SAVE_STATUS.textContent="Loaded"; SAVE_STATUS.className="pill pill-ok";
  }catch(e){
    SAVE_STATUS.textContent="Load failed"; SAVE_STATUS.className="pill pill-warn";
    alert("Reload failed: "+(e.message||e));
  }finally{ BUSY=false; }
});

BTN_FORMAT.addEventListener("click", async ()=>{
  if(BUSY) return;
  try{
    const data=collect();
    if(!data) return;
    const pretty=JSON.stringify(data,null,2);
    const formatted=await tryServerFormat(pretty);
    // 서버 포맷 텍스트를 다시 CONFIG로 반영
    CONFIG=JSON.parse(cleanJson5(formatted));
    render();
    SAVE_STATUS.textContent="Formatted (local/server)"; SAVE_STATUS.className="pill";
  }catch(e){
    SAVE_STATUS.textContent="Format failed"; SAVE_STATUS.className="pill pill-warn";
    alert("Format failed: "+(e.message||e));
  }
});

BTN_SAVE.addEventListener("click", async ()=>{
  if(BUSY) return;
  BUSY=true; SAVE_STATUS.textContent="Saving..."; SAVE_STATUS.className="pill";
  try{
    const data=collect();
    const pretty=JSON.stringify(data,null,2);
    const toWrite=await tryServerFormat(pretty);      // 있으면 서버 포맷 사용
    await saveConfigText(toWrite);
    // 저장 후 재로드(원문 동기화)
    const reTxt=await loadConfigText();
    CONFIG=JSON.parse(cleanJson5(reTxt));
    render();
    await Promise.allSettled([loadMeta(),loadHeartbeat()]);
    SAVE_STATUS.textContent="Saved"; SAVE_STATUS.className="pill pill-ok";
  }catch(e){
    SAVE_STATUS.textContent="Save failed"; SAVE_STATUS.className="pill pill-warn";
    alert("Save failed: "+(e.message||e));
  }finally{ BUSY=false; }
});

// ===== Log Viewer =====
const LogUI = {
  modal: document.getElementById("logModal"),
  title: document.getElementById("logTitle"),
  text: document.getElementById("logText"),
  meta: document.getElementById("logMeta"),
  selDate: document.getElementById("logDate"),
  selTail: document.getElementById("logTail"),
  cbAuto: document.getElementById("logAuto"),
  btnRefresh: document.getElementById("logRefresh"),
  btnClose: document.getElementById("logClose"),
  timer: null,
  name: null,

  open(name){
    this.name = name;
    this.title.textContent = `Logs • ${name}`;
    this.modal.style.display = "flex";
    this.cbAuto.checked = true;
    this.loadDates().then(()=> this.refresh());
    this.startAuto();
  },
  close(){
    this.stopAuto();
    this.modal.style.display = "none";
    this.text.textContent = "";
    this.meta.textContent = "";
    this.name = null;
  },
  startAuto(){
    this.stopAuto();
    if(this.cbAuto.checked){
      this.timer = setInterval(()=> this.refresh(), 2000);
    }
  },
  stopAuto(){
    if(this.timer){ clearInterval(this.timer); this.timer = null; }
  },
  async loadDates(){
    this.selDate.innerHTML = "";
    try{
      const r = await api(`/logs/list/${encodeURIComponent(this.name)}`, "GET");
      const dates = (r && r.dates) || [];
      if(dates.length === 0){
        const opt = document.createElement("option");
        opt.value = ""; opt.textContent = "(no logs)";
        this.selDate.appendChild(opt);
      }else{
        dates.forEach(d=>{
          const opt = document.createElement("option");
          opt.value = d; opt.textContent = d;
          this.selDate.appendChild(opt);
        });
      }
    }catch(e){
      // ignore
    }
  },
  async refresh(){
    if(!this.name) return;
    const params = new URLSearchParams();
    const date = this.selDate.value;
    if(date) params.set("date", date);
    params.set("tail", this.selTail.value || "20000");
    try{
      const r = await api(`/logs/${encodeURIComponent(this.name)}?`+params.toString(), "GET");
      if(r && r.ok){
        this.text.textContent = r.text || "";
        this.meta.textContent = `${r.path}  •  ${r.size} bytes  •  date ${r.date}  •  tail ${r.tail} bytes`;
        // 맨 아래로 스크롤 고정
        this.text.scrollTop = this.text.scrollHeight;
      }else{
        this.text.textContent = (r && r.error) ? r.error : "(no content)";
        this.meta.textContent = "";
      }
    }catch(e){
      this.text.textContent = String(e.message || e);
      this.meta.textContent = "";
    }
  },
};

// 이벤트 바인딩
LogUI.btnRefresh.addEventListener("click", ()=> LogUI.refresh());
LogUI.btnClose.addEventListener("click", ()=> LogUI.close());
LogUI.cbAuto.addEventListener("change", ()=> LogUI.startAuto());
LogUI.selDate.addEventListener("change", ()=> LogUI.refresh());
LogUI.selTail.addEventListener("change", ()=> LogUI.refresh());

// 테이블 클릭 핸들러에 logs 분기 추가
TBL_BODY.addEventListener("click", (ev)=>{
  const b = ev.target.closest("button");
  if (!b) return;

  const act  = b.dataset.a;
  const name = b.dataset.name;
  if (!name) return;

  if (act === "logs") {
    // 같은 탭에서 로그 뷰어 페이지로 이동 (원하면 _blank 사용)
    location.href = `/web/log-viewer.html?name=${encodeURIComponent(name)}&tail=50000`;
    return;
  }

  if (act === "debug") {
    window.open(`/logs/debug/${encodeURIComponent(name)}`, "_blank");
    return;
  }
});


async function fetchLog(name, date, tail=20000){
  const n = encodeURIComponent(name);
  const params = new URLSearchParams();
  if (date) params.set("date", String(date).replaceAll(".","-").replaceAll("/","-"));
  if (tail) params.set("tail", tail);
  return api(`/logs/${n}?${params.toString()}`, "GET");
}

async function fetchLogDates(name){
  return api(`/logs/list/${encodeURIComponent(name)}`, "GET");
}

async function fetchLogDebug(name){
  return api(`/logs/debug/${encodeURIComponent(name)}`, "GET");
}

/* ---------- 폴링 ---------- */
async function poll(){
  if (BUSY) return;
  const now = Date.now();
  const jobs = [api("/status-lite","GET").then(s=>{
    // hb 변경 시 폴링 주기 자동 보정
    if (s && typeof s.heartbeat_interval_sec !== "undefined"){
      const hb = Number(s.heartbeat_interval_sec);
      if (!isNaN(hb) && hb > 0){
        HB_EL.textContent = hb;
        const next = Math.max(1000, Math.floor(hb*1000));
        if (next !== POLL_MS){ POLL_MS = next; restartPolling(); }
      }
    }
  }).catch(()=>{})];

  if (now - META_TICK > 30000){ // 30s마다 한 번만
    META_TICK = now;
    jobs.push(loadMeta().catch(()=>{}));
  }
  await Promise.allSettled(jobs);
}
function startPolling(){ if(!POLL) POLL=setInterval(poll,POLL_MS); }
function stopPolling(){ if(POLL){ clearInterval(POLL); POLL=null; } }
function restartPolling(){ stopPolling(); startPolling(); }

/* ---------- 초기화 ---------- */
(async function init(){
  try{
    const [txt] = await Promise.all([loadConfigText()]);
    CONFIG = JSON.parse(cleanJson5(txt));
    render();
    SAVE_STATUS.textContent = "Loaded"; SAVE_STATUS.className = "pill";
  }catch(e){
    SAVE_STATUS.textContent = "Load failed"; SAVE_STATUS.className = "pill pill-warn";
    alert("Initial load failed: "+(e.message||e));
  }
  await Promise.allSettled([loadMeta(), loadHeartbeatLite()]); // ← lite 사용
  startPolling();
})();


</script>
<div id="logModal" style="display:none; position:fixed; inset:5% 5% auto 5%; height:90%;
  background:var(--card); border:1px solid var(--line); border-radius:8px; padding:12px;
  z-index:9999; gap:8px; flex-direction:column;">
  <div style="display:flex; align-items:center; gap:8px;">
    <strong id="logTitle" style="font-size:16px;">Logs</strong>
    <select id="logDate" class="btn-ghost" style="padding:4px 8px;"></select>
    <select id="logTail" class="btn-ghost" style="padding:4px 8px;">
      <option value="20000">20 KB</option>
      <option value="50000" selected>50 KB</option>
      <option value="200000">200 KB</option>
      <option value="1000000">1 MB</option>
    </select>
    <label style="display:flex;align-items:center;gap:6px;"><input type="checkbox" id="logAuto" checked> auto</label>
    <button id="logRefresh" class="btn-secondary">Refresh</button>
    <span id="logMeta" class="pill" style="margin-left:auto;">-</span>
    <button id="logClose" class="btn-ghost">Close</button>
  </div>
  <pre id="logText" style="flex:1; overflow:auto; background:#0b1220; border:1px solid var(--line); border-radius:8px; margin:0; padding:10px; white-space:pre-wrap;"></pre>
</div>
</body>
</html>
