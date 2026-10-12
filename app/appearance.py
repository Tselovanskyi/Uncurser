"""Bundled UI font and native Windows title-bar colors."""
import ctypes
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import QAbstractButton, QDialog, QDialogButtonBox, QMessageBox, QWidget


POPUP_STYLE = """
QDialog QLabel { font-family: 'Google Sans'; font-size: 11pt; font-weight: 500; }
QDialog QPushButton { font-family: 'Google Sans'; font-size: 11pt; font-weight: 500; min-width: 88px; padding: 10px 20px; background: #161616; color: #ededed; border: none; border-radius: 3.5px; }
QDialog QPushButton:enabled:hover { background: #2b2b2b; }
QDialog QPushButton:enabled:pressed { background: #404040; }
QDialog QPushButton:disabled { background: #101010; color: #555555; }
QDialog QPushButton#primary { background: #42d9e8; color: #082027; font-weight: 600; }
QDialog QPushButton#primary:enabled:hover { background: #79eaf4; }
QDialog QPushButton#primary:enabled:pressed { background: #20a9b9; }
QDialog QPushButton#primary:disabled { background: #101010; color: #666666; }
QDialog QPushButton#risk { font-size: 10pt; min-width: 0; padding: 6px 10px; background: transparent; color: #aaaaaa; }
QDialog QPushButton#risk:enabled:hover { background: transparent; color: #ffffff; }
QDialog QPushButton#risk:enabled:pressed { background: transparent; color: #42d9e8; }
QDialog QPushButton#risk:disabled { background: transparent; color: #555555; }
"""


class PopupLayouts(QObject):
    def eventFilter(self, widget, event):
        if isinstance(widget, QAbstractButton) and event.type() in (QEvent.Type.Polish, QEvent.Type.EnabledChange):
            widget.setCursor(Qt.CursorShape.PointingHandCursor if widget.isEnabled() else Qt.CursorShape.ArrowCursor)
        if event.type() not in (QEvent.Type.Polish, QEvent.Type.Show) or not isinstance(widget, QDialog):
            return False
        if POPUP_STYLE not in widget.styleSheet():
            widget.setStyleSheet(widget.styleSheet() + "\n" + POPUP_STYLE)
        if isinstance(widget, QMessageBox):
            widget.setOption(QMessageBox.Option.DontUseNativeDialog, True)
            if event.type() == QEvent.Type.Show and widget.layout():
                widget.layout().setContentsMargins(24, 24, 24, 24)
                widget.layout().setSpacing(18)
        for buttons in widget.findChildren(QDialogButtonBox):
            if buttons.layout():
                buttons.layout().setSpacing(12)
        return False


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
    ui_font = QFont("Google Sans", 10, QFont.Weight.Medium)
    app.setFont(ui_font)
    app.popup_layouts = PopupLayouts(app)
    app.installEventFilter(app.popup_layouts)
    if sys.platform == "win32":
        try:
            app.title_bars = TitleBars(app)
            app.installEventFilter(app.title_bars)
        except OSError:
            logging.exception("Windows title-bar colors could not be configured")
