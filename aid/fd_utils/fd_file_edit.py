# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# video file editing
# - 2024/11/1
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
# L/O/G/
# check     : ✅
# warning   : ⚠️
# error     : ❌
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import re
import av
import io
import os
import gc
import math
import time
import pickle
import glob
import subprocess
import json
import tempfile
import psutil
import win32api
import win32process
import win32con
import shutil
import ffmpeg
import uuid
import shlex
import random
import sys, platform

import threading
import numpy as np
import fd_utils.fd_config as conf

from collections import deque
from pathlib import Path
from typing import Tuple, Optional, List
from fd_utils.fd_logging import fd_log
from fd_utils.fd_file_util      import fd_format_elapsed_time

from fd_detection.fd_detect     import fd_get_video_on_player
from concurrent.futures         import ProcessPoolExecutor
    
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime, timedelta


def file_copy(file_org, file_dest):
    try:
        if not os.path.isfile(file_org):
            fd_log.error(f"❌ Source file does not exist: {file_org}")
            return False

        dest_dir = os.path.dirname(file_dest)
        if dest_dir and not os.path.exists(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)

        shutil.copy2(file_org, file_dest)  # metadata까지 복사
        # fd_log.info(f"✅ File copied: {file_org} → {file_dest}")
        return True

    except Exception as e:
        fd_log.error(f"❌ Error copying file: {e}")
        return False

def file_exist(file):    
    if os.path.exists(file): 
        fd_log.info("exist file : [{0}]".format(file))
        return True
    return False

def fd_file_delete(file):
    try:
        if os.path.exists(file):
            os.remove(file)
    except Exception as e:
        fd_log.warning(f"⚠️ Failed to delete {file}: {e}")

def fd_save_array_file(file, array):
    with open(file, "wb") as f:
        pickle.dump(array, f)

def fd_load_array_file(file):
    # Step 1: Load the saved 3D pose data
    with open(file, "rb") as f:        
        return pickle.load(f)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_common_ffmpeg_args():
# [owner] hongsu jung
# [date] 2025-07-01
# 각 파일을 로드하여 arrays 리스트에 append합니다.
# Parameters:
#   files (list of str): 로드할 .pkl 파일 경로 리스트
#   arrays (list): 로드된 데이터를 저장할 리스트 (참조로 전달됨)
# ─────────────────────────────────────────────────────────────────────────────
def fd_load_array_files(files, arrays):
    for file in files:
        try:
            if not os.path.exists(file):
                fd_log.info(f"[WARNING] File does not exist: {file}")
                continue
            with open(file, "rb") as f:
                data = pickle.load(f)
                arrays.append(data)
        except FileNotFoundError:
            fd_log.info(f"[ERROR] File not found: {file}")
        except pickle.UnpicklingError:
            fd_log.info(f"[ERROR] Failed to unpickle file (possibly corrupted): {file}")
        except Exception as e:
            fd_log.info(f"[ERROR] Unexpected error while loading file {file}: {e}")
    
# ─────────────────────────────────────────────────────────────────────────────
# def fd_common_ffmpeg_args():
# [owner] hongsu jung
# [date] 2025-05-18
# ─────────────────────────────────────────────────────────────────────────────
def fd_common_ffmpeg_args_pre():
    return [        
        "ffmpeg","-y",
        "-hwaccel", "cuda",      
        "-loglevel", "debug",
    ]
# -i options
# -vf options
def fd_common_ffmpeg_args_post(with_sound = False):
    args = [        
        "-c:v", "copy",        
        "-map", "0:v:0?", "-map", "0:a:0?",
        # If writing to a file:
        "-movflags", "+faststart",
        # If writing to a pipe/stream:
        # "-movflags", "+frag_keyframe+empty_moov",
        "-bsf:a", "aac_adtstoasc",   # only if AAC in ADTS → MP4
        "-f", "mp4",
    ]

    # 오디오 제거는 with_sound=False일 때만 적용
    if not with_sound:
        args.append("-an")
    else:
        args.append("-c:a") 
        args.append("copy")

    return args

# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_clean_file(file_type: int) -> str:
# [owner] hongsu jung
# [date] 2025-05-19
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_clean_file(file_type: int) -> str:
    filename_map = {
        conf._file_type_prev:   f"[1-1]clean_prev_{conf._unique_process_name}.mp4",
        conf._file_type_curr:   f"[1-2]clean_curr_{conf._unique_process_name}.mp4",
        conf._file_type_post:   f"[1-3]clean_post_{conf._unique_process_name}.mp4",
        conf._file_type_last:   f"[1-4]clean_last_{conf._unique_process_name}.mp4",
        conf._file_type_front:  f"[1-1]clean_front_{conf._unique_process_name}.mp4",
        conf._file_type_side:   f"[1-2]clean_side_{conf._unique_process_name}.mp4",
        conf._file_type_back:   f"[1-3]clean_back_{conf._unique_process_name}.mp4",                
    }
    filename = filename_map.get(file_type, f"output_{file_type:02X}.mp4")
    output_file = os.path.join("R:\\", filename)
    return output_file

# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_clean_file(file_type: int) -> str:
# [owner] hongsu jung
# [date] 2025-05-19
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_cali_file(file_type: int, file_directory, t_start = 0, cam_index = 0, with_sec = True) -> str:

    # 문자열 → datetime 객체 변환
    timename = os.path.basename(file_directory)   # "2025_09_12_12_12_14"
    dt = datetime.strptime(timename, "%Y_%m_%d_%H_%M_%S")
    # 초 더하기
    dt_new = dt + timedelta(seconds=t_start)

    # 연월일시분초 추출
    year = dt_new.year
    month = dt_new.month
    day = dt_new.day
    hour = dt_new.hour
    minute = dt_new.minute
    second_new = dt_new.second

    if with_sec:
        base_name = f"{str(year).zfill(4)}{str(month).zfill(2)}{str(day).zfill(2)}_{str(hour).zfill(2)}{str(minute).zfill(2)}{str(second_new).zfill(2)}"
    else:
        base_name = f"{str(year).zfill(4)}{str(month).zfill(2)}{str(day).zfill(2)}_{str(hour).zfill(2)}{str(minute).zfill(2)}"
    filename_map = {
        conf._file_type_cali        : f"{conf._calibration_multi_prefix}_{base_name}_{cam_index:02}.mp4",
        conf._file_type_cali_audio  : f"{conf._calibration_multi_prefix}_{base_name}.{conf._output_shared_audio_type}",
    }
    filename = filename_map.get(file_type, f"output_{file_type:02X}.mp4")
    output_file = os.path.join("R:\\", filename)

    #print(f"{year}/{month}/{day},{hour}:{minute}:{second_new} -> {output_file}")    
    return output_file

# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_output_file(file_type: int) -> str:
# [owner] hongsu jung
# [date] 2025-05-19
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_output_file(file_type: int) -> str:
    filename_map = {
        conf._file_type_prev:   f"[2-1]output_prev_{conf._unique_process_name}.mp4",
        conf._file_type_curr:   f"[2-2]output_curr_{conf._unique_process_name}.mp4",
        conf._file_type_post:   f"[2-3]output_post_{conf._unique_process_name}.mp4",
        conf._file_type_last:   f"[2-4]output_last_{conf._unique_process_name}.mp4",
        conf._file_type_front:  f"[2-1]output_front_{conf._unique_process_name}.mp4",
        conf._file_type_side:   f"[2-2]output_side_{conf._unique_process_name}.mp4",
        conf._file_type_back:   f"[2-3]output_back_{conf._unique_process_name}.mp4",
        conf._file_type_overlay:f"output_png_{conf._unique_process_name}.png"
    }
    filename = filename_map.get(file_type, f"output_{file_type:02X}.mp4")
    output_file = os.path.join("R:\\", filename)
    return output_file

# ─────────────────────────────────────────────────────────────────────────────
# def fd_cleanup_temp_all():
# [owner] hongsu jung
# [date] 2025-05-19
# ─────────────────────────────────────────────────────────────────────────────
def fd_clean_up():

    '''
    fd_cleanup_temp_file(conf._file_type_prev)
    fd_cleanup_temp_file(conf._file_type_curr)
    fd_cleanup_temp_file(conf._file_type_post)
    fd_cleanup_temp_file(conf._file_type_last)
    fd_cleanup_temp_file(conf._file_type_front)
    fd_cleanup_temp_file(conf._file_type_side)
    fd_cleanup_temp_file(conf._file_type_back)
    fd_cleanup_temp_file(conf._file_type_overlay)    
    '''
    # erase all files in ram disk
    # 2025-10-15
    fd_cleanup_ram_disk()

# ─────────────────────────────────────────────────────────────────────────────
# def fd_cleanup_ram_disk():
# [owner] hongsu jung
# [date] 2025-10-15
# ─────────────────────────────────────────────────────────────────────────────
def fd_cleanup_ram_disk():
    ram_disk = r"R:\\"
    if not os.path.exists(ram_disk):
        print(f"RAM Disk not found: {ram_disk}")
        return

    for item in os.listdir(ram_disk):
        if item in ("$Recycle.Bin", "System Volume Information"):
            continue  # 시스템 폴더는 건너뜀

        path = os.path.join(ram_disk, item)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception as e:
            print(f"Failed to delete {path}: {e}")

    print("✅ RAM Disk cleaned up successfully.")

# ─────────────────────────────────────────────────────────────────────────────
# def fd_cleanup_temp_file(file_type):
# [owner] hongsu jung
# [date] 2025-05-19
# ─────────────────────────────────────────────────────────────────────────────
def fd_cleanup_temp_file(file_type):
    # clean file
    path = fd_get_clean_file(file_type)
    if path and os.path.exists(path):
        try:
            os.remove(path)
            fd_log.info(f"🧹 Clean file deleted: {path}")
        except Exception as e:
            fd_log.error(f"❌ Clean file delete failed: {e}")
    # output file
    path = fd_get_output_file(file_type)
    if path and os.path.exists(path):
        try:
            os.remove(path)
            fd_log.info(f"🧹 Draw file deleted: {path}")
        except Exception as e:
            fd_log.error(f"❌ Draw file delete failed: {e}")
  
# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_datetime(file_directory):
# [owner] hongsu jung
# [date] 2025-03-16
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_datetime(file_directory):
    
    # 경로에서 마지막 부분(파일 또는 폴더명) 추출
    last_part = os.path.basename(file_directory)  # '2025_03_14_13_00_54'
    # 숫자 추출 후 datetime 변환
    numbers = list(map(int, re.findall(r'\d+', last_part)))
    dt = datetime(*numbers)  # datetime(2025, 3, 14, 13, 0, 54)
    # add seconds
    dt_selected = dt + timedelta(seconds = conf._selected_moment_sec)

    str_datetime = dt_selected.strftime("%Y_%m_%d_%H_%M_%S")
    return str_datetime

# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_clean_file_name(folder_output):
# [owner] hongsu jung
# [date] 2025-07-03
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_clean_file_name(folder_output):

    team_code = conf._team_code
    player_id = conf._player_no
    play_id_pitch = conf._playId_pitch
    play_id_hit = conf._playId_hit

    match conf._type_target:
        case conf._type_baseball_pitcher | conf._type_baseball_pitcher_multi:
            str_output_type = f"[pitcher]"
        case conf._type_baseball_batter_RH | conf._type_baseball_batter_LH:
            str_output_type = f"[batter]"
        case conf._type_baseball_hit | conf._type_baseball_hit_manual | conf._type_baseball_hit_multi:
            str_output_type = f"[hit]"
        case conf._type_golfer_2ch_LND_RH | conf._type_golfer_2ch_LND_LH | \
             conf._type_golfer_3ch_LND_RH | conf._type_golfer_3ch_LND_LH | \
             conf._type_golfer_3ch_POR_RH | conf._type_golfer_3ch_POR_LH:
            str_output_type = "[golfer]"
    
    base = f"{folder_output}/{str_output_type}{conf._output_datetime}"

    file_prev = f"{base}_prev.mp4"
    file_curr = f"{base}_curr.mp4"
    file_post = f"{base}_post.mp4"
    file_last = f"{base}_last.mp4"

    return [file_prev, file_curr, file_post, file_last]

# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_video_info(path: str):
# [owner] hongsu jung
# [date] 2025-07-10
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_video_info(path: str):
    container = av.open(path)
    video_stream = next(s for s in container.streams if s.type == 'video')
    
    fps = float(video_stream.average_rate)
    width = video_stream.codec_context.width
    height = video_stream.codec_context.height
    container.close()
    return fps, width, height

# ─────────────────────────────────────────────────────────────────────────────
# def extract_frames_from_file(path: str, file_type = 0x00):
# [owner] hongsu jung
# [date] 2025-07-10
# ─────────────────────────────────────────────────────────────────────────────
def fd_extract_frames_from_file(path: str, file_type = 0x00):
    pix_fmt = 'bgr24'
    frames = []
    frames_clean = []

    try:
        _t_start = time.perf_counter()

        container = av.open(path, options={'threads': 'auto'})
        stream = container.streams.video[0]
        stream.thread_type = 'AUTO'  # enable threading

        for i, frame in enumerate(container.decode(stream)):
            img = frame.to_ndarray(format=pix_fmt)
            frames.append(img)              # 작업용

        t_end = time.perf_counter()
        elapsed_ms = (t_end - _t_start) * 1000
        fd_log.info(f"5️⃣ [0x{file_type:X}][Extract][🕒:{elapsed_ms:,.2f} ms] Frame count: {len(frames)}")

        del container
        gc.collect()
        return frames

    except Exception as e:
        import traceback
        fd_log.error(f"❌ Error reading video from path: {path}")
        traceback.print_exc()
        return []
    
# ─────────────────────────────────────────────────────────────────────────────
# def save_clean_feed(file_type, file_name):
# [owner] hongsu jung
# [date] 2025-07-04
# ─────────────────────────────────────────────────────────────────────────────
def save_clean_feed(file_type, file_name):
    match file_type:
        case conf._file_type_prev: 
            file_copy(file_name, conf._clean_file_list[0])
        case conf._file_type_curr: 
            file_copy(file_name, conf._clean_file_list[1])
        case conf._file_type_post: 
            file_copy(file_name, conf._clean_file_list[2])
        case conf._file_type_last: 
            file_copy(file_name, conf._clean_file_list[3])
        case _:
            fd_log.info(f"not save clean feed: {file_type}")
        
# ─────────────────────────────────────────────────────────────────────────────
# def fd_get_output_name(folder_output):
# [owner] hongsu jung
# [date] 2025-03-16
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_output_file_name(folder_output):

    team_code = conf._team_code
    player_id = conf._player_no
    play_id_pitch = conf._playId_pitch
    play_id_hit = conf._playId_hit

    match conf._type_target:
        case conf._type_baseball_pitcher:
            #str_output_type = f"[pitcher][{team_code}][{player_id}]"
            str_output_type = f"[pitcher][{play_id_pitch}]"
        case conf._type_baseball_batter_RH:
            #str_output_type = f"[batter-rh][{team_code}][{player_id}]"
            str_output_type = f"[batter-rh][{play_id_hit}]"
        case conf._type_baseball_batter_LH:  
            #str_output_type = f"[batter-lh][{team_code}][{player_id}]"
            str_output_type = f"[batter-lh][{play_id_hit}]"
        case conf._type_baseball_hit | conf._type_baseball_hit_manual:
            #str_output_type = f"[hit][{team_code}][{player_id}]"
            str_output_type = f"[hit][{play_id_hit}]"
        case conf._type_baseball_pitcher_multi:
            str_output_type = f"[pitcher][multi][{team_code}][{player_id}]"
        case conf._type_baseball_hit_multi:
            str_output_type = f"[hit][multi][{team_code}][{player_id}]"
        case conf._type_golfer_2ch_LND_RH | conf._type_golfer_2ch_LND_LH | \
             conf._type_golfer_3ch_LND_RH | conf._type_golfer_3ch_LND_LH | \
             conf._type_golfer_3ch_POR_RH | conf._type_golfer_3ch_POR_LH:
            str_output_type = "[golfer]"
        case conf._type_cricket_batman:  
            str_output_type = "[batman]"
        case conf._type_cricket_baller:  
            str_output_type = "[baller]"    
    
    file_output = f"{folder_output}/{str_output_type}{conf._output_datetime}.mp4"
    return file_output

