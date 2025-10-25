# websocket_client.py
import json
import asyncio
import time
import threading
import websockets

from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log
from fd_utils.fd_data_manager   import DataManager

import logging
from datetime import datetime


# websocket_client.py
import json
import asyncio
import threading
import websockets
import logging
from datetime import datetime

class WebSocketHandler:
    def __init__(self):
        self.websocket = None
        self.is_connected = False
        self.should_reconnect = True
        self._on_pitch_callback = None

    def set_on_pitch_callback(self, callback):
        ''' aid_main에서 콜백 등록용 '''
        self._on_pitch_callback = callback

    async def connect(self):
        uri = f"{conf._websocket_url}:{conf._websocket_port}"

        while self.should_reconnect:
            try:
                # ping 유지 설정: 끊긴 연결을 빨리 감지
                self.websocket = await websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_queue=32,
                )
                self.is_connected = True
                print(f"✅ WebSocket Connected: {uri}")
                await self.start_listening()
            except Exception as ex:
                self.is_connected = False
                print(f"🚨 WebSocket Connection error: {ex}")
                print("🔁 2 sec reconnection...")
                await asyncio.sleep(2)

    async def start_listening(self):
        allowed_kinds = {"Pitch", "Hit"}

        def parse_json_maybe_twice(s: str):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError:
                return None
            # 이중 인코딩: '"{...}"' 또는 '"[...]"' 형태면 한 번 더 파싱 시도
            if isinstance(obj, str) and obj[:1] in ("{", "["):
                try:
                    return json.loads(obj)
                except json.JSONDecodeError:
                    return obj  # 문자열 그대로 반환 (아래에서 문자열 처리 로직으로 감)
            return obj

        while self.is_connected and self.websocket is not None:
            try:
                raw = await self.websocket.recv()

                # bytes 수신 대비
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "ignore")

                obj = parse_json_maybe_twice(raw)
                if obj is None:
                    logging.warning(f"⚠️ JSON decode fail | head: {raw[:200]!r}")
                    continue

                # 문자열(하트비트/제어메시지) 무시
                if isinstance(obj, str):
                    s = obj.strip().strip('"').lower()
                    if not s or s in {"ping", "pong", "ok", "connected", "hello", "keepalive", "heartbeat", "ack"}:
                        logging.debug(f"⏳ Ignored control msg: {s!r}")
                        continue
                    logging.debug(f"⏳ Ignored string msg head: {s[:80]!r}")
                    continue

                # 리스트/단일 dict 표준화
                if isinstance(obj, list):
                    msgs = [m for m in obj if isinstance(m, dict)]
                    if not msgs:
                        logging.debug("⏳ Ignored non-dict list msg")
                        continue
                elif isinstance(obj, dict):
                    msgs = [obj]
                else:
                    logging.debug(f"⏳ Ignored unsupported type: {type(obj)}")
                    continue

                # 각 메시지 처리
                for message_data in msgs:
                    try:
                        # Kind(대소문자 혼용 방지)
                        kind = message_data.get("Kind") or message_data.get("kind")
                        if kind not in allowed_kinds:
                            continue

                        # DB raw 저장
                        conf._baseball_db.insert_raw_data(message_data)

                        # DataManager 반영
                        datamgr = DataManager()
                        datamgr.SetData(message_data)

                        # 공통 수신 시각
                        received_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                        if kind == "Pitch":
                            release_speed = round(float(datamgr.GetData("Release.Speed", default=0)), 1)
                            release_spinrate = int(datamgr.GetData("Release.SpinRate", default=0))
                            pitch_type = datamgr.GetData("pitchType", default="")

                            fd_log.print(
                                f"🕒 Received [{kind}][{received_time}], "
                                f"speed:{release_speed}, spin:{release_spinrate}, pitch_type:{pitch_type}"
                            )

                            # 2025-08-10
                            # event time
                            conf._time_event_pitching = time.perf_counter()

                            if self._on_pitch_callback:
                                try:
                                    self._on_pitch_callback(message_data)
                                except Exception as ex:
                                    logging.error(f"❌ Error in pitch callback: {ex}")

                        elif kind == "Hit":
                            ball_distance = round(float(datamgr.GetData("LandingFlat.Distance", default=0)), 1)
                            launch_speed = round(float(datamgr.GetData("Launch.Speed", default=0)), 1)
                            launch_angle = round(float(datamgr.GetData("Launch.VerticalAngle", default=0)), 1)

                            fd_log.print(
                                f"🕒 Received [{kind}][{received_time}], "
                                f"distance:{ball_distance}, speed:{launch_speed}, angle:{launch_angle}"
                            )

                    except Exception as ex:
                        # 개별 메시지 처리 중 발생한 오류는 다음 메시지로 계속
                        logging.error(f"❌ Error while handling a message: {ex} | head: {str(message_data)[:200]!r}")
                        continue

            except websockets.exceptions.ConnectionClosed as e:
                # 정상/비정상 모두 여기로 들어올 수 있음 → 상위 connect()에서 재시도
                self.is_connected = False
                code = getattr(e, "code", "?")
                reason = getattr(e, "reason", "?")
                print(f"ℹ️ WebSocket closed (Code: {code}, Reason: {reason})")
                print("🔁 Ready to websocket connect retry...")
                break
            except asyncio.CancelledError:
                # 태스크 취소 시 즉시 종료
                self.is_connected = False
                raise
            except Exception as ex:
                # 알 수 없는 수신 루프 에러 → 상위 connect() 재시도
                self.is_connected = False
                print(f"🚨 WebSocket listen error: {ex}")
                print("🔁 Ready to websocket connect retry...")
                break
    async def close(self):
        self.should_reconnect = False
        self.is_connected = False
        if self.websocket is not None:
            try:
                await self.websocket.close()
            except Exception:
                pass


