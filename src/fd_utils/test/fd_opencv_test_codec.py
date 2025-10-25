import os
os.add_dll_directory(r"C:/Program Files/OpenCV-CUDA/x64/vc17/bin")
os.add_dll_directory(r"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4/bin")

import cv2
print("opencv:", cv2.__version__)
assert hasattr(cv2, "cudacodec")

print("opencv:", cv2.__version__)
assert hasattr(cv2, "cudacodec")

W, H, FPS = 1280, 720, 30.0
out_es = r"C:\tmp\out_nvenc.h264"   # ★ 원시 스트림으로 저장 (MP4 아님)

os.makedirs(os.path.dirname(out_es), exist_ok=True)

# 파라미터: 존재하는 것만 세팅
p = cv2.cudacodec.EncoderParams()

def set_if_has(obj, name, value):
    if hasattr(obj, name):
        setattr(obj, name, value)
        return True
    return False

# 레이트 컨트롤: CBR
set_if_has(p, "rcMode", cv2.cudacodec.ENC_PARAMS_RC_CBR)

# 평균 비트레이트: 필드명이 빌드마다 다를 수 있음
for k in ("avgBitrate", "averageBitrate", "bitrate", "targetBitrate"):
    if set_if_has(p, k, 8_000_000):   # 8 Mbps
        break

# GOP 길이
for k in ("gopLength", "GOPLength", "gop"):
    if set_if_has(p, k, 60):
        break

# (선택) 프로파일: 빌드에 따라 있을 수도/없을 수도
for k in ("profile", "codecProfile", "encProfile"):
    if set_if_has(p, k, cv2.cudacodec.ENC_H264_PROFILE_HIGH):
        break

# VideoWriter 생성 (입력은 BGR GpuMat)
vw = cv2.cudacodec.createVideoWriter(
    out_es,
    (W, H),
    cv2.cudacodec.H264,     # 또는 cv2.cudacodec.HEVC
    FPS,
    cv2.cudacodec.BGR,
    p
)

gpu = cv2.cuda_GpuMat(H, W, cv2.CV_8UC3)
gpu.setTo((0, 0, 0))        # 테스트용 검정 화면
for _ in range(60):
    vw.write(gpu)
del vw

print("Wrote elementary stream:", out_es, "size:", os.path.getsize(out_es))