# ─────────────────────────────────────────────────────────────────────────────
# combine_video(file_list, output_file):
# [owner] hongsu jung
# [date] 2025-03-12
# FFmpeg를 사용하여 비디오 병합
# ─────────────────────────────────────────────────────────────────────────────
def combine_video(file_type, file_list, with_sound=False, is_shared_audio=False):
    """
    - Uses the concat demuxer with a list file for input concatenation.
    - with_sound == False:
        Emits a silent fragmented MP4 to stdout (copies video, drops audio via -an).
    - with_sound == True and is_shared_audio == False:
        (1) Try full stream copy (video+audio). If audio is AAC, apply bitstream filter aac_adtstoasc.
        (2) On failure, re-encode audio to AAC while copying video.
    - with_sound == True and is_shared_audio == True:
        Use the concatenated video and mix with the shared M4A audio (conf._calibration_multi_audio_file).
        Video is copied, audio is encoded to AAC; the audio loops and is cut with -shortest.
    - Returns io.BytesIO on success, or None on failure. Logs up to 4KB of ffmpeg stderr on failure.
    """
    t_start = time.perf_counter()

    if not file_list:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file for merge.")
        return None

    def _run_and_capture(cmd, rm_paths):
        """
        Run ffmpeg, capture stdout/stderr, remove temp files, and return BytesIO on success.
        On failure, log stderr (trimmed) and return None.
        """
        fd_log.info(f"\r1️⃣ [0x{file_type:X}][Merge] command:{cmd}")
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=win32con.HIGH_PRIORITY_CLASS
        )
        out, err = p.communicate()
        # Cleanup temp files
        for pth in (rm_paths if isinstance(rm_paths, (list, tuple)) else [rm_paths]):
            try:
                if pth and os.path.exists(pth):
                    os.remove(pth)
            except Exception:
                pass
        if p.returncode != 0:
            fd_log.info(
                f"\r❌[0x{file_type:X}] ffmpeg merge failed. rc={p.returncode}\n"
                f"STDERR:\n{err.decode('utf-8', 'ignore')[:4000]}"
            )
            return None
        return io.BytesIO(out)

    # Build concat list file (absolute paths, single-quoted)
    with tempfile.NamedTemporaryFile(dir="R:\\", mode='w+', delete=False, suffix='.txt') as tmp:
        for v in file_list:
            tmp.write(f"file '{os.path.abspath(v)}'\n")
        tmp.flush()
        list_path = tmp.name

    if not with_sound:
        # Silent output: copy video, drop audio, fragmented MP4 to stdout
        cmd = [
            'ffmpeg','-v','error',
            '-f','concat','-safe','0','-i', list_path,
            '-c','copy','-an',
            '-movflags','frag_keyframe+empty_moov',
            '-f','mp4','-'
        ]
        out = _run_and_capture(cmd, list_path)
        if out is None: 
            return None

    elif with_sound and not is_shared_audio:
        # Use audio from the input videos
        a_codec = _probe_audio_codec_of_first(file_list)

        # (1) Full copy attempt. If audio is AAC, add the aac_adtstoasc bitstream filter for MP4.
        cmd_copy = [
            'ffmpeg','-v','error',
            '-f','concat','-safe','0','-i', list_path,
            '-map','0:v:0?',
            '-map','0:a:0?',
            '-c:v','copy',
            '-c:a','copy',
            '-movflags','frag_keyframe+empty_moov',
            '-f','mp4','-'
        ]
        # Insert before '-movflags' (which is at index -5 in the current layout)
        if (a_codec or '').lower() == 'aac':
            cmd_copy[-5:-5] = ['-bsf:a', 'aac_adtstoasc']

        out = _run_and_capture(cmd_copy, list_path)

        if out is None:
            # (2) Fallback: re-encode audio to AAC, keep video copy
            with tempfile.NamedTemporaryFile(dir="R:\\", mode='w+', delete=False, suffix='.txt') as tmp2:
                for v in file_list:
                    tmp2.write(f"file '{os.path.abspath(v)}'\n")
                tmp2.flush()
                list_path2 = tmp2.name

            cmd_aac = [
                'ffmpeg','-v','error',
                '-f','concat','-safe','0','-i', list_path2,
                '-map','0:v:0?','-map','0:a:0?',
                '-c:v',"copy",
                '-c:a','aac','-ar','48000','-ac','2','-b:a','192k',
                '-movflags','frag_keyframe+empty_moov',
                '-f','mp4','-'
            ]
            out = _run_and_capture(cmd_aac, list_path2)
            if out is None: 
                return None

    else:
        # Mix with shared audio (M4A) only, ignoring source audios
        audio_src = getattr(conf, "_calibration_multi_audio_file", None)
        if not audio_src or not os.path.exists(audio_src):
            try:
                os.remove(list_path)
            except Exception:
                pass
            fd_log.info(f"\r❌[0x{file_type:X}] shared audio file missing: {audio_src}")
            return None

        # Video from concat list (input 0), audio from shared M4A (input 1)
        # Loop audio with -stream_loop -1 and cut to video length via -shortest.
        # 1) M4A(또는 MP4 컨테이너) → ADTS(.aac)로 리멕스 (무인코딩)
        tmpdir = "R:\\" if os.path.isdir("R:\\") else None
        import tempfile, uuid, subprocess
        with tempfile.NamedTemporaryFile(dir=tmpdir, delete=False, suffix=".aac") as ta:
            adts_path = ta.name
        # remux m4a -> adts (copy)
        rc = subprocess.run(
            ['ffmpeg','-v','error','-nostdin','-y',
             '-i', audio_src,
             '-vn','-c:a','copy','-f','adts', adts_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ).returncode
        if rc != 0 or not os.path.exists(adts_path) or os.path.getsize(adts_path) == 0:
            try: os.remove(list_path)
            except Exception: pass
            try: os.remove(adts_path)
            except Exception: pass
            fd_log.info(f"\r❌[0x{file_type:X}][{tg_index}][{cam_index}] remux to ADTS failed: {audio_src}")
            return None
              # 2) ADTS 입력을 -stream_loop -1 로 무한 반복, -shortest 로 영상 길이에 맞춰 컷
        cmd_mix = [
            'ffmpeg','-v','error','-nostdin',
            '-f','concat','-safe','0','-i', list_path,
            '-stream_loop','-1','-f','aac','-i', adts_path,
            '-map','0:v:0?','-map','1:a:0?',
            '-c:v','copy',
            '-c:a','aac','-ar','48000','-ac','2','-b:a','192k',
            '-shortest',
            # 파이프 출력이므로 faststart 금지, fragmented MP4 사용
            '-movflags','frag_keyframe+empty_moov',
            '-f','mp4','-'
        ]
        out = _run_and_capture(cmd_mix, [list_path, adts_path, *cleanup])
        if out is None: return None




    fd_log.info(f"\r1️⃣ [0x{file_type:X}][Merge][🕒:{(time.perf_counter()-t_start)*1000:,.2f} ms]")
    return out

# ─────────────────────────────────────────────────────────────────────────────
# def trim_frames(file_type, input_buffer, start_frames, end_frame, fps)
# [owner] hongsu jung
# [date] 2025-05-18
# ─────────────────────────────────────────────────────────────────────────────
def trim_frames(file_type, input_buffer, start_frames, end_frame, fps, with_sound = False):

    # time check
    t_start = time.perf_counter()
    
    start_time = start_frames / fps
    new_duration = (end_frame - start_frames) / fps

    with tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4") as temp_in:
        input_buffer.seek(0)
        temp_in.write(input_buffer.read())
        input_file = temp_in.name

    with tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4") as temp_out:
        output_file = temp_out.name

    ffmpeg_cmd = [
        *fd_common_ffmpeg_args_pre(),
        "-ss", str(start_time), 
        "-i", input_file,
        "-t", str(new_duration), 
        *fd_common_ffmpeg_args_post(with_sound),
        "-r", str(conf._input_fps),
        "-avoid_negative_ts", "make_zero",
        output_file
    ]

    # debug
    # fd_log.info(f"\r2️⃣ [0x{file_type:X}][Trim] command:{ffmpeg_cmd}")

    process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(input_file)

    if process.returncode != 0:
        fd_log.info(f"\r❌[0x{file_type:X}] trim failed: {process.stderr.decode(errors='ignore')}")
        return None

    # time check
    t_end = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000

    fd_log.info(f"\r2️⃣ [0x{file_type:X}][Trim][🕒:{elapsed_ms:,.2f} ms]")
    return output_file

# ─────────────────────────────────────────────────────────────────────────────
# def rotate_video(file_type, input_file)
# [owner] hongsu jung
# [date] 2025-05-17
# Portrate Video rotate
# non change fps
# ─────────────────────────────────────────────────────────────────────────────
def rotate_video(file_type, input_file):
    
    # time check
    t_start = time.perf_counter()

    with tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4") as temp_out:
        output_file = temp_out.name
    ffmpeg_cmd = [
        *fd_common_ffmpeg_args_pre(),
        "-i", input_file,
        "-vf", "transpose=1",
        *fd_common_ffmpeg_args_post(),   
        "-r", str(conf._output_fps),     
        output_file
    ]

    # debug
    # fd_log.info(f"\r3️⃣ [0x{file_type:X}][Rotate] command:{ffmpeg_cmd}")


    result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(input_file)

    if result.returncode != 0:
        fd_log.info(f"\r❌[0x{file_type:X}] rotate failed:\n{result.stderr.decode(errors='ignore')}")
        return None

    # time check
    t_end = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000
    fd_log.info(f"\r3️⃣ [0x{file_type:X}][Rotate][🕒:{elapsed_ms:,.2f} ms]")

    return output_file

# ─────────────────────────────────────────────────────────────────────────────
# resize_video(file_type, input_file, target_width, target_height)
# [owner] hongsu jung
# [date] 2025-05-18
# ─────────────────────────────────────────────────────────────────────────────   
def resize_video(file_type, input_buffer, target_width, target_height):

    # time check
    t_start = time.perf_counter()

    # 입력이 파일 경로(str 또는 PathLike)이면 파일 경로로 사용
    if isinstance(input_buffer, (str, os.PathLike)):
        input_file = input_buffer        
    else:
        # 입력이 버퍼이면 임시 파일로 저장
        with tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4") as temp_in:
            input_buffer.seek(0)
            temp_in.write(input_buffer.read())
            input_file = temp_in.name        


    # get fixed named path
    if(file_type == conf._file_type_prev):
        process_file = fd_get_output_file(file_type)
    else:
        process_file = fd_get_clean_file(file_type)

    zoom_factor     = conf._zoom_ratio
    current_width   = conf._input_width
    current_height  = conf._input_height

    if current_width is None or current_height is None:
        fd_log.info(f"\r⚠️[0x{file_type:X}] can't get resolution")
        return None
    
    # ✅ 조건 만족 시 FFmpeg 실행 생략 (pass-through)
    if (
        (file_type == conf._file_type_post) and
        zoom_factor == 1.0 and
        current_width == target_width and
        current_height == target_height
    ):
        fd_log.info(f"\r4️⃣ [0x{file_type:X}][Resize]-Skipped (no crop/scale needed)")
        shutil.copy(input_file, process_file)
        os.remove(input_file)
        return process_file

    # 크롭 계산
    crop_width = int(current_width / zoom_factor)
    crop_height = int(current_height / zoom_factor)
    crop_y = int((current_height - crop_height) // 2)
    crop_x = int((current_width - crop_width) // 2)

    if conf._type_target == conf._type_baseball_batter_RH:
        crop_x = current_width - crop_width
    elif conf._type_target == conf._type_baseball_batter_LH:
        crop_x = 0

    # setpts 설정
    setpts_multiplier = conf._input_fps / conf._output_fps
    if(file_type == conf._file_type_curr and (conf._type_target == conf._type_baseball_hit or conf._type_target == conf._type_baseball_hit_manual)):
        setpts_multiplier = 1


    vf_filter = ",".join([
        f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y},"
        f"scale={target_width}:{target_height},"        
        "unsharp=5:5:1.0:5:5:0.0",  # 🔍 여기에 추가!
        f"format=yuv420p",
        f"setpts={setpts_multiplier:.3f}*PTS,"        
    ])

    ffmpeg_cmd = [
        *fd_common_ffmpeg_args_pre(),
        "-i", input_file,
        "-vf", vf_filter,
        *fd_common_ffmpeg_args_post(),
        "-r", str(conf._output_fps),
        process_file
    ]

    # debug
    #fd_log.info(f"\r4️⃣ [0x{file_type:X}][Resize] command:{ffmpeg_cmd}")

    try:
        result = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.remove(input_file)

        if result.returncode != 0:
            fd_log.info(f"\r❌[0x{file_type:X}] Resize failed:\n{result.stderr.decode(errors='ignore')}")
            return None

        # time check
        t_end = time.perf_counter()  # 종료 시간
        elapsed_ms = (t_end - t_start) * 1000        

        fd_log.info(f"\r4️⃣ [0x{file_type:X}][Resize][🕒:{elapsed_ms:,.2f} ms] {current_width}x{current_height} → {target_width}x{target_height}, zoom:{zoom_factor}, multiplier:{setpts_multiplier}")        
        return process_file

    except subprocess.TimeoutExpired:
        fd_log.info(f"\r⏳[0x{file_type:X}][Resize] FFmpeg resize timeout")
        return None

    except Exception as e:
        fd_log.info(f"\r❌[0x{file_type:X}][Resize] Exception during resize: {e}")
        return None

# ─────────────────────────────────────────────────────────────────────────────
# def slice_and_combine_by_bounds(file_list, output_file):
# [owner] hongsu jung
# [date] 2025-09-19
# 기존처럼 붙이고 앞뒤를 자르는것이 아니고, 바로 앞을 자르면서, 이어 붙이고, 이후 마지막을 잘라내는 형태로 수정
# ─────────────────────────────────────────────────────────────────────────────

# ----- (옵션) 외부가 없을 때를 위한 안전한 기본값/대체 -----
try:
    _ = conf  # noqa
except NameError:
    class _DummyConf:
        _input_fps = 30.0
        _output_fps = 0.0
    conf = _DummyConf()
try:
    import win32con
    _WIN32_HIGH = win32con.HIGH_PRIORITY_CLASS
except Exception:
    _WIN32_HIGH = 0  # 비윈도우/미사용 환경 대체

# ----- 유틸 -----
def _log(msg):
    try: fd_log.info(msg)
    except Exception: print(msg, flush=True)

def _err(msg):
    try: fd_log.error(msg)
    except Exception: print("ERROR:", msg, flush=True)

def _tmpdir():
    """R:\\ 우선, 없으면 None(시스템 temp)"""
    try:
        if os.path.isdir(r"R:\\"):
            # 여유공간 체크(선택): 필요시 추가
            return r"R:\\"
    except Exception:
        pass
    return None

def _ntsc_fix(f):
    f = float(f or 0.0)
    if abs(f - 29.0) < 0.01: return 30000.0/1001.0
    if abs(f - 59.0) < 0.01: return 60000.0/1001.0
    return f if f > 0 else 30.0

def _ffprobe_json(path):
    try:
        r = subprocess.run(
            ["ffprobe","-v","error","-show_streams","-show_format","-of","json", path],
            capture_output=True, text=True, check=True
        )
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}
    
def _video_duration_sec(path):
    try:
        r = subprocess.run(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0", path],
            capture_output=True, text=True, check=True
        )
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0
    
def _probe_audio_codec_of_first(paths):
    """paths[0]의 오디오 코덱명 (aac/mp3/pcm 등) 또는 None"""
    for p in paths:
        j = _ffprobe_json(p)
        for s in j.get("streams", []):
            if s.get("codec_type") == "audio":
                return s.get("codec_name")
    return None

def _streams_signature(path, include_audio=True):
    """concat-copy 가능성 판단을 위한 최소 서명 추출"""
    j = _ffprobe_json(path)
    vs = next((s for s in j.get("streams", []) if s.get("codec_type")=="video"), None)
    as_ = next((s for s in j.get("streams", []) if s.get("codec_type")=="audio"), None)
    sig = {
        "v_codec": vs.get("codec_name") if vs else None,
        "time_base": vs.get("time_base") if vs else None,
        "r_frame_rate": vs.get("r_frame_rate") if vs else None,
        "pix_fmt": vs.get("pix_fmt") if vs else None,
        "width": vs.get("width") if vs else None,
        "height": vs.get("height") if vs else None,
        "sar": vs.get("sample_aspect_ratio") if vs else None,
        "color_range": vs.get("color_range") if vs else None,
    }
    if include_audio:
        sig.update({
            "a_codec": as_.get("codec_name") if as_ else None,
            "a_sr": as_.get("sample_rate") if as_ else None,
            "a_ch": as_.get("channels") if as_ else None,
            "a_layout": as_.get("channel_layout") if as_ else None,
        })
    return sig

# ----- 가장 빠르고 안전한 병합: 앞/뒤만 트림(copy), 중간은 원본 그대로 -----
def combine_segments(
    file_type,
    tg_index,
    cam_index,
    file_list,
    start_frame=None,     # 첫 파일: start_frame 이전 drop (키프레임 근사)
    end_frame=None,       # 마지막 파일: end_frame 이후 drop (키프레임 근사)
    with_sound=False,     # 원본 오디오 포함 여부
    *,
    force_cfr_merge=False,
    debug_save_path=None  # 지정 시 파일로 저장, 아니면 BytesIO 반환
):
    """
    1초 단위 조각들을 "앞/뒤만 트림(copy) + 중간 원본 그대로"로 하나의 파일로 병합.
    - 기본: concat copy (무손실, 원본 fps/bitrate/코덱 유지)
    - 스트림 불일치/PTS 꼬임 감지 시: 마지막 단계에서만 CFR 재인코딩(libx264, aac) 폴백
    - 트림은 속도 최우선: -ss 입력 앞(키프레임 근사)로 수행 (프레임-정확 X)
    """

    t0 = time.perf_counter()
    if not file_list:
        _err(f"\r❌[0x{file_type:X}][{tg_index}][{cam_index}] empty file_list")
        return None

    # FPS 정보
    try:
        fps_in = float(conf._input_fps)
    except Exception:
        fps_in = 30.0
    try:
        fps_out = float(getattr(conf, "_output_fps", 0.0)) or fps_in
    except Exception:
        fps_out = fps_in

    fps = float(fps_in)
    fps_cfr = _ntsc_fix(fps_out)

    start_idx = 0
    last_idx  = len(file_list) - 1
    single_file_case = (start_idx == last_idx)

    # ---- 경계 트림 (copy, 키프레임 근사) ----
    def _trim_first_copy(src_path, start_frame, take_audio):
        """
        앞쪽 drop: -ss (입력 앞) + -c copy
        """
        if not start_frame or start_frame <= 0:
            return os.path.abspath(src_path), None  # 트림 불필요

        ss_sec = float(start_frame) / fps
        tmpdir = _tmpdir() or tempfile.gettempdir()
        dst = os.path.join(tmpdir, f"seg_first_{uuid.uuid4().hex}.mp4")

        cmd = ["ffmpeg","-nostdin","-y","-v","error",
               "-ss", f"{ss_sec:.9f}", "-i", src_path]
        if take_audio:
            # aac → mp4 복사 시 adts→mp4 헤더 변환 필요 가능
            a_codec = _probe_audio_codec_of_first([src_path])
            cmd += ["-map","0:v:0?","-map","0:a:0?","-c:v","copy","-c:a","copy"]
            if (a_codec or "").lower() == "aac":
                cmd += ["-bsf:a","aac_adtstoasc"]
        else:
            cmd += ["-map","0:v:0?","-an","-c:v","copy"]

        cmd += ["-movflags","+faststart","-fflags","+genpts","-avoid_negative_ts","make_zero", dst]

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst, dst
        # 실패 시 그대로 원본 반환(어차피 뒤에서 CFR 폴백이 있으므로 속도 우선)
        _log(f"⚠️ first copy-trim failed → using original: {src_path}")
        return os.path.abspath(src_path), None

    def _trim_last_copy(src_path, end_frame, take_audio):
        """
        뒤쪽 keep: -t + -c copy
        """
        if end_frame is None:
            return os.path.abspath(src_path), None  # 트림 불필요

        keep_sec = max(0.0, float(end_frame) / fps)
        tmpdir = _tmpdir() or tempfile.gettempdir()
        dst = os.path.join(tmpdir, f"seg_last_{uuid.uuid4().hex}.mp4")

        cmd = ["ffmpeg","-nostdin","-y","-v","error",
               "-i", src_path, "-t", f"{keep_sec:.9f}"]
        if take_audio:
            a_codec = _probe_audio_codec_of_first([src_path])
            cmd += ["-map","0:v:0?","-map","0:a:0?","-c:v","copy","-c:a","copy"]
            if (a_codec or "").lower() == "aac":
                cmd += ["-bsf:a","aac_adtstoasc"]
        else:
            cmd += ["-map","0:v:0?","-an","-c:v","copy"]

        cmd += ["-movflags","+faststart","-fflags","+genpts","-avoid_negative_ts","make_zero", dst]

        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst, dst
        _log(f"⚠️ last copy-trim failed → using original: {src_path}")
        return os.path.abspath(src_path), None

    # ---- 앞/중/뒤 구성 ----
    temp_paths, cleanup = [], []
    try:
        if single_file_case:
            first_final, rm1 = _trim_first_copy(file_list[0], start_frame, with_sound)
            last_final,  rm2 = _trim_last_copy(first_final, end_frame, with_sound)
            if last_final is None:
                _err(f"fail trim single file {file_list[0]}, {start_frame}~{end_frame}")
                return None
            temp_paths.append(last_final)
            for r in (rm1, rm2):
                if r: cleanup.append(r)
        else:
            # 첫 조각(앞 트림)
            first_final, rm1 = _trim_first_copy(file_list[start_idx], start_frame, with_sound)
            temp_paths.append(first_final)
            if rm1: cleanup.append(rm1)

            # 중간은 원본 그대로
            for i in range(start_idx + 1, last_idx):
                temp_paths.append(os.path.abspath(file_list[i]))

            # 마지막 조각(뒤 트림)
            last_final, rm2 = _trim_last_copy(file_list[last_idx], end_frame, with_sound)
            temp_paths.append(last_final)
            if rm2: cleanup.append(rm2)

        if not temp_paths:
            _err(f"\r❌[0x{file_type:X}][{tg_index}][{cam_index}] nothing to merge")
            return None

        # ---- concat-copy 가능성 점검 (서명 비교) ----
        sig0 = _streams_signature(temp_paths[0], include_audio=with_sound)
        inconsistent = False
        for pth in temp_paths[1:]:
            s = _streams_signature(pth, include_audio=with_sound)
            for k in ("v_codec","time_base","r_frame_rate","pix_fmt","width","height","sar","color_range"):
                if (sig0.get(k) or "") != (s.get(k) or ""):
                    inconsistent = True; break
            if with_sound and not inconsistent:
                for k in ("a_codec","a_sr","a_ch","a_layout"):
                    if (sig0.get(k) or "") != (s.get(k) or ""):
                        inconsistent = True; break
            if inconsistent: break

        auto_force_cfr = inconsistent
        if auto_force_cfr:
            _log(f"\r⚠️[0x{file_type:X}][{tg_index}][{cam_index}] stream signature mismatch → CFR merge fallback")

        # ---- concat 리스트 파일 작성 ----
        with tempfile.NamedTemporaryFile(dir=_tmpdir(), mode='w+', delete=False, suffix='.txt') as tmp:
            for p in temp_paths:
                tmp.write(f"file '{os.path.abspath(p)}'\n")
            tmp.flush()
            list_path = tmp.name

        def _run(cmd, rm_paths, pipe_binary):
            _log(f"\r1️⃣ [0x{file_type:X}][{tg_index}][{cam_index}][Merge] command: {shlex.join(cmd)}")
            p = subprocess.Popen(
                cmd,
                stdout=(subprocess.PIPE if pipe_binary else None),
                stderr=subprocess.PIPE,
                creationflags=_WIN32_HIGH
            )
            out, err = p.communicate()
            # 정리
            for rp in (rm_paths if isinstance(rm_paths, (list, tuple)) else [rm_paths]):
                try:
                    if rp and os.path.exists(rp): os.remove(rp)
                except Exception:
                    pass
            if p.returncode != 0:
                _err(f"\r❌[0x{file_type:X}][{tg_index}][{cam_index}] ffmpeg merge failed. rc={p.returncode}\nSTDERR:\n{(err or b'').decode('utf-8','ignore')[:4000]}")
                return None
            return io.BytesIO(out) if pipe_binary else True

        # ---- 병합 실행 ----
        to_pipe = (debug_save_path is None)
        do_cfr = bool(force_cfr_merge or auto_force_cfr)


        if not do_cfr:
            # 초고속: concat copy (무손실)
            if not with_sound:
                cmd = ['ffmpeg','-v','error','-nostdin','-y',
                        '-f','concat','-safe','0','-i', list_path,
                        '-c','copy','-an']
            else:
                a_codec = _probe_audio_codec_of_first(temp_paths)
                cmd = ['ffmpeg','-v','error','-nostdin','-y',
                        '-f','concat','-safe','0','-i', list_path,
                        '-map','0:v:0?','-map','0:a:0?','-c:v','copy','-c:a','copy']
                if (a_codec or '').lower() == 'aac':
                    cmd += ['-bsf:a','aac_adtstoasc']

            if to_pipe:
                cmd += ['-movflags','frag_keyframe+empty_moov','-f','mp4','-']
            else:
                os.makedirs(os.path.dirname(debug_save_path), exist_ok=True)
                cmd += ['-movflags','+faststart', debug_save_path]

            out = _run(cmd, [list_path, *cleanup], pipe_binary=to_pipe)
            if out is None: return None
        
        else:
            # 안전 폴백: 최종 단계에서만 CFR 재인코딩
            vr = f"{fps_cfr}"
            if not with_sound:
                cmd = ['ffmpeg','-v','error','-nostdin','-y',
                       '-f','concat','-safe','0','-i', list_path,
                       '-map','0:v:0?',
                       "-vf", f"setpts=PTS-STARTPTS,fps=30000/1001",        # ★ 시간축/프레임 고정
                       '-c:v','libx264','-preset','veryfast','-crf','18',
                       '-r', vr, '-vsync','cfr','-pix_fmt','yuv420p','-an']
            else:
                cmd = ['ffmpeg','-v','error','-nostdin','-y',
                       '-f','concat','-safe','0','-i', list_path,
                       '-map','0:v:0?','-map','0:a:0?',
                       "-vf", f"setpts=PTS-STARTPTS,fps=30000/1001",        # ★ 시간축/프레임 고정
                       "-af", "aresample=async=1:first_pts=0",              # ★
                       '-c:v','libx264','-preset','veryfast','-crf','18',
                       '-r', vr, '-vsync','cfr','-pix_fmt','yuv420p',
                       '-c:a','aac','-ar','48000','-ac','2','-b:a','192k']

            if to_pipe:
                cmd += ['-movflags','frag_keyframe+empty_moov','-f','mp4','-']
            else:
                os.makedirs(os.path.dirname(debug_save_path), exist_ok=True)
                cmd += ['-movflags','+faststart', debug_save_path]

            out = _run(cmd, [list_path, *cleanup], pipe_binary=to_pipe)
            if out is None: return None
        

        ms = (time.perf_counter() - t0) * 1000.0
        _log(f"\r✅[0x{file_type:X}][{tg_index}][{cam_index}][Combine]{' [CFR]' if do_cfr else ''} [🕒:{ms:,.2f} ms]")
        return debug_save_path if not to_pipe else out

    finally:
        # 혹시 남은 트림 산출물 정리(보호)
        for p in cleanup:
            try:
                if p and os.path.exists(p): os.remove(p)
            except Exception:
                pass

def _probe_duration_ms(path: str) -> int:
    # ffprobe로 파일 길이(ms) 구하기
    cmd = [
        "ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","format=duration","-of","json", path
    ]
    out = subprocess.check_output(cmd)
    dur = float(json.loads(out)["format"]["duration"])
    return int(dur * 1000)

# ----- 가장 빠르고 안전한 병합: 앞/뒤만 트림(copy), 중간은 원본 그대로 -----
def combine_segments_simple(file_list, output_path, tg_index):

    t0 = time.perf_counter()

    # concat list 파일
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".txt", delete=False) as tmp:
        for f in file_list:
            # 작은따옴표 안에 경로 넣을 때 이스케이프 주의
            tmp.write("file '{}'\n".format(os.path.abspath(f).replace("'", r"'\''")))
        tmp.flush()
        list_path = tmp.name

    # 총 길이(ms)
    try:
        total_ms = sum(_probe_duration_ms(f) for f in file_list)
    except Exception:
        total_ms = 0  # 실패 시 0 (그럼 퍼센트는 out_time_ms만으로 best-effort)

    # ffmpeg concat copy
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-v", "error",            # 에러만
        "-nostats",               # 통계 줄 제거(진행은 -progress로 받음)
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-c", "copy", "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        "-progress", "pipe:1"     # << 진행 상황을 stdout으로
    ]

    # 출력은 마지막에 둔다 (일부 ffmpeg는 -progress 뒤여도 무관하지만 습관)
    cmd += [output_path]

    # 실행 (stdout에서 진행 파싱)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    last_bucket = -1  # 10% 버킷
    last_printed = -1
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        line = line.strip()
        if line.startswith("out_time_ms="):
            # out_time_ms는 마이크로초(us)
            try:
                us = int(line.split("=",1)[1])
                if total_ms > 0:
                    percent = int((us / 1000) * 100 / total_ms)
                else:
                    # 총 길이 모를 때는 근사치: 1%씩 상승(원치 않으면 제거)
                    percent = min(last_printed + 1, 99)
                percent = max(0, min(99, percent))
                bucket = percent // 10
                if bucket > last_bucket:
                    if tg_index is not None:
                        fd_log.info(f"🔊 [TG:{tg_index:02d}] Merge files for audio: {bucket*10}%")
                    else:
                        fd_log.info(f"🔊 [TG:{tg_index:02d}] Merge files for audio: {bucket*10}%")
                    last_bucket = bucket
                last_printed = percent
            except Exception:
                pass

        elif line.startswith("progress=") and line.endswith("end"):
            if tg_index is not None:
                fd_log.info(f"🔊 [TG:{tg_index:02d}] Merge files for audio: 100% ✅")
    
    proc.wait()
    rc = proc.returncode

    fd_log.info(f"🔊 [TG:{tg_index:02d}] Merging files for audio (🕒{time.perf_counter()-t0:.2f}s)")        
    try:
        os.remove(list_path)
    except Exception:
        pass

    if rc != 0:
        raise RuntimeError(f"ffmpeg failed with code {rc}\nCMD: {' '.join(shlex.quote(x) for x in cmd)}")

    return output_path
    
# ============== ② 합쳐진 영상에서 음성만 추출 ==================
def extract_audio_from_video(
    file_type,
    file_directory,
    merged_video,     # 경로 또는 바이너리 버퍼(BytesIO 등)
    time_start,
    fmt="m4a"         # 'm4a' | 'aac_adts' | 'wav'
):
    """
    합쳐진 동영상에서 오디오만 추출.
    - m4a     : 컨테이너 m4a, 가능하면 copy, 아니면 AAC-LC 인코딩
    - aac_adts: ADTS 원시 AAC (스트리밍/루프 안전)
    - wav     : PCM 16-bit LE (가장 안전)
    반환: 산출 경로 (fd_get_cali_file 규칙 사용)
    """
    t0 = time.perf_counter()

    fmt = conf._output_shared_audio_type

    tmpdir = _tmpdir()
    if isinstance(merged_video, (str, os.PathLike)) and os.path.exists(str(merged_video)):
        input_file = os.path.abspath(str(merged_video))
        created = False
    else:
        with tempfile.NamedTemporaryFile(dir=tmpdir, delete=False, suffix=".mp4") as tmp:
            try: merged_video.seek(0)
            except Exception: pass
            tmp.write(merged_video.read())
            input_file = tmp.name
        created = True

    # 출력 경로 결정
    try:
        base = fd_get_cali_file(file_type, file_directory, time_start)
    except Exception:
        # 폴백: temp
        base = os.path.join(tmpdir or tempfile.gettempdir(), f"audio_{uuid.uuid4().hex}")

    root, _ = os.path.splitext(base)
    if fmt == "m4a":
        out_file = root + ".m4a"
    elif fmt == "aac_adts":
        out_file = root + ".aac"
    elif fmt == "wav":
        out_file = root + ".wav"
    else:
        raise ValueError("fmt must be 'm4a' | 'aac_adts' | 'wav'")

    def _run(cmd):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        ok = (proc.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0)
        if not ok:
            _err(f"❌ extract_audio ffmpeg failed rc={proc.returncode}\nCMD: {shlex.join(cmd)}\n{(proc.stderr or '')[:4000]}")
        return ok

    # 먼저 오디오 스트림 존재 확인
    try:
        pr = subprocess.run(
            ["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=codec_name","-of","csv=p=0", input_file],
            capture_output=True, text=True, check=True
        )
        acodec = (pr.stdout or "").strip().lower()
    except Exception:
        acodec = ""

    if fmt == "m4a":
        # m4a 컨테이너로 remux 우선
        cmd = ["ffmpeg","-nostdin","-y","-v","error","-i", input_file, "-vn", "-map","0:a:0"]
        if acodec in ("aac","mp4a","aac_latm","he-aac","aac_he_v2"):
            cmd += ["-c:a","copy", out_file]
            if not _run(cmd):
                '''
                cmd = ["ffmpeg","-nostdin","-y","-v","error","-i", input_file, "-vn", "-map","0:a:0",
                       "-c:a","aac","-ar","48000","-ac","2","-b:a","192k", out_file]
                '''
                # 2025-10-05 stable change
                cmd = [
                    "ffmpeg", "-nostdin", "-y",
                    "-v", "error",
                    "-i", input_file,
                    "-vn",                 # 영상 비활성화
                    "-acodec", "aac",      # 단순 명시
                    "-strict", "experimental",  # 일부 환경에서 AAC 허용
                    "-ar", "44100",        # 표준화된 샘플레이트 (더 호환성 높음)
                    "-ac", "2",            # 스테레오
                    "-b:a", "128k",        # 보통 수준 비트레이트 (안정적)
                    "-movflags", "+faststart",  # mp4 초기화 안정화
                    out_file
                ]
                '''
                cmd = [
                    "ffmpeg", "-nostdin", "-y",
                    "-v", "error",
                    "-i", input_file,
                    "-vn",
                    "-c:a", "aac",
                    "-ar", "32000",
                    "-ac", "1",
                    "-b:a", "96k",
                    out_file
                ]
                '''
                if not _run(cmd):
                    if created: 
                        try: os.remove(input_file)
                        except Exception: pass
                    return None
        else:
            cmd += ["-c:a","aac","-ar","48000","-ac","2","-b:a","192k", out_file]
            if not _run(cmd):
                if created:
                    try: os.remove(input_file)
                    except Exception: pass
                return None

    elif fmt == "aac_adts":
        # ADTS 원시 AAC (루프/스트리밍에 강함)
        cmd = ["ffmpeg","-nostdin","-y","-v","error","-i", input_file, "-vn", "-map","0:a:0"]
        if acodec in ("aac","mp4a","he-aac","aac_he_v2"):
            cmd += ["-c:a","copy","-f","adts", out_file]
            if not _run(cmd):
                cmd = ["ffmpeg","-nostdin","-y","-v","error","-i", input_file, "-vn","-map","0:a:0",
                       "-c:a","aac","-ar","48000","-ac","2","-b:a","192k","-f","adts", out_file]
                if not _run(cmd):
                    if created: 
                        try: os.remove(input_file)
                        except Exception: pass
                    return None
        else:
            cmd += ["-c:a","aac","-ar","48000","-ac","2","-b:a","192k","-f","adts", out_file]
            if not _run(cmd):
                if created:
                    try: os.remove(input_file)
                    except Exception: pass
                return None

    else:  # wav
        cmd = ["ffmpeg","-nostdin","-y","-v","error","-i", input_file, "-vn","-map","0:a:0",
               "-c:a","pcm_s16le", out_file]
        if not _run(cmd):
            if created:
                try: os.remove(input_file)
                except Exception: pass
            return None

    if created:
        try: os.remove(input_file)
        except Exception: pass

    fd_log.info(f"🎧 audio extracted → {out_file}  (🕒{time.perf_counter()-t0:.2f}s)")
    return out_file

