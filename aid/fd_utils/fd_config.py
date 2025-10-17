# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# config
# - 2024/10/28
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
import sys
import os
import json
import enum
import time
import threading
from PyQt5.QtCore import QSettings
from win32comext.mapi.mapitags import PR_HOME_TELEPHONE_NUMBER_A

def get_resource_path(relative_path):
    """ PyInstaller 또는 일반 환경 모두에서 안전하게 경로 반환 """
    # ✅ 절대 경로가 들어오면 그대로 반환
    if os.path.isabs(relative_path):
        return relative_path
    try:
        # PyInstaller 실행 시 사용되는 임시 디렉토리
        base_path = sys._MEIPASS
    except Exception:
        # 개발 중인 일반 환경의 현재 디렉토리
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)

def resource(path):
    return get_resource_path(path)

_lock                       = None

# ─────────────────────────────────────────────────────────────────────────────#
# config text

_txt_baseball               = "baseball"
_txt_golf                   = "golf"
_txt_nascar                 = "nascar"
_txt_cricket                = "cricket"
_txt_etc                    = "etc"


# ─────────────────────────────────────────────────────────────────────────────#
# game type
_game_type                  = 0x00
_game_type_baseball         = 0x01
_game_type_nascar           = 0x02
_game_type_golf             = 0x03
_game_type_cricket          = 0x04
_game_type_etc              = 0xFF

_tracking_checker           = False
_extra_all_star             = False # all start
_extra_homerun_derby        = False # homerun duby
_extra_LLWS                 = False # LLWS


# ─────────────────────────────────────────────────────────────────────────────#
# common.const
_ERR_FAIL                   = 0x00
_ERR_SUCCESS                = 0x01

# ─────────────────────────────────────────────────────────────────────────────#
#websocket info
_websocket_url          = "ws://localhost"
_websocket_port         = 8001
_recv_hit_msg               = json.dumps({
    "Kind": "Hit", 
    "PlayId": "7047097f-7a77-4dff-acf1-5c0de0386c4a",
    "Time": "2025-02-24T13:01:07.265978+09:00",
    "data": 
    {
        "LandingFlat": 
        {
            "Bearing": -31.565758,
            "Distance": 95,  
            "HangTime": 10,
            "X": 31.82, 
            "Y": -19.55         
        }, 
        "Launch": 
        {
            "Speed": 108.93237, 
            "VerticalAngle": 12.416922,
            "HorizontalAngle": -25.26,
            "SpinAxis": 105.02
        }
    }
})

_recv_pitch_msg               = json.dumps({
    "Kind": "Pitch", 
    "PlayId": "7047097f-7a77-4dff-acf1-5c0de0386c4a",
    "Time": "2025-02-24T13:01:07.265978+09:00",
    "data": 
    {       
        "Release" :
        {
            "Speed": 123.5261,
            "SpinRate" : 1989.8381
        },        
        "pitchType": "Curveball"
    }
})

# ─────────────────────────────────────────────────────────────────────────────#
# DB
db_file                     = resource("aid/fd_db/baseball.db")
db_data_path                = resource("aid/fd_db/data")
_baseball_db                = None
_team_code                  = 3
_player_no                  = 45
_pitcher_team               = 6
_pitcher_no                 = 99
_batter_team                = 3
_batter_no                  = 5
# debug
_option                     = "Fastball:1,Slider:1,Curveball:1"
_interval_delay             = 0
_multi_line_cnt             = -1
_tracking_data_path         = "" 
_tracking_video_path        = ""    
_processing                 = False
_magnifier_pos              = [0, 0]

# 구종 변환 딕셔너리
PITCH_TYPE_MAP = {
    "Fastball": "패스트볼",
    "Four-seam Fastball": "포심 패스트볼",
    "Two-seam Fastball": "투심 패스트볼",
    "Cutter": "커터",
    "Slider": "슬라이더",
    "Curveball": "커브",
    "Knuckle Curve": "너클 커브",
    "ChangeUp": "체인지업",
    "Splitter": "스플리터",
    "Sinker": "싱커",
    "Knuckleball": "너클볼",
    "Sweeper": "스위퍼",
    "Forkball": "포크볼",
    "Eephus": "이퍼스"
}

# ─────────────────────────────────────────────────────────────────────────────#
# Version Management
_version            = "1.1.1.1"
_release_date       = "2025-01-01"

# ─────────────────────────────────────────────────────────────────────────────#
# Main Managment
_daemon_port        = 19737     # 0x4D21
_detect_mode        = "auto"
_encoding_mode      = "sw"
_debug_mode         = False
_detection_viewer   = False

# ─────────────────────────────────────────────────────────────────────────────#
# Live Stream - RTSP
_live_detector          = False
_rtsp_server            = False
_rtsp_client            = False
_rtsp_server_ip_addr    = "10.82.9.1"
_rtsp_port              = 8554
_rtsp_viewers           = {}

#File info
_team_info      = ""
_make_time      = ""

#cache
_cached_trajectory_finish_layer         = None
_cached_trajectory_multi_finish_layer   = None
_cached_baseball_info_layer             = None

# ─────────────────────────────────────────────────────────────────────────────#
# Trackman data
_trackman_mode              = False
_landingflat_distance       = 120
_landingflat_bearing        = -31.565758
_landingflat_hangtime       = 6
_landingflat_x              = 31.82449247757562
_landingflat_y              = -19.552350176598537
_launch_speed               = 108.5
_launch_v_angle             = 12.416922
_launch_h_angle             = 12.416922
_launch_spinaxis            = 105.01997
_release_speed              = 123.2
_release_spinrate           = 1989
_pitch_type                 = "Curveball"
_playId_hit                 = ""
_playId_pitch               = ""

# ─────────────────────────────────────────────────────────────────────────────#
#analysis
_cnt_analysis_camera        = -1
_front_camera_index         = -1
_side_camera_index          = -1
_back_camera_index          = -1
_multi_ch_analysis          = 1
_front_zoom_scale           = -1

# ─────────────────────────────────────────────────────────────────────────────
# for dashboard
# ─────────────────────────────────────────────────────────────────────────────
_dashboard                  = None

class _player_type(enum.IntEnum):
    buf         = 0x11
    pitcher     = 0x12
    batter      = 0x13
    battery     = 0x14
    golfer      = 0x21
    nascar      = 0x31
    file        = 0xa1
    detect      = 0xa2
    draw        = 0xa3

