"""``data/state.sqlite``: cells, download queue, station index, usage counters (SPEC §11.1).

The file is created the first time something has to be stored; until then
every read behaves as if the database were empty.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .grid import distance_km
from .paths import DataBlocked, Home

SAME_STATION_KM = 1.0  # same name and kind closer than this: one search result

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cells (
    id TEXT PRIMARY KEY,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    status TEXT NOT NULL,          -- queued | downloading | cached | updating | failed
    queue_order REAL,
    update_kind TEXT,              -- rail | all  (only while status = updating)
    stage TEXT,                    -- rail | basemap | process
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    rail_at REAL,                  -- when the railway data was downloaded
    basemap_at REAL,
    basemap_build TEXT,
    basemap_maxzoom INTEGER,
    rail_bytes INTEGER NOT NULL DEFAULT 0,
    basemap_bytes INTEGER NOT NULL DEFAULT 0,
    added REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
    cell TEXT NOT NULL,
    osm_type TEXT NOT NULL,
    osm_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    lon REAL NOT NULL,
    lat REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS stations_cell ON stations(cell);
CREATE TABLE IF NOT EXISTS usage (
    endpoint TEXT NOT NULL,
    day TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (endpoint, day)
);
"""

CELL_COLUMNS = (
    "id", "x", "y", "status", "queue_order", "update_kind", "stage", "progress", "error",
    "rail_at", "basemap_at", "basemap_build", "basemap_maxzoom", "rail_bytes", "basemap_bytes", "added",
)
CACHED_STATES = ("cached", "updating")
ACTIVE_STATES = ("queued", "downloading", "updating")