# ============== ③ 공유 오디오를 최종 영상에 삽입 ==================
def mux_shared_audio(
    merged_video_path,        # 합쳐진 영상(파일 경로)
    shared_audio_path,        # 공유 오디오(파일 경로)  *.m4a / *.mp4 / *.aac / *.wav
    out_path=None,            # 지정 없으면 temp에 생성
    *,
    reencode_video=False      # True면 libx264 재인코딩, False면 -c:v copy
):
    """
    1) 최종 영상 길이를 구한다 D
    2) 공유 오디오를 D에 맞춰 **WAV(PCM)**로 1개 생성(반복/트리밍)
    3) 최종 mux: 비디오 copy(가능하면) + 오디오 aac 인코딩, -shortest

    ※ ADTS(.aac), M4A 모두 직접 loop/concat하지 않고, 항상 **WAV로 길이 확정** 후 사용 → 안정.
    """
    if not os.path.exists(merged_video_path):
        _err("mux_shared_audio: merged_video_path not found")
        return None
    if not os.path.exists(shared_audio_path):
        _err("mux_shared_audio: shared_audio_path not found")
        return None

    D = _video_duration_sec(merged_video_path)
    if D <= 0:
        _err("mux_shared_audio: invalid video duration")
        return None

    tmpdir = _tmpdir()
    temp_wav = os.path.join(tmpdir or tempfile.gettempdir(), f"shared_{uuid.uuid4().hex}.wav")

    # 2) 공유 오디오 → 길이 D의 WAV 생성 (반복 + 컷)
    #  - 입력 형식은 ffmpeg가 자동 인식; ADTS면 알아서 인식, 문제 시 `-f adts -i`로 강제 가능.
    cmd_wav = [
        "ffmpeg","-nostdin","-y","-v","error",
        "-stream_loop","-1","-i", shared_audio_path,
        "-t", f"{D:.3f}",
        "-vn","-ac","2","-ar","48000","-c:a","pcm_s16le",
        temp_wav
    ]
    p = subprocess.run(cmd_wav, capture_output=True, text=True)
    if p.returncode != 0 or not os.path.exists(temp_wav) or os.path.getsize(temp_wav) == 0:
        _err(f"mux_shared_audio: failed to build WAV\nCMD: {shlex.join(cmd_wav)}\n{(p.stderr or '')[:4000]}")
        try: os.remove(temp_wav)
        except Exception: pass
        return None

    # 3) 최종 mux
    if out_path is None:
        out_path = os.path.join(tmpdir or tempfile.gettempdir(), f"mux_{uuid.uuid4().hex}.mp4")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    if reencode_video:
        vcodec = ["-c:v","libx264","-preset","veryfast","-crf","18","-pix_fmt","yuv420p"]
    else:
        vcodec = ["-c:v","copy"]

    cmd_mux = [
        "ffmpeg","-nostdin","-y","-v","error",
        "-i", merged_video_path, "-i", temp_wav,
        "-map","0:v:0?","-map","1:a:0?",
        *vcodec,
        "-c:a","aac","-ar","48000","-ac","2","-b:a","192k",
        "-af","aresample=async=1:first_pts=0",
        "-shortest",
        "-movflags","+faststart",
        out_path
    ]
    m = subprocess.run(cmd_mux, capture_output=True, text=True)
    try: os.remove(temp_wav)
    except Exception: pass

    if m.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        _err(f"mux_shared_audio: ffmpeg failed rc={m.returncode}\nCMD: {shlex.join(cmd_mux)}\n{(m.stderr or '')[:4000]}")
        return None

    _log(f"🎚️ muxed(shared audio) → {out_path}")
    return out_path

# ─────────────────────────────────────────────────────────────────────────────
# calibration_video_v2.py
# [owner] hongsu jung
# [date] 2025-09-21
# ─────────────────────────────────────────────────────────────────────────────

def _even_size(w: int, h: int) -> Tuple[int, int]:
    return (int(w) & ~1, int(h) & ~1)

def _ntsc_fps_fix(fps: float) -> float:
    f = float(fps or 0.0)
    if abs(f - 29.0) < 0.01: return 30000.0/1001.0
    if abs(f - 59.0) < 0.01: return 60000.0/1001.0
    return f if f > 0 else 30.0

def _probe_meta_any(path: str) -> Tuple[int, int, float]:
    """width, height, fps(avg_frame_rate) — 비디오가 없으면 예외"""
    def _get(key):
        try:
            out = subprocess.check_output(
                ["ffprobe","-v","error","-select_streams","v:0",
                 "-show_entries","stream="+key,"-of","csv=p=0", path]
            ).decode().strip()
            return out
        except Exception:
            return ""
    fps_s = _get("avg_frame_rate")
    if "/" in fps_s:
        try:
            n, d = fps_s.split("/")
            fps = float(n)/float(d) if float(d) > 0 else 0.0
        except Exception:
            fps = 0.0
    else:
        try:
            fps = float(fps_s) if fps_s else 0.0
        except Exception:
            fps = 0.0
    def _toi(x, dv):
        try: return int(x)
        except: return dv
    W = _toi(_get("width"),  0)
    H = _toi(_get("height"), 0)
    if W<=0 or H<=0:
        raise RuntimeError("ffprobe 실패: width/height")
    return W, H, fps

def _final_out_path(file_type, file_directory, time_start, channel) -> str:
    outp = fd_get_cali_file(file_type, file_directory, time_start, channel)
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    return outp

NVENC_START_MAX_RETRY    = int(os.environ.get("NVENC_START_MAX_RETRY", "5"))
NVENC_START_BACKOFF_S    = float(os.environ.get("NVENC_START_BACKOFF_S", "0.25"))
NVENC_START_BACKOFF_GROW = float(os.environ.get("NVENC_START_BACKOFF_GROW", "1.1"))


# ─────────────────────────────────────────────────────────────────────────────
# NVENC 동시 세션 제어
# ─────────────────────────────────────────────────────────────────────────────
class NVEncLocker:
    def __init__(self, slots: int):
        self.slots = int(slots or 2)
        self.token_path = None

    @staticmethod
    def _pick_lock_root() -> str:
        r = r"R:\fd_nvenc_lock"
        if os.path.isdir(r"R:\\"):
            return r
        return os.path.join(tempfile.gettempdir(), "fd_nvenc_lock")

    @classmethod
    def _ensure_dir(cls) -> str:
        root = cls._pick_lock_root()
        try:
            os.makedirs(root, exist_ok=True)
        except FileNotFoundError:
            base = os.path.dirname(root)
            try:
                if base:
                    os.makedirs(base, exist_ok=True)
                os.makedirs(root, exist_ok=True)
            except Exception:
                pass
        return root

    def acquire(self, timeout_sec=30) -> bool:
        if self.slots <= 0:
            return True
        root = self._ensure_dir()
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                tokens = [f for f in os.listdir(root) if f.endswith(".token")]
            except FileNotFoundError:
                root = self._ensure_dir()
                tokens = []
            if len(tokens) < self.slots:
                token = os.path.join(root, f"{uuid.uuid4().hex}.token")
                try:
                    fd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    self.token_path = token
                    return True
                except FileExistsError:
                    time.sleep(0.02); continue
                except FileNotFoundError:
                    root = self._ensure_dir(); time.sleep(0.02); continue
            time.sleep(0.05)
        return False

    def release(self):
        try:
            if self.token_path and os.path.exists(self.token_path):
                os.remove(self.token_path)
        except Exception:
            pass
        self.token_path = None

