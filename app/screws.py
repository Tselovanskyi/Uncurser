"""Install the manual bed screw helper without changing print-start behavior."""
import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path

from . import mesh, mod_comments, mods, storage
from .config_blocks import (SCREWS_CONFIG, LEGACY_SCREWS_CONFIG, SCREWS_INCLUDE,
                            screws_include_path as _include_path,
                            strip_screws_include as _strip_include)

MOD_ID = "bed_screws_v1"
COMMAND = "_UNCURSER_BED_SCREW_HELPER"
SAVE_COMMAND = "_UNCURSER_BED_SCREW_SAVE_MESH"
PYTHON = "/usr/share/klipper/klippy/extras/screws_tilt_adjust.py"
CONFIG = SCREWS_CONFIG
LEGACY_CONFIG = LEGACY_SCREWS_CONFIG


@dataclass(frozen=True)
class HelperFile:
    TARGET: str

    @property
    def ORIGINAL(self):
        return storage.original(self.TARGET)

    @property
    def MANIFEST(self):
        return self.ORIGINAL + ".json"

    @property
    def REFERENCE_HASH(self):
        return mods.digest(payload(self.TARGET))


FILES = (HelperFile(PYTHON), HelperFile(CONFIG))
LEGACY_FILE = HelperFile(LEGACY_CONFIG)
SCAN_FILES = (*FILES, LEGACY_FILE)


def screws_include_path(text):
    return _include_path(mod_comments.strip(text.encode("utf-8")).decode("utf-8"))


def strip_screws_include(text):
    code = mod_comments.strip(text.encode("utf-8")).decode("utf-8")
    remaining, enabled = _strip_include(code)
    if not enabled:
        return text, False
    block = code[:len(code) - len(remaining)]
    if text.count(block) != 1:
        raise ValueError("The bed screw include is interrupted or duplicated.")
    return text.replace(block, "", 1), True


def recognized(path, data):
    normalized = mod_comments.strip(data).replace(b"\r\n", b"\n")
    return normalized == payload(path)


def recovery_recognized(path, data):
    """Verify the complete current helper, allowing only known command renames."""
    normalized = mod_comments.strip(data).replace(b"\r\n", b"\n")
    if path == PYTHON:
        for old, new in ((b'"SCREWS_TILT_CALCULATE"', ('"' + COMMAND + '"').encode()),
                         (b'"SCREWS_TILT_SAVE_MESH"', ('"' + SAVE_COMMAND + '"').encode()),
                         (b"cmd_SCREWS_TILT_CALCULATE", ("cmd_" + COMMAND).encode()),
                         (b"cmd_SCREWS_TILT_SAVE_MESH", ("cmd_" + SAVE_COMMAND).encode()),
                         (b"Adjust screws and run SCREWS_TILT_CALCULATE again.",
                          b"Adjust screws and run BED_SCREW_HELPER again.")):
            normalized = normalized.replace(old, new)
    elif path in (CONFIG, LEGACY_CONFIG):
        wrapper = (b"[gcode_macro BED_SCREW_HELPER]\ngcode:\n  "
                   + COMMAND.encode() + b" {rawparams}\n")
        old_wrapper = (b"[gcode_macro SCREWS_TILT_CALCULATE]\n"
                       b"rename_existing: _ORIGIN_SCREWS_TILT_CALCULATE\ngcode:\n"
                       b"  _ORIGIN_SCREWS_TILT_CALCULATE {rawparams}\n")
        normalized = normalized.replace(old_wrapper, wrapper)
        normalized = normalized.replace(wrapper.replace(COMMAND.encode(), b"SCREWS_TILT_CALCULATE"),
                                        wrapper)
    else:
        return False
    return normalized == payload(path)


def payload(path):
    folder = Path(__file__).resolve().parent / "assets" / "bed_screws"
    if not folder.is_dir():
        folder = Path(__file__).resolve().parents[1] / "References" / "screws_tilt_adjust"
    data = (folder / path.rsplit("/", 1)[-1]).read_bytes().replace(b"\r\n", b"\n")
    validate(path, data)
    return data


def validate(path, data):
    mod_comments.read(data)
    if path == PYTHON:
        ast.parse(data.decode("utf-8"), filename=path, feature_version=(3, 9))
    else:
        regular, generated = mesh.split(data)
        if generated:
            raise ValueError("The helper config must not contain generated calibration data.")
        mesh.parser(regular)


def edit_include(data, enabled):
    regular, generated = mesh.split(data)
    base, current = strip_screws_include(regular)
    if enabled == current and (not enabled or screws_include_path(regular) == CONFIG):
        return data
    newline = "\r\n" if "\r\n" in regular else "\n"
    result = ((SCREWS_INCLUDE.replace("\n", newline) if enabled else "") + base + generated).encode()
    mesh.parser(mesh.split(result)[0])
    return result


def inspect(root, files, included, patterns):
    try:
        active_config = screws_include_path(mesh.split(root.data)[0])
        enabled = active_config is not None
        sections = {"screws_tilt_adjust", "gcode_macro BED_SCREW_HELPER", "gcode_macro SCREWS_TILT_CALCULATE",
                    "gcode_macro _ORIGIN_SCREWS_TILT_CALCULATE"}
        for path, data in included.items():
            if path == active_config:
                continue
            regular, generated = mesh.split(data)
            if sections.intersection(mesh.parser(regular).sections()) or sections.intersection(mesh.generated_parser(generated).sections()):
                raise ValueError("A bed screw helper is already defined in " + path)
        if enabled and active_config not in included:
            raise ValueError("The helper include and active configuration do not agree.")
        for pattern in patterns:
            if any(fnmatch.fnmatchcase(path, pattern) and path != active_config
                   for path in (CONFIG, LEGACY_CONFIG)) or (active_config is not None
                   and fnmatch.fnmatchcase(active_config, pattern) and pattern != active_config):
                raise ValueError("An existing include would also load the helper: " + pattern)
        for path, value in files.items():
            if path in included and included[path] != value.data:
                raise ValueError("A helper file changed during the scan; scan again: " + path)
            expected = payload(path) if enabled and path in (PYTHON, active_config) else None
            actual = mod_comments.strip(value.data).replace(b"\r\n", b"\n") if value.data is not None else None
            if actual != expected:
                raise ValueError("An existing, edited, or incomplete helper needs review: " + path)
        known = root.analysis["known"]
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if enabled and known else "Original" if known else "Unknown",
                "reason": "" if known else "printer.cfg differs from the supported reference."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False,
                "label": "Conflict", "reason": str(error)}
