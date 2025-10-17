# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_live_detect_status.py
# - 2025/06/02
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
import os
import av
import math
import io
import copy
import time
import ffmpeg
import tempfile
import pickle
import uuid
import threading
import numpy as np
import pandas as pd  
import fd_utils.fd_config   as conf
import matplotlib.pyplot as plt

from ultralytics                import YOLO
from collections                import deque
from collections                import Counter

from fd_utils.fd_logging        import fd_log
from fd_detection.fd_pose       import detect_2d_keypoints_yolo

from fd_detection.fd_detect     import frame_resize
from fd_detection.fd_detect     import process_result_batch
from fd_detection.fd_detect     import get_jersey_number

from collections                import deque
from types                      import SimpleNamespace
from typing                     import List, Tuple
from filterpy.kalman            import KalmanFilter
from scipy.interpolate          import CubicSpline
from scipy.optimize             import fsolve
from scipy.ndimage              import gaussian_filter1d
from scipy.signal               import medfilt
from scipy.signal               import savgol_filter
from scipy.special              import comb
from scipy.interpolate          import UnivariateSpline
from scipy.spatial.distance     import euclidean
from sklearn.linear_model       import LinearRegression
from sklearn.preprocessing      import PolynomialFeatures
from mpl_toolkits.mplot3d       import Axes3D

# 단일 스위치: 기본 ON
_detection_event = threading.Event()
_detection_event.set()

def is_detection_enabled() -> bool:
    return _detection_event.is_set()


yolo_landmark_names = [
    "nose",         # 0
    "left_eye",     # 1
    "right_eye",    # 2
    "left_ear",     # 3
    "right_ear",    # 4
    "left_shoulder",# 5
    "right_shoulder",# 6
    "left_elbow",   # 7
    "right_elbow",  # 8
    "left_wrist",   # 9
    "right_wrist",  # 10
    "left_hip",     # 11
    "right_hip",    # 12
    "left_knee",    # 13
    "right_knee",   # 14
    "left_ankle",   # 15
    "right_ankle"   # 16
]

def to_int_xy(p):
    return tuple(map(int, p[:2])) if p else None

