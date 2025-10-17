# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /P/O/S/E/ D/E/T/E/C/T/I/O/N/
# fd_aid
# - 2024/11/05
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
# L/O/G/
# check     : ✅
# warning   : ⚠️
# error     : ❌
# ─────────────────────────────────────────────────────────────────────────────#


import cv2
import os
import re
import time
import win32gui
import win32con
import win32api
import shutil
import threading
import uuid
import numpy as np
import mediapipe as mp


from pathlib import Path
from datetime import datetime
from mpl_toolkits.mplot3d import Axes3D
from scipy.special import comb
from functools import singledispatch
from datetime import datetime, timedelta

from PyQt5.QtCore import QTimer

# ─────────────────────────────────────────────────────────────────────────────
# internal functions
# ─────────────────────────────────────────────────────────────────────────────
import fd_utils.fd_config       as conf
from fd_utils.fd_logging        import fd_log

from fd_utils.fd_file_edit      import fd_save_array_file
from fd_utils.fd_file_edit      import fd_load_array_files
from fd_utils.fd_file_edit      import fd_set_mem_file_4parts           # 4 parts (pre,start,post,end)
from fd_utils.fd_file_edit      import fd_set_mem_file_multiline        # multi line drawing
from fd_utils.fd_file_edit      import fd_set_mem_file_multi_ch
from fd_utils.fd_file_edit      import fd_set_mem_file_swing_analysis   # multi channels (front, back)
from fd_utils.fd_file_edit      import fd_clean_up
from fd_utils.fd_file_edit      import fd_get_datetime
from fd_utils.fd_file_edit      import fd_get_output_file_name
from fd_utils.fd_file_edit      import fd_get_clean_file_name
from fd_utils.fd_file_edit      import fd_multi_channel_configuration
from fd_utils.fd_file_util      import fd_format_elapsed_time

from fd_detection.fd_detect     import fd_detect_ball_pitcher
from fd_detection.fd_detect     import fd_detect_ball_batter
from fd_detection.fd_detect     import fd_detect_ball_hit
from fd_detection.fd_detect     import fd_detect_swing_poses

from fd_draw.fd_2d_draw         import fd_create_tracking_singleline
from fd_draw.fd_2d_draw         import fd_create_tracking_multiline
from fd_draw.fd_2d_draw         import fd_create_swing_analysis
from fd_draw.fd_2d_draw         import fd_create_division
from fd_draw.fd_2d_draw         import fd_combine_processed_files

from fd_calibration.fd_file_calibration import create_video_each_camera

from pathlib import Path
from datetime import datetime
import shutil

# ─────────────────────────────────────────────────────────────────────────────
# def set_zoom(zoom_ratio, center_x, center_y):
# [owner] hongsu jung
# [date] 2025-04-28
# ─────────────────────────────────────────────────────────────────────────────
def set_zoom(zoom_ratio, zoom_center_x, zoom_center_y):
    # zoom_ratio 처리
    if isinstance(zoom_ratio, str) and ',' in zoom_ratio:
        conf._zoom_ratio_list = [int(z.strip()) / 100.0 for z in zoom_ratio.split(',')]
    else:
        value = int(zoom_ratio.strip()) if isinstance(zoom_ratio, str) else zoom_ratio
        conf._zoom_ratio = value / 100.0

    # center_x 처리
    if isinstance(zoom_center_x, str) and ',' in zoom_center_x:
        conf._zoom_center_x_list = [int(x.strip()) for x in zoom_center_x.split(',')]
    else:
        conf._zoom_center_x = int(zoom_center_x.strip()) if isinstance(zoom_center_x, str) else zoom_center_x

    # center_y 처리
    if isinstance(zoom_center_y, str) and ',' in zoom_center_y:
        conf._zoom_center_y_list = [int(y.strip()) for y in zoom_center_y.split(',')]
    else:
        conf._zoom_center_y = int(zoom_center_y.strip()) if isinstance(zoom_center_y, str) else zoom_center_y

