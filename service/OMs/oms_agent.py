# -*- coding: utf-8 -*-
# ➊ 파일 상단에 유틸 추가 (import 아래 아무 곳)
from __future__ import annotations

import socket
import os
import http.client, sys
import json, re, time, threading, traceback
import subprocess
import errno

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
from collections import defaultdict, deque
from copy import deepcopy
from src.fd_communication.server_mtd_connect import tcp_json_roundtrip, MtdTraceError

###################################################################################
# debug
# logging.debug
# powershell -> Get-Content "C:\4DReplay\V5\logs\OMs\server.log" -Wait -Tail 20
# logging.debug(f"{value} message")
###################################################################################
import logging
logfile = r"C:\4DReplay\V5\logs\OMs\server.log"
os.makedirs(os.path.dirname(logfile), exist_ok=True)
logger = logging.getLogger()  # root logger
logger.setLevel(logging.DEBUG)
# 핸들러 중복 방지
if not logger.handlers:
    fh = logging.FileHandler(logfile, encoding='utf-8')
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
logging.debug("🔥 Logging now works - test message")



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
def _tagged(scope: str, mode: str, msg: str | None) -> str:
    s = str(msg or "").strip()
    if not s:
        return s
    prefix = f"[{scope}][{mode}]"
    if s.startswith(prefix):
        return s
    return f"{prefix} {s}"

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

# --- hard-coded timeouts ---
RESTART_POST_TIMEOUT = 30.0
STATUS_FETCH_TIMEOUT = 10.0

# ─────────────────────────────────────────────────────────────
# state
# ─────────────────────────────────────────────────────────────

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
# ping
# ─────────────────────────────────────────────────────────────
def _tcp_probe(ip: str, port: int = 554, timeout_sec: float = 1.0) -> bool | None:
    """
    TCP 연결 시도. 성공이면 True.
    연결거부(ECONNREFUSED)는 '호스트는 살아있음'으로 보고 True.
    타임아웃/네트워크 에러면 False.
    파라미터 이상/기타 예외 시 None.
    """
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout_sec):
            return True
    except socket.timeout:
        return False
    except OSError as e:
        # 연결 거부면 IP는 살아있다고 간주
        if isinstance(e, ConnectionRefusedError) or getattr(e, "errno", None) == errno.ECONNREFUSED:
            return True
        # 즉시 도달 불가/라우팅 없음 등은 False
        if getattr(e, "errno", None) in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EHOSTDOWN):
            return False
        # 기타는 판단 불가
        return None
    except Exception:
        return None
def _icmp_ping(ip: str, timeout_sec: float = 1.0) -> bool | None:
    """
    시스템 ping 사용. 성공시 True, 실패시 False, 예외시 None
    Windows/Unix 모두 지원.
    """
    try:
        is_win = os.name == "nt"
        if is_win:
            # -n 1(1회), -w timeout(ms)
            cmd = ["ping", "-n", "1", "-w", str(int(timeout_sec * 1000)), ip]
        else:
            # -c 1(1회), -W timeout(s)
            cmd = ["ping", "-c", "1", "-W", str(int(timeout_sec)), ip]
        ret = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout_sec + 0.5
        ).returncode
        return ret == 0
    except Exception:
        return None
def _ping_check(ip: str, method: str = "auto", port: int = 554, timeout_sec: float = 1.0) -> tuple[bool | None, str]:
    """
    method: 'tcp' | 'icmp' | 'auto'
    반환: (alive, used_method)
      alive: True/False/None(None은 판단불가)
    """
    m = (method or "auto").lower()
    if m == "tcp":
        return _tcp_probe(ip, port, timeout_sec), "tcp"
    if m == "icmp":
        return _icmp_ping(ip, timeout_sec), "icmp"

    # auto: TCP 우선 → 판단불가면 ICMP 시도
    a = _tcp_probe(ip, port, timeout_sec)
    if a is not None:
        return a, "tcp"
    return _icmp_ping(ip, timeout_sec), "icmp"

# ─────────────────────────────────────────────────────────────
# connection state 
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
def _clear_connect_state() -> bool:
    """
    CONNECTED 스냅샷을 메모리/디스크 모두 초기화.
    - STATE.clear()
    - STATE_FILE 삭제
    실패해도 예외는 바깥으로 올리지 않고 False 반환.
    """
    try:
        STATE.clear()
        try:
            STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    except Exception:
        return False
