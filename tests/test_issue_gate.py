"""Focused, hidden-window checks for the Scan/Fix issues gate; no printer access."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog

from app import engine, mesh_temperature, mod_versions, mods, storage, ui
from app.remote import Connection


def snapshot():
    analysis = {"known": True, "enabled": False, "patchable": True, "label": "Original", "reason": ""}
    def file():
        return engine.MeshSnapshot(b"", mods.digest(b""), dict(analysis))
    scanned = engine.Snapshot(b"", mods.digest(b""), dict(analysis), None, None, "",
        {"native": {"model": "F008", "modelVersion": "1.1.6.4"}, "info": {}}, "", {}, 0,
        mesh=file(), mesh_temperature=file(), smoothing=file(), homing=file(),
        screws=engine.ScrewsSnapshot({}, dict(analysis)))
    scanned._ui_checks = {"removals": {path: {"present": False, "available": True, "reason": ""}
                                     for path in mod_versions.VERSIONS},
        "updates": {}, "issues": {}, ("restore", False): set(), ("restore", True): set(),
        **{("version", path): "" for path in mod_versions.VERSIONS}}
    return scanned


class IssueGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch.object(ui.settings, "migrate"))
        self.enterContext(patch.object(ui.settings, "load", return_value={"printers": []}))
        self.enterContext(patch.object(engine, "Remote", side_effect=AssertionError("Printer contact forbidden")))
        self.window = ui.Window(Path("build"), start_polling=False)
        self.window.snapshot = snapshot()
        self.window.snapshot_connection = ("192.0.2.1", "root")
        self.window.connected = True
        self.window.connection = lambda: Connection("192.0.2.1")
        self.window.run_task = Mock()
        self.window.save_settings = Mock()
        self.window.refresh()

    def tearDown(self):
        self.window.closing = True
        for timer in (self.window.plan_timer, self.window.save_timer, self.window.probe_timer, self.window.poll_timer):
            timer.stop()
        self.application.removeEventFilter(self.window)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def issues(self):
        self.window.snapshot._ui_checks["issues"] = {
            storage.ROOT: {"kind": "cleanup", "present": True, "available": True, "reason": ""}}
        self.window.refresh()

    def test_any_issue_disables_all_mod_and_update_toggles_and_uses_red_fix_action(self):
        self.issues()
        controls = (self.window.toggle, self.window.mesh_toggle, self.window.temperature_toggle,
                    self.window.screws_toggle, self.window.smoothing_toggle, self.window.update_toggle)
        self.assertTrue(all(not control.isEnabled() for control in controls))
        self.assertEqual(self.window.apply_button.text(), "Fix issues")
        self.assertEqual(self.window.apply_button.objectName(), "fixIssuesAction")
        self.assertIn("QPushButton#fixIssuesAction", ui.STYLE)
        self.assertTrue(self.window.apply_button.isEnabled())
        self.assertFalse(self.window.preview_button.isEnabled())

    def test_issue_gate_preserves_pending_edits_and_ignores_direct_toggle_handlers(self):
        self.window.staged = True
        self.window.mesh_staged = False  # Preserve even an edit equal to the scanned state.
        self.window.update_enabled = False
        self.issues()
        self.assertTrue(self.window.staged)
        self.assertIs(self.window.mesh_staged, False)
        self.assertFalse(self.window.update_enabled)
        for path, field, _ in ui.MOD_CONTROLS:
            previous = getattr(self.window, field)
            self.window.stage_mod_toggle(path, field, not previous)
            self.assertIs(getattr(self.window, field), previous)
        self.window.stage_update(True)
        self.assertFalse(self.window.update_enabled)
        self.window.run_task.assert_not_called()

    def test_footer_and_completed_scan_open_the_same_dialog_without_applying(self):
        self.issues()
        scanned = self.window.snapshot
        with patch.object(ui, "FirmwareIssuesDialog") as dialog, patch.object(engine, "apply") as apply:
            dialog.return_value.exec.return_value = QDialog.DialogCode.Rejected
            self.window.apply_button.click()
            self.window.run_task.assert_not_called()
            self.window.scan_clicked()
            done = self.window.run_task.call_args.args[1]
            done(scanned)
            self.assertEqual(dialog.call_count, 2)
            for call in dialog.call_args_list:
                self.assertIs(call.args[0], scanned._ui_checks["issues"])
                self.assertIs(call.args[1], self.window)
            apply.assert_not_called()

    def test_clearing_issues_restores_normal_apply_update_and_toggle_behavior(self):
        self.issues()
        self.window.snapshot._ui_checks["issues"] = {}
        self.window.snapshot._ui_checks["removals"][mesh_temperature.TARGET].update(
            present=False, available=False, recovery=True)
        self.window.refresh()
        self.assertEqual(self.window.apply_button.text(), "Apply")
        self.assertEqual(self.window.apply_button.objectName(), "primary")
        self.assertTrue(all(control.isEnabled() for control in (self.window.toggle, self.window.mesh_toggle,
            self.window.temperature_toggle, self.window.screws_toggle, self.window.smoothing_toggle)))
        self.window.snapshot.analysis["enabled"] = True
        self.window.snapshot._ui_checks["removals"][mods.TARGET]["present"] = True
        self.window.snapshot._ui_checks["updates"] = {mods.TARGET: {"needed": True, "available": True}}
        self.window.refresh()
        self.assertEqual(self.window.apply_button.text(), "Update")
        self.assertTrue(self.window.update_toggle.isEnabled())
        self.window.mesh_staged = True
        self.window.refresh()
        self.assertEqual(self.window.apply_button.text(), "Apply")

    def test_discovered_issues_cancel_waiting_apply_and_ignore_queued_resume(self):
        self.window.staged = True
        self.window.refresh()
        self.window.apply_waiting = (id(self.window.snapshot), self.window.snapshot_connection,
                                     self.window.policy, self.window.restore_pending)
        self.window.plan_result = {"mandatory": set(), "error": "", "storage_changed": False}
        self.issues()
        self.assertIsNone(self.window.apply_waiting)
        self.assertIsNone(self.window.plan_request)
        self.assertTrue(self.window.staged)
        with patch.object(self.window, "review_firmware_issues") as review, patch.object(engine, "apply") as apply:
            self.window.resume_apply()
            # Also cover an already queued continuation seeing the issues
            # before another refresh has invalidated its old waiting scope.
            self.window.apply_waiting = (id(self.window.snapshot), self.window.snapshot_connection,
                                         self.window.policy, self.window.restore_pending)
            self.window.plan_result = {"mandatory": set(), "error": ""}
            self.window.resume_apply()
            self.assertIsNone(self.window.apply_waiting)
            self.window.run_task.assert_not_called()
            review.assert_not_called()
            apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
