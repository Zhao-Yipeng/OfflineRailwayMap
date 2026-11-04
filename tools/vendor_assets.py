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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / "src" / "offline_railway_map" / "web" / "vendor"
USER_AGENT = "OfflineRailwayMap-build-tools/1.0 (+https://github.com/Zhao-Yipeng/task1)"

MAPLIBRE_VERSION = "5.24.0"


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


def main(argv: list[str]) -> int:
    steps = {"maplibre": vendor_maplibre}
    wanted = argv or list(steps)
    for name in wanted:
        steps[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
