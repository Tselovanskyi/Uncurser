"""Two-file homing enrollment and printer-side undo, with local fixtures."""
import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mod_versions, mods, smoothing
from app.remote import Connection
from tests import test_mesh, test_mod_versions, test_smoothing


def originals():
    return {smoothing.TARGET: test_smoothing.SOURCE,
            smoothing.HOMING.TARGET: test_smoothing.HOMING_SOURCE}


def snapshot(enabled=False):
    current = test_mod_versions.installed_snapshot()
    for name, module in (("smoothing", smoothing), ("homing", smoothing.HOMING)):
        original = originals()[module.TARGET]
        data = module.transform(original, True) if enabled else original
        setattr(current, name, engine.MeshSnapshot(data, mods.digest(data), module.inspect(data, original),
            original if enabled else None, mods.digest(original) if enabled else None,
            original_identity=("F008", "1.1.6.4") if enabled else None))
    return current


def install_records(current, prepared):
    for module in (smoothing, smoothing.HOMING):
        value = engine.file_snapshot(current, module.TARGET)
        value.data = prepared[module.TARGET]
        value.sha256 = mods.digest(value.data)
        value.original = originals()[module.TARGET]
        value.original_hash = mods.digest(value.original)
        value.original_identity = ("F008", "1.1.6.4")
        value.analysis = module.inspect(value.data, value.original)
    ledger = prepared[mod_versions.TARGET]
    current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
        {"known": True, "enabled": True, "patchable": True},
        original_hash=mods.digest(b""), original_absent=True)