# ─────────────────────────────────────────────────────────────────────────────
# def fd_create_analysis_file(folder_input, folder_output, camera_ip, front_camera_ip, side_camera_ip, zoom_factor, analysis_cameras, start_time, end_time, hit_time, hit_frame = -1 ):
# [owner] hongsu jung
# https://4dreplay.atlassian.net/wiki/x/ewAAgQ
# ─────────────────────────────────────────────────────────────────────────────
#
# [input]
# 1. Type of sports : 
# 2. file folder : './videos/input/golf/outdoor/2024_04_28_13_18_33'
# 3. output folder : './videos/output/golf'
# 4. ip class : 101; -> camera ip class
# 5. camera number : 11 -> from "front_dsc" to change ip
# 6. start clip time : -1000 -> from "start_time"
# 7. end clip time : 1000 -> from "end_time"
# 8. fps : 10 -> from "fps"
# 9. zoom scale : 1.8 -> from "zoom_ratio"
# 10. selected moment second    : 508 [from 0 to selected timing file]
# 11. selected moment frame     : 30 [from 0 to selected timing] : default = -1
#
# [output]
# folder_output/XXX_2way_output.mp4'
# folder_output/XXX_3way_output.mp4'
# ───────────────────────────────────────────────────────────────────────────── 
def fd_create_analysis_file(type_target, folder_input, folder_output, camera_ip_class, camera_ip, start_time, end_time, selected_moment_sec, selected_moment_frm, fps, zoom_ratio): 

    '''
    # 2025-07-09
    # wait until previous creating detection
    while conf._multi_job_lock:
        time.sleep(0.01)

    # 2025-07-09
    # lock detection job blocking
    with conf._lock:
        conf._multi_job_lock = True
    '''

    # check process time
    start_process = time.perf_counter()  # 시작 시간 기록
        
    # ─────────────────────────────────────────────────────────────────────────────
    # setup configuration
    # ─────────────────────────────────────────────────────────────────────────────        
    conf._type_target           = type_target
    conf._folder_input          = folder_input
    conf._folder_output         = folder_output
    conf._camera_ip_class       = camera_ip_class
    conf._camera_ip             = camera_ip
    conf._start_sec_from_moment = start_time
    conf._end_sec_from_moment   = end_time
    conf._output_fps            = fps
    conf._zoom_ratio            = int(zoom_ratio)/100
    conf._selected_moment_sec   = selected_moment_sec
    conf._selected_moment_frm   = selected_moment_frm    
    # get date and time
    conf._output_datetime       = fd_get_datetime(folder_input)
    
    # clean previous processing files
    fd_clean_up()    

    # set output file resolution
    # 💡someday, need change to variable
    conf._output_width          = conf._resolution_fhd_width
    conf._output_height         = conf._resolution_fhd_height
    conf._cached_baseball_info_layer    = None
    # check preview
    conf._detect_check_preview  = False     # preview 실시 여부
    conf._detect_check_success  = False     # detect 결과에 대한 confirm
    # get unique name
    conf._unique_process_name    = uuid.uuid4()    
    # get clean feeds
    conf._clean_file_list = fd_get_clean_file_name(conf._local_temp_folder)
    # hit retry couny
    conf._hit_tracking_retry_cnt = 0


    # ─────────────────────────────────────────────────────────────────────────────####    
    # 1. Create File with Threading
    # ─────────────────────────────────────────────────────────────────────────────####    
    start_merging = time.perf_counter()  # 시작 시간 기록    
    fd_log.print("\r")
    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\033[33m── [PROCESS] 1️⃣ Start Merging \033[0m")    
    fd_log.print("\033[33m── Target:{0}, Folder:{1}, Output:{2}  \033[0m".format(type_target, folder_input, folder_output))
    fd_log.print("\033[33m── Camera Class:{0}, Camera IP:{1} \033[0m".format(camera_ip_class, camera_ip))    
    fd_log.print("\033[33m── Start Time:{0}, End Time:{1}  \033[0m".format(start_time, end_time))
    fd_log.print("\033[33m── fps:{0}, zoom:{1}  \033[0m".format(fps, zoom_ratio))        
    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")


    match type_target:        
        # ─────────────────────────────────────────────────────────────────────────────#
        # 2025-04-01
        # /S/I/N/G/L/E/        
        # ─────────────────────────────────────────────────────────────────────────────#
        case conf._type_baseball_batter_RH  | \
             conf._type_baseball_batter_LH  | \
             conf._type_baseball_pitcher    | \
             conf._type_baseball_hit        | \
             conf._type_baseball_hit_manual :            
            
            if conf._trackman_mode:
                #db raw data load
                result = conf._baseball_db.get_next_pitch_after(folder_input, selected_moment_sec)
                if result is False: 
                    fd_log.error(f"Not next pitch data {folder_input}, {selected_moment_sec}")               
                    #return False, ""
                result = conf._baseball_db.get_next_hit_after(folder_input, selected_moment_sec)
                if result is False:
                    fd_log.error(f"Not next hit data {folder_input}, {selected_moment_sec}")
                    #return False, ""


            fd_log.print(f"\033[33m✔️ Selected Moment {selected_moment_sec},{selected_moment_frm}  \033[0m")
            fd_log.print(f"\033[33m🚩hangtime:{conf._landingflat_hangtime:.2f},distance:{conf._landingflat_distance:.1f}, angle:{conf._landingflat_bearing:.1f}\033[0m")    
            fd_log.info("33m────────────────────────────────────────────────────────────────────────────────────────── ")
            # 2025-05-25
            if conf._type_target in (
                    conf._type_baseball_hit,
                    conf._type_baseball_hit_manual
                ):
                # check minimum distance
                if conf._landingflat_distance < conf._hit_minimum_distance:
                    fd_log.info(f"🚩[HIT] not enough distance : distance;{conf._landingflat_distance}, min distance:{conf._hit_minimum_distance} ")
                    return False, ""                 
            
            # additional information fio hit            
            result, file_directory, file_group = fd_set_mem_file_4parts()
            # set pose data file name
            data_file  = f"{file_directory}/{file_group}_baseball_data.pkl"

        # ─────────────────────────────────────────────────────────────────────────────#
        # 2025-07-01
        # Multi Line
        # ─────────────────────────────────────────────────────────────────────────────#
        case conf._type_baseball_hit_multi:
            #db raw data load
            if(conf._debug_mode == False):
                # 비거리와 Path를 hit_count 만큼 최신 데이터를 가져옴.
                result, pkl_list = conf._baseball_db.get_hit_tracking_data_paths(conf._team_code, conf._player_no, conf._multi_line_cnt)
            else:
                # Manual Test
                # file path, 비거리, 체공시간
                pkl_list = []
                pkl_object = ('./videos/input/baseball/KBO/2025_07_01_18_22_48/027014_1756_38_baseball_data.pkl', 120.5, 5.08, -42.4)
                pkl_list.append(pkl_object)                                            
                pkl_object = ('./videos/input/baseball/KBO/2025_06_29_18_42_36/027014_4188_49_baseball_data.pkl', 116.5, 4.68, 17.8 )
                pkl_list.append(pkl_object)                                
                pkl_object = ('./videos/input/baseball/KBO/2025_06_27_18_48_41/027014_7263_20_baseball_data.pkl', 114.3, 5.81, -43.7)
                pkl_list.append(pkl_object)                
                pkl_object = ('./videos/input/baseball/KBO/2025_06_27_18_48_41/027014_5636_46_baseball_data.pkl', 114.6, 4.81, -35.7)
                pkl_list.append(pkl_object)                
                pkl_object = ('./videos/input/baseball/KBO/2025_05_06_13_40_24/027014_2447_3_baseball_data.pkl', 117.6, 5.88, -41.5)
                pkl_list.append(pkl_object)                                
                
                multi_line_cnt = len(pkl_list)

            # set pkl_list
            conf._pkl_list = pkl_list   

            if(conf._multi_line_cnt == -1): # auto detect
                conf._multi_line_cnt = multi_line_cnt
            # set memfile for base video
            result, file_directory, file_group = fd_set_mem_file_multiline()

        # ─────────────────────────────────────────────────────────────────────────────#
        # 2025-04-01
        # Multi Line
        # *** need to restructuring
        # ─────────────────────────────────────────────────────────────────────────────#
        case conf._type_baseball_pitcher_multi:             
            pitch_type_counts = { 
                k.strip(): int(v.strip())
                for k, v in (item.split(":") for item in conf._option.split(","))
                }
            #db raw data load
            if(conf._debug_mode == False):
                result, pkl_list = conf._baseball_db.get_pitching_tracking_data_paths(conf._team_code,conf._player_no, pitch_type_counts)            
            else:
                # debug
                # hongsu
                # 2025-04-03
                # 구종, 구속, 회전수, Path  
                pkl_list = []
                pkl_object = ('Fastball',152.1,1989,'./videos/input/baseball/KBO/2025_03_22_13_56_16/028013_1673_8_baseball_data.pkl')
                pkl_list.append(pkl_object)
            # set pkl_list
            conf._pkl_list = pkl_list   
                
            # set memfile for base video
            result, file_directory, file_group = fd_set_mem_file_multiline(pkl_list)            

        # ─────────────────────────────────────────────────────────────────────────────#
        # hongsu 
        # 2025-04-28
        # Golf Swing Detection
        # ─────────────────────────────────────────────────────────────────────────────#
        case conf._type_golfer_2ch_LND_RH | \
             conf._type_golfer_3ch_LND_RH | \
             conf._type_golfer_3ch_POR_RH :
            conf._swing_right_hand = True
            if(type_target == conf._type_golfer_3ch_POR_RH):
                conf._image_portrait = True
            else:
                conf._image_portrait = False
            result, file_directory, file_group = fd_set_mem_file_swing_analysis()
            data_file  = f"{file_directory}/{file_group}_golf_data.pkl"
        
        case conf._type_golfer_2ch_LND_LH | \
            conf._type_golfer_3ch_LND_LH  | \
            conf._type_golfer_3ch_POR_LH : 
            if(type_target == conf._type_golfer_3ch_POR_LH):
                conf._image_portrait = True
            else:
                conf._image_portrait = False     
            conf._swing_right_hand = False
            result, file_directory, file_group = fd_set_mem_file_swing_analysis()            
            data_file  = f"{file_directory}/{file_group}_golf_data.pkl"

        case _ : 
            fd_log.error("❌ Wrong type of processing")
            result = False

    if result is False:
        fd_log.error("❌ Not exist files")
        return False, ""
    
    # 2025-03-18
    # check process time
    end_merging = time.perf_counter()  # 종료 시간 기록
    conf._time_merging = (end_merging - start_merging) * 1000
    fd_log.info(f"🕒\033[33m[File Process]:{conf._time_merging:,.2f} ms\033[0m")
    
    
    
    # ─────────────────────────────────────────────────────────────────────────────
    # 2. detection Data
    # ─────────────────────────────────────────────────────────────────────────────
    start_detecting = time.perf_counter()  # 시작 시간 기록
    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\033[33m── [PROCESS] 2️⃣ Start Detection \033[0m")
    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")   
    arr_ball_pos = []    
    arr_balls_pos = []    

    match type_target:        
        # baseball pitcher (battery) analysis
        case conf._type_baseball_pitcher: 
            result, arr_ball_pos = fd_detect_ball_pitcher()                        
        # baseball right hand batter analysis
        case conf._type_baseball_batter_RH:
            conf._swing_right_hand = True
            result, arr_ball_pos = fd_detect_ball_batter()
        # baseball left hand batter analysis
        case conf._type_baseball_batter_LH: 
            conf._swing_right_hand = False
            result, arr_ball_pos = fd_detect_ball_batter()
        # hit / homerun ball tracking
        case conf._type_baseball_hit:
            # serach with Trackman data    
            result, arr_ball_pos = fd_detect_ball_hit(True)       
        case conf._type_baseball_hit_manual:
            # serach with Trackman data    
            result, arr_ball_pos = fd_detect_ball_hit(False)           
        # pitcher multi
        case conf._type_baseball_pitcher_multi | conf._type_baseball_hit_multi:
            # already detected files
            result = True
        # 2025-04-29
        # golf
        case conf._type_golfer_2ch_LND_RH | conf._type_golfer_2ch_LND_LH | \
             conf._type_golfer_3ch_LND_RH | conf._type_golfer_3ch_LND_LH | \
             conf._type_golfer_3ch_POR_RH | conf._type_golfer_3ch_POR_LH :  
            result = fd_detect_swing_poses()
        # N/Y
        case _: 
            fd_log.warning(f"⚠️ N/Y in type_target:{type_target}") 
            return False, ""
        
    if(result == False):
        fd_log.error(f"❌ Fail Detection in {type_target}")
        return False, ""


    # save ball array to file
    match type_target:
        case conf._type_baseball_pitcher    | \
            conf._type_baseball_batter_RH   | \
            conf._type_baseball_batter_LH   | \
            conf._type_baseball_hit         | \
            conf._type_baseball_hit_manual :
            fd_save_array_file(data_file, arr_ball_pos)
            conf._tracking_data_path = data_file        
        # 2025-07-01
        # load ball arrays
        case conf._type_baseball_hit_multi | \
            conf._type_baseball_pitcher_multi :
            file_list = [file for file, *_ in conf._pkl_list]
            fd_load_array_files(file_list, arr_balls_pos)     
    
    # check process time
    end_detecting = time.perf_counter()  # 종료 시간 기록
    conf._time_detecting = (end_detecting - start_detecting) * 1000
    fd_log.info(f"\033[33m🕒[Detecting Time] {conf._time_detecting:,.2f} ms\033[0m")
    fd_log.info(f"\033[33m🎯[Detected Object] [{conf._detect_success_count}/{conf._detect_frame_count}]\033[0m")


    # ─────────────────────────────────────────────────────────────────────────────
    # 3. draw tracking data to video
    # ─────────────────────────────────────────────────────────────────────────────   
    
    # get final output name
    file_output = fd_get_output_file_name(folder_output)     
    start_drawing = time.perf_counter()  # 시작 시간 기록

    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\033[33m── [PROCESS] 3️⃣. Drawing File \033[0m")
    fd_log.print("33m────────────────────────────────────────────────────────────────────────────────────────── ")

    match type_target:                
        case conf._type_baseball_batter_RH  | \
             conf._type_baseball_batter_LH  | \
             conf._type_baseball_pitcher    :
            fd_create_tracking_singleline(file_output, arr_ball_pos)

        case conf._type_baseball_hit | conf._type_baseball_hit_manual:
            while(1):
                fd_create_tracking_singleline(file_output, arr_ball_pos)
                # waiting until check preview
                while not conf._detect_check_preview:
                    time.sleep(0.01)
                # check fail by operator
                if conf._detect_check_success:
                    if conf._live_player and conf._live_player_widget:
                        conf._live_player_widget.live_player_restart()
                    break
                else:
                    # detection retry
                    _ , arr_ball_pos = fd_detect_ball_hit(False)
                    # reset for redraw
                    conf._detect_check_preview = False                    

            fd_save_array_file(data_file, arr_ball_pos)

        # draw multi lines
        # 2025-07-01
        case conf._type_baseball_pitcher_multi  | \
            conf._type_baseball_hit_multi:
            fd_create_tracking_multiline(file_output, arr_balls_pos)
            
        case conf._type_golfer_2ch_LND_RH | conf._type_golfer_2ch_LND_LH | \
             conf._type_golfer_3ch_LND_RH | conf._type_golfer_3ch_LND_LH | \
             conf._type_golfer_3ch_POR_RH | conf._type_golfer_3ch_POR_LH :
            fd_create_swing_analysis(file_output)

        case _:
            fd_log.warning("⚠️ not defined type {0}".format(type_target))
            return False, file_output
        

    # drawing thread
    conf._thread_draw_curr.join()
    conf._thread_draw_post.join()

    # check process time
    end_drawing = time.perf_counter()  # 종료 시간 기록
    conf._time_drawing = (end_drawing - start_drawing) * 1000
        
    # 2025-07-09
    # release detection job blocking
    '''
    if not conf._live_player:
        fd_log.info("💡[Release] lock for multi processing: non live player")
        conf._multi_job_lock = False
    '''

    # set save file name
    if conf._live_player and conf._live_player_buffer_manager:
        conf._live_player_buffer_manager.set_save_file_name(file_output)
    else:
        fd_combine_processed_files(file_output)

    # check all process time
    end_process = time.perf_counter()  # 종료 시간 기록
    fd_log.print(f"\033[36m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    fd_log.print(f"\033[36m── \033[0m🚩 Create Analysis File: \033[36m{file_output}]\033[0m")    
    fd_log.print(f"\033[36m── \033[0m1️⃣ File Merging Time: \033[36m{conf._time_merging:,.2f} ms\033[0m")
    fd_log.print(f"\033[36m── \033[0m2️⃣ Detecting Time: [{conf._detect_success_count}/{conf._detect_frame_count}] \033[36m{conf._time_detecting:,.2f} ms\033[0m")
    fd_log.print(f"\033[36m── \033[0m3️⃣ File Drawing Time: \033[36m{conf._time_drawing:,.2f} ms\033[0m")
    if(conf._live_player):
        fd_log.print(f"\033[36m── \033[0m🚀 Total Live Viewer Time: \033[36m{(conf._time_live_play - start_process) * 1000:,.2f} ms\033[0m")
    fd_log.print(f"\033[36m── \033[0m🏁 Total Processing Time: \033[36m{(end_process - start_process) * 1000:,.2f} ms\033[0m")


    # 2025-08-10
    # measure time from get event to create time
    # time_from_event = end_process - conf._time_event_pitching
    # ✅ 단위 보정
    time_from_event = (end_process - conf._time_event_pitching) * 1000.0  
    fd_log.print(f"\033[36m── \033[0m🚩 Total Time From Event to Output: \033[36m{time_from_event:,.2f} ms\033[0m")


    fd_log.print(f"\033[36m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    # additional information fio hit
    fd_log.print(f"\033[36m✔️ Moment {selected_moment_sec},{selected_moment_frm}  \033[0m")
    fd_log.print(f"\033[36m🚩hangtime:{conf._landingflat_hangtime:.1f}\033[0m")    
    fd_log.print(f"\033[36m🎯 Output {file_output} \033[0m")        
    fd_log.print(f"\033[36m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    fd_log.print("")
    fd_log.print("")

    # debug
    # play output video
    if conf._debug_mode and not conf._live_player:
        absolute_path = os.path.abspath(file_output)
        fd_log.info(f"Trying to open: {absolute_path}")
        if os.path.exists(absolute_path):
            os.startfile(absolute_path)
        else:
            fd_log.info(f"No output File: {absolute_path}")

    # 2025-07-09
    # release multi_lock
    '''
    if conf._live_player:
        while conf._live_playing_progress <= conf._live_playing_unlock:
            time.sleep(0.01)
        fd_log.info(f"💡[Release] lock for multi processing: live player {conf._live_playing_unlock}")
        conf._multi_job_lock = False
    '''

    return True, file_output

# ─────────────────────────────────────────────────────────────────────────────
# def fd_multi_channel_video(type_devision, folder_input, folder_output, camera_ip, front_camera_ip, side_camera_ip, zoom_factor, analysis_cameras, start_time, end_time, hit_time, hit_frame = -1 ):
# [owner] hongsu jung
# [date] 2025-03-10
# https://4dreplay.atlassian.net/wiki/x/ewAAgQ
# ─────────────────────────────────────────────────────────────────────────────
#
# [input]
# 1. [type_devision]        type of division : type_2_division = 0x12; type_3_division = 0x13; type_4_division = 0x14; type_9_division = 0x15
# 2. [folder_input]         file folder : './videos/input/golf/outdoor/2024_04_28_13_18_33'
# 3. [folder_output]        output folder : './videos/output/golf'
# 4. [camera_ip_class]      ip class : 101; -> camera ip class
# 5. [devision_camerases]   cameras id : 11,25,38,55,70 "from the left, clockwise"
# 6. [start_time]           start clip time : -1000 -> from "start_time"
# 7. [end_time]             end clip time : 1000 -> from "end_time"
# 8. [resolution]           resolution : resolution_4k = 0x11; resolution_fhd = 0x12; resolution_hd = 0x13
# 9. [fps]                  fps : 30 -> from "fps"
# 10.[zoom_ratio]           zoom scale : 1.0 -> from "zoom_ratio"
# 11.[select_time]          selected time : 508 [from 0 to selected timing file]
# 12.[select_frame]         selected frame : 30 [from 0 to selected timing] : default = -1    #
# ───────────────────────────────────────────────────────────────────────────── 
def fd_multi_channel_video(type_devision, folder_input, folder_output, camera_ip_class, devision_camerases, start_time, end_time, selected_time_sec, selected_moment_frm, fps, zoom_ratio, zoom_center_x, zoom_center_y):
    if conf._live_player_mode == conf._live_player_mode_nascar:
        return fd_multi_channel_video_stream(type_devision, folder_input, folder_output, camera_ip_class, devision_camerases, start_time, end_time, selected_time_sec, selected_moment_frm, fps, zoom_ratio, zoom_center_x, zoom_center_y)
    else:
        return fd_multi_channel_video_presd(type_devision, folder_input, folder_output, camera_ip_class, devision_camerases, start_time, end_time, selected_time_sec, selected_moment_frm, fps, zoom_ratio, zoom_center_x, zoom_center_y)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_multi_channel_video_presd(type_target, rtsp_url, buffer_sec): 
# [owner] hongsu jung
# [date] 2025-05-28
# ─────────────────────────────────────────────────────────────────────────────
def fd_multi_channel_video_presd(type_devision, folder_input, folder_output, camera_ip_class, devision_camerases, start_time, end_time, selected_time_sec, selected_moment_frm, fps, zoom_ratio, zoom_center_x, zoom_center_y):
    
    # 2025-03-18
    # check process time
    start_process = time.perf_counter()  # 시작 시간 기록

    # ─────────────────────────────────────────────────────────────────────────────
    # input configuration
    # ─────────────────────────────────────────────────────────────────────────────    
    conf._type_target           = type_devision    
    conf._type_division         = type_devision
    conf._selected_moment_sec   = selected_time_sec
    conf._selected_moment_frm   = selected_moment_frm    
    conf._output_fps            = fps

    # 2025-04-28
    # set zoom information
    set_zoom(zoom_ratio, zoom_center_x, zoom_center_y)

    # 2025-03-16
    # get date and time
    conf._output_datetime       = fd_get_datetime(folder_input)
    
    # set output file resolution
    conf._output_width  = conf._resolution_fhd_width
    conf._output_height = conf._resolution_fhd_height

    # clean ram disk
    fd_clean_up()

    # ─────────────────────────────────────────────────────────────────────────────####    
    # 1. Create File with Threading
    # ─────────────────────────────────────────────────────────────────────────────####    
    start_merging = time.perf_counter()  # 시작 시간 기록    
    fd_log.print("\r────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\r\033[33m── [PROCESS : Split Video ] 1️⃣ Start Merging \033[0m")
    fd_log.print("\r\033[33m── Target:{0}, Folder:{1}, Output:{2}  \033[0m".format(type_devision, folder_input, folder_output))
    fd_log.print("\r\033[33m── Camera Class:{0}, Camera IP:{1} \033[0m".format(camera_ip_class, devision_camerases))
    fd_log.print("\r\033[33m── Selected Moment Second :{0}, Selected Moment Frame:{1}  \033[0m".format(selected_time_sec, selected_moment_frm))
    fd_log.print("\r\033[33m── Start Time:{0}, End Time:{1}  \033[0m".format(start_time, end_time))
    fd_log.print("\r\033[33m── fps:{0}, zoom:{1}  \033[0m".format(fps, zoom_ratio))    
    fd_log.print("\r\033[33m── zoom X:{0}, zoom Y:{1}  \033[0m".format(zoom_center_x, zoom_center_y))
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")

    # set output resolution
    file_output = fd_multi_channel_configuration(type_devision, folder_output, camera_ip_class)
    # set file list
    result, file_analysis_list = fd_set_mem_file_multi_ch(devision_camerases, folder_input, camera_ip_class, start_time, selected_moment_frm, end_time)
    if result is False:
        return None, None
    
    # check process time
    end_merging = time.perf_counter()  # 종료 시간 기록
    conf._time_merging = (end_merging - start_merging) * 1000
    fd_log.info(f"🕒\033[33m[File Process] {(end_merging - start_merging) * 1000:,.2f} ms\033[0m")


    # ─────────────────────────────────────────────────────────────────────────────
    # 2. gathering videos
    # ─────────────────────────────────────────────────────────────────────────────    
    start_gathering = time.perf_counter()  # 시작 시간 기록
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\033[33m=── [PROCESS] Drawing File ── \033[0m")
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")
    # create multi division video
    fd_create_division(file_output)

    # check process time
    end_gathering = time.perf_counter()  # 종료 시간 기록
    conf._time_gathering = (end_gathering - start_gathering) * 1000

    # check alll process time
    end_process = time.perf_counter()  # 종료 시간 기록
    fd_log.print("\033[33m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    fd_log.print(f"\033[33m── \033[0m🚩 Create Analysis File: \033[34m{file_output}]\033[0m")
    fd_log.print(f"\033[33m── \033[0m1️⃣ Each File Merging Time: \033[32m{conf._time_merging:,.2f} ms\033[0m")
    fd_log.print(f"\033[33m── \033[0m2️⃣ All File Merging Time: \033[32m{conf._time_gathering:,.2f} ms\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Total Processing Time: \033[32m{(end_process - start_process) * 1000:,.2f} ms\033[0m")
    fd_log.print("\033[33m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")

    return True, file_output
    
# ─────────────────────────────────────────────────────────────────────────────
# def fd_multi_channel_video_stream(type_target, rtsp_url, buffer_sec): 
# [owner] hongsu jung
# [date] 2025-06-11
# ─────────────────────────────────────────────────────────────────────────────
def fd_multi_channel_video_stream(type_devision, folder_input, folder_output, camera_ip_class, devision_camerases, start_time, end_time, selected_time_sec, selected_moment_frm, fps, zoom_ratio, zoom_center_x, zoom_center_y):
    
    # 2025-03-18
    # check process time
    start_process = time.perf_counter()  # 시작 시간 기록

    # ─────────────────────────────────────────────────────────────────────────────
    # input configuration
    # ─────────────────────────────────────────────────────────────────────────────    
    conf._type_target           = type_devision    
    conf._type_division         = type_devision
    conf._selected_moment_sec   = selected_time_sec
    conf._selected_moment_frm   = selected_moment_frm    
    conf._output_fps            = fps

    # 2025-04-28
    # set zoom information
    set_zoom(zoom_ratio, zoom_center_x, zoom_center_y)

    # 2025-03-16
    # get date and time
    conf._output_datetime       = fd_get_datetime(folder_input)
    
    # set output file resolution
    conf._output_width  = conf._resolution_fhd_width
    conf._output_height = conf._resolution_fhd_height

    # clean ram disk
    fd_clean_up()


    # ─────────────────────────────────────────────────────────────────────────────####    
    # 1. Create File with Threading
    # ─────────────────────────────────────────────────────────────────────────────####    
    start_merging = time.perf_counter()  # 시작 시간 기록    
    fd_log.print("\r────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\r\033[33m── [PROCESS : Split Video ] 1️⃣ Start Merging \033[0m")
    fd_log.print("\r\033[33m── Target:{0}, Folder:{1}, Output:{2}  \033[0m".format(type_devision, folder_input, folder_output))
    fd_log.print("\r\033[33m── Camera Class:{0}, Camera IP:{1} \033[0m".format(camera_ip_class, devision_camerases))
    fd_log.print("\r\033[33m── Selected Moment Second :{0}, Selected Moment Frame:{1}  \033[0m".format(selected_time_sec, selected_moment_frm))
    fd_log.print("\r\033[33m── Start Time:{0}, End Time:{1}  \033[0m".format(start_time, end_time))
    fd_log.print("\r\033[33m── fps:{0}, zoom:{1}  \033[0m".format(fps, zoom_ratio))    
    fd_log.print("\r\033[33m── zoom X:{0}, zoom Y:{1}  \033[0m".format(zoom_center_x, zoom_center_y))
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")

    # set output resolution
    file_output = fd_multi_channel_configuration(type_devision, folder_output, camera_ip_class)
    # set file list
    result, file_analysis_list = fd_set_mem_file_multi_ch(devision_camerases, folder_input, camera_ip_class, start_time, selected_moment_frm, end_time)
    if result is False:
        return None, None
    
    # check process time
    end_merging = time.perf_counter()  # 종료 시간 기록
    conf._time_merging = (end_merging - start_merging) * 1000
    fd_log.info(f"🕒\033[33m[File Process] {(end_merging - start_merging) * 1000:,.2f} ms\033[0m")


    # ─────────────────────────────────────────────────────────────────────────────
    # 2. gathering videos
    # ─────────────────────────────────────────────────────────────────────────────    
    start_gathering = time.perf_counter()  # 시작 시간 기록
    fd_log.print("\r────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\r\033[33m── [PROCESS] Drawing File \033[0m")
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")
    # create multi division video
    fd_create_division(file_output)

    # check process time
    end_gathering = time.perf_counter()  # 종료 시간 기록
    conf._time_gathering = (end_gathering - start_gathering) * 1000

    # check alll process time5
    end_process = time.perf_counter()  # 종료 시간 기록
    fd_log.print("\033[33m33m──────────────────────────────────────────────────────────────────────────────────────────============ \033[0m")
    fd_log.print(f"\033[33m── \033[0m🚩 Create Analysis File: \033[34m{file_output}]\033[0m")
    fd_log.print(f"\033[33m── \033[0m1️⃣ Each File Merging Time: \033[32m{conf._time_merging:,.2f} ms\033[0m")
    fd_log.print(f"\033[33m── \033[0m2️⃣ All File Merging Time: \033[32m{conf._time_gathering:,.2f} ms\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Total Processing Time: \033[32m{(end_process - start_process) * 1000:,.2f} ms\033[0m")
    fd_log.print("\033[33m33m──────────────────────────────────────────────────────────────────────────────────────────============ \033[0m")

    return True, file_output
    
   
# ─────────────────────────────────────────────────────────────────────────────
# def fd_multi_calibration_video(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
# [owner] hongsu jung
# [date] 2025-09-12
# ─────────────────────────────────────────────────────────────────────────────
def fd_multi_calibration_video(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop, output_mode):
    
    # 2025-09-14
    # check process time
    start_process = time.perf_counter()  # 시작 시간 기록

    # clean previous processing files
    fd_clean_up()    

    # get unique name
    conf._unique_process_name    = uuid.uuid4()  

    # ─────────────────────────────────────────────────────────────────────────────####    
    # 1. Create File with Threading
    # ─────────────────────────────────────────────────────────────────────────────####    
    start_each_video = time.perf_counter()  # 시작 시간 기록    
    fd_log.print("\r────────────────────────────────────────────────────────────────────────────────────────── ")
    fd_log.print("\r\033[33m── [PROCESS : Multi Video ] \033[0m")
    fd_log.print("\r\033[33m── Prefix:{0}, Output:{1}  \033[0m".format(prefix, output_path))
    fd_log.print("\r\033[33m── Logo Path:{0} \033[0m".format(logo_path))
    fd_log.print("\r\033[33m── Resolution:{0}, Codec:{1}, FPS:{2}, Bitrate:{3}, GOP:{4} \033[0m".format(resolution, codec, fps, bitrate, gop))
    fd_log.print("\r\033[33m── Output Mode:{0} \033[0m".format(output_mode))
    fd_log.print("────────────────────────────────────────────────────────────────────────────────────────── ")
    
    # confirm input datas
    ret = check_verified_calibration_input_data(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop, output_mode)
    if ret is False:
        fd_log.error("❌ Wrong input data for calibration")
        return False
    
    # confirm input datas
    ret = create_video_each_camera(Cameras, AdjustData, Markers)
    if ret is False:
        fd_log.error("❌ Error in create_video_each_camera")
        return False
    
    # check all process time
    end_process = time.perf_counter()  # record end time
    elapsed_sec = end_process - start_process
    formatted_elapsed_time = fd_format_elapsed_time(elapsed_sec)

    play_total_time = conf._time_video_length  # per-camera video length (seconds)
    formatted_play_total_time = fd_format_elapsed_time(play_total_time)

    camera_count = conf._calibrate_camera_count

    # --- Derived metrics ---
    # 1) Total video time (all cameras combined)
    total_data_sec = play_total_time * camera_count
    formatted_total_data_time = fd_format_elapsed_time(total_data_sec)

    # Guard against division by zero
    if elapsed_sec > 0:
        # 2) Processing speed multiplier (vs. real-time), per camera
        #    (equivalently: total_data_sec / elapsed_sec)
        speed_ratio = total_data_sec / elapsed_sec  # e.g., 12.45×
        # 3) Time to process 1s of video (per camera)
        per_cam_time_for_1s = 1.0 / speed_ratio     # e.g., 0.080 s
        # 4) Time to process 1s of video (all cameras)
        all_cam_time_for_1s = per_cam_time_for_1s * camera_count
    else:
        speed_ratio = float("inf")
        per_cam_time_for_1s = float("inf")
        all_cam_time_for_1s = float("inf")

    # --- Output ---
    fd_log.print("\033[33m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Total Video Time (per camera): \033[32m{formatted_play_total_time}\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Camera Count: \033[32m{camera_count}\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Total Video Time (all cameras): {formatted_total_data_time}")
    fd_log.print(f"\033[33m── \033[0m🏁 Total Processing Time: \033[32m{formatted_elapsed_time}\033[0m")
    fd_log.print("\033[33m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")
    # Key metrics (English)
    fd_log.print(f"\033[33m── \033[0m🏁 Per-Camera Processing Multiplier (vs. real-time): \033[32m{speed_ratio:.2f}×\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Time to process 1s video (per camera): \033[32m{per_cam_time_for_1s:.3f}s\033[0m")
    fd_log.print(f"\033[33m── \033[0m🏁 Time to process 1s video (all cameras): \033[32m{all_cam_time_for_1s:.3f}s\033[0m")
    fd_log.print("\033[33m────────────────────────────────────────────────────────────────────────────────────────── \033[0m")

    return True
    
# ─────────────────────────────────────────────────────────────────────────────
# def check_verified_calibration_input_data(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop):
# [owner] hongsu jung
# [date] 2025-09-12
# ─────────────────────────────────────────────────────────────────────────────
def check_verified_calibration_input_data(Cameras, Markers, AdjustData, prefix, output_path, logo_path, resolution, codec, fps, bitrate, gop, output_mode):
       
    fd_log.info("check input data for calibration each files")    
    # Count cameras with Video=On
    active_cameras = [cam for cam in Cameras if cam["video"] == True]
    num_active_cameras = len(active_cameras)
    sound_camera = [cam for cam in Cameras if cam["audio"] == True]
    num_sound_camera = len(sound_camera)

    # Count markers and adjust data
    num_markers = len(Markers)
    num_adjusts = len(AdjustData)

    # check processing time    
    process_time = 0
    for marker in Markers:
        process_time += marker['end_time'] - marker['start_time']

    # set info
    conf._calibrate_camera_count = num_active_cameras
    conf._time_video_length = process_time

    play_total_time = conf._time_video_length
    formatted_play_total_time = fd_format_elapsed_time(play_total_time)

    processed_total_time = conf._time_video_length * conf._calibrate_camera_count
    formatted_processed_total_time = fd_format_elapsed_time(processed_total_time)    
    
    expected_time = conf._time_video_length * conf._calibrate_camera_count * conf._time_cali_time_per_camea
    formatted_expected_time = fd_format_elapsed_time(expected_time)

    fd_log.info(f"──────────────────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"── Active Cameras (Video=On): {num_active_cameras}")
    fd_log.info(f"── Sound Camera (Video=Off): {num_sound_camera}")
    fd_log.info(f"── Markers: {num_markers}")
    fd_log.info(f"── AdjustData: {num_adjusts}")
    fd_log.info(f"── Output Mode: {output_mode}")    
    fd_log.info(f"──────────────────────────────────────────────────────────────────────────────────────────")
    fd_log.info(f"── Video Play Time in all time group: \033[32m{formatted_play_total_time}")
    fd_log.info(f"── Total Video Play Time: \033[32m{formatted_processed_total_time}")
    fd_log.info(f"── Expected Processing Time: \033[32m{formatted_expected_time}")
    fd_log.info(f"──────────────────────────────────────────────────────────────────────────────────────────")

    conf._flip_option_cam = [None] * len(AdjustData)
    
    # Cameras Check Data
    for cam in Cameras:
        fd_log.info(f"[Camera-{cam['channel']}] IP={cam['cam_ip']}, Video={cam['video']}, Audio={cam['audio']}")    

    # check adjust data
    for idx, adj in enumerate(AdjustData):
        a = adj["Adjust"]
        margin = a["rtMargin"]        
        flip = adj["flip"]
        fd_log.info(
            f"[Live:{adj['LiveIndex']}/Replay:{adj['ReplayIndex']}] "
            f"dAdj=({a['dAdjustX']:.2f},{a['dAdjustY']:.2f}), "
            f"dAngle={a['dAngle']:.3f}, dScale={a['dScale']:.3f}, "
            f"rot=({a['dRotateX']:.2f},{a['dRotateY']:.2f}), "
            f"rtMargin=({margin['X']},{margin['Y']},{margin['Width']},{margin['Height']}), "
            f"Flip =({flip})"
        )
        # Flip
        conf._flip_option_cam[idx] = flip
        fd_log.info(f"[Flip Option: CAM-{idx}]:{conf._flip_option_cam[idx]}")

    # Markers Check Data
    for idx, marker in enumerate(Markers):
        fd_log.info(f"[Marker-{idx}] start_time={marker['start_time']}, "
                    f"start_frame={marker['start_frame']}, "
                    f"end_time={marker['end_time']}, "
                    f"end_frame={marker['end_frame']}")    
    

    # prefix
    conf._calibration_multi_prefix = prefix
    fd_log.info(f"[prefix]:{conf._calibration_multi_prefix}")    

    # output path
    conf._folder_output         = output_path
    fd_log.info(f"[output_path]:{conf._folder_output}")    
    
    # logo path
    conf._calibration_multi_logo_path = logo_path
    fd_log.info(f"[logo_path]:{conf._calibration_multi_logo_path}")    

    # resolution
    match resolution:
        case conf._txt_resolution_4K :
            conf._output_width  = conf._resolution_4k_width
            conf._output_height = conf._resolution_4k_height
        case conf._txt_resolution_FHD :
            conf._output_width  = conf._resolution_fhd_width
            conf._output_height = conf._resolution_fhd_height
        case conf._txt_resolution_HD :
            conf._output_width  = conf._resolution_hd_width
            conf._output_height = conf._resolution_hd_height
        case conf._txt_resolution_SVGA :
            conf._output_width  = conf._resolution_svga_width
            conf._output_height = conf._resolution_svga_height
        case conf._txt_resolution_SD :
            conf._output_width  = conf._resolution_sd_width
            conf._output_height = conf._resolution_sd_height
        case _ :
            fd_log.warning(f"⚠️ Not defined resolution:{resolution}, set to FHD")
            conf._output_width  = conf._resolution_fhd_width
            conf._output_height = conf._resolution_fhd_height
            
    fd_log.info(f"[resolution]:{resolution}")    
    fd_log.info(f"[resolution:width]:{conf._output_width}")    
    fd_log.info(f"[resolution:height]:{conf._output_height}")  

    # codec
    # resolution
    match codec:
        case conf._txt_codec_h264:
            conf._output_codec = conf._codec_h264
        case conf._txt_codec_h265:
            conf._output_codec = conf._codec_h265
        case conf._txt_codec_av1:
            conf._output_codec = conf._codec_av1
        case _ :        
            fd_log.warning(f"⚠️ Not defined codec:{codec}, set to mpeg4")
            conf._output_codec = conf._codec_h264

    fd_log.info(f"[codec-input]:{codec}")    
    fd_log.info(f"[codec-inside]:{conf._output_codec}")
    
    # fps
    conf._output_fps            = fps
    fd_log.info(f"[fps]:{fps}")    
    
    # bitrate
    conf._output_bitrate_k      = bitrate
    conf._output_bitrate        = bitrate*1000
    fd_log.info(f"[bitrate]:{conf._output_bitrate}")  

    # gop      
    conf._output_gop            = gop
    fd_log.info(f"[gop]:{gop}")

    match output_mode:
        case conf._txt_combined:
            conf._output_individual = False
        case conf._txt_individual:
            conf._output_individual = True
        case _ :        
            fd_log.warning(f"⚠️ Not defined output mode:{output_mode}, set to mpeg4")
            conf._output_individual = True

    
    # Verify camera and adjust data set
    if num_adjusts >= num_active_cameras :
        fd_log.info(f"✅ Cameras and AdjustData sets are consistent. Cameras Video Count={num_active_cameras}, AdjustData Count={num_adjusts}")
    else:
        fd_log.error(
            f"❌ Count mismatch: Cameras={num_active_cameras}, "
            f"Markers={num_markers}, AdjustData={num_adjusts}"
        )
        return False    
    
    if num_markers > 0 :
        fd_log.info(f"✅ exist Markers. Markets Count={num_markers}")
    else:
        fd_log.error(f"❌ Count mismatch: Markers={num_markers}, ")
        return False    
    
    return True
    


