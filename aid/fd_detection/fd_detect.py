# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_mediapipe
# - 2024/11/05
# - Hongsu Jung
# https://4dreplay.atlassian.net/wiki/x/F4DofQ
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
import re
import easyocr
import time
import pickle
import screeninfo
import threading
import numpy as np
import pandas as pd  
import fd_utils.fd_config   as conf
import matplotlib.pyplot as plt

from ultralytics                import YOLO
from fd_detection.fd_pose       import detect_fingers
from fd_detection.fd_pose       import detect_fingers_yolo, detect_fingers_yolo_roi
from fd_detection.fd_pose       import detect_2d_keypoints_yolo
from fd_utils.fd_logging        import fd_log

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
from PyQt5.QtCore               import QSettings

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

_thread_local = threading.local()
# OCR Reader 생성 (한글 제외 → 숫자 인식만)
def get_easyocr_reader():
    if not hasattr(_thread_local, "reader"):
        _thread_local.reader = easyocr.Reader(['en'], gpu=True)
    return _thread_local.reader
    
def file_exist(file):    
    if os.path.exists(file): 
        fd_log.info("exist file : [{0}]".format(file))
        return True
    return False

def save_array_file(file, array):
    with open(file, "wb") as f:
        pickle.dump(array, f)

def load_array_file(file):
    # Step 1: Load the saved 3D pose data
    with open(file, "rb") as f:        
        return pickle.load(f)

def is_valid(p):
    return isinstance(p, (list, tuple, np.ndarray)) and not np.isnan(p).any()

def is_valid_ball(pos):
    return pos is not None and not np.isnan(pos).any() and not (pos[0] == -1 and pos[1] == -1)

def interp_y(a, b, x):
    """
    선분 a-b에서 x에 해당하는 y값을 선형 보간하여 반환.
    """
    if a[0] == b[0]:  # 수직선일 경우 중간값 반환
        return (a[1] + b[1]) / 2
    ratio = (x - a[0]) / (b[0] - a[0])
    return a[1] + ratio * (b[1] - a[1])

def map_index_to_range(i, i_min, i_max, val_start, val_end):
    if i < i_min or i > i_max:
        raise ValueError(f"Input must be in range {i} -> {i_min} to {i_max}")
    ratio = (i - i_min) / (i_max - i_min)
    return val_start + ratio * (val_start - val_end)\
    
def change_none_to_np(arr):
    return [(np.nan, np.nan) if x is None else x for x in arr ]   

def show_resized_fullscreen(winname, frame, screen_res=(1920, 1080)):
    h, w = frame.shape[:2]
    scale_w = screen_res[0] / w
    scale_h = screen_res[1] / h
    scale = min(scale_w, scale_h)  # 비율 유지

    resized_frame = cv2.resize(frame, (int(w * scale), int(h * scale)))

    # 일반 창으로 생성 (이동 가능, 크기 조정 가능)
    cv2.namedWindow(winname, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(winname, resized_frame.shape[1], resized_frame.shape[0])  # 창 크기 지정
    cv2.imshow(winname, resized_frame)

def get_click_indices(frame_count, total_click_num):
    if frame_count < total_click_num:
        # 가능한 만큼만 균등하게 추출
        return list(range(frame_count))
    
    step = frame_count / (total_click_num - 1)
    indices = [round(i * step) for i in range(total_click_num)]

    # 마지막 인덱스는 항상 frame_count - 1 로 강제
    indices[-1] = frame_count - 1
    return indices
# ─────────────────────────────────────────────────────────────────────────────
# fill_linier(arr):
# [owner] hongsu jung
# [date] 2025-02-15
# ─────────────────────────────────────────────────────────────────────────────
def fill_linier(arr):
    if len(arr) < 1:
        return False, arr

    arr = change_none_to_np(arr)
    nEndCnt = find_last_frame(arr)
    valid_arr = arr[0:nEndCnt]
    valid_arr = np.array(valid_arr, dtype=float)
    if len(valid_arr) < 1:
        return False, arr

    df = pd.DataFrame(valid_arr)

    try:
        df.interpolate(method='polynomial', order=2, limit_direction='both', inplace=True)
    except Exception as e:
        df.interpolate(method='linear', limit_direction='both', inplace=True)

    # debug
    # 2025-05-20
    # first nan -> make fill
    if df.isna().any().any():
        df.interpolate(method='linear', limit_direction='both', inplace=True)

    # ⬇ Optional: Savitzky-Golay smoothing
    smoothed = savgol_filter(df.to_numpy(), window_length=15, polyorder=2, axis=0, mode='nearest')

    # ⬇ Optional: 원본 + 보간 혼합
    #blend_alpha = 1
    #final_arr = blend_alpha * smoothed + (1 - blend_alpha) * valid_arr
    #arr[0:nEndCnt] = final_arr
    arr[0:nEndCnt] = smoothed

    return True, arr


# ─────────────────────────────────────────────────────────────────────────────
# fill_linier_except_first(arr):
# [owner] hongsu jung
# [date] 2025-02-15
# ─────────────────────────────────────────────────────────────────────────────
def fill_linier_except_first(arr):
    if len(arr) < 1:
        return False, arr

    arr = change_none_to_np(arr)
    nEndCnt = find_last_frame(arr)
    valid_arr = arr[:nEndCnt]
    valid_arr = np.array(valid_arr, dtype=float)

    if len(valid_arr) < 1:
        return False, arr

    df = pd.DataFrame(valid_arr)

    # ➤ 앞부분 유효한 첫 index 찾기
    first_valid_idx = df.first_valid_index()
    if first_valid_idx is None:
        return False, arr  # 전부 nan인 경우

    # ➤ 앞부분은 그대로 두고, 이후만 보간
    df_tail = df.iloc[first_valid_idx:].copy()

    try:
        df_tail.interpolate(method='polynomial', order=2, limit_direction='both', inplace=True)
    except Exception as e:
        df_tail.interpolate(method='linear', limit_direction='both', inplace=True)

    if df_tail.isna().any().any():
        df_tail.interpolate(method='linear', limit_direction='both', inplace=True)

    # ➤ smoothing도 유효 영역만
    smoothed_tail = savgol_filter(df_tail.to_numpy(), window_length=min(15, len(df_tail) - (len(df_tail) + 1) % 2), polyorder=2, axis=0, mode='nearest')

    # ➤ 앞부분 유지 + 보간/스무딩된 뒷부분 합치기
    df.iloc[first_valid_idx:] = smoothed_tail
    arr[:nEndCnt] = df.to_numpy()

    return True, arr

# ─────────────────────────────────────────────────────────────────────────────
# fill_linier_except_first_enhanced(arr):
# [owner] yelin kim
# [date] 2025-07-29
# ─────────────────────────────────────────────────────────────────────────────
def fill_linier_except_first_enhanced(arr, max_interp_length=10):
    if len(arr) < 1:
        return False, arr

    arr = change_none_to_np(arr)
    nEndCnt = find_last_frame(arr)
    valid_arr = arr[:nEndCnt]
    valid_arr = np.array(valid_arr, dtype=float)

    if len(valid_arr) < 1:
        return False, arr

    df = pd.DataFrame(valid_arr)

    first_valid_idx = df.first_valid_index()
    if first_valid_idx is None:
        return False, arr

    df_tail = df.iloc[first_valid_idx:].copy()

    # 중간 끊김 대비 → 최대 5개까지만 polynomial 보간
    try:
        df_tail.interpolate(method='polynomial', order=2, limit=max_interp_length,
                            limit_direction='both', inplace=True)
    except Exception as e:
        df_tail.interpolate(method='linear', limit=max_interp_length,
                            limit_direction='both', inplace=True)

    # 여전히 NaN 있으면 → 앞값으로 채우기 (보수적 대응)
    if df_tail.isna().any().any():
        df_tail.fillna(method='ffill', inplace=True)
        df_tail.fillna(method='bfill', inplace=True)

    # smoothing 적용
    arr_tail_np = df_tail.to_numpy()
    if len(arr_tail_np) >= 5:
        win_len = min(15, len(arr_tail_np))
        if win_len % 2 == 0:
            win_len -= 1
        smoothed_tail = savgol_filter(arr_tail_np, window_length=win_len, polyorder=2, axis=0, mode='nearest')
        df.iloc[first_valid_idx:] = smoothed_tail
    else:
        df.iloc[first_valid_idx:] = arr_tail_np

    arr[:nEndCnt] = df.to_numpy()
    return True, arr



def fill_none_with_last_valid(data):
    """
    None으로 채워진 항목들을 마지막 유효 좌표로 채움
    Args:
        data (list of (x, y) tuples or None)
    Returns:
        list of (x, y)
    """
    filled = []
    last_valid = None
    for item in data:
        if item is not None:
            filled.append(item)
            last_valid = item
        else:
            filled.append(last_valid)
    return filled

# ─────────────────────────────────────────────────────────────────────────────
# fill_linier2(arr):
# [owner] hongsu jung
# [date] 2025-02-15
# ─────────────────────────────────────────────────────────────────────────────
def fill_linier2(arr):
    n_cnt = len(arr)
    if n_cnt < 1:
        return
    index = -1
    nStartCnt = -1
    nEndCnt = -1
    while(1):
        index += 1
        if(index >= n_cnt):
            break
        pos = arr[index]
        if(pos is not None):
            if(nStartCnt == -1):
                nStartCnt = index
            nEndCnt = index
    
    # 배열 슬라이싱
    valid_arr = arr[nStartCnt:nEndCnt+1]
    nCnt = len(valid_arr)
    if(nCnt < 1):
        return         
    
    index = -1
    while(1):
        index += 1
        if(index >= nCnt):
            break
        pos = valid_arr[index]
        if(pos is None):
            valid_arr[index] = (np.nan, np.nan)

    valid_arr = np.array(valid_arr, dtype=float)     
    df = pd.DataFrame(valid_arr)
    df.interpolate(method='linear', limit_direction='both', inplace=True)
    # 예측된 데이터를 다시 valid_arr에 반영
    valid_arr = df.to_numpy()    
    arr[nStartCnt:nEndCnt+1] = valid_arr

    return df

# ─────────────────────────────────────────────────────────────────────────────
# fill_empty_ball(arr, width, height)
# [owner] hongsu jung
# [date] 2025-03-25
# extend until screen
# ─────────────────────────────────────────────────────────────────────────────
def fill_empty_ball(arr, width = conf._input_width, height = conf._input_height, limit_over_screen = -200):
    try:        
        n_tot = len(arr)
        valid_indices   = [idx for idx, pos in enumerate(arr) if pos is not None and not np.isnan(pos[0])]
        start1_index    = valid_indices[0]
        start2_index    = valid_indices[1]
        last2_index     = valid_indices[-2]
        last1_index     = valid_indices[-1]    
        start1_pos      = arr[start1_index]
        start2_pos      = arr[start2_index]
        last2_pos       = arr[last2_index]
        last1_pos       = arr[last1_index]
        
        # move left
        if((start1_pos[0] - last1_pos[0]) > 0): 
            
            x1 = start1_pos[0]
            y1 = start1_pos[1]
            x2 = start2_pos[0]
            y2 = start2_pos[1]
            slope_angle = (y2 - y1) / (x2 - x1)
            step_x_unit = (x2 - x1) / (start2_index - start1_index)        
            new_x = start1_pos[0]
            new_y = start1_pos[1]

            # fill right side 
            find_step = 1  
            find_move = 0
            base_step = step_x_unit
            fill_index = start1_index - 1

            while limit_over_screen <= new_x < width and limit_over_screen <= new_y < height:
                if(fill_index < 0):
                    break
                find_move += find_step
                new_x += find_step
                new_y = int(y1 + slope_angle * (new_x-x1)) 
                if new_x >= width or new_y >= height or new_x < limit_over_screen or new_y < limit_over_screen:
                    arr[fill_index] = (new_x,new_y)
                    break
                if (abs(base_step) - abs(find_move)) < 0 :
                    arr[fill_index] = (new_x,new_y)
                    fill_index -= 1
                    base_step += step_x_unit
                    
            # fill left side        
            x1 = last1_pos[0]
            y1 = last1_pos[1]
            x2 = last2_pos[0]
            y2 = last2_pos[1]
            slope_angle = (y2 - y1) / (x2 - x1)
            step_x_unit = (x2 - x1) / (last2_index - last1_index)        
            new_x = last1_pos[0]
            new_y = last1_pos[1]

            find_step = -1 
            find_move = 0
            base_step = step_x_unit
            fill_index = last1_index + 1
            while limit_over_screen <= new_x < width and limit_over_screen <= new_y < height:
                if(fill_index >= n_tot):
                    break
                find_move += find_step
                new_x += find_step
                new_y = int(y1 + slope_angle * (new_x-x1)) 
                if new_x >= width or new_y >= height or new_x < limit_over_screen or new_y < limit_over_screen:
                    arr[fill_index] = (new_x,new_y)
                    break
                if (abs(base_step) - abs(find_move)) < 0 :
                    arr[fill_index] = (new_x,new_y)
                    fill_index += 1
                    base_step += step_x_unit
        # move right
        else: 
            # fill left side         
            x1 = start1_pos[0]
            y1 = start1_pos[1]
            x2 = start2_pos[0]
            y2 = start2_pos[1]
            slope_angle = (y2 - y1) / (x2 - x1)
            step_x_unit = (x2 - x1) / (start2_index - start1_index)        
            new_x = start1_pos[0]
            new_y = start1_pos[1]

            find_step = -1 
            find_move = 0
            base_step = step_x_unit
            fill_index = start1_index - 1

            while limit_over_screen <= new_x < width and limit_over_screen <= new_y < height:                    
                if(fill_index < 0):
                    break
                find_move += find_step
                new_x += find_step
                new_y = int(y1 + slope_angle * (new_x-x1)) 
                if new_x >= width or new_y >= height or new_x < limit_over_screen or new_y < limit_over_screen:
                    arr[fill_index] = (new_x,new_y)
                    break
                if (abs(base_step) - abs(find_move)) < 0 :
                    arr[fill_index] = (new_x,new_y)
                    fill_index -= 1
                    base_step += step_x_unit
                    
            # fill right side
            x1 = last1_pos[0]
            y1 = last1_pos[1]
            x2 = last2_pos[0]
            y2 = last2_pos[1]
            slope_angle = (y2 - y1) / (x2 - x1)
            step_x_unit = (x2 - x1) / (last2_index - last1_index)        
            new_x = last1_pos[0]
            new_y = last1_pos[1]
            
            find_step = 1
            find_move = 0
            base_step = step_x_unit
            fill_index = last1_index + 1
            
            while limit_over_screen <= new_x < width and limit_over_screen <= new_y < height:
                if(fill_index >= n_tot):
                    break
                find_move += find_step
                new_x += find_step
                new_y = int(y1 + slope_angle * (new_x-x1)) 
                if new_x >= width or new_y >= height or new_x < limit_over_screen or new_y < limit_over_screen:
                    arr[fill_index] = (new_x,new_y)
                if (abs(base_step) - abs(find_move)) < 0 :
                    arr[fill_index] = (new_x,new_y)
                    fill_index += 1
                    base_step += step_x_unit
        
        return True
    except Exception as e:
        fd_log.error(f"[Error] fill_empty_ball Exception: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# separate_pitch_hit(arr):
# [owner] hongsu jung
# [date] 2025-05-22
# ─────────────────────────────────────────────────────────────────────────────    
def separate_pitch_hit(arr):
    arr = arr.copy()
    result = [None] * len(arr)    
    frame_width = conf._input_width * 1.2
    frame_height = conf._input_height * 1.2
    hit_expected_index = (conf._batter_detect_prev_frame * -1 )
    ########################################################
    # 1. 유효 포인트 수집
    ########################################################
    valid_indices = [
        i for i, v in enumerate(arr)
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).any()
    ]
    if len(valid_indices) < 5:
        fd_log.error(f"❌ not enough detected balls : valid points={valid_indices}")
        return None, None, None

    ########################################################
    # 2. PITCH - 
    # right hand    -> x 증가 구간 추출
    # left hand     -> x 감소 구간 추출
    ########################################################
    original_valid_indices = [
        i for i, v in enumerate(arr)
        if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).any()
    ]

    pitch_indices = []
    if conf._swing_right_hand == True:
        for i in range(1, len(original_valid_indices)):
            prev_idx = original_valid_indices[i - 1]
            curr_idx = original_valid_indices[i]
            if arr[curr_idx][0] < arr[prev_idx][0]:
                if not pitch_indices:
                    pitch_indices.append(prev_idx)
                pitch_indices.append(curr_idx)
            else:
                break
    else:
        for i in range(1, len(original_valid_indices)):
            prev_idx = original_valid_indices[i - 1]
            curr_idx = original_valid_indices[i]
            if arr[curr_idx][0] > arr[prev_idx][0]:
                if not pitch_indices:
                    pitch_indices.append(prev_idx)
                pitch_indices.append(curr_idx)
            else:
                break


    if len(pitch_indices) < 2:
        fd_log.error(f"❌ not enough pitch balls : pitch_indices={pitch_indices}")
        return None
    
    pitch_filled = [np.nan] * len(arr)
    for i in range(len(pitch_indices)):
        idx = pitch_indices[i]
        pitch_filled[idx] = tuple(arr[idx])        

    ########################################################
    # 3. 직선성 확인
    # Pitch 마지막 3개 + 다음 1개 비교하여 명확한 hit 전환 확인
    ########################################################
    # 마지막 세 점의 인덱스
    idx_a, idx_b, idx_c = pitch_indices[-3], pitch_indices[-2], pitch_indices[-1]    
    # 각 점의 좌표
    a, b, c = np.array(arr[idx_a]), np.array(arr[idx_b]), np.array(arr[idx_c])
    # 두 구간의 벡터
    vec1 = b - a
    vec2 = c - b
    # 각 벡터의 단위 벡터
    unit_vec1 = vec1 / np.linalg.norm(vec1)
    unit_vec2 = vec2 / np.linalg.norm(vec2)
    # 코사인 유사도로 각도 계산
    dot_product = np.dot(unit_vec1, unit_vec2)
    dot_product = np.clip(dot_product, -1.0, 1.0)  # 안정성 확보
    angle_rad = np.arccos(dot_product)
    angle_deg = np.degrees(angle_rad)
    # 3도 이상 차이나는지 확인
    if angle_deg >= 3:
        fd_log.warning(f"⚠️ 방향 변화 감지됨: angle={angle_deg:.2f}°")
        pitch_indices.pop()
        for j in range(idx_c, len(pitch_filled)):
            pitch_filled[j] = np.nan        
    else:
        fd_log.info(f"✅ 방향 일정함: angle={angle_deg:.2f}°")

    pitch_filled_clean = [
        pt if isinstance(pt, tuple) and len(pt) == 2 else (np.nan, np.nan)
        for pt in pitch_filled
    ]
    fill_empty_ball(pitch_filled_clean, frame_width, frame_height)
    df = fill_linier2(pitch_filled_clean)
    arr_filled_pitch = df.to_numpy()

    # 2025-05024
    # 보간된 값이 있다면, 다시 arr로 대입하고, 확인
    is_interpolated = False
    for added_index in range(hit_expected_index - 2, hit_expected_index + 1):
        if (
            isinstance(arr[added_index], float) and np.isnan(arr[added_index]) or
            isinstance(arr[added_index], (list, tuple, np.ndarray)) and np.isnan(arr[added_index]).any()
        ):
            arr[added_index] = tuple(arr_filled_pitch[added_index])
            is_interpolated = True

    # 재보정
    if is_interpolated:
        original_valid_indices = [
            i for i, v in enumerate(arr)
            if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).any()
        ]
        pitch_indices = []
        if conf._swing_right_hand == True:
            for i in range(1, len(original_valid_indices)):
                prev_idx = original_valid_indices[i - 1]
                curr_idx = original_valid_indices[i]
                if arr[curr_idx][0] < arr[prev_idx][0]:
                    if not pitch_indices:
                        pitch_indices.append(prev_idx)
                    pitch_indices.append(curr_idx)
                else:
                    break
        else:
            for i in range(1, len(original_valid_indices)):
                prev_idx = original_valid_indices[i - 1]
                curr_idx = original_valid_indices[i]
                if arr[curr_idx][0] > arr[prev_idx][0]:
                    if not pitch_indices:
                        pitch_indices.append(prev_idx)
                    pitch_indices.append(curr_idx)
                else:
                    break


    ########################################################
    # 5. HIT 시작 지점 찾기
    ########################################################
    if len(pitch_indices) < 2:
        fd_log.error(f"❌ not enough pitch balls : pitch_indices={pitch_indices}")
        return None, None, None

    raw_hit_start = pitch_indices[-1] + 1    
    hit_start_idx = None
    for i in range(raw_hit_start, len(arr)):
        if (isinstance(arr[i], (list, tuple, np.ndarray)) and 
            len(arr[i]) >= 2 and 
            not np.isnan(arr[i]).any()):
            hit_start_idx = i
            break
        else:
            arr[i] = tuple(arr_filled_pitch[i])

    ########################################################
    # 6. Hit 보간
    ########################################################
    hit_indices = [i for i in valid_indices if i >= hit_start_idx]
    if len(hit_indices) < 2:
        fd_log.error(f"❌ not enough hit balls : hit_indices={hit_indices}")
        return None, None, None

    hit_filled = [np.nan] * len(arr)
    for i in range(len(hit_indices) - 1):
        idx1, idx2 = hit_indices[i], hit_indices[i + 1]
        p1, p2 = np.array(arr[idx1]), np.array(arr[idx2])
        delta = (p2 - p1) / (idx2 - idx1)

        for k in range(idx2 - idx1 + 1):
            interpolated = p1 + delta * k
            if tuple(interpolated.tolist()) == (-1.0, -1.0):
                continue  # 마지막 점이 (-1, -1)이면 무시
            hit_filled[idx1 + k] = tuple(interpolated.tolist())

    hit_filled_clean = [
        pt if isinstance(pt, tuple) and len(pt) == 2 else (np.nan, np.nan)
        for pt in hit_filled
    ]
    fill_empty_ball(hit_filled_clean, frame_width, frame_height)
    df = fill_linier2(hit_filled_clean)
    arr_filled_hit = df.to_numpy()

    ########################################################
    # 7. 교차점 기반 hit_start_idx 보정 (교차점은 pitch_end와 hit 시작 사이의 정확한 순간)
    ########################################################
    hit_start_idx -= 1

    return arr_filled_pitch, arr_filled_hit, hit_start_idx