class StateStore:
    def __init__(self, home: Home) -> None:
        self.home = home
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        # Overpass usage counted while there is no database yet: a click on a
        # line outside the cache must not create the data folder (SPEC §6.6).
        self._memory_usage: dict[tuple[str, str], tuple[int, int]] = {}

    # -- connection --------------------------------------------------------------------

    def _db(self, create: bool) -> sqlite3.Connection | None:
        if self._conn is not None:
            return self._conn
        if self.home.blocked:  # 「清空缓存」 in progress (SPEC §11.3)
            if create:
                raise DataBlocked("the data folder is being cleared")
            return None
        path = self.home.state_db
        if not create and not path.exists():
            return None
        self.home.ensure_data()
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        self._conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """A transaction on the (created if needed) database."""
        with self._lock:
            conn = self._db(create=True)
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _read(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            conn = self._db(create=False)
            if conn is None:
                return []
            return conn.execute(sql, params).fetchall()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @property
    def exists(self) -> bool:
        return self._conn is not None or self.home.state_db.exists()

    # -- meta -----------------------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self._read("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0][0] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        with self.write() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    # -- cells ------------------------------------------------------------------------------

    @staticmethod
    def _row(row: tuple) -> dict:
        return dict(zip(CELL_COLUMNS, row))

    def cells(self) -> list[dict]:
        cols = ", ".join(CELL_COLUMNS)
        return [self._row(r) for r in self._read(f"SELECT {cols} FROM cells ORDER BY queue_order, id")]

    def cell(self, cell_id: str) -> dict | None:
        cols = ", ".join(CELL_COLUMNS)
        rows = self._read(f"SELECT {cols} FROM cells WHERE id=?", (cell_id,))
        return self._row(rows[0]) if rows else None

    def cached_cells(self) -> set[tuple[int, int]]:
        rows = self._read("SELECT x, y FROM cells WHERE status IN (?, ?)", CACHED_STATES)
        return {(x, y) for x, y in rows}

    def queue_cells(self, entries: list[tuple[str, int, int, float]]) -> list[str]:
        """Queue new cells (id, x, y, order); failed cells are re-queued.

        Returns the ids that were actually queued.
        """
        queued = []
        now = time.time()
        with self.write() as conn:
            for cid, x, y, order in entries:
                row = conn.execute("SELECT status FROM cells WHERE id=?", (cid,)).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO cells (id, x, y, status, queue_order, added) VALUES (?, ?, ?, 'queued', ?, ?)",
                        (cid, x, y, order, now),
                    )
                    queued.append(cid)
                elif row[0] == "failed":
                    conn.execute(
                        "UPDATE cells SET status='queued', queue_order=?, error=NULL, progress=0, stage=NULL WHERE id=?",
                        (order, cid),
                    )
                    queued.append(cid)
        return queued

    def request_update(self, cell_id: str, kind: str, order: float) -> bool:
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE cells SET status='updating', update_kind=?, queue_order=?, error=NULL, progress=0, stage=NULL "
                "WHERE id=? AND status IN ('cached', 'updating')",
                (kind, order, cell_id),
            )
            return cur.rowcount == 1

    def delete_cells(self, ids: list[str]) -> list[dict]:
        removed = []
        with self.write() as conn:
            for cid in ids:
                cols = ", ".join(CELL_COLUMNS)
                row = conn.execute(f"SELECT {cols} FROM cells WHERE id=?", (cid,)).fetchone()
                if row is None:
                    continue
                conn.execute("DELETE FROM cells WHERE id=?", (cid,))
                conn.execute("DELETE FROM stations WHERE cell=?", (cid,))
                removed.append(self._row(row))
        return removed

    def update_cell(self, cell_id: str, **fields) -> None:
        if not fields:
            return
        unknown = set(fields) - set(CELL_COLUMNS)
        if unknown:
            raise KeyError(f"unknown cell fields: {unknown}")
        assignments = ", ".join(f"{k}=?" for k in fields)
        with self.write() as conn:
            conn.execute(f"UPDATE cells SET {assignments} WHERE id=?", (*fields.values(), cell_id))

    def next_job(self, exclude: set[str] | frozenset[str] = frozenset()) -> dict | None:
        """The next cell to download or update, in queue order, skipping ``exclude``
        (cells other download workers are busy with)."""
        cols = ", ".join(CELL_COLUMNS)
        skip = ",".join("?" * len(exclude))
        rows = self._read(
            f"SELECT {cols} FROM cells WHERE (status IN ('queued', 'downloading') "
            "OR (status = 'updating' AND update_kind IS NOT NULL))"
            + (f" AND id NOT IN ({skip})" if exclude else "")
            + " ORDER BY queue_order, id LIMIT 1",
            tuple(sorted(exclude)),
        )
        return self._row(rows[0]) if rows else None

    # -- Overpass usage (SPEC §9.2) ----------------------------------------------------------

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def usage_today(self, endpoint: str) -> tuple[int, int]:
        rows = self._read("SELECT requests, bytes FROM usage WHERE endpoint=? AND day=?", (endpoint, self._today()))
        stored = (rows[0][0], rows[0][1]) if rows else (0, 0)
        memory = self._memory_usage.get((endpoint, self._today()), (0, 0))
        return stored[0] + memory[0], stored[1] + memory[1]

    def add_usage(self, endpoint: str, requests: int, size: int) -> None:
        day = self._today()
        with self._lock:
            if not self.exists or self.home.blocked:
                count, total = self._memory_usage.get((endpoint, day), (0, 0))
                self._memory_usage[(endpoint, day)] = (count + requests, total + size)
                return
        with self.write() as conn:
            conn.execute(
                "INSERT INTO usage (endpoint, day, requests, bytes) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(endpoint, day) DO UPDATE SET requests = requests + excluded.requests, "
                "bytes = bytes + excluded.bytes",
                (endpoint, day, requests, size),
            )
            conn.execute("DELETE FROM usage WHERE day < ?", (time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400)),))

    # -- station index (SPEC §6.7) -----------------------------------------------------------

    def replace_stations(self, cell_id: str, rows: list[tuple[str, int, str, str, float, float]]) -> None:
        """rows: (osm_type, osm_id, kind, name, lon, lat)."""
        with self.write() as conn:
            conn.execute("DELETE FROM stations WHERE cell=?", (cell_id,))
            conn.executemany(
                "INSERT INTO stations (cell, osm_type, osm_id, kind, name, lon, lat) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(cell_id, *row) for row in rows],
            )

    def search_stations(self, text: str, limit: int = 20) -> list[dict]:
        """Cached stations whose name contains ``text``.

        A station mapped several times (e.g. once per metro line, or found in
        two neighbouring cells) is listed once: same name and kind, < 1 km apart.
        """
        needle = text.strip().lower()
        if not needle:
            return []
        rows = self._read(
            "SELECT cell, osm_type, osm_id, kind, name, lon, lat FROM stations "
            "WHERE instr(lower(name), ?) > 0 ORDER BY length(name), name, osm_type, osm_id LIMIT ?",
            (needle, limit * 5),
        )
        keys = ("cell", "osmType", "osmId", "kind", "name", "lon", "lat")
        found: list[dict] = []
        for row in rows:
            item = dict(zip(keys, row))
            if any(
                f["name"] == item["name"] and f["kind"] == item["kind"]
                and distance_km(f["lon"], f["lat"], item["lon"], item["lat"]) < SAME_STATION_KM
                for f in found
            ):
                continue
            found.append(item)
            if len(found) == limit:
                break
        return found

    def reset_interrupted(self) -> None:
        """After a restart, cells that were mid-download go back to the queue."""
        if not self.exists:
            return
        with self.write() as conn:
            conn.execute("UPDATE cells SET status='queued', stage=NULL, progress=0 WHERE status='downloading'")
            conn.execute("UPDATE cells SET stage=NULL, progress=0 WHERE status='updating'")
