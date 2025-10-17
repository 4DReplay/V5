import os
import ctypes
import platform
import time
from ctypes import *
import numpy as np

from loguru import logger
import const as cst


# ─────────────────────────────────────────────────────────────────────────────######
# load trans engine library
if platform.system() == "Linux":
	path_trans = "./so/libffi_trans.so"
	FFI = ctypes.cdll.LoadLibrary(path_trans)
elif platform.system() == "Windows":
    script_directory = os.path.dirname(os.path.abspath(__file__))
    path_trans = os.path.join(script_directory, "..", "dll", "trans.dll")
    FFI = ctypes.WinDLL(path_trans)
else:
	raise NotImplementedError("Unsupported platform")

class LogCtx(Structure):
    _fields_ = (
        ('name', c_char_p),
        ('level', c_int),
        ('rollSize', c_int),
        ('rollCount', c_int)
    )

class PyAudio(Structure):
    _fields_ = (
        ('data', POINTER(c_char)),
        ('pts', c_int64),
        ('length', c_int),
        ('wrap', c_int)
    )
class PyVideo(Structure):
    _fields_ = (
        ('data', POINTER(c_char)),
        ('width', c_int),
        ('height', c_int),
        ('pitch', c_int),
        ('pts', c_int64),
        ('idx', c_uint),
        ('fmt', c_int),
    )

class ReaderParam(Structure):
    _fields_ = (
        ('url', c_char_p),
        ('reWidth', c_int),
        ('reHeight', c_int),
        ('reframe', c_int),
        ('readerType', c_int),
        ('outFmt', c_int),
    )

class WriterParam(Structure):
    _fields_ = (
        ('url', c_char_p),
        ('fps', c_int),
        ('bps', c_int),
        ('width', c_int),
        ('height', c_int),
        ('gop', c_int),
        ('use_gpu', c_bool),
    )

class MediaParam(Structure):
    _fields_ = (
        ('vcodec', c_uint),
        ('width', c_int),
        ('height', c_int),
        ('acodec', c_uint),
        ('samplerate', c_int),
        ('channel', c_int),
    )

# create/ destroy handler
FFI.fr_create_ex.argtypes = [POINTER(LogCtx)]
FFI.fr_create_ex.restype = c_void_p
FFI.fr_create.argtypes = []
FFI.fr_create.restype = c_void_p
FFI.fr_destroy.argtypes = [c_void_p]
FFI.fr_destroy.restype = c_int

# trans reader (get video)
FFI.fr_open_reader.argtypes = [c_void_p, POINTER(ReaderParam), POINTER(MediaParam)]
FFI.fr_open_reader.restype = c_int
FFI.fr_close_reader.argtypes = [c_void_p]
FFI.fr_close_reader.restype = c_int
FFI.fr_readvideo.argtypes = [c_void_p, POINTER(PyVideo)]
FFI.fr_readvideo.restype = c_int
FFI.fr_popvideo.argtypes = [c_void_p, POINTER(PyVideo)]
FFI.fr_popvideo.restype = c_int

FFI.fr_convvideo.argtypes = [c_void_p, POINTER(PyVideo), POINTER(PyVideo)]
FFI.fr_convvideo.restype = c_int

FFI.fr_readaudio.argtypes = [c_void_p, POINTER(PyAudio)]
FFI.fr_readaudio.restype = c_int
FFI.fr_popaudio.argtypes = [c_void_p, POINTER(PyAudio)]
FFI.fr_popaudio.restype = c_int

# trans writer (file write, stream send)
FFI.fr_open_writer.argtypes = [c_void_p, POINTER(WriterParam)]
FFI.fr_open_writer.restype = c_int
FFI.fr_close_writer.argtypes = [c_void_p]
FFI.fr_close_writer.restype = c_int
FFI.fr_writevideo.argtypes = [c_void_p, POINTER(PyVideo)]
FFI.fr_writevideo.restype = c_int
FFI.fr_writeaudio.argtypes = [c_void_p, POINTER(PyAudio)]
FFI.fr_writeaudio.restype = c_int


# inParam = ReaderParam()
# outParam = WriterParam()
# outVideo = PyVideo()
# trans = FFI.fr_create()
# print("fr_create() success...")

LEVEL_FATAL = 1
LEVEL_ERROR	= 2
LEVEL_WARN = 3
LEVEL_INFO = 4
LEVEL_DEBUG = 5
LEVEL_TRACE = 6
# ─────────────────────────────────────────────────────────────────────────────######


