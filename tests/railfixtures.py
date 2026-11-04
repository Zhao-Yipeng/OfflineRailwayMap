"""Synthetic Overpass data around cell 210-97 (lon 115.31–116.72, lat 38.82–39.91)."""

from __future__ import annotations

import json

CELL = (210, 97)


def way(way_id: int, coords, **tags) -> dict:
    return {
        "type": "way",
        "id": way_id,
        "tags": tags,
        "geometry": [{"lon": lon, "lat": lat} for lon, lat in coords],
    }


def node(node_id: int, lon: float, lat: float, **tags) -> dict:
    return {"type": "node", "id": node_id, "lon": lon, "lat": lat, "tags": tags}


def elements() -> list[dict]:
    return [
        # high-speed main line entering the cell from the west
        way(1, [(115.0, 39.0), (116.0, 39.5)], railway="rail", usage="main", maxspeed="350", name="京沪高铁", highspeed="yes"),
        # branch line with directional speeds
        way(2, [(116.1, 39.1), (116.3, 39.2)], railway="rail", usage="branch", **{"maxspeed:forward": "120", "maxspeed:backward": "80"}),
        # siding: only from z11
        way(3, [(116.0, 39.3), (116.02, 39.31)], railway="rail", service="siding", maxspeed="40"),
        # proposed line: always, dashed
        way(4, [(115.5, 39.6), (115.9, 39.7)], railway="proposed", **{"proposed:railway": "rail"}, maxspeed="250"),
        # under construction without speed: always, grey
        way(5, [(115.6, 39.0), (115.8, 39.05)], railway="construction", **{"construction:railway": "rail"}, usage="main"),
        # subway: from z10
        way(6, [(116.5, 39.8), (116.55, 39.82)], railway="subway", name="地铁"),
        # not a railway line we draw
        way(7, [(116.2, 39.2), (116.21, 39.21)], railway="platform"),
        node(100, 116.0, 39.5, railway="station", name="测试站"),
        node(101, 116.05, 39.45, railway="halt", name="小站"),
        node(102, 117.5, 39.0, railway="station", name="界外站"),
        {"type": "way", "id": 103, "center": {"lon": 116.3, "lat": 39.2}, "tags": {"railway": "station", "name": "面状车站"}},
    ]


def overpass_json() -> bytes:
    return json.dumps({"version": 0.6, "elements": elements()}).encode()
