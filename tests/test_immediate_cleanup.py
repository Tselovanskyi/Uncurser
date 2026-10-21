"""Immediate cleanup dispatch: no GUI instance, printer, SSH or network access."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import mesh, mesh_temperature, mods, screws, smoothing, storage, ui
from app.remote import Connection


PENDING_FIELDS = ("staged", "mesh_staged", "temperature_staged", "screws_staged", "smoothing_staged",
                  "restore_pending", "update_enabled", "update_active")


class ImmediateCleanupTests(unittest.TestCase):
    def window(self):
        scanned = SimpleNamespace(status={"native": {"model": "F008", "modelVersion": "1.1.7.0"}},
                                  _ui_checks={"removals": {}}, storage_moves=(), storage_cleanup=(), recovery_cleanup=(), storage_error="")
        connection = Connection("192.0.2.1")
        window = SimpleNamespace(busy=False, connected=True, closing=False, snapshot=scanned,
                                 snapshot_connection=(connection.host, connection.username), known_hosts=Path("unused"),
                                 ignored_mods=set(),
                                 staged=True, mesh_staged=False, temperature_staged=False, screws_staged=True,
                                 smoothing_staged=True, restore_pending=True, update_enabled=True, update_active=True,
                                 receive_scan=Mock(), show_write_success=Mock(), apply_button=Mock(), run_task=Mock())
        window.fix_firmware_issues = lambda *args, **kwargs: ui.Window.fix_firmware_issues(window, *args, **kwargs)
        return window, scanned, connection

    def pending(self, window):
        return {name: getattr(window, name) for name in PENDING_FIELDS}

    def test_storage_findings_use_one_dialog_and_fix_excludes_pending_mod_actions(self):
        for kind in ("records", "relocation", "cleanup"):
            with self.subTest(kind=kind):
                window, scanned, connection = self.window()
                pending = self.pending(window)
                issues = {storage.ROOT: {"kind": kind, "records": True, "present": True,
                                        "available": True, "reason": ""}}
                with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                        patch.object(ui.engine, "prepare_all", return_value={}) as prepare, \
                        patch.object(ui.engine, "apply") as apply, \
                        patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                        patch.object(ui.QTimer, "singleShot", side_effect=lambda delay, callback: callback()):
                    dialog = issue_dialog.return_value
                    dialog.exec.return_value = ui.QDialog.DialogCode.Accepted
                    dialog.reinstall_targets.return_value = ()
                    self.assertTrue(ui.Window.review_firmware_issues(window, scanned, connection))
                    issue_dialog.assert_called_once_with(issues, window)
                    dialog.reinstall_targets.assert_called_once_with()
                    options = {"desired": None, "mesh_desired": None, "temperature_desired": None,
                               "screws_desired": None, "smoothing_desired": None,
                               "restore": False, "update": False, "full_risk": False}
                    prepare.assert_called_once_with(scanned, **options)
                    function, _ = window.run_task.call_args.args
                    progress = Mock()
                    function(progress)
                    apply.assert_called_once_with(connection, window.known_hosts, scanned, progress=progress, **options)
                self.assertEqual(self.pending(window), pending)

    def test_bad_backup_fix_prepares_from_current_firmware_view_and_keeps_raw_scan_for_write_guards(self):
        window, scanned, connection = self.window()
        pending = self.pending(window)
        scanned.storage_cleanup = ({"target": mods.TARGET, "reason": "checksum"},)
        clean = object()
        issues = {mods.TARGET: {"present": True, "available": True},
                  storage.ROOT: {"kind": "cleanup", "available": True, "reason": "", "damaged": True}}
        with patch.object(ui.engine, "cleanup_snapshot", return_value=clean), \
                patch.object(ui.engine, "prepare_all") as prepare, patch.object(ui.engine, "apply") as apply:
            ui.Window.fix_firmware_issues(window, scanned, connection, issues)
            self.assertIs(prepare.call_args.args[0], clean)
            function, _ = window.run_task.call_args.args
            function(Mock())
            self.assertIs(apply.call_args.args[2], scanned)
            self.assertTrue(apply.call_args.kwargs["retire_old_backups"])
            self.assertFalse(apply.call_args.kwargs["desired"])
        self.assertEqual(self.pending(window), pending)

    def test_clicking_fix_runs_only_available_cleanup_without_apply_or_pending_actions(self):
        window, scanned, connection = self.window()
        pending = self.pending(window)
        issues = {mods.TARGET: {"present": True, "available": True},
                  screws.PYTHON: {"present": True, "available": True},
                  mesh.TARGET: {"present": True, "available": False, "reason": "Edited owned setting"},
                  mesh_temperature.TARGET: {"present": False, "available": True}}
        after = object()
        outcome = {"snapshot": after, "written": True, "power_cycle": True, "message": "Saved and verified"}
        progress = Mock()
        window.run_task.side_effect = lambda function, done: done(function(progress))
        with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                patch.object(ui.engine, "prepare_all", return_value={}) as prepare, \
                patch.object(ui.engine, "apply", return_value=outcome) as apply, \
                patch.object(ui.engine, "scan") as scan, patch.object(ui.engine, "Remote") as remote, \
                patch.object(ui.network, "status", side_effect=AssertionError("network access")), \
                patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                patch.object(ui.QTimer, "singleShot") as queued:
            dialog = issue_dialog.return_value
            dialog.exec.return_value = ui.QDialog.DialogCode.Accepted
            dialog.reinstall_targets.return_value = ()
            queued.side_effect = lambda delay, callback: callback()
            self.assertTrue(ui.Window.review_firmware_issues(window, scanned, connection))
            queued.assert_called_once()
            self.assertEqual(queued.call_args.args[0], 0)
            expected = {"desired": False, "mesh_desired": None, "temperature_desired": None,
                        "screws_desired": False, "smoothing_desired": None,
                        "restore": False, "update": False, "full_risk": False}
            prepare.assert_called_once_with(scanned, **expected)
            apply.assert_called_once_with(connection, window.known_hosts, scanned, progress=progress, **expected)
            window.run_task.assert_called_once()
            scan.assert_not_called()
            remote.assert_not_called()
        window.receive_scan.assert_called_once_with(after, connection, review=False)
        window.show_write_success.assert_called_once_with()
        self.assertEqual(self.pending(window), pending)

    def test_compatible_reinstall_uses_isolated_repair_and_other_issues_remain_removals(self):
        window, scanned, connection = self.window()
        pending = self.pending(window)
        issues = {mods.TARGET: {"present": True, "available": True, "reinstall": True},
                  screws.PYTHON: {"present": True, "available": True, "reinstall": False},
                  mesh.TARGET: {"present": True, "available": False, "reinstall": True, "reason": "Edited owned setting"},
                  mesh_temperature.TARGET: {"present": False, "available": True, "reinstall": True}}
        with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                patch.object(ui.engine, "prepare_all", return_value={}) as prepare, \
                patch.object(ui.engine, "fix_issues", create=True) as repair, \
                patch.object(ui.engine, "apply") as apply, \
                patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                patch.object(ui.QTimer, "singleShot", side_effect=lambda delay, callback: callback()):
            dialog = issue_dialog.return_value
            dialog.exec.return_value = ui.QDialog.DialogCode.Accepted
            dialog.reinstall_targets.return_value = (mods.TARGET,)
            self.assertTrue(ui.Window.review_firmware_issues(window, scanned, connection))
            issue_dialog.assert_called_once_with(issues, window)
            self.assertFalse(prepare.call_args.kwargs["desired"])
            self.assertFalse(prepare.call_args.kwargs["screws_desired"])
            self.assertFalse(prepare.call_args.kwargs["update"])
            function, _ = window.run_task.call_args.args
            progress = Mock()
            function(progress)
            repair.assert_called_once_with(connection, window.known_hosts, scanned, progress=progress,
                                           reinstall_targets=(mods.TARGET,))
            apply.assert_not_called()
        self.assertEqual(self.pending(window), pending)

    def test_explicit_reinstall_choices_dispatch_selected_repair_or_removal_only(self):
        for selected in ((), (mods.TARGET,)):
            with self.subTest(selected=selected):
                window, scanned, connection = self.window()
                pending = self.pending(window)
                issues = {mods.TARGET: {"present": True, "available": True, "reinstall": True},
                          screws.PYTHON: {"present": True, "available": True, "reinstall": True}}
                with patch.object(ui.engine, "prepare_all", return_value={}) as prepare, \
                        patch.object(ui.engine, "fix_issues", create=True) as repair, \
                        patch.object(ui.engine, "apply") as apply:
                    ui.Window.fix_firmware_issues(window, scanned, connection, issues,
                                                 reinstall_targets=selected)
                    expected = {"desired": False, "mesh_desired": None, "temperature_desired": None,
                                "screws_desired": False, "smoothing_desired": None,
                                "restore": False, "update": False, "full_risk": False}
                    prepare.assert_called_once_with(scanned, **expected)
                    function, _ = window.run_task.call_args.args
                    progress = Mock()
                    function(progress)
                    if selected:
                        repair.assert_called_once_with(connection, window.known_hosts, scanned,
                                                       progress=progress, reinstall_targets=selected)
                        apply.assert_not_called()
                    else:
                        apply.assert_called_once_with(connection, window.known_hosts, scanned,
                                                      progress=progress, **expected)
                        repair.assert_not_called()
                self.assertEqual(self.pending(window), pending)

    def test_reinstallation_failure_uses_latest_scan_and_warns_without_success_or_losing_staged_edits(self):
        for working_changes, uncertain in ((False, False), (True, False), (False, True)):
            with self.subTest(working_changes=working_changes, uncertain=uncertain):
                window, scanned, connection = self.window()
                pending = self.pending(window)
                issues = {mods.TARGET: {"present": True, "available": True, "reinstall": True}}
                after = object()
                outcome = {"snapshot": after, "written": working_changes, "power_cycle": working_changes or uncertain,
                           "write_uncertain": uncertain,
                           "message": "Cleanup checked.", "repair_error": "Compatible reinstallation failed."}
                window.run_task.side_effect = lambda function, done: done(function(Mock()))
                with patch.object(ui.engine, "prepare_all", return_value={}), \
                        patch.object(ui.engine, "fix_issues", return_value=outcome, create=True), \
                        patch.object(ui.engine, "apply") as apply, patch.object(ui, "QMessageBox") as message:
                    ui.Window.fix_firmware_issues(window, scanned, connection, issues)
                    window.receive_scan.assert_called_once_with(after, connection, review=False)
                    message.warning.assert_called_once()
                    warning = message.warning.call_args.args[2]
                    self.assertIn(outcome["message"], warning)
                    self.assertIn(outcome["repair_error"], warning)
                    self.assertEqual("Physically power cycle" in warning, working_changes or uncertain)
                    message.information.assert_not_called()
                    window.show_write_success.assert_not_called()
                    apply.assert_not_called()
                self.assertEqual(self.pending(window), pending)

    def test_stale_scan_or_different_printer_never_starts_cleanup_or_a_scan(self):
        issues = {mods.TARGET: {"present": True, "available": True}}
        for fault in ("snapshot", "printer"):
            window, scanned, connection = self.window()
            if fault == "snapshot":
                window.snapshot = object()
            else:
                window.snapshot_connection = ("192.0.2.2", connection.username)
            pending = self.pending(window)
            with self.subTest(fault=fault), patch.object(ui.engine, "prepare_all") as prepare, \
                    patch.object(ui.engine, "apply") as apply, patch.object(ui.engine, "scan") as scan, \
                    patch.object(ui.engine, "Remote") as remote, patch.object(ui, "QMessageBox") as message:
                ui.Window.fix_firmware_issues(window, scanned, connection, issues)
                prepare.assert_not_called()
                apply.assert_not_called()
                scan.assert_not_called()
                remote.assert_not_called()
                message.warning.assert_called_once()
            window.run_task.assert_not_called()
            self.assertEqual(self.pending(window), pending)

    def test_conflicting_cleanup_is_reported_without_writing_or_changing_pending_intent(self):
        window, scanned, connection = self.window()
        pending = self.pending(window)
        issues = {mods.TARGET: {"present": True, "available": True}}
        with patch.object(ui.engine, "prepare_all", side_effect=ValueError("Owned code changed")), \
                patch.object(ui.engine, "apply") as apply, patch.object(ui.engine, "scan") as scan, \
                patch.object(ui.engine, "Remote") as remote, patch.object(ui, "QMessageBox") as message:
            ui.Window.fix_firmware_issues(window, scanned, connection, issues)
            message.warning.assert_called_once()
            apply.assert_not_called()
            scan.assert_not_called()
            remote.assert_not_called()
        window.run_task.assert_not_called()
        self.assertEqual(self.pending(window), pending)

    def test_no_available_present_issues_never_start_an_action(self):
        window, scanned, connection = self.window()
        issues = {mods.TARGET: {"present": True, "available": False},
                  smoothing.TARGET: {"present": False, "available": True}}
        with patch.object(ui.engine, "prepare_all") as prepare, patch.object(ui.engine, "apply") as apply, \
                patch.object(ui.engine, "scan") as scan, patch.object(ui.engine, "Remote") as remote:
            ui.Window.fix_firmware_issues(window, scanned, connection, issues)
            prepare.assert_not_called()
            apply.assert_not_called()
            scan.assert_not_called()
            remote.assert_not_called()
        window.run_task.assert_not_called()

    def test_relocation_only_and_combined_fix_keep_pending_choices_out_of_the_transaction(self):
        for remove_mod in (False, True):
            with self.subTest(remove_mod=remove_mod):
                window, scanned, connection = self.window()
                scanned.storage_moves = ({"source": storage.LEGACY_RECORDS,
                                          "destination": storage.RECORDS,
                                          "source_hash": "a" * 64, "destination_hash": None,
                                          "attrs": {"mode": 0o600, "uid": 0, "gid": 0}},)
                pending = self.pending(window)
                issues = {storage.ROOT: {"kind": "relocation", "present": True,
                                        "available": True, "reason": ""}}
                if remove_mod:
                    issues[mods.TARGET] = {"present": True, "available": True}
                after = object()
                outcome = {"snapshot": after, "written": True, "power_cycle": remove_mod,
                           "message": "Saved and verified"}
                progress = Mock()
                window.run_task.side_effect = lambda function, done: done(function(progress))
                with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                        patch.object(ui.engine, "prepare_all", return_value={}) as prepare, \
                        patch.object(ui.engine, "apply", return_value=outcome) as apply, \
                        patch.object(ui, "QMessageBox") as message, \
                        patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                        patch.object(ui.QTimer, "singleShot", side_effect=lambda delay, callback: callback()):
                    dialog = issue_dialog.return_value
                    dialog.exec.return_value = ui.QDialog.DialogCode.Accepted
                    dialog.reinstall_targets.return_value = ()
                    self.assertTrue(ui.Window.review_firmware_issues(window, scanned, connection))
                    issue_dialog.assert_called_once_with(issues, window)
                    expected = {"desired": False if remove_mod else None,
                                "mesh_desired": None, "temperature_desired": None,
                                "screws_desired": None, "smoothing_desired": None,
                                "restore": False, "update": False, "full_risk": False}
                    prepare.assert_called_once_with(scanned, **expected)
                    apply.assert_called_once_with(connection, window.known_hosts, scanned,
                                                  progress=progress, **expected)
                    if remove_mod:
                        window.show_write_success.assert_called_once_with()
                        message.information.assert_not_called()
                    else:
                        window.show_write_success.assert_not_called()
                        message.information.assert_called_once_with(window, "Write successful", outcome["message"])
                window.receive_scan.assert_called_once_with(after, connection, review=False)
                self.assertEqual(self.pending(window), pending)

    def test_blocked_relocation_disables_fix_even_when_a_mod_is_removable(self):
        for remove_mod in (False, True):
            with self.subTest(remove_mod=remove_mod):
                window, scanned, connection = self.window()
                pending = self.pending(window)
                scanned.storage_error = "Conflicting destination <backup>"
                issues = {storage.ROOT: {"kind": "relocation", "present": True,
                                        "available": False, "reason": scanned.storage_error}}
                if remove_mod:
                    issues[mods.TARGET] = {"present": True, "available": True}
                with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                        patch.object(ui.engine, "prepare_all") as prepare, \
                        patch.object(ui.engine, "apply") as apply, patch.object(ui, "QMessageBox") as message, \
                        patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                        patch.object(ui.QTimer, "singleShot") as queued:
                    issue_dialog.return_value.exec.return_value = ui.QDialog.DialogCode.Rejected
                    self.assertTrue(ui.Window.review_firmware_issues(window, scanned, connection))
                    issue_dialog.assert_called_once_with(issues, window)
                    queued.assert_not_called()
                    ui.Window.fix_firmware_issues(window, scanned, connection, issues)
                    message.warning.assert_called_once()
                    prepare.assert_not_called()
                    apply.assert_not_called()
                window.run_task.assert_not_called()
                self.assertEqual(self.pending(window), pending)

    def test_scan_printer_reopens_dismissed_issues_each_time_without_changing_staged_intent(self):
        cases = (("1.1.6.4", {storage.ROOT: {"kind": "relocation", "present": True,
                                           "available": True, "reason": ""}}),
                 ("1.1.7.0", {mods.TARGET: {"present": True, "available": True}}))
        for firmware, issues in cases:
            with self.subTest(firmware=firmware):
                window, scanned, connection = self.window()
                scanned.status["native"]["modelVersion"] = firmware
                scanned.status["info"] = {}
                scanned.identity, scanned.screws, scanned.recovery = {}, None, None
                window.policy, window.active_row = "cancel", None
                window.connection = Mock(return_value=connection)
                window.save_settings = Mock()
                window.update_synced_status, window.refresh = Mock(), Mock()
                window.files_status, window.files_errors, window.review_button = Mock(), Mock(), Mock()
                window.receive_scan = Mock(wraps=lambda *args, **kwargs:
                    ui.Window.receive_scan(window, *args, **kwargs))
                window.review_firmware_issues = Mock(wraps=lambda *args, **kwargs:
                    ui.Window.review_firmware_issues(window, *args, **kwargs))
                pending = self.pending(window)
                progress = Mock()
                window.run_task.side_effect = lambda function, done, **kwargs: done(function(progress))
                with patch.object(ui.engine, "firmware_issues", return_value=issues), \
                        patch.object(ui.engine, "file_entries", return_value=()), \
                        patch.object(ui.engine, "scan", return_value=scanned) as scan, \
                        patch.object(ui.engine, "prepare_all") as prepare, patch.object(ui.engine, "apply") as apply, \
                        patch.object(ui.engine, "Remote") as remote, \
                        patch.object(ui, "FirmwareIssuesDialog") as issue_dialog, \
                        patch.object(ui.QTimer, "singleShot") as queued:
                    issue_dialog.return_value.exec.return_value = ui.QDialog.DialogCode.Rejected
                    for attempt in range(2):
                        ui.Window.scan_clicked(window)
                        self.assertEqual(issue_dialog.call_count, attempt + 1)
                        issue_dialog.assert_called_with(issues, window)
                    self.assertEqual(scan.call_count, 2)
                    scan.assert_called_with(connection, window.known_hosts, progress, ignored_mods=())
                    self.assertEqual(window.receive_scan.call_count, 2)
                    window.receive_scan.assert_called_with(scanned, connection)
                    self.assertEqual(window.review_firmware_issues.call_count, 2)
                    window.review_firmware_issues.assert_called_with(scanned, connection)
                    self.assertTrue(window.run_task.call_args.kwargs["scan"])
                    prepare.assert_not_called()
                    apply.assert_not_called()
                    remote.assert_not_called()
                    queued.assert_not_called()
                    issue_dialog.return_value.reinstall_targets.assert_not_called()
                self.assertEqual(self.pending(window), pending)
                self.assertEqual(window.policy, "cancel")


if __name__ == "__main__":
    unittest.main()
