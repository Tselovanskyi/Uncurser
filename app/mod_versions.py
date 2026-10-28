"""Installed versions and data-only undo records; never execute printer metadata."""
import difflib
import json
import re

from . import fluidd_categories, fluidd_screws, kamp, mesh, mesh_temperature, mod_comments, mods, pa, screws, smoothing, storage, warmup

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
VERSIONS[screws.PYTHON] = "0.1.17"
VERSIONS[kamp.TARGET] = "0.1.18"
VERSIONS[pa.TARGET] = "0.1.18"
MODULES = {module.TARGET: module for module in (mods, mesh, mesh_temperature, smoothing, warmup, kamp, pa)}
# Every mod must declare its complete file scope before it can be applied.
FILES = {path: tuple(getattr(module, "FILES", (path,))) for path, module in MODULES.items()}
FILES[screws.PYTHON] = (mesh.TARGET, screws.PYTHON, screws.CONFIG, fluidd_screws.TARGET)
FILES[smoothing.TARGET] = (smoothing.TARGET, smoothing.HOMING.TARGET)
ADDED_FILES = {path: tuple(getattr(module, "ADDED_FILES", ())) for path, module in MODULES.items()}
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
                or (path in MODULES and (path not in filenames
                    or tuple(ADDED_FILES[path]) != tuple(getattr(MODULES[path], "ADDED_FILES", ()))))):
            raise ValueError("Invalid registered mod uninstall file scope.")


def validate_changes(changes):
    if not isinstance(changes, list) or not changes or len(changes) > 64:
        raise ValueError("Invalid mod undo instructions.")
    for change in changes:
        if (not isinstance(change, dict) or set(change) != {"before", "after"}
                or not isinstance(change["before"], str) or not isinstance(change["after"], str)
                or not change["after"] or change["before"] == change["after"]):
            raise ValueError("Invalid mod undo text.")


def validate_binary_undo(operation):
    if (not isinstance(operation, dict) or set(operation) != {"size", "changes"}
            or type(operation["size"]) is not int or not 1 <= operation["size"] <= 2 * 1024 * 1024
            or not isinstance(operation["changes"], list) or not 1 <= len(operation["changes"]) <= 64):
        raise ValueError("Invalid binary uninstall instructions.")
    end = 0
    for change in operation["changes"]:
        if (not isinstance(change, dict) or set(change) != {"offset", "before", "after"}
                or type(change["offset"]) is not int or change["offset"] < end
                or any(not isinstance(change[key], str) or not re.fullmatch(r"(?:[0-9a-f]{2})+", change[key])
                       for key in ("before", "after"))
                or len(change["before"]) != len(change["after"]) or change["before"] == change["after"]):
            raise ValueError("Invalid binary uninstall byte range.")
        end = change["offset"] + len(change["after"]) // 2
        if end > operation["size"]:
            raise ValueError("A binary uninstall byte range is outside its file.")


def validate_entry(value):
    if isinstance(value, dict) and set(value) == {"undo"}:
        validate_changes(value["undo"])
    elif isinstance(value, dict) and set(value) == {"binary_undo"}:
        validate_binary_undo(value["binary_undo"])
    else:
        raise ValueError("Invalid existing-file uninstall instructions.")


def validate_created_binary(value):
    if (not isinstance(value, dict) or set(value) != {"sha256", "size"}
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or type(value["size"]) is not int or not 52 <= value["size"] <= 2 * 1024 * 1024):
        raise ValueError("Invalid added-binary ownership record.")


def binary_created(data):
    """Record an added ARM shared library without storing executable contents."""
    if (not isinstance(data, bytes) or not 52 <= len(data) <= 2 * 1024 * 1024
            or data[:7] != b"\x7fELF\x01\x01\x01" or data[16:20] != b"\x03\x00\x28\x00"):
        raise ValueError("An added native helper must be a supported ARM shared library.")
    return {"sha256": mods.digest(data), "size": len(data)}


def created_binary_matches(data, recorded):
    validate_created_binary(recorded)
    return isinstance(data, bytes) and len(data) == recorded["size"] and mods.digest(data) == recorded["sha256"]


def entry(record):
    result = {key: record[key] for key in ("undo", "binary_undo") if key in record}
    validate_entry(result)
    return result


def undo_binary(data, operation, *, allow_original=False):
    validate_binary_undo(operation)
    if not isinstance(data, bytes) or len(data) != operation["size"]:
        raise ValueError("The binary file size differs from its uninstall record.")
    result = bytearray(data)
    for change in operation["changes"]:
        offset = change["offset"]
        before, after = bytes.fromhex(change["before"]), bytes.fromhex(change["after"])
        present = data[offset:offset + len(after)]
        if present == after:
            result[offset:offset + len(after)] = before
        elif not allow_original or present != before:
            raise ValueError("The binary mod byte range differs from its uninstall record.")
    return bytes(result)


