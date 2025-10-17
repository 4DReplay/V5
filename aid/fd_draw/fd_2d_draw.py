# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_2d_draw
# - 2024/10/28
# - Hongsu Jung
# https://4dreplay.atlassian.net/wiki/x/F4DofQ
# ─────────────────────────────────────────────────────────────────────────────#
import gc
import cv2
import os
import av
import io
import time
import ffmpeg
import math
import threading
import subprocess
import tempfile
import shutil
import screeninfo
import numpy as np
import mediapipe as mp
import threading
import win32api
import win32con


from queue import Queue
from concurrent.futures import ThreadPoolExecutor

import fd_utils.fd_config           as conf
from fd_utils.fd_logging        import fd_log

from PIL                            import ImageFont, ImageDraw, Image
from fd_utils.fd_file_edit          import fd_get_output_file
from fd_utils.fd_file_edit          import fd_get_clean_file
from fd_utils.fd_file_edit          import fd_common_ffmpeg_args_pre
from fd_utils.fd_file_edit          import fd_common_ffmpeg_args_post
from fd_utils.fd_file_edit          import fd_extract_frames_from_file

from fd_detection.fd_detect         import get_total_frame_count


from fd_draw.fd_drawing         import draw_batter_singleline
from fd_draw.fd_drawing         import draw_pitcher_singleline
from fd_draw.fd_drawing         import draw_pitcher_multiline
from fd_draw.fd_drawing         import draw_hit_singleline
from fd_draw.fd_drawing         import draw_hit_multiline

from fd_draw.fd_drawing         import set_baseball_info_layer


# ─────────────────────────────────────────────────────────────────────────────##
# def preview_and_check(frame):
# owner : hongsu jung
# date : 2025-05-28
# ─────────────────────────────────────────────────────────────────────────────#
def preview_and_check(frame):
    widget = conf._tracking_check_widget
    if widget is None:
        fd_log.error("❌ Tracking widget is not initialized")
        return False
    mouse_click = widget.show_frame_and_wait(frame)
    if mouse_click == conf._mouse_click_left:
        return True
    else:
        return False

def draw_text_with_box(
    frame: np.ndarray,
    text: str,
    position: tuple,
    font_path: str,
    font_size: int = 32,
    text_color=(255, 255, 255, 255),
    outline_color=(0, 0, 0, 255),
    box_color=(0, 0, 0, 128),
    padding: int = 6,
    extra_pad_y: int = 6
):
    """
    OpenCV BGR 이미지 위에 PIL 기반으로
    - 반투명 박스
    - 외곽선 테두리
    - 본문 텍스트
    를 그려주는 함수

    Returns:
    - frame (np.ndarray): 텍스트가 그려진 BGR 이미지
    """

    img_pil = Image.fromarray(frame)
    draw = ImageDraw.Draw(img_pil, mode="RGBA")
    font = ImageFont.truetype(font_path, font_size)

    # 텍스트 크기
    bbox = font.getbbox(text)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    box = [
        position[0] - padding,
        position[1] - padding,
        position[0] + text_w + padding,
        position[1] + text_h + padding + extra_pad_y
    ]

    # 배경 박스
    draw.rectangle(box, fill=box_color)

    # 외곽선 테두리 텍스트
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            if dx == 0 and dy == 0:
                continue
            draw.text((position[0] + dx, position[1] + dy), text, font=font, fill=outline_color)

    # 메인 텍스트
    draw.text(position, text, font=font, fill=text_color)

    return np.array(img_pil)

def draw_multiline_text_with_box(frame, lines, position, font_path, align="left", line_spacing=6, padding=10):
    from PIL import ImageFont, ImageDraw, Image

    image_pil = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(image_pil)

    rendered_lines = []
    total_height = 0
    max_width = 0

    # 각 줄별 텍스트 렌더링 정보 계산
    for line in lines:
        font = ImageFont.truetype(font_path, line["font_size"])
        bbox = font.getbbox(line["text"])
        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        rendered_lines.append({"text": line["text"], "font": font, "width": width, "height": height})
        total_height += height + line_spacing
        max_width = max(max_width, width)

    total_height -= line_spacing  # 마지막 줄 간격 제거

    x_center, y_top = position
    x_left = x_center - max_width // 2
    y_cursor = y_top

    # 배경 박스
    draw.rectangle(
        [x_left - padding, y_top - padding, x_left + max_width + padding, y_top + total_height + padding],
        fill=(0, 0, 0, 180)
    )

    # 각 줄 그리기
    for item in rendered_lines:
        text = item["text"]
        font = item["font"]
        width = item["width"]
        height = item["height"]

        if align == "center":
            x = x_center - width // 2
        elif align == "right":
            x = x_center - max_width + (max_width - width)
        else:  # left
            x = x_left

        draw.text((x, y_cursor), text, font=font, fill=(255, 255, 255))
        y_cursor += height + line_spacing

    return cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)

def to_int_xy(p):
    return tuple(map(int, p[:2])) if p else None

def consume_stderr(proc):
    for line in iter(proc.stderr.readline, b''):
        fd_log.info("[FFmpeg stderr]", line.decode('utf-8', errors='ignore'))

def consume_stderr1(proc):
    for line in iter(proc.stderr.readline, b''):
        fd_log.info("[FFmpeg stderr-1]", line.decode('utf-8', errors='ignore'))

def encode_frames_to_file_gpu(frames, fps, output_file):
    if not frames:
        raise ValueError("❌ 프레임이 없습니다.")

    height, width, _ = frames[0].shape

    process = (
        ffmpeg
        .input('pipe:0',
               format='rawvideo',
               pix_fmt='bgr24',
               s=f'{width}x{height}',
               framerate=fps)
        .output(output_file,
                vcodec='h264_nvenc',
                preset='p4',                
                rc='vbr',
                tune='hq',
                multipass='fullres',                
                r=fps,
                g=30,
                pix_fmt='yuv420p',
                movflags='faststart',
                loglevel='error')
        .overwrite_output()
        .run_async(pipe_stdin=True)
    )

    for frame in frames:
        process.stdin.write(frame.astype(np.uint8).tobytes())

    process.stdin.close()
    process.wait()

def get_overlay_png_path():
    if conf._cached_baseball_info_layer is None:
        conf._cached_baseball_info_layer = set_baseball_info_layer()
 
    image_np = conf._cached_baseball_info_layer
 
    if image_np.shape[2] == 3:
        image_np = cv2.cvtColor(image_np, cv2.COLOR_BGR2RGBA)
    elif image_np.shape[2] == 4:
        image_np = cv2.cvtColor(image_np, cv2.COLOR_BGRA2RGBA)
    else:
        raise ValueError("❌ Unknown channel format for cached_baseball_info_layer")
 
    pil_img = Image.fromarray(image_np)
 
    tmp_overlay = fd_get_output_file(conf._file_type_overlay)
    conf._mem_temp_file[conf._file_type_overlay] = tmp_overlay    
    pil_img.save(tmp_overlay)
    return tmp_overlay

# ─────────────────────────────────────────────────────────────────────────────
#
#
#               /S/I/N/G/L/E/ /L/I/N/E/
#
#               /C/R/E/A/T/E/ /F/I/L/E/
#
#
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# def fd_create_tracking_singleline()
# owner : hongsu jung
# date : 2025-03-28
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def fd_create_tracking_singleline(output_file, arr_ball):

    conf._output_width  = conf._resolution_fhd_width
    conf._output_height = conf._resolution_fhd_height

    # ─────────────────────────────────────────────────────────────────────────────
    # V.4.2.1
    # 화면에 라이브로 Play.
    # ─────────────────────────────────────────────────────────────────────────────
    
    if conf._live_player:
        live_player_singleline_play(arr_ball)
    else:
        create_file_singleline(output_file, arr_ball)

               
# ─────────────────────────────────────────────────────────────────────────────
# def create_file_singleline(output_file, arr_ball)
# owner : hongsu jung
# date : 2025-03-28
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def create_file_singleline(output_file, arr_ball):
    # set thread    
    conf._thread_draw_prev = threading.Thread(target=th_draw_tracking_prev          , args=(conf._file_type_prev, ))
    conf._thread_draw_curr = threading.Thread(target=th_draw_tracking_singleline    , args=(conf._file_type_curr, arr_ball,))
    conf._thread_draw_post = threading.Thread(target=th_draw_tracking_singleline    , args=(conf._file_type_post, arr_ball,))
    conf._thread_draw_last = threading.Thread(target=th_draw_tracking_last          , args=(conf._file_type_last, ))

    conf._thread_draw_prev.start()    
    conf._thread_draw_last.start()    
    conf._thread_draw_curr.start()    
    conf._thread_draw_post.start()    
    
# ────────────────────────────────────────────────────────────────────────────
# def th_draw_tracking_prev()
# owner : hongsu jung
# date : 2025-03-29
# just chnage fps for merging
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_tracking_prev(file_type):
    # time check
    t_start = time.perf_counter()    
    # wait until create last
    if conf._thread_file_prev:
        conf._thread_file_prev.join()   
    # check the file exist
    conf._mem_temp_file[file_type] = fd_get_output_file(file_type)
    # time check
    t_end   = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000        
    fd_log.info(f"\r🎨[Draw][Prev][Singleline] Process Time: {elapsed_ms:,.2f}")

