
from collections import namedtuple

# ─────────────────────────────────────────────────────────────────────────────######
# DO NOT CHANGE THIS VALUE
REMOTE_BUFFER_MODE = 1
LOCAL_FILE_MODE = 2
AUDIO_ONLY_MODE = 3
VIDEO_ONLY_MODE = 4
AUDIO_VIDEO_MODE = 5
# ─────────────────────────────────────────────────────────────────────────────######

ROTATE_NONE = 0
ROTATE_CW_90 = 1
ROTATE_CCW_90 = 2

# error code
ERR_SUCCESS = 0
ERR_FAIL = -1
ERR_AGAIN = -15

FRAME_IMAGE = 0
FRAME_END = -1

# 프레임과 추가 정보를 담은 클래스
class FrameInfo:
    def __init__(self, frame, pts, type, width, height, pitch, fmt):
        self.frame = frame
        self.pts = pts
        self.type = type
        self.width = width
        self.height = height
        self.pitch = pitch
        self.idx = 0
        self.fmt = fmt

class AudioFrame:
    def __init__(self, frame, pts, length, wrap):
        self.frame = frame
        self.pts = pts
        self.len = length
        self.wrap = wrap

class FrameExtraInfo:
    def __init__(self, cls_frame, extra_info):
        self.cls_frame = cls_frame
        self.extra = extra_info

EncParam = namedtuple('EncParam', ['path','codec','width','height', 'fps', 'bps', 'gop', 'rotate_mode'])
RangeParam = namedtuple('RangeParam', ['file_frames','start_file','start_frame','end_file', 'end_frame'])
SrcParam = namedtuple('SrcParam', ['camid','pos','src_path', 'out_file'])