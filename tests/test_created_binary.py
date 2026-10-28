"""Added native-helper ownership and guarded local-batch checks; no printer I/O."""
import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import mod_versions, mods, pa
from app.remote import APPLY_BATCH
from tests import test_mesh


def library(payload=b"native helper"):
    header = bytearray(52)
    header[:7] = b"\x7fELF\x01\x01\x01"
    header[16:20] = b"\x03\x00\x28\x00"
    return bytes(header) + payload


class CreatedBinaryTests(unittest.TestCase):
    def record(self):
        before = {pa.TARGET: b"value = 1\n", pa.LIBRARY: None}
        after = {pa.TARGET: b"value = 2\n", pa.LIBRARY: library()}
        record = mod_versions.capture_files(pa.TARGET, before, after,
            {pa.TARGET: [("value = 1\n", "value = 2\n")]}, firmware="1.1.7.0")
        return before, after, record

    def test_record_is_data_only_and_restores_only_its_files(self):
        before, after, record = self.record()
        self.assertEqual(record["files"][pa.LIBRARY],
                         {"created_binary": mod_versions.binary_created(after[pa.LIBRARY])})
        self.assertNotIn("native helper", mod_versions.dump({pa.TARGET: record}).decode())
        self.assertEqual(mod_versions.undo_files(pa.TARGET, after, record), before)
        changed = dict(after, **{pa.TARGET: after[pa.TARGET] + b"unrelated = 3\n"})
        self.assertEqual(mod_versions.undo_files(pa.TARGET, changed, record)[pa.TARGET],
                         before[pa.TARGET] + b"unrelated = 3\n")

    def test_changed_helper_is_not_removed_and_missing_helper_stays_absent(self):
        before, after, record = self.record()
        with self.assertRaisesRegex(ValueError, "native helper differs"):
            mod_versions.undo_files(pa.TARGET, dict(after, **{pa.LIBRARY: library(b"other")}), record)
        self.assertEqual(mod_versions.undo_files(pa.TARGET, dict(after, **{pa.LIBRARY: None}), record), before)

    def test_unknown_mod_keeps_verified_native_uninstall_instructions(self):
        _, _, record = self.record()
        owner = "/usr/share/klipper/klippy/extras/retired_pa.py"
        self.assertEqual(mod_versions._unknown_scope(owner, record), (pa.TARGET, pa.LIBRARY))
        changed = copy.deepcopy(record)
        changed["files"][pa.TARGET] = changed["files"].pop(pa.LIBRARY)
        with self.assertRaisesRegex(ValueError, "native helper path"):
            mod_versions._unknown_scope(owner, changed)

    def test_bad_identity_and_existing_binary_size_changes_remain_blocked(self):
        for identity in ({"sha256": "0" * 64, "size": True}, {"sha256": "bad", "size": 100},
                         {"sha256": "0" * 64, "size": 2097153}):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                mod_versions.validate_created_binary(identity)
        with self.assertRaises(ValueError):
            mod_versions.binary_created(b"not an ELF")
        with self.assertRaisesRegex(ValueError, "preserve the file size"):
            mod_versions.binary_changes(library(), library(b"larger replacement"))


