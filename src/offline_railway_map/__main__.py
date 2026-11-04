"""Entry point: ``python -m offline_railway_map``.

Without arguments the desktop window starts.  ``--serve`` runs only the local
server so the page can be opened in a normal browser (development and tests).
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="offline_railway_map")
    parser.add_argument("--serve", action="store_true", help="run only the local server (development)")
    parser.add_argument("--port", type=int, default=0, help="port for --serve (default: random)")
    args, rest = parser.parse_known_args()
    if args.serve:
        from .devserver import serve

        return serve(args.port)
    from .app import main as gui_main

    return gui_main([sys.argv[0], *rest])


if __name__ == "__main__":
    sys.exit(main())
