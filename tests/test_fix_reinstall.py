"""Issue repair reinstalls only selected compatible mods, entirely offline."""
import copy
import base64
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mod_versions, mods
from app.remote import Connection
from tests import test_reinstall_compatibility, test_untested_install


class FixReinstallTests(unittest.TestCase):
    def fixture(self):
        source = test_untested_install.UntestedInstallTests()
        source.setUp()
        self.addCleanup(source.doCleanups)
        scanned = source.fresh()
        return scanned, {mods.TARGET: {"present": True, "available": True, "reinstall": True},
                         mesh.TARGET: {"present": True, "available": True, "reinstall": False}}

    def test_repair_removes_issues_then_reinstalls_only_the_chosen_compatible_mod(self):
        scanned, issues = self.fixture()
        cleaned, installed = copy.deepcopy(scanned), copy.deepcopy(scanned)
        first = {"snapshot": cleaned, "written": True, "power_cycle": False}
        second = {"snapshot": installed, "written": True, "power_cycle": True}
        with patch.object(engine, "firmware_issues", return_value=issues), \
                patch.object(engine, "apply", side_effect=[first, second]) as apply:
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned,
                                       reinstall_targets=(mods.TARGET,))
        remove, install = apply.call_args_list
        self.assertFalse(remove.kwargs["desired"])
        self.assertFalse(remove.kwargs["mesh_desired"])
        self.assertIs(install.args[2], cleaned)
        self.assertTrue(install.kwargs["desired"])
        self.assertIsNone(install.kwargs["mesh_desired"])
        self.assertEqual(install.kwargs["repair"]["targets"], (mods.TARGET,))
        self.assertIs(result["snapshot"], installed)

    def test_unselected_and_incompatible_reinstalls_are_never_silently_added(self):
        scanned, issues = self.fixture()
        with patch.object(engine, "firmware_issues", return_value=issues), patch.object(engine, "apply") as apply:
            with self.assertRaisesRegex(ValueError, "could not be verified"):
                engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned,
                                  reinstall_targets=(mesh.TARGET,))
            apply.assert_not_called()
            engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned, reinstall_targets=())
            apply.assert_called_once()
            self.assertFalse(apply.call_args.kwargs["desired"])

    def test_missing_required_heating_history_is_rejected_before_cleanup(self):
        classifier = test_reinstall_compatibility.ReinstallCompatibilityTests()
        classifier.setUp()
        self.addCleanup(classifier.doCleanups)
        scanned = test_reinstall_compatibility.snapshot()
        scanned.identity["history"] = False
        with patch.object(engine, "apply") as apply, \
                patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(ValueError, "could not be verified"):
            engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned,
                              reinstall_targets=(mods.TARGET,))
        apply.assert_not_called()
        remote.assert_not_called()

    def test_reinstall_failure_returns_latest_scan_and_reports_completed_cleanup(self):
        scanned, issues = self.fixture()
        cleaned, latest = copy.deepcopy(scanned), copy.deepcopy(scanned)
        first = {"snapshot": cleaned, "written": True, "power_cycle": False}
        with patch.object(engine, "firmware_issues", return_value=issues), \
                patch.object(engine, "apply", side_effect=[first, RuntimeError("fresh file changed")]), \
                patch.object(engine, "scan", return_value=latest):
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned,
                                       reinstall_targets=(mods.TARGET,))
        self.assertIs(result["snapshot"], latest)
        self.assertEqual(result["message"], "Cleanup completed.")
        self.assertIn("fresh file changed", result["repair_error"])
        self.assertTrue(result["written"])
        self.assertTrue(result["write_uncertain"])
        self.assertTrue(result["power_cycle"])

    def test_firmware_change_after_cleanup_blocks_reinstall(self):
        scanned, issues = self.fixture()
        cleaned = copy.deepcopy(scanned)
        cleaned.status["native"]["modelVersion"] = "1.1.8.0"
        with patch.object(engine, "firmware_issues", return_value=issues), \
                patch.object(engine, "apply", return_value={"snapshot": cleaned, "written": True}) as apply, \
                patch.object(engine, "scan", return_value=cleaned):
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), scanned,
                                       reinstall_targets=(mods.TARGET,))
        apply.assert_called_once()
        self.assertIn("firmware changed", result["repair_error"])

    def test_repaired_helper_startup_error_allows_only_guarded_scoped_reinstallation(self):
        scanned, _ = self.fixture()
        pending = engine.prepare_all(scanned, True)
        after = copy.deepcopy(scanned)
        for path, data in pending.items():
            if path == mod_versions.TARGET:
                after.versions = engine.MeshSnapshot(data, mods.digest(data),
                    {"known": True, "enabled": True, "patchable": True})
                continue
            value = engine.file_snapshot(after, path)
            value.data, value.sha256 = data, mods.digest(data) if data is not None else None
        startup_error = {"state": "error", "state_message": "Include file 'screws_tilt_adjust.cfg' does not exist"}
        scanned.status["info"] = startup_error
        # The file tree is repaired; Klipper has not been physically restarted.
        scanned.recovery = {"available": False, "message": "The old include has already been removed."}
        for expected_error, succeeds in ((startup_error, True), (None, False),
                                         ({**startup_error, "state_message": "different error"}, False)):
            with self.subTest(expected_error=expected_error), \
                    patch.object(engine, "scan", side_effect=[copy.deepcopy(scanned), after]), \
                    patch.object(engine.network, "status", return_value=scanned.status), \
                    patch.object(engine, "Remote") as remote:
                command = remote.return_value.__enter__.return_value.python
                command.return_value = {"saved": list(pending)}
                if succeeds:
                    result = engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, True,
                        repair={"targets": (mods.TARGET,), "startup_error": expected_error})
                    self.assertTrue(result["written"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "Klipper must be ready"):
                        engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, True,
                            repair={"targets": (mods.TARGET,), "startup_error": expected_error})
                    remote.assert_not_called()

    def test_reinstall_cannot_be_combined_with_other_actions(self):
        scanned, _ = self.fixture()
        for options in ({"mesh_desired": False}, {"update": True}, {"full_risk": True}):
            with self.subTest(options=options), patch.object(engine, "scan", return_value=scanned), \
                    patch.object(engine, "Remote") as remote, \
                    self.assertRaisesRegex(ValueError, "may only enable its selected mods"):
                engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, True,
                             repair={"targets": (mods.TARGET,)}, **options)
            remote.assert_not_called()

    def test_clean_install_updates_unproven_shared_records_and_preserves_other_mods(self):
        source = test_untested_install.UntestedInstallTests()
        source.setUp()
        self.addCleanup(source.doCleanups)
        cleaned = source.fresh()
        original = cleaned.mesh.data
        cleaned.mesh.original, cleaned.mesh.original_hash = original, mods.digest(original)
        cleaned.mesh.data = mesh.transform(original, True)
        cleaned.mesh.sha256 = mods.digest(cleaned.mesh.data)
        cleaned.mesh.analysis = mesh.inspect(cleaned.mesh.data, original)
        retained = mod_versions.capture(mesh.TARGET, original, cleaned.mesh.data, firmware="1.1.7.0")
        ledger = mod_versions.dump({mesh.TARGET: retained})
        cleaned.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {"known": True, "enabled": True, "patchable": True},
            backup_guards={mod_versions.ORIGINAL: None, mod_versions.MANIFEST: None})
        request, _ = source.apply_request(cleaned, desired=True)
        item = next(item for item in request["files"] if item["path"] == mod_versions.TARGET)
        records = mod_versions.load(base64.b64decode(item["data"]))
        self.assertEqual(records[mesh.TARGET], retained)
        self.assertEqual(records[mods.TARGET]["version"], mod_versions.VERSIONS[mods.TARGET])
        self.assertEqual(mod_versions.undo(engine.prepare_all(cleaned, True)[mods.TARGET], records[mods.TARGET]), cleaned.data)
        self.assertEqual(item["recovery"], "records")
        self.assertEqual(item["record_targets"], [mods.TARGET])
        self.assertIsNone(item["original_hash"])
        self.assertFalse(item["original_absent"])
        for path in (mod_versions.ORIGINAL, mod_versions.MANIFEST):
            self.assertIsNone(request["guards"][path])
