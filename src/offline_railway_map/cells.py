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


def public_cell(row: dict) -> dict:
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
    }


class CellService:
    def __init__(self, home: Home, store: StateStore, coverage: CellCoverage, events: EventBus) -> None:
        self.home = home
        self.store = store
        self.coverage = coverage
        self.events = events
        self.tiles_version = 1
        self._lock = threading.RLock()
        self._on_queue: list[Callable[[], None]] = []
        self._on_delete: list[Callable[[list[str]], None]] = []
        self._on_coverage: list[Callable[[], None]] = []

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
        return {
            "cells": [public_cell(row) for row in self.store.cells()],
            "paused": self.paused,
            "tilesVersion": self.tiles_version,
        }

    def publish_cells(self) -> None:
        self.events.publish("cells", self.snapshot())

    def publish_cell(self, cid: str) -> None:
        row = self.store.cell(cid)
        if row:
            self.events.publish("cell", public_cell(row))

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

    def reset(self) -> None:
        """After the data folder was deleted: nothing is cached any more."""
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
