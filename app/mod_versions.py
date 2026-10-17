"""Installed versions and data-only undo records; never execute printer metadata."""
import difflib
import json
import re

from . import fluidd_screws, mesh, mesh_temperature, mods, screws, smoothing, storage

TARGET = storage.RECORDS
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "uncurser-mod-records-schema-1"
LEGACY_VERSION = "0.1.5"
LEGACY_TARGETS = frozenset((mods.TARGET, mesh.TARGET, mesh_temperature.TARGET))
# Historical release versions, not the current app version. Change only when
# the corresponding code/configuration installed on the printer changes.
HELPER_CONFIG_REVISION = "0.1.15"
VERSIONS = {mods.TARGET: "0.1.5", mesh.TARGET: "0.1.7", mesh_temperature.TARGET: "0.1.5", screws.PYTHON: HELPER_CONFIG_REVISION, smoothing.TARGET: "0.1.14"}
MODULES = {module.TARGET: module for module in (mods, mesh, mesh_temperature, smoothing)}
# Every mod must declare its complete file scope before it can be applied.
FILES = {path: (path,) for path in MODULES}
FILES[screws.PYTHON] = (mesh.TARGET, screws.PYTHON, screws.CONFIG, fluidd_screws.TARGET)
FILES[smoothing.TARGET] = (smoothing.TARGET, smoothing.HOMING.TARGET)
ADDED_FILES = {path: () for path in MODULES}
ADDED_FILES[screws.PYTHON] = (screws.PYTHON, screws.CONFIG)


def file_scope(path, record=None):
    """Read a verified historical file scope; new records use current paths."""
    require_registered({path: True})
    current = FILES[path]
    if record is None:
        return current
    if not isinstance(record, dict):
        raise ValueError("Invalid mod uninstall record.")
    revision = version(record.get("version"))
    if "firmware" in record:
        validate_firmware(record["firmware"])
    if len(current) == 1:
        return current
    files = record.get("files")
    if not isinstance(files, dict):
        raise ValueError("The mod uninstall record does not cover its registered files.")
    if set(files) == set(current):
        return current
    if path == screws.PYTHON:
        legacy = (mesh.TARGET, screws.PYTHON, screws.LEGACY_CONFIG, fluidd_screws.TARGET)
        if revision < version(HELPER_CONFIG_REVISION) and set(files) == set(legacy):
            return legacy
    raise ValueError("The mod uninstall record does not cover its registered files.")


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


def validate_firmware(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", value):
        raise ValueError("Invalid installed printer firmware version.")
    return value


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
        optional = {"firmware"} if "firmware" in record else set()
        if optional:
            validate_firmware(record["firmware"])
        if len(FILES[path]) == 1:
            if set(record) != {"version", "undo"} | optional:
                raise ValueError("Invalid single-file mod record.")
            validate_changes(record["undo"])
        else:
            files = record.get("files")
            if set(record) != {"version", "files"} | optional or not isinstance(files, dict):
                raise ValueError("The mod uninstall record does not cover its registered files.")
            scope = file_scope(path, record)
            added = (screws.PYTHON, screws.LEGACY_CONFIG) if path == screws.PYTHON and screws.LEGACY_CONFIG in scope else ADDED_FILES[path]
            for filename, entry in files.items():
                if not isinstance(entry, dict):
                    raise ValueError("Invalid mod file record.")
                if set(entry) == {"created"}:
                    if not isinstance(entry["created"], str) or not entry["created"] or filename not in added:
                        raise ValueError("Invalid added-file uninstall record.")
                elif set(entry) == {"undo"} and filename not in added:
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
    if set(current) != set(file_scope(path, record)):
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


def preserve_shared_changes(before, after, records, selected):
    """Check retained ownership only in working files this operation changes."""
    changed = {path for path, data in after.items() if data != before.get(path)}
    for owner, record in records.items():
        if owner in selected:
            continue
        for path in changed.intersection(file_scope(owner, record)):
            entry = record["files"][path] if "files" in record else record
            if "created" in entry:
                raise ValueError("Another mod owns the file being changed: " + path)
            undo(before[path], entry)
            undo(after[path], entry)


def capture_files(path, baseline, enabled, replacements, *, firmware=None):
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
    if firmware is not None:
        record["firmware"] = validate_firmware(firmware)
    if undo_files(path, enabled, record) != baseline:
        raise ValueError("The mod uninstall record does not exactly restore its baseline.")
    return record


def capture(path, baseline, enabled, *, firmware=None):
    require_registered({path: True})
    if len(FILES[path]) != 1:
        raise ValueError("A multi-file mod requires an uninstall record for every affected file.")
    result = {"version": VERSIONS[path], "undo": capture_changes(path, baseline, enabled)}
    if firmware is not None:
        result["firmware"] = validate_firmware(firmware)
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
