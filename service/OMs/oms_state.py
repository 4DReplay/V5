# ─────────────────────────────────────────────────────────────────────────────
# oms_common.py
# - Common JSON loader utilities
# - 2025/11/24
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────

import os, time
import copy
import json
import base64

from pathlib import Path

# ─────────────────────────────────────────────────────────────
# --- Path
# Global root paths
# ─────────────────────────────────────────────────────────────
from oms_env import *

# ─────────────────────────────────────────────────────────────
# 🗂️ STATE / SYSTEM
# ─────────────────────────────────────────────────────────────
SYS_STATE = {} # 새로운 프로세스 상태 저장소
ALLOWED_SYS_KEYS = {
    "connected_daemons",
    "cameras",
    "presd",
    "switches",
    "versions",
    "presd_versions",
    "aic_versions",
    "updated_at",
}
def fd_sys_state_load():
    global SYS_STATE
    try:
        if not SYS_STATE_FILE.exists():
            fd_log.error(f"Load: no file: {SYS_STATE_FILE}")
            SYS_STATE = {}
            return

        raw = json.loads(SYS_STATE_FILE.read_text("utf-8"))
        fd_log.info(f"# Load: {SYS_STATE_FILE}")
        # ① raw 전체에서 updated_at 가장 큰 항목 선택
        if isinstance(raw, dict):
            # case 1) 이미 통합 하나짜리 구조 → 그대로
            if "connected_daemons" in raw and "versions" in raw:
                SYS_STATE = raw
                return
            # case 2) node별 구조: { "10.82.104.210": {...}, "127.0.0.1": {...} }
            best = None
            best_ts = -1
            for key, st in raw.items():
                if not isinstance(st, dict):
                    continue
                ts = st.get("updated_at", 0)
                if isinstance(ts, (int, float)) and ts >= best_ts:
                    best = st
                    best_ts = ts
            if best:
                SYS_STATE = best
            else:
                SYS_STATE = {}
        else:
            SYS_STATE = {}        
    except Exception as e:
        fd_log.exception(f"fd_sys_state_load failed: {e}")
def fd_sys_state_save():
    global SYS_STATE
    try:
        with open(SYS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(SYS_STATE, f, indent=2, ensure_ascii=False)        
    except Exception as e:
        fd_log.exception(f"[save][system][state] failed: {e}")
def fd_sys_state_upsert(payload: dict):
    global SYS_STATE
    clean = {}
    for k, v in payload.items():
        if k in ALLOWED_SYS_KEYS:
            clean[k] = v
    clean["updated_at"] = time.time()
    # SYS_STATE 전체를 clean 으로 교체
    SYS_STATE = clean
    with open(SYS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(SYS_STATE, f, indent=2, ensure_ascii=False) 
    fd_sys_state_save()
def fd_sys_latest_state():
    global SYS_STATE
    if not SYS_STATE:
        return None, {}
    return None, SYS_STATE
def fd_sys_clear_state() -> bool:
    global SYS_STATE
    try:
        SYS_STATE.clear()
        try:
            SYS_STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        return True
    except Exception:
        return False
def fd_sys_clear_connect_state() -> bool:
    global SYS_STATE
    try:
        SYS_STATE["connected_daemons"] = {}
        SYS_STATE["updated_at"] = time.time()
        fd_sys_state_save()
        fd_log.info("[SYS] Clear connect_state OK")
        return True
    except Exception as e:
        fd_log.error(f"[SYS] Clear connect_state FAIL: {e}")
        return False
# ─────────────────────────────────────────────────────────────
# 🗂️ STATE / CAMERA
# ─────────────────────────────────────────────────────────────
CAM_STATE = {} 
def fd_cam_state_load():
    global CAM_STATE
    try:
        if CAM_STATE_FILE.exists():
            CAM_STATE.update(json.loads(CAM_STATE_FILE.read_text("utf-8")))
        else:
            CAM_STATE = {}
    except:
        CAM_STATE = {}
def fd_cam_state_save():
    global CAM_STATE
    try:
        with open(CAM_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(CAM_STATE, f, indent=2, ensure_ascii=False)                
    except Exception as e:
        fd_log.exception(f"[save][camera][state] failed: {e}")
def fd_cam_state_upsert(payload: dict):
    global CAM_STATE    
    # 기존 CAM_STATE 유지 + payload 반영 (merge 방식)
    for k, v in payload.items():
        CAM_STATE[k] = v
    # updated_at 항상 새로 기록
    CAM_STATE["updated_at"] = time.time()
    # 파일 저장
    with open(CAM_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(CAM_STATE, f, indent=2, ensure_ascii=False)
def fd_cam_latest_state():
    global CAM_STATE
    return CAM_STATE
def fd_cam_clear_connect_state() -> bool:
    global CAM_STATE
    try:
        # NOTE: Lock 은 바깥에서 잡아줘야 한다 (self 없음)
        cameras = CAM_STATE.get("cameras")
        if isinstance(cameras, list):
            for cam in cameras:
                if not isinstance(cam, dict):
                    continue
                # unified clear of "connected" flags
                cam["connected"] = False
                if isinstance(cam.get("state"), dict):
                    cam["state"].pop("connected", None)
                cam.pop("connected_state", None)

        # clear summary / aggregation fields
        CAM_STATE["camera_connected"] = {}
        CAM_STATE["camera_record"] = []
        CAM_STATE.pop("connected_summary", None)
        CAM_STATE.pop("connected_map", None)

        CAM_STATE["updated_at"] = time.time()

        fd_cam_state_save()

        fd_log.info("[CAM] Clear connect_state OK")
        return True

    except Exception as e:
        fd_log.error(f"[CAM] Clear connect_state FAIL: {e}")
        return False

# ────────────────────────────────────────────────────────────
# EXPORTS
# ────────────────────────────────────────────────────────────
__all__ = [
    "SYS_STATE","fd_sys_state_load", "fd_sys_state_save","fd_sys_state_upsert", "fd_sys_latest_state","fd_sys_clear_state","fd_sys_clear_connect_state",
    "CAM_STATE","fd_cam_state_load", "fd_cam_state_save", "fd_cam_state_upsert", "fd_cam_latest_state", "fd_cam_clear_connect_state"
]
