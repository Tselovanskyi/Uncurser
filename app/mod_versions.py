"""Installed versions and data-only undo records; never execute printer metadata."""
import difflib
import json
import re

from . import fluidd_screws, mesh, mesh_temperature, mod_comments, mods, screws, smoothing, storage, warmup

TARGET = storage.RECORDS
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "uncurser-mod-records-schema-1"
LEGACY_VERSION = "0.1.5"
LEGACY_TARGETS = frozenset((mods.TARGET, mesh.TARGET, mesh_temperature.TARGET))
# Historical release versions, not the current app version. Change only when
# the corresponding code/configuration installed on the printer changes.
HELPER_CONFIG_REVISION = "0.1.15"
VERSIONS = {module: "0.1.16" for module in
            (mods.TARGET, mesh.TARGET, mesh_temperature.TARGET, screws.PYTHON, smoothing.TARGET, warmup.TARGET)}
MODULES = {module.TARGET: module for module in (mods, mesh, mesh_temperature, smoothing, warmup)}
# Every mod must declare its complete file scope before it can be applied.
FILES = {path: (path,) for path in MODULES}
FILES[screws.PYTHON] = (mesh.TARGET, screws.PYTHON, screws.CONFIG, fluidd_screws.TARGET)
FILES[smoothing.TARGET] = (smoothing.TARGET, smoothing.HOMING.TARGET)
ADDED_FILES = {path: () for path in MODULES}
ADDED_FILES[screws.PYTHON] = (screws.PYTHON, screws.CONFIG)


def file_scope(path, record=None):
    """Read a verified historical file scope; new records use current paths."""
    if path not in VERSIONS:
        return _unknown_scope(path, record)
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


def _unknown_record(path, record):
    """Keep unknown JSON records without interpreting their implementation."""
    if (not isinstance(path, str) or not path or len(path) > 1024
            or path in (storage.ROOT, TARGET, storage.LEGACY_RECORDS)
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or not isinstance(record, dict)):
        raise ValueError("Invalid unknown mod record.")


def discovery_scope(path, record):
    """Discover ownership without authorizing reads, writes or uninstall steps."""
    if path in VERSIONS:
        return file_scope(path, record)
    _unknown_record(path, record)
    if isinstance(record.get("files"), dict) and record["files"] and "undo" not in record:
        scope = tuple(record["files"])
    elif "undo" in record and "files" not in record:
        scope = (path,)
    else:
        raise ValueError("The unknown mod's file ownership could not be determined: " + path)
    for filename in scope:
        storage._absolute(filename)
        if any(ord(char) < 32 or ord(char) == 127 for char in filename):
            raise ValueError("Invalid unknown mod ownership path.")
    return scope


