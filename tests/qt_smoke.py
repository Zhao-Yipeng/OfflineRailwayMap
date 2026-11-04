"""Start the real window off-screen, load the page, report, quit.

    QT_QPA_PLATFORM=offscreen python tests/qt_smoke.py

Checks that the shell starts, the page loads through the local server, the
request interceptor blocks outside requests, and that the program folder is
still empty afterwards.  Not part of the pytest run (needs Qt WebEngine).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from offline_railway_map import app as shell  # noqa: E402
from offline_railway_map.paths import Home  # noqa: E402


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="orm-qt-"))
    home = Home(root)
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    qt_app = QApplication(sys.argv)
    core, window, _ = shell.build(qt_app, home)
    result: dict[str, object] = {}

    def on_loaded(ok: bool) -> None:
        result["loaded"] = ok
        window.page.runJavaScript(
            "fetch('https://example.com/').then(() => 'reached', () => 'blocked')",
            0,
            lambda value: None,
        )
        QTimer.singleShot(3000, check)

    def check() -> None:
        def got(value) -> None:
            result["ready"] = value
            qt_app.quit()

        window.page.runJavaScript(
            "JSON.stringify({ready: document.body.dataset.mapReady || null, "
            "connected: document.body.dataset.connected || null, "
            "fatal: !!document.querySelector('.fatal')})",
            0,
            got,
        )

    window.view.loadFinished.connect(on_loaded)
    window.show()
    QTimer.singleShot(30000, qt_app.quit)
    qt_app.exec()
    core.shutdown()
    window.close()
    blocked = window.interceptor.blocked
    leftovers = sorted(p.name for p in root.iterdir())
    print("page loaded:", result.get("loaded"))
    print("page state:", result.get("ready"))
    print("blocked outside requests:", blocked)
    print("program folder contents:", leftovers)
    ok = result.get("loaded") is True and "https://example.com/" in blocked and not leftovers
    print("QT SMOKE", "OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.exit(main())
