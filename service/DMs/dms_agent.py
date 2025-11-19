# ─────────────────────────────────────────────────────────────────────────────
# dms_agent.py (minimal-fix, consolidated)
# date: 2025-11-08
# owner: hongsu jung
# ─────────────────────────────────────────────────────────────────────────────

# -*- coding: utf-8 -*-
import json
import os
import re
import signal
import subprocess
import threading
import time
import sys, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, List
import urllib.parse as _uparse

# ── paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2] if len(HERE.parts) >= 3 and HERE.parts[-3].lower() == "service" else HERE.parent
DEFAULT_CONFIG = ROOT / "config" / "dms_config.json"
LOG_DIR_DEFAULT = ROOT / "logs" / "DMS"
STATIC_ROOT = ROOT / "web"

# ── json5-lite loader ───────────────────────────────────────────────────────
def _strip_json5_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    lines, in_str, quote = [], False, None
    for line in text.splitlines():
        buf = []
        i = 0
        while i < len(line):
            ch = line[i]
            if ch in ("'", '"'):
                if not in_str:
                    in_str, quote = True, ch
                elif quote == ch:
                    in_str, quote = False, None
                buf.append(ch); i += 1; continue
            if not in_str and i + 1 < len(line) and line[i:i+2] == "//":
                break
            buf.append(ch); i += 1
        lines.append("".join(buf))
    return "\n".join(lines)
def _json5_load(p: Path) -> dict:
    text = p.read_text(encoding="utf-8")
    cleaned = _strip_json5_comments(text)
    cleaned = re.sub(r'(?m)(?<!["\w])([A-Za-z_][A-Za-z0-9_]*)\s*:(?!\s*")', r'"\1":', cleaned)  # unquoted key
    cleaned = re.sub(r",\s*([\]})])", r"\1", cleaned)  # trailing comma
    return json.loads(cleaned)
def load_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    return _json5_load(path)
def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
def now_ms() -> int:
    return int(time.time() * 1000)

# ── Windows helpers (no psutil) ─────────────────────────────────────────────
def _taskkill_pid(pid: int, force: bool = True) -> None:
    flags = ["/T", "/F"] if force else ["/T"]
    try:
        subprocess.run(["taskkill", "/PID", str(pid), *flags],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass
def _pids_by_image_name(image: str) -> List[int]:
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {image}"],
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).decode(errors="ignore")
        rx = re.compile(rf"^{re.escape(image)}\s+(\d+)\s", re.M | re.I)
        return [int(m.group(1)) for m in rx.finditer(out)]
    except Exception:
        return []
def _pids_by_exact_path(path: Path) -> List[int]:
    p = str(path.resolve())
    try:
        ps = [
            "powershell", "-NoProfile", "-Command",
            "$p=@'\n" + p + "\n'@;"
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ExecutablePath -eq $p } | "
            "Select-Object -ExpandProperty ProcessId"
        ]
        out = subprocess.check_output(ps, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                      ).decode(errors="ignore")
        return [int(s) for s in re.findall(r"\d+", out)]
    except Exception:
        return []
def _kill_all_by_path(path: Path) -> int:
    pids = _pids_by_exact_path(path)
    for pid in pids:
        _taskkill_pid(pid, True)
    return len(pids)
def _pids_by_cmd_contains(substr: str) -> List[int]:
    """CommandLine에 substr(대소문자 무시)이 포함된 프로세스 PID 목록"""
    if not substr:
        return []
    s = substr.lower()
    try:
        ps = [
            "powershell","-NoProfile","-Command",
            ("Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains(@'\n" + s + "\n'@) } | "
             "Select-Object -ExpandProperty ProcessId")
        ]
        out = subprocess.check_output(ps, stderr=subprocess.STDOUT,
                                      creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0)
                                     ).decode(errors="ignore")
        return [int(x) for x in re.findall(r"\d+", out)]
    except Exception:
        return []
def _kill_by_cmd_contains(substr: str) -> int:
    cnt = 0
    for pid in _pids_by_cmd_contains(substr):
        _taskkill_pid(pid, True)
        cnt += 1
    return cnt
def _is_wrapper_exe(p: Path) -> bool:
    nm = p.name.lower()
    return nm in ("cmd.exe", "python.exe", "pythonw.exe", "powershell.exe")
def _guess_type(p: Path) -> str:
    suf = p.suffix.lower()
    if suf in (".html", ".htm"): return "text/html; charset=utf-8"
    if suf == ".css": return "text/css; charset=utf-8"
    if suf == ".js": return "application/javascript; charset=utf-8"
    if suf == ".json": return "application/json; charset=utf-8"
    if suf in (".png", ".jpg", ".jpeg", ".gif", ".svg"): return "image/" + suf.lstrip(".")
    return "application/octet-stream"
def _log_dirs_for_exe(exe_path: Path) -> list[Path]:
    base = exe_path.parent
    return [base / "log", base / "logs"]
def _list_logs_in_dir(d: Path) -> list[Path]:
    try:
        if not d.exists():
            return []
        return [p for p in d.glob("*.log") if p.is_file()]
    except Exception:
        return []
