# ─────────────────────────────────────────────────────────────────────────────#
# aid_main.py
# - 2025/10/17 (revised)
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import os
import sys
import json
import queue
import time
import threading
import shutil
import signal
import atexit
from threading import Semaphore
from datetime import datetime

# ── service/env ──────────────────────────────────────────────────────────────
SERVICE_MODE = os.getenv("FD_SERVICE", "0") == "1"
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ["AID_DAEMON_NAME"] = r"AId"
os.environ.setdefault("FD_LOG_DIR", r"C:\4DReplay\V5\daemon\AId\log")

# ── sys.path ─────────────────────────────────────────────────────────────────
cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.insert(0, common_path)

# ── imports (project) ────────────────────────────────────────────────────────
from fd_common.msg               import FDMsg
from fd_common.tcp_server        import TCPServer
from fd_utils.fd_config_manager  import setup, conf, get
from fd_utils.fd_logging         import fd_log
from fd_utils.fd_file_edit       import fd_clean_up

from fd_aid                      import fd_create_analysis_file
from fd_aid                      import fd_multi_channel_video
from fd_aid                      import fd_multi_calibration_video

from fd_stream.fd_stream_rtsp    import StreamViewer
from fd_stabil.fd_stabil         import PostStabil
from fd_utils.fd_calibration     import Calibration
# 외부 유틸 함수들(기존 코드에서 사용): get_team_code_by_index, fd_pause_live_detect,
# fd_resume_live_detect, fd_live_buffering_thread, fd_rtsp_server_stop 등은
# 기존 모듈에 존재한다고 가정.

fd_log.propagate = False

# ── config path ──────────────────────────────────────────────────────────────
AID_CONFIG_PRIVATE = "./config/aid_config_private.json5"
AID_CONFIG_PUBLIC  = "./config/aid_config_public.json5"


