# ─────────────────────────────────────────────────────────────────────────────#
# 
# fd_gui_main.py
#
# - 2025/07/09
# - Hongsu Jung
#
# QT windows managing
#
# ─────────────────────────────────────────────────────────────────────────────#

import cv2
import sys
import os
import numpy as np
import time
import threading
import win32gui
import win32con
import ctypes
import time
import tempfile
import subprocess
import shutil

import fd_utils.fd_config as conf
from fd_utils.fd_logging        import fd_log

from fd_gui.fd_live_player import LivePlayerWindow
from fd_gui.fd_live_player import LiveBufferManager
from fd_gui.fd_dashboard    import DetectDashboard
from fd_gui.fd_tracking_checker import TrackingWindow

from PyQt5.QtWidgets import QApplication, QMainWindow, QOpenGLWidget, QDesktopWidget, QStyle
from PyQt5.QtCore import QTimer, Qt, QSettings, QThread, pyqtSignal, QElapsedTimer
from PyQt5.QtGui import QCursor
from PyQt5.QtOpenGL import QGLWidget

from OpenGL.GL import *

# ─────────────────────────────────────────────────────────────────────────────##
# def fd_start_live_player_thread():
# owner : hongsu jung
# date : 2025-06-06
# ─────────────────────────────────────────────────────────────────────────────#
def fd_start_gui_thread():
    thread = threading.Thread(target=run_gui_viewer, daemon=True)    
    thread.start()
    fd_log.info("✅ Live Player Thread launched.")
    return thread

# ─────────────────────────────────────────────────────────────────────────────##
# def run_gui_viewer(conf, frame_background, frame_interval=1/30):
# owner : hongsu jung
# date : 2025-06-17
# ─────────────────────────────────────────────────────────────────────────────#
def run_gui_viewer():

    app = QApplication(sys.argv)

    # ─────────────────────────────────────────────────────────────────────────────############
    # [GUI] Tracking Checker
    # ─────────────────────────────────────────────────────────────────────────────############
    if conf._tracking_checker:        
        tracker_background = cv2.imread(conf._tracker_backgrond_image, cv2.IMREAD_COLOR)
        window_tw = TrackingWindow(conf, tracker_background)
        window_tw.show()

    # ─────────────────────────────────────────────────────────────────────────────############
    # [GUI] Live Player
    # ─────────────────────────────────────────────────────────────────────────────############
    if conf._live_player:        
        frame_background = cv2.imread(conf._live_backgrond_image, cv2.IMREAD_COLOR)
        window_lp = LivePlayerWindow(conf, frame_background)
        window_lp.show()
        # create live buffer manager
        conf._live_player_buffer_manager = LiveBufferManager(conf)

    # ─────────────────────────────────────────────────────────────────────────────############
    # [GUI] Live Detect
    # ─────────────────────────────────────────────────────────────────────────────############
    if conf._live_detector:
        window_ld = DetectDashboard(conf)
        window_ld.show()
        conf._dashboard = window_ld       
        
    # ─────────────────────────────────────────────────────────────────────────────############
    # [GUI] QT execution
    # ─────────────────────────────────────────────────────────────────────────────############
    try:
        exit_code = app.exec_()
        fd_log.info(f"ℹ️ Qt app exited with code {exit_code}")
        sys.exit(exit_code)
    except Exception as e:
        fd_log.error(f"❌ Exception during QApplication execution: {e}")
        sys.exit(-1)
