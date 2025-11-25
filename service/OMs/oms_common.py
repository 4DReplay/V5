# ─────────────────────────────────────────────────────────────────────────────
# oms_common.py
# - Common JSON loader utilities
# - 2025/11/24
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────

import os, time
import json
import re
import base64
import socket
import errno
import http.client
import subprocess

from pathlib import Path
from oms_env import *

# ─────────────────────────────────────────────────────────────────────────────
# json file save/load
# ─────────────────────────────────────────────────────────────────────────────
def fd_load_json_file(filename: str):
    """
    Load a JSON file using a relative path under the 'web' directory.
    Parameters
    ----------
    filename : str
        Virtual path starting with "/", for example:
            "/config/user-config.json"
            "/record/record-history.json"
    Mapping Rule
    ------------
    Given V5 project structure:
        V5/
          ├─ web/
          │    ├─ config/
          │    ├─ record/
          │    └─ ...
          └─ src/
               └─ common/
                    └─ oms_common.py

    The function maps:
        "/config/user-config.json"
            →  V5/web/config/user-config.json
    Returns
    -------
    dict
        Parsed JSON data, or {} if file does not exist or parsing fails.
    """
    try:
        # Base directory of this file (e.g., V5/src/common/)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # Root directory of V5 project (two levels up)
        v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
        # Clean leading slash: "/config/user.json" → "config/user.json"
        clean = filename.lstrip("/")
        # Construct absolute path under V5/web/
        cfg_path = os.path.join(v5_root, "web", clean)
        cfg_path = os.path.abspath(cfg_path)
        if not os.path.exists(cfg_path):
            fd_log.warning(f"JSON file not found: {cfg_path}")
            return {}
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        fd_log.error(f"load_json_file({filename}) failed: {e}")
        return {}
def fd_save_json_file(filename: str, data: dict):
    """
    Save a JSON file into the 'web' directory using the same relative path rules
    as fd_load_json_file().
    Parameters
    ----------
    filename : str
        Virtual path starting with '/', e.g.
            "/config/user-config.json"
            "/record/record-history.json"

    data : dict
        JSON serializable dictionary to write.
    Returns
    -------
    bool
        True on success, False on failure.
    """
    try:
        # Base directory: e.g. V5/src/common/
        base_dir = os.path.dirname(os.path.abspath(__file__))
        v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))

        # Clean leading slash
        clean = filename.lstrip("/")

        # Full path = V5/web/<clean>
        save_path = os.path.join(v5_root, "web", clean)
        save_path = os.path.abspath(save_path)

        # Ensure directory exists
        save_dir = os.path.dirname(save_path)
        os.makedirs(save_dir, exist_ok=True)

        # Write JSON
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        fd_log.info(f"Saved JSON: {save_path}")
        return True

    except Exception as e:
        fd_log.error(f"fd_save_json_file({filename}) failed: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# common function - format
# ─────────────────────────────────────────────────────────────────────────────
def fd_format_hms_verbose(ms):
    sec = ms / 1000.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}h: {m:02d}m: {s:02d}s"
def fd_format_datetime(ms_value: float):
    """Convert epoch ms → 'YYYY-MM-DD HH:MM:SS'"""
    sec = ms_value / 1000.0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
def fd_format_hms_ms(ms_value: float):
    """Convert duration ms → 'HH:MM:SS.mmm' """
    total_ms = int(ms_value)
    h = total_ms // (3600 * 1000)
    m = (total_ms % (3600 * 1000)) // (60 * 1000)
    s = (total_ms % (60 * 1000)) // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

# ─────────────────────────────────────────────────────────────────────────────
# adjust files
# ─────────────────────────────────────────────────────────────────────────────
def fd_find_adjustinfo_file():
    """
    Search for AdjustInfo file inside:
        V5/daemon/ENd/AdjustInfo/0/*.adj
    Returns the first matched file path or "".
    """

    base_dir = os.path.dirname(os.path.abspath(__file__))
    v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
    target_dir = os.path.join(v5_root, "daemon", "EMd", "AdjustInfo", "0")
    if not os.path.exists(target_dir):
        return ""

    for f in os.listdir(target_dir):
        if f.lower().endswith(".adj"):
            return os.path.join(target_dir, f)

    return ""
def fd_load_adjust_info(adj_root="C:/4DReplay/V5/daemon/EMd/AdjustInfo/0"):
    """
    Load calibration & user point data from AdjustInfo folder.
    Handles Base64 decoding for CalibrationData.adj only.
    UserPointData.pts is plain JSON.
    """

    cal_path = os.path.join(adj_root, "CalibrationData.adj")
    pts_path = os.path.join(adj_root, "UserPointData.pts")

    result = {
        "calibration": {},
        "points": {}
    }

    # -----------------------------
    # 1) CalibrationData.adj (BASE64)
    # -----------------------------
    try:
        with open(cal_path, "r", encoding="utf-8") as f:
            cal_raw = json.load(f)

        summary = cal_raw.get("summary", {})

        # summary.Comment → Base64
        if "Comment" in summary:
            try:
                summary["Comment"] = base64.b64decode(summary["Comment"]).decode("utf-16")
            except:
                pass

        # summary.world_coords → Base64 → JSON
        if "world_coords" in summary:
            try:
                summary["world_coords"] = json.loads(
                    base64.b64decode(summary["world_coords"]).decode("utf-16")
                )
            except:
                pass

        # pts list → Base64
        pts_list_raw = cal_raw.get("PtsList", [])
        decoded_pts_list = []

        for item in pts_list_raw:
            dsc_id = item.get("DscID")
            raw_pts = item.get("pts")

            try:
                decoded = json.loads(
                    base64.b64decode(raw_pts).decode("utf-16")
                )
            except:
                decoded = {}

            decoded_pts_list.append({
                "DscID": dsc_id,
                "pts": decoded
            })

        result["calibration"] = {
            "summary": summary,
            "PtsList": decoded_pts_list
        }

    except Exception as e:
        fd_log.error(f"[AdjustInfo] Failed to load CalibrationData.adj: {e}")

    # -----------------------------
    # 2) UserPointData.pts (PLAIN JSON)
    # -----------------------------
    try:
        with open(pts_path, "r", encoding="utf-8") as f:
            pts_raw = json.load(f)

        # This file is NOT encoded.
        result["points"] = pts_raw

    except Exception as e:
        fd_log.error(f"[AdjustInfo] Failed to load UserPointData.pts: {e}")

    return result

