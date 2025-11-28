# ─────────────────────────────────────────────────────────────────────────────#
# fd_logging.py
# date: 2025/10/24
# owner: Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import logging, os, re, sys, tempfile, glob, time
from datetime import datetime
from pathlib import Path

# =========================
# Environment Variables
# =========================
USE_FIXED_DIRECT    = os.environ.get("AID_LOG_TO_FIXED", "0") == "1"
RESET_ON_START      = os.environ.get("AID_LOG_RESET", "0") == "1"
TEE_FIXED_RAW       = os.environ.get("AID_TEE_RAW_FIXED", "0") == "1"
RETENTION_DAYS      = int(os.environ.get("AID_LOG_RETENTION_DAYS", "60"))
FD_DAEMON_NAME      = os.environ.get("AID_DAEMON_NAME", r"AId")

# =========================
# Utilities
# =========================
def _first_writable_dir(candidates):
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            testfile = os.path.join(path, ".write_test")
            with open(testfile, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(testfile)
            return path
        except Exception:
            continue
    raise PermissionError("No writable log directory among candidates")

_ansi_re = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')  # ANSI 이스케이프 제거용
def remove_ansi_escape_sequences(text: str) -> str:
    return _ansi_re.sub("", text)

def _ts():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]


# =========================
# 경로 결정을 위한 후보
# =========================
def _ensure_writable_dir(path_str: str) -> str:
    p = Path(path_str)
    p.mkdir(parents=True, exist_ok=True)
    t = p / ".touch"
    with open(t, "w", encoding="utf-8") as f:
        f.write("ok")
    try:
        t.unlink(missing_ok=True)
    except Exception:
        pass
    return str(p)

_THIS = Path(__file__).resolve()
SRC_DIR = _THIS.parent.parent
PROJECT_ROOT = SRC_DIR.parent

# ✅ Priority: FD_LOG_DIR → AID_LOG_DIR → (if not set) V5\daemon\<DAEMON>\log
_env_dir = (os.environ.get("FD_LOG_DIR") or os.environ.get("AID_LOG_DIR") or "").strip()
if FD_DAEMON_NAME == "AId":
    DEFAULT_LOG_DIR = str(PROJECT_ROOT / "daemon" / "AId" / "log")
elif FD_DAEMON_NAME == "AIc":
    DEFAULT_LOG_DIR = str(PROJECT_ROOT / "daemon" / "AIc" / "log")
else:
    DEFAULT_LOG_DIR = str(PROJECT_ROOT / "daemon" / FD_DAEMON_NAME / "log")
PATH_LOG = _ensure_writable_dir(_env_dir if _env_dir else DEFAULT_LOG_DIR)

# =========================
# File Paths (Daily append)
# =========================
_cutoff = time.time() - RETENTION_DAYS * 24 * 3600
TODAY = datetime.now().strftime("%Y-%m-%d")

RUN_LOG_FILE   = os.path.join(PATH_LOG, f"{TODAY}.log")
FIXED_LOG_FILE = RUN_LOG_FILE

# 오래된 로그 삭제
for p in glob.glob(os.path.join(PATH_LOG, "????-??-??.log")):
    try:
        if os.path.getmtime(p) < _cutoff:
            os.remove(p)
    except Exception:
        pass

# =========================
# handler
# =========================
class CleanFileHandler(logging.FileHandler):
    def emit(self, record):
        d = record.__dict__.copy()
        d["msg"] = remove_ansi_escape_sequences(str(d.get("msg", "")))
        try:
            super().emit(logging.makeLogRecord(d))
        except Exception:
            pass

