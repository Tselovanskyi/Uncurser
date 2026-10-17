"""Old-firmware original pairs retire only after a guarded batch commits."""
import base64
import copy
import json
import os
from pathlib import Path
import unittest

from app.mods import digest
from tests import test_storage_batch


class RetireBatchTests(unittest.TestCase):
    def setUp(self):
        self.batch = test_storage_batch.StorageBatchTests()
        self.batch.setUp()
        # Windows cannot mirror a drive-qualified test target inside another path.
        self.batch.program = self.batch.program.replace(
            'canonical=os.path.normpath(ROOT+"/originals"+stem+marker+".Original.txt")',
            'canonical=os.path.normpath(os.path.join(ROOT,"originals",os.path.basename(stem)+marker+".Original.txt"))', 1)

    def tearDown(self):
        self.batch.tearDown()

    def pair(self, name="example.py", *, canonical=False, firmware="1.1.6.4"):
        target = self.batch.old / name
        if not target.exists():
            target.write_bytes(b"value = 2\n")
        filename = target.stem + ".ptn.Original.txt"
        backup = self.batch.storage / "originals" / filename if canonical else target.with_name(filename)
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(b"value = 1\n")
        manifest = Path(str(backup) + ".json")
        manifest.write_bytes(json.dumps({"path": str(target), "sha256": digest(backup.read_bytes()),
            "absent": False, "model": "F008", "firmware": firmware,
            **self.batch.attrs(target)}, separators=(",", ":")).encode())
        return {"target": str(target), "backup": str(backup), "model": "F008", "firmware": firmware,
                "files": [{"path": str(path), "sha256": digest(path.read_bytes()), "attrs": self.batch.attrs(path)}
                          for path in (backup, manifest)]}

    def request(self, *pairs):
        return {"files": [], "moves": [], "retire": list(pairs), "model": "F008",
                "firmware": "printer hw ver:;DWIN sw ver:1.1.7.0;",
                "guards": {item["path"]: item["sha256"] for pair in pairs for item in pair["files"]}}

    def refresh_pair(self, pair):
        for item in pair["files"]:
            path = Path(item["path"])
            item["sha256"] = digest(path.read_bytes()) if path.exists() else None
            item["attrs"] = self.batch.attrs(path) if path.exists() else None
        return pair

    def patch_item(self, pair):
        target = Path(pair["target"])
        return {"path": str(target), "backup": pair["backup"], "expected": digest(target.read_bytes()),
                "data": base64.b64encode(b"value = 1\n").decode(), "new_hash": digest(b"value = 1\n"),
                "original_hash": pair["files"][0]["sha256"], "original_absent": False,
                "may_capture_original": False, "reference": "fixture", "kind": "python", "restore": False}

    def test_canonical_and_legacy_old_pairs_retire_while_current_pair_relocates(self):
        old = [self.pair(), self.pair(canonical=True)]
        current = self.pair("current.py", firmware="1.1.7.0")
        request = self.request(*old)
        for item in current["files"]:
            destination = self.batch.storage / "originals" / Path(item["path"]).name
            request["moves"].append({"source": item["path"], "destination": str(destination),
                "source_hash": item["sha256"], "destination_hash": None, "attrs": item["attrs"]})
            request["guards"][str(destination)] = item["sha256"]
        result = self.batch.run_request(request)
        self.assertEqual(set(result["retired"]), {item["path"] for pair in old for item in pair["files"]})
        self.assertTrue(all(not Path(path).exists() for path in result["retired"]))
        for move in request["moves"]:
            self.assertEqual(digest(Path(move["destination"]).read_bytes()), move["source_hash"])
            self.assertFalse(Path(move["source"]).exists())
        self.assertEqual(result["saved"], [])
        self.batch.assert_no_candidates()

    def test_all_retirement_guards_precede_any_copy_or_delete(self):
        first, second = self.pair(), self.pair("second.py")
        request = self.request(first, second)
        request["moves"] = [self.batch.move("current.cfn.Original.txt")]
        Path(second["files"][1]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "changed since scan"):
            self.batch.run_request(request)
        self.assertFalse(self.batch.storage.exists())
        self.assertTrue(all(Path(item["path"]).exists() for pair in (first, second) for item in pair["files"]))
        self.batch.assert_no_candidates()

    def test_invalid_firmware_manifest_scope_or_overlap_never_removes_originals(self):
        pair = self.pair()
        for fault in ("current", "newer", "unknown", "wrong-model", "wrong-path", "incomplete", "overlap", "checksum"):
            with self.subTest(fault=fault):
                candidate = copy.deepcopy(pair)
                request = self.request(candidate)
                if fault in ("current", "newer", "unknown"):
                    candidate["firmware"] = {"current": "1.1.7.0", "newer": "1.1.8.0", "unknown": "unknown"}[fault]
                elif fault == "wrong-model":
                    candidate["model"] = "F009"
                elif fault == "wrong-path":
                    candidate["backup"] = str(self.batch.old / "unrelated.txt")
                elif fault == "incomplete":
                    candidate["files"].pop()
                elif fault == "overlap":
                    request["moves"] = [{"source": pair["backup"], "destination": str(self.batch.storage / "copy.txt"),
                        "source_hash": pair["files"][0]["sha256"], "destination_hash": None,
                        "attrs": pair["files"][0]["attrs"]}]
                else:
                    candidate["files"][0]["sha256"] = digest(b"different")
                with self.assertRaises(ValueError):
                    self.batch.run_request(request)
                self.assertTrue(all(Path(item["path"]).exists() for item in pair["files"]))
        self.batch.assert_no_candidates()

    def test_cleanup_failure_restores_original_pair_attributes_working_file_and_relocation(self):
        pair = self.pair()
        request = self.request(pair)
        request["files"] = [self.patch_item(pair)]
        request["moves"] = [self.batch.move("current.cfn.Original.txt")]
        originals = {item["path"]: Path(item["path"]).read_bytes() for item in pair["files"]}
        native_unlink = os.unlink
        def unlink(path):
            if path == pair["backup"] + ".json":
                self.assertEqual(Path(pair["target"]).read_bytes(), b"value = 1\n")
                raise OSError("retirement interrupted")
            return native_unlink(path)
        with self.assertRaisesRegex(OSError, "retirement interrupted"):
            self.batch.run_request(request, unlink=unlink)
        self.assertEqual(Path(pair["target"]).read_bytes(), b"value = 2\n")
        for item in pair["files"]:
            self.assertEqual(Path(item["path"]).read_bytes(), originals[item["path"]])
            self.assertEqual(self.batch.attrs(Path(item["path"])), item["attrs"])
        self.assertTrue(Path(request["moves"][0]["source"]).exists())
        self.assertFalse(Path(request["moves"][0]["destination"]).exists())
        self.batch.assert_no_candidates()

    def test_concurrent_recreated_original_is_preserved_and_reported(self):
        pair = self.pair()
        request = self.request(pair)
        request["files"] = [self.patch_item(pair)]
        native_unlink = os.unlink
        def unlink(path):
            if path == pair["backup"] + ".json":
                Path(pair["backup"]).write_bytes(b"another writer")
                raise OSError("retirement interrupted")
            return native_unlink(path)
        with self.assertRaisesRegex(ValueError, "some changes may remain"):
            self.batch.run_request(request, unlink=unlink)
        self.assertEqual(Path(pair["backup"]).read_bytes(), b"another writer")
        self.assertEqual(Path(pair["target"]).read_bytes(), b"value = 2\n")
        self.assertTrue(Path(pair["backup"] + ".json").exists())
        self.batch.assert_no_candidates()

    def test_checksum_and_missing_invalid_or_incomplete_metadata_retire_only_existing_files(self):
        for fault in ("checksum", "missing-manifest", "missing-backup", "invalid-json", "missing-fields"):
            with self.subTest(fault=fault):
                pair = self.pair(fault + ".py", canonical=True, firmware="1.1.7.0")
                backup, manifest = Path(pair["backup"]), Path(pair["backup"] + ".json")
                pair["reason"] = "checksum" if fault == "checksum" else "metadata"
                if fault == "checksum":
                    backup.write_bytes(b"damaged original\n")
                elif fault == "missing-manifest":
                    manifest.unlink()
                elif fault == "missing-backup":
                    backup.unlink()
                elif fault == "invalid-json":
                    manifest.write_bytes(b"{incomplete")
                else:
                    metadata = json.loads(manifest.read_bytes())
                    del metadata["firmware"]
                    manifest.write_bytes(json.dumps(metadata).encode())
                self.refresh_pair(pair)
                existing = {item["path"] for item in pair["files"] if item["sha256"] is not None}
                result = self.batch.run_request(self.request(pair))
                self.assertEqual(set(result["retired"]), existing)
                self.assertTrue(all(not Path(item["path"]).exists() for item in pair["files"]))
        self.batch.assert_no_candidates()

    def test_cleanup_reason_cannot_bypass_valid_current_firmware_original_protection(self):
        pair = self.pair(firmware="1.1.7.0")
        for reason in ("checksum", "metadata", "unknown"):
            with self.subTest(reason=reason):
                pair["reason"] = reason
                with self.assertRaises(ValueError):
                    self.batch.run_request(self.request(pair))
                self.assertTrue(all(Path(item["path"]).exists() for item in pair["files"]))
        self.batch.assert_no_candidates()

    def test_missing_counterpart_remains_guarded_during_relocation(self):
        pair = self.pair()
        manifest = Path(pair["backup"] + ".json")
        manifest.unlink()
        pair["reason"] = "metadata"
        request = self.request(self.refresh_pair(pair))
        move = self.batch.move("current.cfn.Original.txt")
        request["moves"] = [move]
        native_link = os.link
        def link(source, destination):
            result = native_link(source, destination)
            if destination == move["destination"]:
                manifest.write_bytes(b"another writer")
            return result
        with self.assertRaisesRegex(ValueError, "changed since scan"):
            self.batch.run_request(request, link=link)
        self.assertEqual(manifest.read_bytes(), b"another writer")
        self.assertTrue(Path(pair["backup"]).exists())
        self.assertTrue(Path(move["source"]).exists())
        self.assertFalse(Path(move["destination"]).exists())
        self.batch.assert_no_candidates()

    def test_checksum_cleanup_rollback_restores_exact_damaged_bytes(self):
        pair = self.pair()
        backup = Path(pair["backup"])
        backup.write_bytes(b"damaged before cleanup")
        pair["reason"] = "checksum"
        request = self.request(self.refresh_pair(pair))
        native_unlink = os.unlink
        def unlink(path):
            if path == pair["backup"] + ".json":
                raise OSError("cleanup interrupted")
            return native_unlink(path)
        with self.assertRaisesRegex(OSError, "cleanup interrupted"):
            self.batch.run_request(request, unlink=unlink)
        self.assertEqual(backup.read_bytes(), b"damaged before cleanup")
        self.assertTrue(Path(pair["backup"] + ".json").exists())
        self.batch.assert_no_candidates()


if __name__ == "__main__":
    unittest.main()
