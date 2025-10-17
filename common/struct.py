from enum import Enum, unique

class ServerObject:
    def __init__(self, ip: str='', connected: bool=False):
        self.ip = ip              # ip address
        self.mac = None           # mac address
        self.ver = None           # sw version
        self.os = None            # os version
        self.gpu_model = None     # graphic card model
        self.gpu_driver = None    # graphic card driver version
        self.connected = connected
        
        
class CameraObject:
    def __init__(self, ip: str, index: int, model: str, presd_ip: str, presd_path: str):
        self.ip = ip                   # 카메라 IP
        self.index = index             # 카메라 인덱스
        self.model = model
        self.presd_ip = presd_ip       # PreSd IP
        self.shaed_dir = presd_path[0:presd_path.find('|')]
        self.disk = presd_path[presd_path.find('|')+1:]
        self.con_status = False   # 연결 상태
        self.rec_status = False   # 녹화 상태
        self.temp = 0             # 카메라 온도
        self.command = None       # 상태 메시지
        self.props = None         # 프로퍼티
        
        
class GimbalObject:
    def __init__(self, ip: str):
        self.ip = ip
        self.status = ''
        self.ver = ''
        self.main_ver = ''
        self.speed = -1
        # self.preset = -1    # 마지막으로 요청된 프리셋 번호를 getposition 결과와 비교하여 같은지로 판단함.
        # roll
        # pan
        # tilt
        
        
class DiskCapacityObject:
    def __init__(self, name:str, total:int, free:int, used:int):
        self.name = name
        self.total = total
        self.free = free
        self.used = used
        
        
class PreStorageObject(ServerObject):
    @ unique
    class Status(Enum):
        UNKNOWN = -1
        NONE = 0
        CONNECTING = 1
        CONNECTED = 2
        PREPARING = 3
        STANDBY = 4
        STREAMING = 5
        STREAMSTOP = 6
    
    def __init__(self, ip: str, connected: bool=False, cam_list: list[str] = []):
        super().__init__(ip, connected)
        self.cam_list = cam_list
        self.status = PreStorageObject.Status.NONE
        self.capacity = None
    
    def set_status(self, status: str):
        # 주석처리된 코드는 PreSd 에서 오타로 보내지는 문자열이지만 4DPD 에서도 오타 문자열로 확인하기 때문에 호환성을 위해 오타로 사용함..
        match status:
            case 'STEP NONE': self.status = PreStorageObject.Status.NONE
            # case 'STEP CONNECTING': self.status = PreStorageObject.Status.CONNECTING
            # case 'STEP CONNECTED': self.status = PreStorageObject.Status.CONNECTED
            case 'STEP CONNETING': self.status = PreStorageObject.Status.CONNECTING
            case 'STEP CONNETED': self.status = PreStorageObject.Status.CONNECTED
            case 'STEP PREPARING': self.status = PreStorageObject.Status.PREPARING
            case 'STEP STANDBY': self.status = PreStorageObject.Status.STANDBY
            # case 'STEP STREAMING': self.status = PreStorageObject.Status.STREAMING
            case 'STEP STEAMING': self.status = PreStorageObject.Status.STREAMING
            case 'STEP STREAMSTOP': self.status = PreStorageObject.Status.STREAMSTOP
            case _: self.status = PreStorageObject.Status.UNKNOWN
            
            
class VideoFormat:
    def __init__(self, codec: str='H265', resol: str='UHD', fps: int=60, gop:int = 30, bitrate:int =50):
        self.codec = codec,
        self.resol = resol, # resolution
        self.fps = fps,
        self.gop = gop,
        self.bitrate = bitrate,  # Mbps