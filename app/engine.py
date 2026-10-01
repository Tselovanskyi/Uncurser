"""Scan, prepare and apply. Scan never writes to the printer."""

import base64
import difflib
import json
import time
from dataclasses import dataclass, field

from . import mesh, mesh_temperature, mods, network
from .remote import APPLY, APPLY_BATCH, CONFIG_TREE, Remote


@dataclass
class MeshSnapshot:
    data: bytes
    sha256: str
    analysis: dict
    original: bytes | None = None
    original_hash: str | None = None
    backup_error: str = ""
    guards: dict = field(default_factory=dict)
    patterns: dict = field(default_factory=dict)
    stable_guards: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    data: bytes
    sha256: str
    analysis: dict
    original: bytes | None
    original_hash: str | None
    backup_error: str
    status: dict | None
    status_error: str
    identity: dict
    scanned_at: float
    mesh: MeshSnapshot | None = None
    mesh_temperature: MeshSnapshot | None = None


def scan_config(remote, module, tree):
    try:
        if isinstance(tree, Exception):
            raise ValueError(str(tree))
        files = dict(tree["files"])
        current = files.pop(module.TARGET, None)
        if current is None:
            raise ValueError("This file is not in the active configuration: " + module.TARGET)
        data = base64.b64decode(current["data"])
        included = {path: base64.b64decode(value["data"]) for path, value in files.items()}
        original = remote.read(module.ORIGINAL)
        manifest = remote.read(module.MANIFEST)
        backup_error = ""
        if original is not None or manifest is not None:
            try:
                if original is None or manifest is None:
                    raise ValueError("An original backup or its manifest is missing.")
                meta = json.loads(manifest["data"])
                if meta.get("path") != module.TARGET or meta.get("sha256") != original["sha256"]:
                    raise ValueError("Original backup checksum does not match its manifest.")
                if any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid")):
                    raise ValueError("Original backup attributes are incomplete.")
                module.validate(original["data"])
            except (ValueError, UnicodeError) as error:
                backup_error = str(error)
        saved = original["data"] if original else None
        analysis = module.inspect(data, saved if not backup_error else None)
        try:
            module.validate_includes(included)
        except ValueError as error:
            analysis = {**analysis, "known": False, "patchable": False, "label": "Conflict", "reason": str(error)}
        return MeshSnapshot(data, current["sha256"], analysis, saved, original["sha256"] if original else None,
                            backup_error, {path: value["sha256"] for path, value in files.items()}, tree["patterns"],
                            {path: mods.digest(mesh.split(value)[0].encode()) for path, value in included.items()})
    except (ValueError, RuntimeError, OSError) as error:
        return MeshSnapshot(b"", "", {"known": False, "enabled": None, "patchable": False,
                                      "label": "Unavailable", "reason": str(error)})


def scan(connection, known_hosts, progress=lambda message: None):
    progress("Reading current printer files…")
    with Remote(connection, known_hosts) as remote:
        current = remote.read(mods.TARGET)
        if current is None:
            raise RuntimeError("The supported K2 Plus file was not found: " + mods.TARGET)
        identity = remote.identity()
        original = remote.read(mods.ORIGINAL)
        manifest = remote.read(mods.MANIFEST)
        try:
            tree = remote.python(CONFIG_TREE, {"path": mesh.TARGET})
        except (ValueError, RuntimeError, OSError) as error:
            tree = error
        mesh_snapshot = scan_config(remote, mesh, tree)
        temperature_snapshot = scan_config(remote, mesh_temperature, tree)
    analysis = mods.inspect(current["data"])
    backup_error = ""
    if original is not None or manifest is not None:
        try:
            if original is None or manifest is None:
                raise ValueError("An original backup or its manifest is missing.")
            meta = json.loads(manifest["data"])
            if meta.get("path") != mods.TARGET or meta.get("sha256") != original["sha256"]:
                raise ValueError("The original backup checksum does not match its manifest.")
            for key in ("mode", "uid", "gid"):
                if not isinstance(meta.get(key), int):
                    raise ValueError("The original backup has incomplete file attributes.")
            mods.validate(original["data"])
        except (ValueError, KeyError, SyntaxError) as error:
            backup_error = str(error)
    progress("Reading printer status…")
    state, status_error = None, ""
    try:
        state = network.status(connection.host)
    except Exception as error:
        status_error = str(error)
    return Snapshot(current["data"], current["sha256"], analysis,
                    original["data"] if original else None, original["sha256"] if original else None,
                    backup_error, state, status_error, identity, time.time(), mesh_snapshot, temperature_snapshot)


