// ===================================================================
//  OMs Connect Sequence Runner (one-by-one via /oms/mtd-connect)
//  - window.initOmsSequence(options?) 로 초기화
//  - options: { apiBase?: string }
// ===================================================================
(function(){
  // ---------- utils ----------
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
  // DaemonList 필터: PreSd/PostSd/VPd/MMc 제외 + MMd→SPd
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
    text = text.replace(/\/\*[\s\S]*?\*\//g, "");
    text = text.split("\n").map(line=>{
      const i=line.indexOf("//");
      return i>=0 ? line.slice(0,i) : line;
    }).join("\n");
    text = text.replace(/,\s*([}\]])/g, "$1");
    return JSON.parse(text);
  }
  async function httpJson(path, init={}){
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

  // ---------- packet builders ----------
  function pkt_mtd_connect_run(dmpdip, daemonMap){
    // MMd→SPd 치환 후 필터된 맵 사용
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
  function pkt_daemon_version(dmpdip, toName){
    return {
      Section1:"Daemon", Section2:"Information", Section3:"Version",
      SendState:"request", From:"4DOMS", To: toName,
      Token: makeToken(), Action:"set", DMPDIP: dmpdip
    };
  }
  async function upsertState(payload){
    try{
      await httpJson("/oms/state/upsert", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });
    }catch(e){ appendLog({warn:"state upsert failed", detail:String(e?.message||e)}); }
  }

  // ---------- parser ----------
  // ResultArray(id, ip, model, cam_idx, PreSd_id, ...) → { cameras[], presd_units[] }
  function parse_ccd_select(resp){
    const ra = Array.isArray(resp?.ResultArray) ? resp.ResultArray : [];

    if (ra.length){
      const cameras = ra
        .map(r => ({
          Index: r.cam_idx ?? r.id,
          IP: r.ip,
          CameraModel: r.model || ""
        }))
        .filter(c => !!c.IP);

      const grouped = {};
      for (const r of ra){
        const pid = r.PreSd_id || r.presd_id || r.PreSd || "";
        if (!pid) continue;
        if (!grouped[pid]) grouped[pid] = { IP: pid, Mode: "replay", Cameras: [] };
        grouped[pid].Cameras.push({
          Index: r.cam_idx ?? r.id,
          IP: r.ip,
          CameraModel: r.model || ""
        });
      }
      const presd_units = Object.values(grouped);
      return { cameras, presd_units };
    }

    // 구 포맷 호환
    const cams_src = resp?.Cameras || resp?.CameraList || resp?.CameraInfo || [];
    const pres_src = resp?.PreSd || resp?.PreSdList || [];
    const cameras = cams_src
      .map(c=>({ Index:c.Index, IP:(c.IP||c.IPAddress), CameraModel:(c.CameraModel||c.Model)||"" }))
      .filter(c=>!!c.IP);
    const presd_units = [];
    for (const u of pres_src){
      const ent = {
        IP: u.IP, Mode: u.Mode || "replay",
        Cameras: (u.Cameras||[]).map(cc=>({ Index: cc.Index, IP:cc.IP, CameraModel: cc.CameraModel||"" }))
      };
      if (ent.IP) presd_units.push(ent);
    }
    return { cameras, presd_units };
  }

  // ---------- main initializer ----------
  window.initOmsSequence = function initOmsSequence(options){
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
    function appendLog(obj){
      const prev = (EL.log.textContent||"").trim();
      const line = (typeof obj === "string") ? obj : JSON.stringify(obj, null, 2);
      EL.log.textContent = (prev && prev !== "-" ? prev+"\n" : "") + line;
      EL.log.scrollTop = EL.log.scrollHeight;
    }
    EL.copy.onclick = ()=> navigator.clipboard.writeText(EL.log.textContent||"");
    EL.save.onclick = ()=>{
      const blob = new Blob([EL.log.textContent||""], {type:"application/json"});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = `connect_seq_${Date.now()}.log`;
      a.click(); URL.revokeObjectURL(a.href);
    };

    // 리스트에서 현재 화면의 프로세스 → DaemonMap 채우기
    EL.fillFromList.onclick = async ()=>{
      try{
        const j = await httpJson("/oms/status");
        const nodes = Array.isArray(j.nodes) ? j.nodes : [];

        // 원하는 타겟들(키: 표시키, 값: 허용 프로세스 이름들)
        const WANT = {
          EMd: ["EMd"],
          CCd: ["CCd"],
          SCd: ["SCd"],
          GCd: ["GCd"],
          PCd: ["PCd"],
          SPd: ["SPd","MMd"], // MMd도 허용 → SPd로 사용
          AId: ["AId"],
        };

        const firstWin = Object.fromEntries(Object.keys(WANT).map(k => [k, undefined]));

        // status → 프로세스 배열 뽑기
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
              if (firstWin[key]) continue;               // 이미 채워졌으면 패스
              if (WANT[key].includes(p.name)) {
                firstWin[key] = n.host;                  // 첫 매칭 우선
              }
            }
          }
        }

        // 결과 JSON 구성(빈 값은 제외)
        const dm = {};
        for (const k of Object.keys(firstWin)){
          if (firstWin[k]) dm[k] = firstWin[k];
        }

        // MMd → SPd 치환은 여기서 이미 반영됨(WANT에서 처리)
        // 텍스트 박스 채우기
        EL.dm.value = JSON.stringify(dm, null, 2);

        // DMPDIP 기본값: EMd IP가 있으면 그걸로
        if (dm.EMd) EL.mgmt.value = dm.EMd;

      }catch(e){
        appendLog({error:"fillFromList failed", detail:String(e?.message||e)});
      }
    };

    async function mtdSend(host, port, message, timeoutSec=12){
      const t0 = performance.now();
      const payload = { host, port:Number(port)||19765, timeout: timeoutSec, message };
      appendLog({send: payload});
      const res = await httpJson(apiBase + "/oms/mtd-connect", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(payload),
      });
      const ms = Math.round(performance.now() - t0);
      const resp = res?.response ?? res;
      appendLog({recv: resp, elapsed_ms: ms});
      return resp;
    }

    async function fetchVersion(host, port, dmpdip, toName){
      const msg = pkt_daemon_version(dmpdip, toName);
      const r = await mtdSend(host, port, msg, 8);
      const ver = (r && r.Version && r.Version[toName]) || {};
      return { version: ver.version || "-", date: ver.date || "-" };
    }
    async function fetchPreSdVersionPerIp(host, port, dmpdip, ip){
      // 단일 IP 기준(가장 확실). 필요 시 Expect 사용으로 변경 가능.
      const msg = pkt_daemon_version(dmpdip, "PreSd");
      msg.Expect = { ips:[ip], count: 1, wait_sec: 5 };
      const r = await mtdSend(host, port, msg, 8);
      const ver = (r && r.Version && r.Version.PreSd) || {};
      // 일부 구현체는 SenderIP로 구분
      return { version: ver.version || "-", date: ver.date || "-", sender: (r && r.SenderIP) || ip };
    }

    async function runSequence(){
      _aborted = false;
      EL.log.textContent = "-";
      setSeqStatus("Running...");

      const host = (EL.host.value||"").trim();
      const port = Number(EL.port.value||19765);
      if (!host){ setSeqStatus("No host","pill-bad"); appendLog("ERR: MTd Host required"); return; }

      let dm;
      try{ dm = parseJsonLoose(EL.dm.value); }
      catch(e){ setSeqStatus("Bad DaemonMap","pill-bad"); appendLog("ERR: DaemonMap JSON invalid"); return; }

      // 허용된 데몬만 남기고 MMd→SPd 치환
      if (EL.sanitize.checked) dm = sanitizeDaemonList(dm);
      const dmpdip = (EL.mgmt.value||"").trim() || guessMgmtIP(host);

      // ▼ 블록 밖에서 쓸 응답들을 미리 선언 (호이스팅 용)
      let r1 = null, r2 = null, r3 = null, r4 = null, r5 = null;
      let msg1 = null, msg2 = null, msg3 = null, msg4 = null, msg5 = null;

      try{
        // Step1: EMd만 run
        if (_aborted) throw new Error("aborted");
        appendLog("== step1: EMd connect(run) ==");
        if (!EL.dry.checked){
          msg1 = pkt_mtd_connect_run(dmpdip, { EMd: dm.EMd });
          if (!EL.autoToken.checked) msg1.Token = msg1.Token || "";
          r1 = await mtdSend(host, port, msg1, 15);
          if (Number(r1?.ResultCode) !== 1000) throw new Error("step1 failed");
        } else {
          appendLog({dry:"skip step1"});
        }
        // step1 OK 후
        await upsertState({
          dmpdip, connected_daemons: { MTd: true },
          mtd_host: host, mtd_port: port
        });

        // Step2: 전체 run (MMd→SPd 치환 포함)
        if (_aborted) throw new Error("aborted");
        appendLog("== step2: all daemons connect(run) ==");
        if (!EL.dry.checked){
          msg2 = pkt_mtd_connect_run(dmpdip, dm);
          if (!EL.autoToken.checked) msg2.Token = msg2.Token || "";
          r2 = await mtdSend(host, port, msg2, 18);
          if (Number(r2?.ResultCode) !== 1000) throw new Error("step2 failed");
        } else {
          appendLog({dry:"skip step2"});
        }
        // step2 OK 후 - 응답의 DaemonList 반영
        if (r2 && r2.DaemonList){
          const cd = {};
          for (const [name, obj] of Object.entries(r2.DaemonList)){
            if (obj && String(obj.Status).toUpperCase()==="OK") cd[name] = true;
          }
          // SPd OK → MMd / MMc 전체 CONNECTED 처리
          if (cd.SPd) {
            cd.MMd = true;            // MMd 표시 갱신
            cd.MMcs = "ALL";          // 서버가 MMc 다수일 때 일괄 처리하도록 힌트(문자열/플래그)
          }
          await upsertState({ dmpdip, connected_daemons: cd, daemon_map: dm });
        }

        // Step3: CCd Select → EMd(get)
        if (_aborted) throw new Error("aborted");
        appendLog("== step3: CCd Select(get) via EMd ==");
        let cameras = [], presd_units = [];
        if (!EL.dry.checked){
          msg3 = pkt_ccd_select_get(dmpdip);
          if (!EL.autoToken.checked) msg3.Token = msg3.Token || "";
          r3 = await mtdSend(host, port, msg3, 12);
          ({ cameras, presd_units } = parse_ccd_select(r3));
          appendLog({parsed: {cameras, presd_units}});
        } else {
          appendLog({dry:"skip step3 (no parsed data)"});
        }
        // step3 파싱 후 목록 저장
        await upsertState({ dmpdip, cameras, presd: presd_units });

        // Step4: PreSd connect(set) → PCd
        if (_aborted) throw new Error("aborted");
        appendLog("== step4: PreSd connect(set) via PCd ==");
        if (!EL.dry.checked){
          msg4 = pkt_pcd_connect_set(dmpdip, presd_units);
          if (!EL.autoToken.checked) msg4.Token = msg4.Token || "";
          r4 = await mtdSend(host, port, msg4, 12);
          if (Number(r4?.ResultCode) !== 1000) appendLog("warn: step4 non-1000");
        } else {
          appendLog({dry:"skip step4"});
        }
        // step4 OK 후 PreSd 연결 true
        const presd_ips = (Array.isArray(presd_units) ? presd_units : [])
          .map(u => u && u.IP).filter(Boolean);
        await upsertState({
          dmpdip,
          connected_daemons: { PreSd: true },
          presd_ips
        });

        // step5 버전 수집
        appendLog("== step5: Versions ==");
        const versions = {};
        // (리스트에 있는 것만 요청)
        const want = [];
        if (dm.EMd) want.push("EMd");
        if (dm.MTd || true) want.push("MTd");   // MTd는 화면에 항상 있으니 요청
        if (dm.CCd) want.push("CCd");
        if (dm.SCd) want.push("SCd");
        if (dm.PCd) want.push("PCd");
        if (dm.GCd) want.push("GCd");
        if (dm.SPd || dm.MMd) want.push("SPd"); // MMd는 SPd로 요청

        for (const toName of want){
          try{
            const v = await fetchVersion(host, port, dmpdip, toName);
            appendLog({version: {[toName]: v}});
            // 저장 시 MMd/ SPd 정규화는 서버에서 처리 → 여기선 보낸 이름 그대로
            versions[toName === "SPd" ? "MMd" : toName] = v;
          }catch(e){
            appendLog({warn:`version failed: ${toName}`, err:String(e?.message||e)});
          }
        }

        // PreSd(IP별)
        const presd_versions = {};
        for (const u of presd_units){
          const ip = u && u.IP;
          if (!ip) continue;
          try{
            const v = await fetchPreSdVersionPerIp(host, port, dmpdip, ip);
            appendLog({version: {PreSd: {[ip]: v}}});
            presd_versions[ip] = {version: v.version, date: v.date};
          }catch(e){
            appendLog({warn:`version failed: PreSd ${ip}`, err:String(e?.message||e)});
          }
        }

        // 서버에 버전 업서트
        await upsertState({ dmpdip, versions, presd_versions });
        // 화면 즉시 반영
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

    // 기본값 세팅
    EL.host.value ||= location.hostname || "127.0.0.1";
    EL.mgmt.value ||= guessMgmtIP(EL.host.value);
  };
})();