# ─────────────────────────────────────────────────────────────────────────────
# def th_draw_tracking_singleline()
# owner : hongsu jung
# date : 2025-05-16
# drawing curr (detected)
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_tracking_singleline(file_type, ball_pos):
    t_start = time.perf_counter()

    output_file = fd_get_output_file(file_type)
    overlay_file = get_overlay_png_path()

    if file_type == conf._file_type_curr:
        frames = conf._frames_curr
        is_curr = True
    elif file_type == conf._file_type_post:
        frames = conf._frames_post
        is_curr = False
    else:
        fd_log.info(f"fd_draw_analysis_overlay, wrong type: {file_type}")
        return

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual,
        conf._type_baseball_hit_multi,
    )

    tot_count = len(frames)
    output_height = conf._resolution_fhd_height
    output_width = conf._resolution_fhd_width

    ffmpeg_command = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-thread_queue_size", "1024",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{output_width}x{output_height}",
        "-r", str(conf._output_fps),
        "-i", "-",  # stdin 입력
        "-i", overlay_file,  # overlay 이미지
        "-filter_complex",
        (
            "[0:v]format=rgba[base];"
            "[1:v]format=rgba[ovl];"
            "[base][ovl]overlay=0:0,"
            "unsharp=5:5:0.5:5:5:0.0,"
            "scale=in_range=limited:out_range=full,"
            "colorspace=all=bt709:iall=bt709:fast=1[out]"
        ),
        "-map", "[out]",
        "-c:v", "h264_nvenc",
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",
        "-preset", str(conf._output_preset),
        "-b:v", conf._output_bitrate,
        "-bufsize", "20M",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov",
        "-probesize", "1000000",
        "-analyzeduration", "2000000",
        "-f", "mp4",
        output_file,  # ✅ pipe:1 → 직접 경로로 저장
    ]

    process = subprocess.Popen(
        ffmpeg_command,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10**8
    )

    try:
        for idx, frame in enumerate(frames):
            if process.poll() is not None:
                fd_log.info("FFmpeg process terminated early.")
                break

            percent_progress = int((idx + 1) / tot_count * 100)
            print("\r" + " " * 20, end="")
            print(f"\r🎨[Draw][{'Curr' if is_curr else 'Post'}][Single] Progress: {percent_progress}%", end="")

            if is_curr:
                frame_draw = fd_draw_frame_singleline(conf._type_target, conf._file_type_curr, frame, idx, tot_count, ball_pos)
            else:
                frame_draw = fd_draw_frame_singleline(conf._type_target, conf._file_type_post, frame, idx, tot_count, ball_pos)
                if conf._type_target in (conf._type_baseball_hit, conf._type_baseball_hit_manual):
                    if not conf._detect_check_preview:
                        conf._detect_check_success = preview_and_check(frame_draw)
                        conf._detect_check_preview = True
                    else:
                        if not conf._detect_check_success:
                            break
            process.stdin.write(frame_draw.tobytes())

    except Exception as e:
        fd_log.info("Unexpected error:", e)

    finally:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except Exception as e:
            fd_log.warning(f"⚠️ Error closing stdin: {e}")

        process.wait()

        if process.returncode != 0:
            err = process.stderr.read().decode(errors='ignore')
            fd_log.error(f"❌ FFmpeg error: {err}")

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    fd_log.info(f"\r🎨[Draw][{'Curr' if is_curr else 'Post'}][Singleline] Process Time: {elapsed_ms:,.2f} ms")

    conf._mem_temp_file[file_type] = output_file

# ─────────────────────────────────────────────────────────────────────────────
# def th_draw_tracking_last()
# owner : hongsu jung
# date : 2025-04-06
# overlay image to mem file
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_tracking_last(file_type):
    # time check
    t_start = time.perf_counter()    
    # check create file
    if conf._thread_file_last:
        conf._thread_file_last.join()

    if file_type != conf._file_type_last:
        return False

    overlay_file    = get_overlay_png_path()
    input_file      = fd_get_clean_file(file_type)
    output_file     = fd_get_output_file(file_type)
    
    ffmpeg_cmd = [
        *fd_common_ffmpeg_args_pre(),
        '-i', input_file,
        '-i', overlay_file,
        '-filter_complex',
        #"[0:v]scale=in_range=limited:out_range=full,format=rgba[base];"
        #"[1:v]format=rgba[ovl];"
        #"[base][ovl]overlay=0:0,scale=in_range=limited:out_range=full,"
        #"colorspace=all=bt709:iall=bt709:fast=1[out]",
        "[0:v]format=rgba[base];"
        "[1:v]format=rgba[ovl];"
        # "[base][ovl]overlay=0:0,unsharp=3:3:0.3:3:3:0.0[out]",          # too sharp
        "[base][ovl]overlay=0:0,unsharp=3:3:-0.5:3:3:-0.3[out]",            
        # "[base][ovl]overlay=0:0,unsharp=5:5:-1.0:5:5:-1.0[out]",        # too much
        '-map', '[out]',
        "-c:v", "h264_nvenc",
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",        
        "-preset", str(conf._output_preset),
        "-b:v", conf._output_bitrate,        
        "-bufsize", "20M",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov",
        "-probesize", "1000000",
        "-analyzeduration", "2000000",
        "-f", "mp4",        
        output_file
    ]
    
    try:
        process = subprocess.run(
            ffmpeg_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        if process.returncode != 0:
            fd_log.info(f"\r❌[0x{file_type:X}][Final]: {process.stderr.decode(errors='ignore')}")
            return None

        # ✅ 저장 경로를 conf에 반영
        conf._mem_temp_file[conf._file_type_last] = output_file
        
        # time check
        t_end   = time.perf_counter()  # 종료 시간
        elapsed_ms = (t_end - t_start) * 1000        
        fd_log.info(f"\r🎨[Draw][Last][Singleline] Process Time: {elapsed_ms:,.2f} ms")
        return True

    except Exception as e:
        fd_log.error(f"❌ Exception in fd_draw_analysis_last: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# def fd_combine_processed_files()
# owner : hongsu jung
# date : 2025-03-28
# combine 3 parts mem to file
# ─────────────────────────────────────────────────────────────────────────────
def fd_combine_processed_files(output_file):

    # time check
    t_start = time.perf_counter()

    # 🎯 실제 파일 경로로 바로 접근
    file_paths = [
        conf._mem_temp_file[conf._file_type_prev],
        conf._mem_temp_file[conf._file_type_curr],
        conf._mem_temp_file[conf._file_type_post],
        conf._mem_temp_file[conf._file_type_last],
    ]

    fd_log.info(f"🚀[Final]start combine:{output_file}, {file_paths}")

    try:
        # 1. concat용 리스트 파일 생성
        with tempfile.NamedTemporaryFile(dir="R:\\", delete=False, mode='w', suffix='.txt') as list_file:
            for path in file_paths:
                list_file.write(f"file '{path}'\n")
            list_file_path = list_file.name

        # 2. FFmpeg 병합 실행
        if conf._encoding_mode == "hw":
            concat_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', list_file_path,
                '-c', 'copy',
                '-movflags', 'frag_keyframe+empty_moov',  # streaming 호환 필요시
                output_file
            ]
        elif conf._encoding_mode == "sw":
            concat_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat', '-safe', '0',
                '-i', list_file_path,
                '-c:v', 'libx264',
                '-preset', 'medium',
                '-b:v', conf._output_bitrate,
                '-profile:v', 'high',
                output_file
            ]
        result = subprocess.run(concat_cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fd_log.error("❌ FFmpeg 명령 실패:")
            fd_log.info(result.stderr)

        # time check
        t_end = time.perf_counter()  # 종료 시간
        elapsed_ms = (t_end - t_start) * 1000        
        fd_log.info(f"✅[Final][🕒:{elapsed_ms:,.2f} ms] finish combine:{output_file}")

    finally:
        # 3. 정리: 리스트 파일 삭제
        if os.path.exists(list_file_path):
            os.unlink(list_file_path)

    return

# ─────────────────────────────────────────────────────────────────────────────
# def fd_draw_frame(type_target, type_mem, frame, frameindex, tot_count, arr_ball): 
# https://4dreplay.atlassian.net/wiki/x/CACVgQ
# ─────────────────────────────────────────────────────────────────────────────
def fd_draw_frame_singleline(type_target, type_mem, frame, frameindex, tot_count, arr_ball): 
    
    match type_target:
        # front up
        # left arm angle 
        case conf._type_baseball_pitcher: 
            frame_draw = draw_pitcher_singleline(type_mem, frame,frameindex, tot_count, arr_ball)            
        case conf._type_baseball_batter_RH | conf._type_baseball_batter_LH: 
            frame_draw = draw_batter_singleline(type_mem, frame, frameindex, tot_count, arr_ball)
        case conf._type_baseball_hit | conf._type_baseball_hit_manual:             
            frame_draw = draw_hit_singleline(type_mem, frame, frameindex, tot_count, arr_ball)                        
        case _:
            fd_log.warning("⚠️ not yet")
    
    return frame_draw    

# ─────────────────────────────────────────────────────────────────────────────
#
#
#               /M/U/L/T/I/ /L/I/N/E/
#               
#               /C/R/E/A/T/E/ /F/I/L/E/
#
#
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# def fd_create_tracking_multiline()
# owner : hongsu jung
# date : 2025-07-01
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def fd_create_tracking_multiline(output_file, arr_balls):
    
    conf._output_width  = conf._resolution_fhd_width
    conf._output_height = conf._resolution_fhd_height

    if conf._live_player:
        live_player_multiline_play(arr_balls)
    else:
        create_file_multiline(output_file, arr_balls)

# ─────────────────────────────────────────────────────────────────────────────
# def create_file_multiline()
# owner : hongsu jung
# date : 2025-07-01
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def create_file_multiline(output_file, arr_balls):

    conf._thread_draw_prev = threading.Thread(target=th_draw_tracking_prev                 , args=(conf._file_type_prev, ))
    conf._thread_draw_curr = threading.Thread(target=fd_draw_analysis_overlay_multiline    , args=(conf._file_type_curr, arr_balls,))
    conf._thread_draw_post = threading.Thread(target=fd_draw_analysis_overlay_multiline    , args=(conf._file_type_post, arr_balls,))
    conf._thread_draw_last = threading.Thread(target=th_draw_tracking_last                 , args=(conf._file_type_last, ))
    
    # start thread
    conf._thread_draw_prev.start()    
    conf._thread_draw_curr.start()
    conf._thread_draw_post.start()
    conf._thread_draw_last.start()
        
    # waiting until finish all of job
    conf._thread_draw_prev.join()
    conf._thread_draw_curr.join()        
    conf._thread_draw_post.join()
    conf._thread_draw_last.join()
    
    # 3 parts memory combine
    fd_combine_processed_files(output_file)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_draw_frame_multi
# 2025-07-01
# ─────────────────────────────────────────────────────────────────────────────
def fd_draw_frame_multiline(type_target, type_mem, frame, frameindex, tot_count, arr_balls): 
    
    match type_target:
        # front up
        # left arm angle 
        case conf._type_baseball_pitcher_multi: 
            frame = draw_pitcher_multiline(type_mem, frame,frameindex, tot_count, arr_balls)            
        case conf._type_baseball_hit_multi:                       
            frame = draw_hit_multiline(type_mem, frame, frameindex, tot_count, arr_balls)                        
        case _ :
            fd_log.warning("⚠️ not yet")
    
    return frame        

# ─────────────────────────────────────────────────────────────────────────────
# def fd_draw_analysis_overlay_multi()
# owner : hongsu jung
# date : 2025-07-01
# ─────────────────────────────────────────────────────────────────────────────
def fd_draw_analysis_overlay_multiline(file_type, arr_balls):

    t_start = time.perf_counter()

    output_file = fd_get_output_file(file_type)
    overlay_file = get_overlay_png_path()

    if file_type == conf._file_type_curr:
        frames = conf._frames_curr
        is_curr = True
    elif file_type == conf._file_type_post:
        frames = conf._frames_post
        is_curr = False
    else:
        fd_log.info(f"fd_draw_analysis_overlay, wrong type: {file_type}")
        return

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual,
        conf._type_baseball_hit_multi,
    )

    tot_count = len(frames)
    output_height = conf._resolution_fhd_height
    output_width = conf._resolution_fhd_width

    ffmpeg_command = [
        "ffmpeg", "-y",
        "-loglevel", "error",
        "-thread_queue_size", "1024",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-s", f"{output_width}x{output_height}",
        "-r", str(conf._output_fps),
        "-i", "-",  # stdin 입력
        "-i", overlay_file,  # overlay 이미지
        "-filter_complex",
        (
            "[0:v]format=rgba[base];"
            "[1:v]format=rgba[ovl];"
            "[base][ovl]overlay=0:0,"
            "unsharp=5:5:0.5:5:5:0.0,"
            "scale=in_range=limited:out_range=full,"
            "colorspace=all=bt709:iall=bt709:fast=1[out]"
        ),
        "-map", "[out]",
        "-c:v", "copy",
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",
        "-preset", str(conf._output_preset),
        "-b:v", conf._output_bitrate,
        "-bufsize", "20M",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-movflags", "frag_keyframe+empty_moov",
        "-probesize", "1000000",
        "-analyzeduration", "2000000",
        "-f", "mp4",
        output_file,  # ✅ pipe:1 → 직접 경로로 저장
    ]

    process = subprocess.Popen(
        ffmpeg_command,
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=10**8
    )

    try:
        index = 0
        for idx, frame in enumerate(frames):
            if process.poll() is not None:
                fd_log.info("FFmpeg process terminated early.")
                break

            percent_progress = int((index + 1) / tot_count * 100)
            print("\r" + " " * 20, end="")
            print(f"\r🎨[Draw][{'Curr' if is_curr else 'Post'}][Multi] Progress: {percent_progress}%", end="")

            if is_curr:
                frame_draw = fd_draw_frame_multiline(conf._type_target, conf._file_type_curr, frame, idx, tot_count, arr_balls)
            else:
                frame_draw = fd_draw_frame_multiline(conf._type_target, conf._file_type_post, frame, idx, tot_count, arr_balls)

            index += 1
            process.stdin.write(frame_draw.tobytes())

    except Exception as e:
        fd_log.info("Unexpected error:", e)

    finally:
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except Exception as e:
            fd_log.warning(f"⚠️ Error closing stdin: {e}")

        process.wait()

        if process.returncode != 0:
            err = process.stderr.read().decode(errors='ignore')
            fd_log.error(f"❌ FFmpeg error: {err}")

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    fd_log.info(f"\r🎨[Draw][{'Curr' if is_curr else 'Post'}][Multiline] Process Time: {elapsed_ms:,.2f} ms")

    conf._mem_temp_file[file_type] = output_file

