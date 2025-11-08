import sys
import os
import socket
import struct
import threading
import time
import traceback

from fd_utils.fd_logging import fd_log

cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.append(common_path)

VSPD_MESSAGE_HEADER_SIZE = 5  # <I len> + <B flag>

# 동일 포트 중복 생성을 한 프로세스 내에서 방지/재사용
_SERVER_REG = {}
_SERVER_LOCK = threading.Lock()


def _recv_exact(sock, n: int) -> bytes:
    """TCP에서 n바이트 정확히 수신. 부족하면 계속 recv."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError(f"peer closed while expecting {n} bytes (got {len(buf)}/{n})")
        buf.extend(chunk)
    return bytes(buf)


class TCPServer:
    def __init__(self, host, port, handle, name=None):
        self.name = name or "TCP"
        self.host = host
        self.port = int(port)
        self.sock = None
        self.lock = threading.Lock()
        self.session_list = []    # (sock, addr, thread)
        self.listen_thread = None
        self.end = False
        self.cb = handle
        self._is_alias = False

        fd_log.info(f"[{self.name}] TCPServer.__init__ host={host} port={port}")

        # 동일 포트 서버 재사용(같은 프로세스 내)
        with _SERVER_LOCK:
            if self.port in _SERVER_REG:
                fd_log.info(f"[{self.name}] reuse existing TCPServer on {host}:{port}")
                existing = _SERVER_REG[self.port]
                # 상태 공유 (주의: 콜백은 새로 주어진 게 우선)
                self.sock = existing.sock
                self.listen_thread = existing.listen_thread
                self.end = existing.end
                self.cb = self.cb or existing.cb
                self._is_alias = True
            else:
                _SERVER_REG[self.port] = self

    def open(self):
        if self._is_alias:
            fd_log.info(f"[{self.name}] alias instance; open() skipped")
            return
        if self.listen_thread and self.listen_thread.is_alive():
            return
        self.listen_thread = threading.Thread(target=self.start, daemon=True)
        self.listen_thread.start()

    def start(self):
        # 소켓 생성 및 옵션 설정
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            # Windows: 동시 바인딩 방지(명확)
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                # 기타 OS: TIME_WAIT 완화
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            self.sock.bind((self.host, self.port))
            self.sock.listen(64)
            self.sock.settimeout(1.0)

        except OSError as e:
            winerr = getattr(e, "winerror", None)
            if winerr == 10048 or getattr(e, "errno", None) == 10048:
                fd_log.error(f"[{self.name}] Port in use {self.host}:{self.port} (WinError 10048). "
                             f"Server paused; waiting for close() to be called. Detail: {e}")
                # 종료 신호 대기(프로세스는 살아있음)
                while not self.end:
                    time.sleep(1.0)
                fd_log.info(f"[{self.name}] stop signal received during paused state.")
                return
            else:
                fd_log.error(f"[{self.name}] socket init/bind failed: {e}")
                return

        fd_log.info(f"[{self.name}] listening on {self.host}:{self.port}")

        # 메인 수신 루프
        while not self.end:
            try:
                conn, addr = self.sock.accept()
                fd_log.info(f"[{self.name}] connected {addr[0]}:{addr[1]}")
                session_thread = threading.Thread(target=self._session_loop, args=(conn, addr), daemon=True)
                with self.lock:
                    self.session_list.append((conn, addr, session_thread))
                session_thread.start()

            except socket.timeout:
                continue
            except OSError as e:
                if not self.end:
                    fd_log.info(f"[{self.name}] close server socket: {e}")
                break
            except Exception as e:
                fd_log.error(f"[{self.name}] unexpected exception in accept: {e}")
                break

        fd_log.info(f"[{self.name}] start() end listening.. ")

    def _session_loop(self, conn, addr):
        try:
            while not self.end:
                # 1) 헤더 정확히 5바이트
                header_data = _recv_exact(conn, VSPD_MESSAGE_HEADER_SIZE)
                body_len, flag = struct.unpack('<IB', header_data)

                # sanity check (64MB 상한)
                if body_len < 0 or body_len > (64 * 1024 * 1024):
                    raise ValueError(f"invalid body length: {body_len}")

                # 2) 본문 정확히 body_len바이트
                body_data = _recv_exact(conn, body_len)

                # 3) 콜백 호출 (예외는 세션 유지)
                if self.cb:
                    try:
                        self.cb(body_data.decode('utf-8', errors='strict'))
                    except Exception as cb_e:
                        fd_log.error(f"[{self.name}] callback error from {addr[0]}:{addr[1]} : {cb_e}")
                        # 콜백 오류가 있어도 세션은 유지
                        continue

        except ConnectionError as ce:
            fd_log.info(f"[{self.name}] peer closed {addr[0]}:{addr[1]}: {ce}")
        except Exception as e:
            fd_log.error(f"[{self.name}] session exception from {addr[0]}:{addr[1]} : {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            with self.lock:
                self.session_list = [s for s in self.session_list if s[1] != addr]
            fd_log.info(f"[{self.name}] session end {addr[0]}:{addr[1]}")

    def close(self):
        if self._is_alias:
            fd_log.info(f"[{self.name}] alias instance; close() skipped")
            return

        fd_log.info(f"[{self.name}] close() begin...")
        self.end = True

        # 수신 소켓 정리
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        # 세션 정리
        with self.lock:
            for sock, addr, th in self.session_list:
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    if th.is_alive():
                        th.join(timeout=2.0)
                except Exception:
                    pass
                fd_log.info(f"[{self.name}] close session [{addr[0]}:{addr[1]}]")
            self.session_list.clear()

        # 리스너 스레드 종료 대기
        if self.listen_thread and self.listen_thread.is_alive():
            try:
                self.listen_thread.join(timeout=3.0)
            except Exception:
                pass

        # 레지스트리 정리
        with _SERVER_LOCK:
            if _SERVER_REG.get(self.port) is self:
                _SERVER_REG.pop(self.port, None)

        fd_log.info(f"[{self.name}] close() end...")

    def is_connected(self) -> bool:
        with self.lock:
            return len(self.session_list) > 0

    def send_msg(self, msg, target=None):
        header = struct.pack('<IB', len(msg), 0)
        payload = msg.encode('utf-8', errors='strict')
        with self.lock:
            targets = list(self.session_list)
        for sock, addr, _ in targets:
            if target is None or target == addr[0]:
                self._send_flush(sock, header)
                self._send_flush(sock, payload)
                if target is not None:
                    break

    def _send_flush(self, sock, data: bytes):
        sent = 0
        while sent < len(data):
            n = sock.send(data[sent:])
            if n == 0:
                raise RuntimeError("[app] socket connection broken")
            sent += n


if __name__ == "__main__":
    pass
