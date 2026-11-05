"""Check the regional extracts against the real Geofabrik server (CI, informational).

1. reads Geofabrik's index and prints which regions the program would download
   for some areas (and whether border slivers or groups like "dach" sneak in);
2. downloads one small extract (Liechtenstein, about 3 MB, crossed by the
   Feldkirch–Buchs railway) the way the program does, filters it and builds
   the cell from it.

    python tools/check_extracts.py [index-url]

Only a handful of HEAD requests and one small download: well within
Geofabrik's terms (https://download.geofabrik.de/technical.html).
"""

from __future__ import annotations

import collections
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

from offline_railway_map import USER_AGENT
from offline_railway_map.extracts import INDEX_URL, ExtractStore, plan
from offline_railway_map.grid import CELL_ZOOM, cell_bounds, lonlat_to_tile
from offline_railway_map.paths import Home
from offline_railway_map.processing import build_cell

AREAS = {
    "北京周边": (115.6, 39.2, 117.6, 40.8),
    "长三角": (118.5, 30.0, 122.0, 32.4),
    "珠三角": (112.6, 21.8, 114.8, 23.8),
    "全国（中国）": (73.5, 18.2, 134.8, 53.5),
    "德国南部（dach/alps 重叠）": (7.5, 47.3, 13.5, 49.8),
    "美国东北（us-* 重叠）": (-77.0, 39.0, -70.0, 43.0),
    "日本关东": (138.5, 34.8, 140.9, 37.0),
}


def cells_in(west: float, south: float, east: float, north: float) -> list[tuple[int, int]]:
    x0, y0 = (int(v) for v in lonlat_to_tile(west, north, CELL_ZOOM))
    x1, y1 = (int(v) for v in lonlat_to_tile(east, south, CELL_ZOOM))
    return [(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]


def check_area(index, name: str, bbox, size_of) -> None:
    cells = cells_in(*bbox)
    started = time.time()
    chosen = collections.Counter()
    for x, y in cells:
        for region in index.most_specific(x, y):
            chosen[region.id] += 1
    # Regions picked next to their own parts (a parent for a border sliver)
    nested = sorted(
        rid for rid in chosen
        if any(index.regions[k].parent == rid for k in chosen) and rid in index.regions
    )
    spent = time.time() - started
    print(f"\n== {name}: {len(cells)} cells, choice {spent:.1f} s")
    print("  per cell:", ", ".join(f"{rid}×{n}" for rid, n in chosen.most_common(14)))
    if nested:
        print("  !! parents next to their parts:", ", ".join(nested))
    if len(cells) <= 200:
        result = plan(index, cells, size_of)
        used = collections.Counter(r for regions in result.values() for r in regions)
        total = sum(size_of(index.regions[r]) or 0 for r in used)
        print(f"  plan: {len(result)} of {len(cells)} cells from extracts, {total / 1e6:.0f} MB:",
              ", ".join(f"{r} ({(size_of(index.regions[r]) or 0) / 1e6:.0f} MB, {n} cells)"
                        for r, n in used.most_common()))


def main(index_url: str = INDEX_URL) -> int:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    with tempfile.TemporaryDirectory(prefix="orm-extracts-", ignore_cleanup_errors=True) as folder:
        store = ExtractStore(Home(Path(folder)), session, index_url=index_url)
        started = time.time()
        index = store.index()
        print(f"index: {len(index.regions)} regions ({time.time() - started:.1f} s)")
        china = index.regions.get("china")
        if china is not None:
            print("china's parts:", ", ".join(sorted(k.id for k in china.children)) or "(none)")
        sizes = {}

        def size_of(region):
            if region.id not in sizes:
                sizes[region.id] = store.size_of(region)
            return sizes[region.id]

        problems = 0
        for name, bbox in AREAS.items():
            try:
                check_area(index, name, bbox, size_of)
            except Exception as exc:  # noqa: BLE001 - report and go on with the next area
                problems += 1
                print(f"\n!! {name}: {type(exc).__name__}: {exc}")

        # A real download, filtered and built like the program does it.
        x, y = (int(v) for v in lonlat_to_tile(9.52, 47.17, CELL_ZOOM))  # Schaan, Liechtenstein
        regions = [r.id for r in index.most_specific(x, y)]
        print(f"\ncell {x}-{y} {cell_bounds(x, y)}: regions {regions}")
        if "liechtenstein" not in regions:
            print("!! liechtenstein not chosen for its own cell")
            return 1
        started = time.time()
        elements = store.cell_elements(
            x, y, ["liechtenstein"], threading.Event(), lambda *a, **k: None, lambda n: None
        )
        ways = sum(1 for e in elements if e["type"] == "way" and "coords" in e)
        print(f"liechtenstein: {ways} railway ways, {len(elements) - ways} stations in the cell "
              f"({time.time() - started:.1f} s with download and filtering)")
        result = build_cell(elements, x, y, Path(folder) / "rail.sqlite")
        names = sorted({s[3] for s in result.stations if s[3]})[:10]
        print(f"built: {result.lines} lines, {result.tiles} tiles; stations e.g. {names}")
        if not ways or not result.tiles:
            return 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
