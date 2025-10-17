# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_draw
# - 2024/10/28
# - Hongsu Jung
# https://4dreplay.atlassian.net/wiki/x/F4DofQ
# ─────────────────────────────────────────────────────────────────────────────#
# L/O/G/
# check     : ✅
# warning   : ⚠️
# error     : ❌
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import os
import threading
import subprocess
import numpy as np
import matplotlib.pyplot as plt
from fd_utils.fd_logging        import fd_log

from PIL import Image, ImageDraw, ImageFont
from scipy.interpolate import CubicSpline

import fd_utils.fd_config           as conf
import fd_utils.fd_data_manager     as datamgr
from fd_utils.fd_file_edit          import fd_common_ffmpeg_args_pre
from fd_utils.fd_file_edit          import fd_common_ffmpeg_args_post


# ─────────────────────────────────────────────────────────────────────────────
# def get_layer_curve_multi_line
# [owner] hongsu jung
# [date] 2025-04-01
# ─────────────────────────────────────────────────────────────────────────────#
def draw_png(draw_line, pos_c, frameindex):
    # 알파 채널 분리
    png = cv2.imread(conf._bat_hit_png, cv2.IMREAD_UNCHANGED)
    if png is None:
        fd_log.error("❌ Could not load PNG:", conf._bat_hit_png)
        return draw_line
    

    scale = 0.35
    rotate_speed = 20
    apply_blur = True
    orig_h, orig_w = png.shape[:2]

    # 비율 유지하며 리사이즈
    resized_w = int(orig_w * scale)
    resized_h = int(orig_h * scale)
    png = cv2.resize(png, (resized_w, resized_h), interpolation=cv2.INTER_AREA)

    # 회전 중심 및 각도
    angle = (-frameindex * rotate_speed) % 360
    center = (resized_w // 2, resized_h // 2)
    rot_mat = cv2.getRotationMatrix2D(center, angle, 1.0)

    # 회전 수행
    rotated = cv2.warpAffine(png, rot_mat, (resized_w, resized_h),
                             flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT,
                             borderValue=(0, 0, 0, 0))

    # 선택적으로 Blur 처리
    if apply_blur:
        rotated = cv2.GaussianBlur(rotated, (3, 3), sigmaX=0.5)

    # 채널 분리
    b, g, r, a = cv2.split(rotated)
    overlay = cv2.merge((b, g, r))        # BGR 이미지
    alpha = a.astype(float) / 255.0       # 알파 0~1
    alpha = cv2.merge([alpha, alpha, alpha])

    # 위치 계산 (중심 정렬)
    center_x, center_y = int(pos_c[0]), int(pos_c[1])
    x, y = center_x - resized_w // 2, center_y - resized_h // 2

    # 유효 위치에만 합성
    if 0 <= x < draw_line.shape[1] - resized_w and 0 <= y < draw_line.shape[0] - resized_h:
        roi = draw_line[y:y+resized_h, x:x+resized_w].astype(float)
        fg = overlay.astype(float)

        # 알파 블렌딩
        blended = alpha * fg + (1 - alpha) * roi
        blended = blended.astype(np.uint8)
        draw_line[y:y+resized_h, x:x+resized_w] = blended

    return draw_line

# ─────────────────────────────────────────────────────────────────────────────
# def get_pitch_color
# [owner] hongsu jung
# [date] 2025-04-02
# ─────────────────────────────────────────────────────────────────────────────#
def get_pitch_color(pitch_type):
    
    '''
    # color from KBO
    conf._color_kbo_blue         = (97,37,0)
    conf._color_kbo_lightblue    = (239,174,0)
    conf._color_kbo_mint         = (189,165,0)
    conf._color_kbo_red          = (36,28,237)
    conf._color_kbo_silver       = (192,190,188)
    conf._color_kbo_gold         = (119,161,179)
    _color_kbo_orange
    conf._color_kbo_white        = (255,255,255)
    '''
        
    match pitch_type:
        case "Fastball":
            start_color = conf._color_kbo_red
            end_color = (0, 0, 255)
        case "Slider":
            start_color = conf._color_kbo_gold
            end_color = (255, 0, 0)
        case "ChangeUp":
            start_color = conf._color_kbo_blue
            end_color = (0, 255, 0)
        case "Splitter":
            start_color = conf._color_kbo_silver
            end_color = (0, 255, 255)
        case "Curveball":
            start_color = conf._color_kbo_lightblue
            end_color = (255, 0, 255)
        case "Cutter":
            start_color = conf._color_kbo_mint
            end_color = (255, 255, 0)
        case "Sinker":
            start_color = conf._color_kbo_orange
            end_color = (0, 128, 255)
        case _:
            start_color = (255, 255, 255)  # 흰색 (밝은 색)
            end_color = (0, 0, 0)
    return start_color, end_color

# ─────────────────────────────────────────────────────────────────────────────
# def draw_hit_ball_trajectory(frame, arr, start_frame, draw_last, start_color, end_color, line_thickness, upscale_factor=1)
# [owner] hongsu jung
# Test Version -- joonho.kim
# [date] 2025-02-14
# 공의 궤적을 고해상도로 그린 후 원래 크기로 합성    
# ─────────────────────────────────────────────────────────────────────────────#
def draw_hit_ball_trajectory(frame, arr, start_frame, draw_last, start_color, end_color, line_thickness, upscale_factor=1):
    h, w = frame.shape[:2]
    upscale_size = (int(w * upscale_factor), int(h * upscale_factor))
    # 1. 업스케일된 overlay 생성
    overlay = cv2.resize(frame, upscale_size, interpolation=cv2.INTER_LINEAR)

    # ball의 위치를 중심으로 Zoom 과 move를 한 이후 최종     
    # fhd output

    num_steps = max(draw_last - start_frame, 1)
    for i in range(start_frame, draw_last):
        pos_1 = (
            int(arr[i][0] * upscale_factor),
            int(arr[i][1] * upscale_factor)
        )
        pos_2 = (
            int(arr[i + 1][0] * upscale_factor),
            int(arr[i + 1][1] * upscale_factor)
        )

        alpha = (i - start_frame) / num_steps
        line_color = (
            int(end_color[0] * alpha + start_color[0] * (1 - alpha)),
            int(end_color[1] * alpha + start_color[1] * (1 - alpha)),
            int(end_color[2] * alpha + start_color[2] * (1 - alpha))
        )

        cv2.line(overlay, pos_1, pos_2, line_color, line_thickness)

    # 2. 다시 원래 사이즈로 downscale
    w = conf._resolution_fhd_width
    h = conf._resolution_fhd_height

    overlay_resized = cv2.resize(overlay, (w, h), interpolation=cv2.INTER_LINEAR)
    frame_resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)

    # 3. 부드러운 합성
    blended_frame = cv2.addWeighted(overlay_resized, 0.2, frame_resized, 0.8, 0.0)
    return blended_frame

