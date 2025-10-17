# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# calibration file handling
# - 2025/09/13
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
# L/O/G/
# check     : ✅
# warning   : ⚠️
# error     : ❌
# ─────────────────────────────────────────────────────────────────────────────#

import os
import cv2
import numpy as np
import fd_utils.fd_config as conf

from fd_utils.fd_logging import fd_log
from fd_utils.fd_file_edit import fd_set_mem_file_calis
from fd_utils.fd_file_edit import fd_set_mem_file_calis_audio
from fd_utils.fd_file_edit import fd_combine_calibrated_output

# calibration imports
from typing import List, Dict, Any, Optional
from collections import defaultdict
from copy import deepcopy

def file_exist(file):    
    if os.path.exists(file): 
        fd_log.info("exist file : [{0}]".format(file))
        return True
    return False

# ─────────────────────────────────────────────────────────────────────────────
# def create_video_each_camera
# [owner] hongsu jung
# [date] 2025-09-13
# PD로 부터 받은 카메라 정보로 부터, 각각 Markers 구간에 맞춰서, AdjustData를 적용한, 영상을 만든다.
# Parameters:
#   files (list of str): 로드할 .pkl 파일 경로 리스트
#   arrays (list): 로드된 데이터를 저장할 리스트 (참조로 전달됨)
# ─────────────────────────────────────────────────────────────────────────────
def create_video_each_camera(Cameras, AdjustData, Markers):
    # Group sets by time
    time_groups = build_marker_camera_sets_by_time(Cameras, AdjustData, Markers)
    if not time_groups:
        fd_log.error("❌ No calibration sets created from input data")
        return False    
        
    # Initialize nested structures:
    # conf._thread_file_calibration[cal_idx][cam_idx] -> Thread or None    
    conf._thread_file_calibration = [
        [None for _ in range(len(cs["camera_set"])+1)] for cs in time_groups
    ]  # Pre-allocate thread slots per calibration set and per camera
    conf._shared_audio_filename = [None] * len(time_groups)
    conf._time_group_count = len(time_groups)

    # Create file for each calibration set
    for idx, time_group in enumerate(time_groups):
        fd_log.info(f"================= Marker {time_group} / {len(time_groups)} =================")
        create_video_set_by_time(idx, time_group)

    # waiting until finish create videos
    # 모든 set의 모든 카메라 쓰레드가 끝날 때까지 대기
    for tg_index, time_group in enumerate(conf._thread_file_calibration):
        for cam_index in range(len(time_group)):  # 하나 더
            if conf._thread_file_calibration[tg_index][cam_index] is not None:
                fd_log.info(f"✅ Waiting Thread Finish Time Group:{tg_index}, Camera:{cam_index}")
                conf._thread_file_calibration[tg_index][cam_index].join()   

    # check combine output
    if conf._output_individual == False:
        camera_groups = build_marker_camera_sets_by_camera(Cameras, Markers)
        fd_combine_calibrated_output(camera_groups)

    fd_log.info("🚩All Thread Finished")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# def build_marker_camera_sets_by_time(
