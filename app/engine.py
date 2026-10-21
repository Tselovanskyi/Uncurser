"""Scan, prepare and apply. Scan never writes to the printer."""

import base64
import ast
import copy
import difflib
import fnmatch
import json
import posixpath
import re
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from . import fluidd_categories, fluidd_screws, mesh, mesh_temperature, mod_comments, mod_versions, mods, network, recovery, screws, smoothing, stock, storage, warmup
from .remote import APPLY_BATCH, CONFIG_TREE, Remote


INCLUDE_STORAGE_ERROR = "Configuration includes could not be verified. Resolve the startup/include issue and scan again before moving backups."


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
    original_identity: tuple[str, str] | None = None
    included: dict = field(default_factory=dict)


@dataclass
class ScrewsSnapshot:
    files: dict
    analysis: dict
    backup_error: str = ""
    frontend: dict = field(default_factory=dict)
    helper_enabled: bool | None = False


@dataclass
class Snapshot:
    data: bytes | None
    sha256: str | None
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
    recovery: dict | None = None
    storage_moves: tuple = ()
    storage_error: str = ""
    original_identity: tuple[str, str] | None = None
    stock: dict | None = None
    storage_cleanup: tuple = ()
    unknown_files: dict = field(default_factory=dict)
    ignored_mods: tuple = ()
    warmup: MeshSnapshot | None = None
    fluidd_macros: dict | None = None
    fluidd_error: str = ""


def version_snapshot(snapshot):
    return snapshot.versions or MeshSnapshot(None, None,
        {"known": True, "enabled": False, "patchable": True, "label": "Not installed"})


def missing_record_proof(snapshot):
    value = version_snapshot(snapshot)
    return (value.data is not None and value.original_hash is None and not value.original_absent
            and not any(value.backup_guards.values())
            and value.backup_error in ("", "The version records have no original absence proof."))


def installed_records(snapshot, *, removal=False):
    value = version_snapshot(snapshot)
    # This is app-owned metadata, not a printer file requiring an OEM baseline.
    # Missing proofs are allowed; partial or damaged proofs remain blocking.
    if value.backup_error and not missing_record_proof(snapshot):
        raise ValueError("Mod version records need attention: " + value.backup_error)
    if value.data is not None and mods.digest(value.data) != value.sha256:
        raise ValueError("Mod version records do not match their scanned checksum.")
    return mod_versions.load(value.data)


def record_scopes(records, *, strict=False):
    scopes = dict(mod_versions.FILES)
    for owner, record in records.items():
        try:
            reader = mod_versions.file_scope if strict or owner in mod_versions.VERSIONS else mod_versions.discovery_scope
            scopes[owner] = reader(owner, record)
        except (ValueError, TypeError, AttributeError, UnicodeError):
            scopes[owner] = None
    return scopes


def original_identity(meta):
    identity = network.device_identity({"native": {"model": meta.get("model"),
                                                   "modelVersion": meta.get("firmware")}})
    return identity if all(identity) else None


def scan_versions(remote):
    current, saved, manifest = (remote.read(path) for path in
        (mod_versions.TARGET, mod_versions.ORIGINAL, mod_versions.MANIFEST))
    error, payload_error, absent, identity = "", "", False, None
    try:
        if saved is not None or manifest is not None:
            if saved is None or manifest is None:
                raise ValueError("An original record backup or manifest is missing.")
            meta = json.loads(manifest["data"])
            absent = meta.get("absent", False)
            if (meta.get("path") not in (mod_versions.TARGET, storage.LEGACY_RECORDS) or meta.get("sha256") != saved["sha256"]
                    or absent is not True or saved["data"] != b""
                    or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                raise ValueError("The original version-record absence proof is damaged.")
            identity = original_identity(meta)
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
                       mod_versions.MANIFEST: manifest["sha256"] if manifest else None},
        original_identity=identity)


def version_details(snapshot, path):
    if path not in mod_versions.VERSIONS:
        return "Unknown version"
    try:
        record = installed_records(snapshot).get(path)
        if record:
            record_baseline(snapshot, path, record)
            return "v" + record["version"]
        headers = [stamp for filename in mod_versions.FILES[path]
                   for value in (file_snapshot(snapshot, filename),) if value and value.data is not None
                   for owner, stamp in mod_comments.read(value.data).items() if owner == path]
        if headers and all(stamp == headers[0] for stamp in headers):
            return "v" + headers[0]["version"]
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
                if path == screws.PYTHON and screws.LEGACY_CONFIG in mod_versions.file_scope(path, record):
                    destination = value.files[screws.CONFIG]
                    if destination.data is not None or destination.backup_error:
                        raise ValueError("The new helper configuration location is occupied or its original record is damaged.")
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


