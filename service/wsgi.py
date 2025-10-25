# ─────────────────────────────────────────────────────────────────────────────#
# wsgi.py
# - 2025/10/21
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

# wsgi.py
import os, sys
from pathlib import Path

# --- 경로 설정 ---
ROOT = r"C:\4DReplay\AId"
AID_DIR = os.path.join(ROOT, "src")
FD_DIR  = os.path.join(AID_DIR, "fd_service")
for p in (ROOT, AID_DIR, FD_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

# --- 설정 디렉터리/파일 보장 ---
CONF_DIR = os.path.join(ROOT, "config")
os.makedirs(CONF_DIR, exist_ok=True)
priv = os.path.join(CONF_DIR, "aid_config_private.json5")
publ = os.path.join(CONF_DIR, "aid_config_public.json5")
if not os.path.exists(priv): open(priv, "w", encoding="utf-8").write("{}\n")
if not os.path.exists(publ): open(publ, "w", encoding="utf-8").write("{}\n")

# 환경변수 주입(라이브러리가 어떤 키를 보든 잡히게)
os.environ["AID_CONF_DIR"] = CONF_DIR
os.environ["FD_CONF_DIR"]  = CONF_DIR
os.environ["CONFIG_DIR"]   = CONF_DIR
os.environ["FD_CFG_PRIVATE"] = priv
os.environ["FD_CFG_PUBLIC"]  = publ
os.environ["FD_REBASE_PATHS"] = os.getenv("FD_REBASE_PATHS", "1")

# --- 토큰/로그/워크디렉터리 ---
TOKEN   = os.environ.get("AID_TOKEN", "AID_TOKEN")  # 필요시 환경변수로 주입
LOG_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# aid_main 실행 커맨드
PY310   = r"C:\Program Files\Python310\python.exe"
AID_CMD = f'{PY310} {os.path.join(AID_DIR, "aid_main.py")}'

# --- 앱 생성 ---
from aid_agent import create_app
app = create_app(aid_cmd=AID_CMD, workdir=ROOT, token=TOKEN, log_dir=LOG_DIR)
