"""Railway data from regional OSM extracts (SPEC §9.8).

When many cells are downloaded (``EXTRACT_MIN_CELLS`` or more), their railway
data comes from Geofabrik's regional extracts instead of the public Overpass
servers: the OSM wiki recommends extracts for large amounts of data, and one
download of a region serves all of its cells.  The data is the same OSM data
Overpass returns; the extracts are updated once a day.

* index: ``index-v1.json`` lists the regions with parent, name, polygon and
  download URLs;
* planning: every cell gets the smallest regions that together cover it
  (provinces rather than their country, countries rather than groups like
  "dach"); the parent (one download) replaces its children when it is not
  much bigger than the children needed anyway; a region that is large
  compared with the cells it serves is not used, and those cells keep using
  Overpass;
* download: ``data/extracts/<region>.osm.pbf``, resumed after interruptions
  and checked against Geofabrik's ``.md5``; then filtered with osmium into
  ``data/extracts/<region>.railways.sqlite`` (the objects our Overpass query
  returns) and the ``.pbf`` is deleted;
* a filtered region is used for ``REUSE_FOR`` (7 days), then fetched again
  when it is needed; older ones are deleted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import shutil
import sqlite3
import threading
import time
from array import array
from pathlib import Path
from typing import Callable

import numpy as np
import requests
import shapely
import shapely.errors
from shapely.geometry import box, shape

from .grid import CELL_ZOOM, cell_bounds, cell_id, lonlat_to_tile
from .paths import Home

log = logging.getLogger(__name__)

INDEX_URL = "https://download.geofabrik.de/index-v1.json"
EXTRACT_MIN_CELLS = 6  # from this many cells waiting, extracts are preferred (user's choice)
REUSE_FOR = 7 * 24 * 3600.0
PARTIAL_KEPT_FOR = 24 * 3600.0  # an interrupted download is continued within this time
# A region bigger than this per cell it serves is not worth it (all of a
# neighbouring country for one border cell).  Real sizes (2026-11): Chinese
# provinces checked: Macau 3 MB to Guangdong 175 MB (Hebei 108, Beijing 37);
# Bavaria 857 MB; Japan's Kanto 524 MB and Chubu 516 MB.
MAX_MB_PER_CELL = 250
FAILED_PAUSE = 30 * 60.0  # after a failed download, cells use Overpass for this long
RETRY_DELAYS = (5, 15, 45, 120)  # seconds before continuing a broken download
MIN_SHARE = 0.005  # share of a cell a region must cover to be used for it
SLIVER = 0.03  # degrees: leftovers narrower than about twice this are border slivers
LINE_TYPES = frozenset({"rail", "narrow_gauge", "subway", "light_rail", "tram", "construction", "proposed", "disused"})
STATION_TYPES = frozenset({"station", "halt"})
CHUNK = 1 << 20

Activity = Callable[..., None]


class ExtractUnavailable(Exception):
    """The extracts cannot be used right now; the cell falls back to Overpass."""


class Cancelled(Exception):
    pass


# -- index and planning -----------------------------------------------------------------


class Region:
    def __init__(self, rid: str, parent: str | None, name: str, pbf: str, geometry) -> None:
        self.id = rid
        self.parent = parent
        self.name = name
        self.pbf = pbf
        self.geometry = geometry
        self.children: list[Region] = []


class RegionIndex:
    """Geofabrik's regions: which ones touch a cell, and how they nest."""

    def __init__(self, features: list[dict]) -> None:
        self.regions: dict[str, Region] = {}
        for feature in features:
            props = feature.get("properties") or {}
            pbf = (props.get("urls") or {}).get("pbf")
            if not props.get("id") or not pbf or not feature.get("geometry"):
                continue
            try:
                geometry = _polygonal(shape(feature["geometry"]))
            except (ValueError, TypeError, KeyError, shapely.errors.GEOSException):
                continue
            if geometry is None:
                continue
            region = Region(props["id"], props.get("parent"), props.get("name") or props["id"], pbf, geometry)
            self.regions[region.id] = region
        for region in self.regions.values():
            parent = self.regions.get(region.parent or "")
            if parent is not None:
                parent.children.append(region)
        self._list = list(self.regions.values())
        self._tree = shapely.STRtree([r.geometry for r in self._list])
        self._contains: dict[tuple[str, str], bool] = {}
        self._groups: dict[str, bool] = {}

    @classmethod
    def from_json(cls, data: dict) -> RegionIndex:
        return cls(data.get("features") or [])

    def touching(self, cx: int, cy: int) -> list[Region]:
        west, south, east, north = cell_bounds(cx, cy)
        area = box(west, south, east, north)
        hits = self._tree.query(area, predicate="intersects")
        return [self._list[i] for i in hits]

    def around(self, outer: Region, inner: Region) -> bool:
        """True if ``outer`` holds ``inner``: its parent, grandparent ..., or a group of
        regions like "us-northeast" or "dach" that covers (nearly) all of it."""
        key = (outer.id, inner.id)
        if key not in self._contains:
            found, region, seen = False, inner, set()
            while region is not None and region.parent and region.id not in seen:
                seen.add(region.id)
                if region.parent == outer.id:
                    found = True
                    break
                region = self.regions.get(region.parent)
            if not found and outer.geometry.area > inner.geometry.area and self.is_group(outer):
                found = _shared_area(outer.geometry, inner.geometry) >= 0.9 * inner.geometry.area
            self._contains[key] = found
        return self._contains[key]

    def is_group(self, region: Region) -> bool:
        """True for a region made of other regions (Geofabrik's "dach", "us-northeast",
        "britain-and-ireland" ...), as opposed to a country that merely surrounds a
        small neighbour (Switzerland and Liechtenstein, Italy and San Marino)."""
        if region.id not in self._groups:
            members = []
            for index in self._tree.query(region.geometry, predicate="intersects"):
                other = self._list[int(index)]
                if other is region or other.parent is None or other.geometry.area >= region.geometry.area:
                    continue
                if _shared_area(region.geometry, other.geometry) >= 0.9 * other.geometry.area:
                    members.append(other.geometry)
            covered = _shared_area(shapely.union_all(members), region.geometry) if members else 0.0
            self._groups[region.id] = covered >= 0.6 * region.geometry.area
        return self._groups[region.id]

    def most_specific(self, cx: int, cy: int) -> list[Region]:
        """Regions that together hold everything in the cell, as small as possible.

        The smallest regions come first.  A bigger region around ones already
        chosen (a country around its provinces, a group like "us-northeast"
        or "dach" around its countries) is not used: their own parts cover
        its land, and what it has beyond them is sea or a border strip.  Any
        other region is used for what the smaller ones leave, but not for
        slivers along their borders: the extracts keep whole ways, so a
        railway crossing a border is in both neighbours.  Continents (no
        parent) are never used.
        """
        west, south, east, north = cell_bounds(cx, cy)
        cell = box(west, south, east, north)
        least = cell.area * MIN_SHARE
        candidates = []
        for region in self.touching(cx, cy):
            if region.parent is None:
                continue
            part = region.geometry.intersection(cell)
            if part.area >= least:
                candidates.append((region.geometry.area, region.id, region, part))
        candidates.sort(key=lambda item: item[:2])
        chosen: list[tuple[Region, object]] = []
        covered = None
        for _, _, region, part in candidates:
            if any(other.intersects(part) and self.around(region, kept) for kept, other in chosen):
                continue
            new = part if covered is None else part.difference(covered)
            if new.area < least:
                continue
            if new.area < part.area / 2 and new.buffer(-SLIVER).is_empty:
                continue  # mostly covered already, and the rest is a thin strip
            chosen.append((region, part))
            covered = part if covered is None else covered.union(part)
        return [region for region, _part in chosen]