# [owner] hongsu jung
# [date] 2025-09-13
# PD로 부터 받은 카메라 정보로 부터, 각각 Markers 구간에 맞춰서, AdjustData를 적용한, 영상을 만든다.
# Parameters:
#   files (list of str): 로드할 .pkl 파일 경로 리스트
#   arrays (list): 로드된 데이터를 저장할 리스트 (참조로 전달됨)
# ─────────────────────────────────────────────────────────────────────────────
def build_marker_camera_sets_by_time(
    Cameras: List[Dict[str, Any]],
    AdjustData: List[Dict[str, Any]],
    Markers: List[Dict[str, Any]],    
    *,
    sort_cameras_by: Optional[str] = "channel",  # 필요 시 정렬 기준 키 지정 (None이면 정렬 안 함)
    copy_items: bool = True,                     # 원본 변형 방지용 deepcopy
) -> List[Dict[str, Any]]:
    """
    video, audio 여부에 상관없이 모든 Cameras를 사용한다.
    반환 구조:
    [
      {
        "marker_index": <int>,
        "marker": {
            "start_time": ...,
            "start_frame": ...,
            "end_time": ...,
            "end_frame": ...
        },
        "camera_set": [ {모든 카메라...} ],
        "adjust_set": [ AdjustData 전체(한 세트) ]
      },
      ...
    ]
    """
    # 1) Camera-Set 구성: video/audio 상관없이 전체 사용
    camera_set = list(Cameras)

    # 선택 정렬 (키가 숫자/문자 섞여도 비교 가능하도록)
    if sort_cameras_by is not None:
        def _key(c):
            v = c.get(sort_cameras_by)
            try:
                return (0, int(v))
            except Exception:
                # None은 항상 뒤로 가도록 보정
                return (2, "") if v is None else (1, str(v))
        camera_set = sorted(camera_set, key=_key)

    # 2) AdjustData는 Camera-Set과 한 세트로 그대로 묶음
    adjust_set = AdjustData

    # 3) 각 Marker마다 구조체 생성
    out: List[Dict[str, Any]] = []
    for idx, m in enumerate(Markers):
        marker_obj = {
            "start_time": m.get("start_time"),
            "start_frame": m.get("start_frame"),
            "end_time": m.get("end_time"),
            "end_frame": m.get("end_frame"),
        }
        item = {
            "marker_index": idx,
            "marker": deepcopy(marker_obj) if copy_items else marker_obj,
            "camera_set": deepcopy(camera_set) if copy_items else camera_set,
            "adjust_set": deepcopy(adjust_set) if copy_items else adjust_set,
        }
        out.append(item)

    return out


def _time_key(t: Any) -> Any:
    """시간 동등성 판정 키. 필요시 양자화 규칙 적용 가능."""
    return t  # 예: float면 round(t, 3) 등으로 교체