# ─────────────────────────────────────────────────────────────────────────────
# def check_batter_frame(latest_frame, timestamp, is_right_hand):
# [owner] hongsu jung
# [date] 2025-06-02
# 1️⃣batter_present = on/off in box (True:on, False:off) | the batter is present in the batter’s box.
# 2️⃣batter_hand = RH/LH (True:RH, False:LH)
# 3️⃣batter_number = int
# 4️⃣batter_motion = int (motion pose step)
# ─────────────────────────────────────────────────────────────────────────────
def check_batter_frame(type_target, frame, timestamp):
    # 2025-08-11
    if not is_detection_enabled():
        return
    
    # ─────────────────────────────────────────────────────────────────────────────
    #0️⃣ set configuration
    # ─────────────────────────────────────────────────────────────────────────────
    detected_status     = conf._object_status_unknown
    detected_handedness = conf._object_handedness_unknown
    detected_number     = conf._object_number_unknown
    detected_is_back    = False

    right_hand_angle = False
    if(type_target == conf._type_live_batter_RH): right_hand_angle = True
    
    if(right_hand_angle):   roi = conf._batter_detect_RH_area
    else:                   roi = conf._batter_detect_LH_area    
    resized_frame = frame_resize(frame, roi, 1)
    [top_x, top_y], [bottom_x,bottom_y] = roi

    # YOLOv8
    # 2025-03-28 
    yolo_model= conf._yolo_model_s
    yolo_pose = conf._yolo_model_pose_s      
    
    # ─────────────────────────────────────────────────────────────────────────────
    #1️⃣ check batter_present
    # - 타자가 타석에 있는지 아닌지 확인
    # ─────────────────────────────────────────────────────────────────────────────
    try:
        if resized_frame is None or resized_frame.shape[0] == 0 or resized_frame.shape[1] == 0:
            fd_log.error(f"❌[0x{type_target:x}] Invalid frame skipped")
            return
                
        # 🚀 predict 부분만 별도로 try-except
        try:
            results = yolo_model.predict(
                resized_frame,
                classes=[conf._yolo_class_id_person, conf._yolo_class_id_basebat],
                conf=conf._yolo_live_conf,
                imgsz=conf._yolo_live_size,
                verbose=False,
                device='cuda'    # ★ 여기서 지정
            )[0]        
        except AttributeError as e:
            fd_log.warning(f"⚠️ [0x{type_target:x}] YOLO predict AttributeError skipped: {e}")
            return  # 에러 발생 시 그냥 skip 하고 빠져나감
        
        detected = len(results.boxes)
        b_person = False
        b_bat = False
        if(detected > 0):
            for box in results.boxes:
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])  # ROI 기준 좌표
                # person
                if(cls_id == conf._yolo_class_id_person):
                    # 🎯 seaching just person
                    height  = abs(y2-y1)
                    width   = abs(x2-x1)
                    if(cls_id == conf._yolo_class_id_person):
                        if(width > 50 and height > 200):
                            b_person = True
                            roi_person = [x1,y1],[x2,y2]
                            if conf._detection_viewer:
                                cv2.rectangle(resized_frame, (x1, y1), (x2,y2), (0, 255, 0), 2)                                
                                break
                        else:
                            if conf._detection_viewer:
                                cv2.rectangle(resized_frame, (x1, y1), (x2,y2), (0, 0, 255), 2)
                # bat
                elif(cls_id == conf._yolo_class_id_basebat):
                    b_bat = True
                    if conf._detection_viewer:
                        cv2.rectangle(frame, (top_x+x1, top_y+y1), (top_x+x2 , top_y+y2), (255, 0, 255), 2)  
        else:
            if conf._detection_viewer:
                cv2.rectangle(frame, (top_x, top_y), (bottom_x, bottom_y),  (0, 0, 255), 2)  
            
        # set status    
        if b_person:
            detected_status = conf._object_status_present_batter
            person_frame = frame_resize(resized_frame, roi_person, 1)    
        else:
            detected_status = conf._object_status_absent
            return
                
        # ─────────────────────────────────────────────────────────────────────────────
        #2️⃣get pose 
        # get shoulder and hip posotion
        # check right hand, left hand
        # get roi for number
        # ─────────────────────────────────────────────────────────────────────────────
        results = yolo_pose(person_frame, stream=False, verbose=False, device='cuda')
        batch_keypoints = process_result_batch(results)
        
        # if (batch_keypoints[0] == None):            
        #     return        
        if batch_keypoints is None or len(batch_keypoints) == 0:
            # print("⚠️ No keypoints detected.")
            return

        if batch_keypoints[0] is None:
            print("⚠️ First keypoint is None.")
            return

        
        shoulder_left   = to_int_xy(batch_keypoints[0].get("left_shoulder"))
        shoulder_right  = to_int_xy(batch_keypoints[0].get("right_shoulder"))

        # check right hand / left hand
        if(right_hand_angle):
            if(shoulder_left[0] > shoulder_right[0]):   
                detected_handedness = conf._object_handedness_right
                detected_is_back    = False
            else:                                       
                detected_handedness = conf._object_handedness_left          
                detected_is_back    = True
        else:            
            if(shoulder_left[0] < shoulder_right[0]):   
                detected_handedness = conf._object_handedness_right        
                detected_is_back    = True
            else:                                       
                detected_handedness = conf._object_handedness_left          
                detected_is_back    = False

        if detected_is_back:
            hip_right  = to_int_xy(batch_keypoints[0].get("right_hip"))
            if(shoulder_left[0] >= hip_right[0]) or (shoulder_left[1] >= hip_right[1]):
                return
            roi_back_number = [shoulder_left,hip_right]

        # ─────────────────────────────────────────────────────────────────────────────
        #3️⃣check batter_number
        # - 타자가 타석에 있다면, 오른속/왼속에 따라 타자의 등번호 확인 (반대쪽 카메라에서)
        # ─────────────────────────────────────────────────────────────────────────────
        if detected_is_back:
            player_number = get_jersey_number(resized_frame, person_frame, roi_back_number, roi_person[0][0], roi_person[0][1])
            if player_number is not None:
                detected_number = player_number               
                
        # ─────────────────────────────────────────────────────────────────────────────
        #4️⃣check motion/pose
        # - 타자가 타석에 있다면, 현재 동작에 대해
        # - stand, swing(motion step)
        # ─────────────────────────────────────────────────────────────────────────────

    finally:        
        # update info to dashboard
        object_status       = set_object_status(type_target,detected_status)
        player_handedness   = set_player_handedness(type_target,detected_handedness)
        if detected_is_back:
            player_number   = set_player_number(type_target,detected_number)
        else:
            player_number   = conf._object_number_skip    
        # check status
        if (object_status == conf._object_status_absent):
            # player_handedness   = set_player_handedness(type_target,conf._object_handedness_unknown,True)
            # player_number       = set_player_number(type_target,conf._object_number_unknown,True)
            # 2025-08-11
            # 이전 상태 유지 - 상태를 초기화하지 않음 
            object_status = conf._prev_bat_status
            player_number = conf._prev_bat_number
            player_handedness = conf._prev_bat_handed

        # pass unknown
        if((object_status == conf._object_status_present_batter) and (player_number == conf._object_number_unknown or player_number == conf._object_number_skip or player_number == None)):
            pass
        else:
            # 🚀 변경 여부 확인 후 전송
            is_changed = ( object_status   != conf._prev_bat_status or player_number   != conf._prev_bat_number or player_handedness != conf._prev_bat_handed)            
            if is_changed:
                # 변경 되었을 때만 전송
                conf.fd_dashboard(conf._player_type.batter, object_status, player_number, player_handedness)                
                # 이전 상태 업데이트
                conf._prev_bat_status = object_status
                conf._prev_bat_number = player_number
                conf._prev_bat_handed = player_handedness            

        # 2025-08-11
        # 🔒 ADD: 확정된 등번호를 전역에 커밋 (타자)
        if (object_status == conf._object_status_present_batter and
            player_number not in (conf._object_number_unknown, conf._object_number_skip, None)):
            # try:
            #     lock = getattr(conf, "_lock", None)
            #     if lock:
            #         with lock:
            #             conf._live_batter_no = int(player_number)
            #     else:
            #         conf._live_batter_no = int(player_number)
            # except Exception:
            #     conf._live_batter_no = int(player_number)
            # fd_log.info(f"live batter no : {player_number}")

            # 2025-08-21
            # ⬇️ Auto일 때만 전역 번호를 덮어쓴다
            if getattr(conf, "_live_batter_auto", True):
                try:
                    lock = getattr(conf, "_lock", None)
                    if lock:
                        with lock:
                            conf._live_batter_no = int(player_number)
                    else:
                        conf._live_batter_no = int(player_number)
                except Exception:
                    conf._live_batter_no = int(player_number)


        # for debug    
        if conf._detection_viewer:
            if(right_hand_angle == True):
                cv2.imshow("[Batter:RH]", resized_frame)
            else:
                cv2.imshow("[Batter:LH]", resized_frame)
            cv2.waitKey(1)