def prepare(snapshot, desired, restore=False, full_risk=False):
    network.require_supported(snapshot.status)
    if snapshot.backup_error:
        raise ValueError("Original backup needs attention: " + snapshot.backup_error)
    if restore:
        if snapshot.original is None:
            raise ValueError("No initial backup exists on this printer yet.")
        return snapshot.original
    if not snapshot.analysis["known"] and not full_risk:
        raise ValueError("This mod affects an unknown file. Review compatibility before applying.")
    if not snapshot.analysis["patchable"]:
        raise ValueError(snapshot.analysis["reason"])
    if snapshot.analysis["enabled"] and snapshot.original is None:
        raise ValueError("This printer has an Uncurser mod but no initial backup. Automatic Apply is blocked to avoid inventing an original.")
    return mods.transform(snapshot.data, desired)


def difference(snapshot, desired, restore=False, full_risk=False):
    new = prepare(snapshot, desired, restore, full_risk)
    return "".join(difflib.unified_diff(snapshot.data.decode().splitlines(True), new.decode().splitlines(True),
                                        fromfile="Current printer file", tofile="Pending file", n=3)) or "No changes."


def prepare_mesh(snapshot, desired, restore=False, full_risk=False, module=mesh):
    filename = module.TARGET.rsplit("/", 1)[-1]
    if snapshot is None:
        raise ValueError("Scan " + filename + " before changing this mod.")
    if snapshot.backup_error:
        raise ValueError(filename + " original needs attention: " + snapshot.backup_error)
    if not snapshot.analysis["known"] and not full_risk:
        raise ValueError("The mesh mod affects an unknown file. Review compatibility first.")
    if not snapshot.analysis["patchable"]:
        raise ValueError(snapshot.analysis["reason"])
    if restore:
        if snapshot.original is None:
            raise ValueError("There is no initial " + filename + " backup on the printer.")
        return module.restore(snapshot.data, snapshot.original)
    if snapshot.analysis["enabled"] is True and snapshot.original is None:
        raise ValueError("An already-applied mod requires its on-printer original backup.")
    return module.transform(snapshot.data, desired, snapshot.original)


def file_entries(snapshot):
    return ((mods, snapshot), (mesh, snapshot.mesh), (mesh_temperature, snapshot.mesh_temperature))


def file_snapshot(snapshot, path):
    return next(value for module, value in file_entries(snapshot) if module.TARGET == path)


def restorable(snapshot, full_risk=False):
    if network.compatibility_error(snapshot.status):
        return set()
    return {module.TARGET for module, value in file_entries(snapshot)
            if value and value.original is not None and not value.backup_error
            and (value.analysis["known"] or full_risk)}


def prepare_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None):
    network.require_supported(snapshot.status)
    results = {}
    originals = restorable(snapshot, full_risk) if restore else set()
    if desired is not None or mods.TARGET in originals:
        results[mods.TARGET] = prepare(snapshot, desired, restore, full_risk)
    if mesh_desired is not None or mesh.TARGET in originals:
        results[mesh.TARGET] = prepare_mesh(snapshot.mesh, mesh_desired, restore, full_risk)
    if temperature_desired is not None or mesh_temperature.TARGET in originals:
        results[mesh_temperature.TARGET] = prepare_mesh(snapshot.mesh_temperature, temperature_desired, restore,
                                                       full_risk, mesh_temperature)
    if restore and not originals:
        raise ValueError("No eligible on-printer original backups are available.")
    return results


