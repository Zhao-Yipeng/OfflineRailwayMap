"""Offline basemap from Protomaps daily builds (SPEC §7.7, §9.3, §10.5).

* the newest v4 build is chosen from builds.json (cached 24 h); a custom
  PMTiles URL can be configured instead;
* per cell, tiles z8..max zoom are copied into ``cells/<id>/basemap.sqlite``;
* the cell's z0–z7 ancestor tiles go to the shared ``basemap_lowzoom.sqlite``
  with a reference per cell, so they are stored once and removed with the
  last cell that needs them.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable

import requests

from .coverage import CELL_ZOOM, CellCoverage
from .downloader import BasemapStage
from .grid import cell_id
from .paths import Home
from .pmtiles import COMPRESSION_GZIP, ArchiveGone, RemoteArchive, descendant_range, id_to_zxy, zxy_to_id

log = logging.getLogger(__name__)

BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BUILD_BASE = "https://build.protomaps.com/"
STYLE_MAJOR = "4"  # the bundled style works with v4 builds
BUILDS_TTL = 24 * 3600

CELL_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (z INTEGER, x INTEGER, y INTEGER, data BLOB NOT NULL, PRIMARY KEY (z, x, y));
CREATE TABLE IF NOT EXISTS ancestors (z INTEGER, x INTEGER, y INTEGER, data BLOB NOT NULL, PRIMARY KEY (z, x, y));
"""


class BuildSelector:
    def __init__(self, session: requests.Session, source: Callable[[], str]) -> None:
        self.session = session
        self.source = source
        self._builds: list[dict] | None = None
        self._fetched = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        with self._lock:
            self._builds = None

    def current(self) -> tuple[str, str]:
        """(build key, archive URL)."""
        source = self.source()
        if source != "auto":
            return source, source
        with self._lock:
            if self._builds is None or time.time() - self._fetched > BUILDS_TTL:
                response = self.session.get(BUILDS_URL, timeout=(20, 60))
                response.raise_for_status()
                self._builds = response.json()
                self._fetched = time.time()
            builds = self._builds
        candidates = [b for b in builds if str(b.get("version", "")).split(".")[0] == STYLE_MAJOR and b.get("key")]
        if not candidates:
            raise RuntimeError("no compatible Protomaps build is listed")
        latest = max(candidates, key=lambda b: b["key"])
        return latest["key"], BUILD_BASE + latest["key"]


def plan_cell(archive: RemoteArchive, cx: int, cy: int, max_zoom: int, cancel=None):
    """[(z, x, y, offset, length)] for the cell's tiles and for its ancestors."""
    tiles, ancestors = [], []
    top = min(max_zoom, archive.header.max_zoom)
    for z in range(CELL_ZOOM, top + 1):
        lo, hi = descendant_range(CELL_ZOOM, cx, cy, z)
        for entry in archive.entries(lo, hi, cancel):
            for tile_id in range(max(entry.tile_id, lo), min(entry.tile_id + entry.run_length, hi)):
                tz, tx, ty = id_to_zxy(tile_id)
                tiles.append((tz, tx, ty, entry.offset, entry.length))
    for z in range(0, CELL_ZOOM):
        shift = CELL_ZOOM - z
        tile_id = zxy_to_id(z, cx >> shift, cy >> shift)
        for entry in archive.entries(tile_id, tile_id + 1, cancel):
            ancestors.append((z, cx >> shift, cy >> shift, entry.offset, entry.length))
    return tiles, ancestors


