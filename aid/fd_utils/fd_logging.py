# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_logging
# - 2025/07/25
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import logging
import os
import re
import sys
from datetime import datetime
from colorama import init

init(autoreset=True)

# 로그 디렉토리 및 파일 생성
os.makedirs("logs", exist_ok=True)
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
log_filename = f"logs/log_{timestamp}.log"

# ANSI 제거용 (info용 포맷터에 사용)
def remove_ansi_escape_sequences(text):
    ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
    return ansi_escape.sub('', text)

class CleanFileHandler(logging.FileHandler):
    def emit(self, record):
        record.msg = remove_ansi_escape_sequences(str(record.msg))
        super().emit(record)

class FDLogger:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FDLogger, cls).__new__(cls)
            cls._instance._initialize_logger()
        return cls._instance

    def _initialize_logger(self):
        self.logger = logging.getLogger("fd_logger")
        self.logger.setLevel(logging.INFO)

        if not self.logger.handlers:
            # Console
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(logging.Formatter('%(message)s'))

            # Clean formatted log
            file_handler = CleanFileHandler(log_filename, encoding='utf-8')
            file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

            self.logger.addHandler(console_handler)
            self.logger.addHandler(file_handler)

        # 🔥 log file object to store raw console print
        self._raw_log_file = open(log_filename, "a", encoding="utf-8")

    def get_logger(self):
        return self.logger

    def print(self, msg: str = "", end="\n", flush=True):
        # ANSI 없이 로그에 저장 / 콘솔은 ANSI 포함 출력
        clean_msg = remove_ansi_escape_sequences(msg)

        # 콘솔 출력
        sys.stdout.write(msg + ("" if end is None else end))
        if flush:
            sys.stdout.flush()

        # 파일 출력
        self._raw_log_file.write(clean_msg + ("" if end is None else end))
        if flush:
            self._raw_log_file.flush()

    def close(self):
        self._raw_log_file.close()

# 전역 객체
fd_logger_instance = FDLogger()
fd_log = fd_logger_instance.get_logger()
fd_log.print = fd_logger_instance.print
fd_log.close = fd_logger_instance.close