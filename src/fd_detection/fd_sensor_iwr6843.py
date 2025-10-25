# ─────────────────────────────────────────────────────────────────────────────#
# /A/I/ /D/E/T/E/C/T/I/O/N/
# fd_sensor_iwr6843  (FULL, velocity & angles from cfg + DetPoints)
# - 2025/09/02
# - Hongsu Jung
# ─────────────────────────────────────────────────────────────────────────────#

import struct, math
import serial, time, threading, sys, os, csv
from datetime import datetime

# ====== User Config ======
CLI_COM  = "COM5"   # Enhanced COM (CLI)
DATA_COM = "COM6"   # Standard COM (DATA)
CFG_PATH = r"./aid/fd_detection/fd_sensor_iwr6843.cfg"

CLI_BAUD  = 115200
DATA_BAUD = 921600

# CSV logging options
WRITE_CSV = True
CSV_PATH  = "./aid/fd_detection/detobj_log.csv"
CSV_MAX_ROWS = 200000

# ====== Print options ======
PRINT_POINTS_PER_FRAME = 5   # Max number of points to print per frame
PRINT_DECIMALS = 3           # Decimal places for printed floats

# ====== Constants ======
MAGIC = b'\x02\x01\x04\x03\x06\x05\x08\x07'
UART_HDR_LEN = 40  # SDK 3.x mmwDemo UART Header v1 = 40B

# TLV Type IDs (SDK 3.x mmwDemo)
TLV_DETOBJ        = 1
TLV_RANGEPROFILE  = 2
TLV_NOISEPROFILE  = 3
TLV_AZIMUTH_HEAT  = 4
TLV_RANGE_DOPPLER = 5
TLV_STATS         = 6
TLV_SIDEINFO      = 7

# ====== Serial helpers ======
def open_port(port, baud):
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.bytesize = serial.EIGHTBITS
    ser.parity   = serial.PARITY_NONE
    ser.stopbits = serial.STOPBITS_ONE
    ser.timeout  = 0.1
    ser.write_timeout = 5.0
    ser.xonxoff = False
    ser.rtscts  = False
    ser.dsrdtr  = False
    ser.open()
    try:
        ser.setDTR(False)
        ser.setRTS(False)
    except Exception:
        pass
    return ser

def _read_until_tokens(ser, tokens=(b"Done", b"Error", b"Ignored", b"mmwDemo:/>"), overall_timeout=5.0):
    t0 = time.time()
    buf = b""
    while time.time() - t0 < overall_timeout:
        n = ser.in_waiting
        chunk = ser.read(n if n else 1)
        if chunk:
            buf += chunk
            if any(tok in buf for tok in tokens):
                break
        else:
            time.sleep(0.01)
    return buf

def send_cli(ser, cmd, wait_done=True, overall_timeout=5.0):
    line = (cmd.strip() + '\r\n').encode('ascii', errors='ignore')
    try:
        ser.reset_output_buffer()
    except Exception:
        pass

    try:
        ser.write(line)
        ser.flush()
    except serial.SerialTimeoutException:
        time.sleep(0.05)
        ser.reset_output_buffer()
        ser.write(line)
        ser.flush()

    if not wait_done:
        time.sleep(0.1)
        return b""
    return _read_until_tokens(ser, overall_timeout=overall_timeout)

def load_cfg_lines(path):
    if not os.path.exists(path):
        print(f"[ERR] cfg not found: {path}")
        sys.exit(1)
    with open(path, 'r', encoding='utf-8-sig', errors='strict') as f:
        raw_lines = f.readlines()

    lines = []
    for ln in raw_lines:
        ln = ln.split('%')[0].split(';')[0].split('#')[0]
        ln = ln.strip()
        if not ln:
            continue
        # ASCII only
        ln = ln.encode('ascii', 'ignore').decode('ascii')
        if ln:
            lines.append(ln)
    return lines