class WebSocketThread(threading.Thread):
    ''' Runs the WebSocket in a separate thread '''
    def __init__(self):
        super().__init__(daemon=True)
        self.loop = None
        self.ws_handler = WebSocketHandler()

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        # connect() 내부 while이 재연결 담당하므로 run_forever 불필요
        self.loop.run_until_complete(self.ws_handler.connect())


# Global variable for WebSocket execution
_websocket_thread = None


def start_websocket():
    ''' Start WebSocket in the background '''
    global _websocket_thread
    if _websocket_thread is None:
        _websocket_thread = WebSocketThread()
        _websocket_thread.start()
        print("✅ WebSocket is running in a separate thread.")
    else:
        print("⚠️ WebSocket is already running.")


def stop_websocket():
    ''' Gracefully stop WebSocket thread '''
    global _websocket_thread
    if _websocket_thread is not None:
        try:
            loop = _websocket_thread.loop
            ws = _websocket_thread.ws_handler
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
                loop.call_soon_threadsafe(loop.stop)
        finally:
            print("🛑 WebSocket thread stop requested.")
    else:
        print("⚠️ No active WebSocket instance.")



class WebSocketThread(threading.Thread):

    ''' Runs the WebSocket in a separate thread '''
    def __init__(self):
        super().__init__(daemon=True)  # Set as daemon thread
        self.loop = None
        self.ws_handler = WebSocketHandler()

    def run(self):
        ''' Run WebSocket in a separate event loop '''
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.ws_handler.connect())
        self.loop.run_forever()  # Keep WebSocket running


# Global variable for WebSocket execution
_websocket_thread = None


def start_websocket():
    ''' Start WebSocket in the background '''
    global _websocket_thread
    if _websocket_thread is None:
        _websocket_thread = WebSocketThread()
        _websocket_thread.start()
        print("✅ WebSocket is running in a separate thread.")
    else:
        print("⚠️ WebSocket is already running.")


def stop_websocket():
    ''' Stop WebSocket thread (currently needs manual termination) '''
    global _websocket_thread
    if _websocket_thread is not None:
        print("🛑 The WebSocket thread must be manually terminated.")
    else:
        print("⚠️ No active WebSocket instance.")
