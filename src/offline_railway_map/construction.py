"""Lines under construction across China (setting 「永远显示在建线路」, SPEC §7.9).

The ORM speed tiles draw lines under construction only from z9, and no
public service has pictures of them below that.  With this setting on, the
program reads them from Geofabrik's extracts of China (extracts.py) and
draws them the way cached cells do: at every zoom, coloured by speed, with
the same widths (SPEC §7.3–7.5).  Only China: elsewhere nothing is drawn or
downloaded.

* Switching the setting on goes through the provinces, the one in view
  first.  Extracts on disk or being downloaded for cells are shared, never
  downloaded twice: all of China's (cell downloads of many cells may choose
  it) serves every province at once, a province's own serves that province.
  The other provinces are downloaded and filtered through the same
  per-region jobs as cell downloads, once cell downloads are done.  The
  railway lines under construction (type rail, without service: the ones
  shown below z9; the ORM tiles draw the others from z10) go to
  data/cache/construction.sqlite.
* The extract read last decides about the ways it has, whatever they are
  now, and about everything inside its borders: older copies there (all of
  China's, a neighbouring province's margin) are dropped, so lines opened or
  deleted since disappear.
* Afterwards the page asks whether the extracts downloaded for this, and for
  nothing else, are deleted now or kept for 7 days like other extracts.
  Extracts that cells used, or that waiting cells may read, are not among
  them.  Unanswered, the question comes back at the next start.
* A province's lines count as current for ``FRESH_FOR`` (7 days); switching
  the setting on again refreshes older ones, and whenever China or a
  province is filtered for cells, its lines are updated as well.
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
INSIDE_MARGIN = 0.01  # degrees (about 1 km): "inside an extract's borders" keeps this far from them
# Version 2: lines cut to China (geometry as WKB).  Older files - the Overpass
# version of this overlay, or lines not yet cut - are emptied and filled again.
# "provinces" holds the extracts read: provinces, or all of China ("china").
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE IF NOT EXISTS provinces (region TEXT PRIMARY KEY, built_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lines (region TEXT NOT NULL, id INTEGER NOT NULL, tags TEXT NOT NULL, geom BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS lines_region ON lines(region);
CREATE INDEX IF NOT EXISTS lines_way ON lines(region, id);
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


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def known_ways(db: Path) -> np.ndarray:
    """Every railway way in a filtered extract, whatever it is now (built, under construction ...)."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        return np.fromiter((row[0] for row in conn.execute("SELECT id FROM ways")), dtype=np.int64)
    finally:
        conn.close()


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


def _prune(area: _Area, known: np.ndarray, inside) -> tuple[_Area, list[int]] | None:
    """``area`` without the lines a newer extract decides about: the ways it has (``known``)
    and the lines inside its borders (``inside``, Web Mercator).  Also the way ids taken
    out; None if there are none."""
    if not area.lines:
        return None
    ids = np.fromiter((line.way_id for line in area.lines), dtype=np.int64, count=len(area.lines))
    gone = np.isin(ids, known) | shapely.contains(inside, [line.geom for line in area.lines])
    if not gone.any():
        return None
    keep = ~gone
    lines = [line for line, k in zip(area.lines, keep) if k]
    lod = {level: [geom for geom, k in zip(geoms, keep) if k] for level, geoms in area.lod.items()}
    geoms = [line.geom for line in lines]
    tree = shapely.STRtree(geoms) if geoms else None
    bounds = tuple(shapely.total_bounds(geoms)) if geoms else None
    return _Area(area.region, area.built_at, lines, tree, lod, bounds), [int(w) for w in ids[gone]]


