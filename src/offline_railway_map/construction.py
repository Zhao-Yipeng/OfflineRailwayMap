"""Lines under construction across China (setting 「永远显示在建线路」, SPEC §7.9).

The ORM speed tiles draw lines under construction only from z9, and no
public service has pictures of them below that.  With this setting on, the
program reads them from Geofabrik's extracts of China's provinces
(extracts.py) and draws them the way cached cells do: at every zoom,
coloured by speed, with the same widths (SPEC §7.3–7.5).  Only China:
elsewhere nothing is drawn or downloaded.

* Switching the setting on goes through the provinces, the one in view
  first.  A province already on disk (downloaded for cells) is used as it is;
  the others are downloaded and filtered through the same per-region jobs as
  cell downloads, so nothing is downloaded twice.  Its railway lines under
  construction (type rail, without service: the ones shown below z9; the
  ORM tiles draw the others from z10) go to data/cache/construction.sqlite.
* Afterwards the page asks whether the extracts downloaded for this, and for
  nothing else, are deleted now or kept for 7 days like other extracts.
  Extracts that cells used, or that waiting cells may read, are not among
  them.  Unanswered, the question comes back at the next start.
* A province's lines count as current for ``FRESH_FOR`` (7 days); switching
  the setting on again refreshes older ones, and whenever a province is
  filtered for cells its lines are updated as well.
* Vector tiles are made on request; inside cached cells (z >= 8) they are
  empty: a cached cell draws its own lines.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import shapely
from shapely.geometry import LineString, box

from . import extracts as extracts_module
from .classify import classify
from .coverage import CellCoverage
from .extracts import ExtractStore, ExtractUnavailable
from .grid import CELL_ZOOM
from .net import NetStatus
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

CHINA = "china"  # Geofabrik's region; its parts are the province-level extracts
FRESH_FOR = 7 * 24 * 3600.0
PUBLISH_EVERY = 2.0  # seconds between "new lines" events to the page
MAX_TILES = 512
# Version 2: lines cut to China (geometry as WKB).  Older files - the Overpass
# version of this overlay, or lines not yet cut - are emptied and filled again.
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS provinces (region TEXT PRIMARY KEY, built_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lines (region TEXT NOT NULL, id INTEGER NOT NULL, tags TEXT NOT NULL, geom BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS lines_region ON lines(region);
CREATE TABLE IF NOT EXISTS owned (region TEXT PRIMARY KEY);
"""


def construction_rows(db: Path, china) -> list[tuple[int, dict, object]]:
    """Railway lines under construction in a filtered extract (extracts.filter_railways),
    cut to ``china`` (lon/lat): a province's extract also holds whole ways that cross
    the border, and a margin around the province."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, tags, coords FROM ways WHERE tags LIKE ?", ('%"railway":"construction"%',)
        ).fetchall()
    finally:
        conn.close()
    shapely.prepare(china)
    out = []
    for way_id, tags, blob in rows:
        coords = np.frombuffer(blob, dtype=np.float64).reshape(-1, 2)
        if len(coords) < 2:
            continue
        line = LineString(coords)
        if not china.contains(line):
            line = line.intersection(china)
            parts = [g for g in getattr(line, "geoms", [line]) if g.geom_type == "LineString" and g.length > 0]
            if not parts:
                continue
            line = parts[0] if len(parts) == 1 else shapely.MultiLineString(parts)
        out.append((way_id, json.loads(tags), line))
    return out


def _make_lines(rows) -> list[_Line]:
    """(way id, tags, geometry in lon/lat) -> lines ready for tiles: railway lines under
    construction without ``service`` (the ones drawn below z9)."""
    lines = []
    for way_id, tags, geom in rows:
        cls = classify(tags)
        if cls is None or cls.state != "construction" or cls.kind != "rail" or cls.service:
            continue
        geom = shapely.transform(geom, to_mercator)
        if geom.is_empty or geom.length == 0:
            continue
        lines.append(_Line(way_id, geom, line_props(tags, cls), cls.minzoom, cls.labelzoom, speed_label(tags)))
    return lines


@dataclass
class _Area:
    """One province's lines, ready for drawing."""

    region: str
    built_at: float
    lines: list[_Line]  # whole lines, Web Mercator metres
    tree: object  # STRtree over the lines, or None
    lod: dict[int, list]  # level -> simplified geometry per line (None: not shown below z8)
    bounds: tuple[float, float, float, float] | None


def _make_area(region: str, built_at: float, lines: list[_Line]) -> _Area:
    tree = shapely.STRtree([line.geom for line in lines]) if lines else None
    lod = {
        level: [
            shapely.simplify(line.geom, lod_tolerance(level), preserve_topology=False) if line.minzoom == 0 else None
            for line in lines
        ]
        for level in LOD_LEVELS
    }
    bounds = tuple(shapely.total_bounds([line.geom for line in lines])) if lines else None
    return _Area(region, built_at, lines, tree, lod, bounds)


