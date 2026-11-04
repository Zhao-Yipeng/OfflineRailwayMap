"""Which railway lines are drawn at which zoom, and how (SPEC §7.3–7.6, §8.1, appendix B)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

OPERATING_TYPES = {"rail", "narrow_gauge", "subway", "light_rail", "tram"}
ALWAYS = 0  # "始终": shown from the smallest zoom
URBAN = {"subway", "light_rail"}
OTHER_USAGES = {"tourism", "military", "test"}

STATE_CODES = {"operating": "o", "construction": "c", "proposed": "p", "disused": "d"}


@dataclass(frozen=True)
class LineClass:
    state: str  # operating | construction | proposed | disused
    kind: str | None  # rail, narrow_gauge, subway, light_rail, tram, ... or None (not stated)
    usage: str | None
    service: str | None
    minzoom: int
    labelzoom: int | None
    rank: int
    width: float  # line width at z >= 9 (px)
    low_class: str  # 'main' | 'line' | 'minor': widths below z9

    @property
    def always(self) -> bool:
        return self.minzoom == ALWAYS


def _value(tags: Mapping[str, str], key: str) -> str | None:
    value = tags.get(key)
    if value is None or value == "" or value == "yes":
        return None
    return value


def state_and_kind(tags: Mapping[str, str]) -> tuple[str, str | None] | None:
    railway = tags.get("railway")
    if railway in OPERATING_TYPES:
        return "operating", railway
    if railway == "construction":
        return "construction", _value(tags, "construction:railway") or _value(tags, "construction")
    if railway == "proposed":
        return "proposed", _value(tags, "proposed:railway") or _value(tags, "proposed")
    if railway == "disused":
        return "disused", _value(tags, "disused:railway")
    return None


def _usage_service(tags: Mapping[str, str], state: str) -> tuple[str | None, str | None]:
    usage, service = _value(tags, "usage"), _value(tags, "service")
    if state != "operating":
        usage = usage or _value(tags, f"{state}:usage")
        service = service or _value(tags, f"{state}:service")
    return usage, service


def _minzoom(state: str, kind: str | None, usage: str | None, service: str | None) -> int:
    """First matching row of the table in SPEC §7.3."""
    if state == "operating":
        if kind == "rail":
            if usage == "industrial":
                return 10 if service is None else 11
            if service in ("siding", "crossover", "spur"):
                return 11
            if service == "yard":
                return 12
            if service is None and usage in ("main", "branch"):
                return ALWAYS
            if service is None and usage in OTHER_USAGES:
                return ALWAYS
            return 13
        if kind == "narrow_gauge":
            if service is None:
                return 10
            if service in ("spur", "siding", "crossover"):
                return 11
            return 12 if service == "yard" else 13
        if kind in URBAN:
            return 10 if service is None else 13
        if kind == "tram":
            return 11 if service is None else 13
        return 13
    if state in ("construction", "proposed"):
        if service is None:
            if kind == "rail":
                return 10 if usage == "industrial" else ALWAYS
            if kind in URBAN:
                return 10
            if kind == "tram":
                return 11
        return 13
    # disused
    if service is None:
        if kind == "rail":
            return 9
        if kind in URBAN:
            return 11
        if kind == "tram":
            return 12
    return 13


def _labelzoom(kind: str | None, minzoom: int) -> int | None:
    base = {"rail": 9, "narrow_gauge": 11, "light_rail": 12, "subway": 12, "tram": 13}.get(kind or "")
    if base is None:
        return None
    return max(base, minzoom)


def _rank(state: str, kind: str | None, usage: str | None, service: str | None) -> int:
    if state == "construction":
        return 400
    if state == "proposed":
        return 390
    if state == "disused":
        return 300
    if kind == "rail":
        if usage == "industrial":
            return 850
        if service is None:
            return {"main": 1100, "branch": 1000}.get(usage or "", 400)
        if usage is None:
            return {"spur": 880, "siding": 870, "yard": 860, "crossover": 300}.get(service, 50)
    return 50


def _width(state: str, kind: str | None, usage: str | None, service: str | None, always: bool) -> float:
    """Width at z >= 9, from OpenRailwayMap-CartoCSS common.mss (SPEC appendix B)."""
    if always:
        return 3.0 if state in ("construction", "proposed") else 3.5
    if state == "disused":
        return 1.5 if service else 3.0
    if kind in URBAN or kind == "tram":
        return 1.5 if service else 3.0
    if kind == "narrow_gauge":
        return 2.0 if (usage == "industrial" or service) else 3.0
    if usage == "industrial":
        return 1.5 if service else 2.0
    if service in ("yard", "spur"):
        return 1.5
    return 2.0


def classify(tags: Mapping[str, str]) -> LineClass | None:
    """Classify an OSM railway way; None if it is never drawn."""
    found = state_and_kind(tags)
    if found is None:
        return None
    state, kind = found
    usage, service = _usage_service(tags, state)
    minzoom = _minzoom(state, kind, usage, service)
    always = minzoom == ALWAYS
    if always:
        low_class = "main" if usage == "main" else "line"
    else:
        low_class = "minor"
    return LineClass(
        state=state,
        kind=kind,
        usage=usage,
        service=service,
        minzoom=minzoom,
        labelzoom=_labelzoom(kind, minzoom),
        rank=_rank(state, kind, usage, service),
        width=_width(state, kind, usage, service, always),
        low_class=low_class,
    )


STATION_KINDS = {"station": 9, "halt": 12, "urban": 13}


def station_kind(tags: Mapping[str, str]) -> str | None:
    """'station', 'halt' or 'urban' (subway / light rail / monorail) - SPEC §7.6."""
    railway = tags.get("railway")
    if railway == "halt":
        return "halt"
    if railway == "station":
        return "urban" if tags.get("station") in ("subway", "light_rail", "monorail") else "station"
    return None
