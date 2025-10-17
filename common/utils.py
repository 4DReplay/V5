from typing import Union
import os
import time
import socket
import subprocess
import random
import string
import math
import glob
from datetime import datetime

import numpy as np
import psutil
import GPUtil



def get_datetime() -> str:
    return f'{datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]}'


def get_memory_usage() -> float:
    # Get system memory usage in bytes
    memory_info = psutil.virtual_memory()
    return memory_info.percent


def get_cpu_usage(duration=None) -> float:
    # Get CPU usage as a percentage
    cpu_usage = psutil.cpu_percent(interval=duration)
    return cpu_usage


def get_gpu_usage():
    try:
        # Get GPU usage as a percentage using GPUtil
        gpus = GPUtil.getGPUs()
        if gpus:
            gpu_usage = gpus[0].load * 100  # Assuming there is at least one GPU
            return round(gpu_usage, 2)
        else:
            return None
    except Exception as e:
        print(f"Error getting GPU usage: {e}")
        return None


def get_network_usage(duration=1, output_unit='mbps'):
    try:
        net_info_before = psutil.net_io_counters(pernic=True)
        time.sleep(duration)
        net_info_after = psutil.net_io_counters(pernic=True)
        net_usage = []

        # Conversion factors for different output units
        unit_factors = {'kbps': 1024, 'mbps': 1024 ** 2, 'gbps': 1024 ** 3}

        for interface, after in net_info_after.items():
            # Exclude loopback interface
            if 'loopback' not in interface.lower() and interface.lower() != 'lo':
                before = net_info_before.get(interface, psutil.net_io_counters())
                
                # Calculate the difference in bytes
                sent_bytes = after.bytes_sent - before.bytes_sent
                received_bytes = after.bytes_recv - before.bytes_recv
                
                # Convert to the specified output unit
                unit = unit_factors.get(output_unit.lower(), 8)

                # Calculate rates per second
                sent_rate = (sent_bytes * 8 / unit) / duration
                received_rate = (received_bytes * 8 / unit) / duration
                
                net_usage.append({
                    'interface': interface,
                    'sent': round(sent_rate, 2),
                    'received': round(received_rate, 2)
                })
        return net_usage
    
    except Exception as e:
        print(f"Error getting network usage: {e}")
        return None
    
    
def convert_bytes(bytes_size: float) -> str:
    # Convert bytes to a more human-readable format
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            break
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} {unit}"


def get_disk_usage():
    disk_usage = []
    partitions = psutil.disk_partitions()
    for partition in partitions:
        try:
            usage = psutil.disk_usage(partition.mountpoint)
            disk_usage.append(
                {
                    'partition': partition.device,
                    'total': convert_bytes(usage.total),
                    'used': convert_bytes(usage.used),
                    'free': convert_bytes(usage.free),
                    'percent': f'{usage.percent:.2f} %'
                }
            )
            
        except Exception as e:
            print(f"  Error retrieving disk information: {e}")
            return None
    return disk_usage
    
def get_mac_addresses(ip_address=None):
    try:
        mac_addresses_set = set()
        # Iterate through network interfaces
        for interface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if ip_address is None or (addr.address == ip_address and socket.AF_INET == addr.family):
                    # Exclude loopback addresses
                    if not addr.address.startswith('127.') and not addr.address.startswith('::1'):
                        mac_addresses_set.add(psutil.net_if_addrs()[interface][0].address)
        mac_addresses = list(mac_addresses_set)
        if not mac_addresses:
            return ["No matching network adapter found."]
        
        return mac_addresses
    except Exception as e:
        return [f"Error: {e}"]


def get_nvidia_gpu_info() -> tuple[str,str]:
    gpu_model, driver_version = '',''
    try:
        # Run nvidia-smi command to get GPU information
        cmd = 'nvidia-smi --query-gpu=name --format=csv,noheader,nounits'
        result_model = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, shell=True)

        # Run nvidia-smi command to get driver version
        cmd = 'nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits'
        result_driver_version = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, shell=True)

        # Extract GPU model and driver version from the command output
        gpu_model = result_model.stdout.strip()
        driver_version = result_driver_version.stdout.strip()
    except Exception as e:
        print(f"Error getting NVIDIA GPU information: {e}")
    return gpu_model, driver_version


