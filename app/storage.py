"""Printer-side storage paths and read-only discovery of adjacent legacy data."""
import hashlib
import json
import posixpath
import re


ROOT = "/mnt/UDISK/Uncurser"
RECORDS = ROOT + "/Uncurser_mods.json"
LEGACY_RECORDS = "/mnt/UDISK/printer_data/config/Uncurser_mods.json"
_ORIGINALS = ROOT + "/originals"
_MARKERS = {".py": ".ptn", ".cfg": ".cfn", ".js": ".jfn", ".json": ".jsn"}
_ATTRS = ("mode", "uid", "gid")


def _absolute(path):
    if (not isinstance(path, str) or not path.startswith("/")
            or path.startswith("//") or "\\" in path or "\0" in path
            or posixpath.normpath(path) != path):
        raise ValueError("Expected a normalized absolute printer path.")
    return path


def original(target):
    """Keep each baseline's old filename under a collision-free mirrored path."""
    _absolute(target)
    if target == RECORDS:
        target = LEGACY_RECORDS
    stem, suffix = posixpath.splitext(target)
    if suffix not in _MARKERS:
        raise ValueError("Unsupported original-backup file type: " + target)
    return _ORIGINALS + stem + _MARKERS[suffix] + ".Original.txt"


def recovery(target, digest):
    """A pre-repair copy is separate from the immutable original baseline."""
    _absolute(target)
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid recovery-copy checksum.")
    stem, suffix = posixpath.splitext(target)
    if suffix not in _MARKERS:
        raise ValueError("Unsupported recovery file type: " + target)
    return ROOT + "/recovery" + stem + _MARKERS[suffix] + "." + digest + ".txt"


def legacy_path(canonical):
    """Return only the old storage location corresponding to a canonical path."""
    _absolute(canonical)
    if canonical == RECORDS:
        return LEGACY_RECORDS
    if canonical.startswith(_ORIGINALS + "/"):
        path = canonical[len(_ORIGINALS):]
        # Limit aliases to baseline files and their manifests, never arbitrary data.
        filename = posixpath.basename(path)
        if any(filename.endswith(marker + ".Original.txt" + manifest)
               for marker in _MARKERS.values() for manifest in ("", ".json")):
            return path
    return None


def scan_paths(paths):
    """Prefetch both locations without changing any printer files."""
    result = []
    for path in paths:
        _absolute(path)
        result.append(path)
        legacy = legacy_path(path)
        if legacy is not None:
            result.append(legacy)
    return list(dict.fromkeys(result))


def _verified(value, path):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("data"), bytes):
        raise ValueError("Invalid printer file response: " + path)
    result = dict(value)
    digest = hashlib.sha256(result["data"]).hexdigest()
    if "sha256" in result and result["sha256"] != digest:
        raise ValueError("The printer file does not match its reported hash: " + path)
    result["sha256"] = digest
    for key in _ATTRS:
        if key in result and (type(result[key]) is not int or result[key] < 0
                              or (key == "mode" and result[key] > 0o7777)):
            raise ValueError("Invalid printer file attributes: " + path)
    return result


def obsolete_originals(remote, targets, identity):
    """Identify obsolete or damaged reserved original pairs without changing them."""
    from . import mod_versions, network, screws
    model, firmware = identity
    current = network.device_identity({"native": {"model": model, "modelVersion": firmware}})
    if not all(current) or current != identity:
        return ()
    allowed = {path for scope in mod_versions.FILES.values() for path in scope} | {screws.LEGACY_CONFIG}
    raw = remote.remote
    result = []
    for target in sorted(set(targets)):
        if target not in allowed or target == RECORDS:
            continue
        canonical = original(target)
        candidates = (canonical, legacy_path(canonical))
        copies, plans = {}, []
        preserve_copy = False
        try:
            for backup in candidates:
                pair = [_verified(raw.read(path), path) for path in (backup, backup + ".json")]
                copies[backup] = pair
                if all(value is None for value in pair):
                    continue
                if any(value is not None and set(_ATTRS) - value.keys() for value in pair):
                    raise ValueError("Original backup file attributes are incomplete.")
                saved, manifest = pair
                try:
                    meta = json.loads(manifest["data"]) if manifest is not None else None
                except (ValueError, UnicodeError):
                    meta = None
                valid = (isinstance(meta, dict) and meta.get("path") == target
                         and isinstance(meta.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", meta["sha256"])
                         and type(meta.get("absent", False)) is bool
                         and all(type(meta.get(key)) is int and meta[key] >= 0 for key in _ATTRS)
                         and meta["mode"] <= 0o7777)
                previous = network.device_identity({"native": {"model": meta.get("model"), "modelVersion": meta.get("firmware")}}) if valid else ("", "")
                reason = ""
                if saved is None or manifest is None or not valid:
                    reason = "metadata"
                elif previous[0] and previous[0] != model:
                    preserve_copy = True
                    continue
                elif not all(previous):
                    reason = "metadata"
                elif previous[0] == model and meta["sha256"] != saved["sha256"]:
                    reason = "checksum"
                elif meta.get("absent", False) and saved["data"] != b"":
                    reason = "metadata"
                elif tuple(map(int, previous[1].split("."))) < tuple(map(int, firmware.split("."))):
                    reason = "obsolete"
                if not reason:
                    preserve_copy = True
                    continue
                plans.append({"target": target, "backup": backup, "model": model, "firmware": previous[1] or None,
                    "reason": reason,
                    "files": [{"path": path, "sha256": value["sha256"] if value is not None else None,
                               "attrs": {key: value[key] for key in _ATTRS} if value is not None else None}
                              for path, value in zip((backup, backup + ".json"), pair)]})
            for current_file, legacy_file in zip(copies[candidates[0]], copies[candidates[1]]):
                if current_file is not None and legacy_file is not None and (
                        current_file["data"] != legacy_file["data"]
                        or any(current_file.get(key) != legacy_file.get(key) for key in _ATTRS)):
                    raise ValueError("Conflicting original backup copies.")
            if not preserve_copy:
                result.extend(plans)
        except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, RuntimeError, OSError):
            continue
    return tuple(result)


class Reader:
    """Read canonical data or its legacy copy and plan a guarded migration."""

    def __init__(self, remote):
        self.remote = remote
        self._moves = {}

    @property
    def moves(self):
        return list(self._moves.values())

    def __getattr__(self, name):
        return getattr(self.remote, name)

    def prefetch(self, paths):
        self._moves.clear()
        return self.remote.prefetch(scan_paths(paths))

    def read(self, path):
        _absolute(path)
        current = _verified(self.remote.read(path), path)
        legacy = legacy_path(path)
        if legacy is None:
            return current
        previous = _verified(self.remote.read(legacy), legacy)
        self._moves.pop(path, None)
        if previous is None:
            return current
        if current is not None and (current["data"] != previous["data"]
                or any(key in current and key in previous and current[key] != previous[key]
                       for key in _ATTRS)):
            raise ValueError("Conflicting Uncurser storage copies require review: " + path)
        self._moves[path] = {
            "source": legacy, "destination": path,
            "source_hash": previous["sha256"],
            "destination_hash": current["sha256"] if current is not None else None,
            "attrs": {key: previous[key] for key in _ATTRS if key in previous},
        }
        return current if current is not None else previous