def plan(index: RegionIndex, cells: list[tuple[int, int]], size_of: Callable[[Region], int | None]) -> dict[str, list[str]]:
    """Cell id -> region ids to read it from; cells left out use Overpass."""
    needs: dict[str, list[Region]] = {cell_id(x, y): index.most_specific(x, y) for x, y in cells}
    needs = {cid: regions for cid, regions in needs.items() if regions}

    def mb(region: Region) -> float | None:
        size = size_of(region)
        return None if size is None else size / (1024 * 1024)

    def cells_using(ids: set[str]) -> int:
        return sum(1 for regions in needs.values() if any(r.id in ids for r in regions))

    # A region much bigger than the cells it serves (an entire neighbouring
    # country for one border cell) is not worth it: those cells use Overpass.
    # Leaving them out can make other regions serve fewer cells, so repeat.
    while True:
        counts: dict[str, int] = {}
        for regions in needs.values():
            for region in regions:
                counts[region.id] = counts.get(region.id, 0) + 1
        too_big = set()
        for rid, count in counts.items():
            size = mb(index.regions[rid])
            if size is not None and size / count > MAX_MB_PER_CELL:
                too_big.add(rid)
        if not too_big:
            break
        needs = {cid: regions for cid, regions in needs.items() if not any(r.id in too_big for r in regions)}

    # A parent (one download) replaces its children when it is not much bigger
    # than the children needed anyway; without sizes, when half of them are needed.
    def worth_parent(parent: Region, kids: list[Region]) -> bool:
        if len(kids) < 2:
            return False
        parent_size = size_of(parent)
        kid_sizes = [size_of(k) for k in kids]
        if parent_size is not None and None not in kid_sizes:
            if parent_size / (1024 * 1024) / cells_using({k.id for k in kids}) > MAX_MB_PER_CELL:
                return False
            return parent_size <= 1.25 * sum(kid_sizes)
        return len(kids) * 2 >= len(parent.children)

    changed = True
    while changed:
        changed = False
        used = {r.id: r for regions in needs.values() for r in regions}
        parents = {index.regions[r.parent] for r in used.values() if r.parent in index.regions}
        for parent in sorted(parents, key=lambda region: region.id):
            if parent.parent is None:  # continents are too big
                continue
            kids = [k for k in parent.children if k.id in used]
            if worth_parent(parent, kids):
                kid_ids = {k.id for k in kids}
                for cid, regions in needs.items():
                    if any(r.id in kid_ids for r in regions):
                        needs[cid] = [r for r in regions if r.id not in kid_ids] + ([parent] if parent not in regions else [])
                changed = True
                break
    return {cid: [r.id for r in regions] for cid, regions in needs.items()}