# ─────────────────────────────────────────────────────────────────────────────
# def check_pitcher_frame():
# [owner] hongsu jung
# [date] 2025-06-02
# 1️⃣pitcher_present = on/off the mound (True:on, False:off) | the pitcher is present on the mound
# 2️⃣pitcher_hand = RH/LH (True:RH, False:LH)
# 3️⃣pitcher_number = int
# 4️⃣pitcher_motion = int (motion pose step)
# ─────────────────────────────────────────────────────────────────────────────
def check_pitcher_frame(frame, timestamp):
    # 2025-08-11
    if not is_detection_enabled():
        return
    
    try:
        # ─────────────────────────────────────────────────────────────────────────────
        # 0️⃣ Configuration
        # ─────────────────────────────────────────────────────────────────────────────
        type_target = conf._type_live_pitcher
        detected_status = conf._object_status_unknown
        detected_number = conf._object_number_unknown
        detected_is_back = False

        roi = conf._pitcher_detect_area
        [top_x, top_y], [bottom_x, bottom_y] = roi
        resized_frame = frame_resize(frame, roi, 1)

        yolo_model = conf._yolo_model_s
        yolo_pose = conf._yolo_model_pose_m

        # ─────────────────────────────────────────────────────────────────────────────
        # 1️⃣ Detect presence of pitcher
        # ─────────────────────────────────────────────────────────────────────────────
        if resized_frame is None or resized_frame.shape[0] == 0 or resized_frame.shape[1] == 0:
            fd_log.error(f"❌[0x{type_target:x}] Invalid frame skipped")
            return

        detection_result = yolo_model.predict(
            resized_frame,
            classes=[conf._yolo_class_id_person],
            conf=conf._yolo_live_conf,
            imgsz=conf._yolo_live_size,
            verbose=False,
            device='cuda'
        )[0]

        b_person = False
        roi_person = None
        largest_person_box = None
        
        # 1️⃣ 가장 큰 사람 box 탐색
        if detection_result.boxes is not None and len(detection_result.boxes) > 0:
            max_area = 0
            for box in detection_result.boxes:
                cls_id = int(box.cls[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                width = abs(x2 - x1)
                height = abs(y2 - y1)

                if cls_id == conf._yolo_class_id_person:
                    area = width * height
                    if width > 80 and height > 350 and area > max_area:
                        max_area = area
                        largest_person_box = (x1, y1, x2, y2)

                elif cls_id == conf._yolo_class_id_baseball:
                    # 🔵 공 위치 원 그리기 (하늘색)
                    if conf._detection_viewer:
                        ball_cx = (x1 + x2) // 2
                        ball_cy = (y1 + y2) // 2
                        cv2.circle(resized_frame, (ball_cx, ball_cy), 15, (255, 0, 255), 3)

        # 2️⃣ Pose Estimation 대상 선택
        if largest_person_box:
            x1, y1, x2, y2 = largest_person_box
            b_person = True
            roi_person = [(x1, y1), (x2, y2)]
            if conf._detection_viewer:
                cv2.rectangle(resized_frame, (x1, y1), (x2, y2), (0, 255, 0), 5)

            detected_status = conf._object_status_present_pitcher
            person_frame = frame_resize(resized_frame, roi_person, 1)
            aspect_ratio = (y2 - y1) / (x2 - x1 + 1e-5)

            # pose detect
            # for back number
            # for hand position
            pose_results = yolo_pose(person_frame, stream=False, verbose=False, device='cuda')
            if not pose_results or len(pose_results) == 0:
                return
            batch_keypoints = process_result_batch(pose_results)
            if not batch_keypoints or batch_keypoints[0] is None:
                return

            kp = batch_keypoints[0]
            shoulder_left = to_int_xy(kp.get("left_shoulder"))
            shoulder_right = to_int_xy(kp.get("right_shoulder"))
            # back number showing or not
            detected_is_back = shoulder_left[0] < shoulder_right[0]
            if detected_is_back:
                hip_right = to_int_xy(kp.get("right_hip"))
                if shoulder_left[0] >= hip_right[0] or shoulder_left[1] >= hip_right[1]:
                    return
                roi_back_number = [shoulder_left, hip_right]

            # handedness
            if conf._prev_pit_handed == conf._object_handedness_left: is_right_handed = False
            else: is_right_handed = True

            left_hand = to_int_xy(kp.get("left_wrist"))
            right_hand = to_int_xy(kp.get("right_wrist"))    
            left_hip = to_int_xy(kp.get("left_hip"))
            right_hip = to_int_xy(kp.get("right_hip"))                       
            left_ankle = to_int_xy(kp.get("left_ankle"))
            right_ankle = to_int_xy(kp.get("right_ankle"))                        

            throwing_hand = right_hand if is_right_handed else left_hand
            
            # 1️⃣ throwing_hand 좌표를 resized_frame 기준으로 변환
            x_offset, y_offset = roi_person[0]
            wrist_global = (
                throwing_hand[0] + x_offset,
                throwing_hand[1] + y_offset
            )
            left_ankle_global = (
                left_ankle[0] + x_offset,
                left_ankle[1] + y_offset
            )
            right_ankle_global = (
                right_ankle[0] + x_offset,
                right_ankle[1] + y_offset
            )
            left_hip_global = (
                left_hip[0] + x_offset,
                left_hip[1] + y_offset
            )
            right_hip_global = (
                right_hip[0] + x_offset,
                right_hip[1] + y_offset
            )

            # 🟢 drawing hand positon
            if conf._detection_viewer:
                cv2.circle(resized_frame, wrist_global, 10, (0, 255, 255), 3)
                cv2.circle(resized_frame, left_ankle_global, 10, (255, 0, 255), 3)
                cv2.circle(resized_frame, right_ankle_global, 10, (255, 0, 255), 3)
                cv2.circle(resized_frame, left_hip_global, 10, (0, 0, 255), 3)
                cv2.circle(resized_frame, right_hip_global, 10, (0, 0, 255), 3)

            # ─────────────────────────────────────────────────────────────────────────────
            # 3️⃣ 릴리스 판단 (aspect ratio + 손목 이동량)
            # ─────────────────────────────────────────────────────────────────────────────
            now_ts = timestamp
            debounce_sec = 10

            if not hasattr(conf, "_last_release_ts"):
                conf._last_release_ts = 0
            if not hasattr(conf, "_prev_wrist_x"):
                conf._prev_wrist_x = wrist_global[0]
                conf._prev_wrist_y = wrist_global[1]
                conf._prev_dx = 0
                conf._prev_frame = None
                return  # 첫 프레임은 비교 불가

            dx = wrist_global[0] - conf._prev_wrist_x
            dy = conf._prev_wrist_y - wrist_global[1]

            # 릴리스: 전 프레임에서 dx가 컸고, 이번 dx가 감소하면
            release_trigger = False
            if is_right_handed:
                if conf._prev_dx > 20 and dx < 5:
                    release_trigger = True
            else:
                if conf._prev_dx < -20 and dx > -5:
                    release_trigger = True

            if release_trigger and (now_ts - conf._last_release_ts > debounce_sec):
                aspect_ratio_thresh = 1.65  # 몸을 낮췄다고 판단되는 종횡비
                if aspect_ratio > aspect_ratio_thresh:
                    release_trigger = False
                    # fd_log.info(f"⛔ Release ignored due to upright posture (AR={aspect_ratio:.2f})")
                '''
                # ▶️ 중심 좌표 기준으로 손목이 충분히 이동했는가?
                person_center_x = (x1 + x2) // 2
                wrist_x = throwing_hand[0]
                hand_offset = wrist_x - person_center_x if is_right_handed else person_center_x - wrist_x
                min_hand_offset = 20  # 최소 이동 거리(px), 상황에 따라 조정
                if release_trigger and hand_offset < min_hand_offset:
                    release_trigger = False
                    fd_log.info(f"⛔ Release ignored: wrist not far enough from body center (offset={hand_offset}px)")
                '''    
                if release_trigger:   
                    conf._last_release_ts = now_ts
                    # fd_log.info(f"✅ RELEASE DETECTED at {timestamp:.3f} (AR={aspect_ratio:.2f}, dx={conf._prev_dx}->{dx}, dy={dy})")

                    # 이전 프레임 저장 (릴리스는 전 프레임 기준)
                    if conf._prev_frame is not None:
                        save_dir = getattr(conf, "_release_debug_dir", "release_debug")
                        os.makedirs(save_dir, exist_ok=True)
                        save_path = os.path.join(save_dir, f"release_{timestamp:.3f}.jpg")
                        cv2.imwrite(save_path, conf._prev_frame)
                        # fd_log.info(f"💾 Saved previous frame as release: {save_path}")

        # 상태 저장
        conf._prev_dx = dx
        conf._prev_wrist_x = wrist_global[0]
        conf._prev_wrist_y = wrist_global[1]
        conf._prev_frame = resized_frame.copy()
        # 4️⃣ 사람 미검출 시 fallback 처리
        if not b_person or roi_person is None:
            detected_status = conf._object_status_absent
            return

        # ─────────────────────────────────────────────────────────────────────────────
        # 3️⃣ Jersey number detection
        # ─────────────────────────────────────────────────────────────────────────────
        if detected_is_back:
            player_number = get_jersey_number(
                resized_frame, person_frame, roi_back_number,
                roi_person[0][0], roi_person[0][1]
            )
            if player_number is not None:
                detected_number = player_number

    except AttributeError as e:
        fd_log.warning(f"🔥 AttributeError: {str(e)}")
        fd_log.warning(f"⚠️ Check if YOLO model is correctly loaded and API version matches")
    except Exception as e:
        #fd_log.error(f"🔥 Unexpected error: {str(e)}")
        pass
    finally:
        # ─────────────────────────────────────────────────────────────────────────────
        # 🔁 Dashboard update
        # ─────────────────────────────────────────────────────────────────────────────
        object_status = set_object_status(type_target, detected_status)

        if detected_is_back:
            player_number = set_player_number(type_target, detected_number)
        else:
            player_number = conf._object_number_skip

        if object_status == conf._object_status_absent:
            # player_number = set_player_number(type_target, conf._object_number_unknown, True)
            # 2025-08-11
            # 이전 상태 유지 - 상태를 초기화하지 않음
            object_status = conf._prev_pit_status
            player_number = conf._prev_pit_number

        if (object_status == conf._object_status_present_pitcher and
            player_number in [conf._object_number_unknown, conf._object_number_skip, None]):
            pass
        else:
            is_changed = (
                object_status != conf._prev_pit_status or
                player_number != conf._prev_pit_number
            )
            if is_changed:
                conf.fd_dashboard(conf._player_type.pitcher, object_status, player_number)
                conf._prev_pit_status = object_status
                conf._prev_pit_number = player_number

        # 2025-08-11
        # 🔒 ADD: 확정된 등번호를 전역에 커밋 (투수)
        if (object_status == conf._object_status_present_pitcher and
            player_number not in (conf._object_number_unknown, conf._object_number_skip, None)):
            # try:
            #     lock = getattr(conf, "_lock", None)
            #     if lock:
            #         with lock:
            #             conf._live_pitcher_no = int(player_number)
            #     else:
            #         conf._live_pitcher_no = int(player_number)
            # except Exception:
            #     conf._live_pitcher_no = int(player_number)
            # fd_log.info(f"live pitcher no : {player_number}")

            # 2025-08-21
            # ⬇️ Auto일 때만 전역 번호를 덮어쓴다
            if getattr(conf, "_live_pitcher_auto", True):
                try:
                    lock = getattr(conf, "_lock", None)
                    if lock:
                        with lock:
                            conf._live_pitcher_no = int(player_number)
                    else:
                        conf._live_pitcher_no = int(player_number)
                except Exception:
                    conf._live_pitcher_no = int(player_number)

        if conf._detection_viewer:
            cv2.imshow("[Pitcher]", resized_frame)
            cv2.waitKey(1)


# ─────────────────────────────────────────────────────────────────────────────
# def check_golfer_frame():
# [owner] hongsu jung
# [date] 2025-06-02
# ─────────────────────────────────────────────────────────────────────────────
def check_golfer_frame(frame, timestamp):
    fd_log.info(f"\r🔍[Golfer][{timestamp:06.3f}] - check")

    # for dubug    
    if conf._detection_viewer:  
        cv2.imshow("[Golfer]", frame)
        cv2.waitKey(1)
    
# ─────────────────────────────────────────────────────────────────────────────
# def get_detect_type(type_target):
# [owner] hongsu jung
# [date] 2025-06-04
# ─────────────────────────────────────────────────────────────────────────────
def get_detect_type(type_target):
    type_detect = conf._type_detect_unknown
    match type_target:
        case conf._type_live_batter_LH | conf._type_live_batter_RH:
            type_detect = conf._type_detect_batter
        case conf._type_live_pitcher:
            type_detect = conf._type_detect_pitcher
        case conf._type_live_golfer:
            type_detect = conf._type_detect_golfer
        case conf._type_live_batsman :
            type_detect = conf._type_detect_batsman
        case conf._type_live_bowler:
            type_detect = conf._type_detect_bowler
        case conf._type_live_nascar_1 | conf._type_live_nascar_2 | conf._type_live_nascar_3 | conf._type_live_nascar_4 :
            type_detect = conf._type_detect_nascar
        case _:
            type_detect = conf._type_detect_unknown
    return type_detect

# ─────────────────────────────────────────────────────────────────────────────
# def set_object_status():
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def set_object_status(type_target, object_status, force_change = False):

    # get object type
    type_object = get_detect_type(type_target)
    # set force
    if force_change:
        conf._object_status_queue[type_object].clear()   
    # add status to queue
    if(object_status != conf._object_status_unknown):
        conf._object_status_queue[type_object].append(object_status)    
    # get most common value
    counter = Counter(conf._object_status_queue[type_object])
    try:
        most_common, _ = counter.most_common(1)[0]
    except IndexError:
        most_common = conf._player_status.get(type_object, None)
    # update status
    conf._player_status[type_object] = most_common
    return most_common

# ─────────────────────────────────────────────────────────────────────────────
# def get_player_status(type_target):
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def get_player_status(type_target):
    # get object type
    type_object = get_detect_type(type_target)
    status = conf._player_status.get(type_object, None)
    return status

# ─────────────────────────────────────────────────────────────────────────────
# def set_player_handedness():
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def set_player_handedness(type_target, player_handedness, force_change= False):        
    
    # get object type
    type_object = get_detect_type(type_target)
    # set force
    if force_change:
        conf._object_handedness_queue[type_object].clear()    
    else:
        if(player_handedness == conf._object_handedness_unknown):
            return
    
    # add status to queue
    conf._object_handedness_queue[type_object].append(player_handedness)
    # get most common value
    counter = Counter(conf._object_handedness_queue[type_object])
    try:
        most_common, _ = counter.most_common(1)[0]
    except IndexError:
        most_common = conf._player_handedness.get(type_object, None)

    # update status
    conf._player_handedness[type_object] = most_common
    return most_common

# ─────────────────────────────────────────────────────────────────────────────
# def get_player_handedness(type_target):
# [owner] hongsu jung
# [date] 2025-06-04
# ─────────────────────────────────────────────────────────────────────────────
def get_player_handedness(type_target):
    # get object type
    type_object = get_detect_type(type_target)
    handedness = conf._player_handedness.get(type_object, None)
    return handedness

# ─────────────────────────────────────────────────────────────────────────────
# def set_player_number():
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def set_player_number(type_target, player_number, force_change = False):

    # get object type
    type_object = get_detect_type(type_target)
    # set force
    if force_change:
        conf._object_number_queue[type_object].clear()
    else:
        if(player_number == conf._object_number_unknown or player_number == conf._object_number_skip):
            return        
    # add status to queue
    conf._object_number_queue[type_object].append(player_number)    
    # get most common value
    counter = Counter(conf._object_number_queue[type_object])    
    try:
        most_common, _ = counter.most_common(1)[0]
    except IndexError:
        most_common = conf._player_number.get(type_object, None)
    # update status
    conf._player_number[type_object] = most_common
    return most_common

# ─────────────────────────────────────────────────────────────────────────────
# def get_player_number(type_target):
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def get_player_number(type_target):
    # get object type
    type_object = get_detect_type(type_target)
    number = conf._player_number.get(type_object, None)
    return number

# ─────────────────────────────────────────────────────────────────────────────
# def refresh_information():
# [owner] hongsu jung
# [date] 2025-06-05
# ─────────────────────────────────────────────────────────────────────────────
def refresh_information():
    # batter
    conf.fd_dashboard(conf._player_type.batter, conf._prev_bat_status, conf._prev_bat_number, conf._prev_bat_handed)                    
    # pitcher
    conf.fd_dashboard(conf._player_type.pitcher, conf._prev_pit_status, conf._prev_pit_number, conf._prev_pit_handed)
