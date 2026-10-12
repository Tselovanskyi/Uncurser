"""Build-only typography check; no windows, images, or printer connections."""
import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def supported_weight(node):
    return (isinstance(node, ast.Constant) and node.value in (500, 600, 700)
            or isinstance(node, ast.Attribute) and node.attr in ("Medium", "DemiBold", "Bold"))


def check_sources():
    literals = []
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            location = f"{path.relative_to(ROOT)}:{getattr(node, 'lineno', 1)}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value)
                if re.search(r"(?:^|[;{\s])font\s*:", node.value):
                    raise ValueError(f"{location}: use explicit font properties, not font shorthand")
                for match in re.finditer(r"font-weight\s*:\s*([^\s;\"'<>}]*)", node.value):
                    if match[1] not in ("500", "600", "700"):
                        raise ValueError(f"{location}: unsupported font weight {match[1]}")
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in ("setStyleStrategy", "setHintingPreference"):
                raise ValueError(f"{location}: use native font rendering defaults")
            if name == "setBold" and (not node.args or not isinstance(node.args[0], ast.Constant)
                                      or node.args[0].value is not True):
                raise ValueError(f"{location}: font weight must be 500, 600 or 700")
            weight = None
            if name in ("setWeight", "setFontWeight"):
                weight = next((kw.value for kw in node.keywords if kw.arg == "weight"),
                              node.args[0] if node.args else None)
                if weight is None:
                    raise ValueError(f"{location}: font weight must be explicit")
            if name == "QFont":
                weight = next((kw.value for kw in node.keywords if kw.arg == "weight"),
                              node.args[2] if len(node.args) > 2 else None)
                if weight is None and (len(node.args) != 1 or not isinstance(node.args[0], ast.Call)
                                       or getattr(node.args[0].func, "attr", "") != "font"):
                    raise ValueError(f"{location}: specify a supported weight or copy an existing font()")
            if weight is not None and not supported_weight(weight):
                raise ValueError(f"{location}: font weight must be Medium/500, DemiBold/600 or Bold/700")
    return literals


def main():
    literals = check_sources()
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QFont, QFontInfo, QRawFont
    from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMainWindow, QPushButton
    from app.appearance import configure
    from app.ui import STYLE

    if tuple(map(int, qVersion().split("."))) < (6, 12, 0):
        raise RuntimeError("Font rendering requires Qt 6.12 or newer.")
    app = QApplication([])
    if app.platformName() != "windows":
        raise RuntimeError("Run the font check using the native Windows platform.")
    configure(app)
    window = QMainWindow()
    window.setStyleSheet(STYLE)
    header = QLabel(next(text for text in literals if text.startswith("UNCURSER <span")), window)
    header.setObjectName("title")
    button = QPushButton("Apply", window)
    button.setObjectName("primary")
    categories = []
    for object_name in ("section", "groupHeading", "cyan"):
        widget = QLabel("Print startup", window)
        widget.setObjectName(object_name)
        categories.append(widget)
    dialog = QDialog(window)
    dialog.setStyleSheet(next(text for text in literals if text.startswith("QLabel#powerCycle {")))
    body, banner = QLabel("Write successful.", dialog), QLabel("Physically power cycle the printer", dialog)
    banner.setObjectName("powerCycle")
    popup_button = QPushButton("OK", dialog)
    popup_button.setObjectName("primary")
    for widget in (window, header, button, dialog, body, banner, popup_button, *categories):
        widget.ensurePolished()
    app.processEvents()
    bold = QFont(app.font())
    bold.setWeight(QFont.Weight.Bold)
    semibold = QFont(app.font())
    semibold.setWeight(QFont.Weight.DemiBold)
    for name, font, weight in (("application", app.font(), 500), ("semibold", semibold, 600), ("bold", bold, 700),
                              ("header", header.font(), 700), ("button", button.font(), 600),
                              ("popup", body.font(), 500), ("power cycle", banner.font(), 600),
                              ("popup button", popup_button.font(), 600),
                              *((widget.objectName(), widget.font(), 600) for widget in categories)):
        info, raw = QFontInfo(font), QRawFont.fromFont(font)
        if (font.weight() != weight or info.family() != "Google Sans" or raw.familyName() != "Google Sans"
                or info.weight() != weight or raw.weight() != weight):
            raise RuntimeError(f"{name}: Google Sans {weight} resolves to {info.family()} {info.weight()}")
        if font.styleStrategy() != QFont.StyleStrategy.PreferDefault or font.hintingPreference() != QFont.HintingPreference.PreferDefaultHinting:
            raise RuntimeError(f"{name}: font rendering defaults were overridden")
    print("Fonts verified: native Google Sans Medium/500, SemiBold/600 and Bold/700; Qt 6.12+ rendering defaults.")


if __name__ == "__main__":
    main()
