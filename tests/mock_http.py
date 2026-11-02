"""A tiny scripted HTTP server for provider tests (127.0.0.1, random port)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockServer:
    """routes: {(METHOD, path_prefix): handler(req) -> (status, body, content_type) | "drop"}"""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                req = {"method": method, "path": self.path, "headers": dict(self.headers), "body": body}
                outer.requests.append(req)
                for (m, prefix), fn in sorted(outer.routes.items(), key=lambda kv: -len(kv[0][1])):
                    if m == method and self.path.startswith(prefix):
                        res = fn(req)
                        if res == "drop":
                            self.close_connection = True
                            self.connection.close()
                            return
                        status, payload, ctype = res if len(res) == 3 else (*res, "application/json")
                        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                        self.send_response(status)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                self.send_response(404); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, prefix=""):
        return [r for r in self.requests if r["method"] == "POST" and r["path"].startswith(prefix)]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
