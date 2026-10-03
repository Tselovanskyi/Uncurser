"""Render the app SVG into Windows icon sizes using the bundled Qt renderer."""
from pathlib import Path
import struct

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


root = Path(__file__).resolve().parents[1]
icons = root / "app" / "assets" / "icons"
app = QGuiApplication([])
renderer = QSvgRenderer(str(icons / "uncurser.svg"))
if not renderer.isValid():
    raise ValueError("Invalid app icon SVG")

sizes = (16, 24, 32, 48, 64, 128, 256)
entries, images = [], []
offset = 6 + 16 * len(sizes)
for size in sizes:
    image = QImage(size * 4, size * 4, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    renderer.render(painter)
    painter.end()
    image = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Could not render app icon")
    payload = bytes(buffer.data())
    # ICO stores 256 pixels as zero; each image retains its PNG alpha channel.
    entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                               1, 32, len(payload), offset))
    images.append(payload)
    offset += len(payload)

(icons / "uncurser.ico").write_bytes(
    struct.pack("<HHH", 0, 1, len(sizes)) + b"".join(entries) + b"".join(images))
