"""Local two-way machine-start G-code editor; no printer operations."""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QGridLayout,
                               QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from . import orca_gcode


class GcodeEditor(QPlainTextEdit):
    def __init__(self, placeholder, parent=None, *, read_only=False):
        super().__init__(parent)
        self.empty_label = QLabel(placeholder, self.viewport())
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_label.setStyleSheet("""
            background: transparent; color: #777777; font-family: 'Google Sans';
            font-size: 14pt; font-weight: 500;
        """)
        self.setReadOnly(read_only)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMinimumHeight(180)
        self.setStyleSheet("""
            QPlainTextEdit { background: #111111; color: #ededed; border: none;
                border-radius: 3px; padding: 12px; font-family: Consolas;
                font-size: 10pt; font-weight: 500; }
            QPlainTextEdit[readOnly="true"] { background: rgba(17, 17, 17, 96); }
            QScrollBar:horizontal { background: #0a0a0a; height: 8px; margin: 0; }
            QScrollBar::handle:horizontal { background: #333333; min-width: 24px; border-radius: 4px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
            QAbstractScrollArea::corner { background: transparent; }
        """)
        self.textChanged.connect(self.update_placeholder)
        self.update_placeholder()

    def update_placeholder(self):
        self.empty_label.setGeometry(self.viewport().rect())
        self.empty_label.setVisible(self.document().isEmpty())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.empty_label.setGeometry(self.viewport().rect())


class OrcaGcodeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("G-code for Orca")
        self.resize(960, 830)
        self.setMinimumSize(680, 620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        self.input = GcodeEditor("paste Orca's Machine Start G-code", self)
        self.output = GcodeEditor("Converted G-code will appear here", self, read_only=True)
        self.input_label = QLabel()
        self.output_label = QLabel()
        self.copy_button = QPushButton("Copy G-code")
        self.copy_button.setObjectName("primary")
        self.copy_button.setStyleSheet("font-size: 12pt; font-weight: 600; min-width: 200px; min-height: 48px; padding: 0;")
        self.copy_button.setAutoDefault(False)
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self.copy_gcode)
        for heading, field in ((self.input_label, self.input), (self.output_label, self.output)):
            heading.setStyleSheet("font-family: 'Google Sans'; font-size: 11pt; font-weight: 600;")
            layout.addWidget(heading)
            layout.addWidget(field, 1)
        self.set_labels()
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setStyleSheet("color: #ff5555; font-size: 11pt; font-weight: 500;")
        self.message.hide()
        layout.addWidget(self.message)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)
        footer = QGridLayout()
        footer.addWidget(self.copy_button, 0, 0, 1, 2, Qt.AlignmentFlag.AlignCenter)
        footer.addWidget(close, 0, 1, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(footer)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(175)
        self._timer.timeout.connect(self.convert)
        self.input.textChanged.connect(self.queue)

    def set_labels(self, kamp=None):
        titles = (("Machine Start G-code", "Converted Machine Start G-code") if kamp is None else
                  ("KAMP Machine Start G-code", "Original Machine Start G-code") if kamp else
                  ("Original Machine Start G-code", "KAMP Machine Start G-code"))
        self.copy_button.setText("Copy G-code" if kamp is None else
                                 "Copy original G-code" if kamp else "Copy KAMP G-code")
        for heading, field, title in zip((self.input_label, self.output_label),
                                        (self.input, self.output), titles):
            heading.setText(title)
            field.setAccessibleName(title)

    def queue(self):
        self.copy_button.setEnabled(False)
        self._timer.start()

    def convert(self):
        text = self.input.toPlainText()
        try:
            kamp = orca_gcode.is_adaptive(text) if text.strip() else None
            result = (orca_gcode.to_original(text) if kamp
                      else orca_gcode.to_adaptive(text)) if kamp is not None else ""
        except orca_gcode.ConversionError as error:
            self.set_labels()
            self.output.clear()
            self.copy_button.setEnabled(False)
            self.message.setText(str(error))
            self.message.show()
            return
        self.set_labels(kamp)
        self.message.clear()
        self.message.hide()
        if self.output.toPlainText() != result:
            self.output.setPlainText(result)
        self.copy_button.setEnabled(bool(result))

    def copy_gcode(self):
        if self.copy_button.isEnabled():
            QApplication.clipboard().setText(self.output.toPlainText())

    def done(self, result):
        self._timer.stop()
        super().done(result)
