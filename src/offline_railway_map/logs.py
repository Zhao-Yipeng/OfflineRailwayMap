"""Logging (SPEC §11.2): log files are written only while the data folder exists.

Until then records are kept in a small in-memory ring buffer.  When the data
folder appears, the buffer is flushed into ``data/logs/app.log``.
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
import threading

from .paths import Home

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

_lock = threading.Lock()
_ring: collections.deque[logging.LogRecord] = collections.deque(maxlen=2000)
_file_handler: logging.Handler | None = None


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if _file_handler is None:
            _ring.append(record)


def setup(home: Home, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(_RingHandler())
    if sys.stderr is not None:  # windowed builds have no console
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(stream)
    if home.data_exists():
        attach_file(home)
    home.on_data_created(lambda: attach_file(home))


def attach_file(home: Home) -> None:
    global _file_handler
    with _lock:
        if _file_handler is not None:
            return
        home.logs_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            home.logs_dir / "app.log", maxBytes=2 * 1024 * 1024, backupCount=4, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(FORMAT))
        for record in list(_ring):
            handler.handle(record)
        _ring.clear()
        logging.getLogger().addHandler(handler)
        _file_handler = handler


def detach_file() -> None:
    """Close the log file so the data folder can be deleted."""
    global _file_handler
    with _lock:
        if _file_handler is None:
            return
        logging.getLogger().removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
