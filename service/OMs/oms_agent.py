# -*- coding: utf-8 -*-
# ➊ 파일 상단에 유틸 추가 (import 아래 아무 곳)
from __future__ import annotations

import socket
import os
import http.client, sys
import json, re, time, threading, traceback

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
from collections import defaultdict, deque
from copy import deepcopy
from src.fd_communication.server_mtd_connect import tcp_json_roundtrip, MtdTraceError

# ▼▼▼ [NEW] 기본 프로세스 Alias 테이블 (oms_config.json의 process_alias로 덮어쓸 수 있음)
PROCESS_ALIAS_DEFAULT = {
    "MTd":  "Message Transport",
    "EMd":  "Enterprise Manager",
    "CCd":  "Camera Control",
    "SCd":  "Switch Control",
    "PCd":  "Processor Control",
    "GCd":  "Gimbal Control",
    "MMd":  "Multimedia Maker",
    "MMc":  "Multimedia Maker Client",    
    "AId":  "AI Daemon",
    "AIc":  "AI Client",
    "PreSd":"Pre Storage",
    "PostSd":"Post Storage",
    "VPd":"Vision Processor",
    "AMd":  "Audio Manager",
    "CMd":  "Compute Multimedia",
}
# ▲▲▲

# --- Paths ---------------------------------------------------
# V5 루트:  C:\4DReplay\V5  (기본)  — 필요시 OMS_ROOT/OMS_LOG_DIR로 오버라이드 가능
V5_ROOT = Path(os.environ.get("OMS_ROOT", Path(__file__).resolve().parents[2]))
LOGD    = Path(os.environ.get("OMS_LOG_DIR", str(V5_ROOT / "logs" / "OMS")))
LOGD.mkdir(parents=True, exist_ok=True)

STATE_FILE = LOGD / "oms_state.json"         # 연결/상태 스냅샷
VERS_FILE  = LOGD / "oms_versions.json"      # 버전 캐시(선택)
TRACE_DIR  = LOGD / "trace"                  # 개별 트레이스 파일 모음
TRACE_DIR.mkdir(parents=True, exist_ok=True)

HERE = Path(__file__).resolve()
env_root = os.environ.get("FOURD_V5_ROOT") or os.environ.get("V5_ROOT")
if env_root and Path(env_root).exists():
    ROOT = Path(env_root).resolve()
else:
    ROOT = HERE
    for i in range(1, 7):
        cand = HERE.parents[i-1]
        if (cand / "config" / "oms_config.json").exists():
            ROOT = cand
            break


# ---- MTd TCP util
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
if str(ROOT/"src") not in sys.path: sys.path.insert(0, str(ROOT/"src"))

WEB  = ROOT / "web"
CFG  = ROOT / "config" / "oms_config.json"


def _state_load():
    global STATE
    try:
        if STATE_FILE.exists():
            STATE.update(json.loads(STATE_FILE.read_text("utf-8")))
    except Exception:
        pass

def _state_save():
    try:
        STATE_FILE.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def _latest_state():
    if not STATE:
        return None, {}
    key = max(STATE.keys(), key=lambda k: STATE[k].get("updated_at", 0))
    st = STATE[key] or {}
    # presd IP 리스트(표시/선택에 활용)
    presd_ips = []
    for u in st.get("presd") or []:
        ip = (u or {}).get("IP")
        if ip: presd_ips.append(ip)
    return key, {
        "dmpdip": key,
        "connected_daemons": st.get("connected_daemons", {}),
        "versions":           st.get("versions", {}),
        "presd_versions":     st.get("presd_versions", {}),
        "presd_ips":          presd_ips,
        "daemon_map":         st.get("daemon_map", {}),
        "updated_at":         st.get("updated_at", 0),
    }

def _cidr(ip, mask_bits):
    return ".".join(str(int(octet)) for octet in ip.split(".")), mask_bits

def _same_subnet(ip1, ip2, mask_bits=24):
    a = list(map(int, ip1.split(".")))
    b = list(map(int, ip2.split(".")))
    m = [255,255,255,0] if mask_bits==24 else [255,255,255,255]  # 필요시 확장
    return all((a[i] & m[i]) == (b[i] & m[i]) for i in range(4))

def _guess_server_ip(peer_ip:str)->str:
    """peer_ip와 같은 /24에 있는 로컬 인터페이스 IP가 있으면 그걸 반환, 없으면 hostname IP"""
    try:
        # hostname 기준 1개만 써도 충분한 경우가 많음
        host_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        host_ip = "127.0.0.1"
    # 여러 NIC을 훑고 싶다면 psutil/netifaces 사용 가능. 여기선 간단히 host_ip만 비교.
    try:
        if peer_ip and _same_subnet(host_ip, peer_ip, 24):
            return host_ip
    except Exception:
        pass
    return host_ip

def _pluck_procs(status_obj):
    if not status_obj:
        return []
    if isinstance(status_obj.get("data"), dict):
        return list(status_obj["data"].values())
    if isinstance(status_obj.get("processes"), list):
        return status_obj["processes"]
    if isinstance(status_obj.get("executables"), list):
        return status_obj["executables"]
    return []

def _read_proc_snapshot(host, port, proc_name, timeout=4.0):
    """노드 /status 에서 특정 프로세스의 스냅샷(pid/uptime/start_ts/running)을 뽑아온다."""
    try:
        st,_,dat = _http_fetch(host, port, "GET", "/status", None, None, timeout=timeout)
        if st != 200:
            return {}
        js = json.loads(dat.decode("utf-8","ignore"))
        for p in _pluck_procs(js):
            if (p or {}).get("name") == proc_name:
                snap = {
                    "running": bool(p.get("running")),
                    "pid": p.get("pid") or p.get("process_id") or None,
                    "uptime": p.get("uptime") or p.get("uptime_sec") or None,
                    "start_ts": p.get("start_ts") or p.get("started_at") or None,
                }
                # 숫자형으로 정규화
                for k in ("uptime","start_ts"):
                    try:
                        if snap[k] is not None:
                            snap[k] = float(snap[k])
                    except Exception:
                        snap[k] = None
                return snap
    except Exception:
        pass
    return {}