def _serve_static_safe(handler, rel_path: str):
    rel = rel_path.lstrip("/")
    fp = (STATIC_ROOT / rel).resolve()
    base = STATIC_ROOT.resolve()
    if not fp.is_file() or not str(fp).startswith(str(base)):
        handler.send_response(404)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        body = b'{"ok": false, "error": "not found"}'
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        try: handler.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError): pass
        return
    data = fp.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", _guess_type(fp))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try: handler.wfile.write(data)
    except (ConnectionAbortedError, BrokenPipeError): pass

# ── supervisor ──────────────────────────────────────────────────────────────
class ProcSpec:
    def __init__(self, name: str, path: Path, args: list,
                 auto_restart: bool, start_on_boot: bool, select: bool,
                 alias: str = "", cwd: Optional[Path] = None, workdir: Optional[Path] = None,
                 env: Optional[Dict[str, str]] = None, shell: bool = False):
        self.name = name
        self.path = path
        self.alias = alias or ""
        self.args = args
        self.workdir = workdir
        self.auto_restart = auto_restart
        self.start_on_boot = start_on_boot
        self.select = select
        self.cwd = cwd
        self.env = env or {}
        self.shell = shell

class ProcState:
    def __init__(self, spec: ProcSpec):
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.last_start_ms: Optional[int] = None
        self.last_exit_code: Optional[int] = None

    def is_running(self) -> bool:
        return bool(self.proc is not None and (self.proc.poll() is None))

    def is_running_fast(self, psnap: dict) -> bool:
        if self.proc is not None and (self.proc.poll() is None):
            self.pid = self.proc.pid
            return True
        p = str(self.spec.path.resolve()).lower()
        pid = psnap.get(p)
        if pid:
            self.pid = pid
            return True
        return False

    def to_dict(self) -> dict:
        exe_posix = self.spec.path.as_posix()
        return {
            "name": self.spec.name,
            "alias": self.spec.alias,
            "exe": exe_posix,
            "path": exe_posix,
            "args": self.spec.args,
            "select": self.spec.select,
            "auto_restart": self.spec.auto_restart,
            "start_on_boot": self.spec.start_on_boot,
            "running": bool(self.proc is not None and (self.proc.poll() is None)),
            "pid": self.pid,
            "last_rc": self.last_exit_code,
            "last_exit_code": self.last_exit_code
        }

