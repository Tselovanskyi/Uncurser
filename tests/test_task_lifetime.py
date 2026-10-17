"""Task callbacks retain error context without retaining their own tasks."""
import gc
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

from app import ui
from tests import test_pending_ui


def defer(jobs):
    def thread(*, target, **kwargs):
        jobs.append(target)
        return SimpleNamespace(start=lambda: None)
    return thread


class TaskLifetimeTests(unittest.TestCase):
    def operation_window(self):
        return SimpleNamespace(busy=False, closing=False, reachability_generation=0, apply_waiting=None,
                               scan_button=Mock(), scan_progress=Mock(), refresh=Mock(), active_task=None)

    def test_finished_and_cancelled_planners_release_tasks_after_replacement(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                window = test_pending_ui.PendingValidationTests().window()
                window.pending_plan()
                jobs = []
                with patch.object(ui.threading, "Thread", new=defer(jobs)), \
                        patch.object(ui.engine, "prepare_all", return_value={}), \
                        patch.object(ui.engine, "automatic_updates", return_value=set()):
                    window.validate_pending()
                    previous = weakref.ref(window.plan_task)
                    if cancelled:
                        ui.Window.clear_pending(window)
                    else:
                        jobs.pop(0)()
                    window.staged = True
                    window.pending_plan()
                    window.validate_pending()
                    if cancelled:
                        jobs.pop(0)()
                    gc.collect()
                    self.assertIsNone(previous())
                    self.assertIsNotNone(window.plan_task)
                    ui.Window.clear_pending(window)
                    jobs.pop(0)()

    def test_completed_scan_releases_task_when_next_operation_starts(self):
        window = self.operation_window()
        jobs = []
        with patch.object(ui.threading, "Thread", new=defer(jobs)):
            ui.Window.run_task(window, lambda progress: {"local": True}, Mock(), scan=True)
            previous = weakref.ref(window.active_task)
            jobs.pop(0)()
            ui.Window.run_task(window, lambda progress: {"local": True}, Mock(), scan=True)
            gc.collect()
            self.assertIsNone(previous())
            jobs.pop(0)()
            window.active_task = None

    def test_failure_keeps_last_progress_and_traceback_without_retaining_old_task(self):
        window = self.operation_window()
        jobs = []
        def fail(progress):
            progress("Reading local fixture")
            raise EOFError()
        with patch.object(ui.threading, "Thread", new=defer(jobs)), \
                patch.object(ui, "QMessageBox") as message, patch.object(ui.logging, "exception"):
            ui.Window.run_task(window, fail, Mock(), scan=True)
            previous = weakref.ref(window.active_task)
            jobs.pop(0)()
            message.return_value.setInformativeText.assert_called_once_with("Last step: Reading local fixture")
            self.assertIn("Traceback", message.return_value.setDetailedText.call_args.args[0])
            self.assertIn("EOFError", message.return_value.setDetailedText.call_args.args[0])
            ui.Window.run_task(window, lambda progress: {"local": True}, Mock(), scan=True)
            gc.collect()
            self.assertIsNone(previous())
            jobs.pop(0)()
            window.active_task = None


if __name__ == "__main__":
    unittest.main()
