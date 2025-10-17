import os
os.add_dll_directory(r"C:\Program Files\OpenCV-CUDA\x64\vc17\bin")
os.add_dll_directory(r"C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.4/bin")

import PyNvVideoCodec as nvc
print("GPUs:", nvc.get_num_gpus())              # 예: 1+
dec = nvc.Decoder("./videos/input/sample/test.mp4")                     # NVDEC
print("W,H,FPS:", dec.width(), dec.height(), dec.framerate())