# ─────────────────────────────────────────────────────────────────────────────
#
#
#               /S/I/N/G/L/E/ /L/I/N/E/
#
#               /L/I/V/E/ /V/I/E/W/E/R/
#
#
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# def live_player_singleline_play()
# owner : hongsu jung
# date : 2025-07-02
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def live_player_singleline_play(ball_pos):

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual
    )

    # check thread    
    conf._thread_file_prev.join()
    conf._thread_file_curr.join()
    conf._thread_file_post.join()    
    conf._thread_file_last.join()

    conf._live_player_frames_prev_cnt = len(conf._frames_prev)
    conf._live_player_frames_curr_cnt = len(conf._frames_curr)
    conf._live_player_frames_post_cnt = len(conf._frames_post)
    conf._live_player_frames_last_cnt = get_total_frame_count(conf._mem_temp_file[conf._file_type_last])    

    # get total frame count
    conf._live_player_frames_total_cnt = (
        conf._live_player_frames_prev_cnt +
        conf._live_player_frames_curr_cnt +
        conf._live_player_frames_post_cnt +
        conf._live_player_frames_last_cnt
    )

    # debug
    fd_log.info(f"Total frame Count: {conf._live_player_frames_total_cnt}")
    fd_log.info(f"prev frame Count: {conf._live_player_frames_prev_cnt}")
    fd_log.info(f"curr frame Count: {conf._live_player_frames_curr_cnt}")
    fd_log.info(f"post frame Count: {conf._live_player_frames_post_cnt}")
    fd_log.info(f"last frame Count: {conf._live_player_frames_last_cnt}")

    # set double buffering from buffer manager
    buffer_index = conf._live_player_buffer_manager.init_frame(conf._live_player_frames_total_cnt)
    
    # need to check with preview
    if is_hit:
        conf._detect_check_preview = False
        conf._detect_check_success = False
    else:
        conf._detect_check_success = True

    # set threads
    conf._thread_draw_prev = threading.Thread(target=th_live_player_prev                ,args=(conf._file_type_prev, buffer_index,))
    conf._thread_draw_curr = threading.Thread(target=th_live_player_overlay_singleline  ,args=(conf._file_type_curr, ball_pos, buffer_index,))
    conf._thread_draw_post = threading.Thread(target=th_live_player_overlay_singleline  ,args=(conf._file_type_post, ball_pos, buffer_index,))
    conf._thread_draw_last = threading.Thread(target=th_live_player_last                ,args=(conf._file_type_last, buffer_index,))
    
    conf._thread_draw_prev.start()
    conf._thread_draw_curr.start()
    conf._thread_draw_post.start()    
    conf._thread_draw_last.start()     

    # wait until progress of drawing
    conf._live_player_drawing_progress = 0   # reset
    while(1):
        time.sleep(0.01)        
        if not is_hit:
            if(conf._live_player_drawing_progress > conf._live_player_drawing_wait):
                break
        else:
            break

    # play to live player
    if not is_hit and conf._detect_check_success:
        conf._live_player_widget.live_player_restart()
        fd_log.info("✅ Frames updated. Playing... (player thread is running)")

# ─────────────────────────────────────────────────────────────────────────────
# def th_live_player_prev()
# owner : hongsu jung
# date : 2025-06-10
# ─────────────────────────────────────────────────────────────────────────────
def th_live_player_prev(file_type: str, buffer_index ):

    t_start = time.perf_counter()

    try:        
        frames = conf._frames_prev
        start_idx = 0
        frame_cnt = len(frames)        
        tot_count = conf._live_player_frames_prev_cnt
        # check frame_cnt vs tot_count
        if(frame_cnt != frame_cnt):
            fd_log.info(f"[fd_live_player_prev] frame count mismatch real frame:{frame_cnt}, file count:{tot_count}")

        for i in range(min(len(frames), tot_count)):
            blurred = cv2.GaussianBlur(frames[i], (3, 3), 0.5)
            target_idx = start_idx + i

            if 0 <= target_idx < frame_cnt:
                # set double buffering from buffer manager
                conf._live_player_buffer_manager.set_frame(buffer_index, target_idx, blurred)
            else:
                fd_log.warning(f"⚠️ Frame index {target_idx} out of bounds, skipping.")

    except Exception as e:
        fd_log.error(f"❌ Exception in fd_live_player_prev: {e}")

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    fd_log.info(f"\n🎨[Live][Prev][Singleline] Process Time: {elapsed_ms:,.2f} ms")
    
