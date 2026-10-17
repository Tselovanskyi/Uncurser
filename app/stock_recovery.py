"""Recover known mod-owned regions using verified current-firmware defaults."""
import fnmatch
import posixpath
import re

from . import config_blocks, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing


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


def regular(path, data):
    return mesh.split(data)[0] if path.endswith(".cfg") else data.decode("utf-8")


def owned_record(path, data, reference, entry, normalized=()):
    """A stock reset may remove owned edits; edited context remains a conflict."""
    if entry is None or regular(path, data) == regular(path, reference):
        return
    text = regular(path, data)
    for change in entry["undo"]:
        before, after = change["before"], change["after"]
        count = text.count(after)
        if count == 1:
            continue
        if count == 0 and ((before and text.count(before) == 1)
                           or any(regular(path, candidate).count(after) == 1 for candidate in normalized)):
            continue
        raise ValueError("The recorded owned code was edited, duplicated or is ambiguous: " + path)


def early(files, stock, entries):
    path = mods.TARGET
    data, reference = current_data(files, path), stock_data(stock, path)
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


def bed_mesh(files, stock, entries):
    path = mesh.TARGET
    data, reference = current_data(files, path), stock_data(stock, path)
    if reference is None:
        raise ValueError("The firmware's printer configuration is missing.")
    current, default = mesh.Document(data), mesh.Document(reference)
    changed = {}
    for key, value in current.values.items():
        if value == default.values[key]:
            continue
        allowed = {mesh.numeric(key, mesh.VALUES[key])}
        if key == ("bed_mesh", "speed"):
            allowed.add(mesh.numeric(key, "700"))
        if value not in allowed:
            raise ValueError("A bed-mesh setting is neither the firmware default nor a known mod value: " + key[1])
        changed[key] = default.raw[key]
    if changed and path not in entries and not any(all(
            current.values[key] == mesh.numeric(key, "700" if key == ("bed_mesh", "speed") and legacy else value)
            for key, value in mesh.VALUES.items()) for legacy in (False, True)):
        raise ValueError("A partial bed-mesh preset has no verified ownership record.")
    normalized = (current.edit(mesh.VALUES), current.edit({**mesh.VALUES, ("bed_mesh", "speed"): "700"}))
    owned_record(path, data, reference, entries.get(path), normalized)
    return {path: current.edit(changed)}, bool(changed)


def temperature(files, stock, entries):
    path = mesh_temperature.TARGET
    data, reference = current_data(files, path), stock_data(stock, path)
    if reference is None:
        raise ValueError("The firmware's print-start macro is missing.")
    stock_lines, stock_index = mesh_temperature.condition(reference)
    lines, index = mesh_temperature.condition(data)
    if stock_lines[stock_index].strip() == mesh_temperature.ENABLED_CONDITION:
        return {path: data}, False
    owned_record(path, data, reference, entries.get(path), (mesh_temperature.transform(data, True),))
    applied = lines[index].strip() == mesh_temperature.ENABLED_CONDITION
    return {path: mesh_temperature.transform(data, False, reference)}, applied


def homing(files, stock, entries):
    cfg, python = smoothing.TARGET, smoothing.HOMING.TARGET
    data, source = current_data(files, cfg), stock_data(stock, cfg)
    module, reference = current_data(files, python), stock_data(stock, python)
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
    owned_record(python, module, reference, entries.get(python), (smoothing.HOMING.transform(module, True),))
    return {cfg: smoothing.transform(data, False, source), python: smoothing.HOMING.transform(module, False)}, (
        python_applied or lines[index].strip() == smoothing.ENABLED_LINE)


def ui_undo(data, reference, entry):
    if entry is None:
        fluidd_screws.strip_mod(fluidd_screws.TARGET, data)
        _, native = fluidd_screws.strip_mod(fluidd_screws.TARGET, reference)
        if native:
            return data
        for before, _ in fluidd_screws.blocks(fluidd_screws.TARGET):
            if reference.decode().count(before) != 1:
                raise ValueError("The firmware's native helper interface has changed.")
        return fluidd_screws.transform(fluidd_screws.TARGET, data, False)
    text, stock_text = data.decode("utf-8"), reference.decode("utf-8")
    if text == stock_text:
        return data
    for change in reversed(entry["undo"]):
        before, after = change["before"], change["after"]
        if text.count(after) == 1:
            if (before and stock_text.count(before) != 1) or (not before and after in stock_text):
                raise ValueError("The stored helper-interface default differs from the current firmware.")
            text = text.replace(after, before, 1)
        elif text.count(after) == 0 and before and text.count(before) == 1 and stock_text.count(before) == 1:
            continue
        else:
            raise ValueError("The helper interface was edited or is ambiguously incomplete.")
    return text.encode("utf-8")


def helper(files, stock, entries):
    root = mesh.TARGET
    data, reference = current_data(files, root), stock_data(stock, root)
    if reference is None:
        raise ValueError("The firmware's printer configuration is missing.")
    if config_blocks.screws_include_path(mesh.split(reference)[0]) is not None:
        if (config_blocks.screws_include_path(mesh.split(data)[0]) == config_blocks.screws_include_path(mesh.split(reference)[0])
                and all(current_data(files, path, absent=True) == stock_data(stock, path)
                        for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG, fluidd_screws.TARGET))):
            return {root: data}, False
        raise ValueError("The current firmware already supplies the helper include; its OEM feature must be preserved.")
    result = {root: screws.edit_include(data, False)}
    applied = result[root] != data
    newline = "\r\n" if b"\r\n" in data else "\n"
    normalized = tuple((block.replace("\n", newline) + mesh.split(result[root])[0] + mesh.split(result[root])[1]).encode()
                       for block in (config_blocks.SCREWS_INCLUDE, config_blocks.LEGACY_SCREWS_INCLUDE))
    owned_record(root, data, reference, entries.get(root), normalized)
    for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG):
        current, default = current_data(files, path, absent=True), stock_data(stock, path)
        if default is not None:
            if current != default:
                raise ValueError("The current firmware supplies this helper file; automatic deletion is blocked: " + path)
            result[path] = current
            continue
        if current is not None:
            entry = entries.get(path)
            if ((entry is not None and entry.get("created", "").encode() != current)
                    or (entry is None and not screws.recognized(path, current))):
                raise ValueError("An added helper file was edited or cannot be recognized: " + path)
            applied = True
        result[path] = None
    remaining, _ = mesh.split(result[root])
    for line in remaining.splitlines():
        match = re.fullmatch(r"\s*\[include ([^\]]+)\]\s*(?:[#;].*)?", line)
        if match:
            pattern = posixpath.normpath(posixpath.join(posixpath.dirname(root), match[1].strip()))
            if any(result.get(path) is None and fnmatch.fnmatchcase(path, pattern) for path in (screws.CONFIG, screws.LEGACY_CONFIG)):
                raise ValueError("Another configuration include still requires a helper file.")
    path = fluidd_screws.TARGET
    current, default = current_data(files, path), stock_data(stock, path)
    if default is None:
        raise ValueError("The firmware's helper interface could not be verified.")
    result[path] = ui_undo(current, default, entries.get(path))
    applied |= result[path] != current
    return result, applied


def plan(files, path, stock, record=None):
    entries = record.get("files", {path: {"undo": record.get("undo", [])}}) if record else {}
    operation = {mods.TARGET: early, mesh.TARGET: bed_mesh, mesh_temperature.TARGET: temperature,
                 smoothing.TARGET: homing, screws.PYTHON: helper}[path]
    result, present = operation(files, stock, entries)
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