# ─────────────────────────────────────────────────────────────────────────────
# def plot_pitch_hit_trajectories(arr_pitching, arr_hitting, hit_index_est)
# [owner] hongsu jung
# [date] 2025-05-22
# ─────────────────────────────────────────────────────────────────────────────
def plot_pitch_hit_trajectories(arr_pitching, arr_hitting, hit_index_est):
    # Hit Point 추출
    estimated_hit_point = None
    if 0 <= hit_index_est < len(arr_pitching):
        pt = arr_pitching[hit_index_est]
        if isinstance(pt, (list, tuple, np.ndarray)) and not np.isnan(pt).any():
            estimated_hit_point = tuple(pt)
    # === 시각화 ===
    plt.figure(figsize=(10, 6))

    # pitch
    pitch_points = []
    for i, pt in enumerate(arr_pitching):
        if isinstance(pt, (list, tuple, np.ndarray)) and not np.isnan(pt).any():
            pitch_points.append(pt)
            plt.scatter(pt[0], pt[1], color='blue', marker='o')
            plt.text(pt[0], pt[1], f'{i}\n({pt[0]:.1f}, {pt[1]:.1f})', fontsize=8, color='blue', va='top', ha='center')

    if len(pitch_points) >= 2:
        pitch_points = np.array(pitch_points)
        plt.plot(pitch_points[:, 0], pitch_points[:, 1], color='blue', label='Pitch Trajectory')

    # hit
    hit_points = []
    for i, pt in enumerate(arr_hitting):
        if isinstance(pt, (list, tuple, np.ndarray)) and not np.isnan(pt).any():
            hit_points.append(pt)
            plt.scatter(pt[0], pt[1], color='red', marker='x')
            plt.text(pt[0], pt[1], f'{i}\n({pt[0]:.1f}, {pt[1]:.1f})', fontsize=8, color='red', va='top', ha='center')

    if len(hit_points) >= 2:
        hit_points = np.array(hit_points)
        plt.plot(hit_points[:, 0], hit_points[:, 1], color='red', label='Hit Trajectory')

    # Estimated Hit Point 시각화
    if estimated_hit_point:
        plt.scatter(estimated_hit_point[0], estimated_hit_point[1], color='green', s=100,
                    label=f"Estimated Hit Point (Frame {hit_index_est})",
                    edgecolors='black', zorder=5)
        plt.text(estimated_hit_point[0], estimated_hit_point[1],
                 f'Hit\n({estimated_hit_point[0]:.1f}, {estimated_hit_point[1]:.1f})',
                 fontsize=9, color='green', va='bottom', ha='center')

    plt.title("Pitch and Hit Trajectories with Frame Indices")
    plt.xlabel("X Position")
    plt.ylabel("Y Position")
    plt.grid(True)
    plt.gca().invert_yaxis()
    plt.gca().set_aspect('equal', adjustable='box')
    plt.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=2, fontsize=9)
    plt.tight_layout()
    plt.show(block=True)

# ─────────────────────────────────────────────────────────────────────────────
# def estimate_intersection_by_min_distance(pitching_arr, htting_arr)
# [owner] hongsu jung
# [date] 2025-05-22
# ─────────────────────────────────────────────────────────────────────────────
def estimate_intersection_by_min_distance(pitching_arr, htting_arr):
    """
    프레임 단위로 pitching과 htting 점들을 비교하여,
    두 점 사이 거리(Euclidean distance)가 최소인 시점의 중간 지점을 교차점으로 반환합니다.

    Parameters:
        - pitching_arr: np.ndarray, (N, 2) pitching 좌표
        - htting_arr: np.ndarray, (N, 2) htting 좌표

    Returns:
        - intersection_point: np.ndarray, (x, y) 좌표
        - min_distance: float, 해당 시점의 두 점 사이 거리
    """
    min_dist = float('inf')
    best_point = None

    for p, h in zip(pitching_arr, htting_arr):
        if -1 in p or -1 in h:
            continue
        mid = (p + h) / 2
        dist = euclidean(p, h)
        if dist < min_dist:
            min_dist = dist
            best_point = mid

    return best_point, min_dist

# ─────────────────────────────────────────────────────────────────────────────
# def refined_intersection_with_both_slopes(pitching_arr, htting_arr)
# [owner] hongsu jung
# [date] 2025-05-22
# ─────────────────────────────────────────────────────────────────────────────
def refined_intersection_with_both_slopes(pitching_arr, htting_arr):
    """
    거리 최소 프레임을 기준으로 x를 찾고,
    pitching과 htting 궤적의 양쪽 기울기를 사용해 y 값을 보정합니다.

    Returns:
        - refined_point: np.ndarray, (x, y)
        - midpoint_before: np.ndarray, (x, y)
        - min_distance: float
    """
    min_dist = float('inf')
    best_index = None
    best_mid = None

    for i, (p, h) in enumerate(zip(pitching_arr, htting_arr)):
        if -1 in p or -1 in h:
            continue
        mid = (p + h) / 2
        dist = euclidean(p, h)
        if dist < min_dist:
            min_dist = dist
            best_index = i
            best_mid = mid

    if best_index is None or best_index <= 0 or best_index >= len(pitching_arr) - 1:
        return best_mid, best_mid, min_dist  # fallback

    # Get slope from pitching
    p1, p2 = pitching_arr[best_index - 1], pitching_arr[best_index + 1]
    if -1 in p1 or -1 in p2:
        return best_mid, best_mid, min_dist
    dx_p = p2[0] - p1[0]
    dy_p = p2[1] - p1[1]
    slope_p = dy_p / dx_p if dx_p != 0 else 0

    # Get slope from htting
    h1, h2 = htting_arr[best_index - 1], htting_arr[best_index + 1]
    if -1 in h1 or -1 in h2:
        return best_mid, best_mid, min_dist
    dx_h = h2[0] - h1[0]
    dy_h = h2[1] - h1[1]
    slope_h = dy_h / dx_h if dx_h != 0 else 0

    # Average slope
    avg_slope = (slope_p + slope_h) / 2

    # Reference point (best_index) from pitching
    x_ref = best_mid[0]
    x0, y0 = pitching_arr[best_index]

    # Adjust y using average slope
    y_refined = y0 + avg_slope * (x_ref - x0)
    refined_point = np.array([x_ref, y_refined])

    return refined_point, best_mid, min_dist

# ─────────────────────────────────────────────────────────────────────────────
# def full_slope_intersection(pitching_arr, htting_arr)
# [owner] hongsu jung
# [date] 2025-05-22
# Compute the exact intersection point of the pitching and hitting lines near the best x point
# ─────────────────────────────────────────────────────────────────────────────
def full_slope_intersection(pitching_arr, htting_arr):
    """
    거리 최소 프레임 기준으로, 해당 구간의 pitching과 htting의 기울기를 사용해
    두 직선의 교차점 (x, y)을 계산합니다.

    Returns:
        - intersection_point: np.ndarray, (x, y)
        - best_index: int
        - min_distance: float
    """
    min_dist = float('inf')
    best_index = None

    for i, (p, h) in enumerate(zip(pitching_arr, htting_arr)):
        if -1 in p or -1 in h:
            continue
        dist = euclidean(p, h)
        if dist < min_dist:
            min_dist = dist
            best_index = i

    if best_index is None or best_index <= 0 or best_index >= len(pitching_arr) - 1:
        return None, best_index, min_dist

    # Get pitching segment (p1, p2)
    p1, p2 = pitching_arr[best_index - 1], pitching_arr[best_index + 1]
    h1, h2 = htting_arr[best_index - 1], htting_arr[best_index + 1]
    if -1 in p1 or -1 in p2 or -1 in h1 or -1 in h2:
        return None, best_index, min_dist

    # Line 1: y = m1*x + b1
    m1 = (p2[1] - p1[1]) / (p2[0] - p1[0]) if p2[0] != p1[0] else 0
    b1 = p1[1] - m1 * p1[0]

    # Line 2: y = m2*x + b2
    m2 = (h2[1] - h1[1]) / (h2[0] - h1[0]) if h2[0] != h1[0] else 0
    b2 = h1[1] - m2 * h1[0]

    # Solve for x: m1*x + b1 = m2*x + b2 -> x = (b2 - b1) / (m1 - m2)
    if m1 == m2:
        return None, best_index, min_dist  # Parallel lines

    x_int = (b2 - b1) / (m1 - m2)
    y_int = m1 * x_int + b1

    return np.array([x_int, y_int]), best_index, min_dist

# ─────────────────────────────────────────────────────────────────────────────
# def full_slope_intersection(pitching_arr, htting_arr)
# [owner] hongsu jung
# [date] 2025-05-22
# Combined method based on angle threshold
# ─────────────────────────────────────────────────────────────────────────────
def hybrid_intersection(pitching_arr, htting_arr, angle_threshold_deg=7.5):
    """
    두 궤적의 기울기 차이를 기준으로:
    - 5도 이하: 평균 기울기로 보정 (refined_intersection_with_both_slopes)
    - 5도 초과: 실제 두 직선의 교차점 계산 (full_slope_intersection)

    Returns:
        - final_point: np.ndarray, (x, y)
        - method_used: str, "refined_average_slope" or "line_intersection"
        - angle_difference: float, 기울기 차이 (deg)
    """
    # 먼저 거리 최소 위치 찾기
    min_dist = float('inf')
    best_index = None
    for i, (p, h) in enumerate(zip(pitching_arr, htting_arr)):
        if -1 in p or -1 in h:
            continue
        dist = euclidean(p, h)
        if dist < min_dist:
            min_dist = dist
            best_index = i

    if best_index is None or best_index <= 0 or best_index >= len(pitching_arr) - 1:
        return None, "invalid_index", None

    # 기울기 계산
    p1, p2 = pitching_arr[best_index - 1], pitching_arr[best_index + 1]
    h1, h2 = htting_arr[best_index - 1], htting_arr[best_index + 1]
    if -1 in p1 or -1 in p2 or -1 in h1 or -1 in h2:
        return None, "invalid_neighbors", None

    slope_p = (p2[1] - p1[1]) / (p2[0] - p1[0]) if (p2[0] - p1[0]) != 0 else 0
    slope_h = (h2[1] - h1[1]) / (h2[0] - h1[0]) if (h2[0] - h1[0]) != 0 else 0

    angle_p = np.degrees(np.arctan(slope_p))
    angle_h = np.degrees(np.arctan(slope_h))
    angle_diff = abs(angle_p - angle_h)

    if angle_diff <= angle_threshold_deg:
        point, _, _ = refined_intersection_with_both_slopes(pitching_arr, htting_arr)
        return point, "refined_average_slope", angle_diff
    else:
        point, _, _ = full_slope_intersection(pitching_arr, htting_arr)
        return point, "line_intersection", angle_diff

# ─────────────────────────────────────────────────────────────────────────────
# def split_pitch_and_hit_with_hitpoint(arr, angle_threshold_deg=25):
#     return arr_pitching, arr_hitting, hit_point, hit_index_1, hit_index_2, merged_arr
# [owner] hongsu jung
# [date] 2025-05-22
# ─────────────────────────────────────────────────────────────────────────────
def split_pitch_and_hit_with_hitpoint(arr, angle_threshold_deg=25):
    
    nStartCnt, nEndCnt = get_start_end_time(arr)
    valid_arr = arr[nStartCnt:nEndCnt + 1]
    valid_arr = np.array(valid_arr, dtype=float)

    # 1. Fill valid trajectory first
    arr_pitching, arr_hitting, hit_index_est = separate_pitch_hit(arr)      
    if arr_pitching is None:
        return None, None, None, None, None, None

    # debug 
    # plot_pitch_hit_trajectories(arr_pitching, arr_hitting, hit_index_est)

    # 2. Find the hit point    
    # Run the refined method
    # hit_point, distance  = estimate_intersection_by_min_distance(arr_pitching, arr_hitting)    
    hit_point, original_midpoint, distance = hybrid_intersection(arr_pitching, arr_hitting)
    
    # 3. Confirm : hit_index에서  hit position의 x는 hit_index_est의 x 값보다 작을수 없다.
    while True:
        pitching_x = arr_pitching[hit_index_est][0]
        hit_x = hit_point[0]
        if(conf._swing_right_hand):
            gap_x = abs(hit_x - pitching_x)
            if(gap_x < 15):
                hit_point = arr_pitching[hit_index_est]
                break
            elif(hit_x > pitching_x ):
                hit_index_est -= 1
            else:
                break
        else:
            gap_x = abs(hit_x - pitching_x)
            if(gap_x < 15):
                hit_point = arr_pitching[hit_index_est]
                break
            elif(hit_x < pitching_x):
                hit_index_est -= 1
            else:
                break
    
    hit_index_1 = hit_index_est
    hit_index_2 = hit_index_est + 1

    # pitching은 부드럽게
    fd_smooth_ball_tracking(arr_pitching, 1)
    # hitting은 최소한만 보정
    fd_smooth_ball_tracking(arr_hitting, 0.1)

    # Array 병합
    merged_arr = arr.copy()
    for i in range(len(arr)):
        if hit_index_1 is not None and i <= hit_index_1:
            if is_valid_ball(arr_pitching[i]):
                merged_arr[i] = arr_pitching[i]
        elif hit_index_2 is not None and i >= hit_index_2:
            if is_valid_ball(arr_hitting[i]):
                merged_arr[i] = arr_hitting[i]


    return arr_pitching, arr_hitting, hit_point, hit_index_1, hit_index_2, merged_arr

# ─────────────────────────────────────────────────────────────────────────────
# def set_last_array(arr):
# [owner] hongsu jung
# [date] 2025-02-15
# ─────────────────────────────────────────────────────────────────────────────
def set_last_array(arr):        
    nTot = len(arr)
    i = nTot
    while(1):
        i -= 1
        if(i < 0): break
        pos = arr[i]
        if pos is None:
            continue
        if np.isnan(pos).any() == False:
            break
    if(i < nTot - 1):
        arr[i+1] = (-1,-1)
        
# ─────────────────────────────────────────────────────────────────────────────
# def is_pitcher_ball_in_movement(frameindex, arr_ball):
# [owner] hongsu jung
# [date] 2025-03-16
# ─────────────────────────────────────────────────────────────────────────────
def is_ball_in_movement(frameindex, arr_ball, margin_min, margin_max, width):

    if(conf._type_target == conf._type_baseball_pitcher):
        margin = map_index_to_range(frameindex, 0, conf._pitcher_detect_post_frame, margin_max, margin_min)
    else:
        margin = int((margin_max+margin_min)/2)

    # check the previous ball 
    ball_index = frameindex
    curr_x = arr_ball[frameindex][0]
    curr_y = arr_ball[frameindex][1]

    while(1):
        ball_index -= 1
        if(ball_index < 0):
            # check position
            if(conf._type_target == conf._type_baseball_pitcher):
                if(curr_x > width/9*7):
                    fd_log.error ("❌ wrong first detection")
                    return False     
            elif(conf._type_target == conf._type_baseball_batter_RH):
                if(curr_x < width/9*5):
                    fd_log.error ("❌ wrong first detection")
                    return False
            elif(conf._type_target == conf._type_baseball_batter_LH):
                if(curr_x > width/9*5):
                    fd_log.error ("❌ wrong first detection")
                    return False
            fd_log.info ("✋ first detection")
            conf._after_detected = True
            return True
        if arr_ball[ball_index] is None:
            return False
        
        prev_x = arr_ball[ball_index][0]
        prev_y = arr_ball[ball_index][1]
        if(prev_x > 0 and prev_y > 0):
            break

    gap_x = abs(curr_x - prev_x)
    gap_y = abs(curr_y - prev_y)

    margin_detected = margin * abs(frameindex - ball_index)
    if(gap_x > margin_detected or gap_y > margin_detected):
        fd_log.warning (f"⚠️[Ball] margin:{margin_detected}, x:{gap_x}, y:{gap_y} ball is not on the way")


        # reset previous detection
        return False
    return True

# ─────────────────────────────────────────────────────────────────────────────
# def is_pitcher_ball_in_hand(frameindex, arr_ball, arr_pose):
# [owner] hongsu jung
# [date] 2025-03-06
# ─────────────────────────────────────────────────────────────────────────────
def is_ball_in_pitcher_hand(frame, frameindex, arr_ball):
    
    # check frame index
    # get hand data
    #result, pos_right_hand, pos_left_hand = detect_fingers(frame)
    # 2025-07-28
    # yolo post

    # for detection timing
    time.sleep(0.0001)
    # result, pos_right_hand, pos_left_hand = detect_fingers_yolo(frame)
    result, pos_l_h, pos_r_h, pos_l_e, pos_r_e, pos_l_s, pos_r_s = detect_fingers_yolo_roi(frame)
    
    if(result == False):
        return True

    # check the near ball 
    ball = get_area_position(arr_ball[frameindex])
    #ball = arr_ball[frameindex]
    x = ball[0]
    y = ball[1]

    x1 = int(pos_right_hand[0])
    y1 = int(pos_right_hand[1])
    x2 = int(pos_left_hand[0])
    y2 = int(pos_left_hand[1])
    margin = conf._margin_pitcher_ball

    #debug                        
    x1_margin = abs(x-x1)
    y1_margin = abs(y-y1)
    x2_margin = abs(x-x2)
    y2_margin = abs(y-y2)
    
    # debug
    text = f"Margin {margin} | right hand:{x1_margin,y1_margin}, left hand:{x2_margin,y2_margin}"
    cv2.putText(frame, text, (x, y - 5), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 3)        
    cv2.circle(frame, (x,y),5,(0,0,255),2)
    cv2.circle(frame, (x1,y1),10,(0,255,0),2)
    cv2.circle(frame, (x2,y2),10,(255,0,0),2)
    #cv2.imshow("[Pitcher] position pitcher hand and ball", frame)
    #cv2.waitKey(0)
    
    fd_log.info (f"[RH] [{x1},{y1}]-[{x},{y}] = [{x1_margin},{y1_margin}][{margin}]")
    fd_log.info (f"[LH] [{x2},{y2}]-[{x},{y}] = [{x2_margin},{y2_margin}][{margin}]")

    if(abs(x - x1) <= margin and abs(y - y1) <= margin):
        fd_log.info ("⚠️ ball is still in the right hand pitcher")
        # check over 10 frame
        if(frameindex < conf._ball_detect_pitcher_hand ):
            reset_previous_data(arr_ball, frameindex)
        return True
    if(abs(x - x2) <= margin and abs(y - y2) <= margin):
        fd_log.info ("⚠️ ball is still in the left hand pitcher")
        # check over 10 frame
        if(frameindex < conf._ball_detect_pitcher_hand ):
            reset_previous_data(arr_ball, frameindex)
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# def is_ball_in_pitcher_hand_enhanced(frameindex, arr_ball, arr_pose):
# [owner] yelin kim
# [date] 2025-07-29
# ─────────────────────────────────────────────────────────────────────────────
def is_ball_in_pitcher_hand_enhanced(frame, frameindex, arr_ball):
    result, p_l_h, p_r_h, p_l_e, p_r_e, p_l_s, p_r_s = detect_fingers_yolo_roi(frame)
    if not result:
        return True, 0  # 손 검출 실패 시 공이 손에 있다고 간주

    # 공 위치
    x, y = get_area_position(arr_ball[frameindex])

    # 관절 위치
    joints = {
        "LH": (int(p_l_h[0]), int(p_l_h[1])),
        "RH": (int(p_r_h[0]), int(p_r_h[1])),
        "LE": (int(p_l_e[0]), int(p_l_e[1])),
        "RE": (int(p_r_e[0]), int(p_r_e[1])),
        "LS": (int(p_l_s[0]), int(p_l_s[1])),
        "RS": (int(p_r_s[0]), int(p_r_s[1]))
    }

    margin = conf._margin_pitcher_ball
    ball_near_joint = False
    lh_gap = rh_gap = float('inf')
    
    # distance
    for name, (jx, jy) in joints.items():
        distance = math.hypot(x - jx, y - jy)
        fd_log.info(f"[{name}] [{jx},{jy}] - [{x},{y}] = [dist: {distance:.2f}] [margin: {margin}]")

        if distance <= margin:
            ball_near_joint = True

        if name == "LH":
            lh_gap = distance
        elif name == "RH":
            rh_gap = distance

    # near joint
    if ball_near_joint:
        if frameindex < conf._ball_detect_pitcher_hand:
            reset_previous_data(arr_ball, frameindex)
        return True, 0

    # 손에서 벗어난 경우 → LH, RH 거리 중 작은 값 기준으로 remove count 결정
    min_gap = min(lh_gap, rh_gap)    
    if min_gap <= 100: release_idx = 1
    elif min_gap   <= 150: release_idx = 2
    else: release_idx = 3
    
    fd_log.info(f"🚀Pitching Release [gap:{min_gap}], curr[{frameindex}] | prev release index[{release_idx}]")
    return False, release_idx