def outdated_mods(snapshot, statuses, issues):
    """Classify ordinary updates using checks already cached for this scan."""
    if snapshot is None:
        return set()
    result = set()
    for path, status in statuses.items():
        if not status.get("needed") or not status.get("available") or path in issues:
            continue
        value = mod_snapshot(snapshot, path)
        if value and all(value.analysis.get(key) is True for key in ("known", "enabled", "patchable")):
            result.add(path)
    return result


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
    for module in screws.SCAN_FILES:
        current = remote.read(module.TARGET)
        saved = remote.read(module.ORIGINAL)
        manifest = remote.read(module.MANIFEST)
        error, absent, identity = "", False, None
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
                identity = original_identity(meta)
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
                                                          module.MANIFEST: manifest["sha256"] if manifest else None},
                                           original_identity=identity)
    if isinstance(tree, Exception) or not root or not root.data:
        analysis = {"known": False, "enabled": None, "patchable": False,
                    "label": "Unavailable", "reason": "The active configuration could not be read."}
    else:
        included = {path: base64.b64decode(value["data"]) for path, value in tree["files"].items()}
        analysis = screws.inspect(root, files, included, tree["patterns"])
    errors = [value.backup_error for value in files.values() if value.backup_error]
    if root and root.backup_error:
        errors.append(root.backup_error)
    if analysis["enabled"]:
        active_config = screws.screws_include_path(mesh.split(root.data)[0])
        if root.original is None or any(files[path].original_hash is None
                for path in (screws.PYTHON, active_config)):
            errors.append("The installed helper is missing its initial on-printer backups.")
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
            saved, manifest = remote.read(module.ORIGINAL), remote.read(module.MANIFEST)
            error, identity = "", None
            if saved is not None or manifest is not None:
                try:
                    if saved is None or manifest is None:
                        raise ValueError("A Fluidd original backup or manifest is missing.")
                    meta = json.loads(manifest["data"])
                    if (not isinstance(meta, dict) or meta.get("path") != module.TARGET
                            or meta.get("sha256") != saved["sha256"] or meta.get("absent", False) is not False
                            or any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid"))):
                        raise ValueError("The Fluidd original backup or manifest is damaged.")
                    identity = original_identity(meta)
                except (ValueError, UnicodeError) as exc:
                    error = str(exc)
            analysis = fluidd_screws.inspect(module, current["data"]) if current else {
                "known": True, "enabled": False, "patchable": False, "label": "Not installed",
                "reason": "The Fluidd file is missing: " + module.TARGET}
            if guard_error:
                analysis = {**analysis, "known": False, "patchable": False, "label": "Conflict", "reason": guard_error,
                            "dependency_error": guard_error}
            files[module.TARGET] = MeshSnapshot(current["data"] if current else None, current["sha256"] if current else None, analysis,
                                                saved["data"] if saved else None, saved["sha256"] if saved else None,
                                                error, guards=dict(guards),
                                                backup_guards={module.ORIGINAL: saved["sha256"] if saved else None,
                                                               module.MANIFEST: manifest["sha256"] if manifest else None},
                                                original_identity=identity)
        except (ValueError, RuntimeError, OSError) as error:
            files[module.TARGET] = MeshSnapshot(b"", "", {"known": False, "enabled": None,
                "patchable": False, "label": "Unavailable", "reason": str(error)})
    return files


def scan_config(remote, module, tree):
    try:
        current = remote.read(module.TARGET)
        original = remote.read(module.ORIGINAL)
        manifest = remote.read(module.MANIFEST)
        backup_error, identity = "", None
        if original is not None or manifest is not None:
            try:
                if original is None or manifest is None:
                    raise ValueError("An original backup or its manifest is missing.")
                meta = json.loads(manifest["data"])
                if (meta.get("path") != module.TARGET or meta.get("sha256") != original["sha256"]
                        or meta.get("absent", False) is not False):
                    raise ValueError("Original backup checksum does not match its manifest.")
                if any(not isinstance(meta.get(key), int) for key in ("mode", "uid", "gid")):
                    raise ValueError("Original backup attributes are incomplete.")
                regular, generated = mesh.split(original["data"])
                mesh.parser(regular)
                mesh.generated_parser(generated)
                identity = original_identity(meta)
            except (ValueError, TypeError, AttributeError, UnicodeError) as error:
                backup_error = str(error)
        saved = original["data"] if original else None
        data, digest = (current["data"], current["sha256"]) if current else (None, None)
        guards, patterns, stable_guards, included = {}, {}, {}, {}
        try:
            if isinstance(tree, Exception):
                raise ValueError(str(tree))
            files = dict(tree["files"])
            active = files.pop(module.TARGET, None)
            if active is None:
                raise ValueError("This file is not in the active configuration: " + module.TARGET)
            active_data = base64.b64decode(active["data"])
            if active_data != data or active["sha256"] != digest:
                raise ValueError("The configuration changed during scan. Scan again.")
            included = {path: base64.b64decode(value["data"]) for path, value in files.items()}
            guards = {path: value["sha256"] for path, value in files.items()}
            patterns = tree["patterns"]
            stable_guards = {path: mods.digest(mesh.split(value)[0].encode()) for path, value in included.items()}
            analysis = module.inspect(data, saved if not backup_error else None)
            try:
                module.validate_includes(included)
            except ValueError as error:
                analysis = {**analysis, "known": False, "patchable": False, "label": "Conflict", "reason": str(error),
                            "include_error": str(error)}
        except (ValueError, RuntimeError, OSError) as error:
            analysis = {"known": False, "enabled": None, "patchable": False,
                        "label": "Unavailable", "reason": str(error), "include_error": str(error)}
        return MeshSnapshot(data, digest, analysis, saved, original["sha256"] if original else None,
                            backup_error, guards, patterns, stable_guards,
                            backup_guards={module.ORIGINAL: original["sha256"] if original else None,
                                           module.MANIFEST: manifest["sha256"] if manifest else None},
                            original_identity=identity, included=included)
    except (ValueError, RuntimeError, OSError) as error:
        return MeshSnapshot(b"", "", {"known": False, "enabled": None, "patchable": False,
                                      "label": "Unavailable", "reason": str(error)})


def scan_homing(remote, module=smoothing.HOMING):
    try:
        current, original, manifest = (remote.read(path) for path in
            (module.TARGET, module.ORIGINAL, module.MANIFEST))
        backup_error, identity = "", None
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
                identity = original_identity(meta)
            except (ValueError, SyntaxError, AttributeError, UnicodeError) as error:
                backup_error = str(error)
        saved = original["data"] if original else None
        return MeshSnapshot(current["data"] if current else None, current["sha256"] if current else None,
            module.inspect(current["data"], saved if not backup_error else None) if current else
            {"known": True, "enabled": False, "patchable": False, "label": "Not installed",
             "reason": "The printer module is missing: " + module.TARGET},
            saved, original["sha256"] if original else None, backup_error,
            backup_guards={module.ORIGINAL: original["sha256"] if original else None,
                           module.MANIFEST: manifest["sha256"] if manifest else None},
            original_identity=identity)
    except (ValueError, RuntimeError, OSError) as error:
        return MeshSnapshot(b"", "", {"known": False, "enabled": None, "patchable": False,
            "label": "Unavailable", "reason": str(error)})


def scan_paths():
    paths = [mods.TARGET, mods.ORIGINAL, mods.MANIFEST, mesh.TARGET]
    for module in (mesh, mesh_temperature, smoothing):
        paths.extend((module.TARGET, module.ORIGINAL, module.MANIFEST))
    paths.extend((smoothing.HOMING.TARGET, smoothing.HOMING.ORIGINAL, smoothing.HOMING.MANIFEST))
    paths.extend((warmup.TARGET, warmup.ORIGINAL, warmup.MANIFEST))
    for module in screws.SCAN_FILES:
        paths.extend((module.TARGET, module.ORIGINAL, module.MANIFEST))
    paths.append(fluidd_screws.INDEX)
    for module in fluidd_screws.FILES:
        paths.extend((module.TARGET + ".gz", module.TARGET + ".br",
                      module.TARGET, module.ORIGINAL, module.MANIFEST))
    paths.extend((mod_versions.TARGET, mod_versions.ORIGINAL, mod_versions.MANIFEST))
    return storage.scan_paths(paths)


def recorded_file(path):
    original = storage.original(path)
    return SimpleNamespace(TARGET=path, ORIGINAL=original, MANIFEST=original + ".json",
                           REFERENCE_HASH="uncurser-recorded-file-schema-1")


def scan_recorded_file(remote, path):
    module = recorded_file(path)
    guards = {}
    try:
        current, saved, manifest = (remote.read(name) for name in (path, module.ORIGINAL, module.MANIFEST))
        if path.endswith(".js"):
            for suffix in (".gz", ".br"):
                alternate = remote.read(path + suffix)
                guards[path + suffix] = alternate["sha256"] if alternate else None
    except (ValueError, RuntimeError, OSError) as exc:
        return MeshSnapshot(None, None, {"known": True, "enabled": None, "patchable": False,
            "label": "Unavailable", "reason": str(exc)}, backup_error=str(exc))
    error, absent, identity = "", False, None
    try:
        if saved is not None or manifest is not None:
            if saved is None or manifest is None:
                raise ValueError("An original backup or its manifest is missing.")
            meta = json.loads(manifest["data"])
            absent = meta.get("absent", False)
            if (meta.get("path") != path or meta.get("sha256") != saved["sha256"]
                    or type(absent) is not bool or (absent and saved["data"] != b"")
                    or any(type(meta.get(key)) is not int for key in ("mode", "uid", "gid"))):
                raise ValueError("The original backup or absence proof is damaged.")
            identity = original_identity(meta)
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        error = str(exc)
    if any(guards.values()):
        error = error or "A compressed copy of this interface file is present; removal could leave its code active."
    data = current["data"] if current else None
    original = saved["data"] if saved and not absent else None
    restored = saved is not None and not error and (data is None if absent else data == original)
    return MeshSnapshot(data, current["sha256"] if current else None,
        {"known": True, "enabled": not restored, "patchable": False,
         "label": "Original" if restored else "Unknown mod", "reason": ""},
        original, saved["sha256"] if saved else None, error, guards=guards, original_absent=absent,
        backup_guards={module.ORIGINAL: saved["sha256"] if saved else None,
                       module.MANIFEST: manifest["sha256"] if manifest else None}, original_identity=identity)


def scan(connection, known_hosts, progress=lambda message: None, *, ignored_mods=(), unknown_paths=()):
    progress("Reading current printer files…")
    with Remote(connection, known_hosts) as remote:
        remote = storage.Reader(remote)
        status_result = Future()
        macro_result = Future()
        def read_status():
            try:
                status_result.set_result((network.status(connection.host), ""))
            except Exception as error:
                status_result.set_result((None, str(error)))
        # A pending status read must not hold the app open when a scan is closed.
        threading.Thread(target=read_status, daemon=True, name="printer-status").start()
        def read_macros():
            try:
                value = network.fluidd_macros(connection.host)
                fluidd_categories.validate(value)
                macro_result.set_result((value, ""))
            except Exception as error:
                macro_result.set_result((None, str(error) or type(error).__name__))
        threading.Thread(target=read_macros, daemon=True, name="fluidd-preferences").start()
        remote.prefetch(scan_paths())
        current = remote.read(mods.TARGET)
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
        warmup_snapshot = scan_homing(remote, warmup)
        screws_snapshot = scan_screws(remote, mesh_snapshot, tree)
        versions_snapshot = scan_versions(remote)
        records = mod_versions.load(versions_snapshot.data) if versions_snapshot.analysis["known"] else {}
        known_paths = {mods.TARGET, mesh.TARGET, mesh_temperature.TARGET, smoothing.TARGET,
                       smoothing.HOMING.TARGET, warmup.TARGET, *(module.TARGET for module in screws.SCAN_FILES),
                       *(module.TARGET for module in fluidd_screws.FILES)}
        scopes = record_scopes(records, strict=True)
        extra_paths = set(unknown_paths) | {filename for owner, scope in scopes.items()
            if owner not in mod_versions.VERSIONS and scope is not None for filename in scope}
        extra_paths.difference_update(known_paths)
        if extra_paths:
            extra_reads = [name for path in sorted(extra_paths)
                for name in (path, storage.original(path), storage.original(path) + ".json")]
            extra_reads.extend(path + suffix for path in sorted(extra_paths) if path.endswith(".js")
                               for suffix in (".gz", ".br"))
            remote.remote.prefetch(storage.scan_paths(scan_paths() + extra_reads))
        unknown_files = {path: scan_recorded_file(remote, path) for path in sorted(extra_paths)}
        recovery_files = (remote.read(mesh.TARGET), remote.read(screws.LEGACY_CONFIG), remote.read(screws.CONFIG))
        progress("Reading printer status…")
        state, status_error = status_result.result()
        macro_preferences, macro_error = macro_result.result()
        storage_cleanup = storage.obsolete_originals(remote,
            {filename for scope in mod_versions.FILES.values() for filename in scope} | {screws.LEGACY_CONFIG},
            network.device_identity(state))
        stock_snapshot = None
        # Stock data is a recovery source, never a replacement for originals.
        values = {mesh.TARGET: mesh_snapshot, mesh_temperature.TARGET: temperature_snapshot,
                  smoothing.TARGET: smoothing_snapshot, smoothing.HOMING.TARGET: homing_snapshot,
                  warmup.TARGET: warmup_snapshot,
                  **(screws_snapshot.files if screws_snapshot else {}),
                  **(screws_snapshot.frontend if screws_snapshot else {})}
        values.update(unknown_files)
        missing_proof = versions_snapshot.data is None or bool(versions_snapshot.backup_error) or any(
            (original is None if filename == mods.TARGET else
             values.get(filename) is None or values[filename].original is None)
            for path, record in records.items() for filename in (scopes.get(path) or ()))
        changed_firmware = any(record.get("firmware") != network.device_identity(state)[1]
                               for record in records.values())
        added_paths = {filename for owner, record in records.items()
                       if owner not in mod_versions.VERSIONS and scopes.get(owner) is not None
                       for filename, entry in record.get("files", {}).items() if "created" in entry}
        if (network.device_identity(state)[0] == "F008"
                and (missing_proof or changed_firmware or network.is_untested(state)
                     or added_paths or any(pair.get("reason", "obsolete") != "obsolete" for pair in storage_cleanup))):
            progress("Checking stock firmware recovery files…")
            stock_snapshot = stock.scan(remote, state, **({"extra_paths": tuple(sorted(added_paths))} if added_paths else {}))
        storage_moves = tuple(remote.moves)
    analysis = mods.inspect(current["data"]) if current else {
        "known": True, "enabled": False, "patchable": False, "label": "Not installed",
        "reason": "The native print module is missing: " + mods.TARGET}
    backup_error, original_source = "", None
    if original is not None or manifest is not None:
        try:
            if original is None or manifest is None:
                raise ValueError("An original backup or its manifest is missing.")
            meta = json.loads(manifest["data"])
            if (meta.get("path") != mods.TARGET or meta.get("sha256") != original["sha256"]
                    or meta.get("absent", False) is not False):
                raise ValueError("The original backup checksum does not match its manifest.")
            for key in ("mode", "uid", "gid"):
                if not isinstance(meta.get(key), int):
                    raise ValueError("The original backup has incomplete file attributes.")
            mods.validate(original["data"])
            original_source = original_identity(meta)
        except (ValueError, TypeError, AttributeError, KeyError, SyntaxError, UnicodeError) as error:
            backup_error = str(error)
    result = Snapshot(current["data"] if current else None, current["sha256"] if current else None, analysis,
                    original["data"] if original else None, original["sha256"] if original else None,
                    backup_error, state, status_error, identity, time.time(), mesh_snapshot, temperature_snapshot, screws_snapshot,
                    versions_snapshot, {mods.ORIGINAL: original["sha256"] if original else None,
                                        mods.MANIFEST: manifest["sha256"] if manifest else None},
                    smoothing=smoothing_snapshot, homing=homing_snapshot,
                    recovery=recovery.inspect(state, *recovery_files), storage_moves=storage_moves,
                    original_identity=original_source, stock=stock_snapshot, storage_cleanup=storage_cleanup,
                    unknown_files=unknown_files, ignored_mods=tuple(ignored_mods), warmup=warmup_snapshot,
                    fluidd_macros=macro_preferences, fluidd_error=macro_error)
    result.storage_error = "\n".join(dict.fromkeys(
        value.backup_error for module, value in file_entries(result) if value and value.backup_error
        and module.TARGET not in result.unknown_files
        and not (module is mod_versions and missing_record_proof(result))))
    if not versions_snapshot.analysis["known"] and not missing_record_proof(result):
        result.storage_error = result.storage_error or versions_snapshot.analysis["reason"]
    if versions_snapshot.analysis["known"]:
        for path, record in installed_records(result).items():
            if path not in mod_versions.VERSIONS or path in (screws.PYTHON, smoothing.TARGET):
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
                    if value.analysis.get("include_error"):
                        continue
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
                for path in helper_scope(result, helper_record):
                    if path in result.screws.files:
                        result.screws.files[path].analysis = dict(analysis)
                for value in result.screws.frontend.values():
                    value.analysis = dict(analysis)
            except (ValueError, SyntaxError, TypeError, AttributeError, UnicodeError) as error:
                result.screws.analysis = {"known": False, "enabled": None, "patchable": False,
                                         "label": "Conflict", "reason": str(error)}
    if result.storage_moves and result.mesh and result.mesh.analysis.get("include_error"):
        result.storage_error = result.storage_error or INCLUDE_STORAGE_ERROR
    return result


def prepare(snapshot, desired, restore=False, full_risk=False):
    network.require_supported(snapshot.status, allow_untested=not restore)
    if snapshot.backup_error:
        raise ValueError("Original backup needs attention: " + snapshot.backup_error)
    if restore:
        if snapshot.original is None:
            raise ValueError("No initial backup exists on this printer yet.")
        if snapshot.original_identity != network.device_identity(snapshot.status):
            raise ValueError("The original backup firmware could not be matched to this printer. Cross-firmware full restore is blocked.")
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
            (smoothing, snapshot.smoothing), (smoothing.HOMING, snapshot.homing), (warmup, snapshot.warmup)) + tuple(
        (module, snapshot.screws.files[module.TARGET]) for module in screws.SCAN_FILES
        if snapshot.screws and module.TARGET in snapshot.screws.files) + tuple(
        (module, snapshot.screws.frontend[module.TARGET]) for module in fluidd_screws.FILES
        if snapshot.screws and module.TARGET in snapshot.screws.frontend) + tuple(
        (recorded_file(path), value) for path, value in snapshot.unknown_files.items()) + ((mod_versions, version_snapshot(snapshot)),)


