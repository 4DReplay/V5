import socket
import errno
import struct
import threading

from fd_utils.fd_logging        import fd_log


class TCPClient:
    def __init__(self, name = None):
        self.name = name
        self.serv_addr = ()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setblocking(0)
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.stop_evt = threading.Event()
        self.recv_th = None
        self.cb = None

    def connect(self, ip: str, port: int, callback=None, timeout: int=3) -> bool:
        try:
            self.sock.settimeout(timeout)
            self.sock.connect((ip, port))
            self.sock.settimeout(None)
            self.serv_addr = self.sock.getpeername()
        except socket.error as e:
            if e.errno == errno.EISCONN:
                fd_log.debug(f'Already connected [{self.name}]')
                return True
            else:
                fd_log.error(f'Failed to create socket [{self.name}] : {e.strerror}')
                return False

        self.cb = callback
        self.recv_th = threading.Thread(target=self.recv_thread_func)
        self.recv_th.start()
        fd_log.debug(f'Connection successful [{self.name}]')
        return True

    def close(self):
        self.sock.close()
        self.serv_addr = ()
        self.stop_evt.set()
        if self.recv_th:
            if self.recv_th.is_alive():
                self.recv_th.join()
        fd_log.debug(f'Connection closed')

    def is_connected(self) -> bool:
        try:
            # This will raise an exception if the socket is not connected
            peername = self.sock.getpeername()
            return True
        except socket.error:
            return False
        
    def get_ip(self):
        return self.serv_addr[0] if len(self.serv_addr) > 0 else None
        
    def recv_all(self, size: int) -> bytes:
        data = b''
        while size > 0:
            packet = self.sock.recv(min(size, 1024))
            if not packet:
                return b''
            data += packet
            size -= len(packet)
        return data

    def recv_thread_func(self):
        header_len = struct.calcsize('<IB')
        while not self.stop_evt.is_set():
            try:
                header = self.recv_all(header_len)
                if not header:
                    fd_log.error(f"recv header error")
                    break
                data_len, data_type = struct.unpack('<IB', header)
                if data_len > 0:
                    data = self.recv_all(data_len)
                    if not data:
                        fd_log.error(f"recv data error")
                        break
                    
                    if self.cb:
                        self.cb(data.decode())
                        
            except socket.error as e:
                ip, port = '', ''
                if len(self.serv_addr) > 1:
                    ip, port = self.serv_addr[0], self.serv_addr[1]
                fd_log.error(f'Recv error [{self.name}] : {e.strerror}')
                break
        self.sock.close()
        fd_log.debug(f'Finish recv thread')

    def send_flush(self, data) -> int:
        # 버퍼링 없이 데이터 전송
        sent_bytes = 0
        while sent_bytes < len(data):
            remaining_bytes = len(data) - sent_bytes
            sent = self.sock.send(data[sent_bytes:sent_bytes + remaining_bytes])
            if sent == 0:
                raise ConnectionError("[app] socket connection broken")
            sent_bytes += sent
        return sent_bytes

    def send_msg(self, msg) -> bool:
        if not self.is_connected():
            fd_log.error(f'Fail to send message: Disconnected:\n{msg}')
            return False

        msg_len = len(msg)
        # !: big-endian byte order, <: little-endian byte order
        # B: unsigned byte, 1btye, I: unsigned integer, 4bytes
        header_data = struct.pack('<IB', msg_len, 0x00)
        header_len = len(header_data)
        msg_bytes = msg.encode('utf-8')
        pkt_size = len(header_data) + msg_len

        with self.lock:
            # resize buffer
            if len(self.buf) < pkt_size:
                self.buf = bytearray(pkt_size)
                
            self.buf[:header_len] = header_data
            self.buf[header_len:] = msg_bytes
            
            try:
                send_size = self.send_flush(self.buf)
                if send_size != pkt_size:
                    fd_log.error(f'Send error.. size: {send_size} / {pkt_size}')
                    return False
            except Exception as e:
                fd_log.error(f'Send error: {e}')
                return False
        
        return True