def fd_dashboard(player_type, text1 = "", text2 = "", text3 = ""):    
    if _dashboard:        
        match player_type:
            case _player_type.pitcher:
                _dashboard.update_player_info(player_type, status=text1, number=text2, handedness=text3)            
                # print(f"🟢[pitcher][status] {text1}, {text2}, {text3}")
                print(f"🟢[pitcher] status={text1}, number={text2}, handedness={text3}")
            case _player_type.batter:
                _dashboard.update_player_info(player_type, status=text1, number=text2, handedness=text3)            
                # print(f"🟢[batter][status] {text1}, {text2}, {text3}")            
                print(f"🟢[batter] status={text1}, number={text2}, handedness={text3}")            
            case _player_type.golfer:
                #_dashboard.update_golfer_status(status=text1, handedness=text2)
                print(f"🟢[golfer][status] {text1}, {text2}, {text3}")
            case _player_type.nascar: 
                #_dashboard.update_nascar_status(status=text1)
                print(f"🟢[nascar][status] {text1}, {text2}, {text3}")
            case _player_type.buf: 
                #_dashboard.update_buffer_status(rtsp_url=text1, record=text2, status=text3)
                print(f"🟢[buffer][status] {text1}, {text2}, {text3}")
            
            

# ─────────────────────────────────────────────────────────────────────────────
# Type of sports : 0x01 / 0X02
# ─────────────────────────────────────────────────────────────────────────────
_type_mask_analysis             = 0x0100
_type_mask_analysis_baseball    = 0x0110
_type_mask_analysis_golf        = 0x0120
_type_mask_analysis_cricket     = 0x0130
_type_mask_multi_ch             = 0x0200
_type_mask_calibration          = 0x0300
_type_mask_calibration_each     = 0x0310
_type_mask_calibration_rotate   = 0x0320

# ─────────────────────────────────────────────────────────────────────────────
# Type of Player 
# ─────────────────────────────────────────────────────────────────────────────
_type_detect_unknown            = 0x00
_type_detect_batter             = 0x01
_type_detect_pitcher            = 0x02
_type_detect_golfer             = 0x03
_type_detect_batsman            = 0x04
_type_detect_bowler             = 0x05
_type_detect_nascar             = 0x0f


# ─────────────────────────────────────────────────────────────────────────────
# Type of sports : Baseball 0x011
# ─────────────────────────────────────────────────────────────────────────────
_type_baseball_batter_RH        = 0x0111
_type_baseball_batter_LH        = 0x0112
_type_baseball_pitcher          = 0x0113
_type_baseball_hit              = 0x0114
_type_baseball_hit_manual       = 0x0115
# multi
_type_baseball_pitcher_multi    = 0x011a
_type_baseball_hit_multi        = 0x011b

# ─────────────────────────────────────────────────────────────────────────────
# Type of sports : Golf 0x012
# ─────────────────────────────────────────────────────────────────────────────
_type_golfer_2ch_LND_RH          = 0x0121    # 2ch 오른손 가로보기 Landscape -> LND
_type_golfer_2ch_LND_LH          = 0x0122    # 2ch 왼손 가로보기 Landscape -> LND
_type_golfer_3ch_LND_RH          = 0x0123    # 3ch 오른손 가로보기 Landscape -> LND
_type_golfer_3ch_LND_LH          = 0x0124    # 3ch 왼손 가로보기 Landscape -> LND
_type_golfer_3ch_POR_RH          = 0x0125    # 3ch 오른손 세로보기 Portrait -> POR
_type_golfer_3ch_POR_LH          = 0x0126    # 3ch 오른손 세로보기 Portrait -> POR

# ─────────────────────────────────────────────────────────────────────────────
# Type of sports : Cricket 0x013
# ─────────────────────────────────────────────────────────────────────────────
_type_cricket_batsman            = 0x0131
_type_cricket_bowler             = 0x0132

# ─────────────────────────────────────────────────────────────────────────────
# Type of division              0x02
# ─────────────────────────────────────────────────────────────────────────────
_type_1_ch                      = 0x0211
_type_2_ch_h                    = 0x0221
_type_2_ch_v                    = 0x0222
_type_3_ch_h                    = 0x0231
_type_3_ch_m                    = 0x0232
_type_4_ch                      = 0x0241
_type_9_ch                      = 0x0291
_type_16_ch                     = 0x02f1  

# ─────────────────────────────────────────────────────────────────────────────
# Type of live detection        0x03
# ─────────────────────────────────────────────────────────────────────────────

# [Live] baseball
_type_live_batter_RH            = 0x0311 
_type_live_batter_LH            = 0x0312
_type_live_pitcher              = 0x0313 
_type_live_hit                  = 0x0314
# [Live] golf
_type_live_golfer               = 0x0320
_type_live_golfer_1             = 0x0321
_type_live_golfer_2             = 0x0322
_type_live_golfer_3             = 0x0323
# [Live] cricket
_type_live_batsman              = 0x0331
_type_live_bowler               = 0x0332
# [Live] nascar
_type_live_nascar_1              = 0x0341        # front right (watching from back)
_type_live_nascar_2              = 0x0342        # back right (watching from  back)
_type_live_nascar_3              = 0x0343        # back left (watching from  back)
_type_live_nascar_4              = 0x0344        # back left (watching from  back)
_type_live_nascar_detect         = 0x0345        

# ─────────────────────────────────────────────────────────────────────────────
# Type of calibration           0x04
# ─────────────────────────────────────────────────────────────────────────────
_type_calibration_multi         = 0x0411 
_type_calibration_roration      = 0x0412



_yolo_live_conf                 = 0.5
_yolo_live_size                 = 384
_yolo_live_nascar_conf          = 0.5
_yolo_live_nascar_size          = 640

#resolution
_txt_resolution_4K  = "4K"
_txt_resolution_FHD = "FHD"
_txt_resolution_HD  = "HD"  
_txt_resolution_SVGA= "SVGA"
_txt_resolution_SD  = "SD"

_resolution_4k_width    = 3840
_resolution_4k_height   = 2160
_resolution_fhd_width   = 1920 
_resolution_fhd_height  = 1080
_resolution_hd_width    = 1280 
_resolution_hd_height   = 720
_resolution_svga_width  = 800 
_resolution_svga_height = 600
_resolution_sd_width    = 640 
_resolution_sd_height   = 480


# GPU
_gpu_session_init_cnt   = 6        # FD_NVENC_INIT_CONCURRENCY | RTX5090 -> 16, RTX4090 -> 12, RTX3090 -> 8
_gpu_session_max_cnt    = 12        # FD_NVENC_MAX_SLOTS | RTX5090 -> 24, RTX4090 -> 16, RTX3090 -> 12
_ffmpeg_path            = r"C:\4DReplay\v4_aid\libraries\ffmpeg\ffmpeg.exe"
#codec
_txt_codec_h264     = "H.264"
_txt_codec_h265     = "H.265"
_txt_codec_av1      = "AV1"