# ====== Physics from cfg ======
def parse_cfg_for_doppler(cfg_lines):
    '''
    Compute vRes and vMax:
      vRes = lambda / (2 * numLoops * numTx * Tc)
      vMax = lambda / (4 * numTx * Tc)
      Tc   = idleTime + rampEndTime   (seconds)
    '''
    startFreqGHz = None
    idle_us = None
    ramp_us = None
    numLoops = None
    numTx = None

    tx_mask = None

    for ln in cfg_lines:
        t = ln.strip().split()
        if not t:
            continue
        k = t[0].lower()
        if k == 'profilecfg':
            # [0]=profileCfg, [1]=profileId, [2]=startFreqGHz, [3]=idle(us), [4]=adcStart(us), [5]=rampEnd(us)
            if len(t) >= 6:
                startFreqGHz = float(t[2])
                idle_us = float(t[3])
                ramp_us = float(t[5])
        elif k == 'framecfg':
            # frameCfg chirpStart chirpEnd numLoops ...
            if len(t) >= 4:
                numLoops = int(t[3])
        elif k == 'channelcfg':
            # channelCfg rxMask txMask ...
            if len(t) >= 3:
                tx_mask = int(t[2])

    if tx_mask is None:
        numTx = 1
    else:
        numTx = bin(tx_mask).count('1')
        if numTx == 0: numTx = 1

    # Fallbacks to avoid crashes if some fields are missing
    if None in (startFreqGHz, idle_us, ramp_us, numLoops):
        startFreqGHz = startFreqGHz or 60.0
        idle_us = idle_us or 7.0
        ramp_us = ramp_us or 57.0
        numLoops = numLoops or 64

    c = 3.0e8
    fc = startFreqGHz * 1e9
    lam = c / fc
    Tc = (idle_us + ramp_us) * 1e-6

    vRes = lam / (2.0 * numLoops * numTx * Tc)
    vMax = lam / (4.0 * numTx * Tc)
    return vRes, vMax

# ====== TLV parsing ======
def parse_uart_header_v1(hdr40: bytes):
    return {
        "version":         int.from_bytes(hdr40[8:12],  'little', signed=False),
        "totalPacketLen":  int.from_bytes(hdr40[12:16], 'little', signed=False),
        "platform":        int.from_bytes(hdr40[16:20], 'little', signed=False),
        "frameNumber":     int.from_bytes(hdr40[20:24], 'little', signed=False),
        "timeCpuCycles":   int.from_bytes(hdr40[24:28], 'little', signed=False),
        "numDetectedObj":  int.from_bytes(hdr40[28:32], 'little', signed=False),
        "numTLVs":         int.from_bytes(hdr40[32:36], 'little', signed=False),
        "subFrameNumber":  int.from_bytes(hdr40[36:40], 'little', signed=False),
    }

def parse_detected_points(payload_bytes, vRes):
    '''
    SDK 3.x Detected Points TLV layout (typical build):
      [0:8]  dataObjDescr:
             numObj (u32), xyzQFormat (u32)
      [8: ]  points[]: per point 12B
             <HhHhhh> = rangeIdx, dopplerIdx, peakVal, x, y, z
             (all int16 except peak & rangeIdx)
    Compute heading/elevation and velocity:
      vel[m/s] = dopplerIdx * vRes
      az[deg]  = atan2(y, x)
      el[deg]  = atan2(z, sqrt(x^2 + y^2))
      range[m] = sqrt(x^2 + y^2 + z^2)
    Coordinates (x,y,z) use Q-format → scale = 2^-xyzQ
    '''
    if len(payload_bytes) < 8:
        return [], 0, 0

    numObj, xyzQ = struct.unpack_from('<II', payload_bytes, 0)
    scale = 2.0 ** (-xyzQ)
    offs = 8
    out = []
    for _ in range(numObj):
        if offs + 12 > len(payload_bytes):
            break
        rIdx, dIdx, peak, x, y, z = struct.unpack_from('<HhHhhh', payload_bytes, offs)
        offs += 12
        x_m = x * scale; y_m = y * scale; z_m = z * scale
        vel = dIdx * vRes
        az  = math.degrees(math.atan2(y_m, x_m))
        el  = math.degrees(math.atan2(z_m, math.hypot(x_m, y_m)))
        rng = math.sqrt(x_m*x_m + y_m*y_m + z_m*z_m)
        out.append({
            "rIdx": rIdx, "dIdx": dIdx, "peak": peak,
            "x": x_m, "y": y_m, "z": z_m,
            "range": rng, "vel": vel, "az": az, "el": el
        })
    return out, numObj, xyzQ

