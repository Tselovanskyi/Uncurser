"""Pending validation stays local, coalesces edits and rejects stale results."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import mods, storage, ui
from app.remote import Connection


class PendingValidationTests(unittest.TestCase):
    def window(self):
        window = SimpleNamespace(snapshot=SimpleNamespace(storage_cleanup=(), storage_moves=(), recovery=None,
                                                         _ui_checks={"issues": {}}),
                                 snapshot_connection=("192.0.2.1", "root"),
                                 connected=True, closing=False, busy=False, policy="known",
                                 staged=False, mesh_staged=None, temperature_staged=None,
                                 screws_staged=None, smoothing_staged=None, restore_pending=False,
                                 update_active=False, plan_key=None, plan_request=None, plan_result=None,
                                 plan_task=None, plan_timer=Mock(), refresh=Mock(), apply_waiting=None,
                                 apply_button=Mock(), connection=lambda: Connection("192.0.2.1"),
                                 known_hosts="unused", run_task=Mock())
        window.plan_timer.isActive.return_value = False
        window.pending_plan = lambda: ui.Window.pending_plan(window, {mods.TARGET: window.staged}, True)
        window.validate_pending = lambda: ui.Window.validate_pending(window)
        window.apply_clicked = lambda: ui.Window.apply_clicked(window)
        window.resume_apply = lambda: ui.Window.resume_apply(window)
        return window

    def test_rapid_edits_keep_one_worker_and_ignore_its_obsolete_result(self):
        window = self.window()
        self.assertIsNone(window.pending_plan())
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.engine, "prepare_all", return_value={}) as prepare:
            ui.Window.validate_pending(window)
            window.staged = True
            self.assertIsNone(window.pending_plan())
            ui.Window.validate_pending(window)
            self.assertEqual(len(jobs), 1)
            prepare.assert_not_called()
            jobs.pop(0)()
            self.assertIsNone(window.plan_result)
            self.assertIsNone(window.plan_task)
            self.assertIsNone(window.pending_plan())
            with patch.object(ui.engine, "automatic_updates", return_value={mods.TARGET}):
                ui.Window.validate_pending(window)
                jobs.pop(0)()
        self.assertEqual(prepare.call_count, 2)
        self.assertFalse(prepare.call_args_list[0].kwargs["desired"])
        self.assertTrue(prepare.call_args_list[1].kwargs["desired"])
        self.assertEqual(window.pending_plan(), {"mandatory": {mods.TARGET}, "error": "", "storage_changed": False})

    def test_disconnect_or_new_scan_invalidates_completed_checks(self):
        for change in ("disconnect", "scan"):
            with self.subTest(change=change):
                window = self.window()
                window.pending_plan()
                window.plan_result = {"mandatory": set(), "error": ""}
                self.assertIsNotNone(window.pending_plan())
                if change == "disconnect":
                    window.connected = False
                else:
                    window.snapshot = object()
                self.assertIsNone(window.pending_plan())

    def test_failed_validation_stays_failed_until_choices_change(self):
        window = self.window()
        window.pending_plan()
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.engine, "prepare_all", side_effect=ValueError("Owned code changed")):
            ui.Window.validate_pending(window)
            jobs.pop(0)()
        self.assertEqual(window.pending_plan()["error"], "Owned code changed")
        window.staged = None
        self.assertIsNone(window.pending_plan())

    def test_backup_cleanup_is_planned_before_mod_changes_against_its_checked_view(self):
        window = self.window()
        window.staged = True
        window.pending_plan()
        scanned, view = window.snapshot, object()
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.engine, "storage_plan", return_value=(view, ({"target": mods.TARGET},), ())) as storage_plan, \
                patch.object(ui.engine, "automatic_updates", return_value=set()) as automatic, \
                patch.object(ui.engine, "prepare_all", return_value={}) as prepare:
            window.validate_pending()
            storage_plan.assert_not_called()
            jobs.pop(0)()
            storage_plan.assert_called_once_with(scanned, {mods.TARGET: True}, False)
            automatic.assert_called_once_with(view, {mods.TARGET: True})
            self.assertIs(prepare.call_args.args[0], view)
        self.assertEqual(window.plan_result, {"mandatory": set(), "error": "", "storage_changed": True})

    def test_backup_plan_conflict_blocks_pending_validation_before_mod_planning(self):
        window = self.window()
        window.pending_plan()
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.engine, "storage_plan", side_effect=ValueError("Conflicting backup metadata.")), \
                patch.object(ui.engine, "automatic_updates") as automatic, \
                patch.object(ui.engine, "prepare_all") as prepare, patch.object(ui.engine, "apply") as apply:
            window.validate_pending()
            jobs.pop(0)()
            automatic.assert_not_called()
            prepare.assert_not_called()
            apply.assert_not_called()
        self.assertEqual(window.plan_result["error"], "Conflicting backup metadata.")
        window.run_task.assert_not_called()

    def test_deferred_cleanup_reports_no_effective_storage_change(self):
        window = self.window()
        window.staged = None
        window.snapshot.storage_cleanup = ({"target": mods.TARGET},)
        window.pending_plan()
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.engine, "storage_plan", return_value=(window.snapshot, (), ())), \
                patch.object(ui.engine, "prepare_all", return_value={}):
            window.validate_pending()
            jobs.pop(0)()
        self.assertEqual(window.plan_result, {"mandatory": set(), "error": "", "storage_changed": False})

    def test_cancelled_planner_completion_cannot_clear_a_new_worker(self):
        window = self.window()
        window.pending_plan()
        with patch.object(ui.threading, "Thread", return_value=Mock()), \
                patch.object(ui.engine, "prepare_all", return_value={}):
            window.validate_pending()
            old_task = window.plan_task
            ui.Window.clear_pending(window)
            self.assertTrue(old_task.cancelled.is_set())
            window.staged = True
            window.pending_plan()
            window.validate_pending()
            new_task = window.plan_task
            old_task.signals.done.emit({"mandatory": set(), "error": "", "storage_changed": False})
            self.assertIs(window.plan_task, new_task)
            self.assertIsNone(window.plan_result)
            window.refresh.assert_not_called()
            new_task.cancelled.set()

    def test_no_pending_edits_cancel_validation_without_running_printer_code(self):
        window = self.window()
        window.pending_plan()
        with patch.object(ui.engine, "prepare_all") as prepare, patch.object(ui.engine, "apply") as apply:
            self.assertIsNone(ui.Window.pending_plan(window, {}, False))
            ui.Window.validate_pending(window)
        prepare.assert_not_called()
        apply.assert_not_called()
        self.assertIsNone(window.plan_request)

    def test_apply_while_computing_waits_for_latest_choices_before_starting_write(self):
        window = self.window()
        window.pending_plan()
        jobs, continuations = [], []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.QTimer, "singleShot", side_effect=lambda delay, callback: continuations.append(callback)), \
                patch.object(ui.engine, "prepare_all", return_value={}), \
                patch.object(ui.engine, "automatic_updates", return_value=set()), \
                patch.object(ui.engine, "apply") as apply:
            window.apply_clicked()
            window.apply_clicked()
            self.assertEqual(len(jobs), 1)
            window.run_task.assert_not_called()
            window.staged = True
            window.pending_plan()
            jobs.pop(0)()
            self.assertEqual(continuations, [])
            window.validate_pending()
            jobs.pop(0)()
            self.assertEqual(len(continuations), 1)
            continuations.pop(0)()
            window.run_task.assert_called_once()
            function, _ = window.run_task.call_args.args
            function(Mock())
            self.assertTrue(apply.call_args.args[3])
            self.assertIsNone(window.apply_waiting)

    def test_removing_last_job_cancels_waiting_apply_even_if_another_is_added(self):
        window = self.window()
        window.pending_plan()
        jobs = []
        with patch.object(ui.threading, "Thread", side_effect=lambda **kw: jobs.append(kw["target"]) or Mock()), \
                patch.object(ui.QTimer, "singleShot") as queued, \
                patch.object(ui.engine, "prepare_all", return_value={}):
            window.apply_clicked()
            ui.Window.pending_plan(window, {}, False)
            self.assertIsNone(window.apply_waiting)
            window.pending_plan()
            jobs.pop(0)()
            window.resume_apply()
            queued.assert_not_called()
        window.run_task.assert_not_called()

    def test_new_scan_disconnect_or_mode_change_cancels_waiting_apply(self):
        for change in ("scan", "disconnect", "policy", "restore"):
            with self.subTest(change=change):
                window = self.window()
                window.pending_plan()
                window.apply_waiting = (id(window.snapshot), window.snapshot_connection, window.policy, False)
                if change == "scan":
                    window.snapshot = object()
                elif change == "disconnect":
                    window.connected = False
                elif change == "policy":
                    window.policy = "all"
                else:
                    window.restore_pending = True
                window.pending_plan()
                self.assertIsNone(window.apply_waiting)
                window.resume_apply()
                window.run_task.assert_not_called()

    def test_validation_error_cancels_waiting_apply(self):
        window = self.window()
        window.pending_plan()
        window.apply_waiting = (id(window.snapshot), window.snapshot_connection, window.policy, False)
        window.plan_result = {"mandatory": set(), "error": "Owned code changed"}
        window.resume_apply()
        self.assertIsNone(window.apply_waiting)
        window.run_task.assert_not_called()


class ScanStorageNoticeTests(unittest.TestCase):
    def test_scan_never_describes_obsolete_backup_sources_as_relocation(self):
        obsolete = storage.legacy_path(mods.ORIGINAL)
        for retain_current in (False, True):
            with self.subTest(retain_current=retain_current):
                moves = [{"source": obsolete, "destination": mods.ORIGINAL}]
                if retain_current:
                    moves.append({"source": storage.LEGACY_RECORDS, "destination": storage.RECORDS})
                scanned = SimpleNamespace(status={"native": {"model": "F008", "modelVersion": "1.1.7.0"},
                                                  "info": {}}, identity={}, screws=None, recovery=None,
                                          storage_error="", storage_moves=tuple(moves),
                                          storage_cleanup=({"target": mods.TARGET,
                                                            "files": [{"path": obsolete}]},))
                window = SimpleNamespace(active_row=None, update_synced_status=Mock(), files_status=Mock(),
                                         files_errors=Mock(), review_button=Mock(), refresh=Mock())
                with patch.object(ui.engine, "file_entries", return_value=()):
                    ui.Window.receive_scan(window, scanned, Connection("192.0.2.1"), review=False)
                expected = ("Current backups and uninstall records will move to /mnt/UDISK/Uncurser/ on Apply."
                            if retain_current else "")
                window.files_errors.setText.assert_called_once_with(expected)
                window.files_errors.setVisible.assert_called_once_with(retain_current)


if __name__ == "__main__":
    unittest.main()
