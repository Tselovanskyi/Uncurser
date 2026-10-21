"""Plan verified, data-only mod removal without requiring tested firmware."""
from . import fluidd_screws, mesh, mod_comments, mod_versions, mods, screws, smoothing, warmup


def _data(files, path):
    data = getattr(files.get(path), "data", None)
    return data if isinstance(data, bytes) else None


def _legacy_state(files, path):
    data = _data(files, path)
    if not isinstance(data, bytes):
        return {"known": False, "enabled": False}
    module = mod_versions.MODULES[path]
    value = files[path]
    try:
        return module.inspect(data) if module is mods else module.inspect(data, getattr(value, "original", None))
    except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
        return {"known": False, "enabled": None, "reason": str(error)}


def _present(files, path):
    if _commented(files, path):
        return True
    if path in mod_versions.LEGACY_TARGETS:
        state = _legacy_state(files, path)
        if state.get("enabled") is not False:
            return True
        return path == mods.TARGET and any(marker in (_data(files, path) or b"") for marker in (b"UNCURSER", b"_uncursed_"))
    if path == smoothing.TARGET:
        return any(any(marker in (_data(files, filename) or b"") for marker in
                       (b"UNCURSER BEGIN homing_speed:", b"_UNCURSER_HOME_CENTRE", b"_uncursed_homing", b"_uncursed_first_home"))
                   for filename in mod_versions.FILES[path])
    if path == screws.PYTHON:
        if b"UNCURSER BEGIN bed_screws" in (_data(files, mesh.TARGET) or b""):
            return True
        if b"SCREWS_TILT_SAVE_MESH" in (_data(files, screws.PYTHON) or b""):
            return True
        for filename in (screws.CONFIG, screws.LEGACY_CONFIG):
            if b"Screws Tilt Adjust for K2 Plus" in (_data(files, filename) or b""):
                return True
        data = _data(files, fluidd_screws.TARGET) or b""
        return any(after.encode() in data for _, after in fluidd_screws.blocks(fluidd_screws.TARGET))
    if path == warmup.TARGET:
        return warmup.inspect(_data(files, path) or b"").get("enabled") is True
    return False


def _commented(files, path):
    for filename in mod_versions.FILES.get(path, ()):
        data = _data(files, filename)
        if data is None:
            continue
        try:
            if path in mod_comments.read(data):
                return True
        except (ValueError, TypeError, UnicodeError):
            if path.encode() in data and b" UNCURSER " in data:
                return True
    return False


def _verified(files, path, *, added=False):
    if path not in files:
        raise ValueError("The mod file was not read: " + path)
    value = files[path]
    data = value.data
    if data is None:
        if added and value.sha256 is None:
            return value
        if value.sha256 is not None:
            raise ValueError("The mod file could not be read safely: " + path)
        raise ValueError("A modified mod file is missing: " + path)
    if not isinstance(data, bytes) or mods.digest(data) != value.sha256:
        raise ValueError("The working file checksum could not be verified: " + path)
    if value.backup_error:
        raise ValueError("The original backup needs attention: " + value.backup_error)
    if added:
        if not value.original_absent or value.original_hash != mods.digest(b""):
            raise ValueError("The added file has no verified original absence proof: " + path)
    elif (not isinstance(value.original, bytes) or value.original_hash != mods.digest(value.original)
          or getattr(value, "original_absent", False)):
        raise ValueError("The verified original backup is missing: " + path)
    return value


def _undo(path, value, entry, owner):
    data = value.data
    regular, generated = mesh.split(data) if path.endswith(".cfg") else (data.decode("utf-8"), "")
    original = mesh.split(value.original)[0] if path.endswith(".cfg") else value.original.decode("utf-8")
    # A fully restored original is proof that insertion-only edits are absent.
    if regular == original:
        return data
    for change in reversed(entry["undo"]):
        before, after = change["before"], change["after"]
        matches = regular.count(after)
        if matches == 1:
            regular = regular.replace(after, before, 1)
        elif (matches == 0 and mod_versions.optional_metadata_change(change, owner)
              and owner not in mod_comments.read(regular.encode("utf-8"))):
            continue  # Supplemental metadata may be missing; owned code may not.
        elif matches == 0 and before and regular.count(before) == 1:
            continue  # Firmware already restored this exact owned region.
        else:
            raise ValueError("The owned code was edited, duplicated, or is ambiguously absent: " + path)
    result = (regular + generated).encode("utf-8")
    if path.endswith(".cfg") and mesh.split(result)[1] != generated:
        raise ValueError("Removal would change generated calibration data: " + path)
    return result


