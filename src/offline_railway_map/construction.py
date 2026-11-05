"""Lines under construction in uncached areas (setting 「永远显示在建线路」, SPEC §7.9).

The ORM speed tiles draw lines under construction from z9 (main and branch
lines; subways from z10, trams from z11 ...), but below z9 they draw none,
and no public service offers such pictures.  With this setting on, the
program fetches the railway lines under construction (``railway=construction``
of type rail, without ``service``) of the area being looked at from Overpass
and draws them the way cached cells draw them: at every zoom, coloured by
speed, with the same widths (SPEC §7.3–7.5).

* Only overviews (z8 and below) fetch anything; closer views use what is
  already there, and the ORM tiles draw the rest.  Data areas are map tiles:
  a view at zoom z needs the areas at ``data_zoom(z)`` (z3 up to z6, z4 at
  z7, z5 at z8).  They are fetched one at a time, the most recently needed
  first; an area the servers refuse is asked for as its four quarters.
* Kept in ``data/cache/construction.sqlite`` for ``FRESH_FOR`` (7 days); after
  that an area is fetched again when needed, and the old data is shown until
  then.  A bigger area replaces the smaller ones inside it.
* Vector tiles are made when the page asks for them, from the areas touching
  the tile.  Inside cached cells (z >= 8) they are empty: a cached cell draws
  its own lines.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import numpy as np
import shapely
from shapely.geometry import LineString, box

from . import overpass
from .classify import classify
from .coverage import CellCoverage
from .grid import CELL_ZOOM, tile_bounds
from .net import NetStatus
from .overpass import AreaTooBig, OverpassClient
from .paths import DataBlocked, Home
from .processing import (
    BUFFER,
    LOD_LEVELS,
    _encode_tile,
    _Line,
    encode_lowzoom,
    line_props,
    lod_tolerance,
    tile_bounds_m,
    to_mercator,
)
from .speed import speed_label

log = logging.getLogger(__name__)

# Railway lines under construction, the ones drawn below z9 (SPEC §7.3 rules
# 16-17).  Subways, trams, sidings and yards under construction appear from
# z10 and the ORM tiles draw them there; leaving them out makes the answers
# several times smaller (Beijing: 4 of 48 ways under construction).
QUERY = (
    "[out:json][timeout:{timeout}];\n"
    "(\n"
    'way["railway"="construction"]["construction"="rail"][!"service"]({s},{w},{n},{e});\n'
    'way["railway"="construction"]["construction:railway"="rail"][!"service"]({s},{w},{n},{e});\n'
    ");\n"
    "out tags geom;"
)
TOP_ZOOM = 3  # the biggest areas asked for (views up to z6)
LAST_FETCH_ZOOM = 8  # closer views fetch nothing: the ORM tiles draw these lines there
MAX_SPLIT_ZOOM = 8  # an area the servers refuse is split down to this size
SERVER_TIMEOUT = 180
FRESH_FOR = 7 * 24 * 3600.0
RETRY_AFTER = 60.0  # after the servers could not be reached or were busy
PUBLISH_EVERY = 2.0  # seconds between "new data" events to the page
MAX_LOADED_AREAS = 96
MAX_TILES = 512
QUEUE_LIMIT = 256

Key = tuple[int, int, int]


def data_zoom(view_zoom: int) -> int:
    """Size of the areas a view at ``view_zoom`` (at most ``LAST_FETCH_ZOOM``) needs."""
    return max(TOP_ZOOM, min(LAST_FETCH_ZOOM, view_zoom) - 3)


def build_query(key: Key, timeout: int = SERVER_TIMEOUT) -> str:
    west, south, east, north = tile_bounds(*key)
    return QUERY.format(timeout=timeout, s=f"{south:.7f}", w=f"{west:.7f}", n=f"{north:.7f}", e=f"{east:.7f}")


def _children(key: Key) -> list[Key]:
    z, x, y = key
    return [(z + 1, 2 * x + dx, 2 * y + dy) for dy in (0, 1) for dx in (0, 1)]


def _contains(outer: Key, inner: Key) -> bool:
    """True if tile ``outer`` contains tile ``inner`` (or is it)."""
    if outer[0] > inner[0]:
        return False
    shift = inner[0] - outer[0]
    return (inner[1] >> shift, inner[2] >> shift) == (outer[1], outer[2])


def _touches(a: Key, b: Key) -> bool:
    return _contains(a, b) or _contains(b, a)


@dataclass
class _Area:
    key: Key
    fetched_at: float
    lines: list[_Line]  # whole lines, Web Mercator metres
    tree: object  # STRtree over the lines, or None
    lod: dict[int, list]  # level -> simplified geometry per line (None: not shown below z8)


def _make_lines(rows) -> list[_Line]:
    """(way id, tags, (lon, lat) array) -> lines ready for tiles: railway lines under
    construction without ``service`` (what QUERY asks for)."""
    lines = []
    for way_id, tags, coords in rows:
        cls = classify(tags)
        if cls is None or cls.state != "construction" or cls.kind != "rail" or cls.service or len(coords) < 2:
            continue
        geom = LineString(to_mercator(np.asarray(coords, dtype=np.float64)))
        if geom.length == 0:
            continue
        lines.append(_Line(way_id, geom, line_props(tags, cls), cls.minzoom, cls.labelzoom, speed_label(tags)))
    return lines


def _make_area(key: Key, fetched_at: float, lines: list[_Line]) -> _Area:
    tree = shapely.STRtree([line.geom for line in lines]) if lines else None
    lod = {
        level: [
            shapely.simplify(line.geom, lod_tolerance(level), preserve_topology=False) if line.minzoom == 0 else None
            for line in lines
        ]
        for level in LOD_LEVELS
    }
    return _Area(key, fetched_at, lines, tree, lod)


class ConstructionLines:
    """Fetches, keeps and draws lines under construction for uncached areas (thread-safe)."""

    def __init__(
        self,
        home: Home,
        client: OverpassClient,
        coverage: CellCoverage,
        enabled: Callable[[], bool],
        status: NetStatus | None = None,
        on_change: Callable[[int], None] = lambda version: None,
    ) -> None:
        self.home = home
        self.client = client
        self.coverage = coverage
        self.enabled = enabled
        self.status = status
        self.on_change = on_change
        self.version = 0
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._conn: sqlite3.Connection | None = None
        self._fetched: dict[Key, float] | None = None  # area -> when it was fetched
        self._areas: OrderedDict[Key, _Area] = OrderedDict()  # loaded areas, least recently used first
        self._tiles: OrderedDict[Key, bytes] = OrderedDict()
        self._queue: OrderedDict[Key, None] = OrderedDict()  # most recently wanted last
        self._too_big: set[Key] = set()  # areas the servers refused: asked for in quarters
        self._busy: Key | None = None
        # Each start of fetching has its own generation and cancel event, so a
        # query still running after stop() can never save anything afterwards.
        self._generation = 0
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._published = 0.0
        self._unpublished = False

    @property
    def path(self):
        return self.home.cache_dir / "construction.sqlite"

    # -- the page asks for a tile ---------------------------------------------------------

    def tile(self, z: int, x: int, y: int) -> bytes:
        """The vector tile (empty when the setting is off or inside a cached cell)."""
        if not self.enabled():
            return b""
        if z >= CELL_ZOOM and self.coverage.is_cached(x >> (z - CELL_ZOOM), y >> (z - CELL_ZOOM)):
            return b""  # the cell draws its own lines; nothing to fetch for it either
        self._want_for_tile(z, x, y)
        key = (z, x, y)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
            version = self.version
        areas = self._areas_touching(key)
        data = self._render(z, x, y, areas) if areas else b""
        with self._lock:
            if version == self.version:
                self._tiles[key] = data
                while len(self._tiles) > MAX_TILES:
                    self._tiles.popitem(last=False)
        return data

    def _want_for_tile(self, z: int, x: int, y: int) -> None:
        if z > LAST_FETCH_ZOOM:
            return  # zoomed in: the ORM tiles draw these lines; use what is already here
        level = data_zoom(z)
        if z >= level:
            shift = z - level
            needed = [(level, x >> shift, y >> shift)]
        else:
            n = 1 << (level - z)
            needed = [(level, x * n + i, y * n + j) for j in range(n) for i in range(n)]
        for key in needed:
            self._want(key)

    def _want(self, key: Key) -> None:
        with self._lock:
            if key in self._too_big:
                for child in _children(key):
                    self._want(child)
                return
            if self._state(key) == "fresh" or key == self._busy:
                return
            self._queue[key] = None
            self._queue.move_to_end(key)
            while len(self._queue) > QUEUE_LIMIT:
                self._queue.popitem(last=False)
            self._wake.notify_all()
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, args=(self._generation, self._cancel), name="construction", daemon=True
                )
                self._thread.start()
        self._show_progress()

    # -- what is known --------------------------------------------------------------------------

    def _known(self) -> dict[Key, float]:
        """Areas fetched so far (read from disk the first time)."""
        if self._fetched is None:
            fetched: dict[Key, float] = {}
            conn = self._db(create=False)
            if conn is not None:
                for z, x, y, at in conn.execute("SELECT z, x, y, fetched_at FROM areas"):
                    fetched[(z, x, y)] = at
            if self._conn is None and self.home.blocked:
                return fetched  # not remembered: read again once the folder is usable
            self._fetched = fetched
        return self._fetched

    def _state(self, key: Key) -> str:
        """'fresh', 'stale' (shown, but due to be fetched again) or 'missing'."""
        known = self._known()
        z, x, y = key
        for level in range(z, TOP_ZOOM - 1, -1):  # the area itself or a bigger one around it
            at = known.get((level, x >> (z - level), y >> (z - level)))
            if at is not None:
                return "fresh" if time.time() - at < FRESH_FOR else "stale"
        if z >= MAX_SPLIT_ZOOM:
            return "missing"
        states = []
        for child in _children(key):  # or all four of its parts
            state = self._state(child)
            if state == "missing":
                return "missing"
            states.append(state)
        return "fresh" if all(s == "fresh" for s in states) else "stale"

    def _areas_touching(self, key: Key) -> list[_Area]:
        with self._lock:
            keys = [k for k in self._known() if _touches(k, key)]
        return [area for area in (self._area(k) for k in keys) if area is not None]

    def _area(self, key: Key) -> _Area | None:
        with self._lock:
            area = self._areas.get(key)
            if area is not None:
                self._areas.move_to_end(key)
                return area
            at = self._known().get(key)
            conn = self._db(create=False)
            if at is None or conn is None:
                return None
            rows = [
                (way_id, json.loads(tags), np.frombuffer(blob, dtype=np.float64).reshape(-1, 2))
                for way_id, tags, blob in conn.execute(
                    "SELECT id, tags, coords FROM lines WHERE z=? AND x=? AND y=?", key
                )
            ]
            area = _make_area(key, at, _make_lines(rows))
            self._remember(area)
            return area

    def _remember(self, area: _Area) -> None:
        self._areas[area.key] = area
        self._areas.move_to_end(area.key)
        while len(self._areas) > MAX_LOADED_AREAS:
            self._areas.popitem(last=False)

    # -- drawing ----------------------------------------------------------------------------------

    @staticmethod
    def _render(z: int, x: int, y: int, areas: list[_Area]) -> bytes:
        minx, miny, maxx, maxy = tile_bounds_m(z, x, y)
        pad = (maxx - minx) * BUFFER
        query = box(minx - pad, miny - pad, maxx + pad, maxy + pad)
        found: dict[int, tuple[float, _Line, object]] = {}  # way id -> newest copy
        level = next((lv for lv in LOD_LEVELS if z <= lv), None) if z < CELL_ZOOM else None
        for area in areas:
            if area.tree is None:
                continue
            for index in area.tree.query(query):
                line = area.lines[int(index)]
                if line.minzoom > z:
                    continue
                geom = line.geom if level is None else area.lod[level][int(index)]
                if geom is None or geom.is_empty:
                    continue
                old = found.get(line.way_id)
                if old is None or area.fetched_at > old[0]:
                    found[line.way_id] = (area.fetched_at, line, geom)
        if not found:
            return b""
        if level is not None:
            return encode_lowzoom(z, x, y, [(line.way_id, line.props, geom) for _at, line, geom in found.values()])
        lines = [line for _at, line, _geom in found.values()]
        data = _encode_tile(z, x, y, lines, shapely.STRtree([line.geom for line in lines]), [], None)
        return data or b""

    # -- fetching -------------------------------------------------------------------------------

    def _run(self, generation: int, cancel: threading.Event) -> None:
        while not cancel.is_set() and generation == self._generation:
            key = self._next(generation)
            if key is None:
                continue
            try:
                self._fetch(key, generation, cancel)
            finally:
                with self._lock:
                    if self._busy == key:
                        self._busy = None
                self._show_progress()

    def _next(self, generation: int) -> Key | None:
        with self._wake:
            self._maybe_publish()
            if generation != self._generation:
                return None
            if not self._queue:
                self._wake.wait(timeout=1.0)
                return None
            if not self.enabled() or self.home.blocked:
                self._queue.clear()
                return None
            key, _ = self._queue.popitem(last=True)
            if self._state(key) == "fresh":
                return None
            self._busy = key
            return key

    def _fetch(self, key: Key, generation: int, cancel: threading.Event) -> None:
        self._show_progress()
        try:
            elements = self.client.query_area(
                build_query(key), cancel, can_split=key[0] < MAX_SPLIT_ZOOM, timeout=SERVER_TIMEOUT
            )
        except AreaTooBig as exc:
            log.info("construction lines: area %s too big (%s), asking for its quarters", key, exc)
            with self._lock:
                self._too_big.add(key)
                for child in reversed(_children(key)):
                    self._queue[child] = None
                    self._queue.move_to_end(child)
            return
        except overpass.Cancelled:
            return
        except (overpass.Offline, overpass.ServersBusy, overpass.BudgetExhausted) as exc:
            log.info("construction lines: %s; trying again in %.0f s", exc, RETRY_AFTER)
            with self._lock:
                self._queue[key] = None
                self._queue.move_to_end(key, last=False)  # newer wishes first
            cancel.wait(RETRY_AFTER)
            return
        except Exception:  # noqa: BLE001 - the overlay is optional; never stop the thread
            log.exception("construction lines: area %s failed", key)
            cancel.wait(RETRY_AFTER)
            return
        if cancel.is_set() or generation != self._generation or not self.enabled():
            return
        rows = []
        for element in elements:
            tags = element.get("tags") or {}
            geometry = element.get("geometry") or []
            if element.get("type") != "way" or tags.get("railway") != "construction":
                continue
            coords = [(p["lon"], p["lat"]) for p in geometry if p]
            if len(coords) >= 2:
                rows.append((element["id"], tags, np.array(coords, dtype=np.float64)))
        self._save(key, rows, generation)

    def _save(self, key: Key, rows: list, generation: int) -> None:
        now = time.time()
        lines = _make_lines(rows)
        with self._lock:
            if generation != self._generation or not self.enabled():
                return  # stopped meanwhile (closing, clearing, switched off)
            known = self._known()
            replaced = [k for k in known if _contains(key, k)]  # this area and the smaller ones inside it
            had_lines = any(self._areas.get(k) is None or self._areas[k].lines for k in replaced)
            try:
                conn = self._db(create=True)
            except (DataBlocked, OSError, sqlite3.Error) as exc:
                log.warning("construction lines not saved: %s", exc)
                conn = None
            if conn is not None:
                conn.execute("BEGIN")
                for k in replaced:
                    conn.execute("DELETE FROM areas WHERE z=? AND x=? AND y=?", k)
                    conn.execute("DELETE FROM lines WHERE z=? AND x=? AND y=?", k)
                conn.execute("INSERT INTO areas VALUES (?, ?, ?, ?)", (*key, now))
                conn.executemany(
                    "INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?)",
                    [(*key, way_id, json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                      coords.astype(np.float64).tobytes()) for way_id, tags, coords in rows],
                )
                conn.execute("COMMIT")
            for k in replaced:
                known.pop(k, None)
                self._areas.pop(k, None)
            known[key] = now
            self._remember(_make_area(key, now, lines))
            if lines or had_lines:
                self.version += 1
                self._tiles.clear()
                self._unpublished = True
            self._maybe_publish()
        log.info("construction lines: area %s, %d lines", key, len(lines))

    def _maybe_publish(self) -> None:
        if self._unpublished and time.monotonic() - self._published >= PUBLISH_EVERY:
            self._unpublished = False
            self._published = time.monotonic()
            version = self.version
            threading.Thread(target=self.on_change, args=(version,), daemon=True).start()

    def _show_progress(self) -> None:
        if self.status is None:
            return
        with self._lock:
            waiting = len(self._queue) + (1 if self._busy is not None else 0)
        on = waiting and self.enabled()
        self.status.note("construction", f"正在加载在建线路（还有 {waiting} 块）" if on else None)

    # -- storage ------------------------------------------------------------------------------------

    def _db(self, create: bool) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if self.home.blocked:  # 「清空缓存」 in progress (SPEC §11.3)
            if create:
                raise DataBlocked("the data folder is being cleared")
            return None
        if not create and not self.path.exists():
            return None
        self.home.ensure_dir(self.home.cache_dir)
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS areas (z INTEGER, x INTEGER, y INTEGER, fetched_at REAL NOT NULL, "
            "PRIMARY KEY (z, x, y));"
            "CREATE TABLE IF NOT EXISTS lines (z INTEGER NOT NULL, x INTEGER NOT NULL, y INTEGER NOT NULL, "
            "id INTEGER NOT NULL, tags TEXT NOT NULL, coords BLOB NOT NULL);"
            "CREATE INDEX IF NOT EXISTS lines_area ON lines(z, x, y);"
        )
        self._conn = conn
        return conn

    # -- lifecycle ----------------------------------------------------------------------------------

    def settings_changed(self) -> None:
        """The setting was switched off: stop fetching (it starts again when tiles are asked for)."""
        if not self.enabled():
            self.stop(timeout=0)

    def stop(self, timeout: float = 10.0) -> None:
        """Stop fetching (closing the program, clearing the cache, setting off).

        A query that is still running is abandoned: whatever it returns later
        is not saved.  Fetching starts again when tiles are asked for."""
        with self._wake:
            self._generation += 1
            cancel, self._cancel = self._cancel, threading.Event()
            cancel.set()
            self._queue.clear()
            self._busy = None
            thread, self._thread = self._thread, None
            self._wake.notify_all()
        if thread is not None and timeout:
            thread.join(timeout)
        self._show_progress()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def forget(self) -> None:
        """After 「清空缓存」: nothing is known any more."""
        with self._lock:
            self._fetched = None
            self._areas.clear()
            self._tiles.clear()
            self._queue.clear()
            self._too_big.clear()
            self.version += 1
