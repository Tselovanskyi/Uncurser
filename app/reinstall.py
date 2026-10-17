"""Classify current mod compatibility without writing or trusting old scan flags."""
from dataclasses import replace

from . import fluidd_screws, mesh, mod_versions, mods, network, screws, smoothing
from .stock_recovery import current_data, stock_data


_ERRORS = (ValueError, TypeError, KeyError, AttributeError, UnicodeError, SyntaxError)


def _inspect(module, data, original=None, *, native=False):
    state = module.inspect(data) if module is mods else module.inspect(data, original)
    if ((not state.get("known") and not native) or not state.get("patchable")
            or state.get("enabled") is not False):
        raise ValueError(state.get("reason") or "The clean file is not a recognized native baseline.")


def _enable(module, data, original=None, *, native=False):
    _inspect(module, data, original, native=native)
    return module.transform(data, True) if module is mods else module.transform(data, True, original)


def _configuration(snapshot, files, exclude=None):
    included = dict(getattr(snapshot.mesh, "included", {}))
    root, _ = mesh.split(current_data(files, mesh.TARGET))
    sections = mesh.parser(root).sections()
    for path, data in included.items():
        if not isinstance(data, bytes) or mods.digest(data) != snapshot.mesh.guards.get(path):
            raise ValueError("An included configuration file could not be verified: " + path)
    for path, value in files.items():
        if path.endswith(".cfg"):
            if (path in (screws.CONFIG, screws.LEGACY_CONFIG) and path not in included
                    and "include " + path not in sections
                    and not (path == screws.LEGACY_CONFIG and "include screws_tilt_adjust.cfg" in sections)):
                continue
            if value.data is None:
                included.pop(path, None)
            elif path in included and value.data == b"" and value.sha256 == "":
                # Keep the verified tree copy when the independent read failed.
                continue
            else:
                included[path] = current_data(files, path)
    included.pop(exclude, None)
    return included


def _helper(engine, snapshot, files, reference, baseline):
    for path in (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG):
        if stock_data(snapshot.stock, path) is not None or current_data(files, path, absent=True) is not None:
            raise ValueError("A helper file is already supplied or occupied: " + path)
    root = replace(files[mesh.TARGET], data=baseline[mesh.TARGET],
                   analysis=mesh.inspect(baseline[mesh.TARGET], reference[mesh.TARGET]))
    if not root.analysis.get("known") or not root.analysis.get("patchable"):
        raise ValueError("The helper's printer configuration is not recognized.")
    mesh.validate_includes(_configuration(snapshot, files, mesh.TARGET))
    included = _configuration(snapshot, files)
    patterns = {pattern: paths for pattern, paths in snapshot.mesh.patterns.items()
                if pattern not in (screws.CONFIG, screws.LEGACY_CONFIG)}
    state = screws.inspect(root, {path: files[path] for path in
                           (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG)}, included, patterns)
    if not state.get("known") or not state.get("patchable") or state.get("enabled") is not False:
        raise ValueError(state.get("reason") or "The helper configuration conflicts with native configuration.")
    index = stock_data(snapshot.stock, fluidd_screws.INDEX)
    frontend = files[fluidd_screws.TARGET]
    if (index is None or index.count(('src="./' + fluidd_screws.ASSET + '"').encode()) != 1
            or frontend.guards.get(fluidd_screws.INDEX) != mods.digest(index)
            or frontend.analysis.get("dependency_error")
            or any(frontend.guards.get(fluidd_screws.TARGET + suffix, "unread") is not None
                   for suffix in (".gz", ".br"))):
        raise ValueError("The active Fluidd asset or its dependencies cannot be verified.")
    for module in fluidd_screws.FILES:
        for data in (reference[module.TARGET], baseline[module.TARGET]):
            state = fluidd_screws.inspect(module, data)
            if not state.get("known") or not state.get("patchable") or state.get("enabled") is not False:
                raise ValueError(state.get("reason") or "The native helper interface has changed.")
    result = {module.TARGET: screws.payload(module.TARGET) for module in screws.FILES}
    result[mesh.TARGET] = screws.edit_include(baseline[mesh.TARGET], True)
    result[fluidd_screws.TARGET] = fluidd_screws.transform(
        fluidd_screws.TARGET, baseline[fluidd_screws.TARGET], True)
    native = {**result, mesh.TARGET: screws.edit_include(reference[mesh.TARGET], True),
              fluidd_screws.TARGET: fluidd_screws.transform(fluidd_screws.TARGET, reference[fluidd_screws.TARGET], True)}
    engine.capture_helper(reference, native)
    engine.capture_helper(baseline, result)
    return result


def eligible(snapshot, issues):
    """Return active issue mods whose current implementation can be reinstalled.

    This is a code/firmware compatibility check. After removal, installation
    still requires a fresh scan and the normal include, ownership and idle checks.
    """
    from . import engine

    if snapshot is None or network.firmware_state(snapshot.status) == "unsupported":
        return set()
    stock = snapshot.stock or {}
    identity = network.device_identity(snapshot.status)
    if (not stock.get("available") or not stock.get("proof")
            or (stock.get("identity", {}).get("model"), stock.get("identity", {}).get("firmware")) != identity):
        return set()
    choices = {path: False for path, state in issues.items()
               if path in mod_versions.VERSIONS and state.get("present") and state.get("available")}
    if not choices:
        return set()
    try:
        view = engine.cleanup_snapshot(snapshot)
        prepared = engine.prepare_removal(view, choices)
        files = {module.TARGET: value for module, value in engine.file_entries(view) if value is not None}
        for path, data in prepared.items():
            files[path] = replace(files[path], data=data, sha256=mods.digest(data) if data is not None else None)
        retained = engine.installed_records(view, removal=True)
        retained = {path: record for path, record in retained.items() if path not in choices}
    except _ERRORS:
        return set()
    result = set()
    for path in choices:
        if path == mods.TARGET and not snapshot.identity.get("history"):
            continue
        try:
            scope = mod_versions.FILES[path]
            reference = {filename: stock_data(stock, filename) for filename in scope}
            baseline = {filename: current_data(files, filename, absent=filename in mod_versions.ADDED_FILES[path])
                        for filename in scope}
            if path == screws.PYTHON:
                enabled = _helper(engine, snapshot, files, reference, baseline)
            else:
                modules = (smoothing, smoothing.HOMING) if path == smoothing.TARGET else (mod_versions.MODULES[path],)
                enabled, native = {}, {}
                for module in modules:
                    filename = module.TARGET
                    native[filename] = _enable(module, reference[filename], reference[filename], native=True)
                    if hasattr(module, "validate_includes"):
                        module.validate_includes(_configuration(snapshot, files, filename))
                    value = files[filename]
                    retired = any(pair["target"] == filename for pair in snapshot.storage_cleanup)
                    original = value.original if not retired and not value.backup_error and value.original_identity == identity else None
                    enabled[filename] = _enable(module, baseline[filename], original)
                    if len(scope) == 1:
                        mod_versions.capture(path, reference[filename], native[filename])
                        mod_versions.capture(path, baseline[filename], enabled[filename])
                if path == smoothing.TARGET:
                    engine.capture_homing(reference, native)
                    engine.capture_homing(baseline, enabled)
            mod_versions.preserve_shared_changes(baseline, enabled, retained, (path,))
            result.add(path)
        except _ERRORS:
            continue
    return result
