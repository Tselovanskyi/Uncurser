"""Recovery integration uses synthetic snapshots and the existing local batch harness."""
import base64
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mod_versions, mods, storage
from app.remote import APPLY_BATCH, Connection
from tests import test_mod_versions, test_recovery


def snapshot():
    current = copy.deepcopy(test_mod_versions.installed_snapshot())
    current.status = {"native": {"model": "F008", "modelVersion": "1.1.7.0", "deviceState": 0},
                      "info": {"state": "ready"}, "objects": {}}
    current.original = current.original_hash = None
    current.backup_guards = {mods.ORIGINAL: None, mods.MANIFEST: None}
    current.mesh.original = current.mesh.original_hash = None
    sources = {path: "/rom" + path for path in test_mod_versions.SOURCES}
    current.stock = {"available": True, "identity": {"model": "F008", "firmware": "1.1.7.0",
                     "board": "CR0CN240319C13", "structure_version": "0", "variant": "F008_CR0CN240319C13"},
                     "proof": {"verified": True},
                     "files": {path: {"data": data, "sha256": mods.digest(data), "source": sources[path]}
                               for path, data in test_mod_versions.SOURCES.items()},
                     "guards": {sources[path]: mods.digest(data) for path, data in test_mod_versions.SOURCES.items()}}
    return current


class StockApplyTests(unittest.TestCase):
    def test_missing_originals_recover_without_creating_originals_or_records(self):
        current = snapshot()
        pending = engine.prepare_all(current, False, mesh_desired=False)
        self.assertEqual(pending[mods.TARGET], test_mod_versions.SOURCES[mods.TARGET])
        self.assertEqual(pending[mesh.TARGET], test_mod_versions.SOURCES[mesh.TARGET])
        self.assertNotIn(mod_versions.TARGET, pending)
        self.assertIn("firmware", engine.difference_all(current, False, mesh_desired=False))
        after = copy.deepcopy(current)
        for path, data in pending.items():
            value = engine.file_snapshot(after, path)
            value.data, value.sha256 = data, mods.digest(data)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(current), after]), \
                patch.object(engine.network, "status", return_value=current.status), \
                patch.object(engine, "Remote") as remote:
            command = remote.return_value.__enter__.return_value.python
            command.return_value = {"saved": list(pending)}
            engine.apply(Connection("192.0.2.1"), Path("unused"), current, False, mesh_desired=False)
        self.assertEqual(command.call_count, 1)
        program, request = command.call_args.args
        self.assertEqual(program, APPLY_BATCH)
        self.assertTrue(current.stock["guards"].items() <= request["guards"].items())
        for item in request["files"]:
            self.assertEqual(item["recovery"], "stock")
            self.assertEqual(item["backup"], storage.recovery(item["path"], item["expected"]))
            self.assertNotIn("Original", item["backup"])

    def test_stock_provenance_change_blocks_before_any_write(self):
        current, fresh = snapshot(), snapshot()
        fresh.stock["identity"]["structure_version"] = "1"
        with patch.object(engine, "scan", return_value=fresh), patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(RuntimeError, "recovery sources changed"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), current, False)
        remote.assert_not_called()

    def test_intact_ledger_with_both_proofs_missing_remains_readable_but_partial_proofs_block(self):
        current = snapshot()
        records = {mods.TARGET: mod_versions.capture(mods.TARGET, test_mod_versions.SOURCES[mods.TARGET], current.data)}
        ledger = mod_versions.dump(records)
        current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger), {"known": False, "enabled": True},
            backup_error="The version records have no original absence proof.",
            backup_guards={mod_versions.ORIGINAL: None, mod_versions.MANIFEST: None})
        pending = engine.prepare_all(current, False)
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET]), {})
        self.assertEqual(engine.installed_records(current), records)
        current.versions.backup_guards[mod_versions.MANIFEST] = "0" * 64
        with self.assertRaises(ValueError):
            engine.prepare_all(current, False)


class StockBatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_recovery.RecoveryWriteTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        fixture = self.fixture
        self.rom = fixture.root / "rom"
        self.rom.mkdir()
        self.source = self.rom / "printer.cfg"
        self.source.write_bytes(fixture.after)
        # Relocate only fixed printer roots so the real batch runs on local fixtures.
        fixture.program = fixture.program.replace(
            'ROOT+"/recovery"+stem+markers[suffix]+"."+item["expected"]+".txt"',
            'os.path.join(ROOT,"recovery",os.path.basename(stem)+markers[suffix]+"."+item["expected"]+".txt")')
        fixture.program = fixture.program.replace('source.startswith("/rom/")', 'source.startswith(' + repr(str(self.rom)) + ')')
        fixture.backup = fixture.backups / ("printer.cfn." + mods.digest(fixture.before) + ".txt")
        fixture.item.update(recovery="stock", backup=str(fixture.backup), stock_source=str(self.source), stock_hash=mods.digest(fixture.after))
        fixture.request["guards"][str(self.source)] = mods.digest(fixture.after)

    def test_stock_batch_removes_verified_recovery_copy_and_clears_candidates(self):
        fixture = self.fixture
        fixture.request["verify_config"] = True
        fixture.run_write()
        self.assertEqual(fixture.path.read_bytes(), fixture.after)
        self.assertFalse(fixture.backup.exists())
        self.assertFalse(list(fixture.root.rglob("*.Original.txt*")))
        fixture.assert_no_temporary_files()

    def test_nested_missing_dependency_blocks_before_any_recovery_write(self):
        fixture = self.fixture
        suffix = b"\n[include user.cfg]\n"
        fixture.before += suffix
        fixture.after += suffix
        fixture.path.write_bytes(fixture.before)
        self.source.write_bytes(fixture.after)
        (fixture.root / "user.cfg").write_bytes(b"[include missing.cfg]\n")
        fixture.backup = fixture.backups / ("printer.cfn." + mods.digest(fixture.before) + ".txt")
        fixture.item.update(expected=mods.digest(fixture.before), backup=str(fixture.backup),
            data=base64.b64encode(fixture.after).decode(), new_hash=mods.digest(fixture.after), stock_hash=mods.digest(fixture.after))
        fixture.request["guards"][str(self.source)] = mods.digest(fixture.after)
        fixture.request["verify_config"] = True
        with self.assertRaisesRegex(ValueError, "include has no matching files"):
            fixture.run_write()
        self.assertEqual(fixture.path.read_bytes(), fixture.before)
        self.assertFalse(fixture.backup.exists())

    def test_stock_absence_allows_owned_helper_delete_and_clears_bytecode(self):
        fixture = self.fixture
        fixture.path = fixture.root / "screws_tilt_adjust.py"
        fixture.before = b"# recognized owned helper\n"
        fixture.path.write_bytes(fixture.before)
        cache = Path(str(fixture.path) + "c")
        cache.write_bytes(b"generated bytecode")
        absent_source = self.rom / "screws_tilt_adjust.py"
        fixture.backup = fixture.backups / ("screws_tilt_adjust.ptn." + mods.digest(fixture.before) + ".txt")
        fixture.item.update(path=str(fixture.path), kind="python", expected=mods.digest(fixture.before),
            backup=str(fixture.backup), data=None, new_hash=None, original_absent=True,
            stock_source=str(absent_source), stock_hash=None)
        fixture.request["guards"][str(absent_source)] = None
        fixture.run_write()
        self.assertFalse(fixture.path.exists())
        self.assertFalse(cache.exists())
        self.assertFalse(fixture.backup.exists())
        fixture.assert_no_temporary_files()

    def test_changed_rom_source_blocks_before_replacement(self):
        fixture = self.fixture
        self.source.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Stock firmware source changed"):
            fixture.run_write()
        self.assertEqual(fixture.path.read_bytes(), fixture.before)
        self.assertFalse(fixture.backup.exists())

    def test_late_guard_failure_restores_prepared_file(self):
        import os
        fixture = self.fixture
        native_replace = os.replace
        changed = [False]
        def replace(source, target):
            native_replace(source, target)
            if not changed[0]:
                changed[0] = True
                self.source.write_bytes(b"changed after write")
        with self.assertRaisesRegex(ValueError, "Included configuration changed"):
            fixture.run_write(replace)
        self.assertEqual(fixture.path.read_bytes(), fixture.before)
        fixture.assert_no_temporary_files()

    def test_only_verified_recovery_copies_for_affected_targets_are_removed(self):
        fixture = self.fixture
        fixture.backups.mkdir()
        old_data = b"previously modified printer configuration\n"
        old = fixture.backups / ("printer.cfn." + mods.digest(old_data) + ".txt")
        legacy = fixture.backups / ("printer." + mods.digest(old_data) + ".txt")
        old.write_bytes(old_data)
        legacy.write_bytes(old_data)
        preserved = {
            fixture.backups / ("other.cfn." + mods.digest(old_data) + ".txt"): old_data,
            fixture.backups / ("printer.cfn." + "0" * 64 + ".txt"): b"checksum does not match",
            fixture.backups / "printer.cfn.invalid-checksum.txt": old_data,
            fixture.backups / ("fluidd-category-" + mods.digest(old_data) + ".txt"): old_data,
            fixture.root / "originals" / "printer.cfn.Original.txt": b"immutable original",
            fixture.root / "originals" / "printer.cfn.Original.txt.json": b'{"proof":"retained"}',
        }
        for path, data in preserved.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        fixture.run_write()
        self.assertEqual(fixture.path.read_bytes(), fixture.after)
        for path in (fixture.backup, old, legacy):
            self.assertFalse(path.exists(), str(path))
        for path, data in preserved.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        fixture.assert_no_temporary_files()

    def test_guarded_unchanged_dependency_recovery_is_removed(self):
        fixture = self.fixture
        dependency = fixture.root / "screws_tilt_adjust.py"
        fixture.request["guards"][str(dependency)] = None
        fixture.request["recovery_targets"] = {str(dependency): None}
        fixture.backups.mkdir()
        old_data = b"# previously removed helper\n"
        old = fixture.backups / ("screws_tilt_adjust.ptn." + mods.digest(old_data) + ".txt")
        old.write_bytes(old_data)
        fixture.run_write()
        self.assertFalse(old.exists())
        self.assertFalse(dependency.exists())
        self.assertEqual(fixture.path.read_bytes(), fixture.after)
        fixture.assert_no_temporary_files()

    def test_recovery_cleanup_preserves_symlinks_and_their_targets(self):
        fixture = self.fixture
        fixture.backups.mkdir()
        data = b"linked recovery data must not be removed\n"
        target = fixture.root / "keep.txt"
        target.write_bytes(data)
        link = fixture.backups / ("printer.cfn." + mods.digest(data) + ".txt")
        try:
            link.symlink_to(target)
        except OSError as error:
            self.skipTest("Symlink creation is unavailable: " + str(error))
        fixture.run_write()
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), data)
        self.assertEqual(target.read_bytes(), data)
        self.assertFalse(fixture.backup.exists())
        fixture.assert_no_temporary_files()

    def test_recovery_unlink_failure_restores_deleted_copies_and_working_file(self):
        fixture = self.fixture
        fixture.backups.mkdir()
        copies = {}
        for data in (b"older config one\n", b"older config two\n"):
            path = fixture.backups / ("printer.cfn." + mods.digest(data) + ".txt")
            path.write_bytes(data)
            copies[path] = data
        native_unlink = os.unlink
        removed = []
        def unlink(path, *args, **kwargs):
            candidate = Path(path)
            if candidate in copies:
                if len(removed) == 1:
                    raise OSError("recovery cleanup denied")
                removed.append(candidate)
            return native_unlink(path, *args, **kwargs)
        with self.assertRaisesRegex(OSError, "recovery cleanup denied"):
            fixture.run_write(unlink=unlink)
        self.assertEqual(len(removed), 1)
        self.assertEqual(fixture.path.read_bytes(), fixture.before)
        self.assertEqual(fixture.backup.read_bytes(), fixture.before)
        for path, data in copies.items():
            self.assertEqual(path.read_bytes(), data, str(path))
        fixture.assert_no_temporary_files()

    def test_failed_recovery_restore_keeps_and_reports_last_intact_copy(self):
        fixture = self.fixture
        fixture.backups.mkdir()
        copies = {}
        for data in (b"older config one\n", b"older config two\n"):
            path = fixture.backups / ("printer.cfn." + mods.digest(data) + ".txt")
            path.write_bytes(data)
            copies[path] = data
        native_unlink, native_link = os.unlink, os.link
        removed = []
        def unlink(path, *args, **kwargs):
            candidate = Path(path)
            if candidate in copies:
                if removed:
                    raise OSError("recovery cleanup denied")
                removed.append(candidate)
            return native_unlink(path, *args, **kwargs)
        def link(source, target, *args, **kwargs):
            if removed and Path(target) == removed[0]:
                raise OSError("recovery restoration denied")
            return native_link(source, target, *args, **kwargs)
        with patch("os.link", link), self.assertRaisesRegex(ValueError, "some changes may remain") as result:
            fixture.run_write(unlink=unlink)
        self.assertEqual(len(removed), 1)
        self.assertFalse(removed[0].exists())
        retained = list(fixture.backups.glob(removed[0].name + ".uncursed-pending-*.txt"))
        self.assertEqual(len(retained), 1)
        self.assertEqual(retained[0].read_bytes(), copies[removed[0]])
        self.assertIn(str(retained[0]), str(result.exception))
        self.assertEqual(fixture.path.read_bytes(), fixture.before)
        self.assertEqual(fixture.backup.read_bytes(), fixture.before)
        self.assertFalse(list(fixture.root.rglob("*.rollback.txt")))


if __name__ == "__main__":
    unittest.main()