def difference_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None):
    prepared = prepare_all(snapshot, desired, mesh_desired, restore, full_risk, temperature_desired)
    return "".join("".join(difflib.unified_diff(
        file_snapshot(snapshot, path).data.decode().splitlines(True),
        data.decode().splitlines(True), fromfile="Current " + path, tofile="Pending " + path, n=3))
        for path, data in prepared.items()) or "No changes."


def initial_difference(snapshot, path):
    """Read-only preview of first enablement, independent of staged/current state."""
    if snapshot is None:
        raise ValueError("Scan the printer to load the files for this comparison.")
    if path == mods.TARGET:
        module, value = mods, snapshot
    elif path == mesh.TARGET:
        module, value = mesh, snapshot.mesh
    elif path == mesh_temperature.TARGET:
        module, value = mesh_temperature, snapshot.mesh_temperature
    else:
        raise ValueError("Unknown mod file.")
    if value is None:
        raise ValueError("This file has not been scanned yet.")
    if value.backup_error:
        raise ValueError("The on-printer original backup needs attention: " + value.backup_error)
    if value.original is not None:
        original = value.original
        baseline = "Baseline: on-printer original backup."
        before_name = "Original " + path
    else:
        if value.analysis["enabled"] is not False:
            raise ValueError("The on-printer original backup is missing. The initial changes cannot be reconstructed from the current file.")
        if not value.data:
            raise ValueError("The printer file is unavailable.")
        original = value.data
        baseline = "Baseline: scanned file; no original backup exists yet."
        before_name = "Scanned baseline " + path
    enabled = module.transform(original, True)
    diff = "".join(difflib.unified_diff(original.decode().splitlines(True), enabled.decode().splitlines(True),
                                        fromfile=before_name, tofile="Enabled " + path, n=3))
    return diff or "The baseline already contains this mod's enabled values; no file changes are needed.", baseline


def apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk, progress, temperature_desired=None):
    progress("Checking fresh file contents and printer activity…")
    fresh = scan(connection, known_hosts, progress)
    if fresh.status is None:
        raise RuntimeError("Cannot verify printer activity: " + fresh.status_error)
    network.require_supported(fresh.status)
    network.require_idle(fresh.status)
    before = prepare_all(scanned, desired, mesh_desired, restore, full_risk, temperature_desired)
    for path in before:
        old = file_snapshot(scanned, path)
        new = file_snapshot(fresh, path)
        unchanged = (new.sha256 == old.sha256) if path == mods.TARGET else (
            mesh.stable_digest(new.data) == mesh.stable_digest(old.data)
            and (new.stable_guards or new.guards) == (old.stable_guards or old.guards)
            and new.patterns == old.patterns)
        if not unchanged or new.original_hash != old.original_hash:
            raise RuntimeError("Printer files changed after the last scan. Scan again; staged choices are retained.")
    pending = prepare_all(fresh, desired, mesh_desired, restore, full_risk, temperature_desired)
    if set(pending) != set(before):
        raise RuntimeError("Available original backups changed after scan. Scan again before restoring.")
    pending = {path: data for path, data in pending.items()
               if data != file_snapshot(fresh, path).data}
    if not pending:
        return {"snapshot": fresh, "message": "Printer files already match the requested state.", "restarted": False}
    if mods.TARGET in pending and not restore and not fresh.identity.get("history"):
        raise RuntimeError("Native print history is unavailable; early heating cannot identify new jobs.")
    with Remote(connection, known_hosts) as remote:
        if mods.TARGET in pending and not restore:
            capabilities = remote.python('''import json
with open("/usr/share/klipper/klippy/gcode.py") as f: text=f.read()
print(json.dumps(all(token in text for token in ("def check_cancel_running(","def invoke_action(","cancel_pending"))))
''')
            if not capabilities:
                raise RuntimeError("The required native cancellation support is missing.")
        items = []
        for path, data in pending.items():
            module, value = next((module, value) for module, value in file_entries(fresh) if module.TARGET == path)
            item = {"path": path, "backup": module.ORIGINAL, "expected": value.sha256,
                    "data": base64.b64encode(data).decode(), "new_hash": mods.digest(data),
                    "original_hash": value.original_hash, "may_capture_original": value.analysis["enabled"] is False,
                    "reference": module.REFERENCE_HASH, "restore": restore,
                    "kind": "python" if path == mods.TARGET else "config"}
            if path == mesh.TARGET:
                item["settings"] = [[section, key, raw] for (section, key), raw in mesh.Document(data).raw.items()]
            elif path == mesh_temperature.TARGET:
                item["settings"] = mesh_temperature.settings(data)
            items.append(item)
        guards, patterns = {}, {}
        for path in pending:
            if path != mods.TARGET:
                value = file_snapshot(fresh, path)
                guards.update(value.guards)
                patterns.update(value.patterns)
        # Files in this batch have their own before/after checks; including them
        # as unchanged dependencies would reject our own preceding replacements.
        guards = {path: digest for path, digest in guards.items() if path not in pending}
        progress("Securing original backups and preparing all replacements…")
        live_status = network.status(connection.host)
        network.require_supported(live_status)
        network.require_idle(live_status)
        remote.python(APPLY_BATCH, {"files": items,
                                   "guards": guards, "patterns": patterns,
                                   "firmware": fresh.status["native"].get("modelVersion"),
                                   "model": fresh.status["native"].get("model")})
    progress("Verifying saved printer files…")
    result = scan(connection, known_hosts, progress)
    for path, data in pending.items():
        value = file_snapshot(result, path)
        if value.sha256 != mods.digest(data):
            raise RuntimeError("A saved file changed after Apply. Rescan before proceeding.")
    return {"snapshot": result, "message": "Saved and verified. Power cycle the printer before printing.", "restarted": False}


