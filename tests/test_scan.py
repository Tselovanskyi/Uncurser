"""Read-only scan batching/concurrency checks using synthetic printer files."""
import base64
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing, storage
from app.remote import CONFIG_TREE, Connection
from tests import test_mesh, test_mod_versions, test_screws, test_smoothing


def record(data):
    return None if data is None else {"data": data, "sha256": mods.digest(data), "mode": 0o644, "uid": 0, "gid": 0}


def documents():
    current = test_mod_versions.installed_snapshot()
    files, versions = {}, {}
    for path, original in test_mod_versions.SOURCES.items():
        module = mod_versions.MODULES[path]
        applied = engine.file_snapshot(current, path).data
        files[path] = applied
        files[module.ORIGINAL] = original
        files[module.MANIFEST] = json.dumps(dict(path=path, sha256=mods.digest(original),
            mode=0o644, uid=0, gid=0, model="F008", firmware="1.1.6.4")).encode()
        versions[path] = mod_versions.capture(path, original, applied)
    files[mod_versions.TARGET] = mod_versions.dump(versions)
    files[mod_versions.ORIGINAL] = b""
    files[mod_versions.MANIFEST] = json.dumps(dict(path=mod_versions.TARGET,
        sha256=mods.digest(b""), absent=True, mode=0o644, uid=0, gid=0)).encode()
    files[fluidd_screws.INDEX] = ('<script type="module" src="./' + fluidd_screws.ASSET + '"></script>').encode()
    files.update(test_screws.FLUIDD_SOURCES)
    return files


