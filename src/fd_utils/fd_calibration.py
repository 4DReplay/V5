from __future__ import annotations

import os
import re
import json
import copy
from typing import Any, Dict, Optional

class Calibration:
    '''JSON 파일을 읽어 dict로 보관하고, to_dict()로 반환하는 초간단 클래스.
       - 최상위가 리스트면 자동으로 {"AdjustData": 리스트} 형태로 감쌈.
    '''

    _FILE_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16le", "utf-16")

    def __init__(self, payload: Dict[str, Any], file_path: Optional[str] = None):
        if not isinstance(payload, dict):
            raise ValueError("payload must be a dict (use from_file to auto-wrap list).")
        self.file_path = file_path
        self._payload = copy.deepcopy(payload)

    @staticmethod
    def _sanitize_unc_path(p: str) -> str:
        # UNC 경로: 앞의 \\ 유지, 중간 중복 \ 축소 + 제로폭 문자 제거
        p = re.sub(r'[\u200B-\u200D\uFEFF]', '', (p or '')).strip()
        if p.startswith(r"\\"):
            return r"\\" + re.sub(r'\\{2,}', r'\\', p[2:])
        return re.sub(r'\\{2,}', r'\\', p)

    @classmethod
    def from_file(cls, file_path: str) -> "Calibration":
        path = cls._sanitize_unc_path(file_path)
        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")

        with open(path, "rb") as f:
            raw = f.read()

        last_err: Optional[Exception] = None
        for enc in cls._FILE_ENCODINGS:
            try:
                text = raw.decode(enc)
            except Exception as e:
                last_err = e
                continue

            try:
                data = json.loads(text)
            except Exception as e:
                last_err = e
                continue

            # dict면 그대로, list면 감싸기
            if isinstance(data, dict):
                return cls(data, file_path=path)
            if isinstance(data, list):
                return cls({"AdjustData": data}, file_path=path)

            last_err = ValueError("Top-level JSON must be object or array.")
            # 다음 인코딩 시도

        raise ValueError(f"Unable to decode/parse JSON (last error: {last_err})")

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._payload)
