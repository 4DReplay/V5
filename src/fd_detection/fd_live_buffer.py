# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_live_buffer.py
# - 2025/05/28
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
# L/O/G/
# check     : ✅
# warning   : ⚠️
# error     : ❌
# function fold -> Ctrl + K, 0
# function unfold -> Ctrl + K, J
# remove breakpoint -> Ctrl + Shift + f9
# move next breakpoint -> Ctrl + Shift + D
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import av
import traceback
import threading
import subprocess
import time

from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log
from collections            import deque
from fd_detection.fd_live_detect_main    import fd_live_detecting_thread

# ─────────────────────────────────────────────────────────────────────────────
# def fd_live_buffering_thread(type_target, rtsp_url, buffer_sec): 
# [owner] hongsu jung
# [date] 2025-05-28
# ─────────────────────────────────────────────────────────────────────────────
def fd_live_buffering_thread(type_target, rtsp_url, output_folder):     
    # create thread
    # PyAV
    thread_buffer = threading.Thread(target=fd_live_buffering_PyAV, args=(type_target, rtsp_url, output_folder,))
    thread_buffer.start()

# ─────────────────────────────────────────────────────────────────────────────
# class RTSPBufferReceiver
# [owner] hongsu jung
# [date] 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────
class RTSPBufferReceiver:
    def __init__(self, rtsp_url, type_target):
        self.type = type_target
        self.rtsp_url = rtsp_url
        self.thread = None
        self.running = False
        self.paused = False
        self.lock = threading.Lock()

    def start(self):
        if self.thread is None:
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
            self.thread = None

    def pause(self):
        with self.lock:
            self.paused = True

    def resume(self):
        with self.lock:
            self.paused = False

    def get_buffer(self):
        return list(self.buffer)

    def _run(self):
        retry_delay_sec = 3
        max_retry_count = 10
        retry_count = 0

        while self.running and retry_count < max_retry_count:
            try:
                av.logging.set_level(av.logging.PANIC)
                container = av.open(self.rtsp_url, options={
                    'rtsp_transport': 'tcp',
                    'stimeout': '5000000'
                })

                video_streams = [s for s in container.streams if s.type == 'video']
                if not video_streams:
                    raise RuntimeError("No video stream found in RTSP.")

                stream = video_streams[0]
                stream.thread_type = 'AUTO'

                fps = float(stream.average_rate) if stream.average_rate else 30.0
                width = stream.codec_context.width
                height = stream.codec_context.height
                max_frame = int(fps * conf._ring_buffer_duration)
                mem_buffer = deque(maxlen=max_frame)

                conf._live_mem_buffer[self.type]        = mem_buffer
                conf._live_mem_buffer_addr[self.type]   = self.rtsp_url 
                conf._live_mem_buffer_status[self.type] = conf._live_mem_buffer_status_record

                fd_log.info(f"🚩 Connected (try {retry_count+1}) - 0x{self.type:x}")
                fd_log.info(f"2️⃣ Resolution: {width}x{height}") 
                fd_log.info(f"3️⃣ fps: {fps:.1f}")    
                fd_log.info(f"4️⃣ buffer size: {max_frame}") 

                # Reset retry count after successful connection
                retry_count = 0

                for packet in container.demux(stream):
                    if not self.running:
                        break

                    with self.lock:
                        if self.paused:
                            time.sleep(0.01)
                            continue

                    for frame in packet.decode():
                        timestamp = time.time()
                        ndarray = frame.to_ndarray(format='bgr24')
                        mem_buffer.append((timestamp, ndarray))

            except Exception as e:
                fd_log.error(f"[RTSPBufferReceiver] ❌ Error (try {retry_count+1}): {e}")
                traceback.print_exc()
                retry_count += 1
                time.sleep(retry_delay_sec)
            finally:
                # PyAV container는 명시적으로 close 필요
                try:
                    container.close()
                except:
                    pass

        if retry_count >= max_retry_count:
            fd_log.error(f"[RTSPBufferReceiver] ❌ Max retry exceeded for {self.rtsp_url}")            

