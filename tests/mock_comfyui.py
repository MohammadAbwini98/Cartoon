"""Stand-alone ComfyUI stand-in for worker tests (a separate process, so it can be
killed and restarted). Implements the endpoints the worker uses.

  python3 tests/mock_comfyui.py --port P --log L [--state S] [--seconds 2] [--frames 6]
                                [--fail "MPS backend out of memory"] [--device mps]
"""
import argparse
import json
import os
import struct
import sys
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NODES = ["LoadImage", "ImageScale", "SaveImage", "CheckpointLoaderSimple", "CLIPTextEncode", "EmptyLatentImage",
         "KSampler", "VAEDecode", "VHS_LoadVideo", "AnimeLineArtPreprocessor", "DWPreprocessor",
         "IPAdapterUnifiedLoader", "IPAdapterAdvanced", "ACN_ControlNetLoaderAdvanced", "ACN_AdvancedControlNetApply_v2",
         "ADE_StandardStaticContextOptions", "ADE_AnimateDiffLoaderGen1"]


def png(w, h, shade):
    raw = b"".join(b"\x00" + bytes([shade, 80, 160]) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def meminfo():
    try:
        d = dict(l.split(":", 1) for l in open("/proc/meminfo"))
        return int(d["MemTotal"].split()[0]) * 1024, int(d["MemAvailable"].split()[0]) * 1024
    except Exception:  # noqa: BLE001
        return 16 << 30, 8 << 30


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--state")
    ap.add_argument("--seconds", type=float, default=2.0)
    ap.add_argument("--frames", type=int, default=6)
    ap.add_argument("--fail")
    ap.add_argument("--device", default="mps")
    a = ap.parse_args()
    prompts = {}
    if a.state and os.path.exists(a.state):
        prompts = json.load(open(a.state))
    ballast = bytearray(48 << 20)   # so the process has a measurable resident size
    ballast[::4096] = b"\x01" * len(ballast[::4096])

    def save():
        if a.state:
            json.dump(prompts, open(a.state, "w"))

    def log(event, **kw):
        with open(a.log, "a") as f:
            f.write(json.dumps({"t": time.time(), "event": event, **kw}) + "\n")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *x):
            pass

        def send(self, code, obj, ctype="application/json"):
            data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

        def body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(n) if n else b""

        def do_GET(self):
            p = self.path
            if p == "/system_stats":
                tot, free = meminfo()
                return self.send(200, {"system": {"os": sys.platform, "ram_total": tot, "ram_free": free, "comfyui_version": "0.43.2-mock"},
                                       "devices": [{"name": a.device, "type": a.device, "vram_total": tot, "vram_free": free}]})
            if p == "/object_info":
                return self.send(200, {n: {} for n in NODES})
            if p == "/queue":
                now = time.time()
                run = [[0, pid] for pid, v in prompts.items() if not v.get("interrupted") and now - v["start"] < a.seconds]
                return self.send(200, {"queue_running": run, "queue_pending": []})
            if p.startswith("/history/"):
                pid = p.split("/")[2]
                v = prompts.get(pid)
                if not v or (time.time() - v["start"] < a.seconds and not v.get("interrupted")):
                    return self.send(200, {})
                if v.get("interrupted"):
                    st = {"status_str": "error", "completed": False, "messages": [["execution_interrupted", {}]]}
                    return self.send(200, {pid: {"status": st, "outputs": {}}})
                if a.fail:
                    st = {"status_str": "error", "completed": False,
                          "messages": [["execution_error", {"exception_message": a.fail, "node_type": "KSampler"}]]}
                    return self.send(200, {pid: {"status": st, "outputs": {}}})
                imgs = [{"filename": f"frame_{i + 1:05d}_.png", "subfolder": v["prefix"], "type": "output"} for i in range(a.frames)]
                return self.send(200, {pid: {"status": {"status_str": "success", "completed": True, "messages": []},
                                             "outputs": {"19": {"images": imgs}}}})
            if p.startswith("/view"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(p).query)
                i = int(q["filename"][0].split("_")[1])
                return self.send(200, png(64, 36, (i * 37) % 256), "image/png")
            self.send(404, {})

        def do_POST(self):
            b = self.body()
            if self.path == "/prompt":
                g = json.loads(b)["prompt"]
                prefix = next((n["inputs"]["filename_prefix"] for n in g.values() if n["class_type"] == "SaveImage"), "x")
                pid = f"p{len(prompts) + 1}-{os.getpid()}"
                prompts[pid] = {"start": time.time(), "prefix": prefix}
                save(); log("prompt", prompt_id=pid)
                return self.send(200, {"prompt_id": pid, "number": len(prompts), "node_errors": {}})
            if self.path == "/upload/image":
                log("upload", bytes=len(b))
                return self.send(200, {"name": "x", "subfolder": "", "type": "input"})
            if self.path == "/interrupt":
                for v in prompts.values():
                    v["interrupted"] = True
                save(); log("interrupt")
                return self.send(200, {})
            if self.path == "/queue":
                log("queue_delete", body=b.decode())
                return self.send(200, {})
            self.send(404, {})

    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    log("started", pid=os.getpid())
    srv.serve_forever()


if __name__ == "__main__":
    main()