# ─────────────────────────────────────────────────────────────────────────────
# def th_live_player_overlay_singleline()
# owner : hongsu jung
# date : 2025-07-03
# ─────────────────────────────────────────────────────────────────────────────
def th_live_player_overlay_singleline(file_type: str, ball_pos, buffer_index):
    # time check
    t_start = time.perf_counter()  

    # 🧩 기본 설정
    if file_type == conf._file_type_curr:
        is_curr = True
        frames = conf._frames_curr
        count_start = conf._live_player_frames_prev_cnt
        count_total = conf._live_player_frames_curr_cnt        
    elif file_type == conf._file_type_post:
        is_curr = False
        frames = conf._frames_post
        count_start = conf._live_player_frames_prev_cnt + conf._live_player_frames_curr_cnt
        count_total = conf._live_player_frames_post_cnt        
    else:
        fd_log.info(f"th_live_player_overlay_singleline: wrong type: {file_type}")
        return

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual,
        conf._type_baseball_hit_multi
    )

    overlay_file = get_overlay_png_path()
    overlay_rgba = cv2.imread(overlay_file, cv2.IMREAD_UNCHANGED)
    if overlay_rgba is None or overlay_rgba.shape[2] != 4:
        fd_log.warning(f"⚠️ overlay image not found or invalid: {overlay_file}")
        return

    overlay_bgr = overlay_rgba[:, :, :3]
    alpha_mask = overlay_rgba[:, :, 3] / 255.0

    frame_cnt = len(frames)
    if frame_cnt != count_total:
        fd_log.info(f"[fd_overlay_to_combined_frames_only][{file_type:X}] frame count mismatch real frame:{frame_cnt}, file count:{count_total}")

    resized_cache = {}
    try:
        for idx in range(frame_cnt):

            frame = frames[idx]
            percent_progress = int((idx + 1) / count_total * 100)
            # set thread progress
            # for waiting thread progress
            if is_curr: conf._live_player_drawing_progress = percent_progress

            print("\r" + " " * 20, end="")
            print(f"\r🎨[Live][{'Curr' if is_curr else 'Post'}] Progress: {percent_progress}%", end="")

            if is_curr:
                img = fd_draw_frame_singleline(conf._type_target, file_type, frame, idx, count_total, ball_pos)
            else:
                img = fd_draw_frame_singleline(conf._type_target, file_type, frame, idx, count_total, ball_pos)
                if conf._type_target in (conf._type_baseball_hit, conf._type_baseball_hit_manual):
                    if not conf._detect_check_preview:
                        conf._detect_check_success = preview_and_check(img)                        
                        conf._detect_check_preview = True
                    else:
                        if not conf._detect_check_success:
                            break

                        
            if conf._trackman_mode:
                # 🖌️ 프레임 드로잉
                h, w = img.shape[:2]
                key = (w, h)

                if key not in resized_cache:
                    # 1. Resize overlay and alpha to current frame size
                    overlay_resized = cv2.resize(overlay_bgr, (w, h), interpolation=cv2.INTER_LINEAR)
                    alpha_resized = cv2.resize(alpha_mask, (w, h), interpolation=cv2.INTER_LINEAR)

                    # 2. Prepare alpha masks (float32, range 0.0~1.0)
                    alpha_bgr_f32 = cv2.merge([alpha_resized] * 3).astype(np.float32)
                    inv_alpha_bgr_f32 = 1.0 - alpha_bgr_f32

                    # 3. Upload overlay & alpha to GPU (once only)
                    gpu_overlay_resized = cv2.cuda_GpuMat(); gpu_overlay_resized.upload(overlay_resized.astype(np.float32))
                    gpu_alpha = cv2.cuda_GpuMat(); gpu_alpha.upload(alpha_bgr_f32)
                    gpu_inv_alpha = cv2.cuda_GpuMat(); gpu_inv_alpha.upload(inv_alpha_bgr_f32)

                    # Cache everything
                    resized_cache[key] = (gpu_overlay_resized, gpu_alpha, gpu_inv_alpha)
                else:
                    gpu_overlay_resized, gpu_alpha, gpu_inv_alpha = resized_cache[key]

                try:
                    # 4. Upload current frame
                    gpu_img = cv2.cuda_GpuMat(); gpu_img.upload(img.astype(np.float32))

                    # 5. Blend: (1 - alpha) * img + alpha * overlay
                    gpu_overlay_part = cv2.cuda.multiply(gpu_overlay_resized, gpu_alpha)
                    gpu_img_part = cv2.cuda.multiply(gpu_img, gpu_inv_alpha)
                    gpu_blended = cv2.cuda.add(gpu_overlay_part, gpu_img_part)

                    # 6. Download to CPU
                    blended = gpu_blended.download().astype(np.uint8)

                except Exception as e:
                    fd_log.warning(f"⚠️ CUDA fallback: {e}")
                    # fallback: CPU blending
                    blended = cv2.addWeighted(img, 0.8, overlay_bgr, 0.2, 0.0)
            else:
                blended = img            
            # set double buffering from buffer manager
            conf._live_player_buffer_manager.set_frame(buffer_index, count_start + idx, blended)


    except Exception as e:
        fd_log.error("❌ Unexpected error in fd_overlay_to_combined_frames_only():", e)

    # time check
    t_end   = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000        
    fd_log.info(f"\r🎨[Live][{'Curr' if is_curr else 'Post'}][Singleline] Process Time: {elapsed_ms:,.2f} ms") 

# ─────────────────────────────────────────────────────────────────────────────
# def th_live_player_last()
# owner : hongsu jung
# date : 2025-04-06
# overlay image to mem file
# ─────────────────────────────────────────────────────────────────────────────
def th_live_player_last(file_type: str, buffer_index):

    t_start = time.perf_counter()
    # drawing last
    if conf._trackman_mode:
        th_draw_tracking_last(file_type)
    
    conf._frames_last = fd_extract_frames_from_file(conf._mem_temp_file[conf._file_type_last], file_type)
    frames = conf._frames_last
    
    count_start = conf._live_player_frames_prev_cnt + conf._live_player_frames_curr_cnt + conf._live_player_frames_post_cnt
    tot_count   = conf._live_player_frames_last_cnt

    frame_cnt = len(frames)        
    # check frame_cnt vs max_count
    if(frame_cnt != tot_count):
        fd_log.info(f"[fd_live_player_last] frame count mismatch real frame:{frame_cnt}, file count:{tot_count}")

    try:
        for i in range(min(len(frames), tot_count)):
            target_idx = count_start + i
            percent_progress = int((i + 1) / tot_count * 100)
            print("\r" + " " * 20, end="")
            # set double buffering from buffer manager
            conf._live_player_buffer_manager.set_frame(buffer_index, target_idx, frames[i])            
            print(f"\r🎨[Live][Last] Progress: {percent_progress}%", end="")

    except Exception as e:
        fd_log.error("❌ Unexpected error in fd_live_player_overlay():", e)

    elapsed_ms = (time.perf_counter() - t_start) * 1000
    fd_log.info(f"\n🎨[Live][Last][Singleline] Process Time: {elapsed_ms:,.2f} ms")

# ─────────────────────────────────────────────────────────────────────────────
# def live_player_multiline_play(arr_balls)
# owner : hongsu jung
# date : 2025-07-03
# thread processing, drawing curr, and post simutaniously
# ─────────────────────────────────────────────────────────────────────────────
def live_player_multiline_play(arr_balls):

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual,
        conf._type_baseball_hit_multi
    )
    
    # check thread    
    conf._thread_file_prev.join()
    conf._thread_file_curr.join()
    conf._thread_file_post.join()    
    conf._thread_file_last.join()

    conf._live_player_frames_prev_cnt = len(conf._frames_prev)
    conf._live_player_frames_curr_cnt = len(conf._frames_curr)
    conf._live_player_frames_post_cnt = len(conf._frames_post)
    conf._live_player_frames_last_cnt = get_total_frame_count(conf._mem_temp_file[conf._file_type_last])    

    # get total frame count
    conf._live_player_frames_total_cnt = (
        conf._live_player_frames_prev_cnt +
        conf._live_player_frames_curr_cnt +
        conf._live_player_frames_post_cnt +
        conf._live_player_frames_last_cnt
    )

    # debug
    fd_log.info(f"Total frame Count: {conf._live_player_frames_total_cnt}")
    fd_log.info(f"prev frame Count: {conf._live_player_frames_prev_cnt}")
    fd_log.info(f"curr frame Count: {conf._live_player_frames_curr_cnt}")
    fd_log.info(f"post frame Count: {conf._live_player_frames_post_cnt}")
    fd_log.info(f"last frame Count: {conf._live_player_frames_last_cnt}")

    # set double buffering from buffer manager
    buffer_index = conf._live_player_buffer_manager.init_frame(conf._live_player_frames_total_cnt)    
    
    # set threads
    conf._thread_draw_prev = threading.Thread(target=th_live_player_prev               ,args=(conf._file_type_prev, buffer_index,  ))
    conf._thread_draw_curr = threading.Thread(target=th_live_player_overlay_multiline  ,args=(conf._file_type_curr, arr_balls, buffer_index, ))
    conf._thread_draw_post = threading.Thread(target=th_live_player_overlay_multiline  ,args=(conf._file_type_post, arr_balls, buffer_index, ))
    conf._thread_draw_last = threading.Thread(target=th_live_player_last               ,args=(conf._file_type_last, buffer_index, ))

    # start threads
    conf._thread_draw_prev.start()            
    conf._thread_draw_curr.start()     
    conf._thread_draw_post.start()           
    conf._thread_draw_last.start()

    # wait until progress of drawing
    conf._live_player_drawing_progress = 0   # reset
    while(1):
        time.sleep(0.01)
        if(conf._live_player_drawing_progress > conf._live_player_drawing_wait):
            break

    conf._live_player_widget.live_player_restart()
    fd_log.info("✅ Frames updated. Playing... (player thread is running)")
     
# ─────────────────────────────────────────────────────────────────────────────
# def th_live_player_overlay_multiline()
# owner : hongsu jung
# date : 2025-07-03
# ─────────────────────────────────────────────────────────────────────────────
def th_live_player_overlay_multiline(file_type: str, arr_balls, buffer_index):

    # time check
    t_start = time.perf_counter()  

    if file_type == conf._file_type_curr:
        is_curr = True
        frames = conf._frames_curr
        count_start = conf._live_player_frames_prev_cnt
        tot_count = conf._live_player_frames_curr_cnt
    elif file_type == conf._file_type_post:
        is_curr = False
        frames = conf._frames_post
        count_start = conf._live_player_frames_prev_cnt + conf._live_player_frames_curr_cnt
        tot_count = conf._live_player_frames_post_cnt
    else:
        fd_log.info(f"th_live_player_overlay_multiline: wrong type: {file_type}")
        return

    is_hit = conf._type_target in (
        conf._type_baseball_hit,
        conf._type_baseball_hit_manual,
        conf._type_baseball_hit_multi
    )

    overlay_file = get_overlay_png_path()
    overlay_rgba = cv2.imread(overlay_file, cv2.IMREAD_UNCHANGED)
    if overlay_rgba is None or overlay_rgba.shape[2] != 4:
        fd_log.warning(f"⚠️ overlay image not found or invalid: {overlay_file}")
        return

    overlay_bgr = overlay_rgba[:, :, :3]
    alpha_mask = overlay_rgba[:, :, 3] / 255.0

    frame_cnt = len(frames)
    if frame_cnt != tot_count:
        fd_log.info(f"[fd_overlay_to_combined_frames_only] frame count mismatch real frame:{frame_cnt}, file count:{tot_count}")

    frame_index = 0
    resized_cache = {}

    try:
        for idx in range(frame_cnt):

            frame = frames[idx]
            percent_progress = int((frame_index + 1) / tot_count * 100)
            # set thread progress
            # for waiting thread progress
            if is_curr: conf._live_player_drawing_progress = percent_progress

            print("\r" + " " * 20, end="")
            print(f"\r🎨[DrawOnly][{'Curr' if is_curr else 'Post'}] Progress: {percent_progress}%", end="")
            img = fd_draw_frame_multiline(conf._type_target, file_type, frame, idx, tot_count, arr_balls)

            if conf._trackman_mode:
                # 🖌️ 프레임 드로잉
                h, w = img.shape[:2]
                key = (w, h)

                if key not in resized_cache:
                    # 1. Resize overlay and alpha to current frame size
                    overlay_resized = cv2.resize(overlay_bgr, (w, h), interpolation=cv2.INTER_LINEAR)
                    alpha_resized = cv2.resize(alpha_mask, (w, h), interpolation=cv2.INTER_LINEAR)

                    # 2. Prepare alpha masks (float32, range 0.0~1.0)
                    alpha_bgr_f32 = cv2.merge([alpha_resized] * 3).astype(np.float32)
                    inv_alpha_bgr_f32 = 1.0 - alpha_bgr_f32

                    # 3. Upload overlay & alpha to GPU (once only)
                    gpu_overlay_resized = cv2.cuda_GpuMat(); gpu_overlay_resized.upload(overlay_resized.astype(np.float32))
                    gpu_alpha = cv2.cuda_GpuMat(); gpu_alpha.upload(alpha_bgr_f32)
                    gpu_inv_alpha = cv2.cuda_GpuMat(); gpu_inv_alpha.upload(inv_alpha_bgr_f32)

                    # Cache everything
                    resized_cache[key] = (gpu_overlay_resized, gpu_alpha, gpu_inv_alpha)
                else:
                    gpu_overlay_resized, gpu_alpha, gpu_inv_alpha = resized_cache[key]

                try:
                    # 4. Upload current frame
                    gpu_img = cv2.cuda_GpuMat(); gpu_img.upload(img.astype(np.float32))

                    # 5. Blend: (1 - alpha) * img + alpha * overlay
                    gpu_overlay_part = cv2.cuda.multiply(gpu_overlay_resized, gpu_alpha)
                    gpu_img_part = cv2.cuda.multiply(gpu_img, gpu_inv_alpha)
                    gpu_blended = cv2.cuda.add(gpu_overlay_part, gpu_img_part)

                    # 6. Download to CPU
                    blended = gpu_blended.download().astype(np.uint8)

                except Exception as e:
                    fd_log.warning(f"⚠️ CUDA fallback: {e}")
                    # fallback: CPU blending
                    blended = cv2.addWeighted(img, 0.8, overlay_bgr, 0.2, 0.0)
            else:
                blended = img

            # set double buffering from buffer manager
            conf._live_player_buffer_manager.set_frame(buffer_index, count_start + frame_index, blended)
            
            frame_index += 1

    except Exception as e:
        fd_log.error("❌ Unexpected error in fd_overlay_to_combined_frames_only():", e)

    # time check
    t_end   = time.perf_counter()  # 종료 시간
    elapsed_ms = (t_end - t_start) * 1000   
    fd_log.info(f"\r🎨[DrawOnly][{'Curr' if is_curr else 'Post'}][Multiline] Progress Time: {elapsed_ms:,.2f}%")     

