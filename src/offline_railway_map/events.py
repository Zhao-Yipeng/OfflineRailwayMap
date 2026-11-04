"""Server-sent events from the backend to the page (SPEC §10.2)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Iterator

log = logging.getLogger(__name__)

_CLOSE = object()


class EventBus:
    """Fan-out of JSON events to every connected page."""

    def __init__(self) -> None:
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, event: str, data) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        message = f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(message)
            except queue.Full:
                # A page that stopped reading must not block the backend.
                log.warning("dropping event %s for a slow subscriber", event)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(_CLOSE)
            except queue.Full:
                pass

    def stream(self, initial: list[bytes] | None = None, heartbeat: float = 15.0) -> Iterator[bytes]:
        """Yield SSE frames until the bus is closed or the client goes away."""
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            if self._closed:
                return
            self._subscribers.add(q)
        try:
            yield b"retry: 2000\n\n"
            for frame in initial or ():
                yield frame
            while True:
                try:
                    item = q.get(timeout=heartbeat)
                except queue.Empty:
                    yield b": ping\n\n"
                    continue
                if item is _CLOSE:
                    return
                yield item
        finally:
            with self._lock:
                self._subscribers.discard(q)


def frame(event: str, data) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
