# ─────────────────────────────────────────────────────────────────────────────
# dms_agent.py (minimal-fix)
# date: 2025-10-27
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
from urllib.parse import unquote, urlsplit

# ── paths ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2] if HERE.parts[-3].lower() == "service" else HERE.parent
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

def _collect_log_dirs(self, exe_path: Path) -> list[Path]:
    """exe 기준으로 위/아래 후보 log 디렉토리 수집 (두 단계 위까지)"""
    seen = set()
    cands: list[Path] = []
    base = exe_path.parent
    for up in [base, base.parent, base.parent.parent]:
        if not up:
            continue
        for dn in ("log", "logs", "Log", "Logs"):
            p = (up / dn).resolve()
            key = str(p).lower()
            if key not in seen:
                seen.add(key)
                cands.append(p)
    # supervisor 기본 로그 폴더도 후보에 추가(있다면)
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

def _log_dates_for(self, name: str) -> list[str]:
    st = self._get_state(name)
    if not st:
        return []
    dates: set[str] = set()
    for d in self._collect_log_dirs(st.spec.path):
        try:
            if not d.exists():
                continue
            for p in d.glob("*.log"):
                m = re.match(r"(\d{4}-\d{2}-\d{2})", p.stem)
                if m:
                    dates.add(m.group(1))
        except Exception:
            pass
    return sorted(dates, reverse=True)

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
    # STATIC_ROOT 하위 파일만 서빙 (디렉토리 탈출 방지)
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
    def __init__(self, name: str, path: Path, args: list, auto_restart: bool, start_on_boot: bool, select: bool, alias: str = ""):
        self.name = name
        self.path = path
        self.alias = alias or ""
        self.args = args
        self.auto_restart = auto_restart
        self.start_on_boot = start_on_boot
        self.select = select

