"""Which map tiles are covered by cached cells (SPEC §5.2, §7.8).

A cell is a z8 Web Mercator tile.  For a tile at zoom z:
* z >= 8: the tile lies inside exactly one cell;
* z < 8:  the tile contains 2^(8-z) x 2^(8-z) cells, whose borders fall on
  whole pixels of the tile.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

CELL_ZOOM = 8


@dataclass(frozen=True)
class TileMask:
    """``full`` -> the whole tile is cached; otherwise ``rects`` lists cached
    areas as (left, top, right, bottom) fractions of the tile size."""

    full: bool
    rects: tuple[tuple[float, float, float, float], ...] = ()


class CellCoverage:
    def __init__(self, cells: set[tuple[int, int]] | None = None) -> None:
        self._cells: frozenset[tuple[int, int]] = frozenset(cells or ())
        self._lock = threading.Lock()
        self.version = 0

    def replace(self, cells: set[tuple[int, int]]) -> None:
        with self._lock:
            self._cells = frozenset(cells)
            self.version += 1

    def cells(self) -> frozenset[tuple[int, int]]:
        return self._cells

    def is_cached(self, x: int, y: int) -> bool:
        return (x, y) in self._cells

    def tile_mask(self, z: int, x: int, y: int) -> TileMask | None:
        """None when no cached cell touches the tile."""
        cells = self._cells
        if not cells:
            return None
        if z >= CELL_ZOOM:
            shift = z - CELL_ZOOM
            return TileMask(True) if (x >> shift, y >> shift) in cells else None
        n = 1 << (CELL_ZOOM - z)
        x0, y0 = x * n, y * n
        rects = []
        if n * n > len(cells):
            hits = [(cx - x0, cy - y0) for cx, cy in cells if x0 <= cx < x0 + n and y0 <= cy < y0 + n]
        else:
            hits = [(i, j) for i in range(n) for j in range(n) if (x0 + i, y0 + j) in cells]
        if not hits:
            return None
        if len(hits) == n * n:
            return TileMask(True)
        for i, j in hits:
            rects.append((i / n, j / n, (i + 1) / n, (j + 1) / n))
        return TileMask(False, tuple(rects))
