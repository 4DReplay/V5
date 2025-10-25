# ─────────────────────────────────────────────────────────────────────────────#
# fd_logging.py
# date: 2025/10/24
# owner: Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import logging, os, re, sys, tempfile, glob, time
from datetime import datetime

# =========================
# 스위치(환경변수)
# =========================
# 고정 로그( aid_main.out.log )에 직접 쓰기 여부
# 기본 0(비활성) → 고정 로그는 에이전트가 stdout을 리다이렉션해서 채움(중복 방지)
USE_FIXED_DIRECT = os.environ.get("AID_LOG_TO_FIXED", "0") == "1"

# 시작 시 고정 로그를 truncate 할지(직접 쓰는 경우에만 의미 있음)
RESET_ON_START   = os.environ.get("AID_LOG_RESET", "0") == "1"

# fd_log.print 의 원시 텍스트를 고정 파일로도 tee 할지(권장: 0)
TEE_FIXED_RAW    = os.environ.get("AID_TEE_RAW_FIXED", "0") == "1"

# 오래된 런 로그 보관 기간(일)
RETENTION_DAYS   = int(os.environ.get("AID_LOG_RETENTION_DAYS", "60"))


# =========================
# 유틸
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
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_programdata = os.environ.get("ProgramData", r"C:\ProgramData")
_env_dir     = (os.environ.get("AID_LOG_DIR") or "").strip()

_candidates = []
if _env_dir:
    _candidates.append(_env_dir)
_candidates.extend([
    os.path.join(_programdata, "4DReplay", "AId", "logs"),
    os.path.join(BASE_DIR, "logs"),
    os.path.join(tempfile.gettempdir(), "AId", "logs"),
])

LOG_DIR = _first_writable_dir(_candidates)

# =========================
# 파일명
# =========================
START_TS       = datetime.now().strftime("%Y%m%d_%H%M%S")
FIXED_LOG_FILE = os.path.join(LOG_DIR, "aid_main.out.log")           # 웹에서 보는 고정 파일
RUN_LOG_FILE   = os.path.join(LOG_DIR, f"aid_main_{START_TS}.log")   # 런(히스토리) 파일

# 오래된 런 로그 정리
_cutoff = time.time() - RETENTION_DAYS * 24 * 3600
for p in glob.glob(os.path.join(LOG_DIR, "aid_main_*.log")):
    try:
        if os.path.getmtime(p) < _cutoff:
            os.remove(p)
    except Exception:
        pass


# =========================
# 핸들러
# =========================
class CleanFileHandler(logging.FileHandler):
    def emit(self, record):
        d = record.__dict__.copy()
        d["msg"] = remove_ansi_escape_sequences(str(d.get("msg", "")))
        try:
            super().emit(logging.makeLogRecord(d))
        except Exception:
            pass


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
            # 1) 콘솔 핸들러(웹 고정 로그는 stdout 리다이렉션으로 채움)
            ch = logging.StreamHandler(stream=sys.stdout or sys.__stdout__)
            ch.setFormatter(fmt)
            self.logger.addHandler(ch)

            # 2) 고정 파일 직접 쓰기(기본 OFF) - 중복 방지 목적
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

            # 3) 런(히스토리) 파일 핸들러(항상 ON)
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
