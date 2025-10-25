# ─────────────────────────────────────────────────────────────────────────────#
# fd_audio_frame_sync.py
# - 2025/10/11
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

# -*- coding: utf-8 -*-
'''
Audio frame-accurate join pipeline for per-second clips (29.97fps etc.)
- Normalize each 1s audio chunk to exactly 1.000000s (WAV intermediate)
- Concatenate via ffconcat and encode ONCE to AAC (gapless)
- Lock total duration to TOTAL_FRAMES / FPS (atrim)
- Mux with frame-joined video safely

Tested on Windows paths; quotes are added in ffconcat.
'''

import os
import shlex
import uuid
import json
import math
import tempfile
import subprocess
from decimal import Decimal, getcontext
from typing import List, Tuple, Optional
from pathlib import PureWindowsPath

# ------------------------------
# Utilities
# ------------------------------

def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    '''Run a subprocess and raise on error with readable message.'''
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\nCMD: {' '.join(shlex.quote(c) for c in cmd)}\nSTDERR:\n{p.stderr}")
    return p

def _ffprobe_json(args: list[str]) -> dict:
    '''Call ffprobe and return JSON.'''
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", *args]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(p.stderr or "ffprobe failed")
    return json.loads(p.stdout or "{}")

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def _tmpfile(suffix: str) -> str:
    fd, p = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return p


# ------------------------------
# Step 1: Normalize each 1s clip to EXACT 1.000000s (WAV)
# ------------------------------

def normalize_1s_to_wav(
    in_media: str,
    out_wav: str,
    ar: int = 48000,
    ac: int = 2
) -> None:
    '''
    Extract audio from (per-second) media and force exact 1.000000s length.
    - decode → trim to exactly 1.0s → set PTS start to 0 → write PCM
    '''
    _ensure_dir(os.path.dirname(out_wav) or ".")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", in_media,
        "-vn",
        "-af", "atrim=0:1.0,asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a", "pcm_s16le",
        out_wav
    ]
    _run(cmd, check=True)


# ------------------------------
# Step 2: Build ffconcat from normalized WAVs and encode ONCE to AAC
# ------------------------------

def build_ffconcat_from_wavs(wav_paths: List[str]) -> str:
    '''
    Create an ffconcat file with duration 1.000000 for every WAV entry.
    '''
    concat_txt = _tmpfile(".ffconcat")
    lines = ["ffconcat version 1.0"]
    for p in wav_paths:
        # Windows path quoting
        lines.append("duration 1.000000")
        lines.append(f"file '{p}'")
    with open(concat_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return concat_txt

def concat_encode_aac(
    ffconcat_txt: str,
    out_m4a: str,
    ar: int = 48000,
    ac: int = 2,
    a_bitrate: str = "128k"
) -> None:
    '''
    Concatenate PCM via ffconcat and encode ONCE to AAC (gapless-friendly).
    '''
    _ensure_dir(os.path.dirname(out_m4a) or ".")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", ffconcat_txt,
        "-vn",
        "-af", "asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a", "aac", "-b:a", a_bitrate,
        "-movflags", "+faststart",
        out_m4a
    ]
    _run(cmd, check=True)


# ------------------------------
# Step 3: Lock final audio duration to TOTAL_FRAMES / FPS
# ------------------------------

def lock_audio_duration_to_video(
    in_audio: str,
    total_frames: int,
    fps: float,
    out_audio: str,
    ar: int = 48000,
    ac: int = 2,
    a_bitrate: str = "128k"
) -> float:
    '''
    Trim/pad final audio so that its duration equals total_frames/fps exactly.
    Returns the locked duration (float seconds).
    '''
    if total_frames <= 0 or fps <= 0:
        raise ValueError("total_frames and fps must be positive")

    # precise decimal to minimize floating-point rounding
    getcontext().prec = 28
    target = float(Decimal(int(total_frames)) / Decimal(str(fps)))

    _ensure_dir(os.path.dirname(out_audio) or ".")
    # apad for safety (in case shorter), then trim exact
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", in_audio,
        "-vn",
        "-af", f"apad=pad_dur=3600,atrim=duration={target:.9f},asetpts=PTS-STARTPTS",
        "-ar", str(int(ar)), "-ac", str(int(ac)),
        "-c:a", "aac", "-b:a", a_bitrate,
        "-movflags", "+faststart",
        out_audio
    ]
    _run(cmd, check=True)
    return target


# ------------------------------
# Step 4: Safe mux with frame-joined video
# ------------------------------

