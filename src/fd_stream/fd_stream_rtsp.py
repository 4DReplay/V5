# stream_viewer.py
import av
import cv2
import logging
import numpy as np

from collections import deque
from fd_utils.fd_logging            import fd_log

class StreamViewer:
    def __init__(self, buffer_size=60):
        self.buffer_size = buffer_size
        self.frame_buffer = deque(maxlen=self.buffer_size)  # 외부 접근 가능하게 선언
        self.frame_index = 0  # 외부에서도 현재 인덱스를 확인할 수 있게 선언

    def preview_rtsp_stream_pyav(
        self,
        rtsp_url: str,
        width: int = None,
        height: int = None,
        preview: bool = True
    ):
        try:
            container = av.open(rtsp_url, options={"rtsp_transport": "tcp"})
            fd_log.info(f"[Info] Stream open: {rtsp_url}")
        except av.AVError as e:
            fd_log.info(f"[Error] Stream open fail: {e}")
            return

        gpu_frame = cv2.cuda_GpuMat()

        try:
            for i, frame in enumerate(container.decode(video=0)):
                if i % 2 != 0:  # ✅ 한 프레임 건너뛰기 (60fps → 30fps)
                    continue

                if frame is None:
                    fd_log.warning("[Warning] Frame is None.")
                    continue

                img = frame.to_ndarray(format="bgr24")
                self.frame_buffer.append((self.frame_index, img.copy()))
                self.frame_index += 1

                if preview and self.frame_index % 5 == 0:
                    gpu_frame.upload(img)
                    if width and height:
                        gpu_frame = cv2.cuda.resize(gpu_frame, (width, height))
                    preview_img = gpu_frame.download()
                    cv2.imshow(f"RTSP - {rtsp_url}", preview_img)
                    cv2.waitKey(1)

        except av.AVError as e:
            fd_log.error(f"[Error] decoding fail: {e}")

        finally:
            container.close()
            if preview:
                try:
                    cv2.destroyWindow(f"RTSP - {rtsp_url}")
                    cv2.waitKey(1)
                except Exception as e:
                    logging.warning(f"[Warnning] Close window error: {e}")