# ─────────────────────────────────────────────────────────────────────────────
# Main Class on Calibration - CPU
# class CalibrationVideoCPU
# ─────────────────────────────────────────────────────────────────────────────
class CalibrationVideoCPU:
    # ───────── Single MUX worker (shared) ─────────
    _MUX_INIT_LOCK = threading.Lock()
    _MUX_QUEUE = None
    _MUX_WORKER = None

    # Shared init lock for NVENC across instances/threads
    _NVENC_INIT_LOCK = threading.Lock()

    def __init__(self):
        slots = int(os.environ.get("FD_NVENC_MAX_SLOTS", "8"))
        self.locker = NVEncLocker(slots)

    # ───────── MUX worker helpers ─────────
    def _ensure_mux_worker(self):
        """Ensure a single remux worker thread is running."""
        import queue
        with CalibrationVideo._MUX_INIT_LOCK:
            if CalibrationVideo._MUX_QUEUE is None:
                CalibrationVideo._MUX_QUEUE = queue.Queue()
            if (CalibrationVideo._MUX_WORKER is None) or (not CalibrationVideo._MUX_WORKER.is_alive()):
                t = threading.Thread(target=self._mux_worker_loop, name="MuxWorker", daemon=True)
                t.start()
                CalibrationVideo._MUX_WORKER = t

    def _enqueue_remux(self, video_path: str, audio_path: str, tg_index: int, cam_index: int):
        """Enqueue a remux job to attach audio to video (processed serially by the dedicated worker)."""
        self._ensure_mux_worker()
        try:
            CalibrationVideo._MUX_QUEUE.put({
                "video": video_path,
                "audio": audio_path,
                "tg": tg_index,
                "cam": cam_index,
            }, block=False)
            fd_log.info(f"🎉[TG:{tg_index:02d}][CAM:{cam_index:02d}] Audio Combine, Finish: {os.path.basename(video_path)}")
        except Exception as e:
            fd_log.error(f"enqueue remux failed: {e}")

    def _mux_worker_loop(self):
        """Serial remux worker loop."""
        import time as _t, queue
        while True:
            try:
                job = CalibrationVideo._MUX_QUEUE.get()
            except Exception:
                _t.sleep(0.05)
                continue
            if not job:
                continue

            v = job.get("video")
            a = job.get("audio")
            tg = job.get("tg")
            cam = job.get("cam")

            try:
                if (v and a) and os.path.exists(v) and os.path.exists(a):
                    # Skip if the result already has an audio stream
                    if self._has_audio_stream(v):
                        fd_log.info(f"[TG:{tg:02d}][CAM:{cam:02d}] already has audio, skip remux.")
                    else:
                        ok = self._remux_add_audio(v, a)
                        if ok:
                            fd_log.info(f"[TG:{tg:02d}][CAM:{cam:02d}] remux done.")
                        else:
                            fd_log.error(f"[TG:{tg:02d}][CAM:{cam:02d}] remux failed.")
                else:
                    fd_log.warning(f"[TG:{tg:02d}][CAM:{cam:02d}] remux skipped (missing paths)")
            except Exception as e:
                fd_log.error(f"[TG:{tg:02d}][CAM:{cam:02d}] remux exception: {e}")
            finally:
                try:
                    CalibrationVideo._MUX_QUEUE.task_done()
                except Exception:
                    pass

    def _remux_add_audio_to(self, video_path: str, audio_path: str, out_path: str) -> bool:
        """Robust remux to a NEW file with fallbacks and SMB-safe finalize."""
        import tempfile, shutil

        if not (video_path and audio_path and out_path and
                os.path.exists(video_path) and os.path.exists(audio_path)):
            fd_log.error("remux(to new): invalid input paths")
            return False

        base = os.path.basename(out_path)
        tmp_dir = tempfile.gettempdir()  # local fast disk
        local_tmp = os.path.join(tmp_dir, base + ".local.tmp.mp4")   # local build
        remote_tmp = out_path + ".part"                               # same-volume as final

        def _run_ffmpeg(copy_audio: bool, use_faststart: bool) -> bool:
            a_args = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "128k"]
            mov = ["-movflags", "+faststart"] if use_faststart else []
            cmd = [
                "ffmpeg","-y","-nostdin","-hide_banner","-loglevel","error",
                "-i", video_path,
                "-i", audio_path,
                "-map","0:v:0","-map","1:a:0?",
                "-c:v","copy", *a_args, "-shortest", *mov, local_tmp
            ]
            try:
                # capture stderr properly
                pr = subprocess.run(cmd, text=True, capture_output=True, check=True)
                return True
            except subprocess.CalledProcessError as e:
                err = (e.stderr or "")[:4000]
                fd_log.error(f"remux ffmpeg failed (copy_audio={copy_audio}, faststart={use_faststart}) "
                            f"rc={e.returncode} | stderr:\n{err}")
                try:
                    if os.path.exists(local_tmp): os.remove(local_tmp)
                except Exception:
                    pass
                return False
            except Exception as e:
                fd_log.error(f"remux ffmpeg exception: {e}")
                try:
                    if os.path.exists(local_tmp): os.remove(local_tmp)
                except Exception:
                    pass
                return False

        # Try 1: copy audio + faststart
        if not _run_ffmpeg(copy_audio=True, use_faststart=True):
            # Try 2: re-encode audio + faststart
            if not _run_ffmpeg(copy_audio=False, use_faststart=True):
                # Try 3: re-encode audio, NO faststart (SMB-safe)
                if not _run_ffmpeg(copy_audio=False, use_faststart=False):
                    return False

        # move local -> remote .part (stream copy to avoid cross-device replace problems)
        try:
            with open(local_tmp, "rb") as src, open(remote_tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=8*1024*1024)
        except Exception as e:
            fd_log.error(f"copy to remote tmp failed: {e}")
            try:
                if os.path.exists(remote_tmp): os.remove(remote_tmp)
            except Exception:
                pass
            try:
                if os.path.exists(local_tmp): os.remove(local_tmp)
            except Exception:
                pass
            return False
        finally:
            try:
                if os.path.exists(local_tmp): os.remove(local_tmp)
            except Exception:
                pass

        # finalize on remote volume
        try:
            os.replace(remote_tmp, out_path)  # atomic on same volume
        except Exception as e:
            fd_log.error(f"finalize (replace) failed: {e}")
            try:
                if os.path.exists(remote_tmp): os.remove(remote_tmp)
            except Exception:
                pass
            return False

        return True

    def _wait_for_audio_ready(self, path: str, *, timeout_s: float = 90.0,
                          poll: float = 0.5, stable_need: int = 4) -> bool:
        """Wait until `path` has audio stream and size is stable."""
        import os, time
        t0 = time.time()
        last_size = -1
        stable = 0
        consecutive_ok = 0
        while time.time() - t0 < timeout_s:
            try:
                if os.path.exists(path) and self._has_audio_stream(path):
                    consecutive_ok += 1
                    sz = os.path.getsize(path)
                    if sz == last_size: stable += 1
                    else: stable, last_size = 0, sz
                    if consecutive_ok >= 2 and stable >= stable_need:
                        return True
                else:
                    consecutive_ok = 0
            except Exception:
                pass
            time.sleep(poll)
        return False

    # ───────── Progress reporters (with 10% bar) ─────────
    def _reporter(self, tg_index, cam_index, progress_cb):
        state = {"last_q": -10}

        def _report(pct: float, msg: str = ""):
            try:
                p = 0 if pct is None else float(pct)
            except Exception:
                p = 0.0
            q = int(max(0, min(100, (int(p) // 10) * 10)))
            if q <= state["last_q"]:
                return
            state["last_q"] = q

            text = f"[TG:{int(tg_index):02d}][CAM:{int(cam_index):02d}] {q:3d}% {msg}".rstrip()
            try:
                if progress_cb:
                    try:
                        progress_cb(q, msg)
                    except Exception:
                        pass
                else:
                    fd_log.info(text)
            except Exception:
                pass

        return _report

    def _bar10(self, pct: float, width: int = 10) -> str:
        try:
            p = 0 if pct is None else float(pct)
        except Exception:
            p = 0.0
        p = max(0.0, min(100.0, p))
        full = int(p) // 10
        empty = width - full
        return f"| {'█' * full}{'.' * empty} |"

    def _reporter_bar(self, tg_index, cam_index, progress_cb=None, step: int = 10):
        state = {"last_q": -step}

        def _report(pct: float, msg: str = ""):
            try:
                p = 0 if pct is None else float(pct)
            except Exception:
                p = 0.0
            q = int(max(0, min(100, (int(p) // step) * step)))
            if q <= state["last_q"]:
                return
            state["last_q"] = q

            bar = self._bar10(q)
            text = f"[TG:{int(tg_index):02d}][CAM:{int(cam_index):02d}] {q:3d}% {bar} {msg}".rstrip()
            try:
                if progress_cb:
                    try:
                        progress_cb(q, f"{bar} {msg}".strip())
                    except Exception:
                        pass
                else:
                    fd_log.info(text)
            except Exception:
                pass

        return _report

    # ───────── Adjust → 2x3 affine ─────────
    def _compute_affine_from_adjust(self, adjust_info: dict, src_w: int, src_h: int, tw: int, th: int) -> 'np.ndarray':
        def _T(tx, ty): return np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], dtype=np.float32)
        def _S(sx, sy=None):
            if sy is None:
                sy = sx
            return np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], dtype=np.float32)
        def _S2(sx, sy): return np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], dtype=np.float32)
        def _R_deg(theta_deg):
            th_ = np.deg2rad(theta_deg)
            c, s = np.cos(th_), np.sin(th_)
            return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)
        def _about(cx, cy, M): return _T(cx, cy) @ M @ _T(-cx, -cy)
        def _M_flip(width, height, flip_x, flip_y):
            M = np.eye(3, dtype=np.float32)
            if flip_x:
                M[0, 0] = -1.0
                M[0, 2] = float(width - 1)
            if flip_y:
                M[1, 1] = -1.0
                M[1, 2] = float(height - 1)
            return M
        def _M_margin(width, height, marginX, marginY, marginW, marginH):
            if (width <= 0 or height <= 0 or marginW <= 0 or marginH <= 0):
                return np.eye(3, dtype=np.float32)
            sx = float(width) / float(marginW)
            sy = float(height) / float(marginH)
            return _S2(sx, sy) @ _T(-float(marginX), -float(marginY))
        def _max_rect_with_aspect(xmin, ymin, xmax, ymax, aspect):
            xmin, ymin = float(xmin), float(ymin)
            xmax, ymax = float(xmax), float(ymax)
            box_w = max(0.0, xmax - xmin)
            box_h = max(0.0, ymax - ymin)
            if box_w <= 0 or box_h <= 0:
                return 0.0, 0.0, 0.0, 0.0
            w1 = box_w
            h1 = w1 / max(1e-9, aspect)
            if h1 <= box_h + 1e-6:
                w, h = w1, h1
            else:
                h = box_h
                w = h * aspect
            x = xmin + (box_w - w) * 0.5
            y = ymin + (box_h - h) * 0.5
            return x, y, w, h

        adj = adjust_info.get("Adjust", adjust_info)
        image_w = float(adj.get("imageWidth", src_w))
        image_h = float(adj.get("imageHeight", src_h))

        if "dAdjustX" in adj and "dAdjustY" in adj:
            dMoveX = float(adj.get("dAdjustX", 0.0))
            dMoveY = float(adj.get("dAdjustY", 0.0))
        else:
            nx = float(adj.get("normAdjustX", 0.0))
            ny = float(adj.get("normAdjustY", 0.0))
            dMoveX = nx * image_w
            dMoveY = ny * image_h

        if "dRotateX" in adj and "dRotateY" in adj:
            dRotateX = float(adj.get("dRotateX", 0.0))
            dRotateY = float(adj.get("dRotateY", 0.0))
        else:
            nrx = float(adj.get("normRotateX", 0.0))
            nry = float(adj.get("normRotateY", 0.0))
            dRotateX = nrx * image_w
            dRotateY = nry * image_h

        dAngle = float(adj.get("dAngle", 0.0)) + 90.0
        dScale = float(adj.get("dScale", 1.0))
        bFlip = bool(adj.get("bFlip", False))

        rt = adj.get("rtMargin", {}) or {}
        dMarginX = float(rt.get("X", adj.get("dMarginX", 0.0)))
        dMarginY = float(rt.get("Y", adj.get("dMarginY", 0.0)))
        dMarginW = float(rt.get("Width", adj.get("dMarginW", 0.0)))
        dMarginH = float(rt.get("Height", adj.get("dMarginH", 0.0)))

        if (abs(image_w - src_w) > 0.5 or abs(image_h - src_h) > 0.5):
            sx = src_w / image_w
            sy = src_h / image_h
            dMoveX *= sx
            dMoveY *= sy
            dRotateX *= sx
            dRotateY *= sy
            dMarginX *= sx
            dMarginY *= sy
            dMarginW *= sx
            dMarginH *= sy

        M_flip = _M_flip(src_w, src_h, bFlip, bFlip)
        M_trn  = _T(dMoveX, dMoveY)
        M_rot  = _about(dRotateX, dRotateY, _R_deg(dAngle))
        M_scl  = _about(dRotateX, dRotateY, _S(max(dScale, 1e-6)))
        M_mgn  = _M_margin(src_w, src_h, dMarginX, dMarginY, dMarginW, dMarginH)
        M_sout = _S2(float(tw)/max(1.0, float(src_w)), float(th)/max(1.0, float(src_h)))

        M_total = M_sout @ M_mgn @ M_scl @ M_rot @ M_trn @ M_flip

        src_corners = np.array([[0, 0, 1],
                                [src_w - 1, 0, 1],
                                [src_w - 1, src_h - 1, 1],
                                [0, src_h - 1, 1]], dtype=np.float32)
        dst_pts = (src_corners @ M_total.T)[:, :2]
        xmin, ymin = np.min(dst_pts, axis=0)
        xmax, ymax = np.max(dst_pts, axis=0)

        xmin = max(0.0, xmin)
        ymin = max(0.0, ymin)
        xmax = min(float(tw), xmax)
        ymax = min(float(th), ymax)

        crop_x, crop_y, crop_w, crop_h = _max_rect_with_aspect(xmin, ymin, xmax, ymax, tw/float(th))
        if crop_w > 0.0 and crop_h > 0.0:
            C_post = (
                np.array([[1, 0, tw*0.5], [0, 1, th*0.5], [0, 0, 1]], dtype=np.float32) @
                _S2(tw/crop_w, th/crop_h) @
                _T(-(crop_x + 0.5*crop_w), -(crop_y + 0.5*crop_h))
            )
        else:
            C_post = np.eye(3, dtype=np.float32)

        M_total = C_post @ M_total
        return M_total[:2, :].astype(np.float32)

    # ───────── FFmpeg PIPE (rawvideo → NVENC/SW) ─────────
    def _spawn_ffmpeg_pipe(
        self, out_path, w, h, *,
        fps_out_num, fps_out_den,
        vcodec="h264", gop=30,
        rc_mode="vbr", bitrate_k=800, maxrate_k=None, bufsize_k=None,
        preset="p4", profile=None,
        audio_path=None, a_bitrate_k=128,
        timescale=None, verbose=False
    ):
        v = (vcodec or "h264").lower()
        if v == "h264":
            codec_name, use_nvenc = "h264_nvenc", True
        elif v == "hevc":
            codec_name, use_nvenc = "hevc_nvenc", True
        elif v.endswith("_nvenc"):
            codec_name, use_nvenc = v, True
        else:
            if v in ("libx264", "x264", "h264"):
                codec_name = "libx264"
            elif v in ("libx265", "x265", "hevc"):
                codec_name = "libx265"
            else:
                codec_name = v
            use_nvenc = False

        # Preset mapping (NVENC p1~p7 → x264/x265 presets)
        if use_nvenc:
            v_preset = preset or "p4"
        else:
            p = (preset or "").lower()
            nv2x = {"p1": "ultrafast", "p2": "superfast", "p3": "veryfast",
                    "p4": "faster", "p5": "fast", "p6": "medium", "p7": "slow"}
            valid = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                     "medium", "slow", "slower", "veryslow", "placebo"}
            v_preset = nv2x.get(p, p if p in valid else "medium")

        vf = "format=nv12,hwupload_cuda" if use_nvenc else "format=yuv420p"

        # Rate control
        rc_args = []
        mode = (rc_mode or "vbr").lower()
        if use_nvenc:
            if mode in ("vbr", "cbr"):
                rc_args += ["-rc", mode]
                if bitrate_k: rc_args += ["-b:v", f"{int(bitrate_k)}k"]
                if maxrate_k: rc_args += ["-maxrate", f"{int(maxrate_k)}k"]
                if bufsize_k: rc_args += ["-bufsize", f"{int(bufsize_k)}k"]
            else:
                rc_args += ["-rc", "vbr"]
                if bitrate_k:
                    rc_args += ["-b:v", f"{int(bitrate_k)}k", "-maxrate", f"{int(bitrate_k)}k"]
        else:
            if bitrate_k: rc_args += ["-b:v", f"{int(bitrate_k)}k"]
            if maxrate_k: rc_args += ["-maxrate", f"{int(maxrate_k)}k"]
            if bufsize_k: rc_args += ["-bufsize", f"{int(bufsize_k)}k"]

        r_out = f"{fps_out_num}/{fps_out_den}"
        ts_args = ["-video_track_timescale", str(int(timescale))] if timescale else []

        # Inputs: stdin(0), optional audio(1)
        cmd = [
            "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
            "-r", r_out, "-i", "-",
        ]
        if audio_path:
            cmd += ["-thread_queue_size", "4096", "-i", audio_path]

        # Filters & encoder (video)
        cmd += [
            "-vf", vf,
            "-vsync", "cfr",
            "-c:v", codec_name,
            "-preset", v_preset,
            "-g", str(int(gop)),
            "-keyint_min", str(int(gop)),
            "-sc_threshold", "0",
        ]
        if use_nvenc:
            cmd += ["-enc_time_base", "-1"]
        if profile:
            cmd += ["-profile:v", profile]

        cmd += rc_args

        # Explicit mapping: using -map disables automap; must specify
        maps = ["-map", "0:v:0"]
        if audio_path:
            maps += ["-map", "1:a:0?"]
            if self._is_aac_file(audio_path):
                maps += ["-c:a", "copy"]
            else:
                maps += ["-c:a", "aac", "-b:a", f"{int(a_bitrate_k)}k"]
            maps += ["-shortest"]
        else:
            maps += ["-an"]

        cmd += maps + ts_args + ["-movflags", "+faststart", out_path]

        if verbose:
            fd_log.info("FFMPEG PIPE CMD: " + " ".join(cmd))

        # Tidy DLL path (Windows: avoid collisions)
        env = os.environ.copy()
        try:
            system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
            path_items = env.get("PATH", "").split(os.pathsep)
            bad = ["CUDA", "NVIDIA GPU Computing Toolkit", "Video Codec SDK", "nvcodec", "NVEncC"]
            path_items = [p for p in path_items if not any(b.lower() in p.lower() for b in bad)]
            env["PATH"] = os.pathsep.join([system32] + path_items)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(system32)
        except Exception:
            pass

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
            text=False,
            env=env
        )
        return proc, proc.stdin

    # ───────── Audio utils ─────────
    @staticmethod
    def _is_aac_file(path: str) -> bool:
        import subprocess
        ext = os.path.splitext(path.lower())[1]
        if ext in (".aac", ".m4a", ".mp4", ".mov", ".3gp"):
            try:
                pr = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=codec_name", "-of", "json", path],
                    text=True, capture_output=True, timeout=5.0
                )
                j = json.loads(pr.stdout or "{}")
                codec = ((j.get("streams") or [{}])[0].get("codec_name") or "").lower()
                return codec == "aac"
            except Exception:
                return ext in (".aac", ".m4a")
        return False

    @staticmethod
    def _has_audio_stream(path: str) -> bool:
        import subprocess
        try:
            pr = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "json", path],
                text=True, capture_output=True, timeout=5.0
            )
            j = json.loads(pr.stdout or "{}")
            return bool(j.get("streams"))
        except Exception:
            return False

    def _remux_add_audio(self, video_path: str, audio_path: str) -> bool:
        """Attach an external audio to a video by remuxing (re-encode audio if needed)."""
        if not (video_path and audio_path and os.path.exists(video_path) and os.path.exists(audio_path)):
            return False

        tmp_out = video_path + "._mux.mp4"
        # Fixed to 128k when re-encoding; copy if already AAC.
        a_args = ["-c:a", "copy"] if self._is_aac_file(audio_path) else ["-c:a", "aac", "-b:a", "128k"]

        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", video_path, "-thread_queue_size", "1024", "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0?",
               "-c:v", "copy", *a_args, "-shortest",
               "-movflags", "+faststart", tmp_out]

        try:
            subprocess.run(cmd, check=True)
        except Exception as e:
            try:
                os.remove(tmp_out)
            except Exception:
                pass
            fd_log.error(f"remux failed during ffmpeg: {e}")
            return False

        # Atomic replace with retry to avoid file locks on Windows
        max_tries = 20
        delay = 0.5
        for _ in range(max_tries):
            try:
                os.replace(tmp_out, video_path)
                return True
            except PermissionError:
                time.sleep(delay)
                continue
            except Exception as e:
                fd_log.error(f"remux replace failed: {e}")
                break

        try:
            os.remove(tmp_out)
        except Exception:
            pass
        return False

    # ───────── Misc helpers ─────────
    def _probe_frames_total(self, files):
        import subprocess

        def _probe_one(p):
            try:
                pr = subprocess.run(
                    ["ffprobe", "-v", "error",
                     "-select_streams", "v:0",
                     "-show_entries", "stream=nb_frames,avg_frame_rate,duration",
                     "-of", "json", p],
                    text=True, capture_output=True, check=True
                )
                j = json.loads(pr.stdout or "{}")
                st = (j.get("streams") or [{}])[0]
                nb = st.get("nb_frames")
                if nb is not None:
                    return int(nb)
                dur = float(st.get("duration") or 0.0)
                fr = st.get("avg_frame_rate") or "0/1"
                a, b = fr.split("/")
                fps = (float(a) / max(1.0, float(b))) if float(b) != 0 else 0.0
                n = int(math.floor(dur * fps + 0.5))
                return n if n > 0 else None
            except Exception:
                return None

        total = 0
        for p in files:
            n = _probe_one(p)
            if n is None:
                return None
            total += n
        return total if total > 0 else None


    _NVENC_CHECK_CACHE = {"ok": None, "t": 0.0}
    def _nvenc_sanity_check(self, vcodec: str = "h264") -> bool:
        """Quick check whether NVENC can be opened right now."""
        now = time.time()
        if self._NVENC_CHECK_CACHE["ok"] is not None and now - self._NVENC_CHECK_CACHE["t"] < 30:
            return self._NVENC_CHECK_CACHE["ok"]
        
        v = (vcodec or "h264").lower()
        if v.startswith("lib"):  # software encoder → skip check
            return False
        nvenc = "h264_nvenc" if v == "h264" else ("hevc_nvenc" if v == "hevc" else v)

        env = os.environ.copy()
        try:
            system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
            path_items = env.get("PATH", "").split(os.pathsep)
            bad = ["CUDA", "NVIDIA GPU Computing Toolkit", "Video Codec SDK", "nvcodec", "NVEncC"]
            path_items = [p for p in path_items if not any(b.lower() in p.lower() for b in bad)]
            env["PATH"] = os.pathsep.join([system32] + path_items)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(system32)
        except Exception:
            pass

        timeout = 3.0
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=size=16x16:rate=1:color=black",
            "-frames:v", "1",
            "-vf", "format=nv12,hwupload_cuda",
            "-c:v", nvenc, "-preset", "p4",
            "-f", "null", "-"
        ]
        try:
            p = subprocess.run(cmd, env=env, capture_output=True, timeout=timeout, text=True)
            rc = p.returncode
            err = (p.stderr or "") + (p.stdout or "")
            if any(s in err.lower() for s in [
                "incompatible client key",
                "openencodesessionex failed",
                "could not open encoder",
            ]):
                return False            
            self._NVENC_CHECK_CACHE.update({"ok": rc==0, "t": now})
            return (rc == 0)
        except subprocess.TimeoutExpired:
            return False
        except Exception:
            return False

    # ───────── Main execution ─────────
    def run(self,
            file_type, tg_index, cam_index,
            file_directory, file_list,
            target_width, target_height,
            time_start, channel, adjust_info,
            progress_cb=None,
            *, ain_args=None, a_map=None,
            use_shared_audio=True, verbose=False):

        report = self._reporter(tg_index, cam_index, progress_cb)
        report_bar = self._reporter_bar(tg_index, cam_index, progress_cb)

        # Validate inputs
        if not file_list:
            fd_log.error(f"\r❌[0x{file_type:X}][{tg_index}][{cam_index}] empty file_list")
            return None

        # Meta / output settings
        try:
            src_w, src_h, fps_in = _probe_meta_any(file_list[0])
        except Exception:
            src_w, src_h, fps_in = (1920, 1080, 30.0)

        tw, th = _even_size(int(target_width), int(target_height))
        out_path = _final_out_path(file_type, file_directory, time_start, channel)

        match conf._output_fps:
            case 29:
                fps_num, fps_den = 30000, 1001
            case _:
                fps_num, fps_den = 30, 1

        gop       = int(conf._output_gop or 30)
        vcodec    = (conf._output_codec or "h264").lower()
        bitrate_k = int(conf._output_bitrate_k or 800)
        maxrate_k = int(round(bitrate_k * 1.25))
        bufsize_k = int(round(maxrate_k * 2.0))
        profile   = "high" if vcodec == "h264" else ("main10" if (vcodec == "hevc" and getattr(conf, "_output_10bit", False)) else "main")
        timescale = fps_num if fps_den == 1 else 30000

        # Wait previous thread if any
        if conf._thread_file_calibration[tg_index][0] is not None:
            conf._thread_file_calibration[tg_index][0].join()

        # Resolve shared audio path (remux later)
        shared_audio = None
        if use_shared_audio:
            try:
                cand = conf._shared_audio_filename[tg_index]
                if cand and os.path.exists(cand):
                    shared_audio = cand
            except Exception:
                pass

        # Progress total frames
        total_frames = self._probe_frames_total(file_list)

        # NVENC slot + guarded init
        token = None
        try:
            if self.locker:
                token = self.locker.acquire(timeout_sec=30)

            with CalibrationVideo._NVENC_INIT_LOCK:
                import random, time as _time
                _time.sleep(0.05 + random.random() * 0.15)
                use_nvenc_ok = self._nvenc_sanity_check(vcodec)

                # Video-only pipe (audio is remuxed by the dedicated worker)
                proc, pipe = self._spawn_ffmpeg_pipe(
                    out_path, tw, th,
                    fps_out_num=fps_num, fps_out_den=fps_den,
                    vcodec=(vcodec if use_nvenc_ok else "libx264"),
                    gop=gop,
                    rc_mode="vbr", bitrate_k=bitrate_k, maxrate_k=maxrate_k, bufsize_k=bufsize_k,
                    preset="p4", profile=(profile if use_nvenc_ok else None),
                    audio_path=None, a_bitrate_k=128,
                    timescale=timescale, verbose=verbose
                )

            # Drain stderr in background
            stderr_buf = deque(maxlen=20000)           

            def _drain_stderr(p):
                try:
                    if p.stderr:
                        for line in iter(p.stderr.readline, b""):
                            try:
                                stderr_buf.append(line.decode("utf-8", "ignore"))
                            except Exception:
                                pass
                except Exception:
                    pass

            drainer = threading.Thread(target=_drain_stderr, args=(proc,), daemon=True)
            drainer.start()

            # Prepare affine and flip
            M_aff = self._compute_affine_from_adjust(adjust_info, src_w, src_h, tw, th)
            flip_option = conf._flip_option_cam[cam_index]

            # Frame loop
            n_written = 0
            t0 = time.perf_counter()
            frame_bytes = tw * th * 3  # BGR24

            for path in file_list:
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    fd_log.warning(f"[TG:{tg_index:02d}][CAM:{cam_index:02d}] open fail: {path}")
                    continue

                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break

                    # high quality -> slow
                    calibrated = cv2.warpAffine(frame, M_aff, (tw, th), flags=cv2.INTER_LINEAR)

                    # low quality -> fast
                    # calibrated = cv2.warpAffine(frame, M_aff, (tw, th), flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0)

                    if flip_option:
                        calibrated = cv2.rotate(calibrated, cv2.ROTATE_180)
                    # if calibrated.dtype != np.uint8 or not calibrated.flags['C_CONTIGUOUS']:
                    #     calibrated = np.ascontiguousarray(calibrated, dtype=np.uint8)
                    # → 실제로 필요한 케이스에서만 수행
                    if calibrated.dtype is not np.uint8:
                        calibrated = calibrated.astype(np.uint8, copy=False)
                    elif not calibrated.flags['C_CONTIGUOUS']:
                        calibrated = np.ascontiguousarray(calibrated)

                    if proc.poll() is not None:
                        est = "".join(stderr_buf)

                        
                        fd_log.error(f"❌ ffmpeg exited early (written={n_written})\n{est[:20000]}")
                        return None

                    try:
                        mv = memoryview(calibrated).cast('B')
                        total = 0
                        CHUNK = 1 << 20  # 1MB

                        while total < frame_bytes:
                            if proc.poll() is not None:
                                raise BrokenPipeError("ffmpeg exited while writing")
                            n = pipe.write(mv[total: total + CHUNK])
                            if n is None:
                                raise OSError("write returned None")
                            total += n
                        # flush 주기: 64프레임 등으로 확대
                        if (n_written & 63) == 0:
                            pipe.flush()
                        n_written += 1
                    except (BrokenPipeError, OSError) as e:
                        est = "".join(stderr_buf)
                        fd_log.error(f"❌ pipe write failed at frame {n_written}: {e}\n{est[:20000]}")
                        return None

                    if total_frames:
                        pct = (n_written / total_frames) * 100.0
                        if pct <= 100:
                            report_bar(pct, "calibration → pipe(enqueue)")

                cap.release()

            # Close pipe/process
            try:
                pipe.flush()
            except Exception:
                pass
            try:
                pipe.close()
            except Exception:
                pass

            rc = proc.wait()
            try:
                drainer.join(timeout=0.5)
            except Exception:
                pass

            if rc != 0:
                est = "".join(stderr_buf)
                fd_log.error(f"❌ FFmpeg failed (rc={rc})\n{est[:20000]}")
                return None

            # Verify result
            try:
                cap2 = cv2.VideoCapture(out_path)
                frame_count = int(cap2.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = cap2.get(cv2.CAP_PROP_FPS)
                dur = (frame_count / fps) if fps > 0 else 0.0
                fd_log.info(
                    f"🟢[TG:{tg_index:02d}][CAM:{cam_index:02d}] Video-only OK "
                    f"Frames:{frame_count} (sent:{n_written}) FPS:{fps:.3f} Dur(s):{dur:.2f} "
                    f"🕒{(time.perf_counter()-t0):.2f}s"
                )
            except Exception:
                pass

            # Enqueue audio remux
            if shared_audio and os.path.exists(out_path):
                mux_path = os.path.splitext(out_path)[0] + "_mux.mp4"
                ok = self._remux_add_audio_to(out_path, shared_audio, mux_path)
                if ok and self._wait_for_audio_ready(mux_path, timeout_s=90.0):
                    return mux_path
                fd_log.warning(f"[TG:{tg_index:02d}][CAM:{cam_index:02d}] audio remux not confirmed; returning video-only")
            return out_path

        finally:
            if token and self.locker:
                try:
                    self.locker.release()
                except Exception:
                    pass

# ─────────────────────────────────────────────────────────────────────────────
# Main Class on Calibration
# class CalibrationVideoGPU
'''

FD_NVENC_INIT_CONCURRENCY
    NVENC “세션 생성(초기화)”을 동시에 몇 개까지 허용할지 정하는 세마포어 폭.
    값이 작을수록 드라이버/FFmpeg가 서로 동시에 NVENC를 붙잡는 상황이 줄어들어 초기화 실패율이 낮아짐(특히 다캠 동시 시작 시 효과 큼).
    👉 초기화 시점만 직렬화/병렬화에 영향, 인코딩 실행 중 동시성 제한과는 별개.

FD_NVENC_MAX_SLOTS
    한 머신에서 동시에 돌릴 실제 인코딩 파이프라인 개수 상한.
    내부의 NVEncLocker(slots)가 이 숫자만큼 “동시에 인코딩 중” 상태를 허용합니다.
    👉 GPU 부하·VRAM·디스크 IO 한계에 맞춰 적정값을 잡는 핵심 레버.

FD_NVENC_EARLY_FRAMES
    “초기 프레임”으로 간주하는 구간 길이(프레임 수).
    이 구간에서는 파이프 에러/조기 종료 발생 시 공격적으로 재오픈(치유) 시도합니다.
    👉 값이 클수록 초반 불안정 치유엔 유리하지만, 실패가 반복되면 초기화 폭주/지연이 생길 수 있음.

FD_NVENC_EARLY_RETRIES
    초기 프레임 구간에서 허용하는 추가 재오픈 횟수.
    👉 드라이버가 따뜻해질 때까지(첫 세션 잡힐 때까지) 버티는 장치.

FD_NVENC_REOPEN_RETRIES
    초기 구간을 벗어난 일반 구간에서의 재오픈 최대 횟수.
    👉 너무 크면 장시간 지연, 너무 작으면 일시적 오류에서 복구 못 하고 실패.

권장값 가이드

해상도 1280×720@30, 일반적 비트레이트(1–6 Mbps) 기준의 보수→점진 상향 전략입니다.
중요: 드라이버/ffmpeg/nvEncodeAPI DLL이 올바르게 정리되어 있다는 전제입니다.

RTX 3090 (Ampere)

    # ───── 시작값 ─────
    FD_NVENC_INIT_CONCURRENCY=1
    FD_NVENC_MAX_SLOTS=4
    FD_NVENC_EARLY_FRAMES=8
    FD_NVENC_EARLY_RETRIES=4
    FD_NVENC_REOPEN_RETRIES=4
    # ───── 안정적 ─────
    FD_NVENC_INIT_CONCURRENCY=2 → 동시 시작 카메라가 많을 때만 올리기
    FD_NVENC_MAX_SLOTS=6~8 (VRAM/발열/디스크 IO를 보며 2씩 증가)
    팁: 3090은 NVENC 2기(Gen7) 계열로 보통 720p/1080p 다캠에 강함이지만, 동시 초기화가 겹치면 실패율이 오를 수 있어 init concurrency는 1~2가 안전.

RTX 4090 (Ada)

    # ───── 시작값 ─────
    FD_NVENC_INIT_CONCURRENCY=2
    FD_NVENC_MAX_SLOTS=6
    FD_NVENC_EARLY_FRAMES=8
    FD_NVENC_EARLY_RETRIES=4
    FD_NVENC_REOPEN_RETRIES=4
    # ───── 안정적 ─────
    FD_NVENC_INIT_CONCURRENCY=3 (다만 첫 스타트가 동시 10+ 스트림이면 2가 더 안전한 경우도 있음)
    FD_NVENC_MAX_SLOTS=8~10
    팁: Ada 세대 NVENC는 스루풋이 더 좋아 slots를 먼저 올리고, 에러 로그가 깨끗하면 init concurrency를 천천히 올리세요.

“RTX 5090급”(최신 세대, 상위 모델 가정)

    # ───── 시작값 ─────
    FD_NVENC_INIT_CONCURRENCY=2~3
    FD_NVENC_MAX_SLOTS=8
    FD_NVENC_EARLY_FRAMES=6~8
    FD_NVENC_EARLY_RETRIES=3~4
    FD_NVENC_REOPEN_RETRIES=4
    # ───── 안정적 ─────
    
    FD_NVENC_MAX_SLOTS=10~12까지 탐색
    FD_NVENC_INIT_CONCURRENCY=3 유지(대규모 동시 스타트가 잦지 않다면 2도 충분)
    튜닝 순서(현장 체크리스트)

우선 안정화:
    INIT_CONCURRENCY=1 + MAX_SLOTS=4~6 + 재시도(8/4/4 or 8/3/3).
    에러(incompatible client key, openencodesessionex failed)가 사라지는지 확인.

스루풋 확장:
    먼저 MAX_SLOTS를 1~2씩 올려 GPU 사용률·온도·VRAM·디스크 IO를 본다.
    문제 없으면 초기화 충돌이 빈번한 환경(동시에 많은 카메라 시작)에서만 INIT_CONCURRENCY를 2→3으로.

초기구간 재시도 조정:
    특정 머신에서 부팅 직후만 실패가 잦다면 EARLY_FRAMES=10~16, EARLY_RETRIES=5~6로 상향.
    반대로 장시간 잡아늘어지는 느낌이면 6/3으로 축소.

로그 모니터링 기준:
    NVENC 키워드 오류(“incompatible client key”, “could not open encoder”) 빈도 ↓면 OK
    poll_exit/pipe_write_fail 빈도가 늘면 INIT_CONCURRENCY를 다시 1 낮추기
    GPU Util이 상시 95~100%면 MAX_SLOTS 과도—한 단계 낮추기
'''

# ─────────────────────────────────────────────────────────────────────────────
import os
import cv2
import sys
import json
import math
import time
import uuid
import shlex
import random
import tempfile
import subprocess
import threading
import numpy as np
from collections import deque

# Assumed external symbols (same as your original context)
# - conf
# - fd_log
# - _probe_meta_any
# - _even_size
# - _final_out_path
# - NVEncLocker

class CalibrationVideo:

    # ───────── Global / Shared ─────────
    _MUX_INIT_LOCK = threading.Lock()
    _MUX_QUEUE = None
    _MUX_WORKER = None

    _NVENC_INIT_LOCK = threading.Lock()     # NVENC probe serialize
    _NVENC_START_SEM = threading.Semaphore(int(os.environ.get("FD_NVENC_INIT_CONCURRENCY", conf._gpu_session_init_cnt)))
    _FFBIN_OK = None                        # Cache successful ffbin

    EARLY_FRAMES_REOPEN = int(os.environ.get("FD_NVENC_EARLY_FRAMES", "64"))
    EARLY_EXTRA_RETRIES = int(os.environ.get("FD_NVENC_EARLY_RETRIES", "64"))
    MAX_OPEN_RETRIES   = int(os.environ.get("FD_NVENC_REOPEN_RETRIES", "64"))  # normal section

    def __init__(self):
        # concurrent NVENC session slots (default by env or conf)
        slots = int(os.environ.get("FD_NVENC_MAX_SLOTS", conf._gpu_session_max_cnt))
        self.locker = NVEncLocker(slots)

    # default log dir (R:\) overridable by FD_LOG_DIR
    _LOG_DIR = os.environ.get("FD_LOG_DIR", r"R\\")

    # ───────── Log helpers (public-ish) ─────────
    @staticmethod
    def _ensure_dir(p: str):
        try:
            os.makedirs(p, exist_ok=True)
        except Exception:
            pass

    @classmethod
    def dump_outer_diag(cls, tag: str, *, file_type=None, extra: dict | None = None,
                        stderr_tail: str | None = None, proc_cmd: str | None = None) -> str | None:
        """
        External (upper caller) diagnostic dumper.
        Always writes to R:\ (or FD_LOG_DIR). Falls back to temp on failure.
        Returns: final log path or None.
        """
        import datetime as _dt
        try:
            ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")

            base = (getattr(cls, "_LOG_DIR", r"R\\") or r"R\\").strip().strip('"').strip("'")
            if len(base) == 2 and base[1] == ":":
                base += "\\"
            cls._ensure_dir(base)

            log_path = os.path.join(base, f"calib_ffdiag_{tag}_{ts}.log")
            try:
                with open(log_path, "a", encoding="utf-8") as _t:
                    _t.write("")  # touch
            except Exception as e:
                fb = os.path.join(tempfile.gettempdir(), f"calib_ffdiag_{tag}_{ts}.log")
                try:
                    fd_log.error(f"[FFDIAG] fallback to temp due to write error on '{log_path}': {e} → {fb}")
                except Exception:
                    pass
                log_path = fb

            lines = []
            lines.append(f"[FFDIAG] tag={tag} at={ts}")
            if file_type is not None:
                try:
                    lines.append(f"FILE_TYPE=0x{int(file_type):X}")
                except Exception:
                    lines.append(f"FILE_TYPE={file_type}")
            if proc_cmd:
                lines.append(f"cmd={proc_cmd}")
            if extra:
                lines.append("---- EXTRA ----")
                for k, v in extra.items():
                    try:
                        lines.append(f"{k}: {v}")
                    except Exception:
                        lines.append(f"{k}: <unprintable>")
            if stderr_tail:
                lines.append("---- STDERR (tail) ----")
                lines.append(stderr_tail)

            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))

            try:
                fd_log.error(f"[FFDIAG] {tag} → {log_path}")
            except Exception:
                pass
            return log_path
        except Exception as e:
            try:
                fd_log.error(f"[FFDIAG] dump_outer_diag failed: {e}")
            except Exception:
                pass
            return None

    # ───────── MUX worker (audio add) ─────────
    def _ensure_mux_worker(self):
        import queue
        with CalibrationVideo._MUX_INIT_LOCK:
            if CalibrationVideo._MUX_QUEUE is None:
                CalibrationVideo._MUX_QUEUE = queue.Queue()
            if (CalibrationVideo._MUX_WORKER is None) or (not CalibrationVideo._MUX_WORKER.is_alive()):
                t = threading.Thread(target=self._mux_worker_loop, name="MuxWorker", daemon=True)
                t.start()
                CalibrationVideo._MUX_WORKER = t

    def _enqueue_remux(self, video_path: str, audio_path: str, tg_index: int, cam_index: int):
        self._ensure_mux_worker()
        try:
            CalibrationVideo._MUX_QUEUE.put({"video": video_path, "audio": audio_path, "tg": tg_index, "cam": cam_index}, block=False)
            fd_log.info(f"[TG:{tg_index:02d}][CAM:{cam_index:02d}] audio combine queued: {os.path.basename(video_path)}")
        except Exception as e:
            fd_log.error(f"enqueue remux failed: {e}")

    def _mux_worker_loop(self):
        import time as _t, queue
        while True:
            try:
                job = CalibrationVideo._MUX_QUEUE.get()
            except Exception:
                _t.sleep(0.05)
                continue
            if not job:
                continue
            v, a = job.get("video"), job.get("audio")
            tg, cam = job.get("tg"), job.get("cam")
            try:
                if (v and a) and os.path.exists(v) and os.path.exists(a):
                    if self._has_audio_stream(v):
                        fd_log.info(f"[TG:{tg:02d}][CAM:{cam:02d}] has audio: skip")
                    else:
                        ok = self._remux_add_audio(v, a)
                        fd_log.info(f"[TG:{tg:02d}][CAM:{cam:02d}] remux {'ok' if ok else 'fail'}")
                else:
                    fd_log.warning(f"[TG:{tg:02d}][CAM:{cam:02d}] remux skipped (missing)")
            except Exception as e:
                fd_log.error(f"[TG:{tg:02d}][CAM:{cam:02d}] remux exception: {e}")
            finally:
                try:
                    CalibrationVideo._MUX_QUEUE.task_done()
                except Exception:
                    pass

    # ───────── concat (no-check copy) ─────────
    def _concat_to_temp_copy_no_check(self, files) -> str | None:
        """Assume same segment; concat demuxer copy."""
        files = [f for f in files if f and os.path.exists(f)]
        if not files:
            return None
        if len(files) == 1:
            return files[0]

        tmpdir = tempfile.gettempdir()
        list_path = os.path.join(tmpdir, f"concat_{uuid.uuid4().hex}.txt")
        out_path  = os.path.join(tmpdir, f"concat_{uuid.uuid4().hex}.mp4")

        def _ffconcat_quote(path: str) -> str:
            p = os.path.abspath(path).replace("\\", "/")
            return p.replace("'", r"'\''")

        try:
            with open(list_path, "w", encoding="utf-8") as f:
                for p in files:
                    f.write(f"file '{_ffconcat_quote(p)}'\n")

            ff = getattr(sys.modules.get("__main__"), "conf", None)._ffmpeg_path if hasattr(sys.modules.get("__main__"), "conf") else "ffmpeg"
            if not ff:
                ff = "ffmpeg"
            cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
                   "-f", "concat", "-safe", "0", "-i", list_path,
                   "-c", "copy", "-movflags", "+faststart", out_path]
            pr = subprocess.run(cmd, text=True, capture_output=True)
            if pr.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path
            fd_log.error(f"[Concat] failed: {(pr.stderr or '')[:500]}")
            return None
        finally:
            try:
                os.remove(list_path)
            except Exception:
                pass

    # ───────── Progress ─────────
    def _bar10(self, pct: float, width: int = 10) -> str:
        try:
            p = 0 if pct is None else float(pct)
        except Exception:
            p = 0.0
        p = max(0.0, min(100.0, p))
        full = int(p) // 10
        return f"| {'█' * full}{'.' * (width - full)} |"

    def _reporter_bar(self, tg_index, cam_index, progress_cb=None, step: int = 10):
        state = {"last_q": -step}

        def _report(pct: float, msg: str = ""):
            try:
                p = 0 if pct is None else float(pct)
            except Exception:
                p = 0.0
            q = int(max(0, min(100, (int(p) // step) * step)))
            if q <= state["last_q"]:
                return
            state["last_q"] = q
            bar = self._bar10(q)
            try:
                if progress_cb:
                    progress_cb(q, f"{bar} {msg}".strip())
                else:
                    fd_log.info(f"[TG:{int(tg_index):02d}][CAM:{int(cam_index):02d}] {q:3d}% {bar} {msg}".rstrip())
            except Exception:
                pass

        return _report

    # ───────── Adjust → 2x3 affine ─────────
    def _compute_affine_from_adjust(self, adjust_info: dict, src_w: int, src_h: int, tw: int, th: int) -> 'np.ndarray':
        def _T(tx, ty): return np.array([[1, 0, tx], [0, 1, ty], [0, 0, 1]], np.float32)
        def _S(sx, sy=None):
            if sy is None: sy = sx
            return np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], np.float32)
        def _S2(sx, sy): return np.array([[sx, 0, 0], [0, sy, 0], [0, 0, 1]], np.float32)
        def _R_deg(theta):
            th_ = np.deg2rad(theta); c, s = np.cos(th_), np.sin(th_)
            return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)
        def _about(cx, cy, M): return _T(cx, cy) @ M @ _T(-cx, -cy)
        def _M_flip(W, H, fx, fy):
            M = np.eye(3, dtype=np.float32)
            if fx: M[0, 0] = -1.0; M[0, 2] = float(W - 1)
            if fy: M[1, 1] = -1.0; M[1, 2] = float(H - 1)
            return M
        def _M_margin(W, H, mx, my, mw, mh):
            if (W <= 0 or H <= 0 or mw <= 0 or mh <= 0): return np.eye(3, dtype=np.float32)
            sx, sy = float(W) / float(mw), float(H) / float(mh)
            return _S2(sx, sy) @ _T(-float(mx), -float(my))
        def _max_rect_with_aspect(xmin, ymin, xmax, ymax, aspect):
            xmin, ymin, xmax, ymax = map(float, (xmin, ymin, xmax, ymax))
            w = max(0.0, xmax - xmin); h = max(0.0, ymax - ymin)
            if w <= 0 or h <= 0: return 0.0, 0.0, 0.0, 0.0
            w1, h1 = w, w / max(1e-9, aspect)
            if h1 <= h + 1e-6: W, H = w1, h1
            else:              H = h; W = H * aspect
            x = xmin + (w - W) * 0.5; y = ymin + (h - H) * 0.5
            return x, y, W, H

        adj = adjust_info.get("Adjust", adjust_info)
        image_w = float(adj.get("imageWidth", src_w))
        image_h = float(adj.get("imageHeight", src_h))

        if "dAdjustX" in adj and "dAdjustY" in adj:
            dMoveX = float(adj.get("dAdjustX", 0.0)); dMoveY = float(adj.get("dAdjustY", 0.0))
        else:
            nx = float(adj.get("normAdjustX", 0.0)); ny = float(adj.get("normAdjustY", 0.0))
            dMoveX, dMoveY = nx * image_w, ny * image_h

        if "dRotateX" in adj and "dRotateY" in adj:
            dRotateX = float(adj.get("dRotateX", 0.0)); dRotateY = float(adj.get("dRotateY", 0.0))
        else:
            nrx = float(adj.get("normRotateX", 0.0)); nry = float(adj.get("normRotateY", 0.0))
            dRotateX, dRotateY = nrx * image_w, nry * image_h

        dAngle = float(adj.get("dAngle", 0.0)) + 90.0
        dScale = float(adj.get("dScale", 1.0))
        bFlip  = bool(adj.get("bFlip", False))

        rt = adj.get("rtMargin", {}) or {}
        dMarginX = float(rt.get("X", adj.get("dMarginX", 0.0)))
        dMarginY = float(rt.get("Y", adj.get("dMarginY", 0.0)))
        dMarginW = float(rt.get("Width", adj.get("dMarginW", 0.0)))
        dMarginH = float(rt.get("Height", adj.get("dMarginH", 0.0)))

        if (abs(image_w - src_w) > 0.5 or abs(image_h - src_h) > 0.5):
            sx, sy = src_w / image_w, src_h / image_h
            dMoveX *= sx; dMoveY *= sy; dRotateX *= sx; dRotateY *= sy
            dMarginX *= sx; dMarginY *= sy; dMarginW *= sx; dMarginH *= sy

        M_total = (
            _S2(float(tw) / max(1.0, float(src_w)), float(th) / max(1.0, float(src_h))) @
            _M_margin(src_w, src_h, dMarginX, dMarginY, dMarginW, dMarginH) @
            _about(dRotateX, dRotateY, _S(max(dScale, 1e-6))) @
            _about(dRotateX, dRotateY, _R_deg(dAngle)) @
            _T(dMoveX, dMoveY) @
            _M_flip(src_w, src_h, bFlip, bFlip)
        )

        src_corners = np.array([[0, 0, 1], [src_w - 1, 0, 1], [src_w - 1, src_h - 1, 1], [0, src_h - 1, 1]], np.float32)
        dst_pts = (src_corners @ M_total.T)[:, :2]
        xmin, ymin = np.min(dst_pts, axis=0); xmax, ymax = np.max(dst_pts, axis=0)
        xmin, ymin = max(0.0, xmin), max(0.0, ymin)
        xmax, ymax = min(float(tw), xmax), min(float(th), ymax)

        crop_x, crop_y, crop_w, crop_h = _max_rect_with_aspect(xmin, ymin, xmax, ymax, tw / float(th))
        if crop_w > 0 and crop_h > 0:
            C_post = (np.array([[1, 0, tw * 0.5], [0, 1, th * 0.5], [0, 0, 1]], np.float32) @
                      _S2(tw / crop_w, th / crop_h) @
                      _T(-(crop_x + 0.5 * crop_w), -(crop_y + 0.5 * crop_h)))
        else:
            C_post = np.eye(3, np.float32)

        M_total = C_post @ M_total
        return M_total[:2, :].astype(np.float32)

    # ───────── FFmpeg PIPE (rawvideo → NVENC) ─────────
    def _spawn_ffmpeg_pipe(self, out_path, w, h, *, fps_out_num, fps_out_den,
                           vcodec="h264", gop=30, rc_mode="vbr", bitrate_k=800,
                           maxrate_k=None, bufsize_k=None, preset="p4", profile=None,
                           timescale=None, verbose=False, ffbin="ffmpeg"):
        codec_name = "h264_nvenc" if vcodec in ("h264", "libx264") else ("hevc_nvenc" if vcodec in ("hevc", "libx265") else vcodec)
        vf = "format=nv12,hwupload_cuda"  # (unchanged per requirement #2 only)

        rc_args = ["-rc", (rc_mode if rc_mode in ("vbr", "cbr") else "vbr")]
        if bitrate_k: rc_args += ["-b:v", f"{int(bitrate_k)}k"]
        if maxrate_k: rc_args += ["-maxrate", f"{int(maxrate_k)}k"]
        if bufsize_k: rc_args += ["-bufsize", f"{int(bufsize_k)}k"]

        r_out = f"{fps_out_num}/{fps_out_den}"
        ts_args = (["-video_track_timescale", str(int(timescale))] if timescale else [])

        cmd = [
            ffbin, "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", r_out, "-i", "-",
            "-vf", vf, "-vsync", "cfr",
            "-c:v", codec_name, "-preset", preset, "-g", str(int(gop)), "-keyint_min", str(int(gop)),
            "-sc_threshold", "0", "-enc_time_base", "-1"
        ]
        if profile: cmd += ["-profile:v", profile]
        cmd += rc_args + ts_args + ["-movflags", "+faststart", out_path]

        # prefer system32 DLLs (avoid stale local NVENC DLL)
        env = os.environ.copy()
        try:
            system32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
            path_items = env.get("PATH", "").split(os.pathsep)
            bad = ["CUDA", "NVIDIA GPU Computing Toolkit", "Video Codec SDK", "nvcodec", "NVEncC"]
            path_items = [p for p in path_items if not any(b.lower() in p.lower() for b in bad)]
            env["PATH"] = os.pathsep.join([system32] + path_items)
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(system32)
        except Exception:
            pass

        if verbose:
            fd_log.info("FFMPEG PIPE CMD: " + " ".join(cmd))

        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, bufsize=0, text=False, env=env)
        return p, p.stdin

    # ───────── Audio utils ─────────
    @staticmethod
    def _is_aac_file(path: str) -> bool:
        ext = os.path.splitext(path.lower())[1]
        if ext in (".aac", ".m4a", ".mp4", ".mov", ".3gp"):
            try:
                pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                                     "-show_entries", "stream=codec_name", "-of", "json", path],
                                    text=True, capture_output=True, timeout=5.0)
                j = json.loads(pr.stdout or "{}")
                codec = ((j.get("streams") or [{}])[0].get("codec_name") or "").lower()
                return codec == "aac"
            except Exception:
                return ext in (".aac", ".m4a")
        return False

    @staticmethod
    def _has_audio_stream(path: str) -> bool:
        try:
            pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                 "-show_entries", "stream=index", "-of", "json", path],
                                text=True, capture_output=True, timeout=5.0)
            j = json.loads(pr.stdout or "{}")
            return bool(j.get("streams"))
        except Exception:
            return False

    def _remux_add_audio(self, video_path: str, audio_path: str) -> bool:
        if not (video_path and audio_path and os.path.exists(video_path) and os.path.exists(audio_path)):
            return False
        tmp_out = video_path + "._mux.mp4"
        a_args = ["-c:a", "copy"] if self._is_aac_file(audio_path) else ["-c:a", "aac", "-b:a", "128k"]
        cmd = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
               "-i", video_path, "-thread_queue_size", "1024", "-i", audio_path,
               "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", *a_args, "-shortest",
               "-movflags", "+faststart", tmp_out]
        try:
            subprocess.run(cmd, check=True)
        except Exception:
            try:
                os.remove(tmp_out)
            except Exception:
                pass
            return False
        for _ in range(20):
            try:
                os.replace(tmp_out, video_path)
                return True
            except PermissionError:
                time.sleep(0.5)
            except Exception:
                break
        try:
            os.remove(tmp_out)
        except Exception:
            pass
        return False

    # ───────── Misc helpers ─────────
    def _probe_frames_total(self, files):
        def _probe_one(p):
            try:
                pr = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=nb_frames,avg_frame_rate,duration", "-of", "json", p],
                    text=True, capture_output=True, check=True
                )
                j = json.loads(pr.stdout or "{}")
                st = (j.get("streams") or [{}])[0]
                nb = st.get("nb_frames")
                if nb is not None:
                    return int(nb)
                dur = float(st.get("duration") or 0.0)
                fr = st.get("avg_frame_rate") or "0/1"
                a, b = fr.split("/")
                fps = (float(a) / max(1.0, float(b))) if float(b) != 0 else 0.0
                n = int(math.floor(dur * fps + 0.5))
                return n if n > 0 else None
            except Exception:
                return None

        total = 0
        for p in files:
            n = _probe_one(p)
            if n is None:
                return None
            total += n
        return total if total > 0 else None

    # ───────── NVENC probe (with cache) ─────────
    def _nvenc_probe_loop(self, width, height, *, vcodec="h264", ffmpeg_candidates=None,
                          tries=999999, base_delay=1.0, max_delay=5.0):
        """Probe NVENC until ready (GPU-only). On success, cache ffbin and return it."""
        if CalibrationVideo._FFBIN_OK:
            return CalibrationVideo._FFBIN_OK

        if ffmpeg_candidates is None:
            ffmpeg_candidates = [
                getattr(sys.modules.get("__main__"), "conf", None)._ffmpeg_path if hasattr(sys.modules.get("__main__"), "conf") else None,
                r"C:\4DReplay\v4_aid\ffmpeg.exe",
                r"C:\4DReplay\v4_aid\libraries\ffmpeg\ffmpeg.exe",
                "ffmpeg"
            ]
        ffmpeg_candidates = [p for p in ffmpeg_candidates if p]
        delay = base_delay

        for _ in range(1, tries + 1):
            for ff in ffmpeg_candidates:
                cmd = [
                    ff, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=size={width}x{height}:rate=1:color=black",
                    "-frames:v", "1",
                    "-vf", "format=nv12,hwupload_cuda",
                    "-c:v", ("h264_nvenc" if vcodec in ("h264", "libx264") else "hevc_nvenc"),
                    "-preset", "p4", "-enc_time_base", "-1",
                    "-f", "null", "-"
                ]
                try:
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=4.0)
                    if p.returncode == 0:
                        CalibrationVideo._FFBIN_OK = ff
                        try:
                            devs = cv2.cuda.getCudaEnabledDeviceCount()
                            fd_log.info(f"[Init] have_cuda={devs>0}, devices={devs}, ffmpeg='{ff}', NVENC=READY")
                        except Exception:
                            fd_log.info(f"[Init] ffmpeg='{ff}', NVENC=READY")
                        return ff
                except FileNotFoundError:
                    pass
                except subprocess.TimeoutExpired:
                    pass
                except Exception:
                    pass
            time.sleep(delay)
            delay = min(max_delay, delay * 1.5)

    # ───────── Main ─────────
    def run(self,
            file_type, tg_index, cam_index,
            file_directory, file_list,
            target_width, target_height,
            time_start, channel, adjust_info,
            progress_cb=None,
            *, ain_args=None, a_map=None,
            use_shared_audio=True, verbose=False):

        report_bar = self._reporter_bar(tg_index, cam_index, progress_cb)

        if not file_list:
            fd_log.error(f"❌[0x{file_type:X}][{tg_index}][{cam_index}] empty file_list")
            return None

        # meta & output
        try:
            src_w, src_h, fps_in = _probe_meta_any(file_list[0])
        except Exception:
            src_w, src_h, fps_in = (1920, 1080, 30.0)

        tw, th = _even_size(int(target_width), int(target_height))
        out_path = _final_out_path(file_type, file_directory, time_start, channel)

        if getattr(sys.modules.get("__main__"), "conf", None) and getattr(sys.modules.get("__main__").conf, "_output_fps", 30) == 29:
            fps_num, fps_den = 30000, 1001
        else:
            fps_num, fps_den = 30, 1

        gop       = int(getattr(sys.modules.get("__main__").conf, "_output_gop", 30) if hasattr(sys.modules.get("__main__"), "conf") else 30)
        vcodec    = (getattr(sys.modules.get("__main__").conf, "_output_codec", "h264") if hasattr(sys.modules.get("__main__"), "conf") else "h264").lower()
        if vcodec in ("h265", "x265"):
            vcodec = "hevc"
        bitrate_k = int(getattr(sys.modules.get("__main__").conf, "_output_bitrate_k", 800) if hasattr(sys.modules.get("__main__"), "conf") else 800)
        maxrate_k = int(round(bitrate_k * 1.25))
        bufsize_k = int(round(maxrate_k * 2.0))
        profile   = "high" if vcodec in ("h264", "libx264") else ("main10" if (vcodec in ("hevc", "libx265") and getattr(sys.modules.get("__main__").conf, "_output_10bit", False) if hasattr(sys.modules.get("__main__"), "conf") else False) else "main")
        timescale = fps_num if fps_den == 1 else 30000
        preset    = "p4"

        # concat → one input
        concat_input = self._concat_to_temp_copy_no_check(file_list) or file_list[0]
        input_seq = [concat_input]
        total_frames = self._probe_frames_total(input_seq)

        token = None
        tmp_concat_to_delete = concat_input if (len(file_list) > 1 and concat_input not in file_list) else None

        # Reopen counters
        max_open_retries = CalibrationVideo.MAX_OPEN_RETRIES
        early_limit = CalibrationVideo.EARLY_FRAMES_REOPEN
        early_extra = CalibrationVideo.EARLY_EXTRA_RETRIES

        # helpers for open/close pipe with backoff
        open_attempt = 0
        start_sem_held = False
        proc = None
        pipe = None

        def _open_pipe(ffbin):
            nonlocal proc, pipe, start_sem_held
            if not start_sem_held:
                CalibrationVideo._NVENC_START_SEM.acquire()
                start_sem_held = True
            proc, pipe = self._spawn_ffmpeg_pipe(
                out_path, tw, th,
                fps_out_num=fps_num, fps_out_den=fps_den,
                vcodec=vcodec, gop=gop,
                rc_mode="vbr", bitrate_k=bitrate_k, maxrate_k=maxrate_k, bufsize_k=bufsize_k,
                preset=preset, profile=profile, timescale=timescale, verbose=verbose,
                ffbin=(CalibrationVideo._FFBIN_OK or "ffmpeg")
            )
            # stderr drainer
            stderr_buf.clear()
            threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

        def reopen_with_backoff(extra=False):
            nonlocal open_attempt
            limit = max_open_retries + (early_extra if extra else 0)
            if open_attempt >= limit:
                return False
            open_attempt += 1
            try:
                try: pipe and pipe.close()
                except Exception: pass
                try: proc and proc.wait(timeout=2)
                except Exception: pass
            except Exception:
                pass
            time.sleep(0.25 + random.random() * 0.35)
            with CalibrationVideo._NVENC_INIT_LOCK:
                ffbin_local = CalibrationVideo._FFBIN_OK or self._nvenc_probe_loop(tw, th, vcodec=vcodec)
            _open_pipe(ffbin_local)
            return True

        # stderr buffer & drainer
        stderr_buf = deque(maxlen=20000)

        def _drain_stderr(p):
            try:
                if p.stderr:
                    for line in iter(p.stderr.readline, b""):
                        try:
                            stderr_buf.append(line.decode("utf-8", "ignore"))
                        except Exception:
                            pass
            except Exception:
                pass

        def _ffdiag(tag: str, *, proc=None, rc=None, n_written_snapshot=None, extra: dict | None = None):
            """
            Detailed diagnostic dump to R:\ (or FD_LOG_DIR). Falls back to temp on failure.
            """
            import datetime as _dt
            try:
                ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                base_dir = r"R:\\"

                log_path = os.path.join(base_dir, f"calib_ffdiag_{tag}_{ts}.log")
                try:
                    with open(log_path, "a", encoding="utf-8") as _probe:
                        _probe.write("")  # touch
                except Exception as e:
                    fb = os.path.join(tempfile.gettempdir(), f"calib_ffdiag_{tag}_{ts}.log")
                    try:
                        fd_log.error(f"[FFDIAG] fallback to temp due to write error on '{log_path}': {e} → {fb}")
                    except Exception:
                        pass
                    log_path = fb

                try:
                    stderr_tail = "".join(list(stderr_buf)[-20000:])
                except Exception:
                    stderr_tail = "<stderr unavailable>"

                try:
                    pid = getattr(proc, "pid", None)
                except Exception:
                    pid = None

                try:
                    if proc and getattr(proc, "args", None):
                        if isinstance(proc.args, (list, tuple)):
                            proc_cmd = " ".join(shlex.quote(str(x)) for x in proc.args)
                        else:
                            proc_cmd = str(proc.args)
                    else:
                        proc_cmd = "<proc.args unavailable>"
                except Exception:
                    proc_cmd = "<proc.args error>"

                try:
                    fps_s = f"{fps_num}/{fps_den}"
                except Exception:
                    fps_s = "<unknown>"

                try:
                    size_s = f"{tw}x{th}"
                except Exception:
                    size_s = "<unknown>"

                lines = []
                lines.append(f"[FFDIAG] tag={tag} at={ts}")
                lines.append(f"TG={tg_index} CAM={cam_index} FILE_TYPE=0x{file_type:X}")
                lines.append(f"OUT={out_path}")
                lines.append("")
                lines.append("---- ENCODER PARAMS ----")
                lines.append(f"vcodec={vcodec} preset={preset} profile={profile} gop={gop}")
                lines.append(f"bitrate_k={bitrate_k} maxrate_k={maxrate_k} bufsize_k={bufsize_k}")
                lines.append(f"fps={fps_s} timescale={timescale} size={size_s}")
                lines.append("")
                lines.append("---- PROCESS ----")
                lines.append(f"pid={pid} rc={rc if rc is not None else '<unknown>'} n_written={n_written_snapshot}")
                lines.append(f"args={proc_cmd}")
                lines.append("")
                if extra:
                    lines.append("---- EXTRA ----")
                    try:
                        for k, v in extra.items():
                            lines.append(f"{k}: {v}")
                    except Exception:
                        lines.append("<failed to render extra>")
                    lines.append("")
                lines.append("---- FFMPEG STDERR (tail) ----")
                lines.append(stderr_tail)

                with open(log_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))

                try:
                    fd_log.error(f"[FFDIAG] {tag} → {log_path}")
                except Exception:
                    pass

            except Exception as e:
                try:
                    fd_log.error(f"[FFDIAG] exception while dumping diag ({tag}): {e}")
                except Exception:
                    pass

        try:
            if self.locker:
                token = self.locker.acquire(timeout_sec=30)

            # NVENC ready (wait until success) + cache
            with CalibrationVideo._NVENC_INIT_LOCK:
                ffbin = CalibrationVideo._FFBIN_OK or self._nvenc_probe_loop(tw, th, vcodec=vcodec)
            _open_pipe(ffbin)

            # CUDA init
            have_cuda = False
            stream = None
            try:
                have_cuda = (cv2.cuda.getCudaEnabledDeviceCount() > 0)
                if have_cuda:
                    stream = cv2.cuda.Stream()
            except Exception:
                have_cuda = False

            # affine/flip
            M_aff = self._compute_affine_from_adjust(adjust_info, src_w, src_h, tw, th)
            flip_option = sys.modules.get("__main__").conf._flip_option_cam[cam_index] if hasattr(sys.modules.get("__main__"), "conf") else 0
            M180 = np.array([[-1, 0, tw - 1], [0, -1, th - 1]], np.float32)

            # reader helpers
            def _open_reader(path):
                if have_cuda and hasattr(cv2, "cudacodec"):
                    try:
                        rdr = cv2.cudacodec.createVideoReader(path)
                        return ("cuda", rdr)
                    except Exception:
                        pass
                cap = cv2.VideoCapture(path)
                return ("cpu", cap)

            def _read_one(kind, reader):
                if kind == "cuda":
                    ok, gpu = reader.nextFrame()
                    return ok, gpu
                else:
                    return reader.read()

            # frame loop
            n_written = 0
            frame_bytes = tw * th * 3

            # ───────── Watchdog: stall detection settings ─────────
            STALL_SEC = float(os.environ.get("FD_NVENC_STALL_SEC", 8.0))
            last_write_ts = time.time()

            for path in input_seq:
                kind, reader = _open_reader(path)
                if kind == "cpu" and (not reader or not reader.isOpened()):
                    fd_log.error(f"open fail: {path}")
                    _ffdiag("early_return_open_fail",
                            n_written_snapshot=n_written,
                            extra={"src_path": path, "reader_kind": kind})
                    return None

                while True:
                    ok, f = _read_one(kind, reader)
                    if not ok:
                        break

                    if kind == "cuda":
                        gpu = f
                        try:
                            if gpu.channels() == 4:
                                gpu = cv2.cuda.cvtColor(gpu, cv2.COLOR_BGRA2BGR, stream=stream)
                        except Exception:
                            pass
                        gpu_out = cv2.cuda.warpAffine(gpu, M_aff, (tw, th), flags=cv2.INTER_LINEAR, stream=stream)
                        if flip_option:
                            gpu_out = cv2.cuda.warpAffine(gpu_out, M180, (tw, th), flags=cv2.INTER_NEAREST, stream=stream)
                        if stream:
                            stream.waitForCompletion()
                        calibrated = gpu_out.download()
                    else:
                        if f.ndim == 3 and f.shape[2] == 4:
                            f = cv2.cvtColor(f, cv2.COLOR_BGRA2BGR)
                        calibrated = cv2.warpAffine(f, M_aff, (tw, th), flags=cv2.INTER_LINEAR)
                        if flip_option:
                            calibrated = cv2.warpAffine(calibrated, M180, (tw, th), flags=cv2.INTER_NEAREST)

                    if calibrated.dtype is not np.uint8:
                        calibrated = calibrated.astype(np.uint8, copy=False)
                    elif not calibrated.flags['C_CONTIGUOUS']:
                        calibrated = np.ascontiguousarray(calibrated)

                    # early pipe-exit check
                    if proc.poll() is not None:
                        time.sleep(0.05)  # allow stderr to fill
                        est = "".join(stderr_buf).lower()
                        nverr = ("incompatible client key" in est or
                                 "could not open encoder" in est or
                                 "openencodesessionex failed" in est or
                                 "error while opening encoder" in est or
                                 "no capable devices found" in est or
                                 "device not supported for this encoder" in est or
                                 "nvencinitializeencoder failed" in est)
                        if n_written < early_limit:
                            if reopen_with_backoff(extra=True):
                                # restart writing this same frame after reopen
                                last_write_ts = time.time()
                                # fallthrough to write logic below
                            else:
                                _ffdiag("ffmpeg exited early", rc=proc.returncode, n_written_snapshot=n_written,
                                        extra={"stderr_hint_contains_nvenc_err": nverr})
                                fd_log.error(f"❌ ffmpeg exited early (written={n_written})")
                                try:
                                    reader.release()
                                except Exception:
                                    pass
                                return None
                        elif nverr and reopen_with_backoff(extra=False):
                            last_write_ts = time.time()
                        else:
                            _ffdiag("ffmpeg exited early", rc=proc.returncode, n_written_snapshot=n_written,
                                    extra={"stderr_hint_contains_nvenc_err": nverr})
                            fd_log.error(f"❌ ffmpeg exited early (written={n_written})")
                            try:
                                reader.release()
                            except Exception:
                                pass
                            return None

                    # write 1 frame (chunked, with stall watchdog)
                    try:
                        mv = memoryview(calibrated).cast('B')
                        total = 0
                        CHUNK = 1 << 20  # 1 MiB

                        while total < frame_bytes:
                            # ── Stall watchdog inside the tight chunk loop
                            if (time.time() - last_write_ts) > STALL_SEC:
                                # consider stalled: dump diag, kill ffmpeg, reopen, and retry this frame from start
                                _ffdiag("ffmpeg_stall_detected",
                                        proc=proc,
                                        rc=(proc.returncode if proc else None),
                                        n_written_snapshot=n_written,
                                        extra={"stall_sec": STALL_SEC, "total_written_bytes_for_frame": total})
                                try:
                                    proc.kill()
                                except Exception:
                                    pass
                                # Try reopen (prefer 'extra' if early frames)
                                if reopen_with_backoff(extra=(n_written < early_limit)):
                                    # restart writing this frame from the beginning
                                    total = 0
                                    last_write_ts = time.time()
                                    continue
                                else:
                                    fd_log.error("❌ ffmpeg stall not recoverable")
                                    raise BrokenPipeError("ffmpeg stall unrecoverable")

                            if proc.poll() is not None:
                                if n_written < early_limit and reopen_with_backoff(extra=True):
                                    total = 0
                                    last_write_ts = time.time()
                                    continue
                                raise BrokenPipeError("ffmpeg exited while writing")

                            n = pipe.write(mv[total: total + CHUNK])
                            if n is None:
                                raise OSError("write returned None")
                            total += n

                            # optional flush guarding
                            # (flush every 64 frames outside this loop as before)

                        if (n_written & 63) == 0:
                            try:
                                pipe.flush()
                            except Exception:
                                pass

                        n_written += 1
                        # successful write: refresh watchdog timestamp
                        last_write_ts = time.time()

                    except (BrokenPipeError, OSError):
                        time.sleep(0.05)
                        est = "".join(stderr_buf).lower()
                        nverr = ("incompatible client key" in est or
                                 "could not open encoder" in est or
                                 "openencodesessionex failed" in est or
                                 "error while opening encoder" in est or
                                 "no capable devices found" in est or
                                 "device not supported for this encoder" in est or
                                 "nvencinitializeencoder failed" in est)
                        if n_written < early_limit:
                            if reopen_with_backoff(extra=True):
                                # retry this frame from start on next loop
                                last_write_ts = time.time()
                                continue
                        if nverr and reopen_with_backoff(extra=False):
                            last_write_ts = time.time()
                            continue

                        _ffdiag("pipe_write_fail",
                                rc=proc.returncode if proc else None,
                                n_written_snapshot=n_written,
                                extra={"stderr_hint_contains_nvenc_err": nverr})
                        fd_log.error(f"❌ pipe write failed at frame {n_written}")
                        try:
                            reader.release()
                        except Exception:
                            pass
                        return None

                    if total_frames:
                        pct = (n_written / total_frames) * 100.0
                        if pct <= 100:
                            report_bar(pct, "calibration → encode(GPU only)")

                try:
                    reader.release()
                except Exception:
                    pass

            # finalize writer
            try:
                try:
                    pipe.flush()
                except Exception:
                    pass
                try:
                    pipe.close()
                except Exception:
                    pass
                rc = proc.wait()
            except Exception as e:
                _ffdiag("finalize pipe failed", rc=(proc.returncode if proc else None),
                        n_written_snapshot=(n_written if 'n_written' in locals() else None),
                        extra={"exc": repr(e)})
                fd_log.error(f"finalize pipe failed: {e}")
                return None

            if rc != 0:
                _ffdiag("FFmpeg failed", rc=rc, n_written_snapshot=n_written)
                fd_log.error(f"❌ FFmpeg failed (rc={rc})")
                return None

            # join prev thread
            if hasattr(sys.modules.get("__main__"), "conf") and sys.modules.get("__main__").conf._thread_file_calibration[tg_index][0] is not None:
                sys.modules.get("__main__").conf._thread_file_calibration[tg_index][0].join()

            # shared audio
            shared_audio = None
            if use_shared_audio:
                try:
                    cand = sys.modules.get("__main__").conf._shared_audio_filename[tg_index]
                    if cand and os.path.exists(cand):
                        shared_audio = cand
                except Exception:
                    pass

            # audio remux (in-place)
            final_out = out_path
            if shared_audio and os.path.exists(out_path):
                ok = self._remux_add_audio(out_path, shared_audio)
                if ok:
                    final_out = out_path
            return final_out

        finally:
            if 'token' in locals() and token and self.locker:
                try:
                    self.locker.release()
                except Exception:
                    pass
            if 'start_sem_held' in locals() and start_sem_held:
                try:
                    CalibrationVideo._NVENC_START_SEM.release()
                except Exception:
                    pass
            # delete temp concat file
            if 'tmp_concat_to_delete' in locals() and tmp_concat_to_delete and os.path.exists(tmp_concat_to_delete):
                try:
                    os.remove(tmp_concat_to_delete)
                except Exception:
                    pass


