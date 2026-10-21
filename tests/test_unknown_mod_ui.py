"""Ignore and pending storage gates use verified scanner/planner results."""
import dataclasses
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from app import engine, mod_versions, mods, storage, ui, ui_checks
from tests import test_mod_versions, test_scan


UNKNOWN = "future-chamber-control"


class UnknownModUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def scanned(self, *, damaged_ledger=False):
        source = test_scan.ScanTests()
        source.setUp()
        self.addCleanup(source.doCleanups)
        records = mod_versions.load(source.files[mod_versions.TARGET])
        records.pop(mods.TARGET)
        for record in records.values():
            record["firmware"] = "1.1.6.4"
        records[UNKNOWN] = {"version": "0.2.0", "files": {mods.TARGET: {"undo": [
            {"before": "other_value = 1\n", "after": "other_value = 2\n"}]}}}
        source.files[mods.TARGET] = test_mod_versions.SOURCES[mods.TARGET] + b"other_value = 2\n"
        source.files[mods.ORIGINAL] = b"damaged original backup\n"
        source.files[mod_versions.TARGET] = mod_versions.dump(records)
        if damaged_ledger:
            source.files[mod_versions.MANIFEST] = b"invalid manifest JSON"
        status = {"native": {"model": "F008", "modelVersion": "1.1.6.4", "deviceState": 0},
                  "info": {"state": "ready"}, "objects": {}}
        with patch.object(engine.network, "status", return_value=status), \
                patch.object(engine.stock, "scan", return_value={"available": False, "reason": "fixture"}):
            result = source.scan()
        self.enterContext(patch.object(engine, "Remote", side_effect=AssertionError("Printer access forbidden")))
        return result

    def setUp(self):
        self.enterContext(patch.object(ui.settings, "migrate"))
        self.enterContext(patch.object(ui.settings, "load", return_value={"printers": []}))
        self.window = ui.Window(Path("build"), start_polling=False)
        self.window.snapshot = self.scanned()
        self.window.snapshot_connection = ("192.0.2.1", "root")
        self.window.connected = True
        self.window.connection = lambda: engine_test_connection()
        self.window.run_task = Mock()
        self.window.save_settings = Mock()
        self.window.update_enabled = False
        self.window.refresh()

    def tearDown(self):
        self.window.closing = True
        for timer in (self.window.plan_timer, self.window.save_timer, self.window.probe_timer, self.window.poll_timer):
            timer.stop()
        self.application.removeEventFilter(self.window)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def ignore(self):
        self.window.ignored_mods = {UNKNOWN}
        ui_checks.set_ignored(self.window.snapshot, (UNKNOWN,))

    def run_pending(self, *, remaining_error="", wait_for_apply=True):
        jobs = []
        real_storage_plan = engine.storage_plan
        def checked_storage(*args, **kwargs):
            view, retire, moves = real_storage_plan(*args, **kwargs)
            return (dataclasses.replace(view, storage_error=remaining_error) if remaining_error else view), retire, moves
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.QTimer, "singleShot"), \
                patch.object(engine, "storage_plan", side_effect=checked_storage):
            self.window.refresh()
            self.assertEqual(self.window.apply_button.isEnabled(), wait_for_apply)
            if wait_for_apply:
                self.window.apply_clicked()
                self.assertIsNotNone(self.window.apply_waiting)
            else:
                self.window.plan_timer.stop()
                self.window.validate_pending()
            self.window.run_task.assert_not_called()
            self.assertEqual(len(jobs), 1)
            jobs.pop()()

    def test_accepting_ignore_recomputes_storage_issues_before_queuing_fix(self):
        scanned = self.window.snapshot
        initial = ui_checks.check(scanned, "issues")
        self.assertFalse(initial[storage.ROOT]["available"])
        with patch.object(ui, "FirmwareIssuesDialog") as dialog, patch.object(ui.QTimer, "singleShot") as queued:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Accepted
            dialog.return_value.reinstall_targets.return_value = ()
            dialog.return_value.unknown_removals.return_value = ()
            dialog.return_value.ignored_targets.return_value = (UNKNOWN,)
            self.assertTrue(self.window.review_firmware_issues(scanned, engine_test_connection()))
        queued.assert_not_called()
        self.window.run_task.assert_not_called()
        self.assertEqual(ui_checks.check(scanned, "issues"), {})
        self.assertEqual(scanned.ignored_mods, (UNKNOWN,))

    def test_apply_waits_then_allows_unrelated_edit_after_damaged_backup_is_deferred(self):
        self.ignore()
        self.window.mesh_staged = False
        self.assertTrue(self.window.snapshot.storage_error)
        self.run_pending()
        self.assertEqual(self.window.plan_result["storage_error"], "")
        self.assertEqual(self.window.plan_result["error"], "")
        self.assertTrue(self.window.snapshot.backup_error)
        self.assertTrue(self.window.apply_button.isEnabled())
        self.window.resume_apply()
        self.window.run_task.assert_called_once()

    def test_actual_target_backup_error_still_blocks_after_deferring_aggregate_error(self):
        self.ignore()
        self.window.policy = "all"
        self.window.staged = True
        self.run_pending(wait_for_apply=False)
        self.assertEqual(self.window.plan_result["storage_error"], "")
        self.assertTrue(self.window.plan_result["error"])
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.resume_apply()
        self.window.run_task.assert_not_called()

    def test_remaining_checked_storage_error_cancels_waiting_apply(self):
        self.ignore()
        self.window.mesh_staged = False
        self.run_pending(remaining_error="Unrelated uninstall-record error")
        self.assertEqual(self.window.plan_result["storage_error"], "Unrelated uninstall-record error")
        self.assertFalse(self.window.apply_button.isEnabled())
        self.assertIsNone(self.window.apply_waiting)
        self.window.resume_apply()
        self.window.run_task.assert_not_called()

    def test_unrelated_damaged_ledger_is_not_hidden_by_ignore(self):
        self.window.snapshot = self.scanned(damaged_ledger=True)
        self.ignore()
        self.window.mesh_staged = False
        self.window.refresh()
        self.assertTrue(self.window.snapshot.versions.backup_error)
        self.assertTrue(ui_checks.check(self.window.snapshot, "issues"))
        self.assertEqual(self.window.apply_button.text(), "Fix issues")
        self.assertFalse(self.window.mesh_toggle.isEnabled())
        self.window.run_task.assert_not_called()


def engine_test_connection():
    from app.remote import Connection
    return Connection("192.0.2.1")


if __name__ == "__main__":
    unittest.main()