def _recorded(files, path, record, stock=None):
    scope = mod_versions.file_scope(path, record)
    entries = record["files"] if "files" in record else {path: {"undo": record["undo"]}}
    result = {}
    for filename in scope:
        entry = entries[filename]
        if "created" in entry:
            value = _verified(files, filename, added=True)
            if value.data is not None and not mod_versions.created_matches(value.data, entry["created"], path):
                raise ValueError("An added mod file differs from its uninstall record: " + filename)
            result[filename] = None
            if (value.data is not None
                    and (path not in mod_versions.VERSIONS or (stock and stock.get("available")))):
                from .stock_recovery import stock_data
                native = stock_data(stock, filename)
                if native is not None:
                    if value.data != native:
                        raise ValueError("The current firmware supplies this file; automatic deletion is blocked: " + filename)
                    result[filename] = value.data
        else:
            value = _verified(files, filename)
            result[filename] = _undo(filename, value, entry, path)
    return result


def _missing_recovery_data(files, path, record):
    scope = mod_versions.file_scope(path, record)
    if path == screws.PYTHON:
        scope = tuple(dict.fromkeys((*scope, screws.CONFIG, screws.LEGACY_CONFIG)))
    missing = record is None
    for filename in scope:
        value = files.get(filename)
        if value is None:
            continue
        if value.backup_error:
            return False
        original, digest = value.original, value.original_hash
        if original is not None:
            if not isinstance(original, bytes) or mods.digest(original) != digest:
                return False
        elif digest is not None:
            if not value.original_absent or digest != mods.digest(b""):
                return False
        else:
            missing = True
    return missing


def inspect(files, records, *, metadata_error="", stock=None, identity=None, header_fallback=False):
    """Return each mod's presence, removal eligibility, edits and blocking reason."""
    if not isinstance(records, dict):
        records, metadata_error = {}, metadata_error or "Installed mod records could not be verified."
    if not metadata_error:
        try:
            mod_versions.dump(records)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            metadata_error = "Installed mod records need attention: " + str(error)
    result = {}
    for path in dict.fromkeys((*mod_versions.VERSIONS, *records)):
        present = path in records or _present(files, path)
        value = result[path] = {"available": False, "files": {}, "reason": "", "present": present,
                                "recovery": False, "defaults": False, "details": ""}
        try:
            if not present:
                continue
            if metadata_error:
                raise ValueError(metadata_error)
            record = records.get(path)
            if record is None:
                if header_fallback and _commented(files, path):
                    from .stock_recovery import header_plan
                    value.update(header_plan(files, path, stock, identity=identity))
                    continue
                if path not in mod_versions.LEGACY_TARGETS:
                    raise ValueError("Unknown installed mod version: verified uninstall instructions are missing.")
                state = _legacy_state(files, path)
                if not state.get("known") or state.get("enabled") is not True:
                    raise ValueError("The unstamped mod is partial or unrecognized; automatic removal is unavailable.")
                current = _verified(files, path)
                baseline = mod_versions.disabled(path, current.data, current.original)
                record = mod_versions.capture(path, baseline, current.data)
            value.update(available=True, files=_recorded(files, path, record, stock))
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, SyntaxError) as error:
            value["reason"] = str(error) or "This mod cannot be removed safely."
        finally:
            if (stock and path in mod_versions.VERSIONS and not metadata_error and not value["available"]
                    and not (header_fallback and _commented(files, path))):
                try:
                    if _missing_recovery_data(files, path, records.get(path)):
                        from . import stock_recovery
                        value.update(stock_recovery.plan(files, path, stock, records.get(path)))
                except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, SyntaxError) as error:
                    if value["present"]:
                        value["reason"] = str(error) or "Current-firmware recovery is unavailable."
    return result
