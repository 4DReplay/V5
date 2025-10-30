# ─────────────────────────────────────────────────────────────────────────────
# dms_service.py  — robust import + clean shutdown
# date: 2025-10-25
# ─────────────────────────────────────────────────────────────────────────────
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

# 1) 우선 parent(=V5)를 경로에 추가
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 2) dms_agent 로드: 패키지 임포트 → 실패하면 파일에서 직접 로드
def _load_agent_module():
    try:
        # 정상 패키지 경로 임포트 (service.DMs.dms_agent)
        from service.DMs import dms_agent as agent  # type: ignore
        return agent
    except Exception as e:
        # fallback: 파일 경로로 직접 로드
        try:
            import importlib.util, traceback
            agent_path = ROOT / "service" / "DMs" / "dms_agent.py"
            spec = importlib.util.spec_from_file_location("dms_agent", str(agent_path))
            mod = importlib.util.module_from_spec(spec)  # type: ignore
            assert spec and spec.loader
            spec.loader.exec_module(mod)  # type: ignore
            # 패키지 임포트 실패 원인 로그
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
        # 서비스 정지 전파
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        try:
            if self._sup is not None:
                # supervisor 내부에서 모든 관리 exe를 강제 정리하도록 구현되어 있음
                self._sup.shutdown()
        except Exception:
            pass

        # 실행 스레드 종료 대기(최대 10초)
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
        # 설정 로드
        try:
            cfg = agent.load_config(agent.DEFAULT_CONFIG)
        except Exception as e:
            agent.ensure_dir(agent.LOG_DIR_DEFAULT)
            with (LOG_ROOT / "DMs.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" [WARN] fallback config: {e}\n")
            cfg = {
                "http_host": "0.0.0.0",
                "http_port": 51050,
                "log_dir": str(agent.LOG_DIR_DEFAULT),
                "heartbeat_interval_sec": 5,
                "executables": [],
            }

        # 로그 경로 절대화
        log_dir = cfg.get("log_dir", str(agent.LOG_DIR_DEFAULT))
        log_dir_path = Path(log_dir)
        if not log_dir_path.is_absolute():
            log_dir_path = (agent.ROOT / log_dir_path).resolve()

        # supervisor 구동
        self._sup = agent.DmsSupervisor(cfg, log_dir_path)
        self._runner = threading.Thread(target=self._run_supervisor, daemon=True)
        self._runner.start()

        # Stop 이벤트 대기
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)


if __name__ == "__main__":
    # 예) 관리자 PowerShell:  python service\DMs\dms_service.py install /start
    win32serviceutil.HandleCommandLine(DMsService)
