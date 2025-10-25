# ─────────────────────────────────────────────────────────────────────────────
# dms_agent.py (final, duplicate-proof, serves web/dms-control.html)
# date: 2025-10-25
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, List
from urllib.parse import unquote

# ── paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2] if HERE.parts[-3].lower() == "service" else HERE.parent
DEFAULT_CONFIG = ROOT / "config" / "dms_config.json5"
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
    # unquoted key → "key":
    cleaned = re.sub(r'(?m)(?<!["\w])([A-Za-z_][A-Za-z0-9_]*)\s*:(?!\s*")', r'"\1":', cleaned)
    # trailing comma
    cleaned = re.sub(r",\s*([\]})])", r"\1", cleaned)
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
    """IMAGENAME 기준(부정확) 보조 수단."""
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
    """
    정확히 ExecutablePath == path 인 PID 목록.
    PowerShell Here-String으로 경로를 넣어 이스케이프 문제 제거.
    """
    p = str(path.resolve())
    try:
        ps = [
            "powershell", "-NoProfile", "-Command",
            "$p=@'\n" + p + "\n'@;"
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.ExecutablePath -eq $p } | "
            "Select-Object -ExpandProperty ProcessId"
        ]
        out = subprocess.check_output(ps, stderr=subprocess.STDOUT).decode(errors="ignore")
        return [int(s) for s in re.findall(r"\d+", out)]
    except Exception:
        return []

def _kill_all_by_path(path: Path) -> int:
    pids = _pids_by_exact_path(path)
    for pid in pids:
        _taskkill_pid(pid, True)
    return len(pids)

def _guess_type(p: Path) -> str:
    suf = p.suffix.lower()
    if suf in (".html", ".htm"): return "text/html; charset=utf-8"
    if suf == ".css": return "text/css; charset=utf-8"
    if suf == ".js": return "application/javascript; charset=utf-8"
    if suf == ".json": return "application/json; charset=utf-8"
    if suf in (".png", ".jpg", ".jpeg", ".gif", ".svg"): return "image/" + suf.lstrip(".")
    return "application/octet-stream"

# ── supervisor ──────────────────────────────────────────────────────────────
class ProcSpec:
    def __init__(self, name: str, path: Path, args: list, auto_restart: bool, start_on_boot: bool):
        self.name = name
        self.path = path
        self.args = args
        self.auto_restart = auto_restart
        self.start_on_boot = start_on_boot

class ProcState:
    def __init__(self, spec: ProcSpec):
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.last_start_ms: Optional[int] = None
        self.last_exit_code: Optional[int] = None

    def is_running(self) -> bool:
        # 핸들 확인 + OS 조회(경로 기준)
        if self.proc is not None and (self.proc.poll() is None):
            return True
        pids = _pids_by_exact_path(self.spec.path)
        if pids:
            self.pid = pids[0]
            return True
        return False

    def to_dict(self) -> dict:
        exe_posix = self.spec.path.as_posix()
        return {
            "name": self.spec.name,
            "exe": exe_posix,                     # 기존 키
            "path": exe_posix,                    # 새 UI 키
            "args": self.spec.args,
            "auto_restart": self.spec.auto_restart,
            "start_on_boot": self.spec.start_on_boot,
            "running": self.is_running(),
            "pid": self.pid,
            "last_rc": self.last_exit_code,       # 기존 키
            "last_exit_code": self.last_exit_code # 새 UI 키
        }

