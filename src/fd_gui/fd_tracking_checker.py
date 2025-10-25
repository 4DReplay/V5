# ─────────────────────────────────────────────────────────────────────────────#
#
# fd_tracking_checker.py
# - 2025/07/09
# - Hongsu Jung
#
# ─────────────────────────────────────────────────────────────────────────────##

import cv2
import numpy as np
import time
from datetime import datetime
import win32gui
import win32con

from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log

from PyQt5.QtWidgets import QApplication, QMainWindow, QOpenGLWidget, QDesktopWidget, QStyle
from PyQt5.QtCore import QTimer, Qt, QSettings
from PyQt5.QtWidgets import QOpenGLWidget, QFileDialog

from OpenGL.GL import *

# ─────────────────────────────────────────────────────────────────────────────##
# class TrackingWindow(QMainWindow):
# owner : hongsu jung
# date : 2025-07-09
# ─────────────────────────────────────────────────────────────────────────────##
class TrackingWindow(QMainWindow):
    def __init__(self, conf, frame_background, fps=30):
        super().__init__()
        self.conf = conf
        self.settings = QSettings("4DReplay", "TrackingChecker")

        self.setWindowTitle(conf._hit_tracking_window_name)

        self.widget = TrackingCheckWidget(conf, frame_background, parent=self)
        self.setCentralWidget(self.widget)
        conf._tracking_check_widget = self.widget

        self.setCursor(Qt.ArrowCursor)          # mouse cursor
        self.widget.setCursor(Qt.ArrowCursor)   # mouse cursor

        self.setFocusPolicy(Qt.StrongFocus)     # key event
        self.widget.setFocusPolicy(Qt.StrongFocus)     # key event

        self.restore_window_geometry()
        # 최상위 설정은 이벤트 큐에 등록
        QTimer.singleShot(200, self.set_topmost)

    def set_topmost(self):
        hwnd = win32gui.FindWindow(None, self.windowTitle())
        if hwnd:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            
    def save_window_geometry(self):
        geo = self.geometry()
        self.settings.setValue("window/x", geo.x())
        self.settings.setValue("window/y", geo.y())
        self.settings.setValue("window/width", geo.width())
        self.settings.setValue("window/height", geo.height())
        self.settings.sync()

    def restore_window_geometry(self):
        x = self.settings.value("window/x", 100, type=int)
        y = self.settings.value("window/y", 100, type=int)
        w = self.settings.value("window/width", 1280, type=int)
        h = self.settings.value("window/height", 720, type=int)
        self.setGeometry(x, y, w, h)

    def closeEvent(self, event):
        self.save_window_geometry()
        event.accept()

# ─────────────────────────────────────────────────────────────────────────────##
# class TrackingCheckWidjet(QGLWidget):
# owner : hongsu jung
# date : 2025-07-09
# ─────────────────────────────────────────────────────────────────────────────##
class TrackingCheckWidget(QOpenGLWidget):
    def __init__(self, conf, frame_background, parent=None):
        super().__init__(parent)
        self.conf = conf
        self.frame_background = frame_background
        self.last_shown_frame = None
        self.is_show_last_frame = False
        self.current_frame = None
        self.texture = None
        self.awaiting_click = False
        self.result = None

        self.selected_point = None
        self.setCursor(Qt.ArrowCursor)
        self.setMouseTracking(True)

        self.timer = QTimer()
        self.timer.timeout.connect(self.update)
        self.timer.start(100)

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


        if self.awaiting_click:
            frame = self.current_frame
        else:
            if self.is_show_last_frame:
                frame = self.last_shown_frame
            else:
                frame = self.frame_background
        
        if frame is None:
            glClearColor(0.1, 0.1, 0.1, 1.0)
            glClear(GL_COLOR_BUFFER_BIT)
            return
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            self._update_texture(rgb)
            widget_width = self.width()
            widget_height = self.height()
            self._draw_texture(0, 0, widget_width, widget_height)

        except Exception as e:
            fd_log.error(f"⚠️ draw error: {e}")
        glFlush()
        glFinish()

    def _update_texture(self, image_rgb):
        glBindTexture(GL_TEXTURE_2D, self.texture)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB,
                     image_rgb.shape[1], image_rgb.shape[0],
                     0, GL_RGB, GL_UNSIGNED_BYTE, image_rgb)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

    def _draw_texture(self, x, y, w, h):
        glBindTexture(GL_TEXTURE_2D, self.texture)
        glBegin(GL_QUADS)
        glTexCoord2f(0, 0); glVertex2f(x, y)
        glTexCoord2f(1, 0); glVertex2f(x + w, y)
        glTexCoord2f(1, 1); glVertex2f(x + w, y + h)
        glTexCoord2f(0, 1); glVertex2f(x, y + h)
        glEnd()

    def mousePressEvent(self, event):
        if not self.awaiting_click:
            return

        widget_width = self.width()
        widget_height = self.height()

        # ✅ 윈도우 좌표 → 원본 영상 좌표 변환
        x_ratio = self.video_width / widget_width
        y_ratio = self.video_height / widget_height

        real_x = int(event.x() * x_ratio)
        real_y = int(event.y() * y_ratio)

        if event.button() == Qt.LeftButton:
            self.selected_point = (real_x, real_y)
            self.result = self.conf._mouse_click_left
        elif event.button() == Qt.RightButton:
            self.selected_point = None
            self.result = self.conf._mouse_click_right
        elif event.button() == Qt.MidButton:
            self.selected_point = None
            self.result = self.conf._mouse_click_middle

        self.awaiting_click = False

    def keyPressEvent(self, event):
        if not self.awaiting_click:
            # 항상 동작할 수 있도록 F키는 따로 처리
            if event.key() == Qt.Key_F:
                file_path, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "Video (*.mp4 *.avi *.mov)")
                if file_path:
                    self.conf._live_player_widget.load_video_to_buffer(file_path)   
                return
        if event.key() in [Qt.Key_Return, Qt.Key_Enter]:
            self.result = True
        elif event.key() == Qt.Key_Q:
            self.result = False
        elif event.key() == Qt.Key_F:
            file_path, _ = QFileDialog.getOpenFileName(self, "Open Video", "", "Video (*.mp4 *.avi *.mov)")
            if file_path:
                self.conf._live_player_widget.load_video_to_buffer(file_path)                
            return

        self.awaiting_click = False

    def show_frame(self, frame, is_show_last=True):
        self.awaiting_click = False
        self.is_show_last_frame = is_show_last
        self.current_frame = frame
        self.last_shown_frame = frame.copy()
        # 원본 해상도 저장
        self.video_height, self.video_width = frame.shape[:2]
        self.update()
        QApplication.processEvents()
        
    def show_last_frame(self, is_show_last):
        self.is_show_last_frame = is_show_last

    def show_frame_and_wait(self, frame, is_show_last = False):
        self.current_frame = frame
        self.last_shown_frame = frame.copy()  # ✅ 나중에 유지할 이미지 저장
        self.is_show_last_frame = is_show_last
        self.result = None
        self.selected_point = None
        self.awaiting_click = True

        # ✅ 원본 영상 크기 저장
        self.video_height, self.video_width = frame.shape[:2]

        self.update()
        while self.awaiting_click:
            QApplication.processEvents()
            time.sleep(0.01)
        return self.result