# ─────────────────────────────────────────────────────────────────────────────
#
#
#
#               /M/U/L/T/I/ /C/H/A/N/N/E/L/
#
#
#
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# def fd_create_division(output_file, array_file):
# [owner] hongsu jung
# [date] 2025-03-10
# ─────────────────────────────────────────────────────────────────────────────
def fd_create_division(output_file):

    match conf._type_division:
        case conf._type_1_ch:
            combine_1_ch(output_file)        
        case conf._type_2_ch_h:
            combine_2_ch(output_file, mode="horizontal")
        case conf._type_2_ch_v:
            combine_2_ch(output_file, mode="vertical")
        case conf._type_3_ch_h:
            combine_3_ch(output_file, mode="horizontal")
        case conf._type_3_ch_m:
            combine_3_ch(output_file, mode="mix")
        case conf._type_4_ch:
            combine_4_ch(output_file)
        case conf._type_9_ch:
            combine_9_ch(output_file)
        case conf._type_16_ch:
            combine_16_ch(output_file)
    
# ─────────────────────────────────────────────────────────────────────────────
# def draw_timestamp(frame, frame_index):
# [owner] hongsu jung
# [date] 2025-03-11
# ─────────────────────────────────────────────────────────────────────────────
def draw_timestamp(frame, frame_index, fps):
    # 기준 프레임 보정
    zero_frame = conf._file_all_frm_start  # 기준 프레임
    frame_index -= zero_frame  # 상대 프레임 계산

    # 프레임 번호를 시간 (초) 단위로 변환
    time_in_seconds = frame_index / fps
    seconds = int(time_in_seconds % 60)
    milliseconds = int((time_in_seconds * 1000) % 1000)

    # 음수 시간일 경우 보정
    if time_in_seconds < 0:
        seconds = seconds - 60 + 1
        milliseconds = abs(milliseconds - 1000)
        timestamp = f"- {seconds:02}:{milliseconds:03}"
    else:
        timestamp = f"{seconds:02}:{milliseconds:03}"

    fd_log.info(f"add timestamp [{timestamp}]")

    # 프레임 크기 가져오기
    height, width = frame.shape[:2]
    
    # 중앙 하단 위치 계산
    x = width // 2
    y = int(height * 19 / 20)  # 하단 95% 위치

    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 1
    font_thickness = 2

    # Text Size
    (text_width, text_height), _ = cv2.getTextSize(timestamp, font, font_scale, font_thickness)
    text_x = x - text_width // 2  # 가운데 정렬
    text_y = y + text_height // 2

    cv2.putText(frame, timestamp, (text_x, text_y), font, font_scale, (0, 0, 0), font_thickness * 5)
    cv2.putText(frame, timestamp, (text_x, text_y), font, font_scale, (255, 255, 255), font_thickness)

    return frame
        