class AId:
    name = 'AId'

    def __init__(self):
        self.name = "AId"
        self.property_data = None
        self.th = None
        self.app_server = None     # 외부로 응답 송신시 사용(없을 수 있음)
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

        # 생산 환경에서 breakpoint 비활성화
        if os.getenv("PYTHONBREAKPOINT") is None:
            os.environ["PYTHONBREAKPOINT"] = "0"
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # 설정 로딩 및 TCP 리스너 오픈
    # ─────────────────────────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────────────────────────
    # 구(舊) 속성 로더(호환 유지)
    # ─────────────────────────────────────────────────────────────────────────
    def load_property(self, file):
        try:
            with open(file, 'r', encoding='utf-8') as f:
                self.property_data = json.load(f)
        except Exception as e:
            fd_log.error(f"exception while load_property(): {e}")
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────
    # 메시지 큐 입력
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
    # 안전한 종료(멱등)
    # ─────────────────────────────────────────────────────────────────────────
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
    # 워커 스레드 루프
    # ─────────────────────────────────────────────────────────────────────────
    def status_task(self):
        fd_log.info("🟢 [AId] Message Receive Start")
        while not self.end:
            msg = None
            with self.lock:
                if not self.msg_queue.empty():
                    msg = self.msg_queue.get(block=False)
            if msg is not None:
                self.classify_msg(msg)
            time.sleep(0.01)
        fd_log.info("🔴 [AId] Message Receive End")

    # ─────────────────────────────────────────────────────────────────────────
    # 실행 시작
    # ─────────────────────────────────────────────────────────────────────────
    def run(self):
        fd_log.info("🟢 [AId] run() begin..")
        self.th = threading.Thread(target=self.status_task, daemon=True)
        self.th.start()

    # ─────────────────────────────────────────────────────────────────────────
    # 메시지 라우팅
    # ─────────────────────────────────────────────────────────────────────────
    def classify_msg(self, msg: dict) -> None:
        _4dmsg = FDMsg()
        _4dmsg.assign(msg)
        #_4dmsg.data.update(msg)  
        #_4dmsg.assign(json.dumps(msg))

        # From 필드 보정
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
                            AId.name: {'version': self.version, 'date': self.release_date}
                        })

                    case 'AI', 'Operation', 'Calibration':
                        conf._processing = True
                        cal = Calibration.from_file(_4dmsg.get('cal_path'))
                        _ = cal.to_dict()
                        fd_log.info("set calibration")
                        conf._processing = False

                    case 'AI', 'Operation', 'LiveEncoding':
                        conf._processing = True
                        fd_log.info("Start LiveEncoding")
                        conf._processing = False

                    case 'AI', 'Operation', 'PostStabil':
                        conf._processing = True
                        swipeperiod = _4dmsg.get('swipeperiod', [])
                        output_file = self.create_ai_poststabil(
                            _4dmsg.get('input'),
                            _4dmsg.get('output'),
                            _4dmsg.get('logo'),
                            _4dmsg.get('logopath'),
                            swipeperiod
                        )
                        conf._processing = False
                        self.on_stabil_done_event(output_file)

                    case 'AI', 'Operation', 'StartVideo':
                        fd_log.info("[Start Video Clip]")
                        playlist = _4dmsg.get('PlayList', [])
                        if playlist and isinstance(playlist, list):
                            item = playlist[0]
                            self.start_video(item.get('path'), item.get('name'))
                        else:
                            fd_log.error("No valid PlayList data found.")

                    case 'AI', 'Process', 'Multi':
                        fd_log.info("Start Calibration Multi-ch video")
                        conf._processing = True
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
                        conf._processing = False

                    case 'AI', 'Process', 'LiveDetect':
                        conf._processing = True
                        type_target = _4dmsg.get('type')
                        fd_log.info(f"[Streaming][0x{type_target:x}] Streaming Start")

                        match type_target:
                            case conf._type_live_batter_RH | conf._type_live_batter_LH | conf._type_live_pitcher | conf._type_live_hit:
                                result, output_file = self.create_ai_file(
                                    _4dmsg.get('type'),
                                    _4dmsg.get('input_path'),
                                    _4dmsg.get('output_path'),
                                    _4dmsg.get('ip_class'),
                                    _4dmsg.get('cam_front'),
                                    _4dmsg.get('start_time'),
                                    _4dmsg.get('end_time'),
                                    _4dmsg.get('fps'),
                                    _4dmsg.get('zoom_scale'),
                                    _4dmsg.get('select_time'),
                                    _4dmsg.get('select_frame')
                                )
                                if result:
                                    match _4dmsg.get('type'):
                                        case conf._type_baseball_pitcher:
                                            conf._baseball_db.insert_data(
                                                conf._recv_pitch_msg, conf._tracking_video_path, conf._tracking_data_path
                                            )
                                        case conf._type_baseball_hit | conf._type_baseball_hit_manual:
                                            conf._baseball_db.insert_data(
                                                conf._recv_hit_msg, conf._tracking_video_path, conf._tracking_data_path
                                            )
                                    duration = self.get_duration(output_file)
                                    _4dmsg.update(output=os.path.basename(output_file))
                                    _4dmsg.update(duration=duration)

                            case conf._type_live_nascar_1 | conf._type_live_nascar_2 | conf._type_live_nascar_3 | conf._type_live_nascar_4:
                                self.ai_live_buffering(
                                    _4dmsg.get('type'),
                                    _4dmsg.get('rtsp_url'),
                                    _4dmsg.get('output_path')
                                )

                            case _:
                                self.ai_live_detecting(
                                    _4dmsg.get('type'),
                                    _4dmsg.get('rtsp_url')
                                )
                        conf._processing = False

                    case 'AI', 'Process', 'UserStart':
                        fd_log.info("[Creating Clip] Set start time")
                        conf._create_file_time_start = time.time()
                        conf._team_info = _4dmsg.get('info')

                    case 'AI', 'Process', 'UserEnd':
                        fd_log.info("[Creating Clip] Set end time and creating clip")
                        conf._create_file_time_end = time.time()
                        play_and_create_multi_clips()

                    case 'AI', 'Process', 'LiveEnd':
                        fd_log.info("[Streaming][All] Streaming End")
                        fd_rtsp_server_stop()

                    case 'AI', 'Process', 'Merge':
                        conf._processing = True
                        conf._team_info = _4dmsg.get('info')
                        conf._make_time = _4dmsg.get('make_time')
                        fd_log.info("🚀 AI:Process:Merge")

                        result = True
                        if conf._live_creating_output_file:
                            conf._live_creating_output_file.join()

                        output_file = conf._final_output_file
                        if "TEMP_TIME" in output_file:
                            new_output_file = output_file.replace("TEMP_TIME", conf._make_time)
                            if os.path.exists(output_file):
                                shutil.move(output_file, new_output_file)
                                output_file = new_output_file
                                fd_log.info(f"✅ Renamed file: {output_file}")
                            else:
                                fd_log.error(f"❌ Original file not found: {output_file}")
                        else:
                            fd_log.error("⚠️ The filename does not contain the keyword 'TIME'. No replacement made.")

                        fd_log.info(f"🎯 AI:Process:Merge:{output_file}")
                        if result:
                            duration = self.get_duration(output_file)
                            _4dmsg.update(output=os.path.basename(output_file))
                            _4dmsg.update(duration=duration)
                        conf._processing = False

                    case 'AI', 'Process', 'Detect':
                        conf._processing = True

                        conf._pitcher_team = conf._live_pitcher_team if _4dmsg.get('pitcher_team') == -1 else _4dmsg.get('pitcher_team')
                        conf._pitcher_no   = conf._live_pitcher_no   if _4dmsg.get('pitcher_no')   == -1 else _4dmsg.get('pitcher_no')
                        conf._batter_team  = conf._live_batter_team  if _4dmsg.get('batter_team')  == -1 else _4dmsg.get('batter_team')
                        conf._batter_no    = conf._live_batter_no    if _4dmsg.get('batter_no')    == -1 else _4dmsg.get('batter_no')

                        conf._option         = _4dmsg.get('option')
                        conf._interval_delay = _4dmsg.get('interval_delay')
                        conf._multi_line_cnt = _4dmsg.get('multi_line_cnt')

                        match _4dmsg.get('type'):
                            case conf._type_baseball_batter_RH | conf._type_baseball_batter_LH:
                                conf._team_code = conf._batter_team
                                conf._player_no = conf._batter_no
                                target_attr = f"_team_box1_img{conf._pitcher_team}"
                                setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                                target_attr = f"_team_box1_img{conf._batter_team}"
                                setattr(conf, "_team_box_sub_img", getattr(conf, target_attr))
                                conf._pitcher_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._pitcher_team),
                                    season=datetime.now().year,
                                    backnum=conf._pitcher_no
                                )
                                conf._batter_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._batter_team),
                                    season=datetime.now().year,
                                    backnum=conf._batter_no
                                )

                            case conf._type_baseball_pitcher:
                                conf._team_code = conf._pitcher_team
                                conf._player_no = conf._pitcher_no
                                target_attr = f"_team_box2_img{conf._pitcher_team}"
                                setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                                conf._pitcher_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._pitcher_team),
                                    season=datetime.now().year,
                                    backnum=conf._pitcher_no
                                )

                            case conf._type_baseball_pitcher_multi:
                                conf._team_code = conf._pitcher_team
                                conf._player_no = conf._pitcher_no
                                target_attr = f"_team_box2_img{conf._pitcher_team}"
                                setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                                conf._pitcher_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._pitcher_team),
                                    season=datetime.now().year,
                                    backnum=conf._pitcher_no
                                )

                            case conf._type_baseball_hit | conf._type_baseball_hit_manual:
                                if conf._extra_homerun_derby:
                                    conf._live_player = False
                                conf._team_code = conf._batter_team
                                conf._player_no = conf._batter_no
                                target_attr = f"_team_box2_img{conf._pitcher_team}"
                                setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                                target_attr = f"_team_box2_img{conf._batter_team}"
                                setattr(conf, "_team_box_sub_img", getattr(conf, target_attr))
                                conf._pitcher_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._pitcher_team),
                                    season=datetime.now().year,
                                    backnum=conf._pitcher_no
                                )
                                conf._batter_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._batter_team),
                                    season=datetime.now().year,
                                    backnum=conf._batter_no
                                )

                            case conf._type_baseball_hit_multi:
                                if conf._extra_homerun_derby:
                                    conf._live_player = True
                                conf._team_code = conf._batter_team
                                conf._player_no = conf._batter_no
                                target_attr = f"_team_box2_img{conf._batter_team}"
                                setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                                conf._batter_player = conf._api_client.get_player_info_by_backnum(
                                    team_id=get_team_code_by_index(conf._batter_team),
                                    season=datetime.now().year,
                                    backnum=conf._batter_no
                                )

                            case _:
                                conf._team_code = 0
                                conf._player_no = 0

                        result, output_file = self.create_ai_file(
                            _4dmsg.get('type'),
                            _4dmsg.get('input_path'),
                            _4dmsg.get('output_path'),
                            _4dmsg.get('ip_class'),
                            _4dmsg.get('cam_front'),
                            _4dmsg.get('start_time'),
                            _4dmsg.get('end_time'),
                            _4dmsg.get('fps'),
                            _4dmsg.get('zoom_scale'),
                            _4dmsg.get('select_time'),
                            _4dmsg.get('select_frame')
                        )

                        if result:
                            match _4dmsg.get('type'):
                                case conf._type_baseball_pitcher:
                                    conf._baseball_db.insert_data(
                                        conf._recv_pitch_msg, conf._tracking_video_path, conf._tracking_data_path
                                    )
                                case conf._type_baseball_hit | conf._type_baseball_hit_manual:
                                    conf._baseball_db.insert_data(
                                        conf._recv_hit_msg, conf._tracking_video_path, conf._tracking_data_path
                                    )
                            duration = self.get_duration(output_file)
                            _4dmsg.update(output=os.path.basename(output_file))
                            _4dmsg.update(duration=duration)

                        conf._processing = False

                # 공통 응답 정리
                _4dmsg.update(ResultCode=result_code)
                _4dmsg.update(ErrorMsg=err_msg)
                _4dmsg.toggle_status()  # REQUEST → RESPONSE

                if not self.app_server:
                    fd_log.warning("[AId] classify_msg: app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])

            elif state == FDMsg.RESPONSE:
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
    # 기능들
    # ─────────────────────────────────────────────────────────────────────────
    def on_web_socket_event(self, pitch_data):
        msg = {
            "From": "AId",
            "To": "4DPD",
            "SendState": "Request",
            "Section1": "WebSocket",
            "Section2": "Realtime",
            "Section3": "Pitch",
            "Data": pitch_data
        }
        if not self.app_server:
            fd_log.warning("[AId] on_web_socket_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))

    def on_stabil_done_event(self, output_file):
        msg = {
            "From": "AId",
            "To": "4DPD",
            "SendState": "Request",
            "Section1": "StabilizeDone",
            "Section2": "",
            "Section3": "",
            "Complete": "OK",
            "Output": output_file
        }
        if not self.app_server:
            fd_log.warning("[AId] on_stabil_done_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))

    def create_ai_poststabil(self, input_file, output_file, logo, logopath, swipeperiod):
        fd_log.info("acreate_ai_poststabil begin")
        stabil = PostStabil()
        stabil.fd_poststabil(input_file, output_file, logo, logopath, swipeperiod)
        return output_file

    def start_video(self, file_path, file_name):
        path = f"{file_path}{file_name}"
        fd_log.info(f"Start Video {path}")
        if conf._live_player:
            conf._live_player_widget.load_video_to_buffer(path)

    def ai_live_player(self, type_target, folder_output, rtsp_url):
        fd_log.info(f"ai_live_player Thread begin.. rtsp url:{rtsp_url}")
        viewer = StreamViewer(buffer_size=600)
        conf._rtsp_viewers[rtsp_url] = viewer
        thread = threading.Thread(
            target=viewer.preview_rtsp_stream_pyav,
            kwargs={"rtsp_url": rtsp_url, "width": 640, "height": 360, "preview": True},
            daemon=True
        )
        thread.start()

    def get_frames_by_range(self, rtsp_url, target_start: int, target_end: int):
        viewer = conf._rtsp_viewers.get(rtsp_url)
        if not viewer:
            fd_log.error(f"해당 스트림을 찾을 수 없습니다: {rtsp_url}")
            return []
        frames_in_range = [frame for idx, frame in viewer.frame_buffer
                           if target_start <= idx <= target_end]
        if not frames_in_range:
            fd_log.warning(f"버퍼 내에 인덱스 범위 {target_start}~{target_end}에 해당하는 프레임이 없습니다.")
        return frames_in_range

    def create_ai_file(self, type_target, folder_input, folder_output,
                       camera_ip_class, camera_ip_list, start_time, end_time,
                       fps, zoom_ratio, select_time, select_frame=-1,
                       zoom_center_x=0, zoom_center_y=0):

        output_file = None
        try:
            fd_log.info("⏸️ [AId] Pausing live detector for making")
            fd_pause_live_detect()

            result = False
            if ((type_target & conf._type_mask_analysis) == conf._type_mask_analysis):
                fd_log.info(f"[AId] fd_create_analysis_file begin.. folder:{folder_input}, camera:{camera_ip_list}")
                result, output_file = fd_create_analysis_file(
                    type_target, folder_input, folder_output,
                    camera_ip_class, camera_ip_list,
                    start_time, end_time, select_time, select_frame,
                    fps, zoom_ratio
                )

            elif ((type_target & conf._type_mask_multi_ch) == conf._type_mask_multi_ch):
                fd_log.info(f"[AId] fd_multi_split_video begin.. folder:{folder_input}, camera list:{camera_ip_list}")
                result, output_file = fd_multi_channel_video(
                    type_target, folder_input, folder_output,
                    camera_ip_class, camera_ip_list,
                    start_time, end_time, select_time, select_frame,
                    fps, zoom_ratio, zoom_center_x, zoom_center_y
                )
            else:
                fd_log.info("❌ [AId] error.. unknown type:%s", type_target)
                result = False

            if result is True:
                fd_log.info(f"✅ [AId] create_ai_file End.. path:{output_file}")
            else:
                fd_log.info("❌ [AId] create_ai_file End.. ")

            return result, output_file
        finally:
            fd_log.info("⏯️ [AId] Resuming live detector after making")
            fd_resume_live_detect()

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

    def ai_live_buffering(self, type_target, rtsp_url, output_folder):
        fd_log.info(f"ai_live_buffering Thread begin.. rtsp url:{rtsp_url}")
        fd_live_buffering_thread(type_target, rtsp_url, output_folder)

    def ai_live_detecting(self, type_target, rtsp_url):
        fd_log.info(f"ai_live_detecting Thread begin.. rtsp url:{rtsp_url}")
        fd_live_detecting_thread(type_target, rtsp_url)

    # 필요시 구현되어 있던 헬퍼
    def get_duration(self, path: str) -> float:
        try:
            # 실제 구현은 프로젝트 공용 유틸을 쓰는 것이 맞습니다.
            # 여기선 방어적 기본값
            return 0.0
        except Exception:
            return 0.0


if __name__ == '__main__':
    # 작업 디렉터리: 프로젝트 루트
    base_path = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_path, ".."))
    os.chdir(project_root)
    conf._path_base = os.getcwd()

    fd_log.info("─────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"📂 [AId] Working directory: {conf._path_base}")

    ver, date = conf.read_latest_release_from_md(f"{conf._path_base}\\AId\\aid_release.md")
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
