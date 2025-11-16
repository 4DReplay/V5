# ─────────────────────────────────────────────────────────────────────────────#
# aic_main.py
# - 2025/10/17 (revised)
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import os
import sys
import json
import queue
import time
import threading
import signal
import atexit
import socket
from threading import Semaphore
from datetime import datetime

# ── service/env ──────────────────────────────────────────────────────────────
def _is_service_env():
    try:
        return not hasattr(sys, "stdin") or (sys.stdin is None) or (not sys.stdin.isatty())
    except Exception:
        return True
SERVICE_MODE = (os.getenv("FD_SERVICE", "0") == "1") or _is_service_env()

os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["AID_DAEMON_NAME"] = r"AIc"
os.environ.setdefault("FD_LOG_DIR", r"C:\4DReplay\V5\daemon\AIc\log")

# ── sys.path ─────────────────────────────────────────────────────────────────
cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.insert(0, common_path)

# ── project imports ──────────────────────────────────────────────────────────
from fd_common.msg               import FDMsg
from fd_common.tcp_server        import TCPServer
from fd_utils.fd_config_manager  import setup, conf, get
from fd_utils.fd_logging         import fd_log
from fd_utils.fd_file_edit       import fd_clean_up

# 필요한 경우에만 사용하는 모듈들(여기선 동작 스텁 수준 로그만 남김)
from fd_stream.fd_stream_rtsp    import StreamViewer
from fd_stabil.fd_stabil         import PostStabil
from fd_utils.fd_calibration     import Calibration
from fd_aid                      import (
    fd_create_analysis_file,
    fd_multi_channel_video,
    fd_multi_calibration_video,
)

fd_log.propagate = False
conf._product = "AIc"

# ── config path ──────────────────────────────────────────────────────────────
AID_CONFIG_PRIVATE = "./config/aid_config_private.json5"
AID_CONFIG_PUBLIC  = "./config/aid_config_public.json5"