@dataclass
class _Run:
    """One pass over China's provinces."""

    generation: int
    cancel: threading.Event = field(default_factory=threading.Event)
    quiet: bool = False  # closing or clearing: no question at the end
    no_china: bool = False  # all of China's extract could not be read: the provinces' own then


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
        self._load_lock = threading.Lock()  # one extract read at a time (the pass, after filtering)
        self._epoch = 0  # counts 「清空缓存」: a read begun before must not save anything
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
            point = shapely.Point(*self.view_center())
            provinces = sorted(
                china.children, key=lambda r: (r.geometry.distance(point), r.geometry.centroid.distance(point))
            )
            todo = [p for p in provinces if self._needs(p.id, refresh)]
            for n, region in enumerate(todo, 1):
                if self._stopped(run):
                    break
                if self._covered_at(region.id) >= started:
                    continue  # read meanwhile: from all of China's extract, or after cells filtered it
                done = self._province(run, region, f"在建线路（{n}/{len(todo)}）", refresh, started)
                if done is None:
                    break
                if not done:
                    failed += 1
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

    def _province(self, run: _Run, region, label: str, refresh: bool, started: float) -> bool | None:
        """Get one province's lines: True when done, False if its extract failed for now,
        None when stopped.

        All of China's extract, if cells downloaded it or are downloading it, serves every
        province at once.  Otherwise the province's own: used if it is on disk, shared if
        cells are downloading it, else downloaded once cell downloads are done.
        """
        try:
            self._note(f"{label}：{region.name}")
            while True:
                if self._from_china(run, refresh, started):
                    return True
                if self._stopped(run):
                    return None
                if not self.extracts.usable(region.id):
                    return False
                if self.extracts.on_hand(region.id) or not self.cells_busy():
                    break
                # Cell downloads come first; they may also bring all of China (checked again above).
                self._note(f"{label}：等方格下载完成后继续")
                run.cancel.wait(1.0)
            self._note(f"{label}：{region.name}")
            downloaded = self.extracts.ensure(
                region.id, run.cancel, lambda text, *a, **k: self._note(f"{label}：{text}")
            )
        except extracts_module.Cancelled:
            return None
        except ExtractUnavailable as exc:
            log.warning("construction lines: %s", exc)
            return False
        if downloaded:
            self._own(region.id)  # even if stopped now: it was downloaded for this
        if self._stopped(run):
            return None
        self._load_region(region.id, since=started if refresh else 0.0)
        return True

    def _from_china(self, run: _Run, refresh: bool, started: float) -> bool:
        """Read every province from all of China's extract if it is on disk or being
        downloaded for cells (waiting for that download; this never downloads it itself).
        True when its lines are in place."""
        if run.no_china or not self.extracts.on_hand(CHINA) or not self.extracts.usable(CHINA):
            return False
        label = "在建线路（全国）"
        self._note(f"{label}：使用方格下载的全国数据包")
        if not self.extracts.share(CHINA, run.cancel, lambda text, *a, **k: self._note(f"{label}：{text}")):
            run.no_china = True
            return False
        if self._stopped(run):
            return False
        self._note(f"{label}：提取在建线路")
        if self._load_region(CHINA, since=started if refresh else 0.0):
            return True
        run.no_china = True
        return False

    def _stopped(self, run: _Run) -> bool:
        return run.cancel.is_set() or run.generation != self._generation or self.home.blocked

    def _needs(self, region: str, refresh: bool) -> bool:
        covered = self._covered_at(region)
        return covered == 0.0 or (refresh and time.time() - covered > FRESH_FOR)

    def _covered_at(self, region: str) -> float:
        """When the province's lines were read last: from its own extract or all of China's."""
        return max(self._built_at(region), self._built_at(CHINA))

    def _built_at(self, region: str) -> float:
        with self._lock:
            conn = self._db(create=False)
            if conn is None:
                return 0.0
            row = conn.execute("SELECT built_at FROM provinces WHERE region=?", (region,)).fetchone()
            return row[0] if row else 0.0

    def _has_lines(self) -> bool:
        """True once some extract was read (the setting was on)."""
        with self._lock:
            conn = self._db(create=False)
            return conn is not None and conn.execute("SELECT 1 FROM provinces LIMIT 1").fetchone() is not None

    def _load_region(self, region: str, since: float = 0.0) -> bool:
        """Read the lines under construction from a filtered extract: all of China's or a
        province's.  True if that file's lines are in place.

        The extract read last decides about the ways it has, whatever they are now, and
        about everything inside its borders, where a way it does not have no longer
        exists: older copies there (all of China's, a neighbouring province's margin)
        are dropped.  Lines read from this file already are kept, unless read before
        ``since`` (switching on again refreshes them).
        """
        db = self.extracts.db_path(region)
        index = self.extracts._index
        if self.home.blocked or index is None or CHINA not in index.regions or region not in index.regions:
            return False
        with self._load_lock:
            try:
                written = db.stat().st_mtime
            except OSError:
                return False
            if self._built_at(region) >= max(written, since):
                return True  # read already (by the pass, or right after cells filtered it)
            epoch = self._epoch
            try:
                rows = construction_rows(db, index.regions[CHINA].geometry)
                known = known_ways(db)
            except (OSError, sqlite3.Error) as exc:
                log.warning("construction lines from %s: %s", region, exc)
                return False
            lines = _make_lines(rows)
            kept = {line.way_id for line in lines}
            now = time.time()
            area = _make_area(region, now, lines)
            # Away from the borders: what lies right at them may be cut off by the extract.
            inside = shapely.transform(index.regions[region].geometry.buffer(-INSIDE_MARGIN), to_mercator)
            shapely.prepare(inside)
            with self._lock:
                older = [(other, old) for other, old in self._loaded().items() if other != region]
            pruned = [(other, result) for other, old in older if (result := _prune(old, known, inside)) is not None]
            with self._lock:
                if self._epoch != epoch:
                    return False  # the cache was cleared meanwhile
                try:
                    conn = self._db(create=True)
                    conn.execute("BEGIN")
                    conn.execute("DELETE FROM lines WHERE region=?", (region,))
                    conn.executemany(
                        "INSERT INTO lines VALUES (?, ?, ?, ?)",
                        [(region, way_id, json.dumps(tags, ensure_ascii=False, separators=(",", ":")),
                          shapely.to_wkb(geom)) for way_id, tags, geom in rows if way_id in kept],
                    )
                    for other, (_smaller, gone) in pruned:
                        conn.executemany("DELETE FROM lines WHERE region=? AND id=?", [(other, w) for w in gone])
                    conn.execute("INSERT OR REPLACE INTO provinces VALUES (?, ?)", (region, now))
                    conn.execute("COMMIT")
                except (DataBlocked, OSError, sqlite3.Error) as exc:
                    log.warning("construction lines not saved: %s", exc)
                    try:
                        if self._conn is not None and self._conn.in_transaction:
                            self._conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    return False
                areas = self._loaded()
                areas[region] = area
                for other, (smaller, _gone) in pruned:
                    areas[other] = smaller
                self.version += 1
                self._tiles.clear()
                self._unpublished = True
        log.info("construction lines: %s, %d lines (%d older copies dropped)", region, len(lines),
                 sum(len(gone) for _other, (_smaller, gone) in pruned))
        self._maybe_publish()
        return True

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

    def _sorted_out(self, planned: set[str]) -> tuple[list[str], list[str]]:
        """What this downloaded for itself and is still on disk: (only for this - the
        question; shared after all - waiting cells will read it, or cells are reading
        it).  Shared ones stay listed: if no cell reads them in the end, they are asked
        about later."""
        alone, shared = [], []
        for region in self._owned():
            if not self.extracts.db_path(region).exists():  # deleted meanwhile (expired, cleared)
                self._db(create=False).execute("DELETE FROM owned WHERE region=?", (region,))
            elif region in planned or self.extracts.used_by_cells(region) or self.extracts.busy(region):
                shared.append(region)
            else:
                alone.append(region)
        return alone, shared

    def _question_now(self) -> dict | None:
        planned = self.planned_regions()  # outside the lock: may take a moment
        with self._lock:
            regions, _shared = self._sorted_out(planned)
            if not regions:
                return None
            size = sum(_size(self.extracts.db_path(r)) for r in regions)
            index = self.extracts._index
            names = [index.regions[r].name if index and r in index.regions else r for r in regions]
            return {"regions": regions, "names": names, "count": len(regions), "bytes": size}

    def decide(self, delete: bool, regions: list[str] | None = None) -> dict:
        """The answer about ``regions`` (those the question listed; None: all it would list
        now): delete them now, or keep them 7 days like other extracts.  Checked again
        first: extracts cells have come to need meanwhile are kept."""
        planned = self.planned_regions()
        with self._lock:
            alone, shared = self._sorted_out(planned)
            listed = set(alone if regions is None else regions)
            answered = [r for r in alone if r in listed]
            doomed = set(answered) if delete else set()
            freed = self.extracts.delete_regions(doomed) if doomed else 0
            conn = self._db(create=False)
            if conn is not None:
                conn.executemany("DELETE FROM owned WHERE region=?", [(r,) for r in answered])
        log.info("construction lines: %s %d extracts (%d bytes)", "deleted" if delete else "kept", len(doomed), freed)
        return {"deleted": len(doomed), "kept": len(listed) - len(doomed), "bytes": freed}

    # -- an extract was filtered (also for cells) -------------------------------------------------

    def _filtered(self, region: str) -> None:
        """All of China or a province was filtered (also for cells): read its lines if the
        setting is on, or was (its lines are kept)."""
        index = self.extracts._index
        if index is None or region not in index.regions:
            return
        if region != CHINA and index.regions[region].parent != CHINA:
            return
        if self.enabled() or self._has_lines():
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
        found: dict[int, tuple[float, _Line, object]] = {}  # way id -> newest copy (normally the only one)
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
            self._epoch += 1
            self._areas = None
            self._tiles.clear()
            self.version += 1