_codec_h264_cpu     = 'h264'
_codec_h264         = 'h264'
_codec_h265         = 'h265'
_codec_av1          = 'av1'

#output_mode
_output_individual  = True
_txt_individual     = 'individual'
_txt_combined       = 'combined'
_input_folder       = ''

#image direction
_image_portrait         = False

_api_key                = "springs:V4ykwrLKZaouflczdmVqIuLwxsR5pc0Xi8ib2B7Hg0MEGL7dFfXBmsIVqo+xMJW6"
_api_client             = None
_teams_info             = None

#Box Image
_pitch_box_img          = None
_bat_box1_img           = None
_bat_box2_img           = None
_hit_box1_img           = None
_hit_box2_img           = None
_hit_box3_img           = None
_kbo_logo_img           = None
_kbo_logo2_img          = None #homerun duby

_team_box_main_img      = None
_team_box_sub_img       = None

_team_box1_img1         = None
_team_box1_img2         = None
_team_box1_img3         = None
_team_box1_img4         = None
_team_box1_img5         = None
_team_box1_img6         = None
_team_box1_img7         = None
_team_box1_img8         = None
_team_box1_img9         = None
_team_box1_img10        = None
#All star
_team_box1_img11        = None
_team_box1_img12        = None
_team_box1_img13        = None
_team_box1_img14        = None

_team_box2_img1         = None
_team_box2_img2         = None
_team_box2_img3         = None
_team_box2_img4         = None
_team_box2_img5         = None
_team_box2_img6         = None
_team_box2_img7         = None
_team_box2_img8         = None
_team_box2_img9         = None
_team_box2_img10        = None
#All star
_team_box2_img11        = None
_team_box2_img12        = None
_team_box2_img13        = None
_team_box2_img14        = None

#player
_pitcher_player             = None
_batter_player              = None
_pitcher_player_unknown     = "Unknown"
_batter_player_unknown      = "Unknown"


#team code
_home_team_name     =      "LG"
_away_team_name     =      "DOOSAN"

_team_code_1        =      7 #LG
_team_code_2        =      1 #DOOSAN
_team_code_3        =      5 #KIA
_team_code_4        =      2 #LOTTE
_team_code_5        =      3 #SAMSUNG
_team_code_6        =      4 #HANWHA
_team_code_7        =      16 #KT
_team_code_8        =      15 #NC
_team_code_9        =      6 #KIWOOM
_team_code_10       =      82 #SSG
#all star
_team_code_11       =      11 #BUKBU
_team_code_12       =      12 #NAMBU
_team_code_13       =      13 #DREAM
_team_code_14       =      14 #NANUM

#team name
_team_name_1        =      "LG"
_team_name_2        =      "DOOSAN"
_team_name_3        =      "KIA"
_team_name_4        =      "LOTTE"
_team_name_5        =      "SAMSUNG"
_team_name_6        =      "HANWHA"
_team_name_7        =      "KT"
_team_name_8        =      "NC"
_team_name_9        =      "KIWOOM"
_team_name_10       =      "SSG"
#all start
_team_name_11       =      "BUKBU"
_team_name_12       =      "NAMBU"
_team_name_13       =      "DREAM"
_team_name_14       =      "NANUM"

# pose
_adjust_height              = 0.25
_adjust_width               = 1.2
_adjust_y                   = 1/5

# YOLO
_yolo_model_s               = []
_yolo_model_m               = []
_yolo_model_l               = []
_yolo_model_x               = []
# YOLO Pose
_yolo_model_pose_n          = []
_yolo_model_pose_s          = []
_yolo_model_pose_m          = []
_yolo_model_pose_l          = []
_yolo_model_pose_x          = []

# YOLO V4
_yolo_cfg                   = resource("./aid/yolo/yolov4.cfg" )          # YOLO 구성 파일
_yolo_weights               = resource("./aid/yolo/yolov4.weights")       # 미리 학습된 가중치 파일
_coco_names                 = resource("./aid/yolo/coco.names")           # COCO 데이터셋 클래스 이름 파일
_tracknet_weight            = resource("./aid/yolo/Weights.pth")          # COCO 데이터셋 클래스 이름 파일
_yolo_detect_size           = 416                               # 416 × 416 → 속도와 정확도의 균형 / 512 X 512 /  608 × 608 → 높은 정확도 (속도 느림)
_yolo_detect_high_size      = 608                               # 416 × 416 → 속도와 정확도의 균형 / 512 X 512 /  608 × 608 → 높은 정확도 (속도 느림)
_yolo_detect_highist        = 832                               # 416 → 640 or 832 로 증가
_yolo_class_id_person       = 0
_yolo_class_id_car          = 2
_yolo_class_id_baseball     = 32
_yolo_class_id_basebat      = 39

# YOLO V8
_yolo_pt_8_s        = resource("./aid/yolo/yolov8s.pt" )          # YOLO small
_yolo_pt_8_m        = resource("./aid/yolo/yolov8m.pt" )          # YOLO medium
_yolo_pt_8_l        = resource("./aid/yolo/yolov8l.pt" )          # YOLO large
_yolo_pt_8_x        = resource("./aid/yolo/yolov8x.pt" )          # YOLO ex large

# YOLO V8 pose
_yolo_pt_8_pose_n   = resource("./aid/yolo/yolov8n-pose.pt")      # YOLO pose
_yolo_pt_8_pose_s   = resource("./aid/yolo/yolov8s-pose.pt")      # YOLO pose
_yolo_pt_8_pose_m   = resource("./aid/yolo/yolov8m-pose.pt")      # YOLO pose
_yolo_pt_8_pose_l   = resource("./aid/yolo/yolov8l-pose.pt")      # YOLO pose
_yolo_pt_8_pose_x   = resource("./aid/yolo/yolov8x-pose.pt")      # YOLO pose

