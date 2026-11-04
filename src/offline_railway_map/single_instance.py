"""Single running instance per program folder (SPEC §11.2).

Uses a Windows named mutex, so no lock file is created.  On other platforms
(development only) no check is made.
"""

from __future__ import annotations

import hashlib
import sys

from . import APP_NAME
from .paths import Home

ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


def _mutex_name(home: Home) -> str:
    digest = hashlib.sha1(str(home.root).lower().encode("utf-8")).hexdigest()[:16]
    return f"Local\\{APP_NAME}-{digest}"


def acquire(home: Home):
    """Return a handle to keep for the process lifetime, or None if another instance runs."""
    if sys.platform != "win32":
        return object()
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateMutexW(None, False, _mutex_name(home))
    error = ctypes.get_last_error()
    if not handle:
        return object()  # cannot tell; do not block the start
    if error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle


def activate_existing() -> None:
    """Bring the already running window to the front."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    hwnd = user32.FindWindowW(None, APP_NAME)
    if hwnd:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
