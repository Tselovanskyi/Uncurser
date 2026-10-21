"""Read-only UI analysis cached for one completed, unchanged scan snapshot.

Apply never uses this cache: its fresh scans and validation remain authoritative.
"""
from . import engine, mod_versions


def check(snapshot, key):
    if snapshot is None:
        return _calculate(None, key)
    cache = getattr(snapshot, "_ui_checks", None)
    if cache is None:
        cache = snapshot._ui_checks = {}
    if key not in cache:
        cache[key] = _calculate(snapshot, key)
    return cache[key]


def set_ignored(snapshot, targets):
    """Acknowledge unknown IDs for this connection without changing printer data."""
    snapshot.ignored_mods = tuple(sorted(targets))
    cache = getattr(snapshot, "_ui_checks", None)
    if cache is not None:
        cache.pop("issues", None)


def _calculate(snapshot, key):
    if key == "removals":
        return engine.removal_statuses(snapshot) if snapshot is not None else {}
    if key == "updates":
        return engine.update_statuses(snapshot) if snapshot is not None else {}
    if key == "issues":
        return engine.firmware_issues(snapshot, removals=check(snapshot, "removals")) if snapshot is not None else {}
    if isinstance(key, tuple) and len(key) == 2:
        kind, value = key
        if kind == "version":
            return engine.version_details(snapshot, value) if snapshot is not None else ""
        if kind == "restore":
            return engine.restorable(snapshot, value) if snapshot is not None else set()
    raise KeyError("Unknown UI check: " + repr(key))


def populate(snapshot):
    if snapshot is not None:
        for key in ("removals", "updates", "issues",
                    *(("version", path) for path in mod_versions.VERSIONS),
                    ("restore", False), ("restore", True)):
            check(snapshot, key)
    return snapshot
