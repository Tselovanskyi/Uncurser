"""Scan-bound UI analysis and worker prefill without GUI or printer access."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import engine, mod_versions, mods, ui, ui_checks


def snapshot():
    return engine.Snapshot(b"", mods.digest(b""), {}, None, None, "",
                           {"native": {"model": "F008", "modelVersion": "1.1.7.0"}},
                           "", {}, 0)


class UIChecksTests(unittest.TestCase):
    def test_populate_and_repeated_reads_compute_each_scan_check_only_once(self):
        scanned = snapshot()
        removals, updates, issues = {"removal": {}}, {"update": {}}, {"issue": {}}
        with patch.object(engine, "removal_statuses", return_value=removals) as remove, \
                patch.object(engine, "update_statuses", return_value=updates) as update, \
                patch.object(engine, "firmware_issues", return_value=issues) as firmware, \
                patch.object(engine, "version_details", return_value="v0.1.15") as version, \
                patch.object(engine, "restorable", return_value={mods.TARGET}) as restore:
            self.assertIs(ui_checks.populate(scanned), scanned)
            self.assertIs(ui_checks.populate(scanned), scanned)
            for _ in range(3):
                self.assertIs(ui_checks.check(scanned, "removals"), removals)
                self.assertIs(ui_checks.check(scanned, "updates"), updates)
                self.assertIs(ui_checks.check(scanned, "issues"), issues)
                self.assertEqual(ui_checks.check(scanned, ("version", mods.TARGET)), "v0.1.15")
                self.assertEqual(ui_checks.check(scanned, ("restore", False)), {mods.TARGET})
            remove.assert_called_once_with(scanned)
            update.assert_called_once_with(scanned)
            firmware.assert_called_once_with(scanned, removals=removals)
            self.assertEqual(version.call_count, len(mod_versions.VERSIONS))
            self.assertEqual(restore.call_count, 2)

    def test_equal_but_separate_snapshots_do_not_share_cached_results(self):
        first, second = snapshot(), snapshot()
        old, new = {"old": {}}, {"new": {}}
        with patch.object(engine, "removal_statuses", side_effect=[old, new]) as remove:
            self.assertIs(ui_checks.check(first, "removals"), old)
            self.assertIs(ui_checks.check(second, "removals"), new)
            self.assertIs(ui_checks.check(first, "removals"), old)
            self.assertEqual(remove.call_count, 2)
        self.assertIsNot(first._ui_checks, second._ui_checks)

    def test_real_firmware_check_uses_cached_removals_instead_of_inspecting_twice(self):
        scanned = snapshot()
        removals = {mods.TARGET: {"present": True, "available": True, "files": {}, "reason": ""}}
        with patch.object(engine, "removal_statuses", return_value=removals) as remove, \
                patch.object(engine, "firmware_issues", wraps=engine.firmware_issues) as firmware:
            self.assertIs(ui_checks.check(scanned, "removals"), removals)
            result = ui_checks.check(scanned, "issues")
            self.assertIs(ui_checks.check(scanned, "issues"), result)
            remove.assert_called_once_with(scanned)
            firmware.assert_called_once_with(scanned, removals=removals)
        self.assertEqual(result[mods.TARGET]["current_firmware"], "1.1.7.0")
        self.assertIsNone(result[mods.TARGET]["installed_firmware"])

    def test_none_defaults_do_not_invoke_engine_analysis(self):
        with patch.object(engine, "removal_statuses") as remove, \
                patch.object(engine, "update_statuses") as update, \
                patch.object(engine, "firmware_issues") as firmware, \
                patch.object(engine, "version_details") as version, \
                patch.object(engine, "restorable") as restore:
            self.assertIsNone(ui_checks.populate(None))
            for key in ("removals", "updates", "issues"):
                self.assertEqual(ui_checks.check(None, key), {})
            self.assertEqual(ui_checks.check(None, ("version", mods.TARGET)), "")
            self.assertEqual(ui_checks.check(None, ("restore", False)), set())
            self.assertEqual(ui_checks.check(None, ("restore", True)), set())
            for operation in (remove, update, firmware, version, restore):
                operation.assert_not_called()

    def test_task_prefills_actual_snapshot_and_snapshot_in_result_dictionary(self):
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                scanned = snapshot()
                result = {"snapshot": scanned, "written": True} if wrapped else scanned
                task = ui.Task(lambda progress: result)
                received, errors = [], []
                task.signals.done.connect(received.append)
                task.signals.error.connect(errors.append)
                with patch.object(ui_checks, "populate") as populate:
                    task.run()
                    populate.assert_called_once_with(scanned)
                self.assertEqual(errors, [])
                self.assertEqual(received, [result])

    def test_task_does_not_prefill_non_snapshot_planning_and_other_results(self):
        for result in (None, [], SimpleNamespace(status={}), {"snapshot": SimpleNamespace(status={})},
                       {"mandatory": set(), "error": ""}, {"snapshot": None}):
            with self.subTest(result=result):
                task = ui.Task(lambda progress: result)
                received = []
                task.signals.done.connect(received.append)
                with patch.object(ui_checks, "populate") as populate:
                    task.run()
                    populate.assert_not_called()
                self.assertEqual(received, [result])


if __name__ == "__main__":
    unittest.main()