# =========================
# Logger Singleton
# =========================
class FDLogger:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize_logger()
        return cls._instance

    def _initialize_logger(self):
        self.logger = logging.getLogger("fd_logger")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

        if not self.logger.handlers:
            ch = logging.StreamHandler(stream=sys.stdout or sys.__stdout__)
            ch.setFormatter(fmt)
            self.logger.addHandler(ch)

            # optional fixed file
            if USE_FIXED_DIRECT:
                mode = 'w' if RESET_ON_START else 'a'
                try:
                    fh = CleanFileHandler(FIXED_LOG_FILE, mode=mode, encoding='utf-8', delay=False)
                except PermissionError:
                    fb_dir = os.path.join(tempfile.gettempdir(), FD_DAEMON_NAME, "log")
                    os.makedirs(fb_dir, exist_ok=True)
                    fb_file = os.path.join(fb_dir, f"fixed.{os.getpid()}.log")
                    fh = CleanFileHandler(fb_file, mode='a', encoding='utf-8', delay=False)
                    print(f"[fd_logging] FIXED denied → fallback {fb_file}", file=sys.stderr)
                fh.setFormatter(fmt)
                self.logger.addHandler(fh)

            # main run file
            try:
                rh = CleanFileHandler(RUN_LOG_FILE, mode='a', encoding='utf-8', delay=False)
            except PermissionError:
                fb_dir = os.path.join(tempfile.gettempdir(), FD_DAEMON_NAME, "log")
                os.makedirs(fb_dir, exist_ok=True)
                fb_file = os.path.join(fb_dir, f"run.{os.getpid()}.log")
                rh = CleanFileHandler(fb_file, mode='a', encoding='utf-8', delay=False)
                print(f"[fd_logging] RUN denied → fallback {fb_file}", file=sys.stderr)
            rh.setFormatter(fmt)
            self.logger.addHandler(rh)

        # ===== raw handles =====
        self._raw_fixed = None
        if TEE_FIXED_RAW and USE_FIXED_DIRECT:
            try:
                self._raw_fixed = open(FIXED_LOG_FILE, "a", encoding="utf-8", errors="replace")
            except Exception:
                try:
                    fb_dir = os.path.join(tempfile.gettempdir(), FD_DAEMON_NAME, "log")
                    os.makedirs(fb_dir, exist_ok=True)
                    fb_file = os.path.join(fb_dir, f"fixed.{os.getpid()}.log")
                    self._raw_fixed = open(fb_file, "a", encoding="utf-8", errors="replace")
                    print(f"[fd_logging] raw_fixed fallback: {fb_file}", file=sys.stderr)
                except Exception:
                    self._raw_fixed = None

        try:
            self._raw_run = open(RUN_LOG_FILE, "a", encoding="utf-8", errors="replace")
        except Exception:
            fb_dir = os.path.join(tempfile.gettempdir(), FD_DAEMON_NAME, "log")
            os.makedirs(fb_dir, exist_ok=True)
            fb_file = os.path.join(fb_dir, f"run.{os.getpid()}.log")
            self._raw_run = open(fb_file, "a", encoding="utf-8", errors="replace")
            print(f"[fd_logging] raw_run fallback: {fb_file}", file=sys.stderr)

    def get_logger(self):
        return self.logger

    def print(self, msg: str = "", end: str = "\n", flush: bool = True):
        line = f"{_ts()} [INFO] {remove_ansi_escape_sequences(str(msg))}"
        out = sys.stdout or sys.__stdout__
        if out:
            try:
                out.write(line + ("" if end is None else end))
                if flush: out.flush()
            except Exception:
                pass
        if getattr(self, "_raw_run", None):
            try:
                self._raw_run.write(line + ("" if end is None else end))
                if flush: self._raw_run.flush()
            except Exception:
                pass
        if getattr(self, "_raw_fixed", None):
            try:
                self._raw_fixed.write(line + ("" if end is None else end))
                if flush: self._raw_fixed.flush()
            except Exception:
                pass

    def close(self):
        for fp in (getattr(self, "_raw_fixed", None), getattr(self, "_raw_run", None)):
            try: fp and fp.close()
            except: pass
        for h in list(self.logger.handlers):
            try: h.flush(); h.close()
            except: pass


fd_logger_instance = FDLogger()
fd_log = fd_logger_instance.get_logger()
fd_log.print = fd_logger_instance.print
fd_log.close = fd_logger_instance.close

fd_log.info(f"[fd_logging] PATH_LOG={PATH_LOG}")
fd_log.info(f"[fd_logging] FIXED_LOG_FILE={FIXED_LOG_FILE}")
fd_log.info(f"[fd_logging] RUN_LOG_FILE={RUN_LOG_FILE}")