ffmpeg_processes = []
# ─────────────────────────────────────────────────────────────────────────────##
# def run_ffmpeg(video, url):
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def run_ffmpeg(video, url):
    proc = subprocess.Popen([
        "ffmpeg",
        "-re",
        "-stream_loop", "-1",
        "-i", video,
        "-rtsp_transport", "tcp",  # ★ TCP 전송 방식 명시
        "-c", "copy",
        "-f", "rtsp",
        url
    ], creationflags=subprocess.CREATE_NEW_CONSOLE)
    ffmpeg_processes.append(proc)

# ─────────────────────────────────────────────────────────────────────────────##
# def stop_all_ffmpeg():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def stop_all_ffmpeg():
    for proc in ffmpeg_processes:
        proc.terminate()
    ffmpeg_processes.clear()

# ─────────────────────────────────────────────────────────────────────────────##
# def fd_rtsp_server_start():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def launch_ffmpeg_streams(video_paths, urls):
    for video, url in zip(video_paths, urls):
        thread = threading.Thread(target=run_ffmpeg, args=(video, url))
        thread.start()


# 2025-08-11
def fd_pause_live_detect():
    if hasattr(conf, "_live_pause_event"):
        conf._live_pause_event.clear()      # 🔴 pause
        fd_log.info("🔴 [AId] live detecting paused")

def fd_resume_live_detect():
    if hasattr(conf, "_live_pause_event"):
        conf._live_pause_event.set()        # 🟢 resume
        fd_log.info("🟢 [AId] live detecting resumed")

# ─────────────────────────────────────────────────────────────────────────────##
# def fd_rtsp_server_start():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def fd_rtsp_server_start():
    subprocess.Popen([
        "./libraries/rtsp/mediamtx.exe",
        "./libraries/rtsp/mediamtx.yml"
    ], creationflags=subprocess.CREATE_NEW_CONSOLE) #CREATE_NO_WINDOW)  🔇 창 안 띄움
    time.sleep(3)

    ipaddress = "localhost"
    port = conf._rtsp_port
    urls = [f"rtsp://{ipaddress}:{port}/cam{i}" for i in range(1, 5)]

    match conf._type_target:
        case conf._type_target_baseball:
            videos = [
                "./libraries/rtsp/baseball_cam1.mp4",
                "./libraries/rtsp/baseball_cam2.mp4",
                "./libraries/rtsp/baseball_cam3.mp4",
                "./libraries/rtsp/baseball_cam4.mp4"
            ]
            launch_ffmpeg_streams(videos, urls)

        case conf._game_type_nascar:
            videos = [
                "./libraries/rtsp/nascar_cam1.mp4",
                "./libraries/rtsp/nascar_cam2.mp4",
                "./libraries/rtsp/nascar_cam3.mp4",
                "./libraries/rtsp/nascar_cam4.mp4"
            ]
            launch_ffmpeg_streams(videos, urls)

        case conf._game_type_golf:
            videos = ["./libraries/rtsp/golf_cam1.mp4"]
            launch_ffmpeg_streams(videos, urls[:1])  # 골프는 cam1만 사용

# ─────────────────────────────────────────────────────────────────────────────##
# def fd_rtsp_server_stop():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def fd_rtsp_server_stop():
    # stop ffmpeg streaming
    stop_all_ffmpeg()

