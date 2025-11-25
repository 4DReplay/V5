# ────────────────────────────────────────────────────────────
# oms_env.py
#   - Shared global environment (C++ header style)
#   - Paths, log directories, config paths
#   - 2025.11.24
# ────────────────────────────────────────────────────────────

import os
import time
import logging
import json, re, time, threading
from pathlib import Path

# ────────────────────────────────────────────────────────────
# V5 ROOT
# - If OMS_ROOT env exists → use that
# - Else → auto-detect project root (2 levels up)
# ────────────────────────────────────────────────────────────
ROOT = Path(os.environ.get("OMS_ROOT", Path(__file__).resolve().parents[2]))

# ────────────────────────────────────────────────────────────
# PATHS
# ────────────────────────────────────────────────────────────
WEB = ROOT / "web"
CFG = ROOT / "config" / "oms_config.json"
CFG_RECORD = ROOT / "config" / "user_config.json"   # user-config.json

# ────────────────────────────────────────────────────────────
# LOG directories
# ────────────────────────────────────────────────────────────
_LOGD = Path(os.environ.get("OMS_LOG_DIR", str(ROOT / "daemon" / "OMs")))
_LOGD.mkdir(parents=True, exist_ok=True)

# Real-time state files
CAM_STATE_FILE = _LOGD / "oms_cam_state.json"
SYS_STATE_FILE = _LOGD / "oms_sys_state.json"

# Trace logs
TRACE_DIR = _LOGD / "trace"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

# Daily rotating log files
LOG_DIR = _LOGD / "log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log_filename = time.strftime("%Y-%m-%d") + ".log"
log_path = LOG_DIR / log_filename


# user-config.json 올바른 절대 경로 계산
CFG_USER = Path(os.path.join(ROOT, "web", "config", "user-config.json"))

# ────────────────────────────────────────────────────────────
# LOGGER
# ────────────────────────────────────────────────────────────
fd_log = logging.getLogger("OMS")
fd_log.setLevel(logging.DEBUG)

# Formatter (optional – but recommended)
formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)

# File handler
fh = logging.FileHandler(log_path, encoding="utf-8")
fh.setFormatter(formatter)
fd_log.addHandler(fh)

# Console output
ch = logging.StreamHandler()
ch.setFormatter(formatter)
fd_log.addHandler(ch)

# Avoid duplicate handler registration
if not fd_log.handlers:
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(formatter)
    fd_log.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    fd_log.addHandler(ch)

# ────────────────────────────────────────────────────────────
# DEFAULT DEFINITION
# ────────────────────────────────────────────────────────────

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
# --- hard-coded timeouts ---
# ─────────────────────────────────────────────────────────────
RESTART_POST_TIMEOUT = 30.0
STATUS_FETCH_TIMEOUT = 10.0

# ─────────────────────────────────────────────────────────────
# global lock
# ─────────────────────────────────────────────────────────────
COMMAND_LOCK = threading.Lock()

# ────────────────────────────────────────────────────────────
# EXPORTS
# ────────────────────────────────────────────────────────────
__all__ = [
    "ROOT", "WEB",
    "CFG", "CFG_RECORD","CFG_USER",
    "LOG_DIR", "TRACE_DIR",
    "CAM_STATE_FILE", "SYS_STATE_FILE",
    "PROCESS_ALIAS_DEFAULT",
    "RESTART_POST_TIMEOUT", "STATUS_FETCH_TIMEOUT",
    "COMMAND_LOCK",
    "fd_log",
]
