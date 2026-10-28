import datetime
import html
import ipaddress
import logging
import re
import threading
import traceback
import weakref
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPointF, QRectF, QRunnable, QSignalBlocker, QSize, Qt, QThreadPool, QTimer, QUrl, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPainter, QPen, QTextCharFormat, QTextCursor
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
from PySide6.QtWidgets import (QAbstractButton, QApplication, QDialog, QDialogButtonBox,
                               QFrame, QGraphicsOpacityEffect, QGridLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QMainWindow, QMessageBox,
                               QPushButton, QPlainTextEdit, QScrollArea, QSizePolicy, QStackedWidget, QStyle, QStyleOptionButton, QVBoxLayout, QWidget)

from . import __version__, engine, kamp, mesh, mesh_temperature, mods, network, pa, recovery, screws, settings, smoothing, storage, ui_checks, warmup
from .orca_dialog import OrcaGcodeDialog
from .remote import Connection, NewHostKey, trust_key


STYLE = """
QWidget { background: #000000; color: #ededed; font-family: 'Google Sans'; font-size: 10pt; font-weight: 500; }
QMainWindow { background: #000000; }
QFrame#card { background: #0a0a0a; border: none; border-radius: 6px; }
QFrame#card QLabel { background: transparent; }
QWidget#modContent, QWidget#modBody, QWidget#modItem { background: transparent; }
QWidget#fileStatuses, QWidget#fileStatusViewport, QWidget#fileStatusRow, QWidget#scanContent { background: transparent; }
QWidget#scrollFade { background: transparent; }
QFrame#printers { background: transparent; border: none; }
QFrame#scanPanel { background: #071625; border: none; border-radius: 6px; }
QFrame#scanPanel QLabel { background: transparent; }
QLabel#title { font-size: 25pt; font-weight: 700; letter-spacing: 1px; }
QLabel#section { font-size: 12pt; font-weight: 600; }
QLabel#groupHeading { font-size: 9pt; font-weight: 600; letter-spacing: 1px; color: #999999; }
QLabel#muted { color: #999999; }
QLabel#compatibilityWarning { color: #ff0000; font-size: 13pt; font-weight: 600; }
QLabel#cyan { color: #42d9e8; font-weight: 600; }
QLabel:disabled, QLabel#cyan:disabled, QLabel#muted:disabled { color: #858585; }
QWidget#printerRow { background: transparent; }
QPushButton { background: #161616; border: none; border-radius: 3.5px; padding: 9px 15px; }
QPushButton:enabled:hover { background: #2b2b2b; }
QPushButton:enabled:pressed { background: #404040; }
QPushButton:disabled { color: #555555; background: #101010; }
QPushButton#primary { background: #42d9e8; color: #082027; font-weight: 600; }
QPushButton#primary:enabled:hover { background: #79eaf4; }
QPushButton#primary:enabled:pressed { background: #20a9b9; }
QPushButton#primary:disabled { background: #101010; color: #666666; }
QPushButton[working="true"] { padding-right: 39px; color: #ededed; }
QPushButton#primary[working="true"] { background: #42d9e8; color: #082027; }
QPushButton#fixIssuesAction { background: #b42323; color: #ffffff; font-weight: 600; }
QPushButton#fixIssuesAction:enabled:hover { background: #cf3131; }
QPushButton#fixIssuesAction:enabled:pressed { background: #8e1c1c; }
QPushButton#fixIssuesAction:disabled { background: #3c1818; color: #9a6b6b; }
QPushButton#fixIssuesAction[working="true"] { background: #b42323; color: #ffffff; }
QPushButton#connected { background: #161616; color: #999999; }
QPushButton#connected:enabled:hover { background: #804000; color: #ffe3cc; }
QPushButton#connected:enabled:pressed { background: #a65100; color: #ffffff; }
QPushButton#connected:disabled { background: #101010; color: #555555; }
QPushButton#scan { background: #13283b; color: #ffffff; }
QPushButton#scan:enabled:hover { background: #254762; }
QPushButton#scan:enabled:pressed { background: #102030; }
QPushButton#scan:disabled { background: #102130; color: #7b8996; }
QPushButton#scan[working="true"] { background: #13283b; color: #ffffff; }
QPushButton#restore, QPushButton#restore:hover, QPushButton#restore:pressed, QPushButton#restore:disabled { background: transparent; }
QPushButton#restore { color: #ff7373; }
QPushButton#restore:enabled:hover { color: #ffa0a0; }
QPushButton#restore:enabled:pressed { color: #f04f4f; }
QPushButton#restore:disabled { color: #754040; }
QPushButton#textOnly, QPushButton#textOnly:hover, QPushButton#textOnly:pressed, QPushButton#textOnly:disabled { background: transparent; border: none; }
QPushButton#textOnly { color: #cccccc; }
QPushButton#textOnly:enabled:hover { color: #ffffff; }
QPushButton#textOnly:enabled:pressed { color: #42d9e8; }
QPushButton#textOnly:disabled { color: #555555; }
QPushButton#support { background: transparent; color: #cccccc; font-weight: 700; min-height: 38px; padding: 0 10px; }
QPushButton#support:enabled:hover { background: transparent; color: #ffffff; }
QPushButton#support:enabled:pressed { background: transparent; color: #42d9e8; }
QPushButton#support:disabled { background: transparent; color: #555555; }
QPushButton#modDetails, QPushButton#modDetails:hover, QPushButton#modDetails:pressed, QPushButton#modDetails:disabled { background: transparent; border: none; }
QPushButton#modDetails { text-align: left; padding: 6px 0; color: #999999; }
QPushButton#modDetails:enabled:hover { color: #ededed; }
QPushButton#modDetails:enabled:pressed { color: #42d9e8; }
QPushButton#modDetails:disabled { color: #555555; }
QPushButton#disclosure, QPushButton#disclosure:hover, QPushButton#disclosure:pressed { background: transparent; border: none; padding: 6px 23px 6px 0; color: #999999; }
QPushButton#disclosure:enabled:hover { color: #ededed; }
QPushButton#disclosure:enabled:pressed { color: #42d9e8; }
QPushButton#disclosure:disabled { color: #555555; }
QPushButton#risk { background: transparent; color: #aaaaaa; border: none; font-size: 9pt; padding: 5px; }
QPushButton#risk:enabled:hover { background: transparent; color: #ffffff; }
QPushButton#risk:enabled:pressed { background: transparent; color: #42d9e8; }
QPushButton#risk:disabled { background: transparent; color: #555555; }
QPushButton#add { padding: 0 12px 0 30px; }
QPushButton#remove { padding: 0; }
QPushButton#remove, QPushButton#remove:hover, QPushButton#remove:pressed, QPushButton#remove:disabled { background: transparent; border: none; }
QPushButton#remove { color: #999999; }
QPushButton#remove:enabled:hover, QPushButton#remove:enabled:focus { color: #ffffff; }
QPushButton#remove:enabled:pressed { color: #ff7373; }
QPushButton#remove:disabled { color: #444444; }
QLineEdit, QComboBox { background: #161616; border: none; border-radius: 3px; padding: 8px; }
QLineEdit:focus, QComboBox:focus { background: #202020; }
QPlainTextEdit { background: #111111; border: none; border-radius: 3px; padding: 9px; font-family: Consolas; font-size: 9pt; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #0a0a0a; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #333333; min-height: 24px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar#cardsScrollBar:vertical { background: #000000; }
QScrollBar#cardsScrollBar:vertical:disabled, QScrollBar#cardsScrollBar::handle:vertical:disabled { background: transparent; }
QScrollArea#fileStatuses QWidget#qt_scrollarea_vcontainer { background: transparent; }
QScrollBar#fileStatusScrollBar:vertical { background: rgba(28, 56, 79, 40); }
QScrollBar#fileStatusScrollBar::handle:vertical { background: #1c3a52; }
QToolTip { background: #000000; color: #ededed; border: 1px solid #666666; }
"""

DEFAULT_PASSWORD = "creality_2024"
PRINTER_ROW_HEIGHT = 48
SUPPORT_URL = "https://donatello.to/uncurse"
MOD_CONTROLS = (
    (mesh.TARGET, "mesh_staged", "Speed up Bed mesh"),
    (smoothing.TARGET, "smoothing_staged", "Speed up homing"),
    (kamp.TARGET, "kamp_staged", "Adaptive priming (KAMP-style)"),
    (mods.TARGET, "staged", "Heat before homing & calibration"),
    (mesh_temperature.TARGET, "temperature_staged", "Mesh at print temperature"),
    (warmup.TARGET, "warmup_staged", "Don’t delay chamber heating"),
    (pa.TARGET, "pa_staged", "Fix Adaptive PA"),
    (screws.PYTHON, "screws_staged", "Bed screw adjustment helper"),
)


