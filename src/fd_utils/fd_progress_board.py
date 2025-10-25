# ─────────────────────────────────────────────────────────────────────────────#
# Progress Bar
# - 2025/10/10
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#
# progress_board.py
from rich.live import Live
from rich.table import Table
from rich.console import Console
from threading import Thread, Lock, Event
import time

class ProgressBoard:
    _inst = None
    _lock = Lock()

    def __init__(self, refresh_hz=10):
        self.console = Console()
        self.data = {}          # key=(tg,cam) -> dict(pct, bar, msg)
        self.mutex = Lock()
        self.stop_ev = Event()
        self.refresh = 1.0/refresh_hz
        self.thread = Thread(target=self._loop, daemon=True)
        self.thread.start()

    @classmethod
    def instance(cls):
        with cls._lock:
            if cls._inst is None:
                cls._inst = ProgressBoard()
            return cls._inst

    def update(self, tg:int, cam:int, pct:int, bar:str, msg:str):
        with self.mutex:
            self.data[(tg,cam)] = {"pct": pct, "bar": bar, "msg": msg}

    def _render(self):
        tb = Table(title="Calibration & Encode Progress", expand=True)
        tb.add_column("TG", justify="right", width=4)
        tb.add_column("CAM", justify="right", width=4)
        tb.add_column("PCT", justify="right", width=6)
        tb.add_column("BAR", overflow="fold", no_wrap=True)
        tb.add_column("MSG")

        with self.mutex:
            # 보기 좋게 정렬
            for (tg, cam), v in sorted(self.data.items()):
                tb.add_row(f"{tg:02d}", f"{cam:02d}",
                           f"{v['pct']:3d}%",
                           v["bar"], v["msg"])
        return tb

    def _loop(self):
        with Live(self._render(), console=self.console, refresh_per_second=int(1/self.refresh), transient=False):
            while not self.stop_ev.is_set():
                time.sleep(self.refresh)

    def shutdown(self):
        self.stop_ev.set()
        try: self.thread.join(timeout=1.0)
        except: pass