# ─────────────────────────────────────────────────────────────────────────────
# def upload_to_remote(local_path, remote_full_path):
# [owner] hongsu jung
# ─────────────────────────────────────────────────────────────────────────────
def upload_to_remote(local_path, remote_full_path):
    fd_log.info(f"🚀 Transferring to: {remote_full_path}")
    try:
        shutil.copy2(local_path, remote_full_path)
        fd_log.info(f"✅ Completed: Saved to remote → {remote_full_path}")
    except Exception as e:
        fd_log.error(f"❌ Transfer failed: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# def combine_1_ch(output_file):
# [owner] hongsu jung
# [date] 2025-04-29
# ─────────────────────────────────────────────────────────────────────────────
def combine_1_ch(remote_full_path):

    conf._output_file = remote_full_path
    temp_output_file = os.path.join("R:\\", "merged_output.mp4")
    fd_log.info("⚡ Using FFmpeg for fast 1 video merging...")
    numbers = conf._cnt_analysis_camera
    temp_files = []

    try:
        # 📦 Write memory buffer to RAM disk
        for idx in range(numbers):
            tmp = tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4")
            tmp.write(conf._mem_temp_file[idx])
            tmp.flush()
            tmp.close()
            temp_files.append(tmp.name)

        # 🟢 1개 채널: crop 없이 직접 인코딩
        command = [
            "ffmpeg",
            "-i", temp_files[0],
            "-c:v", "copy",
            "-b:v", str(conf._output_bitrate),
            "-preset", str(conf._output_preset),            
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-g", str(conf._output_gop),
            "-y",
            temp_output_file
        ]

        fd_log.info("▶️ GPU Encoding...")
        t_start = time.perf_counter()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fd_log.error("❌ FFmpeg 실패")
            fd_log.print("STDERR:")
            fd_log.print(result.stderr)
            raise RuntimeError("FFmpeg 실패")

        t_end = time.perf_counter()
        fd_log.info(f"✅ 로컬에 저장 완료: {temp_output_file} (처리 시간: {t_end - t_start:.2f}s)")

        # 🚚 비동기 전송
        thread = threading.Thread(
            target=upload_to_remote,
            args=(temp_output_file, remote_full_path)
        )
        thread.start()
        # 🕒 전송 완료까지 대기
        thread.join()

    finally:
        for path in temp_files:
            try:
                os.unlink(path)
            except PermissionError as e:
                fd_log.warning(f"⚠️ 삭제 실패: {path} - {e}")

# ─────────────────────────────────────────────────────────────────────────────
# def combine_2_division(output_file, video_files):
# [owner] hongsu jung
# [date] 2025-03-10
# [option] mode: "horizontal" (좌우 병합) 또는 "vertical" (상하 병합)
# [0:v]crop=iw*0.75:ih:iw*0.75:0[p1]
# ─────────────────────────────────────────────────────────────────────────────
def combine_2_ch(remote_full_path, mode="horizontal"):
    conf._output_file = remote_full_path
    temp_output_file = os.path.join("R:\\", "merged_output.mp4")
    fd_log.info("⚡ Using FFmpeg for fast 2 video merging...")
    numbers = 2  # 2개로 고정
    temp_files = []

    try:
        # 📦 Write memory buffer to RAM disk
        for idx in range(numbers):
            tmp = tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4")
            tmp.write(conf._mem_temp_file[idx])
            tmp.flush()
            tmp.close()
            temp_files.append(tmp.name)

        if mode == "horizontal":
            # 좌우로 2개 비디오 나란히 배치
            filter_graph = "[0:v][1:v]hstack=inputs=2[out]"
        elif mode == "vertical":
            # 상하로 2개 비디오 나란히 배치
            filter_graph = "[0:v][1:v]vstack=inputs=2[out]"
        else:
            raise ValueError("mode는 'horizontal' 또는 'vertical'이어야 합니다.")

        command = [
            "ffmpeg",
            "-i", temp_files[0], "-i", temp_files[1],
            "-filter_complex", filter_graph,
            "-map", "[out]",
            "-c:v", "copy",
            "-b:v", conf._output_bitrate,
            "-preset", str(conf._output_preset),            
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-g", "1",
            "-y",
            temp_output_file
        ]

        fd_log.info("▶️ GPU Encoding...")
        t_start = time.perf_counter()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fd_log.error("❌ FFmpeg 실패")
            fd_log.print("STDERR:")
            fd_log.print(result.stderr)
            raise RuntimeError("FFmpeg 실패")

        t_end = time.perf_counter()
        fd_log.info(f"✅ 로컬에 저장 완료: {temp_output_file} (처리 시간: {t_end - t_start:.2f}s)")

        # 🚚 비동기 전송
        thread = threading.Thread(
            target=upload_to_remote,
            args=(temp_output_file, remote_full_path)
        )
        thread.start()
        # 🕒 전송 완료까지 대기
        thread.join()

    finally:
        for path in temp_files:
            try:
                os.unlink(path)
            except PermissionError as e:
                fd_log.warning(f"⚠️ 삭제 실패: {path} - {e}")

# ─────────────────────────────────────────────────────────────────────────────
# def combine_3_division(output_file, video_files):
# [owner] hongsu jung
# [date] 2025-03-10
# [0:v]crop=iw*0.75:ih:iw*0.75:0[p1]
# ─────────────────────────────────────────────────────────────────────────────
def combine_3_ch(remote_full_path, mode="horizontal"):

    conf._output_file = remote_full_path
    temp_output_file = os.path.join("R:\\", "merged_output.mp4")
    fd_log.info("⚡ Using FFmpeg for fast 2x2 video merging...")
    numbers = conf._cnt_analysis_camera
    temp_files = []

    try:
        # 📦 Write memory buffer to RAM disk
        for idx in range(numbers):
            tmp = tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4")
            tmp.write(conf._mem_temp_file[idx])
            tmp.flush()
            tmp.close()
            temp_files.append(tmp.name)

        if mode == "horizontal":
            # 3개 비디오를 좌우로 배치
            filter_graph = (
                "[0:v]crop=iw*0.5:ih:iw*0.25:0[p1];"
                "[1:v]crop=iw*0.5:ih:iw*0.25:0[p2];"
                "[2:v]crop=iw*0.5:ih:iw*0.25:0[p3];"
                "[p1][p2][p3]hstack=inputs=3[out]"
            )
        elif mode == "mix":
            # 한 개 왼쪽, 두 개 오른쪽 상하 배치
            filter_graph = (
                "[0:v]crop=iw*0.75:ih:iw*0.75:0[p1];"
                "[1:v]scale=iw/2:ih/2[p2];"
                "[2:v]scale=iw/2:ih/2[p3];"
                "[p2][p3]vstack[right];"
                "[p1][right]hstack[out]"
            )
        
        command = [
            "ffmpeg",
            "-i", temp_files[0], "-i", temp_files[1], "-i", temp_files[2],
            "-filter_complex", filter_graph,
            "-map", "[out]",
            "-c:v", "copy",
            "-b:v", conf._output_bitrate,
            "-preset", str(conf._output_preset),            
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-g", "1",
            "-y",
            temp_output_file
        ]

        fd_log.info("▶️ GPU Encoding...")
        t_start = time.perf_counter()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fd_log.error("❌ FFmpeg 실패")
            fd_log.print("STDERR:")
            fd_log.print(result.stderr)
            raise RuntimeError("FFmpeg 실패")

        t_end = time.perf_counter()
        fd_log.info(f"✅ 로컬에 저장 완료: {temp_output_file} (처리 시간: {t_end - t_start:.2f}s)")

        # 🚚 비동기 전송
        thread = threading.Thread(
            target=upload_to_remote,
            args=(temp_output_file, remote_full_path)
        )
        thread.start()
        # 🕒 전송 완료까지 대기
        thread.join()

    finally:
        for path in temp_files:
            try:
                os.unlink(path)
            except PermissionError as e:
                fd_log.warning(f"⚠️ 삭제 실패: {path} - {e}")

# ─────────────────────────────────────────────────────────────────────────────
# def combine_4_division(output_file):
# [owner] hongsu jung
# [date] 2025-03-10
# 2X2
# "-g", "1",                         # ✅ GOP 설정 (모든 프레임을 keyframe으로)
# ─────────────────────────────────────────────────────────────────────────────
def combine_4_ch(remote_full_path):

    conf._output_file = remote_full_path
    temp_output_file = os.path.join("R:\\", "merged_output.mp4")
    fd_log.info("⚡ Using FFmpeg for fast 2x2 video merging...")
    numbers = conf._cnt_analysis_camera
    temp_files = []

    try:
        # 📦 Write memory buffer to RAM disk
        for idx in range(numbers):
            '''
            tmp = tempfile.NamedTemporaryFile(dir="R:\\", delete=False, suffix=".mp4")
            tmp.write(conf._mem_temp_file[idx])
            tmp.flush()
            tmp.close()
            '''
            temp_files.append(conf._mem_temp_file[idx])

        # 🧩 Create filtergraph
        filter_graph = (
            '[0:v]scale=iw:ih[p1];'
            '[1:v]scale=iw:ih[p2];'
            '[2:v]scale=iw:ih[p3];'
            '[3:v]scale=iw:ih[p4];'
            '[p1][p2]hstack[top];'
            '[p3][p4]hstack[bottom];'
            '[top][bottom]vstack[out]'
        )

        command = [
            'ffmpeg',
            '-i', temp_files[0], '-i', temp_files[1],
            '-i', temp_files[2], '-i', temp_files[3],
            '-filter_complex', filter_graph,
            '-map', '[out]',
            '-c:v', "copy",
            '-b:v', conf._output_bitrate,
            '-rc', 'vbr',
            '-preset', 'p4',
            '-g', '30',
            '-y',
            temp_output_file
        ]

        fd_log.info(f"▶️ GPU Encoding : {command}")
        t_start = time.perf_counter()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode != 0:
            fd_log.error("❌ FFmpeg 실패")
            fd_log.print()("STDERR:")
            fd_log.print(result.stderr)
            raise RuntimeError("FFmpeg 실패")

        t_end = time.perf_counter()
        fd_log.info(f"✅ 로컬에 저장 완료: {temp_output_file} (처리 시간: {t_end - t_start:.2f}s)")

        # 🚚 비동기 전송
        thread = threading.Thread(
            target=upload_to_remote,
            args=(temp_output_file, remote_full_path)
        )
        thread.start()
        # 🕒 전송 완료까지 대기
        thread.join()

    finally:
        for path in temp_files:
            try:
                os.unlink(path)
            except PermissionError as e:
                fd_log.warning(f"⚠️ 삭제 실패: {path} - {e}")

# ─────────────────────────────────────────────────────────────────────────────
# def combine_9_division(output_file, video_files):
# [owner] hongsu jung
# [date] 2025-03-10
# 3X3
# ─────────────────────────────────────────────────────────────────────────────
def combine_9_ch(output_file, video_files):
    fd_log.info("⚡ Using FFmpeg for fast 2x2 video merging...")

    # 필터그래프 생성
    filter_graph = (
        "[0:v]scale=iw/3:ih/3[p1];"
        "[1:v]scale=iw/3:ih/3[p2];"
        "[2:v]scale=iw/3:ih/3[p3];"
        "[3:v]scale=iw/3:ih/3[p4];"
        "[4:v]scale=iw/3:ih/3[p5];"
        "[5:v]scale=iw/3:ih/3[p6];"
        "[6:v]scale=iw/3:ih/3[p7];"
        "[7:v]scale=iw/3:ih/3[p8];"
        "[8:v]scale=iw/3:ih/3[p9];"
        "[p1][p2][p3]hstack=inputs=3[top];"
        "[p4][p5][p6]hstack=inputs=3[middle];"
        "[p7][p8][p9]hstack=inputs=3[bottom];"
        "[top][middle][bottom]vstack=inputs=3[out]"  # 3x3 그리드로 배치
    )

    command = [
        "ffmpeg",
        "-i", video_files[0], "-i", video_files[1], "-i", video_files[2],
        "-i", video_files[3], "-i", video_files[4], "-i", video_files[5],
        "-i", video_files[6], "-i", video_files[7], "-i", video_files[8],
        "-filter_complex", filter_graph,
        "-map", "[out]",
        "-c:v", "copy",
        "-b:v", f"{conf._output_bitrate}",
        "-preset", str(conf._output_preset),        
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",
        "-y",             
        output_file       
    ]


    subprocess.run(command, stdout=subprocess.DEVNULL)
    fd_log.info(f"✅ Merged video saved as: {output_file}")
    return True

# ─────────────────────────────────────────────────────────────────────────────
# def combine_16_division(output_file, video_files):
# [owner] hongsu jung
# [date] 2025-03-10
# 4X4
# ─────────────────────────────────────────────────────────────────────────────
def combine_16_ch(output_file, video_files):
    fd_log.info("⚡ Using FFmpeg for fast 2x2 video merging...")

    # 필터그래프 생성
    filter_graph = (
        "[0:v]scale =iw/4:ih/4[p1];"
        "[1:v]scale =iw/4:ih/4[p2];"
        "[2:v]scale =iw/4:ih/4[p3];"
        "[3:v]scale =iw/4:ih/4[p4];"
        "[4:v]scale =iw/4:ih/4[p5];"
        "[5:v]scale =iw/4:ih/4[p6];"
        "[6:v]scale =iw/4:ih/4[p7];"
        "[7:v]scale =iw/4:ih/4[p8];"
        "[8:v]scale =iw/4:ih/4[p9];"
        "[9:v]scale =iw/4:ih/4[p10];"
        "[10:v]scale=iw/4:ih/4[p11];"
        "[11:v]scale=iw/4:ih/4[p12];"
        "[12:v]scale=iw/4:ih/4[p13];"
        "[13:v]scale=iw/4:ih/4[p14];"
        "[14:v]scale=iw/4:ih/4[p15];"
        "[15:v]scale=iw/4:ih/4[p16];"
        "[p1][p2][p3][p4]hstack=inputs=4[row1];"
        "[p5][p6][p7][p8]hstack=inputs=4[row2];"
        "[p9][p10][p11][p12]hstack=inputs=4[row3];"
        "[p13][p14][p15][p16]hstack=inputs=4[row4];"
        "[row1][row2][row3][row4]vstack=inputs=4[out]"  # 4x4 그리드로 배치
    )

    command = [
        "ffmpeg",
        "-i", video_files[0], "-i", video_files[1], "-i", video_files[2], "-i", video_files[3],
        "-i", video_files[4], "-i", video_files[5], "-i", video_files[6], "-i", video_files[7],
        "-i", video_files[8], "-i", video_files[9], "-i", video_files[10], "-i", video_files[11],
        "-i", video_files[12], "-i", video_files[13], "-i", video_files[14], "-i", video_files[15],
        "-filter_complex", filter_graph,
        "-map", "[out]",
        "-c:v", "copy",
        "-b:v", f"{conf._output_bitrate}",        
        "-preset", str(conf._output_preset),        
        "-rc", "vbr",
        "-tune", "hq",
        "-multipass", "fullres",
        "-y",            
        output_file      
    ]


    subprocess.run(command, stdout=subprocess.DEVNULL)
    fd_log.info(f"✅ Merged video saved as: {output_file}")
    return True


# ─────────────────────────────────────────────────────────────────────────────
#
#
#
#               /S/W/I/N/G/ /A/N/A/L/Y/S/I/S/ 
#
#               3 - WAYS
#
#
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# def fd_create_swing_analysis()
# owner : hongsu jung
# date : 2025-04-29
# check conf._keypoint_pose_front, conf._keypoint_pose_side
# ─────────────────────────────────────────────────────────────────────────────
def fd_create_swing_analysis(output_file):
    
    if(conf._back_camera_index != -1):
        is_3ch = True
    else:
        is_3ch = False

    thread_front_draw = threading.Thread(target=th_draw_analysis_front)
    thread_side_draw = threading.Thread(target=th_draw_analysis_side)
    if(conf._multi_ch_analysis >= 3):
        thread_back_draw = threading.Thread(target=th_draw_analysis_back)
    
    thread_front_draw.start()    
    thread_side_draw.start()    
    if(conf._multi_ch_analysis >= 3):
        thread_back_draw.start()
    # waiting until finish all of job
    thread_front_draw.join()
    thread_side_draw.join()
    if(conf._multi_ch_analysis >= 3):
        thread_back_draw.join()
    
    # 3 parts memory combine
    create_file_combine_analysis(output_file)
    return None

# ─────────────────────────────────────────────────────────────────────────────
# def create_file_combine_analysis():
# [owner] hongsu jung
# [date] 2025-04-30
# ─────────────────────────────────────────────────────────────────────────────
def create_file_combine_analysis(remote_full_path):
    conf._output_file = remote_full_path
    fd_log.info("⚡ Using FFmpeg for fast side-by-side merge (no temp output)...")
    temp_files = []
    overlay_png_path = conf._logo_image

    try:
        # 1. 프레임 → GPU 인코딩 직접 저장
        front_path = os.path.join("R:\\", "front_encoded.mp4")
        fd_log.info("🎞️ front video encoding...")
        encode_frames_to_file_gpu(conf._frames_front, conf._input_fps, front_path)

        side_path = os.path.join("R:\\", "side_encoded.mp4")
        fd_log.info("🎞️ side video encoding...")
        encode_frames_to_file_gpu(conf._frames_side, conf._input_fps, side_path)

        temp_files.extend([front_path, side_path])

        if conf._multi_ch_analysis >= 3:
            back_path = os.path.join("R:\\", "back_encoded.mp4")
            fd_log.info("🎞️ back video encoding...")
            encode_frames_to_file_gpu(conf._frames_back, conf._input_fps, back_path)
            temp_files.append(back_path)

        # 2. FFmpeg filter 설정
        setpts_multiplier = conf._temp_fps / conf._output_fps

        if conf._multi_ch_analysis == 2:
            filter_graph = (
                f"[0:v]setpts={setpts_multiplier:.3f}*PTS[v0];"
                f"[1:v]setpts={setpts_multiplier:.3f}*PTS[v1];"
                f"[v0][v1]hstack=inputs=2[stacked];"
                f"movie='{overlay_png_path}'[ovr];"
                f"[stacked][ovr]overlay=format=auto[out]"
            )
            command_inputs = ["-i", front_path, "-i", side_path]
        elif conf._multi_ch_analysis == 3:
            filter_graph = (
                f"[0:v]setpts={setpts_multiplier:.3f}*PTS[v0];"
                f"[1:v]setpts={setpts_multiplier:.3f}*PTS[v1];"
                f"[2:v]setpts={setpts_multiplier:.3f}*PTS[v2];"
                f"[v0][v1][v2]hstack=inputs=3[stacked];"
                f"movie='{overlay_png_path}'[ovr];"
                f"[stacked][ovr]overlay=format=auto[out]"
            )
            command_inputs = ["-i", front_path, "-i", side_path, "-i", back_path]

        command = [
            "ffmpeg",
            *command_inputs,
            "-filter_complex", filter_graph,
            "-map", "[out]",
            "-r", str(conf._output_fps),
            "-c:v", "copy",
            "-b:v", str(conf._output_bitrate),
            "-preset", str(conf._output_preset),            
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-g", str(conf._output_gop),
            "-y",
            remote_full_path  # ✅ 직접 저장
        ]

        fd_log.info("▶️ GPU Encoding for merge (direct to remote path)...")
        t_start = time.perf_counter()
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        t_end = time.perf_counter()

        if result.returncode != 0:
            fd_log.error("❌ FFmpeg failed")
            fd_log.print(result.stderr)
            raise RuntimeError("FFmpeg merge failed")

        fd_log.info(f"✅ Merge completed: {remote_full_path} (Duration: {t_end - t_start:.2f}s)")

    finally:
        # 임시 인코딩 파일 정리
        for path in temp_files:
            try:
                os.unlink(path)
            except Exception as e:
                fd_log.warning(f"⚠️ 임시파일 삭제 실패: {path} - {e}")

        # 프레임 및 메모리 정리
        conf._frames_front = None
        conf._frames_side = None
        conf._frames_back = None

        del conf._mem_temp_file[conf._file_type_front]
        del conf._mem_temp_file[conf._file_type_side]
        del conf._mem_temp_file[conf._file_type_back]

# ─────────────────────────────────────────────────────────────────────────────
# def th_draw_analysis_front():
# [owner] hongsu jung
# [date] 2025-04-30
# frames = conf._frames_front
# info = conf._keypoint_pose_front
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_analysis_front():

    pose_info = conf._keypoint_pose_front
    frames = conf._frames_front

    '''
    # debug
    face_keypoints = {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}
    for i, frame in enumerate(frames):
        overlay = frame.copy()
        keypoints = pose_info[i]
        for name, (x, y, z) in keypoints.items():
            if name in face_keypoints:
                continue
            if x > 0 and y > 0:
                cv2.circle(overlay, (int(x), int(y)), 4, (0, 255, 0), -1)
                cv2.putText(overlay, name, (int(x) + 5, int(y) - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)
        alpha = 0.5
        frame[:] = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0.0)
    '''
    # 유효 keypoint 추출
    first_keypoints = pose_info[15]
    if not first_keypoints:
        fd_log.error("❌ No valid keypoints found in front pose data.")
        return

    def to_int_xy(p):
        return tuple(map(int, p[:2])) if p else None

    left_heel_base = to_int_xy(first_keypoints.get("left_ankle"))
    right_heel_base = to_int_xy(first_keypoints.get("right_ankle"))
    head_base = to_int_xy(first_keypoints.get("nose"))

    if None in (left_heel_base, right_heel_base, head_base):
        fd_log.warning("⚠️ Required base keypoints missing.")
        return

    left_foot_x = left_heel_base[0] + 35
    right_foot_x = right_heel_base[0] - 35
    foot_y = int((left_heel_base[1] + right_heel_base[1]) / 2) + 35
    head_y = head_base[1] - 150

    for i, frame in enumerate(frames):
        # 제목 표시
        title_text = "Face-On-View"
        font_size = 30
        font = ImageFont.truetype(conf._font_path_sub, font_size)
        bbox = font.getbbox(title_text)
        text_width = bbox[2] - bbox[0]
        x_center = int((frame.shape[1] - text_width) / 2)
        title_position = (x_center, 40)

        frame[:] = draw_text_with_box(
            frame=frame,
            text=title_text,
            position=title_position,
            font_path=conf._font_path_sub,
            font_size=font_size
        )

        overlay = frame.copy()
        keypoints = pose_info[i]
        if not isinstance(keypoints, dict):
            continue

        left_shoulder = keypoints.get("left_shoulder")
        right_shoulder = keypoints.get("right_shoulder")
        left_hip = keypoints.get("left_hip")
        right_hip = keypoints.get("right_hip")

        if all(k is not None for k in [left_shoulder, right_shoulder, left_hip, right_hip]):
            # 중앙 좌표 계산
            shoulder_center = (
                int((left_shoulder[0] + right_shoulder[0]) / 2),
                int((left_shoulder[1] + right_shoulder[1]) / 2)
            )
            hip_center = (
                int((left_hip[0] + right_hip[0]) / 2),
                int((left_hip[1] + right_hip[1]) / 2)
            )

            # 골반에서 어깨 높이까지 수직선 좌표
            vertical_line_start = hip_center
            vertical_line_end = (hip_center[0], shoulder_center[1])  # x 고정, y는 어깨 높이까지

            # 선 그리기
            cv2.line(overlay, to_int_xy(left_shoulder), to_int_xy(right_shoulder), (0, 0, 255), 2)  # 어깨
            cv2.line(overlay, to_int_xy(left_hip), to_int_xy(right_hip), (0, 0, 255), 2)            # 골반
            cv2.line(overlay, shoulder_center, hip_center, (0, 0, 255), 2)                          # 어깨 ↔ 골반 연결
            cv2.line(overlay, vertical_line_start, vertical_line_end, (0, 255, 255), 2)             # 골반 중심에서 수직선 (노란색)

            # 중심점 표시
            cv2.circle(overlay, shoulder_center, 5, (0, 0, 255), -1)
            cv2.circle(overlay, hip_center, 5, (0, 0, 255), -1)

            # 각도 계산
            dx = hip_center[0] - shoulder_center[0]
            dy = hip_center[1] - shoulder_center[1]
            angle_rad = math.atan2(dy, dx)
            angle_deg = math.degrees(angle_rad)
            vertical_angle = 90 - abs(angle_deg)

            # 기울기 보조선 및 각도 텍스트
            cx, cy = shoulder_center
            angle_line_len = 50
            end_x = int(cx + angle_line_len * math.cos(angle_rad))
            end_y = int(cy + angle_line_len * math.sin(angle_rad))
            #cv2.line(overlay, (cx, cy), (end_x, end_y), (0, 255, 255), 2)

            # ▶️ 기울기 텍스트 위치 계산
            text_lines = [
                {"text": "X-Factor", "font_size": 18},
                {"text": f"{vertical_angle:.1f} °", "font_size": 28}
            ]

            frame[:] = draw_multiline_text_with_box(
                frame=frame,
                lines=text_lines,
                position=(end_x + 10, end_y + 20),  # 중심점 기준으로 중앙정렬
                font_path=conf._font_path_sub,
                align="center"
            )

        # 기준 수직선
        cv2.line(overlay, (left_foot_x, foot_y), (left_foot_x, head_y), (0, 255, 255), 2)
        cv2.line(overlay, (right_foot_x, foot_y), (right_foot_x, head_y), (0, 255, 255), 2)

        # 최종 프레임 적용
        frame[:] = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0.0)
            
