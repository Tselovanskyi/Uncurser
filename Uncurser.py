"""Portable entry point. All application-owned persistent data stays beside it."""

import logging
import logging.handlers
import os
from pathlib import Path
import sys
import tempfile


def main():
    with tempfile.TemporaryDirectory(prefix="Uncurser-") as temporary:
        try:
            return run(Path(temporary))
        finally:
            logging.shutdown()


def run(runtime):
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    try:
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["QT_SHADER_CACHE_PATH"] = str(runtime / "cache")
        sys.dont_write_bytecode = True
        handler = logging.handlers.RotatingFileHandler(runtime / "Uncurser.log", maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.basicConfig(level=logging.WARNING, handlers=[handler])
    except OSError as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "Move Uncurser.exe to a writable folder.\n\n" + str(error), "Uncurser", 16)
        return 1
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app.appearance import configure
    from app.ui import Window
    app = QApplication(sys.argv)
    configure(app)
    app.setApplicationName("Uncurser")
    app.setOrganizationName("Uncurser")
    def unhandled(kind, error, traceback):
        logging.error("Unhandled error", exc_info=(kind, error, traceback))
        QMessageBox.critical(None, "Uncurser", str(error))
    sys.excepthook = unhandled
    try:
        window = Window(base, start_polling="--self-check" not in sys.argv)
    except (OSError, ValueError) as error:
        QMessageBox.critical(None, "Cannot open settings", str(error))
        return 1
    if "--self-check" in sys.argv:
        from PySide6.QtCore import Qt
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        app.processEvents()
        output = Path(os.environ.get("UNCURSER_CHECK_DIR", runtime))
        output.mkdir(parents=True, exist_ok=True)
        window.grab().save(str(output / "self-check.png"))
        import json
        (output / "self-check.json").write_text(json.dumps({"bundled": bool(getattr(sys, "frozen", False)), "ui": "ready"}), encoding="utf-8")
        window.close()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