class BasemapFetcher(BasemapStage):
    def __init__(self, session: requests.Session, selector: BuildSelector, max_zoom: Callable[[], int]) -> None:
        self.session = session
        self.selector = selector
        self.max_zoom = max_zoom
        self._archives: dict[str, RemoteArchive] = {}
        self._lock = threading.Lock()

    def _archive(self, url: str) -> RemoteArchive:
        with self._lock:
            archive = self._archives.get(url)
        if archive is None:
            archive = RemoteArchive(self.session, url)
            with self._lock:
                self._archives = {url: archive}  # keep only the current build
        return archive

    def estimate(self, cx: int, cy: int) -> int:
        """Bytes the cell's basemap would take (directory reads only)."""
        _key, url = self.selector.current()
        tiles, ancestors = plan_cell(self._archive(url), cx, cy, self.max_zoom())
        return sum(length for _o, length in {(t[3], t[4]) for t in tiles + ancestors})

    def fetch(
        self,
        x: int,
        y: int,
        work: Path,
        cancel: threading.Event,
        progress: Callable[[float], None],
        received: Callable[[int], None] | None = None,
    ) -> dict:
        for attempt in (1, 2):
            key, url = self.selector.current()
            try:
                archive = self._archive(url)
                return self._extract(archive, key, x, y, work, cancel, progress, received or (lambda n: None))
            except ArchiveGone:
                if attempt == 2 or self.selector.source() != "auto":
                    raise
                log.info("basemap build %s is gone, refreshing the build list", key)
                self.selector.invalidate()
                with self._lock:
                    self._archives = {}
        raise RuntimeError("unreachable")

    def _extract(self, archive, key, cx, cy, work, cancel, progress, received) -> dict:
        max_zoom = min(self.max_zoom(), archive.header.max_zoom)
        tiles, ancestors = plan_cell(archive, cx, cy, max_zoom, cancel)
        pieces = [(t[3], t[4]) for t in tiles + ancestors]
        total = sum(length for _o, length in set(pieces)) or 1
        done = [0]

        def advance(n: int) -> None:
            done[0] += n
            received(n)
            progress(min(1.0, done[0] / total))

        blobs = archive.fetch_ranges(pieces, cancel, advance)
        path = work / "basemap.sqlite"
        conn = sqlite3.connect(path)
        try:
            conn.executescript(CELL_SCHEMA)
            conn.executemany(
                "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                ((z, x, y, blobs[(o, n)]) for z, x, y, o, n in tiles),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO ancestors VALUES (?, ?, ?, ?)",
                ((z, x, y, blobs[(o, n)]) for z, x, y, o, n in ancestors),
            )
            meta = {"build": key, "maxzoom": max_zoom, "compression": archive.header.tile_compression}
            conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
            conn.commit()
        finally:
            conn.close()
        progress(1.0)
        return {
            "basemap_build": key,
            "basemap_maxzoom": max_zoom,
            "basemap_at": time.time(),
            "basemap_bytes": path.stat().st_size,
        }


class LowzoomStore:
    """Shared z0–z7 basemap tiles with one reference per cell."""

    def __init__(self, home: Home) -> None:
        self.home = home
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _db(self, create: bool) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if not create and not self.home.lowzoom_db.exists():
            return None
        self.home.ensure_data()
        conn = sqlite3.connect(self.home.lowzoom_db, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tiles (z INTEGER, x INTEGER, y INTEGER, data BLOB NOT NULL, "
            "compression INTEGER NOT NULL, PRIMARY KEY (z, x, y))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS refs (cell TEXT, z INTEGER, x INTEGER, y INTEGER, PRIMARY KEY (cell, z, x, y))"
        )
        self._conn = conn
        return conn

    def add(self, cid: str, rows: list[tuple[int, int, int, bytes]], compression: int) -> None:
        with self._lock:
            conn = self._db(create=True)
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?, ?)",
                [(z, x, y, data, compression) for z, x, y, data in rows],
            )
            conn.executemany("INSERT OR IGNORE INTO refs VALUES (?, ?, ?, ?)", [(cid, z, x, y) for z, x, y, _d in rows])
            conn.execute("COMMIT")

    def remove(self, cid: str) -> None:
        with self._lock:
            conn = self._db(create=False)
            if conn is None:
                return
            conn.execute("BEGIN")
            conn.execute("DELETE FROM refs WHERE cell=?", (cid,))
            conn.execute(
                "DELETE FROM tiles WHERE NOT EXISTS "
                "(SELECT 1 FROM refs r WHERE r.z = tiles.z AND r.x = tiles.x AND r.y = tiles.y)"
            )
            conn.execute("COMMIT")

    def get(self, z: int, x: int, y: int) -> tuple[bytes, int] | None:
        with self._lock:
            conn = self._db(create=False)
            if conn is None:
                return None
            row = conn.execute("SELECT data, compression FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
        return (row[0], row[1]) if row else None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def install_ancestors(cell_dir: Path, cid: str, lowzoom: LowzoomStore) -> None:
    """Move the ancestor tiles of a freshly installed cell into the shared store."""
    path = cell_dir / "basemap.sqlite"
    if not path.exists():
        return
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT z, x, y, data FROM ancestors").fetchall()
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        if rows:
            lowzoom.add(cid, rows, int(meta.get("compression", COMPRESSION_GZIP)))
            conn.execute("DELETE FROM ancestors")
            conn.commit()
    finally:
        conn.close()


class BasemapTiles:
    """Serves basemap tiles of cached cells (SPEC §10.5)."""

    def __init__(self, home: Home, coverage: CellCoverage, lowzoom: LowzoomStore, rail_lock_owner) -> None:
        self.home = home
        self.coverage = coverage
        self.lowzoom = lowzoom
        self.rail = rail_lock_owner  # RailTiles: shares the per-cell "busy" lock used during installs

    def tile(self, z: int, x: int, y: int) -> tuple[bytes, int] | None:
        if not (0 <= z <= 15 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return None
        if z < CELL_ZOOM:
            return self.lowzoom.get(z, x, y)
        shift = z - CELL_ZOOM
        cx, cy = x >> shift, y >> shift
        if not self.coverage.is_cached(cx, cy):
            return None
        return self.rail.basemap_tile(cell_id(cx, cy), z, x, y)
