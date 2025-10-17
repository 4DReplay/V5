# ─────────────────────────────────────────────────────────────────────────────#
#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_mediapipe
# - 2024/11/05
# - Hongsu Jung
# https://4dreplay.atlassian.net/wiki/x/F4DofQ
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import numpy as np
import mediapipe as mp
import torch
from ultralytics import YOLO
from pykalman import KalmanFilter
import fd_utils.fd_config   as conf
from fd_utils.fd_logging        import fd_log

yolo_landmark_names = [
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle"
]

# MediaPipe 설정
mp_pose = mp.solutions.pose
mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils

# 추론기 초기화 (1회만)
pose_detector = mp_pose.Pose(
    static_image_mode=False,              # 연속 추론
    model_complexity=2,                   # 높은 정확도
    min_detection_confidence=0.6,
    min_tracking_confidence=0.5
)
hand_detector = mp_hands.Hands(static_image_mode=False, max_num_hands=2, min_detection_confidence=0.5)

landmark_names = {
    "nose": mp_pose.PoseLandmark.NOSE,
    "left_eye_inner": mp_pose.PoseLandmark.LEFT_EYE_INNER,
    "left_eye": mp_pose.PoseLandmark.LEFT_EYE,
    "left_eye_outer": mp_pose.PoseLandmark.LEFT_EYE_OUTER,
    "right_eye_inner": mp_pose.PoseLandmark.RIGHT_EYE_INNER,
    "right_eye": mp_pose.PoseLandmark.RIGHT_EYE,
    "right_eye_outer": mp_pose.PoseLandmark.RIGHT_EYE_OUTER,
    "left_ear": mp_pose.PoseLandmark.LEFT_EAR,
    "right_ear": mp_pose.PoseLandmark.RIGHT_EAR,
    "mouth_left": mp_pose.PoseLandmark.MOUTH_LEFT,
    "mouth_right": mp_pose.PoseLandmark.MOUTH_RIGHT,
    "left_shoulder": mp_pose.PoseLandmark.LEFT_SHOULDER,
    "right_shoulder": mp_pose.PoseLandmark.RIGHT_SHOULDER,
    "left_elbow": mp_pose.PoseLandmark.LEFT_ELBOW,
    "right_elbow": mp_pose.PoseLandmark.RIGHT_ELBOW,
    "left_wrist": mp_pose.PoseLandmark.LEFT_WRIST,
    "right_wrist": mp_pose.PoseLandmark.RIGHT_WRIST,
    "left_pinky": mp_pose.PoseLandmark.LEFT_PINKY,
    "right_pinky": mp_pose.PoseLandmark.RIGHT_PINKY,
    "left_index": mp_pose.PoseLandmark.LEFT_INDEX,
    "right_index": mp_pose.PoseLandmark.RIGHT_INDEX,
    "left_thumb": mp_pose.PoseLandmark.LEFT_THUMB,
    "right_thumb": mp_pose.PoseLandmark.RIGHT_THUMB,
    "left_hip": mp_pose.PoseLandmark.LEFT_HIP,
    "right_hip": mp_pose.PoseLandmark.RIGHT_HIP,
    "left_knee": mp_pose.PoseLandmark.LEFT_KNEE,
    "right_knee": mp_pose.PoseLandmark.RIGHT_KNEE,
    "left_ankle": mp_pose.PoseLandmark.LEFT_ANKLE,
    "right_ankle": mp_pose.PoseLandmark.RIGHT_ANKLE,
    "left_heel": mp_pose.PoseLandmark.LEFT_HEEL,
    "right_heel": mp_pose.PoseLandmark.RIGHT_HEEL,
    "left_foot_index": mp_pose.PoseLandmark.LEFT_FOOT_INDEX,
    "right_foot_index": mp_pose.PoseLandmark.RIGHT_FOOT_INDEX,
}

def to_int_xy(p):
    return tuple(map(int, p[:2])) if p else None

