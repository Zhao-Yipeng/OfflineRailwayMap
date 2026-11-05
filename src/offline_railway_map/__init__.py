"""OfflineRailwayMap: browse OpenRailwayMap-style railway speed data and cache it locally."""

APP_NAME = "OfflineRailwayMap"
__version__ = "0.1.0"
PROJECT_URL = "https://github.com/Zhao-Yipeng/task1"

# SPEC §12.1: every outgoing request identifies the program; never a browser or another app.
USER_AGENT = f"{APP_NAME}/{__version__} (+{PROJECT_URL})"
