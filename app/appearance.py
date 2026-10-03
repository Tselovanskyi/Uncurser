"""Bundled UI font and native Windows title-bar colors."""
import ctypes
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QWidget


class TitleBars(QObject):
    def __init__(self, parent):
        super().__init__(parent)
        from ctypes import wintypes
        self.set_attribute = ctypes.WinDLL("dwmapi").DwmSetWindowAttribute
        self.set_attribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        self.set_attribute.restype = ctypes.c_long

    def eventFilter(self, widget, event):
        if (event.type() in (QEvent.Type.Show, QEvent.Type.ThemeChange)
                and isinstance(widget, QWidget) and widget.isWindow()):
            handle = int(widget.winId())
            # Keep the native frame and controls; override only its colors.
            text_color = 0x000000 if widget.property("hideTitleText") else 0xEDEDED
            for attribute, color in ((20, 1), (35, 0x000000), (36, text_color)):
                value = ctypes.c_uint32(color)
                self.set_attribute(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
        return False


def configure(app):
    icon = QIcon(str(Path(__file__).resolve().parent / "assets" / "icons" / "uncurser.ico"))
    if icon.isNull():
        raise RuntimeError("The bundled app icon could not be loaded. Download a fresh copy of Uncurser.")
    app.setWindowIcon(icon)
    font = Path(__file__).resolve().parent / "assets" / "fonts" / "GoogleSans.ttf"
    if QFontDatabase.addApplicationFont(str(font)) < 0:
        raise RuntimeError("The bundled Google Sans font could not be loaded. Download a fresh copy of Uncurser.")
    ui_font = QFont("Google Sans", 10)
    ui_font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias | QFont.StyleStrategy.NoSubpixelAntialias)
    ui_font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    app.setFont(ui_font)
    if sys.platform == "win32":
        try:
            app.title_bars = TitleBars(app)
            app.installEventFilter(app.title_bars)
        except OSError:
            logging.exception("Windows title-bar colors could not be configured")