# ─────────────────────────────────────────────────────────────────────────────
# def build_marker_camera_sets_by_camera(
# [owner] hongsu jung
# [date] 2025-09-22
# PD로 부터 받은 카메라 정보로 부터, 각각 Markers 구간에 맞춰서, AdjustData를 적용한, 영상을 만든다.
# Parameters:
#   files (list of str): 로드할 .pkl 파일 경로 리스트
#   arrays (list): 로드된 데이터를 저장할 리스트 (참조로 전달됨)
# ─────────────────────────────────────────────────────────────────────────────
def build_marker_camera_sets_by_camera(
    Cameras: List[Dict[str, Any]],
    Markers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Cameras의 channel 값들로 그룹을 만들고,
    각 channel 항목에 Markers의 start_time 배열(모든 채널 동일)을 넣어 반환.

    반환 예:
    [
        {"channel": 1, "start_times": [6, 26]},
        {"channel": 2, "start_times": [6, 26]},
        {"channel": 3, "start_times": [6, 26]},
        {"channel": 4, "start_times": [6, 26]},
    ]
    """
    # 1) 카메라에서 channel 목록 (video == True 인 것만)
    channels = sorted({
        cam.get("channel") 
        for cam in Cameras 
        if cam.get("channel") is not None and cam.get("video") is True
    })

    # 2) Markers에서 start_time 배열 추출 (입력 순서 유지)
    start_times = [
        m["start_time"] 
        for m in Markers 
        if "start_time" in m and m["start_time"] is not None
    ]

    # 3) 채널별로 동일한 start_times를 할당
    result = [{"channel": ch, "start_times": list(start_times)} for ch in channels]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# def create_video_set_by_time(
# [owner] hongsu jung
# [date] 2025-09-13
# Maker 기준으로 start/end time에서의 카메라 그룹을 받아서, Video/Audio option에 따른 영상 제작
# ─────────────────────────────────────────────────────────────────────────────
def create_video_set_by_time(tg_index, cal_set):

    # check marker
    market_set = cal_set.get("marker", {})
    idx = cal_set.get("marker_index", -1)   
    start_time  = market_set.get("start_time")
    start_frame = market_set.get("start_frame")
    end_time    = market_set.get("end_time")    
    end_frame   = market_set.get("end_frame")
    
    # camera set
    cam_set = cal_set.get("camera_set", [])
    # adjust set
    adjust_set = cal_set.get("adjust_set", [])

    # time set
    fd_log.info(f"🕒 Marker {idx}: time {start_time} ~ {end_time}, frame {start_frame} ~ {end_frame}")

    # check audio camera
    is_shared_audio = False
    audio_cam = [c for c in cam_set if c.get("audio") is True]
    if audio_cam:
        is_shared_audio = True
        fd_log.info(f"✅ Audio camera found: {len(audio_cam)}")
    # create audio sound
    if is_shared_audio:
        conf._calibration_multi_audio_file = create_audio_file(tg_index, audio_cam[0], start_time, end_time, start_frame, end_frame)

    is_check_input_file_info = False

    # debug
    cam_set_count = len(cam_set)
    center_idx = cam_set_count // 2   # 8

    # create each files
    for idx, cam in enumerate(cam_set):
        cam_vidio       = cam.get("video", True)
        cam_audio       = cam.get("audio", False)
        cam_ip_class    = cam.get("ip_class", 0)
        cam_ip          = cam.get("cam_ip", "")
        channel         = cam.get("channel", -1)
        file_path       = cam.get("input_path", None)
        conf._folder_input = file_path

        fd_log.info(f"--- Camera {idx}: channel={channel}, video={cam_vidio}, audio={cam_audio}, time=[{start_time}:{start_frame}~{end_time}:{end_frame}] file={file_path}")
        if not cam_vidio:
            fd_log.info(f"⚠️ Camera {idx} has no video, skipping.")
            continue
        if not file_path or not os.path.exists(file_path):
            fd_log.warning(f"⚠️ Camera {idx} video file not found: {file_path}, skipping.")
            continue  
        # test code
        # debug time group
        '''
        if idx != 5:
            fd_log.warning(f"[DEBUG] skip not center camera [{idx}/[{cam_set_count}]")
            continue
        '''
        #   
        # check exist file
        if is_check_input_file_info == False or is_shared_audio == False:    
            file_directory   = file_path
            file_base        = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, cam_ip_class, int(cam_ip), start_time)            
            if file_exist(file_base) is False:
                return False, "", ""
            # get input file info    
            cap = cv2.VideoCapture(file_base)
            conf._input_fps             = cap.get(cv2.CAP_PROP_FPS)
            conf._input_frame_count     = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            conf._input_width           = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
            conf._input_height          = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)    
            cap.release()
            is_check_input_file_info = True

        # ─────────────────────────────────────────────────────────────────────────────
        # Get Previous Time and Frame (Empty frame)
        # ─────────────────────────────────────────────────────────────────────────────
        fd_set_mem_file_calis(file_path, cam_ip_class, cam_ip, channel, tg_index, idx, start_time, start_frame, end_time, end_frame, adjust_set[idx], cam_audio)

# ─────────────────────────────────────────────────────────────────────────────
# def create_audio_file(audio_cam, start_time, end_time, start_frame, end_frame)
# [owner] hongsu jung
# [date] 2025-09-13
# 기준이 되는 audio file을 만들어서 다른 채널의 영상과 공유.
# ─────────────────────────────────────────────────────────────────────────────
def create_audio_file(tg_index, cam, start_time, end_time, start_frame, end_frame):
    cam_vidio       = cam.get("video", True)
    cam_audio       = cam.get("audio", False)
    cam_ip_class    = cam.get("ip_class", 0)
    cam_ip          = cam.get("cam_ip", "")
    channel         = cam.get("channel", -1)
    file_path       = cam.get("input_path", None)
    fd_log.info(f"--- Audio Camera: channel={channel}, video={cam_vidio}, audio={cam_audio}, file={file_path} ---")
    if not cam_audio:
        fd_log.error(f"⚠️ Camera is not audio, skipping.")
        return None
    if not file_path or not os.path.exists(file_path):
        fd_log.error(f"⚠️ Camera file not found: {file_path}, skipping.")
        return None 
    # check exist file
    file_directory   = file_path
    file_base        = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, cam_ip_class, int(cam_ip), start_time)            
    if file_exist(file_base) is False:
        return False, "", ""
    # get input file info    
    cap = cv2.VideoCapture(file_base)
    conf._input_fps             = cap.get(cv2.CAP_PROP_FPS)
    conf._input_frame_count     = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    conf._input_width           = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    conf._input_height          = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)    
    cap.release()

    # ─────────────────────────────────────────────────────────────────────────────
    # Get Previous Time and Frame (Empty frame)
    # ─────────────────────────────────────────────────────────────────────────────
    audio_file = fd_set_mem_file_calis_audio(file_path, cam_ip_class, cam_ip, channel, tg_index, start_time, start_frame, end_time, end_frame)
    return audio_file


