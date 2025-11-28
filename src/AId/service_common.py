# ─────────────────────────────────────────────────────────────────────────────#
# service_common.py
# - 2025/11/27 (revised)
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import os
import sys

# ── service/env ──────────────────────────────────────────────────────────────
def _is_service_env():
    try:
        return not hasattr(sys, "stdin") or (sys.stdin is None) or (not sys.stdin.isatty())
    except Exception:
        return True
SERVICE_MODE = (os.getenv("FD_SERVICE", "0") == "1") or _is_service_env()


# ── config path ──────────────────────────────────────────────────────────────
AID_CONFIG_PRIVATE = "./config/aid_config_private.json5"
AID_CONFIG_PUBLIC  = "./config/aid_config_public.json5"

os.environ.setdefault("PYTHONUNBUFFERED", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ────────────────────────────────────────────────────────────
# EXPORTS
# ────────────────────────────────────────────────────────────
__all__ = [
    "SERVICE_MODE",
    "AID_CONFIG_PRIVATE",
    "AID_CONFIG_PUBLIC",
]
