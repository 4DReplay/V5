import GPUtil
import numpy as np
from loguru import logger
import psutil

def get_gpu_info():
    gpus = GPUtil.getGPUs()
    gpu_info = []
    for gpu in gpus:
        gpu_info.append({
            "name": gpu.name,
            "id": gpu.id,
            "total_mem": gpu.memoryTotal
        })

    return gpu_info

def get_hw_usage_info():
    # CPU 사용률 얻기
    cpu_percent = psutil.cpu_percent(interval=1)  # 1초 동안의 CPU 사용률
    print(f'CPU 사용률: {cpu_percent}%')

    # 메모리 사용량 얻기
    memory = psutil.virtual_memory()
    print(f'전체 메모리: {memory.total} bytes')
    print(f'사용 중인 메모리: {memory.used} bytes')
    print(f'사용 가능한 메모리: {memory.available} bytes')

    # 디스크 사용률 얻기
    disk = psutil.disk_usage('/')
    print(f'전체 디스크 용량: {disk.total} bytes')
    print(f'사용 중인 디스크 용량: {disk.used} bytes')
    print(f'사용 가능한 디스크 용량: {disk.free} bytes')


# backup code, to be safe
class ImageNumpyBuffer:
    def __init__(self, max_size=60):
        self.max_size = max_size
        self.buffer = np.empty((0, 3), dtype=object)    # initialize an empty array with object dtype

    def add_image(self, img, pts):
        if len(self.buffer) >= self.max_size:
            # only to use array index in numpy -> buffer[0].pts (x)
            logger.info("[imagenumpybuffer] add_image() to remove the oldest row pts:{}".format(self.buffer[0][1]))
            self.buffer = np.delete(self.buffer, 0, axis=0) # remove the oldest row if buffer is full
        # self.buffer = np.vstack((self.buffer, np.array([[img, pts]], dtype=object)))
        data_to_add = np.array([[img, pts]], dtype=object)
        self.buffer = np.vstack((self.buffer, data_to_add))

    def get_image(self):
        return self.buffer
