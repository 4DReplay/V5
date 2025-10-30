# -*- coding: utf-8 -*-
import sys, time
from pathlib import Path
import win32event, win32service, win32serviceutil

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent.parent.resolve()
LOG_ROOT = ROOT / "logs" / "OMS"; LOG_ROOT.mkdir(parents=True, exist_ok=True)

# 추가 ⬇⬇⬇
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
src_dir = ROOT / "src"
if src_dir.exists() and str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))
    
def _load_agent():
    try:
        from service.OMs import oms_agent as agent
        return agent
    except Exception:
        import importlib.util, traceback
        p = ROOT / "service" / "OMs" / "oms_agent.py"
        spec = importlib.util.spec_from_file_location("oms_agent", str(p))
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(mod)  # type: ignore
        return mod

agent = _load_agent()

SERVICE_NAME = "OMs"
DISPLAY_NAME = "4DReplay OMs Agent"
DESCRIPTION  = "Orchestrator Manager Service (aggregates multiple DMS nodes)."

class OMsService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = DISPLAY_NAME
    _svc_description_ = DESCRIPTION

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._orch = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        try:
            if self._orch: self._orch.stop()
        except Exception: pass
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        try:
            cfg = agent.load_config(agent.DEFAULT_CONFIG)
        except Exception as e:
            (LOG_ROOT / "OMS.log").open("a", encoding="utf-8").write(time.strftime("%F %T ") + f"[WARN] fallback cfg: {e}\n")
            cfg = {"http_host":"0.0.0.0","http_port":52050,"heartbeat_interval_sec":2,"nodes":[]}
        self._orch = agent.Orchestrator(cfg)
        import threading
        t = threading.Thread(target=self._orch.run, daemon=True)
        t.start()
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)

if __name__ == "__main__":
    # 관리자 PowerShell:
    #   python service\OMs\oms_service.py install /start
    win32serviceutil.HandleCommandLine(OMsService)