class Reader:
    """Require scans to read prefetched values rather than individual commands."""
    def __init__(self, files, hook=None):
        self.files = dict(files)
        self.hook = hook
        self.commands = []
        self.paths = None
        self.cache = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def prefetch(self, paths):
        self.commands.append("prefetch")
        self.paths = tuple(paths)
        if self.hook:
            self.hook()
        self.cache = {path: self.files.get(path) for path in self.paths}

    def read(self, path):
        if path not in self.cache:
            raise AssertionError("Scan attempted an unbatched read: " + path)
        value = self.cache[path]
        if isinstance(value, Exception):
            raise value
        return record(value)

    def identity(self):
        self.commands.append("identity")
        return {"history": True, "hostname": "fixture"}

    def python(self, program, payload):
        if program != CONFIG_TREE or payload != {"path": mesh.TARGET}:
            raise AssertionError("Unexpected remote command")
        self.commands.append("config tree")
        active = {path: self.files[path] for path in (mesh.TARGET, mesh_temperature.TARGET)}
        return {"files": {path: {"data": base64.b64encode(data).decode(), "sha256": mods.digest(data)}
                          for path, data in active.items()}, "patterns": {}}


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.files = documents()
        self.readers = []
        self.prefetch_hook = None
        self.connection = Connection("192.0.2.1")
        self.enterContext(patch.object(engine, "Remote", side_effect=self.remote))
        self.enterContext(patch.object(mods, "REFERENCE_HASH", mods.digest(test_mod_versions.SOURCES[mods.TARGET])))
        self.enterContext(patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_mesh.SOURCE)))
        self.enterContext(patch.object(fluidd_screws, "FILES", tuple(
            fluidd_screws.FluiddFile(path, mods.digest(data))
            for path, data in test_screws.FLUIDD_SOURCES.items())))

    def remote(self, *args):
        value = Reader(self.files, self.prefetch_hook)
        self.readers.append(value)
        return value

    def scan(self):
        return engine.scan(self.connection, Path("unused"))

    def test_missing_or_changed_firmware_stamps_read_stock_on_tested_firmware(self):
        for firmware in (None, "1.1.6.3", "1.1.6.4"):
            with self.subTest(firmware=firmware):
                records = mod_versions.load(self.files[mod_versions.TARGET])
                for record in records.values():
                    record.pop("firmware", None)
                    if firmware is not None:
                        record["firmware"] = firmware
                self.files[mod_versions.TARGET] = mod_versions.dump(records)
                proof = {"available": False, "reason": "fixture"}
                with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS), \
                        patch.object(engine.stock, "scan", return_value=proof) as stock_scan:
                    result = self.scan()
                self.assertEqual(stock_scan.call_count, int(firmware != "1.1.6.4"))
                self.assertEqual(result.stock, proof if firmware != "1.1.6.4" else None)

    def test_status_runs_while_batched_file_read_is_held(self):
        file_read_started, status_started = threading.Event(), threading.Event()

        def held_prefetch():
            file_read_started.set()
            self.assertTrue(status_started.wait(3), "Status was not started concurrently with file reads")

        def status(host):
            self.assertEqual(host, self.connection.host)
            self.assertTrue(file_read_started.wait(3), "Status waited for file reads to finish")
            status_started.set()
            return test_mod_versions.STATUS

        self.prefetch_hook = held_prefetch
        with patch.object(engine.network, "status", side_effect=status):
            result = self.scan()
        self.assertEqual(result.status, test_mod_versions.STATUS)
        self.assertEqual(result.status_error, "")

    def test_all_fixed_files_and_dependency_probes_are_batched_once(self):
        expected = {mods.TARGET, mods.ORIGINAL, mods.MANIFEST,
                    mesh.ORIGINAL, mesh.MANIFEST, mesh_temperature.TARGET, mesh_temperature.ORIGINAL, mesh_temperature.MANIFEST,
                    fluidd_screws.INDEX, mod_versions.TARGET, mod_versions.ORIGINAL, mod_versions.MANIFEST}
        expected.add(mesh.TARGET)
        expected.update((screws.LEGACY_FILE.TARGET, screws.LEGACY_FILE.ORIGINAL, screws.LEGACY_FILE.MANIFEST,
                         engine.smoothing.TARGET, engine.smoothing.ORIGINAL, engine.smoothing.MANIFEST,
                         engine.smoothing.HOMING.TARGET, engine.smoothing.HOMING.ORIGINAL, engine.smoothing.HOMING.MANIFEST))
        for module in screws.FILES:
            expected.update((module.TARGET, module.ORIGINAL, module.MANIFEST))
        for module in fluidd_screws.FILES:
            expected.update((module.TARGET, module.ORIGINAL, module.MANIFEST,
                             module.TARGET + ".gz", module.TARGET + ".br"))
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            self.scan()
        self.assertEqual(set(self.readers[0].paths), set(storage.scan_paths(expected)))
        self.assertEqual(len(self.readers[0].paths), len(storage.scan_paths(expected)))
        self.assertCountEqual(self.readers[0].commands, ("prefetch", "identity", "config tree"))

    def test_halted_new_firmware_still_offers_narrow_recovery_without_writing(self):
        from app.config_blocks import LEGACY_SCREWS_INCLUDE
        from tests.test_recovery import halted
        self.files[mesh.TARGET] = LEGACY_SCREWS_INCLUDE.encode() + self.files[mesh.TARGET]
        original = self.files[mesh.TARGET]
        with patch.object(engine.network, "status", return_value=halted()), \
                patch.object(Reader, "python", side_effect=ValueError("Config include has no matching files")):
            result = self.scan()
        self.assertTrue(result.recovery["available"], result.recovery)
        self.assertEqual(result.recovery["after"], original[len(LEGACY_SCREWS_INCLUDE.encode()):])
        self.assertEqual(self.files[mesh.TARGET], original)
        self.assertEqual(engine.network.compatibility_error(result.status), "")
        self.assertEqual(result.mesh.data, original)
        self.assertEqual(result.mesh.sha256, mods.digest(original))
        self.assertEqual(result.mesh.original, self.files[mesh.ORIGINAL])
        self.assertEqual(result.mesh.backup_guards[mesh.MANIFEST], mods.digest(self.files[mesh.MANIFEST]))
        self.assertEqual(result.mesh.analysis["label"], "Unavailable")
        self.assertIn("Config include", result.mesh.analysis["reason"])
        self.assertFalse(result.mesh.analysis["patchable"])

    def test_scan_records_only_validated_manifest_identity_without_guessing(self):
        for path in tuple(self.files):
            if path.endswith(".Original.txt.json"):
                manifest = json.loads(self.files[path])
                manifest.update(model="F008", firmware="DWIN sw ver:1.1.6.4;")
                self.files[path] = json.dumps(manifest).encode()
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            result = self.scan()
        for value in (result, result.mesh, result.mesh_temperature, result.versions):
            self.assertEqual(value.original_identity, ("F008", "1.1.6.4"))
        self.assertIsNone(result.smoothing.original_identity)

        manifest = json.loads(self.files[mesh.MANIFEST])
        manifest["firmware"] = "not a verified version"
        self.files[mesh.MANIFEST] = json.dumps(manifest).encode()
        manifest = json.loads(self.files[mesh_temperature.MANIFEST])
        manifest["sha256"] = "damaged checksum"
        self.files[mesh_temperature.MANIFEST] = json.dumps(manifest).encode()
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            changed = self.scan()
        self.assertIsNone(changed.mesh.original_identity)
        self.assertIsNone(changed.mesh_temperature.original_identity)
        self.assertIn("checksum", changed.mesh_temperature.backup_error)

    def test_failed_config_tree_keeps_raw_data_and_still_validates_backups(self):
        manifest = json.loads(self.files[mesh.MANIFEST])
        manifest.update(model="F008", firmware="1.1.6.4")
        self.files[mesh.MANIFEST] = json.dumps(manifest).encode()
        manifest = json.loads(self.files[mesh_temperature.MANIFEST])
        manifest["sha256"] = "damaged checksum"
        self.files[mesh_temperature.MANIFEST] = json.dumps(manifest).encode()
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS), \
                patch.object(Reader, "python", side_effect=ValueError("missing helper include")):
            result = self.scan()
        self.assertEqual(result.mesh.original_identity, ("F008", "1.1.6.4"))
        self.assertEqual(result.mesh_temperature.data, self.files[mesh_temperature.TARGET])
        self.assertEqual(result.mesh_temperature.original, self.files[mesh_temperature.ORIGINAL])
        self.assertIn("checksum", result.mesh_temperature.backup_error)
        for value in (result.mesh, result.mesh_temperature):
            self.assertEqual(value.analysis["label"], "Unavailable")
            self.assertFalse(value.analysis["patchable"])

    def test_homing_helper_and_fluidd_originals_have_validated_identity(self):
        identities = {"model": "F008", "firmware": "1.1.6.4"}
        self.files[smoothing.HOMING.TARGET] = test_smoothing.HOMING_SOURCE
        self.files[smoothing.HOMING.ORIGINAL] = test_smoothing.HOMING_SOURCE
        self.files[smoothing.HOMING.MANIFEST] = json.dumps(dict(
            path=smoothing.HOMING.TARGET, sha256=mods.digest(test_smoothing.HOMING_SOURCE),
            mode=0o644, uid=0, gid=0, **identities)).encode()
        for module in screws.SCAN_FILES:
            self.files[module.ORIGINAL] = b""
            self.files[module.MANIFEST] = json.dumps(dict(path=module.TARGET,
                sha256=mods.digest(b""), absent=True, mode=0o644, uid=0, gid=0, **identities)).encode()
        module = fluidd_screws.FILES[0]
        self.files[module.ORIGINAL] = self.files[module.TARGET]
        self.files[module.MANIFEST] = json.dumps(dict(path=module.TARGET,
            sha256=mods.digest(self.files[module.TARGET]), mode=0o644, uid=0, gid=0, **identities)).encode()
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            result = self.scan()
        values = (result.homing, *result.screws.files.values(), *result.screws.frontend.values())
        for value in values:
            self.assertEqual(value.original_identity, ("F008", "1.1.6.4"))

    def test_changed_raw_config_and_active_tree_are_unavailable(self):
        raw = Reader(self.files)
        raw.prefetch(engine.scan_paths())
        tree = raw.python(CONFIG_TREE, {"path": mesh.TARGET})
        tree["files"][mesh.TARGET]["data"] = base64.b64encode(self.files[mesh.TARGET] + b"\n").decode()
        result = engine.scan_config(raw, mesh, tree)
        self.assertEqual(result.data, self.files[mesh.TARGET])
        self.assertEqual(result.analysis["label"], "Unavailable")
        self.assertIn("changed during scan", result.analysis["reason"])

    def test_existing_file_originals_reject_absence_proofs(self):
        modules = (mods, mesh, mesh_temperature, *fluidd_screws.FILES)
        for module in modules:
            if module.ORIGINAL not in self.files:
                self.files[module.ORIGINAL] = self.files[module.TARGET]
            for absent in (True, None, 0):
                with self.subTest(path=module.TARGET, absent=absent):
                    self.files[module.MANIFEST] = json.dumps(dict(path=module.TARGET,
                        sha256=mods.digest(self.files[module.ORIGINAL]), absent=absent,
                        mode=0o644, uid=0, gid=0, model="F008", firmware="1.1.6.4")).encode()
                    with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
                        result = self.scan()
                    value = engine.file_snapshot(result, module.TARGET)
                    self.assertTrue(value.backup_error)
                    self.assertIsNone(value.original_identity)

    def test_repeated_scans_get_fresh_file_cache_and_status(self):
        first_status = {**test_mod_versions.STATUS, "generation": 1}
        second_status = {**test_mod_versions.STATUS, "generation": 2}
        with patch.object(engine.network, "status", side_effect=(first_status, second_status)) as status:
            first = self.scan()
            self.files[mods.TARGET] += b"\n# later user edit\n"
            self.files[mesh.TARGET] = self.files[mesh.TARGET].replace(b"0.1, 0.2", b"0.3, 0.4")
            second = self.scan()
        self.assertEqual(status.call_count, 2)
        self.assertEqual((first.status, second.status), (first_status, second_status))
        self.assertEqual(second.data, first.data + b"\n# later user edit\n")
        self.assertIn(b"0.3, 0.4", second.mesh.data)
        self.assertIsNot(self.readers[0], self.readers[1])
        self.assertIsNot(self.readers[0].cache, self.readers[1].cache)
        self.assertEqual(first.original, second.original)

    def test_partial_file_and_status_errors_keep_other_mod_results(self):
        self.files[fluidd_screws.TARGET] = RuntimeError("Fluidd fixture unreadable")
        with patch.object(engine.network, "status", side_effect=RuntimeError("Status fixture unavailable")):
            result = self.scan()
        self.assertIsNone(result.status)
        self.assertEqual(result.status_error, "Status fixture unavailable")
        for path in test_mod_versions.SOURCES:
            value = engine.file_snapshot(result, path)
            self.assertTrue(value.analysis["known"])
            self.assertTrue(value.analysis["enabled"])
            self.assertEqual(value.original, test_mod_versions.SOURCES[path])
            self.assertEqual(engine.version_details(result, path), "v" + mod_versions.VERSIONS[path])
        frontend = result.screws.frontend[fluidd_screws.TARGET]
        self.assertEqual(frontend.analysis["label"], "Unavailable")
        self.assertIn("Fluidd fixture unreadable", frontend.analysis["reason"])

    def test_batching_preserves_backup_and_installed_code_verification(self):
        manifest = json.loads(self.files[mesh.MANIFEST])
        manifest["sha256"] = "damaged checksum"
        self.files[mesh.MANIFEST] = json.dumps(manifest).encode()
        self.files[mods.TARGET] = self.files[mods.TARGET].replace(
            b"chamber = float(match.group(2))", b"chamber = 99")
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            result = self.scan()
        self.assertIn("checksum", result.mesh.backup_error)
        self.assertNotIn(mesh.TARGET, engine.restorable(result))
        self.assertFalse(result.analysis["known"])
        self.assertFalse(result.analysis["patchable"])
        self.assertEqual(engine.version_details(result, mods.TARGET), "Unknown version")
        self.assertTrue(result.mesh_temperature.analysis["known"])
        self.assertEqual(engine.version_details(result, mesh_temperature.TARGET), "v0.1.5")


if __name__ == "__main__":
    unittest.main()
