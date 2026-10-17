"""Reversible edits confined to the supported Fluidd screw helper."""
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from . import mods, storage

TARGET = "/usr/share/fluidd/assets/index-DAJGCJUz.js"
INDEX = "/usr/share/fluidd/index.html"
ASSET = "assets/index-DAJGCJUz.js"
REFERENCE_HASH = "ceaab565cae7d658716f7af151302b7eff0bde72a0ddc2c2b1ebbed42477c5ed"
_assets = Path(__file__).resolve().parent / "assets"
ORIGINAL_RENDER = (_assets / "screws-list-render.js.txt").read_text(encoding="utf-8").rstrip("\n")
EDITS = json.loads((_assets / "screws-grid-edits.json").read_text(encoding="utf-8"))
DIALOG_EDITS = json.loads((_assets / "screws-dialog-edits.json").read_text(encoding="utf-8"))
GRID_RENDER = ORIGINAL_RENDER
for before, after in EDITS:
    if GRID_RENDER.count(before) != 1:
        raise ValueError("The bundled screw-dialog edit is not unique.")
    GRID_RENDER = GRID_RENDER.replace(before, after, 1)


@dataclass(frozen=True)
class FluiddFile:
    TARGET: str
    REFERENCE_HASH: str

    @property
    def ORIGINAL(self):
        return storage.original(self.TARGET)

    @property
    def MANIFEST(self):
        return self.ORIGINAL + ".json"


FILES = (FluiddFile(TARGET, REFERENCE_HASH),)


def blocks(path):
    if path == TARGET:
        return ((ORIGINAL_RENDER, GRID_RENDER), *DIALOG_EDITS)
    raise ValueError("Unsupported Fluidd file.")


def strip_mod(path, data):
    text = data.decode("utf-8")
    states = []
    for original, replacement in blocks(path):
        matches = text.count(replacement)
        if matches > 1:
            raise ValueError("The Fluidd screw dialog is duplicated.")
        enabled = matches == 1
        if enabled:
            text = text.replace(replacement, original, 1)
        if text.count(original) != 1:
            raise ValueError("The Fluidd screw dialog is unsupported, edited, or duplicated.")
        states.append(enabled)
    if any(states) and not all(states):
        raise ValueError("The Fluidd screw helper is only partially modified.")
    return text.encode("utf-8"), all(states)


def inspect(module, data):
    try:
        base, enabled = strip_mod(module.TARGET, data)
        known = mods.digest(base) == module.REFERENCE_HASH
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if known and enabled else "Original" if known else "Unknown",
                "reason": "" if known else "This Fluidd file differs from the supported build."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False, "label": "Conflict", "reason": str(error)}


def transform(path, data, enabled):
    base, current = strip_mod(path, data)
    if current == enabled:
        return data
    text = base.decode("utf-8")
    if enabled:
        for original, replacement in blocks(path):
            if text.count(original) != 1:
                raise ValueError("The Fluidd screw-dialog edit is not unique.")
            text = text.replace(original, replacement, 1)
    return text.encode("utf-8")


def replacements(path, before, after):
    old_base, old_enabled = strip_mod(path, before)
    new_base, new_enabled = strip_mod(path, after)
    if old_base != new_base:
        raise ValueError("Unrelated Fluidd code would change.")
    return [[values[int(old_enabled)], values[int(new_enabled)]]
            for values in blocks(path) if old_enabled != new_enabled]


def difference(path, before, after, fromfile, tofile):
    # Minified bundles are one very long line. Show the exact changed function
    # when the rest is identical; never hide unrelated changes.
    try:
        pairs = replacements(path, before, after)
    except ValueError:
        pairs = [(before.decode(), after.decode())]
    return "".join("".join(difflib.unified_diff(
        (old + "\n").splitlines(True), (new + "\n").splitlines(True),
        fromfile=fromfile, tofile=tofile, n=3)) for old, new in pairs)
