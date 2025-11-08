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
    # logging 기본 포맷과 맞춰 밀리초 3자리
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S,%f')[:-3]


# =========================
# 경로 결정을 위한 후보
# =========================
def _ensure_writable_dir(path_str: str) -> str:
    p = Path(path_str)
    p.mkdir(parents=True, exist_ok=True)
    # 간단한 쓰기 테스트
    t = p / ".touch"
    with open(t, "w", encoding="utf-8") as f:
        f.write("ok")
    try:
        t.unlink(missing_ok=True)
    except Exception:
        pass
    return str(p)

_THIS = Path(__file__).resolve()
# utils/.. = src
SRC_DIR = _THIS.parent.parent
PROJECT_ROOT = SRC_DIR.parent


# ✅ Priority: FD_LOG_DIR → AID_LOG_DIR → (if not set) V5\daemon\AId\logs
_env_dir = (os.environ.get("FD_LOG_DIR") or os.environ.get("AID_LOG_DIR") or "").strip()
if FD_DAEMON_NAME == "AId":
    DEFAULT_LOG_DIR = str(PROJECT_ROOT / "daemon" / "AId" / "logs")
elif FD_DAEMON_NAME == "AIc":
    DEFAULT_LOG_DIR = str(PROJECT_ROOT / "daemon" / "AIc" / "logs")
LOG_DIR = _ensure_writable_dir(_env_dir if _env_dir else DEFAULT_LOG_DIR)

# =========================
# File Paths
# =========================
_cutoff = time.time() - RETENTION_DAYS * 24 * 3600

START_TS       = datetime.now().strftime("%Y%m%d_%H%M%S")
if FD_DAEMON_NAME == "AId":
    FIXED_LOG_FILE = os.path.join(LOG_DIR, "aid_main.out.log")           # 웹에서 보는 고정 파일
    RUN_LOG_FILE   = os.path.join(LOG_DIR, f"aid_main_{START_TS}.log")   # 런(히스토리) 파일
    for p in glob.glob(os.path.join(LOG_DIR, "aid_main_*.log")):
        try:
            if os.path.getmtime(p) < _cutoff:
                os.remove(p)
        except Exception:
            pass

elif FD_DAEMON_NAME == "AIc":
    FIXED_LOG_FILE = os.path.join(LOG_DIR, "aic_main.out.log")           # 웹에서 보는 고정 파일
    RUN_LOG_FILE   = os.path.join(LOG_DIR, f"aic_main_{START_TS}.log")   # 런(히스토리) 파일
    for p in glob.glob(os.path.join(LOG_DIR, "aic_main_*.log")):
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
        self.logger.propagate = False  # 루트 전파 금지(이중 출력 방지)

        fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

        if not self.logger.handlers:
            # 1) Console handler (web fixed log is filled via stdout redirection)
            ch = logging.StreamHandler(stream=sys.stdout or sys.__stdout__)
            ch.setFormatter(fmt)
            self.logger.addHandler(ch)

            # 2) Direct write to fixed log file (default OFF) - to prevent duplication
            if USE_FIXED_DIRECT:
                mode = 'w' if RESET_ON_START else 'a'
                try:
                    fh = CleanFileHandler(FIXED_LOG_FILE, mode=mode, encoding='utf-8', delay=False)
                except PermissionError:
                    fb_dir = os.path.join(tempfile.gettempdir(), "AId", "logs")
                    os.makedirs(fb_dir, exist_ok=True)
                    fb_file = os.path.join(fb_dir, f"aid_main.fixed.{os.getpid()}.log")
                    fh = CleanFileHandler(fb_file, mode='a', encoding='utf-8', delay=False)
                    print(f"[fd_logging] FIXED denied → fallback {fb_file}", file=sys.stderr)
                fh.setFormatter(fmt)
                self.logger.addHandler(fh)

            # 3) Run (history) file handler (always ON)
            try:
                rh = CleanFileHandler(RUN_LOG_FILE, mode='a', encoding='utf-8', delay=False)
            except PermissionError:
                fb_dir = os.path.join(tempfile.gettempdir(), "AId", "logs")
                os.makedirs(fb_dir, exist_ok=True)
                fb_file = os.path.join(fb_dir, f"aid_main.run.{os.getpid()}.log")
                rh = CleanFileHandler(fb_file, mode='a', encoding='utf-8', delay=False)
                print(f"[fd_logging] RUN denied → fallback {fb_file}", file=sys.stderr)
            rh.setFormatter(fmt)
            self.logger.addHandler(rh)

        # ===== fd_log.print()용 원시 파일 핸들 =====
        self._raw_fixed = None
        if TEE_FIXED_RAW and USE_FIXED_DIRECT:
            try:
                self._raw_fixed = open(FIXED_LOG_FILE, "a", encoding="utf-8", errors="replace")
            except Exception:
                try:
                    fb_dir = os.path.join(tempfile.gettempdir(), "AId", "logs")
                    os.makedirs(fb_dir, exist_ok=True)
                    fb_file = os.path.join(fb_dir, f"aid_main.fixed.{os.getpid()}.log")
                    self._raw_fixed = open(fb_file, "a", encoding="utf-8", errors="replace")
                    print(f"[fd_logging] raw_fixed fallback: {fb_file}", file=sys.stderr)
                except Exception:
                    self._raw_fixed = None

        try:
            self._raw_run = open(RUN_LOG_FILE, "a", encoding="utf-8", errors="replace")
        except Exception:
            fb_dir = os.path.join(tempfile.gettempdir(), "AId", "logs")
            os.makedirs(fb_dir, exist_ok=True)
            fb_file = os.path.join(fb_dir, f"aid_main.run.{os.getpid()}.log")
            self._raw_run = open(fb_file, "a", encoding="utf-8", errors="replace")
            print(f"[fd_logging] raw_run fallback: {fb_file}", file=sys.stderr)

    def get_logger(self):
        return self.logger

    # fd_log.print도 파일/웹에서 같은 포맷으로 보이게(타임스탬프 포함)
    def print(self, msg: str = "", end: str = "\n", flush: bool = True):
        line = f"{_ts()} [INFO] {remove_ansi_escape_sequences(str(msg))}"
        # 콘솔
        out = sys.stdout or sys.__stdout__
        if out:
            try:
                out.write(line + ("" if end is None else end))
                if flush: out.flush()
            except Exception:
                pass
        # 런 파일(필수)
        if getattr(self, "_raw_run", None):
            try:
                self._raw_run.write(line + ("" if end is None else end))
                if flush: self._raw_run.flush()
            except Exception:
                pass
        # (옵션) 고정 파일 tee
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

# 시작 로그
fd_log.info(f"[fd_logging] LOG_DIR={LOG_DIR}")
fd_log.info(f"[fd_logging] FIXED_LOG_FILE={FIXED_LOG_FILE}")
fd_log.info(f"[fd_logging] RUN_LOG_FILE={RUN_LOG_FILE}")
