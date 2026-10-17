"""Installs and updates on newer firmware with all safety checks, entirely offline."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing
from app.remote import APPLY_BATCH, Connection
from tests import test_helper_versions, test_mod_versions, test_smoothing_versions, test_untested_removal


class UntestedInstallTests(unittest.TestCase):
    def setUp(self):
        for path, module in ((mods.TARGET, mods), (mesh.TARGET, mesh), (mesh_temperature.TARGET, mesh_temperature)):
            original = test_mod_versions.SOURCES[path]
            reference = mods.digest(original) if module is mods else module.fingerprint(original)
            self.enterContext(patch.object(module, "REFERENCE_HASH", reference))

    def fresh(self):
        current = test_mod_versions.helper_ready_snapshot()
        for path, original in test_mod_versions.SOURCES.items():
            value = engine.file_snapshot(current, path)
            module = mod_versions.MODULES[path]
            value.data, value.sha256 = original, mods.digest(original)
            value.original, value.original_hash = None, None
            value.analysis = module.inspect(original) if module is mods else module.inspect(original)
        return test_untested_removal.untested(current)

    def helper(self):
        fixture = test_helper_versions.HelperVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before, current, _ = fixture.installed()
        return before, test_untested_removal.untested(current)

    def after(self, current, pending):
        after = copy.deepcopy(current)
        for path, data in pending.items():
            if path == mod_versions.TARGET:
                test_helper_versions.stamp(after, mod_versions.load(data))
            else:
                value = engine.file_snapshot(after, path)
                value.data, value.sha256 = data, mods.digest(data) if data is not None else None
        return after

    def apply_request(self, current, *, desired=None, capabilities=True, **options):
        pending = engine.prepare_all(current, desired, **options)
        after = self.after(current, pending)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            def execute(program, request=None):
                return {"saved": list(pending)} if program == APPLY_BATCH else capabilities
            command.side_effect = execute
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), current, desired, **options)
            calls = command.call_args_list
        script, request = calls[-1].args
        self.assertEqual(script, APPLY_BATCH)
        self.assertTrue(result["written"])
        return request, calls

    def test_first_three_mods_install_with_verified_uninstall_records_and_strict_settings(self):
        current = self.fresh()
        pending = engine.prepare_all(current, True, mesh_desired=True, temperature_desired=True)
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(set(records), set(test_mod_versions.SOURCES))
        for path, original in test_mod_versions.SOURCES.items():
            self.assertEqual(mod_versions.undo(pending[path], records[path]), original)
        request, calls = self.apply_request(current, desired=True, mesh_desired=True, temperature_desired=True)
        self.assertEqual(len(calls), 2)  # Native cancellation support, then the batch.
        items = {item["path"]: item for item in request["files"]}
        self.assertIn(["bed_mesh", "speed", "600"], items[mesh.TARGET]["settings"])
        self.assertIn(["bed_mesh", "probe_count", "9,9"], items[mesh.TARGET]["settings"])
        self.assertTrue(any(mesh_temperature.ENABLED_CONDITION in value for _, _, value in items[mesh_temperature.TARGET]["settings"]))
        self.assertEqual(request["files"][-1]["path"], mod_versions.TARGET)

    def test_verified_installed_mod_update_changes_revision_without_duplicating_code(self):
        current = test_untested_removal.stamped()
        existing = mod_versions.load(current.versions.data)
        for record in existing.values():
            record["firmware"] = "1.1.7.0"
        test_helper_versions.stamp(current, existing)
        with patch.dict(mod_versions.VERSIONS, {mods.TARGET: "0.1.16"}):
            pending = engine.prepare_all(current, None, update=True)
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(records[mods.TARGET]["version"], "0.1.16")
        self.assertEqual(pending[mods.TARGET], current.data)
        for insertion in mods.INSERTIONS:
            self.assertEqual(pending[mods.TARGET].decode().count(insertion.marked), 1)
        self.assertEqual(mod_versions.undo(pending[mods.TARGET], records[mods.TARGET]), current.original)

    def test_helper_removal_and_mesh_install_compose_one_shared_file(self):
        before, current = self.helper()
        pending = engine.prepare_all(current, None, mesh_desired=True, screws_desired=False)
        self.assertEqual(pending[mesh.TARGET], mesh.transform(before.mesh.data, True))
        self.assertIsNone(pending[screws.PYTHON])
        self.assertIsNone(pending[screws.CONFIG])
        self.assertEqual(pending[fluidd_screws.TARGET], before.screws.frontend[fluidd_screws.TARGET].data)
        self.assertEqual(set(mod_versions.load(pending[mod_versions.TARGET])), {mesh.TARGET})
        request, _ = self.apply_request(current, mesh_desired=True, screws_desired=False)
        self.assertEqual(sum(item["path"] == mesh.TARGET for item in request["files"]), 1)
        self.assertEqual(request["files"][-1]["path"], mod_versions.TARGET)

    def test_missing_backups_edited_owned_code_and_partial_helper_still_block(self):
        for fault in ("missing backup", "edited code"):
            current = test_untested_removal.stamped()
            if fault == "missing backup":
                current.original = None
            else:
                insertion = mods.INSERTIONS[0].marked.encode()
                test_untested_removal.set_data(current, mods.TARGET, current.data.replace(insertion, b"# edited owned code\n", 1))
            with self.subTest(fault=fault), self.assertRaises(ValueError):
                engine.prepare_all(current, True, full_risk=True)
        _, current = self.helper()
        test_untested_removal.set_data(current, screws.CONFIG, None)
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, screws_desired=True, full_risk=True)

    def test_first_or_final_firmware_change_and_busy_printer_block_writes(self):
        current = self.fresh()
        for fault in ("firmware", "printing", "error"):
            fresh = copy.deepcopy(current)
            if fault == "firmware":
                fresh.status["native"]["modelVersion"] = "1.1.8.0"
            elif fault == "printing":
                fresh.status["objects"]["print_stats"] = {"state": "printing"}
            else:
                fresh.status["info"]["state"] = "error"
                fresh.recovery = {"available": True}
            with self.subTest(fault=fault), patch.object(engine, "scan", return_value=fresh), \
                    patch.object(engine, "Remote") as remote, self.assertRaises((ValueError, RuntimeError)):
                engine.apply(Connection("192.0.2.1"), Path("unused"), current, True)
            remote.assert_not_called()
        changed = copy.deepcopy(current.status)
        changed["native"]["modelVersion"] = "1.1.8.0"
        with patch.object(engine, "scan", return_value=current), \
                patch.object(engine.network, "status", return_value=changed), patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = True
            with self.assertRaisesRegex(ValueError, "firmware changed"):
                engine.apply(Connection("192.0.2.1"), Path("unused"), current, True)
            self.assertTrue(all(call.args[0] != APPLY_BATCH for call in command.call_args_list))

    def test_early_heating_install_still_requires_history_and_native_cancellation(self):
        for fault in ("history", "cancellation"):
            current = self.fresh()
            if fault == "history":
                current.identity = {}
            with self.subTest(fault=fault), patch.object(engine, "scan", return_value=current), \
                    patch.object(engine, "Remote") as remote:
                command = remote.return_value.__enter__.return_value.python
                command.return_value = False
                with self.assertRaisesRegex(RuntimeError, "history|cancellation"):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), current, True)
                self.assertTrue(all(call.args[0] != APPLY_BATCH for call in command.call_args_list))

    def test_homing_install_writes_python_registration_before_the_macro(self):
        current = test_smoothing_versions.snapshot()
        current.versions = test_untested_removal.stamped().versions
        test_untested_removal.untested(current)
        request, _ = self.apply_request(current, smoothing_desired=True)
        paths = [item["path"] for item in request["files"]]
        self.assertLess(paths.index(smoothing.HOMING.TARGET), paths.index(smoothing.TARGET))
        item = next(item for item in request["files"] if item["path"] == smoothing.TARGET)
        self.assertTrue(any(smoothing.ENABLED_LINE in value for _, _, value in item["settings"]))

    def test_helper_update_undoes_old_ui_then_applies_new_ui_with_exact_pairs(self):
        _, current = self.helper()
        existing = mod_versions.load(current.versions.data)
        for record in existing.values():
            record["firmware"] = "1.1.7.0"
        test_helper_versions.stamp(current, existing)
        with patch.dict(mod_versions.VERSIONS, {screws.PYTHON: "0.1.16"}), \
                patch.object(fluidd_screws, "GRID_RENDER", fluidd_screws.GRID_RENDER + "/* next helper revision */"):
            pending = engine.prepare_all(current, None, update=True)
            request, _ = self.apply_request(current, update=True)
        self.assertIn(b"next helper revision", pending[fluidd_screws.TARGET])
        item = next(item for item in request["files"] if item["path"] == fluidd_screws.TARGET)
        self.assertEqual(item["kind"], "javascript")
        text = current.screws.frontend[fluidd_screws.TARGET].data.decode()
        for before, after in item["replacements"]:
            self.assertEqual(text.count(before), 1)
            text = text.replace(before, after, 1)
        self.assertEqual(text.encode(), pending[fluidd_screws.TARGET])
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(records[screws.PYTHON]["version"], "0.1.16")


if __name__ == "__main__":
    unittest.main()