def append_mtd_debug(direction, host, port, message=None, response=None, error=None, tag=None):
    """
    direction: 'send' | 'recv' | 'error'
    각 /oms/mtd-connect 호출마다 JSONL 한 줄씩 기록.
    """
    try:
        TRACE_DIR.mkdir(parents=True, exist_ok=True)
        fn = TRACE_DIR / f"mtd_debug_{time.strftime('%Y%m%d%H%M')}.json"
        rec = {
            "ts": time.time(),
            "dir": direction,
            "host": host,
            "port": int(port) if port is not None else None,
            "tag": tag,
            "message": message,
            "response": response,
            "error": (str(error) if error is not None else None),
        }
        with fn.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # 로깅 실패가 서비스에 영향 주지 않도록 무시
        pass

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
def _state_for_host(host: str) -> dict:
    # --- NEW (module-level): pick best-matching STATE for a node host
    """노드 host와 가장 그럴듯한 STATE 항목을 선택한다.
       우선순위: (1) 키 == host  (2) daemon_map 값 중 host 포함  (3) updated_at 최신
    """
    best = None
    best_ts = -1.0
    for key, st in STATE.items():
        if not isinstance(st, dict):
            continue
        ts = float(st.get("updated_at") or 0.0)
        if key == host and ts >= best_ts:
            best, best_ts = st, ts
            continue
        dm = st.get("daemon_map") or {}
        try:
            vals = list(dm.values()) if isinstance(dm, dict) else []
        except Exception:
            vals = []
        if host in vals and ts >= best_ts:
            best, best_ts = st, ts
    return best or {}

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self, cfg:dict):
        self.http_host = cfg.get("http_host","0.0.0.0")
        self.http_port = int(cfg.get("http_port",19777))
        self.heartbeat = float(cfg.get("heartbeat_interval_sec",2))
        self.nodes = list(cfg.get("nodes",[]))
        self.state = {}
        self._config = cfg          # 또는 self.config = cfg 도 같이 써도 됨        
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
        # 개별 HTTP 요청 타임아웃 (하드코딩)
        self._restart_post_timeout = RESTART_POST_TIMEOUT
        self._status_fetch_timeout = STATUS_FETCH_TIMEOUT
        # 나머지 값들도 원하시면 고정:
        self._restart_ready_timeout = 40.0
        self._restart_settle_sec    = 20.0      # 타임아웃 뒤 사후 검증 기간
        self._restart_verify_iv     = 0.5       # settle 동안 상태 확인 주기 (초)
        self._restart_poll_interval = 0.25
        self._restart_max_workers = 8
        self._restart_min_prepare_ms = 300
        self._restart_seq = 0
        

        # ── Connect progress (single source of truth)
        self._connect = {
            "state": "idle",      # idle | running | done | error
            "message": "",
            "events": [],         # 최근 실행의 이벤트 요약 (원하면 유지 길이 제한)
            "started_at": 0.0,
            "updated_at": 0.0,
            "seq": 0,
        }
        self._connect_lock = threading.RLock()

    # ── restart state helpers
    def _restart_get(self):
        with self._restart_lock:
            snap = deepcopy(self._restart)
            snap["seq"] = getattr(self, "_restart_seq", 0)
            return snap
    def _restart_set(self, **kw):
        # message가 있으면 여기서 prefix를 붙인다.
        if "message" in kw:
            kw["message"] = _tagged("system", "restart", kw["message"])


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

    @staticmethod
    def _all_targets_running(current_status, targets):
        """
        Return (running_set, stopped_set) of target process fullnames that are RUNNING.
        `targets` is a set of strings like 'DMS-1/MMd'.
        current_status is the dict returned by /oms/status handler.
        """
        running, stopped = set(), set()
        nodes = current_status.get("nodes", [])
        by_key = {}
        for n in nodes:
            node = n.get("name") or n.get("alias") or ""
            s = n.get("status") or {}
            # unify to list
            if isinstance(s.get("data"), dict):
                procs = list(s["data"].values())
            elif isinstance(s.get("processes"), list):
                procs = s["processes"]
            elif isinstance(s.get("executables"), list):
                procs = s["executables"]
            else:
                procs = []
            for p in procs:
                pname = p.get("name") or p.get("proc") or p.get("id") or ""
                if not pname: continue
                key = f"{node}/{pname}"
                is_run = bool(p.get("running")) or str(p.get("status","")).lower() in ("running","started")
                (running if is_run else stopped).add(key)
                by_key[key] = p
        # intersect with targets only
        return running & targets, stopped & targets
    def _connect_get(self):
        with self._connect_lock:
            snap = deepcopy(self._connect)
            return snap
    def _connect_set(self, **kw):
        # connect 진척 메시지에는 [system][connect] prefix를 자동 부여
        if "message" in kw:
            kw["message"] = _tagged("system", "connect", kw.get("message"))
        
        with self._connect_lock:
            self._connect.update(kw)
            self._connect["updated_at"] = time.time()
            self._connect["seq"] = (self._connect.get("seq") or 0) + 1
            snap = deepcopy(self._connect)
        
        try:
            self.PUB.publish("__connect__", snap)  # SSE 브로드캐스트
        except Exception:
            pass
    def _poll_once(self):
        for n in self.nodes:
            name=n.get("name") or n.get("host")
            try:
                st,_,data = _http_fetch(n["host"], int(n.get("port",19776)), "GET", "/status", None, None, timeout=2.5)
                payload = json.loads(data.decode("utf-8","ignore")) if st==200 else {"ok":False,"error":f"http {st}"}
            except Exception as e:
                payload = {"ok":False,"error":repr(e)}
            # ▼ DMS /config에서 실행 항목(alias)도 끌어옴
            alias_map = None
            try:
                st2, hdr2, dat2 = _http_fetch(n["host"], int(n.get("port",19776)), "GET", "/config", None, None, timeout=self._status_fetch_timeout)
                if st2 == 200:
                    txt = dat2.decode("utf-8","ignore")
                    cfg = json.loads(_strip_json5(txt))
                    tmp = {}
                    for ex in (cfg.get("executables") or []):
                        nm = (ex or {}).get("name"); al = (ex or {}).get("alias")
                        if nm and al is not None:        # None/빈 문자열도 수용
                            if al:                        # 비어있지 않으면 매핑
                                tmp[nm] = al
                            # 비어있으면 alias를 제거 의도로 해석 → tmp에 넣지 않음
                    alias_map = tmp  # HTTP 200이면 빈 dict라도 최신 결과로 간주
                else:
                    alias_map = None  # 실패로 취급 → 캐시 유지
            except Exception:
                alias_map = None      # 실패로 취급 → 캐시 유지

            with self._lock:
                self._cache[name] = payload
                self._cache_ts[name] = time.time()
                # ⬇️ 핵심: 200 OK였다면 빈 dict라도 캐시 반영(= 제거 반영)
                if alias_map is not None:
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
                    "host": n["host"], "port": int(n.get("port",19776)),
                    "status": self._cache.get(nm), "ts": self._cache_ts.get(nm,0)
                })
            payload = {"ok": True, "heartbeat_interval_sec": self.heartbeat, "nodes": nodes}

            key, extra = _latest_state()
            if not extra:
                extra = {}
            # alias map (기존)
            extra["alias_map"] = getattr(self, "process_alias", {})
            # ▶ 카메라 패널이 /oms/status만 봐도 동작하도록 카메라 관련 필드도 포함
            st = {}
            try:
                if key:
                    st = STATE.get(key, {}) or {}
            except Exception:
                st = {}
            # GET /oms/state 와 동일 키들 보존
            extra["cameras"]        = st.get("cameras") or []
            extra["connected_ips"]  = st.get("connected_ips") or st.get("connected_camera_ips") or []
            extra["camera_status"]  = st.get("camera_status") or {}
            extra["presd"]          = st.get("presd") or []
            # updated_at은 _latest_state가 이미 줌
            payload["extra"] = extra
            # 행 오버레이는 전역이 아니라 노드별로 보도록 힌트 제공(선택)
            state_by_host = {}
            for n in self.nodes:
                h = n.get("host")
                st = _state_for_host(h)
                state_by_host[h] = {
                    "connected_daemons": st.get("connected_daemons", {}),
                    "versions": st.get("versions", {}),
                    "presd_versions": st.get("presd_versions", {}),
                    "updated_at": st.get("updated_at", 0),
                }
            payload["state_by_host"] = state_by_host

            return payload
    def _overlay_connected(self, payload: dict) -> dict:
        try:
            nodes = payload.get("nodes") or []

            total_selected = 0
            total_connected = 0
            total_running = 0
            total_stopped = 0

            def _ip_of(s: str) -> str:
                m = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", str(s or ""))
                return m.group(0) if m else str(s or "")

            for node in nodes:
                host = node.get("host")
                node_ip = _ip_of(host)
                # 이 노드에 가장 잘 맞는 STATE 스냅샷
                st = _state_for_host(host)
                conn = st.get("connected_daemons", {}) or {}
                ver  = st.get("versions", {}) or {}
                psv  = st.get("presd_versions", {}) or {}
                # PreSd 연결 허용 IP 목록 (STATE.presd[].IP)
                presd_ips = set()
                try:
                    for u in st.get("presd") or []:
                        ip = (u or {}).get("IP")
                        if ip:
                            presd_ips.add(ip)
                except Exception:
                    pass

                node_name = node.get("name") or node.get("host")
                per_node_alias = self._cache_alias.get(node_name, {})  # {"PreSd": "...", ...}

                s = node.get("status") or {}
                if isinstance(s.get("data"), dict):
                    procs = list(s["data"].values())
                elif isinstance(s.get("processes"), list):
                    procs = s["processes"]
                elif isinstance(s.get("executables"), list):
                    procs = s["executables"]
                else:
                    procs = []

                # ── 1) 오버레이(별칭/버전/CONNECTED 판정)
                for p in procs:
                    if not isinstance(p, dict): 
                        continue
                    name = p.get("name")
                    if not name:
                        continue

                    # alias: DMS /config → 서버 기본 alias 순
                    alias = per_node_alias.get(name) or self.process_alias.get(name)
                    if alias and not p.get("alias"):
                        p["alias"] = alias

                    # 버전 오버레이
                    if name == "PreSd":
                        if psv:
                            vals = {(d.get("version"), d.get("date")) for d in psv.values()}
                            if len(vals) == 1:
                                vv = next(iter(vals))
                                p["version"] = vv[0] or "-"
                                p["version_date"] = vv[1] or "-"
                            else:
                                p["version"] = "mixed"; p["version_date"] = "-"
                    else:
                        vv = ver.get(name if name != "SPd" else "MMd") or {}
                        if vv:
                            p["version"] = vv.get("version") or "-"
                            p["version_date"] = vv.get("date") or "-"

                    # CONNECTED 판정(상호배타 집계에 쓸 내부 플래그)
                    key = inward_name(name)  # "SPd" -> "MMd" 정규화
                    # 기본: connected_daemons 에 True면 CONNECTED
                    if conn.get(key):
                        p["connection_state"] = "CONNECTED" if p.get("running") else "STOPPED"
                    # MMc는 MMd 연결 시 CONNECTED 취급
                    if name == "MMc" and conn.get("MMd"):
                        p["connection_state"] = "CONNECTED"
                    # PreSd는 presd_ips에 '이 노드 host의 IP'가 있을 때만 CONNECTED
                    if name == "PreSd" and conn.get("PreSd"):
                        try:
                            node_ip = (node.get("host") or "").split(":")[0]
                        except Exception:
                            node_ip = node.get("host")
                        if node_ip in presd_ips:
                            p["connection_state"] = "CONNECTED"

                # ── 2) 상호배타 집계
                for p in procs:
                    if not isinstance(p, dict) or not p.get("name"):
                        continue
                    if p.get("select", True) is not True:
                        continue
                    total_selected += 1

                    running = bool(p.get("running"))
                    # CONNECTED는 오버레이 결과(connected_daemons 반영)를 우선
                    effective_conn = str(p.get("connection_state") or "").upper() == "CONNECTED"
                    # oms-system 과 동일한 우선순위: STOPPED > CONNECTED > RUNNING
                    if not running:
                        total_stopped += 1
                    elif (effective_conn):
                        total_connected += 1
                    else:
                        total_running += 1

            payload["summary"] = {
                "nodes": len(nodes),
                "processes": total_selected,
                "connected": total_connected,
                "running": total_running,
                "stopped": total_stopped,
            }
            return payload
        except Exception:
            return payload

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

    # ─────────────────────────────────────────────────────────────────────────────────────
    # HTTP
    # ─────────────────────────────────────────────────────────────────────────────────────     
    def _make_handler(self):
        orch = self        
        
        def _build_aic_list_from_status(orch):
            try:
                status = orch._status_core()
                nodes = status.get("nodes", [])
                aic_list = {}

                for node in nodes:
                    host_ip = node.get("host")
                    st = node.get("status") or {}
                    procs = _pluck_procs(st)

                    # 프로세스 리스트 순회
                    for p in procs:
                        if not isinstance(p, dict):
                            continue
                        if p.get("name") != "AIc":
                            continue

                        alias = p.get("alias")
                        if not alias:
                            continue

                        aic_list[alias] = host_ip

                return aic_list
            except Exception as e:
                orch._log(f"[OMS][WARN] Failed to build AIcList from status: {e}")
                return {}
        def _request_version(orch, to_daemon, dmpdip, extra_fields=None, wait=5.0):
            """Request Version to a single daemon via MTd and return response dict."""
            pkt = {
                "Section1": "Daemon",
                "Section2": "Information",
                "Section3": "Version",
                "SendState": "request",
                "From": "4DOMS",
                "To": to_daemon,
                "Token": _make_token(),
                "Action": "set",
                "DMPDIP": dmpdip,
            }
            if extra_fields:
                pkt.update(extra_fields)

            logging.debug(f"[Connect.5.2] _request_version: {to_daemon}")
            MTD_PORT = 19765

            raw = _http_fetch(
                "127.0.0.1",
                orch.http_port,
                "POST",
                "/oms/mtd-connect",
                json.dumps({
                    "host": "127.0.0.1",
                    "port": MTD_PORT,
                    "timeout": wait,
                    "trace_tag": f"ver_{to_daemon}",
                    "message": pkt,
                }).encode("utf-8"),
                {"Content-Type": "application/json"},
                timeout=wait,
            )

            logging.debug(f"[Connect.5.2] _request_version raw = {raw!r} (type={type(raw)})")

            # unwrap (status, headers, body) or (status, body)
            if isinstance(raw, tuple):
                if len(raw) == 3:
                    status_code, headers, body = raw
                elif len(raw) == 2:
                    status_code, body = raw
                    headers = None
                else:
                    logging.debug(f"[Connect.5.2] unexpected _http_fetch tuple length: {len(raw)}")
                    return {}

                raw = body
            # else: raw 그대로 body 로 취급

            # parse JSON
            if isinstance(raw, (bytes, bytearray)):
                try:
                    payload = json.loads(raw.decode("utf-8", "ignore"))
                except Exception as e:
                    logging.debug(f"[Connect.5.2] JSON decode failed: {e}")
                    return {}
            elif isinstance(raw, str):
                try:
                    payload = json.loads(raw)
                except Exception as e:
                    logging.debug(f"[Connect.5.2] JSON loads failed: {e}")
                    return {}
            elif isinstance(raw, dict):
                payload = raw
            else:
                logging.debug(f"[Connect.5.2] unexpected body type after unwrap: {type(raw)}")
                return {}

            # MTd proxy 응답 구조: {"ok": true, "response": {...}} 라고 가정
            resp = payload.get("response") or {}
            logging.debug(f"[Connect.5.2] _request_version response = {resp!r}")
            return resp
        def _get_connected_map_from_status(orch, dmpdip):
            """
            /oms/status 결과에서 extra.connected_daemons 만 뽑아서 dict 로 리턴
            실패하면 {} 리턴
            """
            try:
                raw = _http_fetch(
                    "127.0.0.1",
                    orch.http_port,
                    "GET",
                    f"/oms/status?dmpdip={dmpdip}",
                    None,
                    {},
                    timeout=3.0,
                )
                # _http_fetch 가 (status, body) 또는 (status, headers, body) 튜플일 수도 있음
                if isinstance(raw, tuple):
                    if len(raw) == 3:
                        status_code, headers, body = raw
                    elif len(raw) == 2:
                        status_code, body = raw
                        headers = None
                    else:
                        logging.debug(f"[Connect.5.1] unexpected _http_fetch tuple length: {len(raw)}")
                        return {}

                    raw = body
                else:
                    body = raw

                # 타입별 JSON 파싱
                if isinstance(raw, (bytes, bytearray)):
                    status_json = json.loads(raw.decode("utf-8", "ignore"))
                elif isinstance(raw, str):
                    status_json = json.loads(raw)
                elif isinstance(raw, dict):
                    status_json = raw
                else:
                    logging.debug(f"[Connect.5.1] unexpected _http_fetch type after unwrap: {type(raw)}")
                    return {}

                extra = (status_json.get("extra") or {}) if isinstance(status_json, dict) else {}
                connected_map = extra.get("connected_daemons") or {}
                
                return connected_map

            except Exception as e:
                orch._log(f"[OMS][WARN] fetch /oms/status failed in _get_connected_map_from_status: {e}")
                return {}   
        def _collect_all_versions(orch, dmpdip, presd_ips, aic_map):            
            versions = {}
            presd_versions = {}
            aic_versions = {}

            # 0. connected_daemons 가져오기
            state = getattr(orch, "state", {}) or {}
            connected_map = state.get("connected_daemons") or {}
            
            if not connected_map:
                connected_map = _get_connected_map_from_status(orch, dmpdip)

            logging.debug(f"[Connect.5.1] connected_map(final) = {connected_map}")
            exclude_daemons = {"PreSd", "PostSd", "VPd", "MMc", "AIc"}

            # set 으로 모았다가 나중에 정렬
            connected_set = {
                name
                for name, ok in connected_map.items()
                if ok and name not in exclude_daemons
            }

            connected_daemons = sorted(connected_set)
            logging.debug(f"[Connect.5.1] connected_daemons(from connected_map) = {connected_daemons}")

            # ─────────────────────────────────
            # 1. 단일 Daemon들 처리 (MMd → SPd 특수 케이스)
            # ─────────────────────────────────
            has_mmd = "MMd" in connected_set
            if has_mmd:
                connected_set.remove("MMd")
                connected_daemons = sorted(connected_set)

            for name in connected_daemons:
                try:
                    logging.debug(f"[Connect.5.2] Request version for {name}")
                    r = _request_version(orch, name, dmpdip)
                    vmap = r.get("Version") or {}
                    v = vmap.get(name)
                    if v:
                        versions[name] = v
                        logging.debug(f"[Connect.5.2] {name} Version = {v}")
                    else:
                        logging.debug(f"[Connect.5.2] {name} Version not found in response: {r}")
                except Exception as e:
                    orch._log(f"[OMS][WARN] Version fetch failed for {name}: {e}")

            if has_mmd:
                try:
                    logging.debug(f"[Connect.5.2] Request version for MMd(SPd)")
                    r_spd = _request_version(orch, "SPd", dmpdip)
                    vmap_spd = _unwrap_version_map(r_spd)
                    v_spd = vmap_spd.get("SPd")
                    if v_spd:
                        versions["MMd"] = v_spd
                        logging.debug(f"[Connect.5.2] MMd(SPd) Version = {v_spd}")
                    else:
                        logging.debug(f"[Connect.5.2] MMd(SPd) Version not found in response: {r_spd}")
                except Exception as e:
                    orch._log(f"[OMS][WARN] Version fetch failed for MMd(SPd): {e}")


            # ─────────────────────────────────
            # 2. PreSd 여러 IP 요청 (presd_ips 인자는 그대로 사용)
            # ─────────────────────────────────
            if presd_ips:
                try:
                    expect = {
                        "Expect": {
                            "ips": presd_ips,
                            "count": len(presd_ips),
                            "wait_sec": 5,
                        }
                    }
                    logging.debug(f"[Connect.5.3] Request PreSd versions, ips={presd_ips}")
                    r_pre = _request_version(orch, "PreSd", dmpdip, extra_fields=expect, wait=7.0)

                    # 응답은 여러개 → SenderIP 로 구분
                    v_pre = r_pre.get("Version") or {}
                    sender_ip = r_pre.get("SenderIP")
                    if sender_ip and "PreSd" in v_pre:
                        presd_versions[sender_ip] = v_pre["PreSd"]
                        logging.debug(f"[Connect.5.3] PreSd Version[{sender_ip}] = {v_pre['PreSd']}")
                except Exception as e:
                    orch._log(f"[OMS][WARN] PreSd version fetch failed: {e}")

            # ─────────────────────────────────
            # 3. AId → AId Version + 모든 AIc Version 포함됨
            #    (AId 가 connected 인 경우에만 요청)
            # ─────────────────────────────────
            if "AId" in connected_daemons or "AId" in versions:
                try:
                    logging.debug(f"[Connect.5.4] Request AId(+AIc) version")
                    r_aid = _request_version(orch, "AId", dmpdip)
                    vmap = _unwrap_version_map(r_aid)

                    # AId 본인
                    if "AId" in vmap:
                        versions["AId"] = vmap["AId"]
                        logging.debug(f"[Connect.5.4] AId Version = {vmap['AId']}")

                    # AIc 리스트 (AId 가 주도록 구현되어 있다면 여기서 파싱)
                    if "AIc" in vmap and isinstance(vmap["AIc"], list):
                        for item in vmap["AIc"]:
                            name = item.get("name")
                            if not name:
                                continue
                            aic_versions[name] = {
                                "ip": item.get("ip"),
                                "version": item.get("version"),
                                "date": item.get("date"),
                            }
                        logging.debug(f"[Connect.5.4] AIc Versions = {aic_versions}")
                    else:
                        logging.debug(f"[Connect.5.4] No AIc list in AId Version response: {vmap}")
                except Exception as e:
                    orch._log(f"[OMS][WARN] AId/AIc version fetch failed: {e}")

            # ─────────────────────────────────
            # 4. 모든 정보 STATE 저장
            # ─────────────────────────────────
            up_payload = {
                "versions": versions,
                "presd_versions": presd_versions,
                "aic_versions": aic_versions,
                "updated_at": time.time(),
                "dmpdip": dmpdip,
            }

            logging.debug(f"[Connect.5.5] Version upsert payload = {up_payload}")

            try:
                _http_fetch(
                    "127.0.0.1",
                    orch.http_port,
                    "POST",
                    "/oms/state/upsert",
                    json.dumps(up_payload, ensure_ascii=False).encode("utf-8"),
                    {"Content-Type": "application/json"},
                    timeout=5.0,
                )
            except Exception as e:
                orch._log(f"[OMS][WARN] Version upsert failed: {e}")
        def _reload_state_from_server(orch):
            try:
                raw = _http_fetch(
                    "127.0.0.1",
                    orch.http_port,
                    "GET",
                    "/oms/state",
                    None,
                    {},
                    timeout=3.0,
                )

                # 🔥 tuple 대응: (status, body) 형태인 경우
                if isinstance(raw, tuple) and len(raw) >= 2:
                    _, raw = raw

                # 🔥 타입별 JSON 파싱
                if isinstance(raw, (bytes, bytearray)):
                    state_json = json.loads(raw.decode("utf-8", "ignore"))
                elif isinstance(raw, str):
                    state_json = json.loads(raw)
                elif isinstance(raw, dict):
                    state_json = raw
                else:
                    logging.debug(f"[State] unexpected _http_fetch type: {type(raw)}")
                    state_json = {}

                # /oms/state 가 이미 extra 구조를 그대로 줄 수도 있고,
                # /oms/status 처럼 { extra:{...} } 로 줄 수도 있으니 둘 다 대응
                extra = state_json.get("extra") if isinstance(state_json, dict) else None
                if not extra:
                    extra = state_json

                orch.state = extra or {}
                logging.debug(f"[State] reload_state_from_server: orch.state = {orch.state}")

            except Exception as e:
                orch._log(f"[OMS][WARN] reload_state_from_server failed: {e}")
                orch.state = {}
        def _infer_connect_params_from_server(orch, qs: dict) -> tuple[str, int, str, dict]:
            """qs(쿼리스트링)와 서버 상태(STATUS/STATE)에서 mtd_host/mtd_port/dmpdip/daemon_map을 최대한 보완."""
            # 1) 쿼리
            qget = lambda k, default="": (qs.get(k) or [""])[0].strip() or default
            mtd_host = qget("mtd_host")
            dmpdip   = qget("dmpdip")
            try:
                mtd_port = int(qget("mtd_port", "19765") or "19765")
            except Exception:
                mtd_port = 19765

            # 2) /oms/status 스냅샷
            status = orch._status_core()
            extra  = status.get("extra", {}) if isinstance(status, dict) else {}
            if not dmpdip:
                dmpdip = extra.get("dmpdip") or ""
            daemon_map = extra.get("daemon_map") or {}

            # 3) 최신 STATE 보강
            try:
                _key, ext = _latest_state()
                st = STATE.get(_key, {}) if _key else {}
                if not daemon_map:
                    daemon_map = st.get("daemon_map") or {}
                if not dmpdip:
                    dmpdip = ext.get("dmpdip") or st.get("dmpdip") or ""
                if not mtd_host:
                    mtd_host = st.get("mtd_host") or ""
                if not mtd_port:
                    mtd_port = int(st.get("mtd_port") or 19765)
            except Exception:
                pass

            # 4) 최종 보완
            if not mtd_host:
                # 클라이언트 IP를 단서로 동일 서브넷 NIC를 골라 추정
                # (핸들러 안에서 client_ip를 더 넘겨 받아도 되지만, 여기선 상태 기반 추정만)
                try:
                    # nodes 중 하나라도 있으면 그 호스트와 같은 NIC를 택하도록 보정할 수 있음
                    pass
                except Exception:
                    pass

            if not dmpdip or dmpdip.startswith("127.") or dmpdip == "localhost":
                # dmpdip이 애매하면 mtd_host를 기준으로 보정
                try:
                    dmpdip = _guess_server_ip(mtd_host or "127.0.0.1")
                except Exception:
                    dmpdip = "127.0.0.1"

            return mtd_host, mtd_port, dmpdip, daemon_map
        def _run_connect_sequence_via_proxy(orch, mtd_host, mtd_port, dmpdip, daemon_map,
                                            *, trace=False, return_partial=False, dry_run=False):
            """기존 POST /oms/sys-connect/sequence의 로직을 재사용하기 위해
            자기 자신에게 /oms/mtd-connect 프록시를 치는 방식으로 수행."""
            events = []
            # Connect 작업 시작 시점 표시
            orch._connect_set(state="running", message="Connect start")
            t0 = time.time()
            def tag(step): return f"{step}_{int(t0*1000)}"
            def add_event(step, req_msg, resp=None, error=None, used="proxy"):
                events.append({
                    "step": step, "used": used, "request": req_msg,
                    "response": resp, "error": (None if error is None else str(error)),
                    "t": round(time.time()-t0, 3), "trace_tag": tag(step)
                })

            def via_mtd_connect(step, msg, wait):
                if dry_run:
                    add_event(step, msg, {"Result":"skip","ResultCode":"DRY_RUN"}, used="proxy")
                    return {"Result":"skip","ResultCode":"DRY_RUN"}
                conn = http.client.HTTPConnection("127.0.0.1", orch.http_port, timeout=wait)
                payload = json.dumps({
                    "host": mtd_host, "port": mtd_port, "timeout": wait,
                    "trace_tag": tag(step), "message": msg
                })

                # debug
                logging.debug(f"[Connect] via_mtd_connect : {msg})")

                # 요청 직전: 상태 메시지
                try:
                    pretty = step.replace("_", " ")
                    orch._connect_set(message=f"{pretty} …")
                except Exception:
                    pass

                try:
                    conn.request("POST", "/oms/mtd-connect", body=payload, headers={"Content-Type":"application/json"})
                    res = conn.getresponse(); data = res.read()
                    if res.status != 200:
                        raise MtdTraceError(f"/oms/mtd-connect HTTP {res.status}", tag(step))
                    r = json.loads(data.decode("utf-8","ignore")).get("response")
                    add_event(step, msg, r, used="proxy")
                    try:
                        orch._connect_set(message=f"{step} ✓")
                    except Exception:
                        pass
                    return r
                except Exception as e:
                    add_event(step, msg, error=e, used="proxy")
                    try:
                        orch._connect_set(state="error", message=f"{step} error: {e}")
                    except Exception:
                        pass
                    raise
                finally:
                    try: conn.close()
                    except: pass
            def pkt_connect_run(dm):
                return {
                    "DaemonList": {("SPd" if k=="MMd" else k): v for k,v in dm.items()
                                if k not in ("PreSd","PostSd","VPd","AIc","MMc")},
                    "Section1":"mtd","Section2":"connect","Section3":"",
                    "SendState":"request","From":"4DOMS","To":"MTd",
                    "Token": _make_token(), "Action":"run","DMPDIP": dmpdip
                }

            
            # ─────────────────────────────────────────────────────────────────────────────────────
            # 1. Daemon connect
            # ─────────────────────────────────────────────────────────────────────────────────────
            logging.debug(f"[Connect.1] Daemon Connect")
            orch._connect_set(message="Essential Daemons connect")
            r1 = via_mtd_connect(
                "step1_all_connect",
                pkt_connect_run(daemon_map),
                wait=18.0,
            )
            if not dry_run:
                time.sleep(0.8)

            # ─────────────────────────────────────────────────────────────────────────────────────
            # 2. CCd Select
            # ─────────────────────────────────────────────────────────────────────────────────────
            logging.debug(f"[Connect.2] CCd.Select")
            orch._connect_set(message="Camera Information")
            pkt2 = {
                "Section1": "CCd",
                "Section2": "Select",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "EMd",
                "Token": _make_token(),
                "Action": "get",
                "DMPDIP": dmpdip,
            }
            r2 = via_mtd_connect("step2_ccd_select_get", pkt2, wait=12.0)

            scd_ip = None
            try:
                ra = (r2 or {}).get("ResultArray") or []
                scd_ips = { str(x.get("SCd_id")).strip() for x in ra if x.get("SCd_id") }
                if len(scd_ips) == 1: scd_ip = next(iter(scd_ips))
            except Exception:
                pass
            if scd_ip:
                daemon_map["SCd"] = scd_ip
                upsert_payload = {"dmpdip": dmpdip, "daemon_map": daemon_map}
                try:
                    _http_fetch("127.0.0.1", orch.http_port, "POST", "/oms/state/upsert",
                                json.dumps(upsert_payload, ensure_ascii=False).encode("utf-8"),
                                {"Content-Type":"application/json"}, timeout=3.0)
                except Exception:
                    pass
            # ─────────────────────────────────────────────────────────────────────────────────────
            # 3. CCd.Select 결과를 기반으로 PreSd/Camera 리스트 구성 후 PCd에 전달 ---
            # ─────────────────────────────────────────────────────────────────────────────────────
            logging.debug(f"[Connect.3] PreSd Conenct")
            presd_map = {}
            cameras   = []
            try:
                ra = (r2 or {}).get("ResultArray") or []
                for row in ra:
                    pre_ip = str(row.get("PreSd_id") or "").strip()
                    cam_ip = str(row.get("ip") or "").strip()
                    model  = str(row.get("model") or "").strip()
                    scd_id = str(row.get("SCd_id") or "").strip()
                    try:
                        idx = int(row.get("cam_idx") or 0)
                    except Exception:
                        idx = 0
                    if not pre_ip or not cam_ip:
                        continue

                    # PreSd 단위 그룹
                    if pre_ip not in presd_map:
                        presd_map[pre_ip] = {
                            "IP": pre_ip,
                            "Mode": "replay",
                            "Cameras": []
                        }
                    presd_map[pre_ip]["Cameras"].append({
                        "Index": idx,          # 예제와 다르게 cam_idx 그대로 사용 (규칙이 더 정확해지면 여기만 조정)
                        "IP": cam_ip,
                        "CameraModel": model,
                    })

                    # 카메라 전체 리스트 (카메라 페이지용)
                    cameras.append({
                        "Index": idx,
                        "IP": cam_ip,
                        "CameraModel": model,
                        "PreSdIP": pre_ip,
                        "SCdIP": scd_id,
                    })
            except Exception:
                presd_map = {}
                cameras   = []

            # CCd 정보가 있고, 실제 PreSd 그룹이 만들어졌을 때만 PCd로 전송
            if presd_map and not dry_run:
                pkt3 = {
                    "PreSd": list(presd_map.values()),
                    "PostSd": [],
                    "VPd": [],
                    "Section1": "pcd",
                    "Section2": "daemonlist",
                    "Section3": "connect",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "PCd",
                    "Token": _make_token(),
                    "Action": "set",
                    "DMPDIP": dmpdip,
                }

                # PCd daemonlist connect 실행
                r3 = via_mtd_connect("step3_pcd_daemonlist_set", pkt3, wait=18.0)

                # 응답이 왔으면 STATE에 PreSd/Camera 리스트 저장 + PreSd를 CONNECTED 로 마킹
                try:
                    upsert_payload = {
                        "dmpdip": dmpdip,
                        "presd": list(presd_map.values()),
                        "cameras": cameras,
                        # PreSd 는 connect 성공으로 취급
                        "connected_daemons": {"PreSd": True},
                    }
                    _http_fetch(
                        "127.0.0.1",
                        orch.http_port,
                        "POST",
                        "/oms/state/upsert",
                        json.dumps(upsert_payload, ensure_ascii=False).encode("utf-8"),
                        {"Content-Type": "application/json"},
                        timeout=3.0,
                    )
                except Exception:
                    pass

            # ─────────────────────────────────────────────────────────────
            #  4. AIc Connect (AI Clients)
            # ─────────────────────────────────────────────────────────────
            logging.debug(f"[Connect.4] AIc Connect (AI Clients)")
            try:
                aic_list = _build_aic_list_from_status(orch)
                if aic_list and isinstance(aic_list, dict):
                    orch._connect_set(message="AI Clients connect")
                    pkt4 = {
                        "AIcList": aic_list,
                        "Section1": "AIc",
                        "Section2": "connect",
                        "Section3": "",
                        "SendState": "request",
                        "From": "4DOMS",
                        "To": "AId",
                        "Token": _make_token(),
                        "Action": "run",
                        "DMPDIP": dmpdip,
                    }
                    
                    r4 = via_mtd_connect("step4_aic_connect", pkt4, wait=10.0)
                    # 응답 파싱
                    ok_aic = {}
                    if isinstance(r4, dict):
                        resp_list = (r4.get("AIcList") or {})
                        for alias, info in resp_list.items():
                            if not isinstance(info, dict):
                                continue
                            status = str(info.get("Status", "")).upper()
                            ip = info.get("IP", "")
                            if status == "OK":
                                ok_aic[alias] = ip

                    # STATE 반영
                    if ok_aic:
                        up_payload = {
                            "dmpdip": dmpdip,
                            "aic_connected": ok_aic,
                            "connected_daemons": {"AIc": True},
                        }
                        _http_fetch(
                            "127.0.0.1",
                            orch.http_port,
                            "POST",
                            "/oms/state/upsert",
                            json.dumps(up_payload, ensure_ascii=False).encode("utf-8"),
                            {"Content-Type": "application/json"},
                            timeout=3.0,
                        )
            except Exception as e:
                orch._log(f"[OMS][WARN] AIc connect failed: {e}")

            # ─────────────────────────────────────────────
            # Connect 전체 시퀀스 완료 시점에
            #  1) MTd 응답 기반으로 STATE["connected_daemons"] 갱신
            #  2) [system][connect] "Connect done" 브로드캐스트
            # ─────────────────────────────────────────────
            try:
                connected = {}
                dl = ((r1 or {}).get("DaemonList") or {}) if isinstance(r1, dict) else {}
                for dname, info in dl.items():
                    if not isinstance(info, dict):
                        continue
                    status = str(info.get("Status") or info.get("status") or "").upper()
                    if status == "OK":
                        connected[inward_name(dname)] = True                
                # ✅ 하나라도 OK 있으면 MTd도 CONNECTED 로 간주
                if connected:
                    connected["MTd"] = True

                if connected:
                    up_payload = {
                        "dmpdip": dmpdip,
                        "connected_daemons": connected,
                    }
                    _http_fetch(
                        "127.0.0.1",
                        orch.http_port,
                        "POST",
                        "/oms/state/upsert",
                        json.dumps(up_payload, ensure_ascii=False).encode("utf-8"),
                        {"Content-Type": "application/json"},
                        timeout=3.0,
                    )
            except Exception:
                # STATE 갱신 실패해도 connect 시퀀스는 완료로 처리
                orch._log("[OMS][WARN] sys-connect state upsert failed")


            # ─────────────────────────────────────────────────────────────
            #  5. Get Version
            # ─────────────────────────────────────────────────────────────
            logging.debug(f"[Connect.5] Get Version")
            _reload_state_from_server(orch)
            try:
                
                presd_ips = [x.get("IP") for x in (orch.state.get("presd") or []) if x.get("IP")]
                aic_map = (orch.state.get("aic_connected") or {})
                _collect_all_versions(orch, dmpdip, presd_ips, aic_map)
                
            except Exception as e:
                orch._log(f"[OMS][WARN] Collect version failed: {e}")



            # 여기서 최종 "Connect done" 메시지 + state=done 브로드캐스트
            try:
                orch._connect_set(state="done", message="Connect done")
            except Exception:
                pass

            if trace:
                (LOGD / f"connect_trace_{int(time.time()*1000)}.json").write_text(
                    json.dumps({"ok":True,"events":events}, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return events
        def _unwrap_version_map(r: dict) -> dict:
            """
            _request_version() 이 무엇을 리턴하든,
            최종적으로는 {daemon_name: {...}, ...} 형태의 dict 를 돌려주도록 정규화한다.
            """
            if not isinstance(r, dict):
                return {}

            # case 1: 전체 response 를 돌려준 경우 (MTd raw처럼)
            if "Version" in r and isinstance(r["Version"], dict):
                return r["Version"]

            # case 2: 이미 Version 맵만 돌려준 경우
            return r
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
        # ─────────────────────────────────────────────────────────────────────────────────────
        # BaseHTTPRequestHandler
        # ─────────────────────────────────────────────────────────────────────────────────────         
        class H(BaseHTTPRequestHandler):
            # --- fallback proxies to avoid AttributeError from old handler code
            _restart_post_timeout = RESTART_POST_TIMEOUT
            _status_fetch_timeout = STATUS_FETCH_TIMEOUT
                
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

            # ─────────────────────────────────────────────────────────────────────────────────────
            # do_GET
            # ─────────────────────────────────────────────────────────────────────────────────────         
            def do_GET(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    clean = (urlsplit(self.path).path.rstrip("/") or "/")
                    if clean in {"/","/dashboard"}: return _serve_static(self, "oms-dashboard.html")
                    if clean in {"/system"}: return _serve_static(self, "oms-system.html")
                    if clean in {"/command"}: return _serve_static(self, "oms-command.html")
                    if clean in {"/camera"}: return _serve_static(self, "oms-camera.html")
                    if clean in {"/liveview"}: return _serve_static(self, "oms-liveview.html")

                    # ──────────────────────────────────────────────────────
                    # GET proxy
                    # ──────────────────────────────────────────────────────
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=unquote(parts[1]); target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target: return self._write(404, b'{"ok":false,"error":"unknown node"}')
                        sub="/"+"/".join(parts[2:]) if len(parts)>2 else "/"
                        qs=urlsplit(self.path).query
                        if qs: sub=f"{sub}?{qs}"
                        st,hdr,data=_http_fetch(target["host"], int(target.get("port",19776)), "GET", sub, None, None, 4.0)
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/octet-stream"
                        return self._write(st, data, ct)
                    # ── Log endpoints: /daemon/<PROC>/log  and  /daemon/<PROC>/log/list
                    if parts and parts[0] == "daemon" and len(parts) >= 3 and parts[2] == "log":
                        proc = parts[1]
                        # 안전한 프로세스명만 허용 (영문/숫자/언더스코어만)
                        import re, glob
                        if not re.fullmatch(r"[A-Za-z0-9_]+", proc):
                            return self._write(400, b'{"ok":false,"error":"bad process name"}')

                        log_dir = (ROOT / "daemon" / proc / "log")
                        try:
                            log_dir.mkdir(parents=True, exist_ok=True)
                        except Exception:
                            pass

                        # /daemon/<PROC>/log/list
                        if len(parts) >= 4 and parts[3] == "list":
                            try:
                                dates = []
                                for p in sorted(log_dir.glob("????-??-??.log")):
                                    dates.append(p.stem)
                                # 최신날짜가 뒤에 오도록 정렬(원하면 reverse=True 바꾸세요)
                                body = json.dumps({"ok": True, "dates": dates}, ensure_ascii=False).encode("utf-8")
                                return self._write(200, body)
                            except Exception as e:
                                err = json.dumps({"ok": False, "error": f"list failed: {e}"}, ensure_ascii=False).encode("utf-8")
                                return self._write(500, err)

                        # /daemon/<PROC>/log?date=YYYY-MM-DD&tail=50000                        
                        qs = parse_qs(urlsplit(self.path).query)
                        date = (qs.get("date") or [""])[0].strip()
                        tail = (qs.get("tail") or ["50000"])[0].strip()
                        try:
                            tail_bytes = max(0, int(tail))
                        except Exception:
                            tail_bytes = 50000

                        # 기본: 오늘 날짜
                        if not date:
                            import time
                            date = time.strftime("%Y-%m-%d", time.localtime())

                        log_file = log_dir / f"{date}.log"
                        if not log_file.exists():
                            body = json.dumps({
                                "ok": False,
                                "error": "log file not found",
                                "path": str(log_file),
                                "date": date,
                                "tail": tail_bytes
                            }, ensure_ascii=False).encode("utf-8")
                            return self._write(200, body)

                        try:
                            size = log_file.stat().st_size
                            # tail 읽기 (윈도우/대용량 고려)
                            with open(log_file, "rb") as f:
                                if tail_bytes > 0 and size > tail_bytes:
                                    f.seek(size - tail_bytes)
                                    # 줄 경계 맞추기 위해 처음 줄 버림
                                    _ = f.readline()
                                data = f.read()
                            text = data.decode("utf-8", "ignore")
                            body = json.dumps({
                                "ok": True,
                                "path": str(log_file),
                                "date": date,
                                "tail": tail_bytes,
                                "size": size,
                                "text": text
                            }, ensure_ascii=False).encode("utf-8")
                            return self._write(200, body)
                        except Exception as e:
                            err = json.dumps({"ok": False, "error": f"read failed: {e}"}, ensure_ascii=False).encode("utf-8")
                            return self._write(500, err)
                    if parts[:1]==["web"]: return _serve_static(self, "/".join(parts[1:]))
                    if clean.endswith(".html"): return _serve_static(self, clean)
                    # ──────────────────────────────────────────────────────
                    # GET config
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")
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
                    # ── config GET (raw + meta)
                    if parts == ["oms", "config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")
                    if parts == ["oms", "config","meta"]:
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
                     # (optional) backward-compat
                    # ──────────────────────────────────────────────────────
                    # GET [utility] 
                    # ──────────────────────────────────────────────────────                                        
                    # ── mtd message command
                    if parts == ["oms", "hostip"]:
                        qs = parse_qs(urlsplit(self.path).query)
                        peer = (qs.get("peer") or [""])[0].strip()
                        # 브라우저가 붙은 원격 주소(프록시 없다는 가정)도 힌트로 제공
                        client_ip = self.client_address[0]
                        ip = _guess_server_ip(peer or client_ip)
                        return self._write(200, json.dumps({"ok": True, "ip": ip, "client": client_ip}).encode())
                    if parts == ["oms", "mtd-connect"]:
                        return self._write(
                            405,
                            json.dumps({
                                "ok": False,
                                "error": "method not allowed",
                                "hint": "use POST with JSON body: {host, port, message, timeout}"
                            }).encode("utf-8")
                        )
                    # ── ping
                    if parts == ["oms", "ping"]:
                        qs = parse_qs(urlsplit(self.path).query)
                        ip = (qs.get("ip") or [""])[0].strip()
                        if not ip:
                            return self._write(400, b'{"ok":false,"error":"ip required"}')
                        # 옵션: TCP 포트/방법/타임아웃(ms)
                        port = int((qs.get("port") or ["554"])[0] or 554)
                        method = (qs.get("method") or ["auto"])[0].lower()  # auto|tcp|icmp
                        try:
                            t_ms = int((qs.get("timeout") or ["1000"])[0])
                        except Exception:
                            t_ms = 1000
                        timeout_sec = max(0.2, min(5.0, t_ms / 1000.0))
                        alive, used = _ping_check(ip, method=method, port=port, timeout_sec=timeout_sec)

                        # alive=None(판단불가)도 프론트에서 흔들리지 않게 ok:true로 내려주되 alive=null 표기
                        payload = {
                            "ok": True,
                            "alive": (None if alive is None else bool(alive)),
                            "method": used,
                            "port": port,
                            "timeout_ms": int(timeout_sec * 1000)
                        }
                        return self._write(200, json.dumps(payload).encode("utf-8"))
                    # ── camera state (latest snapshot)
                    if parts == ["oms", "state"]:
                        key, snap = _latest_state()

                        # 최신 항목이 없으면 빈 스냅샷으로 200 반환
                        if not key:
                            payload = {
                                "ok": True,
                                "dmpdip": "",
                                "cameras": [],
                                "connected_ips": [],
                                "camera_status": {},
                                "presd": [],
                                "updated_at": 0,
                                "summary": {"cameras": 0, "connected": 0, "on": 0, "off": 0}
                            }
                            return self._write(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

                        st = STATE.get(key, {}) or {}
                        cams = st.get("cameras") or []
                        conn_ips = st.get("connected_ips") or st.get("connected_camera_ips") or []
                        cstat = st.get("camera_status") or {}

                        # --- normalize & count
                        def _norm(s):
                            if s is True: return "on"
                            s = str(s or "").strip().lower()
                            if s in ("connected", "on", "ok", "video ok", "ready", "streaming", "alive"):
                                return "on"
                            if s in ("disconnected", "off", "down", "error", "fail", "ng", "timeout"):
                                return "off"
                            return "unknown"

                        total = len(cams)
                        connected = 0
                        on_cnt = 0
                        off_cnt = 0

                        ip_list = [ (cam or {}).get("IP") for cam in cams if isinstance(cam, dict) ]
                        for ip in ip_list:
                            if ip in conn_ips:
                                connected += 1
                            stv = _norm(cstat.get(ip))
                            if stv == "on":  on_cnt  += 1
                            elif stv == "off": off_cnt += 1

                        payload = {
                            "ok": True,
                            "dmpdip": key,
                            "cameras": cams,
                            "connected_ips": conn_ips,
                            "camera_status": cstat,
                            "presd": st.get("presd") or [],
                            "updated_at": st.get("updated_at") or 0,
                            "summary": {
                                "cameras": total,
                                "connected": int(connected),
                                "on": int(on_cnt),
                                "off": int(off_cnt),
                            }
                        }
                        return self._write(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    # ── process status
                    if parts == ["oms", "status"]:
                        base = orch._status_core()
                        over = orch._overlay_connected(base)
                        return self._write(200, json.dumps(over).encode())
                    # ──────────────────────────────────────────────────────
                    # GET /oms/process-list
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "process-list"]:
                        # 최신 STATE 기준 daemon_map 가져오기
                        key, snap = _latest_state()
                        dm = snap.get("daemon_map", {}) if snap else {}

                        processes = []
                        exclude = {"MMc", "PreSd", "PostSd", "AIc"}

                        if dm:
                            for name, ip in dm.items():
                                if name in exclude:
                                    continue
                                processes.append({"name": name, "ip": ip})
                        else:
                            # fallback (MMc/PreSd/PostSd 제외)
                            host = snap.get("dmpdip") if snap else "127.0.0.1"
                            fallback = ["EMd", "SCd", "CCd", "PCd", "SPd", "AId"]
                            for name in fallback:
                                processes.append({"name": name, "ip": host})

                        body = json.dumps({"processes": processes}, ensure_ascii=False).encode("utf-8")
                        return self._write(200, body)
                    # ── camera list (for camera page)
                    if parts == ["oms", "camera-list"]:
                        key, snap = _latest_state()
                        if not key:
                            payload = {
                                "ok": True,
                                "dmpdip": "",
                                "presd": [],
                                "cameras": [],
                                "updated_at": 0,
                            }
                        else:
                            st = STATE.get(key, {}) or {}
                            payload = {
                                "ok": True,
                                "dmpdip": key,
                                "presd": st.get("presd") or [],
                                "cameras": st.get("cameras") or [],
                                "updated_at": st.get("updated_at") or 0,
                            }
                        return self._write(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                    # ──────────────────────────────────────────────────────
                    # GET [system][restart]
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "sys-restart","state"]:
                        s = orch._restart_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ── Restart progress stream (SSE)
                    if parts == ["oms", "sys-restart","stream"]:
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
                        # 지난번 run의 완료 상태는 처음 접속할 땐 보여주지 않도록 마스킹
                        snap = orch._restart_get()
                        if snap.get("state") in ("done", "error"):
                            snap["state"] = "idle"
                            snap["message"] = ""
                            snap["total"] = snap["sent"] = snap["done"] = 0
                            snap["fails"] = []

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
                    # ──────────────────────────────────────────────────────
                    # GET [system][connect]
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "sys-connect", "sequence"]:
                        qs = parse_qs(urlsplit(self.path).query)
                        # 쿼리로도 진단을 받아보고 싶을 때:
                        mtd_host = (qs.get("mtd_host") or [""])[0].strip()
                        dm_raw   = (qs.get("daemon_map") or [""])[0]
                        try:
                            dm = json.loads(dm_raw) if dm_raw else {}
                        except Exception:
                            dm = {}
                        missing = []
                        if not mtd_host:
                            missing.append("mtd_host")
                        if not dm.get("EMd"):
                            missing.append("daemon_map.EMd")

                        if missing:
                            return self._write(
                                400,
                                json.dumps({"ok": False, "error": "insufficient parameters", "missing": missing}).encode("utf-8")
                            )
                        # 파라미터가 충분하더라도 실행은 POST만 허용
                        return self._write(
                            405,
                            json.dumps({"ok": False, "error": "method not allowed", "hint": "use POST with JSON body"}).encode("utf-8")
                        )
                    # ── Connect progress (state)
                    if parts == ["oms", "sys-connect", "state"]:
                        s = orch._connect_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ── Connect progress stream (SSE)
                    if parts == ["oms", "sys-connect", "stream"]:
                        self.send_response(200)
                        self.send_header("Content-Type","text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control","no-cache, no-transform")
                        self.send_header("Connection","keep-alive")
                        self.send_header("X-Accel-Buffering","no")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type")
                        self.end_headers()
                        token="__connect__"
                        q = orch.PUB.subscribe(token)
                        first = json.dumps(orch._connect_get(), ensure_ascii=False).encode("utf-8","ignore")
                        try:
                            try:
                                self.wfile.write(b"retry: 3000\r\n")
                                self.wfile.write(b"event: progress\r\n")
                                self.wfile.write(b"data: "); self.wfile.write(first); self.wfile.write(b"\r\n\r\n")
                                try: self.wfile.flush()
                                except: pass
                            except: pass
                            last_ka = time.time()
                            ka_interval = 2.0
                            while True:
                                while q:
                                    line = q.popleft().encode("utf-8","ignore")
                                    self.wfile.write(b"event: progress\r\n")
                                    self.wfile.write(b"data: "); self.wfile.write(line); self.wfile.write(b"\r\n\r\n")
                                    try: self.wfile.flush()
                                    except: pass
                                if time.time() - last_ka > ka_interval:
                                    self.wfile.write(b":ka\r\n\r\n")
                                    last_ka = time.time()
                                    try: self.wfile.flush()
                                    except: pass
                                time.sleep(0.25)
                        except Exception:
                            try: self.wfile.flush()
                            except: pass
                        finally:
                            try: orch.PUB.unsubscribe(token, q)
                            except: pass
                        return
                    # ── Connect state clear
                    if parts == ["oms", "sys-connect", "clear"]:
                        orch._connect_set(state="idle", message="", events=[], started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                                        
                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())


            # ─────────────────────────────────────────────────────────────────────────────────────
            # do_POST
            # cmd : curl -X POST http://127.0.0.1:19777/oms/alias/clear                    
            # ─────────────────────────────────────────────────────────────────────────────────────
            def do_POST(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    length=int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)

                    # ── MTd 단건 프록시(유지)
                    if parts==["oms","mtd-connect"]:
                        # 1) 빈 바디/Content-Type 점검
                        if length <= 0:
                            return self._write(400, b'{"ok":false,"error":"empty body"}')
                        ctype = (self.headers.get("Content-Type") or "").lower()
                        if "application/json" not in ctype:
                            return self._write(400, b'{"ok":false,"error":"content-type must be application/json"}')

                        # 2) JSON 파싱
                        try:
                            req = json.loads(body.decode("utf-8", "ignore"))
                        except Exception as e:
                            return self._write(400, json.dumps({"ok": False, "error": f"bad json: {e}"}).encode())

                        host = req.get("host")
                        port = int(req.get("port", 19765))
                        msg  = req.get("message") or {}
                        timeout = float(req.get("timeout") or 10.0)

                        if not host or not isinstance(msg, dict):
                            return self._write(400, b'{"ok":false,"error":"bad request (need host, message)"}')

                        # 3) 보내기 직전 디버그 로그
                        append_mtd_debug("send", host, port, message=msg)
                        try:
                            resp, tag = tcp_json_roundtrip(host, port, msg, timeout=timeout)
                        # 4) 정상 응답 디버그 로그만 남기고, 상태 갱신은 /oms/sys-connect 쪽에서 처리
                            append_mtd_debug("recv", host, port, message=msg, response=resp, tag=tag)
                            return self._write(
                                200,
                                json.dumps({"ok": True, "tag": tag, "response": resp}).encode()
                            )
                            
                        except MtdTraceError as e:
                            # 5) MTd 트레이스 에러 로그
                            append_mtd_debug("error", host, port, message=msg, error=str(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": str(e)}).encode()
                            )
                        except Exception as e:
                            # 6) 기타 예외도 로그
                            append_mtd_debug("error", host, port, message=msg, error=repr(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": repr(e)}).encode()
                            )
                    # ──────────────────────────────────────────────────────
                    # proxy post
                    # ──────────────────────────────────────────────────────
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
                        st,hdr,data=_http_fetch(target["host"], int(target.get("port",19776)), "POST", sub, body, {"Content-Type": self.headers.get("Content-Type","application/json")})
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/json"
                        return self._write(st, data, ct)
                    # ──────────────────────────────────────────────────────
                    # POST config
                    # ──────────────────────────────────────────────────────                                                            
                    # config save/apply 그대로 (생략)
                    if parts == ["oms","config"]:
                        CFG.parent.mkdir(parents=True, exist_ok=True)
                        txt=body.decode("utf-8","ignore"); CFG.write_text(txt, encoding="utf-8")
                        return self._write(200, json.dumps({"ok":True,"path":str(CFG),"bytes":len(txt)}).encode())
                    if parts == ["oms","config","apply"]:
                        try: cfg = load_config(CFG)
                        except Exception as e: return self._write(400, json.dumps({"ok":False, "error":f"load_config: {e}"}).encode())
                        changed = orch.apply_runtime(cfg)
                        return self._write(200, json.dumps({"ok":True, "applied":changed}).encode())
                    # ── ALIAS CACHE CLEAR (DMS /config에서 끌어온 per-node alias 캐시 제거)
                    if parts == ["oms","alias","clear"]:
                        try:
                            with orch._lock:
                                cnt = len(orch._cache_alias)
                                orch._cache_alias.clear()
                            orch._log(f"[OMS] alias cache cleared ({cnt} entries removed)")
                            return self._write(200, json.dumps({"ok":True,"cleared":cnt}).encode())
                        except Exception as e:
                            return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())                    
                    
                    # ──────────────────────────────────────────────────────
                    # POST [system][restart]
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms","sys-restart","clear"]:                        
                        orch._restart_set(state="idle", total=0, sent=0, done=0, fails=[], message="", started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                    # ── Restart All Orchestrator (server-driven)
                    if parts == ["oms","sys-restart","all"]:                        
                        # 이미 수행 중이면 409
                        cur = orch._restart_get()
                        if cur.get("state") == "running":
                            return self._write(409, json.dumps({"ok":False,"error":"already_running"}).encode())

                        # 🔴 요구사항: 재시작 시작 전에 CONNECT 정보 전체 초기화
                        try:
                            ok = _clear_connect_state()
                            # 🔴 여기서 바로 상태 초기화
                            t0 = time.time()
                            orch._restart_set(
                                state="running",
                                total=0,
                                sent=0,
                                done=0,
                                fails=[],
                                message="Preparing…",
                                started_at=t0,
                            )                            
                            orch._log(f"[OMS] connect state cleared before restart (ok={ok})")
                        except Exception as e:
                            orch._log(f"[OMS][WARN] connect state clear failed before restart: {e}")


                        # 워커 스레드
                        def _worker():                            
                            try:
                                t_start = time.time()
                                # orch._restart_set(state="running", total=0, sent=0, done=0, fails=[], message="Preparing… (collecting jobs)", started_at=t_start)
                                # UI가 'Preparing…'을 볼 수 있도록 최소 표시 보장
                                time.sleep(max(0, orch._restart_min_prepare_ms/1000.0))
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
                                            "port": int(n.get("port",19776)),
                                            "status": deepcopy(orch._cache.get(nm) or {})
                                        })

                                jobs = []  # [(host, port, node_name, proc_name)]
                                # orch._restart_set(message="Preparing… (collecting jobs)")

                                def _unify_procs(st):
                                    """
                                    Normalize process lists coming from DMS status structures.
                                    Accepts both {data:{}} and flat dict/list forms.
                                    """
                                    if not st:
                                        return []
                                    # most DMS send under "data"
                                    if "data" in st and isinstance(st["data"], dict):
                                        try:
                                            return [v for v in st["data"].values() if isinstance(v, dict)]
                                        except Exception:
                                            return []
                                    if "processes" in st and isinstance(st["processes"], list):
                                        return [x for x in st["processes"] if isinstance(x, dict)]
                                    if "executables" in st and isinstance(st["executables"], list):
                                        return [x for x in st["executables"] if isinstance(x, dict)]
                                    # if already a dict of processes
                                    if isinstance(st, dict) and all(isinstance(v, dict) for v in st.values()):
                                        return list(st.values())
                                    return []

                                for nd in nodes:
                                    # ✅ robustly extract process list from nested status.data
                                    status_obj = nd.get("status") or {}
                                    procs = _unify_procs(status_obj)
                                    for p in procs:
                                        if not isinstance(p, dict) or not p.get("name"):
                                            continue
                                        # include if selected or no explicit select flag
                                        if p.get("select", True):
                                            jobs.append((nd["host"], nd["port"], nd["name"], p["name"]))

                                total = len(jobs)
                                # 잡 수집 완료 즉시 total 반영해 UI가 “0/총개”를 바로 볼 수 있게.
                                orch._restart_set(state="running", total=total, sent=0, done=0,
                                                    fails=[], message=f"Queued {total} process(es)… sending", started_at=time.time())
                                if total == 0:
                                    # 아무 것도 보낼 게 없으면 바로 done 처리 (UI가 Preparing…에서 못 빠지는 일 방지)
                                    orch._restart_set(state="done", message="Restart finished: nothing selected to restart · 0.0s")
                                    return

                                # --- Utils ---
                                def _overlay_connected_says_connected(proc_name: str, node_host: str) -> bool:
                                    """
                                    해당 노드의 STATE만 보고 '연결 OK' 판정.
                                    - SPd → MMd 정규화
                                    - MMc 는 MMd 연결 시 OK
                                    """
                                    try:
                                        st = _state_for_host(node_host)
                                        conn = st.get("connected_daemons") or {}

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
                                        st,_,dat = _http_fetch(h, p, "GET", "/status-lite", None, None, timeout=orch._status_fetch_timeout)
                                        if st == 200:
                                            js = json.loads(dat.decode("utf-8","ignore"))
                                            procs = js.get("executables") or []
                                            for q in procs:
                                                if (q or {}).get("name") == proc_name:
                                                    return bool(q.get("running"))
                                    except Exception:
                                        pass
                                    try:
                                        st,_,dat = _http_fetch(h, p, "GET", "/status", None, None, timeout=orch._status_fetch_timeout)
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
                                    s = time.time() - (orch._restart_get().get('started_at') or time.time())
                                    # 소수 1자리로 표기: 1.0s, 10.3s
                                    return f"{s:.1f}s"
                                def _fmt_percent(n, d):
                                    try:
                                        if not d:
                                            return 0
                                        return int(round(100.0 * n / d))
                                    except Exception:
                                        return 0                                

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
                                                         timeout=orch._restart_post_timeout)
                                    if st>=400:
                                        raise RuntimeError(f"http {st}")
                                    return job

                                sent = 0
                                fails = []
                                sent_at_map = {}
                                orch._restart_set(message=f"Sending restarts… 0/{total} (0%)")
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
                                            pct = _fmt_percent(sent, total)
                                            orch._restart_set(
                                                sent=sent,
                                                fails=fails,
                                                message=f"Sent {sent}/{total} ({pct}%) (fail {len(fails)})… waiting"
                                            )

                                # 2) RUNNING 복귀 대기 (병렬 폴링)
                                def wait_ready(job):
                                    host,port,node_name,proc = job
                                    base = base_map.get((node_name,proc), {})
                                    sent_at = sent_at_map.get((node_name,proc)) or time.time()
                                    t0 = time.time()
                                    saw_down = False
                                    seen_running = 0
                                    while True:
                                        cur = _read_proc_snapshot(host, port, proc, timeout=self._status_fetch_timeout)
                                        if cur.get("running") is False:
                                            saw_down = True
                                            seen_running = 0
                                        elif cur.get("running") is True:
                                            seen_running += 1
                                        if _is_restarted(base, cur, sent_at, saw_down):
                                            return (job, True)
                                        # (A) 오버레이가 (해당 노드 기준으로) 연결로 보고하면 승인
                                        try:
                                            if cur.get("running") and _overlay_connected_says_connected(proc_name=proc, node_host=host):
                                                return (job, True)
                                        except Exception:
                                            pass

                                        # (B) 메타 없음 + 빠른 재기동: 연속 running 관측 + 최소 대기
                                        meta_present = any(base.get(k) is not None for k in ("pid","start_ts","uptime")) \
                                                    or any(cur.get(k) is not None for k in ("pid","start_ts","uptime"))
                                        if not meta_present and cur.get("running") and seen_running >= 2 and (time.time() - sent_at) > 1.0:
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
                                def _compact(names, limit=10):
                                    return ", ".join(names[:limit]) + (" …" if len(names) > limit else "")
                                if fails:
                                    # 사람이 보기 쉬운 형태로 노드/프로세스 분해
                                    failed_nodes = []
                                    failed_procs = []
                                    for ent in fails:
                                        # ent 예: "DMS-2/PreSd: timeout" 또는 "DMS-1/CCd: http 500"
                                        head = ent.split(":")[0]             # "DMS-2/PreSd"
                                        node, proc = (head.split("/", 1)+[""])[:2]
                                        if node: failed_nodes.append(node)
                                        if proc: failed_procs.append(proc)

                                    summary_failers = _compact([f.split(":")[0] for f in fails], limit=10)  # "DMS-2/PreSd, DMS-1/CCd, ..."
                                    # 3-1) settle 대상/초기 메시지
                                    failed_fullnames = [f.split(':')[0] for f in fails]   # ["DMS-2/PreSd", ...]
                                    targets = set(failed_fullnames)
                                    # settle 안내 상태로 전환 (프런트가 기다리도록)
                                    orch._restart_set(
                                        state="settling",
                                        message=(
                                            f"pre-finished : ok {done}/{total}, fail {len(fails)}; "
                                            f"failed: {summary_failers} · {_fmt_secs()} · verifying up to {int(orch._restart_settle_sec)}s"
                                        ),
                                        fails=fails,
                                        failed_total=len(fails),
                                        failed_list=failed_fullnames,
                                        failed_nodes=failed_nodes,
                                        failed_procs=failed_procs
                                    )

                                    # 3-2) settle loop: 일정 시간 동안 /oms/status 재검증
                                    t0 = time.time()
                                    while time.time() - t0 < orch._restart_settle_sec and targets:
                                        try:
                                            # 최신 상태 스냅샷
                                            snap = orch._status_core()
                                            ok_set, bad_set = Orchestrator._all_targets_running(snap, targets)
                                            # 회복된 항목 제거
                                            if ok_set:
                                                targets -= ok_set
                                                # fails 집합에서도 제거
                                                fails = [x for x in fails if x.split(":")[0] not in ok_set]
                                                done += len(ok_set)

                                            # 진행 메시지 갱신
                                            if targets:
                                                left = sorted(list(targets))[:10]
                                                orch._restart_set(
                                                    message=(
                                                        f"Verifying settle… recovered {len(failed_fullnames)-len(targets)}/{len(failed_fullnames)} "
                                                        f"(left: {', '.join(left)}; {int(time.time()-t0)}s/{int(orch._restart_settle_sec)}s)"
                                                    ),
                                                    fails=fails,
                                                    failed_total=len(targets),
                                                    failed_list=sorted(list(targets))
                                                )
                                            else:
                                                break
                                        except Exception:
                                            # 상태 조회 실패 시 잠시 대기 후 재시도
                                            pass
                                        time.sleep(orch._restart_verify_iv)

                                    # 3-3) settle 종료 후 최종 확정
                                    if not targets:
                                        # 모두 회복됨 → 실패 0으로 교정
                                        orch._restart_set(
                                            state="done",
                                            message=(
                                                f"Finished: ok {total}/{total}, fail 0 · {_fmt_secs()} "
                                                f"(recovered during settle)"
                                            ),
                                            fails=[],
                                            failed_total=0,
                                            failed_list=[],
                                            failed_nodes=[],
                                            failed_procs=[]
                                        )
                                    else:
                                        # 일부 남음 → 남은 대상 기준 최종 메시지
                                        summary_left = _compact(sorted(list(targets)), limit=10)
                                        orch._restart_set(
                                            state="done",
                                            message=(
                                                f"final stage: ok {done}/{total}, fail {len(targets)}; "
                                                f"failed: {summary_left} · {_fmt_secs()}"
                                            ),
                                            fails=[f for f in fails if f.split(':')[0] in targets],
                                            failed_total=len(targets),
                                            failed_list=sorted(list(targets)),
                                            failed_nodes=sorted({x.split('/')[0] for x in targets}),
                                            failed_procs=sorted({x.split('/')[1] for x in targets if '/' in x})
                                        )

                                    try:
                                        (TRACE_DIR / f"restart_report_{int(time.time()*1000)}.json").write_text(
                                            json.dumps({
                                                "ok": len(fails)==0,
                                                "ok_count": done,
                                                "total": total,
                                                "fails": fails,
                                                "failed_list": [f.split(':')[0] for f in fails],
                                                "failed_nodes": failed_nodes,
                                                "failed_procs": failed_procs
                                            }, ensure_ascii=False, indent=2),
                                            encoding="utf-8"
                                        )
                                    except Exception:
                                        pass
                                else:
                                    orch._restart_set(
                                        state="done",
                                        message=f"Finished: ok {done}/{total}, fail 0 · { _fmt_secs() }",
                                        failed_total=0,
                                        failed_list=[],
                                        failed_nodes=[],
                                        failed_procs=[]
                                    )

                            except Exception as e:
                                orch._log(f"[OMS] Restart worker exception: {e}")
                                orch._log(traceback.format_exc())
                                orch._restart_set(state="error", message=f"Error: {e}")

                        threading.Thread(target=_worker, daemon=True).start()
                        return self._write(200, json.dumps({"ok":True}).encode())
                    # ──────────────────────────────────────────────────────
                    # POST [system][connect]
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "sys-connect"]:
                        # body가 없거나 필수 키 빠졌으면 서버가 추정해서 보완
                        try:
                            req = json.loads(body.decode("utf-8","ignore") or "{}")
                        except Exception:
                            req = {}
                        qs  = parse_qs(urlsplit(self.path).query)
                        ret_partial = bool(req.get("return_partial")) or (qs.get("return_partial",["0"])[0] in ("1","true","True"))
                        trace       = bool(req.get("trace")) or (qs.get("trace",["0"])[0] in ("1","true","True"))
                        dry_run     = bool(req.get("dry_run")) or (qs.get("dry_run",["0"])[0] in ("1","true","True"))

                        mtd_host = req.get("mtd_host") or ""
                        mtd_port = int(req.get("mtd_port") or 19765)
                        dmpdip   = (req.get("dmpdip") or "").strip()
                        daemon_map = req.get("daemon_map") or {}

                        if not mtd_host or not daemon_map:
                            # 서버가 채워 넣기
                            _mtd_host, _mtd_port, _dmpdip, _dm = _infer_connect_params_from_server(orch, {})
                            mtd_host = mtd_host or _mtd_host
                            mtd_port = mtd_port or _mtd_port
                            dmpdip   = dmpdip   or _dmpdip
                            daemon_map = daemon_map or _dm

                        if not mtd_host or not daemon_map:
                            return self._write(400, json.dumps({"ok":False,"error":"insufficient parameters (mtd_host/daemon_map)"}).encode())

                        try:
                            events = _run_connect_sequence_via_proxy(
                                orch, mtd_host, mtd_port, dmpdip, daemon_map,
                                trace=trace, return_partial=ret_partial, dry_run=dry_run
                            )
                            return self._write(200, json.dumps({"ok": True, "events": events}).encode())
                        except Exception as e:
                            if ret_partial:
                                return self._write(200, json.dumps({"ok":False,"error":repr(e)}).encode())
                            return self._write(502, json.dumps({"ok":False, "error":repr(e)}).encode())
                    if parts == ["oms", "sys-connect", "clear"]:
                        ok = _clear_connect_state()
                        if ok:
                            return self._write(200, b'{"ok":true}')
                        else:
                            return self._write(500, json.dumps({"ok":False,"error":"clear failed"}).encode())
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
                            # ▶ 추가 저장: 연결된 카메라/상태
                            if isinstance(req.get("connected_ips"), list):
                                cur["connected_ips"] = req["connected_ips"]
                            if isinstance(req.get("connected_camera_ips"), list):
                                # 구버전 키도 수용
                                cur["connected_ips"] = req["connected_camera_ips"]
                            if isinstance(req.get("camera_status"), dict):
                                cur["camera_status"] = req["camera_status"]
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
        cfg = {"http_host":"0.0.0.0","http_port":19777,"heartbeat_interval_sec":2,"nodes":[]}
    _state_load()  # ← 추가
    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
