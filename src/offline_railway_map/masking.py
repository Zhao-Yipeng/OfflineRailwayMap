"""Make the cached parts of an online raster tile transparent (SPEC §7.8)."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect
from PySide6.QtGui import QColor, QImage, QPainter


def _encode_png(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _transparent_png() -> bytes:
    image = QImage(1, 1, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    return _encode_png(image)


# 1x1 fully transparent PNG; the map stretches it over the tile.
TRANSPARENT_PNG = _transparent_png()


def apply_mask(png: bytes, rects) -> bytes:
    """Clear each (left, top, right, bottom) fraction of the image.

    Cell borders fall on whole pixels (SPEC §5.2), so the rectangles are
    rounded to the nearest pixel.
    """
    image = QImage.fromData(png)
    if image.isNull():
        return TRANSPARENT_PNG
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    w, h = image.width(), image.height()
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
    for left, top, right, bottom in rects:
        x0, y0 = round(left * w), round(top * h)
        x1, y1 = round(right * w), round(bottom * h)
        painter.fillRect(QRect(x0, y0, x1 - x0, y1 - y0), QColor(0, 0, 0, 0))
    painter.end()
    return _encode_png(image)
