# -*- coding: utf-8 -*-
"""
FFmpeg 디코드 → ABGR raw 파이프 → PyNvVideoCodec NVENC(GPU) → ES → MP4
- stderr=DEVNULL, -nostdin (파이프 데드락 방지)
- 프레임 추출 시 memoryview 사용 금지 → bytes로 복사 후 buf 슬라이스 삭제
"""

import os, sys, subprocess, tempfile, time, traceback
import PyNvVideoCodec as nvc

# ===== 설정 =====
INPUT        = r"./videos/input/sample/test.mp4"
OUTPUT_MP4   = r"./out.mp4"
CODEC_NAME   = "h264"        # "hevc" 가능
BITRATE      = 12_000_000    # bps
QP_OR_CRF    = 28
FPS_FALLBACK = 30.0

def log(*a): print(*a, flush=True)

def probe_meta(path):
    def _get(key):
        try:
            out = subprocess.check_output(
                ["ffprobe","-v","error","-select_streams","v:0",
                 "-show_entries","stream="+key,"-of","csv=p=0", path]
            ).decode().strip()
            return out
        except Exception:
            return ""
    fps_s = _get("avg_frame_rate")
    if "/" in fps_s:
        try:
            n, d = fps_s.split("/")
            fps = float(n)/float(d) if float(d)>0 else FPS_FALLBACK
        except Exception:
            fps = FPS_FALLBACK
    else:
        try:
            fps = float(fps_s) if fps_s else FPS_FALLBACK
        except Exception:
            fps = FPS_FALLBACK
    def _toi(x, dv):
        try: return int(x)
        except: return dv
    W = _toi(_get("width"),  0)
    H = _toi(_get("height"), 0)
    if W<=0 or H<=0: raise RuntimeError("ffprobe로 해상도 확인 실패")
    W &= ~1; H &= ~1
    return W, H, (fps if fps>0 else FPS_FALLBACK)

def make_encoder_abgr(w, h, fps):
    return nvc.CreateEncoder(
        int(w), int(h), "ABGR", True,   # CPU 입력
        codec=CODEC_NAME,
        fps=int(round(fps)),
        bitrate=int(BITRATE),
        gop=max(1, int(round(fps*2))),
        constqp=int(QP_OR_CRF)
    )

def mux_es_to_mp4(es_path, out_mp4, fps):
    if not os.path.exists(es_path) or os.path.getsize(es_path) == 0:
        raise RuntimeError("ES 파일이 비어 있습니다 (인코딩 출력 없음).")
    subprocess.run([
        "ffmpeg","-y","-hide_banner","-loglevel","error",
        "-r", str(int(round(fps))), "-i", es_path, "-c","copy", out_mp4
    ], check=True)

def main():
    import numpy as np

    assert os.path.isfile(INPUT), f"Not found: {INPUT}"
    W, H, fps = probe_meta(INPUT)
    frame_bytes = W * H * 4  # ABGR

    ff_cmd = [
        "ffmpeg","-nostdin","-hide_banner","-loglevel","error",
        "-i", INPUT,
        "-vf", f"scale={W}:{H}:flags=bicubic",
        "-pix_fmt", "abgr",
        "-f", "rawvideo",
        "pipe:1"
    ]
    proc = subprocess.Popen(
        ff_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )

    raw_es = tempfile.mktemp(suffix=".h264" if CODEC_NAME.lower()=="h264" else ".h265")
    enc = make_encoder_abgr(W, H, fps)
    frames = 0
    t0 = time.perf_counter()

    with open(raw_es, "wb") as es_fh:
        log(f"[ENC] ABGR {W}x{H}, fps≈{fps:.2f}, codec={CODEC_NAME}, bitrate={BITRATE}")
        buf = bytearray()
        read_size = frame_bytes * 16

        while True:
            chunk = proc.stdout.read(read_size)
            if not chunk:
                if proc.poll() is not None:
                    break
                time.sleep(0.005)
                continue

            buf += chunk
            while len(buf) >= frame_bytes:
                # ⚠️ memoryview로 잡으면 buf를 resize 할 수 없으므로 bytes로 복사
                frame_bytes_copy = bytes(buf[:frame_bytes])
                del buf[:frame_bytes]

                # ndarray(H,W,4,uint8)로 직접 인코드 (이 빌드에서 가장 호환성 좋음)
                arr = np.frombuffer(frame_bytes_copy, dtype=np.uint8).reshape((H, W, 4))
                try:
                    bs = enc.Encode(arr)
                except Exception:
                    # 비상: ndarray가 안 먹히면 bytearray로
                    bs = enc.Encode(bytearray(frame_bytes_copy))
                if bs:
                    es_fh.write(bs)
                frames += 1

        tail = enc.EndEncode()
        if tail:
            es_fh.write(tail)

    try:
        if proc.stdout and not proc.stdout.closed:
            proc.stdout.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()

    ms = (time.perf_counter() - t0) * 1000.0
    log(f"✅ NVENC OK. frames={frames}, time={ms:.0f} ms → {raw_es}")

    if frames == 0:
        raise RuntimeError("ffmpeg 파이프에서 프레임을 읽지 못했습니다 (frames=0). 입력/코덱/필터 확인 필요.")

    mux_es_to_mp4(raw_es, OUTPUT_MP4, fps)
    try: os.remove(raw_es)
    except: pass
    log("🎉 DONE:", OUTPUT_MP4)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("ERROR:", e)
        log(traceback.format_exc())
        sys.exit(1)
