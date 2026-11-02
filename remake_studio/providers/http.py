"""Minimal JSON-over-HTTPS client (stdlib only) with secret redaction.

Retries are deliberately split: `get` retries 429/5xx/connection errors with
backoff because reads are safe to repeat; `post` never retries, because a
repeated submit can create (and bill) a second job. A submit whose outcome is
unknown raises `UnknownOutcome` so the caller records it and recovers by id
instead of resubmitting.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {body[:300]}")
        self.status, self.body, self.url = status, body, url


class UnknownOutcome(Exception):
    """The request may or may not have reached the server."""


class Http:
    def __init__(self, base_url: str, headers: dict | None = None, secrets: tuple = (), timeout: float = 60.0,
                 retries: int = 4, backoff: float = 1.5, sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.headers = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
        self.secrets = tuple(s for s in secrets if s)
        self.timeout, self.retries, self.backoff, self.sleep = timeout, retries, backoff, sleep

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        return text

    def _url(self, path: str) -> str:
        return path if path.startswith(("http://", "https://")) else f"{self.base}/{path.lstrip('/')}"

    def _send(self, method: str, url: str, body: bytes | None, headers: dict) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def get(self, path: str, raw: bool = False):
        url = self._url(path)
        delay = 1.0
        for attempt in range(self.retries + 1):
            try:
                status, data = self._send("GET", url, None, self.headers)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == self.retries:
                    raise HttpError(0, self.redact(str(e)), self.redact(url)) from None
            else:
                if status < 400:
                    return data if raw else json.loads(data or b"null")
                if status not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise HttpError(status, self.redact(data.decode("utf-8", "replace")), self.redact(url))
            self.sleep(delay)
            delay *= self.backoff
        raise AssertionError("unreachable")

    def post(self, path: str, payload=None, raw_body: bytes | None = None, headers: dict | None = None):
        url = self._url(path)
        body = raw_body if raw_body is not None else json.dumps(payload if payload is not None else {}).encode()
        try:
            status, data = self._send("POST", url, body, {**self.headers, **(headers or {})})
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise UnknownOutcome(self.redact(f"POST {url}: {e}")) from None
        if status >= 400:
            raise HttpError(status, self.redact(data.decode("utf-8", "replace")), self.redact(url))
        return json.loads(data or b"null")

    def download(self, url: str, dest: str) -> str:
        data = self.get(url, raw=True)
        with open(dest, "wb") as f:
            f.write(data)
        return dest
