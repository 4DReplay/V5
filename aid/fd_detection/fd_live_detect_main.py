# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_live_detect.py
# - 2025/06/01
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
# ffplay rtsp://192.168.0.100:8554/live
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import time
import threading

import fd_utils.fd_config       as conf
from fd_utils.fd_logging        import fd_log

from collections                import deque

from fd_detection.fd_live_detect_detail import get_detect_type
from fd_detection.fd_live_detect_detail import check_batter_frame
from fd_detection.fd_live_detect_detail import check_pitcher_frame
from fd_detection.fd_live_detect_detail import check_golfer_frame


# ─────────────────────────────────────────────────────────────────────────────
# def fd_live_detecting_thread(type_target, rtsp_url): 
# [owner] hongsu jung
# [date] 2025-05-28
# ─────────────────────────────────────────────────────────────────────────────
def fd_live_detecting_thread(type_target, rtsp_url):     
    # create thread
    thread_detect = threading.Thread(target=fd_live_detecting, args=(type_target, rtsp_url, ))
    thread_detect.start()    

# ─────────────────────────────────────────────────────────────────────────────
# def fd_live_detecting(type_target, rtsp_url):
# [owner] hongsu jung
# [date] 2025-06-01
# ─────────────────────────────────────────────────────────────────────────────
def fd_live_detecting(type_target, rtsp_url):

    conf.fd_dashboard(type_target,f"🚩 Start Read RTSP: URL:{rtsp_url} ")
    
    ############################################################
    # Initiate
    ############################################################
    type_detect = get_detect_type(type_target)    
    if type_detect not in conf._object_status_queue     : conf._object_status_queue[type_detect]        = deque(maxlen=conf._detect_queue_size)
    if type_detect not in conf._object_number_queue     : conf._object_number_queue[type_detect]        = deque(maxlen=conf._detect_queue_size)
    if type_detect not in conf._object_handedness_queue : conf._object_handedness_queue[type_detect]    = deque(maxlen=conf._detect_queue_size)

    match type_target:
        case conf._type_live_batter_RH:
            conf._batter_detect_RH_area = set_detect_area(type_target)
            live_detection_batter(type_target) 
        case conf._type_live_batter_LH: 
            conf._batter_detect_LH_area = set_detect_area(type_target) 
            live_detection_batter(type_target)  
        case conf._type_live_pitcher: 
            conf._pitcher_detect_area   = set_detect_area(type_target)
            live_detection_pitcher(type_target) 
        case conf._type_live_nascar_1 | conf._type_live_nascar_detect:            
            conf._prev_obj_roi       = None    
            conf._prev_obj_roi_valid = False
            conf._nascar_detect_area     = set_detect_area(type_target)
            live_detection_nascar(type_target) 

# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_batter():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_batter(type_target):   

    mem_buffer = conf._live_mem_buffer.get(type_target)
    while True:
        # 2025-08-11
        conf._live_pause_event.wait()       # 🔒 pause 중이면 여기서 블록됨

        if(mem_buffer is None):
            time.sleep(0.1)
            mem_buffer = conf._live_mem_buffer.get(type_target)
            continue
        if len(mem_buffer) == 0:
            time.sleep(0.01)
            continue
        timestamp, latest_frame = mem_buffer[-1]
        check_batter_frame(type_target, latest_frame, timestamp)            
        time.sleep(0.01)


# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_pitcher():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_pitcher(type_target):
    mem_buffer = conf._live_mem_buffer.get(type_target)
    while True:
        # 2025-08-11
        conf._live_pause_event.wait()       # 🔒 pause 중이면 여기서 블록됨
        
        if mem_buffer is None:
            time.sleep(0.1)
            mem_buffer = conf._live_mem_buffer.get(type_target)
            continue
        if len(mem_buffer) == 0:
            time.sleep(0.001)
            continue
        timestamp, latest_frame = mem_buffer[-1]
        check_pitcher_frame(latest_frame, timestamp)
        time.sleep(0.001)

    
# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_nascar():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_nascar(type_target):
    mem_buffer = conf._live_mem_buffer.get(type_target)    
    while True:
        if(mem_buffer is None):
            time.sleep(0.1)
            mem_buffer = conf._live_mem_buffer.get(type_target)
            continue
        if len(mem_buffer) == 0:
            time.sleep(0.01)
            continue            
        timestamp, latest_frame = mem_buffer[-1]        
        check_nascar_frame(type_target, latest_frame, timestamp)         
        time.sleep(0.01)


# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_golfer():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_golfer(type_target):
    mem_buffer = conf._live_mem_buffer.get(type_target)    
    conf.fd_dashboard(conf._player_type.golfer,f"\r🟢[Golfer][0x{type_target:x}] Buffer Ready")

    try:
        while True:
            if(mem_buffer is None):
                time.sleep(0.1)
                mem_buffer = conf._live_mem_buffer.get(type_target)
                continue
            if len(mem_buffer) == 0:
                time.sleep(0.01)
                continue   
            timestamp, latest_frame = mem_buffer[-1]
            check_golfer_frame(latest_frame, timestamp)
            time.sleep(0.01)

    except KeyboardInterrupt:
        conf.fd_dashboard(conf._player_type.golfer,"Stopping...")
    finally:
        conf.fd_dashboard(conf._player_type.golfer,"live_detection_golfer finished")

# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_batsman():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_batsman(type_target):
    mem_buffer = conf._live_mem_buffer.get(type_target)        
    conf.fd_dashboard(conf._player_type.detect,f"\r🟢[Batsman][0x{type_target:x}] Buffer Ready")

    try:
        while True:
            size = mem_buffer.get_size()
            conf.fd_dashboard(conf._player_type.detect,f"\r[Batsman][0x{type_target:x}] Buffer size: {size} frames (~{size/60:.1f} sec)")
            time.sleep(0.01)

    except KeyboardInterrupt:
        conf.fd_dashboard(conf._player_type.detect,"Stopping...")
    finally:
        conf.fd_dashboard(conf._player_type.detect,"live_detection_batsman finished")

# ─────────────────────────────────────────────────────────────────────────────
# def live_detection_bowler():
# [owner] hongsu jung
# [date] 2025-05-29
# ─────────────────────────────────────────────────────────────────────────────
def live_detection_bowler(type_target):
    mem_buffer = conf._live_mem_buffer.get(type_target)                
    conf.fd_dashboard(conf._player_type.detect,f"\r🟢[Bowler][0x{type_target:x}] Buffer Ready")

    try:
        while True:
            size = mem_buffer.get_size()
            conf.fd_dashboard(conf._player_type.detect,f"\r[Bowler][0x{type_target:x}] Buffer size: {size} frames (~{size/60:.1f} sec)")
            time.sleep(0.1)

    except KeyboardInterrupt:
        conf.fd_dashboard(conf._player_type.detect,"Stopping...")
    finally:
        conf.fd_dashboard(conf._player_type.detect,"live_detection_bowler finished")

# ─────────────────────────────────────────────────────────────────────────────
# def set_detect_area(type_target):
# [owner] hongsu jung
# [date] 2025-06-02
# ─────────────────────────────────────────────────────────────────────────────
def set_detect_area(type_target):
    # select tracking object        
    match type_target:
        case conf._type_live_batter_RH:
            min_x = conf._batter_detect_RH_left      
            max_x = conf._batter_detect_RH_right     
            min_y = conf._batter_detect_RH_top       
            max_y = conf._batter_detect_RH_bottom      

        case conf._type_live_batter_LH:
            min_x = conf._batter_detect_LH_left     
            max_x = conf._batter_detect_LH_right    
            min_y = conf._batter_detect_LH_top      
            max_y = conf._batter_detect_LH_bottom     

        case conf._type_live_pitcher:            
            min_x = conf._pitcher_detect_left          
            max_x = conf._pitcher_detect_right         
            min_y = conf._pitcher_detect_top           
            max_y = conf._pitcher_detect_bottom        

        case conf._type_live_golfer:
            min_x = conf._golfer_detect_left     
            max_x = conf._golfer_detect_right    
            min_y = conf._golfer_detect_top      
            max_y = conf._golfer_detect_bottom  

        case conf._type_live_batsman | conf._type_live_bowler :
            min_x = conf._crease_detect_left     
            max_x = conf._crease_detect_right    
            min_y = conf._crease_detect_top      
            max_y = conf._crease_detect_bottom  

        case conf._type_live_nascar_1 | conf._type_live_nascar_detect:
            min_x = conf._nascar_detect_left     
            max_x = conf._nascar_detect_right    
            min_y = conf._nascar_detect_top      
            max_y = conf._nascar_detect_bottom  
    
    return [[min_x,min_y],[max_x,max_y]]


