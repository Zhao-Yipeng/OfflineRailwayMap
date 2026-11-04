"""SPEC §8 / appendix C (speed parsing) and §7.3 (classification)."""

from __future__ import annotations

import pytest

from offline_railway_map.classify import ALWAYS, classify, station_kind
from offline_railway_map.speed import color_speed, speed_color, speed_label

EN = "–"

# (tags, colour speed, label) - SPEC appendix C, mirroring ORM's SQL functions.
CASES = [
    ({"maxspeed": "120"}, 120, "120"),
    ({"maxspeed": "80.5"}, 81, "81"),
    ({"maxspeed": "75 mph"}, 121, "75 mph"),
    ({"maxspeed": "75mph"}, 121, "75 mph"),
    ({"maxspeed:forward": "120", "maxspeed:backward": "80"}, 120, "120/80"),
    ({"maxspeed:forward": "120", "maxspeed:backward": "80", "railway:preferred_direction": "forward"}, 120, "120 (80)"),
    # ORM swaps the fields twice for "backward", so the label still reads forward (backward).
    ({"maxspeed:forward": "120", "maxspeed:backward": "80", "railway:preferred_direction": "backward"}, 80, "120 (80)"),
    ({"maxspeed:forward": "120", "maxspeed:backward": "80", "railway:preferred_direction": "both"}, 120, "120/80"),
    ({"maxspeed:forward": "120", "maxspeed:backward": "80", "railway:preferred_direction": "oops"}, 120, "120"),
    ({"maxspeed:forward": "120"}, 120, f"120/{EN}"),
    ({"maxspeed:backward": "80"}, None, f"{EN}/80"),
    ({"maxspeed": "120", "maxspeed:forward": "100"}, None, None),
    ({"maxspeed": "signals"}, None, None),
    ({"maxspeed": "none"}, None, None),
    ({"maxspeed": "80;100"}, None, None),
    ({}, None, None),
]


@pytest.mark.parametrize("tags, colour, label", CASES)
def test_speed_rules(tags, colour, label):
    assert color_speed(tags) == colour
    assert speed_label(tags) == label


@pytest.mark.parametrize(
    "speed, color",
    [(None, "#808080"), (0, "#0100CB"), (10, "#0100CB"), (11, "#001ECB"), (120, "#00CB47"), (121, "#00CB29"),
     (300, "#CB0044"), (301, "#CB0062"), (350, "#CB009F"), (360, "#CB009F"), (361, "#CB00BD"), (420, "#CB00BD")],
)
def test_speed_colour_bands(speed, color):
    assert speed_color(speed) == color


def zoom_of(**tags):
    result = classify(tags)
    return None if result is None else result.minzoom


def test_lines_always_shown():
    assert zoom_of(railway="rail", usage="main") == ALWAYS
    assert zoom_of(railway="rail", usage="branch") == ALWAYS
    assert zoom_of(railway="rail", usage="tourism") == ALWAYS
    assert zoom_of(railway="construction", **{"construction:railway": "rail"}) == ALWAYS
    assert zoom_of(railway="construction", construction="rail", usage="main") == ALWAYS
    assert zoom_of(railway="proposed", **{"proposed:railway": "rail"}) == ALWAYS


def test_track_layout_follows_the_website():
    assert zoom_of(railway="rail", service="siding") == 11
    assert zoom_of(railway="rail", service="crossover") == 11
    assert zoom_of(railway="rail", service="spur") == 11
    assert zoom_of(railway="rail", service="yard") == 12
    assert zoom_of(railway="rail") == 13
    assert zoom_of(railway="rail", usage="main", service="siding") == 11


def test_other_types_follow_the_website():
    assert zoom_of(railway="rail", usage="industrial") == 10
    assert zoom_of(railway="rail", usage="industrial", service="siding") == 11
    assert zoom_of(railway="narrow_gauge") == 10
    assert zoom_of(railway="narrow_gauge", service="yard") == 12
    assert zoom_of(railway="subway") == 10
    assert zoom_of(railway="light_rail", service="siding") == 13
    assert zoom_of(railway="tram") == 11
    assert zoom_of(railway="construction", **{"construction:railway": "subway"}) == 10
    assert zoom_of(railway="construction", **{"construction:railway": "tram"}) == 11
    assert zoom_of(railway="construction") == 13
    assert zoom_of(railway="construction", construction="yes") == 13
    assert zoom_of(railway="disused", **{"disused:railway": "rail"}) == 9
    assert zoom_of(railway="disused", **{"disused:railway": "subway"}) == 11
    assert zoom_of(railway="disused", **{"disused:railway": "tram"}) == 12
    assert zoom_of(railway="disused") == 13
    assert zoom_of(railway="abandoned") is None
    assert zoom_of(railway="platform") is None


def test_labels_widths_and_ranks():
    main = classify({"railway": "rail", "usage": "main"})
    assert (main.labelzoom, main.rank, main.width, main.low_class) == (9, 1100, 3.5, "main")
    branch = classify({"railway": "rail", "usage": "branch"})
    assert (branch.rank, branch.low_class) == (1000, "line")
    siding = classify({"railway": "rail", "service": "siding"})
    assert (siding.labelzoom, siding.rank, siding.width) == (11, 870, 2.0)
    yard = classify({"railway": "rail", "service": "yard"})
    assert (yard.labelzoom, yard.width) == (12, 1.5)
    tram = classify({"railway": "tram"})
    assert (tram.labelzoom, tram.width) == (13, 3.0)
    building = classify({"railway": "construction", "construction:railway": "rail", "usage": "main"})
    assert (building.rank, building.width, building.low_class) == (400, 3.0, "main")
    planned = classify({"railway": "proposed", "proposed:railway": "rail"})
    assert (planned.rank, planned.state) == (390, "proposed")
    unknown = classify({"railway": "construction"})
    assert unknown.labelzoom is None


def test_station_kinds():
    assert station_kind({"railway": "station"}) == "station"
    assert station_kind({"railway": "station", "station": "subway"}) == "urban"
    assert station_kind({"railway": "halt"}) == "halt"
    assert station_kind({"railway": "platform"}) is None
