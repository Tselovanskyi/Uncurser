"""Initial connection reads can be abandoned without accepting stale results."""
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from app import engine, ui
from tests.test_issue_gate import snapshot


class ConnectionCancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.enterContext(patch.object(ui.settings, "migrate"))
        self.enterContext(patch.object(ui.settings, "load", return_value={"printers": []}))
        self.enterContext(patch.object(engine, "Remote", side_effect=AssertionError("Printer access forbidden")))
        self.scan = self.enterContext(patch.object(engine, "scan", side_effect=lambda *args, **kwargs: snapshot()))
        self.message_exec = self.enterContext(patch.object(ui.QMessageBox, "exec", return_value=0))
        self.jobs = []
        self.enterContext(patch.object(ui.threading, "Thread", side_effect=self.capture_thread))
        self.window = ui.Window(Path("build"), start_polling=False)
        self.window.save_settings = Mock()
        self.window.persist_settings = Mock()
        self.row = self.window.add_printer({"name": "Test printer", "host": "192.0.2.1"}, save=False)
        self.row.reachable = True
        self.window.refresh()

    def capture_thread(self, **kwargs):
        self.jobs.append(kwargs["target"])
        return Mock()

    def tearDown(self):
        self.window.closing = True
        for timer in (self.window.plan_timer, self.window.save_timer, self.window.probe_timer,
                      self.window.poll_timer, self.window.disconnect_timer):
            timer.stop()
        if self.window.active_task is not None:
            self.window.active_task.cancelled.set()
        if self.window.plan_task is not None:
            self.window.plan_task.cancelled.set()
        self.application.removeEventFilter(self.window)
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def start_connection(self):
        self.row.connect_button.click()
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.scanning)
        self.assertFalse(self.window.connected)
        return self.window.active_task

    def test_timeout_enables_only_disconnect_then_unlocks_fields_after_cancel(self):
        task = self.start_connection()
        self.assertTrue(self.window.disconnect_timer.isSingleShot())
        self.assertEqual(self.window.disconnect_timer.interval(), 7000)
        self.assertTrue(self.window.disconnect_timer.isActive())
        self.assertEqual(self.row.connect_button.text(), "Disconnect")
        self.assertFalse(self.row.connect_button.isEnabled())
        self.window.disconnect_printer(self.row)
        self.assertIs(self.window.active_task, task)
        self.assertFalse(task.cancelled.is_set())

        self.window.disconnect_timer.timeout.emit()
        self.assertTrue(self.window.connect_cancel_ready)
        self.assertTrue(self.row.connect_button.isEnabled())
        self.assertFalse(self.row.host.isEnabled())
        self.assertFalse(self.row.remove_button.isEnabled())
        self.row.connect_button.click()
        self.assertTrue(task.cancelled.is_set())
        self.assertIsNone(self.window.active_task)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.scanning)
        self.assertFalse(self.window.connected)
        self.assertFalse(self.window.connect_cancel_ready)
        self.assertFalse(self.window.disconnect_timer.isActive())
        self.assertTrue(self.row.host.isEnabled())
        self.assertFalse(self.window.scan_button.running)
        self.assertEqual(self.row.connect_button.text(), "Connect")
        self.scan.assert_not_called()

    def test_retry_rejects_old_success_error_and_progress_even_if_already_queued(self):
        old = self.start_connection()
        self.window.enable_scan_disconnect()
        self.row.connect_button.click()
        self.row.host.setText("192.0.2.2")
        self.row.reachable = True
        self.window.refresh()
        current = self.start_connection()
        self.assertIsNot(current, old)
        self.assertFalse(self.window.connect_cancel_ready)
        progress = self.window.scan_button.toolTip()

        # Emitting signals directly also covers messages queued before cancellation.
        old.signals.progress.emit("Obsolete scan progress")
        old.signals.done.emit(snapshot())
        old.signals.error.emit(RuntimeError("Obsolete scan failure"))
        self.assertIs(self.window.active_task, current)
        self.assertTrue(self.window.busy)
        self.assertTrue(self.window.scanning)
        self.assertFalse(self.window.connected)
        self.assertIsNone(self.window.snapshot)
        self.assertEqual(self.window.scan_button.toolTip(), progress)
        self.message_exec.assert_not_called()

        current.run()
        self.assertTrue(self.window.connected)
        self.assertEqual(self.window.snapshot_connection, ("192.0.2.2", "root"))
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.disconnect_timer.isActive())

    def test_success_before_timeout_allows_immediate_disconnect(self):
        task = self.start_connection()
        task.run()
        self.assertTrue(self.window.connected)
        self.assertFalse(self.window.busy)
        self.assertFalse(self.window.scanning)
        self.assertIsNone(self.window.active_task)
        self.assertFalse(self.window.disconnect_timer.isActive())
        self.assertFalse(self.window.connect_cancel_ready)
        self.assertTrue(self.row.connect_button.isEnabled())
        self.row.connect_button.click()
        self.assertFalse(self.window.connected)
        self.assertTrue(self.row.host.isEnabled())

    def test_failure_resets_delay_and_writes_cannot_be_cancelled(self):
        failed = self.start_connection()
        failed.signals.error.emit(RuntimeError("Connection failed"))
        self.assertFalse(self.window.busy)
        self.assertIsNone(self.window.active_task)
        self.assertFalse(self.window.disconnect_timer.isActive())
        self.assertFalse(self.window.connect_cancel_ready)
        self.message_exec.assert_called_once()

        with patch.object(self.window.pool, "start"):
            self.window.run_task(lambda progress: None, Mock(), scan=False)
        writing = self.window.active_task
        self.window.enable_scan_disconnect()
        self.window.disconnect_printer(self.row)
        self.assertTrue(self.window.busy)
        self.assertFalse(self.window.scanning)
        self.assertIs(self.window.active_task, writing)
        self.assertFalse(writing.cancelled.is_set())
        self.assertFalse(self.window.connect_cancel_ready)
        self.assertFalse(self.window.disconnect_timer.isActive())
        self.assertFalse(self.row.connect_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
