# ─────────────────────────────────────────────────────────────────────────────#
# aid_main.py
# - 2025/10/17
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import os
import sys     
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import json
import queue
import time
import threading
import shutil
import argparse
import signal   # for service termination handling
import atexit   # for cleanup on exit

from threading import Semaphore
from pathlib import Path
from datetime import datetime
from ultralytics import YOLO

from fd_common.msg                      import FDMsg
from fd_common.tcp_server               import TCPServer

from fd_utils.fd_config_manager         import setup, conf
from fd_utils.fd_logging                import fd_log
from fd_utils.fd_file_edit              import fd_clean_up

from fd_aid                             import fd_create_analysis_file
from fd_aid                             import fd_multi_channel_video
from fd_aid                             import fd_multi_calibration_video

from fd_stream.fd_stream_rtsp           import StreamViewer
from fd_db.fd_db_manager                import BaseballDB
from fd_utils                           import fd_websocket_client
from fd_utils.fd_baseball_info          import BaseballAPI
from fd_utils.fd_websocket_client       import start_websocket 
from fd_stabil.fd_stabil                import PostStabil
from fd_utils.fd_calibration            import Calibration
from fd_detection.fd_live_buffer        import fd_rtsp_server_start, fd_rtsp_server_stop
from fd_detection.fd_live_buffer        import fd_rtsp_client_start, fd_rtsp_client_stop   
from fd_detection.fd_live_buffer        import fd_live_buffering_thread, fd_pause_live_detect, fd_resume_live_detect
from fd_detection.fd_live_detect_main   import fd_live_detecting_thread

from fd_sports.fd_sports_baseball_kbo   import get_team_code_by_index

from fd_gui.fd_gui_main                 import fd_start_gui_thread
from fd_manager.fd_create_clip          import play_and_create_multi_clips

cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.insert(0, common_path)
fd_log.propagate = False

# ─────────────────────────────────────────────────────────────────────────────
# Service-friendly defaults (로그 실시간/UTF-8, 입력루프 방지)
# ─────────────────────────────────────────────────────────────────────────────
# 위치: import 바로 아래 (전역)
os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
# 서비스 구동 시 DMS/서비스 관리자가 FD_SERVICE=1로 넘겨주면 입력 루프 차단
SERVICE_MODE = os.getenv("FD_SERVICE", "0") == "1"
if SERVICE_MODE:
    os.environ.setdefault("FD_TEST_MODE", "1")  # conf._test_mode 플래그와 연동됨
 


# ─────────────────────────────────────────────────────────────────────────────#
'''
AId: central application controller.
Responsibilities:
    - Bootstrap minimal runtime (logs, env) via init_sys()
    - Ingest property JSON and apply to global config (prepare())
    - Spin up a lightweight worker thread that processes inbound messages
    - Provide high-level operations (create files, live detect, callbacks)
    - Graceful shutdown and idempotent stop()
'''
# ─────────────────────────────────────────────────────────────────────────────#
    
AID_CONFIG_PRIVATE = "./config/aid_config_private.json5"
AID_CONFIG_PUBLIC  = "./config/aid_config_public.json5"