# YOLO V9
_yolo_pt_9_t       = resource("./aid/yolo/yolov9t.pt")            # YOLO 
_yolo_pt_9_s       = resource("./aid/yolo/yolov9s.pt")            # YOLO 
_yolo_pt_9_m       = resource("./aid/yolo/yolov9m.pt")            # YOLO 
_yolo_pt_9_c       = resource("./aid/yolo/yolov9c.pt")             # YOLO 
_yolo_pt_9_e       = resource("./aid/yolo/yolov9e.pt")            # YOLO 
# YOLO V10
_yolo_pt_10_n       = resource("./aid/yolo/yolov10n.pt")          # YOLO 
_yolo_pt_10_s       = resource("./aid/yolo/yolov10s.pt")          # YOLO 
_yolo_pt_10_m       = resource("./aid/yolo/yolov10m.pt")          # YOLO 
_yolo_pt_10_b       = resource("./aid/yolo/yolov10b.pt")          # YOLO 
_yolo_pt_10_l       = resource("./aid/yolo/yolov10l.pt")          # YOLO 
_yolo_pt_10_x       = resource("./aid/yolo/yolov10x.pt")          # YOLO 
# YOLO V11
_yolo_pt_11_n       = resource("./aid/yolo/yolo11n.pt")           # YOLO 
_yolo_pt_11_s       = resource("./aid/yolo/yolo11s.pt")           # YOLO 
_yolo_pt_11_m       = resource("./aid/yolo/yolo11m.pt")           # YOLO 
_yolo_pt_11_l       = resource("./aid/yolo/yolo11l.pt")           # YOLO 
_yolo_pt_11_x       = resource("./aid/yolo/yolo11x.pt")           # YOLO 
# YOLO V12
_yolo_pt_12_n       = resource("./aid/yolo/yolo12n.pt")           # YOLO 
_yolo_pt_12_s       = resource("./aid/yolo/yolo12s.pt")           # YOLO 
_yolo_pt_12_m       = resource("./aid/yolo/yolo12m.pt")           # YOLO 
_yolo_pt_12_l       = resource("./aid/yolo/yolo12l.pt")           # YOLO 
_yolo_pt_12_x       = resource("./aid/yolo/yolo12x.pt")           # YOLO 

#_font_path                  = "./aid/font/Anton-Regular.ttf"      # COCO 데이터셋 클래스 이름 파일
#_font_path                  = "./aid/font/KBO_Dia_Gothic_bold.ttf"      # COCO 데이터셋 클래스 이름 파일
_font_path_main             = resource("./aid/font/Paperlogy-8ExtraBold.ttf")
_font_path_sub              = resource("./aid/font/Paperlogy-7Bold.ttf")
_path_timestamp             = resource("./images/overlay_timestamp/overlay_%04d.png")
_logo_image                 = resource("./images/Panasonic.png")
_local_temp_folder          = resource("./temp")

_detect_person_confidence   = 0.8                           # minimum confidence
_bat_length_inch            = 42                            # international bat length, 42 inch
_bat_length_meter           = 1.067                         # international bat length, 42 inch

# detect configuration
_sound_detected_rate        = 0.7       #  15% error rate 60 fps -> 9 frame, 30 fps -> 5 fps, 120 fps -> 18 fps
_type_target                = -1
_type_division              = -1
_folder_input               = f""
_folder_output              = f""
_final_output_file          = f""
_camera_ip_class            = -1
_camera_ip                  = -1
_start_sec_from_moment      = -1
_end_sec_from_moment        = -1
_selected_moment_sec        = -1
_selected_moment_frm        = -1
_continious_next_sec        = -1

# input video info
_input_fps                  = 60        # default 60p
_input_frame_count          = -1        # frame count each input file
_input_width                = 1920
_input_height               = 1080
_zoom_ratio                 = 1.5
_zoom_person                = 1.5
_zoom_center_x              = 1920
_zoom_center_y              = 1080
_zoom_ratio_list            = []
_zoom_center_x_list         = []
_zoom_center_y_list         = []

# output video info
_output_fps                 = 30
_temp_fps                   = 30
_output_width               = 1920
_output_height              = 1080
_output_codec               = 'h264_nvenc'
_output_preset              = 'p4'
_output_bitrate             = '30M'
_output_bitrate_k           = 1000
_output_maxrate             = '50M'
_output_bufsize             = '100M'
_output_gop                 = 30
_output_bframes             = 1
_output_datetime            = ""
_output_shared_audio_type   = 'm4a'


# prev video
_thread_file_prev           = None
_thread_draw_prev           = None
_proc_prev_time_start       = -1
_proc_prev_time_end         = -1
_proc_prev_frm_start        = -1
_proc_prev_frm_end          = -1
# curr video
_thread_file_curr           = None
_thread_draw_curr           = None
_proc_curr_time_start       = -1
_proc_curr_time_end         = -1
_proc_curr_frm_start        = -1
_proc_curr_frm_end          = -1
# post video
_thread_file_post           = None
_thread_draw_post           = None
_proc_post_time_start       = -1
_proc_post_time_end         = -1
_proc_post_frm_start        = -1
_proc_post_frm_end          = -1
# last video
_thread_file_last           = None
_thread_draw_last           = None
_proc_last_time_start       = -1
_proc_last_time_end         = -1
_proc_last_frm_start        = -1
_proc_last_frm_end          = -1
# full video
_proc_full_time_start       = -1
_proc_full_time_end         = -1
_proc_full_frm_start        = -1
_proc_full_frm_end          = -1
_frames_front               = []
_frames_side                = []
_frames_back                = []
# calibration
_time_group_count           = 0
_calibrate_camera_count     = 0
_time_video_length          = 0
_time_cali_time_per_camea   = 0.0804
_thread_file_calibration    = [[]]
_shared_audio_filename      = []
_flip_option_cam            = []

# multi video
_longist_multi_line_index   = -1
_proc_multi_time_start      = -1
_proc_multi_time_end        = -1
_proc_multi_frm_start       = -1
_proc_multi_frm_end         = -1
_file_combine_list          = []
_output_file                = ""

_end_file_trim_frame        = -1
_select_frame_number        = -1
_detect_area_precentage     = 0.3       # 30% area detection of full screen
_detect_area_zoom           = 1
_detect_frame_count         = -1
_detect_success_count       = -1
_detect_check_preview       = False
_detect_check_success       = True


#########################################
# gui mouse event
#########################################
_mouse_click_left           = 0x01
_mouse_click_right          = 0x02
_mouse_click_middle         = 0x03

#########################################
# tracking checker
#########################################
_tracking_check_widget      = None
_tracker_backgrond_image    = resource("./images/tracker.png")



#########################################
# live viewer
#########################################

_live_player                    = False
_live_player_widget             = None
_live_player_mode               = None
_live_player_mode_baseball      = 0x10      # sequence : baseball
_live_player_mode_golf          = 0x20      # sequence : golf
_live_player_mode_nascar        = 0x30      # mosaic : nascar

# double buffer manager
_live_player_buffer_manager     = None
#_live_player_buffercombined_frames    = None

# buffer for multi channel
_live_player_multi_ch_frames     = None