# -- filtering ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE ways (id INTEGER PRIMARY KEY, tags TEXT NOT NULL, coords BLOB NOT NULL);
CREATE TABLE way_cells (cell TEXT NOT NULL, way INTEGER NOT NULL);
CREATE TABLE stations (type TEXT NOT NULL, id INTEGER NOT NULL, lon REAL, lat REAL, tags TEXT NOT NULL, cell TEXT NOT NULL);
"""
INDEXES = """
CREATE INDEX way_cells_cell ON way_cells(cell);
CREATE INDEX stations_cell ON stations(cell);
"""


def _cells_of_bbox(west: float, south: float, east: float, north: float) -> list[str]:
    x0, y0 = (int(v) for v in lonlat_to_tile(west, north, CELL_ZOOM))
    x1, y1 = (int(v) for v in lonlat_to_tile(east, south, CELL_ZOOM))
    top = (1 << CELL_ZOOM) - 1
    return [
        cell_id(x, y)
        for x in range(max(0, x0), min(top, x1) + 1)
        for y in range(max(0, y0), min(top, y1) + 1)
    ]


def filter_railways(
    pbf: Path,
    out: Path,
    cancel: threading.Event | None = None,
    progress: Callable[[float], None] = lambda p: None,
) -> dict:
    """The railway ways and stations of an extract, ready to be read per cell.

    Two passes over the file keep memory small: first the railway ways and
    stations (with their node ids), then the locations of just those nodes.
    """
    import osmium
    from osmium.filter import IdFilter, KeyFilter
    from osmium.osm import NODE, RELATION, WAY

    def check() -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled()

    started = time.time()
    header = osmium.FileProcessor(str(pbf)).header
    data_time = header.get("osmosis_replication_timestamp") or ""
    way_ids: list[int] = []
    way_tags: list[str] = []
    way_kind: list[bool] = []  # True: line, False: station area
    refs = array("q")
    offsets = array("q", [0])
    stations: list[tuple[str, int, float, float, str]] = []
    count = 0
    for obj in osmium.FileProcessor(str(pbf), NODE | WAY | RELATION).with_filter(KeyFilter("railway")):
        count += 1
        if count % 20000 == 0:
            check()
        railway = obj.tags.get("railway")
        if obj.is_node():
            if railway in STATION_TYPES and obj.location.valid():
                tags = json.dumps(dict(obj.tags), ensure_ascii=False, separators=(",", ":"))
                stations.append(("node", obj.id, obj.location.lon, obj.location.lat, tags))
        elif obj.is_way():
            if railway in LINE_TYPES or railway in STATION_TYPES:
                way_ids.append(obj.id)
                way_tags.append(json.dumps(dict(obj.tags), ensure_ascii=False, separators=(",", ":")))
                way_kind.append(railway in LINE_TYPES)
                refs.extend(node.ref for node in obj.nodes)
                offsets.append(len(refs))
    progress(0.4)
    check()
    all_refs = np.frombuffer(refs, dtype=np.int64) if len(refs) else np.zeros(0, dtype=np.int64)
    ids = np.unique(all_refs)
    lons = np.full(len(ids), np.nan)
    lats = np.full(len(ids), np.nan)
    if len(ids):
        count = 0
        for node in osmium.FileProcessor(str(pbf), NODE).with_filter(IdFilter(ids)):
            count += 1
            if count % 50000 == 0:
                check()
            i = np.searchsorted(ids, node.id)
            if node.location.valid():
                lons[i] = node.location.lon
                lats[i] = node.location.lat
    progress(0.8)
    check()

    tmp = out.with_suffix(".part")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SCHEMA)
        positions = np.searchsorted(ids, all_refs) if len(ids) else all_refs
        way_rows, cell_rows = [], []
        for n, way_id in enumerate(way_ids):
            pos = positions[offsets[n]:offsets[n + 1]]
            coords = np.column_stack((lons[pos], lats[pos]))
            coords = coords[~np.isnan(coords).any(axis=1)]
            if len(coords) == 0:
                continue
            west, south = coords.min(axis=0)
            east, north = coords.max(axis=0)
            if way_kind[n]:
                if len(coords) < 2:
                    continue
                way_rows.append((way_id, way_tags[n], coords.astype(np.float64).tobytes()))
                cell_rows.extend((c, way_id) for c in _cells_of_bbox(west, south, east, north))
            else:  # a station drawn as an area: its centre, like Overpass "out center"
                lon, lat = (west + east) / 2, (south + north) / 2
                stations.append(("way", way_id, float(lon), float(lat), way_tags[n]))
        conn.executemany("INSERT OR REPLACE INTO ways VALUES (?, ?, ?)", way_rows)
        conn.executemany("INSERT INTO way_cells VALUES (?, ?)", cell_rows)
        conn.executemany(
            "INSERT INTO stations VALUES (?, ?, ?, ?, ?, ?)",
            [(t, i, lon, lat, tags, cell_id(*(int(v) for v in lonlat_to_tile(lon, lat, CELL_ZOOM))))
             for t, i, lon, lat, tags in stations],
        )
        conn.executescript(INDEXES)
        meta = {"data_time": data_time, "filtered_at": str(time.time()), "ways": str(len(way_rows)),
                "stations": str(len(stations))}
        conn.executemany("INSERT INTO meta VALUES (?, ?)", list(meta.items()))
        conn.commit()
    finally:
        conn.close()
    tmp.replace(out)
    progress(1.0)
    log.info("filtered %s: %d railway ways, %d stations in %.0f s", pbf.name, len(way_rows), len(stations),
             time.time() - started)
    return meta


def read_cell(db: Path, cx: int, cy: int) -> list[dict]:
    """The cell's railway elements from a filtered extract (Overpass-like, see processing)."""
    cid = cell_id(cx, cy)
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        elements = [
            {"type": "way", "id": way_id, "tags": json.loads(tags),
             "coords": np.frombuffer(blob, dtype=np.float64).reshape(-1, 2)}
            for way_id, tags, blob in conn.execute(
                "SELECT w.id, w.tags, w.coords FROM way_cells c JOIN ways w ON w.id = c.way WHERE c.cell = ?", (cid,)
            )
        ]
        elements += [
            {"type": t, "id": i, "lon": lon, "lat": lat, "tags": json.loads(tags)}
            for t, i, lon, lat, tags in conn.execute("SELECT type, id, lon, lat, tags FROM stations WHERE cell = ?", (cid,))
        ]
    finally:
        conn.close()
    return elements


