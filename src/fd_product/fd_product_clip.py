# ─────────────────────────────────────────────────────────────────────────────#
# fd_product_clip.py
# - 2025/11/28
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import os
import time
import threading
import av  # PyAV
from pathlib import Path
from fd_utils.fd_logging        import fd_log

# ─────────────────────────────────────────────────────────────────────────────#
# 🛠️ Helper
# ─────────────────────────────────────────────────────────────────────────────#
def wait_for_file_ready(filepath, timeout=10):
    """파일 사이즈가 증가하지 않을 때까지 대기"""
    start = time.time()
    last_size = -1
    while time.time() - start < timeout:
        if not os.path.exists(filepath):
            time.sleep(0.5)
            continue
        size = os.path.getsize(filepath)
        if size == last_size and size > 0:
            return True
        last_size = size
        time.sleep(0.5)
    return False

# ─────────────────────────────────────────────────────────────────────────────#
# create paylod for send AIc about product info
'''
  { "PreSd_id": "10.82.104.210", "ip": "10.82.104.11", "rotate": 1, "PreSd_path": "C_Movie|C:\\" },
  ----- convert
  {
  "PreSd": [
    {
      "ip": "10.82.104.210",
      "cam_ips": [
        {"ip": "10.82.104.11", "index":1, "rotate": 1},
        {"ip": "10.82.104.12", "index":2, "rotate": 1}
      ],
      "path": "C_Movie|C:\\"
    },
'''
# ─────────────────────────────────────────────────────────────────────────────#
def fd_convert_AIc_info(cam_env: dict) -> dict:    
    grouped = {}
    for cam in cam_env["cameras"]:
        pre_ip = cam["PreSd_id"]
        path   = cam["PreSd_path"]
        cam_ip = cam["ip"]
        cam_id = cam["id"]
        rotate = cam.get("rotate", 0)

        if pre_ip not in grouped:
            grouped[pre_ip] = {
                "ip": pre_ip,
                "cam_ips": [],
                "path": path
            }

        grouped[pre_ip]["cam_ips"].append({
            "ip": cam_ip,
            "id": cam_id,
            "rotate": rotate
        })

    cam_info = {
        "camera-fps": cam_env["camera-fps"],
        "camera-resolution": cam_env["camera-resolution"],
        "source-folder": cam_env["record-folder"],
        "PreSd": list(grouped.values()),
    }
    return cam_info
def fd_create_payload_for_preparing_to_AIc(ip, camera_info, adjust_info):    
    """
    ip: AIC IP (PreSd_id)
    camera_env: fd_convert_AIc_info() 결과
    adjust_info: 조정 정보 (dict)
    """
    # presd_info 구조:
    # { "PreSd": [ { "ip": "...", "cam_ips": [...], "path": ... }, ... ] }

    # Step 1: 해당 ip의 PreSd 정보만 찾기
    matched = None
    for item in camera_info.get("PreSd", []):
        if item["ip"] == ip:
            matched = item
            break

    if matched is None:
        # 해당 IP가 없으면 빈 구조로 반환
        matched = {
            "ip": ip,
            "cam_ips": [],
            "path": ""
        }

    cam_fps         = camera_info.get("camera-fps")
    cam_resolution  = camera_info.get("camera-resolution")
    record_folder   = camera_info.get("source-folder")    
    camera_format_paylod = {
        "fps":cam_fps,
        "resolution":cam_resolution,
    }

    matched["camera-format"] = camera_format_paylod
    matched["record-folder"] = record_folder
    # Step 2: payload 구성
    payload = {
        "video-info": matched,
        "adjust": adjust_info
    }

    return payload

# ─────────────────────────────────────────────────────────────────────────────#
# create paylod for send AIc about product info
'''
    product_start_payload = {
        # for read original file
        "product-start-time"    : self.producing_start_time,        # ms -> production start time (ms)
        "record-start-time"     : self.record_start_time,           # ms -> record start time (ms)
        "record-sync-diff-ms"   : self.record_diff_time,            # ms -> record sync diff (ms)
        "record-folder-name"    : self.recording_name,              # folder name of recorded data
        # for producing info
        "product-name": self.product_name,
        "product-target": self.product_target,
        "product-save-path": self.product_save_path,
        # for creating file info
        "use-audio": use_audio,
        "output": output_opt,
    }
'''
# ─────────────────────────────────────────────────────────────────────────────#    
def fd_create_payload_for_product_to_AIc(pkt_info: dict, cam_fps)     -> dict:
    
    t_product_start = pkt_info.get("product-start-time")
    t_record_start  = pkt_info.get("record-start-time")
    t_record_gap    = pkt_info.get("record-sync-diff-ms")
    
    t_start_ms = t_product_start - (t_record_start + t_record_gap)
    if t_start_ms < 0:
        t_start_ms = 0

    sec = t_start_ms / 1000.0
    t_start_index = int(sec)

    frac = sec - t_start_index
    frame_float = frac * cam_fps

    f_index_zero_based = int(round(frame_float))
    if f_index_zero_based >= cam_fps:
        t_start_index += 1
        f_index_zero_based = 0

    f_start_index = f_index_zero_based + 1

    aic_payload = {
        "start-file-time"   : t_start_index + 1,               # start file index
        "start-file-frame"  : f_start_index,                   # start file frmae        
    }
    pkt_info["source-file"] = aic_payload
    return pkt_info

