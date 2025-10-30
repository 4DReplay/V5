# 상단에 추가
import struct, json, socket, time
from pathlib import Path

TRACE_DIR = Path(__file__).resolve().parents[3] / "logs" / "OMS"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

class MtdTraceError(Exception):
    def __init__(self, msg: str, trace_tag: str):
        super().__init__(msg)
        self.trace_tag = trace_tag

def _now_tag():
    return time.strftime("%Y%m%d-%H%M%S") + f"_{int(time.time()*1000)%100000}"

def tcp_json_roundtrip(host: str, port: int, payload: dict, timeout=10.0, debug=False, trace_tag: str | None = None):
    """
    MTd Binary Format (Type 0 / JSON Only)
    Header: [size(4, LE)] [type(1 = 0)] + JSON(UTF-8)

    변경사항:
    - s.shutdown(SHUT_WR) 제거 (서버가 추가 수신 시도할 때 EOF(0바이트)로 오인 방지)
    - recv 헤더/바디를 타임아웃 내에서 끝까지 읽는 루프 도입
    - 디버그 아티팩트(보낸/받은) 그대로 기록
    """
    tag = trace_tag or _now_tag()

    json_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = struct.pack("<IB", len(json_bytes), 0)
    packet = header + json_bytes

    hdr = b""; body = b""
    try:
        if debug:
            (TRACE_DIR / f"mtd_{tag}_send.json").write_text(
                json.dumps({"host": host, "port": port, "json": payload}, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            (TRACE_DIR / f"mtd_{tag}_send.bin").write_bytes(packet)

        with socket.create_connection((host, port), timeout=timeout) as s:
            # 중요: 서버가 클라이언트 half-close(WR) 를 EOF로 오인하지 않도록 shutdown 호출 제거
            s.settimeout(timeout)
            s.sendall(packet)

            # ── 헤더(5B) 수신: size(4) + type(1)
            deadline = time.time() + timeout
            while len(hdr) < 5:
                if time.time() >= deadline:
                    raise MtdTraceError("timeout while reading header", tag)
                chunk = s.recv(5 - len(hdr))
                if not chunk:
                    # 서버가 먼저 닫음
                    break
                hdr += chunk

            if len(hdr) < 5:
                raise MtdTraceError("empty response or short header", tag)

            size, typ = struct.unpack("<IB", hdr)

            # ── 바디 수신
            while len(body) < size:
                if time.time() >= deadline:
                    raise MtdTraceError("timeout while reading body", tag)
                chunk = s.recv(size - len(body))
                if not chunk:
                    break
                body += chunk

        if debug:
            (TRACE_DIR / f"mtd_{tag}_recv_hdr.bin").write_bytes(hdr)
            (TRACE_DIR / f"mtd_{tag}_recv_body.bin").write_bytes(body)

    except MtdTraceError:
        raise
    except Exception as e:
        (TRACE_DIR / f"mtd_{tag}_error.txt").write_text(
            f"{repr(e)}\nrecv_hdr={len(hdr)}B, recv_body={len(body)}B\n",
            encoding="utf-8"
        )
        raise MtdTraceError(str(e), tag)

    # ── 파싱
    size, typ = struct.unpack("<IB", hdr)
    if typ == 0:
        resp = json.loads(body.decode("utf-8", errors="replace"))
        return resp, tag
    elif typ == 1:
        if len(body) < 16:
            raise MtdTraceError("invalid type-1 body", tag)
        jlen, blen, _, _ = struct.unpack("<IIII", body[:16])
        j = body[16:16+jlen]
        resp = json.loads(j.decode("utf-8", errors="replace"))
        resp["__binary_size__"] = blen
        return resp, tag
    else:
        raise MtdTraceError(f"unexpected resp type={typ}", tag)