# ─────────────────────────────────────────────────────────────────────────────
# def th_draw_analysis_side():
# [owner] hongsu jung
# [date] 2025-04-30
# frames = conf._frames_side
# info = conf._keypoint_pose_side
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_analysis_side():

    pose_info = conf._keypoint_pose_side
    frames = conf._frames_side

    '''
    # debug
    face_keypoints = {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}
    for i, frame in enumerate(frames):
        overlay = frame.copy()
        keypoints = pose_info[i]  # dict 형태: {name: (x, y, z)}

        for name, (x, y, z) in keypoints.items():
            if name in face_keypoints:
                continue  # 얼굴 keypoint는 스킵

            if x > 0 and y > 0:
                cv2.circle(overlay, (int(x), int(y)), 4, (0, 255, 0), -1)
                cv2.putText(overlay, name, (int(x) + 5, int(y) - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

        alpha = 0.5
        frame[:] = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0.0)
    # debug
    '''

    for i, frame in enumerate(frames):
        # 🔹 상단 타이틀 표시
        title_text = "Down-the-Line View"
        font_size = 30
        font = ImageFont.truetype(conf._font_path_sub, font_size)

        # getbbox() → (left, top, right, bottom)
        bbox = font.getbbox(title_text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x_center = int((frame.shape[1] - text_width) / 2)
        y_pos = 40  # 여백 조절

        title_position = (x_center, y_pos)

        # 텍스트 표시
        frame[:] = draw_text_with_box(
            frame=frame,
            text=title_text,
            position=title_position,
            font_path=conf._font_path_sub,
            font_size=font_size  # draw_text_with_box에서 처리되도록
        )

        overlay = frame.copy()
        keypoints = pose_info[i]
        if not isinstance(keypoints, dict):
            continue
        
        shoulder = keypoints.get("right_shoulder")
        left_hip = keypoints.get("left_hip")
        right_hip = keypoints.get("right_hip")
        
        if shoulder is not None and left_hip is not None and right_hip is not None:
            # ▶️ 오른쪽 어깨 ↔ 오른쪽 골반 연결선

            sx, sy = map(int, shoulder[:2])
            # 골반 중심 좌표
            lx, ly = left_hip[:2]
            rx, ry = right_hip[:2]
            cx = int((lx + rx) / 2)
            cy = int((ly + ry) / 2)

            # 1. 어깨 → 골반 중심 선 (red)
            cv2.line(overlay, (sx, sy), (cx, cy), (0, 0, 255), 2)
            # 2. 골반 중심에서 수직 위로 선 (yellow)
            cv2.line(overlay, (cx, cy), (cx, sy), (0, 255, 255), 2)

            vx1, vy1 = sx - cx, sy - cy
            len1 = math.hypot(vx1, vy1)
            if len1 == 0:
                continue  # shoulder와 pelvis center가 동일할 경우 생략

            dot = vx1 * 0 + vy1 * (-1)  # 단위 벡터 (0, -1)과 내적
            angle_rad = math.acos(dot / len1)
            angle_deg = math.degrees(angle_rad)
            if vx1 < 0:
                angle_deg = -angle_deg

            # ▶️ 알파 블렌딩 적용
            alpha = 0.5
            frame[:] = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0.0)

            # ▶️ 기울기 텍스트 위치 계산
            text_lines = [
                {"text": "Torse Lean", "font_size": 18},
                {"text": f"{angle_deg:.1f} °", "font_size": 28}
            ]

            frame[:] = draw_multiline_text_with_box(
                frame=frame,
                lines=text_lines,
                position=(cx - 10, sy + 20),  # 중심점 기준으로 중앙정렬
                font_path=conf._font_path_sub,
                align="center"
            )
        # 최종 프레임 적용
        frame[:] = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0.0)
            