# ─────────────────────────────────────────────────────────────────────────────
# def is_pitcher_ball_in_hand(frameindex, arr_ball, arr_pose):
# [owner] hongsu jung
# [date] 2025-03-06
# ─────────────────────────────────────────────────────────────────────────────    
def check_movement_in_threshold(pos_candidate, pos_predicted , frameindex, arr_ball, threshold_min, threshold_max, kalman):

    pos_prev_index = frameindex - 1
    if 0 <= frameindex <= conf._pitcher_detect_post_frame:
        threshold_per_frame = map_index_to_range(frameindex, 0, conf._pitcher_detect_post_frame, threshold_max, threshold_min)
    else:
        return False  # or continue, or handle gracefully

    # 1. 유효한 과거 위치 찾기
    while pos_prev_index >= 0:
        prev_ball = arr_ball[pos_prev_index]
        if prev_ball is not None and not np.isnan(prev_ball).any():
            break
        pos_prev_index -= 1

    # 2. 유효한 이전 값이 없으면 True (처음 등장 or 예외 상황 허용)
    if pos_prev_index < 0:
        return True

    # 3. 프레임 간 거리 계산
    dx = pos_candidate[0] - prev_ball[0]
    dy = pos_candidate[1] - prev_ball[1]
    dist = np.hypot(dx, dy)

    frame_gap = frameindex - pos_prev_index
    threshold = threshold_per_frame * frame_gap

    if(dist > threshold):
        fd_log.error(f"\r❗[PIT] Threshold:{threshold}, Dist:{dist:.2f} - too far from prev position")  
        return False

    # ─────────────────────────────────────────────────────────────────────────────    
    # check predicted
    # ─────────────────────────────────────────────────────────────────────────────    
    if frameindex > conf._ball_detect_pitcher_pred_start:
        # 3. 프레임 간 거리 계산
        dx = pos_candidate[0] - pos_predicted[0]
        dy = pos_candidate[1] - pos_predicted[1]
        dist = np.hypot(dx, dy)
        
        normal_threshold = conf._ball_detect_pitcher_pred_margin  #* (1 + 0.2 * (frame_gap - 1))
        if(dist > normal_threshold):
            # non obund detection
            fd_log.error(f"\r❗[PIT][Predict] Threshold:{normal_threshold},dist:{dist:.2f} - too far from predict")
            return False
            # for bound // check bound near base
            '''
            if(frameindex > 25):
                ball_x = pos_candidate[0]
                ball_y = pos_candidate[1]                                
                if((630 < ball_y < 730) and (1300 < ball_x < 1500)):
                    bound_threshold = conf._ball_detect_pitcher_margin_bound * (1 + 0.2 * (frame_gap - 1))
                    if(dist > bound_threshold):
                        fd_log.error(f"\r❗[PIT][Predict] Threshold:{threshold},missing:{frame_gap} Dist:{dist:.2f} - too far even not bound")                                
                        return False
                    else:
                        fd_log.warning(f"\r⚠️[PIT][Predict] Threshold:{threshold},missing:{frame_gap} Dist:{dist:.2f} - Bound")                                                
                        kalman.processNoiseCov = np.eye(4, dtype=np.float32) * 1e-1  # increase flexibility
                        return True
                else:
                    return False
            else:
                fd_log.error(f"\r❗[PIT][Predict] Threshold:{normal_threshold},missing:{frame_gap} Dist:{dist:.2f} - too far from predicted")                                
                return False
            '''                    
        else:
            fd_log.info(f"\r✅[PIT][Predict] Threshold:{normal_threshold},dist:{dist:.2f} - within predict")                                
    return True

# ─────────────────────────────────────────────────────────────────────────────
# def is_pitcher_ball_in_hand(frameindex, arr_ball, arr_pose):
# [owner] hongsu jung
# [date] 2025-05-23
# ─────────────────────────────────────────────────────────────────────────────
def check_points(p1, p2, margin):
    """
    p1과 p2가 (x, y) 형태의 점일 때,
    두 점 사이의 유클리드 거리가 margin 이하이면 True, 아니면 False를 반환.
    """
    if p1 is None or p2 is None:
        return False
    if any(np.isnan(p1)) or any(np.isnan(p2)):
        return False
    distance = np.linalg.norm(np.array(p1) - np.array(p2))

    if(distance > margin):
        fd_log.warning(f"[CHK] non expected ball position detect:{p1}, expected:{p2}, distance:{distance:.2f}, margin:{margin}")
        return False, distance
    #fd_log.info(f"[CHK] ball position detect:{p1}, expected:{p2}, distance:{distance:.2f}, margin:{margin}")
    return True, distance


# ─────────────────────────────────────────────────────────────────────────────
# def get_hit_timing(hit_idx, idx, prev_x, curr_x, descending_count):
# [owner] hongsu jung
# [date] 2025-07-28    
# ─────────────────────────────────────────────────────────────────────────────    
def get_hit_timing(hit_idx, idx, curr_pos, predicted_center):
    # get current  pos
    x, y = curr_pos
    pred_x, pred_y = predicted_center
    if conf._swing_right_hand:
        # at least x is another size from pred_x
        if hit_idx == float('inf') and pred_x + 3 < x :
            (left, top), (right, bottom) = conf._batter_hit_RH_area
            if left <= x <= right and top <= y <= bottom:
                hit_idx = idx            
                fd_log.info(f"🎯 [HIT DETECTED][Right Hand] Detected first hit frame at idx={hit_idx}")
    else:
        if hit_idx == float('inf') and pred_x - 3 > x :
            (left, top), (right, bottom) = conf._batter_hit_LH_area            
            if left <= x <= right and top <= y <= bottom:
                hit_idx = idx
                fd_log.info(f"🎯 [HIT DETECTED][Left Hand] Detected first hit frame at idx={hit_idx}")                
    return hit_idx

# ─────────────────────────────────────────────────────────────────────────────
# def is_ball_in_the_range(frameindex, arr_ball, arr_pose):
# [owner] hongsu jung
# [date] 2025-03-06
# ─────────────────────────────────────────────────────────────────────────────
def is_ball_in_the_range(curr_pos, idx, arr, max_movement = 80, tolerance = 55, max_lookback=10):
    
    # 최근 유효 포인트 수집
    valid_points = []
    for i in range(idx - 1, max(idx - max_lookback - 1, -1), -1):
        pt = arr[i]
        if pt is not None and not np.isnan(pt).any():
            adjusted_pt = get_area_position(pt)
            valid_points.append((i, adjusted_pt))

    num_valid = len(valid_points)
    if num_valid == 0:
        return False  # 유효 포인트 없음

    # ✅ 유효 포인트 1개일 경우: 단순 거리 + 상단 이동만 확인
    if num_valid == 1:
        _, last_pos = valid_points[0]

        if curr_pos[1] >= last_pos[1]:
            return False

        dist = np.linalg.norm([curr_pos[0] - last_pos[0], curr_pos[1] - last_pos[1]])
        fd_log.info(f"[CHECK-1pt] curr: {curr_pos}, last: {last_pos}, dist: {dist:.2f}, threshold: {max_movement}")
        return dist <= max_movement

    # ✅ 2개 이상 → 방향성 + 속도 기반 예측
    (i1, (x1, y1)), (i2, (x2, y2)) = valid_points[1], valid_points[0]  # i1 < i2 (시간 순서)

    dt = i2 - i1
    if dt == 0:
        return False

    # 속도 계산 (단순 프레임 간 거리)
    dx = (x2 - x1) / dt
    dy = (y2 - y1) / dt

    # 방향성 확인: curr_pos가 v 방향으로 연장된 지점에 있는지
    v = (x2 - x1, y2 - y1)
    w = (curr_pos[0] - x2, curr_pos[1] - y2)
    dot = v[0]*w[0] + v[1]*w[1]
    if dot <= 0:
        fd_log.info(f"[❌ 방향 불일치] dot: {dot:.2f}, v: {v}, w: {w}")
        return False

    # 예상 위치 계산
    dt_future = idx - i2
    pred_x = x2 + dx * dt_future
    pred_y = y2 + dy * dt_future

    # 거리 체크
    dist = np.linalg.norm([curr_pos[0] - pred_x, curr_pos[1] - pred_y])
    fd_log.info(f"[예상 vs 현재] predict=({pred_x:.1f},{pred_y:.1f}), curr={curr_pos}, dist={dist:.2f}, tol={tolerance}")
    return dist <= tolerance

# ─────────────────────────────────────────────────────────────────────────────
# def reset_previous_data(frameindex, arr_ball):
# [owner] hongsu jung
# [date] 2025-03-16
# ─────────────────────────────────────────────────────────────────────────────
def reset_previous_data(arr_ball, frameindex):

    while(1):
        frameindex -= 1
        if(frameindex < 0):
            break
        arr_ball[frameindex] = (np.nan,np.nan)
    
# ─────────────────────────────────────────────────────────────────────────────
# def add_next_arr_item(arr):
# [owner] hongsu jung
# [date] 2025-02-15
# ─────────────────────────────────────────────────────────────────────────────
def add_next_arr_item(arr, end_pitching_analysis):
    # get valid range
    nEndCnt = find_last_frame(arr)
    valid_arr = arr[0:nEndCnt]
    valid_arr = np.array(valid_arr, dtype=float) 
    end_ball_tracking = len(valid_arr)
    
    add_count = end_pitching_analysis - nEndCnt - 1
    if(add_count > conf._ball_add_until_catcher):
        add_count = conf._ball_add_until_catcher

    if(add_count <= 0):
        return arr
    frame_index = end_ball_tracking - 1

    while(1):
        frame_index += 1        
        if(frame_index > end_ball_tracking + add_count - 1):
            break
        # expect_pos = expect_next_arr_value(valid_arr, frame_index)
        # 2025-05-21, kalman predict
        expect_pos = expect_next_position_kalman(valid_arr, frame_index)
        if(expect_pos == (-1,-1)):
            break
        # set previous hide ball
        valid_arr = np.pad(valid_arr, ((0, 1), (0, 0)), mode='constant', constant_values=-1)
        valid_arr[frame_index] = (expect_pos[0], expect_pos[1])

    valid_arr = np.pad(valid_arr, ((0, 1), (0, 0)), mode='constant', constant_values=-1)        
    # set data
    arr[0:nEndCnt+add_count] = valid_arr


# ─────────────────────────────────────────────────────────────────────────────
# def trim_array(arr, keep_count):
# [owner] hongsu jung
# [date] 2025-02-07
# ─────────────────────────────────────────────────────────────────────────────
def trim_array(arr, keep_count):
    """
    배열의 처음과 끝을 제외한 값들 중에서 보존할 `keep_count`개만 남기고 나머지는 제거하는 함수.
    :param arr: 입력 배열
    :param keep_count: 보존할 요소의 개수 (처음과 끝 제외한 중간 값 중에서)
    :return: 처음과 끝을 제외하고, 중간에서 `keep_count` 개만 보존한 배열
    """
    # 배열의 길이가 보존할 수 있는 개수보다 작으면 그대로 반환
    if len(arr) <= keep_count + 2:  # 처음과 끝을 제외한 요소들이 부족한 경우
        return arr
    
    # 처음과 끝을 제외한 중간 값들
    middle_values = arr[1:-1]  # 처음과 끝 제외한 배열
    keep_count -= 3
    # 중간에서 `keep_count`개만 남기기
    step = len(middle_values) // keep_count if keep_count > 0 else 0
    middle_values = middle_values[::step]  # `keep_count`개만 선택
    
    # 처음, 선택된 중간 값들, 끝을 합쳐서 반환
    return np.concatenate((arr[:1], middle_values, arr[-1:]))

# ─────────────────────────────────────────────────────────────────────────────c
# def get_start_end_time(arr):
# [owner] hongsu jung
# [date] 2025-02-14
# ─────────────────────────────────────────────────────────────────────────────
def get_start_end_time(arr):    
    
    nTot = len(arr)
    # Find the first valid position
    nStartCnt = 0
    for i, pos in enumerate(arr):
        if pos is not None and np.any(pos):  # Checks for [0, 0] and None
            if pos[0] != 0 or pos[1] != 0:
                nStartCnt = i
                break
    else:
        nStartCnt = nTot  # If no valid position is found

    # Find the last valid position
    nEndCnt = 0
    for i in range(nTot - 1, -1, -1):
        pos = arr[i]
        if pos is not None and not np.isnan(pos).any():
            if pos[0] == -1 and pos[1] == -1:
                nEndCnt = i - 1
                break
            if pos[0] != 0 or pos[1] != 0:
                nEndCnt = i
                break
    else:
        nEndCnt = 0  # If no valid ending is found

    return nStartCnt, nEndCnt

# ─────────────────────────────────────────────────────────────────────────────c
# def find_last_frame(arr):
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
def find_last_frame(arr):    
    # find last valid position
    idx = len(arr)
    while(1):
        idx -= 1
        if(idx < 0): break
        pos = arr[idx]
        if pos is not None and pos[0] == -1 and pos[1] == -1:
            break
    # debug
    # 2025-05-21
    if (idx == -1):
        idx = len(arr)
    return idx

# ─────────────────────────────────────────────────────────────────────────────c
# def fd_smooth_ball_tracking_cubic(arr):
# [owner] hongsu jung
# [date] 2025-02-14
# Bézier 곡선 계산 함수
# ─────────────────────────────────────────────────────────────────────────────
def fd_smooth_ball_tracking_cubic(arr):
    """
    입력된 배열에서 None이 아닌 좌표들만 추출하여 CubicSpline 보간을 수행하고,
    (-1, -1) 이전까지의 구간을 기준으로 arr 내부를 직접 수정합니다.

    Parameters:
        arr (list): [(x, y), None, None, ..., (-1, -1), ...]

    Returns:
        list: 보간된 결과가 삽입된 동일한 길이의 리스트
    """

    # 유효 좌표의 인덱스 수집
    valid_idx = []
    x_vals = []
    y_vals = []

    for i, pt in enumerate(arr):
        if pt is None:
            continue
        x, y = pt
        if x == -1 and y == -1:
            break
        valid_idx.append(i)
        x_vals.append(x)
        y_vals.append(y)

    if len(valid_idx) < 2:
        fd_log.info("보간 가능한 유효 좌표가 부족합니다.")
        return arr

    # 보간 함수 생성
    spline_x = CubicSpline(valid_idx, x_vals)
    spline_y = CubicSpline(valid_idx, y_vals)
    '''
    spline_x = CubicSpline(valid_idx, x_vals, bc_type='clamped')
    spline_y = CubicSpline(valid_idx, y_vals)
    '''

    # 보간 적용
    for i in range(valid_idx[0], valid_idx[-1] + 1):
        arr[i] = (int(spline_x(i)), int(spline_y(i)))

    return arr

def smooth_xy_array(arr, window=7, poly=2):
    if len(arr) < 3:
        return arr  # 너무 짧으면 그대로 반환
    
    n = len(arr)
    w = min(window, n if n % 2 == 1 else n - 1)
    if poly >= w:
        poly = w - 1
    
    arr = np.array(arr, dtype=float)
    x = arr[:, 0]
    y = arr[:, 1]

    x_smooth = savgol_filter(x, window_length=w, polyorder=poly)
    y_smooth = savgol_filter(y, window_length=w, polyorder=poly)

    return list(zip(np.round(x_smooth).astype(int), np.round(y_smooth).astype(int)))

# ─────────────────────────────────────────────────────────────────────────────c
# def fd_smooth_ball_tracking_cubic(arr):
# [owner] hongsu jung
# [date] 2025-05-14
# Apply 3rd-degree polynomial regression after normalization
# ─────────────────────────────────────────────────────────────────────────────
def fd_smooth_ball_tracking(arr, blending_alpha = 0.25):
    """ 공 궤적을 부드럽게 보정하는 함수 """
    
    # 🔹 유효한 데이터 필터링 (x, y만 추출)
    #valid_indices = [i for i, v in enumerate(arr) if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2]
    # 2025-03-25 hsj
    # except np.nan
    valid_indices = [i for i, v in enumerate(arr) if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).all()]    
    if len(valid_indices) < 2:
        fd_log.error("Error: 유효한 데이터가 부족합니다.")
        return
    
    nStartCnt = valid_indices[0]  # 첫 번째 유효한 데이터
    nEndCnt = valid_indices[-1]   # 마지막 유효한 데이터

    # 🔹 x, y 좌표만 추출 (나머지 데이터 제외)
    new_arr = np.array([(arr[i][0], arr[i][1]) for i in valid_indices], dtype=float)

    x = new_arr[:, 0]  # x 좌표
    y = new_arr[:, 1]  # y 좌표

    # 🔹 (-1, -1) 값이 존재하면 마지막 유효한 데이터 찾기
    last_valid_index = len(x) - 1
    for i in range(len(x) - 1, -1, -1):
        if not (x[i] == -1 and y[i] == -1):
            last_valid_index = i
            break

    # 🔹 (-1, -1) 값이 포함된 행 제거
    x_filtered = x[:last_valid_index + 1]
    y_filtered = y[:last_valid_index + 1]

    # 🔹 x 값 정규화 (평균 0, 표준편차 1)
    x_mean, x_std = np.mean(x_filtered), np.std(x_filtered)
    x_norm = (x_filtered - x_mean) / x_std if x_std != 0 else x_filtered  # 표준편차 0일 경우 대비

    # 🔹 전체 포인트를 회귀 적용
    if len(x_norm) >= 3:
        coeffs = np.polyfit(x_norm, y_filtered, 3)  # 3차 다항식 회귀
        poly3 = np.poly1d(coeffs)
        y_fit = poly3(x_norm)        
        y_smooth = (1 - blending_alpha) * y_filtered + blending_alpha * y_fit
    else:
        y_smooth = y_filtered  # 회귀 불가능한 경우 기존 값 유지

    # 🔹 정규화된 x 값을 다시 원래 값으로 변환
    x_smooth = x_norm * x_std + x_mean if x_std != 0 else x_filtered

    '''
    # 🔹 debug (그래프 출력)
    plt.figure(figsize=(8, 6))
    plt.plot(x_filtered, y_filtered, 'o', label='Original Points', markersize=8)
    plt.plot(x_smooth, y_smooth, '-', label='Smoothed Curve', color='blue')
    plt.legend()
    plt.xlabel("X")
    plt.ylabel("Y")
    plt.title("Smooth Curve (Excluding -1,-1)")
    
    # 🔹 🔥 마지막 포인트 확인용 출력
    fd_log.info("Before Smoothing: Last Original Point:", (x_filtered[-1], y_filtered[-1]))
    fd_log.info("After Smoothing: Last Smoothed Point:", (x_smooth[-1], y_smooth[-1]))
    
    plt.show()
    '''

    # 🔹 기존 개수 유지하면서 보정된 데이터 생성
    smoothed_pos = [(int(x_smooth[i]), int(y_smooth[i])) for i in range(len(x_smooth))]
    correct_arr = trim_array(smoothed_pos, nEndCnt - nStartCnt)

    # additional smmthing
    if(blending_alpha >= 1):
        correct_arr = smooth_xy_array(correct_arr)

    for i, idx in enumerate(valid_indices[: len(correct_arr)]):
        corrected_xy = correct_arr[i]
        original = arr[idx]

        if isinstance(original, np.ndarray):
            if original.shape[0] >= 2:
                original[:2] = corrected_xy
                arr[idx] = original
            else:
                arr[idx] = np.array(corrected_xy, dtype=original.dtype)
        elif isinstance(original, tuple):
            arr[idx] = tuple(corrected_xy)
        elif isinstance(original, list):
            if len(original) > 2:
                arr[idx] = list(corrected_xy) + original[2:]
            else:
                arr[idx] = list(corrected_xy)
        else:
            # fallback: 기본적으로 numpy array 반환
            arr[idx] = np.array(corrected_xy)

import numpy as np

