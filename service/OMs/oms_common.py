# ─────────────────────────────────────────────────────────────────────────────
# oms_common.py
# - Common JSON loader utilities
# - 2025/11/24
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────

import os, time
import json
import base64

from pathlib import Path
from loguru import logger as fd_log

# user-config.json 올바른 절대 경로 계산
base_dir = os.path.dirname(os.path.abspath(__file__))
v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
USER_CONFIG_PATH = Path(os.path.join(v5_root, "web", "config", "user-config.json"))

# ─────────────────────────────────────────────────────────────────────────────
# json file save/load
# ─────────────────────────────────────────────────────────────────────────────
def fd_load_json_file(filename: str):
    """
    Load a JSON file using a relative path under the 'web' directory.
    Parameters
    ----------
    filename : str
        Virtual path starting with "/", for example:
            "/config/user-config.json"
            "/record/record-history.json"
    Mapping Rule
    ------------
    Given V5 project structure:
        V5/
          ├─ web/
          │    ├─ config/
          │    ├─ record/
          │    └─ ...
          └─ src/
               └─ common/
                    └─ oms_common.py

    The function maps:
        "/config/user-config.json"
            →  V5/web/config/user-config.json
    Returns
    -------
    dict
        Parsed JSON data, or {} if file does not exist or parsing fails.
    """
    try:
        # Base directory of this file (e.g., V5/src/common/)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # Root directory of V5 project (two levels up)
        v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
        # Clean leading slash: "/config/user.json" → "config/user.json"
        clean = filename.lstrip("/")
        # Construct absolute path under V5/web/
        cfg_path = os.path.join(v5_root, "web", clean)
        cfg_path = os.path.abspath(cfg_path)
        if not os.path.exists(cfg_path):
            fd_log.warning(f"JSON file not found: {cfg_path}")
            return {}
        with open(cfg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        fd_log.error(f"load_json_file({filename}) failed: {e}")
        return {}
def fd_save_json_file(filename: str, data: dict):
    """
    Save a JSON file into the 'web' directory using the same relative path rules
    as fd_load_json_file().
    Parameters
    ----------
    filename : str
        Virtual path starting with '/', e.g.
            "/config/user-config.json"
            "/record/record-history.json"

    data : dict
        JSON serializable dictionary to write.
    Returns
    -------
    bool
        True on success, False on failure.
    """
    try:
        # Base directory: e.g. V5/src/common/
        base_dir = os.path.dirname(os.path.abspath(__file__))
        v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))

        # Clean leading slash
        clean = filename.lstrip("/")

        # Full path = V5/web/<clean>
        save_path = os.path.join(v5_root, "web", clean)
        save_path = os.path.abspath(save_path)

        # Ensure directory exists
        save_dir = os.path.dirname(save_path)
        os.makedirs(save_dir, exist_ok=True)

        # Write JSON
        with open(save_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        fd_log.info(f"Saved JSON: {save_path}")
        return True

    except Exception as e:
        fd_log.error(f"fd_save_json_file({filename}) failed: {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
# common function - format
# ─────────────────────────────────────────────────────────────────────────────
def fd_format_hms_verbose(ms):
    sec = ms / 1000.0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}h: {m:02d}m: {s:02d}s"
def fd_format_datetime(ms_value: float):
    """Convert epoch ms → 'YYYY-MM-DD HH:MM:SS'"""
    sec = ms_value / 1000.0
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sec))
def fd_format_hms_ms(ms_value: float):
    """Convert duration ms → 'HH:MM:SS.mmm' """
    total_ms = int(ms_value)
    h = total_ms // (3600 * 1000)
    m = (total_ms % (3600 * 1000)) // (60 * 1000)
    s = (total_ms % (60 * 1000)) // 1000
    ms = total_ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