def parse_detpoints_auto(payload_bytes: bytes, vRes: float, numObj_from_hdr: int):
    '''
    Auto-detect parser for Detected Points TLV.
    Try descriptor format first ([numObj, xyzQ] + 12B/pt). If that fails,
    fallback to legacy format (points only, 12B or 16B).
    Returns: (points: list[dict], num_points: int)
    '''
    pts = []

    # (A) Try descriptor format: [numObj(u32), xyzQ(u32)] + points(12B each)
    if len(payload_bytes) >= 8:
        numObj_desc, xyzQ = struct.unpack_from('<II', payload_bytes, 0)
        remain = len(payload_bytes) - 8
        if numObj_desc >= 0 and remain >= 0 and (remain % 12 == 0) and (remain // 12 == numObj_desc):
            scale = 2.0 ** (-xyzQ)
            offs = 8
            for _ in range(numObj_desc):
                rIdx, dIdx, peak, x, y, z = struct.unpack_from('<HhHhhh', payload_bytes, offs)
                offs += 12
                x_m = x * scale; y_m = y * scale; z_m = z * scale
                vel = dIdx * vRes
                az  = math.degrees(math.atan2(y_m, x_m))
                el  = math.degrees(math.atan2(z_m, math.hypot(x_m, y_m)))
                rng = math.sqrt(x_m*x_m + y_m*y_m + z_m*z_m)
                pts.append({"rIdx":rIdx,"dIdx":dIdx,"peak":peak,"x":x_m,"y":y_m,"z":z_m,
                            "range":rng,"vel":vel,"az":az,"el":el})
            return pts, numObj_desc

    # (B) Legacy: points only (12B or 16B/pt). Use hdr.numDetectedObj as a hint.
    n = len(payload_bytes)
    size = None
    if n % 12 == 0:
        size = 12
    elif n % 16 == 0:
        size = 16

    if size is not None:
        # No xyzQ available → conservative guess 2^-7 (many demos use Q7/Q8).
        # Angles/velocity are not very sensitive to this scale.
        xyzQ_guess = 7
        scale = 2.0 ** (-xyzQ_guess)

        pts_tmp = []
        offs = 0
        while offs + size <= n:
            p = payload_bytes[offs:offs+size]
            offs += size
            rIdx   = int.from_bytes(p[0:2], 'little', signed=False)
            dIdx   = int.from_bytes(p[2:4], 'little', signed=True)
            peak   = int.from_bytes(p[4:6], 'little', signed=False)
            x_raw  = int.from_bytes(p[6:8], 'little', signed=True)
            y_raw  = int.from_bytes(p[8:10],'little', signed=True)
            z_raw  = int.from_bytes(p[10:12],'little', signed=True)
            x_m = x_raw * scale; y_m = y_raw * scale; z_m = z_raw * scale
            vel = dIdx * vRes
            az  = math.degrees(math.atan2(y_m, x_m))
            el  = math.degrees(math.atan2(z_m, math.hypot(x_m, y_m)))
            rng = math.sqrt(x_m*x_m + y_m*y_m + z_m*z_m)
            pts_tmp.append({"rIdx":rIdx,"dIdx":dIdx,"peak":peak,"x":x_m,"y":y_m,"z":z_m,
                            "range":rng,"vel":vel,"az":az,"el":el})

        # If hdr.numDetectedObj looks valid, trust it; else use all parsed points
        if numObj_from_hdr and numObj_from_hdr <= len(pts_tmp):
            return pts_tmp[:numObj_from_hdr], numObj_from_hdr
        else:
            return pts_tmp, len(pts_tmp)

    # Unknown/invalid format → empty
    return [], 0


def try_parse_sideinfo(payload: bytes, num_points: int):
    expect = num_points * 4
    if len(payload) != expect:
        return None
    out = []
    for i in range(0, len(payload), 4):
        snr   = int.from_bytes(payload[i:i+2], 'little', signed=True)
        noise = int.from_bytes(payload[i+2:i+4], 'little', signed=True)
        out.append({"snr":snr, "noise":noise})
    return out

def parse_stats(payload: bytes):
    vals = []
    for i in range(0, len(payload), 4):
        if i+4 <= len(payload):
            vals.append(int.from_bytes(payload[i:i+4], 'little', signed=False))
    return vals

# ====== Data stream reader ======
def stream_reader(port_name, baud, vRes):
    '''
    DATA port auto-reconnect + TLV parsing + CSV logging.
    vRes is pre-computed from cfg and passed in.
    '''
    print("[DATA] reader starting with auto-reconnect")

    ser = None
    first_bytes_deadline = None
    buf = b""

    # ---- CSV local state ----
    write_csv_enabled = WRITE_CSV
    csv_fp = None
    csv_writer = None
    csv_rows = 0
    if write_csv_enabled:
        os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
        csv_fp = open(CSV_PATH, "w", newline="", encoding="utf-8")
        csv_writer = csv.writer(csv_fp)
        csv_writer.writerow([
            "ts","frame","subf","numObj","idx",
            "rIdx","dIdx","peak","x","y","z",
            "range","vel","az","el","snr","noise"
        ])

    def _open_once():
        s = serial.Serial()
        s.port = port_name
        s.baudrate = baud
        s.bytesize = serial.EIGHTBITS
        s.parity   = serial.PARITY_NONE
        s.stopbits = serial.STOPBITS_ONE
        s.timeout  = 0.05
        s.write_timeout = 2.0
        s.xonxoff = False
        s.rtscts  = False
        s.dsrdtr  = False
        s.open()
        try:
            s.setDTR(False); s.setRTS(False)
        except Exception:
            pass
        return s

    try:
        while True:
            try:
                if ser is None or not ser.is_open:
                    while True:
                        try:
                            ser = _open_once()
                            print(f"[DATA] opened {port_name} @ {baud}")
                            buf = b""
                            first_bytes_deadline = time.time() + 3.0
                            break
                        except Exception as e:
                            print(f"[DATA][WARN] open failed ({e!r}), retry in 0.5s")
                            time.sleep(0.5)

                chunk = ser.read(4096)
                if chunk:
                    first_bytes_deadline = None
                    buf += chunk

                    # Parse only when we have a full packet
                    while True:
                        idx = buf.find(MAGIC)
                        if idx < 0:
                            if len(buf) > 16384:
                                buf = buf[-8192:]
                            break
                        if len(buf) < idx + UART_HDR_LEN:
                            break

                        header = buf[idx:idx+UART_HDR_LEN]
                        total_len = int.from_bytes(header[12:16], 'little', signed=False)
                        if len(buf) < idx + total_len:
                            break

                        pkt = buf[idx:idx+total_len]
                        buf = buf[idx+total_len:]

                        hdr = parse_uart_header_v1(header)
                        num_tlv = hdr["numTLVs"]
                        subf    = hdr["subFrameNumber"]
                        frameNo = hdr["frameNumber"]

                        offset = UART_HDR_LEN
                        det_points = []
                        side_info  = None

                        for _ in range(num_tlv):
                            if len(pkt) < offset + 8:
                                break
                            tlv_type = int.from_bytes(pkt[offset:offset+4], 'little', signed=False)
                            tlv_len  = int.from_bytes(pkt[offset+4:offset+8], 'little', signed=False)
                            start = offset + 8
                            end   = offset + tlv_len
                            if len(pkt) < end:
                                break
                            payload = pkt[start:end]

                            if tlv_type == TLV_DETOBJ:
                                # Use auto-detect parser; also give hdr.numDetectedObj to handle legacy format
                                det_points, _npts = parse_detpoints_auto(payload, vRes, hdr["numDetectedObj"])
                            elif tlv_type == TLV_SIDEINFO:
                                npts = len(det_points)
                                side_info = try_parse_sideinfo(payload, npts)
                            # Add more TLVs if needed
                            offset = end

                        # --- Console output (with velocity/angles) ---
                        npts = len(det_points)
                        if npts == 0:
                            print(f"[DATA] frame={frameNo} subf={subf} tlv={num_tlv} detObj=0")
                        else:
                            vel_min = min(p["vel"] for p in det_points)
                            vel_max = max(p["vel"] for p in det_points)
                            print(
                                f"[DATA] frame={frameNo} subf={subf} tlv={num_tlv} detObj={npts} "
                                f"vel[min/max]={vel_min:.{PRINT_DECIMALS}f}/{vel_max:.{PRINT_DECIMALS}f} m/s"
                            )

                            # Print a subset of points per frame
                            for i, p in enumerate(det_points[:PRINT_POINTS_PER_FRAME]):
                                print(
                                    "[DATA]  idx={i} "
                                    f"rng={p['range']:.{PRINT_DECIMALS}f} m "
                                    f"vel={p['vel']:.{PRINT_DECIMALS}f} m/s "
                                    f"az={p['az']:.{PRINT_DECIMALS}f}° "
                                    f"el={p['el']:.{PRINT_DECIMALS}f}° "
                                    f"peak={p['peak']} rIdx={p['rIdx']} dIdx={p['dIdx']}"
                                )
                            if npts > PRINT_POINTS_PER_FRAME:
                                print(f"[DATA]  ... (+{npts-PRINT_POINTS_PER_FRAME} more points)")

                        # CSV logging
                        if write_csv_enabled and det_points and csv_writer:
                            ts = datetime.now().isoformat(timespec='milliseconds')
                            for idxp, p in enumerate(det_points):
                                snr=noise=None
                                if side_info and idxp < len(side_info):
                                    snr  = side_info[idxp]["snr"]
                                    noise= side_info[idxp]["noise"]
                                csv_writer.writerow([
                                    ts, frameNo, subf, len(det_points), idxp,
                                    p["rIdx"], p["dIdx"], p["peak"], p["x"], p["y"], p["z"],
                                    p["range"], p["vel"], p["az"], p["el"], snr, noise
                                ])
                                csv_rows += 1
                                if (csv_rows % 2000) == 0:
                                    try: csv_fp.flush()
                                    except Exception: pass
                                if csv_rows >= CSV_MAX_ROWS:
                                    print(f"[CSV] reached {CSV_MAX_ROWS} rows, stop logging further.")
                                    write_csv_enabled = False

                else:
                    if first_bytes_deadline and time.time() > first_bytes_deadline:
                        print("[DATA][WARN] no bytes yet… check cfg/sensorStart/firmware.")
                        first_bytes_deadline = None
                    time.sleep(0.001)

            except Exception as e:
                try:
                    if ser: ser.close()
                except Exception:
                    pass
                print(f"[DATA][WARN] read error ({e!r}), reconnecting…")
                time.sleep(0.5)
                ser = None

    finally:
        if csv_fp:
            try:
                csv_fp.flush(); csv_fp.close()
            except Exception:
                pass

# ====== Main ======
def main():
    # 1) CLI
    cli = open_port(CLI_COM, CLI_BAUD)
    print(f"[OK] Opened CLI {CLI_COM} @ {CLI_BAUD}, DATA {DATA_COM} @ {DATA_BAUD}")

    # 2) version
    resp = send_cli(cli, "version", wait_done=True, overall_timeout=3.0)
    print("[CLI] version resp:", resp.decode(errors='ignore').strip())

    # 3) stop/flush
    for cmd in ["sensorStop", "flushCfg"]:
        out = send_cli(cli, cmd, wait_done=True, overall_timeout=3.0)
        print(f"[CLI] {cmd} ->", out.decode(errors='ignore').strip())

    # 4) load & send cfg
    lines = load_cfg_lines(CFG_PATH)
    has_sensorstart = any(l.strip().lower().startswith("sensorstart") for l in lines)
    print(f"[CLI] sending cfg ({len(lines)} lines) ...")
    for ln in lines:
        out = send_cli(cli, ln, wait_done=True, overall_timeout=5.0)
        txt = out.decode(errors='ignore').strip()
        print(f"[CLI] {ln} -> {txt}")
        if "Error" in txt:
            print("[CLI][FATAL] Error occurred above. Abort.")
            cli.close(); sys.exit(1)
        time.sleep(0.005)

    # 4.5) compute vRes/vMax from cfg
    vRes, vMax = parse_cfg_for_doppler(lines)
    print(f"[PHY] vRes={vRes:.4f} m/s, vMax≈{vMax:.2f} m/s")

    # 5) sensorStart (send once if not in cfg)
    if not has_sensorstart:
        try:
            send_cli(cli, "sensorStart", wait_done=False)
            print("[CLI] sensorStart -> sent (no wait)")
        except serial.SerialTimeoutException:
            print("[CLI][WARN] sensorStart write timeout ignored (likely already running)")

    # 6) start DATA reader
    time.sleep(1.0)  # give CDC time to enumerate
    t = threading.Thread(target=stream_reader, args=(DATA_COM, DATA_BAUD, vRes), daemon=True)
    t.start()

    # 7) keep alive
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            send_cli(cli, "sensorStop", wait_done=True, overall_timeout=3.0)
        except Exception:
            pass
        try:
            cli.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