# -- downloads and the per-region work ------------------------------------------------------


class _RegionJob:
    """One region being downloaded and filtered, shared by every cell that needs it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: Exception | None = None
        self.text = ""
        self.fraction = 0.0
        self.listeners: list[Callable[[str, float], None]] = []


class ExtractStore:
    """Downloads, filters and serves regional extracts (thread-safe)."""

    def __init__(self, home: Home, session: requests.Session, index_url: str = INDEX_URL) -> None:
        self.home = home
        self.session = session
        self.index_url = index_url
        self._index: RegionIndex | None = None
        self._index_lock = threading.Lock()
        self._sizes: dict[str, int | None] = {}
        self._jobs: dict[str, _RegionJob] = {}
        self._lock = threading.Lock()
        self._failed_until: dict[str, float] = {}
        self._stop = threading.Event()

    @property
    def folder(self) -> Path:
        return self.home.data / "extracts"

    def db_path(self, rid: str) -> Path:
        return self.folder / f"{_safe(rid)}.railways.sqlite"

    # -- index, sizes, plan ----------------------------------------------------------------

    def index(self) -> RegionIndex:
        with self._index_lock:
            if self._index is None:
                try:
                    response = self.session.get(self.index_url, timeout=(20, 120))
                    response.raise_for_status()
                    self._index = RegionIndex.from_json(response.json())
                except (requests.RequestException, ValueError) as exc:
                    raise ExtractUnavailable(f"地区数据包目录读取失败：{exc}") from exc
                log.info("extract index: %d regions", len(self._index.regions))
            return self._index

    def size_of(self, region: Region) -> int | None:
        if region.id not in self._sizes:
            try:
                response = self.session.head(region.pbf, timeout=(20, 30), allow_redirects=True)
                self._sizes[region.id] = int(response.headers.get("Content-Length", "")) if response.ok else None
            except (requests.RequestException, ValueError):
                self._sizes[region.id] = None
        return self._sizes[region.id]

    def plan(self, cells: list[tuple[int, int]]) -> dict[str, list[str]]:
        index = self.index()
        return plan(index, cells, self.size_of)

    def fresh(self, rid: str) -> bool:
        """True if the region was filtered recently enough to be used."""
        path = self.db_path(rid)
        if self.home.blocked or not path.exists():
            return False
        return time.time() - path.stat().st_mtime < REUSE_FOR

    def usable(self, rid: str) -> bool:
        return time.time() >= self._failed_until.get(rid, 0.0)

    def any_fresh(self) -> bool:
        """True if some region was filtered recently (no index download needed to know)."""
        if self.home.blocked or not self.folder.is_dir():
            return False
        now = time.time()
        return any(
            p.name.endswith(".railways.sqlite") and now - p.stat().st_mtime < REUSE_FOR for p in self.folder.iterdir()
        )

    # -- getting a cell's data -----------------------------------------------------------------

    def cell_elements(
        self,
        cx: int,
        cy: int,
        regions: list[str],
        cancel: threading.Event,
        activity: Activity,
        received: Callable[[int], None],
        progress: Callable[[float], None] = lambda p: None,
    ) -> list[dict]:
        """The cell's railway elements from its regions (downloaded and filtered if needed)."""
        for rid in regions:
            if not self.usable(rid):
                raise ExtractUnavailable(f"地区数据包 {rid} 暂时不可用")
        for n, rid in enumerate(regions):
            share = 1 / len(regions)
            self._ensure(rid, cancel, activity, received, lambda f, n=n: progress((n + f) * share))
        found: dict[tuple[str, int], dict] = {}
        for rid in regions:
            for element in read_cell(self.db_path(rid), cx, cy):
                key = (element["type"], element["id"])
                old = found.get(key)
                # a way cut at a region border: keep the more complete copy
                if old is None or len(element.get("coords", ())) > len(old.get("coords", ())):
                    found[key] = element
        return list(found.values())

    def _ensure(
        self,
        rid: str,
        cancel: threading.Event,
        activity: Activity,
        received: Callable[[int], None],
        progress: Callable[[float], None],
    ) -> None:
        if self.fresh(rid):
            return
        name = self.index().regions[rid].name if rid in self.index().regions else rid
        with self._lock:
            job = self._jobs.get(rid)
            starter = job is None
            if starter:
                job = self._jobs[rid] = _RegionJob()

        def tell(text: str, fraction: float) -> None:
            activity(text)
            progress(fraction)

        job.listeners.append(tell)
        try:
            if starter:
                thread = threading.Thread(
                    target=self._run_job, args=(rid, name, job, received), name=f"extract-{rid}", daemon=True
                )
                thread.start()
            else:
                activity(job.text or f"等待地区数据包「{name}」")
            while not job.done.wait(0.5):
                if cancel.is_set():
                    raise Cancelled()
        finally:
            if tell in job.listeners:
                job.listeners.remove(tell)
        if job.error is not None:
            raise ExtractUnavailable(str(job.error))

    def _run_job(self, rid: str, name: str, job: _RegionJob, received: Callable[[int], None]) -> None:
        def report(text: str, fraction: float) -> None:
            job.text, job.fraction = text, fraction
            for listener in list(job.listeners):
                listener(text, fraction)

        stop = self._stop  # this job's stop signal, even if a later stop() makes a new one
        try:
            region = self.index().regions[rid]
            self.home.ensure_dir(self.folder)
            pbf = self.folder / f"{_safe(rid)}.osm.pbf"
            if not pbf.exists():
                self._download(region, pbf, name, report, received, stop)
            report(f"筛选地区数据包「{name}」中的铁路", 0.0)
            filter_railways(
                pbf, self.db_path(rid), stop,
                lambda p: report(f"筛选地区数据包「{name}」中的铁路 {round(p * 100)}%", p),
            )
            pbf.unlink(missing_ok=True)
        except Cancelled:  # closing or clearing: not a failure of the region
            job.error = ExtractUnavailable(f"地区数据包「{name}」已停止")
        except Exception as exc:  # noqa: BLE001 - every cell waiting falls back to Overpass
            log.warning("extract %s failed: %s", rid, exc)
            self._failed_until[rid] = time.time() + FAILED_PAUSE
            job.error = exc if isinstance(exc, ExtractUnavailable) else ExtractUnavailable(f"地区数据包「{name}」：{exc}")
        finally:
            job.done.set()
            with self._lock:
                self._jobs.pop(rid, None)

    def _download(self, region: Region, path: Path, name: str, report, received, stop: threading.Event) -> None:
        """Download (continuing a partial file) and check the MD5 sum."""
        size = self.size_of(region)
        if size is not None:
            free = shutil.disk_usage(self.home.root).free
            if free < size * 1.3 + 1024**3:
                raise ExtractUnavailable(f"磁盘空间不足，无法下载地区数据包「{name}」")
        expected = self._md5(region)
        part = path.with_suffix(".pbf.part")
        for again in (False, True):
            resumed = part.exists() and part.stat().st_size > 0
            self._fetch(region, part, name, size, report, received, stop)
            if expected is None or self._digest(part, name, report, stop) == expected:
                part.replace(path)
                return
            part.unlink(missing_ok=True)
            if again or not resumed:
                break
            # The partial file was of an older version of the extract: once more from the start.
            expected = self._md5(region) or expected
        raise ExtractUnavailable(f"地区数据包「{name}」校验失败，稍后重新下载")

    def _fetch(self, region: Region, part: Path, name: str, size: int | None, report, received, stop) -> None:
        """Get the file into ``part``, continuing after a broken connection (``RETRY_DELAYS``)."""
        validator = None  # the version being downloaded (If-Range: never mix two versions)
        for delay in (*RETRY_DELAYS, None):
            have = part.stat().st_size if part.exists() else 0
            if size is not None and have == size:
                return
            headers = {"Accept-Encoding": "identity"}  # byte ranges of the file itself
            if have:
                headers["Range"] = f"bytes={have}-"
                if validator:
                    headers["If-Range"] = validator
            try:
                with self.session.get(region.pbf, headers=headers, stream=True, timeout=(20, 120)) as response:
                    if response.status_code == 416:  # nothing left to get
                        return
                    if response.status_code not in (200, 206):
                        raise ExtractUnavailable(f"地区数据包「{name}」：HTTP {response.status_code}")
                    etag = response.headers.get("ETag", "")
                    validator = (etag if etag and not etag.startswith("W/") else None) or response.headers.get(
                        "Last-Modified") or validator
                    if response.status_code == 200:
                        have = 0  # the whole file (it changed, or the server does not do ranges)
                    total = size or (have + int(response.headers.get("Content-Length") or 0))
                    with open(part, "ab" if have else "wb") as out:
                        last = 0.0
                        for chunk in response.iter_content(CHUNK):
                            if stop.is_set():
                                raise Cancelled()
                            out.write(chunk)
                            have += len(chunk)
                            received(len(chunk))
                            if time.monotonic() - last > 1.0:
                                last = time.monotonic()
                                text = f"下载地区数据包「{name}」{have / 1e6:.0f}" + (
                                    f" / {total / 1e6:.0f} MB" if total else " MB")
                                report(text, have / total if total else 0.0)
                return
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                if delay is None:
                    raise ExtractUnavailable(f"地区数据包「{name}」下载中断：{exc}") from exc
                log.info("extract %s: connection broken (%s), continuing in %d s", region.id, exc, delay)
                report(f"地区数据包「{name}」下载中断，{delay} 秒后继续", have / size if size else 0.0)
                if stop.wait(delay):
                    raise Cancelled() from None

    def _digest(self, part: Path, name: str, report, stop: threading.Event) -> str:
        report(f"校验地区数据包「{name}」", 1.0)
        digest = hashlib.md5()
        with open(part, "rb") as data:
            for block in iter(lambda: data.read(CHUNK * 8), b""):
                if stop.is_set():
                    raise Cancelled()
                digest.update(block)
        return digest.hexdigest()

    def _md5(self, region: Region) -> str | None:
        try:
            response = self.session.get(region.pbf + ".md5", timeout=(20, 30))
            if response.ok:
                match = re.match(r"\s*([0-9a-fA-F]{32})", response.text)
                return match.group(1).lower() if match else None
        except requests.RequestException:
            pass
        return None

    # -- housekeeping ----------------------------------------------------------------------------

    def remove_stale(self) -> None:
        """At start: delete filtered regions older than ``REUSE_FOR``, downloads
        older than ``PARTIAL_KEPT_FOR`` (newer ones are continued) and leftovers
        of interrupted filtering."""
        if self.home.blocked or not self.folder.is_dir():
            return
        now = time.time()
        for path in self.folder.iterdir():
            age = now - path.stat().st_mtime
            name = path.name
            if (
                name.endswith(".railways.part")
                or (name.endswith(".railways.sqlite") and age > REUSE_FOR)
                or (name.endswith((".osm.pbf", ".osm.pbf.part")) and age > PARTIAL_KEPT_FOR)
            ):
                path.unlink(missing_ok=True)

    def stop(self, timeout: float = 15.0) -> None:
        """Stop downloads and filtering (closing, clearing the cache); later jobs may start again."""
        self._stop.set()
        end = time.monotonic() + timeout
        while self._jobs and time.monotonic() < end:
            time.sleep(0.1)
        self._stop = threading.Event()

    def forget(self) -> None:
        """After 「清空缓存」: nothing is known about local files any more."""
        self._failed_until.clear()


def _shared_area(a, b) -> float:
    try:
        return a.intersection(b).area
    except shapely.errors.GEOSException:
        return 0.0


def _polygonal(geometry):
    """The region's area as a valid (multi)polygon: some polygons in the index
    cross themselves, and GEOS refuses to intersect those."""
    if geometry.is_empty:
        return None
    if not geometry.is_valid:
        geometry = shapely.make_valid(geometry)
    if geometry.geom_type == "GeometryCollection":
        parts = [g for g in geometry.geoms if g.geom_type in ("Polygon", "MultiPolygon")]
        geometry = shapely.union_all(parts) if parts else None
    if geometry is None or geometry.is_empty or geometry.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    return geometry


def _safe(rid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", rid)


def human_size(size: int | None) -> str:
    if not size:
        return ""
    return f"{size / 1e9:.1f} GB" if size >= 1e9 else f"{max(1, math.ceil(size / 1e6))} MB"