class ProcState:
    def __init__(self, spec: ProcSpec):
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.pid: Optional[int] = None
        self.last_start_ms: Optional[int] = None
        self.last_exit_code: Optional[int] = None

    # ✅ 호환용: 자식 프로세스 핸들만 확인 (외부 스캔 없음, 매우 빠름)
    def is_running(self) -> bool:
        return bool(self.proc is not None and (self.proc.poll() is None))

    def is_running_fast(self, psnap: dict) -> bool:
        # 1) 자식 프로세스 핸들이 살아 있으면 그대로 신뢰
        if self.proc is not None and (self.proc.poll() is None):
            self.pid = self.proc.pid
            return True
        # 2) 스냅샷에서 경로로 매칭 (비차단, O(1))
        p = str(self.spec.path.resolve()).lower()
        pid = psnap.get(p)
        if pid:
            self.pid = pid
            return True
        return False

    def status(self, name: Optional[str] = None) -> dict:
        with self._lock:
            if name:
                st = self.states.get(name)
                if not st:
                    return {"ok": False, "error": f"unknown process: {name}"}
                return {"ok": True, "data": st.to_dict()}
            return {"ok": True, "data": {k: v.to_dict() for k, v in self.states.items()}}
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

    def to_dict(self) -> dict:
        exe_posix = self.spec.path.as_posix()
        return {
            "name": self.spec.name,
            "alias": self.spec.alias,           # ← 추가
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
        self.http_port = int(config.get("http_port", 51050))
        self.heartbeat_interval = float(config.get("heartbeat_interval_sec", 5))

        self.specs: Dict[str, ProcSpec] = {}
        self.states: Dict[str, ProcState] = {}

        for item in config.get("executables", []):
            name = str(item["name"])
            alias = str(item.get("alias", "") or "")
            path = (ROOT / item["path"]).resolve()
            args = list(item.get("args", []))
            auto_restart = bool(item.get("auto_restart", True))
            start_on_boot = bool(item.get("start_on_boot", False))
            select = bool(item.get("select", True))
            spec = ProcSpec(name, path, args, auto_restart, start_on_boot, select)
            self.specs[name] = spec
            self.states[name] = ProcState(spec)

        self._stop_evt = threading.Event()
        self._http_thread: Optional[threading.Thread] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._cfg_mtime = DEFAULT_CONFIG.stat().st_mtime if DEFAULT_CONFIG.exists() else 0

        # 프로세스 스냅샷 캐시(2초 TTL)
        self._psnap_ts = 0.0
        self._psnap = {}  # {lowercased_path: pid}
        self._status_cache = None
        self._status_cache_ts = 0.0

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
            out = subprocess.check_output(
                ps, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).decode(errors="ignore")
            snap = {}
            for line in out.splitlines():
                if "|" not in line:
                    continue
                pid_str, path = line.split("|", 1)
                path = path.strip().lower()
                if path:
                    # pid 추출
                    m = re.search(r"\d+", pid_str)
                    if m:
                        snap[path] = int(m.group(0))
            self._psnap = snap
        except Exception:
            self._psnap = {}
        self._psnap_ts = now
        return self._psnap

    def _get_state(self, name: str) -> Optional[ProcState]:
        if not name:
            return None
        low = name.lower()
        # 1) exact key
        if name in self.states:
            return self.states[name]
        # 2) case-insensitive by spec.name
        for st_name, st in self.states.items():
            if st_name.lower() == low:
                return st
        # 3) alias (case-insensitive)
        for st in self.states.values():
            if (st.spec.alias or "").lower() == low:
                return st
        # 4) exe stem (e.g., CCd == CCd.exe)
        for st in self.states.values():
            if st.spec.path.stem.lower() == low:
                return st
        return None

    def _collect_log_dirs(self, exe_path: Path) -> list[Path]:
        """exe 기준으로 위/아래 후보 log 디렉토리 수집"""
        seen = set()
        cands: list[Path] = []
        base = exe_path.parent
        # exe 폴더 ~ 2레벨 위까지
        for up in [base, base.parent, base.parent.parent]:
            if not up:
                continue
            for dn in ("log", "logs", "Log", "Logs"):
                p = (up / dn).resolve()
                key = str(p).lower()
                if key not in seen:
                    seen.add(key)
                    cands.append(p)
        return cands

    def _log_scan_note(self, name: str, exe: Path, dirs: list[Path], found: list[Path] | None = None):
        """찾은(또는 못 찾은) 위치를 DMs.log에 상세 기록"""
        self._log(f"[LOG-SCAN] name={name} exe={exe}")
        for d in dirs:
            self._log(f"[LOG-SCAN]  dir: {d}  exists={d.exists()}")
        if found is None:
            self._log(f"[LOG-SCAN]  found: (scan pending)")
        elif not found:
            self._log(f"[LOG-SCAN]  found: (none)")
        else:
            for fp in found:
                self._log(f"[LOG-SCAN]  found file: {fp}  size={fp.stat().st_size}")

    def _log_path_for(self, name: str, date_str: Optional[str]) -> Optional[Path]:
        st = self._get_state(name)
        if not st:
            self._log(f"[LOG-SCAN] name={name} -> unknown process")
            return None

        dirs = self._collect_log_dirs(st.spec.path)
        self._log_scan_note(name, st.spec.path, dirs, found=None)  # 스캔 시작 기록

        logs: list[Path] = []
        for d in dirs:
            try:
                if d.exists():
                    logs.extend([p for p in d.glob("*.log") if p.is_file()])
            except Exception as e:
                self._log(f"[LOG-SCAN]  scan error on {d}: {e!r}")

        # 스캔 결과 기록
        self._log_scan_note(name, st.spec.path, dirs, found=logs)

        if not logs:
            return None

        # 날짜 지정
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

        # 최신 선택
        pick = sorted(logs, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        self._log(f"[LOG-SCAN]  pick(latest) -> {pick}")
        return pick

    def _log_dates_for(self, name: str) -> list[str]:
        st = self._get_state(name)           # ← 변경
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

        # 디버그 호출도 파일에 남김
        self._log_scan_note(name, st.spec.path, [Path(x["dir"]) for x in info],
                            found=[(Path(x["dir"]) / fn) for x in info if x.get("exists") for fn in x.get("files", [])])

        return {"ok": True, "name": name, "exe": str(st.spec.path), "dirs": info}

    def _status_payload(self, use_snapshot: bool) -> dict:
        """use_snapshot=True면 _proc_snapshot()을 호출해 running 계산, False면 스냅샷 생성 생략(lite)."""
        if use_snapshot:
            snap = self._proc_snapshot()
        else:
            snap = self._psnap  # 마지막 스냅샷 그대로 (없으면 빈 dict)

        def row(st: ProcState):
            running = st.is_running_fast(snap) if use_snapshot else bool(st.proc and st.proc.poll() is None)
            d = st.to_dict()
            d["running"] = running
            d["pid"] = st.pid
            return d

        return {"ok": True, "data": {k: row(v) for k, v in self.states.items()}}

    def status_cached(self) -> dict:
        now = time.time()
        if (now - self._status_cache_ts) < 1.0 and self._status_cache is not None:
            return self._status_cache
        payload = self._status_payload(use_snapshot=True)
        self._status_cache = payload
        self._status_cache_ts = now
        return payload

    def _can_run(self, name: str) -> bool:
        st = self.states.get(name)
        return bool(st and st.spec.select)

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

        # 이름 → 아이템 매핑
        items = {str(it["name"]): it for it in cfg.get("executables", [])}
        # 1) 기존 항목 업데이트
        for nm, st in list(self.states.items()):
            it = items.get(nm)
            if not it:
                # 설정에서 제거되었으면 내려주고 목록에서도 제거(선택)
                try:
                    self.stop(nm, force=True)
                except Exception:
                    pass
                self.states.pop(nm, None)
                self.specs.pop(nm, None)
                continue
            # spec 필드 갱신
            st.spec.select = bool(it.get("select", True))
            st.spec.auto_restart = bool(it.get("auto_restart", True))
            st.spec.start_on_boot = bool(it.get("start_on_boot", False))
            st.spec.args = list(it.get("args", []))
            st.spec.path = (ROOT / it["path"]).resolve()

        # 2) 신규 항목 추가
        for nm, it in items.items():
            if nm in self.states:
                continue
            spec = ProcSpec(
                nm,
                (ROOT / it["path"]).resolve(),
                list(it.get("args", [])),
                bool(it.get("auto_restart", True)),
                bool(it.get("start_on_boot", False)),
                bool(it.get("select", True)),
            )
            self.specs[nm] = spec
            self.states[nm] = ProcState(spec)
        
    # -- process control ------------------------------------------------------
    def _preclean_existing(self, spec: ProcSpec) -> int:
        killed = _kill_all_by_path(spec.path)
        if not killed:
            for pid in _pids_by_image_name(spec.path.name):
                _taskkill_pid(pid, True)
                killed += 1
        if killed:
            self._log(f"[CLEAN] removed {killed} existing '{spec.name}' ({spec.path})")
        return killed

    def _enforce_singleton_after_start(self, st: ProcState):
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

            # ✅ select=false면 시작하지 않고 "skipped"로 정상 반환
            if not st.spec.select:
                self._log(f"[START-SKIP] {name} select=false")
                return {"ok": True, "skipped": True, "msg": f"{name} is not selected (select=false)"}

            spec = st.spec
            if not spec.path.exists():
                return {"ok": False, "error": f"executable not found: {spec.path}"}

            # 이미 실행 중이면 바로 반환 (자식 핸들 → 스냅샷 순)
            if st.is_running():
                return {"ok": True, "msg": f"{name} already running", "pid": st.pid}
            snap = self._proc_snapshot()
            p = str(spec.path.resolve()).lower()
            pid = snap.get(p)
            if pid:
                st.pid = pid
                return {"ok": True, "msg": f"{name} already running", "pid": st.pid}

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

            _kill_all_by_path(st.spec.path)

            st.last_exit_code = None if not st.proc else st.proc.poll()
            st.proc = None
            st.pid = None
            self._log(f"[STOP] {name} done")
            return {"ok": True, "msg": "stopped"}

    def restart(self, name: str) -> dict:
        st = self.states.get(name)
        if st and not st.spec.select:
            return {"ok": False, "error": f"{name} is not selected (select=false)"}
            
        r1 = self.stop(name, force=True)
        r2 = self.start(name)
        return {"ok": r1.get("ok") and r2.get("ok"), "stop": r1, "start": r2}

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

    # -- loops & http ---------------------------------------------------------
    def _tick_loop(self):
        self._log("[DMs] tick loop started")
        for nm, st in self.states.items():
            if st.spec.start_on_boot and st.spec.select:  # ← select 체크 추가
                self.start(nm)

        while not self._stop_evt.is_set():
            self._reload_config_if_needed()
            with self._lock:
                for nm, st in self.states.items():
                    pids = _pids_by_exact_path(st.spec.path)
                    if len(pids) > 1:
                        keep = st.pid if (st.pid in pids) else pids[0]
                        for pid in pids:
                            if pid != keep:
                                _taskkill_pid(pid, True)
                        st.pid = keep

                    if not pids and st.spec.auto_restart and st.last_start_ms is not None:
                        self._log(f"[RESTART] {nm} restarting...")
                        self.start(nm)

                    # select=false 는 무조건 내려가도록 강제
                    if not st.spec.select:
                        if st.is_running():
                            self._log(f"[ENFORCE STOP] {nm} select=false -> stopping")
                            try:
                                self.stop(nm, force=True)
                            except Exception:
                                pass
                        continue  # 아래 보정(중복/auto_restart)은 선택된 것만
            self._stop_evt.wait(self.heartbeat_interval)
        self._log("[DMs] tick loop ended")

    def _make_http_handler(self):
        sup = self

        class H(BaseHTTPRequestHandler):
            # ── minimal-fix: safe writer ──────────────────────────────────────
            def _ok(self, code=200, payload=None):
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    if payload is None:
                        payload = {"ok": True}
                    body = json.dumps(payload).encode("utf-8")
                    try:
                        self.wfile.write(body)
                    except (ConnectionAbortedError, BrokenPipeError):
                        # client closed connection → ignore
                        pass
                except (ConnectionAbortedError, BrokenPipeError):
                    pass

            def _parts(self):
                return [p for p in self.path.split("?")[0].split("/") if p]

            def do_GET(self):
                parts = self._parts()
                try:
                    # 쿼리 제거한 순수 경로
                    clean_path = urlsplit(self.path).path
                    clean_path = re.sub(r'/+', '/', clean_path)  # // -> /
                    norm_path  = re.sub(r'^/(?:web/)+', '/web/', clean_path)  # /web/web/... -> /web/...
                    if norm_path != clean_path:
                        # 쿼리 보존하여 302 리다이렉트
                        qs = urlsplit(self.path).query
                        loc = norm_path + (('?' + qs) if qs else '')
                        self.send_response(302)
                        self.send_header("Location", loc)
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

                    # 이후 분기에서 사용할 parts는 정규화된 경로 기준으로 다시 계산
                    parts = [p for p in norm_path.split('/') if p]

                    # ── [리다이렉트] 루트/별칭
                    #  - / 또는 /dms  → /web/dms-control.html
                    #  - /cms         → /web/cms-control.html
                    if clean_path in ("/", "/dms"):
                        self.send_response(302)
                        self.send_header("Location", "/web/dms-control.html")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if clean_path == "/cms":
                        self.send_response(302)
                        self.send_header("Location", "/web/cms-control.html")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

                    # ───────── 로그 라우트: 최우선으로 처리 ─────────
                    if parts and parts[0] == "logs":
                        # /logs/list/<name>
                        if len(parts) == 3 and parts[1] == "list":
                            name = parts[2]
                            dates = sup._log_dates_for(name)
                            return self._ok(200, {"ok": True, "name": name, "dates": dates})

                        # /logs/debug/<name>
                        if len(parts) == 3 and parts[1] == "debug":
                            name = parts[2]
                            info = sup._log_debug_info(name)  # 기존 함수 사용 가능
                            return self._ok(200, info)

                        # /logs/<name>?date=YYYY-MM-DD&tail=50000
                        if len(parts) >= 2:
                            name = parts[1]
                            from urllib.parse import parse_qs, urlparse
                            q = parse_qs(urlparse(self.path).query or "")
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

                    # ── [정적] /web/... (그대로 파일 매핑)
                    if parts[:1] == ["web"]:
                        sub = "/".join(parts[1:])
                        return _serve_static_safe(self, sub)

                    # ── [정적] 루트에서도 *.html 직접 서빙 (예: /dms-config.html, /dms-control.html, /cms-control.html)
                    if clean_path.endswith(".html"):
                        return _serve_static_safe(self, clean_path.lstrip("/"))

                    # ── [빠른 상태] /status-lite
                    if parts == ["status-lite"]:
                        hb = getattr(sup, "heartbeat_interval", 5)
                        lite = sup._status_payload(use_snapshot=False)
                        out = {"ok": True, "heartbeat_interval_sec": hb}
                        out.update(lite)
                        return self._ok(200, out)

                    # ── [상태] /status (캐시)
                    if parts == ["status"]:
                        hb = getattr(sup, "heartbeat_interval", 5)
                        st = sup.status_cached()
                        out = {"ok": True, "heartbeat_interval_sec": hb}
                        out.update(st)
                        return self._ok(200, out)

                    # ── [상태: name 지정] /status/{name}
                    if parts and parts[0] == "status" and len(parts) > 1:
                        name = parts[1]
                        return self._ok(200, sup.status(name))

                    # ── [설정 메타] /config/meta
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

                    # ── [설정 원문] /config (텍스트 그대로 반환)
                    if parts == ["config"]:
                        if not DEFAULT_CONFIG.exists():
                            return self._ok(404, {"ok": False, "error": "config not found"})
                        with open(DEFAULT_CONFIG, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        try: self.wfile.write(data)
                        except (ConnectionAbortedError, BrokenPipeError): pass
                        return

                    # static: log-viewer.html (root에서도 접근 가능)
                    if parts[0] == "log-viewer.html":
                        fp = (STATIC_ROOT / "log-viewer.html").resolve()
                        if fp.is_file() and str(fp).startswith(str(STATIC_ROOT.resolve())):
                            data = fp.read_bytes()
                            self.send_response(200)
                            self.send_header("Content-Type", _guess_type(fp))
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Content-Length", str(len(data)))
                            self.end_headers()
                            try: self.wfile.write(data)
                            except (ConnectionAbortedError, BrokenPipeError): pass
                            return
                        return self._ok(code=404, payload={"ok": False, "error": "not found"})
                                        
                    # 없으면 404
                    return self._ok(404, {"ok": False, "error": "not found"})

                except Exception as e:
                    return self._ok(500, {"ok": False, "error": repr(e)})
            def do_POST(self):
                parts = self._parts()
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length).decode("utf-8", errors="ignore")

                    # start/stop/restart/{name}
                    if parts and parts[0] in ("start", "stop", "restart"):
                        name = parts[1] if len(parts) > 1 else ""
                        if parts[0] == "start":
                            return self._ok(code=200, payload=sup.start(name))
                        if parts[0] == "stop":
                            return self._ok(code=200, payload=sup.stop(name, force=True))
                        if parts[0] == "restart":
                            return self._ok(code=200, payload=sup.restart(name))

                    if parts and parts[0] == "logs":
                        self.do_GET()  # POST를 GET처럼 처리
                        return

                    # save config (POST /config)                    
                    if parts == ["config"]:
                        DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                        # 그대로 저장 (JSON/JSON5 둘 다 허용)
                        DEFAULT_CONFIG.write_text(body, encoding="utf-8")
                        s = DEFAULT_CONFIG.stat()                        
                        sup._cfg_mtime = 0; 
                        sup._reload_config_if_needed()  # ← 저장 직후 즉시 반영
                        sup._log(f"[CONFIG SAVED] {DEFAULT_CONFIG} ({s.st_size} bytes, mtime={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(s.st_mtime))})")
                        return self._ok(200, {"ok": True})

                    # 포맷: POST /config-format
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
            "http_port": 51050,
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
