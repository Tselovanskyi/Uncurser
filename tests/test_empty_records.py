"""Missing ledger proofs must not leave cleanup unable to install mods again."""
import base64
import unittest

from app import engine, mesh, mod_versions, mods, storage
from tests import test_mod_versions, test_stock_apply, test_storage_batch


def snapshot(records=None):
    current = test_stock_apply.snapshot()
    if records is None:
        records = {mods.TARGET: mod_versions.capture(mods.TARGET,
            test_mod_versions.SOURCES[mods.TARGET], current.data)}
    data = mod_versions.dump(records)
    current.versions = engine.MeshSnapshot(data, mods.digest(data), {"known": False, "enabled": True},
        backup_error="The version records have no original absence proof.",
        backup_guards={mod_versions.ORIGINAL: None, mod_versions.MANIFEST: None})
    return current


class EmptyRecordTests(unittest.TestCase):
    def test_last_removal_deletes_unproven_ledger_and_reinstall_is_possible_after_scan(self):
        current = snapshot()
        pending = engine.prepare_all(current, False, mesh_desired=False, temperature_desired=False)
        self.assertIsNone(pending[mod_versions.TARGET])
        for path, data in pending.items():
            value = engine.file_snapshot(current, path)
            value.data, value.sha256 = data, mods.digest(data) if data is not None else None
            if path in test_mod_versions.SOURCES:
                value.analysis = {"known": True, "enabled": False, "patchable": True}
        class Reader:
            def read(self, path):
                return None
        current.versions = engine.scan_versions(Reader())
        self.assertEqual(engine.firmware_issues(current), {})
        self.assertIn(mods.TARGET, engine.prepare_all(current, True))

    def test_partial_removal_preserves_other_records_without_requiring_ledger_proofs(self):
        current = snapshot()
        records = mod_versions.load(current.versions.data)
        path = next(path for path in test_mod_versions.SOURCES if path != mods.TARGET)
        records[path] = mod_versions.capture(path, test_mod_versions.SOURCES[path],
                                            engine.file_snapshot(current, path).data)
        current = snapshot(records)
        pending = engine.prepare_all(current, False)
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET]), {path: records[path]})
        self.assertEqual(engine.installed_records(current), records)

    def test_empty_valid_shared_records_are_healthy_without_original_proofs(self):
        before = snapshot({})
        self.assertTrue(engine.missing_record_proof(before))
        self.assertNotIn(storage.ROOT, engine.firmware_issues(before))
        self.assertEqual(engine.installed_records(before), {})
        self.assertEqual(engine.prepare_all(before, None), {})

    def test_partial_proof_or_invalid_ledger_is_never_treated_as_empty_cleanup(self):
        for fault in ("proof", "checksum", "schema"):
            with self.subTest(fault=fault):
                current = snapshot({})
                if fault == "proof":
                    current.versions.backup_guards[mod_versions.MANIFEST] = "0" * 64
                elif fault == "checksum":
                    current.versions.sha256 = "0" * 64
                else:
                    current.versions.data = b'{}'
                    current.versions.sha256 = mods.digest(current.versions.data)
                with self.assertRaises(ValueError):
                    engine.installed_records(current)
                self.assertNotIn(mod_versions.TARGET, engine.prepare_all(current, None))

    def test_scanned_valid_shared_records_do_not_need_an_original_absence_proof(self):
        ledger = mod_versions.dump({})
        class Reader:
            def read(self, path):
                return {"data": ledger, "sha256": mods.digest(ledger)} if path == mod_versions.TARGET else None
        scanned = engine.scan_versions(Reader())
        self.assertTrue(scanned.analysis["known"])
        self.assertFalse(scanned.backup_error)
        self.assertFalse(scanned.original_absent)
        self.assertIsNone(scanned.original_hash)


