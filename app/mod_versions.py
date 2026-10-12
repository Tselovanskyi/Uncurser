"""Installed versions and data-only undo records; never execute printer metadata."""
import difflib
import json
import re

from . import fluidd_screws, mesh, mesh_temperature, mods, screws, smoothing

TARGET = "/mnt/UDISK/printer_data/config/Uncurser_mods.json"
ORIGINAL = TARGET.removesuffix(".json") + ".jsn.Original.txt"
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "uncurser-mod-records-schema-1"
LEGACY_VERSION = "0.1.5"
LEGACY_TARGETS = frozenset((mods.TARGET, mesh.TARGET, mesh_temperature.TARGET))
# Historical release versions, not the current app version. Change only when
# the corresponding code/configuration installed on the printer changes.
VERSIONS = {mods.TARGET: "0.1.5", mesh.TARGET: "0.1.7", mesh_temperature.TARGET: "0.1.5", screws.PYTHON: "0.1.12", smoothing.TARGET: "0.1.14"}
MODULES = {module.TARGET: module for module in (mods, mesh, mesh_temperature, smoothing)}
# Every mod must declare its complete file scope before it can be applied.
FILES = {path: (path,) for path in MODULES}
FILES[screws.PYTHON] = (mesh.TARGET, screws.PYTHON, screws.CONFIG, fluidd_screws.TARGET)
FILES[smoothing.TARGET] = (smoothing.TARGET, smoothing.HOMING.TARGET)
ADDED_FILES = {path: () for path in MODULES}
ADDED_FILES[screws.PYTHON] = (screws.PYTHON, screws.CONFIG)


def require_registered(choices):
    if set(VERSIONS) != set(FILES) or set(VERSIONS) != set(ADDED_FILES) or any(path not in VERSIONS for path in choices):
        raise ValueError("Every mod requires a version and a registered uninstall file scope.")
    for path, filenames in FILES.items():
        version(VERSIONS[path])
        if (not filenames or len(set(filenames)) != len(filenames)
                or not set(ADDED_FILES[path]).issubset(filenames)
                or any(not isinstance(filename, str) or not filename.startswith("/") for filename in filenames)
                or (path in MODULES and (path not in filenames or ADDED_FILES[path]))):
            raise ValueError("Invalid registered mod uninstall file scope.")


def validate_changes(changes):
    if not isinstance(changes, list) or not changes or len(changes) > 64:
        raise ValueError("Invalid mod undo instructions.")
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {"before", "after"}
                or not isinstance(change["before"], str) or not isinstance(change["after"], str)
                or not change["after"] or change["before"] == change["after"]):
            raise ValueError("Invalid mod undo text.")


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Invalid installed mod version.")
    return tuple(map(int, value.split(".")))


def load(data):
    require_registered({})
    if data is None:
        return {}
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("The installed mod records are too large.")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate key in installed mod records.")
            result[key] = value
        return result
    document = json.loads(data, object_pairs_hook=unique)
    if not isinstance(document, dict) or document.get("schema") != 1 or not isinstance(document.get("mods"), dict):
        raise ValueError("Unsupported installed mod records.")
    records = document["mods"]
    for path, record in records.items():
        if path not in VERSIONS or not isinstance(record, dict):
            raise ValueError("Unknown mod in installed records.")
        version(record.get("version"))
        if len(FILES[path]) == 1:
            if set(record) != {"version", "undo"}:
                raise ValueError("Invalid single-file mod record.")
            validate_changes(record["undo"])
        else:
            files = record.get("files")
            if set(record) != {"version", "files"} or not isinstance(files, dict) or set(files) != set(FILES[path]):
                raise ValueError("The mod uninstall record does not cover its registered files.")
            for filename, entry in files.items():
                if not isinstance(entry, dict):
                    raise ValueError("Invalid mod file record.")
                if set(entry) == {"created"}:
                    if not isinstance(entry["created"], str) or not entry["created"] or filename not in ADDED_FILES[path]:
                        raise ValueError("Invalid added-file uninstall record.")
                elif set(entry) == {"undo"} and filename not in ADDED_FILES[path]:
                    validate_changes(entry["undo"])
                else:
                    raise ValueError("Invalid mod file uninstall operation.")
    return records