class AddedBinaryBatchTests(unittest.TestCase):
    setUp = test_mesh.BatchTests.setUp
    tearDown = test_mesh.BatchTests.tearDown

    def run_request(self, request, replace=None):
        script = APPLY_BATCH.replace(
            'ADDED_BINARIES={"/usr/share/klipper/klippy/chelper/uncurser_pa.so"}',
            "ADDED_BINARIES={" + repr(str(self.root / "uncurser_pa.so")) + "}")
        with patch.object(test_mesh, "APPLY_BATCH", script):
            return test_mesh.BatchTests.run_request(self, request, replace)

    def item(self, data, *, identity=None, previous=None):
        path = self.root / "uncurser_pa.so"
        backup = path.with_suffix(".sbn.Original.txt")
        before = path.read_bytes() if path.exists() else None
        item = dict(path=str(path), backup=str(backup), expected=mods.digest(before) if before is not None else None,
                    data=base64.b64encode(data).decode() if data is not None else None,
                    new_hash=mods.digest(data) if data is not None else None,
                    original_hash=mods.digest(b"") if backup.exists() else None,
                    original_absent=backup.exists(), may_capture_original=not backup.exists(),
                    reference="synthetic", restore=False, kind="binary",
                    created_binary=identity or mod_versions.binary_created(data if data is not None else before))
        if previous is not None:
            item["previous_created_binary"] = previous
        return item

    def test_create_update_and_remove_preserve_absence_proof(self):
        first = library()
        self.run_request({"files": [self.item(first)]})
        path = self.root / "uncurser_pa.so"
        backup = path.with_suffix(".sbn.Original.txt")
        manifest = Path(str(backup) + ".json")
        original_metadata = manifest.read_bytes()
        self.assertEqual(backup.read_bytes(), b"")
        self.assertTrue(json.loads(original_metadata)["absent"])
        second = library(b"new helper with a different file size")
        self.run_request({"files": [self.item(second, previous=mod_versions.binary_created(first))]})
        self.assertEqual(path.read_bytes(), second)
        self.assertEqual(manifest.read_bytes(), original_metadata)
        self.run_request({"files": [self.item(None)]})
        self.assertFalse(path.exists())
        self.assertEqual(manifest.read_bytes(), original_metadata)

    def test_delete_requires_proven_original_absence_and_exact_owned_hash(self):
        path = self.root / "uncurser_pa.so"
        path.write_bytes(library())
        with self.assertRaisesRegex(ValueError, "original absence proof"):
            self.run_request({"files": [self.item(None)]})
        path.unlink()
        first = library()
        self.run_request({"files": [self.item(first)]})
        path.write_bytes(library(b"user replacement"))
        with self.assertRaisesRegex(ValueError, "differs from its uninstall record"):
            self.run_request({"files": [self.item(None, identity=mod_versions.binary_created(first))]})
        self.assertEqual(path.read_bytes(), library(b"user replacement"))

    def test_full_restore_removes_corrupt_helper_from_verified_absence(self):
        first = library()
        self.run_request({"files": [self.item(first)]})
        path = self.root / "uncurser_pa.so"
        path.write_bytes(b"damaged non-ELF bytes")
        with self.assertRaisesRegex(ValueError, "differs from its uninstall record"):
            self.run_request({"files": [self.item(None, identity=mod_versions.binary_created(first))]})
        item = self.item(None, identity=mod_versions.binary_created(first))
        item.pop("created_binary")
        item["restore"] = True
        self.run_request({"files": [item]})
        self.assertFalse(path.exists())

    def test_full_restore_still_requires_intact_absence_backup(self):
        first = library()
        self.run_request({"files": [self.item(first)]})
        path = self.root / "uncurser_pa.so"
        path.write_bytes(b"damaged non-ELF bytes")
        item = self.item(None, identity=mod_versions.binary_created(first))
        item.pop("created_binary")
        item["restore"] = True
        Path(item["backup"]).chmod(0o600)
        Path(item["backup"]).write_bytes(b"damaged absence backup")
        with self.assertRaisesRegex(ValueError, "backup|Backup"):
            self.run_request({"files": [item]})
        self.assertEqual(path.read_bytes(), b"damaged non-ELF bytes")

    def test_unregistered_creation_and_unproven_replacement_are_blocked(self):
        item = self.item(library())
        item["path"] = str(self.root / "some_other.so")
        with self.assertRaisesRegex(ValueError, "Unregistered"):
            self.run_request({"files": [item]})
        self.run_request({"files": [self.item(library())]})
        with self.assertRaisesRegex(ValueError, "added-binary ownership"):
            self.run_request({"files": [self.item(library(b"new without previous identity"))]})

    def test_later_failure_rolls_back_created_library_and_other_working_files(self):
        import os
        source = self.py.read_bytes()
        request = test_mesh.BatchTests.request(self)
        request["files"].insert(0, self.item(library()))
        native_replace = os.replace

        def replace(before, after):
            if Path(after) == self.cfg:
                raise OSError("simulated later failure")
            return native_replace(before, after)

        with self.assertRaisesRegex(OSError, "later failure"):
            self.run_request(request, replace)
        self.assertFalse((self.root / "uncurser_pa.so").exists())
        self.assertEqual(self.py.read_bytes(), source)
        self.assertFalse(list(self.root.glob("*.uncursed-pending-*.txt")))


if __name__ == "__main__":
    unittest.main()
