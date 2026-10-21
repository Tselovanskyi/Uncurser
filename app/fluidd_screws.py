"""Reversible edits confined to the supported Fluidd screw helper."""
import difflib
import json
from dataclasses import dataclass
from pathlib import Path

from . import mod_comments, mods, storage

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
    mod_comments.read(data)
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
        known = mods.digest(mod_comments.strip(base)) == module.REFERENCE_HASH
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
    if mod_comments.strip(old_base) != mod_comments.strip(new_base):
        raise ValueError("Unrelated Fluidd code would change.")
    return [[values[int(old_enabled)], values[int(new_enabled)]]
            for values in blocks(path) if old_enabled != new_enabled]


def _recovery_values(path, data):
    """Recognize known command renames without retaining old UI templates."""
    mod_comments.read(data)
    text = data.decode("utf-8")
    values, states = [], []
    pairs = blocks(path)
    for original, replacement in pairs:
        forms = {original, replacement}
        if replacement == GRID_RENDER:
            forms.add(replacement.replace("_UNCURSER_BED_SCREW_SAVE_MESH", "SCREWS_TILT_SAVE_MESH"))
        elif replacement.startswith("retry(){"):
            forms.add(replacement.replace('"BED_SCREW_HELPER"', '"SCREWS_TILT_CALCULATE"'))
        for candidate in sorted(forms, key=len, reverse=True):
            if candidate not in text:
                continue
            if text.count(candidate) != 1 or any(form in text.replace(candidate, "", 1) for form in forms):
                raise ValueError("The Fluidd screw dialog is duplicated.")
            values.append(candidate)
            states.append(candidate != original)
            break
        else:
            raise ValueError("The Fluidd screw dialog is unsupported, edited, or duplicated.")

    # These two additions followed the original helper. Older installations
    # legitimately retain their stock forms; all earlier blocks must agree.
    optional = {index for index, (original, _) in enumerate(pairs)
                if original.startswith(('this.printerSupportsBedScrewsCalculate&&',
                                        'show_screws_tilt_adjust_dialog_automatically:'))}
    core = [state for index, state in enumerate(states) if index not in optional]
    if any(core) != all(core) or (not any(core) and any(states)):
        raise ValueError("The Fluidd screw helper is only partially modified.")
    normalized = text
    for index, (original, replacement) in enumerate(pairs):
        target = replacement if all(core) else original
        normalized = normalized.replace(values[index], target, 1)
    strip_mod(path, normalized.encode("utf-8"))
    return values


def recovery_replacements(path, before, after):
    """Return verified owned edits for recovery, preserving all other bytes."""
    old, new = _recovery_values(path, before), _recovery_values(path, after)
    pairs = [[source, target] for source, target in zip(old, new) if source != target]
    text = before.decode("utf-8")
    for source, target in pairs:
        if text.count(source) != 1:
            raise ValueError("The Fluidd screw-dialog edit is not unique.")
        text = text.replace(source, target, 1)
    if mod_comments.strip(text.encode("utf-8")) != mod_comments.strip(after):
        raise ValueError("Unrelated Fluidd code would change.")
    return pairs


def recovery_undo(path, data, reference):
    """Restore known interface blocks to the active firmware's own forms."""
    old, new = _recovery_values(path, data), _recovery_values(path, reference)
    text = data.decode("utf-8")
    for source, target in zip(old, new):
        if source != target:
            if text.count(source) != 1:
                raise ValueError("The Fluidd screw-dialog edit is not unique.")
            text = text.replace(source, target, 1)
    return text.encode("utf-8")


def difference(path, before, after, fromfile, tofile):
    # Minified bundles are one very long line. Show the exact changed function
    # when the rest is identical; never hide unrelated changes.
    try:
        pairs = replacements(path, before, after)
        old_comments, new_comments = mod_comments.lines(before), mod_comments.lines(after)
        pairs = [(old_comments.get(owner, b"").decode().rstrip("\r\n"),
                  new_comments.get(owner, b"").decode().rstrip("\r\n"))
                 for owner in sorted(old_comments.keys() | new_comments.keys())
                 if old_comments.get(owner) != new_comments.get(owner)] + pairs
    except ValueError:
        pairs = [(before.decode(), after.decode())]
    return "".join("".join(difflib.unified_diff(
        (old + "\n").splitlines(True), (new + "\n").splitlines(True),
        fromfile=fromfile, tofile=tofile, n=3)) for old, new in pairs)
