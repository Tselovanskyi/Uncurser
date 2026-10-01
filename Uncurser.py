"""Portable entry point. All application-owned persistent data stays beside it."""

import logging
import logging.handlers
import os
from pathlib import Path
import sys


def main():
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    data = base / "data"
    try:
        data.mkdir(exist_ok=True)
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        os.environ["QT_SHADER_CACHE_PATH"] = str(data / "cache")
        sys.dont_write_bytecode = True
        handler = logging.handlers.RotatingFileHandler(data / "Uncurser.log", maxBytes=1024 * 1024, backupCount=1, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.basicConfig(level=logging.WARNING, handlers=[handler])
    except OSError as error:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, "Extract the entire app to a writable folder.\n\n" + str(error), "Uncurser", 16)
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
    window = Window(data, start_polling="--self-check" not in sys.argv)
    if "--self-check" in sys.argv:
        from PySide6.QtCore import Qt
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.show()
        app.processEvents()
        window.grab().save(str(data / "self-check.png"))
        import json
        (data / "self-check.json").write_text(json.dumps({"bundled": bool(getattr(sys, "frozen", False)), "ui": "ready"}), encoding="utf-8")
        window.close()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
