# ─────────────────────────────────────────────────────────────────────────────#
# aic_main.py
# - 2025/10/17 (revised)
# - Hongsu Jung
# --- how to read log >>> Powershell
# >> Get-Content "C:\4DReplay\V5\daemon\AIc\log\2025-11-20.log" -Wait -Tail 20
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

# ─────────────────────────────────────────────────────────────
# shared codes/functions
# ─────────────────────────────────────────────────────────────
from service_common import *


os.environ["AID_DAEMON_NAME"] = r"AIc"
os.environ.setdefault("FD_LOG_DIR", r"C:\4DReplay\V5\daemon\AIc\log")

# ── sys.path ─────────────────────────────────────────────────────────────────
cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.insert(0, common_path)

# ── project imports ──────────────────────────────────────────────────────────
from fd_common.tcp_server        import TCPServer
from fd_utils.fd_config_manager  import setup, conf, get
from fd_utils.fd_logging         import fd_log

from fd_product.fd_product_clip  import fd_calibrate_files

conf._product = "AIc"
# ─────────────────────────────────────────────────────────────────────────
# 🎯AIc Class (Artificial Intelligence Client)
# ─────────────────────────────────────────────────────────────────────────
class AIc:
    name = 'AIc'

    # ─────────────────────────────────────────────────────────────────────────
    # ✅ MAIN FUNCTIONS
    # ─────────────────────────────────────────────────────────────────────────

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

        # product info
        self.prod_video_source  = None
        self.prod_adjust_info   = None
        self.prod_info = None
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
    def run(self):
        fd_log.info("🟢 [AIc] run() begin..")
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
    # 📦 Message Routing
    # ─────────────────────────────────────────────────────────────────────────
    def on_msg(self, text: str):
        try:
            data = json.loads(text)
        except Exception as e:
            fd_log.error(f"[{self.name}] on_msg JSON parse error: {e}; text={text[:256]}")
            return
        self.put_data(data)  # 큐에 넣고 worker가 처리
    def put_data(self, data):
        with self.lock:
            self.msg_queue.put(data)
    def on_aid_msg(self, data):
        # 1) bytes → str        
        if isinstance(data, bytes):
            if not data:
                fd_log.warning("[AIc] empty packet received from AId")
                return
            fd_log.info(f"[AIc] << AId request (bytes): {data[:500]!r}")
            data = data.decode(errors="ignore")

        # 2) str → dict(JSON)
        if isinstance(data, str):
            if not data.strip():
                fd_log.warning("[AIc] empty text data received from AId")
                return
            try:
                data = json.loads(data)
            except Exception as e:
                fd_log.warning(
                    f"[AIc] on_aid_msg JSON parse error: {e}; raw={data!r}"
                )
                return

        # 3) dict가 아니면 폐기
        if not isinstance(data, dict):
            fd_log.warning(f"[AIc] on_aid_msg: invalid data type after parse: {type(data)}")
            return

        # 4) dispatch
        self._dispatch_aid_command(data) 
    # 🎯 command processing
    def _dispatch_aid_command(self, pkt: dict):
        sec1 = pkt.get("Section1")
        sec2 = pkt.get("Section2")
        sec3 = pkt.get("Section3")
        state = str(pkt.get("SendState", "")).lower()
        action = str(pkt.get("Action", "")).lower()

        # Version 요청
        match (sec1, sec2, sec3):
            # ──────────────────────────────────────────────────────
            # 📦 V5 : [AIc], [Information], [Version]
            # ──────────────────────────────────────────────────────                    
            case ("AIc", "Information", "Version"):
                return self.get_version_request(pkt)
            # ──────────────────────────────────────────────────────
            # 📦 V5 : [AIc], [Operation], [Prepare]
            # ──────────────────────────────────────────────────────
            case ("AIc", "Operation", "Prepare"):
                return self.production_prepare(pkt)
            # ──────────────────────────────────────────────────────
            # 📦 V5 : [AIc], [Operation], [Production], [start/stop]
            # ───────────────────────────────────────`───────────────
            case ("AIc", "Operation", "Production"):
                if action == "start":
                    return self.production_start(pkt)
                elif action == "stop":
                    return self.production_stop(pkt)
            # ──────────────────────────────────────────────────────
            # 📦 V5 : not matching packet
            # ──────────────────────────────────────────────────────
            case _:
                fd_log.warning(f"[AIc] unhandled AId command: {sec1}/{sec2}/{sec3}/{state}")
                pass
    
    # ─────────────────────────────────────────────────────────────────────────
    # 🧩 Functions for Events (V5)
    # ─────────────────────────────────────────────────────────────────────────
    # get version request
    def get_version_request(self, pkt: dict) -> None:
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
            try:
                fd_log.info(f"AId response: {json.dumps(resp, ensure_ascii=False)}")
                self.aid_server.send_msg(json.dumps(resp))
            except Exception as e:
                fd_log.error(f"send_msg failed: {e}")
        else:
            fd_log.error("aid_server is None, cannot send Version response to AId")
    # get prepare request
    def production_prepare(self, pkt: dict) -> None:
        fd_log.info("🚀 [AIc] Handle Prepare from AId")
        camera_info = pkt.get("CamInfo")        
        '''
        "camera-format": {
            "fps":60,
            "resolution":"UHD"
        },
        "video_source": {
            "ip": "10.82.104.210",
            "cam_ips": [
                {"ip":"10.82.104.11","rotate":1},
                {"ip":"10.82.104.12","rotate":1}
            ],
            "path": "C_Movie|C:\\"
        },
        "adjust": {
            ... adjust_info ...
        }
        '''
        self.prod_video_source  = camera_info["video-info"]
        self.prod_adjust_info   = camera_info["adjust"]

        fd_log.info(f"[AIc] ⏯️ video_source:  {self.prod_video_source}")
        fd_log.info(f"[AIc] ⏯️ adjust_info: {self.prod_adjust_info}")

        # 응답 패킷(optional)
        resp = {
            "Section1": "AIc",
            "Section2": "Operation",
            "Section3": "Prepare",
            "SendState": "response",
            "From": "AIc",
            "To": "AId",
            "Action": "set",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }

        try:
            fd_log.info(f"[AIc] >> AId Prepare Response: {resp}")
            self.aid_server.send_msg(json.dumps(resp))
        except Exception as e:
            fd_log.error(f"[AIc] Prepare response send failed: {e}")
    # production start
    def production_start(self, pkt: dict) -> None:
        fd_log.info("🚀 [AIc] Handle Production Start from AId")

        product_info = pkt.get("product_info")
        self.prod_info = product_info

        fd_log.info(f"[AIc] ▶ video_source:  {self.prod_video_source}")
        fd_log.info(f"[AIc] ▶ adjust_info: {self.prod_adjust_info}")
        fd_log.info(f"[AIc] ▶ product_info: {self.prod_info}")

        # ───────────────────────────────────
        # 📌 create / calibration files to output folder
        # ───────────────────────────────────
        fd_calibrate_files(
            self.prod_video_source,
            self.prod_info,
            self.prod_adjust_info,            
        )

        # ───────────────────────────────────
        # 📩 send response to AId
        # ───────────────────────────────────
        fd_log.info(f"[AIc] ⏯️ product_info: {self.prod_info}")
        # 응답 패킷(optional)
        resp = {
            "Section1": "AIc",
            "Section2": "Operation",
            "Section3": "Production",
            "SendState": "response",
            "From": "AIc",
            "To": "AId",
            "Action": "start",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }
        try:
            fd_log.info(f"[AIc] >> AId Prepare Response: {resp}")
            self.aid_server.send_msg(json.dumps(resp))
        except Exception as e:
            fd_log.error(f"[AIc] Prepare response send failed: {e}")
    # production stop
    def production_stop(self, pkt: dict) -> None:
        fd_log.info("⏹️ [AIc] Handle Production Stop from AId")
        
        # ───────────────────────────────────
        # 📩 send response to AId
        # ───────────────────────────────────        
        resp = {
            "Section1": "AIc",
            "Section2": "Operation",
            "Section3": "Production",
            "SendState": "response",
            "From": "AIc",
            "To": "AId",
            "Action": "stop",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }
        try:
            fd_log.info(f"[AIc] >> AId Prepare Response: {resp}")
            self.aid_server.send_msg(json.dumps(resp))
        except Exception as e:
            fd_log.error(f"[AIc] Prepare response send failed: {e}")


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
