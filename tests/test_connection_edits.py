"""Connection changes discard local edits; issue dialogs release their widgets."""
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer, Qt
from PySide6.QtWidgets import QApplication

from app import engine, mods, ui
from tests.test_issue_gate import snapshot


class ConnectionEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch.object(ui.settings, "migrate"))
        self.enterContext(patch.object(ui.settings, "load", return_value={"printers": []}))
        self.enterContext(patch.object(engine, "Remote", side_effect=AssertionError("Printer contact forbidden")))
        self.window = ui.Window(Path("build"), start_polling=False)
        self.window.save_settings = Mock()
        self.window.run_task = Mock()
        self.rows = [self.window.add_printer({"host": host}, save=False)
                     for host in ("192.0.2.1", "192.0.2.2")]
        for row in self.rows:
            row.reachable = True
        self.window.active_row = self.rows[0]
        self.window.connected = True
        self.window.snapshot = snapshot()
        self.window.snapshot_connection = ("192.0.2.1", "root")
        self.window.refresh()

    def tearDown(self):
        self.window.closing = True
        for timer in (self.window.plan_timer, self.window.save_timer, self.window.probe_timer, self.window.poll_timer):
            timer.stop()
        self.application.removeEventFilter(self.window)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def stage(self):
        for _, field, _ in ui.MOD_CONTROLS:
            setattr(self.window, field, True)
        self.window.restore_pending = True
        self.window.update_enabled = False
        self.window.update_active = True
        self.window.apply_waiting = ("pending",)
        self.window.plan_key = self.window.plan_request = self.window.plan_result = ("stale",)
        task = Mock(cancelled=threading.Event())
        self.window.plan_task = task
        self.window.plan_timer.start(10000)
        return task

    def assert_cleared(self, task):
        self.assertTrue(all(getattr(self.window, field) is None for _, field, _ in ui.MOD_CONTROLS))
        self.assertFalse(self.window.restore_pending)
        self.assertTrue(self.window.update_enabled)
        self.assertFalse(self.window.update_active)
        self.assertTrue(task.cancelled.is_set())
        self.assertTrue(all(getattr(self.window, field) is None for field in
                            ("apply_waiting", "plan_key", "plan_request", "plan_result", "plan_task")))
        self.assertFalse(self.window.plan_timer.isActive())
        self.assertFalse(hasattr(self.window, "pending_by_row"))

    def test_disconnect_loss_switch_reconnect_and_active_identity_changes_clear_edits(self):
        actions = ("disconnect", "loss", "switch", "reconnect", "identity", "remove")
        for action in actions:
            with self.subTest(action=action):
                task = self.stage()
                if action == "disconnect":
                    self.window.snapshot.analysis["enabled"] = True
                    self.window.snapshot._ui_checks["removals"][mods.TARGET]["present"] = True
                    self.window.snapshot._ui_checks["updates"] = {mods.TARGET: {"needed": True, "available": True}}
                    self.window.disconnect_printer()
                elif action == "loss":
                    self.window.receive_reachability({self.window.active_row.host.text(): False})
                elif action == "switch":
                    self.window.connect_printer(self.rows[1])
                elif action == "reconnect":
                    self.window.connect_printer(self.window.active_row)
                elif action == "identity":
                    self.window.active_row.host.setText("192.0.2.3")
                    self.window.printer_edited(self.window.active_row)
                else:
                    self.window.remove_printer(self.window.active_row)
                self.assert_cleared(task)
                # Restore a connected test context without running a scan.
                self.window.active_row = self.rows[0]
                self.window.connected = True
                self.window.active_row.reachable = True
                self.window.snapshot = snapshot()
                self.window.snapshot_connection = (self.window.active_row.host.text(), "root")

    def test_same_printer_scan_preserves_edits(self):
        scanned = self.window.snapshot
        task = self.stage()
        with patch.object(self.window, "refresh"), patch.object(ui.engine, "scan", return_value=scanned):
            self.window.scan_clicked()
            function, done = self.window.run_task.call_args.args
            done(function(Mock()))
        self.assertTrue(all(getattr(self.window, field) is True for _, field, _ in ui.MOD_CONTROLS))
        self.assertTrue(self.window.restore_pending)
        self.assertFalse(task.cancelled.is_set())

    def test_queued_mod_and_restore_handlers_cannot_stage_after_disconnect_or_during_work(self):
        self.window.snapshot._ui_checks[("restore", False)] = {mods.TARGET}
        self.window.disconnect_printer()
        for connected, busy in ((False, False), (True, True)):
            with self.subTest(connected=connected, busy=busy):
                self.window.connected, self.window.busy = connected, busy
                with patch.object(ui.QMessageBox, "question", return_value=ui.QMessageBox.StandardButton.Yes) as question:
                    for path, field, _ in ui.MOD_CONTROLS:
                        self.window.stage_mod_toggle(path, field, True)
                    self.window.stage_restore()
                    question.assert_not_called()
                self.assertTrue(all(getattr(self.window, field) is None for _, field, _ in ui.MOD_CONTROLS))
                self.assertFalse(self.window.restore_pending)

    def test_dismissed_issue_dialogs_are_deleted_after_each_review(self):
        scanned = self.window.snapshot
        scanned._ui_checks["issues"] = {mods.TARGET: {"present": True, "available": True, "reinstall": True}}
        dialog_type = ui.FirmwareIssuesDialog
        def create(issues, parent):
            dialog = dialog_type(issues, parent)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            return dialog
        with patch.object(ui, "FirmwareIssuesDialog", side_effect=create):
            for _ in range(3):
                QTimer.singleShot(0, lambda: self.window.findChildren(dialog_type)[-1].reject())
                self.assertTrue(self.window.review_firmware_issues(scanned, self.rows[0].connection()))
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.assertEqual(self.window.findChildren(dialog_type), [])


if __name__ == "__main__":
    unittest.main()
