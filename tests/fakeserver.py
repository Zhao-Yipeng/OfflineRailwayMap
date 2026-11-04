"""A tiny HTTP server for tests: routes map a path to a callable returning
(status, headers, body); every request is recorded."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

Reply = tuple[int, dict[str, str], bytes]


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeServer:
    routes: dict[str, Callable[["Recorded"], Reply]] = field(default_factory=dict)
    default: Callable[["Recorded"], Reply] | None = None
    requests: list[Recorded] = field(default_factory=list)

    def start(self) -> "FakeServer":
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                path = self.path.split("?")[0]
                rec = Recorded(self.command, self.path, {k: v for k, v in self.headers.items()}, body)
                fake.requests.append(rec)
                route = fake.routes.get(path) or fake.default
                status, headers, payload = route(rec) if route else (404, {}, b"")
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_HEAD = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def count(self, prefix: str = "") -> int:
        return sum(1 for r in self.requests if r.path.startswith(prefix))
