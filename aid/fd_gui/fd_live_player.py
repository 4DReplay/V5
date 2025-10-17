# ─────────────────────────────────────────────────────────────────────────────#
# /A/I/D/
# fd_live_player.py
# - 2025/06/06
# - Hongsu Jung
#
# Live Player
# [condition]
# 
#
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import os
import numpy as np
import threading
import win32gui
import win32con
import subprocess
import shutil
import time

import fd_utils.fd_config as conf
from fd_utils.fd_logging        import fd_log

from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtCore import QTimer, Qt, QSettings
from PyQt5.QtOpenGL import QGLWidget

from OpenGL.GL import *

# ─────────────────────────────────────────────────────────────────────────────##pull 
# class LivePlayerWindow(QMainWindow):
# owner : hongsu jung
# date : 2025-06-17
# ─────────────────────────────────────────────────────────────────────────────#
class LivePlayerWindow(QMainWindow):
    def __init__(self, conf, frame_background, fps=30):
        super().__init__()
        self.conf = conf
        self.frame_background = frame_background
        self.fps = fps
        self.is_fullscreen = False
        self._last_geometry = None
        self.settings = QSettings("4DReplay", "LivePlayer")

        window_name = conf._live_player_window_name
        self.setWindowTitle(window_name)

        # 위젯 설정
        if conf._game_type == conf._game_type_nascar:
            widget = MultiChannelWidget(conf, frame_background, 1.0 / fps)
        else:
            widget = SingleChannelWidget(conf, frame_background, 1.0 / fps)

        self.setCentralWidget(widget)
        conf._live_player_widget = widget
        self.resize(1280, 720)        

        # 저장된 위치/모드 복원
        self.restore_window_geometry()

        # 최상위 설정은 이벤트 큐에 등록
        QTimer.singleShot(200, self.set_topmost)

    def set_topmost(self):
        hwnd = win32gui.FindWindow(None, self.windowTitle())
        if hwnd:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                                  
    def force_topmost(self):
        hwnd = win32gui.FindWindow(None, self.windowTitle())
        if hwnd:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE)

    def mousePressEvent(self, event):
        if self.is_fullscreen:
            fd_log.info("🖱️ Clicked — focus activated.")
            self.setFocus(Qt.MouseFocusReason)

    def keyPressEvent(self, event):
        if self.is_fullscreen and event.key() in [Qt.Key_Escape, Qt.Key_Enter, Qt.Key_Return]:
            fd_log.info("🔓 ESC/Enter pressed — exiting fullscreen.")
            self.toggle_fullscreen()
        elif not self.is_fullscreen and event.key() in [Qt.Key_Enter, Qt.Key_Return]:
            self.toggle_fullscreen()
        elif event.key() == Qt.Key_Q:
            fd_log.info("🛑 Quit pressed.")
            self.close()
        elif event.key() == Qt.Key_P:
            with self.conf._live_player_lock:
                self.conf._live_is_paused = not self.conf._live_is_paused
                fd_log.info(f"⏯️ Playback toggled → {'Paused' if self.conf._live_is_paused else 'Playing'}")
            if not self.conf._live_is_paused:
                self.update()

    def save_window_geometry(self):
        # 창 위치 및 크기 저장
        if not self.isFullScreen():
            geometry = self.geometry()
            self.settings.setValue("window/x", geometry.x())
            self.settings.setValue("window/y", geometry.y())
            self.settings.setValue("window/width", geometry.width())
            self.settings.setValue("window/height", geometry.height())
        else:
            screen = QApplication.screenAt(self.pos())
            if screen:
                screen_index = QApplication.screens().index(screen)
                self.settings.setValue("window/fullscreen_screen", screen_index)

        # 전체화면 여부 저장
        self.settings.setValue("window/is_fullscreen", self.is_fullscreen)
        self.settings.sync()

    def restore_window_geometry(self):
        is_fullscreen = self.settings.value("window/is_fullscreen", False, type=bool)

        if is_fullscreen:
            screen_index = self.settings.value("window/fullscreen_screen", 0, type=int)
            screens = QApplication.screens()
            if 0 <= screen_index < len(screens):
                geo = screens[screen_index].geometry()
                self.move(geo.topLeft())
                fd_log.info(f"🖥️ Restoring fullscreen on screen {screen_index} at {geo.topLeft()}")
            QTimer.singleShot(300, self.toggle_fullscreen)
        else:
            # 일반 창 모드 복원
            x = self.settings.value("window/x", type=int)
            y = self.settings.value("window/y", type=int)
            w = self.settings.value("window/width", type=int)
            h = self.settings.value("window/height", type=int)
            if all(v is not None for v in [x, y, w, h]):
                self.setGeometry(x, y, w, h)
                fd_log.info(f"📐 Restored window geometry: ({x}, {y}, {w}, {h})")

    def toggle_fullscreen(self):
        if self.is_fullscreen:
            # 🔓 전체화면 해제
            QApplication.restoreOverrideCursor()
            self.setWindowFlags(Qt.Widget)
            self.showNormal()
            self.show()

            if self._last_geometry:
                self.setGeometry(self._last_geometry)
                fd_log.info(f"📐 Restored window geometry: {self._last_geometry}")
            else:
                fd_log.info("📐 No previous geometry to restore.")

            # 저장: 전체화면 해제됨
            self.settings.setValue("window/is_fullscreen", False)
            fd_log.info("🔳 Switched to windowed mode.")
        else:
            # 🔒 전체화면 전환
            self._last_geometry = self.geometry()
            # QApplication.setOverrideCursor(Qt.BlankCursor)
            self.setCursor(Qt.BlankCursor)  # 마우스 숨김

            self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
            self.showFullScreen()
            self.force_topmost()
            self.setFocusPolicy(Qt.StrongFocus)
            self.setFocus()

            # 현재 화면 인덱스 저장
            screen = QApplication.screenAt(self.pos())
            if screen:
                screen_index = QApplication.screens().index(screen)
                self.settings.setValue("window/fullscreen_screen", screen_index)

            # 저장: 전체화면 상태임
            self.settings.setValue("window/is_fullscreen", True)
            fd_log.info("🖥️ Switched to fullscreen exclusive mode.")

        self.settings.sync()
        self.is_fullscreen = not self.is_fullscreen

    def closeEvent(self, event):
        self.save_window_geometry()
        QApplication.restoreOverrideCursor()
        event.accept()

