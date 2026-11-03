"""Shared job store reachable by outbound connections only.

LocalDirTransport: a directory (tests; also a folder both sides can reach).
S3Transport: any S3-compatible bucket (AWS S3, Cloudflare R2, Backblaze B2),
path-style requests signed with AWS Signature V4, standard library only.
Conditional writes (If-None-Match: * / If-Match: etag) make claims race-free;
`selftest` checks that the bucket actually honours them.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import hmac
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class Conflict(Exception):
    """A conditional write lost (object exists / changed)."""


class Transport:
    def get(self, key: str) -> bytes | None: ...
    def get_etag(self, key: str) -> tuple[bytes, str] | None: ...
    def put(self, key: str, data: bytes, if_none_match: bool = False, if_match: str | None = None) -> str: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...

    def selftest(self, prefix: str = "selftest/") -> list[str]:
        """Verify read/write/list/delete and conditional-write semantics on the real store."""
        problems, key = [], f"{prefix}probe-{os.getpid()}-{os.urandom(4).hex()}"
        try:
            et = self.put(key, b"one", if_none_match=True)
            if self.get(key) != b"one":
                problems.append("read-after-write mismatch")
            try:
                self.put(key, b"two", if_none_match=True)
                problems.append("If-None-Match:* was not enforced (claims would race)")
            except Conflict:
                pass
            et2 = self.put(key, b"three", if_match=et)
            try:
                self.put(key, b"four", if_match=et)
                problems.append("If-Match with a stale etag was not enforced")
            except Conflict:
                pass
            if key not in self.list(prefix):
                problems.append("list did not return the probe")
            _ = et2
        finally:
            try:
                self.delete(key)
            except Exception as e:  # noqa: BLE001
                problems.append(f"delete failed: {e}")
        if self.get(key) is not None:
            problems.append("object still present after delete")
        return problems


class LocalDirTransport(Transport):
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lockfile = os.path.join(self.root, ".lock")

    def _path(self, key: str) -> str:
        p = os.path.abspath(os.path.join(self.root, key))
        if not p.startswith(self.root + os.sep):
            raise ValueError(f"key escapes the store: {key}")
        return p

    def _locked(self):
        f = open(self._lockfile, "a+")
        fcntl.flock(f, fcntl.LOCK_EX)
        return f

    def get(self, key):
        r = self.get_etag(key)
        return r[0] if r else None

    def get_etag(self, key):
        try:
            with open(self._path(key), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            return None
        return data, hashlib.sha256(data).hexdigest()

    def put(self, key, data, if_none_match=False, if_match=None):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        lock = self._locked()
        try:
            cur = self.get_etag(key)
            if if_none_match and cur is not None:
                raise Conflict(key)
            if if_match is not None and (cur is None or cur[1] != if_match):
                raise Conflict(key)
            tmp = f"{p}.tmp{os.getpid()}"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            return hashlib.sha256(data).hexdigest()
        finally:
            lock.close()

    def list(self, prefix):
        base = self._path(prefix) if prefix.rstrip("/") else self.root
        d = base if os.path.isdir(base) else os.path.dirname(base)
        out = []
        for dirpath, _, files in os.walk(d):
            for n in files:
                if n == ".lock" or ".tmp" in n:
                    continue
                k = os.path.relpath(os.path.join(dirpath, n), self.root).replace(os.sep, "/")
                if k.startswith(prefix):
                    out.append(k)
        return sorted(out)

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


# ----------------------------------------------------------------- SigV4
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sigv4_authorization(method: str, url: str, headers: dict, payload_sha256: str, access_key: str,
                        secret_key: str, region: str, amz_date: str, service: str = "s3") -> str:
    """Authorization header value for an AWS Signature Version 4 request.
    `headers` must already contain host, x-amz-date and x-amz-content-sha256."""
    u = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(u.path or "/"), safe="/-_.~")
    q = sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    query = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in q)
    h = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed = ";".join(sorted(h))
    canon_headers = "".join(f"{k}:{h[k]}\n" for k in sorted(h))
    canonical_request = "\n".join([method, path, query, canon_headers, signed, payload_sha256])
    day = amz_date[:8]
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()])
    k = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), day), region), service), "aws4_request")
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={sig}"


class S3Transport(Transport):
    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "auto",
                 timeout: float = 120.0, now=lambda: dt.datetime.now(dt.timezone.utc)):
        self.endpoint, self.bucket = endpoint.rstrip("/"), bucket
        self.ak, self.sk, self.region, self.timeout, self.now = access_key, secret_key, region, timeout, now

    def _request(self, method, key="", query="", data=b"", extra=None):
        url = f"{self.endpoint}/{self.bucket}/{urllib.parse.quote(key, safe='/-_.~')}" + (f"?{query}" if query else "")
        amz_date = self.now().strftime("%Y%m%dT%H%M%SZ")
        payload = hashlib.sha256(data).hexdigest()
        headers = {"host": urllib.parse.urlsplit(url).netloc, "x-amz-date": amz_date, "x-amz-content-sha256": payload,
                   **(extra or {})}
        headers["Authorization"] = sigv4_authorization(method, url, headers, payload, self.ak, self.sk, self.region, amz_date)
        req = urllib.request.Request(url, data=data if method in ("PUT", "POST") else None, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def get_etag(self, key):
        st, h, body = self._request("GET", key)
        if st == 404:
            return None
        if st >= 300:
            raise IOError(f"S3 GET {key}: HTTP {st} {body[:200]!r}")
        return body, (h.get("ETag") or h.get("etag") or "").strip('"')

    def get(self, key):
        r = self.get_etag(key)
        return r[0] if r else None

    def put(self, key, data, if_none_match=False, if_match=None):
        extra = {}
        if if_none_match:
            extra["If-None-Match"] = "*"
        if if_match is not None:
            extra["If-Match"] = f'"{if_match}"'
        st, h, body = self._request("PUT", key, data=data, extra=extra)
        if st in (409, 412):
            raise Conflict(key)
        if st >= 300:
            raise IOError(f"S3 PUT {key}: HTTP {st} {body[:200]!r}")
        return (h.get("ETag") or h.get("etag") or "").strip('"')

    def list(self, prefix):
        keys, token = [], None
        while True:
            q = {"list-type": "2", "prefix": prefix}
            if token:
                q["continuation-token"] = token
            st, _, body = self._request("GET", "", urllib.parse.urlencode(sorted(q.items())))
            if st >= 300:
                raise IOError(f"S3 LIST {prefix}: HTTP {st} {body[:200]!r}")
            root = ET.fromstring(body)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            keys += [c.findtext(f"{ns}Key") for c in root.findall(f"{ns}Contents")]
            if root.findtext(f"{ns}IsTruncated") == "true":
                token = root.findtext(f"{ns}NextContinuationToken")
            else:
                return sorted(keys)

    def delete(self, key):
        st, _, body = self._request("DELETE", key)
        if st >= 300 and st != 404:
            raise IOError(f"S3 DELETE {key}: HTTP {st} {body[:200]!r}")


def from_config(cfg: dict, env=os.environ) -> Transport:
    if cfg["type"] == "local":
        return LocalDirTransport(os.path.expanduser(cfg["root"]))
    if cfg["type"] == "s3":
        return S3Transport(cfg["endpoint"], cfg["bucket"], env[cfg["access_key_env"]], env[cfg["secret_key_env"]],
                           cfg.get("region", "auto"))
    raise ValueError(f"unknown transport {cfg['type']!r}")