@dataclass
class _Run:
    """One pass over China's provinces."""

    generation: int
    cancel: threading.Event = field(default_factory=threading.Event)
    quiet: bool = False  # closing or clearing: no question at the end


class ConstructionLines:
    """Gets, keeps and draws China's lines under construction (thread-safe)."""

    def __init__(
        self,
        home: Home,
        extracts: ExtractStore,
        coverage: CellCoverage,
        enabled: Callable[[], bool],
        status: NetStatus | None = None,
        on_change: Callable[[int], None] = lambda version: None,
        on_question: Callable[[dict], None] = lambda question: None,
        planned_regions: Callable[[], set[str]] = set,
        view_center: Callable[[], tuple[float, float]] = lambda: (105.0, 35.0),
        cells_busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.home = home
        self.extracts = extracts
        self.coverage = coverage
        self.enabled = enabled
        self.status = status
        self.on_change = on_change
        self.on_question = on_question
        self.planned_regions = planned_regions
        self.view_center = view_center
        self.cells_busy = cells_busy
        self.version = 0
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._areas: dict[str, _Area] | None = None  # loaded on first use
        self._tiles: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self._generation = 0
        self._run: _Run | None = None
        self._thread: threading.Thread | None = None
        self._published = 0.0
        self._unpublished = False
        extracts.on_filtered = self._filtered
        extracts.on_cell_use = self._cell_use

    @property
    def path(self) -> Path:
        return self.home.cache_dir / "construction.sqlite"

    # -- switching on and off -------------------------------------------------------------

    def start(self) -> None:
        """At program start: provinces still missing are fetched (switching on again refreshes old ones)."""
        if self.enabled():
            self._begin(refresh=False)

    def settings_changed(self) -> None:
        if self.enabled():
            self._begin(refresh=True)
        else:
            self._halt(quiet=False, timeout=0)  # the question comes when the pass has stopped

    def stop(self, timeout: float = 10.0) -> None:
        """Closing the program or clearing the cache: stop without asking."""
        self._halt(quiet=True, timeout=timeout)

    def _begin(self, refresh: bool) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and self._run and not self._run.cancel.is_set():
                return
            self._generation += 1
            run = self._run = _Run(self._generation)
            self._thread = threading.Thread(target=self._pass, args=(run, refresh), name="construction", daemon=True)
            self._thread.start()

    def _halt(self, quiet: bool, timeout: float) -> None:
        with self._lock:
            self._generation += 1
            run, thread = self._run, self._thread
            if run is not None:
                run.quiet = run.quiet or quiet
                run.cancel.set()
        if thread is not None and timeout:
            thread.join(timeout)

    # -- one pass over the provinces ----------------------------------------------------------

    def _pass(self, run: _Run, refresh: bool) -> None:
        started = time.time()
        failed = 0
        try:
            try:
                index = self.extracts.index()
            except ExtractUnavailable as exc:
                log.warning("construction lines: %s", exc)
                self._problem("在建线路暂时无法下载：读取不到地区数据包目录，下次打开程序时重试")
                return
            china = index.regions.get(CHINA)
            if china is None:
                self._problem("在建线路：地区数据包目录里没有中国")
                return
            lon, lat = self.view_center()
            provinces = sorted(china.children, key=lambda r: r.geometry.centroid.distance(shapely.Point(lon, lat)))
            todo = [p for p in provinces if self._needs(p.id, refresh)]
            for n, region in enumerate(todo, 1):
                if self._stopped(run):
                    break
                label = f"在建线路（{n}/{len(todo)}）"
                self._note(f"{label}：{region.name}")
                if not self.extracts.usable(region.id):
                    failed += 1
                    continue
                if not self.extracts.fresh(region.id) and not self.extracts.busy(region.id):
                    # Cell downloads come first: the next province is downloaded once they are done.
                    while self.cells_busy() and not self._stopped(run):
                        self._note(f"{label}：等方格下载完成后继续")
                        run.cancel.wait(1.0)
                    if self._stopped(run):
                        break
                    self._note(f"{label}：{region.name}")
                try:
                    downloaded = self.extracts.ensure(
                        region.id, run.cancel, lambda text, *a, **k: self._note(f"{label}：{text}")
                    )
                except extracts_module.Cancelled:
                    break
                except ExtractUnavailable as exc:
                    log.warning("construction lines: %s", exc)
                    failed += 1
                    continue
                if downloaded:
                    self._own(region.id)  # even if stopped now: it was downloaded for this
                if self._stopped(run):
                    break
                if self._built_at(region.id) < started:  # not just updated by _filtered
                    self._load_region(region.id)
            if not self._stopped(run):
                if failed:
                    self._problem(f"在建线路：{failed} 个地区的数据包暂时下载失败，下次打开程序时重试")
                else:
                    self._problem(None)
        except Exception:  # noqa: BLE001 - the overlay is optional; never take the program down
            log.exception("construction lines")
        finally:
            self._note(None)
            self._publish_now()
            if not run.quiet:
                question = self._question_now()
                if question:
                    self.on_question(question)

    def _stopped(self, run: _Run) -> bool:
        return run.cancel.is_set() or run.generation != self._generation or self.home.blocked

    def _needs(self, region: str, refresh: bool) -> bool:
        built_at = self._built_at(region)
        return built_at == 0.0 or (refresh and time.time() - built_at > FRESH_FOR)

    def _built_at(self, region: str) -> float:
        with self._lock:
            conn = self._db(create=False)
            if conn is None:
                return 0.0
            row = conn.execute("SELECT built_at FROM provinces WHERE region=?", (region,)).fetchone()
            return row[0] if row else 0.0

    def _load_region(self, region: str) -> None:
        """Take the province's lines under construction from its filtered extract."""
        db = self.extracts.db_path(region)
        index = self.extracts._index
        if self.home.blocked or not db.exists() or index is None or CHINA not in index.regions:
            return
        rows = construction_rows(db, index.regions[CHINA].geometry)
        lines = _make_lines(rows)
        kept = {line.way_id for line in lines}
        now = time.time()
        with self._lock:
            try:
                conn = self._db(create=True)
            except (DataBlocked, OSError, sqlite3.Error) as exc:
                log.warning("construction lines not saved: %s", exc)
                return
            conn.execute("BEGIN")
            conn.execute("DELETE FROM lines WHERE region=?", (region,))
            conn.executemany(
                "INSERT INTO lines VALUES (?, ?, ?, ?)",
                [(region, way_id, json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                  shapely.to_wkb(geom)) for way_id, tags, geom in rows if way_id in kept],
            )
            conn.execute("INSERT OR REPLACE INTO provinces VALUES (?, ?)", (region, now))
            conn.execute("COMMIT")
            self._loaded()[region] = _make_area(region, now, lines)
            self.version += 1
            self._tiles.clear()
            self._unpublished = True
        log.info("construction lines: %s, %d lines", region, len(lines))
        self._maybe_publish()

    # -- what was downloaded for this, and the question --------------------------------------

    def _own(self, region: str) -> None:
        with self._lock:
            if self.extracts.used_by_cells(region):
                return  # cells use it too: a cell download like any other
            try:
                self._db(create=True).execute("INSERT OR IGNORE INTO owned VALUES (?)", (region,))
            except (DataBlocked, OSError, sqlite3.Error) as exc:
                log.warning("construction lines: %s", exc)

    def _cell_use(self, region: str) -> None:
        """Cells read this region: it no longer belongs to the question."""
        with self._lock:
            conn = self._db(create=False)
            if conn is not None:
                conn.execute("DELETE FROM owned WHERE region=?", (region,))

    def _owned(self) -> list[str]:
        conn = self._db(create=False)
        return [row[0] for row in conn.execute("SELECT region FROM owned ORDER BY region")] if conn else []

    def question(self) -> dict | None:
        """The extracts downloaded only for this that are still on disk (None: nothing to
        ask now; while a pass runs, it asks when it is done)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._run.cancel.is_set():
                return None
            return self._question_now()

    def _question_now(self) -> dict | None:
        with self._lock:
            regions, size = [], 0
            for region in self._owned():
                path = self.extracts.db_path(region)
                if path.exists():
                    regions.append(region)
                    size += path.stat().st_size
                else:  # deleted meanwhile (expired, cleared)
                    self._db(create=False).execute("DELETE FROM owned WHERE region=?", (region,))
            if not regions:
                return None
            index = self.extracts._index
            names = [index.regions[r].name if index and r in index.regions else r for r in regions]
            return {"regions": regions, "names": names, "count": len(regions), "bytes": size}

    def decide(self, delete: bool) -> dict:
        """The answer: delete those extracts now, or keep them 7 days like other extracts."""
        with self._lock:
            owned = set(self._owned())
            doomed: set[str] = set()
            freed = 0
            if delete and owned:
                keep = set(self.planned_regions())
                doomed = {r for r in owned - keep if not self.extracts.used_by_cells(r) and not self.extracts.busy(r)}
                freed = self.extracts.delete_regions(doomed)
            conn = self._db(create=False)
            if conn is not None:
                conn.execute("DELETE FROM owned")
        log.info("construction lines: %s %d extracts (%d bytes)", "deleted" if delete else "kept", len(doomed), freed)
        return {"deleted": len(doomed), "kept": len(owned) - len(doomed), "bytes": freed}

    # -- cell downloads filtered a province ------------------------------------------------------

    def _filtered(self, region: str) -> None:
        """A province was filtered (also for cells): update its lines if this is in use."""
        index = self.extracts._index
        if index is None or region not in index.regions or index.regions[region].parent != CHINA:
            return
        if self.enabled() or self._built_at(region) > 0:
            self._load_region(region)

    # -- drawing -------------------------------------------------------------------------------

    def _loaded(self) -> dict[str, _Area]:
        if self._areas is None:
            areas: dict[str, _Area] = {}
            conn = self._db(create=False)
            if conn is not None:
                for region, built_at in conn.execute("SELECT region, built_at FROM provinces").fetchall():
                    rows = [
                        (way_id, json.loads(tags), shapely.from_wkb(blob))
                        for way_id, tags, blob in conn.execute(
                            "SELECT id, tags, geom FROM lines WHERE region=?", (region,)
                        )
                    ]
                    areas[region] = _make_area(region, built_at, _make_lines(rows))
            if self._conn is None and self.home.blocked:
                return areas  # not remembered: read again once the folder is usable
            self._areas = areas
        return self._areas

    def tile(self, z: int, x: int, y: int) -> bytes:
        """The vector tile (empty when the setting is off, outside China or inside a cached cell)."""
        if not self.enabled():
            return b""
        if z >= CELL_ZOOM and self.coverage.is_cached(x >> (z - CELL_ZOOM), y >> (z - CELL_ZOOM)):
            return b""  # the cell draws its own lines
        key = (z, x, y)
        with self._lock:
            if key in self._tiles:
                self._tiles.move_to_end(key)
                return self._tiles[key]
            version = self.version
            areas = list(self._loaded().values())
        data = self._render(z, x, y, areas)
        with self._lock:
            if version == self.version:
                self._tiles[key] = data
                while len(self._tiles) > MAX_TILES:
                    self._tiles.popitem(last=False)
        return data

    @staticmethod
    def _render(z: int, x: int, y: int, areas: list[_Area]) -> bytes:
        minx, miny, maxx, maxy = tile_bounds_m(z, x, y)
        pad = (maxx - minx) * BUFFER
        west, south, east, north = minx - pad, miny - pad, maxx + pad, maxy + pad
        query = box(west, south, east, north)
        found: dict[int, tuple[float, _Line, object]] = {}  # way id -> newest copy (border ways are in both provinces)
        level = next((lv for lv in LOD_LEVELS if z <= lv), None) if z < CELL_ZOOM else None
        for area in areas:
            if area.tree is None or area.bounds is None:
                continue
            a_west, a_south, a_east, a_north = area.bounds
            if a_west > east or a_east < west or a_south > north or a_north < south:
                continue
            for index in area.tree.query(query):
                line = area.lines[int(index)]
                if line.minzoom > z:
                    continue
                geom = line.geom if level is None else area.lod[level][int(index)]
                if geom is None or geom.is_empty:
                    continue
                old = found.get(line.way_id)
                if old is None or area.built_at > old[0]:
                    found[line.way_id] = (area.built_at, line, geom)
        if not found:
            return b""
        if level is not None:
            return encode_lowzoom(z, x, y, [(line.way_id, line.props, geom) for _at, line, geom in found.values()])
        lines = [line for _at, line, _geom in found.values()]
        data = _encode_tile(z, x, y, lines, shapely.STRtree([line.geom for line in lines]), [], None)
        return data or b""

    # -- page and status bar ---------------------------------------------------------------------

    def _maybe_publish(self) -> None:
        with self._lock:
            if not self._unpublished or time.monotonic() - self._published < PUBLISH_EVERY:
                return
        self._publish_now()

    def _publish_now(self) -> None:
        with self._lock:
            if not self._unpublished:
                return
            self._unpublished = False
            self._published = time.monotonic()
            version = self.version
        self.on_change(version)

    def _note(self, text: str | None) -> None:
        if self.status is not None:
            self.status.note("construction", text)

    def _problem(self, text: str | None) -> None:
        if self.status is not None:
            self.status.service("construction", "paused" if text else "ok", text or "")

    # -- storage and clearing ----------------------------------------------------------------------

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
        if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            # Lines of an older version: fetched again.  Extracts still to be asked about stay listed.
            conn.executescript("DROP TABLE IF EXISTS areas; DROP TABLE IF EXISTS lines; DROP TABLE IF EXISTS provinces;")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.executescript(SCHEMA)
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def forget(self) -> None:
        """After 「清空缓存」: nothing is known any more."""
        with self._lock:
            self._areas = None
            self._tiles.clear()
            self.version += 1