def fd_smooth_ball_tracking2(arr, blending_alpha=0.25):
    """ 공 궤적을 부드럽게 보정하는 함수 (시작점과 끝점 고정 버전) """

    # 🔹 유효한 데이터 필터링 (x, y만 추출)
    valid_indices = [i for i, v in enumerate(arr)
                     if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).all()]
    if len(valid_indices) < 2:
        fd_log.error("Error: 유효한 데이터가 부족합니다.")
        return

    nStartCnt = valid_indices[0]
    nEndCnt = valid_indices[-1]

    # 🔹 x, y 좌표만 추출
    new_arr = np.array([(arr[i][0], arr[i][1]) for i in valid_indices], dtype=float)
    x = new_arr[:, 0]
    y = new_arr[:, 1]

    # 🔹 (-1, -1) 값이 포함된 뒤쪽은 제거
    last_valid_index = len(x) - 1
    for i in range(len(x) - 1, -1, -1):
        if not (x[i] == -1 and y[i] == -1):
            last_valid_index = i
            break

    x_filtered = x[:last_valid_index + 1]
    y_filtered = y[:last_valid_index + 1]

    # 🔹 x 정규화
    x_mean, x_std = np.mean(x_filtered), np.std(x_filtered)
    x_norm = (x_filtered - x_mean) / x_std if x_std != 0 else x_filtered

    # 🔹 회귀 및 혼합
    if len(x_norm) >= 3:
        coeffs = np.polyfit(x_norm, y_filtered, 3)
        poly3 = np.poly1d(coeffs)
        y_fit = poly3(x_norm)
        y_smooth = (1 - blending_alpha) * y_filtered + blending_alpha * y_fit
    else:
        y_smooth = y_filtered.copy()

    # 🔹 x 복원
    x_smooth = x_norm * x_std + x_mean if x_std != 0 else x_filtered.copy()

    # 🔹 [핵심] 시작점과 끝점 고정
    x_smooth[0] = x_filtered[0]
    x_smooth[-1] = x_filtered[-1]
    y_smooth[0] = y_filtered[0]
    y_smooth[-1] = y_filtered[-1]

    # 🔹 보정된 포인트 재구성
    smoothed_pos = [(int(x_smooth[i]), int(y_smooth[i])) for i in range(len(x_smooth))]
    correct_arr = trim_array(smoothed_pos, nEndCnt - nStartCnt)

    # 🔹 alpha == 1 이면 추가 smoothing 적용
    if blending_alpha >= 1:
        correct_arr = smooth_xy_array(correct_arr)

    # 🔹 원본 배열에 보정 적용
    for i, idx in enumerate(valid_indices[:len(correct_arr)]):
        corrected_xy = correct_arr[i]
        original = arr[idx]

        if isinstance(original, np.ndarray):
            if original.shape[0] >= 2:
                original[:2] = corrected_xy
                arr[idx] = original
            else:
                arr[idx] = np.array(corrected_xy, dtype=original.dtype)
        elif isinstance(original, tuple):
            arr[idx] = tuple(corrected_xy)
        elif isinstance(original, list):
            if len(original) > 2:
                arr[idx] = list(corrected_xy) + original[2:]
            else:
                arr[idx] = list(corrected_xy)
        else:
            arr[idx] = np.array(corrected_xy)


# ─────────────────────────────────────────────────────────────────────────────c
# hybrid_smooth_ball_tracking(arr):
# [owner] hongsu jung
# [date] 2025-05-19
# Hybrid trajectory correction combining polynomial regression (based on frame indices) and
# a Kalman filter, designed to handle bidirectional linear motion.
# ─────────────────────────────────────────────────────────────────────────────
def hybrid_smooth_ball_tracking(arr):
    
    valid_indices = [i for i, v in enumerate(arr)
                     if isinstance(v, (list, tuple, np.ndarray)) and len(v) >= 2 and not np.isnan(v).all()]
    if len(valid_indices) < 2:
        fd_log.info("Error: 유효한 데이터가 부족합니다.")
        return

    nStartCnt = valid_indices[0]
    nEndCnt = valid_indices[-1]

    start_point = arr[nStartCnt][:2]
    end_point = arr[nEndCnt][:2]

    # 🔹 프레임 인덱스와 좌표 추출
    frame_ids = []
    coords = []
    for idx in valid_indices:
        x, y = arr[idx][:2]
        if x == -1 and y == -1:
            continue
        frame_ids.append(idx)
        coords.append((x, y))

    if len(coords) < 3:
        fd_log.info("Error: 유효 좌표 부족")
        return

    coords = np.array(coords, dtype=float)
    frame_ids = np.array(frame_ids)

    # 🔹 프레임 인덱스를 기반으로 회귀
    f_norm = (frame_ids - frame_ids.mean()) / frame_ids.std()

    poly_x = np.poly1d(np.polyfit(f_norm, coords[:, 0], 3))
    poly_y = np.poly1d(np.polyfit(f_norm, coords[:, 1], 3))

    smooth_x = poly_x(f_norm)
    smooth_y = poly_y(f_norm)

    # 🔹 관측값 만들기
    obs = np.stack((smooth_x, smooth_y), axis=1)

    # 🔹 칼만 필터
    kf = KalmanFilter(dim_x=4, dim_z=2)
    dt = 1.0
    kf.F = np.array([[1, 0, dt, 0],
                     [0, 1, 0, dt],
                     [0, 0, 1, 0],
                     [0, 0, 0, 1]])
    kf.H = np.array([[1, 0, 0, 0],
                     [0, 1, 0, 0]])
    kf.P *= 10000.0
    kf.R *= 5000.0
    kf.Q = np.eye(4) * 5.0
    kf.x[:2] = np.array([[obs[0][0]], [obs[0][1]]])

    smoothed = []
    for z in obs:
        kf.predict()
        kf.update(z)
        smoothed.append((int(kf.x[0, 0]), int(kf.x[1, 0])))

    correct_arr = trim_array(smoothed, nEndCnt - nStartCnt)

    # 🔹 시작, 끝 보존
    
    if correct_arr:
        correct_arr[0] = tuple(map(int, start_point))
        correct_arr[-1] = tuple(map(int, end_point))
    
    
    for i, idx in enumerate(valid_indices[: len(correct_arr)]):
        if isinstance(arr[idx], np.ndarray) and len(arr[idx]) > 2:
            arr[idx][:2] = correct_arr[i]
        else:
            arr[idx] = correct_arr[i]

