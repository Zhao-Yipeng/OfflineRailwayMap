"""Reading parts of a remote PMTiles v3 archive with HTTP range requests.

Spec: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md

Tile ids follow a Hilbert curve per zoom level, so all descendants of a z8
tile at zoom z form one contiguous id range; a cell's tiles can therefore be
located with a few directory reads and fetched with a few large ranges.
"""

from __future__ import annotations

import gzip
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

import requests

HEADER_SIZE = 127
COMPRESSION_NONE, COMPRESSION_GZIP = 1, 2
TILE_TYPE_MVT = 1
MERGE_GAP = 64 * 1024
MAX_RANGE = 16 * 1024 * 1024
RETRY_DELAYS = (5.0, 15.0, 60.0)
# Range requests in flight at once, over all cells being downloaded: the same
# as the official `pmtiles extract` default (--download-threads=4).
RANGE_REQUESTS_AT_ONCE = 4
_range_slots = threading.BoundedSemaphore(RANGE_REQUESTS_AT_ONCE)


class PMTilesError(Exception):
    pass


class ArchiveGone(PMTilesError):
    """The archive URL no longer exists (daily builds rotate)."""


class Cancelled(Exception):
    pass


# -- tile ids -------------------------------------------------------------------------------


def _rotate(n: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def zoom_base(z: int) -> int:
    """Number of tiles in all zoom levels below z."""
    return ((1 << (2 * z)) - 1) // 3


def zxy_to_id(z: int, x: int, y: int) -> int:
    n = 1 << z
    d, s = 0, n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        x, y = _rotate(n, x, y, rx, ry)
        s >>= 1
    return zoom_base(z) + d


def id_to_zxy(tile_id: int) -> tuple[int, int, int]:
    z = 0
    while zoom_base(z + 1) <= tile_id:
        z += 1
    d = tile_id - zoom_base(z)
    n = 1 << z
    x = y = 0
    s = 1
    t = d
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        x, y = _rotate(s, x, y, rx, ry)
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return z, x, y


def descendant_range(z_parent: int, x: int, y: int, z: int) -> tuple[int, int]:
    """[start, end) of the tile ids of all descendants of a tile at zoom z."""
    k = z - z_parent
    block = 4**k
    corner = zxy_to_id(z, x << k, y << k) - zoom_base(z)
    start = zoom_base(z) + (corner // block) * block
    return start, start + block


# -- directories ----------------------------------------------------------------------------


def _varints(buf: bytes, pos: int, count: int) -> tuple[list[int], int]:
    out = []
    for _ in range(count):
        value = shift = 0
        while True:
            byte = buf[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                break
        out.append(value)
    return out, pos


@dataclass(frozen=True)
class Entry:
    tile_id: int
    offset: int
    length: int
    run_length: int  # 0 -> leaf directory


def parse_directory(raw: bytes, compression: int) -> list[Entry]:
    buf = gzip.decompress(raw) if compression == COMPRESSION_GZIP else raw
    (count,), pos = _varints(buf, 0, 1)
    ids, pos = _varints(buf, pos, count)
    runs, pos = _varints(buf, pos, count)
    lengths, pos = _varints(buf, pos, count)
    offsets, pos = _varints(buf, pos, count)
    entries, tile_id = [], 0
    for i in range(count):
        tile_id += ids[i]
        if i > 0 and offsets[i] == 0:
            offset = entries[-1].offset + entries[-1].length
        else:
            offset = offsets[i] - 1
        entries.append(Entry(tile_id, offset, lengths[i], runs[i]))
    return entries


@dataclass(frozen=True)
class Header:
    root_offset: int
    root_length: int
    leaf_offset: int
    data_offset: int
    internal_compression: int
    tile_compression: int
    tile_type: int
    min_zoom: int
    max_zoom: int


def parse_header(data: bytes) -> Header:
    if len(data) < HEADER_SIZE or data[:7] != b"PMTiles" or data[7] != 3:
        raise PMTilesError("not a PMTiles v3 archive")
    (root_off, root_len, _meta_off, _meta_len, leaf_off, _leaf_len, data_off, _data_len,
     _addressed, _entries, _contents) = struct.unpack_from("<11Q", data, 8)
    return Header(root_off, root_len, leaf_off, data_off, data[97], data[98], data[99], data[100], data[101])


# -- remote archive ---------------------------------------------------------------------------


class RemoteArchive:
    def __init__(self, session: requests.Session, url: str, retry_delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        self.session = session
        self.url = url
        self.retry_delays = retry_delays
        self._leaves: OrderedDict[tuple[int, int], list[Entry]] = OrderedDict()
        self._lock = threading.Lock()
        self.header = parse_header(self.read(0, HEADER_SIZE))
        if self.header.internal_compression not in (COMPRESSION_NONE, COMPRESSION_GZIP):
            raise PMTilesError("unsupported directory compression")
        self.root = parse_directory(self.read(self.header.root_offset, self.header.root_length),
                                    self.header.internal_compression)

    def read(self, offset: int, length: int, cancel: threading.Event | None = None) -> bytes:
        headers = {"Range": f"bytes={offset}-{offset + length - 1}"}
        for attempt in range(len(self.retry_delays) + 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                response = self.session.get(self.url, headers=headers, timeout=(20, 120))
                if response.status_code == 404:
                    raise ArchiveGone(self.url)
                if response.status_code == 206 or (response.status_code == 200 and len(response.content) == length):
                    if len(response.content) != length:
                        raise PMTilesError("short range response")
                    return response.content
                problem: Exception = PMTilesError(f"HTTP {response.status_code}")
            except (requests.RequestException, PMTilesError) as exc:
                if isinstance(exc, ArchiveGone):
                    raise
                problem = exc
            if attempt < len(self.retry_delays):
                delay = self.retry_delays[attempt]
                if cancel is not None:
                    if cancel.wait(delay):
                        raise Cancelled()
                else:
                    time.sleep(delay)
        raise PMTilesError(f"range request failed: {problem}")

    def _leaf(self, entry: Entry, cancel) -> list[Entry]:
        key = (entry.offset, entry.length)
        with self._lock:
            if key in self._leaves:
                self._leaves.move_to_end(key)
                return self._leaves[key]
        raw = self.read(self.header.leaf_offset + entry.offset, entry.length, cancel)
        leaf = parse_directory(raw, self.header.internal_compression)
        with self._lock:
            self._leaves[key] = leaf
            while len(self._leaves) > 512:
                self._leaves.popitem(last=False)
        return leaf

    def entries(self, lo: int, hi: int, cancel: threading.Event | None = None,
                directory: list[Entry] | None = None) -> list[Entry]:
        """Tile entries overlapping the tile ids [lo, hi)."""
        directory = self.root if directory is None else directory
        found = []
        for i, entry in enumerate(directory):
            nxt = directory[i + 1].tile_id if i + 1 < len(directory) else None
            if entry.run_length == 0:
                if entry.tile_id < hi and (nxt is None or nxt > lo):
                    found.extend(self.entries(lo, hi, cancel, self._leaf(entry, cancel)))
            elif entry.tile_id < hi and entry.tile_id + entry.run_length > lo:
                found.append(entry)
        return found

    def fetch_ranges(
        self,
        pieces: list[tuple[int, int]],
        cancel: threading.Event | None,
        progress: Callable[[int], None] = lambda _n: None,
        workers: int = RANGE_REQUESTS_AT_ONCE,
    ) -> dict[tuple[int, int], bytes]:
        """Download tile data for (offset, length) pieces, merging neighbours."""
        unique = sorted(set(pieces))
        groups: list[list[tuple[int, int]]] = []
        for offset, length in unique:
            if groups:
                g_start = groups[-1][0][0]
                g_end = groups[-1][-1][0] + groups[-1][-1][1]
                if offset - g_end <= MERGE_GAP and offset + length - g_start <= MAX_RANGE:
                    groups[-1].append((offset, length))
                    continue
            groups.append([(offset, length)])
        result: dict[tuple[int, int], bytes] = {}
        lock = threading.Lock()
        errors: list[BaseException] = []

        def work(group):
            start = group[0][0]
            end = max(o + n for o, n in group)
            with _range_slots:  # shared by all cells downloading at the same time
                blob = self.read(self.header.data_offset + start, end - start, cancel)
            with lock:
                for o, n in group:
                    result[(o, n)] = blob[o - start:o - start + n]
            progress(end - start)

        threads = []
        queue = list(groups)
        queue_lock = threading.Lock()

        def runner():
            while True:
                with queue_lock:
                    if not queue or errors:
                        return
                    group = queue.pop(0)
                try:
                    work(group)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)
                    return

        for _ in range(max(1, min(workers, len(groups)))):
            t = threading.Thread(target=runner, daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        return result
