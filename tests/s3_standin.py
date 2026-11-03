"""S3-compatible stand-in that enforces SigV4 (header and presigned), presigned expiry,
multipart uploads, conditional writes, HEAD, ListObjectsV2 and stored Content-Type."""
import datetime as dt
import hashlib
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from remake_studio.hybrid.transport import presign_url, sigv4_authorization


class S3Standin:
    def __init__(self, access_key="AKSTANDIN", secret_key="standin-secret", bucket="media", region="auto"):
        self.ak, self.sk, self.bucket, self.region = access_key, secret_key, bucket, region
        self.objects, self.uploads, self.denied = {}, {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def reply(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def authorized(self, body: bytes) -> bool:
                url = f"http://{self.headers['Host']}{self.path}"
                u = urllib.parse.urlsplit(url)
                q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
                if "X-Amz-Signature" in q:                          # presigned
                    t = dt.datetime.strptime(q["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
                    if dt.datetime.now(dt.timezone.utc) > t + dt.timedelta(seconds=int(q["X-Amz-Expires"])):
                        return False
                    if not q["X-Amz-Credential"].startswith(outer.ak + "/"):
                        return False
                    base = {k: v for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                            if not k.startswith("X-Amz-")}
                    plain = f"{u.scheme}://{u.netloc}{u.path}" + (("?" + urllib.parse.urlencode(base)) if base else "")
                    want = presign_url(self.command, plain, outer.ak, outer.sk, outer.region, q["X-Amz-Date"],
                                       int(q["X-Amz-Expires"]))
                    return want.rsplit("X-Amz-Signature=", 1)[1] == q["X-Amz-Signature"]
                auth = self.headers.get("Authorization", "")
                if not auth.startswith(f"AWS4-HMAC-SHA256 Credential={outer.ak}/"):
                    return False
                signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
                hdrs = {k: self.headers[k] for k in signed}
                if hashlib.sha256(body).hexdigest() != self.headers.get("x-amz-content-sha256"):
                    return False
                want = sigv4_authorization(self.command, url, hdrs, self.headers["x-amz-content-sha256"], outer.ak,
                                           outer.sk, outer.region, self.headers["x-amz-date"])
                return want == auth

            def handle_any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if not self.authorized(body):
                    outer.denied.append((self.command, self.path))
                    return self.reply(403, b"<Error><Code>AccessDenied</Code></Error>")
                u = urllib.parse.urlsplit(self.path)
                key = urllib.parse.unquote(u.path.split("/", 2)[2]) if u.path.count("/") >= 2 else ""
                q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
                m = self.command
                if m == "POST" and "uploads" in q:
                    uid = uuid.uuid4().hex
                    outer.uploads[uid] = {"key": key, "parts": {}, "ctype": self.headers.get("Content-Type")}
                    return self.reply(200, f"<InitiateMultipartUploadResult><UploadId>{uid}</UploadId></InitiateMultipartUploadResult>".encode())
                if m == "PUT" and "partNumber" in q:
                    up = outer.uploads.get(q.get("uploadId"))
                    if not up or up["key"] != key:
                        return self.reply(404, b"<Error><Code>NoSuchUpload</Code></Error>")
                    etag = hashlib.md5(body).hexdigest()
                    up["parts"][int(q["partNumber"])] = (body, etag)
                    return self.reply(200, headers={"ETag": f'"{etag}"', "Access-Control-Expose-Headers": "ETag"})
                if m == "POST" and "uploadId" in q:
                    up = outer.uploads.pop(q["uploadId"], None)
                    if not up:
                        return self.reply(404, b"<Error><Code>NoSuchUpload</Code></Error>")
                    import re
                    listed = [(int(a), b) for a, b in re.findall(rb"<PartNumber>(\d+)</PartNumber><ETag>\"?([0-9a-f]+)\"?</ETag>", body)]
                    for num, et in listed:
                        if up["parts"].get(num, (None, None))[1] != et.decode():
                            return self.reply(400, b"<Error><Code>InvalidPart</Code></Error>")
                    data = b"".join(up["parts"][num][0] for num, _ in sorted(listed))
                    outer.objects[key] = (data, hashlib.md5(data).hexdigest() + "-" + str(len(listed)), up["ctype"])
                    return self.reply(200, b"<CompleteMultipartUploadResult/>")
                if m == "PUT":
                    cur = outer.objects.get(key)
                    if self.headers.get("If-None-Match") == "*" and cur:
                        return self.reply(412)
                    im = self.headers.get("If-Match")
                    if im and (not cur or f'"{cur[1]}"' != im):
                        return self.reply(412)
                    etag = hashlib.md5(body).hexdigest()
                    outer.objects[key] = (body, etag, self.headers.get("Content-Type"))
                    return self.reply(200, headers={"ETag": f'"{etag}"'})
                if m in ("GET", "HEAD") and "list-type" in q:
                    pre = q.get("prefix", "")
                    xml = "<ListBucketResult><IsTruncated>false</IsTruncated>" + "".join(
                        f"<Contents><Key>{k}</Key></Contents>" for k in sorted(outer.objects) if k.startswith(pre)) + "</ListBucketResult>"
                    return self.reply(200, xml.encode())
                if m in ("GET", "HEAD"):
                    cur = outer.objects.get(key)
                    if not cur:
                        return self.reply(404)
                    h = {"ETag": f'"{cur[1]}"'}
                    if cur[2]:
                        h["Content-Type"] = cur[2]
                    if m == "HEAD":
                        self.send_response(200)
                        for k, v in h.items():
                            self.send_header(k, v)
                        self.send_header("Content-Length", str(len(cur[0])))
                        self.end_headers()
                        return
                    return self.reply(200, cur[0], h)
                if m == "DELETE":
                    if "uploadId" in q:
                        outer.uploads.pop(q["uploadId"], None)
                    else:
                        outer.objects.pop(key, None)
                    return self.reply(204)
                self.reply(400)

            do_GET = do_PUT = do_POST = do_DELETE = do_HEAD = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