def file_snapshot(snapshot, path):
    return next(value for module, value in file_entries(snapshot) if module.TARGET == path)


def removal_statuses(snapshot):
    """Inspect recorded ownership without relying on the current mod templates."""
    from . import removal
    records, error = {}, ""
    try:
        records = installed_records(snapshot, removal=True)
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        error = str(exc)
    return removal.inspect({module.TARGET: value for module, value in file_entries(snapshot)
                            if value is not None}, records, metadata_error=error, stock=snapshot.stock,
                           identity=network.device_identity(snapshot.status),
                           header_fallback=version_snapshot(snapshot).data is None and version_snapshot(snapshot).sha256 is None)


def cleanup_snapshot(snapshot, pairs=None):
    """Ignore unusable originals only when planning their guarded removal."""
    damaged = {pair["target"] for pair in (snapshot.storage_cleanup if pairs is None else pairs)
               if pair.get("reason", "obsolete") != "obsolete"}
    if not damaged:
        return snapshot
    result = copy.deepcopy(snapshot)
    for path in damaged:
        value = file_snapshot(result, path)
        value.original = value.original_hash = value.original_identity = None
        value.original_absent, value.backup_error = False, ""
    result.storage_error = "\n".join(dict.fromkeys(value.backup_error for module, value in file_entries(result)
        if value and value.backup_error and module.TARGET not in result.unknown_files
        and not (value is result.versions and missing_record_proof(result))))
    if result.versions and not result.versions.analysis["known"] and not missing_record_proof(result):
        result.storage_error = result.storage_error or result.versions.analysis["reason"]
    if result.mesh and result.mesh.analysis.get("include_error"):
        result.storage_error = result.storage_error or INCLUDE_STORAGE_ERROR
    return result


def ignored_backup_targets(snapshot, scopes, choices=None):
    """Keep originals owned by unknown mods ignored for this connection."""
    return {pair["target"] for pair in snapshot.storage_cleanup
            for owner in snapshot.ignored_mods
            if owner not in mod_versions.VERSIONS and owner in scopes
            and (choices or {}).get(owner) is not False
            and (scopes[owner] is None or pair["target"] in scopes[owner])}


def deferred_storage_view(snapshot, targets):
    # Only remove deferred-backup diagnostics from the aggregate migration
    # blocker. Their per-file errors still prevent edits to those same files.
    if not targets:
        return snapshot
    errors = {module.TARGET: value.backup_error for module, value in file_entries(snapshot)
              if value and value.backup_error}
    protected = set("\n".join(error for path, error in errors.items()
                              if path not in targets or path == mod_versions.TARGET).splitlines())
    protected.update(version_snapshot(snapshot).analysis.get("reason", "").splitlines())
    deferred = set("\n".join(error for path, error in errors.items()
                             if path in targets and path != mod_versions.TARGET).splitlines()) - protected
    error = "\n".join(line for line in snapshot.storage_error.splitlines() if line not in deferred)
    return replace(snapshot, storage_error=error) if error != snapshot.storage_error else snapshot


def firmware_issues(snapshot, *, removals=None):
    """Find firmware mismatches and legacy storage on any supported release."""
    if snapshot is None or network.firmware_state(snapshot.status) == "unsupported":
        return {}
    current = network.device_identity(snapshot.status)[1]
    try:
        records = installed_records(snapshot, removal=True)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        records = {}
    result = {}
    scopes = record_scopes(records)
    deferred = ignored_backup_targets(snapshot, scopes)
    cleanup = tuple(pair for pair in snapshot.storage_cleanup if pair["target"] not in deferred)
    cleanup_files = {file["path"] for pair in snapshot.storage_cleanup for file in pair["files"]}
    moves = tuple(move for move in snapshot.storage_moves if move["source"] not in cleanup_files)
    view = cleanup_snapshot(snapshot)
    states = removal_statuses(view) if view is not snapshot or removals is None else removals
    damaged = {pair["target"] for pair in snapshot.storage_cleanup if pair.get("reason", "obsolete") != "obsolete"}
    obsolete = {pair["target"] for pair in cleanup if pair.get("reason", "obsolete") == "obsolete"}
    for path, status in states.items():
        installed = records.get(path, {}).get("firmware") or status.get("firmware")
        if path not in mod_versions.VERSIONS:
            if path not in snapshot.ignored_mods:
                result[path] = {**status, "unknown": True, "name": path.rsplit("/", 1)[-1],
                                "firmware_reason": "Unknown mod.", "installed_firmware": installed}
            continue
        invalid_backup = bool(damaged.intersection(mod_versions.FILES[path])) or (path == screws.PYTHON and screws.LEGACY_CONFIG in damaged)
        obsolete_backup = bool(obsolete.intersection(mod_versions.FILES[path])) or (path == screws.PYTHON and screws.LEGACY_CONFIG in obsolete)
        if status["present"] and (installed != current or invalid_backup or obsolete_backup or status.get("header")):
            result[path] = {**status, "installed_firmware": installed, "current_firmware": current,
                            "firmware_reason": ("Its original backup is damaged or incomplete." if invalid_backup else
                                                "Its uninstall record is missing." if status.get("header") and installed == current else
                                                "Installed firmware is unknown." if installed is None else
                                                "Installed on firmware " + installed + "; current firmware is " + current + "." if installed != current else
                                                "Its original backup belongs to older firmware.")}
    if snapshot.storage_moves or snapshot.storage_cleanup:
        error = deferred_storage_view(view, deferred).storage_error
        if error == INCLUDE_STORAGE_ERROR:
            unknown_repairs = tuple(path for path, issue in result.items()
                                    if issue.get("unknown") and issue.get("available"))
            if result.get(screws.PYTHON, {}).get("available") or unknown_startup_repair(view, unknown_repairs):
                error = ""
        for pair in snapshot.storage_cleanup:
            for owner, scope in scopes.items():
                owns = scope is None or pair["target"] in scope or (owner == screws.PYTHON and pair["target"] == screws.LEGACY_CONFIG)
                if owner not in mod_versions.VERSIONS and owner in snapshot.ignored_mods:
                    continue
                verified = result.get(owner, states.get(owner, {}) if pair["target"] in deferred else {})
                if owns and states.get(owner, {}).get("present") and not verified.get("available"):
                    error = error or "An active mod still requires the backup for " + pair["target"] + "."
        if cleanup or moves or error:
            status = {"kind": "cleanup" if cleanup else "relocation", "present": True,
                      "available": not error, "reason": error}
            if cleanup:
                status["relocation"] = bool(moves)
                status["damaged"] = any(pair.get("reason", "obsolete") != "obsolete" for pair in cleanup)
            result[storage.ROOT] = status
    if result:
        from . import reinstall
        eligible = reinstall.eligible(snapshot, result)
        for path in mod_versions.VERSIONS.keys() & result.keys():
            result[path]["reinstall"] = path in eligible
    return result


