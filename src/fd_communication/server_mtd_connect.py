# -*- coding: utf-8 -*-
from __future__ import annotations
import json, struct, time, copy, socket, os
from pathlib import Path
from typing import Tuple

TRACE_DIR = Path(__file__).resolve().parents[3] / "logs" / "OMS"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

class MtdTraceError(Exception):
    def __init__(self, msg: str, trace_tag: str):
        super().__init__(msg)
        self.trace_tag = trace_tag

def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + f"_{int(time.time()*1000)%100000}"

def _prepare_outgoing(payload: dict) -> dict:
    p = copy.deepcopy(payload)
    if "MMd" in p: p["SPd"] = p.pop("MMd")
    p.pop("MMc", None)
    return p

def _normalize_incoming(resp: dict) -> dict:
    if isinstance(resp, dict) and "SPd" in resp:
        resp["MMd"] = resp["SPd"]
    return resp

def _send_once(host: str, port: int, js: bytes, sep_byte: bytes, timeout: float, tag: str):
    header = struct.pack("<I", len(js)) + sep_byte
    packet = header + js

    # ── 로깅: 실제 보낸 헤더 5바이트 저장
    (TRACE_DIR / f"mtd_{tag}_tx_hdr.bin").write_bytes(header)
    (TRACE_DIR / f"mtd_{tag}_tx_sep.txt").write_text(repr(sep_byte), encoding="utf-8")

    hdr = b""; body = b""
    with socket.create_connection((host, int(port)), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(packet)

        deadline = time.time() + timeout
        # header(5)
        while len(hdr) < 5:
            if time.time() >= deadline:
                raise MtdTraceError("timeout while reading header", tag)
            chunk = s.recv(5 - len(hdr))
            if not chunk: break
            hdr += chunk
        if len(hdr) < 5:
            raise MtdTraceError("empty response or short header", tag)

        (TRACE_DIR / f"mtd_{tag}_rx_hdr.bin").write_bytes(hdr)

        size = struct.unpack("<I", hdr[:4])[0]
        typ  = hdr[4]  # 0,1 or ord('|')

        # body
        while len(body) < size:
            if time.time() >= deadline:
                raise MtdTraceError("timeout while reading body", tag)
            chunk = s.recv(size - len(body))
            if not chunk: break
            body += chunk

    # ── 로깅: 수신 본문
    (TRACE_DIR / f"mtd_{tag}_rx_len.txt").write_text(f"{size}", encoding="utf-8")

    # 수신 해석(신/구 호환)
    if typ in (0, ord('|')):  # JSON
        resp = json.loads(body.decode("utf-8", "replace"))
        resp = _normalize_incoming(resp)
        return resp
    elif typ == 1:            # JSON + binary (legacy)
        if len(body) < 16:
            raise MtdTraceError("invalid type-1 body", tag)
        jlen, blen, _, _ = struct.unpack("<IIII", body[:16])
        j = body[16:16+jlen]
        resp = json.loads(j.decode("utf-8", "replace"))
        resp["__binary_size__"] = blen
        resp = _normalize_incoming(resp)
        return resp
    else:
        raise MtdTraceError(f"unexpected resp type={typ}", tag)
# server_mtd_connect.py

def tcp_json_roundtrip(host: str, port: int, message: dict, timeout: float = 10.0, trace_tag: str | None = None):
    tag = trace_tag or _now_tag()
    outgoing = _prepare_outgoing(message)

    js = json.dumps(outgoing, ensure_ascii=False).encode("utf-8")

    # always safe minimum timeout
    timeout = max(timeout, 10.0)

    # ✅ MTd 구규격: 4바이트 길이(LE) + 1바이트 구분자(=0)
    header = struct.pack("<IB", len(js), 0)   # <- 여기만 확실히 0으로 고정
    packet = header + js

    hdr = b""; body = b""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(packet)

            # --- header timeout only ---
            header_deadline = time.time() + 5.0
            while len(hdr) < 5:
                if time.time() >= header_deadline:
                    raise MtdTraceError("timeout while reading header", tag)
                chunk = s.recv(5 - len(hdr))
                if not chunk: break
                hdr += chunk
            if len(hdr) < 5:
                raise MtdTraceError("empty response or short header", tag)

            size, typ = struct.unpack("<IB", hdr)
            
            # --- body timeout only ---
            body_deadline = time.time() + max(5.0, timeout - 5.0)
            # recv body
            while len(body) < size:
                if time.time() >= body_deadline:
                    raise MtdTraceError("timeout while reading body", tag)
                chunk = s.recv(size - len(body))
                if not chunk: break
                body += chunk

    except Exception as e:
        (TRACE_DIR / f"mtd_{tag}_error.txt").write_text(
            f"{repr(e)}\nrecv_hdr={len(hdr)}B, recv_body={len(body)}B\n", encoding="utf-8"
        )
        raise MtdTraceError(str(e), tag)

    # typ==0(JSON) / typ==1(JSON+binary)
    if typ == 0:
        resp = json.loads(body.decode("utf-8","replace"))
        resp = _normalize_incoming(resp)
        return resp, tag
    elif typ == 1:
        if len(body) < 16: raise MtdTraceError("invalid type-1 body", tag)
        jlen, blen, _, _ = struct.unpack("<IIII", body[:16])
        j = body[16:16+jlen]
        resp = json.loads(j.decode("utf-8","replace"))
        resp["__binary_size__"] = blen
        resp = _normalize_incoming(resp)
        return resp, tag
    else:
        raise MtdTraceError(f"unexpected resp type={typ}", tag)