class AId:
    name = 'AId'

    # ─────────────────────────────────────────────────────────────────────────────#
    # Initialize once without re-reading config if already injected
    # ─────────────────────────────────────────────────────────────────────────────#
    def prepare(self, config_private_path: str = AID_CONFIG_PRIVATE, config_public_path: str = AID_CONFIG_PUBLIC) -> bool:        
        setup(
            config_private_path,
            config_public_path,
            runtime_factories={
                "_NVENC_START_SEM": lambda c: Semaphore(int(os.getenv("FD_NVENC_INIT_CONCURRENCY",  c._gpu_session_init_cnt))),
                "_NVENC_MAX_SEM":   lambda c: Semaphore(int(os.getenv("FD_NVENC_MAX_SLOTS",          c._gpu_session_max_cnt))),
            },            
        )

        fd_log.info(f"📄 [AId] Load Config - Private {config_private_path}")
        fd_log.info(f"📄 [AId] Load Config - Public {config_public_path}")        
        
        # Bind global lock to instance lock
        conf._lock = self.lock

        # Load images for baseball - KBO
        '''
        try:
            if not getattr(conf, "_images_loaded_once", False):
                loaded = load_images()
                # fd_log.info(f"[AId] images loaded: {loaded}")
                conf._images_loaded_once = True
        except Exception as e:
            fd_log.error(f"[AId] load_images() failed: {e}")
            return False
        '''
        
        # ready for communication
        self.app_server = TCPServer("", conf._daemon_port, self.put_data)
        self.app_server.open()

        return True
    
    # ─────────────────────────────────────────────────────────────────────────────#
    # Initialize lightweight state; no heavy I/O here
    # ─────────────────────────────────────────────────────────────────────────────#
    def __init__(self):
        self.property_data = None               # Raw JSON if loaded via load_property()
        self.th = None                          # Worker thread handle
        self.app_server = None                  # Outbound TCP server (set elsewhere)
        self.end = False                        # Loop termination flag for status_task()
        self.host = None                        # Placeholder; not used here
        self.msg_queue = queue.Queue()          # Inbound messages to be handled by worker
        self.lock = threading.Lock()            # Guard for shared state; connected to conf._lock in prepare()

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Create runtime folders and set process-wide toggles.
    - Ensures ./log(s) dir exists next to this file
    - Disables breakpoint invocation in production (PYTHONBREAKPOINT=0)
    Returns:
        True on success, False on failure (e.g., directory creation fails)
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def init_sys(self) -> bool:
        current_path = os.path.dirname(os.path.abspath(__file__))
        log_path = current_path + "/log"
        try:
            if not os.path.exists(log_path):
                fd_log.info("create the log directory.")
                os.makedirs(log_path)
        except OSError:
            fd_log.error("Failed to create the directory.")
            return False

        # Avoid debugger breakpoints in production runs
        if os.getenv("PYTHONBREAKPOINT") is None:
            os.environ["PYTHONBREAKPOINT"] = "0"
        return True

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Legacy property loader (kept for compatibility).
    Prefer using FDConfigManager.init_from_property in new code.
    Returns:
        True on success, False on exception.
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def load_property(self, file):
        try:
            with open(file, 'r') as f:
                self.property_data = json.load(f)  # mind json.load vs json.loads
        except Exception as e:
            fd_log.error(f"exception while load_property(): {e}")
            return False
        return True

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def put_data(self, data):
        with self.lock:
            self.msg_queue.put(data)

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Idempotent shutdown:
        - Marks the loop flag to break the worker thread
        - Attempts to close/shutdown the outbound app_server if present
        - Joins the worker thread with a short timeout
    Calling this multiple times is safe.
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def stop(self):
        fd_log.info("[AId] stop() begin..")

        # Make stop() idempotent
        if getattr(self, "_stopped", False):
            fd_log.info("[AId] stop() already called; skipping.")
            return
        self._stopped = True

        # Signal worker loop to end
        self.end = True

        # Gracefully close the app_server if any
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

        # Join worker if running
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

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Worker thread loop:
        - Blocks on queue.get(timeout=0.05) to reduce CPU usage (no busy-wait)
        - Dispatches messages to classify_msg()
        - Exits when self.end is set by stop()
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def status_task(self):
        fd_log.info(f"🟢 [AId] Message Receive Start")        
        msg = None
        while self.end == False:
            with self.lock:
                msg = self.msg_queue.get(block=False) if not self.msg_queue.empty() else None            
            if msg is not None:
                self.classify_msg(msg)
            time.sleep(0.01)
            continue
        fd_log.info(f"🔴[AId] Message Receive End")

    # ─────────────────────────────────────────────────────────────────────────────
    '''
    Start the worker thread.
    Note: This method returns immediately; it does not block the main thread.
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
    def run(self):
        fd_log.info("🟢 [AId] run() begin..")
        self.th = threading.Thread(target=self.status_task)  # consider daemon=True if suitable
        self.th.start()        

    # ─────────────────────────────────────────────────────────────────────────────
    '''
    Message router:
        - Validates and normalizes inbound message
        - Routes by (Section1, Section2, Section3) and/or `type` field
        - Performs operations (create files, live detect, stabilize, etc.)
        - Sends back a Response with ResultCode/ErrorMsg and payload deltas
    Notes:
        - Injects default 'From=4DPD' if missing (compat with legacy sender)
        - Maintains conf._processing guard where long-running operations happen
    '''
    # ─────────────────────────────────────────────────────────────────────────────
    def classify_msg(self, msg: dict) -> None:
        _4dmsg = FDMsg()
        _4dmsg.assign(msg)

        # Backward compat: some senders miss 'From' in STOP; normalize
        if len(_4dmsg.data.get('From', '').strip()) == 0:
            _4dmsg.data.update(From='4DPD')

        result_code, err_msg = 1000, ''
        if _4dmsg.is_valid():
            conf._result_code = 0
            if (state := _4dmsg.get('SendState').lower()) == FDMsg.REQUEST:
                sec1, sec2, sec3 = _4dmsg.get('Section1'), _4dmsg.get('Section2'), _4dmsg.get('Section3')

                match sec1, sec2, sec3:

                    # ─────────────────────────────────────────────────────────────────────────────
                    # Daemon → Info → Version
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'Daemon', 'Information', 'Version':
                        _4dmsg.update(Version={
                            AId.name: {
                                'version': conf._version,
                                'date': conf._release_date
                            }
                        })

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Operation → Calibration
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Operation', 'Calibration':
                        conf._processing = True
                        cal = Calibration.from_file(_4dmsg.get('cal_path'))
                        _ = cal.to_dict()
                        fd_log.info("set calibration")
                        conf._processing = False

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Operation → LiveEncoding
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Operation', 'LiveEncoding':
                        conf._processing = True
                        fd_log.info("Start LiveEncoding")
                        conf._processing = False

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Operation → PostStabil
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Operation', 'PostStabil':
                        conf._processing = True
                        swipeperiod = _4dmsg.get('swipeperiod', [])
                        output_file = aid.create_ai_poststabil(
                            _4dmsg.get('input'),
                            _4dmsg.get('output'),
                            _4dmsg.get('logo'),
                            _4dmsg.get('logopath'),
                            swipeperiod
                        )
                        conf._processing = False
                        # Notify PD
                        aid.on_stabil_done_event(output_file)

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Operation → StartVideo
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Operation', 'StartVideo':
                        fd_log.info("[Start Video Clip]")
                        playlist = _4dmsg.get('PlayList', [])
                        if playlist and isinstance(playlist, list):
                            item = playlist[0]  # Play first clip only
                            aid.start_video(item.get('path'), item.get('name'))
                        else:
                            fd_log.error("No valid PlayList data found.")

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → Multi (Calibration multi-channel)
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'Multi':
                        fd_log.info("Start Calibration Multi-ch video")
                        conf._processing = True
                        conf._type_target = conf._type_process_calibration_each
                        _ = aid.create_ai_calibration_multi(
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

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → LiveDetect (baseball/nascar live paths)
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'LiveDetect':
                        conf._processing = True
                        type_target = _4dmsg.get('type')
                        fd_log.info(f"[Streaming][0x{type_target:x}] Streaming Start")

                        match type_target:

                            # Baseball live make paths (batter/pitcher/hit)
                            case conf._type_live_batter_RH | conf._type_live_batter_LH | conf._type_live_pitcher | conf._type_live_hit:
                                result, output_file = aid.create_ai_file(
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

                            # NASCAR: multi-channel buffering
                            case conf._type_live_nascar_1 | conf._type_live_nascar_2 | conf._type_live_nascar_3 | conf._type_live_nascar_4:
                                aid.ai_live_buffering(
                                    _4dmsg.get('type'),
                                    _4dmsg.get('rtsp_url'),
                                    _4dmsg.get('output_path')
                                )

                            # Default: single-channel live detect
                            case _:
                                aid.ai_live_detecting(
                                    _4dmsg.get('type'),
                                    _4dmsg.get('rtsp_url')
                                )

                        conf._processing = False

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → UserStart (nascar clip marking)
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'UserStart':
                        fd_log.info("[Creating Clip] Set start time")
                        conf._create_file_time_start = time.time()
                        conf._team_info = _4dmsg.get('info')

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → UserEnd (nascar clip finalize)
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'UserEnd':
                        fd_log.info("[Creating Clip] Set end time and creating clip")
                        conf._create_file_time_end = time.time()
                        play_and_create_multi_clips()

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → LiveEnd
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'LiveEnd':
                        fd_log.info("[Streaming][All] Streaming End")
                        fd_rtsp_server_stop()

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → Merge (nascar merge result → reply with output)
                    # ─────────────────────────────────────────────────────────────────────────────
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

                    # ─────────────────────────────────────────────────────────────────────────────
                    # AI → Process → Detect (baseball clip make)
                    # ─────────────────────────────────────────────────────────────────────────────
                    case 'AI', 'Process', 'Detect':
                        conf._processing = True

                        # Fallback to live values if -1 is passed in request
                        conf._pitcher_team = conf._live_pitcher_team if _4dmsg.get('pitcher_team') == -1 else _4dmsg.get('pitcher_team')
                        conf._pitcher_no   = conf._live_pitcher_no   if _4dmsg.get('pitcher_no')   == -1 else _4dmsg.get('pitcher_no')
                        conf._batter_team  = conf._live_batter_team  if _4dmsg.get('batter_team')  == -1 else _4dmsg.get('batter_team')
                        conf._batter_no    = conf._live_batter_no    if _4dmsg.get('batter_no')    == -1 else _4dmsg.get('batter_no')

                        conf._option         = _4dmsg.get('option')
                        conf._interval_delay = _4dmsg.get('interval_delay')
                        conf._multi_line_cnt = _4dmsg.get('multi_line_cnt')

                        match _4dmsg.get('type'):

                            # Batter single
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

                            # Pitcher single
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

                            # Pitcher multi (placeholder)
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

                            # Hit single / manual
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

                            # Hit multi
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

                            # Default fallback
                            case _:
                                conf._team_code = 0
                                conf._player_no = 0

                        # Produce the clip
                        result, output_file = aid.create_ai_file(
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

                # Always finalize the message into a Response
                _4dmsg.update(ResultCode=result_code)
                _4dmsg.update(ErrorMsg=err_msg)
                _4dmsg.toggle_status()  # REQUEST → RESPONSE

                # Safety: app_server may be None during early boot/shutdown
                if not self.app_server:
                    fd_log.warning("[AId] classify_msg: app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])

            elif state == FDMsg.RESPONSE:
                # RESPONSE from PD → no action required by default
                pass

        else:
            # Invalid message → normalize into an error RESPONSE and try to send
            fd_log.error(f'[AId] message parsing error..\nMessage:\n{msg}')
            conf._result_code += 100
            _4dmsg.update(Section1="AI")
            _4dmsg.update(Section2="Process")
            _4dmsg.update(Section3="Multi")
            _4dmsg.update(From="4DPD")
            _4dmsg.update(To="AId")
            _4dmsg.update(ResultCode=conf._result_code)
            _4dmsg.update(ErrorMsg=err_msg)
            _4dmsg.toggle_status()

            if conf._result_code > 100:
                conf._result_code = 0
                if not self.app_server:
                    fd_log.warning("[AId] classify_msg(error path): app_server is None; skipping send.")
                else:
                    self.app_server.send_msg(_4dmsg.get_json()[1])


    # ─────────────────────────────────────────────────────────────────────────────#
    # Message and Function
    # ─────────────────────────────────────────────────────────────────────────────#
    
    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Outbound callback to notify PD process about a pitch event (WebSocket path).
    Safe-guards:
        - Skips send if app_server is not available (prevents AttributeError during early boot/shutdown)
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
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
        if not self.app_server:  # safety guard
            fd_log.warning("[AId] on_web_socket_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))

    # ─────────────────────────────────────────────────────────────────────────────#
    '''
    Outbound callback to notify PD process that a post-stabilization file is ready.
    Safe-guards:
        - Skips send if app_server is not available
    '''
    # ─────────────────────────────────────────────────────────────────────────────#
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
        if not self.app_server:  # safety guard
            fd_log.warning("[AId] on_stabil_done_event: app_server is None; skipping send.")
            return
        self.app_server.send_msg(json.dumps(msg))

    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def create_ai_poststabil(self, input_file, output_file, logo, logopath, swipeperiod):
        fd_log.info(f"acreate_ai_poststabil begin") 
        stabil = PostStabil()  
        stabil.fd_poststabil(input_file, output_file, logo, logopath, swipeperiod)
        return output_file
    
    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def start_video(self, file_path, file_name):        
        path = f"{file_path}{file_name}"
        fd_log.info(f"Start Video {path}")
        if(conf._live_player):
            conf._live_player_widget.load_video_to_buffer(path)           
        
    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def ai_live_player(self, type_target, folder_output, rtsp_url):        
        fd_log.info(f"ai_live_player Thread begin.. rtsp url:{rtsp_url}") 
        # 스레드 생성 및 실행
        viewer = StreamViewer(buffer_size=600)
        # 나중에 이 url로 구분해서 접근 가능
        conf._rtsp_viewers[rtsp_url] = viewer  
        thread = threading.Thread(
            target=viewer.preview_rtsp_stream_pyav,
            kwargs={"rtsp_url": rtsp_url, "width": 640, "height": 360, "preview": True},
            daemon=True
        )
        thread.start()
    
    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def get_frames_by_range(self, rtsp_url, target_start: int, target_end: int):
        viewer = conf._rtsp_viewers.get(rtsp_url)
        if not viewer:
            fd_log.error(f"해당 스트림을 찾을 수 없습니다: {rtsp_url}")
            return []

        frames_in_range = [
            frame for idx, frame in viewer.frame_buffer
            if target_start <= idx <= target_end
        ]

        if not frames_in_range:
            fd_log.warning(f"버퍼 내에 인덱스 범위 {target_start}~{target_end}에 해당하는 프레임이 없습니다.")

        return frames_in_range
    
    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def create_ai_file(self, type_target, folder_input, folder_output, camera_ip_class, camera_ip_list, start_time, end_time, fps, zoom_ratio, select_time, select_frame = -1, zoom_center_x = 0, zoom_center_y = 0):
        
        output_file = None        
        # =======================
        # PAUSE live detecting / RTSP
        # =======================
        try:
            fd_log.info("⏸️ [AId] Pausing live detector for making")
            fd_pause_live_detect()     # 🔴 감지 잠시 멈춤 (RTSP는 유지)

            # =======================
            # 기존 create_ai_file 본연의 작업 (메이킹)
            # =======================
            result = False

            # =======================
            # Analysis
            # =======================            
            if ((type_target & conf._type_mask_analysis) == conf._type_mask_analysis):
                fd_log.info(f"[AId] fd_create_analysis_file begin.. folder:{folder_input}, camera:{camera_ip_list}")
                result, output_file = fd_create_analysis_file(
                    type_target, folder_input, folder_output,
                    camera_ip_class, camera_ip_list,
                    start_time, end_time, select_time, select_frame,
                    fps, zoom_ratio
                )

            # =======================
            # Multi Channel
            # =======================            
            elif ((type_target & conf._type_mask_multi_ch) == conf._type_mask_multi_ch):
                fd_log.info(f"[AId] fd_multi_split_video begin.. folder:{folder_input}, camera list:{camera_ip_list}")
                result, output_file = fd_multi_channel_video(
                    type_target, folder_input, folder_output,
                    camera_ip_class, camera_ip_list,
                    start_time, end_time, select_time, select_frame,
                    fps, zoom_ratio, zoom_center_x, zoom_center_y
                )
            # =======================
            # Others
            # =======================   
            else:
                fd_log.info(f"❌ [AId] error.. unknown type:{type_target}")
                result = False

            if result is True:
                fd_log.info(f"✅ [AId] create_ai_file End.. path:{output_file}")
            else:
                fd_log.info(f"❌ [AId] create_ai_file End.. ")

            return result, output_file

        finally:
            # =======================
            # RESUME live detecting / RTSP
            # =======================
            fd_log.info("⏯️ [AId] Resuming live detector after making")
            fd_resume_live_detect()    # 🟢 감지 재개

    # ─────────────────────────────────────────────────────────────────────────────
    # def create_ai_calibration_multi (self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
    # [owner] hongsu jung
    # ─────────────────────────────────────────────────────────────────────────────
    def create_ai_calibration_multi(self, Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop, output_mode):        
        
        result = False
        try:
            fd_log.info("⏸️ [AId] Calibration Multi channel clips begin..")
            result = fd_multi_calibration_video(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop, output_mode)            
            if result is True:  
                fd_log.info(f"✅ [AId] create_ai_calibration_multi End..") 
            else:
                fd_log.error(f"❌ [AId] create_ai_calibration_multi End.. ")
            return result        
        
        finally:
            fd_log.info("⏯️ [AId] Finish Calibration Multi channel clips")

    # ─────────────────────────────────────────────────────────────────────────────
    # def ai_live_streaming
    # owner: hongsu jung
    # date: 2025-05-28
    # ─────────────────────────────────────────────────────────────────────────────    
    def ai_live_buffering(self, type_target, rtsp_url, output_folder):
        fd_log.info(f"ai_live_buffering Thread begin.. rtsp url:{rtsp_url}") 
        fd_live_buffering_thread(            
            type_target,        # type of target | batter-rh:1; batter-rh:2; pitcher:3; wide:4; golfer:2
            rtsp_url,           # rtsp url address
            output_folder)        # buffer size (sec)
        
    # ─────────────────────────────────────────────────────────────────────────────
    # def ai_live_detect
    # owner: hongsu jung
    # date: 2025-05-28
    # ─────────────────────────────────────────────────────────────────────────────    
    def ai_live_detecting(self, type_target, rtsp_url):
        fd_log.info(f"ai_live_detecting Thread begin.. rtsp url:{rtsp_url}") 
        fd_live_detecting_thread(            
            type_target,        # type of target | batter-rh:1; batter-rh:2; pitcher:3; wide:4; golfer:2
            rtsp_url)           # rtsp url address
    
    
if __name__ == '__main__':

    # ─────────────────────────────────────────────────────────────────────────────
    # Configuration bootstrap
    # ─────────────────────────────────────────────────────────────────────────────
    app_dashboard = None
    base_path = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(base_path, ".."))  # project root (parent of 'aid')
    
    # Set working directory to project root for consistent relative paths
    os.chdir(project_root)
    conf._path_base = os.getcwd()
    fd_log.info(f"─────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"📂 [AId] Working directory: {conf._path_base}")

    ver, date = conf.read_latest_release_from_md("./src/aid_release.md")
    conf._version = ver
    conf._release_date = date

    fd_log.info(f"🧩 Latest Version: {conf._version}")
    fd_log.info(f"📅 Latest Date: {conf._release_date}")
    fd_log.info(f"─────────────────────────────────────────────────────────────────────────────")
    
    # ─────────────────────────────────────────────────────────────────────────────
    # AId daemon start
    # ─────────────────────────────────────────────────────────────────────────────
    aid = AId()

    # ─────────────────────────────────────────────────────────────────────────
    # SIGNAL/ATEXIT 훅: 서비스 Stop/Restart 시 정상 종료 보장
    # 위치: aid 인스턴스 생성 직후
    # ─────────────────────────────────────────────────────────────────────────
    def _graceful_shutdown(signame=""):
        try:
            fd_log.info(f"[AId] graceful shutdown ({signame})")
        except Exception:
            pass
        try:
            aid.stop()
        except Exception:
            pass
        # sys.exit로 넘기면 일부 플랫폼에서 좀비 남을 수 있어 확실히 종료
        os._exit(0)

    try:
        signal.signal(signal.SIGINT,  lambda *_: _graceful_shutdown("SIGINT"))
        signal.signal(signal.SIGTERM, lambda *_: _graceful_shutdown("SIGTERM"))
    except Exception:
        # Windows 파이썬 환경 등에서 SIGTERM 미지원일 수 있음 → 무시
        pass
    atexit.register(lambda: aid.stop())

    # Prepare once (locks, image loading guard, etc.)
    if not aid.prepare():
        fd_log.error("prepare() failed")
        sys.exit(1)

    # Optional Trackman bootstrap (only for baseball)
    if getattr(conf, "_trackman_mode", False) and getattr(conf, "_type_target", None) == getattr(conf, "_type_target_baseball", -1):
        try:
            conf._api_client = BaseballAPI(conf._api_key)
            # Cache active team players for current season
            conf._api_client.cache_all_active_team_players(season=datetime.now().year)
        except Exception as e:
            fd_log.warning(f"[Trackman] API bootstrap skipped: {e}")

    # Optionally start websocket client before/after run depending on your threading model.
    # Here we start it BEFORE run() if needed so callbacks are ready.
    if getattr(conf, "_trackman_mode", False) and (not getattr(conf, "_test_mode", True)) and getattr(conf, "_type_target", None) == getattr(conf, "_type_target_baseball", -1):
        try:
            if "start_websocket" in globals():
                start_websocket()  # non-blocking thread expected
            else:
                fd_log.warning("start_websocket() not found; skipping websocket start.")
        except Exception as e:
            fd_log.warning(f"WebSocket start failed: {e}")

    # If websocket client exposes a thread handler, wire callbacks safely (optional)
    ws_thread = getattr(getattr(fd_websocket_client, "_websocket_thread", None), "ws_handler", None) if "fd_websocket_client" in globals() else None
    if ws_thread and hasattr(ws_thread, "set_on_pitch_callback"):
        try:
            ws_thread.set_on_pitch_callback(aid.on_web_socket_event)
        except Exception as e:
            fd_log.warning(f"Failed to set pitch callback: {e}")

    # 3) Main run loop with safe shutdown
    exit_code = 0


    # ──────────────────────────────────────────────────────
    # aid run
    # ──────────────────────────────────────────────────────
    try:
        aid.run()
    except KeyboardInterrupt:
        fd_log.warning("Interrupted by user (Ctrl+C).")
    except SystemExit as e:
        # If someone calls sys.exit() deeper, capture its code
        exit_code = int(getattr(e, "code", 1) or 1)
        fd_log.warning(f"SystemExit captured with code={exit_code}")
    except Exception as e:
        fd_log.error(f"Unhandled exception in run(): {e}")
        exit_code = 1

    
    if not conf._test_mode:
        fd_log.info(r"⏩ Press 'q' to quit the program.")
        parser = argparse.ArgumentParser(allow_abbrev=False)
        parser.add_argument('-t', nargs=1, type=float   , help='Specify a threshold value between 0.1 and 1.0')
        parser.add_argument('-d', nargs=1, type=int     , help='Specify a duration value between 100 and 3000')
        parser.add_argument('-i', nargs=1, type=int     , help='Specify a interval value between 500 and 5000')


    # ─────────────────────────────────────────────────────────────────────────────
    # Relase mode
    # waiting message
    # ─────────────────────────────────────────────────────────────────────────────
    if not conf._test_mode:
        while True:
            fd_log.info(f"─────────────────────────────────────────────────────────────────────────────")
            user_input = input("⌨ Key Press: \n")
            if len(user_input) == 0:
                    continue
            
            cmd, *args = user_input.split()
            # input key
            match cmd:
                #############################
                # batter                
                #############################
                case 'b':
                    conf._team_code = conf._batter_team
                    conf._player_no = conf._batter_no
                    target_attr = f"_team_box1_img{conf._pitcher_team}"
                    setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                    target_attr = f"_team_box1_img{conf._batter_team}"
                    setattr(conf, "_team_box_sub_img", getattr(conf, target_attr))                    
                    conf._pitcher_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._pitcher_team), season=datetime.now().year, backnum=conf._pitcher_no)
                    conf._batter_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._batter_team), season=datetime.now().year, backnum=conf._batter_no)                               
                    result, output_file = aid.create_ai_file (0x0111, 'D:/Project/v4_aid/videos/input/baseball/KBO/2025_05_07_18_15_35',    'D:/Project/v4_aid//videos/output/baseball',27,11,-2000,1500,30,100,3183,46)                                   
                    continue  
                #############################
                # change debug detection
                #############################
                case "c":
                        if conf._detection_viewer :
                            conf._detection_viewer = False
                            fd_log.info("Change Debug Detection OFF")
                        else:
                            conf._detection_viewer = True
                            fd_log.info("Change Debug Detection ON")
                #############################                
                # baseball
                #############################
                case 'd':
                    hits_data = conf._baseball_db.fetch_hits()
                    for row in hits_data:
                        fd_log.info("🎯 Hit Data:")
                        for key, value in row.items():
                            fd_log.info(f"  {key}: {value}")  
                    '''
                    pitches_data = conf._baseball_db.fetch_pitches()
                    for row in pitches_data:
                        fd_log.info("🎯 pitches Data:")
                        for key, value in row.items():
                            fd_log.info(f"  {key}: {value}")   
                    hits_raw_data = conf._baseball_db.fetch_raw_hits()
                    for row in hits_raw_data:
                        fd_log.info("🎯 Hit Raw Data:")
                        for key, value in row.items():
                            fd_log.info(f"  {key}: {value}")  
                            
                    pitches_raw_data = conf._baseball_db.fetch_raw_pitches()
                    for row in pitches_raw_data:
                        fd_log.info("🎯 pitches Raw Data:")
                        for key, value in row.items():
                            fd_log.info(f"  {key}: {value}") 
                    '''
                    continue
                #############################
                # home run
                #############################                
                case 'h':
                    conf._team_code = conf._batter_team
                    conf._player_no = conf._batter_no
                    target_attr = f"_team_box2_img{conf._pitcher_team}"
                    setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                    target_attr = f"_team_box2_img{conf._batter_team}"
                    setattr(conf, "_team_box_sub_img", getattr(conf, target_attr))

                    conf._pitcher_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._pitcher_team), season=datetime.now().year, backnum=conf._pitcher_no)
                    conf._batter_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._batter_team), season=datetime.now().year, backnum=conf._batter_no)     

                    result, output_file = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,6000,30,100,2443,37)
                    continue   
                #############################
                # pitching
                #############################
                case 'p':   
                    conf._team_code = conf._pitcher_team
                    conf._player_no = conf._pitcher_no                 
                    target_attr = f"_team_box2_img{conf._pitcher_team}"
                    setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
                    conf._pitcher_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._pitcher_team), season=datetime.now().year, backnum=conf._pitcher_no)

                    result, output_file = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,13,-1500,4000,30,100,1141,39)        
                    continue
                #############################
                # baseball 
                #############################
                case 't':         
                    conf._baseball_db.count_hits()
                    conf._baseball_db.count_pitches()
                    conf._baseball_db.count_raw_hits()
                    conf._baseball_db.count_raw_pitches()
                    continue
                #############################
                # quit
                #############################
                case 'q':
                    fd_log.info("Exiting the program.")
                    break     
                case _:
                    fd_log.info(f"Input received:{user_input}")

    # ─────────────────────────────────────────────────────────────────────────────##########
    # Debug mode
    # non waiting message
    # ─────────────────────────────────────────────────────────────────────────────##########        
    
    #############################
    # set temp baseball data
    #############################
    if(conf._type_performance == conf._type_performance_baseball_kbo):
        if conf._trackman_mode:
            conf._pitcher_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._pitcher_team), season=datetime.now().year, backnum=conf._pitcher_no)
            conf._batter_player = conf._api_client.get_player_info_by_backnum(team_id=get_team_code_by_index(conf._batter_team), season=datetime.now().year, backnum=conf._batter_no)    
            target_attr = f"_team_box2_img{conf._pitcher_team}"
            setattr(conf, "_team_box_main_img", getattr(conf, target_attr))
            target_attr = f"_team_box2_img{conf._batter_team}"
            setattr(conf, "_team_box_sub_img", getattr(conf, target_attr))

    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # RTSP Server
    # [Owner] joonho kim
    # [Date] 2025-05-25
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    if conf._rtsp_server :
        fd_rtsp_server_start()        

    if not SERVICE_MODE and conf._test_mode:
        # 로컬 테스트 실행 같은 경우에만 명시 종료
        aid.stop()
        sys.exit(exit_code)