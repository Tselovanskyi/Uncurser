"""Recover known mod-owned regions using verified current-firmware defaults."""
import fnmatch
import posixpath
import re
from dataclasses import replace

from . import (config_blocks, fluidd_screws, kamp, mesh, mesh_temperature, mod_comments,
               mod_versions, mods, pa, screws, smoothing, warmup)


def stock_data(stock, path):
    if not stock or not stock.get("available"):
        raise ValueError((stock or {}).get("reason") or "Verified current-firmware defaults are unavailable.")
    entry = stock.get("files", {}).get(path)
    if not isinstance(entry, dict) or entry.get("available") is False:
        raise ValueError((entry or {}).get("reason") or "The firmware default could not be verified: " + path)
    data, source = entry.get("data"), entry.get("source")
    expected = mods.digest(data) if isinstance(data, bytes) else None
    if ((data is not None and not isinstance(data, bytes)) or entry.get("sha256") != expected
            or not isinstance(source, str) or source not in stock.get("guards", {})
            or stock["guards"][source] != expected or (data is None and entry.get("absent") is not True)):
        raise ValueError("The firmware default or its absence proof is invalid: " + path)
    return data


def current_data(files, path, *, absent=False):
    value = files.get(path)
    if value is None:
        raise ValueError("The working file was not read: " + path)
    data = value.data
    if data is None and absent and value.sha256 is None:
        return None
    if not isinstance(data, bytes) or mods.digest(data) != value.sha256:
        raise ValueError("The working file could not be verified: " + path)
    return data


def _baseline(source, path):
    return source(path) if callable(source) else stock_data(source, path)


def regular(path, data):
    return mesh.split(data)[0] if path.endswith(".cfg") else data.decode("utf-8")


def owned_record(path, data, reference, entry, normalized=(), *, owner=None):
    """A stock reset may remove owned edits; edited context remains a conflict."""
    if entry is None or regular(path, data) == regular(path, reference):
        return
    text = regular(path, data)
    owner = owner or path
    for change in entry["undo"]:
        before, after = change["before"], change["after"]
        count = text.count(after)
        if count == 1:
            continue
        if (count == 0 and mod_versions.optional_metadata_change(change, owner)
                and owner not in mod_comments.read(text.encode("utf-8"))):
            continue
        if count == 0 and ((before and text.count(before) == 1)
                           or any(regular(path, candidate).count(after) == 1 for candidate in normalized)):
            continue
        raise ValueError("The recorded owned code was edited, duplicated or is ambiguous: " + path)


def early(files, stock, entries):
    path = mods.TARGET
    data, reference = current_data(files, path), _baseline(stock, path)
    if reference is None:
        raise ValueError("The firmware's early-heating module is missing.")
    base, applied = mods.strip_mod(data)
    _, native = mods.strip_mod(reference)
    if native:
        return {path: data}, False
    mods.transform(reference, True)  # Verify the current firmware's native anchors.
    normalized = mods.transform(base, True)
    owned_record(path, data, reference, entries.get(path), (normalized,))
    return {path: base}, applied


def bed_mesh(files, stock, entries, *, partial=False):
    path = mesh.TARGET
    data, reference = current_data(files, path), _baseline(stock, path)
    if reference is None:
        raise ValueError("The firmware's printer configuration is missing.")
    current, default = mesh.Document(data), mesh.Document(reference)
    changed = {}
    for key, value in current.values.items():
        if value == default.values[key]:
            continue
        allowed = {mesh.numeric(key, mesh.VALUES[key])}
        if key == ("bed_mesh", "speed") and not partial:
            allowed.add(mesh.numeric(key, "700"))
        if value not in allowed:
            raise ValueError("A bed-mesh setting is neither the firmware default nor a known mod value: " + key[1])
        changed[key] = default.raw[key]
    if changed and path not in entries and not partial and not any(all(
            current.values[key] == mesh.numeric(key, "700" if key == ("bed_mesh", "speed") and legacy else value)
            for key, value in mesh.VALUES.items()) for legacy in (False, True)):
        raise ValueError("A partial bed-mesh preset has no verified ownership record.")
    normalized = (current.edit(mesh.VALUES), current.edit({**mesh.VALUES, ("bed_mesh", "speed"): "700"}))
    owned_record(path, data, reference, entries.get(path), normalized)
    return {path: current.edit(changed)}, bool(changed)


def temperature(files, stock, entries):
    path = mesh_temperature.TARGET
    data, reference = current_data(files, path), _baseline(stock, path)
    if reference is None:
        raise ValueError("The firmware's print-start macro is missing.")
    restored = mesh_temperature.transform(data, False, reference)
    owned_record(path, data, reference, entries.get(path), (mesh_temperature.transform(data, True),))
    return {path: restored}, regular(path, restored) != regular(path, data)