# ─────────────────────────────────────────────────────────────────────────────##
# class MultiChannelWidget(QGLWidget):
# owner : hongsu jung
# date : 2025-06-17
# ─────────────────────────────────────────────────────────────────────────────#
class MultiChannelWidget(QGLWidget):
    def __init__(self, conf, frame_background, frame_interval):
        super().__init__()
        self.conf = conf
        self.frame_background = frame_background
        self.frame_interval = frame_interval
        self.textures = None
        self.frames = [None] * 4
        self.timer = QTimer()
        self.timer.timeout.connect(self.update)
        self.timer.start(int(frame_interval * 1000))

        # 반복 재생 모드 전용
        self.playback_mode = False
        self.playback_video = None
        self.playback_total_frames = 0
        self.playback_frame_index = 0

        self.last_frames_ref = None
        self.last_saved_frames_ref = None
        self.wait_after_new_frame = False
        self.new_frame_time = 0        


    def initializeGL(self):
        glEnable(GL_TEXTURE_2D)
        self.textures = glGenTextures(4)

    def resizeGL(self, w, h):
        glViewport(0, 0, w, h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, w, h, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)

    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT)

        # ▶️ 반복 재생 모드
        if self.playback_mode and self.playback_video is not None:
            self.playback_video.set(cv2.CAP_PROP_POS_FRAMES, self.playback_frame_index)
            ret, frame = self.playback_video.read()
            if not ret:
                self.playback_video.set(cv2.CAP_PROP_POS_FRAMES, 0)
                self.playback_frame_index = 0
                ret, frame = self.playback_video.read()
                if not ret:
                    fd_log.warning("⚠️ 반복 재생 실패: 프레임 읽기 오류")
                    return

            resized = cv2.resize(frame, (1920, 1080))
            rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
            self._update_texture(self.textures[0], rgb)
            self._draw_single_texture(self.textures[0])
            self.playback_frame_index = (self.playback_frame_index + 1) % self.playback_total_frames
            return

        # 🔒 프레임 접근
        with self.conf._live_player_lock:
            frames = self.conf._live_player_multi_ch_frames
            paused = self.conf._live_is_paused

        # 👇 새로운 영상이 들어오면 인덱스 리셋
        if frames is not None and frames is not self.last_frames_ref:
            fd_log.info("🔄 new buffer frames")
            self.last_frames_ref = frames
            self.playback_frame_index = 0
            self.wait_after_new_frame = True
            self.new_frame_time = time.time()

            self._start_temp_video_save(frames)
            self.last_saved_frames_ref = frames

        if paused or frames is None:
            self._draw_waiting()
            return

        if all(len(v) > 0 for v in frames.values()):
            keys = list(frames.keys())
            min_len = min(len(v) for v in frames.values())

            if self.playback_frame_index >= min_len:
                self.playback_frame_index = 0  # 반복 재생

            order = [keys[0], keys[3], keys[1], keys[2]]
            for i, k in enumerate(order):
                img = frames[k][self.playback_frame_index]
                resized = cv2.resize(img, (960, 540), interpolation=cv2.INTER_CUBIC)
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                self._update_texture(self.textures[i], rgb)

            self._draw_textures()
            self.playback_frame_index += 1  # ✅ 명확히 1프레임 증가
        else:
            self._draw_waiting()

    """외부에서 호출해서 재생 모드를 시작할 수 있도록 지원."""
    def set_playback_video(self, filepath):

        if self.playback_video is not None:
            self.playback_video.release()

        self.playback_video = cv2.VideoCapture(filepath, cv2.CAP_FFMPEG)
        if not self.playback_video.isOpened():
            fd_log.error(f"❌ VideoCapture 열기 실패: {filepath}")
            return

        self.playback_total_frames = int(self.playback_video.get(cv2.CAP_PROP_FRAME_COUNT))
        if self.playback_total_frames == 0:
            fd_log.error("❌ 반복 재생 실패: 프레임 수 0")
            return

        self.playback_frame_index = 0
        self.playback_mode = True
        fd_log.info(f"🔁 반복 재생 시작: {filepath}")

    def _draw_waiting(self):
        # 현재 화면 크기에 맞춰 frame_background 리사이즈
        resized = cv2.resize(self.frame_background, (self.width(), self.height()))        
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)        
        self._update_texture(self.texture, rgb)
        self._draw_texture(self.texture, 0, 0, self.width(), self.height())

    def _update_texture(self, tex_id, image_rgb):
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, image_rgb.shape[1], image_rgb.shape[0], 0, GL_RGB, GL_UNSIGNED_BYTE, image_rgb)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

    def _draw_textures(self):
        for i in range(4):
            x = (i % 2) * 960
            y = (i // 2) * 540
            self._draw_texture(self.textures[i], x, y, 960, 540)

    def _draw_single_texture(self, tex_id):
        self._draw_texture(tex_id, 0, 0, 1920, 1080)

    def _draw_texture(self, tex_id, x, y, w, h):
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex2f(x, y)
        glTexCoord2f(1, 0); glVertex2f(x + w, y)
        glTexCoord2f(1, 1); glVertex2f(x + w, y + h)
        glTexCoord2f(0, 1); glVertex2f(x, y + h)
        glEnd()

    def _start_temp_video_save(self, frames):
        fps = int(1.0 / self.frame_interval)
        conf = self.conf
        thread = threading.Thread(target=self._multi_ch_video_create, args=(frames, fps,), daemon=True)
        conf._live_creating_output_file = thread
        thread.start()      

    def _multi_ch_video_create(frames, fps):        
        # 1. 프레임 유효성 검사
        if not all(len(v) > 0 for v in frames.values()):
            fd_log.error("❌ 저장 실패: 유효한 프레임 없음")
            return

        keys = list(frames.keys())
        min_len = min(len(v) for v in frames.values())
        order = [keys[0], keys[3], keys[1], keys[2]]

        # 2. 모자이크 프레임 생성
        temp_video = []
        for idx in range(min_len):
            canvas = np.zeros((1080, 1920, 3), dtype=np.uint8)
            for i, k in enumerate(order):
                img = frames[k][idx]
                if img is None or not isinstance(img, np.ndarray):
                    fd_log.warning(f"⚠️ frame[{idx}][{k}] is invalid")
                    continue
                resized = cv2.resize(img, (960, 540), interpolation=cv2.INTER_CUBIC)
                canvas[(i // 2) * 540 : (i // 2 + 1) * 540, (i % 2) * 960 : (i % 2 + 1) * 960, :] = resized
            temp_video.append(canvas)

        # 3. 임시 저장 (OpenCV)
        os.makedirs("R://temp", exist_ok=True)
        temp_output = os.path.join("R://temp", f"temp_video_{int(time.time())}.mp4")

        writer = cv2.VideoWriter(
            temp_output,
            cv2.VideoWriter_fourcc(*'mp4v'),
            fps,
            (1920, 1080)
        )

        for i, f in enumerate(temp_video):
            if f is not None and isinstance(f, np.ndarray):
                writer.write(f)
            else:
                fd_log.warning(f"⚠️ 저장 제외된 프레임: {i}")

        writer.release()
        fd_log.info(f"📝 Temp file: {temp_output}")

        # 4. GOP=1 재인코딩 (FFmpeg)
        gop_output = temp_output.replace(".mp4", "_gop1.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-i", temp_output,
            "-c:v", "copy",
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-preset", "ultrafast",
            "-b:v", "70M",
            "-x264-params", "keyint=1",
            "-pix_fmt", "yuv420p",
            gop_output
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        fd_log.info(f"🎞️ GOP 1 encoding: {gop_output}")

        # 5. 최종 파일 저장 경로
        team_id = conf._team_info
        # 2025-07-26
        output_dir = conf._folder_output.replace("/", "\\")
        start_time = conf._create_file_time_start
        #formatted_time = time.strftime("%Y%m%d%H%M%S", time.localtime(start_time))
        formatted_time = "TEMP_TIME"
        os.makedirs(output_dir, exist_ok=True)
        final_output_path = os.path.join(output_dir, f"shorts_{formatted_time}_{team_id}_multi_4.mp4")
        final_output_path = final_output_path.replace('/', '\\')
        conf._final_output_file = final_output_path

        # 6. 복사 및 정리
        fd_log.info(f"📁 Copy file: {gop_output} → {final_output_path}")
        if os.path.exists(final_output_path):
            os.remove(final_output_path)
        shutil.copy2(gop_output, final_output_path)
        os.remove(gop_output)
        os.remove(temp_output)
        fd_log.info(f"✅ Final video: {final_output_path}")

# ─────────────────────────────────────────────────────────────────────────────##
# class SingleChannelWidget(QGLWidget):
# owner : hongsu jung
# date : 2025-07-03
# ─────────────────────────────────────────────────────────────────────────────##
class SingleChannelWidget(QGLWidget):
    def __init__(self, conf, frame_background, frame_interval):
        super().__init__()
        self.conf = conf
        self.frame_background   = frame_background
        self.frame_interval     = frame_interval
        self.texture = None
        self.frame = None
        self.last_valid_rgb = None  # 🔸 마지막 유효 프레임 저장용
        self.timer = QTimer()
        self.timer.timeout.connect(self.update)
        self.timer.start(int(frame_interval * 1000))
        self.loop_count_current = 0
        self.loop_count_target = conf._live_player_loop_count
        self.playback_index = 0
        

    def initializeGL(self):
        glEnable(GL_TEXTURE_2D)
        self.texture = glGenTextures(1)

    def resizeGL(self, w, h):
        glViewport(0, 0, w, h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        glOrtho(0, w, h, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)

    def paintGL(self):
        glClear(GL_COLOR_BUFFER_BIT)

        if self.conf._live_is_paused:
            self._draw_waiting()
            return

        with self.conf._live_player_lock:
            shared_frames = self.conf._live_player_buffer_manager.get_active_buf()
            total_frames = len(shared_frames)

        if not isinstance(shared_frames, list) or total_frames == 0:
            self._draw_waiting()
            return

        elapsed = time.perf_counter() - self.conf._time_play
        expected_index = int(elapsed * self.conf._output_fps)

        # ▶️ 반복 조건 체크
        with self.conf._live_player_lock:
            if expected_index >= total_frames:
                self.loop_count_current += 1
                # 최소 1회 수행이 마친다음, 영상저장
                if self.loop_count_current == 1:
                    save_buffer, is_switch = self.conf._live_player_buffer_manager.switch_buffer()
                    if save_buffer:
                        self.conf._live_player_buffer_manager.save_file(save_buffer)
                    if is_switch:
                        self.playback_index = 0
                        self.conf._live_is_paused = False
                        self.conf._time_live_play = time.perf_counter()
                        self.conf._time_play = time.perf_counter()
                        self.loop_count_current = 0    
                    
                # loopping option
                if self.loop_count_target == -1 or self.loop_count_current < self.loop_count_target:
                    # ⏪ 반복 계속
                    self.playback_index = 0
                    self.conf._time_play = time.perf_counter()
                    expected_index = 0
                    fd_log.info(f"🔁 Live Player Looping : {self.loop_count_current + 1}/{self.loop_count_target if self.loop_count_target != -1 else '∞'}")
                # finish looping
                else:
                    # ⏹️ 반복 끝
                    self.conf._live_is_paused = True
                    fd_log.info("⏹️ Playback completed.")
                    return
                
                shared_frames = self.conf._live_player_buffer_manager.get_active_buf()
                total_frames = len(shared_frames)

        self.playback_index = expected_index
        img = shared_frames[self.playback_index]

        # 프레임 유효성 검사
        is_valid = (
            img is not None and isinstance(img, np.ndarray)
            and img.ndim == 3 and img.shape[0] > 0 and img.shape[1] > 0
        )

        if not is_valid:
            fd_log.warning("⚠️ Invalid image — using last valid frame")
            if self.last_valid_rgb is not None:
                self._update_texture(self.texture, self.last_valid_rgb)
                self._draw_texture(self.texture, 0, 0,
                                self.last_valid_rgb.shape[1], self.last_valid_rgb.shape[0])
                glFlush()
                glFinish()
            else:
                self._draw_waiting()
            return
        else:
            self.conf._live_playing_progress = int(expected_index/total_frames*100)

        try:
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            self.last_valid_rgb = rgb.copy()
        except Exception as e:
            fd_log.error(f"❌ Color conversion error: {e}")
            self._draw_waiting()
            return

        self._update_texture(self.texture, rgb)
        self._draw_texture(self.texture, 0, 0, img.shape[1], img.shape[0])
        glFlush()
        glFinish()

    def live_player_restart(self):
        with self.conf._live_player_lock:
            if self.conf._live_is_paused:
                self.playback_index = 0
                self.conf._live_is_paused = False
                self.conf._time_live_play = time.perf_counter()
                self.conf._time_play = time.perf_counter()
                self.loop_count_current = 0                
                fd_log.info(f"✅ [Live Player] start")
                self.update()
            else:
                # check switch
                if self.conf._live_player_buffer_manager.is_need_switch:
                    self.conf._live_player_buffer_manager.is_ready_switch = True
                    # over 1 time play -> switch
                    if self.loop_count_current >= 1:
                        save_buffer = self.conf._live_player_buffer_manager.switch_buffer()
                        if save_buffer:
                            self.conf._live_player_buffer_manager.save_file(save_buffer)
                            self.playback_index = 0
                            self.conf._live_is_paused = False
                            self.conf._time_live_play = time.perf_counter()
                            self.conf._time_play = time.perf_counter()
                            self.loop_count_current = 0    
                        fd_log.info(f"✅ [Live Player] start")
                        self.update()


                fd_log.info(f"✅ [Live Player] already playing")


    def load_video_to_buffer(self, file_path):
        # save option
        thread = threading.Thread(target=self._load_video_to_buffer, args=(file_path, ), daemon=True)
        thread.start()

    def _load_video_to_buffer(self, file_path):
        cap = cv2.VideoCapture(file_path)
        if not cap.isOpened():
            fd_log.error("❌ Cannot open video file.")
            return

        frames = []
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            frames.append(frame)
        cap.release()

        if len(frames) == 0:
            fd_log.error("❌ No frames extracted.")
            return

        self.conf._live_player_buffer_manager.load_frames_to_buffer(frames)
        self.live_player_restart()
        fd_log.info(f"🎞️ {len(frames)} frames loaded into double buffer.")

    def _draw_waiting(self):
        if self.frame_background is None:
            glClearColor(0.1, 0.1, 0.1, 1.0)
            glClear(GL_COLOR_BUFFER_BIT)
            return

        try:
            rgb = cv2.cvtColor(self.frame_background, cv2.COLOR_BGR2RGB)
            self._update_texture(self.texture, rgb)
            self._draw_texture(self.texture, 0, 0, self.frame_background.shape[1], self.frame_background.shape[0])
        except Exception as e:
            fd_log.info(f"Draw waiting error: {e}")

    def _update_texture(self, tex_id, image_rgb):
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB,
                     image_rgb.shape[1], image_rgb.shape[0],
                     0, GL_RGB, GL_UNSIGNED_BYTE, image_rgb)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

    def _draw_texture(self, tex_id, x, y, w, h):
        glBindTexture(GL_TEXTURE_2D, tex_id)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex2f(x, y)
        glTexCoord2f(1, 0); glVertex2f(x + w, y)
        glTexCoord2f(1, 1); glVertex2f(x + w, y + h)
        glTexCoord2f(0, 1); glVertex2f(x, y + h)
        glEnd()

# ─────────────────────────────────────────────────────────────────────────────##
# class LiveBufferManager:
# owner : hongsu jung
# date : 2025-07-09
# ─────────────────────────────────────────────────────────────────────────────#
class LiveBufferManager:
    def __init__(self, conf):
        self.conf = conf
        self.buffer_a = []
        self.buffer_b = []
        self.active_index = True       # (True)1 -> buffer_a, (False)0 -> buffer_b
        self.is_saved = True
        self.is_need_switch = False
        self.is_ready_switch = False
        self.file_output_ready = ""
        self.file_output_stanby = ""
        self.lock = threading.Lock()
        self.buffer_ready_event = threading.Event()

    def init_frame(self, size):
        with self.lock:
            is_playing = not self.conf._live_is_paused
            if not is_playing:
                self.is_need_switch = False
                if self.active_index:   self.buffer_a = [None] * size
                else:                   self.buffer_b = [None] * size
                return self.active_index
            else:
                self.is_need_switch = True
                self.is_ready_switch = False
                if self.active_index:   self.buffer_b = [None] * size
                else:                   self.buffer_a = [None] * size
                return not self.active_index

    def set_frame(self, buffer_index, frame_index, frame):
        with self.lock:
            if buffer_index: # True -> 1 : buffer_a, False -> 0 : buffer_b
                self.buffer_a[frame_index] = frame
            else:
                self.buffer_b[frame_index] = frame

    def load_frames_to_buffer(self, frames: list):
        frame_cnt = len(frames)
        buffer_index = self.init_frame(frame_cnt)
        if buffer_index: # True -> 1 : buffer_a, False -> 0 : buffer_b
            self.buffer_a = frames.copy()
        else:
            self.buffer_b = frames.copy()

    def get_active_buf(self):
        with self.lock:
            if self.active_index:   return self.buffer_a
            else:                   return self.buffer_b
        
    def switch_buffer(self):
        with self.lock:
            frames = []
            is_switch_buffer = False
            if self.is_ready_switch:
                is_switch_buffer = True
                self.is_ready_switch = False
                if self.active_index:
                    self.active_index = False   # change to buffer_b
                    frames = [f.copy() for f in self.buffer_a]
                else:                   
                    self.active_index =  True   # change to buffer_a
                    frames = [f.copy() for f in self.buffer_b]
            else:
                if self.active_index:
                    frames = [f.copy() for f in self.buffer_a]
                else:                   
                    frames = [f.copy() for f in self.buffer_b]
            return frames, is_switch_buffer

    def set_save_file_name(self, file_output):
        if self.is_saved:
            self.is_saved = False
            self.file_output_ready = file_output
            self.file_output_stanby = ""
        else : 
            self.file_output_stanby = file_output

    def save_file(self, save_buffer):
        # save option
        if self.conf._live_player_save_file:
            thread = threading.Thread(target=self._save_file_worker, args=(save_buffer, ), daemon=True)
            thread.start()

    def _save_file_worker(self, save_buffer):
        frames = save_buffer
        file_output = self.file_output_ready

        if not isinstance(frames, list) or len(frames) == 0:
            fd_log.error("❌ No frames to save.")
            return

        height, width = frames[0].shape[:2]
        fps = self.conf._output_fps

        fd_log.info(f"💾 Saving with FFmpeg: {file_output} | {width}x{height} @ {fps}fps")

        ffmpeg_command = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}",
            "-r", str(fps),
            "-i", "-",
            "-c:v", "copy",
            "-preset", str(self.conf._output_preset),
            "-rc", "vbr",
            "-tune", "hq",
            "-multipass", "fullres",
            "-b:v", self.conf._output_bitrate,
            "-profile:v", "high",
            "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov",
            "-f", "mp4",
            file_output
        ]

        process = subprocess.Popen(
            ffmpeg_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=10**8
        )

        try:
            for idx, frame in enumerate(frames):
                if frame is None or frame.size == 0:
                    continue
                process.stdin.write(frame.tobytes())

        except Exception as e:
            fd_log.info(f"\n❌ Error writing frame to FFmpeg: {e}")
        finally:
            try:
                if process.stdin:
                    process.stdin.close()
                process.wait()
            except Exception as e:
                fd_log.warning(f"⚠️ Finalizing FFmpeg failed: {e}")

        fd_log.info(f"\n✅ Save file completed: {self.file_output_ready}")

        # set waiting next file
        if self.file_output_stanby:         
            self.file_output_ready = self.file_output_stanby
            self.file_output_stanby = ""
            self.is_saved = False
            fd_log.info(f"\n✅ Waiting next save file: {self.file_output_ready}")
        else:
            self.is_saved = True