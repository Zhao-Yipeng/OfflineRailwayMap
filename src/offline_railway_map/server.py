"""Local HTTP server that serves the page, the API and all map tiles (SPEC §10.1).

* bound to 127.0.0.1 on a random port;
* every path starts with a random 32-hex-digit token generated at start-up;
* requests with a wrong token or a wrong Host header are rejected.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import parse_qsl, unquote, urlsplit

log = logging.getLogger(__name__)

MAX_BODY = 1 << 20

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".pbf": "application/x-protobuf",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}


@dataclass
class Request:
    method: str
    path: str  # path after the token prefix, e.g. "/api/info"
    query: dict[str, str]
    headers: object
    body: bytes = b""
    params: dict[str, str] = field(default_factory=dict)

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/octet-stream"
    headers: dict[str, str] = field(default_factory=dict)
    stream: Iterator[bytes] | None = None


def json_response(data, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})


def error_response(status: int, message: str) -> Response:
    return json_response({"error": message}, status)


Handler = Callable[[Request], Response]


class Router:
    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        self._routes.append((method.upper(), re.compile(f"^{pattern}$"), handler))

    def resolve(self, method: str, path: str) -> tuple[Handler | None, dict[str, str], int]:
        """Return (handler, params, status); status is 404/405 when no handler fits."""
        method_mismatch = False
        for route_method, regex, handler in self._routes:
            match = regex.match(path)
            if not match:
                continue
            if route_method == method or (method == "HEAD" and route_method == "GET"):
                return handler, match.groupdict(), 200
            method_mismatch = True
        return None, {}, 405 if method_mismatch else 404


class LocalServer:
    """One listening port.  The program runs two: the main one (page, API,
    local tiles) and one for online tiles, so that slow internet requests never
    use up the browser's per-host connections needed for local data."""

    def __init__(self, router: Router, static_dir: Path | None, port: int = 0, token: str | None = None) -> None:
        self.router = router
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.token = token or secrets.token_hex(16)
        # Origin of the page allowed to read responses from this port (the
        # online-tile port is a different origin than the page).
        self.cors_origin: str | None = None
        self._requested_port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------------

    def start(self) -> None:
        handler_cls = type("BoundHandler", (_RequestHandler,), {"local": self})
        httpd = ThreadingHTTPServer(("127.0.0.1", self._requested_port), handler_cls)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="local-server", daemon=True)
        self._thread.start()
        log.info("local server listening on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server not started"
        return self._httpd.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def base_url(self) -> str:
        return f"{self.origin}/{self.token}"

    # -- dispatch ----------------------------------------------------------------------

    def dispatch(self, method: str, raw_path: str, headers, body: bytes) -> Response:
        if headers.get("Host") != f"127.0.0.1:{self.port}":
            return error_response(403, "forbidden")
        parts = urlsplit(raw_path)
        prefix = f"/{self.token}"
        if parts.path != prefix and not parts.path.startswith(prefix + "/"):
            return error_response(404, "not found")
        path = unquote(parts.path[len(prefix):]) or "/"
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if self.static_dir is not None and path in ("/", "/app"):
            return Response(302, headers={"Location": f"{prefix}/app/index.html"})
        if self.static_dir is not None and path.startswith("/app/"):
            if method not in ("GET", "HEAD"):
                return error_response(405, "method not allowed")
            return self._static(path[len("/app/"):])
        handler, params, status = self.router.resolve(method, path)
        if handler is None:
            return error_response(status, "not found" if status == 404 else "method not allowed")
        request = Request(method, path, query, headers, body, params)
        return handler(request)

    def _static(self, relative: str) -> Response:
        assert self.static_dir is not None
        if not relative or relative.endswith("/"):
            relative += "index.html"
        target = (self.static_dir / relative).resolve()
        if self.static_dir not in target.parents or not target.is_file():
            return error_response(404, "not found")
        content_type = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        cache = "no-cache" if target.suffix in (".html", ".js", ".css") else "max-age=86400"
        return Response(200, target.read_bytes(), content_type, {"Cache-Control": cache})


class _RequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OfflineRailwayMap"
    sys_version = ""
    local: LocalServer  # set on the bound subclass

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - BaseHTTPRequestHandler API
        log.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle("GET")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def _handle(self, method: str) -> None:
        body = b""
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            if length > MAX_BODY:
                self._send(error_response(413, "request too large"), head=False)
                return
            body = self.rfile.read(length)
        try:
            response = self.local.dispatch(method, self.path, self.headers, body)
        except Exception:
            log.exception("request failed: %s %s", method, self.path)
            response = error_response(500, "internal error")
        try:
            self._send(response, head=(method == "HEAD"))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.debug("client went away: %s", self.path)

    def _send(self, response: Response, head: bool) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.local.cors_origin:
            self.send_header("Access-Control-Allow-Origin", self.local.cors_origin)
        for key, value in response.headers.items():
            self.send_header(key, value)
        if response.stream is not None:
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            if head:
                return
            stream = response.stream
            try:
                for chunk in stream:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
            return
        self.send_header("Content-Length", str(len(response.body)))
        self.end_headers()
        if not head and response.body:
            self.wfile.write(response.body)