# yyyymmdd_hhmm_{random characters} 형식의 문자열을 생성
# length: 랜덤 문자열 길이
def generate_token(length=4) -> str:
    current_time = datetime.now()
    # yyyymmdd-hhmm 형식으로 포맷팅
    formatted_time = current_time.strftime("%Y%m%d_%H%M")
    # 알파벳 대소문자 랜덤 문자열 생성
    characters = string.ascii_letters
    #characters += string.digits     # 숫자까지 포함할 경우
    random.seed()
    random_str = ''.join(random.choice(characters) for _ in range(length))
    result = f"{formatted_time}_{random_str}"
    return result


# IP 문자열을 6자리 숫자로 구성된 문자열 ID로 변경
# ex) 192.168.5.14 -> 005014
def IPv4ToCamID(ip: str) -> str:
    id = ""
    tokens = ip.split('.')
    if len(tokens) > 3 :
        id = f'{int(tokens[2]):03}{int(tokens[3]):03}'
    return id


# "YYYYMMDD_HHMMSS" 형식으로 문자열을 생성하고 args 의 문자열을 뒤에 붙임.
def create_rec_name(*args) -> str:
    formatted_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    rec_name = formatted_time + ('_' + '_'.join(args) if args else '')
    return rec_name

# presd 에서 저장하는 파일 하나에 담긴 프레임 수
def fileframes(fps: int, gop: int) -> int:
    return fps - (fps % gop)


def framenum_to_fileindex(fps: int, gop: int, frame_num: int) -> tuple[int,int]:
    file_frames = fileframes(fps, gop)
    file_index = int(frame_num // file_frames)
    frame_index = int(frame_num % file_frames)
    return file_index, frame_index


def framenum_to_markertime(fps: int, gop: int, frame_num: int) -> float:
    file_frames = fileframes(fps, gop)
    file_index, frame_index = framenum_to_fileindex(fps, gop, frame_num)
    marekr_time = round((file_index * file_frames + frame_index) / fps, 2)
    return marekr_time


def audio_timestamp_to_framenum(video_start_ts: np.uint64, video_fps: int, audio_ts: np.uint64, audio_cycle: np.uint64, wraparound: np.uint64) -> int:
    n = 1001 if video_fps % 25 != 0 else 1000
    ts =  (audio_cycle * wraparound) + audio_ts - video_start_ts
    sec_num = ts // n
    remain = ts % n
    remain_time = remain / n
    
    frame_num = int(sec_num * video_fps)
    # lower = frame_num + math.floor(remain_time * video_fps)
    # return lower
    upper = frame_num + math.ceil(remain_time * video_fps)
    return upper


def get_error_msg(code: int):
    match code:
        case 1000:
            return "success"
        case 3000:
            return "fails to run"
        case _:
            return "error"
        
        
def most_recent_mp4_file(directory_path: str) -> Union[None, str]:
    # Ensure the provided path is a valid directory
    if not os.path.isdir(directory_path):
        raise ValueError(f"The path '{directory_path}' is not a valid directory.")

    # Create a list of .mp4 files in the directory
    mp4_files = glob.glob(os.path.join(directory_path, '*.mp4'))

    # Check if any .mp4 files are found
    if not mp4_files:
        return None  # No .mp4 files found

    # Get the most recently created .mp4 file
    most_recent_file = max(mp4_files, key=os.path.getctime)
    return most_recent_file

    # Extract and return the file name
    file_name = os.path.basename(most_recent_file)
    return file_name


def create_nested_directories(base_dir, subdirectories) -> str:
    current_path = base_dir
    for subdirectory in subdirectories:
        current_path = os.path.join(current_path, subdirectory)
        os.makedirs(current_path, exist_ok=True)
    return current_path