def _unknown_scope(path, record):
    """Validate data-only removal instructions and supported working paths."""
    _unknown_record(path, record)
    version(record.get("version"))
    optional = {"firmware"} if "firmware" in record else set()
    if optional:
        validate_firmware(record["firmware"])
    if set(record) == {"version", "undo"} | optional:
        validate_changes(record["undo"])
        scope = (path,)
    elif set(record) == {"version", "files"} | optional:
        files = record["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 64:
            raise ValueError("Invalid unknown mod file scope.")
        scope = tuple(files)
        for entry in files.values():
            if isinstance(entry, dict) and set(entry) == {"undo"}:
                validate_changes(entry["undo"])
            elif (not isinstance(entry, dict) or set(entry) != {"created"}
                    or not isinstance(entry["created"], str) or not entry["created"]):
                raise ValueError("Invalid unknown mod file uninstall operation.")
    else:
        raise ValueError("Invalid unknown mod uninstall record.")
    roots = ("/mnt/UDISK/printer_data/config/", "/usr/share/klipper/klippy/extras/",
             "/usr/share/fluidd/assets/")
    for filename in scope:
        # Existing backup naming also validates normalization and file types.
        storage.original(filename)
        if (not filename.startswith(roots) or filename == storage.LEGACY_RECORDS
                or ".Original.txt" in filename or "/__pycache__/" in filename
                or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise ValueError("Unknown mod references an unsupported working file: " + filename)
    return scope


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
        if path not in VERSIONS:
            _unknown_record(path, record)
            continue
        if not isinstance(record, dict):
            raise ValueError("Invalid installed mod record.")
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
        matches = text.count(change["after"])
        if matches != 1:
            owner = _added_header(change)
            if matches == 0 and owner is not None and owner not in mod_comments.read(text.encode("utf-8")):
                continue
            raise ValueError("Installed mod code differs from its undo record; restore original or review the file.")
        text = text.replace(change["after"], change["before"], 1)
    return text.encode("utf-8")


def _added_header(change):
    if change.get("before") != "":
        return None
    data = change["after"].encode("utf-8")
    try:
        headers = mod_comments.lines(data)
        if len(headers) == 1 and mod_comments.strip(data) == b"":
            return next(iter(headers))
    except ValueError:
        pass
    return None


def optional_metadata_change(change, owner):
    return _added_header(change) == owner


def created_matches(data, recorded, owner):
    """Only the recorded owner's supplementary comment may be absent."""
    expected = recorded.encode("utf-8")
    if data == expected:
        return True
    if not isinstance(data, bytes):
        return False
    try:
        return (owner in mod_comments.read(expected) and owner not in mod_comments.read(data)
                and data == mod_comments.remove(expected, owner))
    except ValueError:
        return False


def undo_files(path, current, record):
    load(dump({path: record}))
    if set(current) != set(file_scope(path, record)):
        raise ValueError("All mod files must be read before uninstalling it.")
    result = {}
    for filename, entry in record["files"].items():
        data = current[filename]
        if "created" in entry:
            if not created_matches(data, entry["created"], path):
                raise ValueError("An added mod file differs from its uninstall record: " + filename)
            result[filename] = None
        else:
            if data is None:
                raise ValueError("A modified mod file is missing: " + filename)
            result[filename] = undo(data, entry)
    return result


def preserve_shared_changes(before, after, records, selected):
    """Check retained ownership only in working files this operation changes."""
    changed = {path for path, data in after.items() if path != TARGET and data != before.get(path)}
    for owner, record in records.items():
        if owner in selected:
            continue
        try:
            scope = discovery_scope(owner, record)
        except (ValueError, TypeError):
            if changed:
                raise ValueError("Cannot verify files owned by the unknown mod: " + owner) from None
            continue
        for path in changed.intersection(scope):
            entry = record["files"][path] if "files" in record else {"undo": record.get("undo")}
            if owner not in VERSIONS:
                if isinstance(entry, dict) and set(entry) == {"undo"}:
                    validate_changes(entry["undo"])
                elif (not isinstance(entry, dict) or set(entry) != {"created"}
                        or not isinstance(entry["created"], str) or not entry["created"]):
                    raise ValueError("Cannot verify the unknown mod's ownership of: " + path)
            if "created" in entry:
                raise ValueError("Another mod owns the file being changed: " + path)
            if not isinstance(before.get(path), bytes) or not isinstance(after[path], bytes):
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
            body = [{"before": old, "after": new} for old, new in replacements[filename]]
            files[filename] = {"undo": body + _header_change(path, before, after)}
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


def _header_change(owner, baseline, enabled):
    old, new = mod_comments.lines(baseline), mod_comments.lines(enabled)
    if {key: value for key, value in old.items() if key != owner} != {
            key: value for key, value in new.items() if key != owner}:
        raise ValueError("A mod cannot own another mod's source comment.")
    before, after = old.get(owner, b""), new.get(owner, b"")
    if before == after:
        return []
    if not after:
        raise ValueError("An installed mod must retain its source comment.")
    return [{"before": before.decode("utf-8"), "after": after.decode("utf-8")}]


def capture_changes(path, baseline, enabled):
    """Capture unique data-only undo edits for one existing working file."""
    # Printer-generated calibration is never part of a mod's undo record.
    header_changes = _header_change(path, baseline, enabled)
    old_body, new_body = mod_comments.strip(baseline), mod_comments.strip(enabled)
    old_text = mesh.split(old_body)[0] if path.endswith(".cfg") else old_body.decode()
    new_text = mesh.split(new_body)[0] if path.endswith(".cfg") else new_body.decode()
    if path == mesh.TARGET:
        # A mesh record must never own the independently removable helper include.
        old_text = screws.strip_screws_include(old_text)[0]
        new_text = screws.strip_screws_include(new_text)[0]
    old, new = old_text.splitlines(True), new_text.splitlines(True)
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    changes = []
    for group in matcher.get_grouped_opcodes(0):
        old_start, old_stop = group[0][1], group[-1][2]
        new_start, new_stop = group[0][3], group[-1][4]
        # Context must not cross a separately owned metadata line. Extend only
        # through unchanged lines and only while the full installed bytes match.
        options = [(0, 0)] + [(left, right) for size in range(1, 5)
                              for left in range(size + 1) for right in (size - left,)]
        for left, right in options:
            if (min(old_start, new_start) < left or old_stop + right > len(old)
                    or new_stop + right > len(new)
                    or old[old_start-left:old_start] != new[new_start-left:new_start]
                    or old[old_stop:old_stop+right] != new[new_stop:new_stop+right]):
                continue
            before = "".join(old[old_start-left:old_stop+right])
            after = "".join(new[new_start-left:new_stop+right])
            if after and enabled.decode().count(after) == 1:
                changes.append({"before": before, "after": after})
                break
        else:
            raise ValueError("Cannot capture a unique undo instruction for this mod.")
    changes.extend(header_changes)
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