class SmoothingVersionTests(unittest.TestCase):
    def test_new_mod_enrolls_legacy_three_and_records_both_uninstalls(self):
        current = snapshot()
        prepared = engine.prepare_all(current, None, smoothing_desired=True)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(set(records), set(test_mod_versions.SOURCES) | {smoothing.TARGET})
        for path in test_mod_versions.SOURCES:
            self.assertEqual(prepared[path], engine.file_snapshot(current, path).data)
        record = records[smoothing.TARGET]
        self.assertEqual(record["version"], mod_versions.VERSIONS[smoothing.TARGET])
        self.assertEqual(set(record["files"]), set(originals()))
        self.assertEqual(mod_versions.undo_files(smoothing.TARGET,
            {path: prepared[path] for path in originals()}, record), originals())
        for module in (smoothing, smoothing.HOMING):
            self.assertIn(module.ORIGINAL, engine.scan_paths())
        initial, _ = engine.initial_difference(current, smoothing.TARGET)
        self.assertIn(smoothing.ENABLED_LINE, initial)
        self.assertIn(smoothing.HOMING.TARGET, initial)
        self.assertNotIn("Original.txt", initial)
        install_records(current, prepared)
        self.assertTrue(engine.mod_snapshot(current, smoothing.TARGET).analysis["enabled"])
        self.assertEqual(engine.version_details(current, smoothing.TARGET), "v" + mod_versions.VERSIONS[smoothing.TARGET])
        self.assertFalse(engine.update_statuses(current)[smoothing.TARGET]["needed"])

    def test_disable_preserves_unrelated_edits_and_full_restore_uses_both_backups(self):
        current = snapshot()
        install_records(current, engine.prepare_all(current, None, smoothing_desired=True))
        extra = b"\n[gcode_macro USER_MACRO]\ngcode:\n  G4 P123\n"
        generated = b"\n#*# <---------------------- SAVE_CONFIG ---------------------->\n#*# [bed_mesh default]\n#*# points = 0.123\n"
        current.smoothing.data += extra + generated
        current.homing.data += b"\n# unrelated user change\n"
        for module in (smoothing, smoothing.HOMING):
            value = engine.file_snapshot(current, module.TARGET)
            value.sha256 = mods.digest(value.data)
            value.analysis = module.inspect(value.data, value.original)
        prepared = engine.prepare_all(current, None, smoothing_desired=False, full_risk=True)
        self.assertEqual(prepared[smoothing.TARGET], test_smoothing.SOURCE + extra + generated)
        self.assertEqual(prepared[smoothing.HOMING.TARGET], test_smoothing.HOMING_SOURCE + b"\n# unrelated user change\n")
        self.assertNotIn(smoothing.TARGET, mod_versions.load(prepared[mod_versions.TARGET]))
        restored = engine.prepare_all(current, None, restore=True)
        self.assertEqual(restored[smoothing.TARGET], test_smoothing.SOURCE + generated.lstrip(b"\n"))
        self.assertEqual(restored[smoothing.HOMING.TARGET], test_smoothing.HOMING_SOURCE)
        broken = copy.deepcopy(current)
        broken.homing.data = broken.homing.data.replace(b"ACCEL=5000", b"ACCEL=5001")
        with self.assertRaisesRegex(ValueError, "undo record"):
            engine.prepare_all(broken, None, smoothing_desired=False, full_risk=True)
        broken = copy.deepcopy(current)
        broken.homing.original = None
        with self.assertRaisesRegex(ValueError, "backups"):
            engine.prepare_all(broken, None, smoothing_desired=False, full_risk=True)
        self.assertFalse(set(originals()) & engine.restorable(broken))

    def test_partial_installation_and_incomplete_record_are_blocked(self):
        current = snapshot()
        current.homing.data = smoothing.HOMING.transform(current.homing.data, True)
        current.homing.analysis = smoothing.HOMING.inspect(current.homing.data, test_smoothing.HOMING_SOURCE)
        self.assertIsNone(engine.mod_snapshot(current, smoothing.TARGET).analysis["enabled"])
        with self.assertRaisesRegex(ValueError, "partially installed"):
            engine.prepare_all(current, None, smoothing_desired=True, full_risk=True)
        complete = engine.prepare_all(snapshot(), None, smoothing_desired=True)
        records = mod_versions.load(complete[mod_versions.TARGET])
        del records[smoothing.TARGET]["files"][smoothing.HOMING.TARGET]
        with self.assertRaisesRegex(ValueError, "cover"):
            mod_versions.dump(records)

    def test_combined_enable_update_disable_and_python_fresh_guard(self):
        current = snapshot()
        prepared = engine.prepare_all(current, None, mesh_desired=False, smoothing_desired=True)
        self.assertEqual(prepared[mesh.TARGET], test_mesh.SOURCE)
        install_records(current, prepared)
        records = mod_versions.load(current.versions.data)
        records[smoothing.TARGET]["version"] = "0.1.12"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        updated = engine.prepare_all(current, None, update=True)
        self.assertEqual(mod_versions.load(updated[mod_versions.TARGET])[smoothing.TARGET]["version"], mod_versions.VERSIONS[smoothing.TARGET])
        removed = engine.prepare_all(current, None, smoothing_desired=False, update=True)
        self.assertEqual({path: removed[path] for path in originals()}, originals())
        self.assertNotIn(smoothing.TARGET, mod_versions.load(removed[mod_versions.TARGET]))
        scanned = snapshot()
        fresh = copy.deepcopy(scanned)
        fresh.homing.data += b"# changed after scan\n"
        fresh.homing.sha256 = mods.digest(fresh.homing.data)
        with patch.object(engine, "scan", return_value=fresh), patch.object(engine.network, "require_idle"), \
                patch.object(engine, "Remote") as remote:
            with self.assertRaisesRegex(RuntimeError, "changed after"):
                engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, None, smoothing_desired=True)
            remote.assert_not_called()

    def test_faulty_013_config_updates_via_recorded_uninstall_after_real_scan(self):
        current = snapshot()
        installed = engine.prepare_all(current, None, smoothing_desired=True)
        installed[smoothing.TARGET] = installed[smoothing.TARGET].replace(
            smoothing.ENABLED_LINE.encode(), b"_UNCURSER_HOME_CENTRE X{x_park} Y{y_park}")
        record = engine.capture_homing(originals(), {path: installed[path] for path in originals()})
        record["version"] = "0.1.13"
        records = mod_versions.load(installed[mod_versions.TARGET])
        records[smoothing.TARGET] = record
        installed[mod_versions.TARGET] = mod_versions.dump(records)
        install_records(current, installed)
        # Latest templates intentionally reject the malformed old syntax.
        self.assertFalse(smoothing.inspect(current.smoothing.data, current.smoothing.original)["patchable"])
        payloads = {}
        for module in (mods, mesh, engine.mesh_temperature, smoothing, smoothing.HOMING):
            value = engine.file_snapshot(current, module.TARGET)
            payloads[module.TARGET] = value.data
            payloads[module.ORIGINAL] = value.original
            payloads[module.MANIFEST] = json.dumps({"path": module.TARGET,
                "sha256": mods.digest(value.original), "mode": 0o644, "uid": 0, "gid": 0}).encode()
        payloads[mod_versions.TARGET] = current.versions.data
        payloads[mod_versions.ORIGINAL] = b""
        payloads[mod_versions.MANIFEST] = json.dumps({"path": mod_versions.TARGET,
            "sha256": mods.digest(b""), "absent": True, "mode": 0o644, "uid": 0, "gid": 0}).encode()
        tree = {"files": {path: {"data": base64.b64encode(payloads[path]).decode(),
            "sha256": mods.digest(payloads[path])}
            for path in (mesh.TARGET, engine.mesh_temperature.TARGET, smoothing.TARGET)}, "patterns": {}}

        class ReadOnlyRemote:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def prefetch(self, paths):
                pass
            def identity(self):
                return {"history": True}
            def read(self, path):
                data = payloads.get(path)
                return {"data": data, "sha256": mods.digest(data)} if data is not None else None
            def python(self, script, request):
                if script != engine.CONFIG_TREE:
                    raise AssertionError("This fixture permits only reading the config tree.")
                return tree

        with patch.object(engine, "Remote", return_value=ReadOnlyRemote()), \
                patch.object(engine.network, "status", return_value=test_mod_versions.STATUS):
            scanned = engine.scan(Connection("192.0.2.1"), Path("unused"))
        self.assertTrue(engine.mod_snapshot(scanned, smoothing.TARGET).analysis["known"])
        self.assertTrue(engine.update_statuses(scanned)[smoothing.TARGET]["available"])
        updated = engine.prepare_all(scanned, None, update=True)
        self.assertIn(b"_UNCURSER_HOME_CENTRE X={x_park} Y={y_park}", updated[smoothing.TARGET])
        self.assertNotIn(b"_UNCURSER_HOME_CENTRE X{x_park} Y{y_park}", updated[smoothing.TARGET])
        latest = mod_versions.load(updated[mod_versions.TARGET])[smoothing.TARGET]
        self.assertEqual(latest["version"], mod_versions.VERSIONS[smoothing.TARGET])
        self.assertEqual(mod_versions.undo_files(smoothing.TARGET,
            {path: updated[path] for path in originals()}, latest), originals())

    def test_guarded_batch_backs_up_both_sources_and_cleans_only_matching_python_cache(self):
        harness = test_mesh.BatchTests()
        harness.setUp()
        try:
            harness.cfg.write_bytes(test_smoothing.SOURCE)
            harness.py.write_bytes(test_smoothing.HOMING_SOURCE)
            cache = Path(str(harness.py) + "c")
            cache.write_bytes(b"old compiled homing")
            unrelated_cache = harness.root / "other.pyc"
            unrelated_cache.write_bytes(b"keep")
            items = harness.request()["files"]
            for item, module, old in ((items[0], smoothing.HOMING, test_smoothing.HOMING_SOURCE),
                                       (items[1], smoothing, test_smoothing.SOURCE)):
                enabled = module.transform(old, True)
                item.update(expected=mods.digest(old), data=base64.b64encode(enabled).decode(),
                    new_hash=mods.digest(enabled), reference=module.REFERENCE_HASH)
                if module is smoothing:
                    item["settings"] = smoothing.settings(enabled)
            native_replace = test_mesh.os.replace
            def replace(source, target):
                for item in items:
                    self.assertTrue(Path(item["backup"]).exists())
                native_replace(source, target)
            harness.run_request({"files": items}, replace)
            self.assertEqual(harness.cfg.with_suffix(".cfn.Original.txt").read_bytes(), test_smoothing.SOURCE)
            self.assertEqual(harness.py.with_suffix(".ptn.Original.txt").read_bytes(), test_smoothing.HOMING_SOURCE)
            self.assertFalse(cache.exists())
            self.assertEqual(unrelated_cache.read_bytes(), b"keep")
            self.assertFalse(list(harness.root.glob("*.uncursed-pending-*.txt")))
        finally:
            harness.tearDown()


if __name__ == "__main__":
    unittest.main()