def prepare_removal(snapshot, choices, *, with_recovery=False):
    from . import removal
    records = installed_records(snapshot, removal=True)
    files = {module.TARGET: value for module, value in file_entries(snapshot) if value is not None}
    results, recovered = {}, set()
    for path, choice in choices.items():
        if choice is None:
            continue
        if choice is not False:
            raise ValueError("A removal plan accepts only mods staged for removal.")
        plan = removal.inspect(files, records, stock=snapshot.stock,
                               identity=network.device_identity(snapshot.status),
                               header_fallback=version_snapshot(snapshot).data is None and version_snapshot(snapshot).sha256 is None)[path]
        if not plan["available"]:
            raise ValueError("Cannot remove " + path.rsplit("/", 1)[-1] + ": " + plan["reason"])
        for filename, data in plan["files"].items():
            results[filename] = data
            if plan.get("recovery") and filename in plan.get("recovery_files", plan["files"]):
                recovered.add(filename)
            files[filename] = replace(files[filename], data=data,
                                      sha256=mods.digest(data) if data is not None else None)
        records.pop(path, None)
    mod_versions.preserve_shared_changes(
        {module.TARGET: value.data for module, value in file_entries(snapshot) if value is not None},
        results, records, {path for path, choice in choices.items() if choice is False})
    ledger = version_snapshot(snapshot)
    if any(choice is False for choice in choices.values()) and ledger.data is not None:
        data = None if not records and missing_record_proof(snapshot) else mod_versions.dump(records)
        if data != ledger.data:
            results[mod_versions.TARGET] = data
    return (results, recovered) if with_recovery else results


def require_apply_policy(status, choices, restore=False, update=False):
    network.require_removal_supported(status)
    if network.is_untested(status) and restore:
        raise ValueError("Firmware is not tested. Full Restore original is blocked; verified mod installation, updates, removal and backup migration remain available.")


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


def capture_homing(baseline, enabled, *, firmware=None):
    pairs = {path: [(change["before"], change["after"]) for change in
             mod_versions.capture_changes(path, mod_comments.strip(baseline[path]), mod_comments.strip(enabled[path]))]
             for path in mod_versions.FILES[smoothing.TARGET]}
    return mod_versions.capture_files(smoothing.TARGET, baseline, enabled, pairs, firmware=firmware)


def helper_scope(snapshot, record=None):
    if record:
        return mod_versions.file_scope(screws.PYTHON, record)
    active = screws.screws_include_path(mesh.split(snapshot.mesh.data)[0])
    return tuple(screws.LEGACY_CONFIG if path == screws.CONFIG and active == screws.LEGACY_CONFIG else path
                 for path in mod_versions.FILES[screws.PYTHON])


def helper_current(snapshot, record=None):
    if snapshot.screws is None or snapshot.mesh is None:
        raise ValueError("Scan every bed screw helper file before changing it.")
    return {path: file_snapshot(snapshot, path).data for path in helper_scope(snapshot, record)}


def helper_baseline(snapshot, record=None):
    helper = snapshot.screws
    if helper is None or snapshot.mesh is None:
        raise ValueError("Scan the printer before changing the bed screw helper.")
    if snapshot.mesh.backup_error or snapshot.mesh.original is None:
        raise ValueError("The helper's initial printer.cfg backup is missing or damaged.")
    scope = helper_scope(snapshot, record)
    for path in (path for path in scope if path in helper.files):
        value = helper.files[path]
        if value.backup_error or not value.original_absent or value.original_hash is None:
            raise ValueError("The helper's original file-absence records are missing or damaged.")
    for value in helper.frontend.values():
        if value.backup_error or value.original is None:
            raise ValueError("The helper's initial Fluidd backup is missing or damaged.")
        if value.analysis.get("dependency_error"):
            raise ValueError(value.analysis["dependency_error"])
    if record:
        result = mod_versions.undo_files(screws.PYTHON, helper_current(snapshot, record), record)
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


def capture_helper(baseline, enabled, *, firmware=None):
    regular, _ = mesh.split(enabled[mesh.TARGET])
    newline = "\r\n" if "\r\n" in regular else "\n"
    pairs = {mesh.TARGET: [["", screws.SCREWS_INCLUDE.replace("\n", newline)]]}
    pairs[fluidd_screws.TARGET] = fluidd_screws.replacements(
        fluidd_screws.TARGET, mod_comments.strip(baseline[fluidd_screws.TARGET]), mod_comments.strip(enabled[fluidd_screws.TARGET]))
    return mod_versions.capture_files(screws.PYTHON, baseline, enabled, pairs, firmware=firmware)


def category_change(snapshot, prepared):
    """Compose saved, data-only Fluidd preference undo with this file batch."""
    if mod_versions.TARGET not in prepared:
        return None
    try:
        before = installed_records(snapshot)
    except ValueError:
        # Full original-file restoration may recover an unreadable ledger;
        # unreadable preference instructions cannot authorize database edits.
        return None
    after = mod_versions.load(prepared[mod_versions.TARGET])
    changes = [(before.get(owner, {}).get("fluidd_category"), after.get(owner, {}).get("fluidd_category"))
               for owner in before.keys() | after.keys()
               if before.get(owner, {}).get("fluidd_category") != after.get(owner, {}).get("fluidd_category")]
    if not changes:
        return None
    if snapshot.fluidd_error:
        raise ValueError("Cannot read Fluidd macro preferences: " + snapshot.fluidd_error)
    value = snapshot.fluidd_macros
    for old, new in changes:
        if old is not None:
            value = fluidd_categories.disable(value, old)
        if new is not None:
            value, captured = fluidd_categories.enable(value)
            if captured != new:
                raise ValueError("Fluidd macro preferences changed after scan. Scan again before applying.")
    return {"expected": snapshot.fluidd_macros, "value": value} if value != snapshot.fluidd_macros else None


def category_difference(change):
    if not change:
        return ""
    return file_difference("Fluidd macro preferences", (json.dumps(change["expected"], indent=2) + "\n").encode(),
        (json.dumps(change["value"], indent=2) + "\n").encode(), "Current Fluidd macro preferences", "Pending Fluidd macro preferences")


def inspect_helper_baseline(snapshot, baseline, tree):
    if isinstance(tree, Exception) or snapshot.mesh.analysis.get("include_error"):
        raise ValueError("The helper's active configuration includes need attention.")
    included = {path: base64.b64decode(value["data"]) for path, value in tree["files"].items()}
    active = screws.screws_include_path(mesh.split(snapshot.mesh.data)[0])
    if included.get(active) != snapshot.screws.files[active].data:
        raise ValueError("The installed helper config is not loaded consistently.")
    included.pop(active, None)
    included[mesh.TARGET] = baseline[mesh.TARGET]
    patterns = {path: values for path, values in tree["patterns"].items() if path != active}
    files = {path: replace(value, data=baseline[path]) for path, value in snapshot.screws.files.items() if path in baseline}
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


def helper_js_replacements(snapshot, data, *, tolerant=False):
    record = installed_records(snapshot, removal=True).get(screws.PYTHON)
    current = snapshot.screws.frontend[fluidd_screws.TARGET].data
    pairs = []
    text = current.decode("utf-8")
    if record is None:
        pairs = fluidd_screws.replacements(fluidd_screws.TARGET, current, data)
    elif tolerant or network.is_untested(snapshot.status):
        entry = record["files"][fluidd_screws.TARGET]
        text, pairs = current.decode("utf-8"), []
        for change in reversed(entry["undo"]):
            if text.count(change["after"]) == 1:
                pairs.append([change["after"], change["before"]])
                text = text.replace(change["after"], change["before"], 1)
        if text.encode("utf-8") == data:
            return pairs
    if record is not None:
        entry = record["files"][fluidd_screws.TARGET]
        baseline = mod_versions.undo(current, entry)
        pairs = [[change["after"], change["before"]] for change in reversed(entry["undo"])
                 if not (mod_versions.optional_metadata_change(change, screws.PYTHON)
                         and change["after"] not in current.decode("utf-8"))]
        if data != baseline:
            pairs.extend(fluidd_screws.replacements(fluidd_screws.TARGET, baseline, data))
    text = current.decode("utf-8")
    for before, after in pairs:
        if text.count(before) != 1:
            raise ValueError("The Fluidd uninstall edit is not unique.")
        text = text.replace(before, after, 1)
    # Metadata is independently owned, just like the helper's UI function.
    if text.encode("utf-8") != data and mod_comments.strip(text.encode("utf-8")) == mod_comments.strip(data):
        old, new = mod_comments.lines(text.encode("utf-8")), mod_comments.lines(data)
        for owner, before in old.items():
            after = new.get(owner, b"")
            if before != after:
                pairs.append([before.decode(), after.decode()])
                text = text.replace(before.decode(), after.decode(), 1)
        for owner in new.keys() - old.keys():
            header = new[owner].decode()
            target = data.decode()
            position = target.index(header)
            if text != target[:position] + target[position + len(header):]:
                raise ValueError("The Fluidd comment insertion is ambiguous.")
            for size in (32, 64, 128, 256):
                anchor = text[position:position + size]
                if anchor and text.count(anchor) == 1:
                    pairs.append([anchor, header + anchor])
                    text = text[:position] + header + text[position:]
                    break
            else:
                raise ValueError("The Fluidd comment insertion is not unique.")
    if text.encode("utf-8") != data:
        raise ValueError("Unrelated Fluidd code would change.")
    return pairs


