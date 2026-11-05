"""Choosing, queueing, updating and deleting cells (SPEC §6.3–6.5, §9.6)."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Callable

from .coverage import CellCoverage
from .events import EventBus
from .grid import cell_center, cell_id, distance_km, parse_cell_id
from .paths import Home
from .store import StateStore

log = logging.getLogger(__name__)


def public_cell(row: dict, live: dict | None = None) -> dict:
    """A cell for the page; ``live`` adds what the downloader is doing right now."""
    live = live or {}
    return {
        "id": row["id"],
        "x": row["x"],
        "y": row["y"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": round(row["progress"] or 0, 3),
        "error": row["error"],
        "updateKind": row["update_kind"],
        "railAt": row["rail_at"],
        "basemapAt": row["basemap_at"],
        "basemapMaxzoom": row["basemap_maxzoom"],
        "bytes": (row["rail_bytes"] or 0) + (row["basemap_bytes"] or 0),
        "activity": live.get("text"),
        "activitySince": live.get("since"),
        "activityUntil": live.get("until"),
        "receivedBytes": live.get("bytes", 0),
    }


class CellService:
    def __init__(
        self,
        home: Home,
        store: StateStore,
        coverage: CellCoverage,
        events: EventBus,
        default_basemap_maxzoom: Callable[[], int] = lambda: 14,
    ) -> None:
        self.home = home
        self.store = store
        self.coverage = coverage
        self.events = events
        self.default_basemap_maxzoom = default_basemap_maxzoom
        self.tiles_version = 1
        self._lock = threading.RLock()
        self._on_queue: list[Callable[[], None]] = []
        self._on_delete: list[Callable[[list[str]], None]] = []
        self._on_coverage: list[Callable[[], None]] = []
        # What the downloader is doing per cell (memory only, SPEC §6.5).
        self._live: dict[str, dict] = {}
        self._live_lock = threading.Lock()

    # -- wiring ----------------------------------------------------------------------------

    def start(self) -> None:
        self.store.reset_interrupted()
        self.sync_coverage(publish=False)

    def on_queue(self, callback: Callable[[], None]) -> None:
        self._on_queue.append(callback)

    def on_delete(self, callback: Callable[[list[str]], None]) -> None:
        self._on_delete.append(callback)

    def on_coverage(self, callback: Callable[[], None]) -> None:
        self._on_coverage.append(callback)

    # -- reading -------------------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self.store.get_meta("paused") == "1"

    def snapshot(self) -> dict:
        rows = self.store.cells()
        zooms = [r["basemap_maxzoom"] for r in rows if r["status"] in ("cached", "updating") and r["basemap_maxzoom"]]
        with self._live_lock:
            live = {cid: dict(info) for cid, info in self._live.items()}
        return {
            "cells": [public_cell(row, live.get(row["id"])) for row in rows],
            "paused": self.paused,
            "tilesVersion": self.tiles_version,
            # The basemap source can only have one max zoom: the lowest of the cached cells.
            "basemapMaxzoom": min(zooms) if zooms else self.default_basemap_maxzoom(),
        }

    def publish_cells(self) -> None:
        self.events.publish("cells", self.snapshot())

    def publish_cell(self, cid: str) -> None:
        row = self.store.cell(cid)
        if row:
            with self._live_lock:
                live = dict(self._live.get(cid, {}))
            self.events.publish("cell", public_cell(row, live))

    # -- live download information --------------------------------------------------------

    def set_activity(self, cid: str, text: str, since: float | None = None, until: float | None = None) -> None:
        with self._live_lock:
            info = self._live.setdefault(cid, {"bytes": 0})
            info.update(text=text, since=since, until=until)

    def add_received(self, cid: str, size: int) -> None:
        with self._live_lock:
            info = self._live.setdefault(cid, {"bytes": 0, "text": None, "since": None, "until": None})
            info["bytes"] = info.get("bytes", 0) + size

    def clear_live(self, cid: str) -> None:
        with self._live_lock:
            self._live.pop(cid, None)

    # -- changes ------------------------------------------------------------------------------

    def apply(self, add: list[str], remove: list[str], center: tuple[float, float] | None) -> dict:
        """Apply the edits made in edit mode when "开始浏览" is clicked (SPEC §6.3)."""
        add_cells = [parse_cell_id(c) for c in add]
        remove_ids = [cell_id(*parse_cell_id(c)) for c in remove]
        with self._lock:
            if remove_ids:
                self.delete(remove_ids, publish=False)
            queued: list[str] = []
            if add_cells:
                lon, lat = center or (0.0, 0.0)
                existing = [c["queue_order"] or 0 for c in self.store.cells()]
                base = max(existing, default=0.0) + 1.0
                ordered = sorted(add_cells, key=lambda xy: distance_km(lon, lat, *cell_center(*xy)))
                entries = [(cell_id(x, y), x, y, base + i * 1e-3) for i, (x, y) in enumerate(ordered)]
                queued = self.store.queue_cells(entries)
        self.publish_cells()
        if queued:
            self._fire(self._on_queue)
        return {"queued": queued, "removed": remove_ids}

    def delete(self, ids: list[str], publish: bool = True) -> list[str]:
        """Delete cells and everything stored for them."""
        ids = [cell_id(*parse_cell_id(c)) for c in ids]
        if not self.store.exists:
            return []
        with self._lock:
            rows = self.store.delete_cells(ids)
            removed = [row["id"] for row in rows]
            if removed:
                for callback in list(self._on_delete):
                    try:
                        callback(removed)
                    except Exception:
                        log.exception("delete listener failed")
                for cid in removed:
                    shutil.rmtree(self.home.cell_dir(cid), ignore_errors=True)
                    shutil.rmtree(self.home.tmp_dir / cid, ignore_errors=True)
            if any(row["status"] in ("cached", "updating") for row in rows):
                self.sync_coverage()
        if publish and removed:
            self.publish_cells()
        return removed

    def retry(self, ids: list[str]) -> list[str]:
        rows = [self.store.cell(cell_id(*parse_cell_id(c))) for c in ids]
        failed = [r for r in rows if r and r["status"] == "failed"]
        order = time.time()
        queued = self.store.queue_cells([(r["id"], r["x"], r["y"], order + i * 1e-3) for i, r in enumerate(failed)])
        self.publish_cells()
        if queued:
            self._fire(self._on_queue)
        return queued

    def request_update(self, cid: str, with_basemap: bool) -> bool:
        cid = cell_id(*parse_cell_id(cid))
        ok = self.store.request_update(cid, "all" if with_basemap else "rail", time.time())
        if ok:
            self.publish_cells()
            self._fire(self._on_queue)
        return ok

    def update_all_rail(self) -> int:
        count = 0
        for row in self.store.cells():
            if row["status"] == "cached" and self.store.request_update(row["id"], "rail", time.time() + count * 1e-3):
                count += 1
        if count:
            self.publish_cells()
            self._fire(self._on_queue)
        return count

    def set_paused(self, paused: bool) -> None:
        if not paused and not self.store.exists:
            return
        self.store.set_meta("paused", "1" if paused else "0")
        self.publish_cells()
        if not paused:
            self._fire(self._on_queue)

    def sync_coverage(self, publish: bool = True) -> None:
        cells = self.store.cached_cells()
        if cells == set(self.coverage.cells()):
            return
        self.coverage.replace(cells)
        self.tiles_version += 1
        for callback in list(self._on_coverage):
            try:
                callback()
            except Exception:
                log.exception("coverage listener failed")
        if publish:
            self.events.publish("tiles", {"version": self.tiles_version})

    def mark_failed(self, cid: str, message: str) -> None:
        """A cached cell turned out to be unusable (e.g. damaged database)."""
        row = self.store.cell(cid)
        if row is None or row["status"] == "failed":
            return
        self.store.update_cell(cid, status="failed", error=message, update_kind=None, stage=None, progress=0)
        self.sync_coverage()
        self.publish_cells()

    def bump_tiles(self) -> None:
        """Make the page reload local tiles (a cached cell's data changed)."""
        self.tiles_version += 1
        self.events.publish("tiles", {"version": self.tiles_version})

    def reset(self) -> None:
        """After the data folder was deleted: nothing is cached any more."""
        with self._live_lock:
            self._live.clear()
        self.coverage.replace(set())
        self.tiles_version += 1
        self.events.publish("tiles", {"version": self.tiles_version})
        self.publish_cells()

    @staticmethod
    def _fire(callbacks) -> None:
        for callback in list(callbacks):
            try:
                callback()
            except Exception:
                log.exception("cell listener failed")
