import cv2, numpy as np
print(cv2.__file__)
print("CUDA devices:", cv2.cuda.getCudaEnabledDeviceCount())

# 간단 warpAffine(CPU) + NVENC 파이프 확인 (ffmpeg 설치 가정)
h,w=720,1280
M=cv2.getRotationMatrix2D((w/2,h/2), 5, 1.0)
frm=np.full((h,w,3), 40, np.uint8)
for _ in range(60): frm=cv2.warpAffine(frm,M,(w,h))

print("OK")