class DmsSupervisor:
    def __init__(self, config: dict, log_dir: Path):
        self._lock = threading.RLock()
        self.log_dir = log_dir
        ensure_dir(self.log_dir)

        self.http_host = config.get("http_host", "0.0.0.0")
        self.http_port = int(config.get("http_port", 51050))
        self.heartbeat_interval = float(config.get("heartbeat_interval_sec", 5))

        self.specs: Dict[str, ProcSpec] = {}
        self.states: Dict[str, ProcState] = {}

        for item in config.get("executables", []):
            name = str(item["name"])
            path = (ROOT / item["path"]).resolve()
            args = list(item.get("args", []))
            auto_restart = bool(item.get("auto_restart", True))
            start_on_boot = bool(item.get("start_on_boot", False))
            spec = ProcSpec(name, path, args, auto_restart, start_on_boot)
            self.specs[name] = spec
            self.states[name] = ProcState(spec)

        self._stop_evt = threading.Event()
        self._http_thread: Optional[threading.Thread] = None
        self._tick_thread: Optional[threading.Thread] = None

    # -- process control ------------------------------------------------------
    def _preclean_existing(self, spec: ProcSpec) -> int:
        """시작 전에 같은 경로의 기존/고아 프로세스를 모두 정리."""
        killed = _kill_all_by_path(spec.path)
        if not killed:
            # 경로 매칭 실패하는 환경 대비: 이미지명으로도 한 번 더
            for pid in _pids_by_image_name(spec.path.name):
                _taskkill_pid(pid, True)
                killed += 1
        if killed:
            self._log(f"[CLEAN] removed {killed} existing '{spec.name}' ({spec.path})")
        return killed

    def _enforce_singleton_after_start(self, st: ProcState):
        """시작 직후 중복이 있으면 1개만 남도록 정리."""
        time.sleep(0.3)
        pids = _pids_by_exact_path(st.spec.path)
        if not pids:
            return
        keep = st.proc.pid if (st.proc and st.proc.poll() is None) else pids[0]
        for pid in pids:
            if pid != keep:
                _taskkill_pid(pid, True)
        st.pid = keep

    def start(self, name: str) -> dict:
        with self._lock:
            st = self.states.get(name)
            if not st:
                return {"ok": False, "error": f"unknown process: {name}"}
            spec = st.spec
            if not spec.path.exists():
                return {"ok": False, "error": f"executable not found: {spec.path}"}

            # 시작 전 절대 중복 금지: 모두 제거
            self._preclean_existing(spec)

            if st.is_running():
                return {"ok": True, "msg": f"{name} already running", "pid": st.pid}

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

            proc = subprocess.Popen(
                [str(spec.path), *map(str, spec.args)],
                cwd=spec.path.parent,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                close_fds=True,
            )
            st.proc = proc
            st.pid = proc.pid
            st.last_start_ms = now_ms()
            self._log(f"[START] {name} pid={st.pid}")

            self._enforce_singleton_after_start(st)
            return {"ok": True, "msg": "started", "pid": st.pid}

    def stop(self, name: str, force: bool = True) -> dict:
        with self._lock:
            st = self.states.get(name)
            if not st:
                return {"ok": False, "error": f"unknown process: {name}"}

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

            # 동일 경로의 나머지 전부 강제 종료
            _kill_all_by_path(st.spec.path)

            st.last_exit_code = None if not st.proc else st.proc.poll()
            st.proc = None
            st.pid = None
            self._log(f"[STOP] {name} done")
            return {"ok": True, "msg": "stopped"}

    def restart(self, name: str) -> dict:
        r1 = self.stop(name, force=True)
        r2 = self.start(name)
        return {"ok": r1.get("ok") and r2.get("ok"), "stop": r1, "start": r2}

    def status(self, name: Optional[str] = None) -> dict:
        with self._lock:
            if name:
                st = self.states.get(name)
                if not st:
                    return {"ok": False, "error": f"unknown process: {name}"}
                return {"ok": True, "data": st.to_dict()}
            return {"ok": True, "data": {k: v.to_dict() for k, v in self.states.items()}}

    # -- loops & http ---------------------------------------------------------
    def _tick_loop(self):
        self._log("[DMs] tick loop started")
        for nm, st in self.states.items():
            if st.spec.start_on_boot:
                self.start(nm)

        while not self._stop_evt.is_set():
            with self._lock:
                for nm, st in self.states.items():
                    # 중복 감시: 동일 경로 PID가 2개 이상이면 하나만 남김
                    pids = _pids_by_exact_path(st.spec.path)
                    if len(pids) > 1:
                        keep = st.pid if (st.pid in pids) else pids[0]
                        for pid in pids:
                            if pid != keep:
                                _taskkill_pid(pid, True)
                        st.pid = keep

                    # 죽었고 과거에 시작된 적 있으며 auto_restart면 재기동
                    if not pids and st.spec.auto_restart and st.last_start_ms is not None:
                        self._log(f"[RESTART] {nm} restarting...")
                        self.start(nm)
            self._stop_evt.wait(self.heartbeat_interval)
        self._log("[DMs] tick loop ended")

    def _make_http_handler(self):
        sup = self

        class H(BaseHTTPRequestHandler):
            def _ok(self, code=200, payload=None):
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                if payload is None: payload = {"ok": True}
                self.wfile.write(json.dumps(payload).encode("utf-8"))

            def _parts(self):
                return [p for p in self.path.split("?")[0].split("/") if p]

            def do_GET(self):
                parts = self._parts()
                try:
                    # API
                    if not parts or parts[0] == "status":
                        name = parts[1] if len(parts) > 1 else None
                        return self._ok(payload=sup.status(name))

                    # 정적: /dms-control.html
                    if parts[0] == "dms-control.html":
                        fp = (STATIC_ROOT / "dms-control.html").resolve()
                        if fp.is_file() and str(fp).startswith(str(STATIC_ROOT.resolve())):
                            data = fp.read_bytes()
                            self.send_response(200)
                            self.send_header("Content-Type", _guess_type(fp))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data); return
                        return self._ok(404, {"ok": False, "error": "not found"})

                    # 정적: /web/...
                    if parts[0] == "web":
                        sub = unquote("/".join(parts[1:]))
                        fp = (STATIC_ROOT / sub).resolve()
                        base = STATIC_ROOT.resolve()
                        if fp.is_file() and str(fp).startswith(str(base)):
                            data = fp.read_bytes()
                            self.send_response(200)
                            self.send_header("Content-Type", _guess_type(fp))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data); return
                        return self._ok(404, {"ok": False, "error": "not found"})

                    # 루트 → 대시보드 파일 반환
                    if parts[0] in ("", "/"):
                        fp = (STATIC_ROOT / "dms-control.html").resolve()
                        if fp.is_file():
                            data = fp.read_bytes()
                            self.send_response(200)
                            self.send_header("Content-Type", _guess_type(fp))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            self.wfile.write(data); return
                        return self._ok(404, {"ok": False, "error": "not found"})

                    return self._ok(404, {"ok": False, "error": "not found"})
                except Exception as e:
                    return self._ok(500, {"ok": False, "error": repr(e)})

            def do_POST(self):
                parts = self._parts()
                try:
                    if parts and parts[0] in ("start", "stop", "restart"):
                        name = parts[1] if len(parts) > 1 else ""
                        if parts[0] == "start":
                            return self._ok(payload=sup.start(name))
                        if parts[0] == "stop":
                            return self._ok(payload=sup.stop(name, force=True))
                        if parts[0] == "restart":
                            return self._ok(payload=sup.restart(name))
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
            "http_port": 51050,
            "log_dir": str(LOG_DIR_DEFAULT),
            "heartbeat_interval_sec": 5,
            "executables": [],
        }

    log_dir = Path(cfg.get("log_dir", str(LOG_DIR_DEFAULT)))
    if not log_dir.is_absolute():
        log_dir = (ROOT / log_dir).resolve()

    sup = DmsSupervisor(cfg, log_dir)
    sup.run()

if __name__ == "__main__":
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, 0)
        except Exception:
            pass
    main()
