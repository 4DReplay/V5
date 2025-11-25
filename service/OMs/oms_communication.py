# ─────────────────────────────────────────────────────────────────────────────
# oms_communication.py
# - Common JSON loader utilities
# - 2025/11/25
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────

import os, time
import json
import base64

from pathlib import Path
from oms_env import *

def fd_restart_ccd(self):
    """
    Restart CCd process safely and reconnect Cameras.
    """

    fd_log.warning("=== CCD Restart Sequence BEGIN ===")

    # 1) Stop CCd process
    try:
        fd_log.info("[CCD] Sending STOP")
        self._daemon_process_stop("CCd")
    except Exception as e:
        fd_log.error(f"[CCD] STOP failed: {e}")

    time.sleep(1.0)

    # 2) Start CCd process
    try:
        fd_log.info("[CCD] Sending START")
        self._daemon_process_run("CCd")
    except Exception as e:
        fd_log.error(f"[CCD] START failed: {e}")
        return {"ok": False, "error": f"start fail: {e}"}

    # 3) Wait CCd to become alive
    ok = False
    for i in range(20):
        try:
            req = {
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "Status",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Action": "get",
                "Token": fd_make_token(),
            }
            resp = tcp_json_roundtrip(self.mtd_ip, self.mtd_port, req, timeout=2.0)
            if isinstance(resp, list) and resp:
                fd_log.info("[CCD] Alive after restart")
                ok = True
                break
        except:
            pass

        fd_log.debug("[CCD] Waiting reboot...")
        time.sleep(0.5)

    if not ok:
        fd_log.error("[CCD] Restart failed (no response)")
        return {"ok": False, "error": "no CCD response"}

    # 4) Reconnect each Camera
    try:
        st = fd_cam_latest_state()
        cameras = st.get("cameras", [])
        for cam in cameras:
            ip = cam["IP"]
            fd_log.info(f"[CCD] reconnect camera {ip}")
            self._camera_connect_one(ip)
            time.sleep(0.1)
    except Exception as e:
        fd_log.error(f"[CCD] Camera reconnect failed: {e}")

    fd_log.warning("=== CCD Restart Sequence END ===")

    return {"ok": True}
