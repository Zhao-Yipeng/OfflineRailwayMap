"""Serving cached railway vector tiles (SPEC §10.4).

* z8–z14: read the tile generated when the cell was processed;
* z2–z7: merge the simplified geometry of every cached cell inside the tile,
  encode, and keep the result in memory and in ``data/cache/rail_lowzoom.sqlite``.
  The cache key contains the version of every cell involved, so a tile is
  rebuilt only when one of its cells changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager

import shapely

from .coverage import CELL_ZOOM, CellCoverage
from .grid import cell_id
from .paths import DataBlocked, Home
from .processing import MAX_TILE_ZOOM, encode_lowzoom

log = logging.getLogger(__name__)

MIN_ZOOM = 2
MAX_OPEN_CELLS = 64
MEMORY_TILES = 256
DISK_TILES = 5000


class RailTiles:
    def __init__(self, home: Home, coverage: CellCoverage) -> None:
        self.home = home
        self.coverage = coverage
        self._lock = threading.RLock()
        self._conns: OrderedDict[tuple[str, str], sqlite3.Connection] = OrderedDict()
        self._compression: dict[str, int] = {}
        self._busy: set[str] = set()
        self._versions: dict[str, str] = {}
        self._memory: OrderedDict[str, bytes] = OrderedDict()
        self._disk: sqlite3.Connection | None = None
        self.on_corrupt = lambda cid: None  # SPEC §15: a damaged cell database -> cell failed

    # -- versions ---------------------------------------------------------------------

    def set_version(self, cid: str, version) -> None:
        with self._lock:
            self._versions[cid] = str(version)

    def load_versions(self, rows: list[dict]) -> None:
        for row in rows:
            if row["status"] in ("cached", "updating"):
                self.set_version(row["id"], row["rail_at"])

    def forget(self, ids: list[str]) -> None:
        for cid in ids:
            self.close_cell(cid)
            with self._lock:
                self._versions.pop(cid, None)

    # -- cell databases ------------------------------------------------------------------

    def _cell_conn(self, cid: str, name: str = "rail.sqlite") -> sqlite3.Connection | None:
        """Read-only connection to one of the cell's databases (rail or basemap)."""
        key = (cid, name)
        with self._lock:
            if cid in self._busy or self.home.blocked:
                return None
            conn = self._conns.get(key)
            if conn is not None:
                self._conns.move_to_end(key)
                return conn
            path = self.home.cell_dir(cid) / name
            if not path.exists():
                return None
            conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, check_same_thread=False)
            self._conns[key] = conn
            while len(self._conns) > MAX_OPEN_CELLS:
                _, old = self._conns.popitem(last=False)
                old.close()
            return conn

    def close_cell(self, cid: str) -> None:
        with self._lock:
            for key in [k for k in self._conns if k[0] == cid]:
                self._conns.pop(key).close()
            self._compression.pop(cid, None)

    def basemap_tile(self, cid: str, z: int, x: int, y: int) -> tuple[bytes, int] | None:
        """(data, compression) of a cell's offline basemap tile."""
        with self._lock:
            conn = self._cell_conn(cid, "basemap.sqlite")
            if conn is None:
                return None
            compression = self._compression.get(cid)
            if compression is None:
                row = conn.execute("SELECT value FROM meta WHERE key='compression'").fetchone()
                compression = self._compression[cid] = int(row[0]) if row else 2
            row = conn.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
        return (row[0], compression) if row else None

    @contextmanager
    def exclusive(self, cid: str):
        """Keep the cell's files closed while they are being replaced."""
        with self._lock:
            self._busy.add(cid)
        self.close_cell(cid)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(cid)

    def close_all(self) -> None:
        with self._lock:
            for conn in self._conns.values():
                conn.close()
            self._conns.clear()
            self._memory.clear()
            if self._disk is not None:
                self._disk.close()
                self._disk = None

    def way_tags(self, cid: str, way_id: int) -> dict | None:
        with self._lock:
            conn = self._cell_conn(cid)
            if conn is None:
                return None
            row = conn.execute("SELECT tags FROM ways WHERE id=?", (way_id,)).fetchone()
        return json.loads(row[0]) if row else None

    # -- tiles ----------------------------------------------------------------------------------

    def tile(self, z: int, x: int, y: int) -> bytes:
        if not (MIN_ZOOM <= z <= MAX_TILE_ZOOM and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return b""
        if z >= CELL_ZOOM:
            shift = z - CELL_ZOOM
            cx, cy = x >> shift, y >> shift
            if not self.coverage.is_cached(cx, cy):
                return b""
            cid = cell_id(cx, cy)
            try:
                with self._lock:
                    conn = self._cell_conn(cid)
                    if conn is None:
                        return b""
                    row = conn.execute("SELECT data FROM tiles WHERE z=? AND x=? AND y=?", (z, x, y)).fetchone()
            except sqlite3.DatabaseError:
                log.exception("railway data of cell %s is damaged", cid)
                self.close_cell(cid)
                self.on_corrupt(cid)
                return b""
            return row[0] if row else b""
        return self._lowzoom(z, x, y)

    def _lowzoom(self, z: int, x: int, y: int) -> bytes:
        n = 1 << (CELL_ZOOM - z)
        x0, y0 = x * n, y * n
        cells = sorted(
            cell_id(cx, cy) for cx, cy in self.coverage.cells() if x0 <= cx < x0 + n and y0 <= cy < y0 + n
        )
        if not cells:
            return b""
        with self._lock:
            parts = ",".join(f"{c}@{self._versions.get(c, '')}" for c in cells)
        key = f"{z}/{x}/{y}/" + hashlib.sha1(parts.encode()).hexdigest()
        cached = self._memory_get(key)
        if cached is None:
            cached = self._disk_get(key)
            if cached is not None:
                self._memory_put(key, cached)
        if cached is not None:
            return cached
        level = 5 if z <= 5 else 7
        features = []
        for cid in cells:
            with self._lock:
                conn = self._cell_conn(cid)
                if conn is None:
                    continue
                rows = conn.execute("SELECT id, props, geom FROM lod WHERE level=?", (level,)).fetchall()
            features.extend((way_id, json.loads(props), shapely.from_wkb(geom)) for way_id, props, geom in rows)
        data = encode_lowzoom(z, x, y, features)
        self._memory_put(key, data)
        self._disk_put(key, data)
        return data

    def _memory_get(self, key: str) -> bytes | None:
        with self._lock:
            data = self._memory.get(key)
            if data is not None:
                self._memory.move_to_end(key)
            return data

    def _memory_put(self, key: str, data: bytes) -> None:
        with self._lock:
            self._memory[key] = data
            while len(self._memory) > MEMORY_TILES:
                self._memory.popitem(last=False)

    def _disk_conn(self, create: bool) -> sqlite3.Connection | None:
        if self._disk is not None:
            return self._disk
        if self.home.blocked:  # 「清空缓存」 in progress (SPEC §11.3)
            if create:
                raise DataBlocked("the data folder is being cleared")
            return None
        path = self.home.cache_dir / "rail_lowzoom.sqlite"
        if not create and not path.exists():
            return None
        self.home.ensure_dir(self.home.cache_dir)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS tiles (key TEXT PRIMARY KEY, data BLOB, used REAL)")
        self._disk = conn
        return conn

    def _disk_get(self, key: str) -> bytes | None:
        with self._lock:
            conn = self._disk_conn(create=False)
            if conn is None:
                return None
            row = conn.execute("SELECT data FROM tiles WHERE key=?", (key,)).fetchone()
            if row is not None:
                conn.execute("UPDATE tiles SET used=? WHERE key=?", (time.time(), key))
            return row[0] if row else None

    def _disk_put(self, key: str, data: bytes) -> None:
        try:
            with self._lock:
                conn = self._disk_conn(create=True)
                conn.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?, ?)", (key, data, time.time()))
                count = conn.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
                if count > DISK_TILES:
                    conn.execute(
                        "DELETE FROM tiles WHERE key IN (SELECT key FROM tiles ORDER BY used LIMIT ?)",
                        (count - DISK_TILES + DISK_TILES // 10,),
                    )
        except (OSError, sqlite3.Error):
            log.exception("could not store a low-zoom railway tile")