# ─────────────────────────────────────────────────────────────────────────────
# def draw_hit_ball_tracking_single(frame, trajectory, start_idx, end_idx, tot_count, start_color, end_color, thickness, upscale_factor = 1):
# [owner] hongsu jung
# [date] 2025-02-14
# ─────────────────────────────────────────────────────────────────────────────#
def draw_hit_ball_tracking_single(frame, arr, start_idx, end_idx, tot_count, start_color, end_color, thickness, upscale_factor = 1):
    
    h, w = frame.shape[:2]
    upscale_w, upscale_h = int(w * upscale_factor), int(h * upscale_factor)

    # 🎨 1. 업스케일된 원본과 오버레이 생성
    frame_up = cv2.resize(frame, (upscale_w, upscale_h), interpolation=cv2.INTER_LINEAR)
    overlay = frame_up.copy()

    # 🎯 2. 유효 좌표 업스케일 반영
    points = [
        (int(arr[i][0] * upscale_factor), int(arr[i][1] * upscale_factor))
        for i in range(start_idx, end_idx)
        if 0 <= i < len(arr) and not np.array_equal(arr[i], (-1, -1))
    ]
    if len(points) < 2:
        return cv2.resize(frame, (conf._output_width, conf._output_height))  # fallback

    # ✏️ 3. Trajectory 라인 그리기 (업스케일 좌표 기준)
    # 🔁 [수정됨] 궤적이 앞에서부터 위로 쌓이게 하기 위해 역순으로 그림
    num_steps = max(len(points) - 1, 1)
    for i in reversed(range(1, len(points))):  # ⬅️ 변경된 부분
        alpha = i / num_steps
        line_color = (
            int(end_color[0] * alpha + start_color[0] * (1 - alpha)),
            int(end_color[1] * alpha + start_color[1] * (1 - alpha)),
            int(end_color[2] * alpha + start_color[2] * (1 - alpha))
        )
        cv2.line(overlay, points[i - 1], points[i], line_color, thickness, cv2.LINE_AA)

    # 🔁 4. 최종 리사이즈
    out_w, out_h = conf._output_width, conf._output_height
    overlay_resized = cv2.resize(overlay, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    frame_resized = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

    # 🌀 5. 부드러운 블렌딩
    blended = cv2.addWeighted(overlay_resized, 0.8, frame_resized, 0.2, 0.0)

    return blended

# ─────────────────────────────────────────────────────────────────────────────
# def draw_hit_ball_tracking_multi(frame, trajectory, start_idx, end_idx, tot_count, start_color, end_color, thickness, zoom_scale)
# [owner] hongsu jung
# [date] 2025-07-04
# ─────────────────────────────────────────────────────────────────────────────#
def draw_hit_ball_tracking_multi(frame, arrs, start_end_list, draw_last_list, start_color, end_colors, thickness, upscale_factor=1):

    h, w = frame.shape[:2]
    out_w, out_h = conf._output_width, conf._output_height
    upscale_w, upscale_h = int(w * upscale_factor), int(h * upscale_factor)

    # 🎨 1. 업스케일된 원본 및 오버레이
    frame_up = cv2.resize(frame, (upscale_w, upscale_h), interpolation=cv2.INTER_LINEAR)
    overlay = frame_up.copy()

    # ✏️ 2. Trajectories for all arrs
    for idx, arr in enumerate(arrs):
        start_idx, end_idx = start_end_list[idx]
        draw_last = draw_last_list[idx]

        points = [
            (int(arr[i][0] * upscale_factor), int(arr[i][1] * upscale_factor))
            for i in range(start_idx, draw_last)
            if 0 <= i < len(arr) and not np.array_equal(arr[i], (-1, -1))
        ]
        if len(points) < 2:
            continue

        num_steps = max(len(points) - 1, 1)
        # 🔁 [수정됨] trajectory를 역순으로 그려 앞쪽 궤적이 위로 보이게
        for i in reversed(range(1, len(points))):  # ⬅️ 변경된 부분
            alpha = i / num_steps
            line_color = (
                int(end_colors[idx][0] * alpha + start_color[0] * (1 - alpha)),
                int(end_colors[idx][1] * alpha + start_color[1] * (1 - alpha)),
                int(end_colors[idx][2] * alpha + start_color[2] * (1 - alpha))
            )
            cv2.line(overlay, points[i - 1], points[i], line_color, thickness, lineType=cv2.LINE_AA)

    # 🔁 3. 출력 사이즈로 다운스케일
    overlay_resized = cv2.resize(overlay, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    frame_resized = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

    # 🌀 4. 부드러운 블렌딩
    blended = cv2.addWeighted(overlay_resized, 0.8, frame_resized, 0.2, 0.0)
    return blended

# ─────────────────────────────────────────────────────────────────────────────
# def draw_pitcher_ball_tracking_single
# [owner] joonho kim
# [date] 2025-04-14
# ─────────────────────────────────────────────────────────────────────────────#
def draw_pitcher_ball_tracking_single(frame, arr, draw_first, draw_last, line_thickness, upscale_factor=1):

    white = np.array([255, 255, 255], dtype=np.float32)
    red = np.array(conf._color_kbo_red, dtype=np.float32)

    height, width = frame.shape[:2]
    upscaled_size = (int(width * upscale_factor), int(height * upscale_factor))

    # 🎨 업스케일된 overlay 생성
    overlay = cv2.resize(frame, upscaled_size, interpolation=cv2.INTER_LINEAR)

    if draw_last - draw_first < 1:
        return frame

    trail_start = draw_first
    trail_end = draw_last

    pts = []
    for i in range(trail_start, trail_end + 1):
        if i >= len(arr) or arr[i] is None or np.isnan(arr[i][0:2]).any():
            continue
        pt = (np.array(arr[i][0:2]) * upscale_factor).astype(int)
        pts.append(pt)

    if len(pts) < 2:
        return frame

    for i in range(len(pts) - 1):
        ratio = i / (len(pts) - 1) if len(pts) > 1 else 0
        color = (1 - ratio) * white + ratio * red
        color = tuple(map(int, color))
        cv2.line(overlay, tuple(pts[i]), tuple(pts[i + 1]), color, line_thickness, lineType=cv2.LINE_AA)

    # 🎯 다시 원래 해상도로 리사이즈해서 합성
    # fhd output
    w = conf._resolution_fhd_width
    h = conf._resolution_fhd_height

    overlay_resized = cv2.resize(overlay, (w, h), interpolation=cv2.INTER_LINEAR)
    frame_resized = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)

    frame_draw = cv2.addWeighted(overlay_resized, 0.8, frame_resized, 0.2, 0.0)
    return frame_draw

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_batter_ball_tracking_single(frame, arr, frameindex, draw_first, draw_last):
# drawing the bat swing and ball tracking
# [owner] joonho kim
# [date] 2025-04-09
# ─────────────────────────────────────────────────────────────────────────────#
def draw_batter_ball_tracking_single(clean_frame, arr, frameindex, draw_first, draw_last):
    try:
        frame = clean_frame.copy()
        thickness = 10 * conf._pitcher_draw_upscale_factor
        upscale_factor = conf._pitcher_draw_upscale_factor

        # 🎨 색상 정의
        white = np.array([255, 255, 255], dtype=np.float32)
        red = np.array(conf._color_kbo_red, dtype=np.float32)
        blue = np.array(conf._color_kbo_blue, dtype=np.float32)

        # 🎯 원본 크기 및 업스케일 크기 계산
        original_h, original_w = frame.shape[:2]
        upscaled_size = (int(original_w * upscale_factor), int(original_h * upscale_factor))

        # 🎨 업스케일된 투명 레이어 생성
        overlay = cv2.resize(frame, upscaled_size, interpolation=cv2.INTER_LINEAR)
        pitch_pts = []
        hit_pts = []

        # ✅ Step 1: -1,-1 또는 NaN 이 처음 등장하는 인덱스 기준으로 draw_last 잘라내기
        cutoff_index = len(arr)
        for i in range(draw_first, min(draw_last + 1, len(arr))):
            pt_raw = arr[i]
            if pt_raw is None or not isinstance(pt_raw, (np.ndarray, list, tuple)) or len(pt_raw) < 2:
                cutoff_index = i
                break
            pt_arr = np.array(pt_raw[:2], dtype=np.float32)
            if np.all(pt_arr == -1) or np.isnan(pt_arr).any():
                cutoff_index = i
                break
        draw_last = min(draw_last, cutoff_index - 1)

        # ✅ Step 2: 실제 그리기
        for i in range(draw_first, draw_last + 1):
            pt_raw = arr[i]
            if pt_raw is None or not isinstance(pt_raw, (np.ndarray, list, tuple)) or len(pt_raw) < 2:
                continue
            pt_arr = np.array(pt_raw[:2], dtype=np.float32)
            if np.isnan(pt_arr).any() or np.all(pt_arr == -1):
                continue

            pt = (pt_arr * upscale_factor).astype(int)

            if i == conf._batter_hitting_first_index:
                if (
                    isinstance(conf._batter_intersect_pos, (list, tuple, np.ndarray))
                    and len(conf._batter_intersect_pos) >= 2
                ):
                    intersect = (np.array(conf._batter_intersect_pos[0:2]) * upscale_factor).astype(int)
                    hit_pts.append((i, intersect))
                    hit_pts.append((i, pt))
            elif i <= conf._batter_pitching_last_index:
                pitch_pts.append((i, pt))
            else:
                hit_pts.append((i, pt))

        # 🎯 투구: 흰 → 빨강
        for i in range(len(pitch_pts) - 1):
            _, pt1 = pitch_pts[i]
            _, pt2 = pitch_pts[i + 1]
            ratio = i / (len(pitch_pts) - 1) if len(pitch_pts) > 1 else 0
            color = (1 - ratio) * white + ratio * red
            color = tuple(map(int, color))
            cv2.line(overlay, tuple(pt1), tuple(pt2), color, thickness, lineType=cv2.LINE_AA)

        # ✅ 중간 연결선: 투구 마지막 → 타구 첫번째
        if pitch_pts and hit_pts:
            pt1 = pitch_pts[-1][1]
            pt2 = hit_pts[0][1]
            color = tuple(map(int, red))
            cv2.line(overlay, tuple(pt1), tuple(pt2), color, thickness, lineType=cv2.LINE_AA)

        # 🎯 타구: 파랑 → 흰
        for i in range(len(hit_pts) - 1):
            _, pt1 = hit_pts[i]
            _, pt2 = hit_pts[i + 1]
            ratio = i / (len(hit_pts) - 1) if len(hit_pts) > 1 else 0
            color = (1 - ratio) * blue + ratio * white
            color = tuple(map(int, color))
            cv2.line(overlay, tuple(pt1), tuple(pt2), color, thickness, lineType=cv2.LINE_AA)

        # 🔄 overlay를 다시 원본 크기로 resize 후 합성
        overlay_resized = cv2.resize(overlay, (original_w, original_h), interpolation=cv2.INTER_LINEAR)
        frame_draw = cv2.addWeighted(overlay_resized, 0.8, frame, 0.2, 0.0)

        # 🥎 공 PNG 표시
        if conf._batter_pitching_last_index < draw_last:
            draw_png(frame_draw, conf._batter_intersect_pos, frameindex)

    except Exception as e:
        fd_log.info(f"[Warning] get_layer_bent_single_line_gradient : {e} (i={locals().get('i', 'N/A')}, len(arr)={len(arr)})")

    return frame_draw


# ─────────────────────────────────────────────────────────────────────────────
# def get_layer_curve_multi_line
# [owner] hongsu jung
# [date] 2025-04-01
# ─────────────────────────────────────────────────────────────────────────────#
def get_layer_curve_multi_line(frame, arrs, arrs_idx, frameindex, tot_count, line_thickness, upscale_factor=1):    
    
    height, width = frame.shape[:2]
    new_size = (width * upscale_factor, height * upscale_factor)
    draw_line = np.zeros((new_size[1], new_size[0], frame.shape[2]), dtype=frame.dtype)    

    def get_interp_count(index):
        ratio = index / total_steps
        return max(1, int(5 * (1 - ratio)))  # 초반엔 10, 후반엔 2까지 줄임
    
    for traj_idx, arr in enumerate(arrs):
        if(traj_idx > arrs_idx):
            break
        elif(traj_idx < arrs_idx):
            start_frame, draw_last = get_start_end_time(arrs[traj_idx])
        elif(arrs_idx == traj_idx):
            start_frame, draw_last = get_start_end_time(arrs[traj_idx])
            draw_last = frameindex
        
        total_steps = max(1, draw_last - start_frame)

        start_color, end_color = get_pitch_color(conf._pkl_list[traj_idx][0])
        step_counter = 0
        for i in range(start_frame, draw_last):
            # check data     
            if((arr[i][0:2][0] == -1 and arr[i][0:2][1] == -1) or (arr[i+1][0:2][0] == -1 and arr[i+1][0:2][1] == -1)):
                break        
            if(np.isnan(arr[i][0:2][0]).any() == True or np.isnan(arr[i+1][0:2][0]).any() == True):
                break
            pos_1 = np.array(arr[i][0:2]) * upscale_factor
            pos_2 = np.array(arr[i + 1][0:2]) * upscale_factor
            interp_count = get_interp_count(i - start_frame)

            # 보간 지점 생성
            x_interp = np.linspace(pos_1[0], pos_2[0], interp_count + 2)
            y_interp = np.linspace(pos_1[1], pos_2[1], interp_count + 2)

            for j in range(1, len(x_interp)):
                pt1 = (int(x_interp[j - 1]), int(y_interp[j - 1]))
                pt2 = (int(x_interp[j]), int(y_interp[j]))

                if pt1 == pt2:
                    continue  # 중복 선 생략

                # 그라데이션 색상 계산 (선 단위)
                alpha = step_counter / total_steps
                color = (
                    int(end_color[0] * alpha + start_color[0] * (1 - alpha)),
                    int(end_color[1] * alpha + start_color[1] * (1 - alpha)),
                    int(end_color[2] * alpha + start_color[2] * (1 - alpha)),
                )           
                
                cv2.line(draw_line, pt1, pt2, color, line_thickness, lineType=cv2.LINE_AA)  
                step_counter += 1

    if upscale_factor > 1:
        draw_line = cv2.resize(draw_line, (conf._output_width, conf._output_height), interpolation=cv2.INTER_AREA)

    return draw_line


def draw_fade_in_image(frame, overlay_img, x, y, alpha):
    """
    PNG 이미지를 지정한 위치에 alpha값을 이용해 fade-in 효과로 표시합니다.
   
    frame: 현재 프레임 (BGR)
    image_path: PNG 이미지 경로
    x, y: 프레임에 이미지를 그릴 좌측 상단 좌표
    alpha: 0.0 ~ 1.0 사이의 투명도 값    """
    if overlay_img is None:
        fd_log.info(f"not image")
        return frame
 
    # 크기 조정이 필요하면 여기서 처리
    # overlay_img = cv2.resize(overlay_img, (width, height))
 
    h, w = overlay_img.shape[:2]
    overlay_bgr = overlay_img[:, :, :3]
    overlay_alpha = overlay_img[:, :, 3] / 255.0 * alpha  # 알파채널 × 전체 알파값
 
    # ROI 설정 (겹치는 영역)
    roi = frame[y:y+h, x:x+w]
 
    # BGR 채널에 대해 blending
    for c in range(3):
        roi[:, :, c] = (overlay_alpha * overlay_bgr[:, :, c] + (1 - overlay_alpha) * roi[:, :, c])
 
    # 결과를 원래 프레임에 적용
    frame[y:y+h, x:x+w] = roi
    return frame

# ─────────────────────────────────────────────────────────────────────────────
# def blend_overlay(draw_img, overlay_img, x, y, alpha=1.0):
# [owner] joonho kim
# [date] 2025-04-11
# ─────────────────────────────────────────────────────────────────────────────#
def blend_overlay(base_img, overlay_img, x, y, alpha=1.0, scale=0.24):
    if overlay_img is None:
        fd_log.error("Error: overlay_img None")
        return base_img

    # 🔹 이미지 축소/확대 (기본 1.0 = 원본 크기)
    if scale != 1.0:
        new_w = int(overlay_img.shape[1] * scale)
        new_h = int(overlay_img.shape[0] * scale)
        overlay_img = cv2.resize(overlay_img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    h, w = overlay_img.shape[:2]
    overlay_bgr = overlay_img[:, :, :3]
    overlay_alpha = overlay_img[:, :, 3] / 255.0 * alpha

    height, width = base_img.shape[:2]
    x1 = max(0, x)
    y1 = max(0, y)
    x2 = min(width, x + w)
    y2 = min(height, y + h)

    alpha_crop = overlay_alpha[0:(y2 - y1), 0:(x2 - x1)]
    bgr_crop = overlay_bgr[0:(y2 - y1), 0:(x2 - x1)]
    roi = base_img[y1:y2, x1:x2]

    for c in range(3):
        roi[:, :, c] = (alpha_crop * bgr_crop[:, :, c] +
                        (1 - alpha_crop) * roi[:, :, c]).astype(np.uint8)

    roi[:, :, 3] = (alpha_crop * 255 + (1 - alpha_crop) * roi[:, :, 3]).astype(np.uint8)
    return base_img

 # ─────────────────────────────────────────────────────────────────────────────
# def set_baseball_info_layer(frame, arr, start_pitching, end_pitching, start_hit, end_hit, start_color, end_color, line_thickness, upscale_factor=1):
# [owner] hongsu jung
# [date] 2025-03-24
# ─────────────────────────────────────────────────────────────────────────────#
def set_baseball_info_layer():
    type_target = conf._type_target
    height, width = conf._output_height, conf._output_width
    draw_info_img = np.zeros((height, width, 4), dtype=np.uint8)
    draw_info_img[:, :, 3] = 0  # 완전 투명 초기화
    alpha = 1  # 또는 conf._draw_max_alpha

    match type_target:
        case conf._type_baseball_pitcher | conf._type_baseball_pitcher_multi:

             #data box
            draw_info_img = blend_overlay(draw_info_img, conf._pitch_box_img, conf._pitch_box_x, conf._pitch_box_y, alpha)
            #name box
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, conf._pitch_name_box_x, conf._pitch_name_box_y, alpha, 0.28)
            #kbo logo
            draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_right_x, conf._kbo_logo_right_y, alpha, 0.1)

            draw_info_img = draw_fade_in_text_pitch(draw_info_img,
                                                    conf._pitch_type,
                                                    conf._release_spinrate,
                                                    conf._release_speed,
                                                    conf._font_path_main,
                                                    conf._font_path_sub,
                                                    conf._pitch_box_x,
                                                    conf._pitch_box_y,
                                                    alpha)

        case conf._type_baseball_batter_LH:
            x1, y1 = conf._bat_box1_left_x, conf._bat_box1_left_y
            x2, y2 = conf._bat_box2_left_x, conf._bat_box2_left_y

            x3, y3 = conf._bat_name_box1_left_x, conf._bat_name_box1_left_y
            x4, y4 = conf._bat_name_box2_left_x, conf._bat_name_box2_left_y
            
            #data box
            draw_info_img = blend_overlay(draw_info_img, conf._bat_box1_img, x1, y1, alpha)
            draw_info_img = blend_overlay(draw_info_img, conf._bat_box2_img, x2, y2, alpha)
            #name box
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, x3, y3, alpha)
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha)
            #kbo logo
            draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_right_x, conf._kbo_logo_right_y, alpha, 0.1)

            draw_info_img = draw_fade_in_text_bat(draw_info_img,
                                                  conf._pitch_type,
                                                  conf._release_spinrate,
                                                  conf._release_speed,
                                                  conf._launch_speed,
                                                  conf._launch_v_angle,
                                                  conf._landingflat_distance,
                                                  conf._font_path_main,
                                                  conf._font_path_sub,
                                                  x1, y1,
                                                  alpha)
        
        case conf._type_baseball_batter_RH:
            x1, y1 = conf._bat_box1_right_x, conf._bat_box1_right_y
            x2, y2 = conf._bat_box2_right_x, conf._bat_box2_right_y
        
            x3, y3 = conf._bat_name_box1_right_x, conf._bat_name_box1_right_y
            x4, y4 = conf._bat_name_box2_right_x, conf._bat_name_box2_right_y

            #data box
            draw_info_img = blend_overlay(draw_info_img, conf._bat_box1_img, x1, y1, alpha)
            draw_info_img = blend_overlay(draw_info_img, conf._bat_box2_img, x2, y2, alpha)
            #name box
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, x3, y3, alpha)
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha)
            #kbo logo
            draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_left_x, conf._kbo_logo_left_y, alpha, 0.1)

            draw_info_img = draw_fade_in_text_bat(draw_info_img,
                                                  conf._pitch_type,
                                                  conf._release_spinrate,
                                                  conf._release_speed,
                                                  conf._launch_speed,
                                                  conf._launch_v_angle,
                                                  conf._landingflat_distance,
                                                  conf._font_path_main,
                                                  conf._font_path_sub,
                                                  x1, y1,
                                                  alpha)

        case conf._type_baseball_hit | conf._type_baseball_hit_manual:
            if conf._landingflat_bearing > 0:
                x1, y1 = conf._hit_box1_left_x, conf._hit_box1_left_y
                x2, y2 = conf._hit_box2_left_x, conf._hit_box2_left_y
                x3, y3 = conf._hit_name_box1_left_x, conf._hit_name_box1_left_y
                x4, y4 = conf._hit_name_box2_left_x, conf._hit_name_box2_left_y
                
                #kbo logo 
                if conf._extra_homerun_derby:
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha, 0.28)
                    draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo2_img, conf._kbo_logo2_right_x, conf._kbo_logo2_right_y, alpha, 0.25)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box2_img, x2, y2, alpha)
                else:
                    #name box
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, x3, y3, alpha, 0.28)
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha, 0.28)
                    draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_right_x, conf._kbo_logo_right_y, alpha, 0.1)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box1_img, x1, y1, alpha)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box2_img, x2, y2, alpha)
            else:
                x1, y1 = conf._hit_box1_right_x, conf._hit_box1_right_y
                x2, y2 = conf._hit_box2_right_x, conf._hit_box2_right_y
                x3, y3 = conf._hit_name_box1_right_x, conf._hit_name_box1_right_y
                x4, y4 = conf._hit_name_box2_right_x, conf._hit_name_box2_right_y
                
                #kbo logo
                if conf._extra_homerun_derby:
                    #name box                    
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha, 0.28) 
                    draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo2_img, conf._kbo_logo2_left_x, conf._kbo_logo2_left_y, alpha, 0.25)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box2_img, x2, y2, alpha)
                else:
                    #name box
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, x3, y3, alpha, 0.28)
                    draw_info_img = blend_overlay(draw_info_img, conf._team_box_sub_img, x4, y4, alpha, 0.28)        
                    draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_left_x, conf._kbo_logo_left_y, alpha, 0.1)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box1_img, x1, y1, alpha)
                    draw_info_img = blend_overlay(draw_info_img, conf._hit_box2_img, x2, y2, alpha)

            
            draw_info_img = draw_fade_in_text_hit(draw_info_img,
                                                  conf._pitch_type,
                                                  conf._release_spinrate,
                                                  conf._release_speed,
                                                  conf._launch_speed,
                                                  conf._launch_v_angle,
                                                  conf._landingflat_distance,
                                                  conf._font_path_main,
                                                  conf._font_path_sub,
                                                  x1, y1,
                                                  alpha)
        # HIT Multi
        case conf._type_baseball_hit_multi:
             #data box
            draw_info_img = blend_overlay(draw_info_img, conf._hit_box3_img, conf._hit_box3_right_x, conf._hit_box3_right_y, alpha)
            #name box
            draw_info_img = blend_overlay(draw_info_img, conf._team_box_main_img, conf._hit_name_box3_right_x, conf._hit_name_box3_right_y, alpha, 0.28)
            #kbo logo
            if conf._extra_homerun_derby:
                draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo2_img, conf._kbo_logo2_left_x, conf._kbo_logo2_left_y, alpha, 0.25)
            elif conf._extra_all_star:
                draw_info_img = blend_overlay(draw_info_img, conf._kbo_logo_img, conf._kbo_logo_left_x, conf._kbo_logo_left_y, alpha, 0.1)

            draw_info_img = draw_fade_in_text_hit_multi(draw_info_img,
                                                    conf._font_path_main,
                                                    conf._font_path_sub,
                                                    conf._hit_box3_right_x,
                                                    conf._hit_box3_right_y,
                                                    alpha)
            pass


        case _:
            fd_log.error(f"❌ not in the type_target {type_target} at set_baseball_info_layer")
            return None

    return draw_info_img

