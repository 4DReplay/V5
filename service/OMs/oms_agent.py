# ─────────────────────────────────────────────────────────────
# oms_agent.py
# BaseHTTPRequestHandler backend
#
# --- how to read log >>> Powershell
# >> Get-Content "C:\4DReplay\V5\daemon\OMs\log\2025-11-20.log" -Wait -Tail 20
# ─────────────────────────────────────────────────────────────
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import copy
import http.client
import re
import json, time, threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
from copy import deepcopy
from src.fd_communication.server_mtd_connect import tcp_json_roundtrip, MtdTraceError
from live_mtx_manager import MTX
from collections import OrderedDict

# MTD 통신 충돌 방지용 전역 Lock
_mtd_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────
# shared codes/functions
# ─────────────────────────────────────────────────────────────
from oms_common import *

# ─────────────────────────────────────────────────────────────
# --- Path
# Global root paths
# ─────────────────────────────────────────────────────────────
from oms_env import *

# ─────────────────────────────────────────────────────────────
# --- State
# Global root paths
# ─────────────────────────────────────────────────────────────
from oms_state import *

# ─────────────────────────────────────────────────────────────
# ⚙️ C/O/N/F/I/G/U/R/A/T/I/O/N
# ─────────────────────────────────────────────────────────────
def load_config(p:Path)->dict:
    txt=p.read_text(encoding="utf-8")
    return json.loads(fd_strip_json5(txt))

