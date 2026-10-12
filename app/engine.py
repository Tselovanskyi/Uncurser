"""Scan, prepare and apply. Scan never writes to the printer."""

import base64
import ast
import difflib
import json
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field, replace

from . import fluidd_screws, mesh, mesh_temperature, mod_versions, mods, network, screws, smoothing
from .remote import APPLY_BATCH, CONFIG_TREE, Remote


@dataclass
class MeshSnapshot:
    data: bytes | None
    sha256: str | None
    analysis: dict
    original: bytes | None = None
    original_hash: str | None = None
    backup_error: str = ""
    guards: dict = field(default_factory=dict)
    patterns: dict = field(default_factory=dict)
    stable_guards: dict = field(default_factory=dict)
    original_absent: bool = False
    backup_guards: dict = field(default_factory=dict)


@dataclass
class ScrewsSnapshot:
    files: dict
    analysis: dict
    backup_error: str = ""
    frontend: dict = field(default_factory=dict)
    helper_enabled: bool | None = False


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
    screws: ScrewsSnapshot | None = None
    versions: MeshSnapshot | None = None
    backup_guards: dict = field(default_factory=dict)
    smoothing: MeshSnapshot | None = None
    homing: MeshSnapshot | None = None


def version_snapshot(snapshot):
    return snapshot.versions or MeshSnapshot(None, None,
        {"known": True, "enabled": False, "patchable": True, "label": "Not installed"})


def installed_records(snapshot):
    value = version_snapshot(snapshot)
    if value.backup_error:
        raise ValueError("Mod version records need attention: " + value.backup_error)
    return mod_versions.load(value.data)


