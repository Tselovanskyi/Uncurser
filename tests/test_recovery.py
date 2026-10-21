"""Startup recovery checks on synthetic files only; never connect to a printer."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import config_blocks, mesh, mods, recovery, screws
from app.remote import APPLY_BATCH, Connection


def halted(path=None):
    return {"info": {"state": "error", "config_file": mesh.TARGET,
                     "state_message": "Include file '" + (path or screws.LEGACY_CONFIG) + "' does not exist\n\nPrinter is halted"},
            "native": {"model": "F008", "modelVersion": "1.1.7.0", "deviceState": 0},
            "objects": {"print_stats": {}, "idle_timeout": {}, "toolhead": {}}}


def file_read(data, **attrs):
    return {"data": data, "sha256": mods.digest(data), "mode": 0o644, "uid": 0, "gid": 0, **attrs}


class RecoveryInspectionTests(unittest.TestCase):
    def source(self, block=None):
        return ((block or config_blocks.LEGACY_SCREWS_INCLUDE) + "[printer]\nkinematics: corexy\n"
                + mesh.AUTOSAVE + "\n#*# [bed_mesh default]\n#*# version = 1\n").encode()

    def test_exact_legacy_and_current_blocks_preserve_all_other_bytes(self):
        for block, path in ((config_blocks.LEGACY_SCREWS_INCLUDE, screws.LEGACY_CONFIG),
                            (config_blocks.SCREWS_INCLUDE, screws.CONFIG)):
            for newline in (b"\n", b"\r\n"):
                with self.subTest(path=path, newline=newline):
                    source = self.source(block).replace(b"\n", newline)
                    result = recovery.inspect(halted(path.replace("/printer_data", "//printer_data")), file_read(source), None, None)
                    self.assertTrue(result["available"], result)
                    self.assertEqual(result["after"], source[len(block.encode().replace(b"\n", newline)):])
                    self.assertEqual(mesh.split(source)[1], mesh.split(result["after"])[1])

    def test_other_errors_models_and_firmware_are_not_recovery_candidates(self):
        for field, value in (("version", "1.1.6.4"), ("version", "unknown"), ("model", "other"),
                             ("state", "ready"), ("error", "/mnt/UDISK/printer_data/config/user.cfg")):
            status = halted()
            if field == "version":
                status["native"]["modelVersion"] = value
            elif field == "model":
                status["native"]["model"] = value
            elif field == "state":
                status["info"]["state"] = value
            else:
                status = halted(value)
            with self.subTest(field=field, value=value):
                self.assertIsNone(recovery.inspect(status, file_read(self.source()), None, None))

    def test_newer_verified_firmware_gets_the_same_narrow_repair(self):
        status = halted()
        status["native"]["modelVersion"] = "1.2.0.0"
        self.assertTrue(recovery.inspect(status, file_read(self.source()), None, None)["available"])

    def test_busy_or_paused_printers_cannot_be_repaired(self):
        for section, key, value in (("native", "deviceState", 1), ("print_stats", "state", "printing"),
                                    ("print_stats", "state", "paused"), ("idle_timeout", "state", "Printing"),
                                    ("toolhead", "G29_flag", True)):
            status = halted()
            destination = status["native"] if section == "native" else status["objects"][section]
            destination[key] = value
            self.assertFalse(recovery.inspect(status, file_read(self.source()), None, None)["available"])

    def test_edited_duplicate_and_unowned_includes_are_rejected(self):
        source = self.source()
        for data in (source.replace(b"[include screws_tilt_adjust.cfg]", b"[include ./screws_tilt_adjust.cfg]"),
                     config_blocks.LEGACY_SCREWS_INCLUDE.encode() + source,
                     source.replace(b"[printer]", b"[include screws_tilt_adjust.cfg]\n[printer]"),
                     source.replace(config_blocks.LEGACY_SCREWS_INCLUDE.encode(), b"[include screws_tilt_adjust.cfg]\n")):
            result = recovery.inspect(halted(), file_read(data), None, None)
            self.assertFalse(result["available"], result)

    def test_present_missing_file_and_invalid_attributes_or_checksum_are_rejected(self):
        source = file_read(self.source())
        self.assertFalse(recovery.inspect(halted(), source, file_read(b"present"), None)["available"])
        for key, value in (("sha256", "changed"), ("mode", None), ("uid", True), ("gid", -1)):
            changed = {**source, key: value}
            self.assertFalse(recovery.inspect(halted(), changed, None, None)["available"])
        status = halted()
        status["info"]["config_file"] = "/mnt/UDISK/printer_data/config/other.cfg"
        self.assertFalse(recovery.inspect(status, source, None, None)["available"])

    def test_repair_revalidates_source_before_running_any_write(self):
        source = self.source()
        scanned = recovery.inspect(halted(), file_read(source), None, None)
        remote = Mock()
        remote.read.side_effect = (file_read(source + b"#*# retained new calibration\n"), None, None)
        with patch.object(recovery, "Remote") as remote_class, patch.object(recovery.network, "status", return_value=halted()):
            remote_class.return_value.__enter__.return_value = remote
            with self.assertRaisesRegex(ValueError, "changed after"):
                recovery.repair(Connection("192.0.2.1"), Path("unused"), scanned)
        remote.python.assert_not_called()

    def test_repair_verifies_written_file_and_never_sends_restart(self):
        source = self.source()
        scanned = recovery.inspect(halted(), file_read(source), None, None)
        remote = Mock()
        remote.read.side_effect = (file_read(source), None, None, file_read(scanned["after"]))
        with patch.object(recovery, "Remote") as remote_class, patch.object(recovery.network, "status", return_value=halted()):
            remote_class.return_value.__enter__.return_value = remote
            result = recovery.repair(Connection("192.0.2.1"), Path("unused"), scanned)
        self.assertTrue(result["written"])
        self.assertTrue(result["power_cycle"])
        self.assertEqual(remote.python.call_count, 1)
        program, request = remote.python.call_args.args
        self.assertEqual(program, APPLY_BATCH)
        self.assertEqual(len(request["files"]), 1)
        item = request["files"][0]
        self.assertTrue(item["recovery"])
        self.assertEqual(item["path"], mesh.TARGET)
        self.assertEqual(item["expected_attrs"], scanned["attrs"])
        self.assertEqual(base64.b64decode(item["data"]), scanned["after"])
        self.assertEqual(request["guards"], {scanned["missingpath"]: None})


class RecoveryWriteTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "build" / "file-checks"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.path = self.root / "printer.cfg"
        self.backups = self.root / "recovery"
        self.missing = self.root / "missing.cfg"
        generated = (mesh.AUTOSAVE + "\n#*# [bed_mesh default]\n#*# points = 0.3, 0.4\n").encode()
        self.before = (config_blocks.LEGACY_SCREWS_INCLUDE + "[printer]\nkinematics: corexy\n").encode() + generated
        self.after = b"[printer]\nkinematics: corexy\n" + generated
        self.path.write_bytes(self.before)
        self.sync_file = self.root / "sync.txt"
        self.sync_file.write_bytes(b"")
        attrs = self.path.stat()
        self.backup = self.backups / ("printer." + mods.digest(self.before) + ".txt")
        self.item = {"path": str(self.path), "backup": str(self.backup), "expected": mods.digest(self.before),
                     "expected_attrs": {"mode": stat.S_IMODE(attrs.st_mode), "uid": attrs.st_uid, "gid": attrs.st_gid},
                     "data": base64.b64encode(self.after).decode(), "new_hash": mods.digest(self.after),
                     "removed_prefix": base64.b64encode(config_blocks.LEGACY_SCREWS_INCLUDE.encode()).decode(),
                     "recovery": True, "kind": "config", "restore": False,
                     "original_hash": None, "may_capture_original": True, "original_absent": False, "reference": "startup-recovery"}
        self.request = {"files": [self.item], "guards": {str(self.missing): None}}
        self.program = APPLY_BATCH.replace('ROOT="/mnt/UDISK/Uncurser"', 'ROOT=' + repr(str(self.root)), 1).replace(
            json.dumps(mesh.TARGET), repr(str(self.path)))
        self.program = self.program.replace('ROOT+"/recovery"+stem+marker+"."',
            'os.path.join(ROOT,"recovery",os.path.basename(stem)+marker+".")')

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.directory.cleanup()

    def run_write(self, replace=None, deny_create=None, unlink=None):
        original_open = os.open
        def portable_open(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and deny_create is not None and deny_create():
                raise OSError("No further file creation is possible")
            if Path(path).is_dir():
                return original_open(str(self.sync_file), flags, *args, **kwargs)
            if args and os.name == "nt" and args[0] == 0o444:
                args = (0o666, *args[1:])
            return original_open(path, flags, *args, **kwargs)
        with patch("sys.stdin", io.StringIO(json.dumps(self.request))), contextlib.redirect_stdout(io.StringIO()), \
                patch("os.open", portable_open), patch("os.fsync", lambda fd: None), \
                patch("os.fchown", lambda *args: None, create=True), patch("os.fchmod", lambda *args: None, create=True), \
                patch("os.replace", replace or os.replace), patch("os.unlink", unlink or os.unlink):
            exec(compile(self.program, "<local-startup-recovery>", "exec"), {})

    def assert_no_temporary_files(self):
        self.assertFalse(list(self.root.rglob("*.uncursed-pending-*.txt")))
        self.assertFalse(list(self.root.rglob("*.rollback.txt")))

    def test_atomic_repair_removes_temporary_recovery_without_originals_or_records(self):
        self.run_write()
        self.assertEqual(self.path.read_bytes(), self.after)
        self.assertFalse(self.backup.exists())
        self.assertFalse(list(self.backups.iterdir()))
        self.assertFalse(list(self.root.rglob("*.Original.txt")))
        self.assertFalse(list(self.root.rglob("Uncurser_mods.json")))
        self.assertFalse(list(self.root.rglob("*.json")))
        self.assert_no_temporary_files()

    def test_stale_source_and_appearing_helper_block_before_backup_or_write(self):
        self.path.write_bytes(self.before + b"# changed\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.run_write()
        self.assertFalse(self.backup.exists())
        self.path.write_bytes(self.before)
        self.missing.write_bytes(b"appeared")
        with self.assertRaisesRegex(ValueError, "changed|appeared"):
            self.run_write()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(self.backup.exists())
        self.missing.unlink()
        self.item["expected_attrs"]["mode"] ^= 0o200
        with self.assertRaisesRegex(ValueError, "attributes|changed"):
            self.run_write()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(self.backup.exists())

    def test_failed_atomic_replacement_cleans_candidates_and_preserves_source(self):
        def fail(source, target):
            raise OSError("replacement failed")
        with self.assertRaisesRegex(OSError, "replacement failed"):
            self.run_write(fail)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.backup.read_bytes(), self.before)
        self.assert_no_temporary_files()

    def test_postwrite_failure_rolls_back_without_leftover_scripts_or_candidates(self):
        native_replace = os.replace
        calls = []
        def replace(source, target):
            native_replace(source, target)
            calls.append((source, target))
            if len(calls) == 1:
                self.missing.write_bytes(b"another writer restored the helper")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.run_write(replace)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.backup.read_bytes(), self.before)
        self.assert_no_temporary_files()

    def test_prepared_rollback_survives_no_new_file_creation_after_replacement(self):
        native_replace = os.replace
        calls, blocked = [], [False]
        def replace(source, target):
            native_replace(source, target)
            calls.append((source, target))
            if len(calls) == 1:
                self.missing.write_bytes(b"another writer restored the helper")
                blocked[0] = True
        with self.assertRaisesRegex(ValueError, "Included configuration changed"):
            self.run_write(replace, deny_create=lambda: blocked[0])
        self.assertTrue(blocked[0])
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.backup.read_bytes(), self.before)
        self.assert_no_temporary_files()

    def test_damaged_existing_recovery_copy_blocks_repair(self):
        self.backups.mkdir()
        self.backup.write_bytes(b"damaged")
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.run_write()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertEqual(self.backup.read_bytes(), b"damaged")
        self.assert_no_temporary_files()

    def test_shared_batch_preserves_existing_originals_and_rejects_unrelated_edits(self):
        original = self.root / "printer.cfn.Original.txt"
        manifest = self.root / "printer.cfn.Original.txt.json"
        original.write_bytes(b"unrelated immutable original")
        manifest.write_bytes(b'{"keep":"unchanged"}')
        self.item["data"] = base64.b64encode(self.after.replace(b"corexy", b"cartesian")).decode()
        self.item["new_hash"] = mods.digest(base64.b64decode(self.item["data"]))
        with self.assertRaisesRegex(ValueError, "owned|include|prefix|recovery"):
            self.run_write()
        self.assertEqual(self.path.read_bytes(), self.before)
        self.assertFalse(self.backup.exists())
        self.item["data"] = base64.b64encode(self.after).decode()
        self.item["new_hash"] = mods.digest(self.after)
        self.run_write()
        self.assertEqual(original.read_bytes(), b"unrelated immutable original")
        self.assertEqual(manifest.read_bytes(), b'{"keep":"unchanged"}')
        self.assertFalse(self.backup.exists())
        self.assert_no_temporary_files()


if __name__ == "__main__":
    unittest.main()
