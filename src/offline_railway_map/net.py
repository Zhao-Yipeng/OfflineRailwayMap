"""Outgoing HTTP (SPEC §10.3, §12.1) and the connectivity status shown in the UI.

* every request carries the program's own User-Agent and no Referer;
* at most ``per_host`` concurrent requests per remote host;
* the network is considered offline after a connection-level failure and
  online again after any successful response (SPEC §13).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from . import USER_AGENT
from .events import EventBus


class Http:
    def __init__(self, per_host: int = 2) -> None:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        self.session = session
        self._per_host = per_host
        self._slots: dict[str, threading.BoundedSemaphore] = defaultdict(
            lambda: threading.BoundedSemaphore(self._per_host)
        )
        self._slots_lock = threading.Lock()

    @contextmanager
    def host_slot(self, url: str):
        host = urlsplit(url).netloc
        with self._slots_lock:
            slot = self._slots[host]
        with slot:
            yield

    def get(self, url: str, *, timeout: float, headers: dict | None = None, **kwargs) -> requests.Response:
        with self.host_slot(url):
            return self.session.get(url, timeout=timeout, headers=headers, **kwargs)

    def post(self, url: str, *, timeout: float, data=None, headers: dict | None = None, **kwargs) -> requests.Response:
        return self.session.post(url, timeout=timeout, data=data, headers=headers, **kwargs)


class NetStatus:
    """Connectivity and per-service status, pushed to the page as ``status`` events."""

    # Connection-level failures in a row before the UI says "offline".  One
    # unreachable server should not make the whole program look offline.
    OFFLINE_AFTER = 3

    def __init__(self, events: EventBus) -> None:
        self._events = events
        self._lock = threading.Lock()
        self._network = "online"
        self._failures = 0
        self._services: dict[str, dict] = {}

    def snapshot(self) -> dict:
        with self._lock:
            return {"network": self._network, "services": {k: dict(v) for k, v in self._services.items()}}

    def _publish(self) -> None:
        self._events.publish("status", self.snapshot())

    @property
    def online(self) -> bool:
        with self._lock:
            return self._network == "online"

    def network_ok(self) -> None:
        with self._lock:
            self._failures = 0
            changed = self._network != "online"
            self._network = "online"
        if changed:
            self._publish()

    def network_down(self) -> None:
        with self._lock:
            self._failures += 1
            changed = self._failures >= self.OFFLINE_AFTER and self._network != "offline"
            if changed:
                self._network = "offline"
        if changed:
            self._publish()

    def service(self, name: str, state: str, message: str = "", until: float | None = None) -> None:
        """state: 'ok', 'paused' (rate limited / backing off) or 'blocked' (refused by the server)."""
        entry = {"state": state, "message": message, "until": until}
        with self._lock:
            changed = self._services.get(name) != entry
            if state == "ok":
                changed = name in self._services
                self._services.pop(name, None)
            else:
                self._services[name] = entry
        if changed:
            self._publish()


def is_connection_problem(exc: Exception) -> bool:
    """True for failures that mean "no network" rather than "server said no"."""
    return isinstance(exc, (requests.ConnectionError, requests.Timeout))


def now() -> float:
    return time.time()