class ActivityButton(QPushButton):
    def __init__(self, text, active_text, slot):
        super().__init__(text)
        self.idle_text, self.active_text = text, active_text
        self.clicked.connect(slot)
        self.running = False
        self.angle = 0
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.advance)

    def set_running(self, running):
        self.running = running
        self.setProperty("working", running)
        self.setText(self.active_text if running else self.idle_text)
        self.timer.start() if running else self.timer.stop()
        self.style().unpolish(self)
        self.style().polish(self)
        self.updateGeometry()
        self.update()

    def advance(self):
        self.angle = (self.angle + 16) % 360
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.running:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = "#082027" if self.objectName() == "primary" else "#ffffff" if self.objectName() == "scan" else "#ededed"
            painter.setPen(QPen(QColor(color), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawArc(QRectF(self.width() - 28, (self.height() - 14) / 2, 14, 14),
                            -self.angle * 16, 260 * 16)
            painter.end()


class LoadingLabel(QLabel):
    def __init__(self, caption, parent=None):
        super().__init__(caption + "...", parent)
        self.caption = caption
        self.phase = 0
        self.setObjectName("section")
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.timer = QTimer(self)
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.advance)

    def reserve_width(self):
        self.setFixedWidth(self.fontMetrics().horizontalAdvance(self.caption + "...") + 2)

    def advance(self):
        self.phase = self.phase % 3 + 1
        self.setText(self.caption + "." * self.phase)

    def showEvent(self, event):
        super().showEvent(event)
        self.ensurePolished()
        self.reserve_width()
        self.phase = 0
        self.advance()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(self, "caption"):
            self.reserve_width()


class ConnectionButton(QPushButton):
    def __init__(self):
        super().__init__("Connect")
        self.connected = False
        self.connecting = False
        self.hovered = False
        self.setObjectName("primary")
        self.setFixedWidth(110)

    def set_connected(self, connected, connecting=False):
        if (self.connected, self.connecting) != (connected, connecting):
            self.connected = connected
            self.connecting = connecting
            self.setObjectName("connected" if connected or connecting else "primary")
            self.style().unpolish(self)
            self.style().polish(self)
        self.setAccessibleName("Disconnect printer" if connected or connecting else "Connect printer")
        self.update_caption()

    def update_caption(self):
        self.setText("Disconnect" if self.connecting or (self.connected and self.hovered and self.isEnabled())
                     else "Connected" if self.connected else "Connect")

    def enterEvent(self, event):
        super().enterEvent(event)
        self.hovered = True
        self.update_caption()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.hovered = False
        self.update_caption()


class SymbolButton(QPushButton):
    def __init__(self, symbol, slot, text=""):
        super().__init__(text)
        self.symbol = symbol
        self.setFixedHeight(30)
        if not text:
            self.setFixedWidth(30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(slot)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.isDown():
            color = "#42d9e8" if self.symbol == "+" else "#ff7373"
        elif self.underMouse() or self.hasFocus():
            color = "#ffffff"
        else:
            color = "#ededed" if self.symbol == "+" else "#999999"
        painter.setPen(QPen(QColor(color if self.isEnabled() else "#444444"), 1.5,
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = (15 if self.text() else self.width() / 2), self.height() / 2
        if self.symbol == "+":
            painter.drawLine(QPointF(x - 5, y), QPointF(x + 5, y))
            painter.drawLine(QPointF(x, y - 5), QPointF(x, y + 5))
        else:
            painter.drawLine(QPointF(x - 4, y - 4), QPointF(x + 4, y + 4))
            painter.drawLine(QPointF(x - 4, y + 4), QPointF(x + 4, y - 4))
        painter.end()


class SmoothScrollArea(QScrollArea):
    def __init__(self):
        super().__init__()
        bar = self.verticalScrollBar()
        self.scroll_animation = QVariantAnimation(self)
        self.scroll_animation.setDuration(180)
        self.scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.scroll_animation.valueChanged.connect(lambda value: bar.setValue(round(value)))
        bar.sliderPressed.connect(self.scroll_animation.stop)
        bar.actionTriggered.connect(self.scroll_animation.stop)
        bar.rangeChanged.connect(self.scroll_animation.stop)
        bar.installEventFilter(self)

    def wheelEvent(self, event):
        bar = self.verticalScrollBar()
        if event.pixelDelta().y():
            # Touchpads already provide small, smooth position changes.
            self.scroll_animation.stop()
            bar.setValue(bar.value() - event.pixelDelta().y())
        elif event.angleDelta().y():
            delta = event.angleDelta().y() / 120 * QApplication.wheelScrollLines() * bar.singleStep()
            target = self.scroll_animation.endValue() if self.scroll_animation.state() == QVariantAnimation.State.Running else bar.value()
            if (target - bar.value()) * delta > 0:
                target = bar.value()
            target = max(bar.minimum(), min(bar.maximum(), round(target - delta)))
            self.scroll_animation.stop()
            with QSignalBlocker(self.scroll_animation):
                self.scroll_animation.setStartValue(float(bar.value()))
                self.scroll_animation.setEndValue(float(target))
                self.scroll_animation.start()
        else:
            super().wheelEvent(event)
            return
        event.accept()

    def eventFilter(self, widget, event):
        if event.type() == QEvent.Type.Wheel and widget is self.verticalScrollBar():
            self.wheelEvent(event)
            return event.isAccepted()
        return super().eventFilter(widget, event)


class ScrollFade(QWidget):
    def __init__(self, scroll, parent=None):
        super().__init__(parent if parent is not None else scroll.viewport())
        self.setObjectName("scrollFade")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.viewport = scroll.viewport()
        self.bar = scroll.verticalScrollBar()
        self.bar.rangeChanged.connect(self.sync)
        self.bar.valueChanged.connect(self.sync)
        self.viewport.installEventFilter(self)
        scroll.installEventFilter(self)
        self.parentWidget().installEventFilter(self)
        self.sync()

    def sync(self, *unused):
        origin = self.viewport.mapTo(self.parentWidget(), self.viewport.rect().topLeft())
        self.setGeometry(0, origin.y(), self.parentWidget().width(), self.viewport.height())
        self.bar.setEnabled(self.bar.maximum() > self.bar.minimum())
        self.setVisible(self.bar.maximum() > self.bar.minimum())
        self.raise_()
        self.update()

    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self.sync()
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        top = self.bar.value() - self.bar.minimum()
        bottom = self.bar.maximum() - self.bar.value()
        for distance, y in ((top, 0), (bottom, self.height() - 1)):
            if distance <= 0:
                continue
            painter.setOpacity(min(1.0, distance / 32.0))
            painter.fillRect(QRectF(0, y, self.width(), 1), QColor("#333333"))


class DetailsReveal(QWidget):
    def __init__(self, body):
        super().__init__()
        self.setObjectName("modBody")
        self.body = body
        body.setParent(self)
        body.show()
        self.fraction = 0.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(0)
        self.animation = QVariantAnimation(self)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.animation.valueChanged.connect(self.reveal)

    def set_expanded(self, expanded):
        self.animation.stop()
        target = 1.0 if expanded else 0.0
        with QSignalBlocker(self.animation):
            self.animation.setDuration(max(1, round(300 * abs(target - self.fraction))))
            self.animation.setStartValue(self.fraction)
            self.animation.setEndValue(target)
            self.animation.start()

    def reveal(self, fraction):
        self.fraction = fraction
        layout = self.body.layout()
        height = layout.totalHeightForWidth(self.width())
        if height < 0:
            height = layout.sizeHint().height()
        # Keep the text at its full height and clip it from the bottom.
        self.body.setGeometry(0, 0, self.width(), height)
        self.setFixedHeight(round(height * fraction))
        # Resize the enclosing layouts before a frame can paint a squeezed card.
        parent = self.parentWidget()
        while parent is not None and not parent.isWindow():
            if parent.layout() is not None:
                parent.layout().activate()
            parent = parent.parentWidget()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.reveal(self.fraction)


class DetailsButton(QPushButton):
    def __init__(self, body):
        super().__init__("Show details")
        self.body = body
        self.reveal = DetailsReveal(body)
        self.setObjectName("disclosure")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self.expand)

    def expand(self, expanded):
        self.setText("Hide details" if expanded else "Show details")
        self.reveal.set_expanded(expanded)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = "#555555" if not self.isEnabled() else "#42d9e8" if self.isDown() else "#ededed" if self.underMouse() else "#999999"
        painter.setPen(QPen(QColor(color), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        x, y = self.width() - 11, self.height() / 2
        direction = -1 if self.isChecked() else 1
        painter.drawLine(QPointF(x - 4, y - 2 * direction), QPointF(x, y + 2 * direction))
        painter.drawLine(QPointF(x, y + 2 * direction), QPointF(x + 4, y - 2 * direction))
        painter.end()


class ModFilesButton(QPushButton):
    def __init__(self, text, slot):
        super().__init__(text)
        self.outdated = False
        self.clicked.connect(slot)

    def set_outdated(self, value):
        if self.outdated != value:
            self.outdated = value
            self.updateGeometry()
            self.update()
        self.setAccessibleName(self.text() + (" · Outdated" if value else ""))

    def badge_font(self):
        font = QFont(self.font())
        font.setBold(True)
        return font

    def sizeHint(self):
        size = super().sizeHint()
        if self.outdated:
            size.setWidth(size.width() + QFontMetrics(self.badge_font()).horizontalAdvance(" · Outdated"))
        return size

    def paintEvent(self, event):
        if not self.outdated:
            super().paintEvent(event)
            return
        width = QFontMetrics(self.badge_font()).horizontalAdvance(" · Outdated")
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.rect.setWidth(self.width() - width)
        painter = QPainter(self)
        self.style().drawControl(QStyle.ControlElement.CE_PushButton, option, painter, self)
        painter.setFont(self.badge_font())
        painter.setPen(QColor("#ff0000" if self.isEnabled() else "#555555"))
        painter.drawText(QRectF(self.width() - width, 0, width, self.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, " · Outdated")
        painter.end()


class ModItem(QWidget):
    def __init__(self, name, pending, toggle, files, disclosure):
        super().__init__()
        self.setObjectName("modItem")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.disclosure = disclosure
        disclosure.installEventFilter(self)
        self._press = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        heading = QHBoxLayout()
        heading.addWidget(name, 1, Qt.AlignmentFlag.AlignBottom)
        controls = QWidget()
        controls.setObjectName("modContent")
        column = QVBoxLayout(controls)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(pending)
        column.addWidget(toggle, alignment=Qt.AlignmentFlag.AlignHCenter)
        heading.addWidget(controls, alignment=Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(heading)
        layout.addWidget(files, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(disclosure.reveal)
        layout.addWidget(disclosure, alignment=Qt.AlignmentFlag.AlignLeft)
        for widget in [self, *self.findChildren(QWidget)]:
            if not isinstance(widget, QAbstractButton):
                widget.installEventFilter(self)

    def eventFilter(self, widget, event):
        if widget is self.disclosure and event.type() == QEvent.Type.EnabledChange:
            self.setCursor(Qt.CursorShape.PointingHandCursor if self.disclosure.isEnabled() else Qt.CursorShape.ArrowCursor)
            return False
        if event.type() not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
                                QEvent.Type.MouseButtonRelease):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        position = event.globalPosition().toPoint()
        child = self.childAt(self.mapFromGlobal(position))
        while child is not None and child is not self:
            if isinstance(child, QAbstractButton):
                self._press = None
                return False
            child = child.parentWidget()
        if event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            self._press = position if self.disclosure.isEnabled() else None
            return self._press is not None
        pressed, self._press = self._press, None
        if (pressed is not None and self.disclosure.isEnabled()
                and self.rect().contains(self.mapFromGlobal(position))
                and (position - pressed).manhattanLength() < QApplication.startDragDistance()):
            self.disclosure.click()
            return True
        return False


class Switch(QAbstractButton):
    def __init__(self):
        super().__init__()
        self.setCheckable(True)
        self.setFixedSize(52, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("Heat before homing and calibration")
        self.applied = False
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.pressed.connect(self.update)
        self.released.connect(self.update)

    def event(self, event):
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.EnabledChange):
            self.update()
        return super().event(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = "#42d9e8" if self.applied and self.isEnabled() else "#666666"
        painter.setOpacity(1 if self.isEnabled() else 0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        fill = QColor(color if self.isChecked() else "#2a2a2a")
        if self.isEnabled():
            fill = fill.darker(130) if self.isDown() else fill.lighter(135) if self.underMouse() else fill
        painter.setBrush(fill)
        painter.drawRoundedRect(1, 3, 50, 24, 12, 12)
        painter.setBrush(QColor("#001315" if self.isChecked() and self.applied and self.isEnabled() else "#e5e5e5"))
        painter.drawEllipse(29 if self.isChecked() else 5, 7, 16, 16)
        painter.end()


class Signals(QObject):
    done = Signal(object)
    error = Signal(object)
    progress = Signal(str)
    found = Signal(object)


def operation_error_message(error):
    message = str(error).strip()
    if message:
        return message
    if isinstance(error, TimeoutError):
        message = "The printer did not reply in time."
    elif isinstance(error, EOFError):
        message = "The connection to the printer closed unexpectedly."
    else:
        message = "The operation failed without an error message."
    return message + " (" + type(error).__name__ + ")"


class Task(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function, self.signals = function, Signals()
        self.cancelled = threading.Event()
        self.last_progress = ""
        self.error_details = ""

    def progress(self, message):
        if not self.cancelled.is_set():
            self.last_progress = message
            self.signals.progress.emit(message)

    def run(self):
        if self.cancelled.is_set():
            return
        try:
            result = self.function(self.progress)
            if self.cancelled.is_set():
                return
            scanned = result.get("snapshot") if isinstance(result, dict) else result
            if isinstance(scanned, engine.Snapshot):
                ui_checks.populate(scanned)
        except Exception as error:
            if not self.cancelled.is_set():
                self.error_details = traceback.format_exc()
                logging.exception("Operation failed")
                self.signals.error.emit(error)
        else:
            if not self.cancelled.is_set():
                self.signals.done.emit(result)


class PrinterRow(QWidget):
    edited = Signal(object)
    connect_requested = Signal(object)
    disconnect_requested = Signal(object)
    remove_requested = Signal(object)

    def __init__(self, values):
        super().__init__()
        self.setObjectName("printerRow")
        self.setFixedHeight(PRINTER_ROW_HEIGHT)
        self.reachable = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.name = QLineEdit(values.get("name", ""))
        self.name.setPlaceholderText("Printer name")
        self.name.setAccessibleName("Printer name")
        self.name.setToolTip("Editable name saved in this app")
        self.name.setMinimumWidth(90)
        self.host = QLineEdit(values.get("host", ""))
        self.host.setMinimumWidth(130)
        self.host.setMaximumWidth(180)
        self.host.setAccessibleName("Printer IP address")
        self.host.setToolTip("Printer IPv4 address")
        self.username = QLineEdit(values.get("username", "root"))
        self.username.setMinimumWidth(65)
        self.username.setMaximumWidth(100)
        self.username.setAccessibleName("SSH username")
        self.password = QLineEdit(values.get("password") or DEFAULT_PASSWORD)
        self.password.setMinimumWidth(115)
        self.password.setPlaceholderText("Printer root password")
        self.password.setEchoMode(QLineEdit.EchoMode.Normal)
        self.password.setAccessibleName("Saved SSH password")
        self.connect_button = ConnectionButton()
        self.connect_button.clicked.connect(lambda: self.disconnect_requested.emit(self) if self.connect_button.connected or self.connect_button.connecting
                                            else self.connect_requested.emit(self))
        self.availability = label("Checking…", "muted")
        self.availability.setFixedWidth(110)
        self.availability.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.connection_state = QStackedWidget()
        self.connection_state.setFixedWidth(110)
        self.connection_state.addWidget(self.availability)
        self.connection_state.addWidget(self.connect_button)
        self.remove_button = SymbolButton("×", lambda: self.remove_requested.emit(self))
        self.remove_button.setObjectName("remove")
        self.remove_button.setAccessibleName("Remove saved printer")
        self.remove_button.setToolTip("Remove this printer from the saved list")
        for widget in (self.remove_button, self.name, self.host, self.username, self.password, self.connection_state):
            row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)
        for field in (self.name, self.host, self.username, self.password):
            field.textEdited.connect(lambda text: self.edited.emit(self))
        self.password.returnPressed.connect(lambda: self.connect_requested.emit(self) if self.reachable else None)
        self.identity = (self.host.text().strip(), self.username.text().strip())

    def values(self):
        return {"name": self.name.text().strip(), "host": self.host.text().strip(), "username": self.username.text().strip() or "root",
                "password": self.password.text()}

    def connection(self):
        values = self.values()
        return Connection(network.parse_host(values["host"]), username=values["username"], password=values["password"])

    def refresh(self, active, busy, *, connecting=False, can_disconnect=False):
        self.connection_state.setCurrentWidget(self.connect_button if self.reachable or active or connecting else self.availability)
        self.connect_button.setEnabled(not busy or can_disconnect)
        self.connect_button.set_connected(active, connecting)
        self.remove_button.setEnabled(not busy)
        self.availability.setText(("Checking…" if self.reachable is None else "Unavailable") if self.host.text().strip() else "")
        self.availability.setToolTip("" if active else "Connect is available when the printer is reachable.")
        for field in (self.name, self.host, self.username, self.password):
            field.setEnabled(not busy)


def label(text, name=None, wrap=False):
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def button(text, slot=None, primary=False):
    widget = QPushButton(text)
    if slot:
        widget.clicked.connect(slot)
    if primary:
        widget.setObjectName("primary")
    return widget


def card():
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(20, 17, 20, 17)
    layout.setSpacing(12)
    return frame, layout


def file_summary(modified=0, added=0):
    parts = []
    if modified:
        parts.append(f"modifies {modified} {'file' if modified == 1 else 'files'}")
    if added:
        parts.append(f"adds {added} new {'file' if added == 1 else 'files'}")
    return " · ".join(parts)


def remove_all_targets(snapshot):
    removals = ui_checks.check(snapshot, "removals")
    targets = {path for path, status in removals.items() if status.get("present")}
    if not targets:
        return set(), "No installed mods were found."
    if ui_checks.check(snapshot, "issues") or snapshot.recovery:
        return set(), "Fix the detected issues before removing all mods."
    if targets - {path for path, _, _ in MOD_CONTROLS}:
        return set(), "Use Fix issues to handle unknown mods first."
    if any(not removals[path].get("available") for path in targets):
        return set(), "Some installed mods cannot be safely removed. Scan the printer to review the issues."
    return targets, ""


class RestoreChoiceDialog(QDialog):
    def __init__(self, removal_reason, can_restore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore original")
        self.setMinimumWidth(540)
        self.choice = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        message = label('Restore original uses backups and may undo unrelated changes made by you or the printer. '
                        'The safer choice is <span style="color: #00ff00;">&quot;Remove all mods&quot;</span>, '
                        'which uses each mod’s individual uninstall instructions.', wrap=True)
        message.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(message)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        for choice, title, name, enabled, reason in (
                ("remove", "Remove all mods", "removeModsAction", not removal_reason, removal_reason),
                ("restore", "Restore original", "restoreFilesAction", can_restore,
                 "Verified original backups for this printer model and firmware are required.")):
            action = button(title, lambda checked=False, selected=choice: self.select(selected))
            action.setObjectName(name)
            action.setEnabled(enabled)
            action.setAutoDefault(False)
            action.setDefault(choice == "remove" and enabled)
            action.setToolTip("" if enabled else reason)
            actions.addWidget(action)
        layout.addLayout(actions)
        self.setStyleSheet("""
            QPushButton#removeModsAction { background: #217a3c; color: #ffffff; }
            QPushButton#removeModsAction:enabled:hover { background: #29994c; }
            QPushButton#removeModsAction:enabled:pressed { background: #185b2c; }
            QPushButton#removeModsAction:disabled { background: #152b1b; color: #718777; }
            QPushButton#restoreFilesAction { background: #b42323; color: #ffffff; }
            QPushButton#restoreFilesAction:enabled:hover { background: #cf3131; }
            QPushButton#restoreFilesAction:enabled:pressed { background: #8e1c1c; }
            QPushButton#restoreFilesAction:disabled { background: #3c1818; color: #9a6b6b; }
        """)

    def select(self, choice):
        self.choice = choice
        self.accept()


class FirmwareIssuesDialog(QDialog):
    def __init__(self, issues, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scan for issues")
        self.setMinimumWidth(440)
        self.issue_buttons = {}
        self.unknown_buttons = {}
        self.unknown_available = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        can_fix = False
        entries = [(path, name) for path, _, name in MOD_CONTROLS]
        entries.extend((path, status.get("name") or path.rsplit("/", 1)[-1])
                       for path, status in issues.items() if status.get("unknown"))
        for path, name in entries:
            status = issues.get(path)
            if not status:
                continue
            row = QVBoxLayout()
            row.setSpacing(4)
            unknown = bool(status.get("unknown"))
            reason = ("Unknown mod." if unknown else
                      "Unknown firmware compatibility" if status.get("installed_firmware") is None else
                      status.get("firmware_reason") or "Mod firmware differs from the current printer firmware.")
            heading = label(html.escape(name) + '<br><span style="color: #ff0000;">' +
                            html.escape(reason).replace("\n", "<br>") + '</span>', wrap=True)
            heading.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(heading)
            if unknown or status.get("available") and status.get("reinstall") and status.get("present"):
                action_row = QHBoxLayout()
                action_row.setSpacing(8)
                action_row.addWidget(label('<span style="color: #00ff00;">Solution:</span>'))
                selected, alternative = ("Remove", "Ignore") if unknown else ("Reinstall", "Remove")
                action = button(selected)
                action.setObjectName("issueSolution")
                action.setCheckable(True)
                action.setChecked(True)
                action.setAutoDefault(False)
                (self.unknown_buttons if unknown else self.issue_buttons)[path] = action
                if unknown and status.get("available"):
                    self.unknown_available.add(path)
                action.toggled.connect(lambda checked, target=action, on=selected, off=alternative:
                                       target.setText(on if checked else off))
                action.toggled.connect(self.update_fix_enabled)
                action_row.addWidget(action)
                action_row.addStretch()
                row.addLayout(action_row)
            elif status.get("available"):
                row.addWidget(label('<span style="color: #00ff00;">Solution: Remove</span>'))
            if not status.get("available"):
                unresolved = "Unresolved: " + status.get("reason", "The changes could not be verified.")
                text = label(unresolved, wrap=True)
                text.setTextFormat(Qt.TextFormat.PlainText)
                row.addWidget(text)
            can_fix |= bool(not unknown and status.get("available"))
            layout.addLayout(row)
        storage_issue = issues.get(storage.ROOT)
        if storage_issue:
            obsolete = storage_issue.get("kind") == "cleanup"
            solution = "remove unusable backups" if storage_issue.get("damaged") else "remove obsolete backups"
            solution = solution + " and relocate current backups" if storage_issue.get("relocation") else solution
            solution = solution if obsolete else "relocation"
            problem = ("Obsolete or unusable backups." if storage_issue.get("damaged") else
                       "Backups from older firmware.") if obsolete else "Stored in the old location."
            if storage_issue.get("recovery"):
                recovery_text = "Recovery copies remain from removed mods."
                if storage_issue.get("originals"):
                    problem += "<br>" + recovery_text
                    solution += " and remove leftover recovery copies"
                elif storage_issue.get("relocation"):
                    problem = "Backups and uninstall records are stored in the old location.<br>" + recovery_text
                    solution = "Relocate backups and uninstall records, and remove leftover recovery copies."
                else:
                    problem = recovery_text
                    solution = "Remove leftover recovery copies."
            action = ('<span style="color: #00ff00;">Solution: ' + solution + '</span>' if storage_issue["available"] else
                      html.escape("Unresolved: " + storage_issue["reason"]).replace("\n", "<br>"))
            text = label('Backups and uninstall records<br><span style="color: #ff0000;">' +
                         problem + '</span><br>' + action, wrap=True)
            text.setTextFormat(Qt.TextFormat.RichText)
            layout.addWidget(text)
            can_fix = bool(storage_issue["available"])
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.fix_button = button("Fix issues")
        self.fix_button.setObjectName("fixIssues")
        self.can_fix_known = can_fix
        self.update_fix_enabled()
        buttons.addButton(self.fix_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reinstall_targets(self):
        return tuple(path for path, action in self.issue_buttons.items() if action.isChecked())

    def unknown_removals(self):
        return tuple(path for path, action in self.unknown_buttons.items()
                     if action.isChecked() and path in self.unknown_available)

    def ignored_targets(self):
        return tuple(path for path, action in self.unknown_buttons.items() if not action.isChecked())

    def update_fix_enabled(self):
        # Ignore is a session acknowledgement and does not require a write.
        if hasattr(self, "fix_button"):
            self.fix_button.setEnabled(bool(self.can_fix_known or self.unknown_removals() or self.ignored_targets()))


class FileStatusList(QScrollArea):
    def __init__(self, parent=None):
        super().__init__()
        self.setObjectName("fileStatuses")
        self.viewport().setObjectName("fileStatusViewport")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.verticalScrollBar().setObjectName("fileStatusScrollBar")
        self.setWidgetResizable(False)
        self.content = QWidget()
        self.content.setObjectName("fileStatuses")
        self.rows = QGridLayout(self.content)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setVerticalSpacing(0)
        self.rows.setHorizontalSpacing(40)
        self.rows.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        self.file_rows = []
        self.list_size = QSize(0, 0)
        self.layout_key = None
        self.arrange_timer = QTimer(self)
        self.arrange_timer.setSingleShot(True)
        self.arrange_timer.timeout.connect(self.arrange_rows)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setWidget(self.content)
        if parent is not None:
            self.setParent(parent)
            parent.installEventFilter(self)
        self.set_files(())

    def set_files(self, entries):
        self.layout_key = None
        while self.rows.count():
            old = self.rows.takeAt(0).widget()
            old.hide()
            old.deleteLater()
        self.file_rows = []
        for filename, status in entries:
            row = QWidget()
            row.setObjectName("fileStatusRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(16)
            layout.addWidget(label(filename, "muted"))
            layout.addStretch()
            layout.addWidget(label(status, "muted"))
            self.file_rows.append(row)
        self.arrange_rows()

    def sizeHint(self):
        return self.list_size

    def eventFilter(self, widget, event):
        if (event.type() == QEvent.Type.Show or
                event.type() == QEvent.Type.Resize and event.size().width() != event.oldSize().width()):
            if widget is self.parentWidget():
                # Changing size constraints inside the parent's resize handler
                # can synchronously re-enter the scroll area's event filters.
                self.arrange_timer.start(0)
        return super().eventFilter(widget, event)

    def arrange_rows(self):
        self.arrange_timer.stop()
        columns = 1
        count = len(self.file_rows)
        if count > 1 and self.parentWidget() is not None:
            parent = self.parentWidget()
            available = parent.contentsRect().width()
            if parent.layout() is not None:
                margins = parent.layout().contentsMargins()
                available -= margins.left() + margins.right()
            split = (count + 1) // 2
            widths = [max(row.sizeHint().width() for row in group)
                      for group in (self.file_rows[:split], self.file_rows[split:])]
            gutter = self.verticalScrollBar().sizeHint().width() + 8 if split > 5 else 0
            if sum(widths) + self.rows.horizontalSpacing() + gutter + 2 * self.frameWidth() <= available:
                columns = 2
        key = (columns, tuple((row.sizeHint().width(), row.sizeHint().height()) for row in self.file_rows))
        if key == self.layout_key:
            return
        self.layout_key = key
        while self.rows.count():
            self.rows.takeAt(0)
        row_count = (count + columns - 1) // columns
        heights = [0] * row_count
        for index, row in enumerate(self.file_rows):
            position = index % row_count
            self.rows.addWidget(row, position, index // row_count)
            row.show()
            heights[position] = max(heights[position], row.sizeHint().height())
        self.rows.activate()
        size = self.content.sizeHint()
        visible_height = sum(heights[:5])
        scrollbar_width = self.verticalScrollBar().sizeHint().width() if size.height() > visible_height else 0
        gap = 8 if scrollbar_width else 0
        if self.viewportMargins().right() != gap:
            self.setViewportMargins(0, 0, gap, 0)
        size = QSize(size.width() + gap + scrollbar_width + 2 * self.frameWidth(),
                     visible_height + 2 * self.frameWidth())
        if (size != self.list_size or self.maximumWidth() != size.width()
                or self.minimumHeight() != size.height() or self.maximumHeight() != size.height()):
            self.list_size = size
            self.setMaximumWidth(size.width())
            self.setFixedHeight(size.height())
            self.updateGeometry()


class Window(QMainWindow):
    def __init__(self, app_dir: Path, start_polling=True):
        super().__init__()
        self.settings_path = app_dir / settings.FILENAME
        settings.migrate(self.settings_path)
        self.known_hosts = self.settings_path
        self.settings = settings.load(self.settings_path)
        self.settings_dirty = False
        self.printer_rows = []
        self.active_row = None
        self.connected = False
        self.reachability_task = None
        self.reachability_pending = False
        self.reachability_generation = 0
        self.start_polling = start_polling
        self.snapshot = None
        self.snapshot_connection = None
        self.ignored_mods = set()
        self.staged = None
        self.mesh_staged = None
        self.temperature_staged = None
        self.screws_staged = None
        self.smoothing_staged = None
        self.warmup_staged = None
        self.kamp_staged = None
        self.pa_staged = None
        self.restore_pending = False
        self.update_enabled = True
        self.automatic_update_error = ""
        self.policy = "known"
        self.busy = False
        self.scanning = False
        self.connect_cancel_ready = False
        self.active_task = None
        self.discovery_task = None
        self.discovery_stop = threading.Event()
        self.discovery_ignored = set()
        self.closing = False
        self.pool = QThreadPool(self)
        self.plan_key = None
        self.plan_result = None
        self.plan_request = None
        self.plan_task = None
        self.apply_waiting = None
        self.plan_timer = QTimer(self)
        self.plan_timer.setSingleShot(True)
        self.plan_timer.timeout.connect(self.validate_pending)
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.persist_settings)
        self.probe_timer = QTimer(self)
        self.probe_timer.setSingleShot(True)
        self.probe_timer.timeout.connect(self.check_reachability)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.check_reachability)
        self.disconnect_timer = QTimer(self)
        self.disconnect_timer.setSingleShot(True)
        self.disconnect_timer.setInterval(7000)
        self.disconnect_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.disconnect_timer.timeout.connect(self.enable_scan_disconnect)
        self.setWindowTitle("Uncurser · K2 Plus")
        self.setProperty("hideTitleText", True)
        self.resize(720, 920)
        self.setMinimumSize(720, 730)
        self.setStyleSheet(STYLE)
        container = QWidget()
        outer = QVBoxLayout(container)
        # Reserve a 12px gap before the 8px scrollbar without narrowing cards.
        outer.setContentsMargins(28, 22, 8, 20)
        outer.setSpacing(16)
        outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.setCentralWidget(container)
        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 20, 0)
        header.setSpacing(16)
        outer.addLayout(header)
        top = QHBoxLayout()
        top.addWidget(label('UNCURSER <span style="font-size: 14pt; font-weight: 500; color: #999999;">for K2 Plus</span>', "title"))
        top.addStretch()
        top.addWidget(label(f"v{__version__}", "muted"), alignment=Qt.AlignmentFlag.AlignVCenter)
        header.addLayout(top)

        connection_card, layout = card()
        connection_card.setObjectName("printers")
        connection_card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QHBoxLayout()
        heading.addWidget(label("Printers", "section"))
        heading.addStretch()
        self.add_button = SymbolButton("+", self.add_manual_printer, "Add")
        self.add_button.setObjectName("add")
        self.add_button.setAccessibleName("Add printer manually")
        self.add_button.setToolTip("Add a printer by IP address")
        heading.addWidget(self.add_button)
        self.find_button = ActivityButton("Find printer", "Searching…", self.find_printers)
        heading.addWidget(self.find_button)
        layout.addLayout(heading)
        self.printer_list = QWidget()
        self.printer_list.setObjectName("printerRow")
        self.printer_layout = QVBoxLayout(self.printer_list)
        self.printer_layout.setContentsMargins(0, 0, 0, 0)
        self.printer_layout.setSpacing(8)
        self.printer_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.empty_printers = label("No saved printers. Find a printer or add one.", "muted")
        self.printer_layout.addWidget(self.empty_printers)
        self.printer_list.setAutoFillBackground(False)
        layout.addWidget(self.printer_list)
        saved = self.settings.get("printers")
        if not isinstance(saved, list):
            saved = [self.settings] if self.settings.get("host") else []
        for values in saved:
            if isinstance(values, dict) and str(values.get("host", "")).strip():
                self.add_printer(values, save=False)
        self.update_printer_list()
        header.addWidget(connection_card)

        self.scan_card, layout = card()
        self.scan_card.setObjectName("scanPanel")
        self.scan_loading = LoadingLabel("Reading printer details", self.scan_card)
        layout.addWidget(self.scan_loading, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.scan_content = QWidget()
        self.scan_content.setObjectName("scanContent")
        layout.addWidget(self.scan_content)
        layout = QVBoxLayout(self.scan_content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        scan_heading = QHBoxLayout()
        self.printer_heading = label("Reading printer details…", "section", True)
        self.printer_heading.setTextFormat(Qt.TextFormat.PlainText)
        scan_heading.addWidget(self.printer_heading, 1)
        scan_heading.addStretch()
        self.scan_button = ActivityButton("Scan printer", "Scanning…", self.scan_clicked)
        self.scan_button.setObjectName("scan")
        scan_heading.addWidget(self.scan_button)
        layout.addLayout(scan_heading)
        self.compatibility_status = label("", "compatibilityWarning", True)
        self.compatibility_status.setTextFormat(Qt.TextFormat.PlainText)
        self.compatibility_status.hide()
        layout.addWidget(self.compatibility_status)
        self.files_status = FileStatusList(self.scan_content)
        layout.addWidget(self.files_status, alignment=Qt.AlignmentFlag.AlignLeft)
        self.files_errors = label("", "muted", True)
        self.files_errors.hide()
        layout.addWidget(self.files_errors)
        actions = QHBoxLayout()
        self.review_button = button("Review compatibility", self.review_compatibility)
        self.review_button.hide()
        actions.addWidget(self.review_button)
        self.repair_button = button("Fix issues", self.repair_startup)
        self.repair_button.setObjectName("scan")
        self.repair_button.hide()
        actions.addWidget(self.repair_button)
        actions.addStretch()
        layout.addLayout(actions)

        mod_section, section_layout = card()
        section_layout.addWidget(label("PRINT STARTUP", "groupHeading"))
        mod_list = QVBoxLayout()
        mod_list.setSpacing(28)
        section_layout.addLayout(mod_list)
        self.mod_card = QWidget()
        self.mod_card.setObjectName("modContent")
        layout = QHBoxLayout(self.mod_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Heat before homing & calibration", "section"))
        self.mod_opacity = QGraphicsOpacityEffect(self.mod_card)
        self.mod_card.setGraphicsEffect(self.mod_opacity)
        self.heat_body = QWidget()
        self.heat_body.setObjectName("modBody")
        heat_layout = QVBoxLayout(self.heat_body)
        heat_layout.setContentsMargins(0, 0, 0, 0)
        heat_layout.setSpacing(12)
        self.heat_expand = DetailsButton(self.heat_body)
        self.toggle = Switch()
        self.toggle.toggled.connect(self.stage_toggle)
        self.pending_label = label("Pending", "muted")
        self.pending_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.pending_label.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.pending_label.setSizePolicy(pending_size)
        heat_layout.addWidget(label("<ul><li>Start bed and chamber heating (if used) before homing or pre-print calibration for every print."
                               "<br>This will heat everything up even before the initial homing begins.</li>"
                               "<li>Doesn't affect manually triggered homing.</li></ul>", wrap=True))
        added_lines = sum(insertion.marked.count("\n") for insertion in mods.INSERTIONS)
        self.heat_details = ModFilesButton(file_summary(modified=1) + f" · {added_lines} added lines",
                                   lambda: self.show_mod_code(mods.TARGET))
        self.heat_details.setObjectName("modDetails")
        self.heat_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.heat_details.setToolTip("View the code for the initial application of this mod")
        self.heat_item = ModItem(self.mod_card, self.pending_label, self.toggle,
                                 self.heat_details, self.heat_expand)
        self.mesh_card = QWidget()
        self.mesh_card.setObjectName("modContent")
        layout = QHBoxLayout(self.mesh_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Speed up Bed mesh", "section"))
        self.mesh_opacity = QGraphicsOpacityEffect(self.mesh_card)
        self.mesh_card.setGraphicsEffect(self.mesh_opacity)
        self.mesh_body = QWidget()
        self.mesh_body.setObjectName("modBody")
        mesh_layout = QVBoxLayout(self.mesh_body)
        mesh_layout.setContentsMargins(0, 0, 0, 0)
        mesh_layout.setSpacing(12)
        self.mesh_expand = DetailsButton(self.mesh_body)
        self.mesh_toggle = Switch()
        self.mesh_toggle.setAccessibleName("Speed up Bed mesh")
        self.mesh_toggle.toggled.connect(self.stage_mesh_toggle)
        self.mesh_pending = label("Pending", "muted")
        self.mesh_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.mesh_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.mesh_pending.setSizePolicy(pending_size)
        mesh_layout.addWidget(label("<ul><li>Set mesh travel to 600 mm/s and a 9×9 probe grid.</li>"
                                    "<li>Set Z movement limit, probe lift, and fast Z alignment to 50 mm/s, with a 0.9 fast-travel ratio.</li>"
                                    "<li>Applies to automatic and manually triggered mesh and Z operations.</li></ul>", wrap=True))
        self.mesh_details = ModFilesButton(file_summary(modified=1) + f" · {len(mesh.VALUES)} settings",
                                   lambda: self.show_mod_code(mesh.TARGET))
        self.mesh_details.setObjectName("modDetails")
        self.mesh_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.mesh_details.setToolTip("View the code for the initial application of this mod")
        self.mesh_item = ModItem(self.mesh_card, self.mesh_pending, self.mesh_toggle,
                                 self.mesh_details, self.mesh_expand)
        self.temperature_card = QWidget()
        self.temperature_card.setObjectName("modContent")
        layout = QHBoxLayout(self.temperature_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Mesh at print temperature", "section"))
        self.temperature_opacity = QGraphicsOpacityEffect(self.temperature_card)
        self.temperature_card.setGraphicsEffect(self.temperature_opacity)
        self.temperature_body = QWidget()
        self.temperature_body.setObjectName("modBody")
        temperature_layout = QVBoxLayout(self.temperature_body)
        temperature_layout.setContentsMargins(0, 0, 0, 0)
        temperature_layout.setSpacing(12)
        self.temperature_expand = DetailsButton(self.temperature_body)
        self.temperature_toggle = Switch()
        self.temperature_toggle.setAccessibleName("Mesh at print temperature")
        self.temperature_toggle.toggled.connect(self.stage_temperature_toggle)
        self.temperature_pending = label("Pending", "muted")
        self.temperature_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.temperature_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.temperature_pending.setSizePolicy(pending_size)
        temperature_layout.addWidget(label("<ul><li>Use the requested bed temperature for pre-print bed mesh, including below 50°C."
                                           "<br>A 42°C print will mesh at 42°C instead of heating to 50°C first.</li>"
                                           "<li>Manual G29 calibration and the fallback when no temperature is supplied stay unchanged.</li></ul>", wrap=True))
        self.temperature_details = ModFilesButton(file_summary(modified=1) + " · 1 changed line",
                                          lambda: self.show_mod_code(mesh_temperature.TARGET))
        self.temperature_details.setObjectName("modDetails")
        self.temperature_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.temperature_details.setToolTip("View the code for the initial application of this mod")
        self.temperature_item = ModItem(self.temperature_card, self.temperature_pending, self.temperature_toggle,
                                        self.temperature_details, self.temperature_expand)
        self.smoothing_card = QWidget()
        self.smoothing_card.setObjectName("modContent")
        layout = QHBoxLayout(self.smoothing_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Speed up homing", "section"))
        self.smoothing_opacity = QGraphicsOpacityEffect(self.smoothing_card)
        self.smoothing_card.setGraphicsEffect(self.smoothing_opacity)
        self.smoothing_body = QWidget()
        self.smoothing_body.setObjectName("modBody")
        smoothing_layout = QVBoxLayout(self.smoothing_body)
        smoothing_layout.setContentsMargins(0, 0, 0, 0)
        smoothing_layout.setSpacing(12)
        self.smoothing_expand = DetailsButton(self.smoothing_body)
        self.smoothing_toggle = Switch()
        self.smoothing_toggle.setAccessibleName("Speed up homing")
        self.smoothing_toggle.toggled.connect(self.stage_smoothing_toggle)
        self.smoothing_pending = label("Pending", "muted")
        self.smoothing_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.smoothing_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.smoothing_pending.setSizePolicy(pending_size)
        smoothing_layout.addWidget(label(
            "Uses 5,000 mm/s² acceleration and motion smoothing for the first X/Y homing pass "
            "and the move to the bed centre.\n\n"
            "The second X/Y pass and the remaining homing moves keep their original settings. "
            "Requested movement speeds stay unchanged.",
            wrap=True))
        self.smoothing_details = ModFilesButton(file_summary(modified=2), lambda: self.show_mod_code(smoothing.TARGET))
        self.smoothing_details.setObjectName("modDetails")
        self.smoothing_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.smoothing_details.setToolTip("View the code for the initial application of this mod")
        self.smoothing_item = ModItem(self.smoothing_card, self.smoothing_pending, self.smoothing_toggle,
                                      self.smoothing_details, self.smoothing_expand)
        self.warmup_card = QWidget()
        self.warmup_card.setObjectName("modContent")
        layout = QHBoxLayout(self.warmup_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Don’t delay chamber heating", "section"))
        self.warmup_opacity = QGraphicsOpacityEffect(self.warmup_card)
        self.warmup_card.setGraphicsEffect(self.warmup_opacity)
        self.warmup_body = QWidget()
        self.warmup_body.setObjectName("modBody")
        warmup_layout = QVBoxLayout(self.warmup_body)
        warmup_layout.setContentsMargins(0, 0, 0, 0)
        warmup_layout.setSpacing(12)
        warmup_layout.addWidget(label(
            "Start chamber heating at 50% power while the bed heats, instead of waiting for the bed to be ready", wrap=True))
        self.warmup_expand = DetailsButton(self.warmup_body)
        self.warmup_toggle = Switch()
        self.warmup_toggle.setAccessibleName("Don’t delay chamber heating")
        self.warmup_toggle.toggled.connect(self.stage_warmup_toggle)
        self.warmup_pending = label("Pending", "muted")
        self.warmup_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.warmup_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.warmup_pending.setSizePolicy(pending_size)
        self.warmup_details = ModFilesButton(file_summary(modified=1), lambda: self.show_mod_code(warmup.TARGET))
        self.warmup_details.setObjectName("modDetails")
        self.warmup_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.warmup_details.setToolTip("View the code for the initial application of this mod")
        self.warmup_item = ModItem(self.warmup_card, self.warmup_pending, self.warmup_toggle,
                                   self.warmup_details, self.warmup_expand)
        self.kamp_card = QWidget()
        self.kamp_card.setObjectName("modContent")
        kamp_heading = QHBoxLayout(self.kamp_card)
        kamp_heading.setContentsMargins(0, 0, 0, 0)
        kamp_heading.addWidget(label("Adaptive priming (KAMP-style)", "section"))
        self.kamp_opacity = QGraphicsOpacityEffect(self.kamp_card)
        self.kamp_card.setGraphicsEffect(self.kamp_opacity)
        self.kamp_body = QWidget()
        self.kamp_body.setObjectName("modBody")
        kamp_layout = QVBoxLayout(self.kamp_body)
        kamp_layout.setContentsMargins(0, 0, 0, 0)
        kamp_layout.addWidget(label(
            "<ul><li>Replaces the L-shaped prime line with a KAMP-style prime line beside the printed model.</li>"
            "<li>The printer remains compatible with unmodified slicer G-code and will use "
            "the original L-shaped prime line with it.</li></ul>"
            "<p><b>Setting up:</b></p>"
            "<ol><li>Apply the mod.</li>"
            "<li>Use the <b>G-code for Orca</b> button to convert your <b>Machine start G-code</b>. "
            "The converter works in both directions, so you can also restore the original L-shaped prime line.</li></ol>", wrap=True))
        self.kamp_expand = DetailsButton(self.kamp_body)
        self.kamp_toggle = Switch()
        self.kamp_toggle.setAccessibleName("Adaptive priming (KAMP-style)")
        self.kamp_toggle.toggled.connect(self.stage_kamp_toggle)
        self.kamp_pending = label("Pending", "muted")
        self.kamp_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.kamp_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.kamp_pending.setSizePolicy(pending_size)
        self.kamp_details = ModFilesButton(file_summary(modified=1), lambda: self.show_mod_code(kamp.TARGET))
        self.kamp_details.setObjectName("modDetails")
        self.kamp_details.setToolTip("View the code for the initial application of this mod")
        self.kamp_item = ModItem(self.kamp_card, self.kamp_pending, self.kamp_toggle,
                                 self.kamp_details, self.kamp_expand)
        self.orca_button = button("G-code for Orca", self.show_orca_gcode)
        self.kamp_item.layout().itemAt(0).layout().insertWidget(
            1, self.orca_button, alignment=Qt.AlignmentFlag.AlignBottom)
        self.kamp_item.layout().itemAt(0).layout().insertSpacing(2, 12)
        for item in (self.mesh_item, self.smoothing_item, self.kamp_item, self.heat_item, self.temperature_item, self.warmup_item):
            mod_list.addWidget(item)
        quality_section, quality_layout = card()
        quality_layout.addWidget(label("PRINT QUALITY", "groupHeading"))
        self.pa_card = QWidget()
        self.pa_card.setObjectName("modContent")
        pa_heading = QHBoxLayout(self.pa_card)
        pa_heading.setContentsMargins(0, 0, 0, 0)
        pa_heading.addWidget(label("Fix Adaptive PA", "section"))
        self.pa_opacity = QGraphicsOpacityEffect(self.pa_card)
        self.pa_card.setGraphicsEffect(self.pa_opacity)
        self.pa_body = QWidget()
        self.pa_body.setObjectName("modBody")
        pa_layout = QVBoxLayout(self.pa_body)
        pa_layout.setContentsMargins(0, 0, 0, 0)
        pa_layout.addWidget(label(
            "Adds proper support for Adaptive Pressure Advance. "
            "This mod should remove ugly corner artifacts when using Adaptive PA", wrap=True))
        self.pa_expand = DetailsButton(self.pa_body)
        self.pa_toggle = Switch()
        self.pa_toggle.setAccessibleName("Fix Adaptive PA")
        self.pa_toggle.toggled.connect(self.stage_pa_toggle)
        self.pa_pending = label("Pending", "muted")
        self.pa_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.pa_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.pa_pending.setSizePolicy(pending_size)
        self.pa_details = ModFilesButton(file_summary(modified=1, added=1), lambda: self.show_mod_code(pa.TARGET))
        self.pa_details.setObjectName("modDetails")
        self.pa_details.setToolTip("View the code for the initial application of this mod")
        self.pa_item = ModItem(self.pa_card, self.pa_pending, self.pa_toggle,
                               self.pa_details, self.pa_expand)
        quality_layout.addWidget(self.pa_item)
        self.mod_version_rows = {
            mods.TARGET: (self.heat_details, self.heat_details.text()),
            mesh.TARGET: (self.mesh_details, self.mesh_details.text()),
            mesh_temperature.TARGET: (self.temperature_details, self.temperature_details.text()),
            smoothing.TARGET: (self.smoothing_details, self.smoothing_details.text()),
            warmup.TARGET: (self.warmup_details, self.warmup_details.text()),
            kamp.TARGET: (self.kamp_details, self.kamp_details.text()),
            pa.TARGET: (self.pa_details, self.pa_details.text()),
        }
        maintenance_section, maintenance_layout = card()
        maintenance_layout.addWidget(label("HELPERS", "groupHeading"))
        self.screws_card = QWidget()
        self.screws_card.setObjectName("modContent")
        layout = QHBoxLayout(self.screws_card)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(label("Bed screw adjustment helper", "section"))
        self.screws_opacity = QGraphicsOpacityEffect(self.screws_card)
        self.screws_card.setGraphicsEffect(self.screws_opacity)
        self.screws_body = QWidget()
        self.screws_body.setObjectName("modBody")
        screws_layout = QVBoxLayout(self.screws_body)
        screws_layout.setContentsMargins(0, 0, 0, 0)
        screws_layout.setSpacing(12)
        screws_layout.addWidget(label(
            "<ol><li>After applying and power cycling, clear the bed, clean the nozzle, set the target bed temperature and home all axes.</li>"
            "<li>Once the bed reaches the target temperature run <code>BED_SCREW_HELPER</code> "
            "in Fluidd's <b>UNCURSER</b> macro category. "
            "It probes a full bed mesh and calculates screw adjustments from its overall tilt. "
            "The popup shows the actual bed temperature when probing starts, for example <b>PROBED AT 70°C</b>.</li>"
            "<li>Leave the base screw unchanged, turn the others as shown, then run it again to check. "
            "Use consistent temperatures between attempts.</li>"
            "<li>After the final check, use <b>Save bed mesh</b> to save a temperature-named profile, "
            "or <b>Save as default bed mesh</b> to replace the default profile.</li></ol>", wrap=True))
        self.screws_expand = DetailsButton(self.screws_body)
        self.screws_toggle = Switch()
        self.screws_toggle.setAccessibleName("Bed screw adjustment helper")
        self.screws_toggle.toggled.connect(self.stage_screws_toggle)
        self.screws_pending = label("Pending", "muted")
        self.screws_pending.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pending_size = self.screws_pending.sizePolicy()
        pending_size.setRetainSizeWhenHidden(True)
        self.screws_pending.setSizePolicy(pending_size)
        self.screws_details = ModFilesButton(file_summary(modified=2, added=2), lambda: self.show_mod_code(screws.MOD_ID))
        self.screws_details.setObjectName("modDetails")
        self.screws_details.setCursor(Qt.CursorShape.PointingHandCursor)
        self.screws_details.setToolTip("View the code for the initial application of this mod")
        self.mod_version_rows[screws.PYTHON] = (self.screws_details, self.screws_details.text())
        self.screws_item = ModItem(self.screws_card, self.screws_pending, self.screws_toggle,
                                  self.screws_details, self.screws_expand)
        maintenance_layout.addWidget(self.screws_item)
        self.details_buttons = (self.heat_expand, self.mesh_expand, self.temperature_expand, self.smoothing_expand,
                                self.warmup_expand, self.screws_expand, self.kamp_expand, self.pa_expand)
        for disclosure in self.details_buttons:
            disclosure.toggled.connect(lambda expanded, active=disclosure: self.close_other_details(active, expanded))
        mods_and_footer = QVBoxLayout()
        mods_and_footer.setSpacing(4)
        cards = QWidget()
        cards_layout = QVBoxLayout(cards)
        cards_layout.setContentsMargins(0, 0, 12, 0)
        cards_layout.setSpacing(16)
        cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        cards_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        cards_layout.addWidget(self.scan_card)
        cards_layout.addWidget(mod_section)
        cards_layout.addWidget(quality_section)
        cards_layout.addWidget(maintenance_section)
        self.cards_scroll = SmoothScrollArea()
        self.cards_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.cards_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cards_scroll.verticalScrollBar().setObjectName("cardsScrollBar")
        self.cards_scroll.setWidgetResizable(True)
        self.cards_scroll.setMinimumHeight(100)
        self.cards_scroll.setWidget(cards)
        self.cards_fade = ScrollFade(self.cards_scroll, container)
        mods_and_footer.addWidget(self.cards_scroll, 1)
        outer.addLayout(mods_and_footer, 1)

        footer_group = QVBoxLayout()
        footer_group.setContentsMargins(0, 0, 20, 0)
        footer_group.setSpacing(8)
        review_row = QHBoxLayout()
        self.update_control = QWidget()
        self.update_control.setObjectName("modContent")
        update_row = QHBoxLayout(self.update_control)
        update_row.setContentsMargins(0, 0, 0, 0)
        update_row.setSpacing(12)
        self.update_label = label("")
        self.update_label.setTextFormat(Qt.TextFormat.PlainText)
        update_row.addWidget(self.update_label)
        self.update_toggle = Switch()
        self.update_toggle.setAccessibleName("Update outdated mods")
        self.update_toggle.toggled.connect(self.stage_update)
        update_row.addWidget(self.update_toggle)
        review_row.addWidget(self.update_control)
        review_row.addStretch()
        self.preview_button = button("Review changes", self.preview)
        self.preview_button.setObjectName("textOnly")
        self.preview_button.setToolTip("Review all staged printer-file changes before applying")
        review_row.addWidget(self.preview_button)
        footer_group.addLayout(review_row)
        footer = QHBoxLayout()
        self.support_button = button("SUPPORT DEVELOPMENT", self.support_development)
        self.support_button.setIcon(QIcon(str(Path(__file__).resolve().parent / "assets" / "icons" / "material_favorite_white_18.png")))
        self.support_button.setIconSize(QSize(18, 18))
        self.support_button.setObjectName("support")
        self.support_button.setAccessibleName("Support development")
        self.support_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.support_button.setToolTip(SUPPORT_URL)
        footer.addWidget(self.support_button)
        footer.addStretch()
        self.original_button = button("Restore original…", self.stage_restore)
        self.original_button.setObjectName("restore")
        self.original_button.setToolTip("Choose mod removal or restoration from original backups.")
        footer.addWidget(self.original_button)
        self.cancel_button = button("Cancel", self.cancel_staged)
        self.apply_button = ActivityButton("Apply", "Applying…", self.apply_clicked)
        self.apply_button.setObjectName("primary")
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.apply_button)
        footer_group.addLayout(footer)
        mods_and_footer.addLayout(footer_group)
        self.preview_button.ensurePolished()
        self.apply_button.ensurePolished()
        action_width = max(110, self.preview_button.sizeHint().width(),
                           self.apply_button.fontMetrics().horizontalAdvance("Fixing issues…") + 54)
        self.preview_button.setFixedWidth(action_width)
        self.apply_button.setFixedWidth(action_width)
        QApplication.instance().focusChanged.connect(self.focus_changed)
        QApplication.instance().installEventFilter(self)
        self.refresh()
        if start_polling:
            self.poll_timer.start(10000)
            self.probe_timer.start(0)

    def support_development(self):
        if not QDesktopServices.openUrl(QUrl(SUPPORT_URL)):
            QMessageBox.warning(self, "Support development", "Could not open the support page. Visit:\n\n" + SUPPORT_URL)

    def close_other_details(self, active, expanded):
        if expanded:
            for disclosure in self.details_buttons:
                if disclosure is not active:
                    disclosure.setChecked(False)

    def connection(self):
        if self.active_row is None:
            raise ValueError("Connect to a saved printer first.")
        return self.active_row.connection()

    def save_settings(self):
        settings.update(self.settings_path, printers=[row.values() for row in self.printer_rows if row.host.text().strip()])
        self.settings_dirty = False

    def persist_settings(self):
        self.settings_dirty = True
        try:
            self.save_settings()
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Printer list not saved", str(error))

    def update_printer_list(self):
        self.empty_printers.setVisible(not self.printer_rows)
        visible_rows = len(self.printer_rows)
        height = visible_rows * PRINTER_ROW_HEIGHT + max(0, visible_rows - 1) * self.printer_layout.spacing()
        self.printer_list.setFixedHeight(max(38, height))
        self.find_button.setObjectName("" if any(row.host.text().strip() for row in self.printer_rows) else "primary")
        self.find_button.style().unpolish(self.find_button)
        self.find_button.style().polish(self.find_button)

    def add_printer(self, values, save=True):
        row = PrinterRow(values)
        row.refresh(False, self.busy)
        row.edited.connect(self.printer_edited)
        row.connect_requested.connect(self.connect_printer)
        row.disconnect_requested.connect(self.disconnect_printer)
        row.remove_requested.connect(self.remove_printer)
        self.printer_rows.append(row)
        self.printer_layout.addWidget(row)
        self.update_printer_list()
        self.printer_layout.activate()
        if save:
            self.persist_settings()
            self.refresh()
            if self.start_polling:
                self.probe_timer.start(0)
        return row

    def add_manual_printer(self):
        row = next((row for row in self.printer_rows if not row.host.text().strip()), None)
        if row is None:
            row = self.add_printer({"host": "", "username": "root"})
        row.host.setFocus()

    def focus_changed(self, old, new):
        QTimer.singleShot(0, self.discard_empty_printers)

    def eventFilter(self, widget, event):
        if not self.closing and event.type() == QEvent.Type.MouseButtonPress and isinstance(widget, QWidget):
            for row in list(self.printer_rows):
                if not row.host.text().strip() and widget is not row and not row.isAncestorOf(widget):
                    self.remove_printer(row, empty=True)
        return False

    def discard_empty_printers(self):
        if self.closing:
            return
        focused = QApplication.focusWidget()
        for row in list(self.printer_rows):
            if not row.host.text().strip() and focused is not row and not (focused and row.isAncestorOf(focused)):
                self.remove_printer(row, empty=True)

    def remove_printer(self, row, empty=False):
        if (self.busy and not empty) or row not in self.printer_rows:
            return
        index = self.printer_rows.index(row)
        self.printer_rows.remove(row)
        try:
            self.save_settings()
        except (OSError, ValueError) as error:
            self.printer_rows.insert(index, row)
            QMessageBox.warning(self, "Cannot remove saved printer", str(error))
            return
        self.save_timer.stop()
        if row is self.active_row:
            self.active_row = None
            self.connected = False
            self.ignored_mods.clear()
            self.snapshot = None
            self.snapshot_connection = None
            self.clear_pending()
            self.policy = "known"
            self.scan_progress("")
            self.files_status.set_files(())
            self.files_errors.hide()
            self.review_button.hide()
        self.reachability_generation += 1
        if self.discovery_task is not None:
            self.discovery_ignored.add(row.host.text().strip())
        self.printer_layout.removeWidget(row)
        row.hide()
        row.deleteLater()
        self.update_printer_list()
        self.refresh()

    def printer_edited(self, row):
        self.settings_dirty = True
        identity = (row.host.text().strip(), row.username.text().strip())
        if identity != row.identity:
            row.reachable = None
            if row is self.active_row:
                self.connected = False
                self.ignored_mods.clear()
                self.snapshot = None
                self.snapshot_connection = None
                self.clear_pending()
                self.scan_progress("Printer changed.")
            row.identity = identity
        if row is self.active_row and self.connected and self.snapshot:
            self.update_synced_status()
        self.save_timer.start(400)
        self.update_printer_list()
        if self.start_polling:
            self.probe_timer.start(600)
        self.refresh()

    def check_reachability(self):
        if self.busy or self.closing:
            return
        if self.reachability_task is not None:
            self.reachability_pending = True
            return
        hosts = [row.host.text().strip() for row in self.printer_rows]
        if not hosts:
            return
        task = Task(lambda progress: network.reachable_printers(hosts))
        self.reachability_task = task
        generation = self.reachability_generation
        task.signals.done.connect(lambda results: self.receive_reachability(results, generation))
        task.signals.error.connect(self.reachability_failed)
        self.pool.start(task)

    def reachability_failed(self, error):
        self.reachability_task = None
        if self.reachability_pending and not self.closing:
            self.reachability_pending = False
            self.probe_timer.start(0)

    def receive_reachability(self, results, generation=None):
        self.reachability_failed(None)
        # A probe begun before a scan/Apply must not invalidate that operation.
        if self.busy or (generation is not None and generation != self.reachability_generation):
            return
        for row in self.printer_rows:
            host = row.host.text().strip()
            if host in results:
                row.reachable = results[host]
                if row is self.active_row and self.connected and not row.reachable:
                    self.connected = False
                    self.ignored_mods.clear()
                    self.clear_pending()
                    self.scan_progress("Printer unreachable. Reconnect when it is available.")
        self.refresh()

    def connect_printer(self, row):
        if self.busy or not row.reachable:
            return
        try:
            connection = row.connection()
            self.save_settings()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Connection", str(error))
            return
        if row is not self.active_row:
            self.active_row = row
            self.policy = "known"
        self.connected = False
        self.ignored_mods.clear()
        self.clear_pending()
        self.snapshot = None
        self.snapshot_connection = None
        self.files_status.set_files(())
        self.files_errors.hide()
        self.review_button.hide()
        self.cards_scroll.scroll_animation.stop()
        self.cards_scroll.verticalScrollBar().setValue(0)
        self.run_task(lambda progress: engine.scan(connection, self.known_hosts, progress),
                      lambda result: self.receive_scan(result, connection), scan=True)

    def enable_scan_disconnect(self):
        if (not self.closing and self.busy and self.scanning and not self.connected
                and self.snapshot is None and self.active_task is not None):
            self.connect_cancel_ready = True
            self.refresh()

    def disconnect_printer(self, row=None):
        if row is not None and row is not self.active_row:
            return
        if self.busy:
            if not self.scanning or not self.connect_cancel_ready:
                return
            task, self.active_task = self.active_task, None
            if task is not None:
                task.cancelled.set()
            self.busy = self.scanning = False
            self.scan_button.set_running(False)
        self.disconnect_timer.stop()
        self.connect_cancel_ready = False
        self.connected = False
        self.ignored_mods.clear()
        self.clear_pending()
        self.scan_progress("Disconnected.")
        self.refresh()

    def run_task(self, function, done, *, scan=False):
        if self.busy or self.closing:
            return
        self.apply_waiting = None
        self.reachability_generation += 1
        self.busy = True
        self.scanning = scan
        self.disconnect_timer.stop()
        self.connect_cancel_ready = False
        if scan and not self.connected and self.snapshot is None:
            self.disconnect_timer.start()
        if scan:
            self.scan_button.set_running(True)
            self.scan_progress("Reading printer files…")
        else:
            self.apply_button.set_running(True)
        self.refresh()
        task = Task(function)
        self.active_task = task
        task_ref = weakref.ref(task)
        def current_task():
            active = task_ref()
            return (not self.closing and active is not None and self.active_task is active
                    and not active.cancelled.is_set())
        def progress(message):
            if current_task():
                (self.scan_progress if scan else self.apply_button.setToolTip)(message)
        task.signals.progress.connect(progress)
        def stop_progress():
            self.active_task = None
            self.busy = False
            self.scanning = False
            self.disconnect_timer.stop()
            self.connect_cancel_ready = False
            if scan:
                self.scan_button.set_running(False)
            else:
                self.apply_button.set_running(False)
        def finish(value):
            if not current_task():
                return
            stop_progress()
            done(value)
            self.refresh()
        def fail(error):
            if not current_task():
                return
            failed_task = task_ref()
            stop_progress()
            self.refresh()
            if isinstance(error, NewHostKey):
                reply = QMessageBox.question(self, "Trust this printer?", "This SSH key is not saved in this portable app yet.\n\n" + str(error) + "\n\nTrust it and continue?")
                if reply == QMessageBox.StandardButton.Yes:
                    trust_key(self.known_hosts, error)
                    self.run_task(function, done, scan=scan)
                elif scan:
                    self.scan_progress("Connection cancelled.")
                return
            if scan:
                self.scan_button.setText("Scan failed")
                self.scan_progress(operation_error_message(error))
            else:
                self.apply_button.setToolTip(operation_error_message(error))
            dialog = QMessageBox(QMessageBox.Icon.Warning, "Operation stopped",
                                operation_error_message(error), QMessageBox.StandardButton.Ok, self)
            dialog.setOption(QMessageBox.Option.DontUseNativeDialog, True)
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            context = "Last step: " + failed_task.last_progress if failed_task is not None and failed_task.last_progress else ""
            if not scan:
                context += ("\n\n" if context else "") + "Scan the printer to check its current state before applying again."
            dialog.setInformativeText(context)
            details = failed_task.error_details if failed_task is not None else ""
            dialog.setDetailedText(details or type(error).__name__ + ": " + repr(error))
            dialog.exec()
        task.signals.done.connect(finish)
        task.signals.error.connect(fail)
        if scan:
            # A blocked network read must not hold the app open on exit.
            threading.Thread(target=task.run, daemon=True, name="printer-scan").start()
        else:
            self.pool.start(task)

    def scan_clicked(self):
        if not self.connected:
            return
        try:
            connection = self.connection()
            self.save_settings()
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Connection", str(error))
            return
        ignored = tuple(sorted(self.ignored_mods))
        self.run_task(lambda progress: engine.scan(connection, self.known_hosts, progress, ignored_mods=ignored),
                      lambda result: self.receive_scan(result, connection), scan=True)

    def review_firmware_issues(self, result, connection):
        issues = ui_checks.check(result, "issues")
        if not issues:
            return False
        dialog = FirmwareIssuesDialog(issues, self)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                selected = dialog.reinstall_targets()
                unknown = any(status.get("unknown") for status in issues.values())
                removals = dialog.unknown_removals() if unknown else ()
                ignored = dialog.ignored_targets() if unknown else ()
                if ignored:
                    if (not self.connected or self.snapshot is not result or
                            self.snapshot_connection != (connection.host, connection.username)):
                        return True
                    self.ignored_mods.update(ignored)
                    ui_checks.set_ignored(result, self.ignored_mods)
                    issues = ui_checks.check(result, "issues")
                    self.refresh()
                if issues:
                    extra = {"unknown_removals": removals} if removals else {}
                    QTimer.singleShot(0, lambda: self.fix_firmware_issues(result, connection, issues,
                                                                         reinstall_targets=selected, **extra))
        finally:
            dialog.deleteLater()
        return True

    def fix_firmware_issues(self, scanned, connection, issues, *, reinstall_targets=None, unknown_removals=()):
        if self.busy or not self.connected or self.closing:
            return
        try:
            if self.snapshot is not scanned or self.snapshot_connection != (connection.host, connection.username):
                raise ValueError("The printer scan changed. Scan again before fixing issues.")
            relocation = issues.get(storage.ROOT)
            if relocation and not relocation["available"]:
                raise ValueError("Cannot fix backup storage: " + relocation["reason"])
            targets = {path: False for path, _, _ in MOD_CONTROLS
                       if issues.get(path, {}).get("present") and issues[path].get("available")}
            reinstall_targets = tuple(path for path in targets if issues[path].get("reinstall") and
                                      (reinstall_targets is None or path in reinstall_targets))
            if not targets and not unknown_removals and not issues.get(storage.ROOT, {}).get("available"):
                return
            options = {"desired": targets.get(mods.TARGET), "mesh_desired": targets.get(mesh.TARGET),
                       "temperature_desired": targets.get(mesh_temperature.TARGET),
                       "screws_desired": targets.get(screws.PYTHON), "smoothing_desired": targets.get(smoothing.TARGET),
                       "restore": False, "update": False, "full_risk": False}
            if warmup.TARGET in targets:
                options["warmup_desired"] = targets[warmup.TARGET]
            if kamp.TARGET in targets:
                options["kamp_desired"] = targets[kamp.TARGET]
            if pa.TARGET in targets:
                options["pa_desired"] = targets[pa.TARGET]
            extra = {"unknown_removals": unknown_removals} if unknown_removals else {}
            engine.prepare_all(engine.cleanup_snapshot(scanned) if scanned.storage_cleanup else scanned, **options, **extra)
        except (ValueError, TypeError, SyntaxError) as error:
            QMessageBox.warning(self, "Cannot fix issues", str(error))
            return

        def fixed(result):
            self.receive_scan(result["snapshot"], connection, review=False)
            self.apply_button.setToolTip(result["message"])
            if result.get("repair_error"):
                message = result["message"] + "\n\n" + result["repair_error"]
                if (result.get("written") or result.get("write_uncertain")) and result.get("power_cycle", True):
                    message += "\n\nPhysically power cycle the printer before printing."
                QMessageBox.warning(self, "Reinstallation incomplete", message)
                return
            if result.get("written"):
                if result.get("power_cycle", True):
                    self.show_write_success()
                else:
                    QMessageBox.information(self, "Write successful", result["message"])

        # Repair excludes pending user edits, which remain available for Apply.
        if reinstall_targets or unknown_removals:
            self.run_task(lambda progress: engine.fix_issues(connection, self.known_hosts, scanned,
                                                            progress=progress, reinstall_targets=reinstall_targets,
                                                            **extra), fixed)
        else:
            self.run_task(lambda progress: engine.apply(connection, self.known_hosts, scanned,
                                                       progress=progress, **options,
                                                       **({"retire_old_backups": True} if scanned.storage_cleanup else {}),
                                                       **({"cleanup_recovery": True} if scanned.recovery_cleanup else {})), fixed)

    def receive_scan(self, result, connection, review=True):
        self.connected = True
        if self.active_row is not None:
            self.active_row.reachable = True
        self.snapshot = result
        self.snapshot_connection = (connection.host, connection.username)
        can_review = not network.compatibility_error(result.status) or network.is_untested(result.status)
        state = result.status
        reported_name = (state["info"].get("hostname") if state else None) or result.identity.get("hostname", "")
        if self.active_row is not None and not self.active_row.name.text().strip() and reported_name:
            self.active_row.name.setText(reported_name)
            self.persist_settings()
        self.update_synced_status()
        statuses, errors = [], []
        for module, value in engine.file_entries(result):
            if value:
                filename = module.TARGET.rsplit('/', 1)[-1]
                state_label = value.analysis['label']
                if module is mesh and result.screws and result.screws.helper_enabled and value.analysis["known"]:
                    state_label = "Uncursed"
                if module.TARGET != screws.LEGACY_CONFIG or value.data is not None:
                    statuses.append((filename, state_label))
                if value.backup_error:
                    errors.append(filename + " · Original needs attention: " + value.backup_error)
        if result.screws and result.screws.backup_error:
            errors.append("Bed screw helper · " + result.screws.backup_error)
        if result.storage_error:
            errors.append(result.storage_error)
        else:
            obsolete = {entry["path"] for pair in getattr(result, "storage_cleanup", ())
                        for entry in pair.get("files", ())}
            if any(move["source"] not in obsolete for move in result.storage_moves):
                errors.append("Current backups and uninstall records will move to /mnt/UDISK/Uncurser/ on Apply.")
        self.files_status.set_files(statuses)
        self.files_errors.setText("\n".join(errors))
        self.files_errors.setVisible(bool(errors))
        unknown = any(value and not value.analysis["known"] for module, value in engine.file_entries(result))
        self.review_button.setVisible(unknown and can_review)
        self.refresh()
        if review and self.review_firmware_issues(result, connection):
            return
        if review and result.recovery:
            if result.recovery.get("available"):
                self.repair_startup()
            else:
                dialog = QMessageBox(QMessageBox.Icon.Warning, "Cannot fix issues",
                                    result.recovery["message"], QMessageBox.StandardButton.Ok, self)
                dialog.setOption(QMessageBox.Option.DontUseNativeDialog, True)
                dialog.setTextFormat(Qt.TextFormat.PlainText)
                dialog.exec()
        elif unknown and review and can_review:
            self.review_compatibility()

    def repair_startup(self):
        if (self.busy or not self.connected or self.snapshot is None
                or not self.snapshot.recovery or not self.snapshot.recovery.get("available")):
            return
        try:
            connection = self.connection()
            if self.snapshot_connection != (connection.host, connection.username):
                raise ValueError("Scan the selected printer before fixing issues.")
        except (ValueError, OSError) as error:
            QMessageBox.warning(self, "Cannot fix issues", str(error))
            return
        scanned = self.snapshot.recovery
        dialog = QMessageBox(QMessageBox.Icon.Warning, "Fix issues",
                            scanned["message"], QMessageBox.StandardButton.Cancel, self)
        dialog.setOption(QMessageBox.Option.DontUseNativeDialog, True)
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        repair_button = dialog.addButton("Fix issues", QMessageBox.ButtonRole.AcceptRole)
        repair_button.setObjectName("fixIssues")
        dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
        dialog.exec()
        if dialog.clickedButton() is not repair_button:
            return

        ignored = tuple(sorted(self.ignored_mods))
        def repair(progress):
            result = recovery.repair(connection, self.known_hosts, scanned, progress)
            result["snapshot"] = engine.scan(connection, self.known_hosts, progress, ignored_mods=ignored)
            return result

        def repaired(result):
            self.receive_scan(result["snapshot"], connection, review=False)
            if result.get("written"):
                self.show_write_success()

        self.run_task(repair, repaired)

    def scan_progress(self, message):
        self.scan_button.setToolTip(message)
        self.scan_button.setAccessibleDescription(message)

    def update_synced_status(self):
        host = self.snapshot_connection[0]
        name = self.active_row.name.text().strip() if self.active_row is not None else ""
        stamp = datetime.datetime.fromtimestamp(self.snapshot.scanned_at).strftime("%H:%M:%S")
        self.scan_button.setText(self.scan_button.idle_text)
        self.scan_progress((f"{name}  ·  " if name else "") + f"{host}  ·  Scanned {stamp}"
                           + ("  ·  Status unavailable" if not self.snapshot.status else ""))

    def review_compatibility(self):
        if not self.snapshot or (network.compatibility_error(self.snapshot.status) and not network.is_untested(self.snapshot.status)):
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Unknown printer file")
        dialog.setMinimumWidth(600)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(label("The printer files don’t match the known versions this app supports.", "section", True))
        for module, value in engine.file_entries(self.snapshot):
            if value and not value.analysis["known"]:
                layout.addWidget(label(module.TARGET.rsplit("/", 1)[-1] + "\n" + value.analysis["reason"], wrap=True))
        layout.addWidget(label("Known versions only enables tools whose affected files are recognized. "
                               "Full risk allows tools on unknown files, but ambiguous edits and damaged originals still stop Apply.", "muted", True))
        row = QHBoxLayout()
        row.setSpacing(12)
        cancel = button("Cancel")
        known = button("Proceed with known versions only", primary=True)
        row.addWidget(cancel); row.addWidget(known)
        layout.addLayout(row)
        risk = button("Proceed with full risk")
        risk.setObjectName("risk")
        layout.addWidget(risk, alignment=Qt.AlignmentFlag.AlignRight)
        def choose(policy):
            self.policy = policy
            dialog.accept()
        cancel.clicked.connect(lambda: choose("cancel"))
        known.clicked.connect(lambda: choose("known"))
        risk.clicked.connect(lambda: choose("all"))
        dialog.exec()
        self.refresh()

    def stage_toggle(self, value):
        self.stage_mod_toggle(mods.TARGET, "staged", value)

    def stage_mesh_toggle(self, value):
        self.stage_mod_toggle(mesh.TARGET, "mesh_staged", value)

    def stage_mod_toggle(self, path, field, value):
        if self.snapshot is None or not self.connected or self.busy:
            return
        if ui_checks.check(self.snapshot, "issues") or self.snapshot.recovery:
            return
        status = ui_checks.check(self.snapshot, "removals").get(path, {})
        if network.is_untested(self.snapshot.status) or status.get("recovery"):
            if network.compatibility_error(self.snapshot.status) and not network.is_untested(self.snapshot.status):
                return
            if status.get("present"):
                if not status.get("available"):
                    return
                desired = None if value else False
            else:
                if not self.can_install_mod(engine.mod_snapshot(self.snapshot, path)):
                    return
                desired = True if value else None
        else:
            if engine.mod_snapshot(self.snapshot, path) is None:
                return
            desired = bool(value)
        self.restore_pending = False
        setattr(self, field, desired)
        self.refresh()

    def can_install_mod(self, value):
        return bool(value and value.analysis.get("enabled") is False and value.analysis.get("patchable")
                    and not value.backup_error and (self.policy == "all"
                    or (self.policy == "known" and value.analysis.get("known"))))

    def clear_pending(self, *, update_enabled=True):
        self.apply_waiting = None
        self.plan_timer.stop()
        self.plan_key = self.plan_request = self.plan_result = None
        if self.plan_task is not None:
            self.plan_task.cancelled.set()
            self.plan_task = None
        self.staged = None
        self.mesh_staged = None
        self.temperature_staged = None
        self.screws_staged = None
        self.smoothing_staged = None
        self.warmup_staged = None
        self.kamp_staged = None
        self.pa_staged = None
        self.restore_pending = False
        self.update_enabled = update_enabled
        self.update_active = False
        self.automatic_update_error = ""

    def cancel_staged(self):
        self.clear_pending(update_enabled=False)
        self.refresh()

    def stage_update(self, value):
        if not self.connected or self.busy or self.snapshot is None or self.restore_pending:
            return
        if ui_checks.check(self.snapshot, "issues") or self.snapshot.recovery:
            return
        self.update_enabled = bool(value)
        self.refresh()

    def stage_restore(self):
        if self.snapshot is None or not self.connected or self.busy:
            return
        scanned, connection = self.snapshot, self.snapshot_connection
        targets, reason = remove_all_targets(scanned)
        paths = ui_checks.check(scanned, ("restore", self.policy == "all"))
        if not targets and not paths:
            return
        self.apply_waiting = None
        dialog = RestoreChoiceDialog(reason, bool(paths), self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            if (not self.connected or self.busy or self.snapshot is not scanned
                    or self.snapshot_connection != connection):
                return
            if dialog.choice not in ("remove", "restore"):
                return
            if (dialog.choice == "remove" and not targets) or (dialog.choice == "restore" and not paths):
                return
            self.clear_pending(update_enabled=False)
            if dialog.choice == "remove":
                for path, field, _ in MOD_CONTROLS:
                    if path in targets:
                        setattr(self, field, False)
            else:
                self.restore_pending = True
            self.refresh()
        finally:
            dialog.deleteLater()

    def stage_temperature_toggle(self, value):
        self.stage_mod_toggle(mesh_temperature.TARGET, "temperature_staged", value)

    def stage_screws_toggle(self, value):
        self.stage_mod_toggle(screws.PYTHON, "screws_staged", value)

    def stage_smoothing_toggle(self, value):
        self.stage_mod_toggle(smoothing.TARGET, "smoothing_staged", value)

    def stage_warmup_toggle(self, value):
        self.stage_mod_toggle(warmup.TARGET, "warmup_staged", value)

    def stage_kamp_toggle(self, value):
        self.stage_mod_toggle(kamp.TARGET, "kamp_staged", value)

    def stage_pa_toggle(self, value):
        self.stage_mod_toggle(pa.TARGET, "pa_staged", value)

    def show_orca_gcode(self):
        dialog = OrcaGcodeDialog(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    def pending_plan(self, choices, needed):
        scope = (id(self.snapshot), self.snapshot_connection, self.policy, self.restore_pending)
        if self.apply_waiting != scope:
            self.apply_waiting = None
        if not needed or not self.connected or self.snapshot is None:
            self.apply_waiting = None
            self.plan_key = self.plan_request = self.plan_result = None
            self.plan_timer.stop()
            return None
        options = {"desired": self.staged, "mesh_desired": self.mesh_staged,
                   "temperature_desired": self.temperature_staged, "screws_desired": self.screws_staged,
                   "smoothing_desired": self.smoothing_staged, "restore": self.restore_pending,
                   "full_risk": self.policy == "all", "update": self.update_active}
        if self.warmup_staged is not None:
            options["warmup_desired"] = self.warmup_staged
        if self.kamp_staged is not None:
            options["kamp_desired"] = self.kamp_staged
        if self.pa_staged is not None:
            options["pa_desired"] = self.pa_staged
        key = (id(self.snapshot), self.snapshot_connection, self.policy, tuple(options.items()))
        if key != self.plan_key:
            self.plan_key, self.plan_result = key, None
            self.plan_request = (key, self.snapshot, options, choices.copy())
        if self.plan_result is None and self.plan_task is None and not self.busy and not self.plan_timer.isActive():
            self.plan_timer.start(35)
        return self.plan_result

    def validate_pending(self):
        if self.closing or self.busy or self.plan_task is not None or self.plan_request is None:
            return
        key, scanned, options, choices = self.plan_request

        def validate(progress):
            required = set()
            storage_changed = False
            storage_error = scanned.storage_error
            try:
                view, retire, moves = engine.storage_plan(scanned, choices, options["restore"])
                storage_changed = bool(retire or moves)
                storage_error = view.storage_error
                if not options["restore"] and any(choice is True for choice in choices.values()):
                    required = engine.automatic_updates(view, choices)
                engine.prepare_all(view, **options)
            except (ValueError, SyntaxError, TypeError, UnicodeError) as error:
                return {"mandatory": required, "error": operation_error_message(error), "storage_changed": storage_changed,
                        "storage_error": storage_error}
            return {"mandatory": required, "error": "", "storage_changed": storage_changed, "storage_error": storage_error}

        task = Task(validate)
        self.plan_task = task
        task_ref = weakref.ref(task)

        def finished(result):
            completed_task = task_ref()
            if completed_task is None or self.plan_task is not completed_task:
                return
            self.plan_task = None
            if self.closing:
                return
            if key == self.plan_key:
                self.plan_result = result
            self.refresh()
            if key == self.plan_key and self.apply_waiting is not None:
                QTimer.singleShot(0, self.resume_apply)

        task.signals.done.connect(finished)
        task.signals.error.connect(lambda error: finished({"mandatory": set(), "error": operation_error_message(error),
                                                         "storage_changed": False, "storage_error": scanned.storage_error}))
        # Local planning is read-only and must not block editing or closing.
        threading.Thread(target=task.run, daemon=True, name="pending-changes").start()

    def refresh(self):
        snapshot = self.snapshot
        untested = bool(snapshot and network.is_untested(snapshot.status))
        removals = ui_checks.check(snapshot, "removals")
        issues = ui_checks.check(snapshot, "issues")
        issues_found = bool(issues or (snapshot and snapshot.recovery))
        for path, field, name in MOD_CONTROLS:
            value = engine.mod_snapshot(snapshot, path) if snapshot else None
            removal = removals.get(path, {})
            applied = removal.get("present") if untested or removal.get("recovery") else value.analysis["enabled"] if value else None
            staged = getattr(self, field)
            if (not issues_found and applied is not None and staged is not None and staged == applied
                    and not (staged is False and removal.get("present") and removal.get("available"))):
                setattr(self, field, None)
        device_error = network.compatibility_error(snapshot.status if snapshot else None)
        supported = snapshot is not None and not device_error
        if snapshot:
            model, firmware = network.device_identity(snapshot.status)
            model = network.SUPPORTED_MODEL_NAME if model == network.SUPPORTED_MODEL else model or "Model unavailable"
            self.printer_heading.setText(f"{model} · Firmware {firmware or 'unavailable'}")
        else:
            self.printer_heading.setText("Reading printer details…")
        self.compatibility_status.setText(device_error)
        self.compatibility_status.setVisible(bool(snapshot and device_error))
        self.compatibility_status.setToolTip(snapshot.status_error if snapshot else "")
        restore_paths = ui_checks.check(snapshot, ("restore", self.policy == "all")) if self.restore_pending else set()
        choices = {mods.TARGET: self.staged, mesh.TARGET: self.mesh_staged,
                   mesh_temperature.TARGET: self.temperature_staged, screws.PYTHON: self.screws_staged,
                   smoothing.TARGET: self.smoothing_staged, warmup.TARGET: self.warmup_staged,
                   kamp.TARGET: self.kamp_staged, pa.TARGET: self.pa_staged}
        update_statuses = ui_checks.check(snapshot, "updates")
        outdated_paths = engine.outdated_mods(snapshot, update_statuses, issues)
        update_targets = {path for path in outdated_paths if choices.get(path) is not False} if not self.restore_pending else set()
        self.update_active = bool(self.connected and self.update_enabled and update_targets)
        update_paths = set(update_targets) if self.update_active else set()
        update_error = ""
        needs_plan = bool(snapshot and not issues_found and (self.restore_pending or self.update_active or snapshot.storage_moves
                                       or getattr(snapshot, "storage_cleanup", ())
                                       or any(choice is not None for choice in choices.values())))
        plan = self.pending_plan(choices, needs_plan)
        mandatory_updates = plan["mandatory"] if plan else set()
        update_paths.update(mandatory_updates)
        if plan and plan["error"]:
            update_error = plan["error"]
        plan_ready = not needs_plan or plan is not None
        storage_error = snapshot.storage_error if snapshot else ""
        if needs_plan and plan is None:
            # Apply waits for this plan before any write can begin.
            storage_error = ""
        elif plan is not None:
            storage_error = plan.get("storage_error", storage_error)
        if (storage_error == engine.INCLUDE_STORAGE_ERROR and self.screws_staged is False
                and removals.get(screws.PYTHON, {}).get("available") and not self.restore_pending
                and not self.update_active and not any(choice is True for choice in choices.values())):
            storage_error = ""
        any_changed = bool(snapshot and (snapshot.storage_moves or getattr(snapshot, "storage_cleanup", ())))
        if plan is not None:
            any_changed = plan.get("storage_changed", False)
        permitted = self.connected and (supported or untested) and self.policy != "cancel" and not update_error and not storage_error
        if untested and self.restore_pending:
            permitted = False
        entries = (
            (mods.TARGET, snapshot, self.staged, self.toggle, self.pending_label,
             self.mod_card, self.mod_opacity),
            (mesh.TARGET, snapshot.mesh if snapshot else None, self.mesh_staged, self.mesh_toggle,
             self.mesh_pending, self.mesh_card, self.mesh_opacity),
            (mesh_temperature.TARGET, snapshot.mesh_temperature if snapshot else None, self.temperature_staged,
             self.temperature_toggle, self.temperature_pending, self.temperature_card, self.temperature_opacity),
            (screws.PYTHON, snapshot.screws if snapshot else None, self.screws_staged,
             self.screws_toggle, self.screws_pending, self.screws_card, self.screws_opacity),
            (smoothing.TARGET, engine.mod_snapshot(snapshot, smoothing.TARGET) if snapshot else None, self.smoothing_staged,
             self.smoothing_toggle, self.smoothing_pending, self.smoothing_card, self.smoothing_opacity),
            (warmup.TARGET, engine.mod_snapshot(snapshot, warmup.TARGET) if snapshot else None, self.warmup_staged,
             self.warmup_toggle, self.warmup_pending, self.warmup_card, self.warmup_opacity),
            (kamp.TARGET, engine.mod_snapshot(snapshot, kamp.TARGET) if snapshot else None, self.kamp_staged,
             self.kamp_toggle, self.kamp_pending, self.kamp_card, self.kamp_opacity),
            (pa.TARGET, engine.mod_snapshot(snapshot, pa.TARGET) if snapshot else None, self.pa_staged,
              self.pa_toggle, self.pa_pending, self.pa_card, self.pa_opacity),
        )
        for path, value, staged, toggle, pending, panel, opacity in entries:
            removal = removals.get(path, {})
            recovery_mode = bool(removal.get("recovery") and removal.get("present"))
            maintenance = untested or recovery_mode
            applied = removal.get("present", False) if maintenance else value.analysis["enabled"] if value else None
            removable = bool(removal.get("available") and applied)
            updatable = bool(untested and applied and value and value.analysis.get("enabled") is True
                             and not removal.get("recovery")
                             and value.analysis.get("patchable") and not value.backup_error
                             and update_statuses.get(path, {}).get("available")
                             and (self.policy == "all" or (self.policy == "known" and value.analysis.get("known"))))
            if recovery_mode:
                access = bool((supported or untested) and removable)
            else:
                access = (removable or updatable if applied else self.can_install_mod(value)) if untested else bool(
                    supported and value and (self.policy == "all" or (self.policy == "known" and value.analysis["known"])))
            toggle.applied = applied is True
            toggle.blockSignals(True)
            toggle.setChecked(staged if staged is not None else applied is True)
            toggle.blockSignals(False)
            toggle.update()
            changed = (path in restore_paths or (path in update_paths and staged is not False)
                       or (staged is not None and staged != applied)
                       or (staged is False and removal.get("present") and removal.get("available")))
            any_changed |= changed
            pending.setVisible(bool(changed))
            pending.setToolTip("Restore the initial on-printer original" if path in restore_paths else
                               "Update to latest" if path in update_paths and staged is not False else "Staged change")
            dependency_removal = bool(applied and removable and value and value.analysis.get("dependency_error"))
            allowed = bool(self.connected and access and (removable if maintenance and applied else
                           maintenance or dependency_removal or (value.analysis.get("patchable", False) and not value.backup_error)))
            if changed:
                change_allowed = (bool(removal.get("present") and removal.get("available")) if staged is False else
                                  bool(self.connected and updatable) if maintenance and path in update_paths else allowed)
                permitted &= bool(self.connected and path in restore_paths) if self.restore_pending else change_allowed
            panel.setEnabled(bool(self.connected and access and not self.busy))
            opacity.setOpacity(1 if self.connected and access else 0.45)
            panel.setToolTip(removal.get("reason", "") if maintenance else "")
            toggle.setEnabled(allowed and not self.busy and not self.restore_pending and not issues_found)
            if path in self.mod_version_rows:
                files, text = self.mod_version_rows[path]
                version = ui_checks.check(snapshot, ("version", path)) if snapshot and applied is True else ""
                files.setText(text + (" · " + version if version else ""))
                files.set_outdated(path in outdated_paths)
        self.heat_details.setEnabled(self.connected)
        self.mesh_details.setEnabled(self.connected)
        self.temperature_details.setEnabled(self.connected)
        self.screws_details.setEnabled(self.connected)
        self.smoothing_details.setEnabled(self.connected)
        self.warmup_details.setEnabled(self.connected)
        self.kamp_details.setEnabled(self.connected)
        self.pa_details.setEnabled(self.connected)
        for widget in (self.heat_expand, self.mesh_expand, self.temperature_expand,
                       self.heat_body, self.mesh_body, self.temperature_body, self.screws_expand, self.screws_body,
                       self.smoothing_expand, self.smoothing_body, self.warmup_expand, self.warmup_body,
                       self.kamp_expand, self.kamp_body, self.pa_expand, self.pa_body):
            widget.setEnabled(self.connected)
        for row in self.printer_rows:
            connecting = bool(row is self.active_row and self.scanning and not self.connected and snapshot is None)
            row.refresh(row is self.active_row and self.connected, self.busy,
                        connecting=connecting, can_disconnect=connecting and self.connect_cancel_ready)
        self.find_button.setEnabled(self.discovery_task is None)
        self.scan_card.setVisible(self.connected or self.scanning)
        initial_scan = self.scanning and snapshot is None
        self.scan_loading.setVisible(initial_scan)
        self.scan_content.setVisible(not initial_scan)
        for widget in (self.add_button, self.review_button):
            widget.setEnabled(not self.busy)
        self.scan_button.setEnabled(self.connected and not self.busy)
        repair_available = bool(snapshot and snapshot.recovery and snapshot.recovery.get("available"))
        self.repair_button.setVisible(repair_available)
        self.repair_button.setEnabled(self.connected and not self.busy and repair_available)
        count = len(update_targets)
        self.update_label.setText(f"Update {count} outdated {'mod' if count == 1 else 'mods'}")
        self.update_toggle.setAccessibleName(self.update_label.text())
        self.update_control.setVisible(bool(self.connected and update_targets))
        self.update_toggle.blockSignals(True)
        self.update_toggle.setChecked(bool(self.update_enabled or mandatory_updates))
        self.update_toggle.blockSignals(False)
        self.update_toggle.setEnabled(bool((supported or untested) and self.connected and self.policy != "cancel" and update_targets
                                           and not self.busy and not mandatory_updates and not issues_found))
        self.update_control.setToolTip("Updates are required when enabling another mod." if mandatory_updates
            else "Update active outdated mods in the same Apply batch; mods turned off will be removed instead.")
        self.cancel_button.setEnabled(not self.busy and (self.staged is not None or self.mesh_staged is not None or self.temperature_staged is not None or self.screws_staged is not None or self.smoothing_staged is not None or self.warmup_staged is not None or self.kamp_staged is not None or self.pa_staged is not None or self.restore_pending or self.update_active))
        self.original_button.setEnabled(bool(self.connected and snapshot and not self.busy
                                             and (ui_checks.check(snapshot, ("restore", self.policy == "all"))
                                                  or remove_all_targets(snapshot)[0])))
        updates_only = bool(update_paths) and not self.restore_pending and all(value is None for value in choices.values())
        action_name = "fixIssuesAction" if issues_found else "primary"
        if self.apply_button.objectName() != action_name:
            self.apply_button.setObjectName(action_name)
            self.apply_button.style().unpolish(self.apply_button)
            self.apply_button.style().polish(self.apply_button)
            self.apply_button.update()
        self.apply_button.idle_text = "Fix issues" if issues_found else "Update" if updates_only else "Apply"
        self.apply_button.active_text = "Fixing issues…" if issues_found else "Updating…" if updates_only else "Applying…"
        self.apply_button.setText(self.apply_button.active_text if self.apply_button.running else self.apply_button.idle_text)
        self.apply_button.setEnabled(bool(self.connected and not self.busy) if issues_found else bool(permitted and any_changed and not self.busy))
        self.preview_button.setEnabled(not issues_found and self.apply_button.isEnabled() and plan_ready)
        if not self.apply_button.isEnabled():
            self.apply_waiting = None
        apply_error = ("Review detected issues before changing mods." if issues_found else
                       update_error or storage_error or ("Checking changes…" if needs_plan and not plan_ready else ""))
        if apply_error or self.automatic_update_error:
            self.apply_button.setToolTip(apply_error)
            self.preview_button.setToolTip(apply_error or "Review all staged printer-file changes before applying")
        self.automatic_update_error = apply_error

    def preview(self):
        try:
            diff = engine.difference_all(self.snapshot, self.staged, self.mesh_staged, self.restore_pending, self.policy == "all", self.temperature_staged, self.screws_staged, update=self.update_active, smoothing_desired=self.smoothing_staged,
                                         **({"warmup_desired": self.warmup_staged} if self.warmup_staged is not None else {}),
                                         **({"kamp_desired": self.kamp_staged} if self.kamp_staged is not None else {}),
                                         **({"pa_desired": self.pa_staged} if self.pa_staged is not None else {}))
        except (ValueError, SyntaxError) as error:
            QMessageBox.warning(self, "Cannot prepare changes", str(error))
            return
        self.show_diff("Pending printer changes", "Nothing is written until you press Apply.", diff)

    def show_mod_code(self, path):
        if not self.connected:
            return
        name = {mods.TARGET: "Heat before homing & calibration", mesh.TARGET: "Speed up Bed mesh",
                mesh_temperature.TARGET: "Mesh at print temperature", screws.MOD_ID: "Bed screw adjustment helper",
                smoothing.TARGET: "Speed up homing", warmup.TARGET: "Don’t delay chamber heating",
                 kamp.TARGET: "Adaptive priming (KAMP-style)", pa.TARGET: "Fix Adaptive PA"}[path]
        try:
            diff, baseline = engine.initial_difference(self.snapshot, path)
        except (ValueError, SyntaxError, UnicodeError) as error:
            diff, baseline = str(error), "The original-to-enabled comparison is unavailable."
        self.show_diff(name + " · Original → enabled", baseline, diff)

    def show_diff(self, title, description, diff):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(960, 700)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(18)
        layout.addWidget(label(description, "muted", True))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        cursor = QTextCursor(view.document())
        remaining = 0
        for line in diff.splitlines(keepends=True):
            color = "#ededed"
            if remaining and line[:1] in (" ", "+", "-"):
                marker, line = line[0], line[1:]
                remaining -= 2 if marker == " " else 1
                color = {"-": "#ff0000", "+": "#00ff00", " ": color}[marker]
            else:
                hunk = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
                if hunk:
                    remaining = int(hunk[1] or 1) + int(hunk[2] or 1)
                    color = "#999999"
                elif line.startswith(("--- ", "+++ ")):
                    line, color = line[4:], "#999999"
            text_format = QTextCharFormat()
            text_format.setForeground(QColor(color))
            cursor.insertText(line, text_format)
        layout.addWidget(view)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def resume_apply(self):
        scope = (id(self.snapshot), self.snapshot_connection, self.policy, self.restore_pending)
        if self.apply_waiting == scope and self.plan_result is not None:
            self.apply_waiting = None
            if (not self.plan_result["error"] and not ui_checks.check(self.snapshot, "issues")
                    and not getattr(self.snapshot, "recovery", None)):
                self.apply_clicked()

    def apply_clicked(self):
        if self.snapshot is None or not self.connected or self.closing or self.busy or not self.apply_button.isEnabled():
            return
        issues = ui_checks.check(self.snapshot, "issues")
        if issues or self.snapshot.recovery:
            self.apply_waiting = None
            try:
                connection = self.connection()
                if self.snapshot_connection != (connection.host, connection.username):
                    raise ValueError("Scan the selected printer before fixing issues.")
            except (ValueError, SyntaxError) as error:
                QMessageBox.warning(self, "Cannot fix issues", str(error))
                return
            if issues:
                self.review_firmware_issues(self.snapshot, connection)
            else:
                self.repair_startup()
            return
        if self.plan_key is not None and self.plan_result is None:
            self.apply_waiting = (id(self.snapshot), self.snapshot_connection, self.policy, self.restore_pending)
            self.plan_timer.stop()
            self.validate_pending()
            return
        self.apply_waiting = None
        try:
            connection = self.connection()
            if self.snapshot_connection != (connection.host, connection.username):
                raise ValueError("Scan the selected printer before Apply.")
        except (ValueError, SyntaxError) as error:
            QMessageBox.warning(self, "Cannot apply", str(error))
            return
        scanned, desired, restore, risk = self.snapshot, self.staged, self.restore_pending, self.policy == "all"
        mesh_desired = self.mesh_staged
        temperature_desired = self.temperature_staged
        screws_desired = self.screws_staged
        smoothing_desired = self.smoothing_staged
        warmup_desired = self.warmup_staged
        kamp_desired = self.kamp_staged
        pa_desired = self.pa_staged
        update = self.update_active
        def applied(result):
            self.staged = None
            self.mesh_staged = None
            self.temperature_staged = None
            self.screws_staged = None
            self.smoothing_staged = None
            self.warmup_staged = None
            self.kamp_staged = None
            self.pa_staged = None
            self.restore_pending = False
            self.receive_scan(result["snapshot"], connection, review=False)
            self.apply_button.setToolTip(result["message"])
            if result.get("written"):
                if result.get("power_cycle", True):
                    self.show_write_success()
                else:
                    QMessageBox.information(self, "Write successful", result["message"])
        self.run_task(lambda progress: engine.apply(connection, self.known_hosts, scanned, desired, restore, risk, progress,
                                                   mesh_desired=mesh_desired, temperature_desired=temperature_desired,
                                                   screws_desired=screws_desired, update=update,
                                                   smoothing_desired=smoothing_desired,
                                                   **({"warmup_desired": warmup_desired} if warmup_desired is not None else {}),
                                                    **({"kamp_desired": kamp_desired} if kamp_desired is not None else {}),
                                                    **({"pa_desired": pa_desired} if pa_desired is not None else {})), applied)

    def show_write_success(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Write successful")
        dialog.setStyleSheet("QLabel#powerCycle { font-size: 13pt; font-weight: 600; color: #ffffff; "
                            "background: #123450; border-radius: 0; padding: 4px 8px; }")
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(20)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        success = label("Write successful.")
        success.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(success)
        power_cycle = label("Physically power cycle the printer to apply the changes!", "powerCycle")
        power_cycle.setWordWrap(False)
        power_cycle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(power_cycle)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.setCenterButtons(True)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.exec()

    def find_printers(self):
        if self.discovery_task is not None:
            return
        networks = set()
        for interface in QNetworkInterface.allInterfaces():
            flags = interface.flags()
            if (not flags & QNetworkInterface.InterfaceFlag.IsUp
                    or not flags & QNetworkInterface.InterfaceFlag.IsRunning
                    or flags & QNetworkInterface.InterfaceFlag.IsLoopBack):
                continue
            for entry in interface.addressEntries():
                if entry.ip().protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                    continue
                address = entry.ip().toString()
                prefix = entry.prefixLength()
                if not 0 < prefix <= 32:
                    continue
                value = str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))
                networks.add(value)
        if not networks:
            QMessageBox.information(self, "Find printer", "No suitable local IPv4 network was found. Enter the printer IP manually.")
            return
        self.discovery_stop = threading.Event()
        self.discovery_ignored.clear()
        stop = self.discovery_stop
        task = Task(lambda progress: network.discover(networks, progress, task.signals.found.emit, stop))
        self.discovery_task = task
        task.signals.found.connect(self.receive_candidate)
        task.signals.progress.connect(self.search_progress)
        task.signals.done.connect(self.search_finished)
        task.signals.error.connect(self.search_failed)
        self.find_button.set_running(True)
        self.search_progress("Searching all connected IPv4 networks…")
        self.refresh()
        self.pool.start(task)

    def receive_candidate(self, candidate):
        if self.closing:
            return
        host, name = candidate
        if host in self.discovery_ignored:
            return
        if not any(row.host.text().strip() == host for row in self.printer_rows):
            row = self.add_printer({"name": name, "host": host, "username": "root"}, save=False)
            row.host.setToolTip(name)
            self.persist_settings()
        self.refresh()
        if self.start_polling:
            self.probe_timer.start(0)

    def search_progress(self, message):
        if not self.closing:
            self.find_button.setToolTip(message)
            self.find_button.setAccessibleDescription(message)

    def search_finished(self, results):
        if self.closing:
            return
        self.discovery_task = None
        self.find_button.set_running(False)
        self.search_progress(f"Search complete · {len(results)} printer(s) found")
        self.empty_printers.setText("No printers found. Add one below or search again." if not results
                                    else "No saved printers. Find a printer or add one.")
        self.refresh()

    def search_failed(self, error):
        self.search_finished([])
        self.search_progress("Search stopped: " + str(error))

    def closeEvent(self, event):
        if self.busy and not self.scanning:
            QMessageBox.information(self, "Write in progress", "Wait for the printer files to finish writing before closing the app.")
            event.ignore()
        elif self.staged is not None or self.mesh_staged is not None or self.temperature_staged is not None or self.screws_staged is not None or self.smoothing_staged is not None or self.warmup_staged is not None or self.kamp_staged is not None or self.pa_staged is not None or self.restore_pending:
            dialog = QMessageBox(QMessageBox.Icon.Question, "Discard pending edits?",
                                "Close without applying your pending edits?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
            dialog.setOption(QMessageBox.Option.DontUseNativeDialog, True)
            dialog.setDefaultButton(QMessageBox.StandardButton.No)
            reply = dialog.exec()
            event.accept() if reply == QMessageBox.StandardButton.Yes else event.ignore()
        else:
            event.accept()
        if event.isAccepted():
            try:
                if self.settings_dirty:
                    self.save_settings()
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "Printer list not saved", str(error))
                event.ignore()
                return
            self.closing = True
            self.apply_waiting = None
            self.plan_timer.stop()
            if self.plan_task is not None:
                self.plan_task.cancelled.set()
            if self.scanning and self.active_task is not None:
                self.active_task.cancelled.set()
            self.discovery_stop.set()
            self.find_button.timer.stop()
            self.scan_button.timer.stop()
            self.apply_button.timer.stop()
            self.save_timer.stop()
            self.probe_timer.stop()
            self.poll_timer.stop()
            self.disconnect_timer.stop()
