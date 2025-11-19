# live_mtx_manager.py
import subprocess
import psutil
import os

class MediaMTXManager:
    def __init__(self):
        self.proc = None
        self.exe_path = r"C:\4DReplay\V5\gateway\mediamtx.exe"

    def is_running(self):
        if self.proc and self.proc.poll() is None:
            return True
        # Check by process name
        for p in psutil.process_iter(["name"]):
            if p.info["name"] == "mediamtx.exe":
                return True
        return False

    def start(self):
        if self.is_running():
            return True

        self.proc = subprocess.Popen(
            [self.exe_path],
            cwd=os.path.dirname(self.exe_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        return True

    def stop(self):
        if not self.is_running():
            return True

        # Kill all mediamtx.exe
        for p in psutil.process_iter(["name"]):
            if p.info["name"] == "mediamtx.exe":
                p.terminate()
        return True


MTX = MediaMTXManager()