def _is_restarted(base: dict, cur: dict, sent_at: float, saw_down: bool) -> bool:
    """
    재시작 '증거' 기반 판정:
      - cur.running 이 True 여야 함
      - 아래 중 하나라도 만족하면 OK
         * PID 변경
         * start_ts 가 sent_at 이후
         * uptime 이 뚜렷하게 리셋(예: base.uptime 이 있었고, cur.uptime < 0.5*base.uptime 또는 cur.uptime <= 5)
      - 위 지표가 전혀 없으면, POST 이후 down->up 전이가 있었는지(saw_down)로 판정
    """
    if not cur or not cur.get("running"):
        return False
    pid_changed = bool(base.get("pid") and cur.get("pid") and cur["pid"] != base["pid"])
    started_after = bool(cur.get("start_ts") and sent_at and (cur["start_ts"] >= sent_at - 0.2))
    uptime_reset = False
    if base.get("uptime") is not None and cur.get("uptime") is not None:
        try:
            # 업타임 리셋은 '이전보다 충분히 작다' + '다운→업 전이를 관찰했다' 를 함께 요구
            uptime_reset = (cur["uptime"] < 0.5 * float(base["uptime"])) and bool(saw_down)
        except Exception:
            uptime_reset = False

    # 강한 증거 우선: PID 변경 또는 start_ts 갱신
    if pid_changed or started_after:
        return True
    # 보조 증거: 업타임 리셋(단, down→up 전이가 동반된 경우에만)
    if uptime_reset:
        return True
    # 메타정보가 전혀 없으면, down→up 전이로만 인정
    meta_present = any(base.get(k) is not None for k in ("pid","start_ts","uptime")) \
                   or any(cur.get(k) is not None for k in ("pid","start_ts","uptime"))
    if not meta_present:
        return bool(saw_down)
    return False


# ─────────────────────────────────────────────────────────────
# connection state (시퀀스 결과 저장)
# ─────────────────────────────────────────────────────────────
STATE = {
    # dmpdip: {
    #   "mtd_host": "...",
    #   "mtd_port": 19765,
    #   "daemon_map": {name: ip, ...},
    #   "connected_daemons": {name: bool, ...},   # SPd↔MMd 정규화 반영
    #   "presd": [{"IP":..., "Mode":"replay", "Cameras":[...]}],
    #   "cameras": [{"Index":..,"IP":..,"CameraModel":..}, ...],
    #   "updated_at": epoch
    # }
}

def outward_name(n: str) -> str: return "SPd" if n == "MMd" else n
def inward_name(n: str) -> str:  return "MMd" if n == "SPd" else n

def _make_token() -> str:
    ts = int(time.time() * 1000)
    lt = time.localtime()
    return f"{lt.tm_hour:02d}{lt.tm_min:02d}_{ts}_{hex(ts)[-3:]}"

def _msg_mtd_connect_run(dmpdip: str, daemon_map: dict[str,str]) -> dict:
    # PreSd/PostSd/VPd는 제외, MMd→SPd
    allowed = { outward_name(k): v for k,v in daemon_map.items() if k not in {"PreSd","PostSd","VPd"} }
    return {
        "DaemonList": allowed,
        "Section1": "mtd", "Section2": "connect", "Section3": "",
        "SendState": "request", "From": "4DOMS", "To": "MTd",
        "Token": _make_token(), "Action": "run",
        "DMPDIP": dmpdip
    }

def _msg_ccd_select_get(dmpdip: str) -> dict:
    return {
        "Section1":"CCd","Section2":"Select","Section3":"",
        "SendState":"request","From":"4DOMS","To":"EMd",
        "Token":_make_token(),"Action":"get","DMPDIP":dmpdip
    }

def _msg_pcd_connect(dmpdip: str, presd_units: list[dict]) -> dict:
    return {
        "PreSd": presd_units, "PostSd": [], "VPd": [],
        "Section1":"pcd","Section2":"daemonlist","Section3":"connect",
        "SendState":"request","From":"4DOMS","To":"PCd",
        "Token":_make_token(),"Action":"set","DMPDIP":dmpdip
    }

def _msg_camera_add(dmpdip: str, cameras: list[dict]) -> dict:
    return {
        "Cameras":[{"IPAddress":c.get("IP"),"Model":c.get("CameraModel","")} for c in cameras],
        "Section1":"Camera","Section2":"Information","Section3":"AddCamera",
        "SendState":"request","From":"4DOMS","To":"CCd",
        "Token":_make_token(),"Action":"set","DMPDIP":dmpdip
    }

def _msg_camera_connect(dmpdip: str) -> dict:
    return {
        "Section1":"Camera","Section2":"Operation","Section3":"Connect",
        "SendState":"request","From":"4DOMS","To":"CCd",
        "Token":_make_token(),"Action":"run","DMPDIP":dmpdip
    }

# ─────────────────────────────────────────────────────────────
# Static helpers
# ─────────────────────────────────────────────────────────────
def _strip_json5(text:str)->str:
    text = re.sub(r"/\*.*?\*/","",text,flags=re.S)
    out=[]
    for line in text.splitlines():
        i=0;ins=False;q=None;buf=[]
        while i<len(line):
            ch=line[i]
            if ch in ("'",'"'):
                if not ins: ins=True;q=ch
                elif q==ch: ins=False;q=None
                buf.append(ch);i+=1;continue
            if not ins and i+1<len(line) and line[i:i+2]=="//": break
            buf.append(ch);i+=1
        out.append("".join(buf))
    t="\n".join(out)
    t=re.sub(r'(?m)(?<!["\w])([A-Za-z_]\w*)\s*:(?!\s*")', r'"\1":', t)
    t=re.sub(r",\s*([\]})])", r"\1", t)
    return t

def load_config(p:Path)->dict:
    txt=p.read_text(encoding="utf-8")
    return json.loads(_strip_json5(txt))