def homing(files, stock, entries):
    cfg, python = smoothing.TARGET, smoothing.HOMING.TARGET
    data, source = current_data(files, cfg), _baseline(stock, cfg)
    module, reference = current_data(files, python), _baseline(stock, python)
    if source is None or reference is None:
        raise ValueError("The firmware's native homing files are missing.")
    stock_lines, stock_index, _ = smoothing.anchor(source)
    _, python_applied = smoothing.HOMING.strip_mod(module)
    lines, index, _ = smoothing.anchor(data)
    _, native = smoothing.HOMING.strip_mod(reference)
    if native or stock_lines[stock_index].strip() == smoothing.ENABLED_LINE:
        return {cfg: data, python: module}, False
    smoothing.HOMING.transform(reference, True)
    owned_record(cfg, data, source, entries.get(cfg), (smoothing.transform(data, True),))
    owned_record(python, module, reference, entries.get(python), (smoothing.HOMING.transform(module, True),), owner=cfg)
    return {cfg: smoothing.transform(data, False, source), python: smoothing.HOMING.transform(module, False)}, (
        python_applied or lines[index].strip() == smoothing.ENABLED_LINE)


def chamber(files, stock, entries):
    path = warmup.TARGET
    data, reference = current_data(files, path), _baseline(stock, path)
    if reference is None:
        raise ValueError("The firmware's heater controller is missing.")
    native = warmup.inspect(reference, reference)
    state = warmup.inspect(data, reference)
    if not native.get("patchable") or not state.get("patchable"):
        raise ValueError(state.get("reason") or native.get("reason") or "The chamber heating gate has changed.")
    if native["enabled"]:
        return {path: data}, False
    owned_record(path, data, reference, entries.get(path), (warmup.transform(data, True),))
    return {path: warmup.transform(data, False, reference)}, state["enabled"]


def adaptive_purge(files, stock, entries):
    path = kamp.TARGET
    data, reference = current_data(files, path), _baseline(stock, path)
    if reference is None:
        raise ValueError("The firmware's probing module is missing.")
    if data == reference:
        return {path: data}, False
    if mods.digest(reference) != kamp.REFERENCE_HASH:
        raise ValueError("The current firmware's native probing module does not match the verified binary layout.")
    # Exact module hashes verify every native instruction, including all patch
    # sites. A comment or a similar firmware filename cannot authorize removal.
    state = kamp.inspect(data, reference)
    if kamp.fingerprint(data) != kamp.REFERENCE_HASH or not state.get("present"):
        raise ValueError(state.get("reason") or "The probing module is not a verified KAMP installation.")
    return {path: kamp.transform(data, False, reference)}, True


def adaptive_pa(files, stock, entries):
    source, library = pa.TARGET, pa.LIBRARY
    reference, native_library = _baseline(stock, source), _baseline(stock, library)
    if reference is None:
        raise ValueError("The firmware's extruder module is missing.")
    data = current_data(files, source, absent=True)
    binary = current_data(files, library, absent=True)
    result, present = {source: data, library: binary}, False
    native = pa.inspect(reference, reference)
    if data is not None and native.get("enabled") is not True:
        # Saved instructions can uninstall any verified revision. Without them,
        # only the current implementation's exact owned regions are accepted.
        entry = entries.get(source)
        if entry is None:
            enabled = pa.transform(reference, True, reference)
            entry = {"undo": pa.replacements(reference, enabled)}
        else:
            owned_record(source, data, reference, entry)
        restored = recorded_undo(data, reference, entry, source)
        pa.transform(restored, True, reference)
        result[source], present = restored, restored != data
    if binary is not None:
        if native_library is not None:
            if binary != native_library:
                raise ValueError("The current firmware supplies a different PA helper; automatic deletion is blocked.")
        else:
            entry = entries.get(library, {})
            identity = entry.get("created_binary") or mod_versions.binary_created(pa.helper_data())
            if not mod_versions.created_binary_matches(binary, identity):
                raise ValueError("The PA helper differs from its verified installed contents.")
            result[library], present = None, True
    return result, present


