"""Download the third-party front-end files that ship with the program.

Run from the repository root:  python tools/vendor_assets.py

Everything is pinned to exact versions and verified against the registry's
published integrity hash, then copied into src/offline_railway_map/web/vendor/.
The downloaded archives are only unpacked, never executed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src" / "offline_railway_map" / "web" / "vendor"
USER_AGENT = "OfflineRailwayMap-build-tools/1.0 (+https://github.com/Zhao-Yipeng/task1)"

MAPLIBRE_VERSION = "5.24.0"

# protomaps/basemaps-assets (fonts: SIL OFL; sprites), pinned to one commit and
# fetched through jsDelivr, whose listing provides a SHA-256 for every file.
ASSETS_SHA = "028c18f713baecad011301ff7a69acc39bcc2ae7"
ASSETS_LISTING = f"https://data.jsdelivr.com/v1/package/gh/protomaps/basemaps-assets@{ASSETS_SHA}/flat"
ASSETS_CDN = f"https://cdn.jsdelivr.net/gh/protomaps/basemaps-assets@{ASSETS_SHA}"
FONT_STACKS = ("Noto Sans Regular", "Noto Sans Medium", "Noto Sans Italic", "Noto Sans Devanagari Regular v1")
SPRITES = ("grayscale.json", "grayscale.png", "grayscale@2x.json", "grayscale@2x.png")


def _is_cjk_range(start: int) -> bool:
    """CJK ideographs and Hangul syllables are drawn with system fonts (SPEC §7.7)."""
    return 0x3400 <= start < 0xA000 or 0xAC00 <= start < 0xD800


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def npm_tarball(name: str, version: str) -> tarfile.TarFile:
    meta = json.loads(fetch(f"https://registry.npmjs.org/{name}/{version}"))
    dist = meta["dist"]
    data = fetch(dist["tarball"])
    algo, _, expected = dist["integrity"].partition("-")
    actual = base64.b64encode(hashlib.new(algo, data).digest()).decode("ascii")
    if actual != expected:
        raise SystemExit(f"integrity mismatch for {name}@{version}")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def extract(tar: tarfile.TarFile, members: dict[str, Path]) -> None:
    for member_name, target in members.items():
        member = tar.getmember(member_name)
        if not member.isfile():
            raise SystemExit(f"{member_name} is not a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(tar.extractfile(member).read())
        print(f"  {target.relative_to(ROOT)}")


def vendor_maplibre() -> None:
    print(f"maplibre-gl@{MAPLIBRE_VERSION}")
    out = VENDOR / "maplibre-gl"
    with npm_tarball("maplibre-gl", MAPLIBRE_VERSION) as tar:
        extract(
            tar,
            {
                "package/dist/maplibre-gl.js": out / "maplibre-gl.js",
                "package/dist/maplibre-gl.css": out / "maplibre-gl.css",
                "package/LICENSE.txt": out / "LICENSE.txt",
            },
        )
    (out / "VERSION").write_text(MAPLIBRE_VERSION + "\n", encoding="utf-8")


def _assets_listing() -> list[dict]:
    return json.loads(fetch(ASSETS_LISTING))["files"]


def _download_verified(entry: dict, target: Path) -> None:
    name = entry["name"].lstrip("/")
    data = fetch(f"{ASSETS_CDN}/{urllib.parse.quote(name)}")
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
    if digest != entry["hash"]:
        raise SystemExit(f"hash mismatch for {name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def vendor_fonts() -> None:
    print(f"basemaps-assets@{ASSETS_SHA[:12]} fonts")
    out = VENDOR / "fonts"
    jobs = []
    for entry in _assets_listing():
        parts = entry["name"].strip("/").split("/")
        if parts[:2] == ["fonts", "OFL.txt"]:
            jobs.append((entry, out / "OFL.txt"))
        elif len(parts) == 3 and parts[0] == "fonts" and parts[1] in FONT_STACKS and parts[2].endswith(".pbf"):
            if not _is_cjk_range(int(parts[2].split("-")[0])):
                jobs.append((entry, out / parts[1] / parts[2]))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda job: _download_verified(*job), jobs))
    (out / "SOURCE").write_text(f"protomaps/basemaps-assets@{ASSETS_SHA}\n", encoding="utf-8")
    print(f"  {len(jobs)} files -> {out.relative_to(ROOT)}")


def vendor_sprites() -> None:
    print(f"basemaps-assets@{ASSETS_SHA[:12]} sprites")
    out = VENDOR / "sprites" / "v4"
    wanted = {f"sprites/v4/{name}" for name in SPRITES}
    entries = [e for e in _assets_listing() if e["name"].strip("/") in wanted]
    if len(entries) != len(wanted):
        raise SystemExit("sprite files missing from listing")
    for entry in entries:
        _download_verified(entry, out / Path(entry["name"]).name)
        print(f"  {(out / Path(entry['name']).name).relative_to(ROOT)}")


def main(argv: list[str]) -> int:
    steps = {"maplibre": vendor_maplibre, "fonts": vendor_fonts, "sprites": vendor_sprites}
    wanted = argv or list(steps)
    for name in wanted:
        steps[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
