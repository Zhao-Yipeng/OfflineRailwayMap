from __future__ import annotations

import http.client
import json
from dataclasses import dataclass, field

import pytest

from offline_railway_map.core import AppCore, ShellHooks
from offline_railway_map.paths import Home
from offline_railway_map.settings import Settings


@pytest.fixture
def home(tmp_path) -> Home:
    return Home(tmp_path)


@dataclass
class RecordingHooks(ShellHooks):
    opened: list[str] = field(default_factory=list)
    fatal_reasons: list[str] = field(default_factory=list)

    def open_external(self, url: str) -> None:
        self.opened.append(url)

    def fatal(self, reason: str) -> None:
        self.fatal_reasons.append(reason)


@dataclass
class Client:
    core: AppCore

    def request(self, method: str, path: str, body=None, *, token: bool = True, host: str | None = None):
        server = self.core.server
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        full = (f"/{server.token}" if token else "") + path
        headers = {"Host": host or f"127.0.0.1:{server.port}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, full, body=data, headers=headers)
        response = conn.getresponse()
        payload = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), payload

    def get_json(self, path: str):
        status, _, payload = self.request("GET", path)
        return status, json.loads(payload)

    def post_json(self, path: str, body):
        status, _, payload = self.request("POST", path, body)
        return status, json.loads(payload)


@pytest.fixture
def hooks() -> RecordingHooks:
    return RecordingHooks()


@pytest.fixture
def core(home, hooks):
    app = AppCore(home, Settings(), hooks=hooks)
    app.start()
    yield app
    app.shutdown()


@pytest.fixture
def client(core) -> Client:
    return Client(core)
