# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# dms_service.py  â€” robust import + clean shutdown
# date: 2025-10-25
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# -*- coding: utf-8 -*-

import os
import sys
import time
import threading
from pathlib import Path

import win32event
import win32service
import win32serviceutil

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent.resolve()  # C:\4DReplay\V5
LOG_ROOT = ROOT / "logs" / "DMS"
LOG_ROOT.mkdir(parents=True, exist_ok=True)

# 1) ìš°ì„  parent(=V5)ë¥¼ ê²½ë¡œì— ì¶”ê°€
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 2) dms_agent ë¡œë“œ: íŒ¨í‚¤ì§€ ìž„í¬íŠ¸ â†’ ì‹¤íŒ¨í•˜ë©´ íŒŒì¼ì—ì„œ ì§ì ‘ ë¡œë“œ
def _load_agent_module():
    try:
        # ì •ìƒ íŒ¨í‚¤ì§€ ê²½ë¡œ ìž„í¬íŠ¸ (service.DMs.dms_agent)
        from service.DMs import dms_agent as agent  # type: ignore
        return agent
    except Exception as e:
        # fallback: íŒŒì¼ ê²½ë¡œë¡œ ì§ì ‘ ë¡œë“œ
        try:
            import importlib.util, traceback
            agent_path = ROOT / "service" / "DMs" / "dms_agent.py"
            spec = importlib.util.spec_from_file_location("dms_agent", str(agent_path))
            mod = importlib.util.module_from_spec(spec)  # type: ignore
            assert spec and spec.loader
            spec.loader.exec_module(mod)  # type: ignore
            # íŒ¨í‚¤ì§€ ìž„í¬íŠ¸ ì‹¤íŒ¨ ì›ì¸ ë¡œê·¸
            with (LOG_ROOT / "DMs.service.err.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [INFO] package import failed, used file-loader\n")
                f.write(repr(e) + "\n")
            return mod
        except Exception:
            with (LOG_ROOT / "DMs.service.err.log").open("a", encoding="utf-8") as f:
                import traceback
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [FATAL] cannot load dms_agent\n")
                f.write(traceback.format_exc() + "\n")
            raise

agent = _load_agent_module()

SERVICE_NAME  = "DMs"
DISPLAY_NAME  = "4DReplay DMs Agent"
DESCRIPTION   = "Process supervisor + HTTP control for 4DReplay"


class DMsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._sup = None
        self._runner = None

    def SvcStop(self):
        # ì„œë¹„ìŠ¤ ì •ì§€ ì „íŒŒ
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        try:
            if self._sup is not None:
                # supervisor ë‚´ë¶€ì—ì„œ ëª¨ë“  ê´€ë¦¬ exeë¥¼ ê°•ì œ ì •ë¦¬í•˜ë„ë¡ êµ¬í˜„ë˜ì–´ ìžˆìŒ
                self._sup.shutdown()
        except Exception:
            pass

        # ì‹¤í–‰ ìŠ¤ë ˆë“œ ì¢…ë£Œ ëŒ€ê¸°(ìµœëŒ€ 10ì´ˆ)
        t0 = time.time()
        while self._runner and self._runner.is_alive() and (time.time() - t0 < 10):
            time.sleep(0.1)

        try:
            win32event.SetEvent(self.hWaitStop)
        except Exception:
            pass

    def _run_supervisor(self):
        try:
            self._sup.run()
        except Exception:
            try:
                with (LOG_ROOT / "DMs.service.err.log").open("a", encoding="utf-8") as f:
                    import traceback
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " [EXC] supervisor crashed\n")
                    f.write(traceback.format_exc() + "\n")
            except Exception:
                pass

    def SvcDoRun(self):
        # ì„¤ì • ë¡œë“œ
        try:
            cfg = agent.load_config(agent.DEFAULT_CONFIG)
        except Exception as e:
            agent.ensure_dir(agent.LOG_DIR_DEFAULT)
            with (LOG_ROOT / "DMs.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" [WARN] fallback config: {e}\n")
            cfg = {
                "http_host": "0.0.0.0",
                "http_port": 19776,
                "log_dir": str(agent.LOG_DIR_DEFAULT),
                "heartbeat_interval_sec": 5,
                "executables": [],
            }

        # ë¡œê·¸ ê²½ë¡œ ì ˆëŒ€í™”
        log_dir = cfg.get("log_dir", str(agent.LOG_DIR_DEFAULT))
        log_dir_path = Path(log_dir)
        if not log_dir_path.is_absolute():
            log_dir_path = (agent.ROOT / log_dir_path).resolve()

        # supervisor êµ¬ë™
        self._sup = agent.DmsSupervisor(cfg, log_dir_path)
        self._runner = threading.Thread(target=self._run_supervisor, daemon=True)
        self._runner.start()

        # Stop ì´ë²¤íŠ¸ ëŒ€ê¸°
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)


if __name__ == "__main__":
    # ì˜ˆ) ê´€ë¦¬ìž PowerShell:  python service\DMs\dms_service.py install /start
    win32serviceutil.HandleCommandLine(DMsService)