class DmsSupervisor:
    def __init__(self, config: dict, log_dir: Path):
        self._lock = threading.RLock()
        self.log_dir = log_dir
        ensure_dir(self.log_dir)

        self.http_host = config.get("http_host", "0.0.0.0")
        self.http_port = int(config.get("http_port", 19776))
        self.heartbeat_interval = float(config.get("heartbeat_interval_sec", 5))

        self.specs: Dict[str, ProcSpec] = {}
        self.states: Dict[str, ProcState] = {}

        for item in config.get("executables", []):
            name  = str(item["name"])
            alias = str(item.get("alias", "") or "")
            path  = (ROOT / item["path"]).resolve() if not os.path.isabs(item["path"]) else Path(item["path"]).resolve()
            args  = list(item.get("args", []))
            auto_restart  = bool(item.get("auto_restart", True))
            start_on_boot = bool(item.get("start_on_boot", False))
            select        = bool(item.get("select", True))

            cwd_val = item.get("cwd")
            workdir = None
            if cwd_val:
                p = Path(cwd_val)
                workdir = (ROOT / cwd_val).resolve() if not p.is_absolute() else p.resolve()
            cwd = (ROOT / cwd_val).resolve() if cwd_val and not os.path.isabs(cwd_val) else Path(cwd_val) if cwd_val else None
            env = dict(item.get("env", {}))
            shell = bool(item.get("shell", False))

            spec = ProcSpec(name, path, args, auto_restart, start_on_boot, select,
                            alias=alias, cwd=cwd, env=env, shell=shell, workdir=workdir)
            self.specs[name] = spec
            self.states[name] = ProcState(spec)

        self._stop_evt = threading.Event()
        self._http_thread: Optional[threading.Thread] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._cfg_mtime = DEFAULT_CONFIG.stat().st_mtime if DEFAULT_CONFIG.exists() else 0

        # caches
        self._psnap_ts = 0.0
        self._psnap = {}  # {lowercased_path: pid}
        self._status_cache = None
        self._status_cache_ts = 0.0

        # ── maintenance lock: restart-all 등에서 auto_restart 일시 정지
        self._maint_lock = False
    
    # ── small wait helper (class method) ────────────────────────────────────
    def _wait_until(self, predicate, *, timeout=20.0, interval=0.4, hint=""):
        t0 = time.time()
        while True:
            try:
                if predicate():
                    return True
            except Exception:
                pass
            if (time.time() - t0) > timeout:
                raise TimeoutError(f"timeout while waiting {hint}".strip())
            time.sleep(interval)

    # ── atomic restart-all (class method) ───────────────────────────────────
    def restart_all(self) -> dict:
        with self._lock:
            prev_auto = {nm: st.spec.auto_restart for nm, st in self.states.items()}
            for st in self.states.values():
                st.spec.auto_restart = False
            self._maint_lock = True
        try:
            r_stop = self.stop_all()
            def _all_stopped():
                snap = self._proc_snapshot()
                for st in self.states.values():
                    if not st.spec.select:
                        continue
                    p = str(st.spec.path.resolve()).lower()
                    if (st.proc and st.proc.poll() is None) or (p in snap):
                        return False
                return True
            self._wait_until(_all_stopped, timeout=25.0, interval=0.6, hint="for all selected to stop")

            r_start = self.start_all()
            def _all_running():
                snap = self._proc_snapshot()
                any_sel = False
                for st in self.states.values():
                    if not st.spec.select:
                        continue
                    any_sel = True
                    if st.proc and (st.proc.poll() is None):
                        continue
                    p = str(st.spec.path.resolve()).lower()
                    if p in snap:
                        continue
                    return False
                return any_sel
            self._wait_until(_all_running, timeout=30.0, interval=0.6, hint="for all selected to start")
            return {"ok": True, "stop": r_stop, "start": r_start}
        finally:
            with self._lock:
                for nm, st in self.states.items():
                    st.spec.auto_restart = prev_auto.get(nm, st.spec.auto_restart)
                self._maint_lock = False        
    
    # ── proc snapshot ────────────────────────────────────────────────────────
    def _proc_snapshot(self) -> dict:
        now = time.time()
        if (now - self._psnap_ts) < 2.0 and self._psnap:
            return self._psnap
        try:
            ps = [
                "powershell", "-NoProfile", "-Command",
                ("Get-CimInstance Win32_Process | "
                 "Where-Object {$_.ExecutablePath} | "
                 "ForEach-Object { [Console]::Out.WriteLine(\"{0}|{1}\" -f $_.ProcessId, $_.ExecutablePath) }")
            ]
            out = subprocess.check_output(ps, stderr=subprocess.STDOUT,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                          ).decode(errors="ignore")
            snap = {}
            for line in out.splitlines():
                if "|" not in line: continue
                pid_str, path = line.split("|", 1)
                path = path.strip().lower()
                if not path: continue
                m = re.search(r"\d+", pid_str)
                if m:
                    snap[path] = int(m.group(0))
            self._psnap = snap
        except Exception:
            self._psnap = {}
        self._psnap_ts = now
        return self._psnap

    # ── helpers ──────────────────────────────────────────────────────────────
    def _get_state(self, name: str) -> Optional[ProcState]:
        if not name:
            return None
        low = name.lower()
        if name in self.states:
            return self.states[name]
        for st_name, st in self.states.items():
            if st_name.lower() == low:
                return st
        for st in self.states.values():
            if (st.spec.alias or "").lower() == low:
                return st
        for st in self.states.values():
            if st.spec.path.stem.lower() == low:
                return st
        return None

    def _collect_log_dirs(self, exe_path: Path) -> list[Path]:
        seen = set()
        cands: list[Path] = []
        base = exe_path.parent
        for up in [base, base.parent, base.parent.parent]:
            if not up: continue
            for dn in ("log", "logs", "Log", "Logs"):
                p = (up / dn).resolve()
                key = str(p).lower()
                if key not in seen:
                    seen.add(key); cands.append(p)
        # DMS 자체 로그 폴더 하위 logs 도 후보
        try:
            dms_log = Path(str(self.log_dir)).resolve()
            for dn in ("log", "logs"):
                p = (dms_log / dn).resolve()
                k = str(p).lower()
                if k not in seen:
                    seen.add(k); cands.append(p)
        except Exception:
            pass
        return cands

    def _log_scan_note(self, name: str, exe: Path, dirs: list[Path], found: list[Path] | None = None):
        self._log(f"[LOG-SCAN] name={name} exe={exe}")
        for d in dirs:
            self._log(f"[LOG-SCAN]  dir: {d}  exists={d.exists()}")
        if found is None:
            self._log(f"[LOG-SCAN]  found: (scan pending)")
        elif not found:
            self._log(f"[LOG-SCAN]  found: (none)")
        else:
            for fp in found:
                try:
                    self._log(f"[LOG-SCAN]  found file: {fp}  size={fp.stat().st_size}")
                except Exception:
                    self._log(f"[LOG-SCAN]  found file: {fp}")

    def _log_path_for(self, name: str, date_str: Optional[str]) -> Optional[Path]:
        st = self._get_state(name)
        if not st:
            self._log(f"[LOG-SCAN] name={name} -> unknown process")
            return None

        dirs = self._collect_log_dirs(st.spec.path)
        self._log_scan_note(name, st.spec.path, dirs, found=None)

        logs: list[Path] = []
        for d in dirs:
            try:
                if d.exists():
                    logs.extend([p for p in d.glob("*.log") if p.is_file()])
            except Exception as e:
                self._log(f"[LOG-SCAN]  scan error on {d}: {e!r}")

        self._log_scan_note(name, st.spec.path, dirs, found=logs)
        if not logs:
            return None

        if date_str and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            exact = [p for p in logs if p.name.lower() == f"{date_str}.log".lower()]
            if exact:
                pick = sorted(exact, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                self._log(f"[LOG-SCAN]  pick(exact) -> {pick}")
                return pick
            pref = [p for p in logs if p.name.lower().startswith(date_str.lower())]
            if pref:
                pick = sorted(pref, key=lambda p: p.stat().st_mtime, reverse=True)[0]
                self._log(f"[LOG-SCAN]  pick(prefix) -> {pick}")
                return pick
            self._log(f"[LOG-SCAN]  no match for date={date_str}")
            return None

        pick = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        self._log(f"[LOG-SCAN]  pick(latest) -> {pick}")
        return pick

    def _log_dates_for(self, name: str) -> list[str]:
        st = self._get_state(name)
        if not st:
            return []
        dates: set[str] = set()
        for d in _log_dirs_for_exe(st.spec.path):
            for p in _list_logs_in_dir(d):
                m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
                if m:
                    dates.add(m.group(1))
        return sorted(dates, reverse=True)

    def _log_debug_info(self, name: str) -> dict:
        st = self._get_state(name)
        if not st:
            self._log(f"[LOG-SCAN] debug name={name} -> unknown")
            return {"ok": False, "error": "unknown process", "name": name}

        dirs = self._collect_log_dirs(st.spec.path)
        info = []
        for d in dirs:
            try:
                files = []
                if d.exists():
                    files = [p.name for p in d.glob("*.log") if p.is_file()]
                info.append({"dir": str(d), "exists": d.exists(), "count": len(files),
                             "files": sorted(files, reverse=True)[:50]})
            except Exception as e:
                info.append({"dir": str(d), "exists": d.exists(), "error": repr(e)})

        self._log_scan_note(name, st.spec.path, [Path(x["dir"]) for x in info],
                            found=[(Path(x["dir"]) / fn) for x in info if x.get("exists") for fn in x.get("files", [])])
        return {"ok": True, "name": name, "exe": str(st.spec.path), "dirs": info}

    def _status_payload(self, use_snapshot: bool) -> dict:
        """
        호환 스키마 동시 제공:
          - data: {name: {...}}
          - executables: [{...}, ...]
        """
        snap = self._proc_snapshot() if use_snapshot else self._psnap
        data = {}
        executables = []
        for name, st in self.states.items():
            running = st.is_running_fast(snap) if use_snapshot else bool(st.proc and st.proc.poll() is None)
            d = st.to_dict()
            d["running"] = running
            d["pid"] = st.pid
            data[name] = d
            # 배열 항목(프론트 호환용)
            executables.append({
                "name": d["name"],
                "alias": d.get("alias",""),
                "exe": d.get("exe") or d.get("path"),
                "path": d.get("path"),
                "args": d.get("args", []),
                "select": bool(d.get("select", True)),
                "auto_restart": bool(d.get("auto_restart", True)),
                "start_on_boot": bool(d.get("start_on_boot", False)),
                "running": bool(d.get("running", False)),
                "pid": d.get("pid"),
                "last_rc": d.get("last_rc"),
                "last_exit_code": d.get("last_exit_code"),
            })
        return {"ok": True, "data": data, "executables": executables}

    def status_cached(self) -> dict:
        now = time.time()
        if (now - self._status_cache_ts) < 1.0 and self._status_cache is not None:
            return self._status_cache
        payload = self._status_payload(use_snapshot=True)
        self._status_cache = payload
        self._status_cache_ts = now
        return payload

    # ── process control ──────────────────────────────────────────────────────
    def _preclean_existing(self, spec: ProcSpec) -> int:
        # wrapper는 경로가 공유되므로 여기서 경로 기반 제거를 하지 않는다
        if _is_wrapper_exe(spec.path):
            return 0
        killed = _kill_all_by_path(spec.path)
        if not killed:
            for pid in _pids_by_image_name(spec.path.name):
                _taskkill_pid(pid, True)
                killed += 1
        if killed:
            self._log(f"[CLEAN] removed {killed} existing '{spec.name}' ({spec.path})")
        return killed

    def _enforce_singleton_after_start(self, st: ProcState):
        # wrapper(exe)면 ExecutablePath가 동일하게 찍혀 서로 오탐 → 스킵
        if _is_wrapper_exe(st.spec.path):
            return
        time.sleep(0.3)
        pids = _pids_by_exact_path(st.spec.path)
        if not pids:
            return
        keep = st.proc.pid if (st.proc and st.proc.poll() is None) else pids[0]
        for pid in pids:
            if pid != keep:
                _taskkill_pid(pid, True)
        st.pid = keep

    def _script_hint(self, spec: ProcSpec) -> Optional[str]:
        # 1) 스크립트/배치 파일명
        for a in spec.args:
            a = str(a)
            if a.lower().endswith((".py", ".bat", ".cmd")):
                return os.path.basename(a).lower()
        # 2) -m module 패턴
        for i, a in enumerate(map(str, spec.args)):
            if a == "-m" and i + 1 < len(spec.args):
                return str(spec.args[i + 1]).lower()
        if spec.alias:
            return spec.alias.lower()
        return spec.name.lower()

    def start(self, name: str) -> dict:
        with self._lock:
            st = self.states.get(name)
            if not st:
                return {"ok": False, "error": f"unknown process: {name}"}

            if not st.spec.select:
                self._log(f"[START-SKIP] {name} select=false")
                return {"ok": True, "skipped": True, "msg": f"{name} is not selected (select=false)"}

            spec = st.spec
            if not spec.path.exists():
                return {"ok": False, "error": f"executable not found: {spec.path}"}

            if st.is_running():
                return {"ok": True, "msg": f"{name} already running", "pid": st.pid}

            # 스냅샷 기반 중복 실행 체크 (wrapper는 제외)
            if not _is_wrapper_exe(spec.path):
                snap = self._proc_snapshot()
                p = str(spec.path.resolve()).lower()
                pid = snap.get(p)
                if pid:
                    st.pid = pid
                    return {"ok": True, "msg": f"{name} already running", "pid": st.pid}

            # 이중 Popen 제거: 한 번만 실행
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

            exe = str(spec.path)
            is_bat = spec.path.suffix.lower() in (".bat", ".cmd")
            if is_bat and not spec.shell:
                cmd = ["cmd.exe", "/d", "/c", exe, *map(str, spec.args)]
                use_shell = False
            else:
                cmd = [exe, *map(str, spec.args)]
                use_shell = bool(spec.shell)

            run_cwd = str(spec.cwd) if spec.cwd else str(spec.path.parent)
            run_env = os.environ.copy()
            run_env.update({k: str(v) for k, v in spec.env.items()})

            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=run_cwd,
                    env=run_env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                    close_fds=True,
                    shell=use_shell,
                )
            except Exception as e:
                self._log(f"[START-ERR] {name} failed: {e!r} cmd={cmd} cwd={run_cwd}")
                return {"ok": False, "error": f"spawn failed: {e!r}"}

            st.proc = proc
            st.pid = proc.pid
            st.last_start_ms = now_ms()
            self._log(f"[START] {name} pid={st.pid}")

            # wrapper는 스킵, 일반 exe만 singleton enforcement
            self._enforce_singleton_after_start(st)

            return {"ok": True, "msg": "started", "pid": st.pid}

    def stop(self, name: str, force: bool = True) -> dict:
        with self._lock:
            st = self.states.get(name)
            if not st:
                return {"ok": False, "error": f"unknown process: {name}"}

            # 1) 자식 핸들 종료 시도
            if st.proc and (st.proc.poll() is None):
                try:
                    if os.name == "nt":
                        os.kill(st.proc.pid, signal.SIGTERM)
                    else:
                        st.proc.terminate()
                    for _ in range(50):
                        if st.proc.poll() is not None:
                            break
                        time.sleep(0.1)
                except Exception:
                    pass

            # 2) wrapper/python → 커맨드라인 키워드로만 종료
            spec = st.spec
            killed = 0
            script_kw = self._script_hint(spec)
            img = spec.path.name.lower()
            is_wrapper = _is_wrapper_exe(spec.path)
            is_python  = img in ("python.exe", "pythonw.exe")

            if (is_wrapper or is_python) and script_kw:
                killed += _kill_by_cmd_contains(script_kw)
            else:
                # 3) 일반 exe만 경로 기준 종료
                killed += _kill_all_by_path(spec.path)

            st.last_exit_code = None if not st.proc else st.proc.poll()
            st.proc = None
            st.pid = None
            self._log(f"[STOP] {name} done (killed={killed})")
            return {"ok": True, "msg": "stopped", "killed": killed}

    def restart(self, name: str) -> dict:
        st = self.states.get(name)
        if st and not st.spec.select:
            return {"ok": False, "error": f"{name} is not selected (select=false)"}
        r1 = self.stop(name, force=True)
        r2 = self.start(name)
        return {"ok": r1.get("ok") and r2.get("ok"), "stop": r1, "start": r2}

    def start_all(self) -> dict:
        with self._lock:
            results = {}
            for nm, st in self.states.items():
                if st.spec.select:
                    results[nm] = self.start(nm)
            return {"ok": True, "results": results}

    def stop_all(self) -> dict:
        with self._lock:
            results = {}
            # 선택된 항목은 wrapper 여부와 무관하게 stop() 호출
            for nm, st in self.states.items():
                if st.spec.select:
                    results[nm] = self.stop(nm, force=True)
                else:
                    results[nm] = {"ok": True, "msg": "skipped (select=false)"}
            return {"ok": True, "results": results}

    def status(self, name: Optional[str] = None) -> dict:
        with self._lock:
            snap = self._proc_snapshot()

            def row(st: ProcState):
                running = st.is_running_fast(snap)
                d = st.to_dict()
                d["running"] = running
                d["pid"] = st.pid
                return d

            if name:
                st = self.states.get(name)
                if not st:
                    return {"ok": False, "error": f"unknown process: {name}"}
                return {"ok": True, "data": row(st)}

            return {"ok": True, "data": {k: row(v) for k, v in self.states.items()}}

    # ── loops & http ─────────────────────────────────────────────────────────
    def _tick_loop(self):
        self._log("[DMs] tick loop started")
        for nm, st in self.states.items():
            if st.spec.start_on_boot and st.spec.select:
                try: self.start(nm)
                except Exception: pass

        while not self._stop_evt.is_set():
            self._reload_config_if_needed()
            with self._lock:
                for nm, st in self.states.items():
                    # 중복 보정: 일반 exe만
                    if not _is_wrapper_exe(st.spec.path):
                        pids = _pids_by_exact_path(st.spec.path)
                        if len(pids) > 1:
                            keep = st.pid if (st.pid in pids) else pids[0]
                            for pid in pids:
                                if pid != keep: _taskkill_pid(pid, True)
                            st.pid = keep

                    # auto-restart
                    if (not self._maint_lock) and st.spec.auto_restart and st.last_start_ms is not None:
                        # wrapper라도 여기선 다시 시작 가능
                        snap = self._proc_snapshot()
                        alive = st.is_running_fast(snap)
                        if not alive:
                            self._log(f"[RESTART] {nm} restarting...")
                            try: self.start(nm)
                            except Exception: pass

                    # select=false → 강제 정지
                    if not st.spec.select and st.is_running():
                        self._log(f"[ENFORCE STOP] {nm} select=false -> stopping")
                        try: self.stop(nm, force=True)
                        except Exception: pass
            self._stop_evt.wait(self.heartbeat_interval)
        self._log("[DMs] tick loop ended")

    def _reload_config_if_needed(self):
        try:
            mt = DEFAULT_CONFIG.stat().st_mtime
        except Exception:
            return
        if mt == self._cfg_mtime:
            return
        self._cfg_mtime = mt
        try:
            cfg = load_config(DEFAULT_CONFIG)
        except Exception:
            return

        items = {str(it["name"]): it for it in cfg.get("executables", [])}
        # 업데이트/삭제
        for nm, st in list(self.states.items()):
            it = items.get(nm)
            if not it:
                try: self.stop(nm, force=True)
                except Exception: pass
                self.states.pop(nm, None)
                self.specs.pop(nm, None)
                continue
            st.spec.select = bool(it.get("select", True))
            st.spec.auto_restart = bool(it.get("auto_restart", True))
            st.spec.start_on_boot = bool(it.get("start_on_boot", False))
            st.spec.args = list(it.get("args", []))
            st.spec.path = (ROOT / it["path"]).resolve() if not os.path.isabs(it["path"]) else Path(it["path"]).resolve()

        # 신규 추가
        for nm, it in items.items():
            if nm in self.states:
                continue
            spec = ProcSpec(
                nm,
                (ROOT / it["path"]).resolve() if not os.path.isabs(it["path"]) else Path(it["path"]).resolve(),
                list(it.get("args", [])),
                bool(it.get("auto_restart", True)),
                bool(it.get("start_on_boot", False)),
                bool(it.get("select", True)),
            )
            self.specs[nm] = spec
            self.states[nm] = ProcState(spec)

    def _make_http_handler(self):
        sup = self

        class H(BaseHTTPRequestHandler):
            def _ok(self, code=200, payload=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    body = json.dumps(payload if payload is not None else {"ok": True}).encode("utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try: self.wfile.write(body)
                    except (ConnectionAbortedError, BrokenPipeError): pass
                except (ConnectionAbortedError, BrokenPipeError):
                    pass

            def _parts(self):
                return [p for p in self.path.split("?")[0].split("/") if p]

            def do_GET(self):
                try:
                    # 정규화(// → /, /web/web/… → /web/…)
                    clean_path = _uparse.urlsplit(self.path).path
                    clean_path = re.sub(r'/+', '/', clean_path)
                    norm_path  = re.sub(r'^/(?:web/)+', '/web/', clean_path)
                    if norm_path != clean_path:
                        qs = _uparse.urlsplit(self.path).query
                        loc = norm_path + (('?' + qs) if qs else '')
                        self.send_response(302)
                        self.send_header("Location", loc)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    
                    # ── 간단 별칭: /system, /web/system, /config-ui, /log-viewer 등
                    if clean_path in ("/", "/dms", "/system", "/web/system"):           return _serve_static_safe(self, "dms-system.html")
                    if clean_path in ("/configi", "/dms-config", "/web/dms-config"):    return _serve_static_safe(self, "dms-config.html")
                    if clean_path in ("/log", "/web/log-viewer"):                       return _serve_static_safe(self, "log-viewer.html")
                    
                    if clean_path == "/__diag":
                        info = {
                            "file": str(HERE),
                            "module_file": __file__,
                            "urlsplit_source": getattr(_uparse.urlsplit, "__module__", None),
                            "py_version": sys.version,
                            "cwd": os.getcwd(),
                            "mtime_here": os.path.getmtime(HERE),
                        }
                        return self._ok(200, {"ok": True, **info})

                    parts = [p for p in norm_path.split('/') if p]

                    # 루트/별칭 리다이렉트
                    if clean_path in ("/", "/dms"):
                        self.send_response(302); self.send_header("Location", "/web/dms-system.html")
                        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", "0"); self.end_headers(); return
                    if clean_path == "/oms":
                        self.send_response(302); self.send_header("Location", "/web/oms-system.html")
                        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", "0"); self.end_headers(); return

                    # 로그 API
                    if parts and parts[0] == "logs":
                        if len(parts) == 3 and parts[1] == "list":
                            name = parts[2]
                            dates = sup._log_dates_for(name)
                            return self._ok(200, {"ok": True, "name": name, "dates": dates})

                        if len(parts) == 3 and parts[1] == "debug":
                            name = parts[2]
                            info = sup._log_debug_info(name)
                            return self._ok(200, info)

                        if len(parts) >= 2:
                            name = parts[1]
                            q = _uparse.parse_qs(_uparse.urlsplit(self.path).query or "")
                            date_q = q.get("date", [None])[0]
                            tail_q = q.get("tail", [None])[0]
                            tail_bytes = 50000
                            try:
                                if tail_q is not None:
                                    tail_bytes = max(1000, min(2_000_000, int(tail_q)))
                            except Exception:
                                pass

                            fp = sup._log_path_for(name, date_q)
                            if not fp:
                                hint = sup._log_debug_info(name)
                                return self._ok(404, {"ok": False, "error": "log not found", "hint": hint})

                            try:
                                size = fp.stat().st_size
                                with open(fp, "rb") as f:
                                    if size > tail_bytes:
                                        f.seek(size - tail_bytes)
                                        blob = f.read()
                                        cut = blob.find(b"\n")
                                        if cut != -1:
                                            blob = blob[cut+1:]
                                    else:
                                        blob = f.read()
                                text = blob.decode("utf-8", errors="ignore")
                                return self._ok(200, {
                                    "ok": True,
                                    "name": name,
                                    "path": str(fp),
                                    "date": re.sub(r"\.log$", "", fp.name),
                                    "size": size,
                                    "tail": tail_bytes,
                                    "text": text
                                })
                            except Exception as e:
                                return self._ok(500, {"ok": False, "error": repr(e)})

                    # 정적 파일
                    if parts[:1] == ["web"]:
                        sub = "/".join(parts[1:])
                        return _serve_static_safe(self, sub)
                    if clean_path.endswith(".html"):
                        return _serve_static_safe(self, clean_path.lstrip("/"))
                    if parts == ["log-viewer.html"]:
                        return _serve_static_safe(self, "log-viewer.html")

                    # 상태
                    if parts == ["status-lite"]:
                        hb = getattr(sup, "heartbeat_interval", 5)
                        full = sup._status_payload(use_snapshot=False)
                        # 최소 필드만 추려서도 제공(기존 UI가 이 경량 목록을 사용)
                        lite_execs = [
                            {
                                "name": e["name"],
                                "alias": e.get("alias",""),
                                "running": bool(e.get("running")),
                                "select": bool(e.get("select", True)),
                                "pid": e.get("pid"),
                            } for e in full.get("executables", [])
                        ]
                        return self._ok(200, {
                            "ok": True,
                            "heartbeat_interval_sec": hb,
                            "executables": lite_execs,
                            # 필요 시 data도 같이 (문제 없으면 유지)
                            "data": full.get("data", {})
                        })

                    if parts == ["status"]:
                        hb = getattr(sup, "heartbeat_interval", 5)
                        st = sup.status_cached()
                        return self._ok(200, {
                            "ok": True,
                            "heartbeat_interval_sec": hb,
                            **st  # data + executables 둘 다 포함
                        })

                    if parts and parts[0] == "status" and len(parts) > 1:
                        name = parts[1]
                        one = sup.status(name)
                        # 단건 조회에도 호환용 executables 배열(길이1) 포함
                        execs = []
                        if one.get("ok") and "data" in one:
                            d = one["data"]
                            execs = [{
                                "name": d.get("name"),
                                "alias": d.get("alias",""),
                                "exe": d.get("exe") or d.get("path"),
                                "path": d.get("path"),
                                "args": d.get("args", []),
                                "select": bool(d.get("select", True)),
                                "auto_restart": bool(d.get("auto_restart", True)),
                                "start_on_boot": bool(d.get("start_on_boot", False)),
                                "running": bool(d.get("running", False)),
                                "pid": d.get("pid"),
                                "last_rc": d.get("last_rc"),
                                "last_exit_code": d.get("last_exit_code"),
                            }]
                        one["executables"] = execs
                        return self._ok(200, one)

                    # 설정 메타/본문
                    if parts == ["config", "meta"]:
                        if not DEFAULT_CONFIG.exists():
                            return self._ok(404, {"ok": False, "error": "config not found"})
                        s = DEFAULT_CONFIG.stat()
                        return self._ok(200, {
                            "ok": True,
                            "path": str(DEFAULT_CONFIG),
                            "size": s.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime)),
                        })
                    if parts == ["config"]:
                        if not DEFAULT_CONFIG.exists():
                            return self._ok(404, {"ok": False, "error": "config not found"})
                        data = DEFAULT_CONFIG.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        try: self.wfile.write(data)
                        except (ConnectionAbortedError, BrokenPipeError): pass
                        return

                    return self._ok(404, {"ok": False, "error": "not found"})
                except Exception as e:
                    return self._ok(500, {"ok": False, "error": repr(e)})

            def do_POST(self):
                parts = self._parts()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length).decode("utf-8", errors="ignore")

                    # Bulk
                    if parts == ["start-all"]:
                        return self._ok(200, sup.start_all())
                    if parts == ["stop-all"]:
                        return self._ok(200, sup.stop_all())
                    if parts == ["restart-all"]:
                        return self._ok(200, sup.restart_all())

                    # 개별 제어
                    if parts and parts[0] in ("start", "stop", "restart"):
                        name = parts[1] if len(parts) > 1 else ""
                        if parts[0] == "start":
                            return self._ok(200, sup.start(name))
                        if parts[0] == "stop":
                            return self._ok(200, sup.stop(name, force=True))
                        if parts[0] == "restart":
                            return self._ok(200, sup.restart(name))

                    # 로그 POST → GET 동일 처리
                    if parts and parts[0] == "logs":
                        return self.do_GET()

                    # 설정 저장/포맷
                    if parts == ["config"]:
                        DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                        DEFAULT_CONFIG.write_text(body, encoding="utf-8")
                        s = DEFAULT_CONFIG.stat()
                        sup._cfg_mtime = 0
                        sup._reload_config_if_needed()
                        sup._log(f"[CONFIG SAVED] {DEFAULT_CONFIG} ({s.st_size} bytes)")
                        return self._ok(200, {"ok": True})

                    if parts == ["config-format"] or parts == ["config","format"]:
                        try:
                            cleaned = _strip_json5_comments(body)
                            cleaned = re.sub(r'(?m)(?<!["\w])([A-Za-z_][A-Za-z0-9_]*)\s*:(?!\s*")', r'"\1":', cleaned)
                            cleaned = re.sub(r",\s*([\]})])", r"\1", cleaned)
                            obj = json.loads(cleaned)
                            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
                            return self._ok(200, {"ok": True, "text": pretty})
                        except Exception as ee:
                            return self._ok(400, {"ok": False, "error": repr(ee)})

                    return self._ok(404, {"ok": False, "error": "not found"})
                except Exception as e:
                    return self._ok(500, {"ok": False, "error": repr(e)})

            def log_message(self, fmt, *args):
                sup._log("[HTTP] " + (fmt % args))
        return H

    def _http_loop(self):
        srv = ThreadingHTTPServer((self.http_host, self.http_port), self._make_http_handler())
        self._log(f"[DMs] HTTP listening on {self.http_host}:{self.http_port}")
        try:
            srv.serve_forever(poll_interval=0.5)
        except Exception as e:
            self._log(f"[HTTP ERROR] {e}")

    def run(self):
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True); self._tick_thread.start()
        self._http_thread = threading.Thread(target=self._http_loop, daemon=True); self._http_thread.start()
        try:
            while not self._stop_evt.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_evt.set()
            with self._lock:
                for nm in list(self.states.keys()):
                    try: self.stop(nm, force=True)
                    except Exception: pass

    def shutdown(self):
        self._stop_evt.set()

    def _log(self, msg: str):
        ensure_dir(self.log_dir)
        line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n"
        (self.log_dir / "DMs.log").open("a", encoding="utf-8").write(line)

# ── entry ───────────────────────────────────────────────────────────────────
def main():
    try:
        cfg = load_config(DEFAULT_CONFIG)
    except Exception as e:
        ensure_dir(LOG_DIR_DEFAULT)
        (LOG_DIR_DEFAULT / "DMs.log").open("a", encoding="utf-8").write(
            f"[WARN] Using fallback config; reason: {e}\n"
        )
        cfg = {
            "http_host": "0.0.0.0",
            "http_port": 19776,
            "log_dir": str(LOG_DIR_DEFAULT),
            "heartbeat_interval_sec": 2,
            "executables": [],
        }

    log_dir = Path(cfg.get("log_dir", str(LOG_DIR_DEFAULT)))
    if not log_dir.is_absolute():
        log_dir = (ROOT / log_dir).resolve()
    try:
        sup = DmsSupervisor(cfg, log_dir)
        sup.run()
    except Exception:
        ensure_dir(LOG_DIR_DEFAULT)
        (LOG_DIR_DEFAULT / "DMs.log").open("a", encoding="utf-8").write(
            "FATAL EXCEPTION:\n" + traceback.format_exc() + "\n"
        )
        sys.exit(2)

if __name__ == "__main__":
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, 0)
        except Exception:
            pass
    main()
