"""A repair reinstall validates its own files without enrolling unrelated mods."""
import base64
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mod_versions, mods, screws, storage
from app.remote import APPLY_BATCH, Connection
from tests import test_helper_versions, test_untested_install, test_untested_removal


class ReinstallScopeTests(unittest.TestCase):
    def setUp(self):
        self.source = test_untested_install.UntestedInstallTests()
        self.source.setUp()
        self.addCleanup(self.source.doCleanups)
        self.connection = Connection("192.0.2.1")
        self.repair = {"targets": (mods.TARGET,)}

    def current(self):
        current = self.source.fresh()
        original = current.mesh.data
        current.mesh.original, current.mesh.original_hash = original, mods.digest(original)
        current.mesh.data = mesh.transform(original, True)
        current.mesh.sha256 = mods.digest(current.mesh.data)
        current.mesh.analysis = mesh.inspect(current.mesh.data, original)
        record = mod_versions.capture(mesh.TARGET, original, current.mesh.data, firmware="1.1.6.4")
        test_helper_versions.stamp(current, {mesh.TARGET: record})
        return current

    def test_reinstall_ignores_unrelated_unavailable_older_or_damaged_mods_and_retains_their_records(self):
        for fault in ("unavailable config", "older record", "damaged original", "deferred backup migration"):
            with self.subTest(fault=fault):
                current = self.current()
                if fault == "unavailable config":
                    current.mesh.analysis.update(known=False, enabled=None, patchable=False,
                        include_error="Unrecognized generated configuration block.",
                        reason="Unrecognized generated configuration block.")
                elif fault == "older record":
                    records = engine.installed_records(current)
                    records[mesh.TARGET]["version"] = "0.1.4"
                    test_helper_versions.stamp(current, records)
                else:
                    current.mesh.backup_error = "Original backup checksum mismatch."
                    if fault == "deferred backup migration":
                        current.storage_error = current.mesh.backup_error
                        backup = storage.legacy_path(mesh.ORIGINAL)
                        pair = {"target": mesh.TARGET, "backup": backup, "reason": "checksum", "files": [
                            {"path": path, "sha256": mods.digest(data), "attrs": {"mode": 0o444, "uid": 0, "gid": 0}}
                            for path, data in ((backup, b"damaged original"), (backup + ".json", b"damaged metadata"))]}
                        current.storage_cleanup = (pair,)
                        current.storage_moves = tuple({"source": entry["path"], "destination": path,
                            "source_hash": entry["sha256"], "destination_hash": None, "attrs": entry["attrs"]}
                            for entry, path in zip(pair["files"], (mesh.ORIGINAL, mesh.MANIFEST)))
                retained = copy.deepcopy(engine.installed_records(current)[mesh.TARGET])
                pending = engine.prepare_all(current, True, repair_targets=(mods.TARGET,))
                after = self.source.after(current, pending)
                with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                        patch.object(engine.network, "status", return_value=current.status), \
                        patch.object(engine, "Remote") as remote:
                    command = remote.return_value.__enter__.return_value.python
                    command.side_effect = lambda program, payload=None: (
                        {"saved": list(pending)} if program == APPLY_BATCH else True)
                    result = engine.apply(self.connection, Path("unused"), current, True, repair=self.repair)
                    batches = [call.args[1] for call in command.call_args_list if call.args[0] == APPLY_BATCH]
                self.assertTrue(result["written"])
                self.assertEqual(len(batches), 1)
                items = {item["path"]: item for item in batches[0]["files"]}
                self.assertEqual(set(items), {mods.TARGET, mod_versions.TARGET})
                records = mod_versions.load(base64.b64decode(items[mod_versions.TARGET]["data"]))
                self.assertEqual(records[mesh.TARGET], retained)
                self.assertEqual(records[mods.TARGET]["firmware"], "1.1.7.0")
                self.assertEqual(after.mesh.data, current.mesh.data)

    def test_target_invalid_python_and_missing_history_still_block_before_write(self):
        for fault in ("python", "history"):
            with self.subTest(fault=fault):
                current = self.current()
                if fault == "python":
                    current.data = b"def broken(\n"
                    current.sha256 = mods.digest(current.data)
                    current.analysis = mods.inspect(current.data)
                else:
                    current.identity["history"] = False
                with patch.object(engine, "scan", return_value=copy.deepcopy(current)), \
                        patch.object(engine, "Remote") as remote, \
                        patch.object(engine.network, "status", side_effect=AssertionError("Network access forbidden")), \
                        self.assertRaises((ValueError, SyntaxError, RuntimeError)):
                    engine.apply(self.connection, Path("unused"), current, True, repair=self.repair)
                remote.assert_not_called()

    def test_mesh_reinstall_preserves_helper_root_ownership_without_validating_unrelated_helper_files(self):
        fixture = test_helper_versions.HelperVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for fault in ("damaged UI", "missing Python", "tampered include record"):
            with self.subTest(fault=fault):
                _, current, _ = fixture.installed()
                test_untested_removal.untested(current)
                if fault == "damaged UI":
                    value = current.screws.frontend[fluidd_screws.TARGET]
                    value.data = b"// changed unrelated helper UI\n"
                    value.sha256 = mods.digest(value.data)
                elif fault == "missing Python":
                    value = current.screws.files[screws.PYTHON]
                    value.data = value.sha256 = None
                else:
                    records = engine.installed_records(current)
                    records[screws.PYTHON]["files"][mesh.TARGET]["undo"][0]["after"] += "# altered owned include\n"
                    test_helper_versions.stamp(current, records)
                current.screws.analysis.update(known=False, enabled=None, patchable=False,
                                               reason="An unrelated helper file needs review.")
                retained = copy.deepcopy(engine.installed_records(current)[screws.PYTHON])
                options = {"desired": None, "mesh_desired": True}
                repair = {"targets": (mesh.TARGET,)}
                if fault == "tampered include record":
                    with patch.object(engine, "scan", return_value=copy.deepcopy(current)), \
                            patch.object(engine, "Remote") as remote, \
                            self.assertRaisesRegex(ValueError, "undo record"):
                        engine.apply(self.connection, Path("unused"), current, repair=repair, **options)
                    remote.assert_not_called()
                    continue
                pending = engine.prepare_all(current, repair_targets=(mesh.TARGET,), **options)
                after = self.source.after(current, pending)
                with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                        patch.object(engine.network, "status", return_value=current.status), \
                        patch.object(engine, "Remote") as remote:
                    command = remote.return_value.__enter__.return_value.python
                    command.return_value = {"saved": list(pending)}
                    result = engine.apply(self.connection, Path("unused"), current, repair=repair, **options)
                self.assertTrue(result["written"])
                command.assert_called_once()
                script, request = command.call_args.args
                self.assertEqual(script, APPLY_BATCH)
                items = {item["path"]: item for item in request["files"]}
                self.assertEqual(set(items), {mesh.TARGET, mod_versions.TARGET})
                records = mod_versions.load(base64.b64decode(items[mod_versions.TARGET]["data"]))
                self.assertEqual(records[screws.PYTHON], retained)
                root = base64.b64decode(items[mesh.TARGET]["data"])
                self.assertEqual(root.count(screws.SCREWS_INCLUDE.encode()), 1)

    def test_fresh_or_planned_changes_to_unrelated_records_still_block_before_write(self):
        prepare = engine.prepare_all
        for fault in ("fresh ledger", "planned ledger"):
            with self.subTest(fault=fault):
                current = self.current()
                fresh = copy.deepcopy(current)
                if fault == "fresh ledger":
                    records = engine.installed_records(fresh)
                    records[mesh.TARGET]["firmware"] = "1.1.6.3"
                    test_helper_versions.stamp(fresh, records)
                def plan(snapshot, *args, **kwargs):
                    pending = prepare(snapshot, *args, **kwargs)
                    if fault == "planned ledger":
                        records = mod_versions.load(pending[mod_versions.TARGET])
                        records[mesh.TARGET]["firmware"] = "1.1.6.3"
                        pending[mod_versions.TARGET] = mod_versions.dump(records)
                    return pending
                with patch.object(engine, "scan", return_value=fresh), \
                        patch.object(engine, "prepare_all", side_effect=plan), \
                        patch.object(engine, "Remote") as remote, \
                        patch.object(engine.network, "status", side_effect=AssertionError("Network access forbidden")), \
                        self.assertRaises((ValueError, RuntimeError)):
                    engine.apply(self.connection, Path("unused"), current, True, repair=self.repair)
                remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