_live_is_paused                 = True
_live_is_fullscreen             = False
_live_player_lock               = threading.Lock()
_live_player_window_name        = 'Live Player'
_live_backgrond_image           = resource("./images/stand_by.png")
_live_creating_output_file      = None

_live_playing_progress          = 0
_live_playing_unlock            = 50
_live_player_drawing_wait       = 30
_live_player_drawing_progress   = 0
_live_player_loop_count         = -1
_live_player_save_file          = True


# single channel live player
_live_player_frames_total_cnt   = 0
_live_player_frames_prev_cnt    = 0
_live_player_frames_curr_cnt    = 0
_live_player_frames_post_cnt    = 0
_live_player_frames_last_cnt    = 0
_combined_filled_index          = 0
_playback_frame_index           = 0
_combined_lock                  = threading.Lock()
_combined_ready_event           = threading.Event()

_live_buffer_th                 = {}
_live_mem_buffer                = {}
_live_mem_buffer_addr           = {}
_live_mem_buffer_status         = {}
_live_mem_buffer_receiver       = {}
_live_mem_buffer_status_stop    = 0x00
_live_mem_buffer_status_record  = 0x01
_live_mem_buffer_status_pause   = 0x02
_live_mem_buffer_status_resume  = 0x03

_ring_buffer_duration           = -1
_ring_buffer_duration_batter    = 5
_ring_buffer_duration_pitcher   = 5
_ring_buffer_duration_hit       = 10
_ring_buffer_duration_golfer    = 5
_ring_buffer_duration_cricket   = 5
_ring_buffer_duration_nascar    = 20

#########################################
# create file
#########################################
_create_file_time_start         = 0
_create_file_time_end           = 0

#########################################
# live detection
#########################################
_player_status                  = {}
_player_number                  = {}
_player_handedness              = {}    # right -> true / left = False

_detect_queue_size              = 15
_object_status_queue            = {}
_object_number_queue            = {}
_object_handedness_queue        = {}    # right -> true / left = False

_object_status_unknown          = 0x00
_object_status_absent           = 0x01
_object_status_present          = 0x02
_object_status_present_batter   = 0x03
_object_status_present_pitcher  = 0x04
_object_status_present_golfer   = 0x05
_object_status_present_nascar   = 0x06
_present_change_time            = None

_object_number_unknown          = -1
_object_number_skip             = -2
_object_handedness_unknown      = 0x00
_object_handedness_right        = 0x01
_object_handedness_left         = 0x02

# previous infomation
_prev_bat_status                = _object_status_unknown
_prev_bat_number                = _object_number_unknown
_prev_bat_handed                = _object_handedness_unknown
_prev_pit_status                = _object_status_unknown
_prev_pit_number                = _object_number_unknown
_prev_pit_handed                = _object_handedness_unknown
_prev_obj_status                = _object_status_unknown

_prev_obj_roi                   = []
_prev_obj_roi_valid             = True
_prev_obj_miss_count            = 0
_prev_obj_pos_threshold         = 500
_prev_obj_miss_threshold        = 30

_prev_dx                        = 0
_prev_wrist_x                   = 0
_prev_wrist_y                   = 0
_prev_frame                     = None
_last_release_ts                = 0

#tolerance
_tolerance_bat_pos_x        = 15
_tolerance_bat_pos_y        = 15
_tolerance_bat_length       = 20

#detect strike
_detect_strike_arm_angle_max    = 4     # 0 is vertical
_detect_strike_hand_pos_ratio   = 0.28 # ratio between shoulder and feet
_detect_strike_sholder_x_min    = 18    # ratio between shoulder and feet
_detect_strike_sholder_x_max    = 120   # ratio between shoulder and feet

# memory file
_mem_temp_file          = {}
_file_type_prev         = 0x41
_file_type_curr         = 0x42
_file_type_post         = 0x43
_file_type_last         = 0x44
_file_type_multi        = 0xa0
# 3CH
_file_type_front        = 0x31
_file_type_side         = 0x32
_file_type_back         = 0x33
# Calibration
_file_type_cali         = 0xa1
_file_type_cali_audio   = 0xa2
# Post stabil
_file_type_post_stabil  = 0xb1
# overlay
_file_type_overlay      = 0xf1

# frames
_frames_prev            = []
_frames_curr            = []
_frames_post            = []
_frames_last            = []
_frames_front           = []
_frames_side            = []
_frames_back            = []
_clean_file_list        = []

# live frames
_frames_live_prev       = []
_frames_live_curr       = []
_frames_live_post       = []
_frames_live_last       = []

# key points
_keypoint_pose_front    = []
_keypoint_pose_side     = []
_keypoint_pose_back     = []

# unique name
_unique_process_name    = ""

#########################################
# Optical Flow Configuration
#########################################
_op_max_selection_coutours  = 5
_op_damping_ratio_number    = 0.003
_op_distance_tolerance      = 30    
_op_area_min                = 1
_op_area_max                = 700    
_op_cos_threshold           = 0.5   # 각도 허용치치
_op_duplicate_threshold     = 2     # prev point와의 pixel 차이
_op_distance_weight         = 1.5   # distance (예측 거리 비중)
_op_deviation_weight        = 1.0   # deviation (표준 편차 비중)
_op_roi_hit_width           = 120
_op_roi_hit_height          = 120
_op_roi_reposition_bias     = 0.25    

#########################################
# Baseball team logo
#########################################
_team_logo1_path1        = resource("./images/baseball_team_logo1/LG.png")
_team_logo1_path2        = resource("./images/baseball_team_logo1/DOOSAN.png")
_team_logo1_path3        = resource("./images/baseball_team_logo1/KIA.png")
_team_logo1_path4        = resource("./images/baseball_team_logo1/LOTTE.png")
_team_logo1_path5        = resource("./images/baseball_team_logo1/SAMSUNG.png")
_team_logo1_path6        = resource("./images/baseball_team_logo1/HANWHA.png")
_team_logo1_path7        = resource("./images/baseball_team_logo1/KT.png")
_team_logo1_path8        = resource("./images/baseball_team_logo1/NC.png")
_team_logo1_path9        = resource("./images/baseball_team_logo1/KIWOOM.png")
_team_logo1_path10       = resource("./images/baseball_team_logo1/SSG.png")
#All Start
_team_logo1_path11       = resource("./images/baseball_team_logo1/BUKBU.png")
_team_logo1_path12       = resource("./images/baseball_team_logo1/NAMBU.png")
_team_logo1_path13       = resource("./images/baseball_team_logo1/DREAM.png")
_team_logo1_path14       = resource("./images/baseball_team_logo1/NANUM.png")