def draw_fade_in_box(frame, text_x, text_y, box_w, box_h, alpha):
    """ 서서히 나타나는 박스를 그리기 """
    overlay = frame.copy()
    box_color = (255, 255, 255)
    cv2.rectangle(overlay, (text_x, text_y), (text_x + box_w, text_y + box_h), box_color, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, 0.0)
    return frame

    # 크기 조정이 필요하면 여기서 처리
    # overlay_img = cv2.resize(overlay_img, (width, height))

    h, w = overlay_img.shape[:2]
    overlay_bgr = overlay_img[:, :, :3]
    overlay_alpha = overlay_img[:, :, 3] / 255.0 * alpha  # 알파채널 × 전체 알파값

    # ROI 설정 (겹치는 영역)
    roi = frame[y:y+h, x:x+w]

    # BGR 채널에 대해 blending
    for c in range(3):
        roi[:, :, c] = (overlay_alpha * overlay_bgr[:, :, c] + (1 - overlay_alpha) * roi[:, :, c])

    # 결과를 원래 프레임에 적용
    frame[y:y+h, x:x+w] = roi
    return frame

def convert_pitch_type_to_korean(pitch_type):
    """ 영어 구종명을 한글로 변환하는 함수 """
    return conf.PITCH_TYPE_MAP.get(pitch_type, pitch_type)  # 딕셔너리에 없으면 원래 값 반환


def confirm_player_name(pre_name):
    if pre_name == conf._player_name_old:
        return conf._player_name_new
    return pre_name    

def draw_fade_in_text_pitch(frame, pitch_type, spin_rate, ball_speed, font_path1, font_path2, box_x, box_y, alpha):
    # 구종명을 한글로 변환
    pitch_type_korean = convert_pitch_type_to_korean(pitch_type)
    if conf._pitcher_player is None:
        pitcher_name = conf._pitcher_player_unknown
    else:
        if conf._extra_all_star:
            pitcher_name = conf._pitcher_player["NAME"]
        else:
            pitcher_name = conf._pitcher_player[0]["NAME"]

    # 2025-07-30
    # if need, change the player name
    pitcher_name    = confirm_player_name(pitcher_name)

 
    # 숫자를 3자리마다 쉼표 추가
    spin_rate_str = f"{int(spin_rate):,}"
    ball_speed_str = f"{round(ball_speed, 1):,.1f}"
 
    # 텍스트 스타일 설정
    font_size1 = 46  # 폰트 크기
    font_size2 = 44  # 폰트 크기
    font_size3 = 36  # 폰트 크기
    text_color = (255, 255, 255)  # 흰색 (RGB, alpha는 mask로 적용)
 
    # 한글 폰트 로드
    font1 = ImageFont.truetype(font_path1, font_size1)
    font2 = ImageFont.truetype(font_path1, font_size2)
    font3 = ImageFont.truetype(font_path2, font_size3)
 
    # PIL을 사용하여 이미지에 텍스트 그리기 (RGBA 모드)
    frame_pil = Image.fromarray(frame).convert("RGBA")
    overlay = Image.new("RGBA", frame_pil.size, (0, 0, 0, 0))  # 투명한 레이어 생성
    draw = ImageDraw.Draw(overlay)
 
    # **각 텍스트 크기 계산**
    pitcher_width = font3.getbbox(pitcher_name)[2] - font3.getbbox(pitcher_name)[0]
    pitch_width = font1.getbbox(pitch_type_korean)[2] - font1.getbbox(pitch_type_korean)[0]
    speed_width = font2.getbbox(ball_speed_str)[2] - font2.getbbox(ball_speed_str)[0]
    spin_width = font2.getbbox(spin_rate_str)[2] - font2.getbbox(spin_rate_str)[0]
 
    # **텍스트 위치 설정**
    text_pitcher_name_x_offset = 230  # 구종을 기준으로 중앙 정렬할 기준점
    text_pitch_x_offset = 251  # 구종을 기준으로 중앙 정렬할 기준점
    text_x_offset = 295  # 구속, 회전수를 기준으로 오른쪽 정렬할 기준점
 
    # **이름 중앙 정렬**
    text_x_pitcher_name = box_x + text_pitcher_name_x_offset - (pitcher_width // 2)
    text_y_pitcher_name = box_y - 45   # 구종 Y 위치

    # **구종 중앙 정렬**
    text_x_pitch = box_x + text_pitch_x_offset - (pitch_width // 2)
    text_y_pitch = box_y + 36   # 구종 Y 위치
 
    # **구속 & 회전수 오른쪽 정렬**
    text_x_speed = box_x + text_x_offset - speed_width
    text_x_spin = box_x + text_x_offset - spin_width
    text_y_speed = box_y + 161   # 구속 Y 위치
    text_y_spin = box_y + 286   # 회전수 Y 위치
 
    # **마스크 생성하여 투명도 적용**
    mask = Image.new("L", frame_pil.size, 0)  # 투명도 마스크
    mask_draw = ImageDraw.Draw(mask)
 
    # 텍스트 추가 (투명도 적용)
    mask_draw.text((text_x_pitcher_name, text_y_pitcher_name), pitcher_name, font=font3, fill=int(alpha * 255))  # 중앙 정렬
    mask_draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=int(alpha * 255))  # 중앙 정렬
    mask_draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font2, fill=int(alpha * 255))  # 오른쪽 정렬
    mask_draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font2, fill=int(alpha * 255))  # 오른쪽 정렬
 
    draw.text((text_x_pitcher_name, text_y_pitcher_name), pitcher_name, font=font3, fill=text_color)  # 중앙 정렬
    draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=text_color)  # 중앙 정렬
    draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font2, fill=text_color)  # 오른쪽 정렬
    draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font2, fill=text_color)  # 오른쪽 정렬
 
    # 투명 텍스트를 원본 프레임에 합성
    frame_pil = Image.composite(overlay, frame_pil, mask)
 
    # frame을 numpy 배열로 변환하여 반환
    #frame = np.array(frame_pil.convert("RGB"))
    frame = np.array(frame_pil)  # convert("RGB") 제거!

    return frame
 
