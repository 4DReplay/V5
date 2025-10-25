from typing import Union
import os
import time
import socket
import subprocess
import random
import string
import math
import glob
import numpy as np
import psutil

try:
    import GPUtil
except ImportError:
    GPUtil = None
    import warnings
    warnings.warn("GPUtil not installed; GPU info will be unavailable")

def get_gpu_summary():
    if GPUtil is None:
        return []
    try:
        gpus = GPUtil.getGPUs()
        return [{"id": g.id, "name": g.name, "mem_total": g.memoryTotal, "mem_used": g.memoryUsed, "load": g.load} for g in gpus]
    except Exception as e:
        return []
        

from datetime import datetime


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


# ─────────────────────────────────────────────────────────────────────────────
'''
Probe a video file duration using ffprobe.
Args:
    output_file: full path to media file
Returns:
    Duration in milliseconds, or 0 on failure.
'''
# ─────────────────────────────────────────────────────────────────────────────
def get_duration(self, output_file: str) -> int:
    if output_file == '' or not os.path.exists(output_file):
        return 0

    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "json",
        output_file
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        fd_log.error("ffprobe error:", result.stderr)
        return 0

    try:
        info = json.loads(result.stdout)
        duration_sec = float(info["format"]["duration"])
        duration_ms = int(duration_sec * 1000)
        return duration_ms
    except Exception as e:
        fd_log.error("JSON parse or access error:", e)
        fd_log.error("ffprobe stdout:", result.stdout)
        return 0


def fd_format_elapsed_time(elapsed_sec: float) -> str:
    '''Format seconds as human-readable h m s string.'''
    hours = int(elapsed_sec // 3600)
    minutes = int((elapsed_sec % 3600) // 60)
    seconds = elapsed_sec % 60

    if hours > 0:
        return f"{hours}h {minutes}m {seconds:.2f}s"
    elif minutes > 0:
        return f"{minutes}m {seconds:.2f}s"
    else:
        return f"{seconds:.2f}s"