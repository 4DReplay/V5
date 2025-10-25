# ─────────────────────────────────────────────────────────────────────────────
# fd_data_manager.py
# date: 2025/10/19
# ─────────────────────────────────────────────────────────────────────────────

import json
from dataclasses import dataclass, field
from typing import Dict, Any

from fd_utils.fd_config_manager import conf
from fd_utils.fd_logging        import fd_log

@dataclass
class DataManager:
    kind: str = ""
    play_id: str = ""
    time: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def SetData(self, data):
        '''
        JSON 데이터를 받아 객체에 저장
        :param data: JSON 문자열 또는 딕셔너리
        '''
        if isinstance(data, str):
            try:
                parsed_data = json.loads(data)  # 문자열이면 JSON 파싱
            except json.JSONDecodeError as e:
                print(f"🚨 JSON 파싱 오류: {e}")
                return
        elif isinstance(data, dict):
            parsed_data = data  # 이미 딕셔너리면 그대로 사용
        else:
            print(f"🚨 잘못된 데이터 타입: {type(data)}")
            return

        self.kind = parsed_data.get("Kind", "")
        self.play_id = parsed_data.get("PlayId", "")
        self.time = parsed_data.get("Time", "")
        self.data = parsed_data.get("data", {})

        # debug
        # fd_log.info(f"✅ Save data complete: time : {self.time}, data : {self.data}")  # 디버깅 출력

        # debug
        # save data sample 1
        # data : {
        # 'Location': {'Height': 0.75747, 'Side': -0.3897, 'Time': 0.499498, 'Speed': 112.67191}, 
        # 'LocationMiddle': {'Height': 0.73508, 'Side': -0.41471}, 
        # 'LocationBack': {'Height': 0.71221, 'Side': -0.43988}, 
        # 'Movement': {'Horizontal': -40.2911, 'InducedVertical': 4.5899, 'SpinAxis': 91.6735, 'Vertical': -117.74699, 'Tilt': '9:00', 'Side0': -0.98127, 'Height0': 1.21933}, 
        # 'Release': {'Extension': 1.86781, 'Height': 1.16281, 'Side': 1.07115, 'Speed': 121.978, 'SpinRate': 2386.1306, 'HorizontalAngle': -3.749641, 'VerticalAngle': 2.732638}, 'NineP': {'X0': {'X': -0.98127, 'Y': 15.24, 'Z': 1.21933}, 
        # 'V0': {'X': 2.36228, 'Y': -33.50208, 'Z': 1.19351}, 'A0': {'X': 2.69883, 'Y': 5.66152, 'Z': -9.56572}, 'Pfxx': 18.293, 'Pfxz': 1.6331}, 'pitchType': 'Curveball'}

        # save data sample 2
        # data : {
        # 'LandingFlat': {'Bearing': 11.41795, 'Dist_f': 0, 'Distance': 44.13963, 'HangTime': 1.256927, 'X': 43.266071548328775, 'Y': 8.73807698019395}, 
        # 'Launch': {'Speed': 144.1953, 'VerticalAngle': 7.4837, 'HorizontalAngle': 7.631295, 'SpinAxis': 266.65662}}

    def GetData(self, key: str, default: Any = None) -> Any:
        '''
        중첩된 데이터를 점(.) 구분자로 탐색하는 함수
        :param key: 가져올 데이터의 키 (예: "Launch.Speed" 또는 "Release.SpinRate")
        :param default: 키가 없을 경우 반환할 기본값
        :return: 해당 키의 값 또는 기본값
        '''
        keys = key.split(".")  # 점(.)을 기준으로 키를 분리
        value = self.data

        try:
            for k in keys:
                value = value[k]  # 중첩된 딕셔너리를 순차적으로 탐색
            return value
        except (KeyError, TypeError):
            print(f"🚨 '{key}' 키를 찾을 수 없음! 기본값 {default} 반환")
            return default

