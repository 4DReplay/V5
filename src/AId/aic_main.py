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
from threading import Semaphore

# ── service/env ──────────────────────────────────────────────────────────────
SERVICE_MODE = os.getenv("FD_SERVICE", "0") == "1"
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
        self.app_server = None   # (옵션) 외부 송신용 TCP/WS 등
        self.tcp = None          # 인바운드 TCP 리스너
        self.end = False
        self.host = None
        self.msg_queue = queue.Queue()
        self.lock = threading.Lock()
        self._stopped = False

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

        fd_log.info(f"📄 [AIc] Load Config - Private {config_private_path}")
        fd_log.info(f"📄 [AIc] Load Config - Public  {config_public_path}")

        # 전역 conf 락을 인스턴스 락에 바인딩
        conf._lock = self.lock

        port = conf._aic_daemon_port
        if self.tcp and getattr(self.tcp, "sock", None):
            fd_log.info(f"[{self.name}] TCP already listening 0.0.0.0:{port}")
            return True

        fd_log.info(f"📄 [AIc] TCPService: port {port}")
        try:
            # fd_common.tcp_server.TCPServer(host, port, handle, name)
            self.tcp = TCPServer("0.0.0.0", port, handle=self.on_msg, name=self.name)
            self.tcp.open()
            fd_log.info(f"[{self.name}] listening on 0.0.0.0:{port}")
            return True
        except Exception as e:
            fd_log.error(f"[{self.name}] TCP server start failed on {port}: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # def put_data
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

        # 인바운드 TCP 서버 종료
        try:
            if self.tcp:
                self.tcp.close()
        except Exception as e:
            fd_log.warning(f"[AIc] tcp close failed: {e}")
        finally:
            self.tcp = None

        # (옵션) 아웃바운드 송신 소켓/서버 정리
        srv = getattr(self, "app_server", None)
        if srv is not None:
            try:
                if hasattr(srv, "shutdown"):
                    srv.shutdown()
                elif hasattr(srv, "close"):
                    srv.close()
                else:
                    fd_log.warning("[AIc] app_server has no close/shutdown; skipping.")
            except Exception as e:
                fd_log.warning(f"[AIc] app_server close failed: {e}")
            finally:
                self.app_server = None
        else:
            fd_log.info("[AIc] app_server is None; nothing to close.")

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
    # 워커 루프
    # ─────────────────────────────────────────────────────────────────────────
    def status_task(self):
        fd_log.info("🟢 [AIc] Message Receive Start")
        while not self.end:
            msg = None
            with self.lock:
                if not self.msg_queue.empty():
                    msg = self.msg_queue.get(block=False)
            if msg is not None:
                self.classify_msg(msg)
            time.sleep(0.01)
        fd_log.info("🔴 [AIc] Message Receive End")

    # ─────────────────────────────────────────────────────────────────────────
    # 실행 시작
    # ─────────────────────────────────────────────────────────────────────────
    def run(self):
        fd_log.info("🟢 [AIc] run() begin..")
        self.th = threading.Thread(target=self.status_task, daemon=True)
        self.th.start()

    # ─────────────────────────────────────────────────────────────────────────
    # 메시지 라우팅(스텁)
    # ─────────────────────────────────────────────────────────────────────────
    def classify_msg(self, msg: dict) -> None:
        _4dmsg = FDMsg()
        _4dmsg.assign(msg)

        if len(_4dmsg.data.get('From', '').strip()) == 0:
            _4dmsg.data.update(From='4DOMS')

        result_code, err_msg = 1000, ''
        if _4dmsg.is_valid():
            conf._result_code = 0
            if (state := _4dmsg.get('SendState').lower()) == FDMsg.REQUEST:
                sec1, sec2, sec3 = _4dmsg.get('Section1'), _4dmsg.get('Section2'), _4dmsg.get('Section3')

                match sec1, sec2, sec3:
                    case 'Daemon', 'Information', 'Version':
                        _4dmsg.update(Version={
                            AIc.name: {'version': conf._version, 'date': conf._release_date}
                        })

                    case 'AI', 'Operation', 'Calibration':
                        conf._processing = True
                        fd_log.info("AI → Operation → Calibration")
                        conf._processing = False

                    case 'AI', 'Operation', 'LiveEncoding':
                        conf._processing = True
                        fd_log.info("Start LiveEncoding")
                        conf._processing = False

                    case 'AI', 'Operation', 'PostStabil':
                        conf._processing = True
                        fd_log.info("AI → Operation → PostStabil")
                        conf._processing = False

                    case 'AI', 'Operation', 'StartVideo':
                        conf._processing = True
                        fd_log.info("AI → Operation → StartVideo")
                        conf._processing = False

                    case 'AI', 'Process', 'Multi':
                        conf._processing = True
                        fd_log.info("AI → Process → Multi (Calibration multi-channel)")
                        conf._processing = False

                    case 'AI', 'Process', 'LiveDetect':
                        conf._processing = True
                        fd_log.info("AI → Process → LiveDetect (baseball/nascar live paths)")
                        conf._processing = False

                    case 'AI', 'Process', 'UserStart':
                        conf._processing = True
                        fd_log.info("AI → Process → UserStart (nascar clip marking)")
                        conf._processing = False

                    case 'AI', 'Process', 'UserEnd':
                        conf._processing = True
                        fd_log.info("AI → Process → UserEnd (nascar clip finalize)")
                        conf._processing = False

                    case 'AI', 'Process', 'LiveEnd':
                        conf._processing = True
                        fd_log.info("AI → Process → LiveEnd")
                        conf._processing = False

                    case 'AI', 'Process', 'Merge':
                        conf._processing = True
                        fd_log.info("AI → Process → Merge (nascar merge result → reply with output)")
                        conf._processing = False

                    case 'AI', 'Process', 'Detect':
                        conf._processing = True
                        fd_log.info("AI → Process → Detect (baseball clip make)")
                        conf._processing = False

            elif state == FDMsg.RESPONSE:
                pass  # 응답 수신 시 기본 처리 없음

        else:
            fd_log.error(f'[AIc] message parsing error..\nMessage:\n{msg}')
            conf._result_code += 100
            _4dmsg.update(Section1="AI", Section2="Process", Section3="Multi",
                          From="4DPD", To="AIc", ResultCode=conf._result_code,
                          ErrorMsg=err_msg)
            _4dmsg.toggle_status()

            if conf._result_code > 100:
                conf._result_code = 0
                if not self.app_server:
                    fd_log.warning("[AIc] classify_msg(error path): app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])

    # ─────────────────────────────────────────────────────────────────────────
    # (옵션) 외부 이벤트 송신 스텁
    # ─────────────────────────────────────────────────────────────────────────
    def on_web_socket_event(self, pitch_data):
        msg = {
            "From": "AIc",
            "To": "AId",
            "SendState": "Request",
            "Section1": "WebSocket",
            "Section2": "Realtime",
            "Section3": "Pitch",
            "Data": pitch_data
        }
        if not self.app_server:
            fd_log.warning("[AIc] on_web_socket_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))

    def on_stabil_done_event(self, output_file):
        msg = {
            "From": "AIc",
            "To": "AId",
            "SendState": "Request",
            "Section1": "StabilizeDone",
            "Section2": "",
            "Section3": "",
            "Complete": "OK",
            "Output": output_file
        }
        if not self.app_server:
            fd_log.warning("[AIc] on_stabil_done_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))


if __name__ == '__main__':
    # 작업 디렉터리: 프로젝트 루트
    base_path = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_path, ".."))
    os.chdir(project_root)
    conf._path_base = os.getcwd()

    fd_log.info("─────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"📂 [AIc] Working directory: {conf._path_base}")

    ver, date = conf.read_latest_release_from_md(f"{conf._path_base}\\AId\\aid_release.md")
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
