"""``OfflineRailwayMap.exe --self-check [report]`` (SPEC §17.1).

Checks, in the built program, a part that only runs when many cells are
downloaded: osmium writes a tiny extract, the railways are filtered out of it
and a cell is built from them.  Everything happens in a temporary folder that
is deleted afterwards; the exit code is 0 when it works.  CI runs this on the
PyInstaller build, where a missing library would otherwise go unnoticed.
"""

from __future__ import annotations

import tempfile
import traceback
from pathlib import Path


def run() -> str:
    import osmium
    from osmium.osm.mutable import Node, Way

    from .extracts import filter_railways, read_cell
    from .processing import build_cell

    with tempfile.TemporaryDirectory(prefix="orm-self-check-", ignore_cleanup_errors=True) as folder:
        tmp = Path(folder)
        pbf = tmp / "check.osm.pbf"
        with osmium.SimpleWriter(str(pbf)) as writer:
            writer.add_node(Node(id=1, location=(116.10, 39.70)))
            writer.add_node(Node(id=2, location=(116.30, 39.80)))
            writer.add_node(Node(id=3, location=(116.37, 39.86), tags={"railway": "station", "name": "北京南"}))
            writer.add_node(Node(id=4, location=(116.20, 39.75)))
            writer.add_way(Way(id=10, nodes=[1, 2], tags={"railway": "rail", "usage": "main", "maxspeed": "350"}))
            writer.add_way(Way(id=11, nodes=[1, 4], tags={"highway": "primary"}))
        meta = filter_railways(pbf, tmp / "check.railways.sqlite")
        elements = read_cell(tmp / "check.railways.sqlite", 210, 97)
        result = build_cell(elements, 210, 97, tmp / "rail.sqlite")
        if (meta["ways"], meta["stations"], result.lines) != ("1", "1", 1) or result.tiles < 1:
            raise AssertionError(f"unexpected result: {meta}, {result.lines} lines, {result.tiles} tiles")
        return f"ok: osmium filtered {meta['ways']} way and {meta['stations']} station; {result.tiles} tiles built"


def main(argv: list[str]) -> int:
    position = argv.index("--self-check")
    report = Path(argv[position + 1]) if len(argv) > position + 1 else None
    try:
        text, code = run(), 0
    except Exception:  # noqa: BLE001 - reported through the exit code and the report
        text, code = traceback.format_exc(), 1
    if report is not None:
        report.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return code
