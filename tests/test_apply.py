"""Exercise the SSH write command locally; mock only POSIX metadata/directory sync.

Real printer files are never used. All temporary data stays under the project.
"""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import engine
from app.mods import digest
from app.remote import APPLY, Connection


class ApplyTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "build" / "file-checks"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.path = self.root / "example.py"
        self.path.write_bytes(b"value = 1\n")
        self.sync = self.root / "sync"
        self.sync.write_bytes(b"")

    def tearDown(self):
        for path in self.root.iterdir():
            path.chmod(0o600)
        self.directory.cleanup()

    def apply(self, new=b"value = 2\n", expected=None, original_hash=None, restore=False):
        request = {"path": str(self.path), "expected": expected or digest(self.path.read_bytes()),
                   "data": base64.b64encode(new).decode(), "new_hash": digest(new),
                   "original_hash": original_hash, "may_capture_original": True, "reference": "fixture", "restore": restore}
        original_open = os.open
        def portable_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                return original_open(str(self.sync), flags, *args, **kwargs)
            return original_open(path, flags, *args, **kwargs)
        with patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(io.StringIO()), \
             patch("os.open", portable_open), patch("os.fsync", lambda fd: None), \
             patch("os.chown", lambda *args: None, create=True):
            exec(compile(APPLY, "<remote-apply>", "exec"), {})

    def test_initial_original_is_never_overwritten(self):
        first = self.path.read_bytes()
        self.apply()
        original = self.path.with_suffix(".ptn.Original.txt")
        self.assertEqual(original.read_bytes(), first)
        self.apply(b"value = 3\n", original_hash=digest(first))
        self.assertEqual(original.read_bytes(), first)
        self.assertEqual(self.path.read_bytes(), b"value = 3\n")
        self.assertFalse(list(self.root.glob("*.uncursed-pending-*.txt")))

    def test_stale_scan_does_not_write_or_create_backup(self):
        with self.assertRaisesRegex(ValueError, "changed"):
            self.apply(expected="old fingerprint")
        self.assertEqual(self.path.read_bytes(), b"value = 1\n")
        self.assertFalse(self.path.with_suffix(".ptn.Original.txt").exists())

    def test_damaged_original_blocks_replacement(self):
        first = self.path.read_bytes()
        self.apply()
        original = self.path.with_suffix(".ptn.Original.txt")
        original.chmod(0o600)
        original.write_bytes(b"broken\n")
        with self.assertRaisesRegex(ValueError, "damaged"):
            self.apply(b"value = 3\n", original_hash=digest(first))
        self.assertEqual(self.path.read_bytes(), b"value = 2\n")

    def test_restore_from_ptn_backup_preserves_original(self):
        first = self.path.read_bytes()
        self.apply()
        original = self.path.with_suffix(".ptn.Original.txt")
        manifest = Path(str(original) + ".json")
        saved_manifest = manifest.read_bytes()
        self.assertFalse(Path(str(self.path) + ".Original.txt").exists())
        self.assertEqual(json.loads(saved_manifest)["path"], str(self.path))
        self.apply(original.read_bytes(), original_hash=digest(first), restore=True)
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(original.read_bytes(), first)
        self.assertEqual(manifest.read_bytes(), saved_manifest)


class ApplyWithoutRestartTests(unittest.TestCase):
    def test_apply_verifies_saved_file_without_restart(self):
        old, new = b"value = 1\n", b"value = 2\n"
        status = {"native": {"model": "F008", "modelVersion": "1.1.6.4"}}
        before = engine.Snapshot(old, digest(old), {"enabled": False}, None, None, "",
                                 status, "", {"history": True}, 0)
        after = engine.Snapshot(new, digest(new), {"enabled": True}, old, digest(old), "",
                                status, "", {"history": True}, 1)
        with patch.object(engine, "scan", side_effect=[before, after]) as scan, \
             patch.object(engine, "prepare", return_value=new), \
             patch.object(engine, "Remote") as remote, \
             patch.object(engine.network, "require_idle"), \
             patch.object(engine.network, "status", return_value=status), \
             patch.object(engine.network, "http") as http:
            remote.return_value.__enter__.return_value.python.return_value = True
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, True)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(remote.return_value.__enter__.return_value.python.call_args.args[0], APPLY)
        http.assert_not_called()
        self.assertIs(result["snapshot"], after)
        self.assertFalse(result["restarted"])
        self.assertIn("Power cycle", result["message"])
