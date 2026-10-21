"""Restore choices stage guarded work without contacting the printer."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton

from app import engine, mesh, mods, ui, ui_checks, warmup
from tests import test_warmup_apply
from tests.test_issue_gate import snapshot


class RestoreChoiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch.object(ui.settings, "migrate"))
        self.enterContext(patch.object(ui.settings, "load", return_value={"printers": []}))
        self.enterContext(patch.object(engine, "Remote", side_effect=AssertionError("Printer access forbidden")))
        self.window = ui.Window(Path("build"), start_polling=False)
        self.window.snapshot = snapshot()
        self.window.snapshot_connection = ("192.0.2.1", "root")
        self.window.connected = True
        self.window.run_task = Mock()
        self.window.snapshot._ui_checks["removals"][mods.TARGET]["present"] = True
        self.window.snapshot.analysis["enabled"] = True

    def tearDown(self):
        self.window.closing = True
        for timer in (self.window.plan_timer, self.window.save_timer, self.window.probe_timer, self.window.poll_timer):
            timer.stop()
        self.application.removeEventFilter(self.window)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def choose(self, choice, *, accepted=True, effect=None):
        with patch.object(ui, "RestoreChoiceDialog") as dialog:
            dialog.return_value.choice = choice
            dialog.return_value.exec.return_value = (QDialog.DialogCode.Accepted if accepted
                                                     else QDialog.DialogCode.Rejected)
            if effect:
                dialog.return_value.exec.side_effect = effect
            self.window.stage_restore()
            dialog.return_value.deleteLater.assert_called_once()
        self.window.run_task.assert_not_called()
        return dialog

    def test_remove_all_works_without_restore_backups_and_cancels_pending_installs_and_updates(self):
        self.window.snapshot._ui_checks["removals"][mesh.TARGET]["present"] = True
        # A valid record still needs removal if its code was already restored.
        self.window.snapshot.mesh.analysis["enabled"] = False
        self.window.temperature_staged = True
        self.window.apply_waiting = ("deferred",)
        self.window.update_enabled = self.window.update_active = True
        self.window.refresh()
        self.assertTrue(self.window.original_button.isEnabled())
        dialog = self.choose("remove")
        self.assertEqual(dialog.call_args.args[:2], ("", False))
        self.assertIs(self.window.staged, False)
        self.assertIs(self.window.mesh_staged, False)
        self.assertTrue(all(getattr(self.window, field) is None
                            for path, field, _ in ui.MOD_CONTROLS if path not in (mods.TARGET, mesh.TARGET)))
        self.assertFalse(self.window.restore_pending)
        self.assertFalse(self.window.update_enabled)
        self.assertIsNone(self.window.apply_waiting)
        self.assertTrue(self.window.apply_button.isEnabled())

    def test_restore_stages_only_full_restore(self):
        self.window.snapshot._ui_checks[("restore", False)] = {mods.TARGET}
        self.window.mesh_staged = True
        self.choose("restore")
        self.assertTrue(self.window.restore_pending)
        self.assertFalse(self.window.update_enabled)
        self.assertTrue(all(getattr(self.window, field) is None for _, field, _ in ui.MOD_CONTROLS))

    def test_verified_removal_allows_unrelated_edits_but_still_requires_removal_proof(self):
        scanned = test_warmup_apply.WarmupApplyTests().installed("1.1.7.0")
        extra = b"\n# unrelated user note\n"
        scanned.warmup.data += extra
        scanned.warmup.sha256 = mods.digest(scanned.warmup.data)
        scanned.warmup.analysis = warmup.inspect(scanned.warmup.data, scanned.warmup.original)
        self.assertFalse(scanned.warmup.analysis["known"])
        self.assertTrue(ui_checks.check(scanned, "removals")[warmup.TARGET]["available"])
        self.assertFalse(ui_checks.check(scanned, "issues"))
        self.window.snapshot = scanned
        self.window.refresh()
        self.choose("remove")
        self.assertIs(self.window.warmup_staged, False)
        self.assertTrue(self.window.apply_button.isEnabled())
        jobs = []
        self.window.plan_timer.stop()
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()):
            self.window.validate_pending()
            jobs.pop()()
        self.assertEqual(self.window.plan_result["error"], "")
        self.assertTrue(self.window.apply_button.isEnabled())
        pending = engine.prepare_all(scanned, None, warmup_desired=False)
        self.assertEqual(pending[warmup.TARGET], test_warmup_apply.SOURCE + extra)
        ui_checks.check(scanned, "removals")[warmup.TARGET]["available"] = False
        self.window.refresh()
        self.assertFalse(self.window.apply_button.isEnabled())
        self.window.run_task.assert_not_called()

    def test_dismissal_and_changed_scan_do_not_replace_staged_edits(self):
        self.window.mesh_staged = True
        self.window.update_enabled = False
        self.window.apply_waiting = ("deferred",)
        self.choose("remove", accepted=False)
        self.assertTrue(self.window.mesh_staged)
        self.assertIsNone(self.window.apply_waiting)
        def changed():
            self.window.snapshot = snapshot()
            return QDialog.DialogCode.Accepted
        self.choose("remove", effect=changed)
        self.assertTrue(self.window.mesh_staged)
        self.assertIsNone(self.window.staged)

    def test_remove_all_never_silently_skips_unavailable_unknown_or_unresolved_mods(self):
        scanned = self.window.snapshot
        for kind in ("unavailable", "unknown", "issues"):
            with self.subTest(kind=kind):
                scanned._ui_checks["removals"][mods.TARGET]["available"] = kind != "unavailable"
                scanned._ui_checks["issues"] = {mods.TARGET: {}} if kind == "issues" else {}
                if kind == "unknown":
                    scanned._ui_checks["removals"]["unknown-mod"] = {"present": True, "available": True}
                targets, reason = ui.remove_all_targets(scanned)
                self.assertFalse(targets)
                self.assertTrue(reason)
                scanned._ui_checks["removals"].pop("unknown-mod", None)

    def test_dialog_has_exact_message_two_actions_and_disabled_restore(self):
        dialog = ui.RestoreChoiceDialog("", False, self.window)
        message = dialog.findChild(QLabel).text()
        self.assertIn('The safer choice is <span style="color: #00ff00;">&quot;Remove all mods&quot;</span>', message)
        self.assertIn("which uses each mod’s individual uninstall instructions.", message)
        actions = {action.text(): action for action in dialog.findChildren(QPushButton)}
        self.assertEqual(set(actions), {"Remove all mods", "Restore original"})
        self.assertFalse(actions["Restore original"].isEnabled())
        self.assertTrue(actions["Remove all mods"].isDefault())
        actions["Remove all mods"].click()
        self.assertEqual(dialog.choice, "remove")
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