# ─────────────────────────────────────────────────────────────────────────────##
# def fd_rtsp_client_start():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def fd_rtsp_client_start():
    # set ip of rtsp
    ipaddress = conf._rtsp_server_ip_addr    
    port = conf._rtsp_port

    # rtsp://[PreSd IP]:9554/4DReplayLive/[Cam ID]
    url_1 = f"rtsp://{ipaddress}:{port}/4DReplayLive/027011"
    url_2 = f"rtsp://{ipaddress}:{port}/4DReplayLive/027012"
    url_3 = f"rtsp://{ipaddress}:{port}/4DReplayLive/027013"
    url_4 = f"rtsp://{ipaddress}:{port}/4DReplayLive/027014"

    match conf._type_target:
        ##################################################
        # BASEBALL
        ##################################################    
        case conf._type_target_baseball:
            output_folder = f"./videos/output/baseball"
            # set live player with sequence
            fd_live_buffering_thread (conf._type_live_batter_RH   , url_1, output_folder )
            fd_live_buffering_thread (conf._type_live_batter_LH   , url_2, output_folder )
            fd_live_buffering_thread (conf._type_live_pitcher     , url_3, output_folder )
            fd_live_buffering_thread (conf._type_live_hit         , url_4, output_folder )
            # detecting
            fd_live_detecting_thread (conf._type_live_batter_RH   , url_1)
            fd_live_detecting_thread (conf._type_live_batter_LH   , url_2)
            fd_live_detecting_thread (conf._type_live_pitcher     , url_3)  
            
            
        ##################################################
        # NASCAR
        ##################################################    
        case conf._game_type_nascar:
            output_folder = f"./videos/output/nascar"
            fd_live_buffering_thread (conf._type_live_nascar_1    , url_1, output_folder )
            fd_live_buffering_thread (conf._type_live_nascar_2    , url_2, output_folder )
            fd_live_buffering_thread (conf._type_live_nascar_3    , url_3, output_folder )
            fd_live_buffering_thread (conf._type_live_nascar_4    , url_4, output_folder )      

        ##################################################
        # GOLF
        ##################################################            
        case conf._game_type_golf:      
            output_folder = f"./videos/output/nascar"
            fd_live_buffering_thread (conf._type_live_golfer_1    , url_1, output_folder )
            fd_live_buffering_thread (conf._type_live_golfer_2    , url_2, output_folder )
            fd_live_buffering_thread (conf._type_live_golfer_3    , url_3, output_folder )
            '''
            # detecting
            fd_live_detecting_thread (conf._type_live_golfer_1   , url_1)
            fd_live_detecting_thread (conf._type_live_golfer_2   , url_2)
            fd_live_detecting_thread (conf._type_live_golfer_3   , url_3)  
            '''
            
# ─────────────────────────────────────────────────────────────────────────────##
# def fd_rtsp_client_stop():
# owner : hongsu jung
# date : 2025-06-14
# ─────────────────────────────────────────────────────────────────────────────#
def fd_rtsp_client_stop():
    set_record_status_all(conf._live_mem_buffer_status_stop)    

       
# ─────────────────────────────────────────────────────────────────────────────##
# def set_record_status_all(record):
# owner : hongsu jung
# date : 2025-06-05
# overlay image to mem file
# ─────────────────────────────────────────────────────────────────────────────#
def set_record_status_all(record):

    # PyAV
    for type_target, _ in conf._live_mem_buffer_receiver.items():
        receiver = conf._live_mem_buffer_receiver[type_target]   
        match record:
            case conf._live_mem_buffer_status_stop:
                receiver.stop()
            case conf._live_mem_buffer_status_pause:
                receiver.pause()
            case conf._live_mem_buffer_status_pause:
                receiver.pause()
            case conf._live_mem_buffer_status_resume:
                receiver.resume()        

# ─────────────────────────────────────────────────────────────────────────────##
# fd_live_buffering_PyAV(type_target, rtsp_url, output_folder):
# owner : hongsu jung
# date : 2025-06-05
# overlay image to mem file
# ─────────────────────────────────────────────────────────────────────────────#
def fd_live_buffering_PyAV(type_target, rtsp_url, output_folder):
    
    conf._folder_output = output_folder
    # set live viewer mode
    match type_target:
        # mosaic : nascar
        case conf._type_live_nascar_1 | conf._type_live_nascar_2 | conf._type_live_nascar_3 | conf._type_live_nascar_4 :
            conf._live_player_mode = conf._live_player_mode_nascar
            conf._ring_buffer_duration = conf._ring_buffer_duration_nascar
        # sequence : batter
        case conf._type_live_batter_RH | conf._type_live_batter_LH :
            conf._live_player_mode = conf._live_player_mode_baseball
            conf._ring_buffer_duration = conf._ring_buffer_duration_batter
        # sequence : pitcher
        case conf._type_live_pitcher :
            conf._live_player_mode = conf._live_player_mode_baseball
            conf._ring_buffer_duration = conf._ring_buffer_duration_pitcher
        # sequence : hit
        case conf._type_live_hit :
            conf._live_player_mode = conf._live_player_mode_baseball
            conf._ring_buffer_duration = conf._ring_buffer_duration_hit            
        # sequence : golf
        case conf._type_live_hit :
            conf._live_player_mode = conf._live_player_mode_golf
            conf._ring_buffer_duration = conf._ring_buffer_duration_golfer        
    
    # 최종 캡처 (바로 시작)
    receiver = RTSPBufferReceiver(rtsp_url, type_target)
    receiver.start()
    conf._live_mem_buffer_receiver[type_target] = receiver
    
