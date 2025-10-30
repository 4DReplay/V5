# -*- coding: utf-8 -*-
import json, re, time, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, unquote
import socket, http.client

# ─ paths
HERE = Path(__file__).resolve()
ROOT = HERE.parents[2] if HERE.parts[-3].lower() == "service" else HERE.parent
DEFAULT_CONFIG = ROOT / "config" / "oms_config.json"
STATIC_ROOT = ROOT / "web"
LOG_DIR_DEFAULT = ROOT / "logs" / "OMS"
LOG_DIR_DEFAULT.mkdir(parents=True, exist_ok=True)

def _strip_json5(text:str)->str:
    text = re.sub(r"/\*.*?\*/","",text,flags=re.S)
    out=[]
    for line in text.splitlines():
        i=0;ins=False;q=None;buf=[]
        while i<len(line):
            ch=line[i]
            if ch in ("'",'"'):
                if not ins: ins=True;q=ch
                elif q==ch: ins=False;q=None
                buf.append(ch);i+=1;continue
            if not ins and i+1<len(line) and line[i:i+2]=="//": break
            buf.append(ch);i+=1
        out.append("".join(buf))
    t="\n".join(out)
    t=re.sub(r'(?m)(?<!["\w])([A-Za-z_]\w*)\s*:(?!\s*")', r'"\1":', t) # unquoted keys
    t=re.sub(r",\s*([\]})])", r"\1", t) # trailing comma
    return t

def load_config(p:Path)->dict:
    txt=p.read_text(encoding="utf-8")
    return json.loads(_strip_json5(txt))

def _guess_type(p: Path) -> str:
    suf = p.suffix.lower()
    if suf in (".html", ".htm"): return "text/html; charset=utf-8"
    if suf == ".css": return "text/css; charset=utf-8"
    if suf == ".js": return "application/javascript; charset=utf-8"
    if suf == ".json": return "application/json; charset=utf-8"
    if suf in (".png", ".jpg", ".jpeg", ".gif", ".svg"): return "image/" + suf.lstrip(".")
    return "application/octet-stream"

def _mime(p:Path)->str:
    s=p.suffix.lower()
    if s in (".html",".htm"): return "text/html; charset=utf-8"
    if s==".js": return "application/javascript; charset=utf-8"
    if s==".css": return "text/css; charset=utf-8"
    if s==".json": return "application/json; charset=utf-8"
    if s in (".png",".jpg",".jpeg",".gif",".svg"): return f"image/{s.lstrip('.')}"
    return "application/octet-stream"

# ─ proxy helper
def _http_fetch(host:str, port:int, method:str, path:str, body:bytes|None, headers:dict|None, timeout=4.0):
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        try: conn.close()
        except: pass