def apply(connection, known_hosts, scanned, desired, restore=False, full_risk=False,
          progress=lambda message: None, mesh_desired=None, temperature_desired=None):
    network.require_supported(scanned.status)
    if mesh_desired is not None or temperature_desired is not None or (restore and (scanned.mesh or scanned.mesh_temperature)):
        return apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk, progress, temperature_desired)
    progress("Checking fresh file contents and printer activity…")
    fresh = scan(connection, known_hosts, progress)
    if fresh.sha256 != scanned.sha256 or fresh.original_hash != scanned.original_hash:
        raise RuntimeError("Printer files changed after the last scan. Scan again to review them; your pending choices are retained.")
    if fresh.status is None:
        raise RuntimeError("Cannot verify printer activity: " + fresh.status_error)
    network.require_supported(fresh.status)
    network.require_idle(fresh.status)
    if not fresh.identity.get("history"):
        raise RuntimeError("Native print history is unavailable; the early-heating mod cannot identify new jobs.")
    new = prepare(fresh, desired, restore, full_risk)
    if new == fresh.data:
        return {"snapshot": fresh, "message": "The printer files already match the requested state.", "restarted": False}
    progress("Verifying cancellation support and the prepared Python source…")
    with Remote(connection, known_hosts) as remote:
        capabilities = remote.python('''import ast,json
with open("/usr/share/klipper/klippy/gcode.py") as f: text=f.read()
print(json.dumps(all(token in text for token in ("def check_cancel_running(","def invoke_action(","cancel_pending"))))
''')
        if not capabilities and not restore:
            raise RuntimeError("The required native cancellation support is missing. This firmware cannot use this mod.")
        progress("Securing the initial backup and preparing the replacement…")
        live_status = network.status(connection.host)
        network.require_supported(live_status)
        network.require_idle(live_status)
        remote.python(APPLY, {"path": mods.TARGET, "expected": fresh.sha256,
                              "data": base64.b64encode(new).decode(), "new_hash": mods.digest(new),
                              "original_hash": fresh.original_hash, "may_capture_original": fresh.analysis["enabled"] is False,
                              "reference": mods.REFERENCE_HASH, "restore": restore,
                              "firmware": fresh.status["native"].get("modelVersion"),
                              "model": fresh.status["native"].get("model")})
    progress("Verifying saved printer files…")
    result = scan(connection, known_hosts, progress)
    if result.sha256 != mods.digest(new):
        raise RuntimeError("The saved file changed after Apply. Rescan before making further changes.")
    return {"snapshot": result, "message": "Saved and verified. Power cycle the printer before printing.", "restarted": False}