def _mime(p:Path)->str:
    s=p.suffix.lower()
    if s in (".html",".htm"): return "text/html; charset=utf-8"
    if s==".js": return "application/javascript; charset=utf-8"
    if s==".css": return "text/css; charset=utf-8"
    if s==".json": return "application/json; charset=utf-8"
    if s in (".png",".jpg",".jpeg",".gif",".svg"): return f"image/{s.lstrip('.')}"
    return "application/octet-stream"

def _http_fetch(host:str, port:int, method:str, path:str, body:bytes|None, headers:dict|None, timeout=4.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        try: conn.close()
        except: pass

def _overlay_connected(self, payload: dict) -> dict:
    try:
        nodes = payload.get("nodes") or []
        for node in nodes:
            dmpdip = node.get("host")
            st = STATE.get(dmpdip)
            s = node.get("status") or {}
            if not st: continue

            # unify
            if isinstance(s.get("data"), dict):
                procs = list(s["data"].values())
            elif isinstance(s.get("processes"), list):
                procs = s["processes"]
            elif isinstance(s.get("executables"), list):
                procs = s["executables"]
            else:
                procs = []

            for p in procs:
                name = p.get("name")
                if not name: continue
                key = inward_name(name)          # SPd → MMd 정규화
                # 연결 상태
                if st.get("connected_daemons", {}).get(key):
                    p["connection_state"] = "CONNECTED"
                # 버전 오버레이
                v = (st.get("versions") or {}).get(key)
                if v:
                    p["version"] = v.get("version")
                    p["version_date"] = v.get("date")
                # PreSd는 공용(process = PreSd) 버전 요약
                if name == "PreSd":
                    psv = st.get("presd_versions") or {}
                    if psv:
                        # 모두 동일하면 그 값, 아니면 mixed
                        vals = {(d.get("version"), d.get("date")) for d in psv.values()}
                        if len(vals) == 1:
                            vv = next(iter(vals))
                            p["version"] = vv[0]; p["version_date"] = vv[1]
                        else:
                            p["version"] = "mixed"; p["version_date"] = "-"
        return payload
    except Exception:
        return payload

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self, cfg:dict):
        self.http_host = cfg.get("http_host","0.0.0.0")
        self.http_port = int(cfg.get("http_port",52050))
        self.heartbeat = float(cfg.get("heartbeat_interval_sec",2))
        self.nodes = list(cfg.get("nodes",[]))
        # ▼▼▼ [NEW] process_alias 로드(기본값 + 설정 덮어쓰기)
        try:
            user_alias = cfg.get("process_alias") or {}
            if not isinstance(user_alias, dict): user_alias = {}
        except Exception:
            user_alias = {}
        self.process_alias = {**PROCESS_ALIAS_DEFAULT, **user_alias}
        # ▲▲▲
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cache = {}
        self._cache_ts = {}
        # DMS /config 에서 뽑아온 per-node alias 맵 캐시
        self._cache_alias = {}   # { node_name: { "PreSd": "Pre Storage [#1]", ... } }
        # ── Restart progress (single source of truth)
        self._restart = {
            "state": "idle",      # idle | running | done | error
            "total": 0,
            "sent": 0,            
            "done": 0,
            "fails": [],
            "message": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._restart_lock = threading.RLock()
        # 재가동 대기 파라미터
        self._restart_ready_timeout = 12.0   # 45s → 12s
        self._restart_poll_interval = 0.25   # 0.5s → 0.25s
        self._restart_max_workers = 8        # 동시 재시작 상한 (환경에 맞게 6~12)
        self._restart_seq = 0

    # ── restart state helpers
    def _restart_get(self):
        with self._restart_lock:
            snap = deepcopy(self._restart)
            snap["seq"] = getattr(self, "_restart_seq", 0)
            return snap
    def _restart_set(self, **kw):
        with self._restart_lock:
            self._restart.update(kw)
            self._restart["updated_at"] = time.time()
            self._restart_seq += 1
            snap = deepcopy(self._restart)
            snap["seq"] = self._restart_seq
        # PUB으로 브로드캐스트 (SSE 소비)
        try:
            self.PUB.publish("__restart__", snap)
        except Exception:
            pass

    def run(self):
        threading.Thread(target=self._loop, daemon=True).start()
        self._http_srv = ThreadingHTTPServer((self.http_host, self.http_port), self._make_handler())
        self._log(f"[OMS] HTTP {self.http_host}:{self.http_port}")
        try:
            self._http_srv.serve_forever(poll_interval=0.5)
        finally:
            try: self._http_srv.server_close()
            except: pass

    def stop(self):
        try: self._stop.set()
        except: pass
        try: self._http_srv.shutdown()
        except: pass

    def apply_runtime(self, cfg: dict):
        ch=[]
        with self._lock:
            hb=float(cfg.get("heartbeat_interval_sec", self.heartbeat))
            if hb>0 and hb!=self.heartbeat: self.heartbeat=hb; ch.append("heartbeat_interval_sec")
            if isinstance(cfg.get("nodes"), list): self.nodes=list(cfg["nodes"]); ch.append("nodes")
            # ▼▼▼ [NEW] process_alias 핫리로드
            if isinstance(cfg.get("process_alias"), dict):
                self.process_alias = {**PROCESS_ALIAS_DEFAULT, **cfg["process_alias"]}
                ch.append("process_alias")
            # ▲▲▲
        return ch

    def _poll_once(self):
        for n in self.nodes:
            name=n.get("name") or n.get("host")
            try:
                st,_,data = _http_fetch(n["host"], int(n.get("port",51050)), "GET", "/status", None, None, timeout=2.5)
                payload = json.loads(data.decode("utf-8","ignore")) if st==200 else {"ok":False,"error":f"http {st}"}
            except Exception as e:
                payload = {"ok":False,"error":repr(e)}
            # ▼ DMS /config에서 실행 항목(alias)도 끌어옴
            alias_map = {}
            try:
                st2, hdr2, dat2 = _http_fetch(n["host"], int(n.get("port",51050)), "GET", "/config", None, None, timeout=2.5)
                if st2 == 200:
                    txt = dat2.decode("utf-8","ignore")
                    cfg = json.loads(_strip_json5(txt))
                    for ex in (cfg.get("executables") or []):
                        nm = (ex or {}).get("name")
                        al = (ex or {}).get("alias")
                        if nm and al:
                            alias_map[nm] = al
            except Exception:
                alias_map = {}

            with self._lock:
                self._cache[name] = payload
                self._cache_ts[name] = time.time()
                self._cache_alias[name] = alias_map

    def _loop(self):
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.heartbeat)

    def _status_core(self):
        with self._lock:
            nodes=[]
            for n in self.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm, "alias": n.get("alias",""),
                    "host": n["host"], "port": int(n.get("port",51050)),
                    "status": self._cache.get(nm), "ts": self._cache_ts.get(nm,0)
                })
            payload = {"ok": True, "heartbeat_interval_sec": self.heartbeat, "nodes": nodes}

            # ▼ 추가: 최신 STATE 스냅샷을 extra로 싣기 (프론트 오버레이에서 사용)
            _, extra = _latest_state()
            # extra에 alias_map 주입 (DMS 화면에서 끌어다 쓰도록)
            if not extra: extra = {}
            extra["alias_map"] = getattr(self, "process_alias", {})
            payload["extra"] = extra

            return payload

    # connection_state 오버레이
    def _overlay_connected(self, payload: dict) -> dict:
        try:
            # 최신 STATE 하나만 골라 모든 노드/프로세스에 일괄 적용
            _, extra = _latest_state()
            if not extra:
                extra = {}

            conn = extra.get("connected_daemons", {})
            ver  = extra.get("versions", {})
            psv  = extra.get("presd_versions", {})

            nodes = payload.get("nodes") or []
            for node in nodes:
                # 이 노드에 해당하는 DMS-config 기반 alias 맵
                node_name = node.get("name") or node.get("host")
                per_node_alias = self._cache_alias.get(node_name, {})  # {"PreSd": "Pre Storage [#1]", ...}

                s = node.get("status") or {}
                # unify
                if isinstance(s.get("data"), dict):
                    procs = list(s["data"].values())
                elif isinstance(s.get("processes"), list):
                    procs = s["processes"]
                elif isinstance(s.get("executables"), list):
                    procs = s["executables"]
                else:
                    procs = []

                for p in procs:
                    name = p.get("name")
                    if not name:
                        continue

                    # ▼▼▼ [NEW] alias 주입(없을 때만)
                    # ▼▼▼ [NEW] alias 주입 우선순위:
                    # 1) 해당 노드 DMS /config의 alias
                    # 2) 서버 기본 PROCESS_ALIAS_DEFAULT / process_alias
                    alias = per_node_alias.get(name) or self.process_alias.get(name)
                    if alias and not p.get("alias"):
                        p["alias"] = alias
                    # ▲▲▲

                    # ---- 연결 상태 오버레이 ----
                    key = inward_name(name)  # "SPd" -> "MMd" 정규화
                    if conn.get(key):
                        # running 여부에 따라 그냥 뱃지만 결정 (STATE를 지우지 않음!)
                        if p.get("running"):
                            p["connection_state"] = "CONNECTED"
                        else:
                            p["connection_state"] = "STOPPED"

                    # SPd가 연결되면 MMc도 연결로 표기 (현장 UX)
                    if name in ("MMc",) and conn.get("MMd"):
                        p["connection_state"] = "CONNECTED"

                    # ---- 버전 오버레이 ----
                    if name == "PreSd":
                        # PreSd는 IP별 버전 → 표에서 노드 단위 한 줄이므로 요약
                        if psv:
                            vals = {(d.get("version"), d.get("date")) for d in psv.values()}
                            if len(vals) == 1:
                                vv = next(iter(vals))
                                p["version"] = vv[0] or "-"
                                p["version_date"] = vv[1] or "-"
                            else:
                                p["version"] = "mixed"
                                p["version_date"] = "-"
                    elif name == "MMd":
                        vv = ver.get("MMd")
                        if vv:
                            p["version"] = vv.get("version") or "-"
                            p["version_date"] = vv.get("date") or "-"
                    else:
                        vv = ver.get(name)
                        if vv:
                            p["version"] = vv.get("version") or "-"
                            p["version_date"] = vv.get("date") or "-"

            return payload
        except Exception:
            return payload
    # SSE (옵션)
    class _PubSub:
        def __init__(self): self._subs=defaultdict(list); self._lock=threading.RLock()
        def subscribe(self, token:str):
            q=deque()
            with self._lock: self._subs[token].append(q)
            return q
        def unsubscribe(self, token:str, q):
            with self._lock:
                lst=self._subs.get(token,[])
                try: lst.remove(q)
                except ValueError: pass
                if not lst and token in self._subs: del self._subs[token]
        def publish(self, token:str, obj):
            line=json.dumps(obj, ensure_ascii=False)
            with self._lock:
                for q in list(self._subs.get(token, ())): q.append(line)
    PUB=_PubSub()

    # HTTP
    def _make_handler(self):
        orch=self

        def _serve_static(handler, rel):
            fp=(WEB/rel.lstrip("/")).resolve()
            base=WEB.resolve()
            if not fp.is_file() or not str(fp).startswith(str(base)):
                handler.send_response(404)
                handler.send_header("Content-Type","application/json; charset=utf-8")
                handler.send_header("Cache-Control","no-store")
                b=b'{"ok":false,"error":"not found"}'
                handler.send_header("Content-Length",str(len(b))); handler.end_headers(); handler.wfile.write(b); return
            data=fp.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", _mime(fp))
            handler.send_header("Cache-Control","no-store")
            handler.send_header("Content-Length",str(len(data))); handler.end_headers(); 
            try: handler.wfile.write(data)
            except: pass

        class H(BaseHTTPRequestHandler):
            def _write(self, code=200, body=b"", ct="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                # 일반 응답도 캐시/변환 방지
                self.send_header("Cache-Control","no-cache, no-transform")
                # CORS (외부 페이지에서 진행 상황을 읽을 수 있도록)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", str(len(body)))                
                self.end_headers()
                try: self.wfile.write(body)
                except: pass

            def do_GET(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    clean = (urlsplit(self.path).path.rstrip("/") or "/")
                    if clean in {"/","/system"}: return _serve_static(self, "oms-system.html")
                    if clean in {"/command"}: return _serve_static(self, "oms-command.html")
                    if clean in {"/camera"}: return _serve_static(self, "oms-camera.html")
                    if clean in {"/liveview"}: return _serve_static(self, "oms-liveview.html")
                    
                    # proxy get
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=unquote(parts[1]); target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target: return self._write(404, b'{"ok":false,"error":"unknown node"}')
                        sub="/"+"/".join(parts[2:]) if len(parts)>2 else "/"
                        qs=urlsplit(self.path).query
                        if qs: sub=f"{sub}?{qs}"
                        st,hdr,data=_http_fetch(target["host"], int(target.get("port",51050)), "GET", sub, None, None, 4.0)
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/octet-stream"
                        return self._write(st, data, ct)

                    if parts[:1]==["web"]: return _serve_static(self, "/".join(parts[1:]))
                    if clean.endswith(".html"): return _serve_static(self, clean)

                    # ➋ do_GET 안에 라우트 추가
                    if parts == ["oms", "hostip"]:
                        qs = parse_qs(urlsplit(self.path).query)
                        peer = (qs.get("peer") or [""])[0].strip()
                        # 브라우저가 붙은 원격 주소(프록시 없다는 가정)도 힌트로 제공
                        client_ip = self.client_address[0]
                        ip = _guess_server_ip(peer or client_ip)
                        return self._write(200, json.dumps({"ok": True, "ip": ip, "client": client_ip}).encode())

                    # ---- config GET (raw + meta)
                    if parts == ["oms","config","meta"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        s = CFG.stat()
                        payload = {
                            "ok": True,
                            "path": str(CFG),
                            "size": s.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime)),
                        }
                        return self._write(200, json.dumps(payload).encode())
                    if parts == ["oms","config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")
                    # (optional) backward-compat
                    if parts == ["config","meta"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        s = CFG.stat()
                        payload = {
                            "ok": True,
                            "path": str(CFG),
                            "size": s.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime)),
                        }
                        return self._write(200, json.dumps(payload).encode())
                    if parts == ["config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")


                    if parts==["oms","status"]:
                        base = orch._status_core()
                        over = orch._overlay_connected(base)
                        return self._write(200, json.dumps(over).encode())

                    # ── Restart progress (state)
                    if parts==["oms","restart","state"]:
                        s = orch._restart_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))

                    # ── Restart progress stream (SSE)
                    if parts==["oms","restart","stream"]:
                        self.send_response(200)
                        self.send_header("Content-Type","text/event-stream; charset=utf-8")
                        # SSE는 캐시/변환/버퍼링 금지
                        self.send_header("Cache-Control","no-cache, no-transform")
                        self.send_header("Connection","keep-alive")
                        # Nginx 등 프록시 버퍼링 차단
                        self.send_header("X-Accel-Buffering","no")
                        # CORS 허용
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type")
                        self.end_headers()
                        # 구독
                        token="__restart__"
                        q = orch.PUB.subscribe(token)
                        # 최초 스냅샷
                        first = json.dumps(orch._restart_get(), ensure_ascii=False).encode("utf-8","ignore")
                        try:
                            try:
                                # 재연결 대기 시간 단축(3s 그대로 두되 keep-alive를 촘촘히)
                                self.wfile.write(b"retry: 3000\r\n")
                                self.wfile.write(b"event: progress\r\n")
                                self.wfile.write(b"data: "); self.wfile.write(first); self.wfile.write(b"\r\n\r\n")
                                try: self.wfile.flush(); 
                                except: pass
                            except: pass
                            # 푸시 루프
                            last_ka = time.time()
                            ka_interval = 2.0  # 10s -> 2s (프록시 버퍼링/idle timeout 회피)
                            while True:
                                # 새 메시지 있으면 모두 방출
                                while q:
                                    line = q.popleft().encode("utf-8","ignore")
                                    self.wfile.write(b"event: progress\r\n")
                                    self.wfile.write(b"data: "); self.wfile.write(line); self.wfile.write(b"\r\n\r\n")
                                    try: self.wfile.flush()
                                    except: pass                               
                                # keep-alive
                                if time.time() - last_ka > ka_interval:
                                    # SSE 주석(클라이언트는 무시하지만 프록시 버퍼를 비움)
                                    self.wfile.write(b":ka\r\n\r\n")
                                    last_ka = time.time()
                                    try: self.wfile.flush()
                                    except: pass
                                time.sleep(0.25)
                        except Exception:
                            # 클라이언트 끊김
                            try: self.wfile.flush()
                            except: pass
                        finally:
                            try: orch.PUB.unsubscribe(token, q)
                            except: pass
                        return

                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            def do_POST(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    length=int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)

                    # ── MTd 단건 프록시(유지)
                    if parts==["oms","mtd-connect"]:
                        req=json.loads(body.decode("utf-8","ignore"))
                        host=req.get("host"); port=int(req.get("port",19765)); msg=req.get("message") or {}
                        if not host or not isinstance(msg,dict): return self._write(400, b'{"ok":false,"error":"bad request"}')
                        try:
                            resp, tag = tcp_json_roundtrip(host, port, msg, timeout=float(req.get("timeout") or 10.0))
                            return self._write(200, json.dumps({"ok": True, "tag": tag, "response": resp}).encode())
                        except MtdTraceError as e:
                            return self._write(502, json.dumps({"ok":False,"error":str(e)}).encode())

                    # ── [API] POST /oms/connect/sequence
                    if parts == ["oms", "connect", "sequence"]:
                        try:
                            req = json.loads(body.decode("utf-8", "ignore"))
                            mtd_host = req["mtd_host"]
                            mtd_port = int(req.get("mtd_port", 19765))
                            dmpdip   = (req.get("dmpdip") or "").strip()
                            trace    = bool(req.get("trace") or (parse_qs(urlsplit(self.path).query).get("trace",[0])[0] in ("1","true","True")))
                            return_partial = bool(req.get("return_partial") or (parse_qs(urlsplit(self.path).query).get("return_partial",[0])[0] in ("1","true","True")))
                            dry_run  = bool(req.get("dry_run") or (parse_qs(urlsplit(self.path).query).get("dry_run",[0])[0] in ("1","true","True")))

                            if not dmpdip or dmpdip.startswith("127.") or dmpdip == "localhost":
                                dmpdip = _guess_server_ip(mtd_host)

                            daemon_map = req["daemon_map"]
                            events = []
                            t0 = time.time()

                            def tag(step):  # 파일과 매칭 가능한 고정 태그
                                return f"{step}_{int(t0*1000)}"

                            def add_event(step, req_msg, resp=None, error=None, used="proxy"):
                                events.append({
                                    "step": step,
                                    "used": used,   # 'proxy' or 'direct'
                                    "request": req_msg,
                                    "response": resp,
                                    "error": (None if error is None else str(error)),
                                    "t": round(time.time()-t0, 3),
                                    "trace_tag": tag(step)      # 파일명 매칭용
                                })

                            def via_mtd_connect(step, msg, wait):
                                """항상 /oms/mtd-connect 경로 사용(oms-command와 동일)"""
                                if dry_run:
                                    add_event(step, msg, {"Result":"skip","ResultCode":"DRY_RUN"}, used="proxy")
                                    return {"Result":"skip","ResultCode":"DRY_RUN"}

                                import http.client, json as _json
                                conn = http.client.HTTPConnection("127.0.0.1", orch.http_port, timeout=wait)
                                payload = _json.dumps({
                                    "host": mtd_host,
                                    "port": mtd_port,
                                    "timeout": wait,
                                    "trace_tag": tag(step),   # tcp_json_roundtrip에 전달되도록 넣음 (server_mtd_connect 에서 사용)
                                    "message": msg
                                })
                                try:
                                    conn.request("POST", "/oms/mtd-connect", body=payload, headers={"Content-Type":"application/json"})
                                    res = conn.getresponse()
                                    data = res.read()
                                    if res.status != 200:
                                        raise MtdTraceError(f"/oms/mtd-connect HTTP {res.status}", tag(step))
                                    r = _json.loads(data.decode("utf-8","ignore")).get("response")
                                    add_event(step, msg, r, used="proxy")
                                    return r
                                except Exception as e:
                                    add_event(step, msg, error=e, used="proxy")
                                    raise

                            def pkt_connect_run(dm):
                                return {
                                    "DaemonList": {("SPd" if k=="MMd" else k): v for k,v in dm.items() if k not in ("PreSd","PostSd","VPd")},
                                    "Section1":"mtd","Section2":"connect","Section3":"",
                                    "SendState":"request","From":"4DOMS","To":"MTd",
                                    "Token": _make_token(), "Action":"run","DMPDIP": dmpdip
                                }

                            # 1) EMd만
                            r1 = via_mtd_connect("step1_emd_connect", pkt_connect_run({"EMd": daemon_map["EMd"]}), wait=15.0)

                            # 2) 전체
                            r2 = via_mtd_connect("step2_all_connect", pkt_connect_run(daemon_map), wait=18.0)

                            if not dry_run:
                                time.sleep(0.8)

                            # 3) CCd Select → EMd
                            pkt3 = {
                                "Section1":"CCd","Section2":"Select","Section3":"",
                                "SendState":"request","From":"4DOMS","To":"EMd",
                                "Token": _make_token(),"Action":"get","DMPDIP": dmpdip
                            }
                            r3 = via_mtd_connect("step3_ccd_select_get", pkt3, wait=12.0)

                            # 4)~6) 기존 로직 동일 (via_mtd_connect 로 호출)
                            # ... (PCd connect set)
                            # ... (Camera Add set)
                            # ... (Camera Connect run)

                            # trace 저장
                            if trace:
                                (LOGD / f"connect_trace_{int(time.time()*1000)}.json").write_text(
                                    json.dumps({"ok":True,"events":events}, ensure_ascii=False, indent=2), encoding="utf-8"
                                )
                            return self._write(200, json.dumps({"ok": True, "events": events}).encode("utf-8"))

                        except Exception as e:
                            try:
                                if 'events' not in locals(): events = []
                                events.append({"step":"__error__", "error": repr(e)})
                                (LOGD / f"connect_trace_{int(time.time()*1000)}_ERR.json").write_text(
                                    json.dumps({"ok":False,"events":events}, ensure_ascii=False, indent=2), encoding="utf-8"
                                )
                                if return_partial:
                                    return self._write(200, json.dumps({"ok":False,"events":events,"error":repr(e)}).encode("utf-8"))
                            except: pass
                            return self._write(502, json.dumps({"ok":False, "error":repr(e)}).encode("utf-8"))
                
                    # ── CONNECT STATE CLEAR (Restart All 등에서 사용)
                    if parts==["oms","connect","clear"]:
                        try:
                            STATE.clear()
                            try: STATE_FILE.unlink(missing_ok=True)
                            except: pass
                            return self._write(200, b'{"ok":true}')
                        except Exception as e:
                            return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

                    # ── Restart progress clear (최종 메시지 수동 초기화용)
                    if parts==["oms","restart","clear"]:
                        orch._restart_set(state="idle", total=0, sent=0, done=0, fails=[], message="", started_at=0.0)
                        return self._write(200, b'{"ok":true}')

                    # ── Restart All Orchestrator (server-driven)
                    if parts==["oms","restart","all"]:
                        # 이미 수행 중이면 409
                        cur = orch._restart_get()
                        if cur.get("state") == "running":
                            return self._write(409, json.dumps({"ok":False,"error":"already_running"}).encode())

                        # 워커 스레드
                        def _worker():
                            try:
                                orch._log("[OMS] Restart worker start")
                                orch._log(f"[OMS] Restart worker nodes: {len(orch.nodes)}")
                                orch._log(f"[OMS] Restart worker cache keys: {list(orch._cache.keys())}")

                                # --- 잡 수집 ---
                                with orch._lock:
                                    nodes = []
                                    for n in orch.nodes:
                                        nm = n.get("name") or n.get("host")
                                        nodes.append({
                                            "name": nm,
                                            "host": n["host"],
                                            "port": int(n.get("port",51050)),
                                            "status": deepcopy(orch._cache.get(nm) or {})
                                        })

                                jobs = []  # [(host, port, node_name, proc_name)]
                                def _unify_procs(st):
                                    """
                                    Normalize process lists coming from DMS status structures.
                                    Accepts both {data:{}} and flat dict/list forms.
                                    """
                                    if not st:
                                        return []
                                    # most DMS send under "data"
                                    if "data" in st and isinstance(st["data"], dict):
                                        return list(st["data"].values())
                                    if "processes" in st and isinstance(st["processes"], list):
                                        return st["processes"]
                                    if "executables" in st and isinstance(st["executables"], list):
                                        return st["executables"]
                                    # if already a dict of processes
                                    if isinstance(st, dict) and all(isinstance(v, dict) for v in st.values()):
                                        return list(st.values())
                                    return []

                                for nd in nodes:
                                    # ✅ robustly extract process list from nested status.data
                                    status_obj = nd.get("status") or {}
                                    procs = _unify_procs(status_obj)
                                    for p in procs:
                                        if not p.get("name"):
                                            continue
                                        # include if selected or no explicit select flag
                                        if p.get("select", True):
                                            jobs.append((nd["host"], nd["port"], nd["name"], p["name"]))
                                
                                total = len(jobs)
                                orch._restart_set(state="running", total=total, sent=0, done=0,
                                                fails=[], message=f"Preparing…", started_at=time.time())

                                # --- Utils ---
                                def _overlay_connected_says_connected(proc_name: str) -> bool:
                                    """
                                    STATE 스냅샷(/oms/state/upsert로 누적)만 보고 '연결 OK'라고 빠르게 인정할지 판정.
                                    - SPd → MMd 로 정규화해서 조회
                                    - MMc 는 MMd 가 연결되면 연결된 것으로 간주(UX 요구)
                                    - PreSd 는 connected_daemons["PreSd"] 가 True 면 OK
                                    """
                                    try:
                                        _, extra = _latest_state()  # {"connected_daemons": {...}, "presd_ips": [...], ...}
                                        if not extra:
                                            return False
                                        conn = extra.get("connected_daemons") or {}

                                        key = inward_name(proc_name)  # "SPd" -> "MMd", 나머지는 원형
                                        # MMc는 MMd 연결되면 연결로 인정
                                        if proc_name == "MMc":
                                            return bool(conn.get("MMd"))

                                        # 일반 케이스: connected_daemons 에 동일 키가 True 면 연결로 인정
                                        return bool(conn.get(key))
                                    except Exception:
                                        return False
                                    
                                def _fetch_proc_running(h, p, proc_name)->bool:
                                    # 먼저 /status-lite 시도 → 실패 시 /status 폴백
                                    try:
                                        st,_,dat = _http_fetch(h, p, "GET", "/status-lite", None, None, timeout=2.0)
                                        if st == 200:
                                            js = json.loads(dat.decode("utf-8","ignore"))
                                            procs = js.get("executables") or []
                                            for q in procs:
                                                if (q or {}).get("name") == proc_name:
                                                    return bool(q.get("running"))
                                    except Exception:
                                        pass
                                    try:
                                        st,_,dat = _http_fetch(h, p, "GET", "/status", None, None, timeout=3.0)
                                        if st == 200:
                                            js = json.loads(dat.decode("utf-8","ignore"))
                                            procs = (list(js.get("data",{}).values()) if isinstance(js.get("data"),dict)
                                                     else js.get("processes") or js.get("executables") or [])
                                            for q in procs:
                                                if (q or {}).get("name") == proc_name:
                                                    return bool(q.get("running"))
                                    except Exception:
                                        pass
                                    return False

                                def _fmt_secs():
                                    return f"{int(time.time() - orch._restart_get().get('started_at', time.time()))}s"

                                sent = 0; done = 0; fails = []
                                # 실패는 정확한 식별을 위해 (node,proc) 튜플로 관리
                                fail_set = set()  # {(node_name, proc)}
                                fail_msgs = []    # 사람이 읽는 문자열

                                base_map = {}
                                for (host,port,node_name,proc) in jobs:
                                    base_map[(node_name,proc)] = _read_proc_snapshot(host, port, proc)

 

                                # 1) Restart each selected process via POST (in parallel)
                                from concurrent.futures import ThreadPoolExecutor, as_completed
                                def send_restart(job):
                                    host,port,node_name,proc = job
                                    st,_,_ = _http_fetch(host, port, "POST",
                                                         f"/restart/{proc}",
                                                         b"{}",
                                                         {"Content-Type":"application/json"},
                                                         timeout=6.0)
                                    if st>=400:
                                        raise RuntimeError(f"http {st}")
                                    return job

                                sent = 0
                                fails = []
                                sent_at_map = {}
                                with ThreadPoolExecutor(max_workers=orch._restart_max_workers) as ex:
                                    futs = {ex.submit(send_restart, j): j for j in jobs}
                                    for fut in as_completed(futs):
                                        try:
                                            _ = fut.result()
                                            sent += 1
                                            # 전송 직후 시각 기록
                                            host,port,node_name,proc = futs[fut]
                                            sent_at_map[(node_name,proc)] = time.time()
                                        except Exception as e:
                                            host,port,node_name,proc = futs[fut]
                                            msg = f"{node_name}/{proc}: {e}"
                                            fails.append(msg)
                                            orch._log(f"[Restart][FAIL][send] {msg}")
                                        finally:
                                            orch._restart_set(sent=sent, fails=fails,
                                                              message=f"Sent {sent}/{total} (fail {len(fails)})… waiting")
 

                                # 2) RUNNING 복귀 대기 (병렬 폴링)
                                def wait_ready(job):
                                    host,port,node_name,proc = job
                                    base = base_map.get((node_name,proc), {})
                                    sent_at = sent_at_map.get((node_name,proc)) or time.time()
                                    t0 = time.time()
                                    saw_down = False
                                    while True:
                                        cur = _read_proc_snapshot(host, port, proc, timeout=3.0)
                                        if cur.get("running") is False:
                                            saw_down = True
                                        if _is_restarted(base, cur, sent_at, saw_down):
                                            return (job, True)
                                        if time.time() - t0 > orch._restart_ready_timeout:
                                            return (job, False)
                                        time.sleep(orch._restart_poll_interval)
            
                                pending = [(h,pt,nn,pr) for (h,pt,nn,pr) in jobs
                                           if not any(f"{pr}" in f for f in fails)]
                                with ThreadPoolExecutor(max_workers=orch._restart_max_workers) as ex:
                                    futs = [ex.submit(wait_ready, j) for j in pending]
                                    for fut in as_completed(futs):
                                        job, ok = fut.result()  # wait_ready가 (job, True/False) 반환
                                        host, port, node_name, proc = job
                                        if ok:
                                            done += 1
                                        else:
                                            msg = f"{node_name}/{proc}: timeout"
                                            fails.append(msg)
                                            orch._log(f"[Restart][FAIL][wait] {msg}")
                                        orch._restart_set(
                                            done=done,
                                            fails=fails,
                                            message=f"Sent {sent}/{total} (fail {len(fails)})… waiting"
                                        )
                                # --- 3) 최종 고정 메시지 & 상태 ---
                                if fails:
                                    orch._restart_set(state="done",
                                        message=f"Restart finished: ok {done}/{total}, fail {len(fails)} · { _fmt_secs() }",
                                        fails=fails)
                                    try:
                                        (TRACE_DIR / f"restart_report_{int(time.time()*1000)}.json").write_text(
                                            json.dumps({"ok": False, "fails": fails, "ok_count": done, "total": total}, ensure_ascii=False, indent=2),
                                            encoding="utf-8"
                                        )
                                    except Exception:
                                        pass
                                else:
                                    orch._restart_set(state="done",
                                        message=f"Restart finished: ok {done}/{total}, fail 0 · { _fmt_secs() }")

                            except Exception as e:
                                orch._log(f"[OMS] Restart worker exception: {e}")                                
                                orch._log(traceback.format_exc())
                                orch._restart_set(state="error", message=f"Restart error: {e}")

                        orch._restart_set(state="running", total=0, done=0, fails=[], message="Preparing…", started_at=time.time())
                        threading.Thread(target=_worker, daemon=True).start()
                        return self._write(200, json.dumps({"ok":True}).encode())

                    # proxy post
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=parts[1]
                        target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target: return self._write(404, b'{"ok":false,"error":"unknown node"}')
                        sub="/"+"/".join(parts[2:]) or "/"
                        qs=urlsplit(self.path).query
                        if qs: sub=f"{sub}?{qs}"
                        st,hdr,data=_http_fetch(target["host"], int(target.get("port",51050)), "POST", sub, body, {"Content-Type": self.headers.get("Content-Type","application/json")})
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/json"
                        return self._write(st, data, ct)

                    # config save/apply 그대로 (생략)
                    if parts==["oms","config"]:
                        CFG.parent.mkdir(parents=True, exist_ok=True)
                        txt=body.decode("utf-8","ignore"); CFG.write_text(txt, encoding="utf-8")
                        return self._write(200, json.dumps({"ok":True,"path":str(CFG),"bytes":len(txt)}).encode())
                    if parts==["oms","config","apply"]:
                        try: cfg=load_config(CFG)
                        except Exception as e: return self._write(400, json.dumps({"ok":False,"error":f"load_config: {e}"}).encode())
                        changed=orch.apply_runtime(cfg)
                        return self._write(200, json.dumps({"ok":True,"applied":changed}).encode())
                    # ── state upsert(연결/버전/리스트 반영 & 저장)
                    if parts == ["oms", "state", "upsert"]:
                        try:
                            req = json.loads(body.decode("utf-8","ignore"))
                            dmpdip = (req.get("dmpdip") or "").strip()
                            if not dmpdip:
                                return self._write(400, b'{"ok":false,"error":"dmpdip required"}')

                            cur = STATE.setdefault(dmpdip, {
                                "connected_daemons": {}, "versions": {}, "presd_versions": {},
                                "presd": [], "cameras": [], "updated_at": time.time()
                            })

                            # 연결 상태 합치기
                            cd = req.get("connected_daemons") or {}
                            for k,v in cd.items():
                                kk = inward_name(k)  # SPd → MMd 정규화 저장
                                if v: cur["connected_daemons"][kk] = True

                            # 버전 저장
                            vs = req.get("versions") or {}
                            for k, val in vs.items():
                                kk = inward_name(k)
                                if isinstance(val, dict):
                                    cur["versions"][kk] = {"version": val.get("version","-"), "date": val.get("date","-")}

                            # PreSd 개별 버전
                            psv = req.get("presd_versions") or {}
                            for ip, val in psv.items():
                                if isinstance(val, dict):
                                    cur.setdefault("presd_versions", {})[ip] = {"version": val.get("version","-"), "date": val.get("date","-")}

                            # 목록 저장
                            if isinstance(req.get("presd"), list):   cur["presd"]   = req["presd"]
                            if isinstance(req.get("cameras"), list): cur["cameras"] = req["cameras"]

                            # 부가 정보 업데이트
                            for k in ("mtd_host","mtd_port","daemon_map"):
                                if k in req: cur[k] = req[k]
                            cur["updated_at"] = time.time()

                            _state_save()
                            return self._write(200, b'{"ok":true}')
                        except Exception as e:
                            return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            def log_message(self, fmt, *args):
                orch._log("[HTTP] " + (fmt % args))
        return H

    def _log(self, msg:str):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
        (LOGD / "OMS.log").open("a", encoding="utf-8").write(line)

# ─────────────────────────────────────────────────────────────
def main():
    try:
        cfg = load_config(CFG)
    except Exception as e:
        (LOGD / "OMS.log").open("a", encoding="utf-8").write(f"[WARN] fallback cfg: {e}\n")
        cfg = {"http_host":"0.0.0.0","http_port":52050,"heartbeat_interval_sec":2,"nodes":[]}
    _state_load()  # ← 추가
    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
