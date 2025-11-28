# ─────────────────────────────────────────────────────────────────────────────#
# aid_main.py
# - 2025/10/17 (revised)
# - Hongsu Jung
# --- how to read log >>> Powershell
# >> Get-Content "C:\4DReplay\V5\daemon\AId\log\2025-11-20.log" -Wait -Tail 20
# ─────────────────────────────────────────────────────────────────────────────#

import os
import sys
import json
import struct
import queue
import time
import threading
import shutil
import signal
import atexit
import socket
from threading import Semaphore
from datetime import datetime

# ─────────────────────────────────────────────────────────────
# shared codes/functions
# ─────────────────────────────────────────────────────────────
from service_common import *

os.environ["AID_DAEMON_NAME"] = r"AId"
os.environ.setdefault("FD_LOG_DIR", r"C:\4DReplay\V5\daemon\AId\log")

# ── sys.path ─────────────────────────────────────────────────────────────────
cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.insert(0, common_path)

# ── imports (project) ────────────────────────────────────────────────────────
from fd_common.msg                  import FDMsg
from fd_common.tcp_server           import TCPServer   # communication with MTd
from fd_common.tcp_client           import TCPClient   # communication with AIc
from fd_common.utils                import get_duration

from fd_utils.fd_config_manager     import setup, conf, get
from fd_utils.fd_logging            import fd_log
from fd_utils.fd_file_edit          import fd_clean_up

from fd_aid                         import fd_create_analysis_file
from fd_aid                         import fd_multi_channel_video
from fd_aid                         import fd_multi_calibration_video

from fd_utils.fd_calibration        import Calibration
from fd_manager.fd_create_clip      import play_and_create_multi_clips

# ── imports (projectproduct) ────────────────────────────────────────────────────────
from fd_product.fd_product_clip     import fd_convert_AIc_info, fd_create_payload_for_preparing_to_AIc
from fd_product.fd_product_clip     import fd_create_payload_for_product_to_AIc

