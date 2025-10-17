# ─────────────────────────────────────────────────────────────────────────────#
# /A/I/D/
# fd_create_clip.py
# - 2025/06/25
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import os
import shutil
import time
import threading
import bisect
import subprocess
import numpy as np
from datetime import datetime
import fd_utils.fd_config       as conf
from fd_utils.fd_logging        import fd_log
from collections                import deque


# ─────────────────────────────────────────────────────────────────────────────##
# def get_trimmed_frames():
# owner : hongsu jung
# date : 2025-06-05
# 1️⃣ step 1: 각 cam 영상 개별 저장
# ─────────────────────────────────────────────────────────────────────────────##
def get_trimmed_frames():
    
    trimed_frames = {}
    start_time = conf._create_file_time_start
    end_time = conf._create_file_time_end

    ###########################################################
    # pause memory buffers
    ###########################################################
    for type_target, _ in sorted(conf._live_mem_buffer_status.items()):
        rtsp_url = conf._live_mem_buffer_addr[type_target]
        conf._live_mem_buffer_status[type_target] = conf._live_mem_buffer_status_pause
        conf.fd_dashboard(conf._player_type.buf, rtsp_url, conf._live_mem_buffer_status[type_target], f"change status")

    ###########################################################
    # get trimed buffer
    ###########################################################
    for type_target, _ in sorted(conf._live_mem_buffer.items()):
        mem_buffer = conf._live_mem_buffer[type_target]
        # deque 에서 직접 timestamp filter → 메모리 최적화
        trimed_frames[type_target] = [
            frame for ts, frame in mem_buffer
            if start_time <= ts <= end_time
        ]

    ###########################################################
    # resume memory buffers
    ###########################################################
    for type_target, _ in conf._live_mem_buffer_status.items():        
        rtsp_url = conf._live_mem_buffer_addr[type_target]
        conf._live_mem_buffer_status[type_target] = conf._live_mem_buffer_status_record
        conf.fd_dashboard(conf._player_type.buf, rtsp_url, conf._live_mem_buffer_status[type_target], f"change status")
    
    return trimed_frames


# ─────────────────────────────────────────────────────────────────────────────##
# def create_4split_video_ffmpeg(input_dir, output_path, bitrate='5M'):
# owner : hongsu jung
# date : 2025-06-05
# ─────────────────────────────────────────────────────────────────────────────##
def create_4split_video_ffmpeg(input_dir, output_path, bitrate='80M'):

    temp_output_file = os.path.join("R:\\", "merged_output.mp4")
    fd_log.info(f"[DEBUG] tmp_video_dir 내 mp4 파일 목록:")
    cam_files = sorted([
        os.path.join(input_dir, f)
        for f in os.listdir(input_dir)
        if f.endswith(".mp4") and not f.startswith("shorts_")
    ])
    for f in cam_files:
        fd_log.info(" -", f)
    assert len(cam_files) == 4, "Exactly 4 input videos required"

    fd_log.info("⚡ Using FFmpeg for fast 2x2 video merging...")

    # ✅ CPU-safe 필터 사용
    filter_graph = (
        '[0:v]scale=960:540[p1];'
        '[1:v]scale=960:540[p2];'
        '[2:v]scale=960:540[p3];'
        '[3:v]scale=960:540[p4];'
        '[p1][p2]hstack=inputs=2[top];'
        '[p3][p4]hstack=inputs=2[bottom];'
        '[top][bottom]vstack=inputs=2[out]'
    )

    # GPU, GOP30
    '''
    command = [
        'ffmpeg',
        '-i', cam_files[3], # 3  -> 22
        '-i', cam_files[0], # 10 -> 3
        '-i', cam_files[2], # 15 -> 15
        '-i', cam_files[1], # 22 -> 10
        '-filter_complex', filter_graph,
        '-map', '[out]',
        '-r', '30',
        '-c:v', str(conf._output_codec),
        '-b:v', bitrate,
        '-rc', 'constqp',
        '-qp', '20',
        '-g', '30',        # ✅ GOP: 30
        '-bf', '2',        # ✅ B-frames: 2
        '-preset', 'p4',
        '-forced-idr', '1',
        '-y', temp_output_file
    ]
    '''
    # CPU, GOP 1
    command = [
        "ffmpeg",
        "-i", cam_files[3],  # 3  -> 22
        "-i", cam_files[0],  # 10 -> 3
        "-i", cam_files[2],  # 15 -> 15
        "-i", cam_files[1],  # 22 -> 10
        "-filter_complex", filter_graph,
        "-map", "[out]",
        "-r", "30",
        "-c:v", "libx264",
        "-b:v", bitrate,
        "-g", "1",
        "-bf", "0",
        "-keyint_min", "1",
        "-x264-params", "scenecut=0",
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",
        "-preset", "ultrafast",        # ✅ valid preset
        "-y", temp_output_file
    ]


    fd_log.info("▶️ GPU Encoding...")
    t_start = time.perf_counter()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        fd_log.error("❌ FFmpeg 실패")
        fd_log.print("▶️ COMMAND:", " ".join(command))
        fd_log.print("STDOUT:", result.stdout)
        fd_log.print("STDERR:", result.stderr)
        raise RuntimeError("FFmpeg ??")

    t_end = time.perf_counter()
    fd_log.info(f"✅ 로컬에 저장 완료: {temp_output_file} (처리 시간: {t_end - t_start:.2f}s)")

    # copy to remote
    if os.path.exists(output_path):
        os.remove(output_path)
    shutil.copy2(temp_output_file, output_path)
    os.remove(temp_output_file)

    for path in cam_files:
        try:
            os.unlink(path)
        except PermissionError as e:
            fd_log.warning(f"⚠️ 삭제 실패: {path} - {e}")

# ─────────────────────────────────────────────────────────────────────────────##
# def play_and_create_multi_clips()
# owner : hongsu jung
# date : 2025-06-06
# PyAV + OpenCV 4분할 예시
# ─────────────────────────────────────────────────────────────────────────────#
def play_and_create_multi_clips():

    ####################################################
    # get each frames from buffer
    ####################################################
    trimed_frames = get_trimmed_frames()
    if len(trimed_frames) == 0:
        fd_log.error("❌ no trimed frames : def play_and_create_clip_nascar():")
        return
    
    ####################################################
    # send the frmaes to thread UI windows
    ####################################################
    with conf._live_player_lock:
        conf._live_player_multi_ch_frames = trimed_frames
        conf._live_is_paused = False  # 초기 상태는 재생 중
    fd_log.info("✅ Frames updated. Playing... (player thread is running)")


    
