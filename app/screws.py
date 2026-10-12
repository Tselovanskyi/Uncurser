"""Install the manual bed screw helper without changing print-start behavior."""
import ast
import fnmatch
from dataclasses import dataclass
from pathlib import Path

from . import mesh, mods
from .config_blocks import SCREWS_INCLUDE, strip_screws_include

MOD_ID = "bed_screws_v1"
PYTHON = "/usr/share/klipper/klippy/extras/screws_tilt_adjust.py"
CONFIG = "/mnt/UDISK/printer_data/config/screws_tilt_adjust.cfg"


@dataclass(frozen=True)
class HelperFile:
    TARGET: str

    @property
    def ORIGINAL(self):
        stem, suffix = self.TARGET.rsplit(".", 1)
        return stem + (".ptn" if suffix == "py" else ".cfn") + ".Original.txt"

    @property
    def MANIFEST(self):
        return self.ORIGINAL + ".json"

    @property
    def REFERENCE_HASH(self):
        return mods.digest(payload(self.TARGET))


FILES = (HelperFile(PYTHON), HelperFile(CONFIG))


def recognized(path, data):
    normalized = data.replace(b"\r\n", b"\n")
    return normalized == payload(path)


def payload(path):
    folder = Path(__file__).resolve().parent / "assets" / "bed_screws"
    if not folder.is_dir():
        folder = Path(__file__).resolve().parents[1] / "References" / "screws_tilt_adjust"
    data = (folder / path.rsplit("/", 1)[-1]).read_bytes().replace(b"\r\n", b"\n")
    validate(path, data)
    return data


def validate(path, data):
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
    if enabled == current:
        return data
    newline = "\r\n" if "\r\n" in regular else "\n"
    result = ((SCREWS_INCLUDE.replace("\n", newline) if enabled else "") + base + generated).encode()
    mesh.parser(mesh.split(result)[0])
    return result


def inspect(root, files, included, patterns):
    try:
        _, enabled = strip_screws_include(mesh.split(root.data)[0])
        sections = {"screws_tilt_adjust", "gcode_macro SCREWS_TILT_CALCULATE",
                    "gcode_macro _ORIGIN_SCREWS_TILT_CALCULATE"}
        for path, data in included.items():
            if path == CONFIG and enabled:
                continue
            regular, generated = mesh.split(data)
            if sections.intersection(mesh.parser(regular).sections()) or sections.intersection(mesh.generated_parser(generated).sections()):
                raise ValueError("A bed screw helper is already defined in " + path)
        if enabled != (CONFIG in included):
            raise ValueError("The helper include and active configuration do not agree.")
        for pattern in patterns:
            if fnmatch.fnmatchcase(CONFIG, pattern) and (not enabled or pattern != CONFIG):
                raise ValueError("An existing include would also load the helper: " + pattern)
        for path, value in files.items():
            if path in included and included[path] != value.data:
                raise ValueError("A helper file changed during the scan; scan again: " + path)
            expected = payload(path) if enabled else None
            actual = value.data.replace(b"\r\n", b"\n") if value.data is not None else None
            if actual != expected:
                raise ValueError("An existing, edited, or incomplete helper needs review: " + path)
        known = root.analysis["known"]
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if enabled and known else "Original" if known else "Unknown",
                "reason": "" if known else "printer.cfg differs from the supported reference."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False,
                "label": "Conflict", "reason": str(error)}
