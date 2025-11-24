# ─────────────────────────────────────────────────────────────
# oms_agent.py
# BaseHTTPRequestHandler backend
#
# --- how to read log >>> Powershell
# >> Get-Content "C:\4DReplay\V5\daemon\OMs\log\2025-11-20.log" -Wait -Tail 20
# ─────────────────────────────────────────────────────────────
# -*- coding: utf-8 -*-

from __future__ import annotations

import socket
import os
import copy
import http.client
import json, re, time, threading
import subprocess
import errno
import logging

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
from copy import deepcopy
from src.fd_communication.server_mtd_connect import tcp_json_roundtrip, MtdTraceError
from live_mtx_manager import MTX
from collections import OrderedDict

# shared codes/functions
from oms_common import fd_load_json_file, fd_save_json_file, fd_update_prefix_item
from oms_common import fd_format_hms_verbose, fd_format_datetime, fd_format_hms_ms
from oms_common import fd_load_adjust_info, fd_find_adjustinfo_file
# ─────────────────────────────────────────────────────────────
# --- Path
# ─────────────────────────────────────────────────────────────
PROCESS_ALIAS_DEFAULT = {
    "MTd": "Message Transport",
    "EMd": "Enterprise Manager",
    "CCd": "Camera Control",
    "SCd": "Switch Control",
    "PCd": "Processor Control",
    "GCd": "Gimbal Control",
    "MMd": "Multimedia Maker",
    "MMc": "Multimedia Maker Client",
    "AId": "AI Daemon",
    "AIc": "AI Client",
    "PreSd":"Pre Storage",
    "PostSd":"Post Storage",
    "VPd":"Vision Processor",
    "AMd": "Audio Manager",
    "CMd": "Compute Multimedia",
}

# ─────────────────────────────────────────────────────────────
# Global root paths
# ─────────────────────────────────────────────────────────────

# Default V5 root
V5_ROOT = Path(os.environ.get("OMS_ROOT", Path(__file__).resolve().parents[2]))
# Root / web / config paths 
ROOT = V5_ROOT 
WEB = ROOT /"web" 
CFG = ROOT /"config"/"oms_config.json" 
CFG_RECORD = ROOT/"config"/"user_config.json"

# OMS logs → daemon/OMs/log 아래로 저장되도록 변경
LOGD = Path(os.environ.get("OMS_LOG_DIR", str(V5_ROOT / "daemon" / "OMs")))
LOGD.mkdir(parents=True, exist_ok=True)

# State and trace files
CAM_STATE_FILE = LOGD / "oms_cam_state.json"
SYS_STATE_FILE = LOGD / "oms_sys_state.json"
VERS_FILE = LOGD / "oms_versions.json"
TRACE_DIR = LOGD / "trace"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

# All logs are stored in: C:\4DReplay\V5\daemon\OMs\log\YYYY-MM-DD.log
LOG_DIR = LOGD / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)


# Daily filename
log_filename = time.strftime("%Y-%m-%d") + ".log"
log_path = LOG_DIR / log_filename

# Create logger
fd_log = logging.getLogger("OMS")
fd_log.setLevel(logging.DEBUG)

# Avoid duplicate handler registration
if not fd_log.handlers:
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    fh.setFormatter(fmt)
    fd_log.addHandler(fh)

# ─────────────────────────────────────────────────────────────
# Global log helper used across the system
# ─────────────────────────────────────────────────────────────
def global_log(msg: str):
    fd_log.info(msg)

# ─────────────────────────────────────────────────────────────
# --- hard-coded timeouts ---
# ─────────────────────────────────────────────────────────────
RESTART_POST_TIMEOUT = 30.0
STATUS_FETCH_TIMEOUT = 10.0

# ─────────────────────────────────────────────────────────────
# global space
# ─────────────────────────────────────────────────────────────
PENDING_COMMAND = None
PENDING_TS = 0
COMMAND_LOCK = threading.Lock()

# ─────────────────────────────────────────────────────────────
# ⚙️ C/O/N/F/I/G/U/R/A/T/I/O/N
# ─────────────────────────────────────────────────────────────
def load_config(p:Path)->dict:
    txt=p.read_text(encoding="utf-8")
    return json.loads(_strip_json5(txt))

# ─────────────────────────────────────────────────────────────
# 🗂️ STATE / SYSTEM
# ─────────────────────────────────────────────────────────────
SYS_STATE = {} # 새로운 프로세스 상태 저장소
ALLOWED_SYS_KEYS = {
    "connected_daemons",
    "cameras",
    "presd",
    "switches",
    "versions",
    "presd_versions",
    "aic_versions",
    "updated_at",
}
def _sys_state_load():
    global SYS_STATE
    try:
        if not SYS_STATE_FILE.exists():
            fd_log.error(f"Load: no file: {SYS_STATE_FILE}")
            SYS_STATE = {}
            return

        raw = json.loads(SYS_STATE_FILE.read_text("utf-8"))
        fd_log.info(f"# Load: {SYS_STATE_FILE}")

        # ① raw 전체에서 updated_at 가장 큰 항목 선택
        if isinstance(raw, dict):
            # case 1) 이미 통합 하나짜리 구조 → 그대로
            if "connected_daemons" in raw and "versions" in raw:
                SYS_STATE = raw
                return

            # case 2) node별 구조: { "10.82.104.210": {...}, "127.0.0.1": {...} }
            best = None
            best_ts = -1

            for key, st in raw.items():
                if not isinstance(st, dict):
                    continue
                ts = st.get("updated_at", 0)
                if isinstance(ts, (int, float)) and ts >= best_ts:
                    best = st
                    best_ts = ts

            if best:
                SYS_STATE = best
            else:
                SYS_STATE = {}
        else:
            SYS_STATE = {}

        fd_log.info(f"SYS_STATE(normalized) = {SYS_STATE}")
        fd_log.info("=====================================================")

    except Exception as e:
        fd_log.exception(f"_sys_state_load failed: {e}")
