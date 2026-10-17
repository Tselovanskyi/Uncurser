"""Firmware maintenance policy and composed removals, entirely offline."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing
from app.remote import APPLY_BATCH, Connection
from tests import test_helper_versions, test_mod_versions, test_smoothing_versions, test_storage_apply


def untested(current):
    current.status = {"native": {"model": "F008", "modelVersion": "1.1.7.0", "deviceState": 0},
                      "info": {"state": "ready"}, "objects": {}}
    return current


def stamped():
    current = test_mod_versions.installed_snapshot()
    records = {path: mod_versions.capture(path, original, engine.file_snapshot(current, path).data)
               for path, original in test_mod_versions.SOURCES.items()}
    return untested(test_helper_versions.stamp(current, records))


def set_data(current, path, data):
    value = engine.file_snapshot(current, path)
    value.data, value.sha256 = data, mods.digest(data) if data is not None else None


class UntestedRemovalTests(unittest.TestCase):
    def test_full_restore_stays_blocked_even_at_full_risk(self):
        current = stamped()
        current.status["native"]["modelVersion"] = "1.1.8.0"
        for options in ({"desired": None, "restore": True},):
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "not tested"):
                engine.prepare_all(current, full_risk=True, **options)

    def test_recorded_removal_preserves_user_edits_without_latest_templates(self):
        current = stamped()
        extra = b"\n# unrelated user edit\n"
        set_data(current, mods.TARGET, current.data + extra)
        current.analysis = {"enabled": None, "known": False, "patchable": False}
        with patch.object(mods, "transform", side_effect=AssertionError("latest implementation used")):
            pending = engine.prepare_all(current, False)
        self.assertEqual(pending[mods.TARGET], test_mod_versions.SOURCES[mods.TARGET] + extra)
        self.assertNotIn(mods.TARGET, mod_versions.load(pending[mod_versions.TARGET]))

    def test_legacy_three_can_be_removed_only_with_their_verified_originals(self):
        current = untested(test_mod_versions.installed_snapshot())
        for path, module in ((mods.TARGET, mods), (mesh.TARGET, mesh), (mesh_temperature.TARGET, mesh_temperature)):
            original = test_mod_versions.SOURCES[path]
            reference = mods.digest(original) if module is mods else module.fingerprint(original)
            self.enterContext(patch.object(module, "REFERENCE_HASH", reference))
        pending = engine.prepare_all(current, False, mesh_desired=False, temperature_desired=False)
        self.assertNotIn(mod_versions.TARGET, pending)
        for path, original in test_mod_versions.SOURCES.items():
            self.assertEqual(pending[path], original)
        current.original = None
        with self.assertRaisesRegex(ValueError, "backup"):
            engine.prepare_all(current, False)

    def test_shared_file_removals_compose_and_allow_missing_added_config(self):
        fixture = test_helper_versions.HelperVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before, current, _ = fixture.installed(speed=True)
        untested(current)
        set_data(current, screws.CONFIG, None)  # Firmware removed one dependency.
        current.screws.analysis = {"enabled": None, "known": False, "patchable": False}
        pending = engine.prepare_all(current, None, mesh_desired=False, screws_desired=False)
        self.assertEqual(pending[mesh.TARGET], before.mesh.data)
        self.assertIsNone(pending[screws.CONFIG])
        self.assertIsNone(pending[screws.PYTHON])
        self.assertEqual(pending[fluidd_screws.TARGET], before.screws.frontend[fluidd_screws.TARGET].data)
        self.assertFalse(mod_versions.load(pending[mod_versions.TARGET]))

    def test_partial_homing_reset_removes_retained_python_only(self):
        current = test_smoothing_versions.snapshot()
        test_smoothing_versions.install_records(current, engine.prepare_all(current, None, smoothing_desired=True))
        untested(current)
        original = test_smoothing_versions.originals()[smoothing.TARGET]
        set_data(current, smoothing.TARGET, original)
        current.smoothing.analysis = {"enabled": None, "known": False, "patchable": False}
        pending = engine.prepare_all(current, None, smoothing_desired=False)
        for path, original in test_smoothing_versions.originals().items():
            self.assertEqual(pending[path], original)
        self.assertNotIn(smoothing.TARGET, mod_versions.load(pending[mod_versions.TARGET]))

    def test_changed_owned_code_blocks_removal_and_keeps_records(self):
        current = stamped()
        record = mod_versions.load(current.versions.data)[mods.TARGET]
        after = record["undo"][0]["after"].encode()
        set_data(current, mods.TARGET, current.data.replace(after, b"# user changed owned code\n", 1))
        records = current.versions.data
        self.assertFalse(engine.removal_statuses(current)[mods.TARGET]["available"])
        with self.assertRaises(ValueError):
            engine.prepare_all(current, False, full_risk=True)
        self.assertEqual(current.versions.data, records)

    def test_removal_apply_does_not_require_early_heating_runtime_capabilities(self):
        current = stamped()
        current.identity = {}  # No native history, cancellation capability query forbidden.
        after = copy.deepcopy(current)
        for path, data in engine.prepare_all(current, False).items():
            set_data(after, path, data)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"saved": [mods.TARGET]}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), current, False)
        self.assertEqual(command.call_count, 1)
        script, request = command.call_args.args
        self.assertEqual(script, APPLY_BATCH)
        self.assertEqual([item["path"] for item in request["files"]], [mods.TARGET, mod_versions.TARGET])
        self.assertTrue(result["power_cycle"])

    def test_newer_firmware_storage_only_apply_remains_available(self):
        fixture = test_storage_apply.StorageApplyTests()
        before, after = untested(fixture.snapshot()), untested(fixture.snapshot(False))
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(before), after]), \
                patch.object(engine.network, "status", return_value=before.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"moved": [before.storage_moves[0]["source"]]}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        self.assertEqual(command.call_args.args[1]["files"], [])
        self.assertFalse(result["power_cycle"])

    def test_migration_blocks_when_active_include_patterns_cannot_be_verified(self):
        current = stamped()
        current.storage_moves = test_storage_apply.StorageApplyTests().snapshot().storage_moves
        current.mesh.analysis["include_error"] = "Missing config include"
        with patch.object(engine, "scan", return_value=current), \
                patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(ValueError, "requires verified configuration includes"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), current, None)
        remote.assert_not_called()

    def test_already_reverted_python_still_clears_its_stale_compiled_cache(self):
        current = stamped()
        set_data(current, mods.TARGET, current.original)
        after = copy.deepcopy(current)
        for path, data in engine.prepare_all(current, False).items():
            set_data(after, path, data)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"cleared": [mods.TARGET + "c"]}
            engine.apply(Connection("192.0.2.1"), Path("unused"), current, False)
        request = command.call_args.args[1]
        self.assertEqual(request["python_caches"], {mods.TARGET: current.sha256})
        self.assertNotIn(mods.TARGET, [item["path"] for item in request["files"]])

    def test_full_restore_rejects_unknown_or_other_firmware_backup_identity(self):
        for identity in (None, ("F008", "1.1.6.3"), ("F009", "1.1.6.4")):
            current = test_mod_versions.installed_snapshot()
            for _, value in engine.file_entries(current):
                if value:
                    value.original_identity = identity
            with self.subTest(identity=identity):
                self.assertFalse(engine.restorable(current))
                with self.assertRaisesRegex(ValueError, "No eligible"):
                    engine.prepare_all(current, None, restore=True)

    def test_helper_cannot_restore_other_firmware_fluidd_or_discard_its_record(self):
        fixture = test_helper_versions.HelperVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, current, _ = fixture.installed()
        frontend = current.screws.frontend[fluidd_screws.TARGET]
        frontend.original_identity = ("F008", "1.1.6.3")
        frontend.analysis["enabled"] = False
        self.assertNotIn(mesh.TARGET, engine.restorable(current))
        with self.assertRaisesRegex(ValueError, "firmware"):
            engine.prepare_screws(current, False, restore=True)
        with self.assertRaisesRegex(ValueError, "No eligible"):
            engine.prepare_all(current, None, restore=True)

    def test_full_restore_cannot_bypass_identity_with_explicit_mod_choices(self):
        current = test_mod_versions.installed_snapshot()
        current.mesh.original_identity = None
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            engine.prepare_all(current, None, mesh_desired=False, restore=True)


if __name__ == "__main__":
    unittest.main()