def draw_fade_in_text_bat(frame, pitch_type, spin_rate, ball_speed, launch_speed , launch_angle, landing_distance, font_path1, font_path2, box_x, box_y, alpha):
    # 구종명을 한글로 변환
    pitch_type_korean = convert_pitch_type_to_korean(pitch_type)
    if conf._pitcher_player is None:
        pitcher_name = conf._pitcher_player_unknown
    else:
        if conf._extra_all_star:
            pitcher_name = conf._pitcher_player["NAME"]
        else:
            pitcher_name = conf._pitcher_player[0]["NAME"]

    if conf._batter_player is None:    
        batter_name = conf._batter_player_unknown
    else:
        if conf._extra_all_star:
            batter_name = conf._batter_player["NAME"]
        else:
            batter_name = conf._batter_player[0]["NAME"]

    # 2025-07-30
    # if need, change the player name
    pitcher_name    = confirm_player_name(pitcher_name)
    batter_name     = confirm_player_name(batter_name)


    # 숫자를 3자리마다 쉼표 추가
    spin_rate_str = f"{int(spin_rate):,}"
    ball_speed_str = f"{round(ball_speed, 1):,.1f}"
    launch_speed_str = f"{round(launch_speed, 1):,.1f}"
    launch_angle_str = f"{round(launch_angle, 1):,.1f}"
    landing_distance_str = f"{round(landing_distance, 1):,.1f}"

    # 텍스트 스타일 설정
    font_size1 = 34  # 폰트 크기
    font_size2 = 32  # 폰트 크기
    text_color = (255, 255, 255)  # 흰색 (RGB, alpha는 mask로 적용)

    # 한글 폰트 로드
    font1 = ImageFont.truetype(font_path1, font_size1)
    font2 = ImageFont.truetype(font_path2, font_size2)

    # PIL을 사용하여 이미지에 텍스트 그리기 (RGBA 모드)
    frame_pil = Image.fromarray(frame).convert("RGBA")
    overlay = Image.new("RGBA", frame_pil.size, (0, 0, 0, 0))  # 투명한 레이어 생성
    draw = ImageDraw.Draw(overlay)

    # **각 텍스트 크기 계산**
    pitcher_name_width = font2.getbbox(pitcher_name)[2] - font2.getbbox(pitcher_name)[0]
    batter_name_width = font2.getbbox(batter_name)[2] - font2.getbbox(batter_name)[0]
    pitch_width = font1.getbbox(pitch_type_korean)[2] - font1.getbbox(pitch_type_korean)[0]
    speed_width = font1.getbbox(ball_speed_str)[2] - font1.getbbox(ball_speed_str)[0]
    spin_width = font1.getbbox(spin_rate_str)[2] - font1.getbbox(spin_rate_str)[0]
    launch_speed_width = font1.getbbox(launch_speed_str)[2] - font1.getbbox(launch_speed_str)[0]
    launch_angle_width = font1.getbbox(launch_angle_str)[2] - font1.getbbox(launch_angle_str)[0]
    landing_distance_width = font1.getbbox(landing_distance_str)[2] - font1.getbbox(landing_distance_str)[0]


    # **텍스트 위치 설정**
    text_name_x_offset = 200  # 구종을 기준으로 중앙 정렬할 기준점
    text_pitch_x_offset = 227  # 구종을 기준으로 중앙 정렬할 기준점
    
    # **이름 중앙 정렬**
    text_x_pitcher_name = box_x + text_name_x_offset - (pitcher_name_width // 2)
    text_y_pitcher_name = box_y - 40   # 투수 Y 위치

    text_x_batter_name = box_x + text_name_x_offset - (batter_name_width // 2)
    text_y_batter_name = box_y + 90   # 투수 Y 위치

    # **구종 중앙 정렬**
    text_x_pitch = box_x + text_pitch_x_offset - (pitch_width // 2)
    text_y_pitch = box_y + 13   # 구종 Y 위치

    # **구속 & 회전수 오른쪽 정렬**
    text_x_speed = box_x + 586 - speed_width
    text_y_speed = box_y + 13   # 구속 Y 위치

    text_x_spin = box_x + 915 - spin_width    
    text_y_spin = box_y + 13   # 회전수 Y 위치

    text_x_launch_speed = box_x + 260 - launch_speed_width
    text_y_launch_speed = box_y + 145

    text_x_launch_angle = box_x + 586 - launch_angle_width
    text_y_launch_angle = box_y + 145

    text_x_landing_distance = box_x + 915 - landing_distance_width
    text_y_landing_distance = box_y + 145

    # **마스크 생성하여 투명도 적용**
    mask = Image.new("L", frame_pil.size, 0)  # 투명도 마스크
    mask_draw = ImageDraw.Draw(mask)

    # 텍스트 추가 (투명도 적용)
    mask_draw.text((text_x_pitcher_name, text_y_pitcher_name), pitcher_name, font=font2, fill=int(alpha * 255))               # 중앙 정렬
    mask_draw.text((text_x_batter_name, text_y_batter_name), batter_name, font=font2, fill=int(alpha * 255))               # 중앙 정렬
    mask_draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=int(alpha * 255))               # 중앙 정렬
    mask_draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font1, fill=int(alpha * 255))                  # 오른쪽 정렬
    mask_draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font1, fill=int(alpha * 255))                     # 오른쪽 정렬
    mask_draw.text((text_x_launch_speed, text_y_launch_speed), launch_speed_str, font=font1, fill=int(alpha * 255))  # 오른쪽 정렬
    mask_draw.text((text_x_launch_angle, text_y_launch_angle), launch_angle_str, font=font1, fill=int(alpha * 255))  # 오른쪽 정렬
    mask_draw.text((text_x_landing_distance, text_y_landing_distance), landing_distance_str, font=font1, fill=int(alpha * 255))  # 오른쪽 정렬

    draw.text((text_x_pitcher_name, text_y_pitcher_name), pitcher_name, font=font2, fill=text_color)               # 중앙 정렬
    draw.text((text_x_batter_name, text_y_batter_name), batter_name, font=font2, fill=text_color)               # 중앙 정렬    
    draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=text_color)               # 중앙 정렬
    draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font1, fill=text_color)                  # 오른쪽 정렬
    draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font1, fill=text_color)                     # 오른쪽 정렬    
    draw.text((text_x_launch_speed, text_y_launch_speed), launch_speed_str, font=font1, fill=text_color)  # 오른쪽 정렬
    draw.text((text_x_launch_angle, text_y_launch_angle), launch_angle_str, font=font1, fill=text_color)  # 오른쪽 정렬
    draw.text((text_x_landing_distance, text_y_landing_distance), landing_distance_str, font=font1, fill=text_color)  # 오른쪽 정렬

    # 투명 텍스트를 원본 프레임에 합성
    frame_pil = Image.composite(overlay, frame_pil, mask)

    # frame을 numpy 배열로 변환하여 반환
    #frame = np.array(frame_pil.convert("RGB"))
    frame = np.array(frame_pil)  # convert("RGB") 제거!
    return frame

def draw_fade_in_text_hit(frame, pitch_type, spin_rate, ball_speed, launch_speed , launch_angle , ball_distance , font_path1, font_path2, box_x, box_y, alpha):
    # 구종명을 한글로 변환
    pitch_type_korean = convert_pitch_type_to_korean(pitch_type)
    if conf._pitcher_player is None:
        pitcher_name = conf._pitcher_player_unknown
    else:
        if conf._extra_all_star:
            pitcher_name = conf._pitcher_player["NAME"]
        else:
            pitcher_name = conf._pitcher_player[0]["NAME"]

    if conf._batter_player is None:    
        batter_name = conf._batter_player_unknown
    else:
        if conf._extra_all_star:
            batter_name = conf._batter_player["NAME"]
        else:
            batter_name = conf._batter_player[0]["NAME"]
    
    # 2025-07-30
    # if need, change the player name
    pitcher_name    = confirm_player_name(pitcher_name)
    batter_name     = confirm_player_name(batter_name)

    # 숫자를 3자리마다 쉼표 추가
    spin_rate_str = f"{int(spin_rate):,}"
    ball_speed_str = f"{round(ball_speed, 1):,.1f}"
    launch_speed_str = f"{round(launch_speed, 1):,.1f}"
    launch_angle_str = f"{round(launch_angle, 1):,.1f}"
    ball_distance_str = f"{round(ball_distance, 1):,.1f}"

    # 텍스트 스타일 설정
    font_size1 = 42  # 폰트 크기
    font_size2 = 36  # 폰트 크기
    text_color = (255, 255, 255)  # 흰색 (RGB, alpha는 mask로 적용)

    # 한글 폰트 로드
    font1 = ImageFont.truetype(font_path1, font_size1)
    font2 = ImageFont.truetype(font_path2, font_size2)

    # PIL을 사용하여 이미지에 텍스트 그리기 (RGBA 모드)
    frame_pil = Image.fromarray(frame).convert("RGBA")
    overlay = Image.new("RGBA", frame_pil.size, (0, 0, 0, 0))  # 투명한 레이어 생성
    draw = ImageDraw.Draw(overlay)

    # **각 텍스트 크기 계산**
    pitcher_name_width = font2.getbbox(pitcher_name)[2] - font2.getbbox(pitcher_name)[0]
    batter_name_width = font2.getbbox(batter_name)[2] - font2.getbbox(batter_name)[0]
    pitch_width = font1.getbbox(pitch_type_korean)[2] - font1.getbbox(pitch_type_korean)[0]
    speed_width = font1.getbbox(ball_speed_str)[2] - font1.getbbox(ball_speed_str)[0]
    spin_width = font1.getbbox(spin_rate_str)[2] - font1.getbbox(spin_rate_str)[0]
    launch_speed_width = font1.getbbox(launch_speed_str)[2] - font1.getbbox(launch_speed_str)[0]
    launch_angle_width = font1.getbbox(launch_angle_str)[2] - font1.getbbox(launch_angle_str)[0]
    ball_distance_width = font1.getbbox(ball_distance_str)[2] - font1.getbbox(ball_distance_str)[0]


    # **텍스트 위치 설정**
    text_name_x_offset = 215
    text_pitch_x_offset = 260  # 구종을 기준으로 중앙 정렬할 기준점
    text_x_offset = 300  # 구속, 회전수를 기준으로 오른쪽 정렬할 기준점

    # **name 중앙 정렬**
    text_x_pitcher = box_x + text_name_x_offset - (pitcher_name_width // 2)
    text_y_pitcher = box_y - 45   

    text_x_batter = box_x + text_name_x_offset - (batter_name_width // 2)
    text_y_batter = box_y + 318  

    # **구종 중앙 정렬**
    text_x_pitch = box_x + text_pitch_x_offset - (pitch_width // 2)
    text_y_pitch = box_y + 23   # 구종 Y 위치

    # **구속 & 회전수 오른쪽 정렬**
    text_x_speed = box_x + text_x_offset - speed_width
    text_y_speed = box_y + 115   # 구속 Y 위치

    text_x_spin = box_x + text_x_offset - spin_width    
    text_y_spin = box_y + 215   # 회전수 Y 위치

    text_x_launch_speed = box_x + text_x_offset - launch_speed_width
    text_y_launch_speed = box_y + 385

    text_x_launch_angle = box_x + text_x_offset - launch_angle_width
    text_y_launch_angle = box_y + 482

    text_x_ball_distance = box_x + text_x_offset - ball_distance_width
    text_y_ball_distance = box_y + 578  # 마지막 줄

    # **마스크 생성하여 투명도 적용**
    mask = Image.new("L", frame_pil.size, 0)  # 투명도 마스크
    mask_draw = ImageDraw.Draw(mask)

    # 텍스트 추가 (투명도 적용)
    mask_draw.text((text_x_pitcher, text_y_pitcher), pitcher_name, font=font2, fill=int(alpha * 255))    
    mask_draw.text((text_x_batter, text_y_batter), batter_name, font=font2, fill=int(alpha * 255))      

    mask_draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=int(alpha * 255))                       # 중앙 정렬
    mask_draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font1, fill=int(alpha * 255))                          # 오른쪽 정렬
    mask_draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font1, fill=int(alpha * 255))                             # 오른쪽 정렬
    mask_draw.text((text_x_launch_speed, text_y_launch_speed), launch_speed_str, font=font1, fill=int(alpha * 255))          # 오른쪽 정렬
    mask_draw.text((text_x_launch_angle, text_y_launch_angle), launch_angle_str, font=font1, fill=int(alpha * 255))          # 오른쪽 정렬
    mask_draw.text((text_x_ball_distance, text_y_ball_distance), ball_distance_str, font=font1, fill=int(alpha * 255))  # 오른쪽 정렬

    if conf._extra_homerun_derby:          
        draw.text((text_x_batter, text_y_batter), batter_name, font=font2, fill=text_color)
        draw.text((text_x_launch_speed, text_y_launch_speed), launch_speed_str, font=font1, fill=text_color)            # 오른쪽 정렬
        draw.text((text_x_launch_angle, text_y_launch_angle), launch_angle_str, font=font1, fill=text_color)            # 오른쪽 정렬
        draw.text((text_x_ball_distance, text_y_ball_distance), ball_distance_str, font=font1, fill=text_color)         # 오른쪽 정렬
    else:          
        draw.text((text_x_pitcher, text_y_pitcher), pitcher_name, font=font2, fill=text_color)  
        draw.text((text_x_batter, text_y_batter), batter_name, font=font2, fill=text_color)  

        draw.text((text_x_pitch, text_y_pitch), pitch_type_korean, font=font1, fill=text_color)                         # 중앙 정렬
        draw.text((text_x_speed, text_y_speed), ball_speed_str, font=font1, fill=text_color)                            # 오른쪽 정렬
        draw.text((text_x_spin, text_y_spin), spin_rate_str, font=font1, fill=text_color)                               # 오른쪽 정렬    
        draw.text((text_x_launch_speed, text_y_launch_speed), launch_speed_str, font=font1, fill=text_color)            # 오른쪽 정렬
        draw.text((text_x_launch_angle, text_y_launch_angle), launch_angle_str, font=font1, fill=text_color)            # 오른쪽 정렬
        draw.text((text_x_ball_distance, text_y_ball_distance), ball_distance_str, font=font1, fill=text_color)         # 오른쪽 정렬

    # 투명 텍스트를 원본 프레임에 합성
    frame_pil = Image.composite(overlay, frame_pil, mask)

    # frame을 numpy 배열로 변환하여 반환
    #frame = np.array(frame_pil.convert("RGB"))
    frame = np.array(frame_pil)  # convert("RGB") 제거!
    return frame

