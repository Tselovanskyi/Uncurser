"""Unknown installed mods retain verified ownership without app templates."""
import copy
import base64
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app import engine, mesh, mod_versions, mods, removal, stock, storage
from app.remote import APPLY_BATCH, Connection
from tests import test_mod_versions, test_scan, test_stock, test_stock_recovery


UNKNOWN = "/usr/share/klipper/klippy/extras/future_feature.py"
CONFIG = "/mnt/UDISK/printer_data/config/future_chamber.cfg"
MULTI = "future-chamber-control"
ORIGINAL = b"value = 1\n"
INSTALLED = b"# unrelated user edit\nvalue = 2\n"


def unknown_record():
    return {"version": "0.2.0", "firmware": "1.1.6.4",
            "undo": [{"before": "value = 1\n", "after": "value = 2\n"}]}


def value(data, original=None, *, absent=False):
    return engine.MeshSnapshot(data, mods.digest(data) if data is not None else None,
        {"known": False, "enabled": None, "patchable": False}, original=original,
        original_hash=mods.digest(b"" if absent else original) if absent or original is not None else None,
        original_absent=absent, original_identity=("F008", "1.1.6.4"))


def stamp(snapshot, records):
    data = mod_versions.dump(records)
    snapshot.versions = engine.MeshSnapshot(data, mods.digest(data),
        {"known": True, "enabled": True, "patchable": True},
        original_hash=mods.digest(b""), original_absent=True)


def snapshot():
    current = test_mod_versions.helper_ready_snapshot(stamped=True)
    records = engine.installed_records(current)
    records[UNKNOWN] = unknown_record()
    stamp(current, records)
    current.unknown_files = {UNKNOWN: value(INSTALLED, ORIGINAL)}
    return current


def startup_snapshot():
    current = snapshot()
    records = {owner: record for owner, record in engine.installed_records(current).items()
               if owner in mod_versions.VERSIONS}
    for record in records.values():
        record["firmware"] = "1.1.7.0"
    block = "[include future_chamber.cfg]\n"
    current.mesh.data = block.encode() + current.mesh.data
    current.mesh.sha256 = mods.digest(current.mesh.data)
    records[MULTI] = {"version": "0.2.0", "firmware": "1.1.7.0", "files": {
        mesh.TARGET: {"undo": [{"before": "", "after": block}]},
        CONFIG: {"created": "[gcode_macro FUTURE]\ngcode: M400\n"}}}
    stamp(current, records)
    current.unknown_files = {CONFIG: value(None, absent=True)}
    current.stock = test_stock_recovery.stock({CONFIG: None})
    current.status = {"native": {"model": "F008", "modelVersion": "1.1.7.0", "deviceState": 0},
        "info": {"state": "error", "state_message": "Include file '" + CONFIG + "' does not exist",
                 "config_file": mesh.TARGET}, "objects": {}}
    return current


def ignored_backup_snapshot():
    current = snapshot()
    records = engine.installed_records(current)
    records.pop(mods.TARGET)
    records[UNKNOWN] = {"version": "0.2.0", "files": {mods.TARGET: {"undo": [
        {"before": "other_value = 1\n", "after": "other_value = 2\n"}]}}}
    current.data = current.original + b"other_value = 2\n"
    current.sha256 = mods.digest(current.data)
    current.analysis = mods.inspect(current.data)
    current.original_identity = ("F008", "1.1.6.3")
    stamp(current, records)
    current.unknown_files, current.ignored_mods = {}, (UNKNOWN,)
    current.status = {"native": {**current.status["native"], "deviceState": 0},
                      "info": {"state": "ready"}, "objects": {}}
    backup = storage.legacy_path(mods.ORIGINAL)
    attrs = {"mode": 0o444, "uid": 0, "gid": 0}
    manifest = json.dumps({**attrs, "path": mods.TARGET, "sha256": current.original_hash,
                           "model": "F008", "firmware": "1.1.6.3"}).encode()
    files = [{"path": path, "sha256": mods.digest(data), "attrs": attrs}
             for path, data in ((backup, current.original), (backup + ".json", manifest))]
    current.storage_cleanup = ({"target": mods.TARGET, "backup": backup, "reason": "obsolete",
        "model": "F008", "firmware": "1.1.6.3", "files": files},)
    current.storage_moves = tuple({"source": entry["path"], "destination": path,
        "source_hash": entry["sha256"], "destination_hash": None, "attrs": attrs}
        for entry, path in zip(files, (mods.ORIGINAL, mods.MANIFEST)))
    return current