def dump(records):
    data = (json.dumps({"schema": 1, "mods": records}, indent=2, sort_keys=True) + "\n").encode()
    load(data)
    return data


def undo(data, record):
    # Exact, unique text matching prevents number concatenation and prevents
    # overwriting manual edits within the recorded owned regions.
    text = data.decode("utf-8")
    for change in reversed(record["undo"]):
        if text.count(change["after"]) != 1:
            raise ValueError("Installed mod code differs from its undo record; restore original or review the file.")
        text = text.replace(change["after"], change["before"], 1)
    return text.encode("utf-8")


def undo_files(path, current, record):
    load(dump({path: record}))
    if set(current) != set(FILES[path]):
        raise ValueError("All mod files must be read before uninstalling it.")
    result = {}
    for filename, entry in record["files"].items():
        data = current[filename]
        if "created" in entry:
            if data != entry["created"].encode("utf-8"):
                raise ValueError("An added mod file differs from its uninstall record: " + filename)
            result[filename] = None
        else:
            if data is None:
                raise ValueError("A modified mod file is missing: " + filename)
            result[filename] = undo(data, entry)
    return result


def capture_files(path, baseline, enabled, replacements):
    require_registered({path: True})
    if set(baseline) != set(FILES[path]) or set(enabled) != set(FILES[path]):
        raise ValueError("The uninstall record must cover every affected file.")
    files = {}
    for filename in FILES[path]:
        before, after = baseline[filename], enabled[filename]
        if before is None:
            if after is None:
                raise ValueError("An added mod file has no installed content.")
            files[filename] = {"created": after.decode("utf-8")}
        else:
            files[filename] = {"undo": [{"before": old, "after": new} for old, new in replacements[filename]]}
    record = {"version": VERSIONS[path], "files": files}
    if undo_files(path, enabled, record) != baseline:
        raise ValueError("The mod uninstall record does not exactly restore its baseline.")
    return record


def capture(path, baseline, enabled):
    require_registered({path: True})
    if len(FILES[path]) != 1:
        raise ValueError("A multi-file mod requires an uninstall record for every affected file.")
    result = {"version": VERSIONS[path], "undo": capture_changes(path, baseline, enabled)}
    load(dump({path: result}))
    return result


def capture_changes(path, baseline, enabled):
    """Capture unique data-only undo edits for one existing working file."""
    # Printer-generated calibration is never part of a mod's undo record.
    old_text = mesh.split(baseline)[0] if path.endswith(".cfg") else baseline.decode()
    new_text = mesh.split(enabled)[0] if path.endswith(".cfg") else enabled.decode()
    if path == mesh.TARGET:
        # A mesh record must never own the independently removable helper include.
        old_text = screws.strip_screws_include(old_text)[0]
        new_text = screws.strip_screws_include(new_text)[0]
    old, new = old_text.splitlines(True), new_text.splitlines(True)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changes = []
    for group in matcher.get_grouped_opcodes(2):
        before = "".join(old[group[0][1]:group[-1][2]])
        after = "".join(new[group[0][3]:group[-1][4]])
        if not after or enabled.decode().count(after) != 1:
            raise ValueError("Cannot capture a unique undo instruction for this mod.")
        changes.append({"before": before, "after": after})
    if not changes:
        raise ValueError("This file already contains the enabled values in its baseline; no mod changes can be recorded.")
    validate_changes(changes)
    if undo(enabled, {"undo": changes}) != baseline:
        raise ValueError("The undo instructions do not exactly restore their baseline.")
    return changes


def disabled(path, data, original):
    if len(FILES[path]) != 1:
        raise ValueError("A multi-file mod must restore every affected file together.")
    module = MODULES[path]
    return module.transform(data, False) if module is mods else module.transform(data, False, original)


def enabled(path, data):
    if len(FILES[path]) != 1:
        raise ValueError("A multi-file mod must prepare every affected file together.")
    return MODULES[path].transform(data, True)