class Trans4d:
    def __init__(self, ctxid: str):
        self.inParam = ReaderParam()
        self.outParam = WriterParam()
        self.outVideo = PyVideo()
        self.outAudio = PyAudio()
        self.wrVideo = PyVideo()
        self.mediaInfo = MediaParam()
        # self.trans = FFI.fr_create()
        self.log_ctx = LogCtx()
        log_name = "./log/trans4d_" + ctxid + ".log"
        self.log_ctx.name = c_char_p(log_name.encode('utf-8')) 
        self.log_ctx.level = LEVEL_DEBUG
        self.log_ctx.rollSize = 100 * 1024 * 1024
        self.log_ctx.rollCount = 10
        self.trans = FFI.fr_create_ex(self.log_ctx)

        self.audio_tick = 0
        self.video_tick = 0

        logger.info("fr_create_ex() success...")

        self.writer = False


    # def __del__(self):
    #     FFI.fr_destroy(self.trans)
    #     logger.info("fr_destroy() success...")


    def prepare_writer(self, enc_param):
        logger.info("trans prepare_writer() begin..path: {}".format(enc_param.path))
        logger.info("trans prepare_writer() w: {}, h: {}, reframe: {}".format(enc_param.width, enc_param.height, enc_param.fps))

        try:
            self.outParam.url = c_char_p(enc_param.path.encode('utf-8'))
            self.outParam.width = enc_param.width
            self.outParam.height = enc_param.height
            self.outParam.fps = enc_param.fps
            self.outParam.gop = enc_param.gop
            self.outParam.bps = enc_param.bps   # mbps
            self.outParam.use_gpu = False #True    #False
            ret = FFI.fr_open_writer(self.trans, self.outParam)
            if ret < 0:
                logger.error("fr_open_writer() error: {}".format(ret))
                return ret

            self.writer = True
            logger.info("trans prepare_writer() end success...writer: {}".format(self.writer))
        except Exception as e:
            logger.error(f"exception prepare_writer(): {e}")
            return cst.ERR_FAIL
        
        return cst.ERR_SUCCESS


    def prepare_reader(self, path, resize_w=0, resize_h=0):
        logger.info("trans prepare_reader begin path:{}".format(path))
        try:
            self.audio_tick = 0
            self.video_tick = 0
            self.path = path
            self.inParam.url = c_char_p(self.path.encode('utf-8'))
            self.inParam.reWidth = resize_w
            self.inParam.reHeight = resize_h
            # if "rtsp://" in path:
            #     self.inParam.readerType = cst.REMOTE_BUFFER_MODE # 1: buffer mode, 2: frame mode
            # else:
            #     self.inParam.readerType = cst.LOCAL_FILE_MODE # 1: buffer mode, 2: frame mode
            # self.inParam.readerType = cst.LOCAL_FILE_MODE # 1: buffer mode, 2: frame mode
            self.inParam.readerType = cst.AUDIO_ONLY_MODE # 1: buffer mode, 2: frame mode
            self.inParam.reframe = 0
            self.inParam.outFmt = 16 #1 # yuv (16: BGR24)
            ret = FFI.fr_open_reader(self.trans, self.inParam, self.mediaInfo)
            if ret < 0:
                logger.error("fr_open_reader() error: {}".format(ret))
                return ret, None
                # sys.exit("exit..")

            logger.info("prepare_reader() vcode:{}, width:{}, height:{}".format(self.mediaInfo.vcodec, self.mediaInfo.width, self.mediaInfo.height))
            logger.info("prepare_reader() acode:{}, samplerate:{}, channel:{}".format(self.mediaInfo.acodec, self.mediaInfo.samplerate, self.mediaInfo.channel))
            logger.info("prepare_reader() success..reader type: {}.".format(self.inParam.readerType))

        except Exception as e:
            logger.error(f"exception prepare_reader():{e}")
            return cst.ERR_FAIL, None

        return cst.ERR_SUCCESS, self.mediaInfo


    def prepare_reader_ex(self, path, mode, resize_w=0, resize_h=0):
        logger.info("trans prepare_reader begin path:{}".format(path))
        try:
            self.audio_tick = 0
            self.video_tick = 0
            self.path = path
            self.inParam.url = c_char_p(self.path.encode('utf-8'))
            self.inParam.reWidth = resize_w
            self.inParam.reHeight = resize_h
            self.inParam.readerType = mode
            self.inParam.reframe = 0
            self.inParam.outFmt = 16 #1 # yuv (16: BGR24)
            ret = FFI.fr_open_reader(self.trans, self.inParam, self.mediaInfo)
            if ret < 0:
                logger.error("fr_open_reader() error: {}".format(ret))
                return ret, None
                # sys.exit("exit..")

            logger.info("prepare_reader() vcode:{}, width:{}, height:{}".format(self.mediaInfo.vcodec, self.mediaInfo.width, self.mediaInfo.height))
            logger.info("prepare_reader() acode:{}, samplerate:{}, channel:{}".format(self.mediaInfo.acodec, self.mediaInfo.samplerate, self.mediaInfo.channel))
            logger.info("prepare_reader() success..reader type: {}.".format(self.inParam.readerType))

        except Exception as e:
            logger.error(f"exception prepare_reader():{e}")
            return cst.ERR_FAIL, None

        return cst.ERR_SUCCESS, self.mediaInfo

    def run(self):
        dmp_out = open("trans_out.raw", "wb")

        while True:
            ret = FFI.fr_readvideo(self.trans, self.outVideo)
            # print("fr_readvideo ret...{}".format(ret))
            if ret == -15:  # again
                continue
            elif ret != 0:
                break
            else:
                logger.debug('dec after ret: {0}, w:{1}, h:{2}, dst_idx:{3}, time:{4}'.format(ret, self.outVideo.width, self.outVideo.height, self.outVideo.idx, self.outVideo.pts))

                # conv data shape
                tmp = cast(self.outVideo.data, POINTER(c_byte*3*self.outVideo.width*self.outVideo.height)).contents
                frame = np.frombuffer(tmp, dtype=np.uint8)
                frame = frame.reshape(self.outVideo.height, self.outVideo.width, 3)

                # ─────────────────────────────────────────────────────────────────────────────##
                # wrVideo.data = online_im.ctypes.data_as(ctypes.POINTER(c_char))
                # wrVideo.width = outVideo.width
                # wrVideo.height = outVideo.height
                # wrVideo.pitch = outVideo.pitch
                # wrVideo.pts = outVideo.pts
                # wrVideo.idx = outVideo.idx
                # FFI.fr_writeframe(trans, wrVideo)
                # FFI.fr_writeframe(trans, online_im)
                # ─────────────────────────────────────────────────────────────────────────────##

        dmp_out.close()

    def read_audio(self):
        # if self.inParam.readerType == cst.REMOTE_BUFFER_MODE:
        #     ret = FFI.fr_popaudio(self.trans, self.outAudio)
        # else:
        #     ret = FFI.fr_readaudio(self.trans, self.outAudio)

        tick = time.time()
        if self.audio_tick > 0:
            call_tick = (tick - self.audio_tick)*1000
        self.audio_tick = tick 

        ret = FFI.fr_readaudio(self.trans, self.outAudio)
        if ret != cst.ERR_SUCCESS:
            return ret, None

        # logger.info('read_audio() pts:{}, length:{}, call_tick: {:.1f}'.format(self.outAudio.pts, self.outAudio.length, call_tick))

        # conv data shape
        tmp = cast(self.outAudio.data, POINTER(c_byte*self.outAudio.length)).contents
        frame = np.frombuffer(tmp, dtype=np.uint8)
        # frame = frame.reshape(self.outAudio.length, 1, 1)     # 3 dimension
        # print(frame.shape)

        frame_data = cst.AudioFrame(frame, self.outAudio.pts, self.outAudio.length, self.outAudio.wrap)

        return ret, frame_data

    def read_video(self):
        tick = time.time()
        if self.video_tick > 0:
            call_tick = (tick - self.video_tick)*1000
            # logger.debug(f'read_video() call_tick: {call_tick:.1f}')
        self.video_tick = tick 

        # const int ERR_AGAIN = -15;
        ret = FFI.fr_readvideo(self.trans, self.outVideo)
        if ret != cst.ERR_SUCCESS:
            # logger.error(f'read_video() error: {ret}')
            return ret, None

        # logger.info('read_video() w:{}, h:{}, dst_idx:{}, pts:{}, call_tick: {:.1f}'.format(self.outVideo.width, self.outVideo.height, self.outVideo.idx, self.outVideo.pts, call_tick))

        # conv data shape
        if self.inParam.outFmt == 1:
            tmp = cast(self.outVideo.data, POINTER(c_byte*int(1.5*self.outVideo.width*self.outVideo.height))).contents
            frame = np.frombuffer(tmp, dtype=np.uint8)

            # frame = self.outVideo.y
            self.outVideo.fmt = 1
        else:
            tmp = cast(self.outVideo.data, POINTER(c_byte*3*self.outVideo.width*self.outVideo.height)).contents
            frame = np.frombuffer(tmp, dtype=np.uint8)
            frame = frame.reshape(self.outVideo.height, self.outVideo.width, 3)
            self.outVideo.fmt = 16

        frame_data = cst.FrameInfo(frame, self.outVideo.pts, cst.FRAME_IMAGE, self.outVideo.width, self.outVideo.height, self.outVideo.pitch, self.outVideo.fmt)

        return ret, frame_data

    def conv_video(self, frame_info):
        inVideo = PyVideo()
        inVideo.data = frame_info.frame.ctypes.data_as(ctypes.POINTER(c_char))
        inVideo.width = frame_info.width
        inVideo.height = frame_info.height
        inVideo.pitch = frame_info.pitch
        inVideo.pts = frame_info.pts
        inVideo.idx = frame_info.idx
        if frame_info.fmt == 1:
            inVideo.fmt = 1
        else:
            inVideo.fmt = 16

        outVideo = PyVideo()
        ret = FFI.fr_convvideo(self.trans, inVideo, outVideo)
        if ret != cst.ERR_SUCCESS:
            return ret, None

        tmp = cast(outVideo.data, POINTER(c_byte*3*outVideo.width*outVideo.height)).contents
        frame = np.frombuffer(tmp, dtype=np.uint8)
        frame = frame.reshape(outVideo.height, outVideo.width, 3)
        outVideo.fmt = 16

        frame_data = cst.FrameInfo(frame, outVideo.pts, cst.FRAME_IMAGE, outVideo.width, outVideo.height, outVideo.pitch, outVideo.fmt)

        return ret, frame_data

    def write_video(self, frame_info):
        # logger.debug('write_frame w:{}, h:{}, dst_idx:{}, time:{}, pitch:{}, fmt:{}'.format(frame_info.width, frame_info.height, frame_info.idx, frame_info.pts, frame_info.pitch, frame_info.fmt))
        if self.writer is True:
            self.wrVideo.data = frame_info.frame.ctypes.data_as(ctypes.POINTER(c_char))
            self.wrVideo.width = frame_info.width
            self.wrVideo.height = frame_info.height
            self.wrVideo.pitch = frame_info.pitch
            self.wrVideo.pts = frame_info.pts
            self.wrVideo.idx = frame_info.idx
            if frame_info.fmt == 1:
                self.wrVideo.fmt = 1
            else:
                self.wrVideo.fmt = 16

            # logger.info('write_frame w:{}, h:{}, dst_idx:{}, time:{}, pitch:{}'.format(frame_info.width, frame_info.height, frame_info.idx, frame_info.pts, frame_info.pitch))
            FFI.fr_writevideo(self.trans, self.wrVideo)

    def write_audio(self, frame_info):
        if self.writer is True:
            self.wrAudio.data = frame_info.frame.ctypes.data_as(ctypes.POINTER(c_char))
            self.wrAudio.pts = frame_info.pts

            FFI.fr_writeaudio(self.trans, self.wrAudio)



    def pop_bufferframe(self):
        ret = FFI.fr_popframe(self.trans, self.outVideo)
        if ret != 0:
            return False, None

        logger.info('popframe dec after ret: {0}, w:{1}, h:{2}, dst_idx:{3}, time:{4}'.format(ret, self.outVideo.width, self.outVideo.height, self.outVideo.idx, self.outVideo.pts))

        # conv data shape
        tmp = cast(self.outVideo.data, POINTER(c_byte*3*self.outVideo.width*self.outVideo.height)).contents
        frame = np.frombuffer(tmp, dtype=np.uint8)
        frame = frame.reshape(self.outVideo.height, self.outVideo.width, 3)

        return True, frame



    def destroy_writer(self):
        logger.info("destroy_writer() begin...")
        if self.writer is True:
            ret = FFI.fr_close_writer(self.trans)
            if ret < 0:
                logger.error("fr_close_writer() error: {}".format(ret))
                return
            logger.info("destroy_writer() end success...")

    def destroy_reader(self):
        logger.info("destroy_reader() begin...")
        ret = FFI.fr_close_reader(self.trans)
        if ret < 0:
            logger.error("fr_close_reader() error: {}".format(ret))
            return
        logger.info("destroy_reader() end success...")





if __name__ == "__main__":

    trans = Trans4d()
    # trans.prepare("golf_LET.mp4", 1280, 720)
    trans.prepare("f51.mp4", 1280, 720, False)
    # trans.run()
    cnt = 0
    while True:
        if cnt > 200:       # total range를 구해서 종료할 수 있도록 수정..
            break
        ret, frame_info = trans.read_frame()
        if ret == cst.ERR_SUCCESS:
            print("read_frame, ret:{}, len:{}, type:{}".format(ret, len(frame_info.frame), type(frame_info.frame))) 
            cnt += 1
        else:
            continue


    trans.destroy()

