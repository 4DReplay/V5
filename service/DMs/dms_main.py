# ─────────────────────────────────────────────────────────────────────────────
# aid_agent.py (final integrated)
# - AId Agent API (status/control/config)
# - Web UI serving (aid-control.html / other assets under <workdir>\web)
# - Log endpoints:
#     /logs/fixed        : current fixed log (aid_main.out.log)
#     /logs/list         : list rotated logs (aid_main_YYYYMMDD_HHMMSS.log)
#     /logs/file/<name>  : download specific rotated log
#     /logs/stream       : SSE realtime tail of aid_main.out.log
#
# Usage:
#   python aid_agent.py --host 0.0.0.0 --port 5086 --token AID_TOKEN ^
#     --aid-cmd "\"C:\Program Files\Python310\python.exe\" \"C:\4DReplay\AId\src\aid_main.py\"" ^
#     --workdir "C:\4DReplay\AId" --log-dir "C:\4DReplay\AId\logs"
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import json
import atexit    
import time
import shlex
import signal
import argparse
import threading
import subprocess
from datetime import datetime, timezone
from typing import Optional, List
from pathlib import Path

from flask import (
    Flask, request, jsonify, abort, send_from_directory,
    send_file, Response, stream_with_context
)

HERE        = Path(__file__).resolve().parent              # e.g., C:\4DReplay\AId\service
AID_ROOT    = HERE.parent                                  # e.g., C:\4DReplay\AId
SERVICE_DIR = HERE


# ─────────────────────────────────────────
# Small process manager for AId main process
# ─────────────────────────────────────────
class ProcessManager:
    def __init__(self, cmdline: str, workdir: str,
                 stdout_path: Optional[str] = None,
                 stderr_path: Optional[str] = None):
        self._cmdline_raw = cmdline
        self._cmd: List[str] = shlex.split(cmdline, posix=False)  # Windows-safe split
        self._workdir = workdir
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._started_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._stdout_path = stdout_path
        self._stderr_path = stderr_path
        self._stdout_f = None
        self._stderr_f = None

    def _open_logs(self):
        # Fixed files; reset on each start (w mode)
        if self._stdout_path and self._stdout_f is None:
            os.makedirs(os.path.dirname(self._stdout_path), exist_ok=True)
            self._stdout_f = open(self._stdout_path, "w", buffering=1, encoding="utf-8", errors="replace")
        if self._stderr_path and self._stderr_f is None:
            os.makedirs(os.path.dirname(self._stderr_path), exist_ok=True)
            self._stderr_f = open(self._stderr_path, "w", buffering=1, encoding="utf-8", errors="replace")

    def _close_logs(self):
        for f in (self._stdout_f, self._stderr_f):
            try:
                if f:
                    f.flush()
                    f.close()
            except Exception:
                pass
        self._stdout_f = None
        self._stderr_f = None

    def start(self):
        with self._lock:
            if self.is_running():
                return {"ok": True, "msg": "already running", "pid": self._proc.pid}
            try:
                self._open_logs()
                self._proc = subprocess.Popen(
                    self._cmd,
                    cwd=self._workdir,
                    shell=False,
                    stdout=self._stdout_f or subprocess.DEVNULL,
                    stderr=self._stderr_f or subprocess.DEVNULL,
                    creationflags = 0  # Windows에서도 새 그룹 분리 금지
                )
                self._started_at = datetime.now(timezone.utc).isoformat()
                self._last_error = None
                return {"ok": True, "msg": "started", "pid": self._proc.pid}
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "error": self._last_error}

    def stop(self, timeout=10):
        with self._lock:
            if not self.is_running():
                self._close_logs()
                return {"ok": True, "msg": "already stopped"}
            try:
                # graceful terminate first
                self._proc.terminate()
                if not self._wait_terminate(timeout):
                    self._proc.kill()
                    self._wait_terminate(3)
                self._proc = None
                self._started_at = None
                self._close_logs()
                return {"ok": True, "msg": "stopped"}
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "error": self._last_error}

    def restart(self):
        r1 = self.stop()
        if not r1.get("ok"):
            return r1
        time.sleep(0.5)
        return self.start()

    def status(self):
        with self._lock:
            running = self.is_running()
            st = {
                "running": running,
                "pid": self._proc.pid if running else None,
                "started_at": self._started_at,
                "last_error": self._last_error,
                "cmdline": self._cmdline_raw,
                "cwd": self._workdir,
            }
            if running and self._started_at:
                try:
                    t0 = datetime.fromisoformat(self._started_at)
                    st["uptime_sec"] = int((datetime.now(timezone.utc) - t0).total_seconds())
                except Exception:
                    st["uptime_sec"] = None
            else:
                st["uptime_sec"] = None
            return st

    def is_running(self) -> bool:
        return (self._proc is not None) and (self._proc.poll() is None)

    def _wait_terminate(self, timeout) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._proc.poll() is not None:
                return True
            time.sleep(0.1)
        return False