_team_logo2_path1        = resource("./images/baseball_team_logo1/LG.png")
_team_logo2_path2        = resource("./images/baseball_team_logo1/DOOSAN.png")
_team_logo2_path3        = resource("./images/baseball_team_logo1/KIA.png")
_team_logo2_path4        = resource("./images/baseball_team_logo1/LOTTE.png")
_team_logo2_path5        = resource("./images/baseball_team_logo1/SAMSUNG.png")
_team_logo2_path6        = resource("./images/baseball_team_logo1/HANWHA.png")
_team_logo2_path7        = resource("./images/baseball_team_logo1/KT.png")
_team_logo2_path8        = resource("./images/baseball_team_logo1/NC.png")
_team_logo2_path9        = resource("./images/baseball_team_logo1/KIWOOM.png")
_team_logo2_path10       = resource("./images/baseball_team_logo1/SSG.png")
#All Start
_team_logo2_path11       = resource("./images/baseball_team_logo1/BUKBU.png")
_team_logo2_path12       = resource("./images/baseball_team_logo1/NAMBU.png")
_team_logo2_path13       = resource("./images/baseball_team_logo1/DREAM.png")
_team_logo2_path14       = resource("./images/baseball_team_logo1/NANUM.png")


_kbo_logo_path           = resource("./images/kbo_logo.png")
_kbo_logo_path2           = resource("./images/kbo_logo2.png")


#########################################
# 1/2. batter analysis
#########################################
_batter_detect_prev_frame        = -8
_batter_detect_post_frame       = 8
_batter_ball_afterimage_frame   = 1
_swing_right_hand               = False
_batter_intersect_pos           = []
_batter_pitching_last_index     = -1
_batter_hitting_first_index     = -1
_batter_draw_upscale_factor     = 2
_detect_batter_area_zoom        = 1
_yolo_detect_batter_size        = 960 #1536 #1280
_yolo_detect_batter_confidence  = 0.25
_yolo_batch_size_batter         = 10
_yolo_detect_hit_confidence     = 0.01
_yolo_detect_hit_size           = 960
_batter_ball_max_size           = 25
_batter_ball_min_size           = 13
_tolerance_ball_route           = 2.0
_bat_hit_png                    = resource("./images/bat_hit.png")
_batter_rh_pos                  = 600
_batter_lh_pos                  = 1320

_batter_target_left             = -1
_batter_target_right            = -1
_batter_target_top              = -1
_batter_target_bottom           = -1
_batter_first_ball_height       = 300 

_batter_hit_RH_area             = [[0,0],[0,0]]
_batter_hit_LH_area             = [[0,0],[0,0]]

_batter_detect_RH_area          = [[0,0],[0,0]]
_batter_detect_RH_left          = -1
_batter_detect_RH_right         = -1
_batter_detect_RH_top           = -1
_batter_detect_RH_bottom        = -1
_batter_detect_LH_area          = [[0,0],[0,0]]
_batter_detect_LH_left          = -1
_batter_detect_LH_right         = -1
_batter_detect_LH_top           = -1
_batter_detect_LH_bottom        = -1

_bat_box1_png                   = resource("./images/bat1.png")
_bat_box2_png                   = resource("./images/bat2.png")
_batter_frame_sample_step       = 1 # from 180fps -> 180/3 = 60


#########################################
# 3.pitching analysis
#########################################
_pitcher_detect_prev_frame       = 0
_pitcher_detect_post_frame      = 65
_pitcher_ball_afterimage_frame  = 50
_after_detected                 = False
_detect_pitcher_area_zoom       = 1
_yolo_detect_pitcher_size       = 1280  
_yolo_detect_pitcher_confidence = 0.15

_yolo_batch_size_pitcher        = 20
_pitcher_ball_max_size          = 32
_pitcher_ball_min_size          = 28

_pitcher_target_left            = -1
_pitcher_target_right           = -1
_pitcher_target_top             = -1
_pitcher_target_bottom          = -1

_pitcher_detect_area            = [[0,0],[0,0]]
_pitcher_detect_left            = -1
_pitcher_detect_right           = -1
_pitcher_detect_top             = -1
_pitcher_detect_bottom          = -1

#########################################
# 4.nascar
#########################################
_nascar_detect_area            = [[0,0],[0,0]]
_nascar_detect_left            = -1
_nascar_detect_right           = -1
_nascar_detect_top             = -1
_nascar_detect_bottom          = -1

_golfer_detect_area            = [[0,0],[0,0]]
_golfer_detect_left         = -1
_golfer_detect_right        = -1
_golfer_detect_top          = -1
_golfer_detect_bottom       = -1

_cricket_detect_area            = [[0,0],[0,0]]
_crease_detect_left         = -1
_crease_detect_right        = -1
_crease_detect_top          = -1
_crease_detect_bottom       = -1

_pitcher_draw_upscale_factor    = 2
_pitcher_box_png                = resource("./images/pitch.png")
_pitcher_frame_sample_step      = 1 # from 180fps -> 180/3 = 60



#########################################
# 4.hit tracking
#########################################
_hit_auto_detect_permit_sec = 3.2
_hit_ellipse_angle          = 0
_hit_detect_prev_frame      = 0
_hit_detect_post_sec        = 2
_hit_ball_afterimage_frame  = 1500
_hit_target_right_fence     = [1860,	519.75]
_hit_target_left_fence      = [46.5,	542.25]

_detect_area                = [[0,0],[0,0]]
_detect_hit_area_zoom       = 5
_yolo_detect_size_hit       = 1280 
_detect_zoom_ratio_width    = 7
_detect_zoom_ratio_height   = 7
_hit_ball_max_size          = 25  
_hit_ball_min_size          = 15
_hit_minimum_distance       = 20
_hit_detect_init_x          = 1780
_hit_detect_init_y          = 1930
_hit_detect_init_left       = 1600 
_hit_detect_init_right      = 2000 
_hit_detect_init_top        = 1660 
_hit_detect_init_bottom     = 1800   
_hit_draw_upscale_factor    = 2
_hit_multi_upscale_factor   = 1.5
_hit_box1_png               = resource("./images/hit1.png")
_hit_box2_png               = resource("./images/hit2.png")
_hit_box3_png               = resource("./images/hit3.png")
_hit_frame_detect_step      = 1
_hit_frame_draw_skip_step   = 1

_hit_tracking_window_name   = "Tracking Checker"
_hit_tracking_retry_cnt     = 0
_hit_tracking_visual_fps    = 30
_hit_tracking_window_scale  = 1.5

_detector_type              = 1 # 1:diff, 2:visual, 3:hybrid 
_detector_type_diff         = 0x01
_detector_type_visual       = 0x02
_detector_type_hybrid       = 0x03
_detector_fps               = 30