class UnknownModTests(unittest.TestCase):
    def startup_reinstall(self):
        self.enterContext(patch.object(mods, "REFERENCE_HASH", mods.digest(test_mod_versions.SOURCES[mods.TARGET])))
        current = startup_snapshot()
        records = engine.installed_records(current)
        records[mods.TARGET]["firmware"] = "1.1.6.4"
        stamp(current, records)
        current.stock = test_stock_recovery.stock({CONFIG: None, mods.TARGET: current.original})
        current.stock["proof"] = {"verified": True}
        def advance(before, pending):
            after = copy.deepcopy(before)
            for path, data in pending.items():
                saved = engine.file_snapshot(after, path)
                saved.data, saved.sha256 = data, mods.digest(data) if data is not None else None
            after.analysis = mods.inspect(after.data)
            return after
        cleaned = advance(current, engine.prepare_all(current, False, unknown_removals=(MULTI,)))
        installed = advance(cleaned, engine.prepare_all(cleaned, True, repair_targets=(mods.TARGET,)))
        return current, cleaned, installed

    def apply_request(self, current, *, cleared=(), **options):
        pending = engine.prepare_all(current, **options)
        after = copy.deepcopy(current)
        for path, data in pending.items():
            saved = engine.file_snapshot(after, path)
            saved.data, saved.sha256 = data, mods.digest(data) if data is not None else None
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]) as scan, \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "automatic_updates", side_effect=AssertionError("Unrelated update attempted")), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"saved": list(pending), "cleared": list(cleared)}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), current, **options)
        command.assert_called_once()
        program, request = command.call_args.args
        self.assertEqual(program, APPLY_BATCH)
        self.assertTrue(result["written"])
        return result, request, scan.call_args_list

    def test_unknown_single_and_multi_file_records_roundtrip_without_registration(self):
        records = {UNKNOWN: unknown_record(), MULTI: {
            "version": "0.2.1", "files": {
                CONFIG: {"undo": [{"before": "speed: 100", "after": "speed: 600"}]},
                UNKNOWN: {"created": "# future module\n"}}}}
        saved = copy.deepcopy(records)
        self.assertEqual(mod_versions.load(mod_versions.dump(records)), saved)
        self.assertEqual(mod_versions.file_scope(UNKNOWN, records[UNKNOWN]), (UNKNOWN,))
        self.assertEqual(set(mod_versions.file_scope(MULTI, records[MULTI])), {CONFIG, UNKNOWN})
        self.assertNotIn(UNKNOWN, mod_versions.VERSIONS)
        self.assertNotIn(MULTI, mod_versions.VERSIONS)
        self.assertEqual(records, saved)

    def test_scan_reads_unknown_scope_and_original_proofs_without_writing(self):
        source = test_scan.ScanTests()
        source.setUp()
        self.addCleanup(source.doCleanups)
        records = mod_versions.load(source.files[mod_versions.TARGET])
        for record in records.values():
            record["firmware"] = "1.1.6.4"
        records[UNKNOWN] = unknown_record()
        source.files[mod_versions.TARGET] = mod_versions.dump(records)
        backup = storage.original(UNKNOWN)
        source.files[UNKNOWN] = INSTALLED
        source.files[backup] = ORIGINAL
        source.files[backup + ".json"] = json.dumps({"path": UNKNOWN,
            "sha256": mods.digest(ORIGINAL), "mode": 0o644, "uid": 0, "gid": 0,
            "model": "F008", "firmware": "1.1.6.4"}).encode()
        before = dict(source.files)
        with patch.object(engine.network, "status", return_value=test_mod_versions.STATUS), \
                patch.object(engine.stock, "scan", return_value={"available": False}):
            current = source.scan()
        self.assertEqual(current.unknown_files[UNKNOWN].data, INSTALLED)
        self.assertEqual(current.unknown_files[UNKNOWN].original, ORIGINAL)
        self.assertLessEqual(source.readers[0].commands.count("prefetch"), 2)
        self.assertTrue({UNKNOWN, backup, backup + ".json"}.issubset(source.readers[0].paths))
        issue = engine.firmware_issues(current)[UNKNOWN]
        self.assertTrue(issue["unknown"])
        self.assertTrue(issue["available"], issue)
        self.assertEqual(source.files, before)

    def test_saved_uninstall_removes_unknown_mod_and_preserves_other_records(self):
        current = snapshot()
        before = copy.deepcopy(engine.installed_records(current))
        pending = engine.prepare_all(current, None, unknown_removals=(UNKNOWN,))
        self.assertEqual(pending[UNKNOWN], b"# unrelated user edit\nvalue = 1\n")
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET]),
                         {path: record for path, record in before.items() if path != UNKNOWN})
        self.assertEqual(current.unknown_files[UNKNOWN].data, INSTALLED)
        self.assertEqual(engine.installed_records(current), before)

    def test_apply_removes_unknown_with_guards_cache_cleanup_and_postscan_verification(self):
        current = snapshot()
        current.status = {"native": {**current.status["native"], "deviceState": 0},
                          "info": {"state": "ready"}, "objects": {}}
        backup = storage.original(UNKNOWN)
        manifest_hash = mods.digest(b"verified original manifest")
        current.unknown_files[UNKNOWN].backup_guards = {
            backup: mods.digest(ORIGINAL), backup + ".json": manifest_hash}
        records = engine.installed_records(current)
        result, request, scans = self.apply_request(current, desired=None, unknown_removals=(UNKNOWN,))
        items = {item["path"]: item for item in request["files"]}
        self.assertEqual(set(items), {UNKNOWN, mod_versions.TARGET})
        self.assertEqual(base64.b64decode(items[UNKNOWN]["data"]), b"# unrelated user edit\nvalue = 1\n")
        self.assertEqual(items[UNKNOWN]["expected"], mods.digest(INSTALLED))
        self.assertEqual(items[UNKNOWN]["original_hash"], mods.digest(ORIGINAL))
        self.assertEqual(items[UNKNOWN]["kind"], "python")
        self.assertFalse(items[UNKNOWN]["may_capture_original"])
        self.assertEqual(request["python_caches"], {UNKNOWN: mods.digest(INSTALLED)})
        self.assertEqual(request["guards"][backup + ".json"], manifest_hash)
        self.assertTrue(request["verify_config"])
        self.assertEqual(request["files"][-1]["path"], mod_versions.TARGET)
        self.assertEqual(engine.installed_records(result["snapshot"]),
                         {path: record for path, record in records.items() if path != UNKNOWN})
        self.assertIn(UNKNOWN, result["snapshot"].unknown_files)
        self.assertTrue(result["power_cycle"])
        self.assertEqual(len(scans), 2)
        self.assertTrue(all(call.kwargs["unknown_paths"] == (UNKNOWN,) for call in scans))

    def test_session_ignore_hides_issue_but_retains_record_during_unrelated_changes(self):
        current = snapshot()
        before = copy.deepcopy(engine.installed_records(current)[UNKNOWN])
        self.assertTrue(engine.firmware_issues(current)[UNKNOWN]["unknown"])
        current.ignored_mods = (UNKNOWN,)
        self.assertNotIn(UNKNOWN, engine.firmware_issues(current))
        pending = engine.prepare_all(current, False)
        self.assertNotIn(UNKNOWN, pending)
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET])[UNKNOWN], before)
        self.assertEqual(current.unknown_files[UNKNOWN].data, INSTALLED)
        current.ignored_mods = ()
        self.assertIn(UNKNOWN, engine.firmware_issues(current))

    def test_ignored_unknown_damaged_backup_does_not_block_unrelated_apply(self):
        current = snapshot()
        current.status = {"native": {**current.status["native"], "deviceState": 0},
                          "info": {"state": "ready"}, "objects": {}}
        current.ignored_mods = (UNKNOWN,)
        current.unknown_files[UNKNOWN].backup_error = "Original backup checksum mismatch."
        current.storage_error = current.unknown_files[UNKNOWN].backup_error
        record = copy.deepcopy(engine.installed_records(current)[UNKNOWN])
        self.assertNotIn(UNKNOWN, engine.firmware_issues(current))
        result, request, scans = self.apply_request(current, desired=False)
        self.assertEqual({item["path"] for item in request["files"]}, {mods.TARGET, mod_versions.TARGET})
        self.assertEqual(engine.installed_records(result["snapshot"])[UNKNOWN], record)
        self.assertEqual(result["snapshot"].unknown_files[UNKNOWN].data, INSTALLED)
        self.assertTrue(all(call.kwargs["ignored_mods"] == (UNKNOWN,) for call in scans))
        self.assertFalse(any(name.startswith(storage.original(UNKNOWN)) for name in request["guards"]))

    def test_ignored_unknown_retains_shared_original_after_known_owner_removal(self):
        current = snapshot()
        current.data += b"other_value = 2\n"
        current.sha256 = mods.digest(current.data)
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {mods.TARGET: {"undo": [
            {"before": "other_value = 1\n", "after": "other_value = 2\n"}]}}}
        stamp(current, records)
        current.unknown_files, current.ignored_mods = {}, (UNKNOWN,)
        pair = {"target": mods.TARGET, "backup": mods.ORIGINAL, "reason": "obsolete", "files": [
            {"path": mods.ORIGINAL, "sha256": current.original_hash}]}
        current.storage_cleanup = (pair,)
        view, retired, moves = engine.storage_plan(current, {mods.TARGET: False})
        self.assertEqual(retired, ())
        self.assertEqual(moves, ())
        self.assertEqual(view.original, current.original)
        self.assertEqual(engine.storage_plan(current, {mods.TARGET: False, UNKNOWN: False})[1], (pair,))

    def test_ignored_unknown_obsolete_original_is_deferred_without_a_blocking_issue(self):
        current = ignored_backup_snapshot()
        self.assertNotIn(storage.ROOT, engine.firmware_issues(current))
        pending = engine.prepare_all(current, None, mesh_desired=False)
        self.assertIn(mesh.TARGET, pending)
        self.assertNotIn(mods.TARGET, pending)
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET])[UNKNOWN],
                         engine.installed_records(current)[UNKNOWN])
        for cleanup_all in (False, True):
            with self.subTest(cleanup_all=cleanup_all):
                view, retire, moves = engine.storage_plan(current, {mesh.TARGET: False}, cleanup_all=cleanup_all)
                self.assertEqual(retire, ())
                self.assertEqual(moves, ())
                self.assertEqual(view.original, current.original)

    def test_fix_issues_migrates_actionable_ledger_and_keeps_ignored_owners_old_backup(self):
        current = ignored_backup_snapshot()
        move = {"source": storage.LEGACY_RECORDS, "destination": mod_versions.TARGET,
            "source_hash": current.versions.sha256, "destination_hash": None,
            "attrs": {"mode": 0o644, "uid": 0, "gid": 0}}
        current.storage_moves += (move,)
        issues = engine.firmware_issues(current)
        self.assertTrue(issues[storage.ROOT]["available"], issues[storage.ROOT])
        after = copy.deepcopy(current)
        after.storage_moves = current.storage_moves[:-1]
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"saved": [], "moved": [move]}
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current)
        command.assert_called_once()
        request = command.call_args.args[1]
        self.assertEqual(request["moves"], [move])
        self.assertEqual(request["retire"], [])
        self.assertEqual(request["files"], [])
        self.assertEqual(result["snapshot"].original, current.original)
        self.assertEqual(result["snapshot"].storage_cleanup, current.storage_cleanup)
        self.assertEqual(engine.installed_records(result["snapshot"]), engine.installed_records(current))
        self.assertNotIn(storage.ROOT, engine.firmware_issues(result["snapshot"]))

    def test_apply_counts_selected_unknown_removal_before_planning_backup_cleanup(self):
        current = snapshot()
        current.data += b"other_value = 2\n"
        current.sha256 = mods.digest(current.data)
        current.status = {"native": {**current.status["native"], "deviceState": 0},
                          "info": {"state": "ready"}, "objects": {}}
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {mods.TARGET: {"undo": [
            {"before": "other_value = 1\n", "after": "other_value = 2\n"}]}}}
        stamp(current, records)
        current.unknown_files = {}
        pair = {"target": mods.TARGET, "backup": mods.ORIGINAL, "reason": "obsolete", "files": [
            {"path": mods.ORIGINAL, "sha256": current.original_hash},
            {"path": mods.MANIFEST, "sha256": mods.digest(b"original manifest")}]}
        current.storage_cleanup = (pair,)
        pending = engine.prepare_all(current, False, unknown_removals=(UNKNOWN,))
        after = copy.deepcopy(current)
        for path, data in pending.items():
            saved = engine.file_snapshot(after, path)
            saved.data, saved.sha256 = data, mods.digest(data) if data is not None else None
        after.original = after.original_hash = None
        after.backup_guards = {mods.ORIGINAL: None, mods.MANIFEST: None}
        after.storage_cleanup = ()
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"saved": list(pending), "retired": [item["path"] for item in pair["files"]]}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), current, False,
                                  unknown_removals=(UNKNOWN,), retire_old_backups=True)
        self.assertTrue(result["written"])
        self.assertEqual(command.call_args.args[1]["retire"], [pair])
        self.assertNotIn(UNKNOWN, engine.installed_records(result["snapshot"]))

    def test_verified_unknown_removal_repairs_its_own_missing_startup_include(self):
        current = startup_snapshot()
        self.assertTrue(engine.firmware_issues(current)[MULTI]["available"])
        result, request, _ = self.apply_request(current, desired=None, unknown_removals=(MULTI,))
        self.assertTrue(request["verify_config"])
        self.assertNotIn(b"[include future_chamber.cfg]", result["snapshot"].mesh.data)
        self.assertNotIn(MULTI, engine.installed_records(result["snapshot"]))
        self.assertTrue(result["power_cycle"])

    def test_unknown_startup_cleanup_then_known_reinstall_retains_verified_error_context(self):
        current, cleaned, installed = self.startup_reinstall()
        self.assertTrue(engine.firmware_issues(current)[mods.TARGET]["reinstall"])
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), cleaned,
                                                       copy.deepcopy(cleaned), installed]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "apply", wraps=engine.apply) as apply, \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.side_effect = lambda program, request=None: (
                {"saved": [item["path"] for item in request["files"]]} if program == APPLY_BATCH else True)
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current,
                                       reinstall_targets=(mods.TARGET,), unknown_removals=(MULTI,))
        self.assertEqual(apply.call_count, 2)
        self.assertEqual(apply.call_args_list[1].kwargs["repair"]["startup_error"], current.status["info"])
        self.assertIs(result["snapshot"], installed)
        self.assertNotIn("repair_error", result)
        self.assertNotIn(MULTI, engine.installed_records(installed))
        self.assertEqual(engine.installed_records(installed)[mods.TARGET]["firmware"], "1.1.7.0")
        batches = [call for call in command.call_args_list if call.args[0] == APPLY_BATCH]
        self.assertEqual(len(batches), 2)
        self.assertTrue(batches[0].args[1]["verify_config"])

    def test_reinstall_after_unknown_cleanup_rejects_changed_error_unfixed_include_and_activity(self):
        current, cleaned, _ = self.startup_reinstall()
        for fault in ("changed error", "unfixed include", "busy"):
            with self.subTest(fault=fault):
                fresh = copy.deepcopy(cleaned)
                if fault == "changed error":
                    fresh.status["info"]["state_message"] = "Temperature sensor fault"
                elif fault == "unfixed include":
                    fresh.mesh.analysis["include_error"] = engine.INCLUDE_STORAGE_ERROR
                else:
                    fresh.status["native"]["deviceState"] = 1
                with patch.object(engine, "scan", return_value=fresh), \
                        patch.object(engine, "Remote") as remote, self.assertRaises(RuntimeError):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), cleaned, True,
                        repair={"targets": (mods.TARGET,), "startup_error": current.status["info"]})
                remote.assert_not_called()

    def test_owned_missing_include_allows_issue_cleanup_with_legacy_storage_moves(self):
        current = startup_snapshot()
        current.storage_moves = ({"source": storage.LEGACY_RECORDS, "destination": mod_versions.TARGET,
            "source_hash": current.versions.sha256, "destination_hash": None,
            "attrs": {"mode": 0o644, "uid": 0, "gid": 0}},)
        current.storage_error = engine.INCLUDE_STORAGE_ERROR
        current.mesh.analysis["include_error"] = engine.INCLUDE_STORAGE_ERROR
        issues = engine.firmware_issues(current)
        self.assertTrue(issues[MULTI]["available"])
        self.assertTrue(issues[storage.ROOT]["available"], issues[storage.ROOT])
        outcome = {"snapshot": current, "written": True}
        with patch.object(engine, "apply", return_value=outcome) as apply:
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current,
                                       unknown_removals=(MULTI,))
        self.assertIs(result, outcome)
        apply.assert_called_once()
        self.assertEqual(apply.call_args.kwargs["unknown_removals"], (MULTI,))
        self.assertTrue(all(apply.call_args.kwargs[field] is None for field in (
            "desired", "mesh_desired", "temperature_desired", "screws_desired", "smoothing_desired")))
        current.status["info"]["state_message"] = "Temperature sensor fault"
        self.assertFalse(engine.firmware_issues(current)[storage.ROOT]["available"])
        with patch.object(engine, "apply") as apply, self.assertRaisesRegex(ValueError, "backup storage"):
            engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current, unknown_removals=(MULTI,))
        apply.assert_not_called()

    def test_unknown_startup_repair_rejects_unrelated_errors_activity_and_unresolved_include(self):
        for fault in ("unrelated error", "unowned missing path", "busy", "missing absence proof", "retained include"):
            with self.subTest(fault=fault):
                current = startup_snapshot()
                if fault == "unrelated error":
                    current.status["info"]["state_message"] = "Temperature sensor fault"
                elif fault == "unowned missing path":
                    current.status["info"]["state_message"] = "Include file '/mnt/UDISK/printer_data/config/user.cfg' does not exist"
                elif fault == "busy":
                    current.status["native"]["deviceState"] = 1
                elif fault == "missing absence proof":
                    current.unknown_files[CONFIG].original_absent = False
                    current.unknown_files[CONFIG].original_hash = None
                else:
                    current.mesh.data = b"[include ./future_chamber.cfg]\n" + current.mesh.data
                    current.mesh.sha256 = mods.digest(current.mesh.data)
                with patch.object(engine, "scan", return_value=current), \
                        patch.object(engine, "Remote") as remote, self.assertRaises((ValueError, RuntimeError)):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), current, None, unknown_removals=(MULTI,))
                remote.assert_not_called()

    def test_failed_reinstall_rescan_preserves_session_ignored_ids_and_extra_paths(self):
        current = snapshot()
        current.ignored_mods = (UNKNOWN,)
        cleaned, latest = copy.deepcopy(current), copy.deepcopy(current)
        issues = {mods.TARGET: {"present": True, "available": True, "reinstall": True}}
        with patch.object(engine, "firmware_issues", return_value=issues), \
                patch.object(engine, "apply", side_effect=[{"snapshot": cleaned, "written": True},
                                                          RuntimeError("Reinstall failed")]), \
                patch.object(engine, "scan", return_value=latest) as scan:
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current,
                                       reinstall_targets=(mods.TARGET,))
        scan.assert_called_once()
        self.assertEqual(scan.call_args.kwargs["ignored_mods"], (UNKNOWN,))
        self.assertEqual(scan.call_args.kwargs["unknown_paths"], (UNKNOWN,))
        self.assertIs(result["snapshot"], latest)
        self.assertEqual(result["snapshot"].ignored_mods, (UNKNOWN,))
        self.assertIn("Reinstall failed", result["repair_error"])

    def test_already_missing_unknown_created_source_still_cleans_orphan_cache_on_apply(self):
        current = snapshot()
        current.status = {"native": {**current.status["native"], "deviceState": 0},
                          "info": {"state": "ready"}, "objects": {}}
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {UNKNOWN: {"created": "# future module\n"}}}
        stamp(current, records)
        current.unknown_files[UNKNOWN] = value(None, absent=True)
        current.stock = test_stock_recovery.stock({UNKNOWN: None})
        result, request, _ = self.apply_request(current, desired=None, unknown_removals=(UNKNOWN,),
                                                 cleared=(UNKNOWN + "c",))
        self.assertEqual(request["python_caches"], {UNKNOWN: None})
        self.assertEqual({item["path"] for item in request["files"]}, {mod_versions.TARGET})
        self.assertIsNone(request["guards"][UNKNOWN])
        self.assertNotIn(UNKNOWN, engine.installed_records(result["snapshot"]))
        self.assertIsNone(result["snapshot"].unknown_files[UNKNOWN].data)
        self.assertTrue(result["power_cycle"])

    def test_unknown_created_file_uses_current_rom_absence_or_preserves_native_contents(self):
        record = {"version": "0.2.0", "files": {UNKNOWN: {"created": "# future module\n"}}}
        created = value(b"# future module\n", absent=True)
        for native in (None, created.data, b"# replaced by native firmware\n"):
            with self.subTest(native=native):
                plan = removal.inspect({UNKNOWN: created}, {MULTI: record},
                                       stock=test_stock_recovery.stock({UNKNOWN: native}))[MULTI]
                self.assertEqual(plan["available"], native in (None, created.data), plan)
                self.assertEqual(plan["files"], {UNKNOWN: native} if plan["available"] else {})
        for proof in (None, {"available": True, "files": {}},
                      test_stock_recovery.stock({"/usr/share/klipper/klippy/extras/other.py": None})):
            with self.subTest(proof=proof):
                self.assertFalse(removal.inspect({UNKNOWN: created}, {MULTI: record}, stock=proof)[MULTI]["available"])

    def test_unknown_created_file_apply_guards_current_rom_absence_before_deletion(self):
        current = snapshot()
        current.status = {"native": {**current.status["native"], "deviceState": 0},
                          "info": {"state": "ready"}, "objects": {}}
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {UNKNOWN: {"created": "# future module\n"}}}
        stamp(current, records)
        current.unknown_files[UNKNOWN] = value(b"# future module\n", absent=True)
        current.stock = test_stock_recovery.stock({UNKNOWN: None})
        result, request, _ = self.apply_request(current, desired=None, unknown_removals=(UNKNOWN,))
        self.assertIn("/rom" + UNKNOWN, request["guards"])
        self.assertIsNone(request["guards"]["/rom" + UNKNOWN])
        self.assertIsNone(result["snapshot"].unknown_files[UNKNOWN].data)

    def test_unknown_created_config_rom_source_uses_selected_hardware_variant(self):
        raw = test_stock.response("1")
        folder = "/rom/usr/share/klipper/config/"
        first = folder + "F008_CR0CN240319C13/future_chamber.cfg"
        selected = folder + "F008_CR0CN240319C13_1/future_chamber.cfg"
        raw["files"][first] = test_stock.encoded(b"first variant native file\n")
        raw["files"][selected] = None
        remote = test_stock.Remote(raw)
        proof = stock.scan(remote, test_stock.STATUS, extra_paths=(CONFIG,))
        self.assertTrue(proof["available"], proof["reason"])
        entry = proof["files"][CONFIG]
        self.assertEqual(entry["source"], selected)
        self.assertTrue(entry["available"])
        self.assertTrue(entry["absent"])
        self.assertIsNone(proof["guards"][selected])
        self.assertIn("future_chamber.cfg", remote.calls[0][1]["configs"])

    def test_compressed_unknown_interface_blocks_removal_but_can_be_ignored(self):
        path = "/usr/share/fluidd/assets/future_chamber.js"
        original, installed = b"const mode = 1;\n", b"const mode = 2;\n"
        backup = storage.original(path)
        documents = {path: installed, path + ".gz": b"compressed copy", backup: original,
            backup + ".json": json.dumps({"path": path, "sha256": mods.digest(original),
                "mode": 0o644, "uid": 0, "gid": 0, "model": "F008", "firmware": "1.1.6.4"}).encode()}
        reader = Mock()
        reader.read.side_effect = lambda filename: test_scan.record(documents.get(filename))
        scanned = engine.scan_recorded_file(reader, path)
        self.assertEqual(scanned.guards, {path + ".gz": mods.digest(documents[path + ".gz"]), path + ".br": None})
        current = snapshot()
        records = engine.installed_records(current)
        records.pop(UNKNOWN)
        records[path] = {"version": "0.2.0", "undo": [{"before": original.decode(), "after": installed.decode()}]}
        stamp(current, records)
        current.unknown_files = {path: scanned}
        self.assertFalse(engine.firmware_issues(current)[path]["available"])
        with self.assertRaisesRegex(ValueError, "compressed"):
            engine.prepare_all(current, None, unknown_removals=(path,))
        current.ignored_mods = (path,)
        self.assertNotIn(path, engine.firmware_issues(current))
        pending = engine.prepare_all(current, False)
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET])[path], records[path])

    def test_removal_blocks_overlapping_ownership_with_a_retained_known_mod(self):
        current = snapshot()
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {
            mesh.TARGET: {"undo": copy.deepcopy(records[mesh.TARGET]["undo"])}}}
        stamp(current, records)
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, unknown_removals=(UNKNOWN,))
        current.ignored_mods = (UNKNOWN,)
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, mesh_desired=False)
        self.assertEqual(engine.installed_records(current), records)

    def test_full_restore_cannot_drop_or_overwrite_unknown_mod_records(self):
        current = snapshot()
        before = copy.deepcopy(engine.installed_records(current))
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, restore=True)
        self.assertEqual(engine.installed_records(current), before)
        self.assertEqual(current.unknown_files[UNKNOWN].data, INSTALLED)

    def test_unknown_created_file_requires_exact_contents_and_original_absence(self):
        record = {"version": "0.2.0", "files": {UNKNOWN: {"created": "# future module\n"}}}
        for fault in (None, "edited", "missing proof", "damaged proof"):
            with self.subTest(fault=fault):
                created = value(b"# future module\n", absent=True)
                if fault == "edited":
                    created.data = b"# user module\n"
                    created.sha256 = mods.digest(created.data)
                elif fault == "missing proof":
                    created.original_absent, created.original_hash = False, None
                elif fault == "damaged proof":
                    created.backup_error = "Absence manifest checksum mismatch."
                plan = removal.inspect({UNKNOWN: created}, {MULTI: record},
                                       stock=test_stock_recovery.stock({UNKNOWN: None}))[MULTI]
                self.assertEqual(plan["available"], fault is None, plan)
                self.assertEqual(plan["files"], {UNKNOWN: None} if fault is None else {})

    def test_unsupported_unknown_records_can_be_ignored_without_losing_their_contents(self):
        opaque = [
            {"version": "0.2.0", "files": {"/etc/future.conf": {"undo": [{"before": "off", "after": "on"}]}}},
            {"version": "0.2.0", "files": {CONFIG: {"future_patch": {"setting": "value"}}}},
            {"version": "future-format", "files": {CONFIG: {"undo": [{"before": "off", "after": "on"}]}}},
        ]
        for record in opaque:
            with self.subTest(record=record):
                current = snapshot()
                records = engine.installed_records(current)
                records.pop(UNKNOWN)
                records[MULTI] = copy.deepcopy(record)
                stamp(current, records)
                current.unknown_files = {}
                issue = engine.firmware_issues(current)[MULTI]
                self.assertTrue(issue["unknown"])
                self.assertFalse(issue["available"])
                with self.assertRaises(ValueError):
                    mod_versions.file_scope(MULTI, record)
                with self.assertRaises(ValueError):
                    engine.prepare_all(current, None, unknown_removals=(MULTI,))
                current.ignored_mods = (MULTI,)
                self.assertNotIn(MULTI, engine.firmware_issues(current))
                pending = engine.prepare_all(current, False)
                self.assertEqual(mod_versions.load(pending[mod_versions.TARGET])[MULTI], record)

    def test_ignored_unsupported_entry_blocks_changes_to_its_shared_file(self):
        current = snapshot()
        records = engine.installed_records(current)
        records[UNKNOWN] = {"version": "0.2.0", "files": {mods.TARGET: {"future_patch": "opaque"}}}
        stamp(current, records)
        current.unknown_files, current.ignored_mods = {}, (UNKNOWN,)
        with self.assertRaises(ValueError):
            engine.prepare_all(current, False)
        self.assertEqual(engine.installed_records(current), records)

    def test_unsupported_uninstall_paths_are_retained_but_never_validated_for_removal(self):
        for path in ("/etc/shadow", "/mnt/UDISK/printer_data/config/../outside.py",
                     mod_versions.TARGET, "/usr/share/klipper/klippy/extras/future.py.Original.txt",
                     "/usr/share/klipper/klippy/extras/__pycache__/future.py"):
            record = {"version": "0.2.0", "files": {path: {"created": "# unsupported path\n"}}}
            with self.subTest(path=path):
                self.assertEqual(mod_versions.load(mod_versions.dump({MULTI: record})), {MULTI: record})
                with self.assertRaises(ValueError):
                    mod_versions.file_scope(MULTI, record)
        invalid = unknown_record()
        invalid["undo"][0]["execute"] = "arbitrary code must never be accepted"
        self.assertEqual(mod_versions.load(mod_versions.dump({UNKNOWN: invalid})), {UNKNOWN: invalid})
        with self.assertRaises(ValueError):
            mod_versions.file_scope(UNKNOWN, invalid)

    def test_invalid_json_duplicate_records_and_unsafe_owner_ids_remain_blocked(self):
        for data in (b"{", b'{"schema":1,"mods":{"future":{},"future":{}}}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                mod_versions.load(data)
        for owner in ("", "bad\x00owner", storage.ROOT, mod_versions.TARGET, storage.LEGACY_RECORDS):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                mod_versions.dump({owner: {"version": "future-format"}})


if __name__ == "__main__":
    unittest.main()
