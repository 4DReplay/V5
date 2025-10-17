import os
os.add_dll_directory(r"C:\Program Files\OpenCV-CUDA\x64\vc17\bin")
os.add_dll_directory(r"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4/bin")

import cv2
print(cv2.__file__)
print(cv2.getBuildInformation())
print(cv2.cuda.getCudaEnabledDeviceCount())