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

    ver, date = conf.read_latest_release_from_md("./src/fd_release_aid.md")
    conf._version = ver
    conf._release_date = date

    fd_log.info(f"🧩 Latest Version: {conf._version}")
    fd_log.info(f"📅 Latest Date: {conf._release_date}")
    fd_log.info(f"─────────────────────────────────────────────────────────────────────────────")
    
    # ─────────────────────────────────────────────────────────────────────────────
    # AId daemon start
    # ─────────────────────────────────────────────────────────────────────────────
    aid = AId()

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

    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # Live Streaming
    # [Owner] hongsu
    # [Date] 2025-06-01
    # [Version] V.4.1.2.0
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # if conf._rtsp_client:
    #     fd_rtsp_client_start()
        
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # Analysis
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
    # https://4dreplay.atlassian.net/wiki/x/ewAAgQ
    # input zoom_factor : 확대할 비율 1.5 = 50% 확대
    # input shift_percentage : (0.1,-0.2) = 우측으로 10% 이동 상단으로 20% 이동
    # Input
    # 1. Type of sports : type_golfer = 0x11; type_baseball_batter = 0x21; type_baseball_pitcher = 0x22; type_baseball_homerun = 0x23; type_cricket_batman = 0x31;  type_cricket_bowler = 0x32 
    # 2. file folder : './videos/input/golf/outdoor/2024_04_28_13_18_33'
    # 3. output folder : './videos/output/golf'
    # 4. ip class : 101; -> camera ip class
    # 5. front camera number : 11 -> from "front_dsc" to change ip
    # 6. side camera number : 38 -> from "side_dsc" to change ip
    # 7. back camera number : 70 -> from "back_dsc" to change ip 
    # 8. analysis cameras id : 11,25,38,55,70 -> from "analysis_dsc"
    # 9. analysis cameras angle : 30° / 45° / 90° -> from "analysis_angle"
    # 10. start clip time : -1000 -> from "start_time"
    # 11. end clip time : 1000 -> from "end_time"
    # 12. fps : 10 -> from "fps"
    # 13. zoom scale : 1.8 -> from "zoom_ratio"
    # 14. selected time : 508 [from 0 to selected timing file]
    # 15. selected frame : 30 [from 0 to selected timing] : default = -1
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  

    # test error

    # _, output = aid.create_ai_file(0x0113, '//10.82.27.3/D_Movie/2025_08_17_17_49_41', './videos/output/baseball', 27, 13, -1000, 1000, 30, 100, 3878, 46) 


    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for baseball 
    # _type_baseball_batter_LH = 0x0111
    # 11 camera
    #
    # /B/A/T/T/E/R/ - RIGHT HAND                            11
    #
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────         
    # 25/05/11 [FHD,60p]
    '''
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,1922,16)           
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,3321,10)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,5653,43)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,8436,11)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,8891,26)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,11,-1000,1000,30,100,11911,46)  
    
    # 25/05/13 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,3026,16)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,3631,22)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,4455,14)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,5814,1)     
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,6364,15)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,7112,11)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,11,-1000,1000,30,100,10074,39)  
    # 25/05/14 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,3389,5)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,7753,36)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,8130,15)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,9535,33)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,10148,28)  
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,11,-1000,1000,30,100,10712,7)   

    # 25/05/15 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,11,-1000,1000,30,100,1444,48)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,11,-1000,1000,30,100,2767,22)       
    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,11,-1000,1000,30,100,9583,55)   
    
    # 25/05/17 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,278,38)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,1459,50)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,2378,33)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,2770,28)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,4172,11)       
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,4880,50)       
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,4950,2)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,11,-1000,1000,30,100,5114,0)    
    
    # 25/05/17-2 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,267,25)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,2417,14)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,3276,52)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,6222,24)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,6306,1)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,11,-1000,1000,30,100,9759,33)   
    
    # 25/05/18 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,1857,56)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,2453,31)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,3000,43)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,3256,29)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,4367,43)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,6376,4)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,7259,12)   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,11,-1000,1000,30,100,8349,12)   

    # 25/05/20 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,1066,37)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,1387,7)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,2115,25)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,3403,4)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,5701,20)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,11,-1000,1000,30,100,8832,49)    

    # 25/05/21 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,11,-1000,1000,30,100,2753,59)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,11,-1000,1000,30,100,6277,36)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,11,-1000,1000,30,100,7723,51)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,11,-1000,1000,30,100,8232,50)
    

    # 25/05/22 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,1409,34)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,2557,38)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,3326,46)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,7912,42)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,9674,6)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,11,-1000,1000,30,100,10358,22)

    # 25/05/23 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,11,-1000,1000,30,100,5590,43)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,11,-1000,1000,30,100,6754,10)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,11,-1000,1000,30,100,7166,4)

    # 25/05/24-1 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,11,-1000,1000,30,100,4712,1)  
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,11,-1000,1000,30,100,5705,41) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,11,-1000,1000,30,100,7401,56)
    # 25/05/24-2 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,11,-1000,1000,30,100,512,21)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,11,-1000,1000,30,100,2495,41)
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,11,-1000,1000,30,100,6649,20)
    
    # 25/05/25 [FHD,60p]
    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,107,34) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,1525,7) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,1622,27) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,2507,44)                   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,5231,13) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,5901,25) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,6069,3)     
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,7125,38)                     
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,10125,58)    
    
    # test not exist timing
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,107,30) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34', './videos/output/baseball', 27, 11, -1000, 1000, 30, 100, 1525, 15) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,1622,22) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,2507,49)                   
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,5231,10) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,5901,21) 
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,6069,5)     
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,7125,32)                     
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-1000,1000,30,100,10125,55)    
  

    # rough timing test

    # 25/07/27 [FHD,60p]
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,4191,35)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5177,35)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5306,43)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5902,32)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,9593,39)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,11417,17)    
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,12164,12)    

    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,4191,30)    # -5
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5177,40)    # +5
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5306,40)    # -3
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,5902,25)    # -7
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,9593,30)    # -9
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,11417,25)    # + 8
    _, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,11,-1000,1000,30,100,12164,20)    # +8
    '''

    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for baseball 
    # _type_baseball_batter_LH = 0x0112
    # 12 camera
    #
    # /B/A/T/T/E/R/ - LEFT HAND                             12
    #
    # ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── 
    
    '''
    # 25/05/11 [FHD,60p]        
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,1181,28)       
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,2303,10)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,2538,43)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,2538,43)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,4028,33)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,4206,43)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,4957,50)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,7427,33)   
    
    # 25/05/13 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,2568,42)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,5409,40)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,5523,20)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,6251,53)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,7704,27)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,8530,6)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,11365,4)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,11837,7)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,12053,1)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,12687,54)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,12,-1000,1000,30,100,13079,4)
    
    # 25/05/14 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,1676,25)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,2093,32)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,2256,43)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,3682,59)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,4188,45)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,4572,24)
    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,6260,8)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,6662,47)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,8390,27)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,8683,48)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,12,-1000,1000,30,100,8754,38)   

    # 25/05/15 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,3475,24)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,4305,35)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,6737,40)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,8071,38)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,8663,53)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,8776,21)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,9796,58)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,12,-1000,1000,30,100,10360,8)   
    
    # 25/05/17 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,12,-1000,1000,30,100,775,30)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,12,-1000,1000,30,100,3219,23)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,12,-1000,1000,30,100,4265,46)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,12,-1000,1000,30,100,5020,58)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,12,-1000,1000,30,100,5573,36)
    
    
    # 25/05/17-2 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,3355,59)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,3850,20)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,6042,47)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,7154,16)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,7535,16)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,9468,58)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,12,-1000,1000,30,100,13241,36)
        
    # 25/05/18 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,12,-1000,1000,30,100,928,13)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,12,-1000,1000,30,100,1383,39)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,12,-1000,1000,30,100,1485,39)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,12,-1000,1000,30,100,3659,58)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,12,-1000,1000,30,100,3952,44)   
    
    # 25/05/20 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,960,35)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,2329,10)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,4640,46)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,4840,55)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,5462,56)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,5955,54)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,6301,0)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,8931,55)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,6432,32)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,10563,18)  
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,11138,55)  
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,12,-1000,1000,30,100,11373,9)   
    
    # 25/05/21 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,1313,42)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,2113,27)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,2347,58)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,4934,14)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,5884,55)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,6720,25)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,9067,34)      
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,9307,42)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,10080,4)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,12,-1000,1000,30,100,10845,25)  
    

    # 25/05/22 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,2874,12)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,3742,51)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,5037,48)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,5520,12)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,6411,20)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,7636,22)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,12,-1000,1000,30,100,10896,54)

    # 25/05/23 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,12,-1000,1000,30,100,2264,57)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,12,-1000,1000,30,100,2756,36)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,12,-1000,1000,30,100,2827,55)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,12,-1000,1000,30,100,3972,37) 

    # 25/05/24-1 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,5392,26)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,6295,21)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,6469,23)  
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,7503,15)  
    # 25/05/24 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,823,51)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,2239,25)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,2852,0)      
    
    # test rough timing
    # 25/05/24-1 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,5392,22)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,6295,15)    
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,6469,22)  
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,12,-1000,1000,30,100,7503,10)  
    # 25/05/24 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,823,45)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,2239,27)   
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,12,-1000,1000,30,100,2852,3)
    
    # 25/05/25 [FHD,60p]
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,418,14) 
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,8265,51) 
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,8471,11) 
    
    # _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,10573,46)     
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,10573,40)     
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,10573,45)
    _, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,12,-1000,1000,30,100,10573,50)     
    '''
    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────                          
    # 1-Way for baseball 
    # _type_baseball_batter_LH = 0x0113
    # 13 camera
    #
    # /P/I/T/C/H/E/R/                                       13
    #
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────     
    
    # 25/05/11 [FHD,60p]        
    '''
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,1657,56)  # ✅[pitcher][3][45]2025_05_11_18_12_06    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,2246,37)  # ✅[pitcher][3][45]2025_05_11_18_21_55
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,3517,35)  # ✅[pitcher][3][45]2025_05_11_18_43_06
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,5284,3)   # ✅[pitcher][3][45]2025_05_11_19_12_33
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,5850,27)  # ✅[pitcher][3][45]2025_05_11_19_21_59    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,8346,47)  # ✅[pitcher][3][45]2025_05_11_20_03_35
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,9455,46)  # ✅[pitcher][3][45]2025_05_11_20_22_04    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,11319,36) # ✅[pitcher][3][45]2025_05_11_20_53_08
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,11541,43) # ✅[pitcher][3][45]2025_05_11_20_56_50
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,13,-1000,1000,30,120,11881,3)  # ✅[pitcher][3][45]2025_05_11_21_02_30
    
    # 25/05/13 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,2092,43)  # ✅[pitcher][3][45]2025_05_13_18_48_11
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,2295,3)   # ✅bound [pitcher][3][45]2025_05_13_18_51_34
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,3816,17)  # ✅[pitcher][3][45]2025_05_13_19_16_55
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,3903,38)  # ✅[pitcher][3][45]2025_05_13_19_18_22
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,4858,33)  # ✅[pitcher][3][45]2025_05_13_19_34_17
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,5228,5)   # ✅[pitcher][3][45]2025_05_13_19_40_27
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,7423,56)  # ✅[pitcher][3][45]2025_05_13_20_17_02
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,9351,45)  # ✅[pitcher][3][45]2025_05_13_20_49_10
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,9815,37)  # ✅[pitcher][3][45]2025_05_13_20_56_54

    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,10321,4)  # ✅[pitcher][3][45]2025_05_13_21_05_20
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,10913,29) # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,11278,24) # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,13,-1000,1000,30,120,13042,6)  # ✅
    
    # 25/05/14 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,1553,34)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,1806,8)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,2700,49)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,2823,25)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,3505,3)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,3931,15)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,4735,34)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,5324,22)  # ✅add
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,5542,53)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,6947,59)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,7301,27)  # ✅ 
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,8913,40)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,9186,23)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,9281,1)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,10637,11) # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,10884,9)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_14_18_21_22',    './videos/output/baseball',27,13,-1000,1000,30,120,11168,15) # ✅

    # 25/05/15 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,876,39)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,2076,54)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,2220,30)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,2383,0)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,2659,10)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,3107,56)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,5985,36)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,6598,15)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,9307,19)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,10242,43) # ✅ 
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,13,-1000,1000,30,120,10502,56) # ✅
    
    # 25/05/17 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,877,50)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,1778,5)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,4088,55)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,5441,24)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,5573,7)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,13,-1000,1000,30,120,5844,47)  # ✅

    # 25/05/17 [FHD,60p]        
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,848,7)    # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,965,48)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,2370,20)  # ✅    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,4427,56)  # ✅
    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,6014,2)   # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,6615,30)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,10347,17) # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,12569,44) # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_17_18_56_35',    './videos/output/baseball',27,13,-1000,1000,30,120,13077,27) # ✅

    # 25/05/18 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,1708,57)  # ✅[pitcher][3][45]2025_05_18_14_17_35.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,2413,16)  # ✅[pitcher][3][45]2025_05_18_14_29_20.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,2721,7)   # ✅[pitcher][3][45]2025_05_18_14_34_28.mp4]  
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,3177,45)  # ✅[pitcher][3][45]2025_05_18_14_42_04.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,3829,52)  # ✅[pitcher][3][45]2025_05_18_14_52_56.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,4790,29)  # ✅[pitcher][3][45]2025_05_18_15_08_57.mp4]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,5375,0)   # ✅[pitcher][3][45]2025_05_18_15_18_42.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,5590,18)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,5701,36)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,5778,59)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,6762,11)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,7678,41)  # ✅
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,13,-1000,1000,30,120,9371,19)  # ✅
    
    
    # 25/05/20 [FHD,60p]    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,1245,12)  #✅ 8,039.57 ms [pitcher][3][45]2025_05_20_18_40_40.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,2575,29)  #? ✅ - bqall size (24) 5,374.67 ms [pitcher][3][45]2025_05_20_19_02_50.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,3599,20)  #? ✅ - bqall size (24) 5,519.85 ms [pitcher][3][45]2025_05_20_19_19_54.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,3686,1)   #? ✅ - bqall size (24) 5,712.75 ms [pitcher][3][45]2025_05_20_19_21_21.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,4218,53)  #? ✅ - bqall size (24) 5,544.81 ms[pitcher][3][45]2025_05_20_19_30_13.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,5115,36)  #✅ 5,568.12 ms [pitcher][3][45]2025_05_20_19_45_10.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,5620,19)  #? ✅ - bqall size (24)5,609.26 ms [pitcher][3][45]2025_05_20_19_53_35.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,6113,29)  #? ✅ - bqall size (24) 5,701.13 ms [pitcher][3][45]2025_05_20_20_01_48.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,6646,58)  #? ✅ - bqall size (24) 5,620.51 ms [pitcher][3][45]2025_05_20_20_10_41.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,7480,26)  #? ✅ - bqall size (24) 5,501.99 ms [pitcher][3][45]2025_05_20_20_24_35.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,7611,35)  #? ✅ - bqall size (25) 5,655.36 ms [pitcher][3][45]2025_05_20_20_26_46.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,8624,25)  #? ✅ - bqall size (24) 5,473.46 ms [pitcher][3][45]2025_05_20_20_43_39.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,8695,55)  #❗✅ - kalman predict 5,522.64 ms [pitcher][3][45]2025_05_20_20_44_50.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,10729,23) #❌ ✅ - debug : 5,778.01 ms [pitcher][3][45]2025_05_20_21_18_44.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,13,-1000,1000,30,120,11014,26) #✅ 5,619.72 ms [pitcher][3][45]2025_05_20_21_23_29.mp4]
       
    # 25/05/21 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,956,56)   #✅[pitcher][3][45]2025_05_21_18_32_12
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,2871,42)  #✅[pitcher][3][45]2025_05_21_19_04_07
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,3775,18)  #✅[pitcher][3][45]2025_05_21_19_19_11
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,3985,58)  #✅[pitcher][3][45]2025_05_21_19_22_41
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,4559,3)   #✅-debug [pitcher][3][45]2025_05_21_19_32_15
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,4664,49)  #✅[pitcher][3][45]2025_05_21_19_34_00
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,5613,51)  #✅[pitcher][3][45]2025_05_21_19_49_49
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,5986,1)   #✅-debug[pitcher][3][45]2025_05_21_19_56_02
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,7096,7)   #✅ [pitcher][3][45]2025_05_21_20_14_32
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,7532,23)  #✅-check movement[pitcher][3][45]2025_05_21_20_21_48    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,7971,59)  #❌✅ [pitcher][3][45]2025_05_21_20_29_07.mp4]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,8703,30)  #✅[pitcher][3][45]2025_05_21_20_41_19
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,8845,3)   #✅[pitcher][3][45]2025_05_21_20_43_41
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,10316,32) #✅[pitcher][3][45]2025_05_21_21_08_12
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,13,-1000,1000,30,120,10957,57) #❌✅[pitcher][3][45]2025_05_21_21_18_53
    

    # 25/05/22 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,1293,19)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,1530,56)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,2242,32)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,2322,14)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,2425,7)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,3097,59)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,5180,18)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,5636,23)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,6189,24)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,6855,43)  
    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,7474,44)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,8306,52)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,8881,28)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,9096,40)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,9342,40)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,10241,27)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,13,-1000,1000,30,120,11865,53)    

    # 25/05/23 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,576,46)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,1492,47)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,1900,39)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,2234,45)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,2470,42)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,2652,12)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,4230,36)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,4534,54)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,5249,2)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,6506,41)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,7825,8)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,8999,5)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,13,-1000,1000,30,120,9596,53)


    # 25/05/24-1 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,13,-1000,1000,30,120,4072,21)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,13,-1000,1000,30,120,5222,47)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,13,-1000,1000,30,120,5705,16)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,13,-1000,1000,30,120,6109,9)    
    # 25/05/24-2 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,478,29)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,1114,17)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,1502,32)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,2079,56)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,2442,30)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,3354,6)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,3952,52)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,4042,23)        
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,5777,13)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,5878,58)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,6157,11)    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,13,-1000,1000,30,120,6226,7)    
        
    # 25/05/25 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,215,45)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,1121,44)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,2154,33)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,2364,29)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,2910,2)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,3475,47)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,7075,42)   
    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,7460,1)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,7738,24)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,8877,45)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,120,9886,39)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_28_18_17_16',    './videos/output/baseball',27,13,-1000,1000,30,120,1844,34)  
    
    # rough timing test
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,215,40)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,1121,42)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,2154,30)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,2364,23)   
    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,2909,55)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,3475,40)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,7075,34)   
    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,7459,54)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,7738,20)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,8877,41)   
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,13,-1000,1000,30,100,9886,33)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_05_28_18_17_16',    './videos/output/baseball',27,13,-1000,1000,30,100,1844,31)  
    
    # 25/07/02 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,2378,18)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,3051,11)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,3791,32)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,4717,7)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,5220,40)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,5970,23)           
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,6140,38)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,7054,3)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,7780,16)       
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_02_18_21_20',    './videos/output/baseball',27,13,-1000,1000,30,100,7900,15)           
    
    # 25/07/08 [FHD,60p]
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_08_18_30_41',    './videos/output/baseball',29,13,-1000,1000,30,100,5122,30)          
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_08_18_30_41',    './videos/output/baseball',29,13,-1000,1000,30,100,6821,22)          
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_08_18_30_41',    './videos/output/baseball',29,13,-1000,1000,30,100,7673,20)    


    # 25/07/27 [FHD,60p]

    # 임찬규
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_26_17_55_21', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 6999, 30) 
    time.sleep(10)
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_26_17_55_21', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 6999, 25)  # -5

    # rough timing
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,940,10)    # -7    
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,2105,40)   # -7 --> problem (pose detection error - hand)
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,2105,42)   # -5 --> OK
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,3047,20)   # -9 --> problem (pose detection error - hand)
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,3047,25)   # -4 --> OK
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,3913,40)   # -4
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,6344,20)   # -8
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,6752,50)   # -1
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,6968,10)   # -10
    _, output = aid.create_ai_file (0x0113, './videos/input/baseball/KBO/2025_07_27_17_55_56',    './videos/output/baseball',27,13,-1000,1000,30,100,11141,10)    # -8
    
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_22_18_52_26', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 3462, 30)    
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_22_18_52_26', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 650, 10)
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_22_18_52_26', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 9363, 20)  # 끊김
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_23_18_20_51', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 4675, 13)
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_24_18_18_57', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 13635, 55)  # 너무 빠름
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_24_18_18_57', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 2322, 55)  # 끊김
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_24_18_18_57', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 13112, 5)  # 너무 늦음
    _, output = aid.create_ai_file(0x0113, './videos/input/baseball/KBO/2025_07_24_18_18_57', './videos/output/baseball', 27, 13, -300, 3000, 30, 100, 7030, 20)  # 늦음
    
    '''

    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for baseball : _type_baseball_hit = 0x0114
    #
    #   /H/I/T/ - hit and homerun                           14
    #
    # ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── 
    '''
    #25/05/06 [FHD,60fps], day   
    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_06_13_40_24',    './videos/output/baseball',27,14,-1500,0,30,100,2447,3)      # 🚩hangtime:5.88,distance:117.6, angle:-41.5
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_06_13_40_24',    './videos/output/baseball',27,14,-1500,0,30,100,2133,41)     # ✅[hit][3][45]2025_05_06_14_15_57.mp4    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_06_13_40_24',    './videos/output/baseball',27,14,-1500,0,30,100,2798,22)     # ✅[hit][3][45]2025_05_06_14_27_02.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_06_13_40_24',    './videos/output/baseball',27,14,-1500,0,30,100,6105,5)      # [HIT] not auto detection : over hantime;5.550643
    
    #25/05/07 [FHD,60fps], evening
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,940,18)      # ✅[hit][3][45]2025_05_07_18_31_15.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,2443,37)     # ✅[hit][3][45]2025_05_07_18_56_18.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,3183,46)     # ✅[hit][3][45]2025_05_07_19_08_38.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,3341,45)     # ✅[hit][3][45]2025_05_07_19_11_16.mp4
    '''

    '''
    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,6428,45)     # ✅[hit][3][45]2025_05_07_20_02_43.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,6822,59)     # ✅[hit][3][45]2025_05_07_20_09_17.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,8766,16)     # ✅[hit][3][45]2025_05_07_20_41_41.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,9943,58)     # ✅[hit][3][45]2025_05_07_21_01_18.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,7151,28)     # ✅❗[hit][3][45]2025_05_07_20_14_46.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_07_18_15_35',    './videos/output/baseball',27,14,-1500,0,30,100,5277,39)     # [HIT] not auto detection : over hantime;4.976789
    
    #25/05/11 [FHD,60fps], day
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,1181,28)     # ✅[hit][3][45]2025_05_11_18_04_10.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,2303,10)     # [HIT] not auto detection : over hantime;3.819734
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,2538,43)     # ✅[hit][3][45]2025_05_11_18_26_47.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,2644,17)     # ✅[hit][3][45]2025_05_11_18_28_33.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,3321,10)     # ✅[hit][3][45]2025_05_11_18_28_33.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,4028,33)     # ✅[hit][3][45]2025_05_11_18_51_37.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,4206,43)     # ✅[hit][3][45]2025_05_11_18_54_35.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,5653,43)     # ❗❗-> bat [hit][3][45]2025_05_11_19_18_42.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,8436,11)     # ✅[hit][3][45]2025_05_11_20_05_05.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,11911,46)    # [HIT] not auto detection : over hantime;4.062591
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,14,-1500,0,30,100,1922,16)     # [HIT] not auto detection : over hantime;4.228883
    
    #25/05/13 [FHD,60fps], evening
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,6251,53)    # ✅[hit][3][45]2025_05_13_19_57_30.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,6364,15)    # ✅❗[hit][3][45]2025_05_13_19_59_23.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,7112,11)    # ✅[hit][3][45]2025_05_13_20_11_51.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,7704,27)    # ✅[hit][3][45]2025_05_13_20_21_43.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,8530,6)     # ✅[hit][3][45]2025_05_13_20_35_29.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,11365,4)    # ❗❗[hit][3][45]2025_05_13_21_22_44.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,13079,4)    # ✅[hit][3][45]2025_05_13_21_51_18.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,5409,40)    # [HIT] not auto detection : over hantime;5.345244
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,4455,14)    # [HIT] not auto detection : over hantime;5.697933
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,5814,1)     # [HIT] not auto detection : over hantime;4.266557
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,1777,33)    # [HIT] not auto detection : over hantime;5.551774
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,2568,42)    # [HIT] not auto detection : over hantime;3.871358
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_13_18_13_19',    './videos/output/baseball',27,14,-1500,0,30,100,10074,39)   # [HIT] not auto detection : over hantime;5.683396
    
    
    #25/05/15 [4K,60fps], evening
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,4878,50)    # ✅[hit][3][45]2025_05_15_19_37_56.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,6737,40)    # ✅[hit][3][45]2025_05_15_20_08_55.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,8071,38)    # ✅[hit][3][45]2025_05_15_20_31_09.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,8776,21)    # ✅[hit][3][45]2025_05_15_20_42_54.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,3475,24)    # [HIT] not auto detection : over hantime;3.997293
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,4305,35)    # [HIT] not auto detection : over hantime;4.170543
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,9583,55)    # [HIT] not auto detection : over hantime;5.952356
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_15_18_16_38',    './videos/output/baseball',27,14,-1500,0,30,100,9796,58)    # [HIT] not auto detection : over hantime;4.967761
        
    #25/05/17 [4K,60fps], evening
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,775,30)     # ✅[hit][3][45]2025_05_17_16_09_53.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,1459,50)    # ✅[hit][3][45]2025_05_17_16_21_17.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,2378,33)    # ✅[hit][3][45]2025_05_17_16_36_36.mp4    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,2770,28)    # ✅[hit][3][45]2025_05_17_16_43_08.mp4
    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,3219,23)    # [HIT] not auto detection : over hantime;6.132378
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,4172,11)    # ✅[hit][3][45]2025_05_17_17_06_30.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,4880,50)    # ✅[hit][3][45]2025_05_17_17_18_18.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,4950,2)     # ✅[hit][3][45]2025_05_17_17_19_28.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,5020,58)    # ✅[hit][3][45]2025_05_17_17_20_38.mp4        
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_17_15_56_58',    './videos/output/baseball',27,14,-1500,0,30,100,5114,0)     # [HIT] not auto detection : over hantime;5.686

    #25/05/18 [4K,60fps], day
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,1383,39)     # ❗❗[hit][3][45]2025_05_18_14_12_10.mp4 distance:48.02127, hangtime:3.002344, speed:84.59108, landing (x:38.650395928316144,y:28.49893448880682)
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,1485,39)     # ✅[hit][3][45]2025_05_18_14_13_52.mp4 distance:42.56101, hangtime:1.04114, speed:171.02298, landing (x:33.15847913412361,y:26.682481776101515)
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,3000,43)     # ✅[hit][3][45]2025_05_18_14_39_07.mp4 distance:51.45383, hangtime:1.681026, speed:134.91006, landing (x:44.56274751081826,y:-25.722716729691694)  
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,3256,29)     # ✅[hit][3][45]2025_05_18_14_43_23.mp4 distance:20.81843, hangtime:0.651295, speed:121.5703, landing (x:20.726092422999855,y:-1.9586016793028702)
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,6376,4)      # ✅[hit][3][45]2025_05_18_15_35_23.mp4] distance:49.53322, hangtime:1.657945, speed:127.06748, landing (x:40.303852929304135,y:-28.79477943345672)  
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,1857,56)     # 
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,2453,31)     # 
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_18_13_49_07',    './videos/output/baseball',27,14,-1500,0,30,100,8349,12)     # 
        
    #25/05/20 [4K,60fps], eveing    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,2115,25)      # ✅[hit][3][45]2025_05_20_18_55_10.mp4] distance:48.95135, hangtime:1.527862
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,2329,10)      # hangtime:3.924881,    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,3403,4)       # hangtime:3.980191,
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,4640,46)      # hangtime:3.669688
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,5701,20)      #✅
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,6301,0)       #✅
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,9432,32)      #✅
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,10563,18)     #✅    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,11138,55)     #✅
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_20_18_19_55',    './videos/output/baseball',27,14,-1500,0,30,100,11373,9)      #✅
    
    #25/05/21 [4K,60fps], eveing    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,1313,42)      #✅[hit][3][45]2025_05_21_18_38_09
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,2113,27)      #✅[hit][3][45]2025_05_21_18_51_29
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,2753,59)      #✅[hit][3][45]2025_05_21_19_02_09
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,5664,58)      #✅[hit][3][45]2025_05_21_19_50_40
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,7723,51)      #✅[hit][3][45]2025_05_21_20_24_59
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,8232,50)      #✅[hit][3][45]2025_05_21_20_33_28
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,9067,34)      #✅[hit][3][45]2025_05_21_20_47_23.mp4    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,9307,42)      #✅[hit][3][45]2025_05_21_20_51_23
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_21_18_16_16',    './videos/output/baseball',27,14,-1500,0,30,100,9896,7)       #✅[hit][3][45]2025_05_21_21_01_12
    
    #25/05/22 [4K,60fps], eveing    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,1409,34)      #✅[hit][3][45]2025_05_22_18_45_02.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,2557,38)      #✅[hit][3][45]2025_05_22_19_04_10.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,3326,46)      #✅[hit][3][45]2025_05_22_19_16_59.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,3742,51)      #[HIT] not auto detection : over hantime;5.630186
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,5037,48)      #[HIT] not auto detection : over hantime;4.705534
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,5520,12)      #✅[hit][3][45]2025_05_22_19_53_33.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,7636,22)      #✅[hit][3][45]2025_05_22_20_28_49.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,8409,23)      #✅[hit][3][45]2025_05_22_20_41_42.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,9674,6)       #✅[hit][3][45]2025_05_22_21_02_47.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,10358,22)     #[HIT] not auto detection : over hantime;5.301687
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_22_18_21_33',    './videos/output/baseball',27,14,-1500,0,30,100,10896,54)     #✅[hit][3][45]2025_05_22_21_23_09.mp4

    #25/05/23 [4K,60fps], eveing    3
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,14,-1500,0,30,100,2264,57)      #✅[hit][3][45]2025_05_23_19_46_46.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,14,-1500,0,30,100,6754,10)      #✅[hit][3][45]2025_05_23_21_01_36.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_23_19_09_02',    './videos/output/baseball',27,14,-1500,0,30,100,7166,4)       #✅[hit][3][45]2025_05_23_21_08_28.mp4

    #25/05/24-1 [4K,60fps], eveing  6  
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,4712,1)       #✅[hit][3][45]2025_05_24_17_14_14
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,5392,26)      #✅[hit][3][45]2025_05_24_17_25_34.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,6295,21)      #[HIT] not auto detection : over hantime;5.4859
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,6469,23)      #✅[hit][3][45]2025_05_24_17_43_31.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,7401,56)      #✅[hit][3][45]2025_05_24_17_59_03
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_15_55_42',    './videos/output/baseball',27,14,-1500,0,30,100,7503,15)      #✅[hit][3][45]2025_05_24_18_00_45.mp4
    
    #25/05/24-2 [4K,60fps], eveing  5  
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,14,-1500,0,30,100,512,21)       #[HIT] not auto detection : over hantime;3.340633
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,14,-1500,0,30,100,823,51)       #[HIT] not auto detection : over hantime;3.340633
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,14,-1500,0,30,100,2495,41)      #[HIT] not auto detection : over hantime;4.579864   
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,14,-1500,0,30,100,2852,0)       #[HIT] not auto detection : over hantime;5.453553
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_24_18_07_10',    './videos/output/baseball',27,14,-1500,0,30,100,6649,20)      #✅[hit][3][45]2025_05_24_19_57_59.mp4
    
    #25/05/25 [4K,60fps], eveing    11
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,107,34)       #✅[hit][3][45]2025_05_25_14_07_21.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,418,14)       #✅[hit][3][45]2025_05_25_14_12_32.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,1525,7)       #✅[hit][3][45]2025_05_25_14_30_59.mp4    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,2507,44)      #✅❗[hit][3][45]2025_05_25_14_47_21.mp4    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,5231,13)      #✅❗[hit][3][45]2025_05_25_15_32_45.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,5901,25)      #✅❗[hit][3][45]2025_05_25_15_43_55.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,6069,3)       #❗❗[hit][3][45]2025_05_25_15_46_43.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,7125,38)      #[HIT] not auto detection : over hantime;4.345301    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,8265,51)      #✅[hit][3][45]2025_05_25_15_46_43.mp4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,10125,58)     #✅[hit][3][45]2025_05_25_16_54_19.mp4    
    _, output = aid.create_ai_file (0x0115, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,14,-1500,0,30,100,10573,46)     #✅[hit][3][45]2025_05_25_17_01_47.mp4
    
    #25/06/27 [FHD,60fps], eveing    10    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,595,3)         # 027014_595_3_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,2141,56)       # 027014_2141_56_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,2947,26)       # 027014_2947_26_baseball_data.pkl
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,3205,56)       # 027014_3205_56_baseball_data.pkl    
    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,5538,21)       # 027014_5538_21_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,5636,46)       # 027014_5636_46_baseball_data.pkl    🚩hangtime:4.81,distance:114.6, angle:-35.7
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,6380,26)       # 027014_6380_26_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,7263,20)       # 027014_7263_20_baseball_data.pkl    🚩hangtime:5.81,distance:114.3, angle:-43.7
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,7737,34)       # 027014_7737_34_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_27_18_48_41',    './videos/output/baseball',27,14,-1500,0,30,100,9323,4)        # 027014_9323_4_baseball_data.pkl    
    
    #25/06/30 [FHD,60fps], eveing    11    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_29_18_42_36',    './videos/output/baseball',27,14,-1500,0,30,100,39,29)        # 027014_39_29_baseball_data.pkl    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_29_18_42_36',    './videos/output/baseball',27,14,-1500,0,30,100,1574,53)      # 027014_1574_53_baseball_data.pkl
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_06_29_18_42_36',    './videos/output/baseball',27,14,-1500,0,30,100,4188,43)      # 027014_4188_49_baseball_data.pkl   🚩hangtime:4.68,distance:116.5, angle:17.8
    '''    

    '''    
    #25/07/01 [FHD,60fps], eveing    11    
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_01_18_22_48',    './videos/output/baseball',27,14,-1500,0,30,100,1756,38)      # 027014_1756_38_baseball_data.pkl   🚩hangtime:5.08,distance:120.5, angle:-42.4
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_01_18_22_48',    './videos/output/baseball',27,14,-1500,0,30,100,5428,22)      # 027014_5428_22_baseball_data.pkl
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_01_18_22_48',    './videos/output/baseball',27,14,-1500,0,30,100,5789,35)      # 027014_5789_35_baseball_data.pkl
    _, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_01_18_22_48',    './videos/output/baseball',27,14,-1500,0,30,100,6008,54)      # 027014_6008_54_baseball_data.pkl
    '''
    
    #_, output = aid.create_ai_file (0x0111, './videos/input/baseball/KBO/2025_05_25_14_05_34',    './videos/output/baseball',27,11,-2000,3500,30,100,10125,58)        
    #_, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_01_18_22_48',    './videos/output/baseball',27,14,-1500,0,30,100,1756,38)      # 027014_1756_38_baseball_data.pkl   🚩hangtime:5.08,distance:120.5, angle:-42.4
    
    #_, output = aid.create_ai_file (0x0112, './videos/input/baseball/KBO/2025_05_11_17_44_29',    './videos/output/baseball',27,12,-1000,1000,30,100,1181,28)       
    
    #25/07/01 [FHD,60fps], eveing    11    
    #_, output = aid.create_ai_file (0x0114, './videos/input/baseball/HR_Derby/2024_07_15_21_16_28',    './videos/output/baseball',11,19,-1500,0,30,100,525,54)
    #_, output = aid.create_ai_file (0x0114, './videos/input/baseball/KBO/2025_07_08_18_30_41',    './videos/output/baseball',29,14,-1500,0,30,100,10903,12)      # 027014_6008_54_baseball_data.pkl
    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for pitcher multi : _type_baseball_hit = 0x0115
    #
    #   /M/U/L/T/I/ /H/I/T/
    #
    # ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── 
    #_, output = aid.create_ai_file (0x011b, './videos/input/baseball/KBO/2025_07_01_18_22_48', './videos/output/baseball',27,14,-1500,0,30,100,0,0)      # 027014_6008_54_baseball_data.pkl
    #_, output = aid.create_ai_file (0x011b, './videos/input/baseball/KBO/2025_07_01_18_22_48', './videos/output/baseball',27,14,-1500,0,30,100,0,0)      # 027014_6008_54_baseball_data.pkl
    #_, output = aid.create_ai_file (0x011b, './videos/input/baseball/HR_Derby/2024_07_15_19_49_22',    './videos/output/baseball',11,17,-1000,0,30,100,193,58)

    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for pitcher multi : _type_baseball_hit = 0x0115
    #
    #   /M/U/L/T/I/ /P/I/T/C/H/E/R/                                 15
    #
    # ─────────────────────────────────────────────────────────────────────────────####D######################################################### 
    '''
    # Fastball  ./videos/input/baseball/KBO/2025_03_22_13_56_16/028013_804_36_baseball_data.pkl     # 2025_03_22_14_09_40.mp4
    # Slider    ./videos/input/baseball/KBO/2025_03_22_13_56_16/028013_845_33_baseball_data.pkl     # 2025_03_22_14_10_21.mp4
    # ChangeUp  ./videos/input/baseball/KBO/2025_03_22_13_56_16/028013_1428_14_baseball_data.pkl    # 2025_03_22_14_20_04.mp4
    # Splitter  ./videos/input/baseball/KBO/2025_03_22_13_56_16/028013_1673_8_baseball_data.pkl     # 2025_03_22_14_24_09
    # _, output = aid.create_ai_file (0x0115, './videos/input/baseball/KBO/2025_03_22_13_56_16',    './videos/output/baseball',28,13,-2000,3000,30,130,358,2)
    '''
    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 1-Way for pitcher multi : _type_baseball_hit = 0x0121
    #
    #   /G/O/L/F/       0x0121
    #
    # ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────── 
    ''' 
    # /L/P/G/A/
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,'11,38,70',-1200,1200,10,100,508,2)              # 2way, 2 cameras 90°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38',45,-1500,1700,30,180,508,-1)          # 2way, 3 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,38,70',90,-1500,1700,30,180,508,-1)          # 2way, 3 cameras 90°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,70,'11,38,70',90,-1500,1700,30,180,508,-1)          # 3way, 3 cameras 90°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1500,1700,30,180,508,-1)    # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,70,'11,25,38,55,70',45,-1500,1700,30,180,508,-1)    # 3way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,70,'11,25,38,55,70',45,-1300,1500,30,180,508,2)    # 3way, 5 cameras 45°    
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,584,-1)   # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1192,-1)  # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1242,-1)  # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1281,-1)  # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1911,-1)  # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1948,-1)  # 2way, 5 cameras 45°
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_04_28_13_18_33','./videos/output/golf',101,11,38,-1,'11,25,38,55,70',45,-1300,1500,30,180,1987,-1)  # 2way, 5 cameras 45°
    
    # /P/G/A/
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_09_18_16_02_13','./videos/output/golf',101,'13,26,38',-1000,1000,10,180,26)  # JPGA    
    _, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_06_13_17_27_42','./videos/output/golf',11,'11,40,50',-1200,1200,10,100,144,36) # PGA - Bryson Dechambeau    
    _, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_06_14_11_23_09','./videos/output/golf',11,'11,40,50',-1200,1200,10,180,168,13) # PGA - Xander Schauffele 168
    _, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_06_14_11_23_09','./videos/output/golf',11,'11,40,50',-1200,1200,10,180,210,25) # PGA - Rory Mcilroy 210
    _, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_06_14_11_23_09','./videos/output/golf',11,'11,40,50',-1200,1200,10,180,256,14) # PGA - Scottie Scheffler 256
    _, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_06_14_14_14_00','./videos/output/golf',11,'18,40,50',-1200,1200,10,180,765,46) # PGA - Tiger Woods
    
    # a/m/a/t/e/u/r/
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_18_16_02_13',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,26,23)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_51_48',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,33,52)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_52_49',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,48,10)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_54_00',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,14,25)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_56_11',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,15,31)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_56_11',    './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,28,31)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/indoor/2024_11_06_14_06_53',     './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,58,57)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/indoor/2024_10_22_09_29',        './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,56)
    _, output = aid.create_ai_file (0x0121,'./videos/input/golf/indoor/2024_11_06_14_06_53',     './videos/output/golf',101,13,26,-1,'13,26,38',90,-1000,1200,30,180,58)
    #_, output = aid.create_ai_file (0x0121,'./videos/input/golf/outdoor/2024_09_21_12_54_00',     './videos/output/golf',101,'13,26',-1000,1200,6,100,14,25)       # front, side, back : 2 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2024_09_21_12_54_00',     './videos/output/golf',101,'13,26,38',-1000,1200,6,100,14,25)    # front, side, back : 3 cameras
    '''

    # pana womens 
    # '2025/05/01 - JLPGA - pro
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_01_13_42_17',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,26,52)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_01_14_10_38',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,54,59)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_01_14_10_38',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,552,29)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_01_15_21_20',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,97,20)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_01_15_46_50',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,514,29)    # front, side, back : 3 cameras
    # '2025/05/02
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_02_13_36_27',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,466,40)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_02_15_21_51',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,32,18)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_02_15_21_51',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,607,53)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_02_15_21_51',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,724,42)    # front, side, back : 3 cameras
    # '2025/05/03
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_03_16_00_49',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,328,0)    # front, side, back : 3 cameras
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_03_16_00_49',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,536,52)    # front, side, back : 3 cameras
    # '2025/05/03
    #_, output = aid.create_ai_file (0x0123,'./videos/input/golf/outdoor/2025_05_04_12_54_10',     './videos/output/golf',101,'11,24,37',-1200,1200,6,100,1415,3)    # front, side, back : 3 cameras
    # '2025/05/17 - indoor, portrait, FHD 180p
    # _, output = aid.create_ai_file (0x0125,'./videos/input/golf/indoor/2025_05_17_15_55_59',     './videos/output/golf',19,'11,12,131',-1200,1200,30,100,25,69)    # front, side, back : 3 cameras : portrait
    # '2025/05/18 - indoor, portrait, FHD 180p
    #_, output = aid.create_ai_file (0x0125,'./videos/input/golf/indoor/2025_05_18_16_18_59',     './videos/output/golf',19,'11,12,131',-1200,1200,30,100,48,47)    # front, side, back : 3 cameras : portrait
    
    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # create a multi-division video
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────      
    # Input
    # 1. [type_devision]        type of division 
    #    _type_2_h_division      = 0x0221 [1][2]
    #    _type_2_v_division      = 0x0222 [1]
    #                                     [2]
    #    _type_3_h_division      = 0x0231 [1][2][3] 
    #    _type_3_m_division      = 0x0233 [1][2]
    #                                     [3]
    #    _type_4_division        = 0x0241 [1][2]
    #                                     [3][4]
    #    _type_9_division        = 0x0291 [1][2][3]
    #                                     [4][5][6]
    #                                     [7][8][9]
    #    _type_16_division       = 0x02f1 [1][2][3][4]
    #                                     [5][6][7][8]
    #                                     [9][10][11][12]
    #                                     [13][14][15][16]
    # 2. file folder : './videos/input/golf/outdoor/2024_04_28_13_18_33'
    # 3. output folder : './videos/output/golf'
    # 4. ip class : 101; -> camera ip class
    # 5. analysis cameras id : 11,25,38,55,70 "from the left, clockwise"
    # 6. start clip time : -1000 -> from "start_time"
    # 7. end clip time : 1000 -> from "end_time"
    # 8. fps : 10 -> from "fps"
    # 9. zoom scale : 1.0 -> from "zoom_ratio"
    # 10. selected time : 508 [from 0 to selected timing file]
    # 11. selected frame : 30 [from 0 to selected timing] : default = -1
    #
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────    
    #_, output_devision = aid.create_ai_file (0x0221,'./videos/input/nascar/2025_02_26_10_54_06',    './videos/output/nascar',23,'13,20',0,13000,30,100,43,27)
    #_, output_devision = aid.create_ai_file (0x0222,'./videos/input/nascar/2025_02_26_10_54_06',    './videos/output/nascar',23,'13,20',0,13000,30,100,43,27)
    #_, output_devision = aid.create_ai_file (0x0231,'./videos/input/nascar/2025_02_26_10_54_06',    './videos/output/nascar',23,'13,20,25',0,13000,30,100,43,27)
    #_, output_devision = aid.create_ai_file (0x0232,'./videos/input/nascar/2025_02_26_10_54_06',    './videos/output/nascar',23,'13,20,25',0,13000,30,100,43,27)
        
    # NASCAR 4-CH
    #_, output_devision = aid.create_ai_file (0x0241,'./videos/input/nascar/2025_02_26_10_54_06',    './videos/output/nascar',23,'13,20,25,32',0,10250,30,100,43,27)    
    
    # GOLF 3-CH
    # _, output_devision = aid.create_ai_file (0x0231,'./videos/input/golf/outdoor/2024_09_21_12_54_00', './videos/output/golf',101,'13,26,38',-1000,1200,15,'160,130,100',14,25,'1920,1920,1920','1080,1080,1080')      # right-hand
    # _, output_devision = aid.create_ai_file (0x0231,'./videos/input/golf/outdoor/2024_09_18_16_02_13', './videos/output/golf',101,'13,26,38',-1000,1200,15,'160,130,100',26,23,'1920,1920,1920','1080,1080,1080')      # right-hand
    #     
    # Baseball 3-CH, 180p
    # _, output_devision = aid.create_ai_file (0x0231,'./videos/input/baseball/kbo/2025_04_06_14_35_43', './videos/output/golf',27,'12,13,11',-1000,1000,30,'130,130,130',60,44,'1920,960,0','540,540,540')

    # Baseball 1-CH, 180p
    # _, output_devision = aid.create_ai_file (0x0211,'./videos/input/baseball/kbo/2025_04_06_14_35_43', './videos/output/golf',27,12,-1000,1000,30,100,60,44,960,540)
    


    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # Post Stabil
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────      
    # Input
    # 1. 
    # 2. file folder : './videos/input/golf/outdoor/2024_04_28_13_18_33'
    # 3. output folder : './videos/output/golf'
    # 4. ip class : 101; -> camera ip class
    # 5. analysis cameras id : 11,25,38,55,70 "from the left, clockwise"
    # 6. start clip time : -1000 -> from "start_time"
    # 7. end clip time : 1000 -> from "end_time"
    # 8. fps : 10 -> from "fps"
    # 9. zoom scale : 1.0 -> from "zoom_ratio"
    # 10. selected time : 508 [from 0 to selected timing file]
    # 11. selected frame : 30 [from 0 to selected timing] : default = -1
    #
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────    
    '''
    swipeperiod = [{'no': 0, 'start': 120, 'end': 151, 'target_x': 960, 'target_y': 540, 'zoom': 100, 'roi_left': 1315.6, 'roi_top': 772.9, 'roi_width': 236.3, 'roi_height': 427.5}, {'no': 1, 'start': 248, 'end': 271, 'target_x': 960, 'target_y': 540, 'zoom': 100, 'roi_left': 2515.9, 'roi_top': 369.2, 'roi_width': 252.3, 'roi_height': 642.6}]
    output_file = aid.create_ai_poststabil('./videos/output/baseball/Main(2)_2025_07_10_18_53_15.mp4', './videos/output/baseball/Main(2)_2025_07_10_18_53_15_s.mp4',False,None,swipeperiod)
    '''

    
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────  
    # 4Dist Create Clips (Calibration)
    # ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────      
    # Input Sample Structure       
    # Cameras sample
    Cameras = [
        {
            "input_path": "./videos/input/4dist/2025_09_12_12_12_14",
            "ip_class": 9,
            "cam_ip": 11,
            "channel": 1,
            "audio": False,
            "video": True
        },
        {
            "input_path": "./videos/input/4dist/2025_09_12_12_12_14",
            "ip_class": 9,
            "cam_ip": 12,
            "channel": 2,
            "audio": False,
            "video": True
        },
        {
            "input_path": "./videos/input/4dist/2025_09_12_12_12_14",
            "ip_class": 9,
            "cam_ip": 13,
            "channel": 3,
            "audio": True,
            "video": False
        },
        {
            "input_path": "./videos/input/4dist/2025_09_12_12_12_14",
            "ip_class": 9,
            "cam_ip": 14,
            "channel": 4,
            "audio": False,
            "video": True
        }
    ]
    # Markers sample
    Markers = [
        {
            "start_time": 6,
            "start_frame": 20,
            "end_time": 18,
            "end_frame": 21
        },
        {
            "start_time": 26,
            "start_frame": 7,
            "end_time": 38,
            "end_frame": 4
        }
    ]
    # Adjust sample
    AdjustData = [
        {
            "Adjust": {
                "imageWidth": 1920.0,
                "imageHeight": 1080.0,
                "normAdjustX": -0.026712686146209388,
                "normAdjustY": -0.025846071294284758,
                "normRotateX": 0.6296532879954497,
                "normRotateY": 0.6563974635486297,
                "dAdjustX": -27.351913452148438,
                "dAdjustY": -13.015625,
                "dAngle": -89.31470489501953,
                "dRotateX": 1096.8868408203125,
                "dRotateY": 762.0147705078125,
                "dScale": 1.0,
                "rtMargin": {"X": 168.0, "Y": 94.0, "Width": 1662.0, "Height": 935.0}
            },
            "LiveIndex": 1,
            "ReplayIndex": 1,
            "ReplayGroup": 1,
            "DscID": "009011",
            "Mode": "2D",
            "flip": False,
            "UseLogo": True
        },
        {
            "Adjust": {
                "imageWidth": 1920.0,
                "imageHeight": 1080.0,
                "normAdjustX": 0.013864120484296647,
                "normAdjustY": 0.06513044802101661,
                "normRotateX": 0.5898999743226346,
                "normRotateY": 0.5692895246699532,
                "dAdjustX": 22.585617584553052,
                "dAdjustY": 61.81161167842249,
                "dAngle": -89.9959945678711,
                "dRotateX": 1048.1300048828125,
                "dRotateY": 690.4188232421875,
                "dScale": 0.9477235858270707,
                "rtMargin": {"X": 168.0, "Y": 94.0, "Width": 1662.0, "Height": 935.0}
            },
            "LiveIndex": 2,
            "ReplayIndex": 2,
            "ReplayGroup": 1,
            "DscID": "009012",
            "Mode": "2D",
            "flip": False,
            "UseLogo": True
        },
        {
            "Adjust": {
                "imageWidth": 1920.0,
                "imageHeight": 1080.0,
                "normAdjustX": -0.02328487195233757,
                "normAdjustY": -0.026793776340592258,
                "normRotateX": 0.625011751654633,
                "normRotateY": 0.6559485450785428,
                "dAdjustX": -51.451662621417086,
                "dAdjustY": -32.86457019745808,
                "dAngle": -89.37074279785156,
                "dRotateX": 1118.3616943359375,
                "dRotateY": 780.1870727539062,
                "dScale": 0.9489832685688369,
                "rtMargin": {"X": 168.0, "Y": 94.0, "Width": 1662.0, "Height": 935.0}
            },
            "LiveIndex": 3,
            "ReplayIndex": 3,
            "ReplayGroup": 1,
            "DscID": "009013",
            "Mode": "2D",
            "flip": False,
            "UseLogo": True
        },
        {
            "Adjust": {
                "imageWidth": 1920.0,
                "imageHeight": 1080.0,
                "normAdjustX": 0.03574320842499624,
                "normAdjustY": -0.010018643465909092,
                "normRotateX": 0.5671973934242441,
                "normRotateY": 0.6405700357202541,
                "dAdjustX": 54.77375793457031,
                "dAdjustY": -14.37677001953125,
                "dAngle": -89.8168716430664,
                "dRotateX": 1014.7611694335938,
                "dRotateY": 763.3759155273438,
                "dScale": 1.0,
                "rtMargin": {"X": 168.0, "Y": 94.0, "Width": 1662.0, "Height": 935.0}
            },
            "LiveIndex": 4,
            "ReplayIndex": 4,
            "ReplayGroup": 1,
            "DscID": "009014",
            "Mode": "2D",
            "flip": False,
            "UseLogo": True
        }
    ]
    
    # create calibration files
    # FHD, H.264, 30fps, 5Mbit, GOP:30
    # output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university_fhd.png','FHD','H.264',30,'5M',30)
    #output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university.png','FHD','H.264',30,'5M',30)
    #output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university.png','HD','H.265',30,1000,30,'combined')
    #output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university.png','FHD','H.265',30,'5M',30)
    # FHD, H.264, 30fps, 5Mbit, GOP:30, No logo
    # output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','','FHD','H.264',30,'5M',30)
    # FHD, original, 30fps, 2Mbit, GOP:30, No logo
    # output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','','HD','H.265',30,'2M',30)
    # FHD, original, 30fps, 2Mbit, GOP:30, with logo
    #output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university.png','HD','H.264',30,'2M',30)
    # HD, H.264, 30fps, 5Mbit, GOP:30
    #output_file = aid.create_ai_calibration_multi (Cameras,Markers,AdjustData,'yoga','./videos/output/4dist','./images/wonkwang_university.png','HD','H.265',30,'2M',30)
    
    aid.stop()

    # Exit with the proper code
    sys.exit(exit_code)