# ─────────────────────────────────────────
# Config ensure + env export
# ─────────────────────────────────────────
def _ensure_config_paths(workdir: str) -> tuple[str, str]:
    """
    Ensure config dir and files exist under <workdir>\config.
    Returns (private_path, public_path).
    """
    conf_dir = Path(workdir) / "config"
    conf_dir.mkdir(parents=True, exist_ok=True)

    priv = conf_dir / "aid_config_private.json5"
    publ = conf_dir / "aid_config_public.json5"

    if not priv.exists():
        priv.write_text("{}\n", encoding="utf-8")
    if not publ.exists():
        publ.write_text("{}\n", encoding="utf-8")

    # export env (some modules may read these)
    os.environ["AID_CONF_DIR"]    = str(conf_dir)
    os.environ["FD_CONF_DIR"]     = str(conf_dir)
    os.environ["CONFIG_DIR"]      = str(conf_dir)
    os.environ["FD_CFG_PRIVATE"]  = str(priv)
    os.environ["FD_CFG_PUBLIC"]   = str(publ)
    os.environ["FD_REBASE_PATHS"] = os.getenv("FD_REBASE_PATHS", "1")

    return str(priv), str(publ)


# ─────────────────────────────────────────
# Flask app factory
# ─────────────────────────────────────────
def create_app(aid_cmd: str, workdir: str, token: str,
               log_dir: Optional[str] = None) -> Flask:
    app = Flask(__name__)
    app.url_map.strict_slashes = False   # /logs/stream 와 /logs/stream/ 모두 허용

    # ----- 로그 경로 확정 (한 번만) -----
    LOG_DIR = log_dir or os.path.join(workdir, "logs")
    os.makedirs(LOG_DIR, exist_ok=True)

    os.environ["AID_LOG_DIR"] = LOG_DIR

    FIXED_OUT = os.path.join(LOG_DIR, "aid_main.out.log")
    FIXED_ERR = os.path.join(LOG_DIR, "aid_main.err.log")

    # Process manager uses the same fixed files
    pm = ProcessManager(aid_cmd, workdir, stdout_path=FIXED_OUT, stderr_path=FIXED_ERR)

    def _graceful_stop(*_args):
        try:
            pm.stop(timeout=5)
        except Exception:
            pass

    # 프로세스 종료 시 자식도 먼저 멈춤
    atexit.register(_graceful_stop)
    signal.signal(signal.SIGTERM, lambda *_: (_graceful_stop(), os._exit(0)))
    signal.signal(signal.SIGINT,  lambda *_: (_graceful_stop(), os._exit(0)))


    # Ensure config paths and pre-init the runtime config manager
    cfg_private, cfg_public = _ensure_config_paths(workdir)

    # Initialize config manager directly to avoid setup() path issues
    from src.fd_utils import fd_config_manager as FDC
    try:
        FDC._manager.init(
            cfg_private,
            config_public_path=cfg_public,
            rebase_paths=True,
            force=True
        )
    except Exception:
        FDC._manager.init(
            cfg_private,
            config_public_path=cfg_public,
            rebase_paths=True,
            force=True
        )

    WEB_DIR = os.path.join(workdir, "web")

    # ─── Auth helpers ───
    def require_token_header():
        if not token:
            return
        got = request.headers.get("X-Auth-Token", "")
        if got != token:
            abort(401, description="invalid token")

    def require_token_qs():
        if not token:
            return
        got = request.args.get("token", "")
        if got != token:
            abort(401, description="invalid token")

    # ─── Web pages / static ───
    @app.get("/control")
    def web_control_page():
        return send_from_directory(WEB_DIR, "aid-control.html", mimetype="text/html")

    @app.get("/config")
    def web_config_editor():
        return send_from_directory(WEB_DIR, "aid-config-editor.html", mimetype="text/html")

    @app.get("/web/<path:path>")
    def web_static(path):
        return send_from_directory(WEB_DIR, path)

    # ─── Health / Status ───
    @app.get("/health")
    def health():
        return jsonify({"ok": True})

    @app.get("/status")
    def status():
        require_token_header()
        st = pm.status()
        try:
            st["config_ready"] = bool(FDC.is_ready())
        except Exception:
            st["config_ready"] = True
        return jsonify(st)

    # ─── Control ───
    @app.post("/control")
    def control():
        require_token_header()
        body = request.get_json(force=True, silent=True) or {}
        action = (body.get("action") or "").lower()
        if action == "start":
            return jsonify(pm.start())
        elif action == "stop":
            return jsonify(pm.stop())
        elif action == "restart":
            return jsonify(pm.restart())
        else:
            return jsonify({"ok": False, "error": "action must be start|stop|restart"}), 400

    # ─── Config JSON API ───
    @app.get("/config/public")
    def get_public():
        require_token_header()
        return jsonify(FDC.public_get())

    @app.post("/config/public/replace")
    def replace_public():
        require_token_header()
        body = request.get_json(force=True, silent=True) or {}
        if not isinstance(body, dict):
            return jsonify({"ok": False, "error": "body must be JSON object"}), 400
        FDC.public_replace(body)
        return jsonify({"ok": True})

    @app.patch("/config/public")
    def patch_public():
        require_token_header()
        patch = request.get_json(force=True, silent=True) or {}
        if not isinstance(patch, dict):
            return jsonify({"ok": False, "error": "patch must be JSON object"}), 400
        FDC.public_patch(patch)
        return jsonify({"ok": True})

    @app.get("/config/public/by-path")
    def get_by_path():
        require_token_header()
        path = request.args.get("path", "")
        if not path:
            return jsonify({"ok": False, "error": "missing path"}), 400
        val = FDC.public_get_by_path(path, None)
        return jsonify({"ok": True, "value": val})

    @app.post("/config/public/by-path")
    def set_by_path():
        require_token_header()
        body = request.get_json(force=True, silent=True) or {}
        path = body.get("path")
        if path is None:
            return jsonify({"ok": False, "error": "missing path"}), 400
        value = body.get("value")
        FDC.public_set_by_path(path, value)
        return jsonify({"ok": True})

    # ─── Config text API (JSON5 preserving) ───
    PUBLIC_CFG = cfg_public

    @app.get("/config/public/text")
    def get_public_text():
        require_token_header()
        if not os.path.exists(PUBLIC_CFG):
            with open(PUBLIC_CFG, "w", encoding="utf-8") as f:
                f.write("{}\n")
        with open(PUBLIC_CFG, "r", encoding="utf-8") as f:
            txt = f.read()
        return app.response_class(txt, mimetype="text/plain; charset=utf-8")

    @app.post("/config/public/text")
    def post_public_text():
        require_token_header()
        body_raw = request.get_data(as_text=True) or ""
        # validate JSON5
        try:
            import json5
            parsed = json5.loads(body_raw)
        except Exception as e:
            return jsonify({"ok": False, "error": f"JSON5 parse error: {e}"}), 400

        # backup
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        bak = f"{PUBLIC_CFG}.{ts}.bak"
        if os.path.exists(PUBLIC_CFG):
            try:
                import shutil
                shutil.copy2(PUBLIC_CFG, bak)
            except Exception:
                bak = None

        # write back (preserve formatting/comments)
        with open(PUBLIC_CFG, "w", encoding="utf-8", newline="\n") as f:
            f.write(body_raw if body_raw.endswith("\n") else body_raw + "\n")

        # reflect to runtime
        try:
            FDC.public_replace(parsed)
        except Exception:
            pass

        return jsonify({"ok": True, "backup": os.path.basename(bak) if bak else None})

    # ─── Logs (fixed/rotated/SSE) ───
    @app.get("/logs/fixed")
    def get_fixed_log():
        require_token_header()
        path = FIXED_OUT
        if not path or not os.path.exists(path):
            return Response("No log file found", status=404, mimetype="text/plain")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                data = f.read()
            return Response(data, mimetype="text/plain; charset=utf-8")
        except Exception as e:
            return Response(f"Error reading log: {e}", status=500, mimetype="text/plain")

    @app.get("/logs/list")
    def list_logs():
        require_token_header()
        if not os.path.isdir(LOG_DIR):
            return jsonify([])
        logs = []
        for fn in sorted(os.listdir(LOG_DIR)):
            # 회전 로그: aid_main_YYYYMMDD_HHMMSS.log
            if fn.startswith("aid_main_") and fn.endswith(".log"):
                full = os.path.join(LOG_DIR, fn)
                try:
                    logs.append({
                        "name": fn,
                        "size": os.path.getsize(full),
                        "mtime": os.path.getmtime(full)
                    })
                except Exception:
                    pass
        return jsonify(logs)

    @app.get("/logs/file/<path:filename>")
    def get_log_file(filename):
        require_token_header()
        safe_name = os.path.basename(filename)
        safe_path = os.path.join(LOG_DIR, safe_name)
        if not os.path.exists(safe_path):
            return Response("Not found", status=404, mimetype="text/plain")
        return send_file(safe_path, mimetype="text/plain")

    # 기존: @app.get("/logs/stream")
    # 새로: 아래 3줄 모두 달아줍니다.
    @app.get("/logs/stream")
    @app.get("/logs/stream/")
    @app.get("/logs/sse")
    def logs_stream():
        """
        SSE realtime tail of FIXED_OUT (aid_main.out.log)
        Query:
          token       : for EventSource (when header not usable)
          tail_bytes  : initial bytes from end (default 200000)
          heartbeat   : ping seconds (default 10)
          from_start  : 1 to start from beginning (default tail)
        """
        # SSE는 브라우저가 헤더 추가가 어려워 쿼리 토큰 허용
        q_token = request.args.get("token", "")
        if token and q_token != token:
            abort(401, description="invalid token")

        path = FIXED_OUT
        if not path:
            return Response("No fixed log configured", status=500, mimetype="text/plain")

        try:
            tail_bytes = int(request.args.get("tail_bytes", "200000"))
        except ValueError:
            tail_bytes = 200000
        try:
            hb = max(3, int(request.args.get("heartbeat", "10")))
        except ValueError:
            hb = 10
        from_start = request.args.get("from_start", "0") == "1"

        def _sse(event: str, data: str):
            yield f"event: {event}\n"
            # 여러 줄을 안전하게 보냄
            for line in (data or "").splitlines():
                yield f"data: {line}\n"
            yield "\n"

        @stream_with_context
        def generate():
            import time as _time
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    if from_start:
                        f.seek(0, os.SEEK_SET)
                    else:
                        f.seek(0, os.SEEK_END)
                        sz = f.tell()
                        if tail_bytes > 0 and sz > tail_bytes:
                            f.seek(sz - tail_bytes, os.SEEK_SET)

                    init_chunk = f.read()
                    if init_chunk:
                        yield from _sse("chunk", init_chunk)

                    last = _time.time()
                    while True:
                        chunk = f.read()
                        if chunk:
                            yield from _sse("chunk", chunk)
                            last = _time.time()
                        else:
                            if _time.time() - last > hb:
                                # SSE 주석(하트비트). 일부 프록시 버퍼링 방지
                                yield ": keep-alive\n\n"
                                last = _time.time()
                            _time.sleep(0.5)
            except FileNotFoundError:
                yield from _sse("chunk", "(no log file yet)")

        resp = Response(generate(), mimetype="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    return app


# ─────────────────────────────────────────
# Signals
# ─────────────────────────────────────────
shutdown_flag = False

def handle_exit(signum, frame):
    global shutdown_flag
    shutdown_flag = True
    print(f"[AIdAgent] Received signal {signum}, shutting down gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, handle_exit)
signal.signal(signal.SIGTERM, handle_exit)


# ─────────────────────────────────────────
# Main
# ─────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5086)
    parser.add_argument("--token", default="", help="X-Auth-Token value (empty disables auth)")
    parser.add_argument("--aid-cmd", required=True, help="e.g. \"C:\\Python310\\python.exe C:\\4DReplay\\AId\\src\\aid_main.py\"")
    parser.add_argument("--workdir", default=".", help="AId working directory")
    parser.add_argument("--log-dir", default="", help="stdout/stderr log directory (optional)")
    args = parser.parse_args()

    log_dir = args.log_dir or None
    app = create_app(aid_cmd=args.aid_cmd, workdir=args.workdir, token=args.token, log_dir=log_dir)

    # production: threaded=True, use_reloader=False
    app.run(host=args.host, port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