def restorable(snapshot, full_risk=False):
    if network.compatibility_error(snapshot.status):
        return set()
    if unknown_record_ids(snapshot):
        return set()
    identity = network.device_identity(snapshot.status)
    paths = {module.TARGET for module, value in file_entries(snapshot)
            if value and value.original is not None and not value.backup_error
            and value.original_identity == identity}
    homing_paths = set(mod_versions.FILES[smoothing.TARGET])
    if not homing_paths.issubset(paths) and any(value and value.analysis.get("enabled") is not False
            for value in (snapshot.smoothing, snapshot.homing)):
        # A partial restore must not leave half this mod installed or discard
        # the recorded ownership of a file that could not be restored.
        paths.difference_update(homing_paths)
    if snapshot.screws:
        helper = snapshot.screws
        candidates = {path: value for path, value in helper.files.items()
                      if value.data is not None or value.original_hash is not None or value.backup_error}
        owned = bool(candidates) and all(value.original_hash is not None and value.original_absent and not value.backup_error
                    and value.original_identity == identity
                    for value in candidates.values())
        ready = all((value.original is not None and not value.backup_error and value.original_identity == identity)
                    or (value.original is None and value.analysis["enabled"] is False and not value.backup_error)
                    for value in helper.frontend.values())
        if ready and owned and mesh.TARGET in paths:
            paths.update(candidates)
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
        identity = network.device_identity(snapshot.status)
        if snapshot.mesh is None or snapshot.mesh.original_identity != identity:
            raise ValueError("The helper's original configuration firmware does not match this printer.")
        candidates = {path: file for path, file in value.files.items()
                      if file.data is not None or file.original_hash is not None or file.backup_error}
        if not all(file.original_hash is not None and file.original_absent and not file.backup_error
                   and file.original_identity == identity
                   for file in candidates.values()):
            raise ValueError("The helper's original file-absence records are missing; removal is blocked.")
        result = {path: None for path in candidates}
        for path, file in value.frontend.items():
            if file.backup_error:
                raise ValueError(file.backup_error)
            if file.original is not None:
                if file.original_identity != identity:
                    raise ValueError("The Fluidd original backup firmware does not match this printer.")
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
        if any(not value.files[path].original_absent for path in helper_scope(snapshot) if path in value.files):
            raise ValueError("The helper's original file-absence records are missing; removal is blocked.")
    scope = mod_versions.FILES[screws.PYTHON] if initial else helper_scope(snapshot)
    result = {path: screws.payload(path) if desired else None for path in scope if path in value.files}
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


def unknown_record_ids(snapshot):
    data = version_snapshot(snapshot).data
    try:
        document = json.loads(data) if data is not None else {}
        records = document.get("mods", {})
        return set(records).difference(mod_versions.VERSIONS) if isinstance(records, dict) else set()
    except (ValueError, TypeError, AttributeError, UnicodeError):
        return set()


def prepare_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None, *, warmup_desired=None, repair_targets=None, unknown_removals=()):
    choices = {mods.TARGET: desired, mesh.TARGET: mesh_desired, mesh_temperature.TARGET: temperature_desired,
               screws.PYTHON: screws_desired, smoothing.TARGET: smoothing_desired, warmup.TARGET: warmup_desired}
    mod_versions.require_registered(choices)
    unknown_removals = set(unknown_removals)
    if unknown_removals:
        records = installed_records(snapshot, removal=True)
        if (not unknown_removals.issubset(records.keys() - mod_versions.VERSIONS.keys())
                or restore or update or full_risk or any(choice is True for choice in choices.values())):
            raise ValueError("Unknown mod removal must use its isolated Fix issues action.")
    if restore and unknown_record_ids(snapshot):
        raise ValueError("Remove unknown mods before using Restore original; their records must be preserved.")
    if repair_targets is not None and (not repair_targets or restore or update or full_risk
            or {path for path, choice in choices.items() if choice is True} != set(repair_targets)
            or any(choice is False for choice in choices.values())):
        raise ValueError("A repair reinstall may only enable its selected mods.")
    if restore and (update or any(choice is not None for choice in choices.values())):
        raise ValueError("Restore original cannot be combined with mod changes or updates.")
    require_apply_policy(snapshot.status, choices.values(), restore, update)
    results = {}
    statuses = removal_statuses(snapshot) if any(choice is False for choice in choices.values()) else {}
    removed = {path for path, choice in choices.items() if choice is False
               and (network.is_untested(snapshot.status) or missing_record_proof(snapshot)
                    or statuses[path].get("recovery")
                    or (path == screws.PYTHON and snapshot.recovery and snapshot.recovery.get("available"))
                    or (statuses[path].get("available") and not any(
                        file_snapshot(snapshot, filename).analysis.get("include_error")
                        for filename in statuses[path]["files"])))}
    removed.update(unknown_removals)
    if removed:
        results = prepare_removal(snapshot, {path: False for path in removed})
        # Compose further edits against the verified removal result. Never
        # overwrite a shared file with a second plan based on its old bytes.
        snapshot = copy.deepcopy(snapshot)
        for path, data in results.items():
            value = file_snapshot(snapshot, path)
            value.data, value.sha256 = data, mods.digest(data) if data is not None else None
    originals = restorable(snapshot, full_risk) if restore else set()
    enrollment = set()
    if not restore and repair_targets is None:
        required = automatic_updates(snapshot, choices) if any(choice is True for choice in choices.values()) else set()
        if update:
            updates = update_statuses(snapshot)
            required.update(outdated_mods(snapshot, updates, firmware_issues(snapshot)))
        for path in required:
            if choices[path] is None:
                choices[path] = True
                enrollment.add(path)
    for path in removed:
        if path in choices:
            choices[path] = None
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
    if repair_targets is not None:
        for path in repair_targets:
            value = mod_snapshot(snapshot, path)
            if path in records or value is None or value.analysis.get("enabled") is not False:
                raise ValueError("Verify this mod's cleanup before reinstalling it: " + path)
    baselines = {}
    for path in touched if not restore else ():
        if choices[path] is True and path not in (screws.PYTHON, smoothing.TARGET):
            value = file_snapshot(snapshot, path)
            if value is None:
                raise ValueError("Scan this mod's files before changing it: " + path)
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
    warmup_desired = choices[warmup.TARGET]
    if warmup_desired is not None or warmup.TARGET in originals:
        results[warmup.TARGET] = snapshot.warmup.original if restore else prepare_versioned(snapshot, warmup.TARGET, warmup_desired, full_risk)
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
            legacy = screws.LEGACY_CONFIG in baseline
            if enabled and legacy:
                destination = snapshot.screws.files[screws.CONFIG]
                if destination.data is not None or destination.backup_error:
                    raise ValueError("The new helper configuration location is occupied or its original record is damaged.")
                # The old file is removed only after its own recorded contents
                # and absence proof have been verified. Keep its originals.
                results[screws.LEGACY_CONFIG] = baseline.pop(screws.LEGACY_CONFIG)
                baseline[screws.CONFIG] = None
            current = results.get(mesh.TARGET, snapshot.mesh.data)
            def edit_helper(data):
                base = mod_versions.undo(data, record["files"][mesh.TARGET]) if record else screws.edit_include(data, False)
                return screws.edit_include(base, True) if enabled else base
            baseline[mesh.TARGET] = (mod_versions.undo(current, record["files"][mesh.TARGET]) if record
                                     else screws.edit_include(current, False))
            if enabled:
                if record and not snapshot.screws.analysis.get("patchable"):
                    raise ValueError(snapshot.screws.analysis.get("reason") or "The helper cannot be updated safely.")
                applied = {module.TARGET: screws.payload(module.TARGET) for module in screws.FILES}
                applied[fluidd_screws.TARGET] = fluidd_screws.transform(
                    fluidd_screws.TARGET, baseline[fluidd_screws.TARGET], True)
                if screws.PYTHON in enrollment and not record and not legacy:
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
    if not restore:
        firmware = network.device_identity(snapshot.status)[1]
        def stamp(data, filename, own=None):
            if data is None:
                return None
            for owner, choice in choices.items():
                if choice is None or filename not in mod_versions.FILES[owner]:
                    continue
                data = (mod_comments.put(data, owner, mod_versions.VERSIONS[owner], firmware, filename)
                        if choice is True and owner != own else mod_comments.remove(data, owner))
            return data
        results = {filename: stamp(data, filename) if filename != mod_versions.TARGET else data
                   for filename, data in results.items()}
        for owner, baseline in baselines.items():
            baselines[owner] = ({filename: stamp(data, filename, owner) for filename, data in baseline.items()}
                                if isinstance(baseline, dict) else stamp(baseline, owner, owner))
    if touched or restore:
        firmware = network.device_identity(snapshot.status)[1]
        for path in (originals if restore else touched):
            if path not in mod_versions.VERSIONS:
                continue
            if restore or choices[path] is False:
                records.pop(path, None)
            else:
                if path == screws.PYTHON:
                    category = records.get(path, {}).get("fluidd_category")
                    if category is None:
                        if snapshot.fluidd_error:
                            raise ValueError("Cannot read Fluidd macro preferences: " + snapshot.fluidd_error)
                        _, category = fluidd_categories.enable(snapshot.fluidd_macros)
                    records[path] = capture_helper(baselines[path], {name: results[name] for name in mod_versions.FILES[path]}, firmware=firmware)
                    records[path]["fluidd_category"] = category
                elif path == smoothing.TARGET:
                    records[path] = capture_homing(baselines[path], {name: results[name] for name in mod_versions.FILES[path]}, firmware=firmware)
                else:
                    records[path] = mod_versions.capture(path, baselines[path], results[path], firmware=firmware)
        ledger = version_snapshot(snapshot)
        if restore and not records:
            if ledger.data is not None:
                if not ledger.original_absent and not missing_record_proof(snapshot):
                    raise ValueError("The version-record original absence proof is missing.")
                results[mod_versions.TARGET] = None
        else:
            data = mod_versions.dump(records)
            if data != ledger.data and (records or ledger.data is not None):
                results[mod_versions.TARGET] = data
    retained = installed_records(snapshot) if unknown_record_ids(snapshot) else records
    if repair_targets is not None or any(path not in mod_versions.VERSIONS for path in retained):
        mod_versions.preserve_shared_changes(
            {module.TARGET: value.data for module, value in file_entries(snapshot) if value is not None},
            results, retained, set(touched) | removed)
    return results