def recorded_undo(data, reference, entry, owner):
    """Undo owned regions while accepting ones already restored by firmware."""
    text, stock_text = data.decode("utf-8"), reference.decode("utf-8")
    if text == stock_text:
        return data
    for change in reversed(entry["undo"]):
        before, after = change["before"], change["after"]
        if text.count(after) == 1:
            if (before and stock_text.count(before) != 1) or (not before and after in stock_text):
                raise ValueError("The recorded default differs from the current firmware.")
            text = text.replace(after, before, 1)
        elif (text.count(after) == 0 and mod_versions.optional_metadata_change(change, owner)
              and owner not in mod_comments.read(text.encode("utf-8"))):
            continue
        elif text.count(after) == 0 and before and text.count(before) == 1 and stock_text.count(before) == 1:
            continue
        else:
            raise ValueError("The owned code was edited or is ambiguously incomplete.")
    return text.encode("utf-8")


def ui_undo(data, reference, entry):
    if entry is None:
        return fluidd_screws.recovery_undo(fluidd_screws.TARGET, data, reference)
    return recorded_undo(data, reference, entry, screws.PYTHON)


def helper(files, stock, entries, *, partial=False):
    root = mesh.TARGET
    data = current_data(files, root, absent=partial)
    reference = _baseline(stock, root) if data is not None else None
    if data is not None and reference is None:
        raise ValueError("The firmware's printer configuration is missing.")
    native_include = config_blocks.screws_include_path(mesh.split(reference)[0]) if reference is not None else None
    if native_include is not None and not partial:
        if (config_blocks.screws_include_path(mesh.split(data)[0]) == config_blocks.screws_include_path(mesh.split(reference)[0])
                and all(current_data(files, path, absent=True) == _baseline(stock, path)
                        for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG, fluidd_screws.TARGET))):
            return {root: data}, False
        raise ValueError("The current firmware already supplies the helper include; its OEM feature must be preserved.")
    if native_include is not None and config_blocks.screws_include_path(mesh.split(data)[0]) not in (None, native_include):
        raise ValueError("The helper include differs from the current firmware's native feature.")
    result = {root: screws.edit_include(data, False) if data is not None and native_include is None else data}
    applied = result[root] != data
    if data is not None:
        newline = "\r\n" if b"\r\n" in data else "\n"
        normalized = tuple((block.replace("\n", newline) + mesh.split(result[root])[0] + mesh.split(result[root])[1]).encode()
                           for block in (config_blocks.SCREWS_INCLUDE, config_blocks.LEGACY_SCREWS_INCLUDE))
        owned_record(root, data, reference, entries.get(root), normalized, owner=screws.PYTHON)
    for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG):
        current = current_data(files, path, absent=True)
        if current is None and partial:
            result[path] = None
            continue
        default = _baseline(stock, path)
        if default is not None:
            if current != default:
                raise ValueError("The current firmware supplies this helper file; automatic deletion is blocked: " + path)
            result[path] = current
            continue
        if current is not None:
            if partial and mod_comments.read(current):
                raise ValueError("Another mod still owns the added helper file: " + path)
            entry = entries.get(path)
            if ((entry is not None and not mod_versions.created_matches(current, entry.get("created", ""), screws.PYTHON))
                    or (entry is None and not screws.recovery_recognized(path, current))):
                raise ValueError("An added helper file was edited or cannot be recognized: " + path)
            applied = True
        result[path] = None
    remaining = mesh.split(result[root])[0] if result[root] is not None else ""
    for line in remaining.splitlines():
        match = re.fullmatch(r"\s*\[include ([^\]]+)\]\s*(?:[#;].*)?", line)
        if match:
            pattern = posixpath.normpath(posixpath.join(posixpath.dirname(root), match[1].strip()))
            if any(result.get(path) is None and fnmatch.fnmatchcase(path, pattern) for path in (screws.CONFIG, screws.LEGACY_CONFIG)):
                raise ValueError("Another configuration include still requires a helper file.")
    path = fluidd_screws.TARGET
    current = current_data(files, path, absent=partial)
    if current is None:
        result[path] = None
        return result, applied
    default = _baseline(stock, path)
    if default is None:
        raise ValueError("The firmware's helper interface could not be verified.")
    result[path] = ui_undo(current, default, entries.get(path))
    applied |= result[path] != current
    return result, applied


def plan(files, path, stock, record=None):
    entries = (record["files"] if "files" in record else {path: mod_versions.entry(record)}) if record else {}
    operation = {mods.TARGET: early, mesh.TARGET: bed_mesh, mesh_temperature.TARGET: temperature,
                 smoothing.TARGET: homing, screws.PYTHON: helper, warmup.TARGET: chamber,
                 kamp.TARGET: adaptive_purge, pa.TARGET: adaptive_pa}[path]
    result, present = operation(files, stock, entries)
    result = {filename: mod_comments.remove(data, path) if data is not None and not filename.endswith(".so") else data
              for filename, data in result.items()}
    for filename, data in result.items():
        if filename.endswith(".cfg") and data is not None:
            if mesh.split(data)[1] != mesh.split(current_data(files, filename))[1]:
                raise ValueError("Stock recovery would change generated calibration data: " + filename)
    identity = stock.get("identity", {})
    firmware = identity.get("firmware", "current")
    variant = " (" + identity["variant"] + ")" if identity.get("variant") else ""
    return {"available": bool(present or record), "present": bool(present or record), "files": result,
            "reason": "", "recovery": True, "defaults": True,
            "details": f"Uses verified firmware {firmware}{variant} defaults where restoration data is missing. Unrelated edits are preserved."}