# ─────────────────────────────────────────────────────────────────────────────
# def reset_detect_area():
# [owner] hongsu jung
# [date] 2025-03-18
# ─────────────────────────────────────────────────────────────────────────────
def reset_detect_area():
    width = conf._input_width
    height = conf._input_height
    # select tracking object        
    match conf._type_target:
        case conf._type_baseball_pitcher:
            min_x = int(width   * conf._pitcher_target_left     )
            max_x = int(width   * conf._pitcher_target_right    )
            min_y = int(height  * conf._pitcher_target_top      )
            max_y = int(height  * conf._pitcher_target_bottom   )
            # set area zoom
            conf._detect_area_zoom = conf._detect_pitcher_area_zoom

        case conf._type_baseball_batter_RH:
            min_x = int(width   * conf._batter_target_left     )
            max_x = int(width   * conf._batter_target_right    )
            min_y = int(height  * conf._batter_target_top      )
            max_y = int(height  * conf._batter_target_bottom   )        
            # set area zoom
            conf._detect_area_zoom = conf._detect_batter_area_zoom

        case conf._type_baseball_batter_LH:            
            min_x = int(width   * (1 - conf._batter_target_right )  )   # reverse from right hand
            max_x = int(width   * (1 - conf._batter_target_left) )      # reverse from right hand
            min_y = int(height  * conf._batter_target_top       )
            max_y = int(height  * conf._batter_target_bottom    )
            # set area zoom
            conf._detect_area_zoom = conf._detect_batter_area_zoom

        case conf._type_baseball_hit        | \
             conf._type_baseball_hit_manual | \
             conf._type_baseball_hit_multi   :
            detect_width = width / conf._detect_zoom_ratio_width
            detect_height = height / conf._detect_zoom_ratio_height
            curr_pos_x = conf._hit_detect_init_x
            curr_pos_y = conf._hit_detect_init_y

            min_x = max(0, int(curr_pos_x - detect_width // 2))
            max_x = min(width, int(curr_pos_x + detect_width // 2))
            min_y = max(0, int(curr_pos_y - detect_height ))
            max_y = min(height, int(curr_pos_y + detect_height // 4))
            # set area zoom
            conf._detect_area_zoom = conf._detect_hit_area_zoom
    
    return [[min_x,min_y],[max_x,max_y]]

# ─────────────────────────────────────────────────────────────────────────────
# def move_detect_area():
# [owner] hongsu jung
# [date] 2025-04-01
# ─────────────────────────────────────────────────────────────────────────────
def move_detect_area(pos):
    width = conf._input_width
    height = conf._input_height
    curr_pos_x = pos[0] 
    curr_pos_y = pos[1] 

    detect_width = width / conf._detect_zoom_ratio_width
    detect_height = height / conf._detect_zoom_ratio_height

    if conf._type_target in (
                    conf._type_baseball_hit,
                    conf._type_baseball_hit_manual,
                    conf._type_baseball_hit_multi
                ):
        min_x = max(0, int(curr_pos_x - detect_width // 2))
        max_x = min(width, int(curr_pos_x + detect_width // 2))
        min_y = max(0, int(curr_pos_y - detect_width // 2))
        max_y = min(height, int(curr_pos_y + detect_width // 2))
        # set area zoom
        conf._detect_area_zoom = conf._detect_hit_area_zoom    
    return [[min_x,min_y],[max_x,max_y]]

# ─────────────────────────────────────────────────────────────────────────────    
# def frame_resize(frame, area = conf._detect_area):
# [owner] hongsu jung
# [date] 2025-03-23
# ─────────────────────────────────────────────────────────────────────────────
def frame_resize(frame, area, zoom):
    
    center_x_min = int(area[0][0])
    center_x_max = int(area[1][0])
    center_y_min = int(area[0][1])
    center_y_max = int(area[1][1])                
    cropped_frame = frame[center_y_min:center_y_max, center_x_min:center_x_max]

    if(zoom != 1):
        resized_frame = cv2.resize(cropped_frame, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    else:
        resized_frame = cropped_frame.copy()

    return resized_frame

# ─────────────────────────────────────────────────────────────────────────────
# def get_real_position(pos_x, pos_y):
# [owner] hongsu jung
# [date] 2025-03-18
# ─────────────────────────────────────────────────────────────────────────────
def get_real_position(pos):
    pos_real_x = int(pos[0]/conf._detect_area_zoom + conf._detect_area[0][0])
    pos_real_y = int(pos[1]/conf._detect_area_zoom + conf._detect_area[0][1])
    return (pos_real_x, pos_real_y)

# ─────────────────────────────────────────────────────────────────────────────
# def get_real_position(pos_x, pos_y):
# [owner] hongsu jung
# [date] 2025-03-18
# ─────────────────────────────────────────────────────────────────────────────
def get_area_position(pos):
    pos_area_x = int((pos[0] - conf._detect_area[0][0])* conf._detect_area_zoom)
    pos_area_y = int((pos[1] - conf._detect_area[0][1])* conf._detect_area_zoom)
    return (pos_area_x, pos_area_y)

# ─────────────────────────────────────────────────────────────────────────────c
# def set_ball_pos
# [owner] hongsu jung
# [date] 2025-03-23
# ─────────────────────────────────────────────────────────────────────────────
def set_ball_pos(arr_ball, frame_index, new_pos):    
    prev_ball = arr_ball[frame_index-1]
    if(prev_ball is None):
        return False
    # Calculate the differences in the x and y coordinates
    delta_x = abs(new_pos[0] - prev_ball[0])
    delta_y = abs(new_pos[1] - prev_ball[1])
    
    # Ensure the differences do not exceed 10
    if delta_x > conf._hit_ball_max_movement or delta_y > conf._hit_ball_max_movement:
        fd_log.info("The difference in position exceeds 10. Position not updated.")
        return False  # Return the previous ball position if the difference is too large
    else:
        # Update and return the new position if the difference is within the allowed range
        arr_ball[frame_index] = new_pos
        
    return True    

# ─────────────────────────────────────────────────────────────────────────────
# def expect_next_position_kalman(arr, frame_index):
# [owner] hongsu jung
# [date] 2025-03-06
# ─────────────────────────────────────────────────────────────────────────────
def expect_next_position_kalman(arr, frame_index):
    
    # ─────────────────────────────────────────────────────────────────────────────    
    # Kalman Filter Initiation
    # ─────────────────────────────────────────────────────────────────────────────    
    kalman = cv2.KalmanFilter(4, 2)
    dt = 1.0
    kalman.transitionMatrix = np.array([[1, 0, dt, 0],
                                        [0, 1, 0, dt],
                                        [0, 0, 1, 0 ],
                                        [0, 0, 0, 1 ]], dtype=np.float32)
    kalman.measurementMatrix = np.array([[1, 0, 0, 0],
                                         [0, 1, 0, 0]], dtype=np.float32)
    kalman.processNoiseCov = np.eye(4, dtype=np.float32) * 1e-4
    kalman.measurementNoiseCov = np.eye(2, dtype=np.float32) * 1e-2
    kalman.errorCovPost = np.eye(4, dtype=np.float32)
    
    kalman_initialized = False
    
    nStartCnt, nEndCnt = get_start_end_time(arr)  
    trajectory = arr[nStartCnt:nEndCnt+1]

    # 유효 좌표 필터링 (None, NaN, (-1, -1) 제거)
    filtered_pts = []
    for pt in trajectory:
        if pt is None:
            continue
        if isinstance(pt, (list, tuple, np.ndarray)) and len(pt) >= 2:
            if pt[0] != -1 and pt[1] != -1 and not np.isnan(pt[0]) and not np.isnan(pt[1]):
                filtered_pts.append(np.array(pt, dtype=np.float32))

    if len(filtered_pts) < 2:
        return None  # 예측 불가

    # 🔹 마지막 5개만 사용
    recent_pts = filtered_pts[-5:]

    # 초기 상태 설정 (마지막 5개 중 앞 두 점 기반 속도 추정)
    p0, p1 = recent_pts[0], recent_pts[1]
    vx, vy = p1 - p0
    kalman.statePost = np.array([[p1[0]], [p1[1]], [vx], [vy]], dtype=np.float32)

    kalman.predict()
    kalman.correct(np.array([[p1[0]], [p1[1]]], dtype=np.float32))

    for pt in recent_pts[2:]:
        kalman.predict()
        kalman.correct(np.array([[pt[0]], [pt[1]]], dtype=np.float32))

    prediction = kalman.predict()
    pred_x, pred_y = prediction[0][0], prediction[1][0]

    return (int(pred_x), int(pred_y))

# ─────────────────────────────────────────────────────────────────────────────    
# def reposition_array(arr,frame_index):
# [owner] hongsu jung
# [date] 2025-02-14
# 마지막 데이터를 기준으로 frame_index를 보정
# ─────────────────────────────────────────────────────────────────────────────
def reposition_array(arr,frame_index):

    # 현재 배열의 크기
    nTot = len(arr)    
    # 배열 크기가 0일 경우 처리
    if nTot == 0:
        return arr
    if conf._detect_fail_cnt == 0:
        return arr
    
    nStartCnt, nEndCnt = get_start_end_time(arr)  
    new_arr = arr[nStartCnt:nEndCnt+1]
    new_arr = np.array(new_arr, dtype=float) 
    nChange = conf._detect_fail_cnt
    new_arr_cnt = len(new_arr)
    last_index = new_arr_cnt - 1
    first_index = last_index - nChange - 1

    last_pos = new_arr[last_index]
    first_pos = new_arr[first_index]

    move_x_unit = (last_pos[0] - first_pos[0])/(last_index - first_index)
    move_y_unit = (last_pos[1] - first_pos[1])/(last_index - first_index)

    index = first_index
    while(1):
        index += 1
        if(index >= last_index):
            break
        new_arr[index][0] = first_pos[0] + move_x_unit * (index - first_index)
        new_arr[index][1] = first_pos[1] + move_y_unit * (index - first_index)        

    arr[nStartCnt:nEndCnt+1] = new_arr
    # Detect 실패 카운트 초기화
    conf._detect_fail_cnt = 0
    return arr
    
# ─────────────────────────────────────────────────────────────────────────────c
# def organize_array(arr,frame_index):
# [owner] hongsu jung
# [date] 2025-02-14
# ─────────────────────────────────────────────────────────────────────────────
def get_total_frame_count(video_path: str) -> int:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        fd_log.error(f"❌ Cannot open video file: {video_path}")
        return 0
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()
    return frame_count

# ─────────────────────────────────────────────────────────────────────────────
# def detect_ball_in_batch(cap, type_target):
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
clicked_pos = [None]  # 전역처럼 쓸 변수
right_click_flag = [False]
def mouse_callback(event, x, y, flags, param):
    if event == cv2.EVENT_LBUTTONDOWN:  # 좌클릭
        fd_log.info(f"🖱️ Left Click at: ({x}, {y})")
        clicked_pos[0] = (x, y)
    elif event == cv2.EVENT_RBUTTONDOWN:  # 우클릭
        fd_log.info(f"🖱️ Right Click detected")
        right_click_flag[0] = True

# ─────────────────────────────────────────────────────────────────────────────
# def draw_ball_on_frame(cap, type_target):
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
def draw_ball_on_frame(frame, ball_pos, predicted_center, confidence, b_detect = False):
    # draw predict                        
    if 'predicted_center' in locals():
        pred_ball = get_area_position((predicted_center[0], predicted_center[1]))
        cv2.circle(frame, pred_ball, 15, (255, 255, 0), 5)

    if ball_pos is None:
        return
    cx, cy, w, h = ball_pos
    if not any(np.isnan([cx, cy])):        
        # 텍스트 위치를 원 위쪽으로 이동 (y축 방향 음수)        
        text_position = (cx - 50, cy - 20)  # cx 기준으로 왼쪽, cy 기준으로 위쪽으로 띄움

        if b_detect:
            text = f"select C[{confidence:.2f}], x={cx}, y={cy}, w={w}, h={h}"
            cv2.putText(frame, text, text_position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            cv2.circle(frame, (cx, cy), 10, (0, 255, 255), 3)
        else:
            text = f"non select C[{confidence:.2f}], x={cx}, y={cy}, w={w}, h={h}"
            cv2.putText(frame, text, text_position, cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.circle(frame, (cx, cy), 10, (0, 0, 255), 3)

    
# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_pitcher_multi(pkl_list):
# [owner] hongsu jung
# [date] 2025-04-01
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_multi_pitcher(pkl_list):
    ball_arrays = []
    for pitch_type, speed, frame, path in pkl_list:
        arr_ball = load_array_file(path)
        ball_arrays.append(arr_ball)

    return True, ball_arrays

click_type = [None]      # 'manual', 'skip', 'auto'
selected_point = [(np.nan, np.nan)]
def create_mouse_callback(selected_point, click_type):
    def mouse_callback(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            selected_point[0] = (x, y)
            click_type[0] = 'left'
        elif event == cv2.EVENT_MBUTTONDOWN:
            click_type[0] = 'middle'
        elif event == cv2.EVENT_RBUTTONDOWN:
            selected_point[0] = (np.nan, np.nan)
            click_type[0] = 'right'
    return mouse_callback

def detect_baseball_trajectory(frames, roi_coords, zoom_factor=3, debug=False):
    """
    야구공 움직임을 프레임에서 감지하고 궤적을 반환합니다.

    Parameters:
        frames (list): BGR 프레임 리스트
        roi_coords (tuple): (x1, y1, x2, y2) - 초기 탐색 영역
        zoom_factor (int): ROI 확대 배율 (기본값 3배)
        debug (bool): 디버깅 모드 (True시 시각화)

    Returns:
        trajectory (list): [(frame_idx, x, y)] 공 중심 좌표 목록
    """

    x1_init, y1_init, x2_init, y2_init = roi_coords
    trajectory = []
    prev_roi_zoomed = None
    is_first_detect = False
    is_first_move_roi = True
    prev_candidates = []
    curr_candidates = []
    window_name = "[Curr]Motion Detected"
    window_name2 = "[Prev] Motion Detected"
        

    for idx, frame in enumerate(frames):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        curr_roi = gray[y1_init:y2_init, x1_init:x2_init]
        curr_roi_zoomed = cv2.resize(curr_roi, None, fx=zoom_factor, fy=zoom_factor, interpolation=cv2.INTER_NEAREST)

        if prev_roi_zoomed is None:
            prev_roi_zoomed = curr_roi_zoomed
            continue

        # 차이 계산
        diff = cv2.absdiff(prev_roi_zoomed, curr_roi_zoomed)
        _, thresh = cv2.threshold(diff, 3, 255, cv2.THRESH_BINARY)

        # 윤곽선 추출
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        frame_copy = cv2.cvtColor(curr_roi_zoomed, cv2.COLOR_GRAY2BGR)

        # 윤곽선 정보 수집
        curr_candidates = []
        for idx_detect, cnt in enumerate(contours):
            area = cv2.contourArea(cnt)
            
            if 40 * zoom_factor < area < 100 * zoom_factor:
                x, y, w, h = cv2.boundingRect(cnt)
                if(is_first_detect == False):
                    prev_candidates.append((x, y, w, h, area))
                else:
                    curr_candidates.append((x, y, w, h, area))
                cv2.rectangle(frame_copy, (x, y), (x + w, y + h), (0, 255, 0), 2)

                text = f"x,y({x},{y}),w,h({w},{h}) Area:({area})"
                fd_log.info(f"[{idx}][{idx_detect}] {text}")
                cv2.putText(frame_copy, text, (x, y - 10), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (0, 255, 0), 1, cv2.LINE_AA)
                
        # check 1st detection
        if(is_first_detect == False):
            is_first_detect = True
            frame_prev = frame_copy.copy()
            continue

        cv2.drawContours(frame_copy, contours, -1, (255, 0, 0), 1)
        cv2.putText(frame_copy, f"Frame: {idx}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
        cv2.imshow(window_name, frame_copy)
        key = cv2.waitKey(0)
        
        found = False
        # 공으로 간주할 영역 찾기: 이전 프레임에는 있었지만, 현재 프레임에는 없는 경우
        for prev_x, prev_y, prev_w, prev_h, prev_area in prev_candidates:
            match_found = False
            for curr_x, curr_y, curr_w, curr_h, curr_area in curr_candidates:
                if abs(curr_x - prev_x) < 3 and abs(curr_y - prev_y) < 3:
                    match_found = True
                    break
            if not match_found:
                # 이전엔 있었는데 현재 없다면 -> 이동한 공으로 간주
                cv2.rectangle(frame_prev, (prev_x, prev_y), (prev_x + prev_w, prev_y + prev_h), (0, 0, 255), 2)
                real_x = x1_init + prev_x // zoom_factor
                real_y = y1_init + prev_y // zoom_factor
                trajectory.append((idx, real_x, real_y))

        if debug:
            prev_idx = idx - 1
            cv2.putText(frame_prev, f"Frame: {prev_idx}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
            cv2.imshow(window_name2, frame_prev)
            key = cv2.waitKey(0)
            if key == 27:
                break

        prev_candidates = curr_candidates
        frame_prev = frame_copy.copy()

        # roi reposition        
        should_move_roi, new_center = check_movement(trajectory)
        if should_move_roi and new_center:
            real_x, real_y = new_center            
            if(is_first_move_roi == True):
                # ROI 이동: 새 중심 기준 재계산
                is_first_move_roi = False
                half_w = (x2_init - x1_init) // 5
                half_h = (y2_init - y1_init) 
            else:
                # ROI 이동: 새 중심 기준 재계산
                half_w = (x2_init - x1_init) // 2
                half_h = (y2_init - y1_init) // 2

            x1_init = int(max(real_x - half_w, 0)       )
            x2_init = int(x1_init + half_w              )
            y1_init = int(max(real_y - 2 * half_h, 0)   )
            y2_init = int(y1_init + half_h / 2          )
            
            fd_log.info(f"🔄 ROI 이동 → 새로운 중심: ({real_x}, {real_y}), ROI: ({x1_init},{y1_init})~({x2_init},{y2_init})")

            prev_roi_zoomed = None  # ROI가 바뀌었으므로 리셋
            is_first_detect = False
            prev_candidates = []
            trajectory = []  # 기존 trajectory 초기화하여 재탐색

    cv2.destroyWindow(window_name)
    cv2.destroyWindow(window_name2)
    return trajectory

def check_movement(trajectory, angle_thresh=30, min_points=3):
    if len(trajectory) < min_points + 1:
        return False, None

    motion_vectors = []
    for i in range(1, len(trajectory)):
        idx_prev, x_prev, y_prev = trajectory[i - 1]
        idx_curr, x_curr, y_curr = trajectory[i]

        dx = x_curr - x_prev
        dy = y_curr - y_prev
        distance = math.hypot(dx, dy)
        angle_deg = math.degrees(math.atan2(-dy, dx))

        motion_vectors.append({
            'from': (x_prev, y_prev),
            'to': (x_curr, y_curr),
            'frame_from': idx_prev,
            'frame_to': idx_curr,
            'angle_deg': angle_deg,
            'distance': distance
        })

        fd_log.info(f"📄[{idx_prev}->{idx_curr}] Δx: {dx}, Δy: {dy}, dist: {distance:.1f}, angle: {angle_deg:.1f}°")

    # 이상치 제거 + 연속성 판단
    cleaned_vectors = [motion_vectors[0]]
    for i in range(1, len(motion_vectors)):
        prev_angle = cleaned_vectors[-1]['angle_deg']
        curr_angle = motion_vectors[i]['angle_deg']
        diff = abs(curr_angle - prev_angle) % 360
        if diff > 180:
            diff = 360 - diff

        if diff < angle_thresh:
            cleaned_vectors.append(motion_vectors[i])
        else:
            fd_log.warning(f"⚠️ 이상치 제거: [{motion_vectors[i]['frame_from']}→{motion_vectors[i]['frame_to']}] angle diff = {diff:.1f}°")

    if len(cleaned_vectors) >= min_points:
        last_point = cleaned_vectors[-1]['to']
        return True, last_point
    else:
        fd_log.error("❌ 방향성 있는 연속 움직임이 충분하지 않음.")
        return False, None

# ─────────────────────────────────────────────────────────────────────────────
# def add_smooth_tracking_position(ball_pos_arr, max_retry=5)
# [owner] hongsu jung
# [date] 2025-07-07
# ─────────────────────────────────────────────────────────────────────────────
def add_smooth_tracking_position(ball_pos_arr, max_retry=5):
    retries = 0
    arr_ball = None

    def compare_positions(original, smoothed):
        for o, s in zip(original, smoothed):
            if o is None:
                continue
            if np.isnan(o[0]) or np.isnan(o[1]):
                continue
            if o != s:
                return False
        return True

    while retries < max_retry:
        arr_ball = fd_smooth_ball_tracking_cubic(ball_pos_arr)
        if compare_positions(ball_pos_arr, arr_ball):
            fd_log.info(f"✅ Smoothing succeeded at attempt {retries + 1}")
            return arr_ball
        fd_log.info(f"🔁 Retry smoothing... attempt {retries + 1}")
        retries += 1

    fd_log.warning("⚠️ Max retries reached. Returning last result.")
    return arr_ball

# ─────────────────────────────────────────────────────────────────────────────
# def adaptive_moving_average_fixed_ends(arr, max_window=10)
# [owner] hongsu jung
# [date] 2025-07-07
# ─────────────────────────────────────────────────────────────────────────────
def adaptive_moving_average_fixed_ends(arr, max_window=10):
    """
    적응형 이동 평균을 적용하되, 첫 번째와 마지막 값은 고정합니다.
    
    Parameters:
        arr (array-like): 입력 1D 배열 (예: x 좌표 또는 y 좌표)
        max_window (int): 최대 윈도우 크기
    
    Returns:
        np.ndarray: 부드럽게 처리된 배열
    """
    arr = np.array(arr)
    smoothed = []
    n = len(arr)
    
    for i in range(n):
        if i == 0 or i == n - 1:
            # 시작점과 끝점은 그대로 유지
            smoothed.append(arr[i])
        else:
            # 윈도우 크기 조정: 경계 근처에서는 자동 축소
            w = min(max_window, i, n - i - 1)
            window_vals = arr[i - w : i + w + 1]
            smoothed.append(np.mean(window_vals))
    
    return np.array(smoothed)

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_swing_poses():
# [owner] hongsu jung
# [date] 2025-04-29
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_swing_poses():

    # reset detect count
    conf._detect_frame_count = 0
    conf._detect_success_count = 0

    # set thread
    thread_front_detect = threading.Thread(target=fd_detect_swing_pose, args=(conf._file_type_front,))
    thread_side_detect  = threading.Thread(target=fd_detect_swing_pose, args=(conf._file_type_side,))
    if conf._multi_ch_analysis >= 3:
        thread_back_detect  = threading.Thread(target=fd_detect_swing_pose, args=(conf._file_type_back,))
    
    thread_front_detect.start()    
    thread_side_detect.start()    
    if conf._multi_ch_analysis >= 3:
        thread_back_detect.start()

    thread_front_detect.join()
    thread_side_detect.join()    
    if conf._multi_ch_analysis >= 3:
        thread_back_detect.join()
    
    return True

def smooth_keypoints_savgol_list(keypoints_pose_list, window_size=11, polyorder=2):
    """
    keypoints_pose_list: list[dict[joint_name: (x, y, z)] or invalid]
    return: same structure with smoothed values
    """
    total_frames = len(keypoints_pose_list)

    # ✅ 유효한 첫 dict 탐색 (joint 이름 추출용)
    valid_frame = next((f for f in keypoints_pose_list if isinstance(f, dict)), None)
    if valid_frame is None:
        fd_log.error("❗ No valid keypoint frame found.")
        return [{} for _ in range(total_frames)]

    joint_names = list(valid_frame.keys())

    smoothed_pose = [{} for _ in range(total_frames)]

    for joint in joint_names:
        xs, ys, zs = [], [], []

        for frame in keypoints_pose_list:
            if isinstance(frame, dict):
                x, y, z = frame.get(joint, (0, 0, 0))
            else:
                x, y, z = 0, 0, 0  # 잘못된 프레임 무시 (기본값 삽입)
            xs.append(x)
            ys.append(y)
            zs.append(z)

        # Savitzky-Golay 필터 적용
        if total_frames < window_size:
            xs_smooth, ys_smooth, zs_smooth = xs, ys, zs
        else:
            xs_smooth = savgol_filter(xs, window_size, polyorder)
            ys_smooth = savgol_filter(ys, window_size, polyorder)
            zs_smooth = savgol_filter(zs, window_size, polyorder)

        for i in range(total_frames):
            smoothed_pose[i][joint] = (xs_smooth[i], ys_smooth[i], zs_smooth[i])

    return smoothed_pose

def process_result_batch(results):
    batch_keypoints = []
    for res in results:
        if res is None or not hasattr(res, 'keypoints') or not hasattr(res, 'boxes'):
            batch_keypoints.append(None)
            continue

        if res.keypoints is None or res.boxes is None:
            batch_keypoints.append(None)
            continue

        boxes = res.boxes.xyxy.cpu().numpy()
        keypoints = res.keypoints.xy

        if keypoints.shape[0] == 0:
            batch_keypoints.append(None)
            continue

        areas = [(x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in boxes]

        # 2025-08-12
        if len(areas) == 0:
            # fd_log.info("All boxes have non-positive area; skipping frame.")
            continue

        largest_idx = areas.index(max(areas))
        
        keypoints_xy = keypoints[largest_idx].cpu().numpy()
        updated_keypoints = {}
        for i, name in enumerate(yolo_landmark_names):
            x, y = keypoints_xy[i]
            updated_keypoints[name] = (float(x), float(y), 0.0)
        batch_keypoints.append(updated_keypoints)

    return batch_keypoints
    
def detect_2d_keypoints_yolo_batch(images, yolo_pose, batch_size=16):
    keypoints_list = []
    # 🔁 배치 단위로 모델에 입력
    for i in range(0, len(images), batch_size):
        batch = images[i:i + batch_size]

        try:
            results = yolo_pose(batch, stream=False, verbose=False)
            keypoints_list.extend(process_result_batch(results))
        except MemoryError:
            fd_log.error(f"❌ MemoryError: falling back to single-frame inference [{i} ~ {i+batch_size}]")
            for img in batch:
                try:
                    result = yolo_pose(img, stream=False, verbose=False)
                    keypoints_list.extend(process_result_batch(result))
                except Exception as e:
                    fd_log.warning(f"⚠️ Failed to process single image in fallback: {e}")
                    keypoints_list.append(None)

    return keypoints_list

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_swing_poses():
# [owner] hongsu jung
# [date] 2025-04-29
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_swing_pose(output_type):
    
    #yolo_pose = conf._yolo_model_pose_n          
    yolo_pose = conf._yolo_model_pose_s          
    #yolo_pose = conf._yolo_model_pose_m          
    #yolo_pose = conf._yolo_model_pose_l          

    match output_type:
        case conf._file_type_front: frames = conf._frames_front            
        case conf._file_type_side:  frames = conf._frames_side            
        case conf._file_type_back:  frames = conf._frames_back
        case _:
            return False, None

    frame_count = len(frames)
    if frame_count < 1:
        return False, None
    keypoints_pose = [None] * frame_count

    # 🔧 설정: 프레임 추론 간격
    stride = getattr(conf, '_pose_detect_stride', 1)
    sampled_indices = list(range(0, frame_count, stride))
    sampled_frames = [frames[i] for i in sampled_indices]

    fd_log.info(f"\n🚀 [YOLOv8][{output_type}] Pose detection start: {len(sampled_frames)} frames (stride={stride})")
    _t_start = time.perf_counter()

    # ✅ 배치 추론
    keypoint_results = detect_2d_keypoints_yolo_batch(sampled_frames, yolo_pose)
    # 🔁 결과 보간 및 할당
    keypoints_prev = None
    for i, idx in enumerate(sampled_indices):
        key = keypoint_results[i]
        keypoints_pose[idx] = key or keypoints_prev
        if key is not None:
            keypoints_prev = key
            conf._detect_success_count += 1
        conf._detect_frame_count += 1
        print(f"🦾 [YOLOv8][{output_type}] Progress: {int(i/len(sampled_frames)*100)}%", end='')

    # ⏳ 보간: skip된 프레임은 이전 값으로 채움
    for i in range(frame_count):
        if keypoints_pose[i] is None:
            keypoints_pose[i] = keypoints_prev

    _t_end = time.perf_counter()
    fd_log.info(f"\🕒[Player Motion Detection][{output_type}] {(1000 * (_t_end - _t_start)):,.2f} ms")

    # smooth
    smoothed_keypoints_pose = smooth_keypoints_savgol_list(keypoints_pose, window_size=19, polyorder=2)
    
    # set pose data
    match output_type:
        case conf._file_type_front:
            conf._keypoint_pose_front = smoothed_keypoints_pose.copy()
        case conf._file_type_side:
            conf._keypoint_pose_side = smoothed_keypoints_pose.copy()
        case conf._file_type_back:
            conf._keypoint_pose_back = smoothed_keypoints_pose.copy()
    return True

# ─────────────────────────────────────────────────────────────────────────────
# def get_video_scale_on_player(file, is_show_video = False):
# [owner] hongsu jung
# ─────────────────────────────────────────────────────────────────────────────
def fd_get_video_on_player(video_buffer, file_type):    
    
    zoom_scale = 1.2
    center_x = 0
    center_y = 0
    height = conf._input_height
    width = conf._input_width
    
    is_detect, largest_person_box = find_player_location(video_buffer)
    if not is_detect or largest_person_box is None:
        fd_log.error("[Error] fail to detect object ")
        return is_detect, zoom_scale, center_x, center_y
    else:
        fd_log.info(largest_person_box)
    
    (x, y, w, h) = largest_person_box
    # 확장 설정
    expanded_top = h * 0.7
    expanded_bottom = h * 0.7
    expanded_w = 0

    if(file_type == conf._file_type_side):
        if(conf._swing_right_hand == True):
            expanded_w = w * 0.3
        else:
            expanded_w = -1 * w * 0.3
    
    # 박스 조정
    adj_x = max(0, x + expanded_w)
    adj_y = max(0, y - (expanded_top+expanded_bottom)/2)
    adj_w = min(width - adj_x, w + expanded_w)
    adj_h = min(height - adj_y, h + expanded_top + expanded_bottom)

    zoom_scale_w = width / adj_w
    zoom_scale_h = height / adj_h
    zoom_scale = min(zoom_scale_w, zoom_scale_h)
    
    # 중심 재조정
    original_center_x = x 
    original_center_y = y 
    relative_center_shift_y = (expanded_top - expanded_bottom) / 2
    
    center_x = original_center_x + expanded_w
    center_y = original_center_y - relative_center_shift_y

    return is_detect, zoom_scale, center_x, center_y

def extract_first_frame(video_buffer):
    container = av.open(video_buffer)
    video_stream = next(s for s in container.streams if s.type == 'video')
    for frame in container.decode(video_stream):
        img = frame.to_ndarray(format='bgr24')
        container.close()
        return img
    container.close()
    return None

# ─────────────────────────────────────────────────────────────────────────────
# def find_player_location(file, is_show_video = False):
# [owner] hongsu jung
# ─────────────────────────────────────────────────────────────────────────────
def find_player_location(video_buffer):
    # 1. 첫 프레임 추출
    frame = extract_first_frame(video_buffer)
    if frame is None:
        return None

    height, width, _ = frame.shape

    # 가로: 중앙 기준 1/3
    x_min = width // 3
    x_max = (width * 2) // 3

    # 세로: 중앙 기준 1/2
    y_min = height // 4
    y_max = (height * 3) // 4

    # 2. YOLOv8로 사람 검출
    yolo_model = conf._yolo_model_x
    results = yolo_model.predict(frame, conf=0.3, classes=[0])  # 클래스 0: 'person'

    if len(results) == 0 or len(results[0].boxes) == 0:
        return False, None

    # 3. 조건 영역 안에 있는 사람 중 가장 큰 사람 선택
    max_area = 0
    best_box = None

    for box in results[0].boxes.xyxy.cpu().numpy():
        x1, y1, x2, y2 = box
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2

        # 중앙 1/3 영역 안에 있는지 확인
        if not (x_min <= center_x <= x_max and y_min <= center_y <= y_max):
            continue  # 조건에 맞지 않으면 스킵

        width_box = x2 - x1
        height_box = y2 - y1
        area = width_box * height_box

        if area > max_area:
            max_area = area
            best_box = (int(center_x), int(center_y), int(width_box), int(height_box))

    # debug
    '''
    if best_box is not None:
        frame_draw = frame.copy()
        x1 = int(best_box[0] - best_box[2]/2)
        y1 = int(best_box[1] - best_box[3]/2)
        x2 = int(best_box[0] + best_box[2]/2)
        y2 = int(best_box[1] + best_box[3]/2)
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        
        cv2.rectangle(frame_draw, (x1,y1), (x2,y2), (0, 255, 0), 2)        
        filename = f"detected_{uuid.uuid4().hex}.jpg"
        cv2.imwrite(os.path.join("./", filename), frame_draw)
    '''
    if best_box is not None:
        return True, best_box
    else:
        return False, None













# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_pitcher():
# [owner] hongsu jung
# [date] 2025-03-06
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_pitcher():

    # YOLOv8
    # 2025-03-28 
    # yolo_model = conf._yolo_model_s
    # yolo_model = conf._yolo_model_m
    # yolo_model = conf._yolo_model_l
    yolo_model = conf._yolo_model_x

    # ─────────────────────────────────────────────────────────────────────────────####################
    # Kalman Filter Initiation
    # ─────────────────────────────────────────────────────────────────────────────####################    
    dt = 1.0
    kalman = cv2.KalmanFilter(4, 2)

    '''
    6차 벡터
    # 상태 전이 행렬
    kalman.transitionMatrix = np.array([
        [1, 0, dt, 0, 0.5*dt**2, 0],
        [0, 1, 0, dt, 0, 0.5*dt**2],
        [0, 0, 1, 0, dt, 0],
        [0, 0, 0, 1, 0, dt],
        [0, 0, 0, 0, 1, 0],
        [0, 0, 0, 0, 0, 1]
    ], dtype=np.float32)
 
    # 측정 행렬
    kalman.measurementMatrix = np.array([
        [1, 0, 0, 0, 0, 0],
        [0, 1, 0, 0, 0, 0]
    ], dtype=np.float32)
 
    # 잡음 공분산
    kalman.processNoiseCov = np.diag([1e-4]*6).astype(np.float32)
    kalman.measurementNoiseCov = np.eye(2, dtype=np.float32) * 0.01
    kalman.errorCovPost = np.eye(6, dtype=np.float32)  # ✅ 크기 수정됨
    '''

    kalman.transitionMatrix = np.array([[1, 0, dt, 0],
                                        [0, 1, 0, dt],
                                        [0, 0, 1, 0 ],
                                        [0, 0, 0, 1 ]], dtype=np.float32)
    kalman.measurementMatrix = np.array([[1, 0, 0, 0],
                                         [0, 1, 0, 0]], dtype=np.float32)
    kalman.processNoiseCov = np.eye(4, dtype=np.float32) * 1e-4
    kalman.measurementNoiseCov = np.eye(2, dtype=np.float32) * 1e-4
    kalman.errorCovPost = np.eye(4, dtype=np.float32)
    

    last_center = None
    velocity = np.array([0, -20], dtype=np.float32)
    kalman_initialized = False

    frames = conf._frames_curr
    frame_count = len(frames)
    if(frame_count < 1):
        return False, None
    
    conf._detect_frame_count = frame_count
    ball_pos = [None] * frame_count
    detect_ball_frame = 0
    error_detect_ball_frame = 0
    percent_progress = 0
    conf._detect_area = reset_detect_area()
    conf._after_detected = False

    predict_start_idx = 10
    batch_imgs = []
    frame_indices = []
    batch_size = (abs(conf._pitcher_detect_prev_frame) + abs(conf._pitcher_detect_post_frame) + 1) // 3
    window_name = "[Detect][PIT] Ball Detection"

    # ─────────────────────────────────────────────────────────────────────────────#
    # start search bat/ball position
    # ─────────────────────────────────────────────────────────────────────────────#
    for idx, img in enumerate(frames):
        frame_h, frame_w = img.shape[:2]
        yolo_frame = frame_resize(img, conf._detect_area, conf._detect_area_zoom)
        
        batch_imgs.append(yolo_frame)
        frame_indices.append(idx)
                
        if len(batch_imgs) >= batch_size or idx == frame_count - 1:
            results = yolo_model(batch_imgs, classes=[conf._yolo_class_id_baseball], conf=conf._yolo_detect_pitcher_confidence, imgsz=conf._yolo_detect_pitcher_size, iou=0.4, verbose=False)
            for b_idx, result in enumerate(results):
                idx = frame_indices[b_idx]
                yolo_frame = batch_imgs[b_idx]
                mem_img = frames[idx]  # 원본 프레임

                percent_progress = int(idx / frame_count * 100)
                fd_log.info(f"🎯[PIT][{idx}] Detect Batting Position Progress: {percent_progress}% detect ball: {detect_ball_frame}/{frame_count}")

                ball_detected = False
                max_confidence = 0
                best_ball_pos = (np.nan, np.nan)

                # every time predict
                prediction = kalman.predict()
                if kalman_initialized:
                    x_pred = int(prediction[0, 0])
                    y_pred = int(prediction[1, 0])      
                    predicted_center = (x_pred, y_pred)            
                else:
                    if np.isnan(prediction).any():
                        x_pred, y_pred = 0, 0
                    else:
                        x_pred = int(prediction[0, 0])
                        y_pred = int(prediction[1, 0])
                    predicted_center = (np.array(last_center) + velocity).astype(np.int32) if last_center is not None else None

                pred_ball = get_area_position((x_pred, y_pred))  
                if conf._detection_viewer:                                                     
                    cv2.circle(yolo_frame, pred_ball, 15, (255, 255, 0), 3)       
                
                boxes = result.boxes
                for box in boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    confidence = box.conf[0].item()
                    w, h = x2 - x1, y2 - y1
                    center_x = int(x1 + w / 2)
                    center_y = int(y1 + h / 2)

                    if conf._detection_viewer:                    
                        pos = (int(center_x-200), int(center_y - 30))
                        cv2.putText(yolo_frame, f"Ball[{idx}][{confidence:.2f}],x,y({center_x}, {center_y}),w:{w},h:{h})", pos, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,255,255), 1)
                        cv2.circle(yolo_frame, (center_x, center_y), 15, (255, 255, 255), 1)                        

                    if confidence > max_confidence and \
                        w <= conf._pitcher_ball_max_size and h <= conf._pitcher_ball_max_size and \
                        w >= conf._pitcher_ball_min_size and h >= conf._pitcher_ball_min_size:
                        # check previous ball
                        check_ball = get_real_position((center_x,center_y))
                        if conf._after_detected and (idx >= predict_start_idx + 3):
                            # 2025-05-22
                            # prediction comparing
                            if(check_movement_in_threshold(check_ball, predicted_center, idx, ball_pos,  conf._ball_detect_pitcher_margin_min, conf._ball_detect_pitcher_margin_max, kalman ) == False):
                                continue  
                        # 최초 Ball은 중앙기준 왼쪽에 있어야함.
                        else:
                            if(check_ball[0] > (frame_w*2/3)):
                                continue

                        max_confidence = confidence
                        best_ball_pos = (center_x, center_y, int(w), int(h))
                        ball_detected = True

                # if ball_detected:
                #     best_ball_real_pos= get_real_position(best_ball_pos)
                #     ball_pos[idx] = best_ball_real_pos

                #     fd_log.info(f"\r✅ [PIT][BALL][{idx}] Detected (x,y {best_ball_pos[0]}, {best_ball_pos[1]}) Confidence: {max_confidence:.2f}")

                # 2025-08-22
                if ball_detected:
                    cx, cy, bw, bh = best_ball_pos

                    # ROI 전체 크기
                    roi_w = yolo_frame.shape[1]
                    roi_h = yolo_frame.shape[0]

                    # 허용 구역 설정 (우 40%, 위 10% 제외)
                    # min_x = int(roi_w * 0.2)
                    max_x = int(roi_w * 0.6)
                    min_y = int(roi_h * 0.1)

                    # 👉 아직 첫 공을 못 찾았을 때만 무시
                    if not conf._after_detected and (cx > max_x or cy < min_y):
                        fd_log.info(f"⚠️ [PIT][BALL][{idx}] First ball ignored (outside valid area)")
                        ball_pos[idx] = (np.nan, np.nan)
                        ball_detected = False
                        continue
                    else:
                        best_ball_real_pos = get_real_position(best_ball_pos)
                        ball_pos[idx] = best_ball_real_pos
                        fd_log.info(
                            f"\r✅ [PIT][BALL][{idx}] Detected (x,y {cx}, {cy}) Confidence: {max_confidence:.2f}"
                        )
                        
                    ########################################################
                    # check ball in hand - enhanced
                    # 2025-07-29
                    ########################################################
                    if not conf._after_detected:
                        is_in_hand, release_idx = is_ball_in_pitcher_hand_enhanced(yolo_frame, idx, ball_pos)                        
                        if not is_in_hand:
                            conf._after_detected = True
                            if idx >= release_idx:
                                for i in range(idx - release_idx):
                                    ball_pos[i] = (np.nan, np.nan)
                            else:
                                for i in range(idx):
                                    ball_pos[i] = (np.nan, np.nan)
                        else:
                            continue
                    ########################################################
                    # kalman predict        
                    ########################################################
                    if not kalman_initialized:
                        # until wait set predict
                        if idx >= predict_start_idx + 10:
                            vx, vy = np.array(ball_pos[idx]) - np.array(ball_pos[idx-1])
                        else:
                            vx, vy = 25, 0
                        ax, ay = 0.0, 0.0                       
                        # kalman.statePost = np.array([[best_ball_real_pos[0]], [best_ball_real_pos[1]], [vx], [vy],[ax],[ay]], dtype=np.float32)
                        kalman.statePost = np.array([[best_ball_real_pos[0]], [best_ball_real_pos[1]], [vx], [vy]], dtype=np.float32)
                        kalman_initialized = True         
                        predict_start_idx = idx
                    else:
                        measurement = np.array([[np.float32(best_ball_real_pos[0])], [np.float32(best_ball_real_pos[1])]])
                        kalman.correct(measurement)  

                    if is_ball_in_movement(idx, ball_pos, conf._ball_detect_pitcher_margin_min, conf._ball_detect_pitcher_margin_max, conf._input_width):
                        detect_ball_frame += 1
                        last_center = best_ball_real_pos
                    else:
                        ball_pos[idx] = (np.nan, np.nan)
                        if conf._detection_viewer:
                            # debug wrong detection
                            cv2.putText(yolo_frame, f"not ball[{idx}][{max_confidence:.2f}],x,y({best_ball_pos[0]}, {best_ball_pos[1]}))", (100, 200), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 2)
                            cv2.circle(yolo_frame, (best_ball_pos[0], best_ball_pos[1]), 15, (0, 0, 255), 10)                        
                else:
                    ball_pos[idx] = (np.nan, np.nan)
                    fd_log.info(f"\r⚠️ [PIT][BALL][{idx}] Non Detected")
                    error_detect_ball_frame += 1

                    if predicted_center is not None and last_center is not None:
                        last_center = predicted_center
                    elif last_center is not None:
                        fallback = (np.array(last_center) + velocity).astype(np.int32)
                        last_center = tuple(fallback)
                    ########################################################
                    # kalman predict -> non detected case       
                    ########################################################
                    if kalman_initialized:
                        measurement = np.array([[np.float32(x_pred)], [np.float32(y_pred)]])
                        kalman.correct(measurement)                      

                # debug
                if conf._detection_viewer:
                    if not np.isnan(best_ball_pos).any():
                        cv2.putText(yolo_frame, f"[{idx}]Confidence[{max_confidence:.2f}],x,y({best_ball_pos[0]},{best_ball_pos[1]}),w:{best_ball_pos[2]},,h:{best_ball_pos[3]})", (100, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 255), 2)
                        cv2.circle(yolo_frame, (best_ball_pos[0], best_ball_pos[1]), 15, (0, 255, 255), 3)
                    
                    # for debug -> erase
                    cv2.imshow(window_name, yolo_frame)
                    cv2.waitKey(0)
                
            batch_imgs = []
            frame_indices = []

    # set detection count
    conf._detect_success_count = detect_ball_frame
    # close windows
    if conf._detection_viewer:        
        cv2.destroyWindow(window_name)    

    # ball process
    # organization of ball detection
    set_last_array(ball_pos)
    
    # fill empty
    # bReturn, arr_ball = fill_linier(ball_pos) 

    # 2025-07-28
    # fill except first empty balls
    # bReturn, arr_ball = fill_linier_except_first(ball_pos) 

    # 2025-07-29
    # fill except first empty balls (enhanced)
    # arr_ball = remove_outlier_ball_points(ball_pos, threshold=40)
    bReturn, arr_ball = fill_linier_except_first_enhanced(ball_pos)

    # 2025-05-21
    # kalman fill
    #bReturn, arr_ball = fill_kalman_interpolation(ball_pos) 

    if(bReturn == False):
        return False, arr_ball
    
    add_next_arr_item(arr_ball, frame_count)

    # make smoother
    fd_smooth_ball_tracking(arr_ball, 0.75)    
    '''
    arr_ball = add_smooth_tracking_position(arr_ball)
    x_vals, y_vals = zip(*arr_ball)
    smoothed_x = adaptive_moving_average_fixed_ends(x_vals)
    smoothed_y = adaptive_moving_average_fixed_ends(y_vals)
    arr_ball = list(zip(smoothed_x, smoothed_y))
    arr_ball.append((-1, -1))
    ''' 
    return True, arr_ball


def remove_outlier_ball_points(arr_ball, threshold=40):
    """
    공 궤적에서 갑작스럽게 튄 프레임을 np.nan 처리
    """
    for i in range(1, len(arr_ball) - 1):
        curr = arr_ball[i]
        prev = arr_ball[i - 1]
        next = arr_ball[i + 1]

        if None in [curr, prev, next]:
            continue
        if np.isnan(curr[0]) or np.isnan(prev[0]) or np.isnan(next[0]):
            continue

        dx1 = np.linalg.norm(np.array(curr) - np.array(prev))
        dx2 = np.linalg.norm(np.array(curr) - np.array(next))

        if dx1 > threshold and dx2 > threshold:
            arr_ball[i] = (np.nan, np.nan)

    return arr_ball


# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_batter():
# [owner] hongsu jung
# [date] 2025-03-05
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_batter():

    # YOLOv8
    # 2025-03-28 
    #yolo_model = conf._yolo_model_s
    #yolo_model = conf._yolo_model_m
    #yolo_model = conf._yolo_model_l
    yolo_model = conf._yolo_model_x


    # ─────────────────────────────────────────────────────────────────────────────####################
    # Kalman Filter Initiation
    # pitching  = kalman_p
    # batting   = kalman_b
    # ─────────────────────────────────────────────────────────────────────────────####################    

    # Kalman pitching (prev hit)
    kalman_p = cv2.KalmanFilter(4, 2)    
    dt = 1.0
    kalman_p.transitionMatrix = np.array([[1, 0, dt, 0],
                                        [0, 1, 0, dt],
                                        [0, 0, 1, 0 ],
                                        [0, 0, 0, 1 ]], dtype=np.float32)
    kalman_p.measurementMatrix = np.array([[1, 0, 0, 0],
                                         [0, 1, 0, 0]], dtype=np.float32)
    kalman_p.processNoiseCov = np.eye(4, dtype=np.float32) * 1e-4
    kalman_p.measurementNoiseCov = np.eye(2, dtype=np.float32) * 1e-2
    kalman_p.errorCovPost = np.eye(4, dtype=np.float32)

    # Kalman batting (after hit)
    kalman_b = cv2.KalmanFilter(4, 2)
    kalman_b.transitionMatrix = np.array([[1, 0, dt, 0],
                                        [0, 1, 0, dt],
                                        [0, 0, 1, 0 ],
                                        [0, 0, 0, 1 ]], dtype=np.float32)
    kalman_b.measurementMatrix = np.array([[1, 0, 0, 0],
                                         [0, 1, 0, 0]], dtype=np.float32)
    kalman_b.processNoiseCov = np.eye(4, dtype=np.float32) * 1e-4
    kalman_b.measurementNoiseCov = np.eye(2, dtype=np.float32) * 1e-2
    kalman_b.errorCovPost = np.eye(4, dtype=np.float32)

    velocity = np.array([0, -20], dtype=np.float32)    
    kalman_p_initialized = False
    kalman_b_initialized = False

    frames = conf._frames_curr
    frame_count = len(frames)
    
    if(frame_count < 1):
        return False, None
    
    conf._detect_frame_count = frame_count
    ball_pos = [None] * frame_count
    pitching_ball_pos = [None] * frame_count 
    hitting_ball_pos = [None] * frame_count   
    detect_ball_frame = 0    
    error_detect_ball_frame = 0    
    percent_progress = 0
    finish_detection = False
    conf._detect_area = reset_detect_area()

    is_detected = False
    first_detection_height = conf._batter_first_ball_height
    
    # 2025-07-28 
    first_hit_idx = float('inf')
    cnt_pitching_detect = 0
    cnt_hitting_detect = 0
    
    # 2025-07-30
    # check ball angle
    detect_ball_margin = conf._ball_detect_batter_batting_margin
    predicted_center = (0,0)

    batch_imgs = []
    frame_indices = []
    batch_size = (abs(conf._batter_detect_prev_frame) + abs(conf._batter_detect_post_frame) + 1) // 3

    window_name = "[Detect][BAT] Ball Detection"

    # ─────────────────────────────────────────────────────────────────────────────#
    # start search bat/ball position
    # ─────────────────────────────────────────────────────────────────────────────#
    for idx, img in enumerate(frames):
        yolo_frame = frame_resize(img, conf._detect_area, conf._detect_area_zoom)
        yolo_height, yolo_width = yolo_frame.shape[:2]
        batch_imgs.append(yolo_frame)
        frame_indices.append(idx)
        if finish_detection:
            break

        if len(batch_imgs) >= batch_size or idx == frame_count - 1:
            results = yolo_model(batch_imgs, classes=[conf._yolo_class_id_baseball], conf=conf._yolo_detect_batter_confidence, imgsz=conf._yolo_detect_batter_size, verbose=False)
            # detection
            if conf._detection_viewer:
                cv2.namedWindow(window_name)

            for b_idx, result in enumerate(results):
                # over screen balls
                if finish_detection:
                    break
                # pitching
                if(idx < first_hit_idx):
                    # every time predict
                    prediction = kalman_p.predict()
                    if kalman_p_initialized:
                        x_pred = int(prediction[0, 0])
                        y_pred = int(prediction[1, 0])     
                        # set predicted         
                        predicted_center = (x_pred, y_pred)  
                        #fd_log.info(f"Set Predict [HIT] [{x_pred},{y_pred}]")                         
                # after hit                
                else:
                    # every time predict
                    prediction = kalman_b.predict()
                    if kalman_b_initialized:
                        x_pred = int(prediction[0, 0])
                        y_pred = int(prediction[1, 0])      
                        # set predicted         
                        predicted_center = (x_pred, y_pred)  
                        #fd_log.info(f"Set Predict [HIT] [{x_pred},{y_pred}]")                                         
                                
                idx = frame_indices[b_idx]
                yolo_frame = batch_imgs[b_idx]
                mem_img = frames[idx]

                percent_progress = int(idx / frame_count * 100)
                fd_log.info(f"🎯[BAT][{idx}] {percent_progress}% detect ball: {detect_ball_frame}/{frame_count}")

                ball_detected = False
                max_confidence = 0
                best_ball_pos = None

                boxes = result.boxes
                for box in boxes:
                    x1, y1, x2, y2 = map(int, box.xyxy[0])
                    confidence = box.conf[0].item()

                    w, h = x2 - x1, y2 - y1
                    center_x = int(x1 + w / 2)
                    center_y = int(y1 + h / 2)
                    check_ball = get_real_position((center_x, center_y))
                    
                    # check 1st detect
                    if is_detected is False:
                        if check_ball[1] < first_detection_height: continue

                    #check ball
                    cv2.circle(yolo_frame, (center_x,center_y), 25, (0, 0, 0), 2)                        
                    # Display confidence and real-world coordinates
                    text = f"{confidence:.2f}, ({check_ball[0]:.1f}, {check_ball[1]:.1f})"
                    cv2.putText(yolo_frame, text, (center_x + 30, center_y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2, cv2.LINE_AA)
                    # debug
                    # fd_log.info(text)
                                            
                    if confidence > max_confidence and \
                    conf._batter_ball_min_size <= w <= conf._batter_ball_max_size and \
                    conf._batter_ball_min_size <= h <= conf._batter_ball_max_size:
                        # pitching
                        if(idx < first_hit_idx):
                            # check previous ball
                            if kalman_p_initialized:                                                        
                                ret, distance = check_points(check_ball, predicted_center, conf._ball_detect_batter_pitching_margin)
                                if ret == False:                                    
                                    # ─────────────────────────────────────────────────────────────────────────────    
                                    # find hited timing
                                    # check first_hit_idx (타격으로 인해 예측값과 틀어졌는지, 그것이 Hit Area 였는지)
                                    # ─────────────────────────────────────────────────────────────────────────────    
                                    if is_detected: 
                                        first_hit_idx = get_hit_timing(first_hit_idx, idx, check_ball, predicted_center)
                                        if first_hit_idx < float('inf'):
                                            fd_log.info(f"💡Detect Hit Timing [{first_hit_idx}]")
                                            pass
                                        else:
                                            continue
                                    else:
                                        continue
                                        
                            # 최초 detect는 영상 오른쪽, 왼쪽에서 시작 되어야 함
                            if detect_ball_frame == 0:
                                # Y Range
                                if not (yolo_height / 4 <= center_y <= yolo_height / 4 * 3): 
                                    continue
                                # X Range
                                if conf._swing_right_hand:  
                                    if center_x < yolo_width * 2 / 3: 
                                        continue
                                else:                       
                                    if center_x > yolo_width / 4: 
                                        continue

                        # after hit
                        elif(idx >= first_hit_idx):
                            # check previous ball
                            if kalman_b_initialized:
                                # too many -> out of range
                                if(error_detect_ball_frame > 5):
                                    finish_detection = True
                                    break
                                if(check_ball[1] > conf._ball_detect_bound_height):
                                    detect_ball_margin = conf._ball_detect_batter_base_margin
                                else:
                                    detect_ball_margin = conf._ball_detect_batter_batting_margin
                                ret, distance = check_points(check_ball, predicted_center, detect_ball_margin * (error_detect_ball_frame+1))
                                if(ret == False):
                                    continue
                        # near hit position 
                        '''
                        else:
                            if(conf._swing_right_hand): # 중앙에서 왼쪽
                                expected_ball = (yolo_width/5, yolo_height/3*2)
                                if(check_points((center_x,center_y), expected_ball, conf._ball_detect_batter_base_margin) == False):
                                    continue
                            else: #중앙에서 오른쪽
                                expected_ball = (yolo_width/5*4, yolo_height/3*2)
                                if(check_points((center_x,center_y), expected_ball, conf._ball_detect_batter_base_margin) == False):
                                    continue
                        '''

                        max_confidence = confidence
                        best_ball_pos = (center_x, center_y, w, h)
                        ball_detected = True                                        

                if ball_detected:
                    best_ball_real_pos= get_real_position(best_ball_pos)
                    ball_pos[idx] = best_ball_real_pos

                    fd_log.info(f"\r✅ [BALL][BAT][{idx}] detected [{best_ball_real_pos[0]},{best_ball_real_pos[1]}]")

                    # pitching
                    if(idx < first_hit_idx):
                        cnt_pitching_detect += 1
                        if not kalman_p_initialized:
                            valid_indices = [
                                i for i, pos in enumerate(ball_pos[:idx + 1])
                                if isinstance(pos, (list, tuple, np.ndarray)) and len(pos) >= 2 and not np.isnan(pos).any()
                            ]
                            # 현재 idx와 idx-1이 유효한지 추가로 체크
                            if (len(valid_indices) >= 2):
                                idx1 = valid_indices[-2]
                                idx2 = valid_indices[-1]                                
                                p1 = np.array(ball_pos[idx1])
                                p2 = np.array(ball_pos[idx2])                                
                                divide_factor = idx2 - idx1                                
                                vx, vy = (p2 - p1) / divide_factor                                
                                kalman_p.statePost = np.array([[best_ball_real_pos[0]],
                                                            [best_ball_real_pos[1]],
                                                            [vx],
                                                            [vy]], dtype=np.float32)
                                kalman_p_initialized = True
                        else:
                            measurement = np.array([[np.float32(best_ball_real_pos[0])], [np.float32(best_ball_real_pos[1])]])
                            kalman_p.correct(measurement)  
                    # after hit
                    else:
                        cnt_hitting_detect += 1
                        if not kalman_b_initialized:
                            valid_indices = [
                                i for i, pos in enumerate(ball_pos[first_hit_idx:], start=first_hit_idx)
                                if isinstance(pos, (list, tuple, np.ndarray)) and len(pos) >= 2 and not np.isnan(pos).any()
                            ]
                            # 현재 idx와 idx-1이 유효한지 추가로 체크
                            if (len(valid_indices) >= 2):
                                idx1 = valid_indices[-2]
                                idx2 = valid_indices[-1]                                
                                p1 = np.array(ball_pos[idx1])
                                p2 = np.array(ball_pos[idx2])                                
                                divide_factor = idx2 - idx1                                
                                vx, vy = (p2 - p1) / divide_factor                                
                                kalman_b.statePost = np.array([[best_ball_real_pos[0]],
                                                            [best_ball_real_pos[1]],
                                                            [vx],
                                                            [vy]], dtype=np.float32)
                                kalman_b_initialized = True
                        else:
                            measurement = np.array([[np.float32(best_ball_real_pos[0])], [np.float32(best_ball_real_pos[1])]])
                            kalman_b.correct(measurement)  
                    
                    detect_ball_frame += 1                
                    last_center = best_ball_real_pos
                    error_detect_ball_frame = 0

                    if is_detected is False: is_detected = True
                else:
                    ball_pos[idx] = (np.nan, np.nan)
                    fd_log.error(f"\r❌ [BALL][BAT][{idx}] not detected")
                    error_detect_ball_frame += 1

                # debug        
                if conf._detection_viewer:
                    draw_ball_on_frame(yolo_frame, best_ball_pos, predicted_center, max_confidence, ball_detected)
                    while True:
                        cv2.imshow(window_name, yolo_frame)
                        key = cv2.waitKey(1) & 0xFF
                        if key != 255:  # 아무 키 입력
                            break
           
            batch_imgs = []
            frame_indices = []
            
    # set detection count
    conf._detect_success_count = detect_ball_frame
    # close windows
    if conf._detection_viewer:  
        cv2.destroyWindow(window_name)   
    
    # organization of ball detection
    set_last_array(ball_pos)
    
    # 2025-05-22 new find hit position
    # detect를 충분히 못했으면, 영상 제작 skip
    if cnt_pitching_detect < 3: 
        fd_log.error(f"not enough detect pitching [{cnt_pitching_detect}]")
        return False, None
    if cnt_hitting_detect < 3: 
        fd_log.error(f"not enough detect hitting [{cnt_hitting_detect}]")
        return False, None
    
    # seperate array between pitching and batting
    pitching_ball_pos, hitting_ball_pos, hit_point, hit_index_pitching, hit_index_hitting, arr_ball = split_pitch_and_hit_with_hitpoint(ball_pos)
    if pitching_ball_pos is None:
        return False, None
        
    # log
    conf._batter_hitting_first_index = hit_index_hitting
    conf._batter_pitching_last_index = hit_index_pitching
    conf._batter_intersect_pos       = hit_point

    # debug
    '''
    start_pitch, end_pitch  = get_start_end_time(pitching_ball_pos)
    detect_pitch = end_pitch-start_pitch
    start_hit, end_hit      = get_start_end_time(hitting_ball_pos)
    detect_hit = end_hit-start_hit
    fd_log.info(f"\033[33m[Detected Pitching] {start_pitch} ~ {conf._batter_pitching_last_index} [{detect_pitch}/{conf._detect_frame_count}]\033[0m")
    fd_log.info(f"\033[33m[Detected Hitting] {conf._batter_hitting_first_index} ~ {end_hit} [{detect_hit}/{conf._detect_frame_count}]\033[0m")
    fd_log.info(f"\033[33m[Intersection] {conf._batter_intersect_pos}\033[0m")
    '''

    return True, arr_ball

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_hit():
# [owner] hongsu jung
# [date] 2025-05-17
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_hit(auto_detect):    

    # until manual success
    def detect_manual_until_success():
        while True:
            result, arr = fd_detect_ball_hit_manual()
            if result:
                return result, arr
            
    if auto_detect:
        fd_log.info(f"[HIT] hangtime: {conf._landingflat_hangtime:.2f}s")
        if conf._landingflat_hangtime > conf._hit_auto_detect_permit_sec:
            fd_log.info("[HIT] → Manual detection (hangtime too long)")
            return detect_manual_until_success()
        else:
            fd_log.info("[HIT] → Try auto detection first")
            result, arr = fd_detect_ball_hit_auto_detect()
            if not result:
                fd_log.info("[HIT] → Auto failed, fallback to manual")
                return detect_manual_until_success()
            else:
                return result, arr
    else:
        fd_log.info("[HIT] → Manual detection (auto disabled)")
        return detect_manual_until_success()

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_hit_manual():
# [owner] hongsu Jung
# [date] 2025-07-07
# detect with diff images
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_hit_manual():
    match conf._detector_type:
        case conf._detector_type_diff:
            result, arr = fd_detect_ball_hit_manual_diff()
        case conf._detector_type_visual:
            result, arr = fd_detect_ball_hit_manual_visual()
        case conf._detector_type_hybrid:
            conf._hit_tracking_retry_cnt += 1
            if conf._hit_tracking_retry_cnt % 2 == 0:
                result, arr = fd_detect_ball_hit_manual_visual()
            else:
                result, arr = fd_detect_ball_hit_manual_diff()
    return result, arr

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_hit_manual_old():
# [owner] joonho
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_hit_manual_visual():
    frames = conf._frames_curr
    frame_count = len(frames)
    if frame_count < 1:
        return False, None
    conf._detect_frame_count = frame_count

    # 클릭 횟수 결정
    if conf._landingflat_hangtime <= 1.0:
        total_click_num = 5
    elif conf._landingflat_hangtime <= 2.0:
        total_click_num = 6
    elif conf._landingflat_hangtime <= 3.0:
        total_click_num = 7
    else:
        total_click_num = 8

    # 클릭 프레임 인덱스 설정
    select_indices = get_click_indices(frame_count, total_click_num)
    select_indices[total_click_num - 1] += 1
    select_indices_idx = 1
    sleep_for_play = 1 / conf._detector_fps

    ball_pos_arr = [None] * frame_count
    ball_pos_arr[0] = (conf._hit_detect_init_x, conf._hit_detect_init_y)

    scale = conf._hit_tracking_window_scale
    img_sample = frames[0]
    h, w = img_sample.shape[:2]

    for idx in range(1, frame_count + 1):
        img = frames[idx - 1].copy()
        h, w = img.shape[:2]

        display_img = cv2.resize(img, (int(w * scale), int(h * scale)))

        # 클릭 시각화
        for i, click_frame_idx in enumerate(select_indices[:total_click_num]):
            if click_frame_idx >= idx:
                break
            if click_frame_idx:
                click_frame_idx -= 1
            pos = ball_pos_arr[click_frame_idx]
            if pos is not None:
                center = (int(pos[0] * scale), int(pos[1] * scale))
                cv2.circle(display_img, center, 8, (0, 0, 255), -1)
                cv2.putText(display_img, f"{i+1}/{total_click_num}",(center[0] + 10, center[1] + 10),cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)                

        # 클릭 타이밍일 때만 처리
        if idx == select_indices[select_indices_idx]:
            select_indices_idx += 1
            click_number = select_indices_idx
            percentage = idx / frame_count * 100

            # 오버레이 추가
            cv2.putText(display_img, f"Frame {idx}/{frame_count}", (int(w * 0.4), 30),cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(display_img, f"Tracking {percentage:.1f} %", (int(w * 0.4), 60),cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            cv2.putText(display_img, f"Select {click_number}/{total_click_num}", (int(w * 0.4), 90),cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            if idx == frame_count:
                cv2.rectangle(display_img, (0, 0), (display_img.shape[1] - 1, display_img.shape[0] - 1), (0, 0, 255), 5)

            # ⬇️ Qt 기반 마우스 입력 대기
            mouse_click = conf._tracking_check_widget.show_frame_and_wait(display_img, True)
            match mouse_click:
                case conf._mouse_click_left:
                    raw_pos = conf._tracking_check_widget.selected_point
                    real_pos = (int(raw_pos[0] / scale), int(raw_pos[1] / scale))
                    if 0 <= real_pos[0] < w and 0 <= real_pos[1] < h:
                        ball_pos_arr[idx - 1] = real_pos
                    else:
                        fd_log.warning(f"⚠️ Invalid click: {real_pos}")
                case conf._mouse_click_right:
                    if idx == frame_count:
                        fd_log.info("🔁 Right click ignored on last frame.")
                        continue
                    ball_pos_arr[idx - 1] = None
                case conf._mouse_click_middle:
                    return False, None
        else:
            # Qt는 자동으로 다음 프레임을 넘기지 않음 — 필요시 여기에서 delay 처리 추가 가능
            conf._tracking_check_widget.show_frame(display_img, True)
            time.sleep(sleep_for_play)

    # 최종 위치 저장 해제
    conf._tracking_check_widget.show_last_frame(False)

    # 후처리 및 smoothing
    arr_ball = add_smooth_tracking_position(ball_pos_arr)
    x_vals, y_vals = zip(*arr_ball)
    smoothed_x = adaptive_moving_average_fixed_ends(x_vals)
    smoothed_y = adaptive_moving_average_fixed_ends(y_vals)
    arr_ball = list(zip(smoothed_x, smoothed_y))
    arr_ball.append((-1, -1))

    return True, arr_ball

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_hit_manual_new():
# [owner] hongsu Jung
# [date] 2025-07-05
# detect with diff images
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_hit_manual_diff():

    frames = conf._frames_curr
    frame_count = len(frames)
    if frame_count < 1:
        return False, None
    conf._detect_frame_count = frame_count

    # 클릭 횟수 결정
    if conf._landingflat_hangtime <= 1.0:
        total_click_num = 4
    elif conf._landingflat_hangtime <= 2.0:
        total_click_num = 5
    elif conf._landingflat_hangtime <= 3.0:
        total_click_num = 6
    else:
        total_click_num = 7

    # 선택 프레임 인덱스
    select_indices = get_click_indices(frame_count, total_click_num)
    select_indices[total_click_num - 1] += 1
    select_indices_idx = 1

    ball_pos_arr = [None] * frame_count
    ball_pos_arr[0] = (conf._hit_detect_init_x, conf._hit_detect_init_y)

    scale = conf._hit_tracking_window_scale
    img_sample = frames[0]
    h, w = img_sample.shape[:2]

    prev_frame = None
    acc_diff = None

    for idx in range(1, frame_count + 1):
        img = frames[idx - 1].copy()
        h, w = img.shape[:2]

        # Optical diff accumulation
        blur_gauge = 9
        threshold_gauge = 3

        if prev_frame is not None:
            blur_img = cv2.GaussianBlur(img, (blur_gauge, blur_gauge), 0)
            blur_prev = cv2.GaussianBlur(prev_frame, (blur_gauge, blur_gauge), 0)
            diff = cv2.absdiff(blur_img, blur_prev)

            diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
            _, diff_thresh = cv2.threshold(diff_gray, threshold_gauge, 255, cv2.THRESH_BINARY)

            if np.sum(diff_thresh) >= 1000:
                if acc_diff is None:
                    acc_diff = np.zeros_like(diff_thresh)
                acc_diff = cv2.add(acc_diff, diff_thresh)

        prev_frame = img

        # 클릭 타이밍일 때만 처리
        if idx == select_indices[select_indices_idx]:
            select_indices_idx += 1

            if acc_diff is None:
                acc_diff = np.zeros((h, w), dtype=np.uint8)
            acc_vis_color = cv2.applyColorMap(cv2.normalize(acc_diff, None, 0, 255, cv2.NORM_MINMAX),cv2.COLORMAP_HOT)

            img_resized = cv2.resize(img, (int(w * scale), int(h * scale)))
            acc_resized = cv2.resize(acc_vis_color, (int(w * scale), int(h * scale)))
            display_img = cv2.addWeighted(img_resized, 0.1, acc_resized, 0.9, 0)

            # 클릭 시각화
            for i, click_frame_idx in enumerate(select_indices[:total_click_num]):
                if click_frame_idx >= idx:
                    break
                if click_frame_idx:
                    click_frame_idx -= 1
                pos = ball_pos_arr[click_frame_idx]
                if pos is not None:
                    center = (int(pos[0] * scale), int(pos[1] * scale))
                    cv2.circle(display_img, center, 8, (0, 0, 255), -1)
                    cv2.putText(display_img, f"{i+1}/{total_click_num}",(center[0] + 10, center[1] + 10),cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

            # 진행 상태 표시
            percentage = idx / frame_count * 100
            cv2.putText(display_img, f"Frame {idx}/{frame_count}", (int(w / 2), 30),cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(display_img, f"Tracking {percentage:.1f} %", (int(w / 2), 60),cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)
            cv2.putText(display_img, f"Select {select_indices_idx - 1}/{total_click_num}", (int(w / 2), 90),cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            if idx == frame_count:
                cv2.rectangle(display_img, (0, 0), (display_img.shape[1]-1, display_img.shape[0]-1), (0, 0, 255), 5)

            # ⬇️ Qt로 사용자 입력 대기
            mouse_click = conf._tracking_check_widget.show_frame_and_wait(display_img, True)            
            match mouse_click:
                case conf._mouse_click_left:
                    raw_pos = conf._tracking_check_widget.selected_point
                    real_pos = (int(raw_pos[0] / scale), int(raw_pos[1] / scale))
                    if 0 <= real_pos[0] < w and 0 <= real_pos[1] < h:
                        ball_pos_arr[idx - 1] = real_pos
                        acc_diff = np.zeros((h, w), dtype=np.uint8)
                    else:
                        fd_log.warning(f"⚠️ Invalid click: {real_pos}")
                case conf._mouse_click_right:
                    if idx == frame_count:
                        fd_log.info("🔁 Right click ignored on last frame.")
                        continue
                    ball_pos_arr[idx - 1] = None
                    acc_diff = np.zeros((h, w), dtype=np.uint8)
                case conf._mouse_click_middle:
                    return False, None

    # 최종 결과 보정 및 smoothing
    arr_ball = add_smooth_tracking_position(ball_pos_arr)
    x_vals, y_vals = zip(*arr_ball)
    smoothed_x = adaptive_moving_average_fixed_ends(x_vals)
    smoothed_y = adaptive_moving_average_fixed_ends(y_vals)
    arr_ball = list(zip(smoothed_x, smoothed_y))
    arr_ball.append((-1, -1))

    conf._tracking_check_widget.show_last_frame(False)

    return True, arr_ball

# ─────────────────────────────────────────────────────────────────────────────
# def fd_detect_ball_hit_auto_detect():
# [owner] hongsu jung
# [date] 2025-05-05
# ─────────────────────────────────────────────────────────────────────────────
def fd_detect_ball_hit_auto_detect():    

    frames = conf._frames_curr
    if not frames or len(frames) < 2:
        fd_log.error("❌ not enough frame count.")
        return False, None

    ###################################
    # Initial ROI Set
    # jamsil 5/4
    ###################################
    arr_ball_pos = track_ball_with_cuda_optical_flow(frames)
 
    # fill empty
    bReturn, arr_ball = fill_linier(arr_ball_pos) 
    if(bReturn == False):
        return False, arr_ball
    

    # fill the none array values
    arr_ball_pos = fill_none_with_last_valid(arr_ball)

    # (x, y) 각각 분리
    x_vals, y_vals = zip(*arr_ball_pos)
    # Adaptive Moving Average 각각 적용
    smoothed_x = adaptive_moving_average_fixed_ends(x_vals, 5)
    smoothed_y = adaptive_moving_average_fixed_ends(y_vals, 5)
    # 다시 (x, y) 튜플로 결합
    arr_ball = list(zip(smoothed_x, smoothed_y))
    arr_ball.append((-1, -1))

    # make smoother
    # hybrid_smooth_ball_tracking(arr_ball)
    return True, arr_ball

# ─────────────────────────────────────────────────────────────────────────────
# def track_ball_with_cuda_optical_flow(frames, initial_roi):
# [owner] hongsu jung
# ─────────────────────────────────────────────────────────────────────────────
def track_ball_with_cuda_optical_flow(frames):

    # YOLOv8
    # 2025-03-28 
    #yolo_model= conf._yolo_model_s
    #yolo_model= conf._yolo_model_m
    yolo_model = conf._yolo_model_l
    #yolo_model= conf._yolo_model_x
    
    op_max_selection_coutours   = conf._op_max_selection_coutours  
    op_damping_ratio_number     = conf._op_damping_ratio_number    
    op_distance_tolerance       = conf._op_distance_tolerance      
    op_area_min                 = conf._op_area_min                
    op_area_max                 = conf._op_area_max                
    op_cos_threshold            = conf._op_cos_threshold           
    op_duplicate_threshold      = conf._op_duplicate_threshold     
    op_distance_weight          = conf._op_distance_weight         
    op_deviation_weight         = conf._op_deviation_weight        
    op_roi_hit_width            = conf._op_roi_hit_width           
    op_roi_hit_height           = conf._op_roi_hit_height          
    op_roi_reposition_bias      = conf._op_roi_reposition_bias     
    gpu_prev                    = None
    gpu_curr                    = None
    
    
    # ─────────────────────────────────────────────────────────────────────────────    
    # Kalman Filter Initiation
    # ─────────────────────────────────────────────────────────────────────────────    
    dt = 1.0
    kalman = cv2.KalmanFilter(6, 2)

    # 상태 전이 행렬
    kalman.transitionMatrix = np.array([
        [1, 0, dt, 0, 0.5*dt**2, 0],
        [0, 1, 0, dt, 0, 0.5*dt**2],
        [0, 0, 1, 0, dt, 0],
        [0, 0, 0, 1, 0, dt],
        [0, 0, 0, 0, 1, 0],
        [0, 0, 0, 0, 0, 1]
    ], dtype=np.float32)

    # 측정 행렬
    kalman.measurementMatrix = np.array([
        [1, 0, 0, 0, 0, 0],
        [0, 1, 0, 0, 0, 0]
    ], dtype=np.float32)

    # 잡음 공분산
    kalman.processNoiseCov = np.diag([1e-4]*6).astype(np.float32)
    kalman.measurementNoiseCov = np.eye(2, dtype=np.float32) * 0.5
    kalman.errorCovPost = np.eye(6, dtype=np.float32)  # ✅ 크기 수정됨
        
    # ─────────────────────────────────────────────────────────────────────────────    
    # Optical Flow -> Initiation
    # Optical Flow 민감도 ↑	--> winSize ↓, polyN ↓, numLevels ↑, numIters ↑
    # ─────────────────────────────────────────────────────────────────────────────    
    optical_flow = cv2.cuda_FarnebackOpticalFlow.create(
        winSize=3,
        polyN=5,
        numLevels=6,        
        polySigma=0.9,
        pyrScale=0.1,
        numIters=15, 
        fastPyramids=False,
        flags=cv2.OPTFLOW_FARNEBACK_GAUSSIAN
    )

    start_trajectory = 5
    first_trajectory = (conf._hit_detect_init_x, conf._hit_detect_init_y)
    ball_pos_arr = [None] * len(frames)
    trajectory = []  # 위치 값만
    trajectory_indices = []  # 해당 위치가 기록된 프레임 번호

    last_center     = None
    pred_speed      = 0    
    fail_count      = 0
    success_count   = 0    
    total_count     = len(frames)
    window_name     = "[ROI Zoom x5]"

    # ─────────────────────────────────────────────────────────────────────────────    
    # Expected intial ball position
    # ─────────────────────────────────────────────────────────────────────────────    
    # 초기 위치
    x0 = conf._hit_detect_init_x
    y0 = conf._hit_detect_init_y
    # 이동 거리
    # 60 fps = 15
    # 30 fps = 30
    movement_1frame = 900 / conf._input_fps
    adjusted_distance = movement_1frame * (conf._launch_speed / 100)
    fd_log.info(f"ball distance:{adjusted_distance}, angle:{conf._launch_h_angle}, speed:{conf._launch_speed}")
    
    # 수평 각도 (도 → 라디안 변환)
    theta_rad = math.radians(conf._launch_h_angle)
    # 이동 벡터 계산
    dx = adjusted_distance * math.sin(theta_rad)
    dy = adjusted_distance * math.cos(theta_rad)
    velocity = np.array([dx,dy], dtype=np.float32)
    # 새로운 위치
    vx, vy = dx, -1 * dy
    ax, ay = 0.0, 0.0  
    kalman.statePost = np.array([[x0],[y0],[vx],[vy],[ax],[ay]], dtype=np.float32)
    
    n_pred_cnt = 0
    while(1):
        n_pred_cnt += 1
        if(n_pred_cnt > start_trajectory):
            break        
        next_x = x0 + n_pred_cnt * vx
        next_y = y0 + n_pred_cnt * vy     
        # add measure
        predicted = kalman.predict()
        measurement = np.array([[np.float32(next_x)], [np.float32(next_y)]])
        kalman.correct(measurement)
        #fd_log.info(f"🔍[predict] x,y:({next_x},{next_y} -> predict:{predicted[0, 0]}, {predicted[1, 0]})")

    last_center = (next_x, next_y)    
    roi_left    = int(next_x - op_roi_hit_width//2   )
    roi_right   = int(next_x + op_roi_hit_width//2   )
    roi_top     = int(next_y - op_roi_hit_height//2  )
    roi_bottom  = int(next_y + op_roi_hit_height//2  )
    
    # ─────────────────────────────────────────────────────────────────────────────
    # Searching Each Frame
    # ─────────────────────────────────────────────────────────────────────────────
    ball_pos_arr[0] = first_trajectory    

    # ─────────────────────────────────────────────────────────────────────────────
    # Searching Each Frame
    # ─────────────────────────────────────────────────────────────────────────────
    for idx in range(start_trajectory, total_count):   # JAMSIL
        
        frame_prev = frames[idx - 1]
        frame_curr = frames[idx]
        #frame_vis = frame_curr.copy()
        frame_vis = cv2.addWeighted(frame_prev, 0.5, frame_curr, 0.5, 0.0)
        frame_h, frame_w = frame_curr.shape[:2]
        frame_size = (frame_w, frame_h)


        # ─────────────────────────────────────────────────────────────────────────────
        # get predict
        # ─────────────────────────────────────────────────────────────────────────────
        prediction = kalman.predict()         
        x_pred = int(prediction[0, 0])
        y_pred = int(prediction[1, 0])      
        predicted_center = (x_pred, y_pred)
            
        # show predict
        if conf._detection_viewer:
            cv2.putText(frame_vis,f"predict:({x_pred},{y_pred})",(x_pred, y_pred - 5),cv2.FONT_HERSHEY_SIMPLEX,0.3,(255, 0, 0),1)       
            cv2.circle(frame_vis, (x_pred, y_pred), 15, (255, 0, 255), 3)
            #fd_log.info(f"🔍[predict][{idx}] x,y:({x_pred},{y_pred})")

        # get pred speed
        pred_speed = get_speed(last_center, predicted_center)
        # get direction
        pred_dx = last_center[0] - predicted_center[0]
        pred_dy = last_center[1] - predicted_center[1]

        # ─────────────────────────────────────────────────────────────────────────────
        # Get Frame and ROI
        # ─────────────────────────────────────────────────────────────────────────────
        roi_top = max(0, roi_top)
        roi_left = max(0, roi_left)
        roi_bottom = min(roi_bottom, frame_h)
        roi_right = min(roi_right, frame_w)
        if roi_bottom <= roi_top or roi_right <= roi_left:
            fd_log.error(f"[frame {idx}]❌ Fail ROI Size - Frame Skip")
            continue

        roi_prev = frame_prev[roi_top:roi_bottom, roi_left:roi_right]
        roi_curr = frame_curr[roi_top:roi_bottom, roi_left:roi_right]
        gray_prev = cv2.cvtColor(roi_prev, cv2.COLOR_BGR2GRAY)
        gray_curr = cv2.cvtColor(roi_curr, cv2.COLOR_BGR2GRAY)
        # 2025-07-04
        # 💡 작은 움직임 감지 개선을 위한 Blur
        gray_prev = cv2.GaussianBlur(gray_prev, (7, 7), 2.0)
        gray_curr = cv2.GaussianBlur(gray_curr, (7, 7), 2.0)

        gpu_prev = cv2.cuda_GpuMat(); gpu_prev.upload(gray_prev)
        gpu_curr = cv2.cuda_GpuMat(); gpu_curr.upload(gray_curr)

        # ─────────────────────────────────────────────────────────────────────────────
        # Optical Flow -> Detect Movement
        # ─────────────────────────────────────────────────────────────────────────────
        flow = optical_flow.calc(gpu_prev, gpu_curr, None).download()

        # 1. Optical Flow Magnitude
        mag = np.sqrt(flow[..., 0]**2 + flow[..., 1]**2)
        mag_norm = cv2.normalize(mag, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        # 2. Thresholding - 아주 작은 움직임도 감지
        _, thresh = cv2.threshold(mag_norm, 1, 255, cv2.THRESH_BINARY)  # 0.01 → 1 (uint8 기준)
        # 3. Morphological Opening: 노이즈 제거만, 객체는 살림
        kernel = np.ones((3, 3), np.uint8)
        opened = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
        # 4. Distance Transform
        dist_transform = cv2.distanceTransform(opened, cv2.DIST_L2, 5)
        _, sure_fg = cv2.threshold(dist_transform, 0.3 * dist_transform.max(), 255, 0)  # 1.0 → 0.3
        sure_fg = np.uint8(sure_fg)
        # 5. Marker 생성
        unknown = cv2.subtract(opened, sure_fg)
        _, markers = cv2.connectedComponents(sure_fg)
        markers = markers + 1
        markers[unknown == 255] = 0
        # 6. Watershed
        color_frame = cv2.cvtColor(mag_norm, cv2.COLOR_GRAY2BGR)
        cv2.watershed(color_frame, markers)
        # 7. 객체 마스크 생성
        object_mask = np.uint8(markers > 1) * 255
        # 8. Contour 추출
        contours, _ = cv2.findContours(object_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        
        detected = False        
        best_center     = None
        best_area       = None

        # ─────────────────────────────────────────────────────────────────────────────
        # get contours
        # ─────────────────────────────────────────────────────────────────────────────
        min_distance_gap = float('inf')
        best_area = None
        total_contours = len(contours)
        if conf._detection_viewer:
            fd_log.info(f"🔍[HIT][{idx}] contour count: {total_contours}")

        # ─────────────────────────────────────────────────────────────────────────────
        # YOLOv8 Detection
        # ─────────────────────────────────────────────────────────────────────────────
        roi_yolo_top    = int(max(0, roi_top - (roi_bottom - roi_top) ))
        roi_yolo_left   = int(max(0, roi_left - (roi_right - roi_left) ))
        roi_yolo_bottom = int(min(roi_bottom + (roi_bottom - roi_top), frame_h))
        roi_yolo_right  = int(min(roi_right + (roi_right - roi_left), frame_w))
        roi_yolo = frame_curr[roi_yolo_top:roi_yolo_bottom, roi_yolo_left:roi_yolo_right]

        results = yolo_model(roi_yolo, classes=[conf._yolo_class_id_person, conf._yolo_class_id_basebat], augment=True, conf=conf._yolo_detect_hit_confidence, imgsz=conf._yolo_detect_hit_size, verbose=False)[0]
        yolo_detections = []
        for box in results.boxes:
            cls_id = int(box.cls[0])            
            configuration = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])  # ROI 기준 좌표
            # 🎯 seaching just person
            # ROI 기준 → 전체 이미지 기준으로 변환
            global_x = x1 + roi_yolo_left
            global_y = y1 + roi_yolo_top
            global_w = (x2 - x1)
            global_h = (y2 - y1)
            yolo_detections.append((global_x, global_y, global_w, global_h))

        cnt_contours = len(contours)             
        # ─────────────────────────────────────────────────────────────────────────────
        # Check [1] Check Contour count
        # ─────────────────────────────────────────────────────────────────────────────
        if(cnt_contours > 10):
            #fd_log.info(f"🚧[{idx}][{cnt_contours}] too many contours")
            pass
        else:
            for idx_contour, cnt in enumerate(contours):

                # ─────────────────────────────────────────────────────────────────────────────
                # Check each contour
                # ─────────────────────────────────────────────────────────────────────────────

                area = cv2.contourArea(cnt)
                x, y, w, h = cv2.boundingRect(cnt)
                bbox_contour = (x, y, w, h)
                global_x = roi_left + x 
                global_y = roi_top + y                
                global_bbox_contour = (global_x, global_y, w, h)
                gx, gy, gw, gh = global_bbox_contour  # (x, y, w, h)

                global_cx = roi_left + x + w//2
                global_cy = roi_top + y + h//2                                
                candidate_point = np.array([global_cx, global_cy])       

                # draw each contour
                if conf._detection_viewer:
                    #fd_log.info(f"[{idx}][{idx_contour + 1}/{cnt_contours}] Position=({candidate_point}), Area={area}, bbox={global_bbox_contour}")                
                    cv2.putText(frame_vis, f"[{idx}]:{candidate_point}", (global_cx-100,global_cy), cv2.FONT_HERSHEY_SIMPLEX, 0.2, (200, 200, 200), 1)
                    cv2.rectangle(frame_vis, (gx, gy), (gx + gw, gy + gh), (200,200,200),1)

                # ─────────────────────────────────────────────────────────────────────────────
                # Check [1] Check Contour Person
                # ─────────────────────────────────────────────────────────────────────────────
                is_person = False
                for det in yolo_detections:
                    bx, by, bw, bh = det
                    bbox_yolo = (bx - roi_left, by - roi_top, bw, bh)
                    if bboxes_intersect(bbox_contour, bbox_yolo):
                        is_person = True
                        break                    
                if is_person:
                    if conf._detection_viewer:
                        #fd_log.info(f"[{idx}]:[{idx_contour + 1}/{cnt_contours}]🟡[1.person]:position=({candidate_point}), Area={area}, bbox={global_bbox_contour}")
                        cv2.rectangle(frame_vis, (gx, gy), (gx + gw, gy + gh), (0, 0, 255), 2)
                    continue

                # ─────────────────────────────────────────────────────────────────────────────
                # Check [2] check predict and candidate
                # condition : at least more 5 positions
                # ─────────────────────────────────────────────────────────────────────────────
                ret, pred_distance = check_points(candidate_point, predicted_center, conf._ball_detect_hit_margin)
                if(idx > 15):
                    if(pred_distance > conf._ball_detect_hit_margin):
                        #fd_log.info(f"[{idx}]:[{idx_contour + 1}/{cnt_contours}]🟡[2.distance]:distance=({pred_distance:.2f}), threshold={conf._ball_detect_hit_margin}")
                        cv2.rectangle(frame_vis, (gx, gy), (gx + gw, gy + gh), (0, 255, 255), 2) 
                        continue                    

                # ─────────────────────────────────────────────────────────────────────────────
                # Selection [Last] minimum score
                # condition : short distance, less deviation
                # ─────────────────────────────────────────────────────────────────────────────
                if (pred_distance < min_distance_gap and area > 10):
                    min_distance_gap = pred_distance
                    best_center = (global_cx, global_cy)                        
                    best_area = area
                    _, y, _, h_box = cv2.boundingRect(cnt)
                    detected = True     
                    #fd_log.info(f"✅[{idx}]:[{idx_contour + 1}/{cnt_contours}]{best_center}by distance:{min_distance_gap}")                               

        # ─────────────────────────────────────────────────────────────────────────────
        # After Detection
        # ─────────────────────────────────────────────────────────────────────────────
        if detected:
            if conf._detection_viewer:
                fd_log.info(f"\r🎯[Detected][{idx}]:x,y:{best_center}, area:{best_area}")

            measurement = np.array([[np.float32(best_center[0])], [np.float32(best_center[1])]])
            kalman.correct(measurement)                

            last_center = best_center
            fail_count = 0
            success_count += 1
            
            percent_progress = int((idx + 1) / total_count * 100)
            success_percentage = int((success_count) / idx * 100)
            fd_log.info(f"\r🎯[Detection] Progress: {percent_progress}% | Success: {success_percentage}% ({success_count}/{total_count})")
            #fd_log.info(f"🟢[frame {idx}][Success] Posotion: {best_center}, Area: {best_area}, Distance: {best_distance} Score:{best_score}")
            if conf._detection_viewer:
                cv2.putText(frame_vis, f"Tracking: {last_center}", (30, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.circle(frame_vis, last_center, 5, (0, 255, 0), 2)                
                
        else:
            fail_count += 1
            if predicted_center is not None and last_center is not None:
                last_center = predicted_center                                
                #fd_log.error(f"❌[frame {idx}][Fail:{fail_count}] Expected Position: {last_center}")
            else:
                #fd_log.error(f"❌[frame {idx}][Fail Detect]  1st Detection - Expected Position: {last_center}")           
                continue

        # add to ball position
        global_center = last_center
        trajectory.append(global_center)
        trajectory_indices.append(idx)
        # actual data
        if detected:
            ball_pos_arr[idx] = global_center

        
        # ─────────────────────────────────────────────────────────────────────────────
        # ROI Reposition
        # ─────────────────────────────────────────────────────────────────────────────
        roi_center = last_center
        roi_left, roi_right, roi_top, roi_bottom = reposition_roi_centered(roi_center,frame_size,op_roi_hit_width,op_roi_hit_height)

        # ─────────────────────────────────────────────────────────────────────────────
        # Debug
        # ─────────────────────────────────────────────────────────────────────────────
        if conf._detection_viewer:
            cv2.putText(frame_vis, f"Frame: {idx}", (30, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 1)

            for i in range(1, len(trajectory)):
                cv2.line(frame_vis, trajectory[i - 1], trajectory[i], (255, 255, 0), 1)
                cv2.circle(frame_vis, trajectory[i], 1, (255, 255, 0), 1)            

            box_color = (255, 255, 255) if detected else (128, 128, 128)
            cv2.rectangle(frame_vis, (roi_left, roi_top), (roi_right, roi_bottom), box_color, 2)          
            show_resized_fullscreen("[Detect]Ball Detection", frame_vis, screen_res=(1920, 1080))

            # ▶ ROI만 잘라서 확대해서 별도 창으로 표시
            roi_crop = frame_vis[roi_top:roi_bottom, roi_left:roi_right]
            roi_zoomed = cv2.resize(roi_crop, None, fx=5.0, fy=5.0, interpolation=cv2.INTER_LINEAR)
            cv2.imshow(window_name, roi_zoomed)
            # 대기 및 종료
            cv2.waitKey(0)            

    conf._detect_success_count  = success_count
    conf._detect_frame_count    = total_count

    # memory release (안전 확인 후 처리)
    try:
        if 'gpu_prev' in locals() and gpu_prev is not None:
            gpu_prev.release()
            del gpu_prev 
        if 'gpu_curr' in locals() and gpu_curr is not None:
            gpu_curr.release()
            del gpu_curr 
    except Exception as e:
        fd_log.warning(f"⚠️ GPU release error: {e}")
    
    # close windows
    try:
        if conf._detection_viewer:
            cv2.destroyWindow(window_name)
    except cv2.error as e:
        fd_log.warning(f"Warning: failed to destroy window {window_name}: {e}")
    
    # length check
    ball_pos_arr = ball_pos_arr[:len(frames)]
    return ball_pos_arr

def reposition_roi_centered(roi_center, frame_size, op_roi_hit_width, op_roi_hit_height):
    frame_w, frame_h = frame_size
    roi_half_width = op_roi_hit_width // 2
    roi_half_height = op_roi_hit_height // 2

    # 중심 좌표 정수로 변환
    roi_center_x = int(roi_center[0])
    roi_center_y = int(roi_center[1])

    # 중심이 프레임 밖으로 나가지 않도록 클램핑
    roi_center_x = np.clip(roi_center_x, roi_half_width, frame_w - roi_half_width)
    roi_center_y = np.clip(roi_center_y, roi_half_height, frame_h - roi_half_height)

    # ROI 영역 계산 (고정된 크기 유지)
    roi_left   = roi_center_x - roi_half_width
    roi_right  = roi_center_x + roi_half_width
    roi_top    = roi_center_y - roi_half_height
    roi_bottom = roi_center_y + roi_half_height

    return roi_left, roi_right, roi_top, roi_bottom

# ─────────────────────────────────────────────────────────────────────────────
# def bboxes_intersect(boxA, boxB):
# [owner] hongsu jung
# [date] 2025-05-12
# ─────────────────────────────────────────────────────────────────────────────
def bboxes_intersect(boxA, boxB):
    ax, ay, aw, ah = boxA
    bx, by, bw, bh = boxB
    return not (
        ax + aw < bx or  # A의 오른쪽이 B의 왼쪽보다 왼쪽에 있음
        bx + bw < ax or  # B의 오른쪽이 A의 왼쪽보다 왼쪽에 있음
        ay + ah < by or  # A의 아래가 B의 위보다 위에 있음
        by + bh < ay     # B의 아래가 A의 위보다 위에 있음
    )

# ─────────────────────────────────────────────────────────────────────────────
# def bboxes_intersect(boxA, boxB):
# [owner] hongsu jung
# [date] 2025-05-12
# ─────────────────────────────────────────────────────────────────────────────
def angle_between_vectors(v1, v2):
    """두 벡터 사이의 각도 (도 단위) 반환"""
    if np.linalg.norm(v1) == 0 or np.linalg.norm(v2) == 0:
        return 0  # 정지 상태는 각도 없음
    unit_v1 = v1 / np.linalg.norm(v1)
    unit_v2 = v2 / np.linalg.norm(v2)
    dot_product = np.clip(np.dot(unit_v1, unit_v2), -1.0, 1.0)
    angle_rad = np.arccos(dot_product)
    angle_deg = np.degrees(angle_rad)
    return angle_deg

# ─────────────────────────────────────────────────────────────────────────────
# def bboxes_intersect(boxA, boxB):
# [owner] hongsu jung
# [date] 2025-05-12
# ─────────────────────────────────────────────────────────────────────────────
def check_angle_on_candidate(ball_pos_arr, new_pos, idx, threshold_deg = 15.0):

    # 1. 과거에서 유효한 두 포인트 + 인덱스 추출
    valid_points = []
    valid_indices = []
    i = idx - 1
    while i >= 0 and len(valid_points) < 2:
        if ball_pos_arr[i] is not None:
            valid_points.insert(0, np.array(ball_pos_arr[i]))
            valid_indices.insert(0, i)
        i -= 1

    if len(valid_points) < 2:
        return True, None, None  # 비교할 유효한 벡터 부족

    p1, p2 = valid_points
    i1, i2 = valid_indices
    p3 = np.array(new_pos)

    # 벡터 계산
    v1 = p2 - p1
    v2 = p3 - p2

    # 프레임 간격으로 거리 정규화 (일종의 속도)
    dt1 = i2 - i1
    dt2 = idx - i2

    if dt1 == 0 or dt2 == 0:
        return True, 0.0, 0.0  # 시간 간격 0은 정지 처리

    movement_base = np.linalg.norm(v1) / dt1

    # 이동량에 따라 threshold를 보정
    if movement_base < 1e-3:
        return True, None, None  # 비교할 벡터 부족 or None일 경우
    base_distance = 20.0
    adjusted_threshold = threshold_deg * (base_distance / movement_base)
    
    # 각도 계산
    angle = angle_between_vectors(v1, v2)
    if(angle > adjusted_threshold):
        return False, angle, adjusted_threshold
    
    return True, angle, adjusted_threshold

# ─────────────────────────────────────────────────────────────────────────────
# def bboxes_intersect(boxA, boxB):
# [owner] hongsu jung
# [date] 2025-05-12
# ─────────────────────────────────────────────────────────────────────────────
def check_speed_on_candidate(ball_pos_arr, new_pos, idx):

    # 1. 과거에서 유효한 두 포인트 + 인덱스 추출
    valid_points = []
    valid_indices = []
    i = idx - 1
    while i >= 0 and len(valid_points) < 2:
        if ball_pos_arr[i] is not None:
            valid_points.insert(0, np.array(ball_pos_arr[i]))
            valid_indices.insert(0, i)
        i -= 1

    if len(valid_points) < 2:
        return True, None, None  # 비교할 유효한 벡터 부족

    p1, p2 = valid_points
    i1, i2 = valid_indices
    p3 = np.array(new_pos)

    # 벡터 계산
    v1 = p2 - p1
    v2 = p3 - p2

    # 프레임 간격으로 거리 정규화 (일종의 속도)
    dt1 = i2 - i1
    dt2 = idx - i2

    if dt1 == 0 or dt2 == 0:
        return True, 0.0, 0.0  # 시간 간격 0은 정지 처리

    movement_base = np.linalg.norm(v1) / dt1
    movement_new = np.linalg.norm(v2) / dt2

    min_valid_base_distance = 2.5
    if movement_base < min_valid_base_distance:
        return True, movement_base, movement_new  # 거의 정지 상태 → 무조건 허용
        
    # 3. 거리 비율 검사
    distance_ratio = movement_new / movement_base
    if distance_ratio > 4 or distance_ratio < 0.3:
        return False, movement_base, movement_new


    return True, movement_base, movement_new

# ─────────────────────────────────────────────────────────────────────────────
# def get_speed(last_center, predicted_center):
# [owner] hongsu jung
# [date] 2025-05-12
# ─────────────────────────────────────────────────────────────────────────────
def get_speed(last_center, predicted_center):
    if last_center is None or predicted_center is None:
        return None  # 유효하지 않은 경우

    p1 = np.array(last_center)
    p2 = np.array(predicted_center)
    distance = np.linalg.norm(p2 - p1)  # 유클리드 거리
    return distance

# ─────────────────────────────────────────────────────────────────────────────
# def get_jersey_number(last_center, predicted_center):
# [owner] hongsu jung
# [date] 2025-06-03
# ─────────────────────────────────────────────────────────────────────────────
def get_jersey_number(frame, frame_person, roi_back_number, offset_x , offset_y ):
    
    # 가장 높은 confidence 찾기용 변수
    best_number = conf._object_number_unknown
    best_confidence = 0.0
    best_number_roi = []
    # ★ results 초기화
    results = []

    # check minimum detect roi
    width   = roi_back_number[1][0] - roi_back_number[0][0]
    height  = roi_back_number[1][1] - roi_back_number[0][1]
    if(width < 40 or height < 60):
        return best_number

    # margin 적용
    x1 = max(0, roi_back_number[0][0] - 10)
    y1 = max(0, roi_back_number[0][1] - 10)
    x2 = roi_back_number[1][0] + 10
    y2 = max(0, roi_back_number[1][1] - 35)

    
    roi = [[x1,y1],[x2,y2]]
    # 유효성 검사
    if x2 > x1 and y2 > y1:
        roi = [[x1, y1], [x2, y2]]
        frame_roi_number = frame_resize(frame_person, roi, 1)

        # 다시 빈 이미지 확인 (추가 안전)
        if frame_roi_number is not None and frame_roi_number.size > 0:
            reader = get_easyocr_reader()
            results = reader.readtext(frame_roi_number)
        else:
            return conf._object_number_unknown  # 빈 경우는 여기서 안전하게 종료
    else:
        return conf._object_number_unknown  # ROI 유효하지 않은 경우도 종료
    
    # 시각화 (선택)
    if conf._detection_viewer:
        cv2.rectangle(frame, ([roi[0][0]+offset_x,roi[0][1]+offset_y] ), ([roi[1][0]+offset_x,roi[1][1]+offset_y]), (200, 200, 200), 2)
        
    # 결과 출력
    for (bbox, text, prob) in results:
        # 최소 80% 이상
        if(prob < 0.9):
            continue
        # 숫자 추출 (정규식 사용: 숫자만 남김)
        numbers = re.findall(r'\d+', text)
        if not numbers:
            continue  # 숫자 없으면 skip
        number_text = numbers[0]  # 첫 번째 숫자 그룹 사용 (ex: "23")

        # fd_log.info(f"Detected Number: {text}, Confidence: {prob}")
        # 좌상단 (x1, y1)
        x1 = int(bbox[0][0]) + offset_x + roi[0][0]
        y1 = int(bbox[0][1]) + offset_y + roi[0][1]

        # 우하단 (x2, y2)
        x2 = int(bbox[2][0]) + offset_x + roi[0][0]
        y2 = int(bbox[2][1]) + offset_y + roi[0][1]
        
        # 가장 높은 confidence 비교
        if prob > best_confidence:
            best_confidence = prob
            best_number = text  
            best_number_roi = [[x1,y1],[x2,y2]]

    # 시각화 (선택)
    if conf._detection_viewer:
        if(best_confidence > 0):
            cv2.rectangle(frame, (best_number_roi[0]), (best_number_roi[1]), (255, 255, 0), 2)
            cv2.putText(frame, text, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255,255,0), 2)
    return best_number