def storage_plan(snapshot, choices, restore=False, *, cleanup_all=False):
    """Plan cleanup before migration without discarding a retained mod's baseline."""
    cleanup = getattr(snapshot, "storage_cleanup", ())
    retire, deferred = [], set()
    if cleanup:
        states = removal_statuses(cleanup_snapshot(snapshot))
        records = installed_records(snapshot) if unknown_record_ids(snapshot) else {}
        scopes = record_scopes(records)
        deferred = ignored_backup_targets(snapshot, scopes, choices)
        for pair in cleanup:
            needed = restore
            for owner, scope in scopes.items():
                owns = scope is None or pair["target"] in scope or (owner == screws.PYTHON and pair["target"] == screws.LEGACY_CONFIG)
                needed |= bool(owns and (choices.get(owner) is True or
                               (states.get(owner, {}).get("present") and choices.get(owner) is not False)))
            if needed and cleanup_all and pair["target"] not in deferred:
                raise ValueError("Remove the mod before deleting its backup: " + pair["target"])
            if not needed:
                retire.append(pair)
    # Deferred originals stay where they are until their owners are removed.
    excluded = {file["path"] for pair in cleanup for file in pair["files"]}
    moves = tuple(move for move in getattr(snapshot, "storage_moves", ()) if move["source"] not in excluded)
    view = cleanup_snapshot(snapshot, retire) if retire else snapshot
    return deferred_storage_view(view, deferred), tuple(retire), moves


