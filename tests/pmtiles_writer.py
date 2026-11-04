"""Minimal PMTiles v3 writer for tests (optionally with leaf directories)."""

from __future__ import annotations

import gzip
import struct

from offline_railway_map.pmtiles import zxy_to_id


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """entries: (tile_id, offset, length, run_length), sorted by tile id."""
    buf = bytearray(_varint(len(entries)))
    last = 0
    for tile_id, *_ in entries:
        buf += _varint(tile_id - last)
        last = tile_id
    for *_, run in entries:
        buf += _varint(run)
    for _, _, length, _ in entries:
        buf += _varint(length)
    for _, offset, _, _ in entries:
        buf += _varint(offset + 1)
    return gzip.compress(bytes(buf))


def write_archive(tiles: dict[tuple[int, int, int], bytes], leaf_size: int | None = None, max_zoom: int = 15) -> bytes:
    by_id = sorted((zxy_to_id(*zxy), data) for zxy, data in tiles.items())
    data = bytearray()
    entries = []
    for tile_id, blob in by_id:
        entries.append((tile_id, len(data), len(blob), 1))
        data += blob
    leaves = bytearray()
    if leaf_size:
        root_entries = []
        for i in range(0, len(entries), leaf_size):
            chunk = entries[i:i + leaf_size]
            raw = _directory(chunk)
            root_entries.append((chunk[0][0], len(leaves), len(raw), 0))
            leaves += raw
        root = _directory(root_entries)
    else:
        root = _directory(entries)
    root_offset = 127
    metadata_offset = root_offset + len(root)
    leaf_offset = metadata_offset
    data_offset = leaf_offset + len(leaves)
    header = b"PMTiles" + bytes([3]) + struct.pack(
        "<11Q",
        root_offset, len(root), metadata_offset, 0, leaf_offset, len(leaves), data_offset, len(data),
        len(tiles), len(tiles), len(tiles),
    )
    header += bytes([1, 2, 2, 1, 0, max_zoom]) + struct.pack("<iiii", 0, 0, 0, 0) + bytes([0]) + struct.pack("<ii", 0, 0)
    assert len(header) == 127
    return header + root + bytes(leaves) + bytes(data)


def range_route(blob: bytes):
    """FakeServer route serving ``blob`` with HTTP Range support."""

    def route(rec):
        header = rec.headers.get("Range")
        if not header:
            return 200, {"Content-Type": "application/octet-stream"}, blob
        start, end = (int(v) for v in header.split("=")[1].split("-"))
        return 206, {"Content-Range": f"bytes {start}-{end}/{len(blob)}"}, blob[start:end + 1]

    return route