class AIc:
    name = 'AIc'

    def __init__(self):
        self.name = "AIc"
        self.property_data = None
        self.th = None
        self.aid_server = None   # AId <-> AIc 전용 TCPServer (19738)
        self.end = False
        self.host = None
        self.msg_queue = queue.Queue()
        self.lock = threading.Lock()
        self._stopped = False

        self.conf = conf  # conf 객체를 직접 할당
        self.version = self.conf._version  # conf에서 _version 가져오기
        self.release_date = self.conf._release_date  # conf에서 release_date 가져오기

    # ─────────────────────────────────────────────────────────────────────────
    # 시스템 초기화(로그 폴더 등). 실패시 False
    # ─────────────────────────────────────────────────────────────────────────
    def init_sys(self) -> bool:
        current_path = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(current_path, "log")
        try:
            if not os.path.exists(log_path):
                fd_log.info("create the log directory.")
                os.makedirs(log_path)
        except OSError:
            fd_log.error("Failed to create the directory.")
            return False

        if os.getenv("PYTHONBREAKPOINT") is None:
            os.environ["PYTHONBREAKPOINT"] = "0"
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # 설정 로딩 및 TCP 리스너 오픈
    # ─────────────────────────────────────────────────────────────────────────
    def prepare(self,
                config_private_path: str = AID_CONFIG_PRIVATE,
                config_public_path: str = AID_CONFIG_PUBLIC) -> bool:
        setup(
            config_private_path,
            config_public_path,
            runtime_factories={
                "_NVENC_START_SEM": lambda c: Semaphore(
                    int(os.getenv("FD_NVENC_INIT_CONCURRENCY", c._gpu_session_init_cnt))
                ),
                "_NVENC_MAX_SEM": lambda c: Semaphore(
                    int(os.getenv("FD_NVENC_MAX_SLOTS", c._gpu_session_max_cnt))
                ),
            },
        )

        try:
            aid_port = conf._aic_daemon_port
            self.aid_server = TCPServer("", aid_port, self.on_aid_msg)
            self.aid_server.open()
            fd_log.info(f"[{self.name}] listening for AId on 0.0.0.0:{aid_port}")
            return True
        except Exception as e:
            fd_log.error(f"[{self.name}] TCP server start failed: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # def put_data (OMS/4DOMS 쪽에서 오는 메시지)
    # ─────────────────────────────────────────────────────────────────────────
    def put_data(self, data):
        with self.lock:
            self.msg_queue.put(data)

    # ─────────────────────────────────────────────────────────────────────────
    # def on_msg
    # ─────────────────────────────────────────────────────────────────────────
    def on_msg(self, text: str):
        try:
            data = json.loads(text)
        except Exception as e:
            fd_log.error(f"[{self.name}] on_msg JSON parse error: {e}; text={text[:256]}")
            return
        self.put_data(data)  # 큐에 넣고 worker가 처리

    # ----------------------------------------------------------
    # AId -> AIc : persistent 포트(19738)로 들어오는 메시지 처리
    # ----------------------------------------------------------
    def on_aid_msg(self, data):
        # 1) bytes → str
        if isinstance(data, bytes):
            data = data.decode(errors="ignore")

        # 2) str → dict(JSON)
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception as e:
                fd_log.warning(f"[AIc] on_aid_msg: JSON parse error: {e}; raw={data[:200]}")
                return

        # 3) dict가 아니면 폐기
        if not isinstance(data, dict):
            fd_log.warning(f"[AIc] on_aid_msg: invalid data type after parse: {type(data)}")
            return

        # 4) dispatch
        self._dispatch_aid_command(data) 

    # ─────────────────────────────────────────────────────────────────────────
    # AID Commands
    # ─────────────────────────────────────────────────────────────────────────
    def _dispatch_aid_command(self, pkt: dict):
        sec1 = pkt.get("Section1")
        sec2 = pkt.get("Section2")
        sec3 = pkt.get("Section3")
        state = str(pkt.get("SendState", "")).lower()

        # Version 요청
        if (sec1, sec2, sec3) == ("AIc", "Information", "Version") and state == "request":
            return self.handle_version_request_from_aid(pkt)

        # Calibration 명령 예시
        if (sec1, sec2, sec3) == ("AI", "Operation", "Calibration"):
            return self.handle_calibration(pkt)

        # StartVideo 명령 예시
        if (sec1, sec2, sec3) == ("AI", "Operation", "StartVideo"):
            return self.handle_start_video(pkt)

        # 앞으로 여기에 계속 명령 추가
        # if (sec1, sec2, sec3) == (...):
        #     return self.handle_xxx(pkt)

        fd_log.warning(f"[AIc] unhandled AId command: {sec1}/{sec2}/{sec3}/{state}")
    # ─────────────────────────────────────────────────────────────────────────
    # 안전 종료(멱등)
    # ─────────────────────────────────────────────────────────────────────────
    def stop(self):
        fd_log.info("[AIc] stop() begin..")

        if self._stopped:
            fd_log.info("[AIc] stop() already called; skipping.")
            return
        self._stopped = True

        self.end = True

        # AId <-> AIc 전용 서버 종료
        try:
            if self.aid_server:
                self.aid_server.close()
        except Exception as e:
            fd_log.warning(f"[AIc] aid_server close failed: {e}")
        finally:
            self.aid_server = None

        # 워커 합류
        th = getattr(self, "th", None)
        if th is not None:
            try:
                if th.is_alive():
                    th.join(timeout=3.0)
            except Exception as e:
                fd_log.warning(f"[AIc] thread join failed: {e}")
            finally:
                self.th = None
        else:
            fd_log.info("[AIc] worker thread is None; nothing to join.")

        fd_log.info("[AIc] stop() end..")

    # ─────────────────────────────────────────────────────────────────────────
    # AId → AIc : Version 요청 처리(19738 포트)
    # ─────────────────────────────────────────────────────────────────────────
    def handle_version_request_from_aid(self, pkt: dict) -> None:
        """
        AId → AIc : Version 요청
        - conf._version, conf._release_date 를 그대로 사용
        - AId 쪽은 SenderIP 를 굳이 믿지 않아도, TCPClient 생성 시점에 IP를 알고 있음.
        """
        ver = self.version
        date = self.release_date

        resp = {
            "Section1": "Daemon",
            "Section2": "Information",
            "Section3": "Version",
            "SendState": "response",
            "From": "AIc",
            "To": "AId",
            "Token": pkt.get("Token"),
            "Action": "set",
            "DMPDIP": pkt.get("DMPDIP"),
            "Version": {
                "AIc": {
                    "version": ver,
                    "date": date,
                }
            },
            # 선택 사항: 자기 IP를 같이 실어줌
            "SenderIP": socket.gethostbyname(socket.gethostname()),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }

        if self.aid_server:
            self.aid_server.send_msg(json.dumps(resp))
        else:
            fd_log.error("[AIc] aid_server is None, cannot send Version response to AId")

    # ─────────────────────────────────────────────────────────────────────────
    # 실행 시작
    # ─────────────────────────────────────────────────────────────────────────
    def run(self):
        fd_log.info("🟢 [AIc] run() begin..")

    # ─────────────────────────────────────────────────────────────────────────
    # 메시지 라우팅(스텁)
    # ─────────────────────────────────────────────────────────────────────────
    def classify_msg(self, msg: dict) -> None:
        # AIc는 더 이상 OMS/4DOMS 메시지를 처리하지 않음
        return

    # ─────────────────────────────────────────────────────────────────────────
    # (옵션) 외부 이벤트 송신 스텁
    # ─────────────────────────────────────────────────────────────────────────
    def on_web_socket_event(self, pitch_data):
        # AIc는 OMS 송신 기능 없음
        return



if __name__ == '__main__':
    # 작업 디렉터리: 프로젝트 루트
    base_path = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_path, ".."))
    os.chdir(project_root)
    conf._path_base = os.getcwd()

    fd_log.info("─────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"📂 [AIc] Working directory: {conf._path_base}")

    # 1) get version from markdown
    release_md_path = os.path.join(conf._path_base, "AId", "aic_release.md")
    ver, _ = conf.read_latest_release_from_md(release_md_path)

    # 2) get last modified time of aid_release.md as release date
    try:
        stat = os.stat(release_md_path)
        dt = datetime.fromtimestamp(stat.st_mtime)
        # Example: "Nov 11 2025 - 16:13:33"
        date = dt.strftime("%b %d %Y - %H:%M:%S")
    except Exception as e:
        # Fallback when something goes wrong
        fd_log.warning(f"[AIc] failed to read release file mtime: {e}")
        date = ""

    conf._version = ver
    conf._release_date = date

    fd_log.info(f"🧩 Latest Version: {conf._version}")
    fd_log.info(f"📅 Latest Date: {conf._release_date}")
    fd_log.info("─────────────────────────────────────────────────────────────────────────────")

    aic = AIc()

    # 종료 훅
    def _graceful_shutdown(signame=""):
        try:
            fd_log.info(f"[AIc] graceful shutdown ({signame})")
        except Exception:
            pass
        try:
            aic.stop()
        except Exception:
            pass
        os._exit(0)

    try:
        signal.signal(signal.SIGINT,  lambda *_: _graceful_shutdown("SIGINT"))
        signal.signal(signal.SIGTERM, lambda *_: _graceful_shutdown("SIGTERM"))
    except Exception:
        pass
    atexit.register(lambda: aic.stop())

    # 준비
    if not aic.init_sys():
        fd_log.error("init_sys() failed")
        sys.exit(1)

    if not aic.prepare():
        fd_log.error("prepare() failed")
        sys.exit(0)  # 서비스 관리자 입장에서 '정상 종료'처럼 처리

    exit_code = 0
    try:
        aic.run()
    except KeyboardInterrupt:
        fd_log.warning("Interrupted by user (Ctrl+C).")
    except SystemExit as e:
        exit_code = int(getattr(e, "code", 1) or 1)
        fd_log.warning(f"SystemExit captured with code={exit_code}")
    except Exception as e:
        fd_log.error(f"Unhandled exception in run(): {e}")
        exit_code = 1

    if SERVICE_MODE:
        fd_log.info("[AIc] SERVICE_MODE: blocking main thread")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    # 서비스 모드에서는 입력 대기 금지
    if (not SERVICE_MODE) and get("_test_mode", True):
        while True:
            fd_log.info("─────────────────────────────────────────────────────────────────────────────")
            user_input = input("⌨ Key Press: \n")
            if not user_input:
                continue
            fd_log.info(f"[AIc] Input received:{user_input}")

    if (not SERVICE_MODE) and get("_test_mode", True):
        aic.stop()
        sys.exit(exit_code)