def binary_changes(before, after):
    if not isinstance(before, bytes) or not isinstance(after, bytes) or len(before) != len(after):
        raise ValueError("Binary edits must preserve the file size.")
    changes, start = [], None
    for offset, (old, new) in enumerate(zip(before, after)):
        if old != new and start is None:
            start = offset
        elif old == new and start is not None:
            changes.append({"offset": start, "before": before[start:offset].hex(), "after": after[start:offset].hex()})
            start = None
    if start is not None:
        changes.append({"offset": start, "before": before[start:].hex(), "after": after[start:].hex()})
    return changes


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
    elif ("undo" in record or "binary_undo" in record) and "files" not in record:
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
    if "fluidd_category" in record:
        fluidd_categories.validate_record(record["fluidd_category"])
        optional.add("fluidd_category")
    if set(record) in ({"version", "undo"} | optional, {"version", "binary_undo"} | optional):
        entry(record)
        scope = (path,)
    elif set(record) == {"version", "files"} | optional:
        files = record["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 64:
            raise ValueError("Invalid unknown mod file scope.")
        scope = tuple(files)
        for owned in files.values():
            if isinstance(owned, dict) and set(owned) in ({"undo"}, {"binary_undo"}):
                validate_entry(owned)
            elif isinstance(owned, dict) and set(owned) == {"created_binary"}:
                validate_created_binary(owned["created_binary"])
            elif (not isinstance(owned, dict) or set(owned) != {"created"}
                    or not isinstance(owned["created"], str) or not owned["created"]):
                raise ValueError("Invalid unknown mod file uninstall operation.")
    else:
        raise ValueError("Invalid unknown mod uninstall record.")
    roots = ("/mnt/UDISK/printer_data/config/", "/usr/share/klipper/klippy/extras/",
             "/usr/share/fluidd/assets/")
    for filename in scope:
        # Existing backup naming also validates normalization and file types.
        storage.original(filename)
        owned = record["files"][filename] if "files" in record else entry(record)
        if "created_binary" in owned and not filename.endswith(".so"):
            raise ValueError("Added-binary ownership requires a native helper path.")
        if ((not filename.startswith(roots) and filename not in (pa.TARGET, pa.LIBRARY)) or filename == storage.LEGACY_RECORDS
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
        if "fluidd_category" in record:
            if path != screws.PYTHON:
                raise ValueError("Unexpected Fluidd category uninstall record.")
            fluidd_categories.validate_record(record["fluidd_category"])
            optional.add("fluidd_category")
        if len(FILES[path]) == 1:
            operation = "binary_undo" if getattr(MODULES.get(path), "BINARY", False) else "undo"
            if set(record) != {"version", operation} | optional:
                raise ValueError("Invalid single-file mod record.")
            validate_entry({operation: record[operation]})
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
                    if not isinstance(entry["created"], str) or not entry["created"] or filename not in added or filename.endswith(".so"):
                        raise ValueError("Invalid added-file uninstall record.")
                elif set(entry) == {"created_binary"} and filename in added and filename.endswith(".so"):
                    validate_created_binary(entry["created_binary"])
                elif set(entry) in ({"undo"}, {"binary_undo"}) and filename not in added:
                    validate_entry(entry)
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
    if "binary_undo" in record:
        return undo_binary(data, record["binary_undo"])
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
        if "created_binary" in entry:
            if data is not None and not created_binary_matches(data, entry["created_binary"]):
                raise ValueError("An added native helper differs from its uninstall record: " + filename)
            result[filename] = None
        elif "created" in entry:
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
            owned = record["files"][path] if "files" in record else entry(record)
            if owner not in VERSIONS:
                if isinstance(owned, dict) and set(owned) in ({"undo"}, {"binary_undo"}):
                    validate_entry(owned)
                elif isinstance(owned, dict) and set(owned) == {"created_binary"}:
                    validate_created_binary(owned["created_binary"])
                elif (not isinstance(owned, dict) or set(owned) != {"created"}
                        or not isinstance(owned["created"], str) or not owned["created"]):
                    raise ValueError("Cannot verify the unknown mod's ownership of: " + path)
            if "created" in owned or "created_binary" in owned:
                raise ValueError("Another mod owns the file being changed: " + path)
            if not isinstance(before.get(path), bytes) or not isinstance(after[path], bytes):
                raise ValueError("Another mod owns the file being changed: " + path)
            undo(before[path], owned)
            undo(after[path], owned)


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
            if filename not in ADDED_FILES[path]:
                raise ValueError("A file may only be created within its registered added-file scope.")
            files[filename] = ({"created_binary": binary_created(after)} if filename.endswith(".so")
                               else {"created": after.decode("utf-8")})
        elif filename.endswith(".so"):
            operation = {"size": len(before), "changes": binary_changes(before, after)}
            validate_binary_undo(operation)
            files[filename] = {"binary_undo": operation}
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
    operation = ({"binary_undo": MODULES[path].capture(baseline, enabled)}
                 if getattr(MODULES.get(path), "BINARY", False)
                 else {"undo": capture_changes(path, baseline, enabled)})
    result = {"version": VERSIONS[path], **operation}
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