class EmptyRecordBatchTests(unittest.TestCase):
    def setUp(self):
        self.batch = test_storage_batch.StorageBatchTests()
        self.batch.setUp()
        self.addCleanup(self.batch.tearDown)
        self.batch.program = self.batch.program.replace(
            'ROOT+"/recovery"+stem+markers[suffix]+"."+item["expected"]+".txt"',
            'os.path.join(ROOT,"recovery",os.path.basename(stem)+markers[suffix]+"."+item["expected"]+".txt")')
        self.batch.storage.mkdir()
        self.path = self.batch.storage / "Uncurser_mods.json"

    def request(self, records):
        self.before = mod_versions.dump(records)
        self.path.write_bytes(self.before)
        digest = mods.digest(self.before)
        self.backup = self.batch.storage / "recovery" / ("Uncurser_mods.jsn." + digest + ".txt")
        return {"files": [{"path": str(self.path), "backup": str(self.backup),
            "expected": digest, "data": None, "new_hash": None, "original_hash": None,
            "original_absent": False, "may_capture_original": False,
            "reference": "fixture", "restore": False, "kind": "json", "recovery": "records"}]}

    def test_deletion_cleans_recovery_copy_without_inventing_an_original_proof(self):
        request = self.request({})
        result = self.batch.run_request(request)
        self.assertEqual(result["saved"], [str(self.path)])
        self.assertFalse(self.path.exists())
        self.assertFalse(self.backup.exists())
        self.assertIn(str(self.backup), result["recovery_removed"])
        self.assertFalse(list(self.batch.root.rglob("*.Original.txt*")))
        self.batch.assert_no_candidates()

    def test_late_failure_rolls_back_ledger_deletion(self):
        import os
        request = self.request({})
        guard = self.batch.root / "guard.txt"
        guard.write_bytes(b"before")
        request["guards"] = {str(guard): mods.digest(b"before")}
        native_unlink = os.unlink
        def unlink(path, *args, **kwargs):
            native_unlink(path, *args, **kwargs)
            if path == str(self.path):
                guard.write_bytes(b"changed after deletion")
        with self.assertRaisesRegex(ValueError, "Included configuration changed"):
            self.batch.run_request(request, unlink=unlink)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.batch.assert_no_candidates()

    def update_request(self):
        records = {path: mod_versions.capture(path, test_mod_versions.SOURCES[path],
                   mod_versions.enabled(path, test_mod_versions.SOURCES[path]), firmware="1.1.7.0")
                   for path in (mods.TARGET, mesh.TARGET)}
        request = self.request({mods.TARGET: records[mods.TARGET]})
        after = mod_versions.dump(records)
        request["files"][0].update(data=base64.b64encode(after).decode(), new_hash=mods.digest(after),
                                  record_targets=[mesh.TARGET])
        return request, records

    def test_update_adds_only_new_mod_and_cleans_recovery_without_creating_proofs(self):
        request, records = self.update_request()
        self.batch.run_request(request)
        self.assertEqual(mod_versions.load(self.path.read_bytes()), records)
        self.assertFalse(self.backup.exists())
        self.assertFalse(list(self.batch.root.rglob("*.Original.txt*")))
        self.batch.assert_no_candidates()

    def test_update_rejects_changes_to_unselected_records(self):
        request, records = self.update_request()
        records[mods.TARGET]["version"] = "0.1.0"
        after = mod_versions.dump(records)
        request["files"][0].update(data=base64.b64encode(after).decode(), new_hash=mods.digest(after))
        with self.assertRaisesRegex(ValueError, "unrelated mod"):
            self.batch.run_request(request)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.batch.assert_no_candidates()

    def test_proof_appearing_during_update_rolls_back_shared_records(self):
        import os
        request, _ = self.update_request()
        proof = self.batch.storage / "originals" / "proof.json"
        proof.parent.mkdir()
        request["guards"] = {str(proof): None}
        native_replace = os.replace
        def replace(source, destination):
            native_replace(source, destination)
            if destination == str(self.path):
                proof.write_bytes(b"concurrent metadata")
        with self.assertRaisesRegex(ValueError, "Included configuration changed"):
            self.batch.run_request(request, replace=replace)
        self.assertEqual(self.path.read_bytes(), self.before)
        self.batch.assert_no_candidates()