def mux_video_audio(
    video_only: str,
    audio_locked: str,
    out_mp4: str,
    copy_video: bool = True,
    copy_audio: bool = True
) -> None:
    '''
    Mux final audio with video. Use genpts/make_zero, shortest for safety.
    '''
    _ensure_dir(os.path.dirname(out_mp4) or ".")
    v_copy = ["-c:v", "copy"] if copy_video else ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
    a_copy = ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "128k"]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-i", video_only, "-i", audio_locked,
        "-map", "0:v:0", "-map", "1:a:0",
        *v_copy, *a_copy,
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-shortest",
        "-movflags", "+faststart",
        out_mp4
    ]
    _run(cmd, check=True)


def _to_ffconcat_path(p: str) -> str:
    '''
    ffconcat 전용 경로 정규화:
    - UNC: '\\\\host\\share\\...'  -> '//host/share/...'
    - 일반: 'C:\\path\\to\\file'   -> 'C:/path/to/file'
    - 혼용 방지: 모든 백슬래시를 슬래시로 통일
    '''
    if not p:
        return p
    # PureWindowsPath로 표준화 후 슬래시 통일
    p_win = str(PureWindowsPath(p))
    # UNC 처리
    if p_win.startswith("\\\\"):
        p_posix = "//" + p_win[2:].replace("\\", "/")
    else:
        p_posix = p_win.replace("\\", "/")
    return p_posix


# ------------------------------
# High-level: End-to-end builder
# ------------------------------

def build_audio_from_1s_clips_and_mux(
    per_second_media_files: List[str],     # e.g., ["...1030.mp4", "...1031.mp4", ...]
    video_joined_path: str,               # frame-joined video-only mp4 (no/any audio)
    total_frames: int,                    # exact total frames of the joined video
    fps: float,                           # e.g., 30000/1001
    work_dir: str,                        # temp/intermediate/output folder
    out_audio_joined: Optional[str] = None,
    out_audio_locked: Optional[str] = None,
    out_final_mp4: Optional[str] = None,
    ar: int = 48000,
    ac: int = 2,
    a_bitrate: str = "128k"
) -> Tuple[str, str, str]:
    '''
    Full pipeline:
    1) For each 1s media, make WAV fixed to exactly 1.000000s
    2) Concat via ffconcat, encode ONCE to AAC (.m4a)
    3) Lock to total_frames/fps
    4) Mux with video

    Returns: (joined_audio_m4a, locked_audio_m4a, final_mp4)
    '''
    _ensure_dir(work_dir)

    # 1) Normalize each 1s media → wav
    wavs: List[str] = []
    for i, m in enumerate(per_second_media_files):
        wav_out = os.path.join(work_dir, f"norm_{i:05d}.wav")
        normalize_1s_to_wav(m, wav_out, ar=ar, ac=ac)
        wavs.append(wav_out)

    # 2) ffconcat + single encode to AAC
    concat_txt = build_ffconcat_from_wavs(wavs)
    try:
        if out_audio_joined is None:
            out_audio_joined = os.path.join(work_dir, "audio_joined.m4a")
        concat_encode_aac(concat_txt, out_audio_joined, ar=ar, ac=ac, a_bitrate=a_bitrate)
    finally:
        try:
            os.remove(concat_txt)
        except Exception:
            pass

    # 3) Lock final audio to exact video length (TOTAL_FRAMES / FPS)
    if out_audio_locked is None:
        out_audio_locked = os.path.join(work_dir, "audio_locked.m4a")
    locked_duration = lock_audio_duration_to_video(
        out_audio_joined, total_frames, fps, out_audio_locked,
        ar=ar, ac=ac, a_bitrate=a_bitrate
    )

    # 4) Mux with video
    if out_final_mp4 is None:
        out_final_mp4 = os.path.join(work_dir, "final_muxed.mp4")
    mux_video_audio(video_joined_path, out_audio_locked, out_final_mp4, copy_video=True, copy_audio=True)

    return out_audio_joined, out_audio_locked, out_final_mp4


# ------------------------------
# (Optional) Validators
# ------------------------------

def probe_audio_duration_sec(path: str) -> float:
    '''Return audio stream duration from ffprobe (fallback to container).'''
    j = _ffprobe_json(["-show_format", "-show_streams", path])
    fmt_d = float(j.get("format", {}).get("duration") or "nan")
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    if a and a.get("duration"):
        return float(a["duration"])
    return fmt_d

def probe_pkt_span_sec(path: str) -> float:
    '''Span between first and last audio packet pts_time (more robust than container).'''
    j = _ffprobe_json(["-select_streams", "a:0", "-show_packets", "-of", "json", path])
    pkts = j.get("packets", [])
    if not pkts:
        return float("nan")
    try:
        s = float(pkts[0]["pts_time"])
        e = float(pkts[-1]["pts_time"])
        return e - s
    except Exception:
        return float("nan")