def calibration_video(file_type, tg_index, cam_index, file_directory, file_list,
                      target_width, target_height, time_start, channel, adjust_info,
                      progress_cb=None):
    """
    기존 시그니처 유지용 래퍼 (input_buffer → file_list 로 변경).
    conf._output_fps/_output_bitrate/_output_codec 적용.
    """
    runner = CalibrationVideo()
    return runner.run(
        file_type=file_type, tg_index=tg_index, cam_index=cam_index,
        file_directory=file_directory, file_list=file_list,
        target_width=target_width, target_height=target_height,
        time_start=time_start, channel=channel, adjust_info=adjust_info,
        progress_cb=progress_cb
    )



# ─────────────────────────────────────────────────────────────────────────────
# resize_and_shift_video(file_type, input_file, zoom_scale, center_x, center_y)
# [owner] hongsu jung
# [date] 2025-05-18
# ─────────────────────────────────────────────────────────────────────────────
def resize_and_shift_video(file_type, input_file, zoom_scale, center_x, center_y):
    
    # time check
    t_start = time.perf_counter()

    iw, ih = conf._input_width, conf._input_height
    ow, oh = conf._output_width // conf._multi_ch_analysis, conf._output_height
    if file_type in (
                    conf._type_baseball_hit,
                    conf._type_baseball_hit_manual,
                    conf._type_baseball_hit_multi
                ):
        ow, oh = conf._resolution_fhd_width, conf._resolution_fhd_height

    crop_w = int(iw / zoom_scale)
    crop_h = int(crop_w * (oh / ow))
    crop_h = min(crop_h, int(ih / zoom_scale))
    crop_w = int(crop_h * (ow / oh))
    crop_x = max(0, min(int(center_x - crop_w / 2), iw - crop_w))
    crop_y = max(0, min(int(center_y - crop_h / 2), ih - crop_h))

    vf_filter = ",".join([
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={ow}:{oh}:force_original_aspect_ratio=disable,"        
        "unsharp=5:5:1.0:5:5:0.0", # 🔍 여기에 추가!
        f"setpts={conf._input_fps / conf._output_fps:.3f}*PTS"
    ])

    # get fixed named path
    if(file_type == conf._file_type_prev):
        process_file = fd_get_output_file(file_type)
    else:
        process_file = fd_get_clean_file(file_type)

    ffmpeg_cmd = [
        *fd_common_ffmpeg_args_pre(),
        "-i", input_file,
        "-vf", vf_filter,
        *fd_common_ffmpeg_args_post(),
        "-r", str(conf._output_fps),
        process_file
    ]

    # debug
    #fd_log.info(f"\r4️⃣ [0x{file_type:X}][Resize/Shift] command:{ffmpeg_cmd}")

    process = subprocess.run(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.remove(input_file)
    
    if process.returncode != 0:
        fd_log.info(f"\r❌[0x{file_type:X}] Resize+crop failed:\n{process.stderr.decode(errors='ignore')}")
        return None

    # time check
    t_end = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000

    fd_log.info(f"\r4️⃣ [0x{file_type:X}][Resize/Shift][🕒:{elapsed_ms:,.2f} ms] x,y({crop_x},{crop_y}) w,h({crop_w},{crop_h})")
    return process_file

# ─────────────────────────────────────────────────────────────────────────────
# def process_video_parts(file_directory, camera_ip_class, camera_ip, file_type):
# [owner] hongsu jung
# [date] 2025-03-10
# ─────────────────────────────────────────────────────────────────────────────
def process_video_parts(file_directory, camera_ip_class, camera_ip, file_type):

    check_player = False
    match file_type:
        case conf._file_type_prev:
            time_start = conf._proc_prev_time_start
            time_end = conf._proc_prev_time_end
            frame_start = conf._proc_prev_frm_start
            frame_end = conf._proc_prev_frm_end
        case conf._file_type_curr:
            time_start = conf._proc_curr_time_start
            time_end = conf._proc_curr_time_end
            frame_start = conf._proc_curr_frm_start
            frame_end = conf._proc_curr_frm_end
        case conf._file_type_post:
            time_start = conf._proc_post_time_start
            time_end = conf._proc_post_time_end
            frame_start = conf._proc_post_frm_start
            frame_end = conf._proc_post_frm_end
        case conf._file_type_last:
            time_start = conf._proc_last_time_start
            time_end = conf._proc_last_time_end
            frame_start = conf._proc_last_frm_start
            frame_end = conf._proc_last_frm_end
        case conf._file_type_front | conf._file_type_side | conf._file_type_back:
            time_start = conf._proc_full_time_start
            time_end = conf._proc_full_time_end
            frame_start = conf._proc_full_frm_start
            frame_end = conf._proc_full_frm_end
            check_player = True
        case _:
            fd_log.info(f"\r❌[0x{file_type:X}] not exist fittable file type in create_combine_video")
            return

    file_combine_list = generate_file_list(file_directory, camera_ip_class, camera_ip, time_start, time_end)
    if not file_combine_list:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file list")
        return False

    # 1️⃣ 병합
    combined_buf = combine_video(file_type, file_combine_list)
    if combined_buf is None:
        return False

    # 2️⃣ 트리밍
    trimmed_path = trim_frames(file_type, combined_buf, frame_start, frame_end, conf._input_fps)
    if trimmed_path is None:
        return False

    # 3️⃣ 리사이즈
    target_width = conf._output_width
    target_height = conf._output_height

    # 4️⃣ 회전 (세로영상)
    if conf._image_portrait:
        trimmed_path = rotate_video(file_type, trimmed_path)
        if trimmed_path is None:
            return False

    # 5️⃣ Zoom / Shift (골프용)
    if check_player:
        is_detect, zoom_scale, center_x, center_y = fd_get_video_on_player(trimmed_path, file_type)
        if not is_detect:
            return False
        fd_log.info(f"✅[detect][0x{file_type:X}] scale: {zoom_scale}")
        fd_log.info(f"✅[detect][0x{file_type:X}] position: {center_x},{center_y}")
        process_file = resize_and_shift_video(file_type, trimmed_path, zoom_scale, center_x, center_y)
    else:
        process_file = resize_video(file_type, trimmed_path, target_width, target_height)

    if process_file is None:
        fd_log.info(f"Error in process_video_parts [file type]:{file_type}")
        return False

    # 6️⃣ 메모리 버퍼에 저장
    conf._mem_temp_file[file_type] = process_file

    # 7️⃣ Clean Feed 저장
    # 2025-08-09
    # using for fd_set_mem_file_multiline
    # save_clean_feed(file_type, process_file)

    # 8️⃣ 프레임 추출
    match file_type:
        case conf._file_type_prev: conf._frames_prev                        = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_curr: conf._frames_curr                        = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_post: conf._frames_post                        = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_front: conf._frames_front                      = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_side: conf._frames_side                        = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_back: conf._frames_back                        = fd_extract_frames_from_file(process_file, file_type)
        case conf._file_type_cali:
            # file copy to destination
            # get file name
            file_name = process_file.split("\\")[-1]
            dest_path = f"{conf._folder_output}/{file_name}"
            # copy to destination
            shutil.copy(process_file, dest_path)
            fd_log.info(f"✅[0x{file_type:X}] saved to {dest_path}")
            return True
                
    fd_log.info(f"\r🚩[0x{file_type:X}][Loaded]")
 
# ─────────────────────────────────────────────────────────────────────────────
# def process_video_parts_pipe(file_directory, camera_ip_class, camera_ip, file_type):
# [owner] hongsu jung
# [date] 2025-09-19
# ─────────────────────────────────────────────────────────────────────────────
def process_video_parts_pipe(file_directory, tg_index, cam_index, camera_ip_class, camera_ip, file_type, t_start = 0, f_start = 0, t_end = 0, f_end = 0, channel = 0, adjust_info = 0, shared_audio = False):

    fd_log.info(f"🚀 [TG:{tg_index:02d}][CAM:{cam_index:02d}] Start process_video_parts_pipe")
    file_list = generate_file_list(file_directory, camera_ip_class, camera_ip, t_start, t_end)
    if not file_list:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file list")
        return False
    
    target_width = conf._output_width
    target_height = conf._output_height

    process_file = calibration_video(file_type, tg_index, cam_index, file_directory, file_list, target_width, target_height, t_start, channel, adjust_info)
    
    if process_file is None:
        fd_log.info(f"Error in process_video_parts_pipe [file type]:{file_type}")
        return False

    # 메모리 버퍼에 저장
    conf._mem_temp_file[file_type] = process_file

    # 프레임 추출
    file_name = process_file.split("\\")[-1].replace("_mux", "")
    dest_path = f"{conf._folder_output}/{file_name}"
    # copy to destination
    shutil.copy(process_file, dest_path)
    fd_log.info(f"✅[0x{file_type:X}][{tg_index}][{cam_index}] Saved to {dest_path}")
    return True
    

# ─────────────────────────────────────────────────────────────────────────────
# generate_file_list(file_directory, camera_ip_class, camera_ip, start_time, end_time):
# [owner] hongsu jung
# [date] 2025-03-12
# 파일 리스트를 생성하고 존재 여부 확인
# ─────────────────────────────────────────────────────────────────────────────
def generate_file_list(file_directory, camera_ip_class, camera_ip, start_time, end_time):
    
    file_list = []    
    for i in range(start_time, end_time + 1):
        file_name = f"{file_directory}/{camera_ip_class:03d}{int(camera_ip):03d}_{i}.mp4"
        if os.path.exists(file_name):
            file_list.append(file_name)
        else:
            fd_log.warning(f"⚠️[File-List] Warning: non exist {file_name}")
    
    if not file_list:
        fd_log.error(f"❌[File-List] Error: there is no video file.{file_name}")
        return None

    return file_list
  
# ─────────────────────────────────────────────────────────────────────────────
# find_frames_on_file(start_sec, end_sec, start_ms, end_ms):
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
def find_frames_from_time(start_sec, end_sec, start_ms, end_ms):
    start_frame_in_file = round((start_ms%1000) / ( 1000 / conf._input_frame_count))
    if(end_ms == 0):
        end_frame_in_file = 0
    else:
        length_ms = end_ms-start_ms
        end_frame_in_file = start_frame_in_file + round(length_ms / ( 1000 / conf._input_frame_count))
    return start_frame_in_file, end_frame_in_file

# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_4part():
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
def fd_set_mem_file_4parts():

    folder_input                = conf._folder_input
    camera_ip_class             = conf._camera_ip_class
    camera_ip                   = conf._camera_ip
    start_time                  = conf._start_sec_from_moment
    end_time                    = conf._end_sec_from_moment

    # ─────────────────────────────────────────────────────────────────────────────
    # get file directory, filename
    # ─────────────────────────────────────────────────────────────────────────────        
    file_directory              = folder_input
    file_base                   = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, camera_ip_class, int(camera_ip), conf._selected_moment_sec)
    file_group_base             = os.path.splitext(os.path.basename(file_base))[0]
    file_group                  = f"{file_group_base}_{conf._selected_moment_frm}"
    
    # check exist file
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
    # set start/end frame and time
    # ─────────────────────────────────────────────────────────────────────────────
    match conf._type_target:
        case conf._type_baseball_pitcher:
            detect_prev_frm = conf._pitcher_detect_prev_frame
            detect_post_frm = conf._pitcher_detect_post_frame
            afterimage_ms   = conf._pitcher_ball_afterimage_frame * 1000 / conf._input_frame_count
        case conf._type_baseball_batter_RH | \
             conf._type_baseball_batter_LH: 
            detect_prev_frm = conf._batter_detect_prev_frame
            detect_post_frm = conf._batter_detect_post_frame
            afterimage_ms   = conf._batter_ball_afterimage_frame * 1000 / conf._input_frame_count
        case conf._type_baseball_hit | conf._type_baseball_hit_manual : 
            detect_prev_frm = conf._hit_detect_prev_frame
            detect_post_frm = conf._batter_detect_post_frame
            flying_time_ms  = int(conf._landingflat_hangtime * 1000)
            # 2025-05-26
            # sync with hang time
            flying_time_ms  += 3 * (1000 / conf._input_frame_count)
            afterimage_ms   = conf._hit_ball_afterimage_frame * 1000 / conf._input_frame_count
            end_time        = conf._hit_detect_post_sec * 1000
        case _:
            fd_log.info("❌ wrong type in fd_set_mem_file_4parts():")
            return
    
    selected_moment_ms  = conf._selected_moment_sec * 1000 + (conf._selected_moment_frm / conf._input_frame_count * 1000)
    # ─────────────────────────────────────────────────────────────────────────────
    # Get Previous Time and Frame (Empty frame)
    # ─────────────────────────────────────────────────────────────────────────────
    prev_start_ms   = selected_moment_ms + start_time
    prev_end_ms     = selected_moment_ms + detect_prev_frm / conf._input_frame_count * 1000
    conf._proc_prev_time_start  = int(prev_start_ms/1000)
    conf._proc_prev_time_end    = int(prev_end_ms/1000)
    conf._proc_prev_frm_start, conf._proc_prev_frm_end = find_frames_from_time (conf._proc_prev_time_start, conf._proc_prev_time_end, prev_start_ms, prev_end_ms)
    
    # ─────────────────────────────────────────────────────────────────────────────
    # Get Current Time and Frame (Detect) 
    # ─────────────────────────────────────────────────────────────────────────────
    curr_start_ms   = prev_end_ms
    if(conf._type_target == conf._type_baseball_hit or conf._type_target == conf._type_baseball_hit_manual):
        curr_end_ms     = selected_moment_ms + flying_time_ms
    else:
        curr_end_ms     = selected_moment_ms + detect_post_frm / conf._input_frame_count * 1000
    conf._proc_curr_time_start  = int(curr_start_ms/1000)
    conf._proc_curr_time_end    = int(curr_end_ms/1000)
    conf._proc_curr_frm_start, conf._proc_curr_frm_end = find_frames_from_time (conf._proc_curr_time_start, conf._proc_curr_time_end, curr_start_ms, curr_end_ms)    

    # ─────────────────────────────────────────────────────────────────────────────
    # Get Post Time and Frame (Afterimage) 
    # ─────────────────────────────────────────────────────────────────────────────
    post_start_ms   = curr_end_ms 
    post_end_ms     = post_start_ms + afterimage_ms
    conf._proc_post_time_start  = int(post_start_ms/1000)
    conf._proc_post_time_end    = int(post_end_ms/1000)
    conf._proc_post_frm_start, conf._proc_post_frm_end = find_frames_from_time (conf._proc_post_time_start, conf._proc_post_time_end, post_start_ms, post_end_ms)    

    # ─────────────────────────────────────────────────────────────────────────────
    # Get Last Time and Frame (Empty frame)
    # ─────────────────────────────────────────────────────────────────────────────
    last_start_ms   = post_end_ms 
    last_end_ms     = last_start_ms + end_time
    conf._proc_last_time_start  = int(last_start_ms/1000)
    conf._proc_last_time_end    = int(last_end_ms/1000)
    conf._proc_last_frm_start, conf._proc_last_frm_end = find_frames_from_time (conf._proc_last_time_start, conf._proc_last_time_end, last_start_ms, last_end_ms)    

    # ─────────────────────────────────────────────────────────────────────────────
    # create each combined files
    # 2025-03-12
    # multi thread excution
    # ─────────────────────────────────────────────────────────────────────────────    
    conf._thread_file_prev = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, camera_ip, conf._file_type_prev))
    conf._thread_file_curr = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, camera_ip, conf._file_type_curr))
    conf._thread_file_post = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, camera_ip, conf._file_type_post))
    conf._thread_file_last = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, camera_ip, conf._file_type_last))
    
    conf._thread_file_prev.start()
    conf._thread_file_curr.start()
    conf._thread_file_post.start()
    conf._thread_file_last.start()
    
    # wait until finish process_thread_curr
    conf._thread_file_curr.join()
    conf._thread_file_post.join()
        
    return True, file_directory, file_group

# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_calis_audio():
# [owner] hongsu jung
# [date] 2025-09-13
# 기준이 되는 audio file을 만들어서 공유할 수 있게 한다.
# ─────────────────────────────────────────────────────────────────────────────
def fd_set_mem_file_calis_audio(file_path, cam_ip_class, cam_ip, channel, tg_index, start_time, start_frame, end_time, end_frame):    
    # get output file name
    fd_log.info(f"🎬 fd_set_mem_file_calis_audio channel={channel}, file={file_path}, start_time={start_time}, end_time={end_time}")
    
    conf._thread_file_calibration[tg_index][0] = threading.Thread(target=process_audio_parts, args=(file_path, tg_index, cam_ip_class, cam_ip, conf._file_type_cali_audio, start_time, start_frame, end_time, end_frame))
    conf._thread_file_calibration[tg_index][0].start()
    
    #    process_audio_parts(file_path, tg_index, cam_ip_class, cam_ip, conf._file_type_cali_audio, start_time, start_frame, end_time, end_frame)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_calis():
# [owner] hongsu jung
# [date] 2025-09-13
# 실제 파일들을 합친 이후, Calibration을 적용
# ─────────────────────────────────────────────────────────────────────────────
def fd_set_mem_file_calis(file_path, cam_ip_class, cam_ip, channel, tg_index, cam_index, start_time, start_frame, end_time, end_frame, adjust_set, cam_audio):

    # validate file
    adjust_dsc_id = adjust_set.get("DscID","")   
    if(adjust_dsc_id != f"{cam_ip_class:03d}{cam_ip:03d}"):
        fd_log.error(f"❌ Invalid AdjustData DSC_ID:{adjust_dsc_id} for ip_class={cam_ip_class}, ip={cam_ip}")
        return False

    # get output file name
    fd_log.info(f"🎬 fd_set_mem_file_calis channel={channel}, file={file_path}, Time = ({start_time},{start_frame}~{end_time},{end_frame}")

    # sound option
    if cam_audio:
        shared_audio = False
    else:
        shared_audio = True

    # get adjust info    
    adjust_info = adjust_set.get("Adjust","")  

    conf._thread_file_calibration[tg_index][cam_index+1] = threading.Thread(target=process_video_parts_pipe, args=(file_path, tg_index, cam_index, cam_ip_class, cam_ip, conf._file_type_cali, start_time, start_frame, end_time, end_frame, channel, adjust_info, shared_audio))
    conf._thread_file_calibration[tg_index][cam_index+1].start()

    # non thread
    #process_video_parts_new(file_path, tg_index, cam_index, cam_ip_class, cam_ip, conf._file_type_cali, start_time, start_frame, end_time, end_frame, channel, adjust_info, shared_audio)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_swing_analysis():