def draw_fade_in_text_hit_multi(frame, font_path1, font_path2, box_x, box_y, alpha):
    multi_line_cnt_str = f"{conf._multi_line_cnt}"
    if conf._batter_player is None:    
        batter_name = conf._batter_player_unknown
    else:
        if conf._extra_all_star:
            batter_name = conf._batter_player["NAME"]
        else:
            batter_name = conf._batter_player[0]["NAME"]

    batter_name = confirm_player_name(batter_name)

    # 숫자를 3자리마다 쉼표 추가    
    ball_distance_str = f"{round(conf._landingflat_distance, 1):,.1f}"

    # 텍스트 스타일 설정
    font_size1 = 42  # 폰트 크기
    font_size2 = 36  # 폰트 크기
    text_color = (255, 255, 255)  # 흰색 (RGB, alpha는 mask로 적용)

    # 한글 폰트 로드
    font1 = ImageFont.truetype(font_path1, font_size1)
    font2 = ImageFont.truetype(font_path2, font_size2)

    # PIL을 사용하여 이미지에 텍스트 그리기 (RGBA 모드)
    frame_pil = Image.fromarray(frame).convert("RGBA")
    overlay = Image.new("RGBA", frame_pil.size, (0, 0, 0, 0))  # 투명한 레이어 생성
    draw = ImageDraw.Draw(overlay)

    # **각 텍스트 크기 계산**    
    batter_name_width = font2.getbbox(batter_name)[2] - font2.getbbox(batter_name)[0]    
    multi_line_cnt_width = font1.getbbox(multi_line_cnt_str)[2] - font1.getbbox(multi_line_cnt_str)[0]
    ball_distance_width = font1.getbbox(ball_distance_str)[2] - font1.getbbox(ball_distance_str)[0]


    # **텍스트 위치 설정**
    text_name_x_offset = 215
    text_x_offset1 = 316  
    text_x_offset2 = 320  

    # **name 중앙 정렬**
    text_x_batter = box_x + text_name_x_offset - (batter_name_width // 2)
    text_y_batter = box_y - 48  

    text_x_multi_line_cnt = box_x + text_x_offset1 - multi_line_cnt_width
    text_y_multi_line_cnt = box_y + 25  # 마지막 줄

    text_x_ball_distance = box_x + text_x_offset2 - ball_distance_width
    text_y_ball_distance = box_y + 120  # 마지막 줄

    # **마스크 생성하여 투명도 적용**
    mask = Image.new("L", frame_pil.size, 0)  # 투명도 마스크
    mask_draw = ImageDraw.Draw(mask)

    # 텍스트 추가 (투명도 적용)
    mask_draw.text((text_x_batter, text_y_batter), batter_name, font=font2, fill=int(alpha * 255))      
    mask_draw.text((text_x_multi_line_cnt, text_y_multi_line_cnt), multi_line_cnt_str, font=font1, fill=int(alpha * 255)) # 오른쪽 정렬
    mask_draw.text((text_x_ball_distance, text_y_ball_distance), ball_distance_str, font=font1, fill=int(alpha * 255)) # 오른쪽 정렬
    draw.text((text_x_batter, text_y_batter), batter_name, font=font2, fill=text_color)  
    draw.text((text_x_multi_line_cnt, text_y_multi_line_cnt), multi_line_cnt_str, font=font1, fill=text_color) # 오른쪽 정렬
    draw.text((text_x_ball_distance, text_y_ball_distance), ball_distance_str, font=font1, fill=text_color) # 오른쪽 정렬

    # 투명 텍스트를 원본 프레임에 합성
    frame_pil = Image.composite(overlay, frame_pil, mask)

    # frame을 numpy 배열로 변환하여 반환
    #frame = np.array(frame_pil.convert("RGB"))
    frame = np.array(frame_pil)  # convert("RGB") 제거!
    return frame

def draw_rotating_ellipse(frame, center, axes, angle, thickness, color=(0, 0, 255)):
    """
    회전하는 타원을 일정한 두께로 그리는 함수
    - center: 타원의 중심 좌표 (x, y)
    - axes: 타원의 가로, 세로 크기 (width, height)
    - angle: 현재 회전 각도
    - thickness: 타원의 선 두께
    - color: 타원의 색상 (기본값: 파란색)
    """
    num_segments = 12  # 타원을 나누는 개수 (각도 간격)
    angle_step = 360 // num_segments  # 각도 간격
    next_angle = (angle + 20) % 360  # 회전 속도 증가

    for i in range(num_segments):
        start_angle = (angle + i * angle_step) % 360
        end_angle = (start_angle + angle_step) % 360

        cv2.ellipse(frame, center, axes, 0, start_angle, end_angle, color, thickness)

    return frame, next_angle  # 다음 회전 각도를 반환

# ─────────────────────────────────────────────────────────────────────────────##
# def draw_indicator_line
# ─────────────────────────────────────────────────────────────────────────────#
def draw_indicator_line(line_type, frame):
    match line_type:
        
        case conf._line_shl_hdl:
            # [2D] draw line of angle waist    
            shoulder_left  = np.array([conf._pt_sh_2d_l[0] , conf._pt_sh_2d_l[1]])
            hand_left  = np.array([conf._pt_th_2d_l[0] , conf._pt_th_2d_l[1]])            
            # draw line
            draw_dotted_line(frame, tuple(shoulder_left.astype(int)), tuple(hand_left.astype(int)), conf._color_swing_angle, 2)            
        
        case conf._line_shc_hpc:
            # [2D] draw line of angle waist    
            shoulder_midpoint = np.array([conf._pt_sh_2d_c[0] , conf._pt_sh_2d_c[1]])
            hip_midpoint  = np.array([conf._pt_hp_2d_c[0], conf._pt_hp_2d_c[1]])
            # draw shoulder to hip
            cv2.line(frame, tuple(hip_midpoint.astype(int)), tuple(shoulder_midpoint.astype(int)), conf._color_waist_angle, conf._line_width)
            # draw shoulder to hip (vertical)
            cv2.line(frame, tuple(hip_midpoint.astype(int)), (int(hip_midpoint[0]), int(shoulder_midpoint[1])), conf._color_waist_vertical, conf._line_width) 
        
        case conf._line_foot:
            # [2D] draw line of angle waist    
            foot_1  = np.array([conf._pt_hp_2d_r[0] , conf._pt_hp_2d_r[1]])
            foot_2 = np.array([conf._pt_kn_2d_r[0] , conf._pt_kn_2d_r[1]])   
            cv2.line(frame, foot_1, foot_2, conf._color_joint, 2)  
            
            foot_1  = np.array([conf._pt_hp_2d_l[0] , conf._pt_hp_2d_l[1]])
            foot_2 = np.array([conf._pt_kn_2d_l[0] , conf._pt_kn_2d_l[1]])            
            cv2.line(frame, foot_1, foot_2, conf._color_joint, 2)  
            
            foot_1  = np.array([conf._pt_kn_2d_r[0] , conf._pt_kn_2d_r[1]])
            foot_2 = np.array([conf._pt_an_2d_r[0] , conf._pt_an_2d_r[1]])            
            cv2.line(frame, foot_1, foot_2, conf._color_joint, 2)  
            
            foot_1  = np.array([conf._pt_kn_2d_l[0] , conf._pt_kn_2d_l[1]])
            foot_2 = np.array([conf._pt_an_2d_l[0] , conf._pt_an_2d_l[1]])            
            cv2.line(frame, foot_1, foot_2, conf._color_joint, 2)  
            
        case conf._line_shl_shr:
            # [2D] draw line of angle waist    
            shoulder_left  = np.array([conf._pt_sh_2d_l[0] , conf._pt_sh_2d_l[1]])
            shoulder_right = np.array([conf._pt_sh_2d_r[0] , conf._pt_sh_2d_r[1]])            
            # draw line
            draw_extention_line(frame, tuple(shoulder_left.astype(int)), tuple(shoulder_right.astype(int)), conf._color_line_shoulder, 2, 1.2, True) 

        case conf._line_hpl_hpr:
            # [2D] draw line of angle waist
            hip_left  = np.array([conf._pt_hp_2d_l[0] , conf._pt_hp_2d_l[1]])
            hip_right = np.array([conf._pt_hp_2d_r[0] , conf._pt_hp_2d_r[1]])            
            # draw line
            draw_extention_line(frame, tuple(hip_left.astype(int)), tuple(hip_right.astype(int)), conf._color_line_hip, 2, 1.2, True)   

        case conf._line_indicator_foot:
            # [2D] draw line of angle waist                
            # draw shoulder to hip
            cv2.line(frame, (int(conf._avr_foot_left_x+20), int(conf._avr_foot_y+20)), (int(conf._avr_foot_left_x+20), int(conf._avr_head_y-80)), conf._color_base_indicator, conf._line_width)
            # draw shoulder to hip (vertical)
            cv2.line(frame, (int(conf._avr_foot_right_x-20), int(conf._avr_foot_y+20)), (int(conf._avr_foot_right_x-20), int(conf._avr_head_y-80)), conf._color_base_indicator, conf._line_width) 
        
        case conf._line_indicator_chest:
            chest_height = conf._pt_sh_2d_c[1] + int(conf._pt_hp_2d_c[1] - conf._pt_sh_2d_c[1])/2

            if(conf._length_chest_line == -1):                   
                conf._length_chest_line = int(abs(conf._pt_sh_2d_l[0] - conf._pt_sh_2d_r[0])/4*3)
                
            center_position = conf._pt_hp_2d_c[0]
            chest_left  = np.array([center_position - conf._length_chest_line,chest_height])
            chest_right = np.array([center_position + conf._length_chest_line,chest_height])            
            # draw line
            draw_extention_line(frame, tuple(chest_left.astype(int)), tuple(chest_right.astype(int)), conf._color_ext_line, 2, 1)   

        case conf._line_indicator_knee: 
            knee_height = conf._pt_kn_2d_c[1]            

            if(conf._length_chest_line == -1):                   
                conf._length_chest_line = int(abs(conf._pt_sh_2d_l[0] - conf._pt_sh_2d_r[0])/4*3)
            
            center_position = conf._pt_hp_2d_c[0]
            knee_left  = np.array([center_position - conf._length_chest_line, knee_height])
            knee_right = np.array([center_position + conf._length_chest_line,knee_height])           
            # draw line
            draw_extention_line(frame, tuple(knee_left.astype(int)), tuple(knee_right.astype(int)), conf._color_ext_line, 2, 1)   

        case conf._box_body:
            # [2D] draw line of angle waist
            hip_left  = np.array([conf._pt_hp_2d_l[0] , conf._pt_hp_2d_l[1]])
            hip_right = np.array([conf._pt_hp_2d_r[0] , conf._pt_hp_2d_r[1]])            
            # draw line
            draw_extention_line(frame, tuple(hip_left.astype(int)), tuple(hip_right.astype(int)), conf._color_line_hip, 2, 1.2, True)    

# ─────────────────────────────────────────────────────────────────────────────##
# def calculate_distance
# 점 간의 거리 계산
# ─────────────────────────────────────────────────────────────────────────────#
def calculate_distance(pt1, pt2):
    return np.sqrt((pt1[0] - pt2[0]) ** 2 + (pt1[1] - pt2[1]) ** 2)

# ─────────────────────────────────────────────────────────────────────────────#
# def get_start_end_time(arr):
# 그라데이션으로 색상을 채우는 함수 (속도 개선)
# [owner] joonho kim
# [date] 2025-03-03
# ─────────────────────────────────────────────────────────────────────────────#
def gradient_fill(frame, points, center):
    # 다각형 내부 마스크 생성
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    polygon_points = np.array([points], dtype=np.int32)
    cv2.fillPoly(mask, polygon_points, 255)

    # 최소/최대 좌표 계산
    min_x, min_y = np.min(polygon_points[:, :, 0]), np.min(polygon_points[:, :, 1])
    max_x, max_y = np.max(polygon_points[:, :, 0]), np.max(polygon_points[:, :, 1])

    # 다각형 내부 픽셀 좌표 찾기
    inside_indices = np.where(mask[min_y:max_y, min_x:max_x] == 255)

    # Check if there are valid pixels
    if inside_indices[0].size == 0:
        fd_log.info("Warning: No valid pixels inside the polygon!")
    else:
        inside_pixels = np.column_stack((inside_indices[1] + min_x, inside_indices[0] + min_y))

        # 중심으로부터의 거리 계산
        distances = np.hypot(inside_pixels[:, 0] - center[0], inside_pixels[:, 1] - center[1])
        max_distance = distances.max() if distances.size > 0 else 1  # Prevent empty array error

        # 벡터 연산으로 색상 계산
        progress = distances / max_distance  # 0 ~ 1 사이 값
        hues = np.linspace(10, 120, 256)  # 색상 범위
        hue_values = np.interp(progress * 255, np.arange(256), hues).astype(np.uint8)

        # HSV에서 BGR로 변환
        if hue_values.size > 0:
            hsv_colors = np.zeros((len(hue_values), 1, 3), dtype=np.uint8)
            hsv_colors[:, 0, 0] = hue_values
            hsv_colors[:, 0, 1] = 255  # 채도
            hsv_colors[:, 0, 2] = 255  # 명도

            # Ensure correct shape for cvtColor()
            hsv_colors = hsv_colors.reshape(-1, 1, 3)
            bgr_colors = cv2.cvtColor(hsv_colors, cv2.COLOR_HSV2BGR)[:, 0, :]

            # 프레임에 한 번에 적용
            frame[inside_pixels[:, 1], inside_pixels[:, 0]] = bgr_colors

    return frame

# ─────────────────────────────────────────────────────────────────────────────#
# def get_center_of_movement:
# ─────────────────────────────────────────────────────────────────────────────#
def get_center_of_movement(arr):
    nStartCnt, nEndCnt = get_start_end_time(arr)
    # array slicing
    valid_arr = arr[nStartCnt:nEndCnt]
    valid_arr = np.array(valid_arr, dtype=float) 
    if(len(valid_arr) < 1):
        return
    
    x_arr_center = valid_arr[:, 0]  # x 좌표
    y_arr_center = valid_arr[:, 1]  # y 좌표

    mean_x = np.mean(x_arr_center)
    mean_y = np.mean(y_arr_center)
    return (mean_x,mean_y)  

# ─────────────────────────────────────────────────────────────────────────────#
# def validate_point:
# check the over the screen point to validation
# ─────────────────────────────────────────────────────────────────────────────#
def validate_point(height,width,x1,y1,x2,y2):
    if(x1 < 0):         x1 = 0
    elif(x1 > width):   x1 = width

    if(x2 < 0):         x2 = 0
    elif(x2 > width):   x2 = width
    
    if(y1 < 0):         y1 = 0
    elif(y1 > height):  y1 = height

    if(y2 < 0):         y2 = 0
    elif(y2 > height):  y2 = height   
    return x1,y1,x2,y2
    
# ─────────────────────────────────────────────────────────────────────────────c
# def find_last_frame(arr):
# [owner] hongsu jung
# [date] 2025-03-28
# ─────────────────────────────────────────────────────────────────────────────
def find_valid_arr_frame(arr):    

    # find last valid position
    tot_cnt = len(arr)
    idx = -1
    while(1):
        idx += 1
        if(idx >= tot_cnt): break
        pos = arr[idx]
        pos = np.array(pos, dtype=float)
        if (np.isnan(pos).any() == False):
            break
    n_start = idx

    # find last valid position
    idx = tot_cnt
    while(1):
        idx -= 1
        if(idx < 0): break
        pos = arr[idx]
        pos = np.array(pos, dtype=float)
        if (np.isnan(pos).any() == False):
            break
        if (pos[0] == -1 and pos[1] == -1):
            break
    n_end = idx - 1 
    return n_start, n_end

# ─────────────────────────────────────────────────────────────────────────────#
# def get_start_end_time(arr):
# [owner] hongsu jung
# [date] 2025-02-14
# ─────────────────────────────────────────────────────────────────────────────#
def get_start_end_time(arr):       
    valid_pos = None
    start_frame = 0
    end_frame = 0
    nTot = len(arr)

    i = -1
    while(1):
        i = i + 1
        if(i >= nTot):
            break
        valid_pos = arr[i]
        if valid_pos is not None and np.array(valid_pos).any():
            if((valid_pos[0] != 0) or (valid_pos[1] != 0)):
                break
    start_frame = i    
    # find last valid position
    i = len(arr)
    while(1):
        i = i - 1
        if(i <= 0):
            break
        valid_pos = arr[i]
        if valid_pos is not None and np.array(valid_pos).any():
            if(np.isnan(valid_pos).any() == True):
                continue
            if((valid_pos[0] == -1) and (valid_pos[1] == -1)):
                end_frame = i - 1
                break
            if((valid_pos[0] != 0) or (valid_pos[1] != 0)):
                end_frame = i
                break    
    return start_frame, end_frame

# ─────────────────────────────────────────────────────────────────────────────#
# def sort_rectangle(points):
# [owner] hongsu jung
# [date] 2025-02-16
# ─────────────────────────────────────────────────────────────────────────────#
def sort_rectangle(points):
    # 중앙점(center_x, center_y) 계산
    center_x = np.mean(points[:, 0])
    center_y = np.mean(points[:, 1])
    # 각도를 기준으로 정렬 (시계 방향)
    points = sorted(points, key=lambda p: np.arctan2(p[1] - center_y, p[0] - center_x))
    return np.array(points, dtype=np.int32)

# ─────────────────────────────────────────────────────────────────────────────#
# def get_arrow_pos(nframe, nAllcnt, pos_start, pos_end):
# [owner] hongsu jung
# [date] 2025-02-14
# ─────────────────────────────────────────────────────────────────────────────#
def get_arrow_pos(x, pos_start, pos_end, floor_down_margin):
    a = (pos_end[1] - pos_start[1]) / (pos_end[0] - pos_start[0] )
    y = a * (x-pos_start[0]) + pos_start[1]
    y += floor_down_margin
    return int(y)

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_gradient_line:
# ─────────────────────────────────────────────────────────────────────────────#
def draw_gradient_line(image, p1, p2, thinkness_out = 15, thinkness_in = 2):
    # Define line properties
    line_color = (255, 50, 170)  # Dark pink color (BGR)
    line_color_center = (255, 255, 255)  # Light pink color (BGR)

    # Draw the lines
    cv2.line(image, p1, p2, line_color, thinkness_out)
    cv2.line(image, p1, p2, line_color_center, thinkness_in)
    
    return image

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_analysis_line:
# ─────────────────────────────────────────────────────────────────────────────#
def draw_analysis_box(frame, frameindex, pos_data):
    selected_frame = frameindex - 2
    cnt_frame = len(pos_data)
    if(selected_frame < 0):
        return
    if(cnt_frame <= selected_frame):
        return
    if np.isnan(pos_data[selected_frame]).any():
        return
    x = int(pos_data[selected_frame][0])
    y = int(pos_data[selected_frame][1])
    w = int(pos_data[selected_frame][2])
    h = int(pos_data[selected_frame][3])
    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_dotte)d_line(img, start, end, color, thickness=1, gap=10):
