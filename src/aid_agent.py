# ─────────────────────────────────────────────────────────────────────────────
# aid_agent.py  (HTTP service for AId Agent)
# - 2025/10/24
# - Hongsu Jung (refactor by aligning with install_aid_service.ps1)
# ─────────────────────────────────────────────────────────────────────────────

import os
import sys
import json
import time
import socket
import platform
import argparse
from datetime import datetime
from typing import Dict, Any, Optional

from flask import Flask, Response, request, abort

# -----------------------------------------------------------------------------
# Logging file resolution (fd_logging optional)
# -----------------------------------------------------------------------------
def _resolve_fixed_log(log_dir: str) -> str:
    # Try fd_logging.get_fixed_log_file(), fallback to <log_dir>\aid_main.out.log
    try:
        from fd_logging import get_fixed_log_file  # type: ignore
        return get_fixed_log_file()
    except Exception:
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, "aid_main.out.log")

# -----------------------------------------------------------------------------
# Token helper
# -----------------------------------------------------------------------------
def _extract_token() -> Optional[str]:
    # Accept both header and query for convenience when testing
    tok = request.headers.get("X-Token")
    if not tok:
        tok = request.args.get("token")
    return tok

def _require_token(expected: Optional[str]):
    if not expected:
        return  # token feature disabled
    got = _extract_token()
    if got != expected:
        abort(401, description="unauthorized")

# -----------------------------------------------------------------------------
# App factory
# -----------------------------------------------------------------------------
def create_app(host: str, port: int, token: Optional[str], workdir: str, log_dir: str) -> Flask:
    app = Flask(__name__)

    # Make these visible to routes
    app.config["AID_HOST"] = host
    app.config["AID_PORT"] = port
    app.config["AID_TOKEN"] = token
    app.config["AID_WORKDIR"] = workdir
    app.config["AID_LOG_DIR"] = log_dir

    # Export for child processes that rely on it
    os.environ["AID_LOG_DIR"] = log_dir

    FIXED_LOG = _resolve_fixed_log(log_dir)
    app.config["AID_FIXED_LOG"] = FIXED_LOG

    @app.get("/health")
    def health():
        # No token required for liveness check used by installer
        data = {
            "ok": True,
            "ts": datetime.utcnow().isoformat() + "Z",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "workdir": app.config["AID_WORKDIR"],
            "log": app.config["AID_FIXED_LOG"],
            "port": app.config["AID_PORT"],
        }
        return data, 200

    @app.get("/logs")
    def logs_html():
        _require_token(app.config["AID_TOKEN"])
        path = app.config["AID_FIXED_LOG"]
        if not os.path.exists(path):
            return "No log file found", 404
        try:
            lines = int(request.args.get("lines", 500))
        except Exception:
            lines = 500
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.readlines()[-lines:]
        return "<pre style='white-space:pre-wrap;word-break:break-word;'>" + "".join(content) + "</pre>"

    @app.get("/logs/raw")
    def logs_raw():
        _require_token(app.config["AID_TOKEN"])
        path = app.config["AID_FIXED_LOG"]
        if not os.path.exists(path):
            return "No log file found", 404

        def generate():
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                chunk = f.read(1 << 20)
                while chunk:
                    yield chunk
                    chunk = f.read(1 << 20)

        return Response(generate(), mimetype="text/plain")

    @app.get("/logs/stream")
    def logs_stream():
        _require_token(app.config["AID_TOKEN"])
        path = app.config["AID_FIXED_LOG"]
        if not os.path.exists(path):
            return "No log file found", 404

        def event_stream():
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    yield f"data: {line.rstrip()}\n\n"

        return Response(event_stream(), mimetype="text/event-stream")

    return app

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="AId Agent HTTP service")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=5086)
    p.add_argument("--token", default=None, help="Shared token for protected endpoints")
    p.add_argument("--workdir", default=os.getcwd())
    p.add_argument("--log-dir", default=r"C:\4DReplay\AId\logs")
    p.add_argument("--aid-cmd", default="", help="Reserved for future use")
    return p.parse_args(argv)

def main():
    args = parse_args()

    # Normalize paths
    workdir = os.path.abspath(args.workdir)
    log_dir = os.path.abspath(args.log_dir)
    os.makedirs(workdir, exist_ok=True)
    os.makedirs(log_dir, exist_ok=True)
    os.chdir(workdir)

    app = create_app(args.host, args.port, args.token, workdir, log_dir)

    # Prefer waitress in service context
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8)
    except Exception as e:
        # Fallback: Flask dev server (not recommended for production)
        print(f"[WARN] waitress serve failed ({e}). Falling back to Flask built-in server.", file=sys.stderr)
        app.run(host=args.host, port=args.port)

if __name__ == "__main__":
    main()