# [owner] hongsu jung
# [date] 2025-04-29
# front, back
# ─────────────────────────────────────────────────────────────────────────────
def fd_set_mem_file_swing_analysis():
    folder_input                = conf._folder_input
    camera_ip_class             = conf._camera_ip_class
    start_time                  = conf._start_sec_from_moment
    end_time                    = conf._end_sec_from_moment
    #reset
    conf._multi_ch_analysis     = 1    

    # set front, side camera
    numbers = re.findall(r'\d+', conf._camera_ip)
    if len(numbers) == 2:
        conf._multi_ch_analysis = 2
        conf._front_camera_index = int(numbers[0])
        camera_ip = conf._front_camera_index
        conf._side_camera_index = int(numbers[1])
    elif len(numbers) == 3:
        conf._multi_ch_analysis = 3
        conf._front_camera_index = int(numbers[0])
        camera_ip = conf._front_camera_index
        conf._side_camera_index = int(numbers[1])
        conf._back_camera_index = int(numbers[2])
    else:
        conf._multi_ch_analysis = 1
        fd_log.info (f"[Error] non enough input camera id {conf._camera_ip}")
        return False, None, None
    
    # set camera count
    conf._cnt_analysis_camera = numbers    

    # ─────────────────────────────────────────────────────────────────────────────
    # get file directory, filename
    # ─────────────────────────────────────────────────────────────────────────────        
    file_directory              = folder_input
    file_base                   = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, camera_ip_class, camera_ip, conf._selected_moment_sec)
    file_group_base             = os.path.splitext(os.path.basename(file_base))[0]
    file_group                  = f"{file_group_base}_{conf._selected_moment_frm}"
    
    # check exist file
    if file_exist(file_base) is False:
        return False, "", ""
    # get input file info    
    cap = cv2.VideoCapture(file_base)
    conf._input_fps             = cap.get(cv2.CAP_PROP_FPS)
    conf._input_frame_count     = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    conf._input_width           = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    conf._input_height          = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)    
    cap.release()


    selected_moment_ms  = conf._selected_moment_sec * 1000 + (conf._selected_moment_frm / conf._input_frame_count * 1000)
    # ─────────────────────────────────────────────────────────────────────────────    
    # Get Previous Time and Frame (Empty frame)
    # ─────────────────────────────────────────────────────────────────────────────    
    start_ms   = selected_moment_ms + start_time
    end_ms     = selected_moment_ms + end_time
    conf._proc_full_time_start  = int(start_ms/1000)
    conf._proc_full_time_end    = int(end_ms/1000)
    conf._proc_full_frm_start, conf._proc_full_frm_end = find_frames_from_time (conf._proc_full_time_start, conf._proc_full_time_end, start_ms, end_ms)

    # ─────────────────────────────────────────────────────────────────────────────
    # create each combined files
    # 2025-03-12
    # multi thread excution
    # ─────────────────────────────────────────────────────────────────────────────
    
    process_thread_front    = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, conf._front_camera_index, conf._file_type_front))
    process_thread_side     = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, conf._side_camera_index, conf._file_type_side))
    process_thread_front.start()
    process_thread_side.start()
    # 3-CH
    if conf._multi_ch_analysis >= 3:
        process_thread_back = threading.Thread(target=process_video_parts, args=(file_directory, camera_ip_class, conf._back_camera_index, conf._file_type_back))
        process_thread_back.start()
        

    # wait until finish process_thread_front
    process_thread_front.join()
    process_thread_side.join()
    # 3-CH
    if(conf._multi_ch_analysis >= 3):
        process_thread_back.join()
        
    return True, file_directory, file_group

# ─────────────────────────────────────────────────────────────────────────────
# def find_longist_hang_pkl_file(pkl_list):
# [owner] hongsu jung
# [date] 2025-07-01
# ─────────────────────────────────────────────────────────────────────────────
def find_longist_hang_pkl_file(pkl_list):
    max_long_hangtime = -1
    max_long_distance = -1
    max_index = -1

    for i, (path, distance, hangtime, angle) in enumerate(pkl_list):
        try:
            arr_ball = fd_load_array_file(path)
            if isinstance(arr_ball, list) or isinstance(arr_ball, tuple):
                if hangtime > max_long_hangtime:
                    max_long_hangtime = hangtime
                    max_index = i

                if distance > max_long_distance:
                    max_long_distance = distance
                    
        except Exception as e:
            fd_log.warning(f"⚠️ {path} 로드 실패: {e}")
    
    # set hang time
    conf._landingflat_hangtime = max_long_hangtime
    conf._landingflat_distance = max_long_distance
    return max_index

# ─────────────────────────────────────────────────────────────────────────────
# def ordeing_pkl_file_from_angle(pkl_list):
# [owner] hongsu jung
# [date] 2025-07-02
# ─────────────────────────────────────────────────────────────────────────────
def ordering_pkl_file_from_angle(pkl_list):
    def angle_sort_key(item):
        angle = item[3]
        if angle < 0:
            # 음수 angle: 그룹 0, 절대값 기준 오름차순
            return (0, abs(angle))
        else:
            # 양수 angle: 그룹 1, 값 기준 오름차순
            return (1, angle)

    pkl_list.sort(key=angle_sort_key)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_multiline():
# [owner] hongsu jung
# [date] 2025-07-03
# ─────────────────────────────────────────────────────────────────────────────
def fd_set_mem_file_multiline():

    n_line_count = len(conf._pkl_list)
    if n_line_count < 1:
        fd_log.error(f"❌ not exist pkl file list")
        return False, "", ""
    
    # ordering from angle
    ordering_pkl_file_from_angle(conf._pkl_list)
    
    pkl_list = conf._pkl_list
    # find long hang time
    longist_index = find_longist_hang_pkl_file(pkl_list)
    if longist_index < 0:
        fd_log.error("❌ 유효한 pkl 파일이 없습니다.")
        return False, "", ""
    
    conf._longist_multi_line_index = longist_index

    # find 1st array
    path = pkl_list[longist_index][0]
    parent_dir = os.path.dirname(path)  # 전체 폴더 경로
    filename = os.path.basename(path)
    name_only = os.path.splitext(filename)[0]  # 확장자 제거
    # '_' 기준 분리
    parts = name_only.split('_')

    if len(parts) >= 4:
        camera_ip_class = int(parts[0][:3])
        camera_ip       = int(parts[0][3:])
        mement_sec      = int(parts[1])
        mement_frm      = int(parts[2])
    else:
        fd_log.error("❌ 파일명 형식이 올바르지 않습니다.")
        return False, "", ""

    conf._folder_input          = parent_dir
    conf._camera_ip_class       = camera_ip_class
    conf._camera_ip             = camera_ip
    conf._selected_moment_sec   = mement_sec
    conf._selected_moment_frm   = mement_frm
    
    # get clean feeds
    conf._output_datetime       = fd_get_datetime(parent_dir)    
    conf._clean_file_list       = fd_get_clean_file_name(conf._local_temp_folder)

    # ─────────────────────────────────────────────────────────────────────────────
    # get file directory, filename
    # ─────────────────────────────────────────────────────────────────────────────        
    file_directory              = parent_dir
    file_base                   = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, camera_ip_class, camera_ip, conf._selected_moment_sec)
    file_group_base             = os.path.splitext(os.path.basename(file_base))[0]
    file_group                  = f"{file_group_base}_{conf._selected_moment_frm}"
    
    # check exist file
    if file_exist(file_base) is False:
        return False, "", ""
    # get input file info    
    cap = cv2.VideoCapture(file_base)
    conf._input_fps             = cap.get(cv2.CAP_PROP_FPS)
    conf._input_frame_count     = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    conf._input_width           = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    conf._input_height          = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()


    # ─────────────────────────────────────────────────────────────────────────────####
    # create each combined files
    # 2025-07-04
    # use exist file from previou detection
    # ─────────────────────────────────────────────────────────────────────────────####

    conf._thread_file_prev = threading.Thread(target=load_clean_file, args=(conf._file_type_prev,))
    conf._thread_file_curr = threading.Thread(target=load_clean_file, args=(conf._file_type_curr,))
    conf._thread_file_post = threading.Thread(target=load_clean_file, args=(conf._file_type_post,))
    conf._thread_file_last = threading.Thread(target=load_clean_file, args=(conf._file_type_last,))
    
    conf._thread_file_prev.start()
    conf._thread_file_curr.start()
    conf._thread_file_post.start()
    conf._thread_file_last.start()

    conf._thread_file_prev.join()
    conf._thread_file_curr.join()
    conf._thread_file_post.join()
    conf._thread_file_last.join()
        
    return True, file_directory, file_group

# ─────────────────────────────────────────────────────────────────────────────
# def load_clean_file(file_type):
# [owner] hongsu jung
# [date] 2025-07-04
# ─────────────────────────────────────────────────────────────────────────────
def load_clean_file(file_type):
    
    match file_type:
        case conf._file_type_prev:
            process_file = conf._clean_file_list[0]
            #check exist
            if file_exist(process_file):
                clean_file = fd_get_output_file(file_type)
                conf._mem_temp_file[file_type] = clean_file
                file_copy(process_file, clean_file)            
                conf._frames_prev = fd_extract_frames_from_file(process_file, file_type )
            else:
                fd_log.error(f"❌ [Load][Clean][{file_type:X}]")
        case conf._file_type_curr:
            process_file = conf._clean_file_list[1]
            #check exist
            if file_exist(process_file):
                clean_file = fd_get_clean_file(file_type)
                conf._mem_temp_file[file_type] = clean_file
                file_copy(process_file, clean_file)                        
                conf._frames_curr = fd_extract_frames_from_file(process_file, file_type )
            else:
                fd_log.error(f"❌ [Load][Clean][{file_type:X}]")
        case conf._file_type_post:
            process_file = conf._clean_file_list[2]
            #check exist
            if file_exist(process_file):
                clean_file = fd_get_clean_file(file_type)
                conf._mem_temp_file[file_type] = clean_file
                file_copy(process_file, clean_file)                        
                conf._frames_post = fd_extract_frames_from_file(process_file, file_type )
            else:
                fd_log.error(f"❌ [Load][Clean][{file_type:X}]")
        case conf._file_type_last:
            process_file = conf._clean_file_list[3]
            #check exist
            if file_exist(process_file):
                clean_file = fd_get_clean_file(file_type)
                conf._mem_temp_file[file_type] = clean_file
                file_copy(process_file, clean_file)          
            else:
                fd_log.error(f"❌ [Load][Clean][{file_type:X}]")  
    
    fd_log.e(f"\r🚩[0x{file_type:X}][Loaded]")

# ─────────────────────────────────────────────────────────────────────────────
# def fd_multi_channel_configuration(file_path, frame):
# [owner] hongsu jung
# [date] 2025-03-10
# ─────────────────────────────────────────────────────────────────────────────    
def fd_multi_channel_configuration(type_devision, folder_output, camera_ip_class):
    if conf._make_time:
        conf._output_datetime = conf._make_time
        conf._make_time = ""
    
    match type_devision:
        case conf._type_1_ch:   file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_1ch.mp4"
        case conf._type_2_ch_h: file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_2ch.mp4"
        case conf._type_2_ch_v: file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_2ch.mp4"
        case conf._type_3_ch_h: file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_3ch.mp4"
        case conf._type_3_ch_m: file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_3ch.mp4"
        case conf._type_4_ch:   file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_4ch.mp4"
        case conf._type_9_ch:   file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_9ch.mp4"
        case conf._type_16_ch:  file_output     = f"{folder_output}/shorts_{conf._output_datetime}_{conf._team_info}_multi_16ch.mp4"
    return file_output

# ─────────────────────────────────────────────────────────────────────────────
# def get_zoom_info(index):
# [owner] hongsu jung
# [date] 2025-03-10
# ─────────────────────────────────────────────────────────────────────────────    
def get_zoom_info(index):
    zoom_ratio_cnt = len(conf._zoom_ratio_list)
    if(zoom_ratio_cnt > 1):
        zoom_ratio = conf._zoom_ratio_list[index]
    else:
        zoom_ratio = conf._zoom_ratio

    zoom_x_cnt = len(conf._zoom_center_x_list)
    if(zoom_x_cnt > 1):
        zoom_center_x = conf._zoom_center_x_list[index]
    else:
        zoom_center_x = conf._zoom_center_x

    zoom_y_cnt = len(conf._zoom_center_y_list)
    if(zoom_y_cnt > 1):
        zoom_center_y = conf._zoom_center_y_list[index]
    else:
        zoom_center_y = conf._zoom_center_y

    return zoom_ratio, zoom_center_x, zoom_center_y
    
# ─────────────────────────────────────────────────────────────────────────────
# def fd_set_mem_file_multi_ch(devision_camerases, folder_input, camera_ip_class, start_time, select_frame, end_time):
# [owner] hongsu jung
# [date] 2025-03-10
# ─────────────────────────────────────────────────────────────────────────────    
def fd_set_mem_file_multi_ch(devision_camerases, folder_input, camera_ip_class, start_time, select_frame, end_time):

    conf._folder_input          = folder_input
    conf._camera_ip_class       = camera_ip_class
    conf._start_sec_from_moment = start_time
    conf._end_sec_from_moment   = end_time

    # ─────────────────────────────────────────────────────────────────────────────####
    # set files
    # ─────────────────────────────────────────────────────────────────────────────####   
    if isinstance(devision_camerases, str):
        numbers = re.findall(r'\d+', devision_camerases)
    elif isinstance(devision_camerases, int):
        numbers = [devision_camerases]  # 숫자 하나를 리스트에 넣기
    else:
        raise TypeError(f"Unsupported type for devision_camerases: {type(devision_camerases)}")     
    
    numbers = [int(num) for num in numbers]
    cnt_cameras = len(numbers)
    conf._cnt_analysis_camera = cnt_cameras
    base_camera_ip = numbers[0]
    fd_log.info(numbers)
    
    # ─────────────────────────────────────────────────────────────────────────────
    # get file directory, filename
    # ─────────────────────────────────────────────────────────────────────────────        
    file_directory  = folder_input
    file_base       = "{0}/{1:03d}{2:03d}_{3}.mp4".format(file_directory, camera_ip_class, base_camera_ip, conf._selected_moment_sec)
        
    # check exist file
    if file_exist(file_base) is False:
        return False, ""
    # get fps
    cap = cv2.VideoCapture(file_base)
    conf._input_fps             = cap.get(cv2.CAP_PROP_FPS)
    conf._input_frame_count     = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    conf._input_width           = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    conf._input_height          = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    selected_moment_ms  = conf._selected_moment_sec * 1000 + (conf._selected_moment_frm / conf._input_frame_count * 1000)
    # ─────────────────────────────────────────────────────────────────────────────    
    # Get Previous Time and Frame (Empty frame)
    # ─────────────────────────────────────────────────────────────────────────────    
    multi_start_ms   = selected_moment_ms + start_time
    multi_end_ms     = selected_moment_ms + end_time
    conf._proc_multi_time_start  = int(multi_start_ms/1000)
    conf._proc_multi_time_end    = int(multi_end_ms/1000)
    conf._proc_multi_frm_start, conf._proc_multi_frm_end = find_frames_from_time (conf._proc_multi_time_start, conf._proc_multi_time_end, multi_start_ms, multi_end_ms)
    
    # ─────────────────────────────────────────────────────────────────────────────
    # create each combined files
    # 2025-03-12
    # multi thread excution
    # ─────────────────────────────────────────────────────────────────────────────
    conf._file_combine_list = []
    threads = [] 
    
    for idx,num in enumerate(numbers):
        camera_ip = int(num)
        zoom_ratio, zoom_center_x, zoom_center_y = get_zoom_info(idx)
        file_combined_each = "{0}/{1:03d}{2:03d}_{3}_{4}_{5}_combined.mp4".format(file_directory, camera_ip_class, camera_ip, conf._selected_moment_sec, conf._selected_moment_frm, camera_ip)
        conf._file_combine_list.append(file_combined_each)        

        process_thread = threading.Thread(target=create_multi_combine_video, args=(file_directory, camera_ip_class, camera_ip, idx, zoom_ratio, zoom_center_x, zoom_center_y))
        if process_thread:
            process_thread.start()        
            threads.append(process_thread)  # 실행된 스레드 리스트에 추가
        else:
            fd_log.error(f"❌ [ERROR][{camera_ip}] thread : fd_set_multi_files") 

    # 모든 스레드가 종료될 때까지 대기
    for thread in threads:
        thread.join()

    conf._cnt_analysis_camera = len(conf._file_combine_list)
    return True, conf._file_combine_list

# ─────────────────────────────────────────────────────────────────────────────
# def create_multi_combine_video(file_directory, camera_ip_class, camera_ip, file_index, zoom_ratio, zoom_center_x, zoom_center_y):
# [owner] hongsu jung
# [date] 2025-07-01
# ─────────────────────────────────────────────────────────────────────────────
def create_multi_combine_video(file_directory, camera_ip_class, camera_ip, file_index, zoom_ratio, zoom_center_x, zoom_center_y):
    
    time_start  = conf._proc_multi_time_start
    time_end    = conf._proc_multi_time_end
    frame_start = conf._proc_multi_frm_start
    frame_end   = conf._proc_multi_frm_end            

    file_combine_list = generate_file_list(file_directory, camera_ip_class, camera_ip, time_start, time_end)    
    if not file_combine_list:
        fd_log.info(f"\r❌[multi files][{file_index}] there is no file list")
        return False


    start_time = time.perf_counter()  # 시작 시간 기록    
    # ─────────────────────────────────────────────────────────────────────────────    
    # 1️⃣ 병합 (combine_video_files)
    # ─────────────────────────────────────────────────────────────────────────────    
    merged_buf = combine_video(file_index, file_combine_list)
    if(merged_buf is None):
        return False    
    end_time = time.perf_counter()  # 시작 시간 기록   
    fd_log.info(f"\r🕒[merge][{file_index}] {(end_time - start_time) * 1000:,.2f} ms") 
    
    # ─────────────────────────────────────────────────────────────────────────────    
    # 2️⃣ zoom
    # ─────────────────────────────────────────────────────────────────────────────    
    if zoom_ratio != 1.0:
        start_time = time.perf_counter()
        zoomed_buf = resize_video(file_index, merged_buf, zoom_ratio, zoom_center_x, zoom_center_y)
        if zoomed_buf is None:
            return False
        end_time = time.perf_counter()
        fd_log.info(f"\r🕒[zoom][{file_index}][{zoom_ratio}%({zoom_center_x},{zoom_center_y})] {(end_time - start_time) * 1000:,.2f} ms") 
    else:
        zoomed_buf = merged_buf  # 이후는 zoom된 영상 사용        
    # ─────────────────────────────────────────────────────────────────────────────
    # 3️⃣ resize video
    # ─────────────────────────────────────────────────────────────────────────────
    
    target_width    = conf._output_width
    target_height   = conf._output_height

    if(     conf._type_target == conf._type_2_ch_h or \
            conf._type_target == conf._type_2_ch_v):
        target_width    = int(target_width/2)
        target_height   = int(target_height)
    elif(   conf._type_target == conf._type_3_ch_h or \
            conf._type_target == conf._type_3_ch_m):
        target_width    = int(target_width/3)
        target_height   = int(target_height)
    elif(   conf._type_target == conf._type_4_ch):
        target_width    = int(target_width/2)
        target_height   = int(target_height/2)
    elif(   conf._type_target == conf._type_9_ch):
        target_width    = int(target_width/3)
        target_height   = int(target_height/3)
    elif(   conf._type_target == conf._type_16_ch):
        target_width    = int(target_width/4)
        target_height   = int(target_height/4)    
    
    # ─────────────────────────────────────────────────────────────────────────────    
    # 프레임 트리밍 (trim_frames_buffer)
    # ─────────────────────────────────────────────────────────────────────────────    
    start_time = time.perf_counter()  # 시작 시간 기록    
    trimmed_path = trim_frames(file_index, zoomed_buf, frame_start, frame_end, conf._output_fps)
    end_time = time.perf_counter()  # 시작 시간 기록   
    fd_log.info(f"\r🕒[trim][{file_index}] {(end_time - start_time) * 1000:,.2f} ms") 
    
    # erase timestamp
    # customer needs 
    # 2025-04-24
    #conf._mem_temp_file[file_index] = trim_buf.getvalue()
    conf._mem_temp_file[file_index] = trimmed_path
    
# ─────────────────────────────────────────────────────────────────────────────
# def fd_combine_calibrated_output(camera_groups):
# [owner] hongsu jung
# [date] 2025-09-23
# For each camera group, collect segment files and run combine_segments_simple in a separate thread.
# Wait until all threads are finished before exiting.    
# ─────────────────────────────────────────────────────────────────────────────    
def fd_combine_calibrated_output(camera_groups):    
    fd_log.info("combine files during the sequences")
    file_directory = conf._folder_input
    threads = []

    for camera in camera_groups:
        channel = camera.get("channel")
        times   = camera.get("start_times", [])
        if not times:
            fd_log.warning(f"⚠️ Camera[{channel}] has no start_times")
            continue

        first_time = None
        combine_file_list = []

        # Collect all segment files for this channel
        for idx, start_time in enumerate(times):
            file_name = fd_get_cali_file(conf._file_type_cali, file_directory, start_time, channel)
            combine_file_list.append(file_name)
            if idx == 0:
                first_time = start_time

        if first_time is None:
            fd_log.warning(f"⚠️ Camera[{channel}] has invalid first_time")
            continue

        # Generate output file name
        output_file_name = fd_get_cali_file(
            conf._file_type_cali,
            file_directory,
            first_time,
            channel,
            False
        )

        # Check if all input files exist
        missing_files = [f for f in combine_file_list if not os.path.exists(f)]
        if missing_files:
            for mf in missing_files:
                fd_log.error(f"❌ Missing source file: {mf}")
            fd_log.error(f"⛔ Skipping combine for Camera[{channel}] due to missing sources")
            continue

        fd_log.info(f"🚀 [Combine] Dest:{output_file_name} <- from:{combine_file_list}")

        # Worker thread for combining
        def _worker(files, out_file, ch):
            try:
                combine_segments_simple(files, out_file, ch)
                fd_log.info(f"✅ [Combine Done] Camera[{ch}] -> {out_file}")
            except Exception as e:
                fd_log.exception(f"💥 [Combine Failed] Camera[{ch}] -> {e}")

        t = threading.Thread(target=_worker, args=(combine_file_list, output_file_name, channel), daemon=False)
        t.start()
        threads.append(t)

    # Wait for all threads to finish
    for t in threads:
        t.join()

    fd_log.info("🚩 All combine threads finished")