# ─────────────────────────────────────────────────────────────────────────────
# adjust files
# ─────────────────────────────────────────────────────────────────────────────
def fd_find_adjustinfo_file():
    """
    Search for AdjustInfo file inside:
        V5/daemon/ENd/AdjustInfo/0/*.adj
    Returns the first matched file path or "".
    """

    base_dir = os.path.dirname(os.path.abspath(__file__))
    v5_root = os.path.abspath(os.path.join(base_dir, "..", ".."))
    target_dir = os.path.join(v5_root, "daemon", "EMd", "AdjustInfo", "0")
    if not os.path.exists(target_dir):
        return ""

    for f in os.listdir(target_dir):
        if f.lower().endswith(".adj"):
            return os.path.join(target_dir, f)

    return ""
def fd_load_adjust_info(adj_root="C:/4DReplay/V5/daemon/EMd/AdjustInfo/0"):
    """
    Load calibration & user point data from AdjustInfo folder.
    Handles Base64 decoding for CalibrationData.adj only.
    UserPointData.pts is plain JSON.
    """

    cal_path = os.path.join(adj_root, "CalibrationData.adj")
    pts_path = os.path.join(adj_root, "UserPointData.pts")

    result = {
        "calibration": {},
        "points": {}
    }

    # -----------------------------
    # 1) CalibrationData.adj (BASE64)
    # -----------------------------
    try:
        with open(cal_path, "r", encoding="utf-8") as f:
            cal_raw = json.load(f)

        summary = cal_raw.get("summary", {})

        # summary.Comment → Base64
        if "Comment" in summary:
            try:
                summary["Comment"] = base64.b64decode(summary["Comment"]).decode("utf-16")
            except:
                pass

        # summary.world_coords → Base64 → JSON
        if "world_coords" in summary:
            try:
                summary["world_coords"] = json.loads(
                    base64.b64decode(summary["world_coords"]).decode("utf-16")
                )
            except:
                pass

        # pts list → Base64
        pts_list_raw = cal_raw.get("PtsList", [])
        decoded_pts_list = []

        for item in pts_list_raw:
            dsc_id = item.get("DscID")
            raw_pts = item.get("pts")

            try:
                decoded = json.loads(
                    base64.b64decode(raw_pts).decode("utf-16")
                )
            except:
                decoded = {}

            decoded_pts_list.append({
                "DscID": dsc_id,
                "pts": decoded
            })

        result["calibration"] = {
            "summary": summary,
            "PtsList": decoded_pts_list
        }

    except Exception as e:
        fd_log.error(f"[AdjustInfo] Failed to load CalibrationData.adj: {e}")

    # -----------------------------
    # 2) UserPointData.pts (PLAIN JSON)
    # -----------------------------
    try:
        with open(pts_path, "r", encoding="utf-8") as f:
            pts_raw = json.load(f)

        # This file is NOT encoded.
        result["points"] = pts_raw

    except Exception as e:
        fd_log.error(f"[AdjustInfo] Failed to load UserPointData.pts: {e}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# USER Config
# ─────────────────────────────────────────────────────────────────────────────
def fd_load_user_config():
    try:
        if USER_CONFIG_PATH.exists():
            return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
        return {}
    except Exception as e:
        fd_log.error(f"[config] load_user_config fail: {e}")
        return {}
def fd_save_user_config(cfg: dict):
    try:
        USER_CONFIG_PATH.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        return True
    except Exception as e:
        fd_log.error(f"[config] save_user_config fail: {e}")
        return False
def fd_update_prefix_item(index: int):
    cfg = fd_load_user_config()
    if "prefix" not in cfg:
        return False, "no prefix field"
    prefix = cfg["prefix"]
    items = prefix.get("list", [])
    # bounds check
    if index < 0 or index >= len(items):
        return False, "index out of range"
    # update value
    prefix["select-item"] = index
    ok = fd_save_user_config(cfg)
    if not ok:
        return False, "save failed"
    fd_log.info(f"[prefix] Updated select-item -> {index}")
    return True, ""