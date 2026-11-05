"""Speed parsing, ported from OpenRailwayMap-CartoCSS ``sql/functions.sql`` (SPEC §8).

The functions mirror ORM's SQL one to one so that cached areas show the same
colours and numbers as the website:

* ``color_speed``   <- railway_dominant_speed (km/h, mph converted)
* ``speed_label``   <- railway_direction_speed_limit + railway_speed_label

A tag counts as present when the key exists, whatever its value, exactly as
``IS NOT NULL`` does in the original.
"""

from __future__ import annotations

import math
import re
from typing import Mapping

_NUMBER = re.compile(r"^([0-9]+(?:\.[0-9]+)?)$")
_MPH = re.compile(r"^([0-9]+(?:\.[0-9]+)?) ?mph$")
MPH_TO_KMH = 1.609344
DASH = "–"  # null_to_dash() in ORM


def _round(value: float) -> int:
    """Round half away from zero (PostgreSQL NUMERIC -> INTEGER)."""
    return int(math.floor(value + 0.5))


def speed_int(value: str | None) -> int | None:
    """railway_speed_int: km/h, mph converted; None if not a plain speed."""
    if value is None:
        return None
    if _NUMBER.match(value):
        return _round(float(value))
    match = _MPH.match(value)
    if match:
        return _round(float(match.group(1)) * MPH_TO_KMH)
    return None


def speed_int_noconvert(value: str | None) -> int | None:
    """railway_speed_int_noconvert: the number as tagged, unit kept."""
    if value is None:
        return None
    if _NUMBER.match(value):
        return _round(float(value))
    match = _MPH.match(value)
    if match:
        return _round(float(match.group(1)))
    return None


def speed_imperial(value: str | None) -> int:
    return 1 if value is not None and _MPH.match(value) else 0


def _tags(tags: Mapping[str, str]):
    return (
        tags.get("railway:preferred_direction"),
        tags.get("maxspeed"),
        tags.get("maxspeed:forward"),
        tags.get("maxspeed:backward"),
    )


def color_speed(tags: Mapping[str, str]) -> int | None:
    """railway_dominant_speed: the speed (km/h) used for the line colour."""
    preferred, speed, forward, backward = _tags(tags)
    if speed is not None and (forward is not None or backward is not None):
        return None
    if speed is not None:
        return speed_int(speed)
    if preferred == "forward":
        return speed_int(forward) if speed_int(forward) is not None else speed_int(speed)
    if preferred == "backward":
        return speed_int(backward) if speed_int(backward) is not None else speed_int(speed)
    return speed_int(forward)


def direction_speed_limit(tags: Mapping[str, str]) -> tuple[int | None, int | None, int, int, int]:
    """railway_direction_speed_limit -> (forward, backward, primary_direction, forward_unit, backward_unit).

    primary_direction: 1 line direction, 2 opposite direction, 3 none,
    4 same speed both ways, 6 contradicting tags, 7 no speed information.
    """
    preferred, speed, forward, backward = _tags(tags)
    nc, imp = speed_int_noconvert, speed_imperial
    if speed is None and forward is None and backward is None:
        return None, None, 7, 0, 0
    if speed is not None and forward is None and backward is None:
        return nc(speed), nc(speed), 4, imp(speed), imp(speed)
    if speed is not None:
        return nc(speed), nc(speed), 6, imp(speed), imp(speed)
    if preferred == "forward":
        return nc(forward), nc(backward), 1, imp(forward), imp(backward)
    if preferred == "backward":
        return nc(backward), nc(forward), 2, imp(backward), imp(forward)
    if preferred == "both" or preferred is None:
        return nc(forward), nc(backward), 3, imp(forward), imp(backward)
    return nc(forward), nc(backward), 4, imp(forward), imp(backward)


def _with_unit(value: int | None, imperial: int) -> str | None:
    if value is None:
        return None
    return f"{value} mph" if imperial == 1 else str(value)


def _dash(value: str | None) -> str:
    return value if value else DASH


def speed_label(tags: Mapping[str, str]) -> str | None:
    """railway_speed_label(railway_direction_speed_limit(...)): the number drawn on the line."""
    forward, backward, primary, forward_unit, backward_unit = direction_speed_limit(tags)
    fwd = _with_unit(forward, forward_unit)
    bwd = _with_unit(backward, backward_unit)
    if primary == 7:
        return None
    if primary == 4:
        return fwd
    if primary == 3:
        return f"{_dash(fwd)}/{_dash(bwd)}"
    if primary == 2:
        return f"{_dash(bwd)} ({_dash(fwd)})"
    if primary == 1:
        return f"{_dash(fwd)} ({_dash(bwd)})"
    return None


# Colour bands from OpenRailwayMap-CartoCSS maxspeed.mss (SPEC appendix A).
SPEED_COLORS = (
    (10, "#0100CB"), (20, "#001ECB"), (30, "#003DCB"), (40, "#005BCB"), (50, "#007ACB"),
    (60, "#0098CB"), (70, "#00B7CB"), (80, "#00CBC1"), (90, "#00CBA2"), (100, "#00CB84"),
    (110, "#00CB66"), (120, "#00CB47"), (130, "#00CB29"), (140, "#00CB0A"), (150, "#14CB00"),
    (160, "#33CB00"), (170, "#51CB00"), (180, "#70CB00"), (190, "#8ECB00"), (200, "#ADCB00"),
    (210, "#CBCB00"), (220, "#CBAD00"), (230, "#CB8E00"), (240, "#CB7000"), (250, "#CB5100"),
    (260, "#CB3300"), (270, "#CB1400"), (280, "#CB0007"), (290, "#CB0025"), (300, "#CB0044"),
    (320, "#CB0062"), (340, "#CB0081"), (360, "#CB009F"),
)
OVER_360_COLOR = "#CB00BD"
UNKNOWN_COLOR = "#808080"


def speed_color(speed: int | None) -> str:
    if speed is None:
        return UNKNOWN_COLOR
    for upper, color in SPEED_COLORS:
        if speed <= upper:
            return color
    return OVER_360_COLOR