# ─────────────────────────────────────────────────────────────────────────────#
def draw_base_line(graph_type, frame, width, height, start_x, start_y, draw_title = True):
    # draw vertical base line
    line_v_start_x = int(start_x + width / 12 * 2  )
    line_v_start_y = int(start_y + height / 6     )
    line_v_end_x   = int(start_x + width / 12 * 2  )
    line_v_end_y   = int(start_y + height / 6 * 5 )     
    # draw hotizontal base line
    line_h_start_x = int(start_x + width / 12 * 2  )
    line_h_start_y = int(start_y + height / 6 * 5 )
    line_h_end_x   = int(start_x + width / 12 * 11 )
    line_h_end_y   = int(start_y + height / 6 * 5 )      
    # horizontal 1/2/3          
    line_h_1st_y = int(start_y + height / 6 * 2 ) + 7
    line_h_2nd_y = int(start_y + height / 6 * 3 ) + 7
    line_h_3rd_y = int(start_y + height / 6 * 4 ) + 7
    # draw black base box
    title_box_start_x = int(start_x + width / 120 * 2  )
    title_box_start_y = int(start_y + height / 6 )
    title_box_end_x   = int(start_x + width / 120 * 20 )
    title_box_end_y   = int(start_y + height / 6 * 5 )                
    # draw title position
    title_pos_x = int(start_x + width / 120 * 17 ) 
    title_pos_y = int(start_y + height / 12 * 8 )    

    match graph_type:
        case conf._graph_front_up:            

            cv2.line(frame, (line_v_start_x, line_v_start_y), (line_v_end_x,line_v_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Vertical            
            cv2.line(frame, (line_h_start_x, line_h_start_y), (line_h_end_x,line_h_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_1st_y), (line_h_end_x,line_h_1st_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_2nd_y), (line_h_end_x,line_h_2nd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_3rd_y), (line_h_end_x,line_h_3rd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            
            if(draw_title == True):
                # draw title
                cv2.rectangle(frame, (title_box_start_x, title_box_start_y), (title_box_end_x, title_box_end_y), conf._color_title_box, -1)
                title_box_end_x = title_box_end_x + 10
                draw_graph_title(conf._graph_title_front_up, frame, title_pos_x, title_pos_y, conf._color_title, conf._text_size_title, 1)
                # draw index value
                draw_graph_title(f"180", frame, title_box_end_x, line_h_1st_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"120", frame, title_box_end_x, line_h_2nd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"60", frame, title_box_end_x, line_h_3rd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)


        case conf._graph_front_down:            
            # draw base line
            cv2.line(frame, (line_v_start_x, line_v_start_y), (line_v_end_x,line_v_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Vertical
            cv2.line(frame, (line_h_start_x, line_h_start_y), (line_h_end_x,line_h_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_1st_y), (line_h_end_x,line_h_1st_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_2nd_y), (line_h_end_x,line_h_2nd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_3rd_y), (line_h_end_x,line_h_3rd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            
            if(draw_title == True):
                # draw title
                cv2.rectangle(frame, (title_box_start_x, title_box_start_y), (title_box_end_x, title_box_end_y), conf._color_title_box, -1)
                title_box_end_x = title_box_end_x + 10
                draw_graph_title(conf._graph_title_front_down, frame, title_pos_x, title_pos_y, conf._color_title, conf._text_size_title, 1)
                draw_graph_title(f"90", frame, title_box_end_x, line_h_1st_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"60", frame, title_box_end_x, line_h_2nd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"30", frame, title_box_end_x, line_h_3rd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)             
        
        case conf._graph_side_up:            
            # draw base line
            cv2.line(frame, (line_v_start_x, line_v_start_y), (line_v_end_x,line_v_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Vertical
            cv2.line(frame, (line_h_start_x, line_h_start_y), (line_h_end_x,line_h_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_1st_y), (line_h_end_x,line_h_1st_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_2nd_y), (line_h_end_x,line_h_2nd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_3rd_y), (line_h_end_x,line_h_3rd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            
            if(draw_title == True):
                cv2.rectangle(frame, (title_box_start_x, title_box_start_y), (title_box_end_x, title_box_end_y), conf._color_title_box, -1)
                # draw title
                title_box_end_x = title_box_end_x + 10
                draw_graph_title(conf._graph_title_side_up, frame, title_pos_x, title_pos_y, conf._color_title, conf._text_size_title, 1)
                draw_graph_title(f"180", frame, title_box_end_x, line_h_1st_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"120", frame, title_box_end_x, line_h_2nd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"60", frame, title_box_end_x, line_h_3rd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
        
        case conf._graph_side_down:
            # draw base line
            cv2.line(frame, (line_v_start_x, line_v_start_y), (line_v_end_x,line_v_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Vertical            
            cv2.line(frame, (line_h_start_x, line_h_start_y), (line_h_end_x,line_h_end_y), conf._color_baseline, conf._draw_baseline_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_1st_y), (line_h_end_x,line_h_1st_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_2nd_y), (line_h_end_x,line_h_2nd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal
            cv2.line(frame, (line_h_start_x, line_h_3rd_y), (line_h_end_x,line_h_3rd_y), conf._color_baseline, conf._draw_middle_line_thick)              # Horizontal

            if(draw_title == True):
                cv2.rectangle(frame, (title_box_start_x, title_box_start_y), (title_box_end_x, title_box_end_y), conf._color_title_box, -1)
                # draw title
                title_box_end_x = title_box_end_x + 10                
                draw_graph_title(conf._graph_title_side_down, frame, title_pos_x, title_pos_y, conf._color_title, conf._text_size_title, 1)
                draw_graph_title(f"30", frame, title_box_end_x, line_h_1st_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"20", frame, title_box_end_x, line_h_2nd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)
                draw_graph_title(f"10", frame, title_box_end_x, line_h_3rd_y, conf._color_title, conf._text_size_graph_text, 1, False, conf._text_align_right)

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_dotted_line(img, start, end, color, thickness=1, gap=10):
# ─────────────────────────────────────────────────────────────────────────────#
def draw_tracking_graph(frame, frameindex, graph_type, width, height, start_x, start_y):
    
    line_h_start_x = int(start_x + width / 12 * 2  )
    line_h_end_x   = int(start_x + width / 12 * 11 )
    line_h_end_y   = int(start_y + height / 6 * 5 )  

    total_data_cnt = conf._input_frame_count
    bar_base_x = int(start_x + width / 12 * 2 )
    bar_base_y = line_h_end_y
    bar_width = int(line_h_end_x - line_h_start_x)
    bar_unit_width = int(bar_width/total_data_cnt)

    max_text_x = 0
    max_text_y = 0
    min_text_x = 0
    min_text_y = 0
    strMax = f""
    strMin = f""

    match graph_type:
        # swing vertical
        case conf._type_swing_x_angle:            
            array_cnt = len(conf._array_swing_x_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_swing_x_angle)
            max_index = np.argmax(conf._array_swing_x_angle)            
            min_value = min(conf._array_swing_x_angle)
            min_index = np.argmin(conf._array_swing_x_angle)

            for i in range(frameindex):
                value = conf._array_swing_x_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_start = int(bar_base_y - int(value/2))         # 데이터 값에 따라 높이 조정 
                y_end = int(bar_base_y)                
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)   
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_swing_angle_x, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)
        # swing horizontal
        case conf._type_swing_y_angle:
            array_cnt = len(conf._array_swing_y_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            max_value = max(conf._array_swing_y_angle)
            max_index = np.argmax(conf._array_swing_y_angle)            
            min_value = min(conf._array_swing_y_angle)
            min_index = np.argmin(conf._array_swing_y_angle)
            
            for i in range(frameindex):
                value = conf._array_swing_y_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value/2))         # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_swing_angle_y, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)   
        
        # swing horizontal
        case conf._type_swing_z_angle:
            array_cnt = len(conf._array_swing_z_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_swing_z_angle)
            max_index = np.argmax(conf._array_swing_z_angle)            
            min_value = min(conf._array_swing_z_angle)
            min_index = np.argmin(conf._array_swing_z_angle)
            for i in range(frameindex):
                value = conf._array_swing_z_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value/2))         # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_swing_angle_z, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)  

        # waist angle
        case conf._type_waist_y_angle:
            array_cnt = len(conf._array_waist_y_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_waist_y_angle)
            max_index = np.argmax(conf._array_waist_y_angle)            
            min_value = min(conf._array_waist_y_angle)
            min_index = np.argmin(conf._array_waist_y_angle)
            for i in range(frameindex):
                value = conf._array_waist_y_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value * 3))         # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_waist_angle, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left) 

        # elbow left angle
        case conf._type_elbow_l_angle:
            array_cnt = len(conf._array_elbow_l_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_elbow_l_angle)
            max_index = np.argmax(conf._array_elbow_l_angle)            
            min_value = min(conf._array_elbow_l_angle)
            min_index = np.argmin(conf._array_elbow_l_angle)
            for i in range(frameindex):
                value = conf._array_elbow_l_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value/2))         # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_elbow_l_angle, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left) 


        # elbow right angle
        case conf._type_elbow_r_angle:
            array_cnt = len(conf._array_elbow_r_angle)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_elbow_r_angle)
            max_index = np.argmax(conf._array_elbow_r_angle)            
            min_value = min(conf._array_elbow_r_angle)
            min_index = np.argmin(conf._array_elbow_r_angle)
            for i in range(frameindex):
                value = conf._array_elbow_r_angle[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value/2))         # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_elbow_r_angle, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, True, conf._text_align_left) 

        # swing speed
        case conf._type_swing_speed:            
            array_cnt = len(conf._array_swing_speed)
            if (frameindex > array_cnt): frameindex = array_cnt
            # get min/max value and index
            max_value = max(conf._array_swing_speed)
            max_index = np.argmax(conf._array_swing_speed)            
            min_value = min(conf._array_swing_speed)
            min_index = np.argmin(conf._array_swing_speed)
            for i in range(frameindex):
                value = conf._array_swing_speed[i]
                # bar position
                x_start = int(bar_base_x + (i * bar_unit_width))
                x_end = int(bar_base_x + (i + 1) * bar_unit_width - 1)   # 막대 간격을 위해 약간의 여유를 둡니다
                y_end = int(bar_base_y)
                y_start = int(bar_base_y - int(value))                  # 데이터 값에 따라 높이 조정 
                # draw bar
                if(i == max_index):                    
                    strMax = f"{max_value:.2f}m/h"
                    max_text_x = x_start     
                    max_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_max_value, -1)                
                elif(i == min_index):                    
                    strMin = f"{min_value:.2f}m/h"
                    min_text_x = x_start     
                    min_text_y = y_start + 5
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_min_value, -1)               
                else:
                    cv2.rectangle(frame, (x_start, y_start), (x_end, y_end), conf._color_swing_speed, -1)                
            # draw max/min values
            draw_graph_title(strMax, frame, max_text_x, max_text_y, conf._color_max_value_text, conf._text_size_graph_max, 2, False, conf._text_align_left)            
            draw_graph_title(strMin, frame, min_text_x, min_text_y, conf._color_min_value_text, conf._text_size_graph_max, 2, False, conf._text_align_left)  
        
# ─────────────────────────────────────────────────────────────────────────────#
# def draw_graph_title(frame, text, title_x, title_y, color = conf._color_title, font_scale = 1, thickness = 2):
# desc : draw graph title
# ─────────────────────────────────────────────────────────────────────────────#
def draw_graph_title(str_title, frame, title_x, title_y, color = conf._color_title, font_scale = 1, thickness = 2, is_draw_degree  = False, align_type = conf._text_align_right):
    # text configuraiton
    font = cv2.FONT_HERSHEY_SIMPLEX
    # 텍스트 크기 계산
    (text_width, text_height), _ = cv2.getTextSize(str_title, font, font_scale, thickness)    
    x = title_x
    y = title_y
    match align_type:
        case conf._text_align_left:            
            x = int(title_x + conf._graph_draw_text_gap)  # 오른쪽 끝에서 약간 여유를 두기 위해 10 픽셀 추가
            y = y + text_height
        case conf._text_align_center:
            x = int(title_x - text_width/2)
        case conf._text_align_right:
            x = int(title_x - text_width - conf._graph_draw_text_gap)  # 오른쪽 끝에서 약간 여유를 두기 위해 10 픽셀 추가   
    
    if(is_draw_degree == True):
        circle_x = int(x + text_width )                
        circle_y = int(title_y - conf._graph_draw_text_gap / 2 )
        circle_position = (circle_x, circle_y) 
        cv2.circle(frame, circle_position, 4, color, 2)
    
    # 텍스트 이미지에 추가
    cv2.putText(frame, str_title, (x, y), font, font_scale, color, thickness)

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_dotted_line(img, start, end, color, thickness=1, gap=10):
# ─────────────────────────────────────────────────────────────────────────────#
def draw_dotted_line(img, start, end, color, thickness=1, gap=10):
    # 시작점과 끝점 좌표 설정
    x1, y1 = start
    x2, y2 = end    
    # 선의 길이 및 각도 계산
    line_length = ((x2 - x1)**2 + (y2 - y1)**2) ** 0.5
    if(line_length == 0):
        return
    dx = (x2 - x1) / line_length
    dy = (y2 - y1) / line_length    
    # 점선을 이루는 각 점을 그리기
    for i in range(0, int(line_length), gap):
        x = int(x1 + i * dx)
        y = int(y1 + i * dy)
        cv2.circle(img, (x, y), thickness, color, -1)

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_dotted_line(img, start, end, color, thickness=1, gap=10):
# ─────────────────────────────────────────────────────────────────────────────#
def draw_extention_line(img, point_a, point_b, color, thikness = 2, extend_ratio = 1.5, is_dot = False):
    # vector
    ventor = (point_b[0] - point_a[0], point_b[1] - point_a[1])
    # extened points
    ex_point_a = (int(point_a[0] - extend_ratio * ventor[0]), int(point_a[1] - extend_ratio * ventor[1]))  # A에서 왼쪽으로 2배 연장
    ex_point_b = (int(point_b[0] + extend_ratio * ventor[0]), int(point_b[1] + extend_ratio * ventor[1]))  # B에서 오른쪽으로 2배 연장
    # draw extened line
    if(is_dot == True):
        draw_dotted_line(img, ex_point_a, ex_point_b, color, thikness)  
    else:
        cv2.line(img, ex_point_a, ex_point_b, color, thikness)  

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_graph (graph_type, frame, frameindex, width, height, start_x, start_y):
# ─────────────────────────────────────────────────────────────────────────────#
def draw_graph (graph_type, frame, frameindex, width, height, start_x, start_y):

    #draw baseline of graph
    draw_base_line(graph_type, frame, width, height, start_x, start_y)
    
    #exception
    if(frameindex >= conf._input_frame_count):
        return
    # data index
    data_index = frameindex
    if(data_index < 0):
        return
    match graph_type:

        # front up
        # left arm angle 
        case conf._graph_front_up:            
            # exception
            array_cnt = len(conf._array_swing_x_angle)
            if(data_index >= array_cnt): data_index = array_cnt - 1           

            # swing x angle
            # draw 2 digit (x,y)
            value = conf._array_swing_y_angle[data_index]
            str = f"V: {value:.2f} "
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 40 )            
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_swing_angle_y, conf._text_size_2_value, 2, True)

            value = conf._array_swing_x_angle[data_index]
            str = f"H: {value:.2f} "
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 57 )
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_swing_angle_x, conf._text_size_2_value, 2, True)

            # draw x,y,z overlay
            #draw bar graphs (waist bend angle)
            draw_tracking_graph(frame, data_index, conf._type_swing_x_angle, width, height, start_x, start_y)            
            #draw bar graphs (waist bend angle)
            draw_tracking_graph(frame, data_index, conf._type_swing_y_angle, width, height, start_x, start_y)
            
        # front down
        # swing speed
        case conf._graph_front_down:
            # exception
            array_cnt = len(conf._array_swing_speed)
            if(data_index >= array_cnt): data_index = array_cnt - 1
                        
            # swing speed
            # draw digit            
            value = conf._array_swing_speed[data_index]    
            str  = f"{value:.2f}m/h"
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 50 )                
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_swing_speed, conf._text_size_value, 2)
            # draw digit beside line
            #text_pos_x = conf._pt_hp_2d_c[0]
            #text_pos_y = conf._pt_sh_2d_c[1]
            #draw_graph_title(frame, strSpeed, text_pos_x, text_pos_y, conf._color_baseline, conf._text_size_value)            
            draw_tracking_graph(frame, data_index, conf._type_swing_speed, width, height, start_x, start_y)
            

        # side up
        # body angle (shoulder and hip)()
        case conf._graph_side_up:                        
            # exception
            array_cnt = len(conf._array_elbow_l_angle)
            if(data_index >= array_cnt): data_index = array_cnt - 1

            # swing x angle
            # draw 2 digit (x,y)
            value = conf._array_elbow_l_angle[data_index]
            str = f"L: {value:.2f} "
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 40 )            
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_elbow_l_angle, conf._text_size_2_value, 2, True)

            value = conf._array_elbow_r_angle[data_index]
            str = f"R: {value:.2f} "
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 57 )
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_elbow_r_angle, conf._text_size_2_value, 2, True)

            # draw digit beside line
            #text_pos_x = conf._pt_hp_2d_c[0]
            #text_pos_y = conf._pt_sh_2d_c[1]
            #draw_graph_title(strAngle, frame, text_pos_x, text_pos_y, conf._color_baseline,conf._text_size_value)

            # draw x,y,z overlay
            #draw bar graphs (elbow right)
            draw_tracking_graph(frame, data_index, conf._type_elbow_r_angle, width, height, start_x, start_y)
            #draw bar graphs (elbow left)
            draw_tracking_graph(frame, data_index, conf._type_elbow_l_angle, width, height, start_x, start_y)            
            
        # side down
        # waist angle (shoulder and hip)()
        case conf._graph_side_down:
            # exception
            array_cnt = len(conf._array_waist_y_angle)
            if(data_index >= array_cnt):
                return
            
            # swing waist angle
            # draw digit

            value = conf._array_waist_y_angle[data_index]
            str = f"{value:.2f} "
            text_pos_x = int(start_x + width / 120 * 17 )            
            text_pos_y = int(start_y + height / 120 * 50 )
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_waist_angle, conf._text_size_value, 2, True)
            # draw digit beside line
            text_pos_x = conf._pt_hp_2d_c[0]
            text_pos_y = conf._pt_sh_2d_c[1] + 10
            draw_graph_title(str, frame, text_pos_x, text_pos_y, conf._color_waist_angle, conf._text_size_value, 2, True)

            #draw bar graphs (waist bend angle)
            draw_tracking_graph(frame, data_index, conf._type_waist_y_angle, width, height, start_x, start_y)
    
    # just redraw line
    draw_base_line(graph_type, frame, width, height, start_x, start_y, False)