class Orchestrator:
    def __init__(self, cfg:dict):
        self.http_host = cfg.get("http_host","0.0.0.0")
        self.http_port = int(cfg.get("http_port",52050))
        self.heartbeat = float(cfg.get("heartbeat_interval_sec",2))
        self.nodes = list(cfg.get("nodes",[]))  # [{name, alias, host, port}]
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._cache = {}       # name -> last status payload
        self._cache_ts = {}    # name -> timestamp

    def _poll_once(self):
        for n in self.nodes:
            name=n.get("name") or n.get("host")
            try:
                st, _, data = _http_fetch(n["host"], int(n.get("port",51050)), "GET", "/status", None, None, timeout=2.5)
                if st==200:
                    payload = json.loads(data.decode("utf-8","ignore"))
                else:
                    payload = {"ok": False, "error": f"http {st}"}
            except Exception as e:
                payload = {"ok": False, "error": repr(e)}
            with self._lock:
                self._cache[name]=payload
                self._cache_ts[name]=time.time()

    def _loop(self):
        while not self._stop.is_set():
            self._poll_once()
            self._stop.wait(self.heartbeat)

    def run(self):
        threading.Thread(target=self._loop, daemon=True).start()
        srv = ThreadingHTTPServer((self.http_host, self.http_port), self._make_handler())
        self._log(f"[OMS] HTTP {self.http_host}:{self.http_port}")
        srv.serve_forever(poll_interval=0.5)

    def stop(self):
        self._stop.set()

    def _status(self):
        with self._lock:
            nodes=[]
            for n in self.nodes:
                nm = n.get("name") or n.get("host")
                nodes.append({
                    "name": nm,
                    "alias": n.get("alias",""),
                    "host": n["host"],
                    "port": int(n.get("port",51050)),
                    "status": self._cache.get(nm),
                    "ts": self._cache_ts.get(nm,0)
                })
            return {"ok": True, "heartbeat_interval_sec": self.heartbeat, "nodes": nodes}

    def _make_handler(self):
        orch = self
        # Redirect address
        def _serve_static_safe(handler, rel_path: str):
            # STATIC_ROOT 밑의 파일만 서빙 (디렉토리 탈출 방지)
            rel = rel_path.lstrip("/")
            fp = (STATIC_ROOT / rel).resolve()
            base = STATIC_ROOT.resolve()
            if not fp.is_file() or not str(fp).startswith(str(base)):
                handler.send_response(404)
                handler.send_header("Content-Type", "application/json; charset=utf-8")
                handler.send_header("Cache-Control", "no-store")
                body = b'{"ok": false, "error": "not found"}'
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)
                return
            data = fp.read_bytes()
            handler.send_response(200)
            handler.send_header("Content-Type", _guess_type(fp))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            try:
                handler.wfile.write(data)
            except (ConnectionAbortedError, BrokenPipeError):
                pass

        class H(BaseHTTPRequestHandler):
            def _write(self, code=200, body:bytes=b"", ct="application/json; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except BrokenPipeError: pass

            def do_GET(self):
                try:
                    parts = [p for p in self.path.split("?")[0].split("/") if p]
                    clean_path = urlsplit(self.path).path

                    # 1) ───────── /proxy/<node>/... → 해당 DMS로 프록시
                    if parts and parts[0] == "proxy" and len(parts) >= 2:
                        node_raw = parts[1]
                        node = unquote(node_raw)  # 링크의 인코딩 대비
                        target = None
                        for n in orch.nodes:
                            nm = (n.get("name") or n.get("host"))
                            if nm == node:
                                target = n
                                break
                        if not target:
                            return self._write(404, json.dumps({"ok": False, "error": "unknown node"}).encode())

                        # 전달할 서브패스(+쿼리)
                        sub = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
                        qs = urlsplit(self.path).query
                        if qs:
                            sub = f"{sub}?{qs}"

                        # 그대로 터널링
                        st, hdr, data = _http_fetch(
                            target["host"],
                            int(target.get("port", 51050)),
                            "GET",
                            sub,
                            None,
                            None,
                            timeout=4.0,
                        )
                        ct = hdr.get("Content-Type") or hdr.get("content-type") or "application/octet-stream"
                        return self._write(st, data, ct)

                    # ── [리다이렉트] / 또는 /oms → /web/oms-control.html
                    if clean_path in ("/", "/oms"):
                        self.send_response(302)
                        self.send_header("Location", "/web/oms-control.html")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return

                    # ── [정적] /web/... (그대로 파일 매핑)
                    if parts[:1] == ["web"]:
                        sub = "/".join(parts[1:])
                        return _serve_static_safe(self, sub)

                    # ── [정적] 루트에서 *.html 도 허용 (예: /oms-config.html, /oms-control.html)
                    if clean_path.endswith(".html"):
                        # clean_path 는 앞에 / 가 있으므로 lstrip
                        return _serve_static_safe(self, clean_path.lstrip("/"))

                    # ── [API] /oms/status
                    if parts == ["oms", "status"]:
                        payload = orch._status()
                        return self._write(200, json.dumps(payload).encode("utf-8"))

                    # ── [프록시] /proxy/{node}/...
                    if parts and parts[0] == "proxy" and len(parts) >= 2:
                        node = parts[1]
                        target = None
                        for n in orch.nodes:
                            nm = n.get("name") or n.get("host")
                            if nm == node:
                                target = n
                                break
                        if not target:
                            return self._write(404, json.dumps({"ok": False, "error": "unknown node"}).encode())

                        sub = "/" + "/".join(parts[2:]) if len(parts) > 2 else "/"
                        qs = urlsplit(self.path).query
                        if qs:
                            sub = f"{sub}?{qs}"
                        st, hdr, data = _http_fetch(target["host"], int(target.get("port", 51050)),
                                                    "GET", sub, None, None)
                        ct = hdr.get("Content-Type") or hdr.get("content-type") or "application/octet-stream"
                        return self._write(st, data, ct)

                    # ── [헬스] GET /status  (※ 버그 픽스: sup → orch)
                    if not parts or parts[0] == "status":
                        hb = float(getattr(orch, "heartbeat", 2))
                        return self._write(200, json.dumps({"ok": True, "heartbeat_interval_sec": hb}).encode("utf-8"))

                    # ── [설정 메타] GET /config/meta
                    if parts == ["config", "meta"]:
                        if not DEFAULT_CONFIG.exists():
                            return self._write(404, json.dumps({"ok": False, "error": "config not found"}).encode())
                        s = DEFAULT_CONFIG.stat()
                        payload = {
                            "ok": True,
                            "path": str(DEFAULT_CONFIG),
                            "size": s.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.st_mtime)),
                        }
                        return self._write(200, json.dumps(payload).encode("utf-8"))

                    # ── [설정 원문] GET /config
                    if parts == ["config"]:
                        if not DEFAULT_CONFIG.exists():
                            return self._write(404, json.dumps({"ok": False, "error": "config not found"}).encode())
                        data = DEFAULT_CONFIG.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        try:
                            self.wfile.write(data)
                        except (ConnectionAbortedError, BrokenPipeError):
                            pass
                        return
                    # ── 없다
                    return self._write(404, json.dumps({"ok": False, "error": "not found"}).encode("utf-8"))

                except Exception as e:
                    return self._write(500, json.dumps({"ok": False, "error": repr(e)}).encode("utf-8"))
            def do_POST(self):
                try:
                    parts=[p for p in self.path.split("?")[0].split("/") if p]
                    length=int(self.headers.get("Content-Length") or 0)
                    body=self.rfile.read(length)

                    # /proxy/{node}/...  (start/stop/restart 등)
                    if parts and parts[0]=="proxy" and len(parts)>=2:
                        node=parts[1]
                        target=None
                        for n in orch.nodes:
                            nm=n.get("name") or n.get("host")
                            if nm==node: target=n; break
                        if not target:
                            return self._write(404, json.dumps({"ok":False,"error":"unknown node"}).encode())
                        sub="/"+"/".join(parts[2:]) or "/"
                        qs = urlsplit(self.path).query
                        if qs: sub = f"{sub}?{qs}"
                        st, hdr, data = _http_fetch(target["host"], int(target.get("port",51050)), "POST", sub, body, {"Content-Type": self.headers.get("Content-Type","application/json")})
                        ct = hdr.get("Content-Type") or hdr.get("content-type") or "application/json"
                        return self._write(st, data, ct)

                    # 설정 저장: POST /config  (선택사항)
                    if parts==["config"]:
                        DEFAULT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
                        txt = body.decode("utf-8","ignore")
                        DEFAULT_CONFIG.write_text(txt, encoding="utf-8")
                        return self._write(200, json.dumps({"ok":True}).encode())

                    return self._write(404, json.dumps({"ok":False,"error":"not found"}).encode())
                except Exception as e:
                    return self._write(500, json.dumps({"ok":False,"error":repr(e)}).encode())

            def log_message(self, fmt, *args):
                orch._log("[HTTP] " + (fmt % args))
        return H

    def _log(self, msg:str):
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
        (LOG_DIR_DEFAULT / "OMS.log").open("a", encoding="utf-8").write(line)

def main():
    try:
        cfg = load_config(DEFAULT_CONFIG)
    except Exception as e:
        (LOG_DIR_DEFAULT / "OMS.log").open("a", encoding="utf-8").write(f"[WARN] fallback cfg: {e}\n")
        cfg = {"http_host":"0.0.0.0","http_port":52050,"heartbeat_interval_sec":2,"nodes":[]}
    Orchestrator(cfg).run()

if __name__ == "__main__":
    main()
