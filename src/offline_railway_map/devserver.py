"""Run the backend without a window (``python -m offline_railway_map --serve``).

Prints the page URL; open it in a browser.  Used for development and for the
end-to-end tests, which drive the page with Playwright.
"""

from __future__ import annotations

import threading

from . import logs
from .core import AppCore
from .paths import Home, program_dir
from .settings import Settings


def serve(port: int = 0) -> int:
    home = Home(program_dir())
    logs.setup(home)
    core = AppCore(home, Settings.load(home), port=port)
    url = core.start()
    print(url, flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()
    return 0