# ─────────────────────────────────────────────────────────────────────────────#
# def draw_batter_tracking(frame, frameindex, arr_bat, arr_ball):
# drawing the bat swing and ball tracking
# [owner] hongsu jung
# [date] 2025-02-16
# ─────────────────────────────────────────────────────────────────────────────#
def draw_batter_singleline(type_mem, frame, frameindex, tot_count, ball_pos):

    draw_last = 0    
    upscale_factor  = conf._batter_draw_upscale_factor
    line_thickness  = int(12 * upscale_factor)  # 🔹 두께도 업스케일
    
    ##############################
    # draw line layer
    ##############################    
    # pitching
    start_pitching_frame, end_hitting_frame = get_start_end_time(ball_pos)
    draw_last = min(frameindex, end_hitting_frame)   

    # curr
    if type_mem == conf._file_type_curr:
        draw_first = start_pitching_frame
        draw_last = min(frameindex, end_hitting_frame)   
        frame_draw = draw_batter_ball_tracking_single(frame, ball_pos, frameindex, draw_first, draw_last)                      
    # post
    elif type_mem == conf._file_type_post:        
        draw_first = start_pitching_frame  # 고정된 시작 프레임
        draw_last = end_hitting_frame      # 고정된 끝 프레임  
        frameindex += conf._live_player_frames_curr_cnt
        frame_draw = draw_batter_ball_tracking_single(frame, ball_pos, frameindex, draw_first, draw_last)

    return frame_draw

