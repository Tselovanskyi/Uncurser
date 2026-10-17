"""Apply and Fix issues clean originals without migrating obsolete copies."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mod_versions, mods, screws, storage
from app.remote import Connection
from tests import test_mod_versions, test_stock_apply


def snapshot(active=False):
    current = (test_mod_versions.helper_ready_snapshot(stamped=True) if active
               else test_mod_versions.installed_snapshot())
    current.status = {"native": {"model": "F008", "modelVersion": "1.1.7.0"}}
    if not active:
        current.data = current.original
        current.sha256 = mods.digest(current.data)
        current.analysis = mods.inspect(current.data)
    backup = storage.legacy_path(mods.ORIGINAL)
    manifest = json.dumps({"path": mods.TARGET, "sha256": current.original_hash,
                          "model": "F008", "firmware": "1.1.6.4"}).encode()
    files = [{"path": path, "sha256": mods.digest(data), "attrs": {"mode": 0o444, "uid": 0, "gid": 0}}
             for path, data in ((backup, current.original), (backup + ".json", manifest))]
    current.storage_cleanup = ({"target": mods.TARGET, "backup": backup, "model": "F008",
                                "firmware": "1.1.6.4", "files": files},)
    current.storage_moves = tuple({"source": item["path"], "destination": path,
                                   "source_hash": item["sha256"], "destination_hash": None, "attrs": item["attrs"]}
                                  for item, path in zip(files, (mods.ORIGINAL, mods.MANIFEST)))
    return current


class ObsoleteApplyTests(unittest.TestCase):
    def test_bad_backup_is_excluded_from_fix_planning_and_current_firmware_supplies_the_baseline(self):
        for reason in ("checksum", "metadata"):
            with self.subTest(reason=reason):
                current = test_stock_apply.snapshot()
                current.original = b"damaged backup"
                current.original_hash = mods.digest(current.original)
                current.backup_error = current.storage_error = "Original checksum mismatch or metadata missing."
                current.storage_cleanup = ({"target": mods.TARGET, "backup": storage.legacy_path(mods.ORIGINAL),
                    "model": "F008", "firmware": "1.1.7.0", "reason": reason,
                    "files": [{"path": storage.legacy_path(mods.ORIGINAL), "sha256": current.original_hash}]},)
                self.assertFalse(engine.removal_statuses(current)[mods.TARGET]["available"])
                view = engine.cleanup_snapshot(current)
                self.assertIsNone(view.original)
                self.assertEqual(view.backup_error, "")
                self.assertEqual(engine.prepare_all(view, False)[mods.TARGET], test_mod_versions.SOURCES[mods.TARGET])
                issues = engine.firmware_issues(current)
                self.assertTrue(issues[mods.TARGET]["available"])
                self.assertTrue(issues[storage.ROOT]["available"])
                self.assertTrue(issues[storage.ROOT]["damaged"])
                self.assertEqual(current.original, b"damaged backup")

    def test_fix_removes_obsolete_pairs_without_relocation_and_uses_physical_original_for_undo(self):
        for isolated in (False, True):
            self.check_removal_batch(isolated)

    def check_removal_batch(self, isolated):
        for active in (False, True):
            with self.subTest(active=active, isolated=isolated):
                before = snapshot(active)
                after = copy.deepcopy(before)
                after.data, after.sha256 = before.original, before.original_hash
                after.original = after.original_hash = None
                after.backup_guards = {mods.ORIGINAL: None, mods.MANIFEST: None}
                after.storage_moves = after.storage_cleanup = ()
                if active:
                    records = mod_versions.load(before.versions.data)
                    records.pop(mods.TARGET)
                    after.versions.data = mod_versions.dump(records)
                    after.versions.sha256 = mods.digest(after.versions.data)
                paths = [file["path"] for pair in before.storage_cleanup for file in pair["files"]]
                with patch.object(engine, "scan", side_effect=[copy.deepcopy(before), after]), \
                        patch.object(engine, "Remote") as remote, \
                        patch.object(engine.network, "require_idle"), \
                        patch.object(engine.network, "status", return_value=before.status):
                    command = remote.return_value.__enter__.return_value.python
                    command.return_value = {"retired": paths, "saved": [mods.TARGET] if active else []}
                    result = engine.apply(Connection("192.0.2.1"), Path("unused"), before,
                                          False if active else None, retire_old_backups=isolated)
                request = command.call_args.args[1]
                self.assertEqual(request["moves"], [])
                self.assertEqual(request["retire"], list(before.storage_cleanup))
                if active:
                    item = next(item for item in request["files"] if item["path"] == mods.TARGET)
                    self.assertEqual(item["backup"], before.storage_cleanup[0]["backup"])
                    self.assertNotIn(mods.ORIGINAL, request["guards"])
                    self.assertEqual(request["guards"][item["backup"]], before.original_hash)
                else:
                    self.assertEqual(request["files"], [])
                    self.assertEqual(request["guards"][mods.TARGET], before.sha256)
                    self.assertIsNone(request["guards"][mod_versions.TARGET])
                    self.assertFalse(result["power_cycle"])
                self.assertTrue(result["written"])

    def test_normal_apply_preserves_an_active_owner_and_does_not_relocate_its_old_original(self):
        before = snapshot(True)
        with patch.object(engine, "scan", return_value=copy.deepcopy(before)), \
                patch.object(engine, "Remote") as remote, patch.object(engine.network, "require_idle"):
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        remote.assert_not_called()
        self.assertFalse(result["written"])
        self.assertEqual(result["snapshot"].storage_moves, before.storage_moves)

    def test_unrelated_mod_removal_and_current_storage_migration_preserve_deferred_originals(self):
        before = snapshot(True)
        move = {"source": storage.LEGACY_RECORDS, "destination": mod_versions.TARGET,
                "source_hash": before.versions.sha256, "destination_hash": None,
                "attrs": {"mode": 0o644, "uid": 0, "gid": 0}}
        before.storage_moves += (move,)
        after = copy.deepcopy(before)
        prepared = engine.prepare_all(before, None, temperature_desired=False)
        for path, data in prepared.items():
            value = engine.file_snapshot(after, path)
            value.data, value.sha256 = data, mods.digest(data) if data is not None else None
        after.storage_moves = before.storage_moves[:-1]
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(before), after]), \
                patch.object(engine, "Remote") as remote, patch.object(engine.network, "require_idle"), \
                patch.object(engine.network, "status", return_value=before.status):
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"moved": [move["source"]], "saved": list(prepared)}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, None,
                                  temperature_desired=False)
        request = command.call_args.args[1]
        self.assertEqual(request["retire"], [])
        self.assertEqual(request["moves"], [move])
        self.assertNotIn(mods.TARGET, {item["path"] for item in request["files"]})
        self.assertEqual(result["snapshot"].original, before.original)
        self.assertEqual(mod_versions.load(result["snapshot"].versions.data)[mods.TARGET],
                         mod_versions.load(before.versions.data)[mods.TARGET])

    def test_shared_original_is_retired_only_when_all_its_owners_are_removed(self):
        before = snapshot(True)
        before.storage_cleanup[0]["target"] = mesh.TARGET
        states = {mesh.TARGET: {"present": True}, screws.PYTHON: {"present": True}}
        with patch.object(engine, "removal_statuses", return_value=states):
            for choices, count in (({mesh.TARGET: False}, 0),
                                   ({mesh.TARGET: False, screws.PYTHON: False}, 1),
                                   ({mesh.TARGET: True, screws.PYTHON: False}, 0)):
                with self.subTest(choices=choices):
                    _, retire, moves = engine.storage_plan(before, choices)
                    self.assertEqual(len(retire), count)
                    self.assertEqual(moves, ())
            with self.assertRaisesRegex(ValueError, "Remove the mod"):
                engine.storage_plan(before, {mesh.TARGET: False}, cleanup_all=True)

    def test_review_shows_deletion_for_cleanup_and_no_move_for_deferred_originals(self):
        current = snapshot()
        diff = engine.difference_all(current, None)
        for item in current.storage_cleanup[0]["files"]:
            self.assertIn("Delete " + item["path"], diff)
        self.assertNotIn("Move ", diff)
        self.assertEqual(engine.difference_all(snapshot(True), None), "No changes.")

    def test_changed_owner_cannot_change_the_reviewed_retirement_plan(self):
        before, fresh = snapshot(True), snapshot()
        fresh.storage_cleanup = copy.deepcopy(before.storage_cleanup)
        fresh.storage_moves = copy.deepcopy(before.storage_moves)
        with patch.object(engine, "scan", return_value=fresh), patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(RuntimeError, "Mod ownership changed"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        remote.assert_not_called()

    def test_retained_owner_or_changed_cleanup_plan_blocks_before_write(self):
        for fault in ("owner", "changed"):
            with self.subTest(fault=fault):
                before = snapshot(active=fault == "owner")
                fresh = copy.deepcopy(before)
                if fault == "changed":
                    fresh.storage_cleanup[0]["files"][0]["sha256"] = "0" * 64
                with patch.object(engine, "scan", return_value=fresh), patch.object(engine, "Remote") as remote, \
                        self.assertRaises((ValueError, RuntimeError)):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), before, None, retire_old_backups=True)
                remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