#########################################
# 5.multi pitcher
#########################################
_pkl_list                   = {}


#########################################
# live_detect
#########################################
_detect_area_batter_rh      = [[0,0],[0,0]]
_detect_area_batter_lh      = [[0,0],[0,0]]
_detect_area_pitcher        = [[0,0],[0,0]]

#########################################
# check time
#########################################

_time_event_pitching        = 0
_time_merging               = 0
_time_detecting             = 0
_time_drawing               = 0
_time_play                  = 0
_time_live_play             = 0
_time_gathering             = 0


#########################################
# ball detection
#########################################
_multi_job_lock             = False
_detect_noise_reduce        = 2
_detect_max_contour         = 12
_detect_min_contour         = 1
_detect_max_radius          = 4
_detect_min_radius          = 1
_detect_prev_window         = 5
_detect_reduce_distance     = 1
_detect_bezier_num          = 100000
_detect_swing_bezier_num    = 200
_detect_linier_num          = 1000000
_detect_splitt_num          = 5000
_detect_sigma_value         = 100
_detect_bat_tracking_start  = 0
_detect_bat_tracking_end    = 0

# ball tracking    
_margin_max_ball_distance   = 10
_max_detect_fail_cnt        = 5
_detect_fail_cnt            = 0
_margin_pitcher_ball        = 120
_margin_distance_weight     = 30
_ball_detect_hit_gap_margin = 60
_ball_add_until_catcher     = 2
_ball_detect_pitcher_hand   = 10
_hit_ball_max_movement      = 20
_swing_color_cache          = {}
_ball_detect_pitcher_margin_min     = 20
_ball_detect_pitcher_margin_max     = 55
_ball_detect_pitcher_pred_start     = 15
_ball_detect_pitcher_pred_margin    = 70
_ball_detect_pitcher_margin_bound   = 80
_ball_detect_batter_pitching_margin = 30
_ball_detect_batter_batting_margin  = 85 # for check bound
_ball_detect_batter_base_margin     = 260  # bound
_ball_detect_bound_height           = 600
_ball_detect_hit_margin     = 50

# finger detect
# YOLOv8 Pose model, key
_yolo_detect_key_left_finger    = 8    # 왼손 검지 끝
_yolo_detect_key_right_finger   = 12  # 오른손 검지 끝


# confirm
_player_name_old    = ""
_player_name_new    = ""


#color

_color_kbo_blue         = (200,50,0)
_color_kbo_blue_2       = (100,25,0)
#_color_kbo_blue         = (87,178,137)
_color_kbo_lightblue    = (239,174,0)
_color_kbo_red          = (36,28,237)
_color_kbo_silver       = (192,190,188)
_color_kbo_gold         = (119,161,179)
_color_kbo_mint         = (189,165,0)
_color_kbo_orange       = (0,78,252)
_color_kbo_white        = (255,255,255)

_color_joint            = (0,255,255)
_color_line             = (0,255,0)
_color_swing            = (255,255,0)

_color_main_line        = (0,0,255)
_color_ext_line         = (0,0,255)
_color_graph            = (0,200,0)
_color_digit            = (0,0,255)

_color_light_blue       = (255, 255, 0)
_color_dark_blue        = (139, 0, 0)

_color_title            = (230,230,230)
_color_title_box        = (50,50,50)
_color_baseline         = (255,255,255)
_color_base_indicator   = (113,184,251) #(80,80,252)    #red

_color_line_shoulder    = (176,101,253) # (61,186,136)  #green
_color_line_hip         = (202,93,145) # (61,186,136)  #green    

_color_swing_angle      = (113,184,251) # (255,183,115) #orange
_color_swing_angle_x    = (34,147,255) # (255,183,115) #orange
_color_swing_angle_y    = (80,80,252) # (255,183,115) #orange
_color_swing_angle_z    = (255,125,188) # (255,183,115) #orange
_color_waist_angle      = (61,186,136) # (61,186,136)  #green
_color_elbow_l_angle    = (176,101,253) # (61,186,136)  #green
_color_elbow_r_angle    = (202,93,145) # (61,186,136)  #green
_color_body_angle       = (0,255,0) # (61,186,136)  #green
_color_swing_speed      = (219,147,76) #(187,174,38)  #magenta
_color_hand_tracking    = (0,255,255) # (219,147,76)  #orange
_color_waist_vertical   = (0,0,255) #(80,80,252)    #red
_color_max_value        = (255,0,0) #(80,80,252)    #red
_color_min_value        = (0,0,255) #(80,80,252)    #red
_color_max_value_text   = (255,0,0) #(80,80,252)    #red
_color_min_value_text   = (0,0,255) #(80,80,252)    #red

'''
sample color

(212,195,109)   #(109,195,212)
(223,158,09)    #(0,158,223)
(131,74,09)     #(0,74,131)
(1091,136,115)  #(115,136,191)
(144,100,76)    #(76,100,144)

(223,212,75) (187,174,38)           magenta
(113,184,251) (34,147,255)          orange 
(124,122,255) (80,80,252)           red    
(209,159,255) (176,101,253)         pink   
(255,125,188) (202,93,145)          pupple 
(255,183,115) (219,147,76)          blue   
(89,209,162 (61,186,136)            green   
'''

#analysis
_speed_adjust           = 12
_stabile_alpha_1d       = 0.5  # 평활화 계수 (0과 1 사이)
_stabile_alpha_2d       = 0.5  # 지수 이동 평균(Exponential Moving Average, EMA): 최근 좌표에 더 높은 가중치를 부여하여 변화를 더 빠르게 반영하면서도 안정화된 값을 제공합니다. alpha는 가중치를 조절하는 계수로, 값이 작을수록 더 부드럽습니다.

#drawing
_length_ext             = 0.5
_line_width             = 3
_points_size            = 5
_dotted_line_gap        = 10
_draw_baseline_thick    = 2
_draw_middle_line_thick = 1
_frame_margin           = 50
_pause_frame_cnt        = 15
_draw_timestamp_size    = 30
_scale_fix              = False

_text_size_title        = 0.5
_text_size_value        = 0.7
_text_size_2_value      = 0.6
_text_size_graph_max    = 0.7
_text_size_graph_text   = 0.4

#drawing box
_homerun_box_pos        = (0.02, 0.35, 0.15, 0.30)



