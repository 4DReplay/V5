# ────────────────────────────────────────────────────────────
# oms_env.py
#   - Shared global environment (C++ header style)
#   - Paths, log directories, config paths
#   - 2025.11.24
# --- how to read log >>> Powershell
# >> Get-Content "C:\4DReplay\V5\daemon\OMs\log\2025-11-20.log" -Wait -Tail 20
# ─────────────────────────────────────────────────────────────

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
PATH_WEB = ROOT / "web"
PATH_CFG = ROOT / "config" / "oms_config.json"

# ────────────────────────────────────────────────────────────
# LOG directories
# ────────────────────────────────────────────────────────────
PATH_OMS = Path(os.environ.get("OMS_LOG_DIR", str(ROOT / "daemon" / "OMs")))
PATH_OMS.mkdir(parents=True, exist_ok=True)
# Real-time state files
FILE_SYS_STATE = PATH_OMS / "oms_sys_state.json"
FILE_CAM_STATE = PATH_OMS / "oms_cam_state.json"
FILE_REC_STATE = PATH_OMS / "oms_rec_state.json"
# Trace logs
PATH_TRACE = PATH_OMS / "trace"
PATH_TRACE.mkdir(parents=True, exist_ok=True)
# Daily rotating log files
PATH_LOG = PATH_OMS / "log"
PATH_LOG.mkdir(parents=True, exist_ok=True)
log_filename = time.strftime("%Y-%m-%d") + ".log"
log_path = PATH_LOG / log_filename
# user-config.json 올바른 절대 경로 계산
FILE_USER_CFG = Path(os.path.join(ROOT, "web", "config", "user-config.json"))
FILE_CAM_ENV  = PATH_WEB / "config" / "camera-env.json"
FILE_RECORD_HISTORY = PATH_WEB / "record" / "record_history.json"
FILE_PRODUCT_HISTORY = PATH_WEB / "record" / "product_history.json"


# ────────────────────────────────────────────────────────────
# LOGGER
# fd_log.d() — custom debug printer
# DEBUG_MODE = True/False 로 설정하면 d() 로그가 출력됨
# ────────────────────────────────────────────────────────────
DEBUG_MODE = True   # ← True 로 바꾸면 d() 로그가 출력됨

fd_log = logging.getLogger("OMS")
fd_log.setLevel(logging.DEBUG)

# Logger를 함수처럼 호출 가능하도록 패치
def _logger_call(self, msg):
    self.info(msg)
logging.Logger.__call__ = _logger_call

# Formatter
formatter = logging.Formatter(
    "%(asctime)s %(message)s", "%Y-%m-%d %H:%M:%S"
)

# File handler
fh = logging.FileHandler(log_path, encoding="utf-8")
fh.setFormatter(formatter)
fd_log.addHandler(fh)

# Console handler
ch = logging.StreamHandler()
ch.setFormatter(formatter)
fd_log.addHandler(ch)

# ──────────────────────────────────────────
# ⭐ fd_log.d() — custom debug printer
# ──────────────────────────────────────────
def _debug_short(self, msg):
    if DEBUG_MODE:
        self.debug(f"[DEBUG] {msg}")

# attach to logger instance
fd_log.d = _debug_short.__get__(fd_log, logging.Logger)

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
# STATE DEFINITION
# 1X:system, 2X:camera, 3X:product
# X0:Unknown, X1:need restart, X2:Restarting, X3:need connect, X4:Connecting, X5:Ready
# ───────────────────────────────────────────────────── ────────
UI_STATE_TITLE = {
    0 : "Unknown",          # unknown                       | 🟠"chip-orange",
    # SYSTEM
    10: "Check System",     # system/check                  | 🟠"chip-orange",
    11: "Need Restart",     # not on everything             | 🔵"chip-blue",
    12: "Restarting",       # on restarting system          | 🔵"chip-blue",
    13: "Need Connect",     # on everything + not connect   | 🟡"chip-yellow",
    14: "Connecting...",    # on connecting system          | 🟡"chip-yellow",
    15: "Ready",            # ready (on+connected)          | 🟢"chip-green",
    # CAMERA
    20: "Check Camera",     # camera/check                  | 🟠"chip-orange",
    21: "Need Restart",     # not on everything             | 🔵"chip-blue",
    22: "Restarting",       # on restarting camera          | 🔵"chip-blue",
    23: "Need Connect",     # on everything + not connect   | 🟡"chip-yellow",   
    24: "Connecting...",    # on connecting camera          | 🟡"chip-yellow",   
    25: "Ready",            # ready (on+connected)          | 🟢"chip-green",  = 31
    26: "Recording",        # on recording                  | 🟢"chip-green",  = 33
    27: "Recording Error",  # on recording                  | 🟠"chip-orange",  
    # PRODUCTION
    30: "Check Camera",     # recording/check               | 🟠"chip-orange",
    31: "Need Recording",   # not on everything             | 🟡"chip-yellow",   = 25
    32: "Preparing...",     # on recording camera           | 🟡"chip-yellow",    
    33: "Product Ready",    # on everything + not connect   | 🟢"chip-green",  = 26
    34: "Producing...",     # on connecting camera          | 🔴"chip-red",
    35: "Creating...",      # waiting until finish jon      | 🔴"chip-red",
}

# ─────────────────────────────────────────────────────────────
# global lock
# ─────────────────────────────────────────────────────────────
COMMAND_LOCK = threading.Lock()

# ────────────────────────────────────────────────────────────
# EXPORTS
# ────────────────────────────────────────────────────────────
__all__ = [
    "fd_log",                                               # log
    "ROOT", "PATH_WEB","PATH_CFG","PATH_LOG", "PATH_TRACE", # path
    "FILE_USER_CFG","FILE_CAM_ENV",                         # file
    "FILE_CAM_STATE", "FILE_SYS_STATE", "FILE_REC_STATE",   # state file
    "FILE_RECORD_HISTORY","FILE_PRODUCT_HISTORY",           # history file
    "PROCESS_ALIAS_DEFAULT",                                # alias
    "RESTART_POST_TIMEOUT", "STATUS_FETCH_TIMEOUT",         # timeout 
    "COMMAND_LOCK",                                         # lock    
    "UI_STATE_TITLE",                                       # state definition
]