# ─────────────────────────────────────────────────────────────────────────────#
# def draw_pitcher_tracking(frame, frameindex, arr_bat, arr_ball):
# drawing the pitcher's ball tracking
# [owner] joonho kim
# [date] 2025-03-05
# ─────────────────────────────────────────────────────────────────────────────#
def draw_pitcher_singleline(type_mem, frame, frameindex, tot_count, arr):

    upscale_factor = conf._pitcher_draw_upscale_factor
    line_thickness = int(10 * upscale_factor)  # 🔹 두께도 업스케일
    start_pitching_frame, end_hitting_frame = get_start_end_time(arr)

    ##############################
    # set line layer & draw
    ##############################
    # curr
    if type_mem == conf._file_type_curr:
        draw_first = start_pitching_frame
        draw_last = min(frameindex, end_hitting_frame)
        #라인 불투명 처리
        frame_draw = draw_pitcher_ball_tracking_single(frame, arr, draw_first, draw_last, line_thickness, upscale_factor)
        #라인 투명 처리 
        #trajectory_drawing_layer = get_layer_pitcher_line(frame, arr, draw_first, draw_last, line_thickness, upscale_factor)
        #frame = cv2.add(frame, trajectory_drawing_layer)        
    # post 
    elif type_mem == conf._file_type_post:
        draw_first = start_pitching_frame  # 고정된 시작 프레임
        draw_last = end_hitting_frame      # 고정된 끝 프레임  
        #frameindex += conf._curr_buffer_frame

        #라인 그라데이션
        frame_draw = draw_pitcher_ball_tracking_single(frame, arr, draw_first, draw_last, line_thickness, upscale_factor)
        #기존코드 
        #trajectory_drawing_layer = get_layer_pitcher_line(frame, arr, draw_first, draw_last, line_thickness, upscale_factor)
        #frame = cv2.add(frame, trajectory_drawing_layer)
       
    return frame_draw
    
# ─────────────────────────────────────────────────────────────────────────────#
# def draw_hit_singleline
# drawing the pitcher's ball tracking
# [owner] hongsu jung
# [date] 2025-04-08
# ─────────────────────────────────────────────────────────────────────────────#
def draw_hit_singleline(type_mem, frame, frameindex, tot_count, arr):

    upscale_factor = conf._hit_draw_upscale_factor
    line_thickness = int(10 * upscale_factor)  # 🔹 두께도 업스케일
    start_frame, end_frame = get_start_end_time(arr)

    start_color = (255, 255, 255)  # 흰색 (밝은 색)
    end_color = conf._color_kbo_blue
    draw_last = min(frameindex+1, end_frame)

    ##############################
    # curr
    ##############################    
    if type_mem == conf._file_type_curr:
        frame_draw = draw_hit_ball_tracking_single(frame, arr, start_frame, draw_last, tot_count, start_color, end_color, line_thickness, upscale_factor)          
                
    ##############################        
    # post 
    ##############################
    elif type_mem == conf._file_type_post:        
        frame_height = frame.shape[0]
        show_ellipse = True  # 타원 표시 여부        
        # draw circle
        if show_ellipse and frameindex > 1:
            last_point = arr[end_frame] if 0 <= end_frame < len(arr) else None
            if last_point is not None and not np.array_equal(last_point, (-1, -1)):
                ellipse_center = (int(last_point[0]), int(last_point[1]))  # 중심 좌표
                ellipse_axes = (20 , 5 )  # 타원의 크기
                ellipse_color = conf._color_kbo_red
                # 🔹 타원이 빠르게 회전하도록 속도 증가
                start_angle = conf._hit_ellipse_angle
                end_angle = conf._hit_ellipse_angle + 180  # 더 넓은 부분이 보이도록 설정
                # 🔹 타원의 선 두께를 원근감을 반영하여 점점 증가
                ellipse_thickness = int(2 + (last_point[1] / (frame_height )) * 5 )
                cv2.ellipse(frame, ellipse_center, ellipse_axes, 0, start_angle, end_angle, ellipse_color, ellipse_thickness)
                # 🔹 회전 애니메이션 속도를 높임
                conf._hit_ellipse_angle = (conf._hit_ellipse_angle + 20) % 360  # 더 빠른 회전 속도

        frame_draw = draw_hit_ball_tracking_single(frame, arr, start_frame, end_frame, tot_count, start_color, end_color, line_thickness, upscale_factor)
    return frame_draw
    
# ─────────────────────────────────────────────────────────────────────────────#
# def draw_hit_multi_line(type_mem, frame, frameindex, tot_count, arrs):
# drawing the hit balls tracking
# [owner] hongsu jung
# [date] 2025-07-01
# ─────────────────────────────────────────────────────────────────────────────#
def draw_hit_multiline(type_mem, frame, frameindex, tot_count, arrs):

    upscale_factor = conf._hit_multi_upscale_factor
    line_thickness = int(10 * upscale_factor)  # 🔹 두께도 업스케일
    start_color = (255, 255, 255)  # 시작 색상 (흰색)

    # 색상 순환 정의
    color_cycle = [
        conf._color_kbo_orange,
        conf._color_kbo_lightblue,
        conf._color_kbo_red,
        conf._color_kbo_silver,
        conf._color_kbo_gold,
        conf._color_kbo_mint
    ]

    frame_height = frame.shape[0]

    # ➤ 공통: 궤적 인덱스 및 컬러 설정
    start_end_list = [get_start_end_time(arr) for arr in arrs]
    end_colors = [color_cycle[i % len(color_cycle)] for i in range(len(arrs))]

    if type_mem == conf._file_type_curr:
        # ➤ [1] 그릴 마지막 인덱스 지정
        draw_last_list = [min(frameindex + 1, end) for (_, end) in start_end_list]
        # ➤ [2] GPU 기반 궤적 렌더링
        frame = draw_hit_ball_tracking_multi(frame, arrs, start_end_list, draw_last_list,start_color, end_colors, line_thickness, upscale_factor)

    elif type_mem == conf._file_type_post:
        # ➤ [1] 전체 궤적 완전 표시
        draw_last_list = [end for (_, end) in start_end_list]
        # ➤ [2] 타원 강조
        for idx, arr in enumerate(arrs):
            _, end_frame = start_end_list[idx]
            last_point = arr[end_frame] if 0 <= end_frame < len(arr) else None
            if last_point is not None and not np.array_equal(last_point, (-1, -1)):
                ellipse_center = (int(last_point[0]), int(last_point[1]))
                ellipse_axes = (20, 5)
                ellipse_color = conf._color_kbo_red
                start_angle = conf._hit_ellipse_angle
                end_angle = conf._hit_ellipse_angle + 180
                ellipse_thickness = int(2 + (last_point[1] / frame_height) * 5)
                cv2.ellipse(frame, ellipse_center, ellipse_axes, 0,
                            start_angle, end_angle, ellipse_color, ellipse_thickness)

        # ➤ [4] GPU 기반 궤적 렌더링
        frame = draw_hit_ball_tracking_multi(frame, arrs, start_end_list, draw_last_list,start_color, end_colors, line_thickness, upscale_factor)
        # ➤ [5] 회전 각도 업데이트 (타원 효과용)
        conf._hit_ellipse_angle = (conf._hit_ellipse_angle + 20) % 360

    else:
        fd_log.warning(f"[❗] Unknown type_mem: {type_mem}")

    return frame

# ─────────────────────────────────────────────────────────────────────────────
# def draw_pitcher_draw_multi(frame, frameindex, arr_bat, arr_ball):
# drawing the pitcher's ball tracking
# [owner] joonho kim
# [date] 2025-03-05
# ─────────────────────────────────────────────────────────────────────────────#
def draw_pitcher_multiline(type_mem, frame, frameindex, tot_count, arrs, arr_idx, pitch_type = conf._pitch_type, speed = conf._release_speed, spinrate = conf._release_spinrate):

    alpha = 0
    upscale_factor = conf._pitcher_draw_upscale_factor
    line_thickness = int(10 * upscale_factor)  # 🔹 두께도 업스케일
    # get color in pitching type
    start_color, end_color = get_pitch_color(pitch_type)

    ##############################
    # 1. set alpha
    ##############################    
    # currunt frames
    if type_mem == conf._file_type_curr:
        if frameindex < conf._fade_in_duration:
            progress = frameindex/ conf._fade_in_duration
            alpha = progress * conf._draw_max_alpha         
        else:
            alpha = conf._draw_max_alpha
    # post frames
    else:
        fade_out = min(tot_count, conf._fade_out_duration)
        if frameindex > (tot_count - fade_out):
            progress = (tot_count - frameindex)/ fade_out
            beta = progress * conf._draw_max_alpha         
        else:
            beta = conf._draw_max_alpha
        alpha = 1.0 - alpha
    # set beta
    
    # set last drawing layer
    if(conf._cached_trajectory_finish_layer is None):
        conf._cached_trajectory_finish_layer = get_layer_curve_multi_line(frame, arrs, arr_idx, frameindex, tot_count, line_thickness, upscale_factor)
        
    ##############################
    # 2. set line layer
    ##############################
    if type_mem == conf._file_type_curr:
        _cached_trajectory_drawing_layer = get_layer_curve_multi_line(frame, arrs, arr_idx, frameindex, tot_count, line_thickness, upscale_factor)
    elif type_mem == conf._file_type_post:
        _cached_trajectory_drawing_layer = conf._cached_trajectory_finish_layer
    
    ##############################
    # 3. draw line layer
    ##############################    
    # curr    
    if type_mem == conf._file_type_curr:
        # not fade in
        frame = cv2.add(frame, _cached_trajectory_drawing_layer)
    # post
    else:
        # fade out
        pics_frame = frame.copy()        
        frame = cv2.addWeighted(pics_frame, alpha, conf._cached_trajectory_finish_layer, beta, 0.0)
        
    ##############################
    # 4. draw box and text
    ##############################
    if type_mem == conf._file_type_curr:        
        frame = draw_fade_in_image(frame, conf._pitch_box_img, conf._pitch_box_x, conf._pitch_box_y, alpha)  
        frame = draw_fade_in_text_pitch(frame, pitch_type, spinrate, speed, conf._font_path_main, conf._pitch_box_x, conf._pitch_box_y, alpha)  
        
    return frame
    
