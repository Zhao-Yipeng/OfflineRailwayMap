"""Window shell: a Qt window with one web view that shows the local page.

SPEC §10.6 / §11.2:
* the web view uses an off-the-record profile (memory cache only, no cookies);
* a request interceptor blocks every request that does not go to the local
  server, so all network access goes through Python;
* Chromium runs with the GPU shader disk cache and crash dumps turned off.
"""

from __future__ import annotations

import logging
import os
import sys

CHROMIUM_FLAGS = ("--disable-gpu-shader-disk-cache", "--disable-breakpad")


def _set_chromium_flags() -> None:
    existing = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    flags = existing + [f for f in CHROMIUM_FLAGS if f not in existing]
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    # Qt's own shader / graphics pipeline disk cache would go to
    # %LOCALAPPDATA%\OfflineRailwayMap\cache (SPEC §11.2).
    os.environ["QT_DISABLE_SHADER_DISK_CACHE"] = "1"


# Chromium reads its flags when Qt WebEngine starts, so set them before any Qt import.
_set_chromium_flags()

from PySide6.QtCore import QByteArray, QObject, Qt, QUrl, Signal  # noqa: E402
from PySide6.QtGui import QDesktopServices  # noqa: E402
from PySide6.QtWebEngineCore import (  # noqa: E402
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox  # noqa: E402

from . import APP_NAME, cleanup, logs, single_instance  # noqa: E402
from .core import AppCore, ShellHooks  # noqa: E402
from .paths import Home, program_dir  # noqa: E402
from .settings import Settings  # noqa: E402

log = logging.getLogger(__name__)

NOT_WRITABLE = "程序文件夹不可写。\n\n请把程序文件夹放到可写的位置，例如 D:\\Tools\\OfflineRailwayMap，然后重新打开。"
NO_WEBGL = "显卡不支持 WebGL 2，地图无法显示。\n\n请更新显卡驱动后重试。"


class Bridge(QObject):
    """Carries backend requests (made on server threads) to the GUI thread."""

    open_external = Signal(str)
    fatal = Signal(str)


class QtHooks(ShellHooks):
    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge

    def open_external(self, url: str) -> None:
        self.bridge.open_external.emit(url)

    def fatal(self, reason: str) -> None:
        self.bridge.fatal.emit(reason)


class LocalOnlyInterceptor(QWebEngineUrlRequestInterceptor):
    """Blocks every page request that does not go to the local server."""

    def __init__(self, origins: list[str], parent: QObject) -> None:
        super().__init__(parent)
        self.prefixes = tuple(origin + "/" for origin in origins)
        self.blocked: list[str] = []

    def interceptRequest(self, info) -> None:  # noqa: N802 - Qt API
        url = info.requestUrl()
        if url.scheme() in ("qrc", "data", "blob", "about"):
            return
        text = url.toString()
        if text.startswith(self.prefixes):
            return
        self.blocked.append(text)
        log.warning("blocked page request to %s", text)
        info.block(True)


class Page(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, parent: QObject, prefix: str) -> None:
        super().__init__(profile, parent)
        self.prefix = prefix

    def acceptNavigationRequest(self, url, nav_type, is_main_frame) -> bool:  # noqa: N802 - Qt API
        if url.toString().startswith(self.prefix):
            return True
        if url.scheme() in ("http", "https"):
            QDesktopServices.openUrl(url)
        return False

    def createWindow(self, _type):  # noqa: N802 - Qt API
        return None  # no pop-ups; external links go to the system browser

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:  # noqa: N802 - Qt API
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            log.warning("page: %s (%s:%s)", message, source, line)


class MainWindow(QMainWindow):
    def __init__(self, core: AppCore, url: str) -> None:
        super().__init__()
        self.core = core
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(1024, 700)
        self.resize(1280, 800)
        if core.settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(core.settings.window_geometry.encode("ascii")))
        # Create the view before the profile: on teardown the page (owned by the
        # view) must go away before the profile it uses.
        self.view = QWebEngineView(self)
        self.profile = QWebEngineProfile(self)  # no storage name -> off-the-record
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.profile.setSpellCheckEnabled(False)
        self.interceptor = LocalOnlyInterceptor(core.allowed_origins, self)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.page = Page(self.profile, self.view, core.server.base_url + "/")
        self.view.setPage(self.page)
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCentralWidget(self.view)
        self.view.load(QUrl(url))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.core.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        super().closeEvent(event)


def build(app: QApplication, home: Home) -> tuple[AppCore, MainWindow, Bridge]:
    """Create backend and window (used by ``main`` and by the smoke test)."""
    bridge = Bridge()
    core = AppCore(home, Settings.load(home), hooks=QtHooks(bridge))
    url = core.start()
    window = MainWindow(core, url)
    bridge.open_external.connect(lambda link: QDesktopServices.openUrl(QUrl(link)))

    def on_fatal(reason: str) -> None:
        message = NO_WEBGL if reason == "webgl2" else f"程序遇到无法继续的问题：{reason}"
        QMessageBox.critical(window, APP_NAME, message)
        app.quit()

    bridge.fatal.connect(on_fatal)
    return core, window, bridge


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    home = Home(program_dir())
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)  # required by Qt WebEngine
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DisableShaderDiskCache)  # nothing in %LOCALAPPDATA%
    app = QApplication(argv)
    app.setApplicationDisplayName(APP_NAME)

    instance = single_instance.acquire(home)
    if instance is None:
        single_instance.activate_existing()
        return 0
    if not home.writable():
        QMessageBox.critical(None, APP_NAME, NOT_WRITABLE)
        return 1
    cleanup.finish_pending_clear(home)
    logs.setup(home)
    cleanup.remove_tmp(home)

    core, window, _bridge = build(app, home)
    window.show()
    try:
        return app.exec()
    finally:
        core.shutdown()


if __name__ == "__main__":
    sys.exit(main())