def header_plan(files, path, stock=None, *, identity=None):
    """Validate surviving current-code regions; a comment alone never owns code."""
    scope = mod_versions.FILES[path]
    scanned = tuple(dict.fromkeys((*scope, screws.LEGACY_CONFIG))) if path == screws.PYTHON else scope
    headers, clean = [], {}
    for filename in scanned:
        data = current_data(files, filename, absent=True)
        if data is not None and not filename.endswith(".so"):
            stamp = mod_comments.read(data).get(path)
            if stamp is not None:
                headers.append(stamp)
            data = mod_comments.remove(data, path)
        clean[filename] = replace(files[filename], data=data, sha256=mods.digest(data) if data is not None else None)
    if not headers:
        raise ValueError("Verified uninstall instructions and source comments are missing.")
    stamp = headers[0]
    if (any(value != stamp for value in headers)
            or (path != screws.PYTHON and stamp["version"] != mod_versions.VERSIONS[path])):
        raise ValueError("Source comments have conflicting or unsupported mod revisions.")
    if identity is None:
        current = (stock or {}).get("identity", {})
        identity = (current.get("model"), current.get("firmware"))
    if not identity or identity[0] != "F008" or not identity[1]:
        raise ValueError("The current printer firmware identity could not be verified.")
    mod_versions.validate_firmware(identity[1])
    native_identity = (stock or {}).get("identity", {})
    if stock and stock.get("available") and (native_identity.get("model"), native_identity.get("firmware")) != identity:
        raise ValueError("The current firmware sources do not match the printer.")

    rom_files = set()

    def baseline(filename):
        value = files[filename]
        if value.backup_error:
            raise ValueError("The original backup needs attention: " + value.backup_error)
        if getattr(value, "original_identity", None) == identity:
            if value.original_absent and value.original is None and value.original_hash == mods.digest(b""):
                return None
            if (not value.original_absent and isinstance(value.original, bytes)
                    and value.original_hash == mods.digest(value.original)):
                return value.original if filename.endswith(".so") else mod_comments.strip(value.original)
        data = stock_data(stock, filename)
        rom_files.add(filename)
        return (data if filename.endswith(".so") else mod_comments.strip(data)) if data is not None else None

    if path == smoothing.TARGET:
        result = {}
        for module in (smoothing, smoothing.HOMING):
            filename = module.TARGET
            data = current_data(clean, filename, absent=True)
            if data is None:
                result[filename] = None
                continue
            reference = baseline(filename)
            if reference is None:
                raise ValueError("The native homing source is missing: " + filename)
            if module is smoothing:
                lines, index, _ = smoothing.anchor(reference)
                smoothing.anchor(data)
                result[filename] = data if lines[index].strip() == smoothing.ENABLED_LINE else smoothing.transform(data, False, reference)
            else:
                _, native = module.strip_mod(reference)
                module.strip_mod(data)
                module.transform(reference, True)
                result[filename] = data if native else module.transform(data, False)
    elif path == screws.PYTHON:
        result, _ = helper(clean, baseline, {}, partial=True)
    elif path == pa.TARGET:
        result, _ = adaptive_pa(clean, baseline, {})
    elif current_data(clean, path, absent=True) is None:
        result = {path: None}
    else:
        operation = {mods.TARGET: early, mesh.TARGET: bed_mesh,
                     mesh_temperature.TARGET: temperature, warmup.TARGET: chamber}[path]
        result, _ = operation(clean, baseline, {}, **({"partial": True} if path == mesh.TARGET else {}))
    for filename, data in result.items():
        if data is not None and filename.endswith(".cfg") and mesh.split(data)[1] != mesh.split(files[filename].data)[1]:
            raise ValueError("Comment recovery would change generated calibration data: " + filename)
    return {"available": True, "present": True, "files": result, "reason": "", "recovery": True,
            "defaults": bool(rom_files), "recovery_files": tuple(sorted(rom_files)), "header": True, **stamp,
            "incomplete": any(files[filename].data is None for filename in scope),
            "details": "Verifies surviving mod code using source comments and current-firmware restoration sources. Missing files remain absent."}