# ─────────────────────────────────────────────────────────────────────────
# 🎯AId Class (Artificial Intelligence Daemon)
# ─────────────────────────────────────────────────────────────────────────
class AId:
    name = 'AId'

    # ─────────────────────────────────────────────────────────────────────────
    # ✅MAIN FUNCTIONS
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self):
        self.name = "AId"
        self.property_data = None
        self.th = None
        self.app_server = None     # 외부로 응답 송신시 사용(없을 수 있음)
        self.end = False
        self.host = None        
        self.msg_queue = queue.Queue()
        self._lock = threading.Lock()
        self._stopped = False

        self.conf = conf  # conf 객체를 직접 할당
        self.version = self.conf._version  # conf에서 _version 가져오기
        self.release_date = self.conf._release_date  # conf에서 release_date 가져오기

        # AIc 연결 관리 (persistent AId -> AIc)
        #  - aic_name_ip_map : MTd에서 받은 이름 → IP 매핑
        #  - aic_ip_name_map : IP → 이름 매핑
        #  - aic_sessions    : IP 별 TCPClient 세션
        #  - aic_version_cache: IP 별 버전 응답 캐시
        self.aic_name_ip_map: dict[str, str] = {}
        self.aic_ip_name_map: dict[str, str] = {}
        self.aic_sessions = {}        # { ip: TCPClient }
        self.aic_version_cache = {}   # { ip: {"name":..,"ip":..,"version":..,"date":..} }

        # production variables
        self.camera_fps = 0
        self.prod_camera_env = {}
        self.prod_adjust_info = {}
    # System initialization (e.g., log folder). Returns False on failure.    
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

        # 생산 환경에서 breakpoint 비활성화
        if os.getenv("PYTHONBREAKPOINT") is None:
            os.environ["PYTHONBREAKPOINT"] = "0"
        return True
    # Load configuration and open TCP listener
    def prepare(self, config_private_path: str = AID_CONFIG_PRIVATE,
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

        fd_log.info(f"📄 [AId] Load Config - Private {config_private_path}")
        fd_log.info(f"📄 [AId] Load Config - Public  {config_public_path}")

     
        # NOTE: 전역 conf 사용 (self.conf 아님)
        port = conf._aid_daemon_port
        fd_log.info(f"📄 [AId] TCPService: port {port}")

        try:
            self.app_server = TCPServer("", port, self.put_data)
            self.app_server.open()
            fd_log.info(f"[{self.name}] listening on 0.0.0.0:{port}")
            return True
        except Exception as e:
            fd_log.error(f"[{self.name}] TCP server start failed on {port}: {e}")
            return False
    # AId Service Start
    def run(self):
        fd_log.info("🟢 [AId] run() begin..")
        self.th = threading.Thread(target=self.status_task, daemon=True)
        self.th.start()
    # stop the AId service
    def stop(self):
        fd_log.info("[AId] stop() begin..")

        if self._stopped:
            fd_log.info("[AId] stop() already called; skipping.")
            return
        self._stopped = True

        self.end = True

        # 인바운드 TCP 서버 종료
        try:
            if self.app_server:
                self.app_server.close()
        except Exception as e:
            fd_log.warning(f"[AId] app_server close failed: {e}")
        finally:
            self.app_server = None

        # 아웃바운드 서버(있을 수 있음) 종료
        srv = getattr(self, "app_server", None)
        if srv is not None:
            try:
                if hasattr(srv, "shutdown"):
                    srv.shutdown()
                elif hasattr(srv, "close"):
                    srv.close()
                else:
                    fd_log.warning("[AId] app_server has no close/shutdown; skipping.")
            except Exception as e:
                fd_log.warning(f"[AId] app_server close failed: {e}")
            finally:
                self.app_server = None
        else:
            fd_log.info("[AId] app_server is None; nothing to close.")

        # 워커 합류
        th = getattr(self, "th", None)
        if th is not None:
            try:
                if th.is_alive():
                    th.join(timeout=3.0)
            except Exception as e:
                fd_log.warning(f"[AId] thread join failed: {e}")
            finally:
                self.th = None
        else:
            fd_log.info("[AId] worker thread is None; nothing to join.")

        fd_log.info("[AId] stop() end..")


    # ─────────────────────────────────────────────────────────────────────────
    # COMMON FUNCTIONS
    # ─────────────────────────────────────────────────────────────────────────    
    # Check AIc connectivity (simple TCP connect)
    def check_aic_connectivity(self, ip: str, timeout: float = 1.0) -> bool:
        """Check connectivity to an AIc daemon by trying a TCP connect.

        Returns True if connection succeeds, False otherwise.
        """
        port = getattr(conf, "_aic_daemon_port", None)
        if not port:
            fd_log.error("[AId] _aic_daemon_port is not configured.")
            return False

        sock = None
        try:
            fd_log.info(f"[AId] checking AIc connectivity: {ip}:{port}")
            sock = socket.create_connection((ip, port), timeout=timeout)
            return True
        except Exception as e:
            fd_log.warning(f"[AId] AIc connectivity check failed for {ip}:{port} - {e}")
            return False
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    # Legacy property loader (for backward compatibility)
    def load_property(self, file):
        try:
            with open(file, 'r', encoding='utf-8') as f:
                self.property_data = json.load(f)
        except Exception as e:
            fd_log.error(f"exception while load_property(): {e}")
            return False
        return True
    # MTd → AId : after get message, message received and put into queue
    def put_data(self, data):
        try:
            fd_log.info(f"[AId] << Incoming request (from MTd/4DOMS): {data}")
        except:
            pass
        with self._lock:
            self.msg_queue.put(data)
    # Message processing worker
    def status_task(self):
        fd_log.info("🟢 [AId] Message Receive Start")
        while not self.end:
            msg = None
            with self._lock:
                if not self.msg_queue.empty():
                    msg = self.msg_queue.get(block=False)
            if msg is not None:
                self.classify_msg(msg)
            time.sleep(0.01)
        fd_log.info("🔴 [AId] Message Receive End")
        # 정확히 size 만큼 수신하는 함수
    # read exact size from socket
    def _recv_exact(self, sock, size):
        buf = b''
        while len(buf) < size:
            chunk = sock.recv(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf
    # AIc recv loop
    def _aic_recv_loop(self, sess, ip):
        sock = sess.sock
        try:
            while not self.end:
                # --- 정확하게 5바이트 읽기 ---
                header = b""
                while len(header) < 5:
                    chunk = sock.recv(5 - len(header))
                    if not chunk:
                        time.sleep(0.01)
                        break
                    header += chunk
                if len(header) < 5:
                    continue
                body_len, flag = struct.unpack("<IB", header)
                # --- 정확하게 body_len 만큼 읽기 ---
                body = b""
                while len(body) < body_len:
                    chunk = sock.recv(body_len - len(body))
                    if not chunk:
                        time.sleep(0.01)
                        break
                    body += chunk
                if len(body) < body_len:
                    continue
                text = body.decode("utf-8", errors="ignore")
                self.on_aic_msg(text, ip)
        except Exception as e:
            fd_log.error(f"[AId] AIC recv loop error for {ip}: {e}")
        fd_log.warning(f"[AId] AIC connection closed for {ip}")
        if ip in self.aic_sessions:
            del self.aic_sessions[ip]

    # ─────────────────────────────────────────────────────────────────────────
    # AIc Utility Functions
    # ─────────────────────────────────────────────────────────────────────────
    # AIc message callback
    def on_aic_msg(self, text: str, src_ip: str) -> None:
        """
        TCPClient(message_callback)에 연결되는 콜백.
        AIc 에서 들어오는 Version 응답 등을 수집한다.
        """
        # 안정화: 빈 패킷/노이즈 패킷 무시
        if not text or not text.strip():
            fd_log.warning(f"[AId] empty/blank packet from {src_ip}")
            return
        # 안정화: JSON 파싱 실패하더라도 세션을 죽이지 않고 skip
        try:
            data = json.loads(text)
        except Exception as e:
            fd_log.warning(
                f"[AId] JSON parse error from {src_ip}: {e}; raw={text!r}"
            )
            return
        
        fd_log.info(f"[AId] << From AIc({src_ip}) request: {text}")

        sec1 = data.get("Section1")
        sec2 = data.get("Section2")
        sec3 = data.get("Section3")
        state = str(data.get("SendState", "")).lower()

        if (sec1, sec2, sec3) == ("Daemon", "Information", "Version") and state == "response":
            ver_map = data.get("Version", {})
            aic_info = ver_map.get("AIc", {})

            name = self.aic_ip_name_map.get(src_ip, src_ip)
            key_ip = src_ip
            self.aic_version_cache[key_ip] = {
                "name": name,
                "ip": src_ip,
                "version": aic_info.get("version", ""),
                "date": aic_info.get("date", ""),
            }
            fd_log.info(f"[AId] <- AIc Version from {src_ip}: {self.aic_version_cache[src_ip]}")
        else:
            # 현재는 Version 응답만 수집. 필요하면 여기서 추가 분기 가능.
            fd_log.debug(f"[AId] on_aic_msg: ignore msg from {src_ip} : {data}")
    # Get list of currently connected AIc IPs
    def _get_target_aic_list(self):
        """현재 연결된 AIc IP 목록 반환"""
        try:
            ips = list(self.aic_sessions.keys())
            fd_log.info(f"[AId] _get_target_aic_list → {ips}")
            return ips
        except Exception as e:
            fd_log.error(f"[AId] _get_target_aic_list failed: {e}")
            return []
    # Send message to a specific AIc
    def _send_to_aic(self, ip: str, packet: dict) -> bool:
        """AIc 하나에게 안전하게 메시지 전송"""
        try:
            sess = self.aic_sessions.get(ip)
            if not sess:
                fd_log.error(f"[AId] No AIc session for {ip}")
                return False

            text = json.dumps(packet)
            fd_log.info(f"[AId] >> To AIc({ip}) {text}")
            sess.send_msg(text)
            return True

        except Exception as e:
            fd_log.error(f"[AId] send_to_aic({ip}) failed: {e}")
            return False
    # Ensure AIc TCPClient session
    def _ensure_aic_session(self, ip: str):
        """Ensure there is a TCPClient session for the given AIc IP."""
        ip = str(ip)
        if ip in self.aic_sessions:
            return self.aic_sessions[ip]

        try:
            sess = TCPClient(name=f"AId→AIc:{ip}")
            ok = sess.connect(
                ip,
                conf._aic_daemon_port,
                callback=lambda text, _ip=ip: self.on_aic_msg(text, _ip)
            )
            if ok:
                self.aic_sessions[ip] = sess
                
            fd_log.info(f"[AId] Connected persistent session → AIc ({ip})")
            return sess
        except Exception as e:
            fd_log.error(f"[AId] failed to connect {ip}: {e}")
            return None
    # Broadcast message to multiple AIcs
    def _broadcast_to_aic(self, packet: dict, only_ips=None):
        if only_ips is None:
            only_ips = list(self.aic_sessions.keys())

        for ip in only_ips:
            try:
                text = json.dumps(packet)
                sess = self.aic_sessions.get(ip)
                if not sess:
                    continue
                sess.send_msg(text)
            except Exception as e:
                fd_log.error(f"[AId] send to {ip} failed: {e}")
    
    # ─────────────────────────────────────────────────────────────────────────
    # 📦 Message Routing
    # ─────────────────────────────────────────────────────────────────────────
    # Message handler
    def on_msg(self, text: str):
        try:
            data = json.loads(text)
        except Exception as e:
            fd_log.error(f"[{self.name}] on_msg JSON parse error: {e}; text={text[:256]}")
            return
        self.put_data(data)  # 큐에 넣고 worker가 처리
    # 🎯 command processing
    def classify_msg(self, msg: dict) -> None:
        try:
            fd_log.info(f"[AId] << classify_msg REQ: {msg}")
        except:
            pass
        _4dmsg = FDMsg()
        _4dmsg.assign(msg)
        
        # From 필드 보정
        if len(_4dmsg.data.get('From', '').strip()) == 0:
            _4dmsg.data.update(From='4DOMS')
        
        if _4dmsg.is_valid():
            result_code, err_msg = 1000, ''
            conf._result_code = 0
            if (state := _4dmsg.get('SendState').lower()) == FDMsg.REQUEST:
                sec1, sec2, sec3 = _4dmsg.get('Section1'), _4dmsg.get('Section2'), _4dmsg.get('Section3')
                action = _4dmsg.get('Action', '').lower()            
                match sec1, sec2, sec3:
                    # ──────────────────────────────────────────────────────
                    # 📦 V5 : [AIc], [connect]
                    # ──────────────────────────────────────────────────────                    
                    case 'AIc', 'connect', _:
                    # AIc Connect Request
                        aic_list = _4dmsg.get('AIcList', {})
                        if not isinstance(aic_list, dict):
                            fd_log.warning(f"[AId] invalid AIcList type: {type(aic_list)}")
                            aic_list = {}

                        result_map = {}
                        all_ok = True
                        for name, ip in aic_list.items():
                            status = "OK" if self.check_aic_connectivity(str(ip)) else "FAIL"
                            if status != "OK":
                                all_ok = False
                            result_map[str(name)] = {
                                "IP": str(ip),
                                "Status": status,
                            }
                        
                        # MTd에서 내려준 AIc 이름/IP 저장
                        self.aic_name_ip_map = dict(aic_list)
                        self.aic_ip_name_map = {str(ip): str(name) for name, ip in aic_list.items()}
                        fd_log.info(f"[AId] Save AIc list: {self.aic_name_ip_map}")
                        # 각 AIc 와 persistent 연결 생성 (포트 19738)
                        for name, ip in aic_list.items():
                            ip = str(ip)
                            if ip in self.aic_sessions:
                                continue
                            try:
                                sess = TCPClient()
                                sess.connect(ip, conf._aic_daemon_port)
                                sess.set_callback(lambda text, _ip=ip: self.on_aic_msg(text, _ip))
                                sess.start_recv()   # ★★★ 반드시 필요 ★★★
                                self.aic_sessions[ip] = sess
                                fd_log.info(f"[AId] Connected persistent session → AIc {name} ({ip})")
                            except Exception as e:
                                fd_log.error(f"[AId] AIc connect failed {ip}: {e}")

                        # request type:
                        # "AIcList": {
                        #   "AI Client [#1]": {"IP": "...", "Status": "OK"},
                        #   ...
                        # }

                        _4dmsg.update(AIcList=result_map)
                        # 모든 AIc가 OK일 때만 성공 코드 유지, 아니면 에러 코드로 교체
                        if not all_ok:
                            result_code = 1100                    
                    # ──────────────────────────────────────────────────────
                    # 📦 V5 : [Daemon], [Information], [Version]
                    # ──────────────────────────────────────────────────────                    
                    case 'Daemon', 'Information', 'Version':
                    # Version Request
                        with self._lock:
                            self.mtd_version_request(_4dmsg.data)
                        return
                    # ──────────────────────────────────────────────────────
                    # 📦 V5 : [Daemon], [Operation], [Prepare]
                    # ──────────────────────────────────────────────────────                    
                    case 'Daemon', 'Operation', 'Prepare':
                    # Production Preparing
                        with self._lock:
                            self.production_preparing(_4dmsg.data)
                        return
                    # ──────────────────────────────────────────────────────
                    # 📦 V5 : [Daemon], [Operation], [Production], / action:[start/stop]
                    # ──────────────────────────────────────────────────────                    
                    case 'Daemon', 'Operation', 'Production':
                    # Production Start/Stop
                        with self._lock:
                            if action == 'start':
                                self.production_start(_4dmsg.data)   
                            elif action == 'stop':
                                self.production_stop(_4dmsg.data)
                        return
                    # ──────────────────────────────────────────────────────
                    # 📦 V4 : [Daemon], [Operation], [Calibration]
                    # ──────────────────────────────────────────────────────                    
                    case 'AI', 'Operation', 'Calibration':
                        with self._lock:
                            cal = Calibration.from_file(_4dmsg.get('cal_path'))
                            _ = cal.to_dict()
                        fd_log.info("set calibration")
                        
                    case 'AI', 'Process', 'Multi':
                        fd_log.info("Start Calibration Multi-ch video")
                        with self._lock:
                            conf._type_target = conf._type_process_calibration_each
                            _ = self.create_ai_calibration_multi(
                            _4dmsg.get("Cameras", []),
                            _4dmsg.get("Markers", []),
                            _4dmsg.get("AdjustData", []),
                            _4dmsg.get('prefix'),
                            _4dmsg.get('output_path'),
                            _4dmsg.get('logo_path'),
                            _4dmsg.get('resolution'),
                            _4dmsg.get('codec'),
                            _4dmsg.get('fps'),
                            _4dmsg.get('bitrate'),
                            _4dmsg.get('gop'),
                            _4dmsg.get('output_mode'),
                        )
                        
                    case 'AI', 'Process', 'UserStart':
                        fd_log.info("[Creating Clip] Set start time")
                        with self._lock:
                            conf._create_file_time_start = time.time()
                        conf._team_info = _4dmsg.get('info')
                    
                    case 'AI', 'Process', 'UserEnd':
                        fd_log.info("[Creating Clip] Set end time and creating clip")
                        with self._lock:
                            conf._create_file_time_end = time.time()
                            play_and_create_multi_clips()
                    
                    
                # 공통 응답 정리
                _4dmsg.update(ResultCode=result_code)
                _4dmsg.update(ErrorMsg=err_msg)
                _4dmsg.toggle_status()  # REQUEST → RESPONSE

                if not self.app_server:
                    fd_log.warning("[AId] classify_msg: app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])

            elif state == FDMsg.RESPONSE:
                # AIc → AId : Version 응답 수신 처리
                sender_ip = msg.get("SenderIP")
                if sender_ip:
                    self._aic_version_cache[sender_ip] = msg
                pass  # PD 응답 수신 시 기본 처리 없음

        else:
            # 유효하지 않은 메시지 → 에러 응답 시도
            fd_log.error(f'[AId] message parsing error..\nMessage:\n{msg}')
            conf._result_code += 100
            _4dmsg.update(Section1="AI", Section2="Process", Section3="Multi",
                          From="4DPD", To="AId", ResultCode=conf._result_code,
                          ErrorMsg=err_msg)
            _4dmsg.toggle_status()
            if conf._result_code > 100:
                conf._result_code = 0
                if not self.app_server:
                    fd_log.warning("[AId] classify_msg(error path): app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])
    
    # ─────────────────────────────────────────────────────────────────────────
    # 🧩 Functions for Events (V5)
    # ─────────────────────────────────────────────────────────────────────────
    # Get Version Request
    def mtd_version_request(self, pkt: dict) -> None:
        """
        4DOMS(MTd) → AId : Handles Version request.
        - Collects AId's own version (conf._version, conf._release_date)
        - Collects versions of all AIc daemons (based on AIcList / Expect.AIc)
        - Sends the aggregated response back to 4DOMS.
        """
        token = pkt.get("Token")
        dmpdip = pkt.get("DMPDIP")
        # AId version/date are set in main via conf._version and conf._release_date
        aid_info = {
            "version": self.version,
            "date": self.release_date,
        }        
        expect = pkt.get("Expect", {}) or {}
        expect_ips = expect.get("AIc", []) or []
        # --------------------------------------------------------
        # If Expect.AIc is empty (not provided by MTd), fallback to current AIc list known by AId
        # --------------------------------------------------------
        if not expect_ips:
            expect_ips = list(self.aic_ip_name_map.keys())
            fd_log.warning("[AId] Using fallback AIc list from mapping")
        wait_sec = int(expect.get("wait_sec", 5) or 5)
        # AIc Version 수집
        aic_versions = self.request_aic_versions(expect_ips, dmpdip, token, wait_sec)
        # send response
        resp = {
            "Section1": "Daemon",
            "Section2": "Information",
            "Section3": "Version",
            "SendState": "response",
            "From": "AId",
            "To": pkt.get("From", "4DOMS"),
            "Token": token,
            "Action": "set",
            "ResultCode": 1000,
            "ErrorMsg": "",
            "Version": {
                "AId": aid_info,
                "AIc": aic_versions,
            },
        }
        if self.app_server:
            self.app_server.send_msg(json.dumps(resp))
        else:
            fd_log.error("[AId] app_server is None, cannot send Version response")
    # Request AIc versions
    def request_aic_versions(self, expect_ips, dmpdip, token, wait_sec=5):
        expect_ips = [str(ip) for ip in expect_ips]

        # 초기화
        for ip in expect_ips:
            self.aic_version_cache.pop(ip, None)

        # 세션 확보
        for ip in expect_ips:
            self._ensure_aic_session(ip)

        # 요청 패킷
        def build_packet():
            return {
                "Section1": "AIc",
                "Section2": "Information",
                "Section3": "Version",
                "SendState": "request",
                "From": "AId",
                "To": "AIc",
                "Token": token,
                "Action": "get",
                "DMPDIP": dmpdip,
            }

        # 요청 전송
        for ip in expect_ips:
            sess = self.aic_sessions.get(ip)
            if not sess:
                continue
            sess.send_msg(json.dumps(build_packet()))

        # 응답 수집
        pending = {ip: None for ip in expect_ips}
        deadline = time.time() + wait_sec

        while time.time() < deadline:
            for ip in expect_ips:
                if ip in self.aic_version_cache and pending[ip] is None:
                    pending[ip] = self.aic_version_cache[ip]
            if all(pending[ip] is not None for ip in expect_ips):
                break
            time.sleep(0.05)

        # 결과 구성
        results = []
        for ip in expect_ips:
            info = pending[ip]
            if not info:
                fd_log.warning(f"[AId] No Version response from AIc({ip})")
                continue
            results.append({
                "name": self.aic_ip_name_map.get(ip, ip),
                "ip": ip,
                "version": info.get("version"),
                "date": info.get("date"),
            })

        return results
    # Production Prepare
    def production_preparing(self, pkt: dict) -> None:
        fd_log.info("🚀 AI:Daemon:Operation:Prepare")        
        
        # 입력받은 파라미터 정리
        camera_env = pkt.get("camera_env")
        adjust_info = pkt.get("adjust_info") or pkt.get("adjust-info")  # 둘 다 지원

        # 처리 실행
        fd_log.info("⏸️ [AId] Production Environment Setup begin..")
        self.prod_camera_env    = camera_env
        self.prod_adjust_info   = adjust_info           

        # 🔥 Prepare 응답 패킷 생성
        resp = {
            "Section1": "Daemon",
            "Section2": "Operation",
            "Section3": "Prepare",
            "SendState": "response",
            "From": "AId",
            "To": pkt.get("From", "4DOMS"),
            "Action": "set",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }

        fd_log.info(f"📨 AId → OMS Prepare Response: {resp}")
        # send response
        if self.app_server:
            self.app_server.send_msg(json.dumps(resp))
        else:
            fd_log.error("[AId] app_server is None, cannot send Prepare response")

        # --------------------------------------------------------------
        #  🔥 Version 처리 방식과 완전히 동일하게 Prepare broadcast
        # --------------------------------------------------------------
        try:
            target_ips = self._get_target_aic_list()
            fd_log.info(f"[AId] Broadcasting Prepare to AIc (individual payload): {target_ips}")
            # convert pread info
            camera_list = camera_env["cameras"]
            camera_fps = camera_env["camera-fps"]
            camera_resolution = camera_env["camera-resolution"]
            folder = camera_env["record-folder"]

            camera_info = fd_convert_AIc_info({
                "cameras": camera_list,
                "camera-fps": camera_fps,
                "camera-resolution": camera_resolution,
                "record-folder": folder
            })
            for ip in target_ips:
                try:                    
                    # 🔥 IP별 payload 생성
                    per_ip_payload = fd_create_payload_for_preparing_to_AIc(
                        ip, camera_info, adjust_info
                    )
                    # 기본 공통 헤더 추가
                    pkt_to_aic = {
                        "Section1": "AIc",
                        "Section2": "Operation",
                        "Section3": "Prepare",
                        "SendState": "request",
                        "From": "AId",
                        "To": "AIc",
                        "Action": "set",
                        "Token": pkt.get("Token", ""),
                        "DMPDIP": pkt.get("DMPDIP"),
                        # IP별로 변경된 payload 삽입
                        "CamInfo": per_ip_payload
                    }

                    # 🔥 IP별 세션 가져오기
                    sess = self.aic_sessions.get(ip)
                    if not sess:
                        fd_log.warning(f"[AId] No active session for AIC {ip}")
                        continue
                    # 전송
                    text = json.dumps(pkt_to_aic)
                    sess.send_msg(text)
                    fd_log.info(f"[AId] Sent Prepare to AIC {ip}")
                except Exception as e:
                    fd_log.error(f"[AId] Send Prepare to {ip} failed: {e}")

        except Exception as e:
            fd_log.error(f"[AId] Prepare broadcast error: {e}")
    # Production Start
    def production_start(self, pkt: dict) -> None:
        fd_log.info("🚀 AI:Daemon:Operation:Production")             
        fd_log.info(f"{pkt}")
        # 입력받은 파라미터 정리
        product_info = pkt.get("product_info")
        
        # 🔥 Prepare 응답 패킷 생성
        resp = {
            "Section1": "Daemon",
            "Section2": "Operation",
            "Section3": "Production",
            "SendState": "response",
            "From": "AId",
            "To": pkt.get("From", "4DOMS"),
            "Action": "start",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }

        fd_log.info(f"📨 AId → OMS Prepare Response: {resp}")
        # send response
        if self.app_server:
            self.app_server.send_msg(json.dumps(resp))
        else:
            fd_log.error("[AId] app_server is None, cannot send Prepare response")


        # --------------------------------------------------------------
        #  🔥 Product Broadcast
        # --------------------------------------------------------------
        try:
            # get payload for AIc
            aic_payload = fd_create_payload_for_product_to_AIc(product_info, self.camera_fps)

            # create output folder
            output_folder = product_info["product-save-path"]
            os.makedirs(output_folder, exist_ok=True)

            # broadcast to AIc
            target_ips = self._get_target_aic_list()
            fd_log.info(f"Production to AIc: {target_ips}:{aic_payload}")            

            pkt_to_aic = {
                "Section1": "AIc",
                "Section2": "Operation",
                "Section3": "Production",
                "SendState": "request",
                "From": "AId",
                "To": "AIc",
                "Action": "start",
                "Token": pkt.get("Token", ""),
                "DMPDIP": pkt.get("DMPDIP"),
                # Prepare Data (MTd → AId 그대로)
                "product_info": aic_payload
            }
            # Version broadcast 방식과 동일
            self._broadcast_to_aic(pkt_to_aic, target_ips)
        except Exception as e:
            fd_log.error(f"[AId] AIc Prepare broadcast failed: {e}")
    # Production Stop
    def production_stop(self, pkt: dict) -> None:
        fd_log.info("🛑 AI:Daemon:Operation:Production:Stop")
        fd_log.info(f"{pkt}")
        
        # 🔥 Prepare 응답 패킷 생성
        resp = {
            "Section1": "Daemon",
            "Section2": "Operation",
            "Section3": "Production",
            "SendState": "response",
            "From": "AId",
            "To": pkt.get("From", "4DOMS"),
            "Action": "stop",
            "Token": pkt.get("Token", ""),
            "ResultCode": 1000,
            "ErrorMsg": ""
        }

        fd_log.info(f"📨 AId → OMS Prepare Response: {resp}")
        # send response
        if self.app_server:
            self.app_server.send_msg(json.dumps(resp))
        else:
            fd_log.error("[AId] app_server is None, cannot send Prepare response")

        # --------------------------------------------------------------
        #  🔥 Production Stop Broadcast
        # --------------------------------------------------------------
        try:
            target_ips = self._get_target_aic_list()
            fd_log.info(f"[AId] Product Stop to AIc: {target_ips}")

            pkt_to_aic = {
                "Section1": "AIc",
                "Section2": "Operation",
                "Section3": "Production",
                "SendState": "request",
                "From": "AId",
                "To": "AIc",
                "Action": "stop",
                "Token": pkt.get("Token", ""),
                "DMPDIP": pkt.get("DMPDIP"),        
            }
            # Version broadcast 방식과 동일
            self._broadcast_to_aic(pkt_to_aic, target_ips)
        except Exception as e:
            fd_log.error(f"[AId] AIc Product Stop failed: {e}")


    # ─────────────────────────────────────────────────────────────────────────
    # Functions for Events (V4)
    # ─────────────────────────────────────────────────────────────────────────
    def create_ai_calibration_multi(self, Cameras, Markers, AdjustData, prefix,
                                    output_path, logo_path, resolution, codec,
                                    fps, bitrate, gop, output_mode):
        result = False
        try:
            fd_log.info("⏸️ [AId] Calibration Multi channel clips begin..")
            result = fd_multi_calibration_video(
                Cameras, Markers, AdjustData, prefix, output_path, logo_path,
                resolution, codec, fps, bitrate, gop, output_mode
            )
            if result is True:
                fd_log.info("✅ [AId] create_ai_calibration_multi End..")
            else:
                fd_log.error("❌ [AId] create_ai_calibration_multi End.. ")
            return result
        finally:
            fd_log.info("⏯️ [AId] Finish Calibration Multi channel clips")
    
    
if __name__ == '__main__':
    # 작업 디렉터리: 프로젝트 루트
    base_path = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_path, ".."))
    os.chdir(project_root)
    conf._path_base = os.getcwd()

    fd_log.info("─────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"📂 [AId] Working directory: {conf._path_base}")

    # 1) get version from markdown
    release_md_path = os.path.join(conf._path_base, "AId", "aid_release.md")
    ver, _ = conf.read_latest_release_from_md(release_md_path)

    # 2) get last modified time of aid_release.md as release date
    try:
        stat = os.stat(release_md_path)
        dt = datetime.fromtimestamp(stat.st_mtime)
        # Example: "Nov 11 2025 - 16:13:33"
        date = dt.strftime("%b %d %Y - %H:%M:%S")
    except Exception as e:
        # Fallback when something goes wrong
        fd_log.warning(f"[AId] failed to read release file mtime: {e}")
        date = ""

    conf._version = ver
    conf._release_date = date
    

    fd_log.info(f"🧩 Latest Version: {conf._version}")
    fd_log.info(f"📅 Latest Date: {conf._release_date}")
    fd_log.info("─────────────────────────────────────────────────────────────────────────────")

    aid = AId()

    # 종료 훅
    def _graceful_shutdown(signame=""):
        try:
            fd_log.info(f"[AId] graceful shutdown ({signame})")
        except Exception:
            pass
        try:
            aid.stop()
        except Exception:
            pass
        os._exit(0)

    try:
        signal.signal(signal.SIGINT,  lambda *_: _graceful_shutdown("SIGINT"))
        signal.signal(signal.SIGTERM, lambda *_: _graceful_shutdown("SIGTERM"))
    except Exception:
        pass
    atexit.register(lambda: aid.stop())

    # 준비
    if not aid.init_sys():
        fd_log.error("init_sys() failed")
        sys.exit(1)

    if not aid.prepare():
        fd_log.error("prepare() failed")
        # 서비스 매니저가 “정상 종료”로 보게 하려면 0 반환:
        sys.exit(0)

    exit_code = 0
    try:
        aid.run()
    except KeyboardInterrupt:
        fd_log.warning("Interrupted by user (Ctrl+C).")
    except SystemExit as e:
        exit_code = int(getattr(e, "code", 1) or 1)
        fd_log.warning(f"SystemExit captured with code={exit_code}")
    except Exception as e:
        fd_log.error(f"Unhandled exception in run(): {e}")
        exit_code = 1

    # 서비스 모드에서는 메인 스레드를 블로킹해서 프로세스가 내려가지 않게 함
    if SERVICE_MODE:
        fd_log.info("[AId] SERVICE_MODE: blocking main thread")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass

    # 서비스 모드에선 입력 대기 금지
    if not SERVICE_MODE and get("_test_mode", True):
        while True:
            fd_log.info("─────────────────────────────────────────────────────────────────────────────")
            user_input = input("⌨ Key Press: \n")
            if not user_input:
                continue
            fd_log.info(f"[AId] Input received:{user_input}")

    if not SERVICE_MODE and get("_test_mode", True):
        aid.stop()
        sys.exit(exit_code)