# ─────────────────────────────────────────────────────────────────────────────
# def process_audio_parts_sync(file_directory, camera_ip_class, camera_ip, file_type, t_start, f_start, t_end, f_end):
# sync 보장된 버전
# [owner] hongsu jung
# [date] 2025-10-10
# ─────────────────────────────────────────────────────────────────────────────
def process_audio_parts_sync(file_directory, tg_index, camera_ip_class, camera_ip,
                        file_type, t_start, f_start, t_end, f_end,
                        *, ar=48000, ac=2,
                        fade_ms=3,              # 각 1초 내부 미세 페이드(길이 불변)
                        use_tail_sweeten=True,  # 경계부의 '0에 가까운 꼬리'를 살짝 살리는 길이-불변 보정
                        ofps_override=None):
    """
    프레임-정확, 길이-보전 오디오 생성 (싱크 확정판 + 길이 불변 보정):
      1) 1초 WAV normalize (길이 1.000000s, 소규모 페이드 in/out만)
      2) ffconcat + aresample(async=1)로 미세 드리프트 보정 (길이 불변)
      3) (옵션) compand 기반 'tail sweeten' – 아주 작은 레벨의 꼬리만 살짝 들어 올려 연결감 개선 (길이 불변)
      4) 총 프레임 수 기반 end_sample 잠금 (길이 하드락 → 영상과 절대 싱크)
      5) AAC 1회 인코드
    ※ crossfade/atempo/겹치기 없음. 무음 대체 없음(모든 세그먼트에 오디오가 있다고 가정).
    """
    import os, uuid, subprocess, shlex, json
    from pathlib import PureWindowsPath

    # ───────── helpers ─────────
    def _run(cmd, check=True):
        p = subprocess.run(cmd, text=True, capture_output=True)
        if check and p.returncode != 0:
            raise RuntimeError(
                "FFmpeg failed:\nCMD: " + " ".join(shlex.quote(c) for c in cmd) +
                "\nSTDERR:\n" + (p.stderr or "")
            )
        return p

    def _ensure_dir(p):
        if p:
            os.makedirs(p, exist_ok=True)

    def _to_ffconcat_path(p: str) -> str:
        # ffconcat 전용 경로 정규화: UNC → //host/share/..., 로컬 → C:/...
        if not p:
            return p
        p_win = str(PureWindowsPath(p))
        if p_win.startswith("\\\\"):
            return "//" + p_win[2:].replace("\\", "/")
        return p_win.replace("\\", "/")

    def _has_audio_stream(path: str) -> bool:
        pr = subprocess.run(
            ["ffprobe","-v","error","-select_streams","a:0",
             "-show_entries","stream=index","-of","json", path],
            text=True, capture_output=True
        )
        if pr.returncode != 0:
            return False
        try:
            j = json.loads(pr.stdout or "{}")
            return bool(j.get("streams"))
        except Exception:
            return False

    def _normalize_1s_to_wav(in_media: str, out_wav: str, ar_=48000, ac_=2):
        """
        입력 세그먼트 → 정확 1.000000s WAV
        - 내부 페이드(예: 3ms in/out)는 '길이 불변'으로만 사용
        - 오디오 스트림이 없으면 예외(무음 대체 안함)
        """
        _ensure_dir(os.path.dirname(out_wav) or ".")
        if not _has_audio_stream(in_media):
            raise RuntimeError(f"no audio stream: {in_media}")

        fad = max(0, int(fade_ms)) / 1000.0
        af = "atrim=0:1.0,asetpts=PTS-STARTPTS"
        if fad > 0:
            af += f",afade=t=in:st=0:d={fad:.3f},afade=t=out:st={1.0 - fad:.3f}:d={fad:.3f}"

        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-probesize","50M","-analyzeduration","5M",
            "-i", in_media,
            "-map","a:0?","-vn",
            "-af", af,
            "-ar", str(int(ar_)), "-ac", str(int(ac_)),
            "-c:a","pcm_s16le",
            out_wav
        ])

    # ───────── inputs & FPS ─────────
    time_start = t_start
    time_end   = t_end

    file_list = generate_file_list(file_directory, camera_ip_class, camera_ip, time_start, time_end)
    if not file_list:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file list")
        return None

    ofps = getattr(conf, "_output_fps", 30) if ofps_override is None else ofps_override
    fps = (30000 / 1001.0) if ofps == 29 else 30.0

    # 싱크 OK 버전과 동일한 프레임/샘플 산정
    total_frames   = max(1, int(round((time_end - time_start) * fps)))
    target_samples = int(round(total_frames * (ar / fps)))

    # ───────── paths ─────────
    work_root = os.path.join(file_directory if os.path.isdir(file_directory) else "R:\\", "_audio_work")
    work_dir  = os.path.join(work_root, f"tg{tg_index:02d}_{time_start:06d}_{time_end:06d}")
    _ensure_dir(work_dir)

    try:
        base = fd_get_cali_file(file_type, file_directory, time_start)
    except Exception:
        base = os.path.join(work_dir, f"audio_{uuid.uuid4().hex}")
    root, _   = os.path.splitext(base)
    concat_txt = os.path.join(work_dir, f"audio_{uuid.uuid4().hex}.ffconcat")
    joined_wav = root + "._joined.wav"
    sweet_wav  = root + "._sweet.wav"    # 길이-불변 보정 결과(옵션)
    locked_wav = root + "._locked.wav"
    out_m4a    = root + "._locked.m4a"

    fd_log.info(f"🚀[TG:{tg_index:02d}] audio start | frames={total_frames}, fps={fps:.6f}, target_samples={target_samples}")

    # ───────── 1) 각 1초 → 정확 1초 WAV ─────────
    wavs = []
    for i, seg in enumerate(file_list):
        w = os.path.join(work_dir, f"norm_{i:05d}.wav")
        _normalize_1s_to_wav(seg, w, ar_=ar, ac_=ac)
        wavs.append(w)

    # ───────── 2) ffconcat (file→duration, 마지막 duration 생략) ─────────
    with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        last = len(wavs) - 1
        for i, p in enumerate(wavs):
            f.write(f"file '{_to_ffconcat_path(p)}'\n")
            if i != last:
                f.write("duration 1.000000\n")

    # ───────── 3) concat → 단일 WAV (+ aresample async=1) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-f","concat","-safe","0","-i", _to_ffconcat_path(concat_txt),
        "-vn",
        "-af","aresample=async=1:min_hard_comp=0.10:comp_duration=1:first_pts=0",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s16le",
        joined_wav
    ])

    # ───────── 3.5) (옵션) 길이-불변 tail sweeten ─────────
    # compand로 -55dB 이하 꼬리만 소폭 들어 올려 '0처럼 들리는' 단절감을 완화.
    # 길이 불변 필터이므로 타임라인/싱크 불변.
    src_for_lock = joined_wav
    if use_tail_sweeten:
        # points: input_dB/output_dB 쌍. 아주 작은 구간만 살짝(+8~10dB) 올리고,
        # -40dB 이상은 거의 그대로. attacks/decays를 짧게(수 ms)로 해서 경계만 반응.
        compand = (
            "compand=attacks=0.005:decays=0.050:"
            "points=-80/-72|-70/-62|-60/-50|-50/-40|-40/-39|-30/-29|-20/-19|0/0:"
            "gain=0:volume=0:delay=0"
        )
        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-i", joined_wav, "-vn",
            "-af", compand,
            "-ar", str(int(ar)), "-ac", str(int(ac)),
            "-c:a","pcm_s16le",
            sweet_wav
        ])
        src_for_lock = sweet_wav

    # ───────── 4) 총 샘플수로 하드락(절대 싱크) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", src_for_lock, "-vn",
        "-af", f"apad=pad_dur=3600,atrim=end_sample={target_samples},asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s16le",
        locked_wav
    ])

    # ───────── 5) AAC 인코드(1회) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", locked_wav, "-vn",
        "-c:a","aac","-b:a","128k",
        "-movflags","+faststart",
        out_m4a
    ])

    # 정리
    for p in (concat_txt, joined_wav, locked_wav, (sweet_wav if use_tail_sweeten else None)):
        if not p: continue
        try: os.remove(p)
        except Exception: pass

    conf._shared_audio_filename[tg_index] = out_m4a
    fd_log.info(f"🎧[TG:{tg_index:02d}] Audio OK (sync+tail-sweeten={use_tail_sweeten}) → {out_m4a}")
    return out_m4a

# ─────────────────────────────────────────────────────────────────────────────
# def process_audio_parts(file_directory, camera_ip_class, camera_ip, file_type, t_start, f_start, t_end, f_end):
# [owner] hongsu jung
# [date] 2025-10-11
# ─────────────────────────────────────────────────────────────────────────────
def process_audio_parts_fill(file_directory, tg_index, camera_ip_class, camera_ip,
                        file_type, t_start, f_start, t_end, f_end,
                        *, ar=48000, ac=2,
                        fade_ms=3,              # 각 1초 내부 미세 페이드(길이 불변)
                        use_tail_sweeten=True,  # (선택) compand로 아주 작은 레벨만 소폭 보정(길이 불변)
                        ofps_override=None):
    """
    프레임-정확, 길이-보전 오디오 생성 (루프/무음 패딩 없이 타임스트레치로 1초 정규화):
      1) 각 1초 세그먼트를 디코드 → 실제 dur 측정 → atempo 로 1.000s로 미세 타임스트레치
         └ asetnsamples=48000 으로 정확히 48k 샘플 잠금 (0패딩/0컷 정책)
      2) ffconcat 로 병합 (전부 1초 조각) + aresample(async=0) 고정 (드리프트 금지)
      3) (옵션) compand 기반 tail sweeten – 아주 작은 레벨만 소폭 보정 (길이 불변)
      4) 총 프레임 수 기반 end_sample 하드락 (절대 싱크)
      5) AAC 1회 인코드

    ※ crossfade/atempo/겹치기/무음 apad 기반 패딩을 쓰지 않음. 모든 보정은 타임스트레치로만 수행.
    """
    import os, uuid, subprocess, shlex, json, math
    from pathlib import PureWindowsPath

    # ───────── helpers ─────────
    def _run(cmd, check=True):
        p = subprocess.run(cmd, text=True, capture_output=True)
        if check and p.returncode != 0:
            raise RuntimeError(
                "FFmpeg failed:\nCMD: " + " ".join(shlex.quote(c) for c in cmd) +
                "\nSTDERR:\n" + (p.stderr or "")
            )
        return p

    def _ensure_dir(p):
        if p:
            os.makedirs(p, exist_ok=True)

    def _to_ffconcat_path(p: str) -> str:
        # ffconcat 전용 경로 정규화: UNC → //host/share/..., 로컬 → C:/...
        if not p:
            return p
        p_win = str(PureWindowsPath(p))
        if p_win.startswith("\\\\"):
            return "//" + p_win[2:].replace("\\", "/")
        return p_win.replace("\\", "/")

    def _has_audio_stream(path: str) -> bool:
        pr = subprocess.run(
            ["ffprobe","-v","error","-select_streams","a:0",
             "-show_entries","stream=index","-of","json", path],
            text=True, capture_output=True
        )
        if pr.returncode != 0:
            return False
        try:
            j = json.loads(pr.stdout or "{}")
            return bool(j.get("streams"))
        except Exception:
            return False

    def _probe_duration(path: str) -> float | None:
        # ffprobe format.duration (초)
        pr = subprocess.run(
            ["ffprobe","-v","error","-show_entries","format=duration","-of","json", path],
            text=True, capture_output=True
        )
        if pr.returncode != 0:
            return None
        try:
            j = json.loads(pr.stdout or "{}")
            d = (j.get("format") or {}).get("duration")
            return float(d) if d is not None else None
        except Exception:
            return None

    def _probe_sr_nb(path: str):
        # (로그용) 스트림 sample_rate, nb_samples
        pr = subprocess.run(
            ["ffprobe","-v","error","-select_streams","a:0",
             "-show_entries","stream=sample_rate,nb_samples","-of","json", path],
            text=True, capture_output=True
        )
        sr = None
        nb = None
        try:
            j = json.loads(pr.stdout or "{}")
            streams = j.get("streams") or []
            if streams:
                s = streams[0]
                sr = int(s.get("sample_rate")) if s.get("sample_rate") else None
                nb = int(s.get("nb_samples")) if s.get("nb_samples") else None
        except Exception:
            pass
        return sr, nb

    def _normalize_1s_to_wav(in_media: str, out_wav: str, ar_=48000, ac_=2):
        """
        입력 세그먼트 → 타임스트레치로 정확 1.000000s WAV (48k 샘플)
        - 내부 페이드(예: 3ms in/out)는 '길이 불변'으로만 사용
        - 루프/무음 패딩 전혀 없음. dur<1.0인 경우 atempo로 미세 연장
        """
        _ensure_dir(os.path.dirname(out_wav) or ".")
        if not _has_audio_stream(in_media):
            raise RuntimeError(f"no audio stream: {in_media}")

        fad = max(0, int(fade_ms)) / 1000.0

        # 1) 먼저 0~1.0s 잘라 임시 WAV 생성 (실제 dur은 코덱/프레임경계로 0.938/0.960/0.981/1.000 등)
        tmp_wav = os.path.join(os.path.dirname(out_wav), f".tmp_{uuid.uuid4().hex}.wav")
        af1 = "atrim=0:1.0,asetpts=PTS-STARTPTS"
        if fad > 0:
            af1 += f",afade=t=in:st=0:d={fad:.3f},afade=t=out:st={1.0 - fad:.3f}:d={fad:.3f}"
        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-probesize","50M","-analyzeduration","5M",
            "-i", in_media,
            "-map","a:0?","-vn",
            "-af", af1,
            "-ar", str(int(ar_)), "-ac", str(int(ac_)),
            "-c:a","pcm_s16le",
            tmp_wav
        ])

        # 2) dur / sr / nb_samples 로깅
        dur = _probe_duration(tmp_wav) or 1.0
        sr0, nb0 = _probe_sr_nb(tmp_wav)
        fd_log.info(f"✅ TMP-WAV CHECK | nb_samples={nb0 if nb0 is not None else 'None'} (expect 48000) | sr={sr0 if sr0 else ar_} | duration={dur:.6f}")

        # 3) atempo 로 1.0초 정규화 (피치 보존) + 정확히 48000샘플 하드락
        if abs(dur - 1.0) < 1e-6:
            # 정확히 1.0인 경우도 asetnsamples 로 샘플수 잠금
            tempo = 1.0
            stretch_ms = 0.0
        else:
            tempo = 1.0 / max(1e-6, dur)  # 0.938→1.066, 0.960→1.0417, 0.9813→1.0190
            # atempo 권장범위 [0.5, 2.0] 클램프
            tempo = min(2.0, max(0.5, tempo))
            stretch_ms = (1.0 - dur) * 1000.0

        fd_log.info(f"🎛️ RESAMPLE@1s | dur={dur:.6f}s | tempo={tempo:.6f} | stretch={stretch_ms:.2f} ms → 1.000000s")

        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-i", tmp_wav,
            "-filter:a", f"atempo={tempo},asetnsamples=n=48000:p=0,aresample=async=0,asetpts=PTS-STARTPTS",
            "-ar", str(int(ar_)), "-ac", str(int(ac_)),
            "-c:a","pcm_s16le",
            out_wav
        ])

        try:
            os.remove(tmp_wav)
        except Exception:
            pass

    # ───────── inputs & FPS ─────────
    time_start = t_start
    time_end   = t_end

    file_list = generate_file_list(file_directory, camera_ip_class, camera_ip, time_start, time_end)
    if not file_list:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file list")
        return None

    ofps = getattr(conf, "_output_fps", 30) if ofps_override is None else ofps_override
    fps = (30000 / 1001.0) if ofps == 29 else 30.0

    # ✅ 총 프레임/샘플 하드 타깃
    total_frames   = max(1, int(round((time_end - time_start) * fps)))
    target_samples = int(round(total_frames * (ar / fps)))

    # ───────── paths ─────────
    work_root = os.path.join(file_directory if os.path.isdir(file_directory) else "R:\\", "_audio_work")
    work_dir  = os.path.join(work_root, f"tg{tg_index:02d}_{time_start:06d}_{time_end:06d}")
    _ensure_dir(work_dir)

    try:
        base = fd_get_cali_file(file_type, file_directory, time_start)
    except Exception:
        base = os.path.join(work_dir, f"audio_{uuid.uuid4().hex}")
    root, _   = os.path.splitext(base)
    concat_txt = os.path.join(work_dir, f"audio_{uuid.uuid4().hex}.ffconcat")
    joined_wav = root + "._joined.wav"
    sweet_wav  = root + "._sweet.wav"    # tail-sweeten 결과(옵션)
    locked_wav = root + "._locked.wav"
    out_m4a    = root + "._locked.m4a"

    fd_log.info(f"🚀[TG:{tg_index:02d}] audio start | frames={total_frames}, fps={fps:.6f}, target_samples={target_samples}")

    # ───────── 1) 각 세그먼트 → 정확 1초 WAV (타임스트레치) ─────────
    wavs = []
    for i, seg in enumerate(file_list):
        w = os.path.join(work_dir, f"norm_{i:05d}.wav")
        _normalize_1s_to_wav(seg, w, ar_=ar, ac_=ac)
        wavs.append(w)

    # ───────── 2) ffconcat (file→duration, 마지막 duration 생략) ─────────
    with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        last = len(wavs) - 1
        for i, p in enumerate(wavs):
            f.write(f"file '{_to_ffconcat_path(p)}'\n")
            if i != last:
                f.write("duration 1.000000\n")

    # ───────── 3) concat → 단일 WAV (드리프트 보정 비활성) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-f","concat","-safe","0","-i", _to_ffconcat_path(concat_txt),
        "-vn",
        "-af","aresample=async=0:first_pts=0",  # 또는 "anull"
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s16le",
        joined_wav
    ])

    # ───────── 3.5) (옵션) 길이-불변 tail sweeten ─────────
    src_for_lock = joined_wav
    if use_tail_sweeten:
        compand = (
            "compand=attacks=0.003:decays=0.030:"
            "points=-80/-68|-70/-60|-60/-50|-50/-42|-40/-39|0/0:"
            "gain=0:volume=0:delay=0"
        )
        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-i", joined_wav, "-vn",
            "-af", compand + ",aresample=async=0,asetpts=PTS-STARTPTS",
            "-ar", str(int(ar)), "-ac", str(int(ac)),
            "-c:a","pcm_s16le",
            sweet_wav
        ])
        src_for_lock = sweet_wav

    # ───────── 4) 총 샘플수로 하드락(절대 싱크) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", src_for_lock, "-vn",
        "-af", f"atrim=end_sample={target_samples},asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s16le",
        locked_wav
    ])

    # ───────── 5) AAC 인코드(1회) ─────────
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", locked_wav, "-vn",
        "-c:a","aac","-b:a","128k",
        "-movflags","+faststart",
        out_m4a
    ])

    # 정리
    for p in (concat_txt, joined_wav, locked_wav, (sweet_wav if use_tail_sweeten else None)):
        if not p: continue
        try: os.remove(p)
        except Exception: pass

    conf._shared_audio_filename[tg_index] = out_m4a
    fd_log.info(f"🎧[TG:{tg_index:02d}] Audio OK (tail-extend=0, sweeten={use_tail_sweeten}) → {out_m4a}")
    return out_m4a


# ─────────────────────────────────────────────────────────────────────────────
# def process_audio_parts(file_directory, camera_ip_class, camera_ip, file_type, t_start, f_start, t_end, f_end):
# [owner] hongsu jung
# [date] 2025-10-12
def process_audio_parts(file_directory, tg_index, camera_ip_class, camera_ip,
                        file_type, t_start, f_start, t_end, f_end,
                        *, ar=48000, ac=2,
                        xf_dur=0.03,      # (미사용)
                        xf_gap_db=6.0,    # (미사용)
                        xf_quiet_db=-8.0, # (미사용)
                        xf_floor_db=-15.0,# (미사용)
                        ofps_override=None,
                        # ▼▼ 로그 옵션 (유지용 dummy) ▼▼
                        head_ms=120, tail_ms=120,
                        debug_focus_indices=(69, 70, 71),
                        save_raw_astats=True):
    """
    MP4에서 오디오만 추출해 바로 이어붙이고, 전체 길이를 len(files)초로 고정
      1) 원본 MP4 리스트 수집
      2) concat demuxer로 오디오 스트림 복사(-c:a copy) 결합 (실패 시 폴백 재인코드)
      3) 1회 디코드+리샘플(soxr) → PCM
      4) apad+atrim(end_sample)으로 총 샘플 수(len(files)*ar) 하드락
      5) AAC 인코드 1회
    """
    import os, uuid, subprocess, shlex, json
    from pathlib import PureWindowsPath

    # ───────── helpers ─────────
    def _run(cmd, check=True):
        p = subprocess.run(cmd, text=True, capture_output=True)
        if check and p.returncode != 0:
            raise RuntimeError(
                "FFmpeg failed:\nCMD: " + " ".join(shlex.quote(c) for c in cmd) +
                "\nSTDERR:\n" + (p.stderr or "")
            )
        return p

    def _ensure_dir(p):
        if p:
            os.makedirs(p, exist_ok=True)

    def _to_ffconcat_path(p: str) -> str:
        if not p:
            return p
        p_win = str(PureWindowsPath(p))
        if p_win.startswith("\\\\"):
            return "//" + p_win[2:].replace("\\", "/")
        return p_win.replace("\\", "/")

    def _has_audio_stream(path: str) -> bool:
        pr = subprocess.run(
            ["ffprobe","-v","error","-select_streams","a:0",
             "-show_entries","stream=index","-of","json", path],
            text=True, capture_output=True
        )
        if pr.returncode != 0:
            return False
        try:
            j = json.loads(pr.stdout or "{}")
            return bool(j.get("streams"))
        except Exception:
            return False

    # ───────── inputs ─────────
    time_start = t_start
    time_end   = t_end

    files = generate_file_list(file_directory, camera_ip_class, camera_ip, time_start, time_end)
    if not files:
        fd_log.info(f"\r❌[0x{file_type:X}] there is no file list")
        return None

    # 타깃 길이(초) 및 샘플 수
    total_seconds  = len(files)
    target_samples = int(total_seconds * ar)
    target_sec     = target_samples / float(ar)

    # ───────── paths ─────────
    work_root = os.path.join("R:\\", "_aw")
    work_dir  = os.path.join(work_root, f"tg{tg_index:02d}_{time_start:06d}_{time_end:06d}")
    _ensure_dir(work_dir)

    try:
        base = fd_get_cali_file(file_type, file_directory, time_start)
    except Exception:
        base = os.path.join(work_dir, f"au_{uuid.uuid4().hex}")
    root, _   = os.path.splitext(base)

    concat_txt = os.path.join(work_dir, f"c_{uuid.uuid4().hex}.ffconcat")
    concat_aud = os.path.join(work_dir, f"j_{uuid.uuid4().hex}.m4a")  # stream copy 결과
    master_wav = os.path.join(work_dir, f"m_{uuid.uuid4().hex}.wav")  # 1회 디코드/리샘플
    locked_wav = os.path.join(work_dir, f"l_{uuid.uuid4().hex}.wav")  # 길이 하드락
    out_m4a    = root + "._locked.m4a"

    play_time = fd_format_elapsed_time(len(files))
    fd_log.info(f"🚀[TG:{tg_index:02d}] start | files={len(files)} | video play time: {play_time} ({target_samples} samples)")

    # ───────── 1) concat 목록 작성(오디오 있는 입력만) ─────────
    kept = []
    with open(concat_txt, "w", encoding="utf-8", newline="\n") as f:
        f.write("ffconcat version 1.0\n")
        for p in files:
            if not _has_audio_stream(p):
                fd_log.info(f"❌ no audio stream in {p}")
                continue
            kept.append(p)
            f.write(f"file '{_to_ffconcat_path(p)}'\n")

    if not kept:
        fd_log.info("❌ no audio-capable inputs")
        return None

    # ───────── 2) 오디오만 무손실 concat (stream copy) ─────────
    try:
        fd_log.info("[1/4] Concatenating audio by stream copy…")
        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-f","concat","-safe","0","-i", _to_ffconcat_path(concat_txt),
            "-map","0:a:0","-c:a","copy",
            concat_aud
        ])
    except Exception as e:
        fd_log.info(f"⚠️ stream copy concat failed, fallback to normalize params & concat: {e}")
        # 코덱/샘플레이트/채널 불일치 시 폴백: 통일 파라미터로 재인코드 후 copy-concat
        tmp_list = os.path.join(work_dir, f"r_{uuid.uuid4().hex}.ffconcat")
        tmp_files = []
        for i, p in enumerate(kept):
            t = os.path.join(work_dir, f"r_{i:05d}.m4a")
            _run([
                "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
                "-i", p, "-vn",
                "-ar", str(int(ar)), "-ac", str(int(ac)),
                "-c:a","aac","-profile:a","aac_low","-b:a","192k",
                t
            ])
            tmp_files.append(t)
        with open(tmp_list, "w", encoding="utf-8", newline="\n") as f:
            f.write("ffconcat version 1.0\n")
            for t in tmp_files:
                f.write(f"file '{_to_ffconcat_path(t)}'\n")
        _run([
            "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
            "-f","concat","-safe","0","-i", _to_ffconcat_path(tmp_list),
            "-map","0:a:0","-c:a","copy",
            concat_aud
        ])

    # ───────── 3) 1회 디코드+리샘플(soxr) → PCM ─────────
    fd_log.info("[2/4] Decode once to WAV with soxr resample…")
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", concat_aud, "-vn",
        "-af", f"aresample={ar}:resampler=soxr:dither_method=triangular",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s24le",
        master_wav
    ])

    # ───────── 4) 총 샘플 수(end_sample)로 길이 하드락 ─────────
    fd_log.info(f"[3/4] Locking length to {total_seconds}s ({target_samples} samples)…")
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", master_wav, "-vn",
        "-af", f"apad=pad_dur=3600,atrim=end_sample={target_samples},asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a","pcm_s24le",
        locked_wav
    ])

    # ───────── 5) AAC 인코드 ─────────
    fd_log.info("[4/4] Encoding AAC…")
    _run([
        "ffmpeg","-nostdin","-y","-hide_banner","-loglevel","warning",
        "-i", locked_wav, "-vn",
        "-c:a","aac","-b:a","192k",
        "-movflags","+faststart",
        out_m4a
    ])

    # cleanup
    for p in (concat_txt, concat_aud, master_wav, locked_wav):
        try: os.remove(p)
        except Exception: pass

    conf._shared_audio_filename[tg_index] = out_m4a
    fd_log.info(f"🎧[TG:{tg_index:02d}] Audio OK (mp4→copy-concat→1×resample→sec-lock) → {out_m4a}")
    return out_m4a