# ─────────────────────────────────────────────────────────────────────────────#
# calibration file from original source
'''
    video_source = 
    {
        "ip": "10.82.104.210",
        "fps":60,
        "resolution":"UHD",
        "cam_ips": [
            {"ip":"10.82.104.11", "id":1, "rotate":1},
            {"ip":"10.82.104.12", "id":2, "rotate":1}
        ],
        "path": "C_Movie|C:\\"
    }

    prod_info = 
    {
        'source-file': 
        {
            'start-file-time': 3, 
            'start-file-frame': 8}
        },
        'record-folder':"2025_11_28_16_58_06"
        'output': 
        {
            'resolution': 'FHD', 
            'codec': 'H.264', 
            'fps': '30', 
            'bitrate': '15', 
            'gop': '30', 
            'output_path': '\\\\10.82.104.210\\Output'
        }, 
        'product-save-path': '\\\\10.82.104.210\\Output\\20251128\\165811_23XI Racing-Team_23', 
        'product-start-time': 1764316691437.1753, 
        'record-start-time': 1764316688273.3662, 
        'record-sync-diff-ms': 1030.81689453125, 
        'record-folder-name': '2025_11_28_16_58_06', 
        'product-name': '165811_23XI Racing-Team_23', 
        'product-target': 
        '23XI Racing-Team_23', 
        'use-audio': False,   
    }

    prod_adjust_info = 
        "adjust": {
        ... adjust_info ...
        }
'''
# ─────────────────────────────────────────────────────────────────────────────#    

# ─────────────────────────────────────────────────────────────────────────────#    
#  Main Worker (Thread job on each IP)
# ─────────────────────────────────────────────────────────────────────────────#    
# ─────────────────────────────────────────────────────────────────────────────#    
#  Main Worker (Thread job on each camera)
# ─────────────────────────────────────────────────────────────────────────────#    
def calibrate_worker(ch_index, cam_ip, video_source, prod_info, prod_adjust_info):

    tag, root_drive = video_source["path"].split("|")
    # C_Movie → C:\Movie
    MOVIE_MAP = {
        "C_Movie": "C:\\Movie",
        "D_Movie": "D:\\Movie",
        "E_Movie": "E:\\Movie",
    }

    root_path = MOVIE_MAP.get(tag, root_drive)   # fallback: raw path
    folder = prod_info["record-folder"]
    input_root = os.path.join(root_path, folder)
    
    # IP “10.82.104.11” → “104011”
    ip_parts = cam_ip.split(".")
    cam_suffix = ip_parts[-2] + ip_parts[-1]

    start_time  = prod_info["source-file"]["start-file-time"]   # 예: 3
    start_frame = prod_info["source-file"]["start-file-frame"]  # 예: 8

    # 출력 폴더 생성
    save_root = prod_info["product-save-path"]
    ch_path = os.path.join(save_root, f"ch{ch_index:02d}")
    os.makedirs(ch_path, exist_ok=True)

    # Calibrator 준비 (Hook)
    calibrator = None  # FrameCalibrator(prod_adjust_info) 가능

    current_second = start_time
    segment_index = 1

    print(f"[ch{ch_index}] Start processing cam {cam_ip}...")

    while True:
        mp4_file = os.path.join(input_root, f"{cam_suffix}_{current_second}.mp4")

        # 파일 준비 대기 (녹화 중)
        if not wait_for_file_ready(mp4_file):
            fd_log.info(f"[ch{ch_index}] waiting for file: {mp4_file}")
            time.sleep(1)
            continue

        # 파일 오픈
        try:
            container = av.open(mp4_file)
        except Exception as e:
            fd_log.info(f"[ch{ch_index}] open fail, retry: {mp4_file} ({e})")
            time.sleep(1)
            continue

        # output 파일명
        out_file = os.path.join(ch_path, f"segment_{segment_index:04d}.m4s")
        out = av.open(out_file, mode="w")

        # output format = FHD 30fps
        stream_out = out.add_stream("h264_nvenc", rate=30)
        stream_out.options = {"preset": "p3", "rc": "vbr", "bitrate": "15M"}
            
        stream_out.width = 1920
        stream_out.height = 1080

        # 시작 초면 start_frame offset 적용
        skip_frames = start_frame if current_second == start_time else 0

        for frame in container.decode(video=0):

            if skip_frames > 0:
                skip_frames -= 1
                continue

            # Calibration 적용
            if calibrator:
                frame = calibrator.apply(frame)

            # encoding
            packet = stream_out.encode(frame)
            if packet:
                out.mux(packet)

        # flush
        packet = stream_out.encode(None)
        if packet:
            out.mux(packet)

        out.close()
        container.close()

        fd_log.info(f"[ch{ch_index}] saved {out_file}")

        segment_index += 1
        current_second += 1
        time.sleep(0.05)


# ─────────────────────────────────────────────────────────────────────────────#    
#  Main Controller (create threads for each cam)
# ─────────────────────────────────────────────────────────────────────────────#    
def fd_calibrate_files(video_source: dict, prod_info: dict, prod_adjust_info: dict):

    cam_list = video_source.get("cam_ips", [])
    threads = []

    for cam in cam_list:
        ch_id = cam["id"]     # ← 핵심: cam_ips의 id로 chXX 생성
        cam_ip = cam["ip"]

        t = threading.Thread(
            target=calibrate_worker,
            args=(ch_id, cam_ip, video_source, prod_info, prod_adjust_info),
            daemon=True
        )
        t.start()
        threads.append(t)

        print(f"[Controller] Started thread for cam {cam_ip}, ch{ch_id:02d}")

    # 필요 시, 모든 스레드 join (옵션)
    # for t in threads:
    #     t.join()