def difference_all(snapshot, desired, mesh_desired=None, restore=False, full_risk=False, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None, *, warmup_desired=None):
    choices = {mods.TARGET: desired, mesh.TARGET: mesh_desired, mesh_temperature.TARGET: temperature_desired,
               screws.PYTHON: screws_desired, smoothing.TARGET: smoothing_desired, warmup.TARGET: warmup_desired}
    snapshot, retire, pending_moves = storage_plan(snapshot, choices, restore)
    prepared = prepare_all(snapshot, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired, warmup_desired=warmup_desired)
    moves = "".join("Move " + item["source"] + "\n  to " + item["destination"] + "\n" for item in pending_moves)
    deleted = "".join("Delete " + file["path"] + "\n" for pair in retire for file in pair["files"] if file["sha256"] is not None)
    statuses = removal_statuses(snapshot) if not restore and any(c is False for c in choices.values()) else {}
    recovery_notes = "".join(statuses[path]["details"] + "\n" for path, choice in choices.items()
                             if choice is False and statuses[path].get("recovery"))
    category = category_change(snapshot, prepared)
    return moves + deleted + recovery_notes + category_difference(category) + "".join(file_difference(path, file_snapshot(snapshot, path).data, data,
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
    owner = screws.PYTHON if path == screws.MOD_ID else path
    def stamp(filename, data):
        return mod_comments.put(data, owner, mod_versions.VERSIONS[owner],
                                network.device_identity(snapshot.status)[1], filename)
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
        after = {filename: stamp(filename, data) for filename, data in after.items()}
        diff = "".join(file_difference(path, before[path], data,
            "Original " + path if before[path] is not None else "/dev/null",
            "Enabled " + path) for path, data in after.items())
        if snapshot.fluidd_error:
            raise ValueError("Cannot read Fluidd macro preferences: " + snapshot.fluidd_error)
        preferences = snapshot.fluidd_macros
        try:
            record = installed_records(snapshot).get(screws.PYTHON, {}).get("fluidd_category")
        except ValueError:
            record = None
        if record:
            preferences = fluidd_categories.disable(preferences, record)
        categorized, _ = fluidd_categories.enable(preferences)
        diff += category_difference({"expected": preferences, "value": categorized})
        return diff, "Baseline: on-printer original backups, or scanned files before the first Apply."
    if path == smoothing.TARGET:
        if mod_snapshot(snapshot, path) is None:
            raise ValueError("This mod's printer files have not been scanned yet.")
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
            enabled = stamp(module.TARGET, module.transform(original, True))
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
    elif path == warmup.TARGET:
        module, value = warmup, snapshot.warmup
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
    enabled = stamp(path, module.transform(original, True))
    diff = "".join(difflib.unified_diff(original.decode().splitlines(True), enabled.decode().splitlines(True),
                                        fromfile=before_name, tofile="Enabled " + path, n=3))
    return diff or "The baseline already contains this mod's enabled values; no file changes are needed.", baseline


def rescan(connection, known_hosts, scanned, progress=lambda message: None):
    scan_options = {}
    if scanned.ignored_mods:
        scan_options["ignored_mods"] = scanned.ignored_mods
    if scanned.unknown_files:
        scan_options["unknown_paths"] = tuple(scanned.unknown_files)
    return scan(connection, known_hosts, progress, **scan_options)


def unknown_startup_repair(snapshot, targets):
    """Only allow saved undo that removes this exact owned missing include."""
    info = (snapshot.status or {}).get("info", {})
    message = info.get("state_message", "")
    match = re.fullmatch(r"Include file '([^'\r\n]+)' does not exist", message.splitlines()[0] if message else "")
    if (not targets or info.get("state") != "error" or info.get("config_file") != mesh.TARGET or not match):
        return False
    try:
        missing = posixpath.normpath(match[1])
        records = installed_records(snapshot, removal=True)
        owned = any("created" in records[owner].get("files", {}).get(missing, {}) for owner in targets)
        value = file_snapshot(snapshot, missing)
        if (not owned or value.data is not None or value.sha256 is not None or value.backup_error
                or not value.original_absent or value.original_hash != mods.digest(b"")):
            return False
        network.require_idle(snapshot.status, allow_error=True)
        pending = prepare_removal(snapshot, {owner: False for owner in targets})
        def references(path, data):
            if data is None:
                return False
            regular, _ = mesh.split(data)
            return any(posixpath.normpath(posixpath.join(posixpath.dirname(path), name.strip())) == missing
                       for name in re.findall(r"^\s*\[include ([^\]\r\n]+)\]\s*(?:[#;].*)?$", regular, re.M))
        return any(path.endswith(".cfg") and data is not None
                   and references(path, file_snapshot(snapshot, path).data) and not references(path, data)
                   for path, data in pending.items())
    except (ValueError, RuntimeError, TypeError, KeyError, AttributeError, UnicodeError, StopIteration):
        return False


def apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk, progress, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None, retire_old_backups=False, repair=None, unknown_removals=(), *, warmup_desired=None):
    progress("Checking fresh file contents and printer activity…")
    fresh = rescan(connection, known_hosts, scanned, progress)
    if fresh.status is None:
        raise RuntimeError("Cannot verify printer activity: " + fresh.status_error)
    choices = (desired, mesh_desired, temperature_desired, screws_desired, smoothing_desired, warmup_desired)
    selected = dict(zip((mods.TARGET, mesh.TARGET, mesh_temperature.TARGET, screws.PYTHON, smoothing.TARGET, warmup.TARGET), choices))
    selected.update({path: False for path in unknown_removals})
    if repair is not None:
        allowed = set(repair["targets"])
        if (restore or update or full_risk or retire_old_backups or not allowed
                or {path for path, choice in selected.items() if choice is True} != allowed
                or any(choice is False for choice in choices)):
            raise ValueError("A repair reinstall may only enable its selected mods.")
    if retire_old_backups:
        if restore or update or any(choice is True for choice in choices):
            raise ValueError("Backup cleanup cannot be combined with installing, updating or restoring mods.")
    if fresh.storage_cleanup != scanned.storage_cleanup:
        raise RuntimeError("Original backups changed after scan. Scan again before applying changes.")
    scanned, planned_retire, _ = storage_plan(scanned, selected, restore, cleanup_all=retire_old_backups)
    fresh, retire, moves = storage_plan(fresh, selected, restore, cleanup_all=retire_old_backups)
    if retire != planned_retire:
        raise RuntimeError("Mod ownership changed after scan. Scan again before applying changes.")
    retired_paths = {file["path"] for pair in retire for file in pair["files"] if file["sha256"] is not None}
    aliases = {}
    for pair in fresh.storage_cleanup:
        canonical = storage.original(pair["target"])
        if all(file["sha256"] is not None for file in pair["files"]) and (canonical not in aliases or pair["backup"] == canonical):
            aliases.update({canonical: pair["backup"], canonical + ".json": pair["backup"] + ".json"})
    require_apply_policy(fresh.status, choices, restore, update)
    if network.device_identity(fresh.status) != network.device_identity(scanned.status):
        raise ValueError("Printer firmware changed after scan. Scan again before applying changes.")
    metadata_only = not restore and not update and not unknown_removals and all(choice is None for choice in choices)
    startup_removal = bool(screws_desired is False and fresh.recovery and fresh.recovery.get("available"))
    removal_only = not restore and not update and not any(choice is True for choice in choices)
    unknown_startup = bool(removal_only and unknown_startup_repair(fresh, unknown_removals)
                           and fresh.status.get("info") == scanned.status.get("info"))
    repair_includes = bool(screws_desired is False and removal_only
                          and removal_statuses(fresh)[screws.PYTHON].get("available"))
    repair_includes |= unknown_startup
    allow_error = unknown_startup or (recovery.supports_startup_repair(fresh.status)
                                     and (metadata_only or (startup_removal and removal_only)))
    if (repair is not None and repair.get("startup_error")
            and repair["startup_error"].get("state") == "error"
            and fresh.status.get("info") == repair["startup_error"]
            and not (fresh.recovery and fresh.recovery.get("available"))
            and not any(value.analysis.get("include_error") for _, value in file_entries(fresh) if value)):
        # The verified cleanup repaired the include on disk; Klipper still has
        # its old startup error until the user's physical power cycle.
        allow_error = True
    network.require_idle(fresh.status, allow_error=allow_error)
    if fresh.storage_moves != scanned.storage_moves:
        raise RuntimeError("Backup storage changed after the last scan. Scan again; staged choices are retained.")
    if moves and fresh.storage_error and not (repair_includes and fresh.storage_error == INCLUDE_STORAGE_ERROR):
        raise ValueError("Backup storage migration is blocked: " + fresh.storage_error)
    if moves and fresh.mesh and fresh.mesh.analysis.get("include_error") and not repair_includes:
        raise ValueError("Backup storage migration requires verified configuration includes. Repair startup and scan again before Apply.")
    scope = {"repair_targets": repair["targets"]} if repair is not None else {}
    if warmup_desired is not None:
        scope["warmup_desired"] = warmup_desired
    if unknown_removals:
        scope["unknown_removals"] = unknown_removals
    before = prepare_all(scanned, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired, **scope)
    removal_states = removal_statuses(fresh) if not restore and (unknown_removals or any(c is False for c in choices)) else {}
    recover_choices = {path: False for path, choice in selected.items() if choice is False
                       and removal_states[path].get("recovery")} if not restore else {}
    recovered = prepare_removal(fresh, recover_choices, with_recovery=True)[1] if recover_choices else set()
    stock_guarded = not restore and bool(unknown_removals or any(c is False for c in choices)) and bool((fresh.stock or {}).get("available"))
    if (recovered or stock_guarded) and (not fresh.stock or not fresh.stock.get("available")
            or fresh.stock.get("identity") != (scanned.stock or {}).get("identity")
            or fresh.stock.get("proof") != (scanned.stock or {}).get("proof")
            or fresh.stock.get("guards") != (scanned.stock or {}).get("guards")):
        raise RuntimeError("Stock firmware recovery sources changed after scan. Scan again before applying changes.")
    for path in before:
        old = file_snapshot(scanned, path)
        new = file_snapshot(fresh, path)
        unchanged = (new.sha256 == old.sha256 and new.guards == old.guards) if new.data is None or old.data is None or path in (fluidd_screws.TARGET, mod_versions.TARGET) else (
            (new.sha256 == old.sha256) if path in fresh.unknown_files or path in (mods.TARGET, screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG, smoothing.HOMING.TARGET, warmup.TARGET) else (
            mesh.stable_digest(new.data) == mesh.stable_digest(old.data)
            and (new.stable_guards or new.guards) == (old.stable_guards or old.guards)
            and new.patterns == old.patterns))
        if (not unchanged or new.original_hash != old.original_hash or new.backup_guards != old.backup_guards
                or getattr(new, "original_absent", False) != getattr(old, "original_absent", False)):
            raise RuntimeError("Printer files changed after the last scan. Scan again; staged choices are retained.")
    pending = prepare_all(fresh, desired, mesh_desired, restore, full_risk, temperature_desired, screws_desired, update, smoothing_desired, **scope)
    category = category_change(fresh, pending)
    if repair is not None:
        retained = installed_records(fresh)
        updated = mod_versions.load(pending.get(mod_versions.TARGET, version_snapshot(fresh).data))
        if ({path: record for path, record in updated.items() if path not in allowed}
                != {path: record for path, record in retained.items() if path not in allowed}
                or not set(pending).issubset({filename for path in allowed for filename in mod_versions.FILES[path]}
                                            | {mod_versions.TARGET})):
            raise ValueError("Reinstall would change an unrelated mod or file.")
    if set(pending) != set(before) or (mod_versions.TARGET in pending and pending[mod_versions.TARGET] != before[mod_versions.TARGET]):
        raise RuntimeError("Available original backups changed after scan. Scan again before restoring.")
    # Include unchanged restored sources and an already-deleted helper so a
    # previous uninstall's orphan bytecode can still be removed.
    removed_python = {path for owner, state in removal_states.items()
                      if selected.get(owner) is False and state.get("available")
                      for path in state["files"] if path.endswith(".py")}
    python_caches = {path: file_snapshot(fresh, path).sha256 for path, data in pending.items()
                     if path.endswith(".py")
                     and (restore or path in recovered or path in removed_python or network.is_untested(fresh.status)
                          or data is None or data != file_snapshot(fresh, path).data)}
    pending = {path: data for path, data in pending.items()
               if data != file_snapshot(fresh, path).data}
    if not pending and not python_caches and not moves and not retire and not category:
        return {"snapshot": fresh, "message": "Printer files already match the requested state.", "restarted": False, "written": False}
    enabling_heat = desired is not False and not restore and not unknown_removals
    if mods.TARGET in pending and enabling_heat and not fresh.identity.get("history"):
        raise RuntimeError("Native print history is unavailable; early heating cannot identify new jobs.")
    with Remote(connection, known_hosts) as remote:
        if mods.TARGET in pending and enabling_heat:
            capabilities = remote.python('''import json
with open("/usr/share/klipper/klippy/gcode.py") as f: text=f.read()
print(json.dumps(all(token in text for token in ("def check_cancel_running(","def invoke_action(","cancel_pending"))))
''')
            if not capabilities:
                raise RuntimeError("The required native cancellation support is missing.")
        items = []
        strict_files = None
        if network.is_untested(fresh.status) or recovered or removal_only:
            enabled = {path for path, choice in selected.items() if choice is True}
            if enabled and repair is None:
                enabled.update(automatic_updates(fresh, selected))
            if update:
                updates = outdated_mods(fresh, update_statuses(fresh), firmware_issues(fresh))
                enabled.update(path for path in updates if selected[path] is not False)
            strict_files = {filename for path in enabled for filename in mod_versions.FILES[path]}
            if any(path in strict_files and file_snapshot(fresh, path).original_hash is None for path in recovered):
                raise ValueError("Recover leftovers before installing or updating another mod that shares a file without its original backup.")
        unknown_files = {filename for owner in unknown_removals
                         for filename in mod_versions.file_scope(owner, installed_records(fresh)[owner])}
        for path, data in pending.items():
            module, value = next((module, value) for module, value in file_entries(fresh) if module.TARGET == path)
            item = {"path": path, "backup": aliases.get(module.ORIGINAL, module.ORIGINAL), "expected": value.sha256,
                    "data": base64.b64encode(data).decode() if data is not None else None,
                    "new_hash": mods.digest(data) if data is not None else None,
                    "original_hash": value.original_hash, "may_capture_original": value.analysis["enabled"] is False,
                    "original_absent": getattr(value, "original_absent", False),
                    "reference": module.REFERENCE_HASH, "restore": restore,
                    "kind": "python" if path.endswith(".py") else "json" if path.endswith(".json") else "config"}
            if path.endswith(".js") and path in unknown_files and data is not None:
                item.update(kind="javascript", replacements=[(value.data.decode("utf-8"), data.decode("utf-8"))])
            if path == mesh.TARGET:
                if screws.screws_include_path(mesh.split(value.data)[0]) is not None:
                    item["may_capture_original"] = False
                if not restore and (strict_files is None or path in strict_files):
                    item["settings"] = [[section, key, raw] for (section, key), raw in mesh.Document(data).raw.items()]
            elif path == mesh_temperature.TARGET and not restore and (strict_files is None or path in strict_files):
                item["settings"] = mesh_temperature.settings(data)
            elif path == smoothing.TARGET and not restore and (strict_files is None or path in strict_files):
                item["settings"] = smoothing.settings(data)
            elif path == fluidd_screws.TARGET:
                item["kind"] = "javascript"
                if not restore and path not in unknown_files:
                    item["replacements"] = helper_js_replacements(fresh, data, tolerant=path in recovered or screws_desired is False)
            elif path == mod_versions.TARGET:
                item["original_paths"] = [mod_versions.TARGET, storage.LEGACY_RECORDS]
            if path in recovered:
                source = fresh.stock["files"][path]
                item.update(recovery="stock", backup=storage.recovery(path, value.sha256),
                            stock_source=source["source"], stock_hash=source["sha256"])
                # This is current-firmware absence proof, not an invented
                # initial-original manifest. The batch validates its ROM guard.
                item["original_absent"] = data is None and source["data"] is None
            elif path == mod_versions.TARGET and missing_record_proof(fresh):
                old_records, new_records = installed_records(fresh), mod_versions.load(data)
                changed_records = sorted(owner for owner in old_records.keys() | new_records.keys()
                                         if old_records.get(owner) != new_records.get(owner))
                item.update(recovery="records", backup=storage.recovery(path, value.sha256),
                            record_targets=changed_records, restore=False)
            items.append(item)
        # Install dependencies before adding the include; remove the include
        # before deleting dependencies. printer.cfg is written once for all mods.
        helper_paths = (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG)
        items.sort(key=lambda item: (0 if item["data"] is not None else 2)
                   if item["path"] in helper_paths else 1)
        if unknown_removals:
            items.sort(key=lambda item: item["data"] is None)
        if set(mod_versions.FILES[smoothing.TARGET]) & pending.keys():
            homing_data = pending.get(smoothing.TARGET, fresh.smoothing.data)
            homing_enabling = homing_data is not None and smoothing.inspect(homing_data,
                fresh.smoothing.original).get("enabled") is True
            # Register the native command before its config starts calling it;
            # remove that config call before removing the Python command.
            items.sort(key=lambda item: item["path"] == (
                smoothing.TARGET if homing_enabling else smoothing.HOMING.TARGET))
        # Commit records after the working files they describe.
        items.sort(key=lambda item: item["path"] == mod_versions.TARGET)
        guards, patterns = {}, {}
        if retire:
            records = installed_records(fresh)
            scopes = record_scopes(records)
            owners = {owner for pair in retire for owner, scope in scopes.items()
                      if scope is None or pair["target"] in scope or (owner == screws.PYTHON and pair["target"] == screws.LEGACY_CONFIG)}
            if any(scopes[owner] is None for owner in owners):
                raise ValueError("Unknown mod ownership must be resolved before deleting backups.")
            dependencies = {path for owner in owners for path in scopes[owner]}
            if screws.PYTHON in owners:
                dependencies.add(screws.LEGACY_CONFIG)
            dependencies.add(mod_versions.TARGET)
            for path in dependencies:
                value = file_snapshot(fresh, path)
                if value is None:
                    raise ValueError("Backup cleanup requires a complete scan of mod files: " + path)
                guards[path] = value.sha256
        if stock_guarded:
            guards.update(fresh.stock["guards"])
        # Unchanged files in a metadata-only enrollment still require the same
        # race checks as files we replace, including their immutable originals.
        for path in before:
            module, value = next((module, value) for module, value in file_entries(fresh) if module.TARGET == path)
            if path not in pending:
                guards[path] = value.sha256
            guards.update({name: digest for name, digest in value.backup_guards.items()
                           if digest is not None or path in recovered or
                           (path == mod_versions.TARGET and (removal_only or missing_record_proof(fresh)))})
            if path == mod_versions.TARGET and missing_record_proof(fresh):
                for name in (mod_versions.ORIGINAL, mod_versions.MANIFEST):
                    guards[name] = guards[storage.legacy_path(name)] = None
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
            guards.update({name: digest for name, digest in ledger.backup_guards.items()
                           if digest is not None or removal_only})
        # Files in this batch have their own before/after checks; including them
        # as unchanged dependencies would reject our own preceding replacements.
        if moves and fresh.mesh:
            guards.update(fresh.mesh.guards)
            guards[mesh.TARGET] = fresh.mesh.sha256
            patterns.update(fresh.mesh.patterns)
        guards = {path: digest for path, digest in guards.items() if path not in pending}
        for path in list(guards):
            if path in aliases and aliases[path] != path:
                guards[aliases[path]] = guards.pop(path)
        for pair in retire:
            guards.update({file["path"]: file["sha256"] for file in pair["files"]})
        for move in moves:
            if move["destination"] not in pending:
                guards[move["destination"]] = move["source_hash"]
            if any(fnmatch.fnmatchcase(move["source"], pattern) for pattern in patterns):
                raise ValueError("An active config include matches a legacy backup; review it before moving backups.")
        progress("Securing original backups and preparing all replacements…")
        live_status = network.status(connection.host)
        require_apply_policy(live_status, choices, restore, update)
        if network.device_identity(live_status) != network.device_identity(fresh.status):
            raise ValueError("Printer firmware changed during Apply. Scan again before applying changes.")
        network.require_idle(live_status, allow_error=allow_error)
        if allow_error and live_status.get("info", {}).get("state") == "error" and not metadata_only:
            if live_status.get("info") != fresh.status.get("info"):
                raise ValueError("Printer startup error changed during Apply. Scan again before removing mods.")
        progress("Writing printer files…")
        outcome = remote.python(APPLY_BATCH, {"files": items, "python_caches": python_caches, "moves": list(moves), "retire": list(retire),
                                   "fluidd_category": category,
                                   "guards": guards, "patterns": patterns,
                                   "verify_config": screws_desired is False or bool(unknown_removals),
                                   "firmware": fresh.status["native"].get("modelVersion"),
                                   "model": fresh.status["native"].get("model")})
    progress("Verifying saved printer files…")
    result = rescan(connection, known_hosts, scanned, progress)
    if category and (result.fluidd_error or result.fluidd_macros != category["value"]):
        raise RuntimeError("Saved printer files, but Fluidd macro preferences could not be verified. Scan again before proceeding.")
    if retire and set(outcome.get("retired", ())) != retired_paths:
        raise RuntimeError("Backup cleanup did not complete. Scan the printer before applying again.")
    moved_sources = {move["source"] for move in moves}
    if any(move["source"] in moved_sources for move in result.storage_moves):
        raise RuntimeError("Backup storage migration could not be verified. Scan the printer before applying again.")
    if any(file_snapshot(result, pair["target"]).original_hash is not None
           or any(file_snapshot(result, pair["target"]).backup_guards.values()) for pair in retire):
        raise RuntimeError("Backup cleanup could not be verified. Scan the printer before applying again.")
    for path, data in pending.items():
        value = file_snapshot(result, path)
        if value.sha256 != (mods.digest(data) if data is not None else None):
            raise RuntimeError("A saved file changed after Apply. Rescan before proceeding.")
    cleared = bool(outcome and outcome.get("cleared"))
    moved = bool(outcome and outcome.get("moved"))
    retired = bool(outcome and outcome.get("retired"))
    if not pending and not cleared and not moved and not retired:
        return {"snapshot": result, "message": "Printer files already match the requested state.", "restarted": False, "written": False}
    power_cycle = any(path != mod_versions.TARGET for path in pending) or cleared
    return {"snapshot": result, "message": "Saved and verified. Power cycle the printer before printing." if power_cycle
            else "Obsolete or unusable backups removed; current backups relocated." if retired and moved
            else "Obsolete or unusable backups removed." if retired
            else "Backups and uninstall records saved in /mnt/UDISK/Uncurser/." if moved
            else "Uninstall records removed." if pending == {mod_versions.TARGET: None}
            else "Version stamps and undo records saved.", "restarted": False, "written": True, "power_cycle": power_cycle}


def apply(connection, known_hosts, scanned, desired, restore=False, full_risk=False,
          progress=lambda message: None, mesh_desired=None, temperature_desired=None, screws_desired=None, update=False, smoothing_desired=None, retire_old_backups=False, repair=None, unknown_removals=(), warmup_desired=None):
    require_apply_policy(scanned.status, (desired, mesh_desired, temperature_desired, screws_desired, smoothing_desired, warmup_desired), restore, update)
    extra = {"warmup_desired": warmup_desired} if warmup_desired is not None else {}
    return apply_many(connection, known_hosts, scanned, desired, mesh_desired, restore, full_risk,
                      progress, temperature_desired, screws_desired, update, smoothing_desired, retire_old_backups, repair, unknown_removals, **extra)


def fix_issues(connection, known_hosts, scanned, progress=lambda message: None, *, reinstall_targets=(), unknown_removals=()):
    """Remove verified issues, then reinstall only the user's compatible choices."""
    issues = firmware_issues(scanned)
    if storage.ROOT in issues and not issues[storage.ROOT]["available"]:
        raise ValueError("Cannot fix backup storage: " + issues[storage.ROOT]["reason"])
    targets = {path for path in mod_versions.VERSIONS
               if issues.get(path, {}).get("present") and issues[path].get("available")}
    unknown_removals = set(unknown_removals)
    if any(not issues.get(path, {}).get("unknown") or not issues[path].get("available")
           for path in unknown_removals):
        raise ValueError("The selected unknown mod could not be verified for removal. Scan again.")
    reinstall_targets = set(reinstall_targets)
    if any(path not in targets or not issues[path].get("reinstall") for path in reinstall_targets):
        raise ValueError("The selected mod could not be verified for reinstallation. Scan again.")
    fields = {mods.TARGET: "desired", mesh.TARGET: "mesh_desired", mesh_temperature.TARGET: "temperature_desired",
              screws.PYTHON: "screws_desired", smoothing.TARGET: "smoothing_desired"}
    if warmup.TARGET in targets or warmup.TARGET in reinstall_targets:
        fields[warmup.TARGET] = "warmup_desired"
    options = {field: False if path in targets else None for path, field in fields.items()}
    extra = {"unknown_removals": tuple(sorted(unknown_removals))} if unknown_removals else {}
    result = apply(connection, known_hosts, scanned, progress=progress, **options, **extra,
                   retire_old_backups=bool(scanned.storage_cleanup))
    if not reinstall_targets:
        return result
    cleaned = result["snapshot"]
    install_started = False
    try:
        if network.device_identity(cleaned.status) != network.device_identity(scanned.status):
            raise ValueError("Printer firmware changed during cleanup. Scan again before reinstalling mods.")
        progress("Checking compatible mods for reinstallation…")
        startup_error = (scanned.status.get("info")
                         if (scanned.recovery and scanned.recovery.get("available"))
                         or unknown_startup_repair(scanned, unknown_removals) else None)
        options = {field: True if path in reinstall_targets else None for path, field in fields.items()}
        install_started = True
        installed = apply(connection, known_hosts, cleaned, progress=progress, **options,
                          repair={"targets": tuple(sorted(reinstall_targets)), "startup_error": startup_error})
    except Exception as error:
        # The cleanup already committed. Never roll old incompatible code back
        # over a successful repair, or claim a failed post-write scan wrote nothing.
        try:
            result["snapshot"] = rescan(connection, known_hosts, scanned, progress)
        except Exception:
            pass
        if install_started:
            result.update(write_uncertain=True, power_cycle=True)
        result.update(message="Cleanup completed.", repair_error="Reinstallation could not be verified: "
                      + (str(error) or type(error).__name__))
        return result
    installed["written"] = bool(installed.get("written") or result.get("written"))
    installed["power_cycle"] = bool(installed.get("power_cycle") or result.get("power_cycle"))
    return installed