def scan_versions(remote):
    current, saved, manifest = (remote.read(path) for path in
        (mod_versions.TARGET, mod_versions.ORIGINAL, mod_versions.MANIFEST))
    error, payload_error, absent = "", "", False
    try:
        if saved is not None or manifest is not None:
            if saved is None or manifest is None:
                raise ValueError("An original record backup or manifest is missing.")
            meta = json.loads(manifest["data"])
            absent = meta.get("absent", False)
            if (meta.get("path") != mod_versions.TARGET or meta.get("sha256") != saved["sha256"]
                    or absent is not True or saved["data"] != b""
                    or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                raise ValueError("The original version-record absence proof is damaged.")
        elif current is not None:
            raise ValueError("The version records have no original absence proof.")
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        error = str(exc)
    try:
        mod_versions.load(current["data"] if current else None)
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        payload_error = str(exc)
    return MeshSnapshot(current["data"] if current else None, current["sha256"] if current else None,
        {"known": not (error or payload_error), "enabled": current is not None, "patchable": not (error or payload_error),
         "label": "Conflict" if error or payload_error else "Version records" if current else "Not installed",
         "reason": error or payload_error},
        original_hash=saved["sha256"] if saved else None, backup_error=error, original_absent=absent,
        backup_guards={mod_versions.ORIGINAL: saved["sha256"] if saved else None,
                       mod_versions.MANIFEST: manifest["sha256"] if manifest else None})


def version_details(snapshot, path):
    if path not in mod_versions.VERSIONS:
        return "Unknown version"
    try:
        record = installed_records(snapshot).get(path)
        if record:
            record_baseline(snapshot, path, record)
            return "v" + record["version"]
        value = mod_snapshot(snapshot, path)
        return "v" + mod_versions.LEGACY_VERSION if path in mod_versions.LEGACY_TARGETS and value.analysis.get("enabled") is True and value.analysis.get("known") else "Unknown version"
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return "Unknown version"


def update_statuses(snapshot):
    """Keep an installed mod's update visible when safe enrollment is blocked."""
    statuses = {path: {"needed": False, "available": False, "reason": ""}
                for path in mod_versions.VERSIONS}
    if snapshot is None:
        return statuses
    records, metadata_error = {}, ""
    try:
        records = installed_records(snapshot)
    except (ValueError, TypeError, AttributeError, UnicodeError) as error:
        metadata_error = "Mod version records need attention: " + str(error)
    for path, latest in mod_versions.VERSIONS.items():
        status = statuses[path]
        try:
            value = mod_snapshot(snapshot, path)
            record = records.get(path)
            if record:
                revision = mod_versions.version(record["version"])
                current = mod_versions.version(latest)
                status["needed"] = revision < current
                if revision > current:
                    raise ValueError("This mod was installed by a newer app; use that app or Restore original.")
            elif value and value.analysis.get("enabled") is True and value.analysis.get("known"):
                # Even unchanged legacy code needs a stored version and undo record.
                status["needed"] = True
            else:
                if value and value.analysis.get("enabled") is not False:
                    status["reason"] = value.analysis.get("reason") or "The installed mod code is not recognised."
                continue
            if metadata_error:
                raise ValueError(metadata_error)
            if value is None:
                raise ValueError("Scan this mod's files before updating it.")
            if value.backup_error:
                raise ValueError("Original backups need attention: " + value.backup_error)
            if value.analysis.get("include_error"):
                raise ValueError(value.analysis["include_error"])
            if path != screws.PYTHON and value.original is None:
                raise ValueError("The mod's initial original backup is missing.")
            if record:
                # Exact recorded ownership can remain valid after unrelated
                # manual edits. Applying still enforces the user's risk policy.
                record_baseline(snapshot, path, record)
                if path == screws.PYTHON and not value.analysis.get("patchable"):
                    raise ValueError(value.analysis.get("reason") or "The helper cannot be updated safely.")
            elif path == screws.PYTHON:
                if not value.analysis.get("patchable"):
                    raise ValueError(value.analysis.get("reason") or "The installed helper cannot be enrolled safely.")
                capture_helper(helper_baseline(snapshot), helper_current(snapshot))
            elif path == smoothing.TARGET:
                capture_homing(homing_baseline(snapshot), homing_current(snapshot))
            else:
                if not value.analysis.get("patchable"):
                    raise ValueError(value.analysis.get("reason") or "The installed mod code cannot be enrolled safely.")
                baseline = mod_versions.disabled(path, value.data, value.original)
                mod_versions.capture(path, baseline, value.data)
            status["available"] = status["needed"]
        except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
            status["reason"] = str(error) or "The installed mod code cannot be updated safely."
    return statuses


def automatic_updates(snapshot, choices):
    """Require enrollment of retained installed mods when enabling a new mod."""
    if snapshot is None:
        return set()
    mod_versions.require_registered(choices)
    states = {path: mod_snapshot(snapshot, path) for path in mod_versions.VERSIONS}
    if not any(choice is True and states.get(path) is not None
               and states[path].analysis.get("enabled") is not True for path, choice in choices.items()):
        return set()
    records = installed_records(snapshot)
    required = set()
    for path in mod_versions.VERSIONS:
        value = states[path]
        if value is None or choices.get(path) is False:
            continue
        record = records.get(path)
        applied = value.analysis.get("enabled")
        if applied is False and record is None:
            continue
        # An unavailable Fluidd dialog does not imply an installed helper.
        # Leave unrelated mods usable when its include and added files are absent.
        if (path == screws.PYTHON and record is None and value.helper_enabled is False
                and all(name in value.files and value.files[name].data is None
                        for name in mod_versions.ADDED_FILES[path])
                and not any(file.analysis.get("enabled") is True for file in value.frontend.values())):
            continue
        filename = path.rsplit("/", 1)[-1]
        try:
            if applied is not True or not value.analysis.get("known") or not value.analysis.get("patchable"):
                raise ValueError(value.analysis.get("reason") or "The installed mod code is not recognised.")
            if value.backup_error or (path != screws.PYTHON and value.original is None):
                raise ValueError(value.backup_error or "Its initial original backup is missing.")
            if record:
                record_baseline(snapshot, path, record)
                revision, latest = mod_versions.version(record["version"]), mod_versions.version(mod_versions.VERSIONS[path])
                if revision > latest:
                    raise ValueError("This mod was installed by a newer app.")
                if revision < latest:
                    required.add(path)
            else:
                if path == screws.PYTHON:
                    capture_helper(helper_baseline(snapshot), helper_current(snapshot))
                elif path == smoothing.TARGET:
                    capture_homing(homing_baseline(snapshot), homing_current(snapshot))
                else:
                    baseline = mod_versions.disabled(path, value.data, value.original)
                    mod_versions.capture(path, baseline, value.data)
                required.add(path)
        except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
            raise ValueError("Cannot enable another mod until " + filename + " is verified: " + str(error)) from error
    return required


def scan_screws(remote, root, tree):
    files = {}
    for module in screws.FILES:
        current = remote.read(module.TARGET)
        saved = remote.read(module.ORIGINAL)
        manifest = remote.read(module.MANIFEST)
        error, absent = "", False
        if saved is not None or manifest is not None:
            try:
                if saved is None or manifest is None:
                    raise ValueError("An original backup or its manifest is missing.")
                meta = json.loads(manifest["data"])
                if not isinstance(meta, dict):
                    raise ValueError("Original backup manifest must be an object.")
                absent = meta.get("absent", False)
                if (meta.get("path") != module.TARGET or meta.get("sha256") != saved["sha256"]
                        or not isinstance(absent, bool) or (absent and saved["data"] != b"")
                        or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                    raise ValueError("Original backup or its manifest is damaged.")
                if not absent:
                    raise ValueError("This backup describes a pre-existing helper file; automatic replacement is blocked.")
            except (ValueError, SyntaxError, UnicodeError) as exc:
                error = str(exc)
        data = current["data"] if current else None
        recognized = data is None or screws.recognized(module.TARGET, data)
        analysis = {"known": recognized, "enabled": data is not None, "patchable": recognized,
                    "label": "Not installed" if data is None else "Uncursed" if recognized else "Unknown",
                    "reason": "" if recognized else "This helper file differs from the bundled version."}
        files[module.TARGET] = MeshSnapshot(data, current["sha256"] if current else None, analysis,
                                           saved["data"] if saved and not absent else None,
                                           saved["sha256"] if saved else None, error, original_absent=absent,
                                           backup_guards={module.ORIGINAL: saved["sha256"] if saved else None,
                                                          module.MANIFEST: manifest["sha256"] if manifest else None})
    if isinstance(tree, Exception) or not root or not root.data:
        analysis = {"known": False, "enabled": None, "patchable": False,
                    "label": "Unavailable", "reason": "The active configuration could not be read."}
    else:
        included = {path: base64.b64decode(value["data"]) for path, value in tree["files"].items()}
        analysis = screws.inspect(root, files, included, tree["patterns"])
    errors = [value.backup_error for value in files.values() if value.backup_error]
    if root and root.backup_error:
        errors.append(root.backup_error)
    if analysis["enabled"] and (root.original is None or any(value.original_hash is None for value in files.values())):
        errors.append("The installed helper is missing its initial on-printer backups.")
    if not analysis["patchable"]:
        for value in files.values():
            value.analysis = dict(analysis)
    helper_enabled = analysis["enabled"]
    frontend = scan_fluidd_screws(remote)
    errors.extend(value.backup_error for value in frontend.values() if value.backup_error)
    combined = [analysis, *(value.analysis for value in frontend.values())]
    enabled = (True if all(part["enabled"] is True for part in combined)
               else False if all(part["enabled"] is False for part in combined) else None)
    known = all(part["known"] for part in combined)
    patchable = all(part["patchable"] for part in combined)
    analysis = {"known": known, "enabled": enabled, "patchable": patchable,
                "label": "Uncursed" if enabled else "Original" if enabled is False else "Incomplete" if known else "Conflict",
                "reason": "\n".join(part["reason"] for part in combined if part.get("reason"))}
    return ScrewsSnapshot(files, analysis, "\n".join(errors), frontend, helper_enabled)


def scan_fluidd_screws(remote):
    files, guards, guard_error = {}, {}, ""
    try:
        index = remote.read(fluidd_screws.INDEX)
        if index is None or index["data"].count(('src="./' + fluidd_screws.ASSET + '"').encode()) != 1:
            raise ValueError("The installed Fluidd interface is not the supported build.")
        guards[fluidd_screws.INDEX] = index["sha256"]
        for module in fluidd_screws.FILES:
            for extension in (".gz", ".br"):
                path = module.TARGET + extension
                alternate = remote.read(path)
                if alternate is not None:
                    raise ValueError("A precompressed Fluidd file needs separate support: " + path)
                guards[path] = None
    except (ValueError, RuntimeError, OSError) as error:
        guard_error = str(error)
    for module in fluidd_screws.FILES:
        try:
            current = remote.read(module.TARGET)
            if current is None:
                raise ValueError("Fluidd file not found: " + module.TARGET)
            saved, manifest = remote.read(module.ORIGINAL), remote.read(module.MANIFEST)
            error = ""
            if saved is not None or manifest is not None:
                try:
                    if saved is None or manifest is None:
                        raise ValueError("A Fluidd original backup or manifest is missing.")
                    meta = json.loads(manifest["data"])
                    if (not isinstance(meta, dict) or meta.get("path") != module.TARGET
                            or meta.get("sha256") != saved["sha256"] or meta.get("absent", False)
                            or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                        raise ValueError("The Fluidd original backup or manifest is damaged.")
                except (ValueError, UnicodeError) as exc:
                    error = str(exc)
            analysis = fluidd_screws.inspect(module, current["data"])
            if analysis["enabled"] and saved is None:
                error = "The Fluidd grid is missing its on-printer original backup."
            if guard_error:
                analysis = {**analysis, "known": False, "patchable": False, "label": "Conflict", "reason": guard_error,
                            "dependency_error": guard_error}
            files[module.TARGET] = MeshSnapshot(current["data"], current["sha256"], analysis,
                                                saved["data"] if saved else None, saved["sha256"] if saved else None,
                                                error, guards=dict(guards),
                                                backup_guards={module.ORIGINAL: saved["sha256"] if saved else None,
                                                               module.MANIFEST: manifest["sha256"] if manifest else None})
        except (ValueError, RuntimeError, OSError) as error:
            files[module.TARGET] = MeshSnapshot(None, None, {"known": False, "enabled": None,
                "patchable": False, "label": "Unavailable", "reason": str(error)})
    return files


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
                regular, generated = mesh.split(original["data"])
                mesh.parser(regular)
                mesh.generated_parser(generated)
            except (ValueError, UnicodeError) as error:
                backup_error = str(error)
        saved = original["data"] if original else None
        analysis = module.inspect(data, saved if not backup_error else None)
        try:
            module.validate_includes(included)
        except ValueError as error:
            analysis = {**analysis, "known": False, "patchable": False, "label": "Conflict", "reason": str(error),
                        "include_error": str(error)}
        return MeshSnapshot(data, current["sha256"], analysis, saved, original["sha256"] if original else None,
                            backup_error, {path: value["sha256"] for path, value in files.items()}, tree["patterns"],
                            {path: mods.digest(mesh.split(value)[0].encode()) for path, value in included.items()},
                            backup_guards={module.ORIGINAL: original["sha256"] if original else None,
                                           module.MANIFEST: manifest["sha256"] if manifest else None})
    except (ValueError, RuntimeError, OSError) as error:
        return MeshSnapshot(b"", "", {"known": False, "enabled": None, "patchable": False,
                                      "label": "Unavailable", "reason": str(error)})


def scan_homing(remote):
    module = smoothing.HOMING
    try:
        current, original, manifest = (remote.read(path) for path in
            (module.TARGET, module.ORIGINAL, module.MANIFEST))
        if current is None:
            raise ValueError("The existing homing module was not found: " + module.TARGET)
        backup_error = ""
        if original is not None or manifest is not None:
            try:
                if original is None or manifest is None:
                    raise ValueError("An original homing module backup or manifest is missing.")
                meta = json.loads(manifest["data"])
                if (meta.get("path") != module.TARGET or meta.get("sha256") != original["sha256"]
                        or meta.get("absent", False) is not False
                        or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                    raise ValueError("The original homing module backup is damaged.")
                ast.parse(original["data"].decode("utf-8"))
            except (ValueError, SyntaxError, AttributeError, UnicodeError) as error:
                backup_error = str(error)
        saved = original["data"] if original else None
        return MeshSnapshot(current["data"], current["sha256"],
            module.inspect(current["data"], saved if not backup_error else None),
            saved, original["sha256"] if original else None, backup_error,
            backup_guards={module.ORIGINAL: original["sha256"] if original else None,
                           module.MANIFEST: manifest["sha256"] if manifest else None})
    except (ValueError, RuntimeError, OSError) as error:
        return MeshSnapshot(b"", "", {"known": False, "enabled": None, "patchable": False,
            "label": "Unavailable", "reason": str(error)})


def scan_paths():
    paths = [mods.TARGET, mods.ORIGINAL, mods.MANIFEST]
    for module in (mesh, mesh_temperature, smoothing):
        paths.extend((module.ORIGINAL, module.MANIFEST))
    paths.extend((smoothing.HOMING.TARGET, smoothing.HOMING.ORIGINAL, smoothing.HOMING.MANIFEST))
    for module in screws.FILES:
        paths.extend((module.TARGET, module.ORIGINAL, module.MANIFEST))
    paths.append(fluidd_screws.INDEX)
    for module in fluidd_screws.FILES:
        paths.extend((module.TARGET + ".gz", module.TARGET + ".br",
                      module.TARGET, module.ORIGINAL, module.MANIFEST))
    paths.extend((mod_versions.TARGET, mod_versions.ORIGINAL, mod_versions.MANIFEST))
    return tuple(dict.fromkeys(paths))


def scan(connection, known_hosts, progress=lambda message: None):
    progress("Reading current printer files…")
    with Remote(connection, known_hosts) as remote:
        status_result = Future()
        def read_status():
            try:
                status_result.set_result((network.status(connection.host), ""))
            except Exception as error:
                status_result.set_result((None, str(error)))
        # A pending status read must not hold the app open when a scan is closed.
        threading.Thread(target=read_status, daemon=True, name="printer-status").start()
        remote.prefetch(scan_paths())
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
        smoothing_snapshot = scan_config(remote, smoothing, tree)
        homing_snapshot = scan_homing(remote)
        screws_snapshot = scan_screws(remote, mesh_snapshot, tree)
        versions_snapshot = scan_versions(remote)
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
    state, status_error = status_result.result()
    result = Snapshot(current["data"], current["sha256"], analysis,
                    original["data"] if original else None, original["sha256"] if original else None,
                    backup_error, state, status_error, identity, time.time(), mesh_snapshot, temperature_snapshot, screws_snapshot,
                    versions_snapshot, {mods.ORIGINAL: original["sha256"] if original else None,
                                        mods.MANIFEST: manifest["sha256"] if manifest else None},
                    smoothing=smoothing_snapshot, homing=homing_snapshot)
    if versions_snapshot.analysis["known"]:
        for path, record in installed_records(result).items():
            if path in (screws.PYTHON, smoothing.TARGET):
                continue
            value = file_snapshot(result, path)
            if value.analysis.get("include_error"):
                continue
            try:
                baseline = mod_versions.undo(value.data, record)
                module = mod_versions.MODULES[path]
                base_analysis = module.inspect(baseline) if module is mods else module.inspect(baseline, value.original)
                # Current templates are used only to check whether the restored
                # baseline can receive the latest mod, not to undo the old code.
                mod_versions.enabled(path, baseline)
                newer = mod_versions.version(record["version"]) > mod_versions.version(mod_versions.VERSIONS[path])
                value.analysis = {**base_analysis, "enabled": True, "patchable": not newer,
                    "label": "Uncursed" if base_analysis["known"] else "Unknown + Uncursed",
                    "reason": "This mod was installed by a newer app." if newer else base_analysis.get("reason", "")}
            except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
                value.analysis = {"known": False, "enabled": None, "patchable": False,
                                  "label": "Conflict", "reason": str(error)}
        homing_record = installed_records(result).get(smoothing.TARGET)
        if homing_record:
            try:
                baseline = homing_baseline(result, homing_record)
                newer = mod_versions.version(homing_record["version"]) > mod_versions.version(mod_versions.VERSIONS[smoothing.TARGET])
                for module in (smoothing, smoothing.HOMING):
                    value = file_snapshot(result, module.TARGET)
                    base_analysis = module.inspect(baseline[module.TARGET], value.original)
                    module.transform(baseline[module.TARGET], True)
                    value.analysis = {**base_analysis, "enabled": True, "patchable": not newer,
                        "label": "Uncursed" if base_analysis["known"] else "Unknown + Uncursed",
                        "reason": "This mod was installed by a newer app." if newer else base_analysis.get("reason", "")}
            except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
                for value in (result.smoothing, result.homing):
                    value.analysis = {"known": False, "enabled": None, "patchable": False,
                                      "label": "Conflict", "reason": str(error)}
        helper_record = installed_records(result).get(screws.PYTHON)
        if helper_record and result.screws:
            try:
                baseline = helper_baseline(result, helper_record)
                analysis = inspect_helper_baseline(result, baseline, tree)
                newer = mod_versions.version(helper_record["version"]) > mod_versions.version(mod_versions.VERSIONS[screws.PYTHON])
                analysis = {**analysis, "enabled": True, "patchable": not newer,
                            "label": "Uncursed" if analysis["known"] else "Unknown + Uncursed",
                            "reason": "This mod was installed by a newer app." if newer else analysis.get("reason", "")}
                result.screws.analysis = analysis
                result.screws.helper_enabled = True
                for value in result.screws.files.values():
                    value.analysis = dict(analysis)
                for value in result.screws.frontend.values():
                    value.analysis = dict(analysis)
            except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
                result.screws.analysis = {"known": False, "enabled": None, "patchable": False,
                                         "label": "Conflict", "reason": str(error)}
    return result


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


def prepare_mesh(snapshot, desired, restore=False, full_risk=False, module=mesh):
    filename = module.TARGET.rsplit("/", 1)[-1]
    if snapshot is None:
        raise ValueError("Scan " + filename + " before changing this mod.")
    if snapshot.backup_error:
        raise ValueError(filename + " original needs attention: " + snapshot.backup_error)
    if restore:
        if snapshot.original is None:
            raise ValueError("There is no initial " + filename + " backup on the printer.")
        regular, _ = mesh.split(snapshot.original)
        _, generated = mesh.split(snapshot.data)
        mesh.parser(regular)
        mesh.generated_parser(generated)
        return (regular + generated).encode("utf-8")
    if not snapshot.analysis["known"] and not full_risk:
        raise ValueError(filename + " is an unknown file. Review compatibility first.")
    if not snapshot.analysis["patchable"]:
        raise ValueError(snapshot.analysis["reason"])
    if snapshot.analysis["enabled"] is True and snapshot.original is None:
        raise ValueError("An already-applied mod requires its on-printer original backup.")
    return module.transform(snapshot.data, desired, snapshot.original)


def file_entries(snapshot):
    return ((mods, snapshot), (mesh, snapshot.mesh), (mesh_temperature, snapshot.mesh_temperature),
            (smoothing, snapshot.smoothing), (smoothing.HOMING, snapshot.homing)) + tuple(
        (module, snapshot.screws.files[module.TARGET]) for module in screws.FILES if snapshot.screws) + tuple(
        (module, snapshot.screws.frontend[module.TARGET]) for module in fluidd_screws.FILES
        if snapshot.screws and module.TARGET in snapshot.screws.frontend) + ((mod_versions, version_snapshot(snapshot)),)


def file_snapshot(snapshot, path):
    return next(value for module, value in file_entries(snapshot) if module.TARGET == path)


def mod_snapshot(snapshot, path):
    if path == screws.PYTHON:
        return snapshot.screws
    if path != smoothing.TARGET:
        return file_snapshot(snapshot, path)
    if snapshot.smoothing is None or snapshot.homing is None:
        return None
    values = (snapshot.smoothing, snapshot.homing)
    states = {value.analysis.get("enabled") for value in values}
    enabled = states.pop() if len(states) == 1 else None
    known = all(value.analysis.get("known") for value in values) and enabled is not None
    patchable = all(value.analysis.get("patchable") for value in values) and enabled is not None
    reason = " ".join(value.analysis.get("reason", "") for value in values if value.analysis.get("reason"))
    if enabled is None and not reason:
        reason = "The homing mod is only partially installed; its two files disagree."
    return replace(snapshot.smoothing, analysis={"known": known, "enabled": enabled,
        "patchable": patchable, "label": "Uncursed" if known and enabled else "Original" if known else "Conflict" if not patchable else "Unknown",
        "reason": reason, "include_error": snapshot.smoothing.analysis.get("include_error", "")},
        backup_error=" ".join(value.backup_error for value in values if value.backup_error),
        original=snapshot.smoothing.original if all(value.original is not None for value in values) else None)


def homing_current(snapshot):
    if snapshot.smoothing is None or snapshot.homing is None:
        raise ValueError("Scan both homing mod files before changing it.")
    return {path: file_snapshot(snapshot, path).data for path in mod_versions.FILES[smoothing.TARGET]}


def homing_baseline(snapshot, record=None):
    current = homing_current(snapshot)
    for module in (smoothing, smoothing.HOMING):
        value = file_snapshot(snapshot, module.TARGET)
        if value.analysis.get("include_error"):
            raise ValueError(value.analysis["include_error"])
        if value.backup_error:
            raise ValueError("The homing mod's original backups need attention: " + value.backup_error)
        if record and value.original is None:
            raise ValueError("The homing mod's initial original backups are missing.")
    if record:
        return mod_versions.undo_files(smoothing.TARGET, current, record)
    return {module.TARGET: module.transform(current[module.TARGET], False,
        file_snapshot(snapshot, module.TARGET).original or current[module.TARGET])
        for module in (smoothing, smoothing.HOMING)}


def capture_homing(baseline, enabled):
    pairs = {path: [(change["before"], change["after"]) for change in
             mod_versions.capture_changes(path, baseline[path], enabled[path])]
             for path in mod_versions.FILES[smoothing.TARGET]}
    return mod_versions.capture_files(smoothing.TARGET, baseline, enabled, pairs)


def helper_current(snapshot):
    if snapshot.screws is None or snapshot.mesh is None:
        raise ValueError("Scan every bed screw helper file before changing it.")
    return {path: file_snapshot(snapshot, path).data for path in mod_versions.FILES[screws.PYTHON]}


def helper_baseline(snapshot, record=None):
    helper = snapshot.screws
    if helper is None or snapshot.mesh is None:
        raise ValueError("Scan the printer before changing the bed screw helper.")
    if snapshot.mesh.backup_error or snapshot.mesh.original is None:
        raise ValueError("The helper's initial printer.cfg backup is missing or damaged.")
    for value in helper.files.values():
        if value.backup_error or not value.original_absent or value.original_hash is None:
            raise ValueError("The helper's original file-absence records are missing or damaged.")
    for value in helper.frontend.values():
        if value.backup_error or value.original is None:
            raise ValueError("The helper's initial Fluidd backup is missing or damaged.")
        if value.analysis.get("dependency_error"):
            raise ValueError(value.analysis["dependency_error"])
    if record:
        result = mod_versions.undo_files(screws.PYTHON, helper_current(snapshot), record)
    else:
        result = prepare_screws(snapshot, False)
        result[mesh.TARGET] = screws.edit_include(snapshot.mesh.data, False)
    regular, generated = mesh.split(result[mesh.TARGET])
    mesh.parser(regular)
    mesh.generated_parser(generated)
    if screws.strip_screws_include(regular)[1]:
        raise ValueError("The helper uninstall record leaves its include enabled.")
    return result


def record_baseline(snapshot, path, record):
    if path == screws.PYTHON:
        return helper_baseline(snapshot, record)
    if path == smoothing.TARGET:
        return homing_baseline(snapshot, record)
    return mod_versions.undo(file_snapshot(snapshot, path).data, record)


def capture_helper(baseline, enabled):
    regular, _ = mesh.split(enabled[mesh.TARGET])
    newline = "\r\n" if "\r\n" in regular else "\n"
    pairs = {mesh.TARGET: [["", screws.SCREWS_INCLUDE.replace("\n", newline)]]}
    pairs[fluidd_screws.TARGET] = fluidd_screws.replacements(
        fluidd_screws.TARGET, baseline[fluidd_screws.TARGET], enabled[fluidd_screws.TARGET])
    return mod_versions.capture_files(screws.PYTHON, baseline, enabled, pairs)


def inspect_helper_baseline(snapshot, baseline, tree):
    if isinstance(tree, Exception) or snapshot.mesh.analysis.get("include_error"):
        raise ValueError("The helper's active configuration includes need attention.")
    included = {path: base64.b64decode(value["data"]) for path, value in tree["files"].items()}
    if included.get(screws.CONFIG) != snapshot.screws.files[screws.CONFIG].data:
        raise ValueError("The installed helper config is not loaded consistently.")
    included.pop(screws.CONFIG, None)
    included[mesh.TARGET] = baseline[mesh.TARGET]
    patterns = {path: values for path, values in tree["patterns"].items() if path != screws.CONFIG}
    files = {path: replace(value, data=baseline[path]) for path, value in snapshot.screws.files.items()}
    root = replace(snapshot.mesh, data=baseline[mesh.TARGET])
    analysis = screws.inspect(root, files, included, patterns)
    if not analysis["patchable"]:
        raise ValueError(analysis["reason"])
    for module in fluidd_screws.FILES:
        part = fluidd_screws.inspect(module, baseline[module.TARGET])
        if not part["patchable"]:
            raise ValueError(part["reason"])
        analysis["known"] = analysis["known"] and part["known"]
    return analysis


def helper_js_replacements(snapshot, data):
    record = installed_records(snapshot).get(screws.PYTHON)
    current = snapshot.screws.frontend[fluidd_screws.TARGET].data
    if record is None:
        return fluidd_screws.replacements(fluidd_screws.TARGET, current, data)
    entry = record["files"][fluidd_screws.TARGET]
    baseline = mod_versions.undo(current, entry)
    pairs = [[change["after"], change["before"]] for change in reversed(entry["undo"])]
    if data != baseline:
        pairs.extend(fluidd_screws.replacements(fluidd_screws.TARGET, baseline, data))
    text = current.decode("utf-8")
    for before, after in pairs:
        if text.count(before) != 1:
            raise ValueError("The Fluidd uninstall edit is not unique.")
        text = text.replace(before, after, 1)
    if text.encode("utf-8") != data:
        raise ValueError("Unrelated Fluidd code would change.")
    return pairs


def restorable(snapshot, full_risk=False):
    if network.compatibility_error(snapshot.status):
        return set()
    paths = {module.TARGET for module, value in file_entries(snapshot)
            if value and value.original is not None and not value.backup_error}
    homing_paths = set(mod_versions.FILES[smoothing.TARGET])
    if not homing_paths.issubset(paths) and any(value and value.analysis.get("enabled") is not False
            for value in (snapshot.smoothing, snapshot.homing)):
        # A partial restore must not leave half this mod installed or discard
        # the recorded ownership of a file that could not be restored.
        paths.difference_update(homing_paths)
    if snapshot.screws:
        helper = snapshot.screws
        owned = all(value.original_hash is not None and value.original_absent and not value.backup_error
                    for value in helper.files.values())
        ready = all((value.original is not None and not value.backup_error)
                    or (value.analysis["enabled"] is False and not value.backup_error)
                    for value in helper.frontend.values())
        if ready and owned and mesh.TARGET in paths:
            paths.update(helper.files)
        else:
            # Restoring a shared config alone could orphan this helper or its include.
            if helper.helper_enabled is not False or any(value.data is not None for value in helper.files.values()):
                paths.discard(mesh.TARGET)
            paths.difference_update(helper.frontend)
    return paths


def prepare_screws(snapshot, desired, full_risk=False, restore=False, initial=False):
    value = snapshot.screws
    if value is None:
        raise ValueError("Scan the printer before changing the bed screw helper.")
    if restore:
        if not all(file.original_hash is not None and file.original_absent and not file.backup_error
                   for file in value.files.values()):
            raise ValueError("The helper's original file-absence records are missing; removal is blocked.")
        result = {path: None for path in value.files}
        for path, file in value.frontend.items():
            if file.backup_error:
                raise ValueError(file.backup_error)
            if file.original is not None:
                result[path] = file.original
            elif file.analysis["enabled"] is not False:
                raise ValueError("The Fluidd original backup is missing; restoration is blocked.")
        return result
    if value.backup_error:
        raise ValueError(value.backup_error)
    if not value.analysis["known"] and not full_risk:
        raise ValueError("The bed screw helper affects unknown files. Review compatibility first.")
    if not value.analysis["patchable"]:
        raise ValueError(value.analysis["reason"])
    if not desired and value.helper_enabled:
        if any(not file.original_absent for file in value.files.values()):
            raise ValueError("The helper's original file-absence records are missing; removal is blocked.")
    result = {path: screws.payload(path) if desired else None for path in value.files}
    for path, file in value.frontend.items():
        baseline = file.original if initial and file.original is not None else file.data
        result[path] = fluidd_screws.transform(path, baseline, desired)
    return result


def prepare_versioned(snapshot, path, desired, full_risk=False):
    record = installed_records(snapshot).get(path)
    value = file_snapshot(snapshot, path)
    if record is None:
        return prepare(snapshot, desired, full_risk=full_risk) if path == mods.TARGET else prepare_mesh(
            value, desired, full_risk=full_risk, module=mod_versions.MODULES[path])
    if value.backup_error or value.original is None:
        raise ValueError("The mod's initial original backup is missing or damaged.")
    if value.analysis.get("include_error"):
        raise ValueError(value.analysis["include_error"])
    if not value.analysis.get("known") and not full_risk:
        raise ValueError("This mod affects unknown files. Review compatibility first.")
    if mod_versions.version(record["version"]) > mod_versions.version(mod_versions.VERSIONS[path]):
        raise ValueError("This mod was installed by a newer app; use that app or Restore original.")
    baseline = mod_versions.undo(value.data, record)
    return mod_versions.enabled(path, baseline) if desired else baseline


def prepare_homing(snapshot, desired, full_risk=False, record=None):
    value = mod_snapshot(snapshot, smoothing.TARGET)
    if value is None:
        raise ValueError("Scan both homing mod files before changing it.")
    if value.backup_error:
        raise ValueError("The homing mod's original backups need attention: " + value.backup_error)
    if value.analysis.get("include_error"):
        raise ValueError(value.analysis["include_error"])
    if not value.analysis["known"] and not full_risk:
        raise ValueError("The homing mod affects unknown files. Review compatibility first.")
    if record:
        if mod_versions.version(record["version"]) > mod_versions.version(mod_versions.VERSIONS[smoothing.TARGET]):
            raise ValueError("This mod was installed by a newer app; use that app or Restore original.")
        baseline = homing_baseline(snapshot, record)
        return {module.TARGET: module.transform(baseline[module.TARGET], True) if desired else baseline[module.TARGET]
                for module in (smoothing, smoothing.HOMING)}
    if not value.analysis["patchable"]:
        raise ValueError(value.analysis["reason"])
    if value.analysis["enabled"] and value.original is None:
        raise ValueError("An already-applied homing mod requires both on-printer original backups.")
    result = {}
    for module in (smoothing, smoothing.HOMING):
        file = file_snapshot(snapshot, module.TARGET)
        result[module.TARGET] = module.transform(file.data, desired, file.original)
    return result


def prepare_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None):
    network.require_supported(snapshot.status)
    results = {}
    originals = restorable(snapshot, full_risk) if restore else set()
    choices = {mods.TARGET: desired, mesh.TARGET: mesh_desired, mesh_temperature.TARGET: temperature_desired,
               screws.PYTHON: screws_desired, smoothing.TARGET: smoothing_desired}
    mod_versions.require_registered(choices)
    enrollment = set()
    if not restore:
        required = automatic_updates(snapshot, choices)
        if update:
            updates = update_statuses(snapshot)
            for path, status in updates.items():
                if status["needed"] and not status["available"] and choices[path] is not False:
                    raise ValueError("Cannot update " + path.rsplit("/", 1)[-1] + ": " + status["reason"])
            required.update(path for path, status in updates.items()
                            if status["needed"] and status["available"])
        for path in required:
            if choices[path] is None:
                choices[path] = True
                enrollment.add(path)
    touched = {path for path, choice in choices.items() if choice is not None}
    try:
        records = installed_records(snapshot) if touched or restore else {}
    except ValueError:
        ledger = version_snapshot(snapshot)
        # Full restoration uses initial backups, even if current metadata is
        # unreadable. Its absence proof must remain valid, and no active mod
        # may be skipped before its undo instructions are removed.
        if not restore or ledger.backup_error or not ledger.original_absent or any(
                path not in originals and value is not None and (not value.analysis.get("known")
                                          or value.analysis.get("enabled") is not False)
                for path in mod_versions.VERSIONS for value in (mod_snapshot(snapshot, path),)):
            raise
        records = {}
    baselines = {}
    for path in touched if not restore else ():
        if choices[path] is True and path not in (screws.PYTHON, smoothing.TARGET):
            value = file_snapshot(snapshot, path)
            baselines[path] = mod_versions.undo(value.data, records[path]) if path in records else mod_versions.disabled(
                path, value.data, value.original if value.original is not None else value.data)
    desired, mesh_desired, temperature_desired = (choices[path] for path in
        (mods.TARGET, mesh.TARGET, mesh_temperature.TARGET))
    screws_desired = choices[screws.PYTHON]
    smoothing_desired = choices[smoothing.TARGET]
    if desired is not None or mods.TARGET in originals:
        results[mods.TARGET] = prepare(snapshot, desired, True, full_risk) if restore else prepare_versioned(snapshot, mods.TARGET, desired, full_risk)
    if mesh_desired is not None or mesh.TARGET in originals:
        results[mesh.TARGET] = prepare_mesh(snapshot.mesh, mesh_desired, True, full_risk) if restore else prepare_versioned(snapshot, mesh.TARGET, mesh_desired, full_risk)
    if temperature_desired is not None or mesh_temperature.TARGET in originals:
        results[mesh_temperature.TARGET] = prepare_mesh(snapshot.mesh_temperature, temperature_desired, True,
            full_risk, mesh_temperature) if restore else prepare_versioned(snapshot, mesh_temperature.TARGET, temperature_desired, full_risk)
    if restore:
        if smoothing.TARGET in originals:
            results[smoothing.TARGET] = prepare_mesh(snapshot.smoothing, None, True, full_risk, smoothing)
        if smoothing.HOMING.TARGET in originals:
            results[smoothing.HOMING.TARGET] = snapshot.homing.original
    elif smoothing_desired is not None:
        record = records.get(smoothing.TARGET)
        results.update(prepare_homing(snapshot, smoothing_desired, full_risk, record))
        if smoothing_desired:
            baselines[smoothing.TARGET] = homing_baseline(snapshot, record)
    # Adopting an unchanged unstamped implementation changes metadata only,
    # including when an equivalent numeric value uses different formatting.
    for path in enrollment - records.keys():
        if path == smoothing.TARGET:
            results.update(homing_current(snapshot))
        elif path != screws.PYTHON and not file_snapshot(snapshot, path).analysis.get("legacy"):
            results[path] = file_snapshot(snapshot, path).data
    if screws_desired is not None or screws.PYTHON in originals:
        enabled = False if restore else screws_desired
        if restore:
            results.update(prepare_screws(snapshot, enabled, full_risk, True))
        else:
            record = records.get(screws.PYTHON)
            if record:
                if not snapshot.screws.analysis.get("known") and not full_risk:
                    raise ValueError("The bed screw helper affects unknown files. Review compatibility first.")
                if mod_versions.version(record["version"]) > mod_versions.version(mod_versions.VERSIONS[screws.PYTHON]):
                    raise ValueError("This mod was installed by a newer app; use that app or Restore original.")
                baseline = helper_baseline(snapshot, record)
            else:
                prepare_screws(snapshot, enabled, full_risk)
                baseline = helper_baseline(snapshot) if snapshot.screws.analysis.get("enabled") is True else helper_current(snapshot)
            current = results.get(mesh.TARGET, snapshot.mesh.data)
            def edit_helper(data):
                base = mod_versions.undo(data, record["files"][mesh.TARGET]) if record else screws.edit_include(data, False)
                return screws.edit_include(base, True) if enabled else base
            baseline[mesh.TARGET] = (mod_versions.undo(current, record["files"][mesh.TARGET]) if record
                                     else screws.edit_include(current, False))
            if enabled:
                if record and not snapshot.screws.analysis.get("patchable"):
                    raise ValueError(snapshot.screws.analysis.get("reason") or "The helper cannot be updated safely.")
                applied = {path: screws.payload(path) for path in snapshot.screws.files}
                applied[fluidd_screws.TARGET] = fluidd_screws.transform(
                    fluidd_screws.TARGET, baseline[fluidd_screws.TARGET], True)
                if screws.PYTHON in enrollment and not record:
                    applied = helper_current(snapshot)
            else:
                applied = dict(baseline)
            applied[mesh.TARGET] = edit_helper(current)
            results.update(applied)
            if enabled:
                baselines[screws.PYTHON] = baseline
            if mesh.TARGET in baselines:
                baselines[mesh.TARGET] = edit_helper(baselines[mesh.TARGET])
    if restore and not originals:
        raise ValueError("No eligible on-printer original backups are available.")
    if touched or restore:
        for path in (originals if restore else touched):
            if path not in mod_versions.VERSIONS:
                continue
            if restore or choices[path] is False:
                records.pop(path, None)
            else:
                if path == screws.PYTHON:
                    records[path] = capture_helper(baselines[path], {name: results[name] for name in mod_versions.FILES[path]})
                elif path == smoothing.TARGET:
                    records[path] = capture_homing(baselines[path], {name: results[name] for name in mod_versions.FILES[path]})
                else:
                    records[path] = mod_versions.capture(path, baselines[path], results[path])
        ledger = version_snapshot(snapshot)
        if restore and not records:
            if ledger.data is not None:
                if not ledger.original_absent:
                    raise ValueError("The version-record original absence proof is missing.")
                results[mod_versions.TARGET] = None
        else:
            data = mod_versions.dump(records)
            if data != ledger.data and (records or ledger.data is not None):
                results[mod_versions.TARGET] = data
    return results


def difference_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None):
    prepared = prepare_all(snapshot, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired)
    return "".join(file_difference(path, file_snapshot(snapshot, path).data, data,
        "Current " + path if file_snapshot(snapshot, path).data is not None else "/dev/null",
        "Pending " + path if data is not None else "/dev/null")
        for path, data in prepared.items()) or "No changes."


def file_difference(path, before, after, fromfile, tofile):
    if path == fluidd_screws.TARGET:
        return fluidd_screws.difference(path, before, after, fromfile, tofile)
    return "".join(difflib.unified_diff((before or b"").decode().splitlines(True),
        (after or b"").decode().splitlines(True), fromfile=fromfile, tofile=tofile, n=3))


def initial_difference(snapshot, path):
    """Read-only preview of first enablement, independent of staged/current state."""
    if snapshot is None:
        raise ValueError("Scan the printer to load the files for this comparison.")
    if path == screws.MOD_ID:
        if snapshot.screws is None or snapshot.mesh is None:
            raise ValueError("The helper files have not been scanned yet.")
        prepared = prepare_screws(snapshot, True, full_risk=True, initial=True)
        original = snapshot.mesh.original if snapshot.mesh.original is not None else snapshot.mesh.data
        if not original:
            raise ValueError("The original printer.cfg is unavailable.")
        before = {mesh.TARGET: original}
        before.update({path: value.original for path, value in snapshot.screws.files.items()})
        after = {mesh.TARGET: screws.edit_include(original, True), **prepared}
        for path, value in snapshot.screws.frontend.items():
            before[path] = value.original if value.original is not None else value.data
        diff = "".join(file_difference(path, before[path], data,
            "Original " + path if before[path] is not None else "/dev/null",
            "Enabled " + path) for path, data in after.items())
        return diff, "Baseline: on-printer original backups, or scanned files before the first Apply."
    if path == smoothing.TARGET:
        if mod_snapshot(snapshot, path) is None:
            raise ValueError("Both homing mod files have not been scanned yet.")
        diffs = []
        for module in (smoothing, smoothing.HOMING):
            value = file_snapshot(snapshot, module.TARGET)
            if value.backup_error:
                raise ValueError("The on-printer original backup needs attention: " + value.backup_error)
            if value.original is None and value.analysis.get("enabled") is not False:
                raise ValueError("The on-printer original backup is missing; the initial changes cannot be reconstructed.")
            original = value.original if value.original is not None else value.data
            if not original:
                raise ValueError("The original printer file is unavailable: " + module.TARGET)
            enabled = module.transform(original, True)
            diffs.append(file_difference(module.TARGET, original, enabled,
                ("Original " if value.original is not None else "Scanned baseline ") + module.TARGET,
                "Enabled " + module.TARGET))
        return "".join(diffs) or "The baseline already contains the enabled values.", "Baseline: on-printer original backups, or scanned files before the first Apply."
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


def apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk, progress, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None):
    progress("Checking fresh file contents and printer activity…")
    fresh = scan(connection, known_hosts, progress)
    if fresh.status is None:
        raise RuntimeError("Cannot verify printer activity: " + fresh.status_error)
    network.require_supported(fresh.status)
    network.require_idle(fresh.status)
    before = prepare_all(scanned, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired)
    for path in before:
        old = file_snapshot(scanned, path)
        new = file_snapshot(fresh, path)
        unchanged = (new.sha256 == old.sha256 and new.guards == old.guards) if path in (fluidd_screws.TARGET, mod_versions.TARGET) else (
            (new.sha256 == old.sha256) if path in (mods.TARGET, screws.PYTHON, screws.CONFIG, smoothing.HOMING.TARGET) else (
            mesh.stable_digest(new.data) == mesh.stable_digest(old.data)
            and (new.stable_guards or new.guards) == (old.stable_guards or old.guards)
            and new.patterns == old.patterns))
        if (not unchanged or new.original_hash != old.original_hash or new.backup_guards != old.backup_guards
                or getattr(new, "original_absent", False) != getattr(old, "original_absent", False)):
            raise RuntimeError("Printer files changed after the last scan. Scan again; staged choices are retained.")
    pending = prepare_all(fresh, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired)
    if set(pending) != set(before) or (mod_versions.TARGET in pending and pending[mod_versions.TARGET] != before[mod_versions.TARGET]):
        raise RuntimeError("Available original backups changed after scan. Scan again before restoring.")
    # Include unchanged restored sources and an already-deleted helper so a
    # previous uninstall's orphan bytecode can still be removed.
    python_caches = {path: file_snapshot(fresh, path).sha256 for path, data in pending.items()
                     if path.endswith(".py")
                     and (restore or data is None or data != file_snapshot(fresh, path).data)}
    pending = {path: data for path, data in pending.items()
               if data != file_snapshot(fresh, path).data}
    if not pending and not python_caches:
        return {"snapshot": fresh, "message": "Printer files already match the requested state.", "restarted": False, "written": False}
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
                    "data": base64.b64encode(data).decode() if data is not None else None,
                    "new_hash": mods.digest(data) if data is not None else None,
                    "original_hash": value.original_hash, "may_capture_original": value.analysis["enabled"] is False,
                    "original_absent": getattr(value, "original_absent", False),
                    "reference": module.REFERENCE_HASH, "restore": restore,
                    "kind": "python" if path.endswith(".py") else "json" if path == mod_versions.TARGET else "config"}
            if path == mesh.TARGET:
                if fresh.screws and fresh.screws.analysis["enabled"] is not False:
                    item["may_capture_original"] = False
                if not restore:
                    item["settings"] = [[section, key, raw] for (section, key), raw in mesh.Document(data).raw.items()]
            elif path == mesh_temperature.TARGET and not restore:
                item["settings"] = mesh_temperature.settings(data)
            elif path == smoothing.TARGET and not restore:
                item["settings"] = smoothing.settings(data)
            elif path == fluidd_screws.TARGET:
                item["kind"] = "javascript"
                if not restore:
                    item["replacements"] = helper_js_replacements(fresh, data)
            items.append(item)
        # Install dependencies before adding the include; remove the include
        # before deleting dependencies. printer.cfg is written once for all mods.
        enabling = pending.get(screws.PYTHON) is not None
        helper_paths = (screws.PYTHON, screws.CONFIG)
        items.sort(key=lambda item: (item["path"] not in helper_paths) if enabling else (item["path"] in helper_paths))
        if set(mod_versions.FILES[smoothing.TARGET]) & pending.keys():
            homing_enabling = smoothing.inspect(pending.get(smoothing.TARGET, fresh.smoothing.data),
                fresh.smoothing.original).get("enabled") is True
            # Register the native command before its config starts calling it;
            # remove that config call before removing the Python command.
            items.sort(key=lambda item: item["path"] == (
                smoothing.TARGET if homing_enabling else smoothing.HOMING.TARGET))
        # Commit records after the working files they describe.
        items.sort(key=lambda item: item["path"] == mod_versions.TARGET)
        guards, patterns = {}, {}
        # Unchanged files in a metadata-only enrollment still require the same
        # race checks as files we replace, including their immutable originals.
        for path in before:
            module, value = next((module, value) for module, value in file_entries(fresh) if module.TARGET == path)
            if path not in pending:
                guards[path] = value.sha256
            guards.update({name: digest for name, digest in value.backup_guards.items() if digest is not None})
            if value.original_hash is not None:
                guards[module.ORIGINAL] = value.original_hash
        for path in before:
            if path != mods.TARGET:
                value = file_snapshot(fresh, path)
                guards.update(value.guards)
                patterns.update(value.patterns)
        if any(path in mod_versions.VERSIONS for path in before):
            ledger = version_snapshot(fresh)
            if mod_versions.TARGET not in pending:
                guards[mod_versions.TARGET] = ledger.sha256
            guards.update({name: digest for name, digest in ledger.backup_guards.items() if digest is not None})
        # Files in this batch have their own before/after checks; including them
        # as unchanged dependencies would reject our own preceding replacements.
        guards = {path: digest for path, digest in guards.items() if path not in pending}
        progress("Securing original backups and preparing all replacements…")
        live_status = network.status(connection.host)
        network.require_supported(live_status)
        network.require_idle(live_status)
        progress("Writing printer files…")
        outcome = remote.python(APPLY_BATCH, {"files": items, "python_caches": python_caches,
                                   "guards": guards, "patterns": patterns,
                                   "firmware": fresh.status["native"].get("modelVersion"),
                                   "model": fresh.status["native"].get("model")})
    progress("Verifying saved printer files…")
    result = scan(connection, known_hosts, progress)
    for path, data in pending.items():
        value = file_snapshot(result, path)
        if value.sha256 != (mods.digest(data) if data is not None else None):
            raise RuntimeError("A saved file changed after Apply. Rescan before proceeding.")
    cleared = bool(outcome and outcome.get("cleared"))
    if not pending and not cleared:
        return {"snapshot": result, "message": "Printer files already match the requested state.", "restarted": False, "written": False}
    power_cycle = any(path != mod_versions.TARGET for path in pending) or cleared
    return {"snapshot": result, "message": "Saved and verified. Power cycle the printer before printing." if power_cycle
            else "Version stamps and undo records saved.", "restarted": False, "written": True, "power_cycle": power_cycle}


def apply(connection, known_hosts, scanned, desired, restore=False, full_risk=False,
          progress=lambda message: None, mesh_desired=None, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None):
    network.require_supported(scanned.status)
    return apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk,
                      progress, temperature_desired, screws_desired, update, smoothing_desired)