# ─────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self, cfg:dict):

        self._log(f"")
        self._log(f"")
        self._log(f"#####################################################################")
        
        self.state = {}        
        self._config = cfg # 또는 self.config = cfg 도 같이 써도 됨                

        self.http_host = cfg.get("http_host","0.0.0.0")
        self.http_port = int(cfg.get("http_port",19777))
        self.heartbeat = float(cfg.get("heartbeat_interval_sec",2))
        self.nodes = list(cfg.get("nodes",[])) 
        try:
            user_alias = cfg.get("process_alias") or {}
            if not isinstance(user_alias, dict): user_alias = {}
        except Exception:
            user_alias = {}

        self.process_alias = {**PROCESS_ALIAS_DEFAULT, **user_alias}
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cache = {}
        self._cache_ts = {}
        self._cache_alias = {} # { node_name: { "PreSd": "Pre Storage [#1]", ... } }
        
        # ────────────────────────────────────────
        # system restart
        # ────────────────────────────────────────
        self._sys_restart = {
            "state": "idle", # idle | running | done | error
            "total": 0,
            "sent": 0,
            "done": 0,
            "fails": [],
            "message": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._restart_lock = threading.RLock()
        self._restart_post_timeout = RESTART_POST_TIMEOUT
        self._status_fetch_timeout = STATUS_FETCH_TIMEOUT
        self._restart_ready_timeout = 40.0
        self._restart_settle_sec = 20.0 # 타임아웃 뒤 사후 검증 기간
        self._restart_verify_iv = 0.5 # settle 동안 상태 확인 주기 (초)
        self._restart_poll_interval = 0.25
        self._restart_max_workers = 8
        self._restart_min_prepare_ms = 300        
        # ────────────────────────────────────────
        # system connect
        # ────────────────────────────────────────
        self._sys_connect_lock = threading.RLock() 
        self._sys_connect = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "events": [], # 최근 실행의 이벤트 요약 (원하면 유지 길이 제한)
            "started_at": 0.0,
            "updated_at": 0.0,
            "seq": 0,
        }
        self.presd_map = {}
        self.cameras = []
        self.switch_ips = {}
        fd_sys_state_load()
        self.mtd_port = int(cfg.get("mtd_port", 19765)) 
        self.mtd_ip = cfg.get("mtd_host", "127.0.0.1")
        # get daemonss ip
        self.daemon_ips = {}        
        self.scd_ip = self.mtd_ip        
        self.ccd_ip = self.mtd_ip 
        # ────────────────────────────────────────
        # cam restart
        # ────────────────────────────────────────
        self._cam_restart = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "error": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._cam_restart_lock = threading.RLock() 
        # ────────────────────────────────────────
        # cam connect
        # ────────────────────────────────────────
        self._cam_connect = {
            "state": "idle", # idle | running | done | error
            "message": "",
            "summary": {},
            "error": "",
            "started_at": 0.0,
            "updated_at": 0.0,
        }
        self._cam_connect_lock = threading.RLock() 
        fd_cam_state_load()

        self.cam_state_lock  = threading.RLock() # for state change
        self.camera_poll_locked_until = 0
        # ────────────────────────────────────────
        # record
        # ────────────────────────────────────────
        self.recording_name = ""
        self.record_start_time = 0
        self.current_adjustinfo = None

    # ────────────────────────────────────────────
    # ⚙️ C/O/M/M/O/N
    # ────────────────────────────────────────────      
    def _mtd_command(self, tag, msg, wait=7.0):
        with _mtd_lock:
            return self._mtd_command_nolock(tag, msg, wait)
    def _mtd_command_nolock(self, tag, msg, wait=7.0):
        token = msg.get("Token")
        conn = None
        try:
            fd_log.info(f"mtd:request: >>\n{msg}")
            payload = json.dumps({
                "host": self.mtd_ip,
                "port": self.mtd_port,
                "timeout": wait,
                "trace_tag": f"{tag}_{int(time.time()*1000)}",
                "message": msg,
            })
            conn = http.client.HTTPConnection("127.0.0.1", self.http_port, timeout=wait)
            conn.request("POST", "/oms/mtd-query", body=payload,
                        headers={"Content-Type": "application/json"})
            res = conn.getresponse()
            data = res.read()
            if res.status != 200:
               raise Exception(f"HTTP {res.status}")
            try:
                resp = json.loads(data.decode("utf-8", "ignore"))
            except:
                raise Exception("JSON decode failed")
            r = resp.get("response")
            if not r:
                raise Exception("Missing response field")
            # Token 검사
            if r.get("Token") != token:
                raise Exception(f"Token mismatch: {r.get('Token')} != {token}")
            fd_log.info(f"mtd:response <<\n {r}")
            return r
        except Exception as e:
            fd_log.exception(f"_mtd_command error: {e}")
            raise
        finally:
            if conn:
                try: conn.close()
                except: pass

    def _get_process_list(self):
        try:
            status = self._sys_status_core()            
            nodes = status.get("nodes", [])
        except Exception:
            return []
        proc_map = {} # pname → set(ip)
        for node in nodes:
            host_ip = node.get("host")
            st = node.get("status", {})
            executables = st.get("executables", [])
            for exe in executables:
                pname = exe.get("name")
                if not pname:
                    continue
                # 🔥 select == True 인 프로세스만 리스트 포함
                if not exe.get("select", False):
                    continue
                proc_map.setdefault(pname, set()).add(host_ip)
        # 결과 변환
        processes = []
        for pname, ips in proc_map.items():
            processes.append({
                "name": pname,
                "ips": sorted(list(ips)),
            })
        return processes
    def _get_daemon_ip(self):
        daemon_ips = {}
        try:
            plist = self._get_process_list()
        except Exception as e:
            fd_log.exception(f"_get_daemon_ip exception: {e}")
            return {}
        # ★ 케이스 1: None
        if plist is None:
            fd_log.error("process_list is None")
            return {}
        # ★ 케이스 2: dict 형태로 오는 경우 (자동 변환)
        if isinstance(plist, dict):
            fd_log.warning("[PATCH] process-list was dict; converting to list")
            new_list = []
            for name, ip in plist.items():
                if ip is None:
                    continue
                if not isinstance(ip, list):
                    ip = [ip]
                new_list.append({"name": name, "ips": ip})
            plist = new_list
        # ★ 케이스 3: list 가 아닌 경우
        if not isinstance(plist, list):
            fd_log.error(f"process_list invalid type: {type(plist)}")
            return {}
        # ★ 핵심 패치: 리스트 요소 필터링
        cleaned = []
        for proc in plist:
            if not proc:
                continue
            if not isinstance(proc, dict):
                fd_log.error(f"invalid process entry skipped: {proc}")
                continue
            cleaned.append(proc)
        plist = cleaned
        # 최종 처리
        for proc in plist:
            name = proc.get("name")
            ips = proc.get("ips") or []
            if not name:
                continue
            daemon_ips[name] = list(ips)
        fd_log.info(f"[PATCH] daemon_ips fetched = {daemon_ips}")
        return daemon_ips
    def _tagged_time(self, msg, start_time):
        s = str(msg or "").strip()
        if not s:
            return s
        time_proc = time.time() - (start_time or time.time())        
        ret_msg = f"{s} · {time_proc:.1f}s"
        return ret_msg
    def _extract_http_body(self, raw):
        # HTTP 응답 tuple 처리
        if isinstance(raw, tuple):
            # (status, headers, body)
            for item in raw:
                # body 후보: bytes 또는 str
                if isinstance(item, bytes):
                    return item
                if isinstance(item, str):
                    return item
            raise ValueError("No HTTP body found in tuple response")
        # 이미 bytes, str, 또는 dict일 수 있음
        return raw

    # ────────────────────────────────────────────
    # 🛠️ /S/Y/S/T/E/M/
    # ────────────────────────────────────────────    
    @staticmethod
    def _build_aic_list_from_status(self):
        try:
            status = self._sys_status_core()
            nodes = status.get("nodes", [])
            aic_list = {}

            for node in nodes:
                host_ip = node.get("host")
                st = node.get("status") or {}
                procs = fd_pluck_procs(st)

                # 프로세스 리스트 순회
                for p in procs:
                    if not isinstance(p, dict):
                        continue
                    if p.get("name") != "AIc":
                        continue

                    alias = p.get("alias")
                    if not alias:
                        continue

                    aic_list[alias] = host_ip

            return aic_list
        except Exception as e:
            fd_log.exception(f"Failed to build AIcList from status: {e}")
            return {}
    def _get_connected_map_from_status(self, dmpdip):
        connected = {}
        multi_count = {"PreSd": 0, "AIc": 0}
        try:
            # ① MTd 응답 기반 (단일 데몬)
            last_connect_resp = self._connected_daemonlist
            for name, info in (last_connect_resp or {}).items():
                if not isinstance(info, dict):
                    continue
                status = str(info.get("Status") or "").upper()
                if status == "OK":
                    connected[name] = True

            # ② node.status.data 기반 (멀티 인스턴스용)
            for node in self.nodes:
                data = node.get("status", {}).get("data", {})
                for name, info in data.items():
                    if not isinstance(info, dict):
                        continue
                    if info.get("running") is True and name in multi_count:
                        multi_count[name] += 1

            # 멀티 인스턴스 반영
            for name, cnt in multi_count.items():
                if cnt > 0:
                    connected[name] = cnt
            return connected
        except Exception:
            return {}
    def _unwrap_version_map(self, r: dict) -> dict:
        if not isinstance(r, dict):
            return {}
        # case 1: 전체 response 를 돌려준 경우 (MTd raw처럼)
        if "Version" in r and isinstance(r["Version"], dict):
            return r["Version"]            
        return r
    
    # ─────────────────────────────────────────────────────────────────────────────────────
    # 🔁 SYSTEM RESTART
    # ─────────────────────────────────────────────────────────────────────────────────────    
    def _sys_restart_get(self):
        with self._restart_lock:
            snap = deepcopy(self._sys_restart)            
            return snap
    def _sys_restart_set(self, **kw):
        
        start_time = kw.get('started_at') or self._sys_restart.get("started_at")
        msg_origin = kw.get("message")        

        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]        
        fd_log.info(f">> [SYS][RESTART][{state_code}]:{msg}")
        # update status
        with self._restart_lock:
            self._sys_restart.update(kw)
            self._sys_restart["updated_at"] = time.time()            
            self._sys_restart["started_at"] = start_time
    def _sys_restart_process(self, orch):
        try:
            time.sleep(max(0, orch._restart_min_prepare_ms / 1000.0))
            with self.cam_state_lock:
                # SYS_STATE 초기화
                global SYS_STATE
                SYS_STATE = {
                    "connected_daemons": {},
                    "versions": {},
                    "presd_versions": {},
                    "aic_versions": {},
                    "presd": [],
                    "cameras": [],
                    "switches": [],
                    "updated_at": time.time(),
                }
                fd_sys_clear_state()
                fd_log.info("[SYS] reset connection info")

                # CAM_STATE 초기화
                with self.cam_state_lock:
                    fd_cam_clear_connect_state()
                fd_log.info("[CAM] reset connection info")

            # --- Gather nodes safely
            nodes = []
            for n in orch.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm,
                    "host": n["host"],
                    "port": int(n.get("port", 19776)),
                    "status": deepcopy(orch._cache.get(nm) or {})
                })

            processes = orch._get_process_list() # /oms/system/process-list 와 동일한 반환 구조
            # 예: [{ "name": "EMd", "ips": ["10.82.104.210"] }, ...]

            jobs = []
            for proc in processes:
                name = proc["name"]
                for ip in proc["ips"]:
                    jobs.append((ip, 19776, ip, name)) # ip별로 restart job 생성

            fd_log.info(f"jobs, {jobs}")
            total = len(jobs)
            orch._sys_restart_set(state=1,total=total,sent=0,done=0,fails=[],message=f"Process Restart Queued : {total}")

            if total == 0:
                orch._sys_restart_set(state=2,message="Restart finished: nothing selected to restart")
                return

            def _fmt_percent(n, d):
                if not d:
                    return 0
                return int(round(100.0 * n / d))
            # ---------- pre-snapshots ----------
            base_map = {}
            for (host, port, node_name, proc) in jobs:
                base_map[(node_name, proc)] = fd_read_proc_snapshot(host, port, proc)
                # check daemon host 
                if proc == "MTd":
                    self.mtd_ip = host

            # ======================================================
            # 1) POST /restart/<proc>  --- 병렬 전송
            # ======================================================

            def send_restart(job):
                host, port, node_name, proc = job
                st, _, _ = fd_http_fetch(
                    host, port, "POST",
                    f"/restart/{proc}",
                    b"{}",
                    {"Content-Type": "application/json"},
                    timeout=orch._restart_post_timeout
                )
                if st >= 400:
                    raise RuntimeError(f"http {st}")
                return job

            sent = 0
            fails = []
            sent_at_map = {}
            orch._sys_restart_set(state=1,message=f"Sending restarts… 0/{total} (0%)")

            # --- 병렬 max_workers = job 수
            with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                futs = {ex.submit(send_restart, j): j for j in jobs}
                for fut in as_completed(futs):
                    job = futs[fut]
                    host, port, node_name, proc = job
                    try:
                        fut.result()
                        sent += 1
                        sent_at_map[(node_name, proc)] = time.time()
                    except Exception as e:
                        msg = f"{node_name}/{proc}: {e}"
                        fails.append(msg)
                        fd_log.exception(f"[send] {msg}")
                    finally:
                        pct = _fmt_percent(sent, total)
                        orch._sys_restart_set(state=1,sent=sent,fails=fails,message=f"Restart Process [{proc}] {sent}:/{total} ({pct}%) (fail {len(fails)})… waiting")

            # ======================================================
            # 2) wait_ready --- 병렬 polling (완전 병렬화)
            # ======================================================
            pending = [
                j for j in jobs
                if not any(f"{j[3]}" in f for f in fails)
            ]

            def wait_ready(job):
                host, port, node_name, proc = job
                base = base_map.get((node_name, proc), {})
                sent_at = sent_at_map.get((node_name, proc)) or time.time()
                t0 = time.time()
                saw_down = False
                seen_running = 0

                while True:
                    cur = fd_read_proc_snapshot(
                        host, port, proc,
                        timeout=self._status_fetch_timeout
                    )

                    # down → up transition 체크
                    if cur.get("running") is False:
                        saw_down = True
                        seen_running = 0
                    elif cur.get("running") is True:
                        seen_running += 1

                    # 방식1: meta 기반 restart 판단
                    if fd_is_restarted(base, cur, sent_at, saw_down):
                        return (job, True)

                    # 방식3: meta 없고 빠른 재기동 → running 2회 관측
                    meta_present = any(base.get(k) is not None for k in ("pid","start_ts","uptime")) \
                        or any(cur.get(k) is not None for k in ("pid","start_ts","uptime"))
                    if (not meta_present and cur.get("running") and seen_running >= 2 and (time.time() - sent_at) > 1.0):
                        return (job, True)

                    # timeout
                    if time.time() - t0 > orch._restart_ready_timeout:
                        return (job, False)

                    time.sleep(orch._restart_poll_interval)

            done = 0

            # --- 강제 병렬: max_workers = pending 수
            with ThreadPoolExecutor(max_workers=len(pending)) as ex:
                futs = [ex.submit(wait_ready, j) for j in pending]
                for fut in as_completed(futs):
                    job, ok = fut.result()
                    host, port, node_name, proc = job
                    if ok:
                        done += 1
                    else:
                        msg = f"{node_name}/{proc}: timeout"
                        fails.append(msg)
                        fd_log.error(f"[wait] {msg}")

                    orch._sys_restart_set(state=1,done=done,fails=fails,message=f"Restart Process {sent}/{total} (fail {len(fails)})… waiting")

            # ======================================================
            # 3) settle 단계 (병렬 검사 버전)
            # ======================================================

            def _compact(names, limit=10):
                return ", ".join(names[:limit]) + (" …" if len(names) > limit else "")

            if fails:
                failed_fullnames = [f.split(":")[0] for f in fails]
                targets = set(failed_fullnames)

                failed_nodes = [x.split("/")[0] for x in failed_fullnames]
                failed_procs = [x.split("/")[1] for x in failed_fullnames]

                summary_failers = _compact(failed_fullnames, 10)

                orch._sys_restart_set(state=1,    #run
                    message=(
                        f"pre-finished : ok {done}/{total}, fail {len(fails)}; "
                        f"failed: {summary_failers} · "
                        f"verifying up to {int(orch._restart_settle_sec)}s"
                    ),
                    fails=fails,
                    failed_total=len(failed_fullnames),
                    failed_list=failed_fullnames,
                    failed_nodes=failed_nodes,
                    failed_procs=failed_procs
                )

                # ---- 병렬 settle 검사 ----
                t0 = time.time()
                
                while time.time() - t0 < orch._restart_settle_sec and targets:
                    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
                        futs = {ex.submit(orch._sys_check_process, t): t for t in list(targets)}
                        for fut in as_completed(futs):
                            t = futs[fut]
                            ok = fut.result()
                            if ok:
                                targets.discard(ok)
                                fails = [x for x in fails if not x.startswith(ok)]
                                done += 1
                    if targets:
                        left = sorted(list(targets))[:10]
                        orch._sys_restart_set(state=1,    #run
                            message=(
                                f"Verifying settle… recovered {len(failed_fullnames)-len(targets)}/"
                                f"{len(failed_fullnames)} "
                                f"(left: {', '.join(left)}; "
                                f"{int(time.time()-t0)}s/{int(orch._restart_settle_sec)}s)"
                            ),
                            fails=fails,
                            failed_total=len(targets),
                            failed_list=sorted(list(targets))
                        )
                # --- settle 후 최종 상태 fix
                if not targets:
                    orch._sys_restart_set(state=2,   #done
                        message=(
                            f"Restart Process Finished: ok {total}/{total}"
                            f"(recovered during settle)"
                        ),
                        fails=[],
                        failed_total=0,
                        failed_list=[],
                        failed_nodes=[],
                        failed_procs=[]
                    )
                    return
                else:
                    summary_left = _compact(sorted(list(targets)), 10)
                    orch._sys_restart_set(state=2,   #done
                        message=(
                            f"final stage: ok {done}/{total}, fail {len(targets)}; "
                            f"failed: {summary_left}"
                        ),
                        fails=[f for f in fails if f.split(":")[0] in targets],
                        failed_total=len(targets),
                        failed_list=sorted(list(targets)),
                        failed_nodes=sorted({x.split('/')[0] for x in targets}),
                        failed_procs=sorted({x.split('/')[1] for x in targets})
                    )
            else:
                # waiting for organization
                time.sleep(2)
                orch._sys_restart_set(state=2,   #done
                    message=f"Restart Process Finished: ok {done}/{total}",
                    failed_total=0,
                    failed_list=[],
                    failed_nodes=[],
                    failed_procs=[]
                )
                return

        except Exception as e:
            fd_log.exception(f"Restart worker exception: {e}")                    
            orch._sys_restart_set(state=3, message=f"Error: {e}")
    def _sys_check_process(self, fullname):
        snap = self._sys_status_core()
        ok_set, _ = self._sys_check_processes(snap, {fullname})
        return fullname if ok_set else None
    def _sys_check_processes(self, current_status, targets):
        running, stopped = set(), set()
        nodes = current_status.get("nodes", [])
        by_key = {}
        for n in nodes:
            node = n.get("name") or n.get("alias") or ""
            s = n.get("status") or {}
            # unify to list
            if isinstance(s.get("data"), dict):
                procs = list(s["data"].values())
            elif isinstance(s.get("processes"), list):
                procs = s["processes"]
            elif isinstance(s.get("executables"), list):
                procs = s["executables"]
            else:
                procs = []
            for p in procs:
                pname = p.get("name") or p.get("proc") or p.get("id") or ""
                if not pname: continue
                key = f"{node}/{pname}"
                is_run = bool(p.get("running")) or str(p.get("status","")).lower() in ("running","started")
                (running if is_run else stopped).add(key)
                by_key[key] = p
        # intersect with targets only
        return running & targets, stopped & targets
            
    # ─────────────────────────────────────────────────────────────────────────────────────
    # 🔗 SYSTEM CONNECT
    # ─────────────────────────────────────────────────────────────────────────────────────    
    def _sys_connect_get(self):
        with self._sys_connect_lock:
            snap = deepcopy(self._sys_connect)
            return snap
    def _sys_connect_set(self, **kw):
        start_time = kw.get('started_at') or self._sys_connect.get("started_at")
        msg_origin = kw.get("message")        

        kw["message"] = self._tagged_time(msg_origin,start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]        
        fd_log.info(f">> [SYS][CONNECT][{state_code}]:{msg}")
        # update status
        with self._sys_connect_lock:
            self._sys_connect.update(kw)
            self._sys_connect["updated_at"] = time.time()  
            self._sys_connect["started_at"] = start_time    
    # ================================================================
    # FULL REFACTORED VERSION OF _sys_connect_sequence + sub functions
    # ================================================================
    def _sys_connect_sequence(
            self, mtd_host, mtd_port, dmpdip, daemon_map,
            *, trace=False, return_partial=False
        ):

        # reset connect info
        fd_sys_clear_connect_state()
        fd_log.info("[SYS] reset connection info")
        fd_cam_clear_connect_state()
        fd_log.info("[CAM] reset connection info")

        self._prepare_daemon_ips()
        self._sys_connect_set(state=1, message="Connect start", started_at=time.time())

        # STEP 1: Daemon Connect
        self._sys_connect_set(state=1, message="Daemon Connect")
        r1 = fd_retry(self._seq_step_1_daemon_connect, daemon_map, retry=3)

        # STEP 2: CCd.Select
        self._sys_connect_set(state=1, message="CCd.Select")
        r2 = fd_retry(self._seq_step_2_ccd_select, retry=3)

        # STEP 3: Build PreSd map
        self._sys_connect_set(state=1, message="Build PreSd map")
        self._seq_step_3_build_presd_map(r2)

        # STEP 4: PCd Connect
        self._sys_connect_set(state=1, message="PCd connect")
        fd_retry(self._seq_step_4_pcd_connect, retry=3)

        # STEP 5: AIc Connect
        self._sys_connect_set(state=1, message="AIc connect")
        fd_retry(self._seq_step_5_aic_connect, retry=3)

        # STEP 6: Update daemon status
        self._sys_connect_set(state=1, message="Update daemon dtatus")
        final_connected = self._seq_step_6_update_daemon_status(r1)

        # reload state
        self._sys_connect_set(state=1, message="Reload State")
        self._reload_state_from_server()

        fd_log.info("---------------------------------------------------------------------")
        fd_log.info(f"[SYS][CONNECT] Get Version")
        fd_log.info("---------------------------------------------------------------------")
        # STEP 7: Get Versions
        self._sys_connect_set(state=1, message="Get Version")
        temp = self._seq_step_7_get_version(dmpdip, final_connected)

        # STEP 8: Switch Information
        self._sys_connect_set(state=1, message="Switch Information")
        fd_retry(self._seq_step_8_switch_info, temp, retry=3)

        # STEP 9: Save States
        self._sys_connect_set(state=1, message="Save States")
        self._seq_step_9_save_states(temp)

        self._sys_connect_set(state=2, message="Finish Connection")
        return {"ok": True}
    def _prepare_daemon_ips(self):
    # STEP 0: preparing daemon IPs
        self.daemon_ips = self._get_daemon_ip()
        self.mtd_ip = (self.daemon_ips.get("MTd") or [None])[0]
        self.scd_ip = (self.daemon_ips.get("SCd") or [None])[0]
        self.ccd_ip = (self.daemon_ips.get("CCd") or [None])[0]
        fd_log.info("---------------------------------------------------------------------")
        fd_log.info(f"[SYS][CONNECT] Start Deamon Connect")
        fd_log.info("---------------------------------------------------------------------")
    def _seq_step_1_daemon_connect(self, daemon_map):
    # STEP 1: Daemon Connect
        self._sys_connect_set(state=1, message="Essential Daemons connect")
        pkt = self._build_mtd_daemon_connect_packet(daemon_map)
        r1 = self._mtd_command("Connect Essential Daemons", pkt, wait=10.0)
        try:
            self._connected_daemonlist = (r1.get("DaemonList") or {})
        except:
            self._connected_daemonlist = {}

        time.sleep(0.8)
        return r1
    def _seq_step_2_ccd_select(self):
    # STEP 2: CCd.Select
        self._sys_connect_set(state=1, message="Camera Information")
        pkt = {
            "Section1": "CCd",
            "Section2": "Select",
            "Section3": "",
            "SendState": "request",
            "From": "4DOMS",
            "To": "EMd",
            "Token": fd_make_token(),
            "Action": "get",
            "DMPDIP": self.mtd_ip
        }
        return self._mtd_command("Camera Daemon Information", pkt, wait=10.0)
    def _seq_step_3_build_presd_map(self, r2):
    # STEP 3: Build PreSd Map
        self.presd_map = {}
        self.cameras = []
        self.switch_ips = set()
        ra = (r2 or {}).get("ResultArray") or []
        for row in ra:
            pre_ip = str(row.get("PreSd_id") or "").strip()
            cam_ip = str(row.get("ip") or "").strip()
            model = str(row.get("model") or "").strip()
            scd_id = str(row.get("SCd_id") or "").strip()
            idx = int(row.get("cam_idx") or 0)

            if not pre_ip or not cam_ip:
                continue

            if pre_ip not in self.presd_map:
                self.presd_map[pre_ip] = {
                    "IP": pre_ip,
                    "Mode": "replay",
                    "Cameras": []
                }

            self.presd_map[pre_ip]["Cameras"].append({
                "Index": idx,
                "IP": cam_ip,
                "CameraModel": model
            })

            if scd_id:
                self.switch_ips.add(scd_id)

            self.cameras.append({
                "Index": idx,
                "IP": cam_ip,
                "CameraModel": model,
                "PreSdIP": pre_ip,
                "SCdIP": scd_id,
            })

        fd_log.info(f"presd = {list(self.presd_map.values())}")
    def _seq_step_4_pcd_connect(self):
    # STEP 4: PCD connect
        if not self.presd_map:
            return
        pkt3 = {
            "PreSd": list(self.presd_map.values()),
            "PostSd": [],
            "VPd": [],
            "Section1": "pcd",
            "Section2": "daemonlist",
            "Section3": "connect",
            "SendState": "request",
            "From": "4DOMS",
            "To": "PCd",
            "Token": fd_make_token(),
            "Action": "set",
            "DMPDIP": self.mtd_ip,
        }

        r3 = self._mtd_command("PreSd Daemon List", pkt3, wait=18.0)
        self.state["presd_ips"] = [u["IP"] for u in self.presd_map.values()]
    def _seq_step_5_aic_connect(self):
    # STEP 5: AId Connect
        try:
            aic_list = self._build_aic_list_from_status(self)
            if not aic_list:
                fd_log.warning("[AIc] aic_list empty → auto build from PreSd")
                aic_list = {}
                for item in self.state.get("presd") or []:
                    ip = item.get("IP")
                    alias = f"AIc-{ip.split('.')[-1]}"
                    aic_list[alias] = ip

            if not aic_list:
                fd_log.warning("[AIc] No AIc nodes detected; skipping")
                return

            pkt4 = {
                "AIcList": aic_list,
                "Section1": "AIc",
                "Section2": "connect",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "AId",
                "Token": fd_make_token(),
                "Action": "run",
                "DMPDIP": self.mtd_ip
            }

            # 🔥 반드시 응답을 r4 로 받아야 함
            r4 = self._mtd_command("AId Connect", pkt4, wait=10.0)

            # 🔥 여기서 AId 응답 기반으로 aic_connected 업데이트
            self.state["aic_connected"] = {
                name: info.get("IP")
                for name, info in (r4.get("AIcList") or {}).items()
                if isinstance(info, dict) and info.get("Status") == "OK"
            }

        except Exception as e:
            fd_log.exception(f"AIc connect failed: {e}")
    def _seq_step_6_update_daemon_status(self, r1):
    # STEP 6: Update daemon status
        connected = {}
        dl = (r1.get("DaemonList") or {}) if isinstance(r1, dict) else {}
        for dname, info in dl.items():
            if not isinstance(info, dict):
                continue
            if str(info.get("Status") or "").upper() == "OK":
                connected[fd_daemon_name_for_inside(dname)] = True
        return connected
    def _seq_step_7_get_version(self, dmpdip, final_connected):
    # STEP 7: Get versions  (FULL ABSORBED VERSION)
        self._sys_connect_set(state=1, message="Get Daemon Version ...")
        presd_ips = list(self.presd_map.keys())
        fd_log.info(f"* presd_ips = {presd_ips}")
        aic_map = self.state.get("aic_connected", {})

        versions = {}
        presd_versions = {}
        aic_versions = {}

        # STEP 7-1: Essential Daemon Versions
        connected_map = self._ver_load_connected_map(dmpdip)
        if not connected_map:
            connected_map = final_connected        

        self._sys_connect_set(state=1, message="Essential Daemon Versions ...")        
        fd_retry(
            self._ver_load_essential,
            dmpdip, connected_map, versions,
            retry=3
        )
        # STEP 7-2: PreSd Version
        self._sys_connect_set(state=1, message="PreSd Version ...")        
        fd_retry(
            self._ver_load_presd,
            dmpdip, presd_ips, versions, presd_versions,
            retry=3
        )
        # STEP 7-3: AId + AIc Version
        self._sys_connect_set(state=1, message="AId + AIc Version ...")        
        fd_retry(
            self._ver_load_aic,
            dmpdip, aic_map, connected_map, versions, aic_versions,
            retry=3
        )
        self._sys_connect_set(state=1, message="Final Connected Map ...")        
        # STEP 7-4: Final Connected Map
        return self._ver_finalize(
            dmpdip,
            versions,
            presd_versions,
            aic_versions,
            connected_map
        )
    def _seq_step_8_switch_info(self, temp):
    # STEP 8: Switch Info
        fd_log.info(">>> Switch Information")

        switches_info = []
        for ip in self.switch_ips:
            pkt = {
                "Section1": "Switch",
                "Section2": "Information",
                "Section3": "Model",
                "SendState": "request",
                "From": "4DOMS",
                "To": "SCd",
                "Token": fd_make_token(),
                "Action": "get",
                "Switches": [{"ip": ip}],
                "DMPDIP": self.mtd_ip
            }

            r = self._mtd_command("Switch Information", pkt, wait=10.0)
            sw_list = (r or {}).get("Switches") or []

            if sw_list:
                info = sw_list[0]
                switches_info.append({
                    "IP": ip,
                    "Brand": info.get("Brand", ""),
                    "Model": info.get("Model", ""),
                })

        temp["switches"] = switches_info
        return temp
    def _seq_step_9_save_states(self, temp):
    # STEP 9: Save States
        payload = {
            "connected_daemons": temp.get("connected_daemons", {}),
            "versions": temp.get("versions", {}),
            "presd_versions": temp.get("presd_versions", {}),
            "aic_versions": temp.get("aic_versions", {}),
            "presd": list(self.presd_map.values()),
            "aic_connected": self.state.get("aic_connected", {}),
            "cameras": self.cameras,
            "switches": temp.get("switches", []),
            "updated_at": time.time(),
        }

        fd_sys_state_upsert(payload)
        fd_log.info("---------------------------------------------------------------------")
        fd_log.info(f"[SYS][CONNECT] Final Payload (Connect + Version)")
        fd_log.info("---------------------------------------------------------------------")
        fd_log.info(f"{payload}")
        fd_cam_state_upsert({
            "cameras": self.cameras,
            "switches": temp.get("switches", []),
            "updated_at": time.time(),
        })
    def _ver_load_connected_map(self, dmpdip):
        connected_map = self._get_connected_map_from_status(dmpdip)
        if not connected_map:
            connected_map = self.state.get("connected_daemons", {})
        return connected_map
    def _ver_load_essential(self, dmpdip, connected_map, versions):
        # MTd - must
        self._sys_connect_set(state=1, message="MTd Versions ...")
        self._ver_get_MTd(versions)        
        # EMd
        if connected_map.get("EMd"):
            self._sys_connect_set(state=1, message="EMd Versions ...")
            self._ver_get_EMd(dmpdip, versions)
        # CCd
        if connected_map.get("CCd"):
            self._sys_connect_set(state=1, message="CCd Versions ...")
            self._ver_get_CCd(dmpdip, versions)
        # SCd
        if connected_map.get("SCd"):
            self._sys_connect_set(state=1, message="SCd Versions ...")
            self._ver_get_SCd(dmpdip, versions)
        # PCd
        if connected_map.get("PCd"):
            self._sys_connect_set(state=1, message="PCd Versions ...")
            self._ver_get_PCd(dmpdip, versions)
        # SPd → MMd
        if connected_map.get("SPd") or connected_map.get("MMd"):
            self._sys_connect_set(state=1, message="MMd Versions ...")
            self._ver_get_SPd_as_MMd(dmpdip, versions)
    def _ver_get_EMd(self, dmpdip, versions):
        def _op():
            r = self._request_version("EMd", dmpdip)
            v = (r.get("Version") or {}).get("EMd")
            if v:
                versions["EMd"] = v
        fd_retry(_op, retry=3)
    def _ver_get_CCd(self, dmpdip, versions):
        def _op():
            r = self._request_version("CCd", dmpdip)
            v = (r.get("Version") or {}).get("CCd")
            if v:
                versions["CCd"] = v
        fd_retry(_op, retry=3)
    def _ver_get_SCd(self, dmpdip, versions):
        def _op():
            r = self._request_version("SCd", dmpdip)
            v = (r.get("Version") or {}).get("SCd")
            if v:
                versions["SCd"] = v
        fd_retry(_op, retry=3)
    def _ver_get_PCd(self, dmpdip, versions):
        def _op():
            r = self._request_version("PCd", dmpdip)
            v = (r.get("Version") or {}).get("PCd")
            if v:
                versions["PCd"] = v
        fd_retry(_op, retry=3)
    def _ver_get_SPd_as_MMd(self, dmpdip, versions):
        def _op():
            r = self._request_version("SPd", dmpdip)
            v = (r.get("Version") or {}).get("SPd")
            if v:
                versions["MMd"] = v
        fd_retry(_op, retry=3)
    def _ver_get_MTd(self, versions):
        def _op():
            r = self._request_version("MTd", self.mtd_ip)
            v = (r.get("Version") or {}).get("MTd")
            if v:
                versions["MTd"] = v
        fd_retry(_op, retry=3)
    def _ver_load_presd(self, dmpdip, presd_ips, versions, presd_versions):        
        self._sys_connect_set(state=1, message="Get PreSd Version ...")        
        snapshot_key = None
        snapshot = {}
        try:
            snapshot_key, snapshot = fd_sys_latest_state()
            if not isinstance(snapshot, dict):
                snapshot = {}
        except:
            snapshot = {}            
        try:
            if dmpdip and dmpdip in SYS_STATE:
                st = SYS_STATE.get(dmpdip) or {}
            elif snapshot_key and snapshot_key in SYS_STATE:
                st = SYS_STATE.get(snapshot_key) or {}
            else:
                st = snapshot or {}
        except:
            st = snapshot or {}

        if not presd_ips:
            presd_ips = list(snapshot.get("presd_ips") or [])
            fd_log.warning(f"new presd_ips = {presd_ips}")
        if not presd_ips and isinstance(st, dict):
            presd_ips = []
            for u in (st.get("presd") or []):
                if isinstance(u, dict):
                    ip = u.get("IP")
                    if ip:
                        presd_ips.append(str(ip).strip())

        fd_log.debug(f"[Connect.5.3] presd_ips = {presd_ips}")
        if presd_ips:
            try:
                expect = {
                    "ips": presd_ips,
                    "count": len(presd_ips),
                    "wait_sec": 5
                }
                msg = {
                    "Section1": "Daemon",
                    "Section2": "Information",
                    "Section3": "Version",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "PreSd",
                    "Token": fd_make_token(),
                    "Action": "set",
                    "DMPDIP": dmpdip,     # 변경 금지
                    "Expect": expect
                }
                fd_log.debug(f"[SYS][CONNECT] Request PreSd version (tcp direct) → {msg}")
                resp = tcp_json_roundtrip("127.0.0.1", 19765, msg, timeout=7.0)[0]
                fd_log.debug(f"[SYS][CONNECT] PreSd batched version response = {resp}")

                resp_versions = resp.get("Version", {})
                v_presd = resp_versions.get("PreSd", {})
                sender_ip = resp.get("SenderIP")

                if isinstance(v_presd, dict):
                    versions["PreSd"] = {
                        "version": v_presd.get("version", "-"),
                        "date": v_presd.get("date", "-")
                    }
                # presd_ips 
                for ip in presd_ips:
                    presd_versions[ip] = {
                        "version": v_presd.get("version", "-"),
                        "date": v_presd.get("date", "-"),
                    }
                    fd_log.info(f"PreSd Version[{ip}] = {presd_versions[ip]}")
                # sender_ip mismatch는 정상 → INFO 유지
                if sender_ip and sender_ip != dmpdip:
                    fd_log.info(
                        f"[Connect.5.3] PreSd SenderIP differs (cluster master): "
                        f"DMPDIP={dmpdip}, SenderIP={sender_ip}"
                    )
            except Exception as e:
                fd_log.exception(f"PreSd version fetch failed: {e}")
        else:
            fd_log.debug(f"non presd_ips")
    def _ver_load_aic(self, dmpdip, aic_map, connected_map, versions, aic_versions):
        self._sys_connect_set(state=1, message="Get AId Version ...")

        if "AId" not in connected_map:
            raise Exception("AId not in connected_map")

        expect = {
            "AIc": list(aic_map.values()),
            "count": len(aic_map),
            "wait_sec": 5,
        }

        msg_base = {
            "Section1": "Daemon",
            "Section2": "Information",
            "Section3": "Version",
            "SendState": "request",
            "From": "4DOMS",
            "To": "AId",
            "Action": "set",
            "DMPDIP": dmpdip,
            "Expect": expect,
        }

        last_error = None

        # 🔥 🔥 🔥 _mtd_command 자체를 10번 재시도 🔥 🔥 🔥
        for attempt in range(1, 100):
            try:
                msg = dict(msg_base)
                msg["Token"] = fd_make_token()

                fd_log.debug(f"[AId Version] Try {attempt}/10 → {msg}")

                r = self._mtd_command("Version(AId)", msg, wait=7.0)

                vmap = self._unwrap_version_map(r)
                if not isinstance(vmap, dict):
                    raise Exception("Invalid AId version map")

                # ---------------------------------------------------
                # AId version
                # ---------------------------------------------------
                aid_info = vmap.get("AId")
                if not isinstance(aid_info, dict):
                    raise Exception("Missing AId version")

                versions["AId"] = aid_info

                # ---------------------------------------------------
                # AIc list
                # ---------------------------------------------------
                raw = vmap.get("AIc")
                if not raw:
                    raise Exception("Missing AIc list")

                if isinstance(raw, dict):
                    aic_versions.update(raw)

                elif isinstance(raw, list):
                    for item in raw:
                        if not isinstance(item, dict):
                            continue
                        ip = item.get("IP") or item.get("ip")
                        name = item.get("name") or "AIc"
                        if not ip:
                            continue
                        slot = aic_versions.setdefault(ip, {})
                        slot[name] = {
                            "version": item.get("version", "-"),
                            "date": item.get("date", "-")
                        }
                else:
                    raise Exception("AIc format invalid")

                fd_log.info(f"[AId Version] Success on attempt {attempt}")
                break  # 정상 처리 → 루프 종료

            except Exception as e:
                last_error = e
                fd_log.warning(f"[AId Version] failed {attempt}/10: {e}")
                time.sleep(0.3)
        else:
            raise Exception(f"AId version fetch failed after 10 attempts: {last_error}")
    def _ver_finalize(self, dmpdip, versions, presd_versions, aic_versions, connected_map):
        final = {}
        # --- SPd → MMd rename before building final map ---
        if "SPd" in connected_map:
            connected_map["MMd"] = connected_map["SPd"]
            del connected_map["SPd"]
        for name, ok in connected_map.items():
            if ok:
                final[name] = [dmpdip]
        if presd_versions:
            final["PreSd"] = list(presd_versions.keys())
        if aic_versions:
            final["AIc"] = list(aic_versions.keys())
        if "MTd" not in final:
            final["MTd"] = [self.mtd_ip]
        return {
            "versions": versions,
            "presd_versions": presd_versions,
            "aic_versions": aic_versions,
            "connected_daemons": final,
        }
    # Helper Functions (required stubs)
    def _request_version(self, daemon, ip, extra_fields=None, wait=8.0):
        """
        daemon에 Version 요청을 보내고,
        정상 응답이 올 때까지 전체 응답(response) dict 그대로 반환한다.
        """
        max_retry = 100

        for attempt in range(1, max_retry + 1):
            msg = {
                "Section1": "Daemon",
                "Section2": "Information",
                "Section3": "Version",
                "SendState": "request",
                "From": "4DOMS",
                "To": daemon,
                "Token": fd_make_token(),
                "Action": "set",
                "DMPDIP": ip,        # ← 반드시 daemon 의 ip
            }

            if extra_fields:
                msg.update(extra_fields)

            fd_log.debug(f"[Version] Try {attempt}/{max_retry} → {msg}")

            try:
                r = self._mtd_command(f"Version({daemon})", msg, wait=wait)

                # ==== 기본 검증 ====
                if not isinstance(r, dict):
                    raise Exception("Response not dict")

                if r.get("From") != daemon:
                    raise Exception(f"Unexpected From={r.get('From')}")

                vmap = r.get("Version")
                if not isinstance(vmap, dict):
                    raise Exception("Missing Version map")

                info = vmap.get(daemon)
                if not isinstance(info, dict):
                    raise Exception("Missing daemon Version object")

                version_str = info.get("version")
                if not isinstance(version_str, str) or not version_str.strip():
                    raise Exception("Invalid version string")

                # ==== AIc 전용 추가 검증 (Expect.count 만큼 수신해야 성공) ====
                if daemon == "AId":
                    expect = extra_fields.get("Expect") if extra_fields else None
                    if expect:
                        expected_list = expect.get("AIc") or []
                        expected_count = expect.get("count") or len(expected_list)

                        # AIc 리스트는 Version → AIc 에 존재
                        aic_raw = vmap.get("AIc") or []

                        actual_count = len(aic_raw)
                        if actual_count != expected_count:
                            raise Exception(
                                f"Missing AIc list: expected={expected_count}, got={actual_count}"
                            )

                fd_log.info(f"[Version] {daemon} success on attempt {attempt}")

                # ★★ 응답 전체를 반환해야 상위에서 Version 전체 map 을 이용할 수 있다
                return r

            except Exception as e:
                fd_log.warning(f"[Version] {daemon} failed {attempt}/{max_retry}: {e}")
                time.sleep(0.3)

        raise Exception(f"[Version] {daemon} failed after {max_retry} attempts")
    def _build_mtd_daemon_connect_packet(self, daemon_map):
        """
        MTd connect 패킷은 원본 포맷 그대로 복원해야 정상 동작한다.
        - MTd 자신은 포함하면 안 됨
        - PreSd, PostSd, VPd, AIc, MMc 제외
        - suffix(-1) 절대 사용하지 않음
        """
        daemon_list = {}
        for name, ip in daemon_map.items():
            # 1) MTd 자신 제외
            if name == "MTd":
                continue
            # 2) 클러스터형 데몬 제외
            if name in ("PreSd", "PostSd", "VPd", "AIc", "MMc"):
                continue
            # 3) 내부 데몬 이름 매핑
            mapped = fd_daemon_name_for_inside(name)
            # 4) IP 는 반드시 단일 문자열이어야 한다
            if isinstance(ip, list):
                # 여러 개라도 이전 로직은 첫 번째 것만 보냄
                ip = ip[0]
            # 5) MTd Connect 패킷은 name: ip 구조로만 보냄
            daemon_list[mapped] = ip
        pkt = {
            "DaemonList": daemon_list,
            "Section1": "mtd",
            "Section2": "connect",
            "Section3": "",
            "SendState": "request",
            "From": "4DOMS",
            "To": "MTd",
            "Token": fd_make_token(),
            "Action": "run",
            "DMPDIP": self.mtd_ip,
        }
        return pkt
    def _unwrap_version_map(self, r):
        v = r.get("Version") or {}
        return v
    def _get_connected_map_from_status(self, dmpdip):
        st = self.state or {}
        return st.get("connected_daemons", {})
    def _reload_state_from_server(self):
        try:
            raw = fd_http_fetch(
                "127.0.0.1",
                self.http_port,
                "GET",
                "/oms/state",
                None,
                {},
                timeout=3.0,
            )
            # raw 는 dict 이어야 한다.
            if not isinstance(raw, dict):
                fd_log.warning(f"[OMS][WARN] reload_state_from_server: raw is not dict: {raw}")
                return
            # 'state' 키가 있으면 그걸 사용하고, 없으면 raw 전체를 상태로 사용
            st = raw.get("state") or raw
            self.state = st
            fd_log.info(f"[OMS] reload_state_from_server OK")
        except Exception as e:
            fd_log.error(f"[OMS][WARN] reload_state_from_server failed: {e}", exc_info=True)
    
    # 🎯 oms/system/state
    def _sys_status_core(self):
        with self._lock:
            # ---------------------------------------------------------
            # 1) 기본 nodes 리스트 구성
            # ---------------------------------------------------------
            nodes = []
            for n in self.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm,
                    "alias": n.get("alias", ""),
                    "host": n["host"],
                    "port": int(n.get("port", 19776)),
                    "status": self._cache.get(nm),
                    "ts": self._cache_ts.get(nm, 0),
                })
            payload = {
                "ok": True,
                "heartbeat_interval_sec": self.heartbeat,
                "nodes": nodes,
            }

            # ---------------------------------------------------------
            # 2) extra (SYS_STATE 최신 스냅샷)
            # ---------------------------------------------------------
            _, latest = fd_sys_latest_state()
            extra = latest or {}
            payload["extra"] = extra
            # 반드시 추가! summary 계산에 필요
            sys_st = extra

            # ---------------------------------------------------------
            # 3) summary 계산
            # ---------------------------------------------------------
            try:
                nodes_count = len(nodes)
                # 1) connected = SYS_STATE["connected_daemons"] 기준
                st_daemons = sys_st.get("connected_daemons", {}) or {}
                connected_total = sum(len(v) for v in st_daemons.values())
                # 2) processes/running/stopped = nodes[].status.executables 기준
                process_count = 0
                running_total = 0
                stopped_total = 0
                for node in nodes:
                    st = node.get("status") or {}
                    exes = st.get("executables") or []
                    if not isinstance(exes, list):
                        continue
                    for p in exes:
                        if not isinstance(p, dict):
                            continue
                        if not p.get("select", True):
                            continue
                        process_count += 1
                        if p.get("running"):
                            running_total += 1
                        else:
                            stopped_total += 1
                payload["summary"] = {
                    "node": nodes_count,
                    "processes": process_count,
                    "connected": connected_total,
                    "running": running_total,
                    "stopped": stopped_total,
                }
            except Exception as e:
                payload["summary"] = {"error": str(e)}

            # ---------------------------------------------------------
            # 4) state/message
            # ---------------------------------------------------------
            # get system/restart, system/connect
            # state_code
            # 0 : idel
            # 1 : running
            # 2 : done
            # 3 : error
            # state_total_code
            # 0 : Check System (Unknown) : red
            # 1 : Check Setting : red
            # 2 : Needs Restart : yellow
            # 3 : Restarting... : yellow
            # 4 : Needs Connect : blue
            # 5 : Connecting... : blue
            # 6 : Ready         : green            

            sys_restart  = self._sys_restart_get()
            sys_connect  = self._sys_connect_get()
            sys_restart_state = sys_restart.get("state")
            sys_restart_msg = sys_restart.get("message")
            sys_connect_state = sys_connect.get("state")
            sys_connect_msg = sys_connect.get("message")

            # restart running -> Restarting...
            if sys_restart_state == 1:          # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 3
                sys_total_message = sys_restart_msg
            # restart error -> need to restart
            elif sys_restart_state == 3:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 2
                sys_total_message = "Please restart each process of the system."
            # connect running -> Connecting...
            elif sys_connect_state == 1:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 5
                sys_total_message = sys_connect_msg
            # connect error -> need to connect
            elif sys_connect_state == 3:        # 0:idle | 1:running | 2:done | 3:error
                sys_total_state_code = 4
                sys_total_message = "Please connect each process of the system."
            # restart cause stopped process
            elif stopped_total > 0:
                sys_total_state_code = 2
                sys_total_message = "Please check stopped process."
            # every process connected
            elif process_count > 0 and (process_count == connected_total):
                sys_total_state_code = 6
                sys_total_message = "The system is now ready for use."
            # regist process
            elif process_count == 0 or nodes_count == 0:            
                sys_total_state_code = 1
                sys_total_message = "Please register each process of the system."                
            # need process connect
            elif process_count > connected_total:
                sys_total_state_code = 4
                sys_total_message = "Please connect each process of the system."
            # need process run
            elif process_count > running_total: 
                sys_total_state_code = 2
                sys_total_message = "Please restart each process of the system."
            # 0 : unknown status
            else:                                                   
                sys_total_state_code = 0
                sys_total_message = "Please contact the administrator."

            payload["state"] = sys_total_state_code
            payload["message"] = sys_total_message
            
            return payload
    
    # ────────────────────────────────────────────
    # 📷 /C/A/M/E/R/A/ 
    # ────────────────────────────────────────────

    # ────────────────────────────────────────────
    # 🔁 CAMERA RESTART
    # ────────────────────────────────────────────
    def _cam_restart_get(self):
        with self._cam_restart_lock:
            return deepcopy(self._cam_restart)
    def _cam_restart_set(self, **kw):
        start_time = kw.get('started_at') or self._cam_restart.get("started_at")
        msg_origin = kw.get("message")  
        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]
        fd_log.info(f"camera restart message : {state_code}|{msg}")
        with self._cam_restart_lock:
            self._cam_restart.update(kw)
            self._cam_restart["updated_at"] = time.time()
            self._cam_restart["started_at"] = start_time 
    def _camera_force_off(self):
        with self.cam_state_lock:
            st = CAM_STATE

            cams = st.get("cameras", [])
            # --- 모든 카메라 강제 OFF ---
            for cam in cams:
                if not isinstance(cam, dict):
                    continue

                # 네트워크 연결 상태 (daemon 통신)
                cam["connected"] = False
                # 전원 상태 (UI 표시 기준)
                cam["alive"] = False
                # record 등도 정리
                cam["record"] = False

            # --- 집계 필드 초기화 ---
            st["camera_connected"] = {}
            st["camera_record"] = {}
            st["camera_alive"] = {cam.get("IP"): False for cam in cams}
            st["updated_at"] = time.time()

            fd_cam_state_save()

            fd_log.info("[CAM] FORCE OFF: all cameras set offline")
    def _camera_action_switch(self, type=1):

        with _mtd_lock:
            # 10초 polling 금지
            self.camera_poll_locked_until = time.time() + 5
            fd_log.info("[CAM] polling locked for 10 seconds after switch action")
            # 초기화
            with self.cam_state_lock:
                fd_cam_clear_connect_state(alive_reset=True)
            # command opt
            if type == 1:
                command_opt = "Reboot"
                self._cam_restart_set(state=1, message="All Cameras Reboot",started_at=time.time())
            elif type == 2:
                command_opt = "On"
                self._cam_restart_set(state=1, message="All Camera Trun On",started_at=time.time())
            elif type == 3:
                command_opt = "Off"
                self._cam_restart_set(state=1, message="All Camera Trun Off",started_at=time.time())
            else:
                return {"ok": False, "error": "INVALID_COMMAND_TYPE"}

            self._cam_restart_set(state=1, message=f"Camera {command_opt} via switch")

            # switch 조회
            sw_state = self._cam_status_core()
            switches = sw_state.get("switches") or []

            if not switches:
                sw_state = self._sys_status_core()
                switches = sw_state.get("switches") or []
                if not switches:
                    return {"ok": False, "error": "NO_SWITCHES"}

            switch_list = [sw.get("IP") or sw.get("IPAddress") for sw in switches if sw.get("IP") or sw.get("IPAddress")]

            if not switch_list:
                return {"ok": False, "error": "NO_VALID_SWITCH_IP"}

            # DMPDIP
            oms_ip = self.mtd_ip

            # payload
            req = {
                "Switches": [{"ip": ip} for ip in switch_list],
                "Section1": "Switch",
                "Section2": "Operation",
                "Section3": command_opt,
                "SendState": "request",
                "From": "4DOMS",
                "To": "SCd",
                "Action": "run",
                "Token": fd_make_token(),
                "DMPDIP": oms_ip,
            }

            # switch command 전송
            res = tcp_json_roundtrip(oms_ip, self.mtd_port, req, timeout=10)[0]


        with self.cam_state_lock:
            fd_cam_clear_connect_state(alive_reset=True)

        # Unlocked        
        # — 30초 카메라 부팅 대기 —
        if type in (1, 2):
            self._cam_restart_set(state=1, message="waiting until camera boot on...")
            start_ts = time.time()
            TIMEOUT = 30
            while True:
                if time.time() - start_ts > TIMEOUT:
                    return {"ok": False, "error": "CAMERA_BOOT_TIMEOUT"}
                w_state = self._cam_status_core()
                summary = w_state.get("summary", {})
                cameras = summary.get("cameras", 0)
                cam_alive = summary.get("alive", 0)

                if cameras > 0 and cameras == cam_alive:
                    break
                result_msg = f"waiting until camera boot on...{cam_alive}/{cameras}"
                self._cam_restart_set(state=1, message=result_msg)
                time.sleep(1)

        self._cam_restart_set(state=2, message=f"Finish Cameras {command_opt}")
        return {"ok": True, "response": res}

    
    # ────────────────────────────────────────────
    # 🔗 CAMERA CONNECT
    # ────────────────────────────────────────────
    # camera/connect
    def _cam_connect_get(self):
        with self._cam_connect_lock:
            return deepcopy(self._cam_connect)
    def _cam_connect_set(self, **kw):
        start_time = kw.get('started_at') or self._cam_connect.get("started_at")
        msg_origin = kw.get("message")  
        kw["message"] = self._tagged_time(msg_origin, start_time)
        # debug
        msg = kw["message"]
        state_code = kw["state"]
        fd_log.info(f"camera connect message : {state_code}|{msg}")
        with self._cam_connect_lock:
            self._cam_connect.update(kw)
            self._cam_connect["updated_at"] = time.time()
            self._cam_connect["started_at"] = start_time    
    def _connect_all_cameras(self):        
        fd_log.info("[OMS] _connect_all_cameras")
        try:
            # 0) Init camera/connect state
            self._cam_connect_set(state=1,message="Camera connect start",started_at=time.time())
            # 1) Load current camera state from HTTP
            self._cam_connect_set(state=1,message="Load current camera state")
            try:
                raw = fd_http_fetch(
                    "127.0.0.1",
                    self.http_port,
                    "GET",
                    "/oms/camera/state",
                    None,
                    {},
                    timeout=3.0,
                )
                fd_log.info(f"[OMS] /oms/camera/state raw({type(raw)}): {raw}")
                raw_body = self._extract_http_body(raw)
                if isinstance(raw_body, dict):
                    state = raw_body
                elif isinstance(raw_body, bytes):
                    state = json.loads(raw_body.decode("utf-8"))
                elif isinstance(raw_body, str):
                    state = json.loads(raw_body)
                else:
                    raise ValueError(f"Unsupported HTTP body type: {type(raw_body)}")
            except Exception as e:
                fd_log.error(f"[OMS] FAILED to load state: {e}")
                state = {}

            fd_log.info(f"[OMS] Loaded state: {state}")
            state_cams = state.get("cameras") or []

            # 2) Decide command DMPDIP (never hard-code)
            oms_ip = self.mtd_ip
            if not oms_ip:
                msg = "command DMPDIP not found (state/CFG/nodes)"
                fd_log.error(msg)
                self._cam_connect_set(state=3,message=msg,error=msg)
                return {"ok": False, "error": msg}
            fd_log.info(f"[camera/connect] command DMPDIP = {oms_ip}")

            # 3) CCD / MTd helper using oms_ip                        
            def _send_ccd(msg, timeout=10.0, retry=3, wait_after=0.8):
                last_err = None
                for attempt in range(1, retry + 1):
                    try:
                        fd_log.info(f"oms_ip:{oms_ip},mtd:{self.mtd_port} msg:{msg}")
                        resp, tag = tcp_json_roundtrip(oms_ip, self.mtd_port, msg, timeout=timeout)
                        fd_log.info(f"[camera/connect] CCD response tag={tag}: {resp}")
                        time.sleep(wait_after)
                        return resp
                    except MtdTraceError as e:
                        last_err = e
                        fd_log.debug(f"[camera/connect] attempt {attempt}/{retry} failed: {e}")
                        time.sleep(0.5)
                raise last_err

            # 4) Build camera IP list / AddCamera payload
            cam_add_list = []
            ip_list = []
            fd_log.info(f"[CCd] ip list = {state_cams}")
            # set message
            self._cam_connect_set(state=1,message="Build camera IP list")
            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                fd_log.info(f"[CCd] {ip}")
                if not ip:
                    continue

                ip_list.append(ip)
                cam_add_list.append(
                    {
                        "IPAddress": ip,
                        "Model": cam.get("Model") or cam.get("ModelName") or "BGH1",
                    }
                )

            if not cam_add_list:
                self._cam_connect_set(state=3,message="No cameras in OMs state",error="No cameras in OMs state")
                return {"ok": False, "error": "No cameras in OMs state"}

            # 5) MTd connect
            mtd_payload = {
                "DaemonList": {
                    "SCd": self.scd_ip,
                    "CCd": self.ccd_ip,
                },
                "Section1": "mtd",
                "Section2": "connect",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "MTd",
                "Token": fd_make_token(),
                "Action": "run",
                "DMPDIP": oms_ip,
            }
            # set message
            self._cam_connect_set(state=1,message="reconnect camera control daemon")
            fd_log.info(f"[MTd.connect] request:{mtd_payload}")
            mtd_res = _send_ccd(mtd_payload, timeout=10.0, wait_after=0.3)
            if int(mtd_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3, # error
                    message="[system][connect] MTd connect failed",
                    error=f"MTd connect failed: {mtd_res}",
                )
                return {"ok": False, "step": "MTd.connect", "response": mtd_res}

            # 6) CCd Select
            select_payload = {
                "Section1": "CCd",
                "Section2": "Select",
                "Section3": "",
                "SendState": "request",
                "From": "4DOMS",
                "To": "EMd",
                "Token": fd_make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.Select] request:{select_payload}")
            # set message
            select_res = _send_ccd(select_payload, timeout=10.0, wait_after=0.3)
            if int(select_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] CCd Select failed",error=f"CCd Select failed: {select_res}")
                return {"ok": False, "step": "CCd.Select", "response": select_res}

            # 7) AddCamera
            add_payload = {
                "Cameras": cam_add_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "AddCamera",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": fd_make_token(),
                "Action": "set",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.1.AddCamera] request:{add_payload}")
            # set message
            self._cam_connect_set(state=1,message="add camera list to daemon")                        
            add_res = _send_ccd(add_payload, timeout=10.0, wait_after=0.3)
            if int(add_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] AddCamera failed",error=f"AddCamera failed: {add_res}")
                return {"ok": False, "step": "AddCamera", "response": add_res}

            # 8) Camera Connect
            conn_payload = {
                "Section1": "Camera",
                "Section2": "Operation",
                "Section3": "Connect",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": fd_make_token(),
                "Action": "run",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.2.Connect] request:{conn_payload}")
            conn_res = _send_ccd(conn_payload, timeout=30.0, wait_after=0.3)
            if int(conn_res.get("ResultCode", 0)) != 1000:
                self._cam_connect_set(state=3,message="[camera][connect] Connect failed",error=f"Connect failed: {conn_res}")
                return {"ok": False, "step": "Connect", "response": conn_res}

            status_by_ip = {
                c["IPAddress"]: (c.get("Status") == "OK")
                for c in conn_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                # set message
                msg = f"sucess connect camera : [{ip}]"
                self._cam_connect_set(state=1,message=msg)            
                if ip in status_by_ip:
                    cam["connected"] = status_by_ip[ip]

            # 9) GetCameraInfo
            info_payload = {
                "Cameras": ip_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "GetCameraInfo",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": fd_make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.3.GetCameraInfo] request:{info_payload}")
            self._cam_connect_set(state=1,message="get camera information")                                
            info_res = _send_ccd(info_payload, timeout=10.0, wait_after=0.3)

            info_by_ip = {
                c["IPAddress"]: c
                for c in info_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                if ip in info_by_ip:
                    msg = f"sucess get camera info: [{ip}]"                
                    self._cam_connect_set(state=1,message=msg)            
                    cam.setdefault("info", {}).update(info_by_ip[ip])

            # 10) GetVideoFormat
            fmt_payload = {
                "Cameras": ip_list,
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "GetVideoFormat",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": fd_make_token(),
                "Action": "get",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"[CCd.4.GetVideoFormat] request:{fmt_payload}")
            self._cam_connect_set(state=1,message="get video format information")                                
            fmt_res = _send_ccd(fmt_payload, timeout=10.0, wait_after=0.3)

            fmt_by_ip = {
                c["IPAddress"]: c
                for c in fmt_res.get("Cameras", [])
                if c.get("IPAddress")
            }

            for cam in state_cams:
                ip = cam.get("IP")
                fmt = fmt_by_ip.get(ip)
                if fmt:
                    cam.setdefault("info", {}).update(
                        {
                            "StreamType": fmt.get("StreamType"),
                            "VideoFormatMain": fmt.get("VideoFormatMain"),
                            "VideoBitrateMain": fmt.get("VideoBitrateMain"),
                            "VideoGop": fmt.get("VideoGop"),
                            "VideoGopMain": fmt.get("VideoGopMain"),
                            "Codec": fmt.get("Codec"),
                        }
                    )

            # 11) Summary / camera-status in local "state" (for HTTP response, etc.)
            connected_ips = [ip for ip, ok in status_by_ip.items() if ok]            
            fd_log.info(f"connected camera ip:{connected_ips}")

            # ★ HERE: connected flag 업데이트
            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                cam["connected"] = (ip in connected_ips)

            # camera_status 생성
            camera_status = {}
            for cam in state_cams:
                ip = cam.get("IP")
                if not ip:
                    continue
                camera_status[ip] = "on" if cam.get("connected") else "off"
            fd_log.info(f"camera_status:{camera_status}")            
            summary = {
                "cameras": len(state_cams),
                "connected": sum(1 for c in state_cams if c.get("connected")),
                "on": sum(
                    1
                    for c in state_cams
                    if c.get("status") == "on" and not c.get("connected")
                ),
                "off": sum(1 for c in state_cams if c.get("status") == "off"),
            }            
            fd_log.info(f"summary:{summary}")

            # 12) Update global SYS_STATE (merge to existing system state entry)             
            cam_payload = {
                "cameras": [
                    {
                        "Index": cam.get("Index"),
                        "IP": cam.get("IP") or cam.get("IPAddress"),
                        "CameraModel": cam.get("CameraModel") or cam.get("Model") or cam.get("ModelName"),
                        "PreSdIP": cam.get("PreSdIP"),
                        "SCdIP": cam.get("SCdIP"),
                        "status": cam.get("status"),
                        "connected": cam.get("connected"),
                        "info": cam.get("info") or {},
                    }
                    for cam in state_cams
                ],
                "switches": state.get("switches", []),
                "camera_status": camera_status,
                "connected_ips": connected_ips,
                "updated_at": time.time(),
                "summary": summary,
            }
            fd_cam_state_upsert(cam_payload)
            self._cam_connect_set(state=2,#done
                                  message="success camera connection and get information")
            # update screen
            return {"ok": True}

        except Exception as e:
            fd_log.exception("connect_all_cameras unexpected error", exc_info=True)
            self._cam_connect_set(state=3,  #error
                message="[camera][connect] unexpected error",
                error=str(e),
            )
            return {"ok": False, "error": str(e)}
    def _camera_state_update(self, timeout_sec: float = 1.0) -> None:
        with COMMAND_LOCK:
            global CAM_STATE        
            cs = fd_cam_latest_state()            
            cams = cs.get("cameras") or []
            # test
            # fd_log.info(f"_camera_state_update -> cams:{cams}")

            ip_map = {str(c.get("IP")).strip(): c for c in cams if isinstance(c, dict)}
            ips = list(ip_map.keys())
            if not ips:
                return

            # ---------------------------------------------------------
            # 3) ping 보정 (CCD NG 는 무시)
            # ---------------------------------------------------------
            with ThreadPoolExecutor(max_workers=min(8, len(ips))) as ex:
                ping_results = {
                    ip: ex.submit(fd_ping_check, ip, method="auto", port=554, timeout_sec=timeout_sec)
                    for ip in ips
                }

            for ip, fut in ping_results.items():
                try:
                    alive, _ = fut.result()
                except Exception:
                    alive = None
                cam = ip_map[ip]
                cam["alive"] = bool(alive)

            # ---------------------------------------------------------
            # 1) CCD Status Query (state >= 6)
            # ---------------------------------------------------------
            ccd_map = {}
            if True:
                req = {
                    "Section1": "Camera",
                    "Section2": "Information",
                    "Section3": "Status",
                    "SendState": "request",
                    "From": "4DOMS",
                    "To": "CCd",
                    "Action": "get",
                    "Token": fd_make_token(),
                }

                try:
                    ccd_resp = tcp_json_roundtrip(
                        self.mtd_ip, self.mtd_port, req, timeout=3.0
                    )[0]
                except Exception as e:
                    fd_log.warning(f"CCd Status query fail: {e}")
                    ccd_resp = None

                if isinstance(ccd_resp, dict):
                    for item in (ccd_resp.get("Cameras") or []):
                        ip = str(item.get("IPAddress") or item.get("IP") or "").strip()
                        if not ip:
                            continue

                        status = (item.get("Status") or "").upper()
                        record = (item.get("Record") or "").upper()

                        ccd_map[ip] = {
                            "raw_status": status,
                            "connected": (status == "OK"),
                            "record": (record == "RUN"),
                            "temperature": item.get("Temperature"),
                        }

            # ---------------------------------------------------------
            # 2) CCD 결과 → cam dict에 반영
            # ---------------------------------------------------------
            for ip, info in ccd_map.items():
                cam = ip_map.get(ip)
                if not cam:
                    continue
                raw_status = info["raw_status"]
                # CCD -> connected
                cam["connected"] = (raw_status == "OK")
                # CCD -> record
                cam["record"] = info["record"]
                # NG는 record/connected 강제로 끔
                if raw_status == "NG":
                    cam["connected"] = False
                    cam["record"] = False
                if info["temperature"] is not None:
                    cam["temperature"] = info["temperature"]

            # ---------------------------------------------------------
            # 2.5) alive=False 이면 connected=False 강제
            # ---------------------------------------------------------
            for ip, cam in ip_map.items():
                if not cam.get("alive"):
                    cam["connected"] = False

            # ---------------------------------------------------------
            # 4) CAM_STATE 저장 (record 포함)
            # ---------------------------------------------------------
            CAM_STATE["camera_alive"] = {
                ip: ip_map[ip].get("alive", False)
                for ip in ip_map
            }
            CAM_STATE["camera_connected"] = {
                ip: ip_map[ip].get("connected", False)
                for ip in ip_map
            }
            CAM_STATE["camera_record"] = {
                ip: ip_map[ip].get("record", False)
                for ip in ip_map
            }
            CAM_STATE["connected_ips"] = [
                ip for ip, cam in ip_map.items() if cam.get("connected")
            ]
            CAM_STATE["updated_at"] = time.time()
            fd_cam_state_save()
    # 🎯 /oms/cameara/state
    def _cam_status_core(self):
        with self._lock:
            st = fd_cam_latest_state() or {}
            if isinstance(st, tuple):
                st = st[1] or {}

            cams = st.get("cameras") or []
            alive_map = st.get("camera_alive") or {}
            connected_map = st.get("camera_connected") or {}
            record_map = st.get("camera_record") or {}
            switches = st.get("switches") or {}

            # ---------------------------------------------------------
            # cameras 보정
            # ---------------------------------------------------------
            cameras_fixed = []
            for cam in cams:
                if not isinstance(cam, dict):
                    continue
                ip = (cam.get("IP") or "").strip()
                if not ip:
                    continue
                cam2 = dict(cam)
                # alive (ping)
                cam2["alive"] = bool(alive_map.get(ip, False))
                # connected (CCD Status)
                cam2["connected"] = bool(connected_map.get(ip, False))
                # record (CCD Record)
                cam2["record"] = bool(record_map.get(ip, False))
                # status 필드 제거
                cam2.pop("status", None)
                cameras_fixed.append(cam2)

            cams = cameras_fixed

            # ---------------------------------------------------------
            # Summary 계산
            # ---------------------------------------------------------
            total_cams = len(cams)
            alive_cams = sum(cam["alive"] for cam in cams)
            connected_cams = sum(cam["connected"] for cam in cams)
            record_cams = sum(cam["record"] for cam in cams)
            off_cams = total_cams - alive_cams

            summary = {
                "cameras": total_cams,
                "record": record_cams,
                "connected": connected_cams,
                "alive": alive_cams,
                "off": off_cams,
            }


            # ---------------------------------------------------------
            # State 계산
            # ---------------------------------------------------------
            cam_restart = self._cam_restart_get()
            cam_connect = self._cam_connect_get()

            cam_restart_state = cam_restart.get("state")
            cam_connect_state = cam_connect.get("state")
            cam_restart_msg = cam_restart.get("message")
            cam_connect_msg = cam_connect.get("message")

            #    0 : "Check System",
            #    1 : "Check Setting",
            #    2 : "Needs Restart",
            #    3 : "Restarting...",
            #    4 : "Needs Connect",
            #    5 : "Connecting...",
            #    6 : "Ready",
            #    7 : "Recording...",
            #    8 : "Recording Error"

            # Restaring... running
            # 3 : "Restarting...",
            if cam_restart_state == 1:
                state_code = 3
                message = cam_restart_msg
            # Restaring... error
            # 2 : "Needs Restart",
            elif cam_restart_state == 3:
                state_code = 2
                message = cam_connect_msg
            # Connecting... running
            # 5 : "Connecting...",
            elif cam_connect_state == 1:
                state_code = 5
                message = cam_connect_msg
            # Connecting... error
            # 4 : "Needs Connect",  
            elif cam_connect_state == 3:
                state_code = 4
                message = cam_connect_msg
            # all recording (recored)
            # 7 : "Recording...",
            elif total_cams > 0 and (total_cams == record_cams):
                state_code = 7
                message = "All cameras recording..."
            # prtial recording (recored)
            # 8 : "Recording Error"         
            elif total_cams > 0 and (0 < record_cams < total_cams):
                state_code = 8
                message = "Warning: Some cameras are NOT recording!"
            # all connected (connected) - from CCd
            # 6 : "Ready"
            elif total_cams > 0 and (total_cams == connected_cams):
                state_code = 6
                message = "The system is now ready for use."
            # partial stop - from ping
            # 2 : "Needs Restart"
            elif off_cams > 0:
                state_code = 2
                message = "Please check camera power status."
            # partial connected - from CCd/status -> conected
            # 4 : "Needs Connect",
            elif total_cams > connected_cams:
                state_code = 4
                message = "Please connect cameras."
            # partial on (some off) - ping
            # 2 : "Needs Restart",
            elif total_cams > alive_cams:
                state_code = 2
                message = "Please restart cameras."
            # non camera count
            # 1 : "Check Setting",
            elif total_cams == 0:
                state_code = 1
                message = "Please check the system."
            # undefined (unknown)
            # 1 : "Check Setting",
            else:
                state_code = 1
                message = "Please contact the administrator."

            updated_at = st.get("updated_at") or time.time()
            payload = {
                "ok": True,
                "cameras": cams,
                "connected_ips": [ip for ip, val in connected_map.items() if val],
                "camera_alive": alive_map,
                "camera_connected": connected_map,
                "camera_record": record_map,
                "switches": switches,
                "updated_at": updated_at,
                "summary": summary,
                "state": state_code,
                "message": message,
            }
            return payload
        
    # ────────────────────────────────────────────
    # 🔴 CAMERA RECORD
    # ────────────────────────────────────────────
    # camera record stop
    def _camera_record_start(self):
        """
        Full recording sequence:
        1) PreSd PREPARE
        2) CCd RUN
        3) Save record history into record/record_history.json
        4) Save detail file into record/history/<self.recording_name>.json
        """

        # ---------------------------------------------------------------------
        # 1) Load current camera state
        # ---------------------------------------------------------------------
        try:
            st = fd_cam_latest_state() or {}
        except Exception as e:
            return {"ok": False, "error": f"Cannot load camera/system state: {e}"}

        cameras = st.get("cameras") or []
        if not cameras:
            return {"ok": False, "error": "No cameras found"}

        camera_ips = [cam["IP"] for cam in cameras]

        # ---------------------------------------------------------------------
        # 2) Load user-config.json (Record Setting)
        # ---------------------------------------------------------------------
        config = fd_load_json_file("/config/user-config.json")
        RS = config.get("RecordSetting", {})

        cam_time      = RS.get("CameraRecordWaitTime", 1500)
        cam_synctime  = RS.get("CameraSyncWaitTime", 2000)
        cam_synclimit = RS.get("CameraSyncLimit", 10)
        cam_syncskip  = RS.get("CameraSyncSkip", False)
        use_audio     = RS.get("UseAudio", False)

        # ---------------------------------------------------------------------
        # 3) Generate record name
        # ---------------------------------------------------------------------
        now = time.localtime()
        self.recording_name = time.strftime("%Y_%m_%d_%H_%M_%S", now)

        # ---------------------------------------------------------------------
        # 4) Build PreSd group table
        # ---------------------------------------------------------------------
        presd_groups = {}

        for cam in cameras:
            ip = cam["IP"]
            presd_ip = cam["PreSdIP"]
            info = cam.get("info", {})

            vf = info.get("VideoFormatMain", "UHD-60")
            fps = 60
            if "-" in vf:
                try:
                    fps = int(vf.split("-")[1])
                except:
                    fps = 60

            storage_entry = {
                "IP": ip,
                "camfps": fps,
                "FrameRateConversionDenom": 1,
                "LiveGOP": 1,
                "Path": "C:\\MOVIE\\",
                "UseAudio": use_audio
            }

            presd_groups.setdefault(presd_ip, []).append(storage_entry)

        # ---------------------------------------------------------------------
        # 5) Build PreSd PREPARE packet
        # ---------------------------------------------------------------------
        presd_prepare = OrderedDict([
            ("Section1", "Camera"),
            ("Section2", "Operation"),
            ("Section3", "Prepare"),
            ("SendState", "request"),
            ("Token", fd_make_token()),
            ("From", "4DPD"),
            ("To", "PreSd"),
            ("Action", "set"),
            ("DMPDIP", self.mtd_ip),

            ("RecordName", self.recording_name),
            ("RecordFrameNo", 0),
            ("Record", True),
            ("CalibrationRecord", False),
            ("Limit", 0),
            ("LiveStabil", False),

            ("PreSd", [])
        ])

        for presd_ip, storage_list in presd_groups.items():
            presd_prepare["PreSd"].append({
                "IP": presd_ip,
                "Storage": storage_list
            })

        # ---------------------------------------------------------------------
        # 6) Send PREPARE
        # ---------------------------------------------------------------------
        try:
            fd_log.info(f"[record][prepare][request]:{presd_prepare}")
            presd_resp = tcp_json_roundtrip(self.mtd_ip, self.mtd_port,
                                            presd_prepare, timeout=10.0)
            fd_log.info(f"[record][prepare][response]:{presd_resp}")
        except Exception as e:
            return {"ok": False, "error": f"Prepare send failed: {e}"}

        # ---------------------------------------------------------------------
        # 7) Build CCd RUN packet
        # ---------------------------------------------------------------------
        ccdrun = OrderedDict([
            ("Section1", "Camera"),
            ("Section2", "Operation"),
            ("Section3", "Run"),
            ("SendState", "request"),
            ("Token", fd_make_token()),
            ("From", "4DPD"),
            ("To", "CCd"),
            ("Action", "run"),
            ("Streaming", True),
            ("Status", OrderedDict([
                ("time", cam_time),
                ("synctime", cam_synctime),
                ("syncskip", cam_syncskip),
                ("synclimit", cam_synclimit),
                ("active", camera_ips)
            ])),
            ("DMPDIP", self.mtd_ip)
        ])

        # ---------------------------------------------------------------------
        # 8) Send CCd RUN (measure time)
        # ---------------------------------------------------------------------
        send_ts = time.time() * 1000  # ms
        try:
            fd_log.info(f"[record][run][request]:{ccdrun}")
            ccdrun_resp = tcp_json_roundtrip(
                self.mtd_ip, self.mtd_port, ccdrun, timeout=20.0)
            recv_ts = time.time() * 1000  # ms
            # set real start time
            self.record_start_time = recv_ts
            diff_ms = recv_ts - send_ts

            fd_log.info(f"[record][run][response]:{ccdrun_resp}")
            fd_log.info(f"[record][sync-diff-ms]: {diff_ms:.2f}")

        except Exception as e:
            return {"ok": False, "error": f"CCd run failed: {e}"}

        # ---------------------------------------------------------------------
        # 9) Save record history (record/record_history.json)
        # ---------------------------------------------------------------------
        history = fd_load_json_file("/record/record_history.json")
        history.setdefault("history", [])

        detail_path = f"/record/history/recorded/{self.recording_name}.json"

        # ① Load Target
        config = fd_load_json_file("/config/user-config.json")
        pt = config.get("production-target", {})
        groups = pt.get("groups", [])
        g = pt.get("select-group", 0)
        i = pt.get("select-item", 0)

        selected_prod_target = ""

        if 0 <= g < len(groups):
            group = groups[g]
            if group and "group-name" in group:
                group_name = group["group-name"]
                lst = group.get("list", [])
                if 0 <= i < len(lst):
                    item = lst[i]
                    selected_prod_target = f"{group_name} - {item['name']}"

        # --- NEW: start time is stored at record START ---
        record_start_hms = fd_format_datetime(self.record_start_time)
        history["history"].append({
            self.recording_name: {
                "file-location": f"/web/record/history/recorded/{self.recording_name}.json",
                "production-target": selected_prod_target,         # ← 추가됨
                "record-start-time": record_start_hms,  # ← 추가됨
                "record-end-time": "",
                "recording-time": ""
            }
        })
        fd_save_json_file("/record/record_history.json", history)

        # ---------------------------------------------------------------------
        # Load AdjustInfo (CalibrationData.adj + UserPointData.pts)
        # ---------------------------------------------------------------------
        adj_path = fd_find_adjustinfo_file()
        adjust_data = {}

        if adj_path:
            # adj_path = "C:/4DReplay/V5/daemon/EMd/AdjustInfo/0/CalibrationData.adj"
            adj_root = os.path.dirname(adj_path)
            adjust_data = fd_load_adjust_info(adj_root)
            fd_log.info(f"[record] AdjustInfo loaded from {adj_path}")
        else:
            fd_log.warning("[record] AdjustInfo file not found")

        # Keep in memory for later use (during replay, send to daemon, etc.)
        self.current_adjustinfo = adjust_data

        # ---------------------------------------------------------------------
        # Done
        # ---------------------------------------------------------------------
        full_ts = time.strftime("%Y-%m-%d %H:%M:%S", now)
        fd_log.info("--------------------------------------------------")
        fd_log.info(f"[RECORDING STARTED][{self.recording_name}] start time:{full_ts}")
        fd_log.info("--------------------------------------------------")

        # ---------------------------------------------------------------------
        # 10) Save detail file (record/history/<self.recording_name>.json)
        # ---------------------------------------------------------------------
        detail = {
            "cameras": camera_ips,
            "record-set": {
                "production-target": selected_prod_target,
                "record-start-time-ms": send_ts,
                "record-response-time-ms": recv_ts,
                "record-sync-diff-ms": diff_ms
            },
            "adjust-info": adjust_data
        }
        
        fd_save_json_file(detail_path, detail)        

        return {
            "ok": True,
            "prepare_resp": presd_resp,
            "run_resp": ccdrun_resp,
            "sync_diff_ms": diff_ms,
            "detail_file": detail_path
        }
    def _camera_record_stop(self):
        """
        Send CCd Stop command and update record history with end-time information.
        """
        req = {
            "Section1": "Camera",
            "Section2": "Operation",
            "Section3": "Stop",
            "SendState": "request",
            "Token": fd_make_token(),
            "From": "4DOMS",
            "To": "CCd",
            "Action": "run",
            "DMPDIP": self.mtd_ip
        }

        try:
            # ------------------------------------------------------------------
            # 1) Send STOP packet
            # ------------------------------------------------------------------
            stop_ts = time.time() * 1000  # ms
            fd_log.info(f"[record][stop][request]:{req}")

            resp = tcp_json_roundtrip(self.mtd_ip, self.mtd_port, req, timeout=3.0)
            fd_log.info(f"[record][stop][response]:{resp}")

            # ------------------------------------------------------------------
            # 2) Compute recorded duration
            # ------------------------------------------------------------------
            if not hasattr(self, "record_start_time"):
                fd_log.error("record_start_time is missing!")
                return {"ok": False, "error": "Missing record_start_time"}

            recorded_time_ms = stop_ts - self.record_start_time

            hms = fd_format_hms_verbose(recorded_time_ms)
            fd_log.info("--------------------------------------------------")
            fd_log.info(f"[RECORDING END][{self.recording_name}] recording time : {hms}")
            fd_log.info("--------------------------------------------------")

            # ------------------------------------------------------------------
            # 3) Update detail file (record/history/<recording_name>.json)
            # ------------------------------------------------------------------
            detail_path = f"/record/history/recorded/{self.recording_name}.json"
            detail = fd_load_json_file(detail_path)

            if "record-set" not in detail:
                detail["record-set"] = {}

            detail["record-set"]["record-end-time-ms"] = stop_ts
            detail["record-set"]["recording-time-ms"] = recorded_time_ms

            # ---------------------------
            # Human-readable fields added
            # ---------------------------
            detail["record-set"]["record-start-time"] = fd_format_datetime(self.record_start_time)
            detail["record-set"]["record-end-time"]   = fd_format_datetime(stop_ts)
            detail["record-set"]["recording-time"]    = fd_format_hms_ms(recorded_time_ms)

            # -------------------------------------------------------------
            # 4) Save to record_history.json (with human-readable timestamps)
            # -------------------------------------------------------------
            # Load existing history
            history = fd_load_json_file("/record/record_history.json")
            history.setdefault("history", [])
            # record name
            rn = self.recording_name
            # detail to update
            update_fields = {
                "record-end-time": fd_format_datetime(stop_ts),
                "recording-time": fd_format_hms_ms(recorded_time_ms)
            }
            # --------- MERGE LOGIC ---------
            updated = False
            for item in history["history"]:
                if rn in item:
                    entry = item[rn]

                    # 만약 기존이 prefix 라면 production-target으로 키 변경
                    if "prefix" in entry:
                        entry["production-target"] = entry["prefix"]
                        entry.pop("prefix", None)

                    # 업데이트 결합
                    entry.update(update_fields)
                    updated = True
                    break

            if not updated:
                # create new entry (if for some reason not found)
                history["history"].append({ rn: update_fields })

            fd_save_json_file("/record/record_history.json", history)
            fd_save_json_file(detail_path, detail)
            fd_log.info(f"[record][history-update] updated: {detail_path}")
            return {"ok": True, "resp": resp}
        except Exception as e:
            fd_log.error(f"record stop fail: {e}")
            return {"ok": False, "error": str(e)}
    
    # ────────────────────────────────────────────
    # 🔴 VIDEO MAKE
    # ────────────────────────────────────────────


    # ────────────────────────────────────────────
    # 🧩 CAMERA ACTION
    # ────────────────────────────────────────────
    # connect action - focus
    def _camera_action_autofocus(self, body):
        try:
            # 0) 요청 body에서 target ip 추출
            try:
                raw_body = body
                if isinstance(raw_body, bytes):
                    payload = json.loads(raw_body.decode("utf-8") or "{}")
                elif isinstance(raw_body, str):
                    payload = json.loads(raw_body or "{}")
                elif isinstance(raw_body, dict):
                    payload = raw_body
                else:
                    payload = {}
            except Exception as e:
                fd_log.warning(f"autofocus: payload parse error: {e}")
                payload = {}

            # ⭐ target_ip 안전 파싱
            val = payload.get("ip")
            if isinstance(val, str):
                target_ip = val.strip()
            else:
                target_ip = ""

            fd_log.info(f"camera_action_autofocus -> Target IP: {target_ip}")
            # 1) /oms/camera/state 로드
            try:
                raw = fd_http_fetch(
                    "127.0.0.1",
                    self.http_port,
                    "GET",
                    "/oms/camera/state",
                    None,
                    {},
                    timeout=3.0,
                )
                raw_body = self._extract_http_body(raw)

                if isinstance(raw_body, dict):
                    state = raw_body
                elif isinstance(raw_body, (bytes, str)):
                    state = json.loads(
                        raw_body if isinstance(raw_body, str)
                        else raw_body.decode("utf-8")
                    )
                else:
                    raise ValueError("Unsupported body type")
            except Exception as e:
                return {"ok": False, "error": f"STATE_LOAD_FAIL: {e}"}

            # 2) 카메라 목록 필터링 (target_ip 있으면 해당 IP만)
            state_cams = state.get("cameras") or []
            ip_list = []

            for cam in state_cams:
                ip = cam.get("IP") or cam.get("IPAddress")
                if not ip:
                    continue
                if target_ip and ip != target_ip:
                    continue
                ip_list.append(ip)

            if not ip_list:
                return {"ok": False, "error": "NO_CAMERAS_FOUND"}

            fd_log.info(f"camera list for AF: {ip_list} (target_ip={target_ip!r})")

            # 3) DMPDIP 선택 (기존 로직 그대로)
            oms_ip = self.mtd_ip
            fd_log.info(f"self.mtd_ip = {self.mtd_ip}")

            if not oms_ip:
                return {"ok": False, "error": "NO_DMPDIP"}

            # 4) AF command 생성
            req = {
                "Cameras": [
                    {"IPAddress": ip, "OneShotAF": {"arg": "none"}}
                    for ip in ip_list
                ],
                "Section1": "Camera",
                "Section2": "Information",
                "Section3": "SetCameraInfo",
                "SendState": "request",
                "From": "4DOMS",
                "To": "CCd",
                "Token": fd_make_token(),
                "Action": "set",
                "DMPDIP": oms_ip,
            }

            fd_log.info(f"AF request: {req}")

            # 5) CCd 전송 (기존 로직)
            def _send_ccd(msg, timeout=10.0, retry=3, wait_after=0.8):
                last_err = None
                for attempt in range(1, retry + 1):
                    try:
                        resp, tag = tcp_json_roundtrip(
                            oms_ip, self.mtd_port, msg, timeout=timeout
                        )
                        time.sleep(wait_after)
                        return resp
                    except Exception as e:
                        last_err = e
                        time.sleep(0.5)
                raise last_err

            res = _send_ccd(req)

            # 6) 결과 집계 (요청한 ip_list 기준)
            ok_count = 0
            fail_count = 0

            fd_log.info(f"AF response: {res}")

            for cam in res.get("Cameras", []):
                ip = cam.get("IPAddress") or cam.get("IP")
                if ip not in ip_list:
                    continue
                st = cam.get("OneShotAF", {}).get("Status")
                if st == "OK":
                    ok_count += 1
                else:
                    fail_count += 1

            detail = {
                "ok_count": ok_count,
                "fail_count": fail_count,
            }

            return {
                "ok": fail_count == 0,
                "detail": detail,
                "response": res,
            }

        except Exception as e:
            return {"ok": False, "error": str(e)}
    
    # ────────────────────────────────────────────
    # 🔄 POLLING
    # ────────────────────────────────────────────    
    def _poll_node_info(self):
        for n in self.nodes:
            name=n.get("name") or n.get("host")
            try:
                st,_,data = fd_http_fetch(n["host"], int(n.get("port",19776)), "GET", "/status", None, None, timeout=2.5)
                payload = json.loads(data.decode("utf-8","ignore")) if st==200 else {"ok":False,"error":f"http {st}"}
            except Exception as e:
                payload = {"ok":False,"error":repr(e)}
            # ▼ DMS /config에서 실행 항목(alias)도 끌어옴
            alias_map = None
            try:
                st2, hdr2, dat2 = fd_http_fetch(n["host"], int(n.get("port",19776)), "GET", "/config", None, None, timeout=self._status_fetch_timeout)
                if st2 == 200:
                    txt = dat2.decode("utf-8","ignore")
                    cfg = json.loads(fd_strip_json5(txt))
                    tmp = {}
                    for ex in (cfg.get("executables") or []):
                        nm = (ex or {}).get("name"); al = (ex or {}).get("alias")
                        if nm and al is not None:
                            if al:
                                tmp[nm] = al
                    alias_map = tmp
                else:
                    alias_map = None
            except Exception:
                alias_map = None
            with self._lock:
                self._cache[name] = payload
                self._cache_ts[name] = time.time()
                # ⬇️ 핵심: 200 OK였다면 빈 dict라도 캐시 반영(= 제거 반영)
                if alias_map is not None:
                    self._cache_alias[name] = alias_map
    def _node_info_loop(self):
        while not self._stop.is_set():
            # 1) MTD 작업 중이면 잠깐 쉰다 (CAM_STATE 건드리지 않음)
            if _mtd_lock.locked():
                self._stop.wait(0.2)
                continue

            # 2) CAM_STATE 읽기/쓰기 보호
            with self.cam_state_lock:
                try:
                    self._poll_node_info()
                except Exception:
                    fd_log.exception("[OMS] node info loop error")

            self._stop.wait(self.heartbeat)
    def _camera_loop(self):
        while not self._stop.is_set():
            # 1) MTD 작업 중이면 잠깐 쉰다
            if _mtd_lock.locked():
                self._stop.wait(0.2)
                continue

            # (2) 카메라 강제 OFF 상태 유지 시간이 남아있으면 polling skip
            if time.time() < getattr(self, "camera_poll_locked_until", 0):
                self._stop.wait(0.2)
                continue
            
            # (3) 정상 polling
            with self.cam_state_lock:
                st = fd_cam_latest_state() or {}
                cameras = st.get("cameras", [])
                if not cameras:
                    # 카메라 없으면 바로 빠져나와서 쉬기
                    pass
                else:
                    try:
                        self._camera_state_update(timeout_sec=1)
                    except Exception:
                        fd_log.exception("[OMS] camera ping loop error")
                        continue
                    
            self._stop.wait(1.0)
    # ────────────────────────────────────────────
    # ⭐ MAIN FUNTIONS
    # ────────────────────────────────────────────        
    def run(self):
        # looping command
        threading.Thread(target=self._node_info_loop, daemon=True).start()
        threading.Thread(target=self._camera_loop, daemon=True).start()        

        self._http_srv = ThreadingHTTPServer((self.http_host, self.http_port), self._make_handler()) 
        self._log(f"# START OMS SERVICE")
        self._log(f"# http {self.mtd_ip}:{self.http_port}")        
        self._log(f"#####################################################################")
        
        try:
            self._http_srv.serve_forever(poll_interval=0.5)
        finally:
            try: self._http_srv.server_close()
            except: pass
    def stop(self):
        try: self._stop.set()
        except: pass
        try: self._http_srv.shutdown()
        except: pass
    def apply_runtime(self, cfg: dict):
        ch=[]
        with self._lock:
            hb=float(cfg.get("heartbeat_interval_sec", self.heartbeat))
            if hb>0 and hb!=self.heartbeat: self.heartbeat=hb; ch.append("heartbeat_interval_sec")
            if isinstance(cfg.get("nodes"), list): self.nodes=list(cfg["nodes"]); ch.append("nodes")
            # ▼▼▼ [NEW] process_alias 핫리로드
            if isinstance(cfg.get("process_alias"), dict):
                self.process_alias = {**PROCESS_ALIAS_DEFAULT, **cfg["process_alias"]}
                ch.append("process_alias")
            # ▲▲▲
        return ch
    def _make_handler(self):
        orch = self 
        def _serve_static(handler, rel):
            fp=(WEB/rel.lstrip("/")).resolve()
            base=WEB.resolve()
            if not fp.is_file() or not str(fp).startswith(str(base)):
                handler.send_response(404)
                handler.send_header("Content-Type","application/json; charset=utf-8")
                handler.send_header("Cache-Control","no-store")
                b=b'{"ok":false,"error":"not found"}'
                handler.send_header("Content-Length",str(len(b))); handler.end_headers(); handler.wfile.write(b); return
            data=fp.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", fd_mime(fp))
            handler.send_header("Cache-Control","no-store")
            handler.send_header("Content-Length",str(len(data))); handler.end_headers();
            try: handler.wfile.write(data)
            except: pass
        # ─────────────────────────────────────────────────────────────────────────────────────
        # BaseHTTPRequestHandler
        # ─────────────────────────────────────────────────────────────────────────────────────         
        class H(BaseHTTPRequestHandler):
            # --- fallback proxies to avoid AttributeError from old handler code
            _restart_post_timeout = RESTART_POST_TIMEOUT
            _status_fetch_timeout = STATUS_FETCH_TIMEOUT
            def _write(self, code=200, body=b"", ct="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                # 일반 응답도 캐시/변환 방지
                self.send_header("Cache-Control","no-cache, no-transform")
                # CORS (외부 페이지에서 진행 상황을 읽을 수 있도록)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except: pass
            def _send_json(self, obj, status=200):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            # ─────────────────────────────────────────────────────────────────────────────────────
            # 🧰 do_GET
            # ─────────────────────────────────────────────────────────────────────────────────────         
            def do_GET(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    clean = (urlsplit(self.path).path.rstrip("/") or "/")
                    if clean in {"/","/dashboard"}: return _serve_static(self, "oms-dashboard.html")
                    if clean in {"/system"}: return _serve_static(self, "oms-system.html")
                    if clean in {"/command"}: return _serve_static(self, "oms-command.html")
                    if clean in {"/camera"}: return _serve_static(self, "oms-camera.html")
                    if clean in {"/record"}: return _serve_static(self, "oms-record.html")
                    if clean in {"/liveview"}: return _serve_static(self, "oms-liveview.html")
                    if clean in {"/user"}: return _serve_static(self, "user-config.html")

                    # ──────────────────────────────────────────────────────
                    # 📦 GET : proxy
                    # ──────────────────────────────────────────────────────
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=unquote(parts[1]); target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target: return self._write(404, b'{"ok":false,"error":"unknown node"}')
                        sub="/"+"/".join(parts[2:]) if len(parts)>2 else "/"
                        qs=urlsplit(self.path).query
                        if qs: sub=f"{sub}?{qs}"
                        st,hdr,data=fd_http_fetch(target["host"], int(target.get("port",19776)), "GET", sub, None, None, 4.0)
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/octet-stream"
                        return self._write(st, data, ct)
                    # ──────────────────────────────────────────────────────
                    # 📦 GET : Logs
                    # ──────────────────────────────────────────────────────
                    if parts and parts[0] == "daemon" and len(parts) >= 3 and parts[2] == "log":
                        proc = parts[1]
                        # 안전한 프로세스명만 허용 (영문/숫자/언더스코어만)                        
                        if not re.fullmatch(r"[A-Za-z0-9_]+", proc):
                            return self._write(400, b'{"ok":false,"error":"bad process name"}')
                        log_dir = (ROOT / "daemon" / proc / "log")
                        try:
                            log_dir.mkdir(parents=True, exist_ok=True)
                        except Exception:
                            pass
                        # /daemon/<PROC>/log/list
                        if len(parts) >= 4 and parts[3] == "list":
                            try:
                                dates = []
                                for p in sorted(log_dir.glob("????-??-??.log")):
                                    dates.append(p.stem)
                                # 최신날짜가 뒤에 오도록 정렬(원하면 reverse=True 바꾸세요)
                                body = json.dumps({"ok": True, "dates": dates}, ensure_ascii=False).encode("utf-8")
                                return self._write(200, body)
                            except Exception as e:
                                err = json.dumps({"ok": False, "error": f"list failed: {e}"}, ensure_ascii=False).encode("utf-8")
                                return self._write(500, err)
                        # /daemon/<PROC>/log?date=YYYY-MM-DD&tail=50000                        
                        qs = parse_qs(urlsplit(self.path).query)
                        date = (qs.get("date") or [""])[0].strip()
                        tail = (qs.get("tail") or ["50000"])[0].strip()
                        try:
                            tail_bytes = max(0, int(tail))
                        except Exception:
                            tail_bytes = 50000
                        if not date: 
                            date = time.strftime("%Y-%m-%d", time.localtime())
                        log_file = log_dir / f"{date}.log"
                        if not log_file.exists():
                            body = json.dumps({
                                "ok": False,
                                "error": "log file not found",
                                "path": str(log_file),
                                "date": date,
                                "tail": tail_bytes
                            }, ensure_ascii=False).encode("utf-8")
                            return self._write(200, body)
                        try:
                            size = log_file.stat().st_size
                            with open(log_file, "rb") as f:
                                if tail_bytes > 0 and size > tail_bytes:
                                    f.seek(size - tail_bytes)
                                    _ = f.readline()
                                data = f.read()
                            text = data.decode("utf-8", "ignore")
                            body = json.dumps({
                                "ok": True,
                                "path": str(log_file),
                                "date": date,
                                "tail": tail_bytes,
                                "size": size,
                                "text": text
                            }, ensure_ascii=False).encode("utf-8")
                            return self._write(200, body)
                        except Exception as e:
                            err = json.dumps({"ok": False, "error": f"read failed: {e}"}, ensure_ascii=False).encode("utf-8")
                            return self._write(500, err)
                    if parts[:1]==["web"]: return _serve_static(self, "/".join(parts[1:]))
                    if clean.endswith(".html"): return _serve_static(self, clean)                    
                    # ──────────────────────────────────────────────────────
                    # 🧩 GET : /oms/config
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")
                    # ──────────────────────────────────────────────────────
                    # 🧩 GET : /oms/config/meta
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "config", "meta"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        s = CFG.stat()
                        payload = {
                            "ok": True,
                            "path": str(CFG),
                            "size": s.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime)),
                        }
                        return self._write(200, json.dumps(payload).encode()) 
                    # ──────────────────────────────────────────────────────
                    # 🚀 GET : /oms/mtd-query
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "mtd-query"]:
                        return self._write(
                            405,
                            json.dumps({
                                "ok": False,
                                "error": "method not allowed",
                                "hint": "use POST with JSON body: {host, port, message, timeout}"
                            }).encode("utf-8")
                        )
                    # ──────────────────────────────────────────────────────
                    # 1️⃣ GET : /oms/system/state
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system", "state"]:
                        status = orch._sys_status_core()
                        return self._write(200, json.dumps(status, ensure_ascii=False).encode("utf-8"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣ GET /oms/system/process-list
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system", "process-list"]:
                        plist = orch._get_process_list()
                        body = json.dumps({"processes": plist}, ensure_ascii=False).encode("utf-8")
                        return self._write(200, body)
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ GET /oms/system/restart/state
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "system", "restart", "state"]:
                        s = orch._sys_restart_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ GET /oms/system/connect/state
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system" , "connect", "state"]:
                        s = orch._sys_connect_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ GET /oms/system/connect/clear
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "system" ,"connect", "clear"]:
                        orch._sys_connect_set(state=0, message="", events=[], started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣ GET : /oms/camera/state
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "camera", "state"]:
                        status = orch._cam_status_core()
                        return self._write(200, json.dumps(status, ensure_ascii=False).encode("utf-8"))
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ GET /oms/camera/connect/state
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "camera", "connect", "state"]:
                        s = orch._cam_connect_get()
                        return self._write(200, json.dumps(s, ensure_ascii=False).encode("utf-8","ignore"))
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-2️⃣ GET /oms/camera/liveview/status
                    # ──────────────────────────────────────────────────────
                    if parts == ["oms", "camera", "liveview","status"]:
                        running = MTX.is_running()
                        self._send_json({"running": running})
                        return
                    # ──────────────────────────────────────────────────────
                    # 🔴 GET /N/O/T/ /F/O/U/N/D/
                    # ──────────────────────────────────────────────────────                    
                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            # ─────────────────────────────────────────────────────────────────────────────────────
            # 🌟 do_POST
            # cmd : curl -X POST http://127.0.0.1:19777/oms/alias/clear                    
            # ─────────────────────────────────────────────────────────────────────────────────────
            def do_POST(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    length=int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length)
                    # ──────────────────────────────────────────────────────
                    # 📦 POST :proxy post
                    # ──────────────────────────────────────────────────────
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=parts[1]
                        target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target: return self._write(404, b'{"ok":false,"error":"unknown node"}')
                        sub="/"+"/".join(parts[2:]) or "/"
                        qs=urlsplit(self.path).query
                        if qs: sub=f"{sub}?{qs}"
                        st,hdr,data=fd_http_fetch(target["host"], int(target.get("port",19776)), "POST", sub, body, {"Content-Type": self.headers.get("Content-Type","application/json")})
                        ct=hdr.get("Content-Type") or hdr.get("content-type") or "application/json"
                        return self._write(st, data, ct)
                    # ──────────────────────────────────────────────────────
                    # 🎯 POST : /oms/mtd-query
                    # ──────────────────────────────────────────────────────                    # 
                    if parts==["oms", "mtd-query"]:
                        # 1) 빈 바디/Content-Type 점검
                        if length <= 0:
                            return self._write(400, b'{"ok":false,"error":"empty body"}')
                        ctype = (self.headers.get("Content-Type") or "").lower()
                        if "application/json" not in ctype:
                            return self._write(400, b'{"ok":false,"error":"content-type must be application/json"}')

                        # 2) JSON 파싱
                        try:
                            req = json.loads(body.decode("utf-8", "ignore"))
                        except Exception as e:
                            return self._write(400, json.dumps({"ok": False, "error": f"bad json: {e}"}).encode())

                        host = req.get("host")
                        port = int(req.get("port", 19765))
                        msg = req.get("message") or {}
                        timeout = float(req.get("timeout") or 10.0)

                        if not host or not isinstance(msg, dict):
                            return self._write(400, b'{"ok":false,"error":"bad request (need host, message)"}')

                        # 3) 보내기 직전 디버그 로그
                        fd_append_mtd_debug("send", host, port, message=msg)
                        try:
                            resp, tag = tcp_json_roundtrip(host, port, msg, timeout=timeout)
                        # 4) 정상 응답 디버그 로그만 남기고, 상태 갱신은 /oms/system/connect 쪽에서 처리
                            fd_append_mtd_debug("recv", host, port, message=msg, response=resp, tag=tag)
                            return self._write(
                                200,
                                json.dumps({"ok": True, "tag": tag, "response": resp}).encode()
                            )

                        except MtdTraceError as e:
                            # 5) MTd 트레이스 에러 로그
                            fd_append_mtd_debug("error", host, port, message=msg, error=str(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": str(e)}).encode()
                            )
                        except Exception as e:
                            # 6) 기타 예외도 로그
                            fd_append_mtd_debug("error", host, port, message=msg, error=repr(e))
                            return self._write(
                                502,
                                json.dumps({"ok": False, "error": repr(e)}).encode()
                            )
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config"]:
                        if not CFG.exists():
                            return self._write(404, json.dumps({"ok":False,"error":"config not found"}).encode())
                        data = CFG.read_bytes()
                        return self._write(200, data, "text/plain; charset=utf-8")                    
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config/update
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config", "update"]:
                        try:
                            data = json.loads(body.decode("utf-8", "ignore"))
                            ok, resp, err = fd_handle_config_update(data)
                            if not ok:
                                return self._write(400,json.dumps({"ok": False, "error": err}).encode())
                            return self._write(200, json.dumps(resp).encode())
                        except Exception as e:
                            return self._write(500,json.dumps({"ok": False, "error": str(e)}).encode())
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/config/apply
                    # ──────────────────────────────────────────────────────                      
                    if parts==["oms", "config", "apply"]:
                        try: cfg=load_config(CFG)
                        except Exception as e: return self._write(400, json.dumps({"ok":False,"error":f"load_config: {e}"}).encode())
                        changed=orch.apply_runtime(cfg)
                        return self._write(200, json.dumps({"ok":True,"applied":changed}).encode())
                    # ──────────────────────────────────────────────────────
                    # 🧩 POST : /oms/alias/clear
                    # ──────────────────────────────────────────────────────                      
                    if parts == ["oms", "alias", "clear"]:
                        try:
                            cnt = len(orch._cache_alias)
                            orch._cache_alias.clear()
                            fd_log.info(f"alias cache cleared ({cnt} entries removed)")
                            return self._write(200, json.dumps({"ok":True,"cleared":cnt}).encode())
                        except Exception as e:
                            return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode()) 
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ POST : /oms/system/restart/clear
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system" ,"restart", "clear"]: 
                        orch._sys_restart_set(state=0, total=0, sent=0, done=0, fails=[], message="", started_at=0.0)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-1️⃣ POST : /oms/system/restart/all
                    # ──────────────────────────────────────────────────────                                        
                    if parts == ["oms", "system" ,"restart", "all"]: 
                        # If already running, return 409 Conflict
                        cur = orch._sys_restart_get()
                        if cur.get("state") == "running":
                            return self._write(409, json.dumps({"ok":False,"error":"already_running"}).encode())
                        # reset camera info
                        try:
                            # reset connected daemons
                            orch._sys_restart_set(state=1,total=0,sent=0,done=0,fails=[],message="Preparing…",started_at=time.time())
                            fd_log.info(f"connect state cleared before restart (ok={ok})") 
                        except Exception as e:
                            fd_log.exception(f"connect state clear failed before restart: {e}")
                        # Perform full restart in the background
                        threading.Thread(
                            target=orch._sys_restart_process,
                            args=(orch,),
                            daemon=True
                        ).start()
                        return self._write(200, json.dumps({"ok":True}).encode())
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ POST : /oms/system/connect
                    # ──────────────────────────────────────────────────────                    
                    if parts == ["oms", "system" ,"connect"]:
                        processes = orch._get_process_list() # [{name:"EMd", ips:[...]}, ...]
                        MTD_WHITELIST = {"MTd", "EMd", "CCd", "SCd", "PCd", "MMd", "AId"}
                        daemon_map = {}
                        # processes 는 list
                        for proc in processes:
                            name = proc.get("name")
                            ips = proc.get("ips") or []
                            if not name or len(ips) == 0:
                                continue
                            base = name.split("-")[0] # PreSd-1 → PreSd
                            # whitelist 이외는 MTd 로 보내지 않음
                            if base not in MTD_WHITELIST:
                                continue
                            # 중복 제거 (첫 번째 IP만)
                            if base not in daemon_map:
                                daemon_map[base] = ips[0]
                        fd_log.info(f"[PATCH] daemon_map override from process-list = {daemon_map}")
                        events = orch._sys_connect_sequence(orch.mtd_ip, orch.mtd_port, orch.mtd_ip, daemon_map)
                        return self._write(200, json.dumps({"ok": True, "events": events}).encode())
                    # ──────────────────────────────────────────────────────
                    # 1️⃣-2️⃣ POST : /oms/system/state/upsert
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "system" ,"state", "upsert"]:
                        fd_log.info(f"/oms/system/state/upsert")
                        try:
                            req = json.loads(body.decode("utf-8", "ignore") or "{}")
                        except Exception:
                            return self._write(400, b'{"ok":false,"error":"invalid json"}')
                        # 핵심: 여기서 바로 기존 함수 호출
                        fd_sys_state_upsert(req)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ POST : /oms/camera/state/upsert
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "camera" ,"state", "upsert"]:
                        fd_log.info(f"/oms/camera/state/upsert")
                        try:
                            req = json.loads(body.decode("utf-8", "ignore") or "{}")
                        except Exception:
                            return self._write(400, b'{"ok":false,"error":"invalid json"}')
                        # 핵심: 여기서 바로 기존 함수 호출
                        fd_cam_state_upsert(req)
                        return self._write(200, b'{"ok":true}')
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/reboot
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "reboot"]:                        
                        res = orch._camera_action_switch(1) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/start
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "start"]:                        
                        res = orch._camera_action_switch(2) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-1️⃣ POST : /oms/camera/action/stop
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "stop"]:                        
                        res = orch._camera_action_switch(3) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-2️⃣ POST : /oms/camera/connect/all
                    # ──────────────────────────────────────────────────────                  
                    if parts == ["oms", "camera", "connect", "all"]:
                        fd_log.info("oms/camera/connect/all")
                        try:
                            res = orch._connect_all_cameras() or {}
                            ok = bool(res.get("ok", False))

                            if "ok" not in res:
                                res["ok"] = ok
                            if not ok and "error" not in res:
                                res["error"] = "Connect failed"

                            body = json.dumps(res, ensure_ascii=False).encode("utf-8")
                            # cam-connect는 항상 200으로, 내부 ok/error 로 판단
                            return self._write(200, body)

                        except Exception as e:
                            fd_log.exception("connect_all_cameras error")
                            body = json.dumps(
                                {"ok": False, "error": str(e)},
                                ensure_ascii=False,
                            ).encode("utf-8")
                            return self._write(500, body)
                    # ──────────────────────────────────────────────────────
                    # 2️⃣-3️⃣ POST : /oms/camera/action/autofocus
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "autofocus"]:                        
                        fd_log.info(f"oms/camera/action/autofocus")
                        res = orch._camera_action_autofocus(body) or {}
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-1️⃣ POST : /oms/camera/action/record/start
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "record", "start"]:                        
                        fd_log.info(f"/oms/camera/action/record/start")
                        res = orch._camera_record_start()
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-1️⃣ POST : /oms/camera/action/record/stop
                    # ──────────────────────────────────────────────────────     
                    if parts == ["oms", "camera", "action", "record", "stop"]:                        
                        fd_log.info(f"/oms/camera/action/record/stop")
                        res = orch._camera_record_stop()
                        ok = bool(res.get("ok", False))
                        self._send_json({
                            "ok": ok,
                            "detail": res
                        })
                        return
                    # ──────────────────────────────────────────────────────
                    # 3️⃣-2️⃣ POST : /oms/camera/liveview/on , /oms/camera/liveview/off
                    # ────────────────────────────────────────────────────── 
                    if parts == ["oms", "camera" , "liveview", "on"]:
                        ok = MTX.start()
                        self._send_json({"ok": ok})
                        return
                    if parts == ["oms", "camera" , "liveview", "off"]:
                        ok = MTX.stop()
                        self._send_json({"ok": ok})
                        return
                        # 저장 경로 매칭
                    # ──────────────────────────────────────────────────────
                    # 0️⃣-1️⃣ POST : load file "/web/config/user-config.json
                    # ────────────────────────────────────────────────────── 
                    if self.path == "/web/config/user-config.json":
                        raw_body = body
                        try:
                            data = raw_body.decode("utf-8")
                            file_path = os.path.join(WEB, "config", "user-config.json")
                            with open(file_path, "w", encoding="utf-8") as f:
                                f.write(data)
                            self._send_json({"status": "ok"})
                        except Exception as e:
                            self._send_json({"status": "error", "msg": str(e)}, status=500)
                        return
                    # ──────────────────────────────────────────────────────
                    # 🔴 POST /N/O/T/ /F/O/U/N/D/
                    # ──────────────────────────────────────────────────────
                    self._write(404, b"Not Found", ct="text/plain")
                    return self._write(404, b'{"ok":false,"error":"not found"}')
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            def log_message(self, fmt, *args):
                # fd_log.info(fmt % args)
                # too many gabage message
                pass 
        return H

    def _log(self, msg:str):
        fd_log.info(msg)

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def main():
    try:
        cfg = load_config(CFG)
    except Exception as e:
        fd_log.warning(f"fallback cfg: {e}") 
        cfg = {"http_host":"0.0.0.0","http_port":19777,"heartbeat_interval_sec":2,"nodes":[]}

    # load system state
    fd_sys_state_load() 
    # load camera state
    fd_cam_state_load() 
    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
