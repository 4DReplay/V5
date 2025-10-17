import sys
import os
import socket
import struct
import threading

from loguru import logger

cur_path = os.path.abspath(os.path.dirname(__file__))
common_path = os.path.abspath(os.path.join(cur_path, '..'))
sys.path.append(common_path)


VSPD_MESSAGE_HEADER_SIZE = 5


class TCPServer:
    def __init__(self, host, port, handle, name = None):
        self.name = name
        self.host = host
        self.port = port
        self.sock = None
        self.lock = threading.Lock()
        self.session_list = []    # sock, session
        self.listen_thread = None
        self.end = False
        self.cb = handle
        
        
    def open(self):
        self.listen_thread = threading.Thread(target=self.start)
        self.listen_thread.start()
        
        
    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.listen(5) # minimum backlog
        # logger.info(f"[app] start() begin listening.....host: {self.host}:{self.port}")

        while self.end == False:
            try:
                conn, addr = self.sock.accept()
                logger.info(f"[app] connected to: {addr[0]}:{addr[1]}")
                session_thread = threading.Thread(target=self.create_session, args=(conn,addr))
                with self.lock:
                    self.session_list.append((conn, addr, session_thread))
                session_thread.start()
            except Exception as e:
                logger.info(f"[app] close server socket: {e}")
                break
            
        logger.info(f"[app] start() end listening.. ")
        
        
    def create_session(self, conn, addr):
        logger.info(f"[app] create_session() begin..ready to receive data .........")
        while self.end == False:
            try:
                header_data = conn.recv(VSPD_MESSAGE_HEADER_SIZE)
                unpacked_data = struct.unpack('<IB', header_data)
                # logger.info(f"[app] unpacked_data_size: {unpacked_data[0]}")
                # logger.info('receive header from server: size={}, sep={}'.format(unpacked_data[0], unpacked_data[1].decode()))
                body_data = conn.recv(unpacked_data[0])
                # logger.info('[app] receive body from server: data={}'.format(body_data.decode()))
                if self.cb:
                    self.cb(body_data.decode())
                    
                # cmd, token, url = self.msg.parse_app_msg(body_data)
                # if cmd == "get_version":
                #     res_msg = self.msg.make_default_msg(self.host, 1000)
                # elif cmd == "run":
                #     ret = self.callback_marker(self.cb, url)
                #     res_msg = self.msg.make_res_msg(ret, token, "Run", self.host)
                # elif cmd == "stop":
                #     ret = self.callback_marker(self.cb)
                #     res_msg = self.msg.make_res_msg(ret, token, "Stop", self.host)
                # else:
                #     pass
                # # 헤더 구성 (처음 4바이트는 데이터 크기, 마지막 1바이트는 타입 코드)
                # header = struct.pack('>IB', len(res_msg), 0)
                # self.send_flush(header)
                # self.send_flush(res_msg.encode())
                # logger.info(f"[app] send response msg: {res_msg}")
            except Exception as e:
                logger.error(f"[{self.name}] exception : {e}")
                break
            
        with self.lock:
            self.session_list = [session for session in self.session_list if session[1] != addr]
            
        logger.info(f"[app] create_session() end..")
        
        
    def close(self):
        logger.info(f"[app] close() begin...")
        self.end = True
        
        for sock, addr, th in self.session_list:
            sock.close()
            th.join()
            logger.info(f"[app] close session [{addr[0]}:{addr[1]}]")
        self.session_list.clear()
            
        self.sock.close()
        self.listen_thread.join()
        logger.info(f"[app] close() end...")
        
        
    def is_connected(self) -> bool:
        with self.lock:
            return len(self.session_list) > 0
        
        
    def send_msg(self, msg, target=None):
        header = struct.pack('<IB', len(msg), 0)
        for sock, addr, _ in self.session_list:
            if target is None or target == addr[0]:
                self.send_flush(sock, header)
                self.send_flush(sock, msg.encode())
                if target is not None:
                    break
                
                
    def send_flush(self, sock, data):
        # 버퍼링 없이 데이터 전송
        sent_bytes = 0
        while sent_bytes < len(data):
            remaining_bytes = len(data) - sent_bytes
            sent = sock.send(data[sent_bytes:sent_bytes + remaining_bytes])
            if sent == 0:
                raise RuntimeError("[app] socket connection broken")
            sent_bytes += sent
        # logger.debug(f'send_flush - size:{sent_bytes}, data:{data}')
        
        
if __name__ == "__main__":
    pass