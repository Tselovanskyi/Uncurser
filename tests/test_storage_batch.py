"""Storage relocation and rollback, executed on synthetic project-local files."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from app.mods import digest
from app.remote import APPLY_BATCH


class StorageBatchTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "build" / "file-checks"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.storage = self.root / "Uncurser"
        self.old = self.root / "config"
        self.old.mkdir()
        self.sync = self.root / "sync.txt"
        self.sync.write_bytes(b"")
        self.program = APPLY_BATCH.replace('ROOT="/mnt/UDISK/Uncurser"', "ROOT=" + repr(str(self.storage)), 1)

    def tearDown(self):
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        self.directory.cleanup()

    def attrs(self, path):
        value = path.stat()
        return {"mode": stat.S_IMODE(value.st_mode), "uid": value.st_uid, "gid": value.st_gid}

    def move(self, name="printer.cfn.Original.txt", data=b"original config", destination=None):
        source = self.old / name
        source.write_bytes(data)
        destination = destination or self.storage / "originals" / name
        return {"source": str(source), "destination": str(destination), "source_hash": digest(data),
                "destination_hash": digest(destination.read_bytes()) if destination.exists() else None, "attrs": self.attrs(source)}

    def run_request(self, request, *, replace=None, link=None, unlink=None):
        native_open = os.open
        def portable_open(path, flags, *args, **kwargs):
            if Path(path).is_dir():
                return native_open(str(self.sync), flags, *args, **kwargs)
            if args and os.name == "nt" and args[0] == 0o444:
                args = (0o666, *args[1:])
            return native_open(path, flags, *args, **kwargs)
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(output), \
                patch("os.open", portable_open), patch("os.fsync", lambda fd: None), \
                patch("os.fchown", lambda *args: None, create=True), patch("os.fchmod", lambda *args: None, create=True), \
                patch("os.replace", replace or os.replace), patch("os.link", link or os.link), patch("os.unlink", unlink or os.unlink):
            exec(compile(self.program, "<local-storage-relocation>", "exec"), {})
        return json.loads(output.getvalue())

    def assert_no_candidates(self):
        self.assertFalse(list(self.root.rglob("*.uncursed-pending-*.txt")))

    def normal_request(self):
        path = self.old / "example.py"
        path.write_bytes(b"value = 1\n")
        backup = self.storage / "originals" / "example.ptn.Original.txt"
        first = self.move("example.ptn.Original.txt", path.read_bytes(), backup)
        metadata = {"path": str(path), "sha256": digest(path.read_bytes()), "absent": False,
                    **self.attrs(path)}
        second = self.move("example.ptn.Original.txt.json", json.dumps(metadata, separators=(",", ":")).encode(), Path(str(backup) + ".json"))
        item = {"path": str(path), "backup": str(backup), "expected": digest(path.read_bytes()),
                "data": base64.b64encode(b"value = 2\n").decode(), "new_hash": digest(b"value = 2\n"),
                "original_hash": first["source_hash"], "original_absent": False, "may_capture_original": False,
                "reference": "fixture", "restore": False, "kind": "python"}
        return {"files": [item], "moves": [first, second],
                "guards": {move["destination"]: move["source_hash"] for move in (first, second)}}, path

    def test_migration_only_preserves_exact_bytes_attributes_and_cleans_old_copies(self):
        moves = [self.move(), self.move("printer.cfn.Original.txt.json", b'{"immutable":"metadata"}')]
        expected = {move["destination"]: Path(move["source"]).read_bytes() for move in moves}
        result = self.run_request({"files": [], "moves": moves})
        self.assertEqual(len(result["moved"]), 2)
        self.assertEqual(result["saved"], [])
        for move in moves:
            self.assertFalse(Path(move["source"]).exists())
            self.assertEqual(Path(move["destination"]).read_bytes(), expected[move["destination"]])
            self.assertEqual(self.attrs(Path(move["destination"])), move["attrs"])
        self.assert_no_candidates()

    def test_all_move_guards_are_checked_before_first_copy(self):
        moves = [self.move(), self.move("other.txt")]
        Path(moves[1]["source"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed since scan"):
            self.run_request({"files": [], "moves": moves})
        self.assertFalse(self.storage.exists())
        self.assertTrue(all(Path(move["source"]).exists() for move in moves))

    def test_identical_existing_destination_is_reused_without_overwriting(self):
        move = self.move()
        destination = Path(move["destination"])
        destination.parent.mkdir(parents=True)
        destination.write_bytes(Path(move["source"]).read_bytes())
        move["destination_hash"] = move["source_hash"]
        self.run_request({"files": [], "moves": [move]})
        self.assertFalse(Path(move["source"]).exists())
        self.assertEqual(destination.read_bytes(), b"original config")
        self.assert_no_candidates()

    def test_conflicting_destination_and_unsafe_destination_block_relocation(self):
        move = self.move()
        destination = Path(move["destination"])
        destination.parent.mkdir(parents=True)
        destination.write_bytes(b"different backup")
        move["destination_hash"] = digest(destination.read_bytes())
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.run_request({"files": [], "moves": [move]})
        move["destination"] = str(self.root / "outside.txt")
        move["destination_hash"] = None
        with self.assertRaisesRegex(ValueError, "migration path"):
            self.run_request({"files": [], "moves": [move]})
        self.assertEqual(Path(move["source"]).read_bytes(), b"original config")

    def test_copy_failure_removes_only_destinations_created_by_this_batch(self):
        moves = [self.move(), self.move("second.txt")]
        native_link = os.link
        def link(source, destination):
            if destination == moves[1]["destination"]:
                raise OSError("copy failed")
            return native_link(source, destination)
        with self.assertRaisesRegex(OSError, "copy failed"):
            self.run_request({"files": [], "moves": moves}, link=link)
        self.assertTrue(all(Path(move["source"]).exists() for move in moves))
        self.assertFalse(any(Path(move["destination"]).exists() for move in moves))
        self.assert_no_candidates()

    def test_normal_patch_and_metadata_relocation_commit_together(self):
        request, path = self.normal_request()
        result = self.run_request(request)
        self.assertEqual(path.read_bytes(), b"value = 2\n")
        self.assertEqual(result["saved"], [str(path)])
        self.assertTrue(all(Path(move["destination"]).exists() and not Path(move["source"]).exists() for move in request["moves"]))
        self.assert_no_candidates()

    def test_cleanup_failure_restores_deleted_legacy_metadata_and_normal_patch(self):
        request, path = self.normal_request()
        native_unlink = os.unlink
        def unlink(target):
            if target == request["moves"][1]["source"]:
                raise OSError("cleanup failed")
            return native_unlink(target)
        with self.assertRaisesRegex(OSError, "cleanup failed"):
            self.run_request(request, unlink=unlink)
        self.assertEqual(path.read_bytes(), b"value = 1\n")
        self.assertTrue(all(Path(move["source"]).exists() for move in request["moves"]))
        self.assertFalse(any(Path(move["destination"]).exists() for move in request["moves"]))
        self.assert_no_candidates()

    def test_concurrent_destination_writer_is_preserved_and_reported(self):
        move = self.move()
        native_unlink = os.unlink
        def unlink(target):
            if target == move["source"]:
                Path(move["destination"]).write_bytes(b"another writer")
                raise OSError("cleanup interrupted")
            return native_unlink(target)
        with self.assertRaisesRegex(ValueError, "some changes may remain"):
            self.run_request({"files": [], "moves": [move]}, unlink=unlink)
        self.assertEqual(Path(move["source"]).read_bytes(), b"original config")
        self.assertEqual(Path(move["destination"]).read_bytes(), b"another writer")
        self.assert_no_candidates()

    def test_ledger_manifest_alias_is_preserved_while_new_ledger_commits(self):
        ledger = self.storage / "Uncurser_mods.json"
        before = b'{"schema":1,"mods":{}}'
        after = b'{"schema":1,"mods":{"fixture":{}}}'
        ledger_move = self.move("Uncurser_mods.json", before, ledger)
        backup = self.storage / "originals" / "Uncurser_mods.jsn.Original.txt"
        original_move = self.move("Uncurser_mods.jsn.Original.txt", b"", backup)
        manifest = {"path": "/mnt/UDISK/printer_data/config/Uncurser_mods.json", "sha256": digest(b""), "absent": True,
                    **ledger_move["attrs"]}
        raw_manifest = json.dumps(manifest, separators=(",", ":")).encode()
        manifest_move = self.move("Uncurser_mods.jsn.Original.txt.json", raw_manifest, Path(str(backup) + ".json"))
        item = {"path": str(ledger), "backup": str(backup), "expected": digest(before),
                "data": base64.b64encode(after).decode(), "new_hash": digest(after),
                "original_hash": digest(b""), "original_absent": True, "may_capture_original": False,
                "original_paths": [str(ledger), manifest["path"]], "reference": "fixture", "kind": "json"}
        moves = [ledger_move, original_move, manifest_move]
        self.run_request({"files": [item], "moves": moves})
        self.assertEqual(ledger.read_bytes(), after)
        self.assertEqual(Path(str(backup) + ".json").read_bytes(), raw_manifest)
        self.assertTrue(all(not Path(move["source"]).exists() for move in moves))
        self.assert_no_candidates()


if __name__ == "__main__":
    unittest.main()