# ─────────────────────────────────────────────────────────────────────────────
# def detect_fingers(frame):
# [owner] hongsu jung
# [date] 2025/02/11
# ─────────────────────────────────────────────────────────────────────────────
def detect_fingers(frame):

    height, width = frame.shape[:2]
    area = conf._detect_area_precentage 
    
    # 초기 ROI 설정
    if conf._type_target == conf._type_baseball_pitcher:
        crop_x1 = max(0, int(width/4 - width * (area / 2)))
        crop_x2 = max(0, int(width/4 + width * (area / 2))) 
    else:
        crop_x1 = max(0, int(width/2 - width * (area / 2)))
        crop_x2 = max(0, int(width/2 + width * (area / 2))) 
        
    crop_y1 = max(0, int(height/2 - height * (area / 2))) 
    crop_y2 = max(0, int(height/2 + height * (area / 2))) 
    
    while True:
        keypoint = {}
        cropped_frame = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        cropped_height, cropped_width = cropped_frame.shape[:2]
        frame_rgb = cv2.cvtColor(cropped_frame, cv2.COLOR_BGR2RGB)

        # 포즈 감지 실행
        pose_result = pose_detector.process(frame_rgb)
        detected_people = []  # 감지된 인물 리스트
        if pose_result.pose_landmarks:
            landmarks = pose_result.pose_landmarks.landmark
            nose_x = landmarks[mp_pose.PoseLandmark.NOSE].x * cropped_width + crop_x1
            left_shoulder_x = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].x * cropped_width + crop_x1
            right_shoulder_x = landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER].x * cropped_width + crop_x1
            body_center_x = (nose_x + left_shoulder_x + right_shoulder_x) / 3

            detected_people.append((body_center_x, pose_result.pose_landmarks))
            detected_people.sort(key=lambda person: abs(person[0] - (cropped_width // 2)))

        # 감지된 사람이 있을 경우만 처리
        if detected_people:
            closest_person = detected_people[0]
            # Pose 키포인트 저장
            for landmark_name, idx in mp_pose.PoseLandmark.__members__.items():
                landmark = closest_person[1].landmark[idx]
                keypoint[landmark_name] = (
                    landmark.x * cropped_width + crop_x1,
                    landmark.y * cropped_height + crop_y1,
                    landmark.z
                )
            # Pose를 화면에 그리기 (감지된 사람이 있을 경우만)
            # draw_pose_on_frame(frame, closest_person[1], crop_x1, crop_y1, cropped_width, cropped_height)
            right_hand_x    = landmarks[mp_pose.PoseLandmark.RIGHT_INDEX].x * cropped_width + crop_x1
            right_hand_y    = landmarks[mp_pose.PoseLandmark.RIGHT_INDEX].y * cropped_height + crop_y1
            left_hand_x     = landmarks[mp_pose.PoseLandmark.LEFT_INDEX].x * cropped_width + crop_x1
            left_hand_y     = landmarks[mp_pose.PoseLandmark.LEFT_INDEX].y * cropped_height + crop_y1
            return True, (right_hand_x, right_hand_y), (left_hand_x, left_hand_y)

        # 감지 실패 시 영역 확대
        area += 0.05
        if area > 1:
            break

        # 감지 영역 확장
        if conf._type_target == conf._type_baseball_pitcher:
            crop_x1 = max(0, int(width/4 - width * (area / 2)))
            crop_x2 = max(0, int(width/4 + width * (area / 2))) 
        else:
            crop_x1 = max(0, int(width/2 - width * (area / 2)))
            crop_x2 = max(0, int(width/2 + width * (area / 2))) 
            
        crop_y1 = max(0, int(height/2 - height * (area / 3)))  # 세로 확장은 천천히 증가
        crop_y2 = max(0, int(height/2 + height * (area / 3)))
        
        # 감지 영역 확장
        if conf._type_target == conf._type_baseball_pitcher:
            crop_x1 = max(0, int(width/4 - width * (area / 2)))
            crop_x2 = max(0, int(width/4 + width * (area / 2))) 
        else:
            crop_x1 = max(0, int(width/2 - width * (area / 2)))
            crop_x2 = max(0, int(width/2 + width * (area / 2))) 
            
        crop_y1 = max(0, int(height/2 - height * (area / 3)))  # 세로 확장은 천천히 증가
        crop_y2 = max(0, int(height/2 + height * (area / 3)))
    
    return False, None, None




# ─────────────────────────────────────────────────────────────────────────────
# def detect_fingers_yolo(frame):
# [owner] hongsu jung
# [date] 2025-07-28
# ─────────────────────────────────────────────────────────────────────────────
def detect_fingers_yolo(frame):
    
    try:
        # ─────────────────────────────────────────────────────────────────────────────
        # 0️⃣ Configuration
        # ─────────────────────────────────────────────────────────────────────────────
        yolo_model = conf._yolo_model_m
        yolo_pose = conf._yolo_model_pose_x
        b_detect = False
        # ─────────────────────────────────────────────────────────────────────────────
        # 1️⃣ Detect presence of pitcher
        # ─────────────────────────────────────────────────────────────────────────────
        if frame is None or frame.shape[0] == 0 or frame.shape[1] == 0:
            return

        detection_result = yolo_model.predict(
            frame,
            classes=[conf._yolo_class_id_person],
            conf=conf._yolo_live_conf,
            imgsz=conf._yolo_live_size,
            verbose=False,
            device='cuda'
        )[0]

        b_detect = False
        roi_person = None
        left_person_box = None
        
        # 1️⃣ 가장 왼쪽 사람 box 탐색
        if detection_result.boxes is not None and len(detection_result.boxes) > 0:
            min_x = float('inf')
            for box in detection_result.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                if x1 < min_x:                    
                    min_x = x1                    
                    left_person_box = (x1, y1, x2, y2)

        # ─────────────────────────────────────────────────────────────────────────────
        # 2️⃣ Pose Estimation 대상 선택
        # ─────────────────────────────────────────────────────────────────────────────
        if left_person_box:
            x1, y1, x2, y2 = left_person_box
            roi_person = [(x1, y1), (x2, y2)]
            if conf._detection_viewer:
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 5)

            person_frame = frame_resize(frame, roi_person, 1)            
            # 🧠 Pose detection (keypoint)
            pose_results = yolo_pose(person_frame, stream=False, verbose=False, device='cuda')
            if not pose_results or len(pose_results) == 0:
                return
            batch_keypoints = process_result_batch(pose_results)
            if not batch_keypoints or batch_keypoints[0] is None:
                return

            kp = batch_keypoints[0]
            left_hand = to_int_xy(kp.get("left_wrist"))
            right_hand = to_int_xy(kp.get("right_wrist"))   
            left_ear = to_int_xy(kp.get("left_ear"))   
            right_ear = to_int_xy(kp.get("right_ear"))   
            left_shoulder = to_int_xy(kp.get("left_shoulder"))   
            right_shoulder = to_int_xy(kp.get("right_shoulder"))   
            
            # 1️⃣ throwing_hand 좌표를 resized_frame 기준으로 변환
            x_offset, y_offset = roi_person[0]
            left_hand_global = (
                left_hand[0] + x_offset,
                left_hand[1] + y_offset
            )
            right_hand_global = (
                right_hand[0] + x_offset,
                right_hand[1] + y_offset
            )
            left_ear_global = (
                left_ear[0] + x_offset,
                left_ear[1] + y_offset
            )
            right_ear_global = (
                right_ear[0] + x_offset,
                right_ear[1] + y_offset
            )
            left_shoulder_global = (
                left_shoulder[0] + x_offset,
                left_shoulder[1] + y_offset
            )
            right_shoulder_global = (
                right_shoulder[0] + x_offset,
                right_shoulder[1] + y_offset
            )


            b_detect = True            
            # 🟢 drawing hand position
            if conf._detection_viewer:
                cv2.circle(frame, left_hand_global, 10, (0, 0, 255), 3)
                cv2.circle(frame, right_hand_global, 10, (0, 0, 255), 3)
                cv2.circle(frame, left_ear_global, 30, (255, 0, 255), 3)
                cv2.circle(frame, right_ear_global, 30, (255, 0, 255), 3)
                cv2.circle(frame, left_shoulder_global, 50, (0, 255, 0), 3)
                cv2.circle(frame, right_shoulder_global, 50, (0, 255, 0), 3)

    except AttributeError as e:
        fd_log.warning(f"🔥 AttributeError: {str(e)}")
        fd_log.warning(f"⚠️ Check if YOLO model is correctly loaded and API version matches")
    except Exception as e:
        fd_log.error(f"🔥 Unexpected error: {str(e)}")
        pass
    finally:
        if b_detect:
            return True, left_hand_global, right_hand_global, left_ear_global, right_ear_global, left_shoulder_global, right_shoulder_global

    return False, None, None, None, None, None, None

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
        largest_idx = areas.index(max(areas))
        keypoints_xy = keypoints[largest_idx].cpu().numpy()
        updated_keypoints = {}
        for i, name in enumerate(yolo_landmark_names):
            x, y = keypoints_xy[i]
            updated_keypoints[name] = (float(x), float(y), 0.0)
        batch_keypoints.append(updated_keypoints)

    return batch_keypoints


# ─────────────────────────────────────────────────────────────────────────────
# def detect_fingers_yolo_roi(frame):
# [owner] yelin kim
# [date] 2025-07-29
# ─────────────────────────────────────────────────────────────────────────────
def detect_fingers_yolo_roi(frame):
    try:
        # ─────────────────────────────────────────────────────────────────────────────
        # 0️⃣ Configuration
        # ─────────────────────────────────────────────────────────────────────────────
        yolo_pose = conf._yolo_model_pose_x
        b_detect = False

        # ─────────────────────────────────────────────────────────────────────────────
        # 1️⃣ Frame 검사
        # ─────────────────────────────────────────────────────────────────────────────
        if frame is None or frame.shape[0] == 0 or frame.shape[1] == 0:
            return False, None, None, None, None, None, None

        h, w, _ = frame.shape

        # ─────────────────────────────────────────────────────────────────────────────
        # 2️⃣ 중심 기준 ROI 설정 (투수가 존재할 가능성 높은 위치)
        # ─────────────────────────────────────────────────────────────────────────────
        roi_x1 = int(w * 0.1)
        roi_x2 = int(w * 0.6)
        roi_y1 = int(h * 0.2)
        roi_y2 = int(h * 1)


        roi_person = [(roi_x1, roi_y1), (roi_x2, roi_y2)]

        frame_roi = frame[roi_y1:roi_y2, roi_x1:roi_x2]

        if conf._detection_viewer:
            cv2.rectangle(frame, (roi_x1, roi_y1), (roi_x2, roi_y2), (0, 255, 0), 5)

        # ─────────────────────────────────────────────────────────────────────────────
        # 3️⃣ Pose Estimation
        # ─────────────────────────────────────────────────────────────────────────────
        pose_results = yolo_pose(frame_roi, stream=False, verbose=False, device='cuda')
        if not pose_results or len(pose_results) == 0:
            fd_log.error(f"pose_results is none")
            if conf._detection_viewer:
                debug_frame = frame_roi.copy()
                cv2.putText(debug_frame, "❌ No pose detected", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                cv2.imshow("Debug - pose_results is None", debug_frame)
                cv2.waitKey(1)  # 또는 0으로 하면 멈춤, 디버깅용
                return False, None, None, None, None, None, None
        batch_keypoints = process_result_batch(pose_results)
        if not batch_keypoints or batch_keypoints[0] is None:
            fd_log.error(f"process_result_batch. batch_keypoints is none")
            if conf._detection_viewer:
                debug_frame = frame_roi.copy()
                cv2.putText(debug_frame, "❌ No pose detected", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
                cv2.imshow("Debug - pose_results is None", debug_frame)
                cv2.waitKey(1)  # 또는 0으로 하면 멈춤, 디버깅용
                return False, None, None, None, None, None, None
            return False, None, None, None, None, None, None

        kp = batch_keypoints[0]
        left_hand = to_int_xy(kp.get("left_wrist"))
        right_hand = to_int_xy(kp.get("right_wrist"))   
        left_ear = to_int_xy(kp.get("left_ear"))   
        right_ear = to_int_xy(kp.get("right_ear"))   
        left_shoulder = to_int_xy(kp.get("left_shoulder"))   
        right_shoulder = to_int_xy(kp.get("right_shoulder"))   
        
        # 1️⃣ throwing_hand 좌표를 resized_frame 기준으로 변환
        x_offset, y_offset = roi_person[0]
        left_hand_global = (
            left_hand[0] + x_offset,
            left_hand[1] + y_offset
        )
        right_hand_global = (
            right_hand[0] + x_offset,
            right_hand[1] + y_offset
        )
        left_ear_global = (
            left_ear[0] + x_offset,
            left_ear[1] + y_offset
        )
        right_ear_global = (
            right_ear[0] + x_offset,
            right_ear[1] + y_offset
        )
        left_shoulder_global = (
            left_shoulder[0] + x_offset,
            left_shoulder[1] + y_offset
        )
        right_shoulder_global = (
            right_shoulder[0] + x_offset,
            right_shoulder[1] + y_offset
        )
        b_detect = True

        # ─────────────────────────────────────────────────────────────────────────────
        # 4️⃣ 시각화
        # ─────────────────────────────────────────────────────────────────────────────
        if conf._detection_viewer:


            cv2.circle(frame, left_hand_global, 10, (0, 0, 255), 3)
            cv2.circle(frame, right_hand_global, 10, (0, 0, 255), 3)
            cv2.circle(frame, left_ear_global, 30, (255, 0, 255), 3)
            cv2.circle(frame, right_ear_global, 30, (255, 0, 255), 3)
            cv2.circle(frame, left_shoulder_global, 50, (0, 255, 0), 3)
            cv2.circle(frame, right_shoulder_global, 50, (0, 255, 0), 3)
            

    except AttributeError as e:
        fd_log.warning(f"🔥 AttributeError: {str(e)}")
        fd_log.warning(f"⚠️ Check if YOLO model is correctly loaded and API version matches")
    except Exception as e:
        fd_log.error(f"🔥 Unexpected error: {str(e)}")
        pass
    finally:
        if b_detect:
            return True, left_hand_global, right_hand_global, left_ear_global, right_ear_global, left_shoulder_global, right_shoulder_global

    return False, None, None, None, None, None, None


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

def fd_detect_area_keypoints(type_target, frame, keypoint):

    height, width = frame.shape[:2]
    area = conf._detect_area_precentage 

    # 초기 ROI 설정
    if type_target == conf._type_baseball_pitcher:
        crop_x1 = max(0, int(width/4 - width * (area / 2)))
        crop_x2 = max(0, int(width/4 + width * (area / 2))) 
    else:
        crop_x1 = max(0, int(width/2 - width * (area / 2)))
        crop_x2 = max(0, int(width/2 + width * (area / 2))) 
        
    crop_y1 = max(0, int(height/2 - height * (area / 2))) 
    crop_y2 = max(0, int(height/2 + height * (area / 2))) 

    while True:
        cropped_frame = frame[crop_y1:crop_y2, crop_x1:crop_x2]
        cropped_height, cropped_width = cropped_frame.shape[:2]
        # 해상도 증가 (작은 손을 감지하기 위해)
        frame_rgb = cv2.cvtColor(cropped_frame, cv2.COLOR_BGR2RGB)

        # 포즈 감지 실행
        pose_result = pose_detector.process(frame_rgb)

        detected_people = []  # 감지된 인물 리스트
        if pose_result.pose_landmarks:
            landmarks = pose_result.pose_landmarks.landmark
            nose_x = landmarks[mp_pose.PoseLandmark.NOSE].x * cropped_width + crop_x1
            left_shoulder_x = landmarks[mp_pose.PoseLandmark.LEFT_SHOULDER].x * cropped_width + crop_x1
            right_shoulder_x = landmarks[mp_pose.PoseLandmark.RIGHT_SHOULDER].x * cropped_width + crop_x1
            body_center_x = (nose_x + left_shoulder_x + right_shoulder_x) / 3

            detected_people.append((body_center_x, pose_result.pose_landmarks))
            # 중앙과 가장 가까운 사람 선택
            detected_people.sort(key=lambda person: abs(person[0] - (cropped_width // 2)))

        # 손 감지 실행
        hand_results = hand_detector.process(frame_rgb)
        if hand_results.multi_hand_landmarks:
            for hand_landmarks in hand_results.multi_hand_landmarks:
                for idx, landmark in enumerate(hand_landmarks.landmark):
                    keypoint[f'HAND_{idx}'] = (
                        landmark.x * cropped_width + crop_x1,
                        landmark.y * cropped_height + crop_y1,
                        landmark.z
                    )

        # 감지된 사람이 있을 경우만 처리
        if detected_people:
            closest_person = detected_people[0]

            # Pose 키포인트 저장
            for landmark_name, idx in mp_pose.PoseLandmark.__members__.items():
                landmark = closest_person[1].landmark[idx]
                keypoint[landmark_name] = (
                    landmark.x * cropped_width + crop_x1,
                    landmark.y * cropped_height + crop_y1,
                    landmark.z
                )

            # Pose를 화면에 그리기 (감지된 사람이 있을 경우만)
            #draw_pose_on_frame(frame, closest_person[1], crop_x1, crop_y1, cropped_width, cropped_height)
            return True

        # 감지 실패 시 영역 확대
        area += 0.05
        if area > 1:
            break

        # 감지 영역 확장
        if type_target == conf._type_baseball_pitcher:
            crop_x1 = max(0, int(width/4 - width * (area / 2)))
            crop_x2 = max(0, int(width/4 + width * (area / 2))) 
        else:
            crop_x1 = max(0, int(width/2 - width * (area / 2)))
            crop_x2 = max(0, int(width/2 + width * (area / 2))) 
            
        crop_y1 = max(0, int(height/2 - height * (area / 3)))  # 세로 확장은 천천히 증가
        crop_y2 = max(0, int(height/2 + height * (area / 3)))

    return False  # 감지 실패

def draw_pose_on_frame(frame, pose_landmarks, crop_x1, crop_y1, cropped_width, cropped_height):
    """ 감지된 사람의 Pose를 원본 프레임 위에 그려줌 """
    for landmark in pose_landmarks.landmark:
        x = int(landmark.x * cropped_width + crop_x1)
        y = int(landmark.y * cropped_height + crop_y1)
        cv2.circle(frame, (x, y), 5, (0, 255, 0), -1)  # 초록색 점

    cv2.imshow("[Detect] Person", frame)
    cv2.waitKey(0)

# ─────────────────────────────────────────────────────────────────────────────
# def detect_2d_keypoints_yolo(frame, keypoint, nCamIndex):
# [owner] hongsu jung
# [date] 2025-04-29
# ─────────────────────────────────────────────────────────────────────────────
def detect_2d_keypoints_yolo(image, model):
    
    results = model(image, verbose=False)
    # 검출 실패 시
    if not results or not results[0].keypoints or not results[0].boxes:
        return None

    boxes = results[0].boxes.xyxy.cpu().numpy()        # (N, 4): x1, y1, x2, y2
    keypoints = results[0].keypoints.xy                # (N, 17, 2)
    if keypoints.shape[0] == 0:
        return None

    # ▶️ 각 box의 넓이 계산
    areas = [(x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in boxes]

    # ▶️ 가장 큰 box를 가진 사람의 인덱스
    largest_idx = areas.index(max(areas))
    keypoints_xy = keypoints[largest_idx].cpu().numpy()  # (17, 2)

    # ▶️ 이름 매핑
    updated_keypoints = {}
    for i, name in enumerate(yolo_landmark_names):
        x, y = keypoints_xy[i]
        updated_keypoints[name] = (float(x), float(y), 0.0)

    return updated_keypoints