def _sys_state_save():
    try:
        with open(SYS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(SYS_STATE, f, indent=2, ensure_ascii=False)        
    except Exception as e:
        fd_log.exception(f"[save][system][state] failed: {e}")
def _sys_state_upsert(payload: dict):
    global SYS_STATE
    clean = {}
    for k, v in payload.items():
        if k in ALLOWED_SYS_KEYS:
            clean[k] = v
    clean["updated_at"] = time.time()
    # SYS_STATE 전체를 clean 으로 교체
    SYS_STATE = clean
    with open(SYS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(SYS_STATE, f, indent=2, ensure_ascii=False) 
    _sys_state_save()
def _sys_latest_state():
    if not SYS_STATE:
        return None, {}
    return None, SYS_STATE

# ─────────────────────────────────────────────────────────────
# 🗂️ STATE / CAMERA
# ─────────────────────────────────────────────────────────────
CAM_STATE = {} 
ALLOWED_CAM_KEYS = {
    "cameras",
    "switches",
    "updated_at",
}
def _cam_state_load():
    global CAM_STATE
    try:
        if CAM_STATE_FILE.exists():
            CAM_STATE.update(json.loads(CAM_STATE_FILE.read_text("utf-8")))
        else:
            CAM_STATE = {}
    except:
        CAM_STATE = {}
def _cam_state_save():
    try:
        with open(CAM_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(CAM_STATE, f, indent=2, ensure_ascii=False)        
    except Exception as e:
        fd_log.exception(f"[save][camera][state] failed: {e}")
def _cam_state_upsert(payload: dict):
    global CAM_STATE
    CAM_STATE = payload
    _cam_state_save()
def _cam_latest_state():
    return CAM_STATE
def _cam_clear_connect_state() -> bool:
    """
    CONNECTED 스냅샷을 메모리/디스크 모두 초기화.
    - CAM_STATE.clear()
    - CAM_STATE_FILE 삭제
    실패해도 예외는 바깥으로 올리지 않고 False 반환.
    """
    try:
        CAM_STATE.clear()
        try:
            CAM_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    except Exception:
        return False

# ─────────────────────────────────────────────────────────────
# 🛠️ UTILITY
# ─────────────────────────────────────────────────────────────
def _append_mtd_debug(direction, host, port, message=None, response=None, error=None, tag=None):
    """
    direction: 'send' | 'recv' | 'error'
    각 /oms/mtd-query 호출마다 JSONL 한 줄씩 기록.
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
def _daemon_name_for_inside(n: str) -> str: return "SPd" if n == "MMd" else n
def _make_token() -> str:
    ts = int(time.time() * 1000)
    lt = time.localtime()
    return f"{lt.tm_hour:02d}{lt.tm_min:02d}_{ts}_{hex(ts)[-3:]}"
def _same_subnet(ip1, ip2, mask_bits=24):
    a = list(map(int, ip1.split(".")))
    b = list(map(int, ip2.split(".")))
    m = [255,255,255,0] if mask_bits==24 else [255,255,255,255] # 필요시 확장
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
def load_record_prefix_list():
    """record_config.json에서 prefix 목록 읽는 함수"""
    try:
        fd_log.info(f"CFG_RECORD = {CFG_RECORD}")
        if not CFG_RECORD.exists():
            return {"ok": False, "message": "user_config.json not found"}

        data = json.loads(CFG_RECORD.read_text("utf-8"))
        return {"ok": True, "prefix": data.get("prefix", [])}

    except Exception as e:
        return {"ok": False, "message": str(e)}

# ─────────────────────────────────────────────────────────────
# PING
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
# BACKEND HELPER
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

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self, cfg:dict):

        self._log(f"")
        self._log(f"")
        self._log(f"#####################################################################")
        
        self.state = {}
        self._config = cfg # 또는 self.config = cfg 도 같이 써도 됨                

        self.http_host = cfg.get("http_host","0.0.0.0")
        self.http_port = int(cfg.get("http_port",19777))
        self.heartbeat = float(cfg.get("heartbeat_interval_sec",2))
        self.nodes = list(cfg.get("nodes",[])) 
        try:
            user_alias = cfg.get("process_alias") or {}
            if not isinstance(user_alias, dict): user_alias = {}
        except Exception:
            user_alias = {}
        self.process_alias = {**PROCESS_ALIAS_DEFAULT, **user_alias}
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cache = {}
        self._cache_ts = {}
        self._cache_alias = {} # { node_name: { "PreSd": "Pre Storage [#1]", ... } }
        # ────────────────────────────────────────
        # system restart
        # ────────────────────────────────────────
        self._sys_restart = {
            "state": "idle", # idle | running | done | error
            "total": 0,
            "sent": 0,
            "done": 0,
            "fails": [],
            "message": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._restart_lock = threading.RLock()
        self._restart_post_timeout = RESTART_POST_TIMEOUT
        self._status_fetch_timeout = STATUS_FETCH_TIMEOUT
        self._restart_ready_timeout = 40.0
        self._restart_settle_sec = 20.0 # 타임아웃 뒤 사후 검증 기간
        self._restart_verify_iv = 0.5 # settle 동안 상태 확인 주기 (초)
        self._restart_poll_interval = 0.25
        self._restart_max_workers = 8
        self._restart_min_prepare_ms = 300        
        # ────────────────────────────────────────
        # system connect
        # ────────────────────────────────────────
        self._sys_connect_lock = threading.RLock() 
        self._sys_connect = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "events": [], # 최근 실행의 이벤트 요약 (원하면 유지 길이 제한)
            "started_at": 0.0,
            "updated_at": 0.0,
            "seq": 0,
        }
        self.presd_map = {}
        self.cameras = []
        self.switch_ips = {}
        _sys_state_load()
        self.mtd_port = int(cfg.get("mtd_port", 19765)) 
        self.mtd_ip = cfg.get("mtd_host", "127.0.0.1")
        # get daemonss ip
        self.daemon_ips = {}        
        self.scd_ip = self.mtd_ip        
        self.ccd_ip = self.mtd_ip 
        # ────────────────────────────────────────
        # cam restart
        # ────────────────────────────────────────
        self._cam_restart = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "error": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._cam_restart_lock = threading.RLock() 
        # ────────────────────────────────────────
        # cam connect
        # ────────────────────────────────────────
        self._cam_connect = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "summary": {},
            "error": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._cam_connect_lock = threading.RLock() 
        _cam_state_load()

        # ────────────────────────────────────────
        # record
        # ────────────────────────────────────────
        self.recording_name = ""
        self.record_start_time = 0
        self.current_adjustinfo = None

    # ────────────────────────────────────────────
    # ⚙️ C/O/M/M/O/N
    # ────────────────────────────────────────────      
    def _mtd_command(self, step, msg, wait=5.0):
        try:
            fd_log.info(f"mtd:request: >>\n{msg}")
            payload = json.dumps({
                "host": self.mtd_ip,
                "port": self.mtd_port,
                "timeout": wait,
                "trace_tag": f"{step}_{int(time.time()*1000)}",
                "message": msg
            })
            conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=wait)
            conn.request("POST", "/oms/mtd-query", body=payload,
                headers={"Content-Type": "application/json"})
            # ─────────────────────────────────────
            # ★ 응답을 기다리는 루프 (최대 wait 초)
            # ─────────────────────────────────────
            end_time = time.time() + wait
            while time.time() < end_time:
                res = conn.getresponse()
                data = res.read()
                if res.status != 200:
                    raise Exception(f"HTTP {res.status}")
                try:
                    resp = json.loads(data.decode("utf-8", "ignore"))
                except Exception as e:
                    fd_log.exception(f"JSON decode error: {e}")
                    time.sleep(0.1)
                    continue
                r = resp.get("response")
                if r:
                    fd_log.info(f"mtd:response <<\n {r}")
                    return r
                # 응답 없음 → 잠깐 대기 후 다시 polling
                time.sleep(0.1)
            # ──────────────────────────────
            # 타임아웃: response 끝내 못 받음
            # ──────────────────────────────
            fd_log.error("mtd:response timeout - no response received from MTd")
            return None

        except Exception as e:
            fd_log.exception(f"_mtd_command error: {e}")
            return None

        finally:
            try:
                conn.close()
            except:
                pass
    def _get_process_list(self):
        try:
            status = self._sys_status_core()            
            nodes = status.get("nodes", [])
        except Exception:
            return []
        proc_map = {} # pname → set(ip)
        for node in nodes:
            host_ip = node.get("host")
            st = node.get("status", {})
            executables = st.get("executables", [])
            for exe in executables:
                pname = exe.get("name")
                if not pname:
                    continue
                # 🔥 select == True 인 프로세스만 리스트 포함
                if not exe.get("select", False):
                    continue
                proc_map.setdefault(pname, set()).add(host_ip)
        # 결과 변환
        processes = []
        for pname, ips in proc_map.items():
            processes.append({
                "name": pname,
                "ips": sorted(list(ips)),
            })
        return processes
    def _get_daemon_ip(self):
        daemon_ips = {}
        try:
            plist = self._get_process_list()
        except Exception as e:
            fd_log.exception(f"_get_daemon_ip exception: {e}")
            return {}
        # ★ 케이스 1: None
        if plist is None:
            fd_log.error("process_list is None")
            return {}
        # ★ 케이스 2: dict 형태로 오는 경우 (자동 변환)
        if isinstance(plist, dict):
            fd_log.warning("[PATCH] process-list was dict; converting to list")
            new_list = []
            for name, ip in plist.items():
                if ip is None:
                    continue
                if not isinstance(ip, list):
                    ip = [ip]
                new_list.append({"name": name, "ips": ip})
            plist = new_list
        # ★ 케이스 3: list 가 아닌 경우
        if not isinstance(plist, list):
            fd_log.error(f"process_list invalid type: {type(plist)}")
            return {}
        # ★ 핵심 패치: 리스트 요소 필터링
        cleaned = []
        for proc in plist:
            if not proc:
                continue
            if not isinstance(proc, dict):
                fd_log.error(f"invalid process entry skipped: {proc}")
                continue
            cleaned.append(proc)
        plist = cleaned
        # 최종 처리
        for proc in plist:
            name = proc.get("name")
            ips = proc.get("ips") or []
            if not name:
                continue
            daemon_ips[name] = list(ips)
        fd_log.info(f"[PATCH] daemon_ips fetched = {daemon_ips}")
        return daemon_ips
    def _tagged_time(self, msg, start_time):
        s = str(msg or "").strip()
        if not s:
            return s
        time_proc = time.time() - (start_time or time.time())        
        ret_msg = f"{s} · {time_proc:.1f}s"
        return ret_msg
    def _extract_http_body(self, raw):
        # HTTP 응답 tuple 처리
        if isinstance(raw, tuple):
            # (status, headers, body)
            for item in raw:
                # body 후보: bytes 또는 str
                if isinstance(item, bytes):
                    return item
                if isinstance(item, str):
                    return item
            raise ValueError("No HTTP body found in tuple response")
        # 이미 bytes, str, 또는 dict일 수 있음
        return raw

    # ────────────────────────────────────────────
    # 🛠️ /S/Y/S/T/E/M/
    # ────────────────────────────────────────────    
    @staticmethod
    def _build_aic_list_from_status(self):
        try:
            status = self._sys_status_core()
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
            fd_log.exception(f"Failed to build AIcList from status: {e}")
            return {}
    def _request_version(self, to_daemon, dmpdip, extra_fields=None, wait=8.0):
        """Request Version to a single daemon via MTd and return response dict."""
        RETRY = 3
        last_err = None

        pkt = {
            "Section1": "Daemon",
            "Section2": "Information",
            "Section3": "Version",
            "SendState": "request",
            "From": "4DOMS",
            "To": to_daemon,
            "Token": _make_token(),
            "Action": "set",
            "DMPDIP": self.mtd_ip,
        }
        if extra_fields:
            pkt.update(extra_fields)
        # ----------------------------------------------------------    
        #  Retry Logic (AIc 버전 조회 timeout 대비)
        # ----------------------------------------------------------
        for attempt in range(1, RETRY + 1):
            try:
                fd_log.info(f"[version][{to_daemon}] request >>\n{pkt}")
                raw = _http_fetch(
                    "127.0.0.1",
                    self.http_port,
                    "POST",
                    "/oms/mtd-query",
                    json.dumps({
                        "host": "127.0.0.1",
                        "port": self.mtd_port,
                        "timeout": wait,
                        "trace_tag": f"ver_{to_daemon}",
                        "message": pkt,
                    }).encode("utf-8"),
                    {"Content-Type": "application/json"},
                    timeout=wait,
                )
                # success and wait for next query
                time.sleep(0.1)
                break                
            except TimeoutError as e:
                fd_log.exception(f"[version] fetching version from {to_daemon}, retry {attempt}/{RETRY}")
                if attempt == RETRY:
                    raise    # 마지막 시도도 실패 → 그대로 오류
                time.sleep(0.5)
            except Exception as e:
                fd_log.exception(f"[version] fetch failed for {to_daemon}: {e}")
                if attempt == RETRY:
                    raise
                time.sleep(0.5)

        # unwrap (status, headers, body) or (status, body)
        if isinstance(raw, tuple):
            if len(raw) == 3:
                status_code, headers, body = raw
            elif len(raw) == 2:
                status_code, body = raw
                headers = None
            else:
                fd_log.info(f"[version] unexpected _http_fetch tuple length: {len(raw)}")
                return {}
            raw = body

        # parse JSON
        if isinstance(raw, (bytes, bytearray)):
            try:
                payload = json.loads(raw.decode("utf-8", "ignore"))
            except Exception as e:
                fd_log.debug(f"[version] JSON decode failed: {e}")
                return {}
        elif isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except Exception as e:
                fd_log.debug(f"[version] JSON loads failed: {e}")
                return {}
        elif isinstance(raw, dict):
            payload = raw
        else:
            fd_log.debug(f"[version] unexpected body type after unwrap: {type(raw)}")
            return {}

        # MTd proxy 응답 구조: {"ok": true, "response": {...}} 라고 가정
        resp = payload.get("response") or {}
        fd_log.info(f"[version][{to_daemon}] response <<\n{resp!r}")
        return resp
    def _get_connected_map_from_status(self, dmpdip):
        connected = {}
        multi_count = {"PreSd": 0, "AIc": 0}
        try:
            # ① MTd 응답 기반 (단일 데몬)
            last_connect_resp = self._connected_daemonlist
            for name, info in (last_connect_resp or {}).items():
                if not isinstance(info, dict):
                    continue
                status = str(info.get("Status") or "").upper()
                if status == "OK":
                    connected[name] = True

            # ② node.status.data 기반 (멀티 인스턴스용)
            for node in self.nodes:
                data = node.get("status", {}).get("data", {})
                for name, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    if info.get("running") is True and name in multi_count:
                        multi_count[name] += 1

            # 멀티 인스턴스 반영
            for name, cnt in multi_count.items():
                if cnt > 0:
                    connected[name] = cnt
            return connected
        except Exception:
            return {}
    def _unwrap_version_map(self, r: dict) -> dict:
        if not isinstance(r, dict):
            return {}
        # case 1: 전체 response 를 돌려준 경우 (MTd raw처럼)
        if "Version" in r and isinstance(r["Version"], dict):
            return r["Version"]            
        return r
    
    # ─────────────────────────────────────────────────────────────────────────────────────
    # 🔁 SYSTEM RESTART
    # ─────────────────────────────────────────────────────────────────────────────────────    
    def _sys_restart_get(self):
        with self._restart_lock:
            snap = deepcopy(self._sys_restart)            
            return snap
    def _sys_restart_set(self, **kw):
        
        start_time = kw.get('started_at') or self._sys_restart.get("started_at")
        msg_origin = kw.get("message")        

        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]        
        fd_log.info(f"system restart message : {start_time}| {state_code} |{msg}")
        # update status
        with self._restart_lock:
            self._sys_restart.update(kw)
            self._sys_restart["updated_at"] = time.time()            
            self._sys_restart["started_at"] = start_time
    def _sys_restart_process(self, orch):
        try:
            time.sleep(max(0, orch._restart_min_prepare_ms / 1000.0))
            # reset state
            # NEW: SYS_STATE 전체 초기화 (대표님이 정의한 최종 구조)
            global SYS_STATE
            SYS_STATE = {
                "connected_daemons": {},
                "versions": {},
                "presd_versions": {},
                "aic_versions": {},
                "presd": [],
                "cameras": [],
                "switches": [],
                "updated_at": time.time(),
            }
            _sys_state_save()
            fd_log.info("SYS_STATE reset completed")

            # --- Gather nodes safely
            nodes = []
            for n in orch.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm,
                    "host": n["host"],
                    "port": int(n.get("port", 19776)),
                    "status": deepcopy(orch._cache.get(nm) or {})
                })

            processes = orch._get_process_list() # /oms/system/process-list 와 동일한 반환 구조
            # 예: [{ "name": "EMd", "ips": ["10.82.104.210"] }, ...]

            jobs = []
            for proc in processes:
                name = proc["name"]
                for ip in proc["ips"]:
                    jobs.append((ip, 19776, ip, name)) # ip별로 restart job 생성

            fd_log.info(f"jobs, {jobs}")
            total = len(jobs)
            orch._sys_restart_set(state=1,total=total,sent=0,done=0,fails=[],message=f"Process Restart Queued : {total}")

            if total == 0:
                orch._sys_restart_set(state=2,message="Restart finished: nothing selected to restart")
                return

            def _fmt_percent(n, d):
                if not d:
                    return 0
                return int(round(100.0 * n / d))
            # ---------- pre-snapshots ----------
            base_map = {}
            for (host, port, node_name, proc) in jobs:
                base_map[(node_name, proc)] = _read_proc_snapshot(host, port, proc)
                # check daemon host 
                if proc == "MTd":
                    self.mtd_ip = host

            # ======================================================
            # 1) POST /restart/<proc>  --- 병렬 전송
            # ======================================================

            def send_restart(job):
                host, port, node_name, proc = job
                st, _, _ = _http_fetch(
                    host, port, "POST",
                    f"/restart/{proc}",
                    b"{}",
                    {"Content-Type": "application/json"},
                    timeout=orch._restart_post_timeout
                )
                if st >= 400:
                    raise RuntimeError(f"http {st}")
                return job

            sent = 0
            fails = []
            sent_at_map = {}
            orch._sys_restart_set(state=1,message=f"Sending restarts… 0/{total} (0%)")

            # --- 병렬 max_workers = job 수
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futs = {ex.submit(send_restart, j): j for j in jobs}
                for fut in as_completed(futs):
                    job = futs[fut]
                    host, port, node_name, proc = job
                    try:
                        fut.result()
                        sent += 1
                        sent_at_map[(node_name, proc)] = time.time()
                    except Exception as e:
                        msg = f"{node_name}/{proc}: {e}"
                        fails.append(msg)
                        fd_log.exception(f"[send] {msg}")
                    finally:
                        pct = _fmt_percent(sent, total)
                        orch._sys_restart_set(state=1,sent=sent,fails=fails,message=f"Restart Process [{proc}] {sent}:/{total} ({pct}%) (fail {len(fails)})… waiting")

            # ======================================================
            # 2) wait_ready --- 병렬 polling (완전 병렬화)
            # ======================================================
            pending = [
                j for j in jobs
                if not any(f"{j[3]}" in f for f in fails)
            ]

            def wait_ready(job):
                host, port, node_name, proc = job
                base = base_map.get((node_name, proc), {})
                sent_at = sent_at_map.get((node_name, proc)) or time.time()
                t0 = time.time()
                saw_down = False
                seen_running = 0

                while True:
                    cur = _read_proc_snapshot(
                        host, port, proc,
                        timeout=self._status_fetch_timeout
                    )

                    # down → up transition 체크
                    if cur.get("running") is False:
                        saw_down = True
                        seen_running = 0
                    elif cur.get("running") is True:
                        seen_running += 1

                    # 방식1: meta 기반 restart 판단
                    if _is_restarted(base, cur, sent_at, saw_down):
                        return (job, True)

                    # 방식3: meta 없고 빠른 재기동 → running 2회 관측
                    meta_present = any(base.get(k) is not None for k in ("pid","start_ts","uptime")) \
                        or any(cur.get(k) is not None for k in ("pid","start_ts","uptime"))
                    if (not meta_present and cur.get("running") and seen_running >= 2 and (time.time() - sent_at) > 1.0):
                        return (job, True)

                    # timeout
                    if time.time() - t0 > orch._restart_ready_timeout:
                        return (job, False)

                    time.sleep(orch._restart_poll_interval)

            done = 0

            # --- 강제 병렬: max_workers = pending 수
            with ThreadPoolExecutor(max_workers=len(pending)) as ex:
                futs = [ex.submit(wait_ready, j) for j in pending]
                for fut in as_completed(futs):
                    job, ok = fut.result()
                    host, port, node_name, proc = job
                    if ok:
                        done += 1
                    else:
                        msg = f"{node_name}/{proc}: timeout"
                        fails.append(msg)
                        fd_log.error(f"[wait] {msg}")

                    orch._sys_restart_set(state=1,done=done,fails=fails,message=f"Restart Process {sent}/{total} (fail {len(fails)})… waiting")

            # ======================================================
            # 3) settle 단계 (병렬 검사 버전)
            # ======================================================

            def _compact(names, limit=10):
                return ", ".join(names[:limit]) + (" …" if len(names) > limit else "")

            if fails:
                failed_fullnames = [f.split(":")[0] for f in fails]
                targets = set(failed_fullnames)

                failed_nodes = [x.split("/")[0] for x in failed_fullnames]
                failed_procs = [x.split("/")[1] for x in failed_fullnames]

                summary_failers = _compact(failed_fullnames, 10)

                orch._sys_restart_set(state=1,    #run
                    message=(
                        f"pre-finished : ok {done}/{total}, fail {len(fails)}; "
                        f"failed: {summary_failers} · "
                        f"verifying up to {int(orch._restart_settle_sec)}s"
                    ),
                    fails=fails,
                    failed_total=len(failed_fullnames),
                    failed_list=failed_fullnames,
                    failed_nodes=failed_nodes,
                    failed_procs=failed_procs
                )

                # ---- 병렬 settle 검사 ----
                t0 = time.time()
                
                while time.time() - t0 < orch._restart_settle_sec and targets:
                    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                        futs = {ex.submit(orch._sys_check_process, t): t for t in list(targets)}
                        for fut in as_completed(futs):
                            t = futs[fut]
                            ok = fut.result()
                            if ok:
                                targets.discard(ok)
                                fails = [x for x in fails if not x.startswith(ok)]
                                done += 1
                    if targets:
                        left = sorted(list(targets))[:10]
                        orch._sys_restart_set(state=1,    #run
                            message=(
                                f"Verifying settle… recovered {len(failed_fullnames)-len(targets)}/"
                                f"{len(failed_fullnames)} "
                                f"(left: {', '.join(left)}; "
                                f"{int(time.time()-t0)}s/{int(orch._restart_settle_sec)}s)"
                            ),
                            fails=fails,
                            failed_total=len(targets),
                            failed_list=sorted(list(targets))
                        )
                # --- settle 후 최종 상태 fix
                if not targets:
                    orch._sys_restart_set(state=2,   #done
                        message=(
                            f"Restart Process Finished: ok {total}/{total}"
                            f"(recovered during settle)"
                        ),
                        fails=[],
                        failed_total=0,
                        failed_list=[],
                        failed_nodes=[],
                        failed_procs=[]
                    )
                    return
                else:
                    summary_left = _compact(sorted(list(targets)), 10)
                    orch._sys_restart_set(state=2,   #done
                        message=(
                            f"final stage: ok {done}/{total}, fail {len(targets)}; "
                            f"failed: {summary_left}"
                        ),
                        fails=[f for f in fails if f.split(":")[0] in targets],
                        failed_total=len(targets),
                        failed_list=sorted(list(targets)),
                        failed_nodes=sorted({x.split('/')[0] for x in targets}),
                        failed_procs=sorted({x.split('/')[1] for x in targets})
                    )
            else:
                # waiting for organization
                time.sleep(2)
                orch._sys_restart_set(state=2,   #done
                    message=f"Restart Process Finished: ok {done}/{total}",
                    failed_total=0,
                    failed_list=[],
                    failed_nodes=[],
                    failed_procs=[]
                )
                return

        except Exception as e:
            fd_log.exception(f"Restart worker exception: {e}")                    
            orch._sys_restart_set(state=3, message=f"Error: {e}")
    def _sys_check_process(self, fullname):
        snap = self._sys_status_core()
        ok_set, _ = self._sys_check_processes(snap, {fullname})
        return fullname if ok_set else None
    def _sys_check_processes(self, current_status, targets):
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
            
    # ─────────────────────────────────────────────────────────────────────────────────────
    # 🔗 SYSTEM CONNECT
    # ─────────────────────────────────────────────────────────────────────────────────────    
    def _sys_connect_get(self):
        with self._sys_connect_lock:
            snap = deepcopy(self._sys_connect)
            return snap
    def _sys_connect_set(self, **kw):
        start_time = kw.get('started_at') or self._sys_connect.get("started_at")
        msg_origin = kw.get("message")        

        kw["message"] = self._tagged_time(msg_origin,start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]        
        fd_log.info(f"system connect message : {start_time}/{state_code}|{msg}")
        # update status
        with self._sys_connect_lock:
            self._sys_connect.update(kw)
            self._sys_connect["updated_at"] = time.time()  
            self._sys_connect["started_at"] = start_time    
    def _sys_connect_sequence(
        self, mtd_host, mtd_port, dmpdip, daemon_map, *,
        trace=False, return_partial=False
    ):            
        orch = self
        self.daemon_ips = self._get_daemon_ip() 
        mtd_list = self.daemon_ips.get("MTd", [])
        scd_list = self.daemon_ips.get("SCd", [])
        ccd_list = self.daemon_ips.get("CCd", [])
        self.mtd_ip = mtd_list[0] if mtd_list else None
        self.scd_ip = scd_list[0] if scd_list else None
        self.ccd_ip = ccd_list[0] if ccd_list else None
        
        fd_log.info(f"---------------------------------------------------------------------")
        fd_log.info(f"MTd:{self.mtd_ip} | SCd:{self.scd_ip} | CCd:{self.ccd_ip}")
        fd_log.info(f"---------------------------------------------------------------------")
        # ----------------------------------------------------
        # PATCH: dmpdip must be a single IP, not CSV string
        # ----------------------------------------------------
        if isinstance(dmpdip, str) and "," in dmpdip:
            fd_log.warning(f"[PATCH] dmpdip contains multiple IPs: {dmpdip}")
            dmpdip = dmpdip.split(",")[0].strip()
        events = []
        
        # start process connect             
        self._sys_connect_set(state=1, message="Connect start",started_at=time.time())
        t0 = time.time()
        def tag(step):
            return f"{step}_{int(t0*1000)}"
        # ----------------------------------------------------
        # EMd connect
        # ----------------------------------------------------
        def emd_connect_with_daemons(dm):
            daemon_list = {}
            for name, ips in dm.items():
                if name in ("PreSd", "PostSd", "VPd", "AIc", "MMc"):
                    continue
                mapped_name = _daemon_name_for_inside(name)
                if mapped_name == "MTd":
                    continue

                if not isinstance(ips, list):
                    ips = [ips]

                if len(ips) == 1:
                    daemon_list[mapped_name] = ips[0]
                else:
                    for idx, ip in enumerate(ips, start=1):
                        daemon_list[f"{mapped_name}-{idx}"] = ip

            fd_log.info(f"daemon_list:{daemon_list}")
            return {
                "DaemonList": daemon_list,
                "Section1": "mtd",
                "Section2": "connect",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "MTd",
                "Token": _make_token(),
                "Action": "run",
                "DMPDIP": orch.mtd_ip,
            }
        # ----------------------------------------------------
        # SCd reconnect helper
        # ----------------------------------------------------
        def reconnect_scd(scd_ip: str):
            try:
                pkt = {
                    "Section1": "mtd",
                    "Section2": "connect",
                    "Section3": "",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "MTd",
                    "Token": _make_token(),
                    "Action": "run",
                    "DMPDIP": orch.mtd_ip,
                    "DaemonList": {"SCd": scd_ip},
                }
                fd_log.info(f"[connect][SCd] : {scd_ip}")
                orch._mtd_command("Reconnect-SCd", pkt, wait=10.0)
                return True
            except Exception as e:
                fd_log.exception(f"SCd reconnect failed: {e}")
                return False
        # ----------------------------------------------------
        # 1. EMd Daemon Connect
        # ----------------------------------------------------
        fd_log.info(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        fd_log.info(f">>> [Connect.1] Daemon Connect")
        fd_log.info(f"daemon_map = {daemon_map}")
        # MTd 제외, MMd -> SPd로 명령
        orch._sys_connect_set(state=1, message="Essential Daemons connect")
        r1 = orch._mtd_command(
            "Connect Essential Daemons",
            emd_connect_with_daemons(daemon_map),
            wait=10.0,
        )
        try:
            daemonlist = (r1.get("DaemonList") or {}) if isinstance(r1, dict) else {}
            orch._connected_daemonlist = daemonlist
        except Exception as e:
            fd_log.exception(f"cache DaemonList failed: {e}")
            orch._connected_daemonlist = {}
        time.sleep(0.8)
        # ----------------------------------------------------
        # 2. CCd Select
        # ----------------------------------------------------
        fd_log.info(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        fd_log.info(f">>> [Connect.2] CCd.Select")
        orch._sys_connect_set(state=1, message="Camera Information")
        pkt2 = {
            "Section1": "CCd",
            "Section2": "Select",
            "Section3": "",
            "SendState": "request",
            "From": "4DOMS",
            "To": "EMd",
            "Token": _make_token(),
            "Action": "get",
            "DMPDIP": orch.mtd_ip,
        }
        r2 = orch._mtd_command("Camera Daemon Information", pkt2, wait=10.0)
        # ----------------------------------------------------
        # 3. Build PreSd/Camera map
        # ----------------------------------------------------
        fd_log.info(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        fd_log.info(f">>> [Connect.3] PreSd Connect")
        orch.presd_map = {}
        orch.cameras = []
        orch.switch_ips = set()
        try:
            ra = (r2 or {}).get("ResultArray") or []
            for row in ra:
                pre_ip = str(row.get("PreSd_id") or "").strip()
                cam_ip = str(row.get("ip") or "").strip()
                model = str(row.get("model") or "").strip()
                scd_id = str(row.get("SCd_id") or "").strip()
                try:
                    idx = int(row.get("cam_idx") or 0)
                except Exception:
                    idx = 0
                if not pre_ip or not cam_ip:
                    continue
                if pre_ip not in orch.presd_map:
                    orch.presd_map[pre_ip] = {"IP": pre_ip, "Mode": "replay", "Cameras": []}
                orch.presd_map[pre_ip]["Cameras"].append({
                    "Index": idx,
                    "IP": cam_ip,
                    "CameraModel": model,
                })
                if scd_id:
                    orch.switch_ips.add(scd_id)
                orch.cameras.append({
                    "Index": idx,
                    "IP": cam_ip,
                    "CameraModel": model,
                    "PreSdIP": pre_ip,
                    "SCdIP": scd_id,
                })
        except Exception:
            orch.presd_map = {}
            orch.cameras = []
        fd_log.info(f"presd = {list(orch.presd_map.values())}")
        # ----------------------------------------------------
        # PCd connect
        # ----------------------------------------------------
        if orch.presd_map:
            pkt3 = {
                "PreSd": list(orch.presd_map.values()),
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
                "DMPDIP": orch.mtd_ip,
            }

            r3 = orch._mtd_command("PreSd Daemon List", pkt3, wait=18.0)
            orch.state["presd_ips"] = [u["IP"] for u in orch.presd_map.values()]
            fd_log.info(f"[PATCH] Saved presd_ips = {orch.state['presd_ips']}")
        # ----------------------------------------------------
        # 4. AIc Connect
        # ----------------------------------------------------
        fd_log.info(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        fd_log.info(f">>> [Connect.4] AIc Connect")
        try:
            # 4-1) AIc 리스트 자동 생성
            aic_list = orch._build_aic_list_from_status(self)
            # ----- 자동 보정: presd_list 기반 AIc 연결 정보 추가 -----
            if (not aic_list) or not isinstance(aic_list, dict):
                fd_log.info("[AIc] aic_list empty → auto build from PreSd list")
                aic_list = {}
                presd_list = orch.state.get("presd") or []
                for item in presd_list:
                    if not isinstance(item, dict):
                        continue
                    ip = str(item.get("IP") or "").strip()
                    if not ip:
                        continue
                    alias = f"AIc-{ip.split('.')[-1]}"
                    aic_list[alias] = ip

            # ----- 보호: 여전히 비면 진행하지 않음 -----
            if aic_list and isinstance(aic_list, dict):
                orch._sys_connect_set(state=1, message="AI Clients connect")
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
                    "DMPDIP": orch.mtd_ip,
                }
                r4 = orch._mtd_command("AId Connect", pkt4, wait=10.0)
            else:
                fd_log.warning("[AIc] No AIc nodes detected; skipping AId Connect")
        except Exception as e:
            fd_log.exception(f"AIc connect failed: {e}")
        # ----------------------------------------------------
        # Update connected status
        # ----------------------------------------------------
        try:
            orch._sys_connect_set(state=1, message="Update Daemon Status")
            connected = {}
            dl = ((r1 or {}).get("DaemonList") or {}) if isinstance(r1, dict) else {}
            for dname, info in dl.items():
                if not isinstance(info, dict):
                    continue
                status = str(info.get("Status") or info.get("status") or "").upper()
                if status == "OK":
                    connected[_daemon_name_for_inside(dname)] = True
        except Exception:
            fd_log.exception("system connect state upsert failed")
        # ─────────────────────────────────────────────────────────────
        #  5. Get Version
        # ─────────────────────────────────────────────────────────────
        fd_log.info(f">>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>>")
        fd_log.info(f">>> [Connect.5] Get Version\n")

        orch._sys_connect_set(state=1, message="Get Daemon Version ...")
        temp = {} # 반드시 필요
        try:
            # ---------------------------------------------------------
            # 5-1) presd_list, aic_map 로드
            # ---------------------------------------------------------
            state_root = orch.state or {}
            presd_list = []
            aic_map = {}
            # case 1) direct camera/state
            if isinstance(state_root, dict) and (
                "presd" in state_root or "aic_connected" in state_root
            ):
                presd_list = state_root.get("presd") or []
                aic_map = state_root.get("aic_connected") or {}
            # case 2) internal SYS_STATE
            elif isinstance(state_root, dict):
                candidate = None
                if dmpdip and dmpdip in state_root and isinstance(state_root[dmpdip], dict):
                    candidate = state_root[dmpdip]
                else:
                    best_ts = -1.0
                    for key, st in state_root.items():
                        if not isinstance(st, dict):
                            continue
                        try:
                            ts = float(st.get("updated_at") or 0)
                        except Exception:
                            ts = 0.0
                        if ts >= best_ts:
                            best_ts = ts
                            candidate = st
                if isinstance(candidate, dict):
                    presd_list = candidate.get("presd") or []
                    aic_map = candidate.get("aic_connected") or {}
            # presd IP list
            presd_ips = [
                str(item.get("IP")).strip()
                for item in (presd_list or [])
                if isinstance(item, dict) and item.get("IP")
            ]
            # ---------------------------------------------------------
            # 5-2) Daemon Versions
            # ---------------------------------------------------------
            ver, presd_ver, aic_ver, final_connected = orch._sys_get_versions(dmpdip, presd_ips, aic_map)

            temp["versions"] = ver
            temp["presd_versions"] = presd_ver
            temp["aic_versions"] = aic_ver
            temp["connected_daemons"] = final_connected
            # 🔥 추가: Dashboard compatibility — AIc 버전을 versions["AIc"]에도 넣기
            if aic_ver:
                temp["versions"]["AIc"] = aic_ver

            # ---------------------------------------------------------
            # 5-3) Switch 정보 수집
            # ---------------------------------------------------------
            fd_log.info(f"Get Switch IP = {orch.switch_ips})")
            switches_info = []
            last_error = None
            MAX_TRY = 3
            if orch.switch_ips:
                for switch_ip in orch.switch_ips:
                    fd_log.info(f"--- Processing Switch: {switch_ip} ---")
                    for attempt in range(1, MAX_TRY + 1):
                        try:
                            ok = reconnect_scd(orch.scd_ip)
                            if ok:
                                fd_log.info("[OMS] SCd reconnect request sent successfully. Waiting 3s...")
                                time.sleep(3.0)
                            pkt_sw = {
                                "Section1": "Switch",
                                "Section2": "Information",
                                "Section3": "Model",
                                "SendState": "request",
                                "From": "4DOMS",
                                "To": "SCd",
                                "Token": _make_token(),
                                "Action": "get",
                                "Switches": [{"ip": switch_ip}],
                                "DMPDIP": orch.mtd_ip
                            }
                            fd_log.info(f"[info][switch] request >>\n{pkt_sw}")
                            r_sw = orch._mtd_command("Switch Information", pkt_sw, wait=10.0)
                            result_list = (r_sw or {}).get("Switches") or []
                            if result_list:
                                sw = result_list[0]
                                brand = (sw.get("Brand") or "").strip()
                                model = (sw.get("Model") or "").strip()                                    
                                switches_info.append({
                                    "IP": switch_ip,
                                    "Brand": brand,
                                    "Model": model,
                                })
                                break                                
                            if attempt < MAX_TRY:
                                time.sleep(1.0)
                        except Exception as e:
                            last_error = e
                            fd_log.exception(f"[info][switch] Switch info fetch failed on try {attempt}/{MAX_TRY} for {switch_ip}: {e}")
                            if attempt < MAX_TRY:
                                time.sleep(1.0)
                # ---- 루프 종료 후 최종 처리 ----
                if switches_info:
                    for sw in switches_info:
                        fd_log.info(f"[info][switch] response <<\nSwitch IP:{sw['IP']}, Brand:{sw['Brand']}, Model:{sw['Model']}")
                    temp["switches"] = switches_info[:]
                    orch._sys_connect_set(state=1, message="got switch information")
                else:
                    fd_log.error("[info][switch] No Switch information retrieved.")
                    if last_error:
                        fd_log.debug(f"[info][switch] Switch information final fail: {last_error}")
                    orch._sys_connect_set(state=1, message="Switch Infomation Fail")
            else:
                fd_log.debug("[info][switch] No Switch IP -> skip")
                orch._sys_connect_set(state=2, message="Finish Connection")

            fd_log.info("Finish Connection")
            # ---------------------------------------------------------
            # 5-4) SYS_STATE 저장
            # ---------------------------------------------------------

            presd_ips = presd_ips or [item["IP"] for item in orch.presd_map.values()]
            aic_ips = list(aic_ver.keys())
            daemon_ips = {
                "PreSd": presd_ips[:],
                "AIc": aic_ips[:],
            }
            for name, ips in final_connected.items():
                if name not in ("PreSd", "AIc"):
                    daemon_ips[name] = ips[:]

            final_payload = {
                "connected_daemons": final_connected,
                "versions": temp.get("versions", {}),
                "presd_versions": temp.get("presd_versions", {}),
                "aic_versions": temp.get("aic_versions", {}),
                "presd": list(orch.presd_map.values()),
                "cameras": orch.cameras,
                "switches": temp.get("switches", []),
                "updated_at": time.time(),
            }
            # upsert/save to system
            _sys_state_upsert(final_payload)
            # ────────────────────────────────────────────
            # upsert/save to camera
            # 🔥 camera 전용 payload
            # ────────────────────────────────────────────
            cam_payload = {
                "cameras": orch.cameras,
                "switches": temp.get("switches", []),
                "updated_at": time.time(),
            }
            _cam_state_upsert(cam_payload)
            # finish message
            orch._sys_connect_set(state=2, message="Finish Connection")
        except Exception as e:
            fd_log.exception(f"Collect version failed: {e}")
    def _sys_get_versions(self, dmpdip, presd_ips, aic_map):
        orch = self
        # Load previous state to avoid losing entries when partial responses come in
        state = getattr(orch, "state", {}) or {}
        prev_aic_versions = state.get("aic_versions") or {}
        versions = {}
        presd_versions = {}
        aic_versions = copy.deepcopy(prev_aic_versions)
        orch._sys_connect_set(state=1, message="Get Essential Daemons Version ...")
        # ────────────────────────────────────────────
        # 0. Connected Daemon 가져오기
        # ────────────────────────────────────────────
        connected_map = orch._get_connected_map_from_status(dmpdip) or {}
        fd_log.info(f"connected_map:{connected_map}")
        if not connected_map:
            connected_map = state.get("connected_daemons") or {}
        # exclude 는 버전 요청 대상에서만 제외, connected_daemons 계산에는 제외 X
        exclude_for_version = {"PreSd", "PostSd", "VPd", "MMc", "AIc"}
        # dict(name → count)
        connected_daemons = {}
        for name, val in connected_map.items():
            if not val:
                continue
            if name in ("PostSd", "VPd", "MMc"):
                continue
            connected_daemons[name] = [dmpdip]
        # ────────────────────────────────────────────
        # 1. Single Daemon Version
        # ────────────────────────────────────────────
        # 버전 요청 대상 목록
        single_daemon_list = [
            name for name in connected_daemons.keys()
            if name not in exclude_for_version and name != "MMd"
        ]
        # esscentil daemon version
        for name in single_daemon_list:
            try:
                r = orch._request_version(name, dmpdip)
                vmap = r.get("Version") or {}
                v = vmap.get(name)
                if v:
                    versions[name] = v                    
            except Exception as e:
                fd_log.exception(f"Version fetch failed for {name}: {e}")
        # MTd (no daemon list)
        try:
            r_mtd = orch._request_version("MTd", orch.mtd_ip)
            vmap = r_mtd.get("Version") or {}
            if "MTd" in vmap:
                versions["MTd"] = vmap["MTd"]
                connected_daemons["MTd"] = orch.self.daemon_ips.get("MTd", [])

        except Exception:
            pass
        # ────────────────────────────────────────────
        # 2. PreSd Version 요청
        # ────────────────────────────────────────────
        orch._sys_connect_set(state=1, message="Get PreSd Version ...")
        presd_ips = [str(ip).strip() for ip in orch.presd_map.keys()]            
        fd_log.info(f"[version][PreSd] : {presd_ips}")

        if presd_ips:
            try:
                expect = {
                    "ips": presd_ips,
                    "count": len(presd_ips),
                    "wait_sec": 5
                }
                msg = {
                    "Section1": "Daemon",
                    "Section2": "Information",
                    "Section3": "Version",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "PreSd",
                    "Token": _make_token(),
                    "Action": "set",
                    "DMPDIP": dmpdip,
                    "Expect": expect
                }
                fd_log.info(f"[version][PreSd] request >>\n{msg}")
                resp = tcp_json_roundtrip("127.0.0.1", orch.mtd_port, msg, timeout=7.0)[0]
                fd_log.info(f"[version][PreSd] response <<\n{resp}")

                v_presd = (resp.get("Version") or {}).get("PreSd", {})

                if isinstance(v_presd, dict):
                    versions["PreSd"] = {
                        "version": v_presd.get("version", "-"),
                        "date": v_presd.get("date", "-")
                    }

                for ip in presd_ips:
                    presd_versions[ip] = {
                        "version": v_presd.get("version", "-"),
                        "date": v_presd.get("date", "-"),
                    }
            except Exception as e:
                fd_log.exception(f"PreSd batch version fetch failed: {e}")

        # ────────────────────────────────────────────
        # 3. AId + AIc Version 요청
        # ────────────────────────────────────────────
        orch._sys_connect_set(state=1, message="Get AId Version ...")
        if "AId" in connected_daemons:
            try:
                max_retry = 3
                retry_delay = 0.5
                def _fill_aic_versions_from_vmap(vmap_obj):
                    if not isinstance(vmap_obj, dict):
                        return False
                    if "AIc" not in vmap_obj:
                        return False
                    raw = vmap_obj["AIc"]
                    if isinstance(raw, list):
                        added = False
                        for item in raw:
                            if not isinstance(item, dict):
                                continue
                            ip = item.get("ip") or item.get("IP")
                            proc_name = item.get("name") or "AIc"
                            if not ip:
                                continue
                            slot = aic_versions.setdefault(ip, {})
                            slot[proc_name] = {
                                "version": item.get("version", "-"),
                                "date": item.get("date", "-")
                            }
                            added = True
                        return added
                    if isinstance(raw, dict):
                        added = False
                        for ip, by_name in raw.items():
                            if not isinstance(by_name, dict):
                                continue
                            slot = aic_versions.setdefault(ip, {})
                            for pname, info in by_name.items():
                                if isinstance(info, dict):
                                    slot[pname] = {
                                        "version": info.get("version", "-"),
                                        "date": info.get("date", "-")
                                    }
                                    added = True
                        return added
                    return False
                for attempt in range(1, max_retry + 1):
                    expect = {
                        "AIc": list(aic_map.values()),
                        "count": len(aic_map),
                        "wait_sec": 5
                    }
                    r = orch._request_version("AId", dmpdip, extra_fields=expect)
                    vmap = orch._unwrap_version_map(r) or {}
                    if "AId" in vmap:
                        versions["AId"] = vmap["AId"]
                    if _fill_aic_versions_from_vmap(vmap):
                        break
                    if attempt < max_retry:
                        time.sleep(retry_delay)
                # AIc 누락 보완
                if isinstance(aic_map, dict):
                    for alias, ip in aic_map.items():
                        if ip not in aic_versions:
                            aic_versions[ip] = {
                                alias: {"version": "-", "date": "-"}
                            }
            except Exception as e:
                fd_log.exception(f"AId/AIc version fetch failed: {e}")

        # -----------------------------------------------------------
        # ★  SPd → MMd 이름 변경 (버전 rename)
        # -----------------------------------------------------------
        if "SPd" in versions:
            fd_log.info("[PATCH] Version map: SPd → MMd rename")
            versions["MMd"] = versions["SPd"]
            del versions["SPd"]

        # ────────────────────────────────────────────
        # 4. connected_daemons 재계산 (정확한 summary를 위해 필수)
        # ────────────────────────────────────────────
        cd_map = {}
        # 4-1. connect 단계에서 얻은 connected_map 기반 단일 데몬
        for name, _ in (connected_map or {}).items():
            if name in ("PostSd", "VPd", "MMc"): # 제외 대상
                continue
            mapped = "MMd" if name == "SPd" else name                    
            cd_map[mapped] = self.daemon_ips.get(mapped, [])

        # default
        cd_map["MTd"] = self.daemon_ips.get("MTd", [])
        # 4-2. PreSd 개수는 CONNECT 단계 기반으로 강제 반영
        if isinstance(presd_ips, list) and presd_ips:
            cd_map["PreSd"] = presd_ips[:]
        # 4-3. AIc 개수는 AId 응답 기반으로 강제 반영
        if isinstance(aic_versions, dict) and aic_versions:
            cd_map["AIc"] = list(aic_versions.keys())
            
        orch.state["connected_daemons"] = cd_map

        # -----------------------------------------------
        # CONNECTED_DAEMONS (True/False → IP list)
        # -----------------------------------------------
        final_connected = {}
        # 1) 단일 데몬
        for name, ok in (connected_map or {}).items():
            if not ok:
                continue
            if name in ("PostSd", "VPd", "MMc"):
                continue
            final_connected[name] = [dmpdip]
        # 2) PreSd
        if presd_versions:
            final_connected["PreSd"] = list(presd_versions.keys())
        # 3) AIc
        if aic_versions:
            final_connected["AIc"] = list(aic_versions.keys())
        # 4) MMd (SPd 아래 붙어있을 때)
        if "SPd" in connected_map:
            final_connected["MMd"] = orch.daemon_ips.get("MMd", [])
            final_connected.pop("SPd", None)
        # 5) MTd 항상 true
        final_connected["MTd"] = orch.daemon_ips.get("MTd", [])
        return versions, presd_versions, aic_versions, final_connected
    # 🎯 oms/system/state
    def _sys_status_core(self):
        with self._lock:
            # ---------------------------------------------------------
            # 1) 기본 nodes 리스트 구성
            # ---------------------------------------------------------
            nodes = []
            for n in self.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm,
                    "alias": n.get("alias", ""),
                    "host": n["host"],
                    "port": int(n.get("port", 19776)),
                    "status": self._cache.get(nm),
                    "ts": self._cache_ts.get(nm, 0),
                })
            payload = {
                "ok": True,
                "heartbeat_interval_sec": self.heartbeat,
                "nodes": nodes,
            }

            # ---------------------------------------------------------
            # 2) extra (SYS_STATE 최신 스냅샷)
            # ---------------------------------------------------------
            _, latest = _sys_latest_state()
            extra = latest or {}
            payload["extra"] = extra
            # 반드시 추가! summary 계산에 필요
            sys_st = extra

            # ---------------------------------------------------------
            # 3) summary 계산
            # ---------------------------------------------------------
            try:
                nodes_count = len(nodes)
                # 1) connected = SYS_STATE["connected_daemons"] 기준
                st_daemons = sys_st.get("connected_daemons", {}) or {}
                connected_total = sum(len(v) for v in st_daemons.values())
                # 2) processes/running/stopped = nodes[].status.executables 기준
                process_count = 0
                running_total = 0
                stopped_total = 0
                for node in nodes:
                    st = node.get("status") or {}
                    exes = st.get("executables") or []
                    if not isinstance(exes, list):
                        continue
                    for p in exes:
                        if not isinstance(p, dict):
                            continue
                        if not p.get("select", True):
                            continue
                        process_count += 1
                        if p.get("running"):
                            running_total += 1
                        else:
                            stopped_total += 1
                payload["summary"] = {
                    "node": nodes_count,
                    "processes": process_count,
                    "connected": connected_total,
                    "running": running_total,
                    "stopped": stopped_total,
                }
            except Exception as e:
                payload["summary"] = {"error": str(e)}

            # ---------------------------------------------------------
            # 4) state/message
            # ---------------------------------------------------------
            # get system/restart, system/connect
            # state_code
            # 0 : idel
            # 1 : running
            # 2 : done
            # 3 : error
            # state_total_code
            # 0 : Check System (Unknown) : red
            # 1 : Check Setting : red
            # 2 : Needs Restart : yellow
            # 3 : Restarting... : yellow
            # 4 : Needs Connect : blue
            # 5 : Connecting... : blue
            # 6 : Ready         : green            

            sys_restart  = self._sys_restart_get()
            sys_connect  = self._sys_connect_get()
            sys_restart_state = sys_restart.get("state")
            sys_restart_msg = sys_restart.get("message")
            sys_connect_state = sys_connect.get("state")
            sys_connect_msg = sys_connect.get("message")

            # restart running -> Restarting...
            if sys_restart_state == 1:          # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 3
                sys_total_message = sys_restart_msg
            # restart error -> need to restart
            elif sys_restart_state == 3:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 2
                sys_total_message = "Please restart each process of the system."
            # connect running -> Connecting...
            elif sys_connect_state == 1:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 5
                sys_total_message = sys_connect_msg
            # connect error -> need to connect
            elif sys_connect_state == 3:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 4
                sys_total_message = "Please connect each process of the system."
            # restart cause stopped process
            elif stopped_total > 0:
                sys_total_state_code = 2
                sys_total_message = "Please check stopped process."
            # every process connected
            elif process_count > 0 and (process_count == connected_total):
                sys_total_state_code = 6
                sys_total_message = "The system is now ready for use."
            # regist process
            elif process_count == 0 or nodes_count == 0:            
                sys_total_state_code = 1
                sys_total_message = "Please register each process of the system."                
            # need process connect
            elif process_count > connected_total:
                sys_total_state_code = 4
                sys_total_message = "Please connect each process of the system."
            # need process run
            elif process_count > running_total: 
                sys_total_state_code = 2
                sys_total_message = "Please restart each process of the system."
            # 0 : unknown status
            else:                                                   
                sys_total_state_code = 0
                sys_total_message = "Please contact the administrator."

            payload["state"] = sys_total_state_code
            payload["message"] = sys_total_message
            
            return payload
    
    # ────────────────────────────────────────────
    # 📷 /C/A/M/E/R/A/ 
    # ────────────────────────────────────────────

    # ────────────────────────────────────────────
    # 🔁 CAMERA RESTART
    # ────────────────────────────────────────────
    def _cam_restart_get(self):
        with self._cam_connect_lock:
            return deepcopy(self._cam_restart)
    def _cam_restart_set(self, **kw):
        start_time = kw.get('started_at') or self._cam_restart.get("started_at")
        msg_origin = kw.get("message")  
        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]
        fd_log.info(f"camera restart message : {state_code}|{msg}")
        with self._cam_restart_lock:
            self._cam_restart.update(kw)
            self._cam_restart["updated_at"] = time.time()
            self._cam_restart["started_at"] = start_time 
    def _camera_action_switch(self, type):
        try:
            match type:
                case 1: command_opt = "Reboot"
                case 2: command_opt = "On"
                case 3: command_opt = "Off"

            # 0) Init camera/connect state
            msg = f"Camera {command_opt} via switch"
            self._cam_restart_set(state=1,message=msg,started_at=time.time())
            fd_log.info(f"switch command = {command_opt}")            
            # ----------------------------------------------------
            # 1) switch list 로드  (_cam_connect_get)
            # ----------------------------------------------------
            try:
                sw_state = self._cam_status_core()   # ★ 사용자 요청
                fd_log.info(f"sw_state = {sw_state}")
                # 기대 구조: { "switches": [ {"IP":..., ...}, ... ] }
                switches = sw_state.get("switches") or []
            except Exception as e:
                msg = "Camera {command_opt} exception"
                self._cam_restart_set(state=2,message=msg)
                fd_log.error(f"self._cam_connect_get()")            
                return {"ok": False, "error": f"SWITCH_LOAD_FAIL: {e}"}

            if not switches:
                sw_state = self._sys_status_core()   # ★ 사용자 요청
                fd_log.info(f"sw_state = {sw_state}")
                switches = sw_state.get("switches") or []
                if not switches:
                    msg = "not switches information, connect system"
                    self._cam_restart_set(state=3,message=msg)
                    fd_log.error(f"error switches:{switches}")       
                    return {"ok": False, "error": "NO_SWITCHES"}
            
            fd_log.info(f"switches:{switches}")

            switch_list = []
            for sw in switches:
                ip = sw.get("IP") or sw.get("IPAddress")
                if not ip:
                    continue
                switch_list.append(ip)

            fd_log.info(f"switch list = {switch_list}")
            if not switch_list:                
                self._cam_restart_set(state=2,message="no valid switch ip")
                return {"ok": False, "error": "NO_VALID_SWITCH_IP"}

            msg=f"get switch list {switch_list}"
            self._cam_restart_set(state=1,message=msg)            
            # ----------------------------------------------------
            # 2) DMPDIP 선택
            # ----------------------------------------------------
            oms_ip = self.mtd_ip
            if not oms_ip:
                return {"ok": False, "error": "NO_DMPDIP"}

            fd_log.info(f"self.mtd_ip = {oms_ip}")
            # ----------------------------------------------------
            # 3) Switch Operation Payload 생성 (프론트에서 하던 것)
            # ----------------------------------------------------
            req = {
                "Switches": [{"ip": ip} for ip in switch_list],
                "Section1": "Switch",
                "Section2": "Operation",
                "Section3": command_opt,          # ★ Reboot / On / Off
                "SendState": "request",
                "From": "4DOMS",
                "To": "SCd",
                "Action": "run",
                "Token": _make_token(),
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"Switch Request Payload = {req}")
            # ----------------------------------------------------
            # 4) CCd/SCd 로 전송 (카메라 AF와 동일한 패턴)
            # ----------------------------------------------------
            def _send_scd(msg, timeout=10.0, retry=3, wait_after=0.8):
                last_err = None
                for attempt in range(1, retry + 1):
                    try:
                        resp, tag = tcp_json_roundtrip(
                            oms_ip, self.mtd_port, msg, timeout=timeout
                        )
                        time.sleep(wait_after)
                        return resp
                    except Exception as e:
                        self._cam_restart_set(state=3,message="tcp_json_roundtrip")
                        last_err = e
                        time.sleep(0.5)
                raise last_err

            msg=f"send message to switch : {command_opt}"
            self._cam_restart_set(state=1,message=msg)            
            res = _send_scd(req)
            fd_log.info(f"Switch Response: {res}")

            # ----------------------------------------------------
            # 5) 결과 집계 (SUCCESS / FAIL)
            # ----------------------------------------------------
            ok_list = []
            fail_list = []

            for sw in res.get("Switches", []):
                ip = sw.get("IPAddress") or sw.get("IP")
                status = sw.get("errorMsg") or ""
                if status == "SUCCESS":
                    ok_list.append(ip)
                else:
                    fail_list.append({"ip": ip, "error": status})
                msg=f"response from switch[{ip}]:{status}"
                self._cam_restart_set(state=1,message=msg)               

            detail = {
                "ok": ok_list,
                "fail": fail_list,
                "command": command_opt,
            }

            msg = f"response from switch {switches}:{detail}"
            self._cam_restart_set(state=1,message=msg)

            # waiting until camera all on
            if type == 1 or type == 2:
                self._cam_restart_set(state=1, message="waiting until camera boot on...")
                start_ts = time.time()   # ★ 타임아웃 시작 시간
                TIMEOUT = 60             # ★ 최대 1분
                time.sleep(5)            # wait until shutdown all cameras
                while True:
                    # 타임아웃 검사
                    if time.time() - start_ts > TIMEOUT:
                        fd_log.error("camera boot timeout: exceeded 30s")
                        self._cam_restart_set(
                            state=3,
                            message="error: camera boot timeout (30s exceeded)"
                        )
                        return {"ok": False, "error": "CAMERA_BOOT_TIMEOUT"}  # ★ error 처리
                    w_state = self._cam_status_core()
                    summary = w_state.get("summary", {})
                    cameras = summary.get("cameras", 0)
                    cam_alive = summary.get("alive", 0)

                    if cameras > 0 and cameras == cam_alive:
                        break
                    msg = f"waiting until camera boot on... {cam_alive}/{cameras}"
                    self._cam_restart_set(state=1, message=msg)
                    time.sleep(1)

            msg = f"Finish Cameras {command_opt}"
            self._cam_restart_set(state=2, message=msg)
            return {
                "ok": len(fail_list) == 0,
                "detail": detail,
                "response": res,
            }
        
        except Exception as e:
            msg = f"send switch command error"
            self._cam_restart_set(state=3,message=msg)
            return {"ok": False, "error": str(e)}
    
    # ────────────────────────────────────────────
    # 🔗 CAMERA CONNECT
    # ────────────────────────────────────────────
    # camera/connect
    def _cam_connect_get(self):
        with self._cam_connect_lock:
            return deepcopy(self._cam_connect)
    def _cam_connect_set(self, **kw):
        start_time = kw.get('started_at') or self._cam_connect.get("started_at")
        msg_origin = kw.get("message")  
        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]
        fd_log.info(f"camera connect message : {state_code}|{msg}")
        with self._cam_connect_lock:
            self._cam_connect.update(kw)
            self._cam_connect["updated_at"] = time.time()
            self._cam_connect["started_at"] = start_time    
    def _connect_all_cameras(self):        
        fd_log.info("[OMS] _connect_all_cameras")
        try:
            # 0) Init camera/connect state
            self._cam_connect_set(state=1,message="Camera connect start",started_at=time.time())

            # 1) Load current camera state from HTTP
            self._cam_connect_set(state=1,message="Load current camera state")
            try:
                raw = _http_fetch(
                    "127.0.0.1",
                    self.http_port,
                    "GET",
                    "/oms/camera/state",
                    None,
                    {},
                    timeout=3.0,
                )
                fd_log.info(f"[OMS] /oms/camera/state raw({type(raw)}): {raw}")
                raw_body = self._extract_http_body(raw)
                if isinstance(raw_body, dict):
                    state = raw_body
                elif isinstance(raw_body, bytes):
                    state = json.loads(raw_body.decode("utf-8"))
                elif isinstance(raw_body, str):
                    state = json.loads(raw_body)
                else:
                    raise ValueError(f"Unsupported HTTP body type: {type(raw_body)}")
            except Exception as e:
                fd_log.error(f"[OMS] FAILED to load state: {e}")
                state = {}

            fd_log.info(f"[OMS] Loaded state: {state}")
            state_cams = state.get("cameras") or []

            # 2) Decide command DMPDIP (never hard-code)
            oms_ip = self.mtd_ip
            if not oms_ip:
                msg = "command DMPDIP not found (state/CFG/nodes)"
                fd_log.error(msg)
                self._cam_connect_set(state=3,message=msg,error=msg)
                return {"ok": False, "error": msg}
            fd_log.info(f"[camera/connect] command DMPDIP = {oms_ip}")

            # 3) CCD / MTd helper using oms_ip                        
            def _send_ccd(msg, timeout=10.0, retry=3, wait_after=0.8):
                last_err = None
                for attempt in range(1, retry + 1):
                    try:
                        fd_log.info(f"oms_ip:{oms_ip},mtd:{self.mtd_port} msg:{msg}")
                        resp, tag = tcp_json_roundtrip(oms_ip, self.mtd_port, msg, timeout=timeout)
                        fd_log.info(f"[camera/connect] CCD response tag={tag}: {resp}")
                        time.sleep(wait_after)
                        return resp
                    except MtdTraceError as e:
                        last_err = e
                        fd_log.debug(f"[camera/connect] attempt {attempt}/{retry} failed: {e}")
                        time.sleep(0.5)
                raise last_err

            # 4) Build camera IP list / AddCamera payload
            cam_add_list = []
            ip_list = []
            fd_log.info(f"[CCd] ip list = {state_cams}")
            # set message
            self._cam_connect_set(state=1,message="Build camera IP list")
            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                fd_log.info(f"[CCd] {ip}")
                if not ip:
                    continue

                ip_list.append(ip)
                cam_add_list.append(
                    {
                        "IPAddress": ip,
                        "Model": cam.get("Model") or cam.get("ModelName") or "BGH1",
                    }
                )

            if not cam_add_list:
                self._cam_connect_set(state=3,message="No cameras in OMs state",error="No cameras in OMs state")
                return {"ok": False, "error": "No cameras in OMs state"}

            # 5) MTd connect
            mtd_payload = {
                "DaemonList": {
                    "SCd": self.scd_ip,
                    "CCd": self.ccd_ip,
                },
                "Section1": "mtd",
                "Section2": "connect",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "MTd",
                "Token": _make_token(),
                "Action": "run",
                "DMPDIP": oms_ip,
            }
            # set message
            self._cam_connect_set(state=1,message="reconnect camera control daemon")
            fd_log.info(f"[MTd.connect] request:{mtd_payload}")
            mtd_res = _send_ccd(mtd_payload, timeout=10.0, wait_after=0.3)
            if int(mtd_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3, # error
                    message="[system][connect] MTd connect failed",
                    error=f"MTd connect failed: {mtd_res}",
                )
                return {"ok": False, "step": "MTd.connect", "response": mtd_res}

            # 6) CCd Select
            select_payload = {
                "Section1": "CCd",
                "Section2": "Select",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "EMd",
                "Token": _make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.Select] request:{select_payload}")
            # set message
            select_res = _send_ccd(select_payload, timeout=10.0, wait_after=0.3)
            if int(select_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] CCd Select failed",error=f"CCd Select failed: {select_res}")
                return {"ok": False, "step": "CCd.Select", "response": select_res}

            # 7) AddCamera
            add_payload = {
                "Cameras": cam_add_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "AddCamera",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": _make_token(),
                "Action": "set",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.1.AddCamera] request:{add_payload}")
            # set message
            self._cam_connect_set(state=1,message="add camera list to daemon")                        
            add_res = _send_ccd(add_payload, timeout=10.0, wait_after=0.3)
            if int(add_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] AddCamera failed",error=f"AddCamera failed: {add_res}")
                return {"ok": False, "step": "AddCamera", "response": add_res}

            # 8) Camera Connect
            conn_payload = {
                "Section1": "Camera",
                "Section2": "Operation",
                "Section3": "Connect",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": _make_token(),
                "Action": "run",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.2.Connect] request:{conn_payload}")
            conn_res = _send_ccd(conn_payload, timeout=30.0, wait_after=0.3)
            if int(conn_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] Connect failed",error=f"Connect failed: {conn_res}")
                return {"ok": False, "step": "Connect", "response": conn_res}

            status_by_ip = {
                c["IPAddress"]: (c.get("Status") == "OK")
                for c in conn_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                # set message
                msg = f"sucess connect camera : [{ip}]"
                self._cam_connect_set(state=1,message=msg)            
                if ip in status_by_ip:
                    cam["connected"] = status_by_ip[ip]

            # 9) GetCameraInfo
            info_payload = {
                "Cameras": ip_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "GetCameraInfo",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": _make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.3.GetCameraInfo] request:{info_payload}")
            self._cam_connect_set(state=1,message="get camera information")                                
            info_res = _send_ccd(info_payload, timeout=10.0, wait_after=0.3)

            info_by_ip = {
                c["IPAddress"]: c
                for c in info_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                if ip in info_by_ip:
                    msg = f"sucess get camera info: [{ip}]"                
                    self._cam_connect_set(state=1,message=msg)            
                    cam.setdefault("info", {}).update(info_by_ip[ip])

            # 10) GetVideoFormat
            fmt_payload = {
                "Cameras": ip_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "GetVideoFormat",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": _make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.4.GetVideoFormat] request:{fmt_payload}")
            self._cam_connect_set(state=1,message="get video format information")                                
            fmt_res = _send_ccd(fmt_payload, timeout=10.0, wait_after=0.3)

            fmt_by_ip = {
                c["IPAddress"]: c
                for c in fmt_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                fmt = fmt_by_ip.get(ip)
                if fmt:
                    cam.setdefault("info", {}).update(
                        {
                            "StreamType": fmt.get("StreamType"),
                            "VideoFormatMain": fmt.get("VideoFormatMain"),
                            "VideoBitrateMain": fmt.get("VideoBitrateMain"),
                            "VideoGop": fmt.get("VideoGop"),
                            "VideoGopMain": fmt.get("VideoGopMain"),
                            "Codec": fmt.get("Codec"),
                        }
                    )

            # 11) Summary / camera-status in local "state" (for HTTP response, etc.)
            connected_ips = [ip for ip, ok in status_by_ip.items() if ok]            
            fd_log.info(f"connected camera ip:{connected_ips}")

            # ★ HERE: connected flag 업데이트
            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                cam["connected"] = (ip in connected_ips)

            # camera_status 생성
            camera_status = {}
            for cam in state_cams:
                ip = cam.get("IP")
                if not ip:
                    continue
                camera_status[ip] = "on" if cam.get("connected") else "off"
            fd_log.info(f"camera_status:{camera_status}")            
            summary = {
                "cameras": len(state_cams),
                "connected": sum(1 for c in state_cams if c.get("connected")),
                "on": sum(
                    1
                    for c in state_cams
                    if c.get("status") == "on" and not c.get("connected")
                ),
                "off": sum(1 for c in state_cams if c.get("status") == "off"),
            }            
            fd_log.info(f"summary:{summary}")

            # 12) Update global SYS_STATE (merge to existing system state entry)             
            cam_payload = {
                "cameras": [
                    {
                        "Index": cam.get("Index"),
                        "IP": cam.get("IP") or cam.get("IPAddress"),
                        "CameraModel": cam.get("CameraModel") or cam.get("Model") or cam.get("ModelName"),
                        "PreSdIP": cam.get("PreSdIP"),
                        "SCdIP": cam.get("SCdIP"),
                        "status": cam.get("status"),
                        "connected": cam.get("connected"),
                        "info": cam.get("info") or {},
                    }
                    for cam in state_cams
                ],
                "switches": state.get("switches", []),
                "camera_status": camera_status,
                "connected_ips": connected_ips,
                "updated_at": time.time(),
                "summary": summary,
            }
            _cam_state_upsert(cam_payload)

            self._cam_connect_set(state=2,#done
                                  message="success camera connection and get information")
            # update screen
            return {"ok": True}

        except Exception as e:
            fd_log.exception("connect_all_cameras unexpected error", exc_info=True)
            self._cam_connect_set(state=3,  #error
                message="[camera][connect] unexpected error",
                error=str(e),
            )
            return {"ok": False, "error": str(e)}
    def _camera_state_update(self, timeout_sec: float = 1.0) -> None:
        with COMMAND_LOCK:
            cams = CAM_STATE.get("cameras") or []
            ip_map = {str(c.get("IP")).strip(): c for c in cams if isinstance(c, dict)}
            ips = list(ip_map.keys())
            if not ips:
                return

            current_state = int(CAM_STATE.get("state") or 0)

            # ---------------------------------------------------------
            # 1) CCD Status Query (state >= 6)
            # ---------------------------------------------------------
            ccd_map = {}
            if True:
                req = {
                    "Section1": "Camera",
                    "Section2": "Information",
                    "Section3": "Status",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "CCd",
                    "Action": "get",
                    "Token": _make_token(),
                }

                try:
                    ccd_resp = tcp_json_roundtrip(
                        self.mtd_ip, self.mtd_port, req, timeout=3.0
                    )[0]
                except Exception as e:
                    fd_log.warning(f"CCd Status query fail: {e}")
                    ccd_resp = None

                if isinstance(ccd_resp, dict):
                    for item in (ccd_resp.get("Cameras") or []):
                        ip = str(item.get("IPAddress") or item.get("IP") or "").strip()
                        if not ip:
                            continue

                        status = (item.get("Status") or "").upper()
                        record = (item.get("Record") or "").upper()

                        ccd_map[ip] = {
                            "raw_status": status,
                            "connected": (status == "OK"),
                            "record": (record == "RUN"),
                            "temperature": item.get("Temperature"),
                        }

            # ---------------------------------------------------------
            # 2) CCD 결과 → cam dict에 반영
            # ---------------------------------------------------------
            for ip, info in ccd_map.items():
                cam = ip_map.get(ip)
                if not cam:
                    continue
                raw_status = info["raw_status"]
                # CCD -> connected
                cam["connected"] = (raw_status == "OK")
                # CCD -> record
                cam["record"] = info["record"]
                # NG는 record/connected 강제로 끔
                if raw_status == "NG":
                    cam["connected"] = False
                    cam["record"] = False
                if info["temperature"] is not None:
                    cam["temperature"] = info["temperature"]

            # ---------------------------------------------------------
            # 3) ping 보정 (CCD NG 는 무시)
            # ---------------------------------------------------------
            with ThreadPoolExecutor(max_workers=min(8, len(ips))) as ex:
                ping_results = {
                    ip: ex.submit(_ping_check, ip, method="auto", port=554, timeout_sec=timeout_sec)
                    for ip in ips
                }

            for ip, fut in ping_results.items():
                try:
                    alive, _ = fut.result()
                except Exception:
                    alive = None

                cam = ip_map[ip]
                cam["alive"] = bool(alive)

            # ---------------------------------------------------------
            # 4) CAM_STATE 저장 (record 포함)
            # ---------------------------------------------------------
            CAM_STATE["camera_alive"] = {
                ip: ip_map[ip].get("alive", False)
                for ip in ip_map
            }
            CAM_STATE["camera_connected"] = {
                ip: ip_map[ip].get("connected", False)
                for ip in ip_map
            }
            CAM_STATE["camera_record"] = {
                ip: ip_map[ip].get("record", False)
                for ip in ip_map
            }
            CAM_STATE["connected_ips"] = [
                ip for ip, cam in ip_map.items() if cam.get("connected")
            ]
            CAM_STATE["updated_at"] = time.time()

            #fd_log.info(f"CAM_STATE(before save):{CAM_STATE}")
            _cam_state_save()
    # 🎯 /oms/cameara/state
    def _cam_status_core(self):
        with self._lock:
            st = _cam_latest_state() or {}
            if isinstance(st, tuple):
                st = st[1] or {}

            cams = st.get("cameras") or []
            alive_map = st.get("camera_alive") or {}
            connected_map = st.get("camera_connected") or {}
            record_map = st.get("camera_record") or {}
            switches = st.get("switches") or {}

            # ---------------------------------------------------------
            # cameras 보정
            # ---------------------------------------------------------
            cameras_fixed = []
            for cam in cams:
                if not isinstance(cam, dict):
                    continue

                ip = (cam.get("IP") or "").strip()
                if not ip:
                    continue

                cam2 = dict(cam)

                # alive (ping)
                cam2["alive"] = bool(alive_map.get(ip, False))

                # connected (CCD Status)
                cam2["connected"] = bool(connected_map.get(ip, False))

                # record (CCD Record)
                cam2["record"] = bool(record_map.get(ip, False))

                # status 필드 제거
                cam2.pop("status", None)

                cameras_fixed.append(cam2)

            cams = cameras_fixed

            # ---------------------------------------------------------
            # Summary 계산
            # ---------------------------------------------------------
            total_cams = len(cams)
            alive_cams = sum(cam["alive"] for cam in cams)
            connected_cams = sum(cam["connected"] for cam in cams)
            record_cams = sum(cam["record"] for cam in cams)
            off_cams = total_cams - alive_cams

            summary = {
                "cameras": total_cams,
                "record": record_cams,
                "connected": connected_cams,
                "alive": alive_cams,
                "off": off_cams,
            }


            # ---------------------------------------------------------
            # State 계산
            # ---------------------------------------------------------
            cam_restart = self._cam_restart_get()
            cam_connect = self._cam_connect_get()

            cam_restart_state = cam_restart.get("state")
            cam_connect_state = cam_connect.get("state")
            cam_restart_msg = cam_restart.get("message")
            cam_connect_msg = cam_connect.get("message")

            #    0 : "Check System",
            #    1 : "Check Setting",
            #    2 : "Needs Restart",
            #    3 : "Restarting...",
            #    4 : "Needs Connect",
            #    5 : "Connecting...",
            #    6 : "Ready",
            #    7 : "Recording...",
            #    8 : "Recording Error"

            # Restaring... running
            # 3 : "Restarting...",
            if cam_restart_state == 1:
                state_code = 3
                message = cam_restart_msg
            # Restaring... error
            # 2 : "Needs Restart",
            elif cam_restart_state == 3:
                state_code = 2
                message = cam_connect_msg
            # Connecting... running
            # 5 : "Connecting...",
            elif cam_connect_state == 1:
                state_code = 5
                message = cam_connect_msg
            # Connecting... error
            # 4 : "Needs Connect",  
            elif cam_connect_state == 3:
                state_code = 4
                message = cam_connect_msg
            # all recording (recored)
            # 7 : "Recording...",
            elif total_cams > 0 and (total_cams == record_cams):
                state_code = 7
                message = "All cameras recording..."
            # prtial recording (recored)
            # 8 : "Recording Error"         
            elif total_cams > 0 and (0 < record_cams < total_cams):
                state_code = 8
                message = "Warning: Some cameras are NOT recording!"
            # all connected (connected) - from CCd
            # 6 : "Ready"
            elif total_cams > 0 and (total_cams == connected_cams):
                state_code = 6
                message = "The system is now ready for use."
            # partial stop - from ping
            # 2 : "Needs Restart"
            elif off_cams > 0:
                state_code = 2
                message = "Please check camera power status."
            # partial connected - from CCd/status -> conected
            # 4 : "Needs Connect",
            elif total_cams > connected_cams:
                state_code = 4
                message = "Please connect cameras."
            # partial on (some off) - ping
            # 2 : "Needs Restart",
            elif total_cams > alive_cams:
                state_code = 2
                message = "Please restart cameras."
            # non camera count
            # 1 : "Check Setting",
            elif total_cams == 0:
                state_code = 1
                message = "Please check the system."
            # undefined (unknown)
            # 1 : "Check Setting",
            else:
                state_code = 1
                message = "Please contact the administrator."

            updated_at = st.get("updated_at") or time.time()
            payload = {
                "ok": True,
                "cameras": cams,
                "connected_ips": [ip for ip, val in connected_map.items() if val],
                "camera_alive": alive_map,
                "camera_connected": connected_map,
                "camera_record": record_map,
                "switches": switches,
                "updated_at": updated_at,
                "summary": summary,
                "state": state_code,
                "message": message,
            }
            return payload
        
    # ────────────────────────────────────────────
    # 🔴 CAMERA RECORD
    # ────────────────────────────────────────────
    # camera record stop
    def _camera_record_start(self):
        """
        Full recording sequence:
        1) PreSd PREPARE
        2) CCd RUN
        3) Save record history into record/record_history.json
        4) Save detail file into record/history/<self.recording_name>.json
        """

        # ---------------------------------------------------------------------
        # 1) Load current camera state
        # ---------------------------------------------------------------------
        try:
            st = _cam_latest_state() or {}
        except Exception as e:
            return {"ok": False, "error": f"Cannot load camera/system state: {e}"}

        cameras = st.get("cameras") or []
        if not cameras:
            return {"ok": False, "error": "No cameras found"}

        camera_ips = [cam["IP"] for cam in cameras]

        # ---------------------------------------------------------------------
        # 2) Load user-config.json (Record Setting)
        # ---------------------------------------------------------------------
        config = fd_load_json_file("/config/user-config.json")
        RS = config.get("RecordSetting", {})

        cam_time      = RS.get("CameraRecordWaitTime", 1500)
        cam_synctime  = RS.get("CameraSyncWaitTime", 2000)
        cam_synclimit = RS.get("CameraSyncLimit", 10)
        cam_syncskip  = RS.get("CameraSyncSkip", False)
        use_audio     = RS.get("UseAudio", False)

        # ---------------------------------------------------------------------
        # 3) Generate record name
        # ---------------------------------------------------------------------
        now = time.localtime()
        self.recording_name = time.strftime("%Y_%m_%d_%H_%M_%S", now)

        # ---------------------------------------------------------------------
        # 4) Build PreSd group table
        # ---------------------------------------------------------------------
        presd_groups = {}

        for cam in cameras:
            ip = cam["IP"]
            presd_ip = cam["PreSdIP"]
            info = cam.get("info", {})

            vf = info.get("VideoFormatMain", "UHD-60")
            fps = 60
            if "-" in vf:
                try:
                    fps = int(vf.split("-")[1])
                except:
                    fps = 60

            storage_entry = {
                "IP": ip,
                "camfps": fps,
                "FrameRateConversionDenom": 1,
                "LiveGOP": 1,
                "Path": "C:\\MOVIE\\",
                "UseAudio": use_audio
            }

            presd_groups.setdefault(presd_ip, []).append(storage_entry)

        # ---------------------------------------------------------------------
        # 5) Build PreSd PREPARE packet
        # ---------------------------------------------------------------------
        presd_prepare = OrderedDict([
            ("Section1", "Camera"),
            ("Section2", "Operation"),
            ("Section3", "Prepare"),
            ("SendState", "request"),
            ("Token", _make_token()),
            ("From", "4DPD"),
            ("To", "PreSd"),
            ("Action", "set"),
            ("DMPDIP", self.mtd_ip),

            ("RecordName", self.recording_name),
            ("RecordFrameNo", 0),
            ("Record", True),
            ("CalibrationRecord", False),
            ("Limit", 0),
            ("LiveStabil", False),

            ("PreSd", [])
        ])

        for presd_ip, storage_list in presd_groups.items():
            presd_prepare["PreSd"].append({
                "IP": presd_ip,
                "Storage": storage_list
            })

        # ---------------------------------------------------------------------
        # 6) Send PREPARE
        # ---------------------------------------------------------------------
        try:
            fd_log.info(f"[record][prepare][request]:{presd_prepare}")
            presd_resp = tcp_json_roundtrip(self.mtd_ip, self.mtd_port,
                                            presd_prepare, timeout=10.0)
            fd_log.info(f"[record][prepare][response]:{presd_resp}")
        except Exception as e:
            return {"ok": False, "error": f"Prepare send failed: {e}"}

        # ---------------------------------------------------------------------
        # 7) Build CCd RUN packet
        # ---------------------------------------------------------------------
        ccdrun = OrderedDict([
            ("Section1", "Camera"),
            ("Section2", "Operation"),
            ("Section3", "Run"),
            ("SendState", "request"),
            ("Token", _make_token()),
            ("From", "4DPD"),
            ("To", "CCd"),
            ("Action", "run"),
            ("Streaming", True),
            ("Status", OrderedDict([
                ("time", cam_time),
                ("synctime", cam_synctime),
                ("syncskip", cam_syncskip),
                ("synclimit", cam_synclimit),
                ("active", camera_ips)
            ])),
            ("DMPDIP", self.mtd_ip)
        ])

        # ---------------------------------------------------------------------
        # 8) Send CCd RUN (measure time)
        # ---------------------------------------------------------------------
        send_ts = time.time() * 1000  # ms
        try:
            fd_log.info(f"[record][run][request]:{ccdrun}")
            ccdrun_resp = tcp_json_roundtrip(
                self.mtd_ip, self.mtd_port, ccdrun, timeout=20.0)
            recv_ts = time.time() * 1000  # ms
            # set real start time
            self.record_start_time = recv_ts
            diff_ms = recv_ts - send_ts

            fd_log.info(f"[record][run][response]:{ccdrun_resp}")
            fd_log.info(f"[record][sync-diff-ms]: {diff_ms:.2f}")

        except Exception as e:
            return {"ok": False, "error": f"CCd run failed: {e}"}

        # ---------------------------------------------------------------------
        # 9) Save record history (record/record_history.json)
        # ---------------------------------------------------------------------
        history = fd_load_json_file("/record/record_history.json")
        history.setdefault("history", [])

        detail_path = f"/record/history/recorded/{self.recording_name}.json"

        # ① prefix 읽기
        config = fd_load_json_file("/config/user-config.json")
        pref = config.get("prefix", {})
        sel = pref.get("select-item", 0)
        items = pref.get("list", [])
        selected_prefix = ""
        if 0 <= sel < len(items):
            selected_prefix = items[sel]["name"]

        # --- NEW: start time is stored at record START ---
        record_start_hms = fd_format_datetime(self.record_start_time)
        history["history"].append({
            self.recording_name: {
                "file-location": f"/web/record/history/recorded/{self.recording_name}.json",
                "prefix": selected_prefix,              # ← 추가됨
                "record-start-time": record_start_hms,   # ← 추가됨
                "record-end-time": "",
                "recording-time": ""
            }
        })
        fd_save_json_file("/record/record_history.json", history)

        # ---------------------------------------------------------------------
        # Load AdjustInfo (CalibrationData.adj + UserPointData.pts)
        # ---------------------------------------------------------------------
        adj_path = fd_find_adjustinfo_file()
        adjust_data = {}

        if adj_path:
            # adj_path = "C:/4DReplay/V5/daemon/EMd/AdjustInfo/0/CalibrationData.adj"
            adj_root = os.path.dirname(adj_path)
            adjust_data = fd_load_adjust_info(adj_root)
            fd_log.info(f"[record] AdjustInfo loaded from {adj_path}")
        else:
            fd_log.warning("[record] AdjustInfo file not found")

        # Keep in memory for later use (during replay, send to daemon, etc.)
        self.current_adjustinfo = adjust_data

        # ---------------------------------------------------------------------
        # Done
        # ---------------------------------------------------------------------
        full_ts = time.strftime("%Y-%m-%d %H:%M:%S", now)
        fd_log.info("--------------------------------------------------")
        fd_log.info(f"[RECORDING STARTED][{self.recording_name}] start time:{full_ts}")
        fd_log.info("--------------------------------------------------")

        # ---------------------------------------------------------------------
        # 10) Save detail file (record/history/<self.recording_name>.json)
        # ---------------------------------------------------------------------
        detail = {
            "cameras": camera_ips,
            "record-set": {
                "prefix": selected_prefix,                         # ← 추가
                "record-start-time-ms": send_ts,
                "record-response-time-ms": recv_ts,
                "record-sync-diff-ms": diff_ms
            },
            "adjust-info": adjust_data
        }
        
        fd_save_json_file(detail_path, detail)        

        return {
            "ok": True,
            "prepare_resp": presd_resp,
            "run_resp": ccdrun_resp,
            "sync_diff_ms": diff_ms,
            "detail_file": detail_path
        }
    def _camera_record_stop(self):
        """
        Send CCd Stop command and update record history with end-time information.
        """
        req = {
            "Section1": "Camera",
            "Section2": "Operation",
            "Section3": "Stop",
            "SendState": "request",
            "Token": _make_token(),
            "From": "4DOMS",
            "To": "CCd",
            "Action": "run",
            "DMPDIP": self.mtd_ip
        }

        try:
            # ------------------------------------------------------------------
            # 1) Send STOP packet
            # ------------------------------------------------------------------
            stop_ts = time.time() * 1000  # ms
            fd_log.info(f"[record][stop][request]:{req}")

            resp = tcp_json_roundtrip(self.mtd_ip, self.mtd_port, req, timeout=3.0)
            fd_log.info(f"[record][stop][response]:{resp}")

            # ------------------------------------------------------------------
            # 2) Compute recorded duration
            # ------------------------------------------------------------------
            if not hasattr(self, "record_start_time"):
                fd_log.error("record_start_time is missing!")
                return {"ok": False, "error": "Missing record_start_time"}

            recorded_time_ms = stop_ts - self.record_start_time

            hms = fd_format_hms_verbose(recorded_time_ms)
            fd_log.info("--------------------------------------------------")
            fd_log.info(f"[RECORDING END][{self.recording_name}] recording time : {hms}")
            fd_log.info("--------------------------------------------------")

            # ------------------------------------------------------------------
            # 3) Update detail file (record/history/<recording_name>.json)
            # ------------------------------------------------------------------
            detail_path = f"/record/history/recorded/{self.recording_name}.json"
            detail = fd_load_json_file(detail_path)

            if "record-set" not in detail:
                detail["record-set"] = {}

            detail["record-set"]["record-end-time-ms"] = stop_ts
            detail["record-set"]["recording-time-ms"] = recorded_time_ms

            # ---------------------------
            # Human-readable fields added
            # ---------------------------
            detail["record-set"]["record-start-time"] = fd_format_datetime(self.record_start_time)
            detail["record-set"]["record-end-time"]   = fd_format_datetime(stop_ts)
            detail["record-set"]["recording-time"]    = fd_format_hms_ms(recorded_time_ms)

            # -------------------------------------------------------------
            # 4) Save to record_history.json (with human-readable timestamps)
            # -------------------------------------------------------------
            # Load existing history
            history = fd_load_json_file("/record/record_history.json")
            history.setdefault("history", [])
            # record name
            rn = self.recording_name
            # detail to update
            update_fields = {
                "record-end-time": fd_format_datetime(stop_ts),
                "recording-time": fd_format_hms_ms(recorded_time_ms)
            }
            # --------- MERGE LOGIC ---------
            updated = False
            for item in history["history"]:
                if rn in item:
                    # update existing fields
                    item[rn].update(update_fields)
                    updated = True
                    break

            if not updated:
                # create new entry (if for some reason not found)
                history["history"].append({ rn: update_fields })

            fd_save_json_file("/record/record_history.json", history)
            fd_save_json_file(detail_path, detail)
            fd_log.info(f"[record][history-update] updated: {detail_path}")
            return {"ok": True, "resp": resp}
        except Exception as e:
            fd_log.error(f"record stop fail: {e}")
            return {"ok": False, "error": str(e)}
    
    # ────────────────────────────────────────────
    # 🔴 VIDEO MAKE
    # ────────────────────────────────────────────


    # 🧩 CAMERA ACTION
    # ────────────────────────────────────────────
    # connect action - focus
    def _camera_action_autofocus(self, body):
        try:
            # 0) 요청 body에서 target ip 추출
            try:
                raw_body = body
                if isinstance(raw_body, bytes):
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                elif isinstance(raw_body, str):
                    payload = json.loads(raw_body or "{}")
                elif isinstance(raw_body, dict):
                    payload = raw_body
                else:
                    payload = {}
            except Exception as e:
                fd_log.warning(f"autofocus: payload parse error: {e}")
                payload = {}

            # ⭐ target_ip 안전 파싱
            val = payload.get("ip")
            if isinstance(val, str):
                target_ip = val.strip()
            else:
                target_ip = ""

            fd_log.info(f"camera_action_autofocus -> Target IP: {target_ip}")
            # 1) /oms/camera/state 로드
            try:
                raw = _http_fetch(
                    "127.0.0.1",
                    self.http_port,
                    "GET",
                    "/oms/camera/state",
                    None,
                    {},
                    timeout=3.0,
                )
                raw_body = self._extract_http_body(raw)

                if isinstance(raw_body, dict):
                    state = raw_body
                elif isinstance(raw_body, (bytes, str)):
                    state = json.loads(
                        raw_body if isinstance(raw_body, str)
                        else raw_body.decode("utf-8")
                    )
                else:
                    raise ValueError("Unsupported body type")
            except Exception as e:
                return {"ok": False, "error": f"STATE_LOAD_FAIL: {e}"}

            # 2) 카메라 목록 필터링 (target_ip 있으면 해당 IP만)
            state_cams = state.get("cameras") or []
            ip_list = []

            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                if not ip:
                    continue
                if target_ip and ip != target_ip:
                    continue
                ip_list.append(ip)

            if not ip_list:
                return {"ok": False, "error": "NO_CAMERAS_FOUND"}

            fd_log.info(f"camera list for AF: {ip_list} (target_ip={target_ip!r})")

            # 3) DMPDIP 선택 (기존 로직 그대로)
            oms_ip = self.mtd_ip
            fd_log.info(f"self.mtd_ip = {self.mtd_ip}")

            if not oms_ip:
                return {"ok": False, "error": "NO_DMPDIP"}

            # 4) AF command 생성
            req = {
                "Cameras": [
                    {"IPAddress": ip, "OneShotAF": {"arg": "none"}}
                    for ip in ip_list
                ],
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "SetCameraInfo",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": _make_token(),
                "Action": "set",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"AF request: {req}")

            # 5) CCd 전송 (기존 로직)
            def _send_ccd(msg, timeout=10.0, retry=3, wait_after=0.8):
                last_err = None
                for attempt in range(1, retry + 1):
                    try:
                        resp, tag = tcp_json_roundtrip(
                            oms_ip, self.mtd_port, msg, timeout=timeout
                        )
                        time.sleep(wait_after)
                        return resp
                    except Exception as e:
                        last_err = e
                        time.sleep(0.5)
                raise last_err

            res = _send_ccd(req)

            # 6) 결과 집계 (요청한 ip_list 기준)
            ok_count = 0
            fail_count = 0

            fd_log.info(f"AF response: {res}")

            for cam in res.get("Cameras", []):
                ip = cam.get("IPAddress") or cam.get("IP")
                if ip not in ip_list:
                    continue
                st = cam.get("OneShotAF", {}).get("Status")
                if st == "OK":
                    ok_count += 1
                else:
                    fail_count += 1

            detail = {
                "ok_count": ok_count,
                "fail_count": fail_count,
            }

            return {
                "ok": fail_count == 0,
                "detail": detail,
                "response": res,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    # ────────────────────────────────────────────
    # 🔄 POLLING
    # ────────────────────────────────────────────    
    def _poll_node_info(self):
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
                        if nm and al is not None:
                            if al:
                                tmp[nm] = al
                    alias_map = tmp
                else:
                    alias_map = None
            except Exception:
                alias_map = None
            with self._lock:
                self._cache[name] = payload
                self._cache_ts[name] = time.time()
                # ⬇️ 핵심: 200 OK였다면 빈 dict라도 캐시 반영(= 제거 반영)
                if alias_map is not None:
                    self._cache_alias[name] = alias_map
    def _node_info_loop(self):
        while not self._stop.is_set():
            self._poll_node_info()
            self._stop.wait(self.heartbeat)
    def _camera_loop(self):
        while not self._stop.is_set():
            try:
                self._camera_state_update(timeout_sec=1)
            except Exception:
                fd_log.exception("[OMS] camera ping loop error")
            # 1 second interval
            self._stop.wait(1.0)
        
    # ────────────────────────────────────────────
    # ⭐ MAIN FUNTIONS
    # ────────────────────────────────────────────        
    def run(self):
        # looping command
        threading.Thread(target=self._node_info_loop, daemon=True).start()
        threading.Thread(target=self._camera_loop, daemon=True).start()        

        self._http_srv = ThreadingHTTPServer((self.http_host, self.http_port), self._make_handler()) 
        self._log(f"# START OMS SERVICE")
        self._log(f"# http {self.mtd_ip}:{self.http_port}")
        self._log(f"#####################################################################")
        
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
    def _make_handler(self):
        orch = self 
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
            def _send_json(self, obj, status=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            # ─────────────────────────────────────────────────────────────────────────────────────
            # 🧰 do_GET
            # ─────────────────────────────────────────────────────────────────────────────────────         
            def do_GET(self):
                try:
                    path = self.path
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    clean = (urlsplit(self.path).path.rstrip("/") or "/")
                    if clean in {"/","/dashboard"}: return _serve_static(self, "oms-dashboard.html")
                    if clean in {"/system"}: return _serve_static(self, "oms-system.html")
                    if clean in {"/command"}: return _serve_static(self, "oms-command.html")
                    if clean in {"/camera"}: return _serve_static(self, "oms-camera.html")
                    if clean in {"/record"}: return _serve_static(self, "oms-record.html")
                    if clean in {"/liveview"}: return _serve_static(self, "oms-liveview.html")
                    if clean in {"/user"}: return _serve_static(self, "user-config.html")

                    # ──────────────────────────────────────────────────────
                    # 📦 GET : proxy
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
                    # ──────────────────────────────────────────────────────
                    # 📦 GET : Logs
                    # ──────────────────────────────────────────────────────
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
                        if not date: 
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
                            with open(log_file, "rb") as f:
                                if tail_bytes > 0 and size > tail_bytes:
                                    f.seek(size - tail_bytes)
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
                    # 🧩 GET : /oms/config
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")
                    # ──────────────────────────────────────────────────────
                    # 🧩 GET : /oms/config/meta
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "config", "meta"]:
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
                    # ──────────────────────────────────────────────────────
                    # 🧩 GET : /oms/hostip
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "hostip"]:
                        qs = parse_qs(urlsplit(self.path).query)
                        peer = (qs.get("peer") or [""])[0].strip()
                        # 브라우저가 붙은 원격 주소(프록시 없다는 가정)도 힌트로 제공
                        client_ip = self.client_address[0]
                        ip = _guess_server_ip(peer or client_ip)
                        return self._write(200, json.dumps({"ok": True, "ip": ip, "client": client_ip}).encode())
                    # ──────────────────────────────────────────────────────
                    # 🚀 GET : /oms/mtd-query
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "mtd-query"]:
                        return self._write(
                            405,
                            json.dumps({
                                "ok": False,
                                "error": "method not allowed",
                                "hint": "use POST with JSON body: {host, port, message, timeout}"
                            }).encode("utf-8")
                        )
                    # ──────────────────────────────────────────────────────
                    # 1️⃣ GET : /oms/system/state
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system", "state"]:
                        status = orch._sys_status_core()
                        return self._write(200, json.dumps(status, ensure_ascii=False).encode("utf-8"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣ GET /oms/system/process-list
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system", "process-list"]:
                        plist = orch._get_process_list()
                        body = json.dumps({"processes": plist}, ensure_ascii=False).encode("utf-8")
                        return self._write(200, body)
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ GET /oms/system/restart/state
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "system", "restart", "state"]:
                        s = orch._sys_restart_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ GET /oms/system/connect/state
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system" , "connect", "state"]:
                        s = orch._sys_connect_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ GET /oms/system/connect/clear
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system" ,"connect", "clear"]:
                        orch._sys_connect_set(state=0, message="", events=[], started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣ GET : /oms/camera/state
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "camera", "state"]:
                        status = orch._cam_status_core()
                        return self._write(200, json.dumps(status, ensure_ascii=False).encode("utf-8"))
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ GET /oms/camera/connect/state
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "camera", "connect", "state"]:
                        s = orch._cam_connect_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-2️⃣ GET /oms/camera/liveview/status
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "camera", "liveview","status"]:
                        running = MTX.is_running()
                        self._send_json({"running": running})
                        return
                    # ──────────────────────────────────────────────────────
                    # 🔴 GET /N/O/T/ /F/O/U/N/D/
                    # ──────────────────────────────────────────────────────                    
                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            # ─────────────────────────────────────────────────────────────────────────────────────
            # 🌟 do_POST
            # cmd : curl -X POST http://127.0.0.1:19777/oms/alias/clear                    
            # ─────────────────────────────────────────────────────────────────────────────────────
            def do_POST(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    length=int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)
                    # ──────────────────────────────────────────────────────
                    # 📦 POST :proxy post
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
                    # 🎯 POST : /oms/mtd-query
                    # ──────────────────────────────────────────────────────                    # 
                    if parts==["oms", "mtd-query"]:
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
                        msg = req.get("message") or {}
                        timeout = float(req.get("timeout") or 10.0)

                        if not host or not isinstance(msg, dict):
                            return self._write(400, b'{"ok":false,"error":"bad request (need host, message)"}')

                        # 3) 보내기 직전 디버그 로그
                        _append_mtd_debug("send", host, port, message=msg)
                        try:
                            resp, tag = tcp_json_roundtrip(host, port, msg, timeout=timeout)
                        # 4) 정상 응답 디버그 로그만 남기고, 상태 갱신은 /oms/system/connect 쪽에서 처리
                            _append_mtd_debug("recv", host, port, message=msg, response=resp, tag=tag)
                            return self._write(
                                200,
                                json.dumps({"ok": True, "tag": tag, "response": resp}).encode()
                            )

                        except MtdTraceError as e:
                            # 5) MTd 트레이스 에러 로그
                            _append_mtd_debug("error", host, port, message=msg, error=str(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": str(e)}).encode()
                            )
                        except Exception as e:
                            # 6) 기타 예외도 로그
                            _append_mtd_debug("error", host, port, message=msg, error=repr(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": repr(e)}).encode()
                            )
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")                    
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config/update
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config", "update"]:
                        try:
                            data = json.loads(body.decode("utf-8", "ignore"))
                            new_index = int(data.get("index", 0))                            
                            ok, err = fd_update_prefix_item(new_index)
                            if not ok:
                                return self._write(400, json.dumps({
                                    "ok": False,
                                    "error": err
                                }).encode())
                            return self._write(200, json.dumps({
                                "ok": True,
                                "select-item": new_index
                            }).encode())
                        except Exception as e:
                            return self._write(500, json.dumps({
                                "ok": False,
                                "error": str(e)
                            }).encode())
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config/apply
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config", "apply"]:
                        try: cfg=load_config(CFG)
                        except Exception as e: return self._write(400, json.dumps({"ok":False,"error":f"load_config: {e}"}).encode())
                        changed=orch.apply_runtime(cfg)
                        return self._write(200, json.dumps({"ok":True,"applied":changed}).encode())
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/alias/clear
                    # ──────────────────────────────────────────────────────                      
                    if parts == ["oms", "alias", "clear"]:
                        try:
                            cnt = len(orch._cache_alias)
                            orch._cache_alias.clear()
                            fd_log.info(f"alias cache cleared ({cnt} entries removed)")
                            return self._write(200, json.dumps({"ok":True,"cleared":cnt}).encode())
                        except Exception as e:
                            return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode()) 
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ POST : /oms/system/restart/clear
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system" ,"restart", "clear"]: 
                        orch._sys_restart_set(state=0, total=0, sent=0, done=0, fails=[], message="", started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ POST : /oms/system/restart/all
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system" ,"restart", "all"]: 
                        # If already running, return 409 Conflict
                        cur = orch._sys_restart_get()
                        if cur.get("state") == "running":
                            return self._write(409, json.dumps({"ok":False,"error":"already_running"}).encode())
                        # reset camera info
                        try:
                            # reset connected daemons
                            orch._sys_restart_set(state=0, total=0, sent=0, done=0, fails=[], message="", started_at=0.0) 
                            ok = _cam_clear_connect_state()
                            orch._sys_restart_set(state=1,total=0,sent=0,done=0,fails=[],message="Preparing…",started_at=time.time())
                            fd_log.info(f"connect state cleared before restart (ok={ok})") 
                        except Exception as e:
                            fd_log.exception(f"connect state clear failed before restart: {e}")
                        # Perform full restart in the background
                        threading.Thread(
                            target=orch._sys_restart_process,
                            args=(orch,),
                            daemon=True
                        ).start()
                        return self._write(200, json.dumps({"ok":True}).encode())
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ POST : /oms/system/connect
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "system" ,"connect"]:
                        processes = orch._get_process_list() # [{name:"EMd", ips:[...]}, ...]
                        MTD_WHITELIST = {"MTd", "EMd", "CCd", "SCd", "PCd", "MMd", "AId"}
                        daemon_map = {}
                        # processes 는 list
                        for proc in processes:
                            name = proc.get("name")
                            ips = proc.get("ips") or []
                            if not name or len(ips) == 0:
                                continue
                            base = name.split("-")[0] # PreSd-1 → PreSd
                            # whitelist 이외는 MTd 로 보내지 않음
                            if base not in MTD_WHITELIST:
                                continue
                            # 중복 제거 (첫 번째 IP만)
                            if base not in daemon_map:
                                daemon_map[base] = ips[0]
                        fd_log.info(f"[PATCH] daemon_map override from process-list = {daemon_map}")
                        events = orch._sys_connect_sequence(orch.mtd_ip, orch.mtd_port, orch.mtd_ip, daemon_map)
                        return self._write(200, json.dumps({"ok": True, "events": events}).encode())
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ POST : /oms/system/state/upsert
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "system" ,"state", "upsert"]:
                        fd_log.info(f"/oms/system/state/upsert")
                        try:
                            req = json.loads(body.decode("utf-8", "ignore") or "{}")
                        except Exception:
                            return self._write(400, b'{"ok":false,"error":"invalid json"}')
                        # 핵심: 여기서 바로 기존 함수 호출
                        _sys_state_upsert(req)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ POST : /oms/camera/state/upsert
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "camera" ,"state", "upsert"]:
                        fd_log.info(f"/oms/camera/state/upsert")
                        try:
                            req = json.loads(body.decode("utf-8", "ignore") or "{}")
                        except Exception:
                            return self._write(400, b'{"ok":false,"error":"invalid json"}')
                        # 핵심: 여기서 바로 기존 함수 호출
                        _cam_state_upsert(req)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/reboot
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "reboot"]:                        
                        res = orch._camera_action_switch(1) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/start
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "start"]:                        
                        res = orch._camera_action_switch(2) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/stop
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "stop"]:                        
                        res = orch._camera_action_switch(3) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ POST : /oms/camera/connect/all
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "camera", "connect", "all"]:
                        fd_log.info("oms/camera/connect/all")
                        try:
                            res = orch._connect_all_cameras() or {}
                            ok = bool(res.get("ok", False))

                            if "ok" not in res:
                                res["ok"] = ok
                            if not ok and "error" not in res:
                                res["error"] = "Connect failed"

                            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                            # cam-connect는 항상 200으로, 내부 ok/error 로 판단
                            return self._write(200, body)

                        except Exception as e:
                            fd_log.exception("connect_all_cameras error")
                            body = json.dumps(
                                {"ok": False, "error": str(e)},
                                ensure_ascii=False,
                            ).encode("utf-8")
                            return self._write(500, body)
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-3️⃣ POST : /oms/camera/action/autofocus
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "autofocus"]:                        
                        fd_log.info(f"oms/camera/action/autofocus")
                        res = orch._camera_action_autofocus(body) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-1️⃣ POST : /oms/camera/action/record/start
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "record", "start"]:                        
                        fd_log.info(f"/oms/camera/action/record/start")
                        res = orch._camera_record_start()
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-1️⃣ POST : /oms/camera/action/record/stop
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "record", "stop"]:                        
                        fd_log.info(f"/oms/camera/action/record/stop")
                        res = orch._camera_record_stop()
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-2️⃣ POST : /oms/camera/liveview/on , /oms/camera/liveview/off
                    # ────────────────────────────────────────────────────── 
                    if parts == ["oms", "camera" , "liveview", "on"]:
                        ok = MTX.start()
                        self._send_json({"ok": ok})
                        return
                    if parts == ["oms", "camera" , "liveview", "off"]:
                        ok = MTX.stop()
                        self._send_json({"ok": ok})
                        return
                        # 저장 경로 매칭
                    # ──────────────────────────────────────────────────────
                    # 0️⃣-1️⃣ POST : load file "/web/config/user-config.json
                    # ────────────────────────────────────────────────────── 
                    if self.path == "/web/config/user-config.json":
                        raw_body = body
                        try:
                            data = raw_body.decode("utf-8")
                            file_path = os.path.join(WEB, "config", "user-config.json")
                            with open(file_path, "w", encoding="utf-8") as f:
                                f.write(data)
                            self._send_json({"status": "ok"})
                        except Exception as e:
                            self._send_json({"status": "error", "msg": str(e)}, status=500)
                        return
                    # ──────────────────────────────────────────────────────
                    # 🔴 POST /N/O/T/ /F/O/U/N/D/
                    # ──────────────────────────────────────────────────────
                    self._write(404, b"Not Found", ct="text/plain")
                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            def log_message(self, fmt, *args):
                # fd_log.info(fmt % args)
                # too many gabage message
                pass 
        return H

    def _log(self, msg:str):
        fd_log.info(msg)
# ─────────────────────────────────────────────────────────────
def main():
    try:
        cfg = load_config(CFG)
        fd_log.info(f"start oms service") 
    except Exception as e:
        fd_log.warning(f"fallback cfg: {e}") 
        cfg = {"http_host":"0.0.0.0","http_port":19777,"heartbeat_interval_sec":2,"nodes":[]}

    # load system state
    _sys_state_load() 
    # load camera state
    _cam_state_load() 
    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