_pitch_box_x            = 110
_pitch_box_y            = 410
_bat_box1_left_x        = 110
_bat_box1_left_y        = 800
_bat_box2_left_x        = 110
_bat_box2_left_y        = 930
_bat_box1_right_x        = 835
_bat_box1_right_y        = 800
_bat_box2_right_x        = 835
_bat_box2_right_y        = 930
_hit_box1_left_x         = 130
_hit_box1_left_y         = 350
_hit_box2_left_x         = 130
_hit_box2_left_y         = 715
_hit_box1_right_x        = 1455
_hit_box1_right_y        = 350
_hit_box2_right_x        = 1455
_hit_box2_right_y        = 715
_hit_box3_right_x        = 1455
_hit_box3_right_y        = 805
_fade_in_duration       = 5
_fade_out_duration      = 60
_draw_max_alpha         = 1

_pitch_name_box_x             = 139
_pitch_name_box_y             = 361
_bat_name_box1_left_x         = 133
_bat_name_box1_left_y         = 757
_bat_name_box2_left_x         = 133
_bat_name_box2_left_y         = 887
_bat_name_box1_right_x        = 858
_bat_name_box1_right_y        = 757
_bat_name_box2_right_x        = 858
_bat_name_box2_right_y        = 887
_hit_name_box1_left_x         = 158
_hit_name_box1_left_y         = 300
_hit_name_box2_left_x         = 158
_hit_name_box2_left_y         = 665
_hit_name_box1_right_x        = 1483
_hit_name_box1_right_y        = 300
_hit_name_box2_right_x        = 1483
_hit_name_box2_right_y        = 665
_hit_name_box3_right_x        = 1483
_hit_name_box3_right_y        = 755

_kbo_logo_left_x            = 80
_kbo_logo_left_y            = 890
_kbo_logo_right_x           = 1550
_kbo_logo_right_y           = 890

_kbo_logo2_left_x           = 80
_kbo_logo2_left_y           = 830
_kbo_logo2_right_x           = 1480
_kbo_logo2_right_y           = 830


#graph type
_graph_front_up         = 0
_graph_front_down       = 1
_graph_side_up          = 2
_graph_side_down        = 3


#graph
_graph_title_front_up   = 'Swing Angle'
_graph_title_front_down = 'Swing Speed'
_graph_title_side_up    = 'Body Angle'
_graph_title_side_down  = 'Waist Angle'
_graph_draw_text_gap    = 15


#analysis information type
_type_swing_x_angle     = 0
_type_swing_y_angle     = 1
_type_swing_z_angle     = 2         
_type_waist_y_angle     = 3 
_type_elbow_l_angle     = 4
_type_elbow_r_angle     = 5
_type_swing_speed       = 6            
_type_swing_path        = 7    
_type_precessed_info    = 8

#information array 
_array_swing_x_angle    = []
_array_swing_y_angle    = []
_array_swing_z_angle    = []
_array_waist_y_angle    = []
_array_elbow_l_angle    = []
_array_elbow_r_angle    = []
_array_swing_speed      = []
_array_swing_path       = []
_array_precessed_info   = []

#line type
_line_shl_hdl           = 0     # shoulder center to hand center
_line_shc_hpc           = 1     # shoulder center to hip center
_line_shl_shr           = 2
_line_hpl_hpr           = 3
_line_foot              = 4
_line_indicator_foot    = 11     # foot to vertical
_line_indicator_chest   = 12     
_line_indicator_knee    = 13    
_box_body               = 22

#tracking type
_tracking_hand          = 0

#text
_text_align_left        = 0
_text_align_center      = 1
_text_align_right       = 2

#precessed infomation
_avr_foot_left_x        = 0
_avr_foot_right_x       = 0
_avr_foot_y             = 0
_avr_head_y             = 0

#position data
#head
_pt_hd_2d_c     = []        # head center
_pt_hd_2d_t     = []        # head top
_pt_hd_2d_s     = []        # head size
# shoulder
_pt_sh_2d_r     = []        # shoulder right
_pt_sh_2d_l     = []        # shoulder left
_pt_sh_2d_c     = []        # shoulder center
_pt_sh_3d_r     = []        # shoulder 3d right
_pt_sh_3d_l     = []        # shoulder 3d left
_pt_sh_3d_c     = []        # shoulder 3d center
# Elbow 
_pt_el_2d_r     = []
_pt_el_2d_l     = []
_pt_el_2d_c     = []
_pt_el_3d_r     = []
_pt_el_3d_l     = []
_pt_el_3d_c     = []
# Wrist 
_pt_wr_2d_r     = []
_pt_wr_2d_l     = []
_pt_wr_2d_c     = []
_pt_wr_3d_r     = []
_pt_wr_3d_l     = []
_pt_wr_3d_c     = []
# Hip 
_pt_hp_2d_r     = []
_pt_hp_2d_l     = []
_pt_hp_2d_c     = []
_pt_hp_3d_r     = []
_pt_hp_3d_l     = []
_pt_hp_3d_c     = []
# Knee 
_pt_kn_2d_r     = []
_pt_kn_2d_l     = []
_pt_kn_2d_c     = []
_pt_kn_3d_r     = []
_pt_kn_3d_l     = []
_pt_kn_3d_c     = []
# Ankle 
_pt_an_2d_r     = []
_pt_an_2d_l     = []
_pt_an_2d_c     = []
_pt_an_3d_r     = []
_pt_an_3d_l     = []
_pt_an_3d_c     = []
# Heel P
_pt_hl_2d_r     = []
_pt_hl_2d_l     = []
_pt_hl_2d_c     = []
_pt_hl_3d_r     = []
_pt_hl_3d_l     = []
_pt_hl_3d_c     = []
# Foot
_pt_ft_2d_r     = []
_pt_ft_2d_l     = []
_pt_ft_2d_c     = []
_pt_ft_3d_r     = []
_pt_ft_3d_l     = []
_pt_ft_3d_c     = []
# Thumb (hand)
_pt_th_2d_r     = []
_pt_th_2d_l     = []
_pt_th_2d_c     = []
_pt_th_3d_r     = []
_pt_th_3d_l     = []
_pt_th_3d_c     = []
# Index (hand)
_pt_in_2d_r     = []
_pt_in_2d_l     = []
_pt_in_2d_c     = []
_pt_in_3d_r     = []
_pt_in_3d_l     = []
_pt_in_3d_c     = []

# width
_length_chest_line  = -1

#striket zone
_strike_top     = 0.5635
_strike_bottom  = 0.2764

#cricket
_ratio_detect_wicket    = 10        # 15% of full screen 
_cricket_wicket_length  = 72.39      # 71.12 + 1.27
_cricket_wicket_height  = 0


###################################################
# multi-ch
###################################################
_calibration_multi_prefix       = ""
_calibration_multi_logo_path    = None
_calibration_multi_audio_file   = None

_result_code = 0