# ─────────────────────────────────────────────────────────────────────────────
# def th_draw_analysis_back():
# [owner] hongsu jung
# [date] 2025-05-01
# frames = conf._frames_back
# info = conf._keypoint_pose_back
# ─────────────────────────────────────────────────────────────────────────────
def th_draw_analysis_back():

    pose_info = conf._keypoint_pose_back
    frames = conf._frames_back

    '''
    # debug
    face_keypoints = {"nose", "left_eye", "right_eye", "left_ear", "right_ear"}
    for i, frame in enumerate(frames):
        overlay = frame.copy()
        keypoints = pose_info[i]  # dict 형태: {name: (x, y, z)}

        for name, (x, y, z) in keypoints.items():
            if name in face_keypoints:
                continue  # 얼굴 keypoint는 스킵

            if x > 0 and y > 0:
                cv2.circle(overlay, (int(x), int(y)), 4, (0, 255, 0), -1)
                cv2.putText(overlay, name, (int(x) + 5, int(y) - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)

        alpha = 0.5
        frame[:] = cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0.0)
    # debug
    '''

    # ▶️ 보조 함수
    def to_int_xy(p):
        return tuple(map(int, p[:2])) if p else None

    # ✅ 유효한 keypoint 딕셔너리 찾기
    #first_keypoints = next((kp for kp in pose_info if isinstance(kp, dict)), None)
    first_keypoints = pose_info[15]
    if not first_keypoints:
        fd_log.error("❌ No valid keypoints found in back pose data.")
        return

    def to_int_xy(p):
        return tuple(map(int, p[:2])) if p else None

    left_heel_base = to_int_xy(first_keypoints.get("left_ankle"))
    right_heel_base = to_int_xy(first_keypoints.get("right_ankle"))
    left_shoulder = to_int_xy(first_keypoints.get("left_shoulder"))

    if None in (left_heel_base, right_heel_base, left_shoulder):
        fd_log.warning("⚠️ Required base keypoints missing.")
        return   
    
    
    # 기준 라인 좌표
    left_foot_x = left_heel_base[0] - 35
    right_foot_x = right_heel_base[0] + 35
    foot_y = int((left_heel_base[1] + right_heel_base[1]) / 2) + 35
    head_y = left_shoulder[1] - 150

    for i, frame in enumerate(frames):
        if frame is None:
            continue  # 프레임 자체가 None이면 skip

        # Face-On-View
        title_text = "Back View"
        font_size = 30
        font = ImageFont.truetype(conf._font_path_sub, font_size)

        # getbbox() → (left, top, right, bottom)
        bbox = font.getbbox(title_text)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

        x_center = int((frame.shape[1] - text_width) / 2)
        y_pos = 40  # 여백 조절

        title_position = (x_center, y_pos)

        # 텍스트 표시
        frame[:] = draw_text_with_box(
            frame=frame,
            text=title_text,
            position=title_position,
            font_path=conf._font_path_sub,
            font_size=font_size  # draw_text_with_box에서 처리되도록
        )
        

        # 1. 프레임 복사 → 오버레이용
        overlay = frame.copy()
        keypoints = pose_info[i] if i < len(pose_info) else None

        if not isinstance(keypoints, dict):
            continue

        # 기준선 그리기
        cv2.line(overlay, (left_foot_x, foot_y), (left_foot_x, head_y), (0, 255, 255), 2)
        cv2.line(overlay, (right_foot_x, foot_y), (right_foot_x, head_y), (0, 255, 255), 2)

        '''
        ### Left knee angle
        
        left_hip = keypoints.get("left_hip")
        left_knee = keypoints.get("left_knee")
        left_ankle = keypoints.get("left_ankle")

        if None not in (left_hip, left_knee, left_ankle):
            ba = np.array([left_hip[0] - left_knee[0], left_hip[1] - left_knee[1]])
            bc = np.array([left_ankle[0] - left_knee[0], left_ankle[1] - left_knee[1]])
            cosine_angle = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
            angle_rad = np.arccos(np.clip(cosine_angle, -1.0, 1.0))
            knee_angle = np.degrees(angle_rad)
            
            # 무릎 선 시각화
            cv2.line(overlay, to_int_xy(left_hip), to_int_xy(left_knee), (0, 0, 255), 2)
            cv2.line(overlay, to_int_xy(left_knee), to_int_xy(left_ankle), (0, 0, 255), 2)

            # 무릎 중심 좌표
            cx = int(left_knee[0])
            cy = int(left_knee[1])

            # 텍스트 출력
            frame[:] = draw_multiline_text_with_box(
                frame=frame,
                lines=[
                    {"text": "Knee Angle", "font_size": 16},
                    {"text": f"{knee_angle:.1f}°", "font_size": 28}
                ],
                position=(cx + 100, cy - 60),
                font_path=conf._font_path_sub,
                align="center"
            )
        '''

        ### SWAY Movement
        # ▶️ sway 기준 저장 (초기화할 때 1회만 수행)

        left_hip    = keypoints.get("left_hip")
        right_hip   = keypoints.get("right_hip")
        left_shoulder = keypoints.get("left_shoulder")
        right_shoulder = keypoints.get("right_shoulder")

        if i == 0:
            sway_base_x = (left_hip[0] + right_hip[0]) / 2 if left_hip and right_hip else None

        # sway 계산
        if left_hip and right_hip and sway_base_x is not None and left_shoulder and right_shoulder:
            hip_center_x = (left_hip[0] + right_hip[0]) / 2
            sway_px = hip_center_x - sway_base_x

            # ✅ 어깨 너비 계산
            shoulder_width = np.linalg.norm(
                np.array([left_shoulder[0], left_shoulder[1]]) - 
                np.array([right_shoulder[0], right_shoulder[1]])
            )
            if shoulder_width > 0:
                sway_percent = (sway_px / shoulder_width) * 100
            else:
                sway_percent = 0.0

            # 시각화
            base_x = int(sway_base_x)
            hip_center_y = int((left_hip[1] + right_hip[1]) / 2)
            hip_center_x = int(hip_center_x)

            # 기준선: 노랑 / 현재 위치: 파랑
            cv2.line(overlay, (base_x, head_y), (base_x, foot_y), (0, 255, 255), 1, lineType=cv2.LINE_AA)
            cv2.line(overlay, (hip_center_x, head_y), (hip_center_x, foot_y), (255, 255, 0), 1, lineType=cv2.LINE_AA)

            # 텍스트 표시 (px 및 %)
            frame[:] = draw_multiline_text_with_box(
                frame=frame,
                lines=[
                    {"text": "Sway", "font_size": 16},
                    {"text": f"{sway_px:.1f}px", "font_size": 20},
                    #{"text": f"{sway_percent:.1f}%", "font_size": 24}
                ],
                position=(hip_center_x + 80, hip_center_y - 40),
                font_path=conf._font_path_sub,
                align="center"
            )

        
        # 최종 프레임 적용
        frame[:] = cv2.addWeighted(overlay, 0.5, frame, 0.5, 0.0)
5