# ─────────────────────────────────────────────────────────────────────────────
# USER Config
# ─────────────────────────────────────────────────────────────────────────────
def fd_load_user_config():
    try:
        if CFG_USER.exists():
            return json.loads(CFG_USER.read_text(encoding="utf-8"))
        return {}
    except Exception as e:
        fd_log.error(f"[config] load_user_config fail: {e}")
        return {}
def fd_save_user_config(cfg: dict):
    try:
        CFG_USER.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return True
    except Exception as e:
        fd_log.error(f"[config] save_user_config fail: {e}")
        return False
def fd_update_prefix_item(index: int):
    cfg = fd_load_user_config()
    if "prefix" not in cfg:
        return False, "no prefix field"
    prefix = cfg["prefix"]
    items = prefix.get("list", [])
    # bounds check
    if index < 0 or index >= len(items):
        return False, "index out of range"
    # update value
    prefix["select-item"] = index
    ok = fd_save_user_config(cfg)
    if not ok:
        return False, "save failed"
    fd_log.info(f"[prefix] Updated select-item -> {index}")
    return True, ""

# ─────────────────────────────────────────────────────────────
# 🛠️ UTILITY
# ─────────────────────────────────────────────────────────────
def fd_append_mtd_debug(direction, host, port, message=None, response=None, error=None, tag=None):
    """
    direction: 'send' | 'recv' | 'error'
    각 /oms/mtd-query 호출마다 JSONL 한 줄씩 기록.
    """
    try:
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
def fd_daemon_name_for_inside(n: str) -> str: return "SPd" if n == "MMd" else n
def fd_make_token() -> str:
    ts = int(time.time() * 1000)
    lt = time.localtime()
    return f"{lt.tm_hour:02d}{lt.tm_min:02d}_{ts}_{hex(ts)[-3:]}"
def fd_pluck_procs(status_obj):
    if not status_obj:
        return []
    if isinstance(status_obj.get("data"), dict):
        return list(status_obj["data"].values())
    if isinstance(status_obj.get("processes"), list):
        return status_obj["processes"]
    if isinstance(status_obj.get("executables"), list):
        return status_obj["executables"]
    return []
def fd_read_proc_snapshot(host, port, proc_name, timeout=4.0):
    """노드 /status 에서 특정 프로세스의 스냅샷(pid/uptime/start_ts/running)을 뽑아온다."""
    try:
        st,_,dat = fd_http_fetch(host, port, "GET", "/status", None, None, timeout=timeout)
        if st != 200:
            return {}
        js = json.loads(dat.decode("utf-8","ignore"))
        for p in fd_pluck_procs(js):
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
def fd_is_restarted(base: dict, cur: dict, sent_at: float, saw_down: bool) -> bool:
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
def fd_retry(func, *args, retry=3, retry_delay=0.5, **kwargs):
    """
    Common retry wrapper.
    retry: how many attempts
    retry_delay: delay between attempts
    """
    last_err = None
    for attempt in range(1, retry + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < retry:
                fd_log.warning(
                    f"[retry] {func.__name__} failed ({attempt}/{retry}): {e}"
                )
                time.sleep(retry_delay)
            else:
                fd_log.error(
                    f"[retry] {func.__name__} final fail ({attempt}/{retry}): {e}"
                )
                raise last_err

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
def fd_ping_check(ip: str, method: str = "auto", port: int = 554, timeout_sec: float = 1.0) -> tuple[bool | None, str]:
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
def fd_strip_json5(text:str)->str:
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
def fd_mime(p:Path)->str:
    s=p.suffix.lower()
    if s in (".html",".htm"): return "text/html; charset=utf-8"
    if s==".js": return "application/javascript; charset=utf-8"
    if s==".css": return "text/css; charset=utf-8"
    if s==".json": return "application/json; charset=utf-8"
    if s in (".png",".jpg",".jpeg",".gif",".svg"): return f"image/{s.lstrip('.')}"
    return "application/octet-stream"
def fd_http_fetch(host:str, port:int, method:str, path:str, body:bytes|None, headers:dict|None, timeout=4.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        try: conn.close()
        except: pass


# ────────────────────────────────────────────────────────────
# EXPORTS
# ────────────────────────────────────────────────────────────
__all__ = [
    "fd_load_json_file","fd_save_json_file","fd_update_prefix_item",
    "fd_format_hms_verbose","fd_format_datetime", 
    "fd_load_adjust_info","fd_find_adjustinfo_file",
    "fd_append_mtd_debug","fd_daemon_name_for_inside",
    "fd_make_token",
    "fd_pluck_procs","fd_read_proc_snapshot",
    "fd_is_restarted",
    "fd_retry",
    "fd_ping_check",
    "fd_strip_json5","fd_mime",
    "fd_http_fetch"
]