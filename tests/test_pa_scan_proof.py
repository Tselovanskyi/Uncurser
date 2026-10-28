"""PA absence proofs must avoid needless ROM reads without hiding bad backups."""
import json
import unittest
from unittest.mock import patch

from app import engine, mod_versions, mods, pa, storage
from tests import test_scan
from tests.test_pa_apply import DEPENDENCIES, SOURCE
from tests.test_mod_versions import STATUS
from tests.test_stock_recovery import stock
from tests.test_warmup_apply import apply_locally


def recovery_defaults():
    result = stock({pa.TARGET: SOURCE, pa.LIBRARY: None, **DEPENDENCIES})
    result["identity"]["firmware"] = engine.network.device_identity(STATUS)[1]
    result["proof"] = {"verified": True}
    return result


class PAScanProofTests(unittest.TestCase):
    def setUp(self):
        self.scan_fixture = test_scan.ScanTests()
        self.scan_fixture.setUp()
        self.addCleanup(self.scan_fixture.doCleanups)
        self.enterContext(patch.object(pa, "REFERENCE_HASH", mods.digest(SOURCE)))
        self.enterContext(patch.object(pa, "validate_dependencies", return_value=None))
        self.enterContext(patch.object(engine.network, "status", return_value={
            "info": {"state": "ready"}, "objects": {}, "native": {**STATUS["native"], "deviceState": 0}}))
        self.stock_scan = self.enterContext(patch.object(engine.stock, "scan", return_value={
            "available": False, "reason": "fixture"}))
        self.files = self.scan_fixture.files
        firmware = engine.network.device_identity(STATUS)[1]
        records = mod_versions.load(self.files[mod_versions.TARGET])
        for entry in records.values():
            entry["firmware"] = firmware
        enabled = {pa.TARGET: pa.transform(SOURCE, True), pa.LIBRARY: pa.helper_data()}
        records[pa.TARGET] = engine.capture_pa({pa.TARGET: SOURCE, pa.LIBRARY: None},
                                              enabled, firmware=firmware)
        self.files[mod_versions.TARGET] = mod_versions.dump(records)
        self.files.update(enabled)
        self.files.update(DEPENDENCIES)
        for path, data in ((pa.TARGET, SOURCE), (pa.LIBRARY, b"")):
            backup = storage.original(path)
            self.files[backup] = data
            self.files[backup + ".json"] = json.dumps(dict(path=path, sha256=mods.digest(data),
                absent=path == pa.LIBRARY, mode=0o644, uid=0, gid=0,
                model="F008", firmware=firmware)).encode()

    def test_valid_added_library_absence_proof_skips_stock_reads(self):
        result = self.scan_fixture.scan()
        self.assertTrue(result.pa_library.original_absent)
        self.assertEqual(result.pa_library.original_hash, mods.digest(b""))
        self.assertEqual(result.pa_library.backup_error, "")
        self.assertTrue(result.pa.analysis["enabled"])
        self.stock_scan.assert_not_called()
        self.assertIsNone(result.stock)

    def test_incomplete_damaged_or_wrong_identity_absence_proofs_still_read_stock(self):
        backup = storage.original(pa.LIBRARY)
        pristine = dict(self.files)
        for fault in ("backup missing", "manifest missing", "checksum", "attributes",
                      "firmware missing", "older firmware", "different model"):
            with self.subTest(fault=fault):
                self.files.clear()
                self.files.update(pristine)
                self.stock_scan.reset_mock()
                if fault == "backup missing":
                    del self.files[backup]
                elif fault == "manifest missing":
                    del self.files[backup + ".json"]
                elif fault == "checksum":
                    self.files[backup] = b"damaged backup"
                else:
                    meta = json.loads(self.files[backup + ".json"])
                    if fault == "attributes":
                        del meta["uid"]
                    elif fault == "firmware missing":
                        del meta["firmware"]
                    elif fault == "older firmware":
                        meta["firmware"] = "1.1.6.3"
                    else:
                        meta["model"] = "F009"
                    self.files[backup + ".json"] = json.dumps(meta).encode()
                self.scan_fixture.scan()
                self.stock_scan.assert_called_once()

    def test_restored_source_loads_recovery_and_offers_verified_reinstall(self):
        self.files[pa.TARGET] = SOURCE
        self.stock_scan.return_value = recovery_defaults()
        result = self.scan_fixture.scan()
        self.stock_scan.assert_called_once()
        issue = engine.firmware_issues(result)[pa.TARGET]
        self.assertTrue(issue["incomplete"])
        self.assertTrue(issue["available"], issue)
        self.assertTrue(issue["reinstall"], issue)

    def test_missing_original_pair_is_an_issue_even_with_complete_current_code(self):
        pristine = dict(self.files)
        for path in pa.FILES:
            with self.subTest(path=path):
                self.files.clear()
                self.files.update(pristine)
                backup = storage.original(path)
                del self.files[backup]
                del self.files[backup + ".json"]
                self.stock_scan.reset_mock()
                self.stock_scan.return_value = recovery_defaults()
                result = self.scan_fixture.scan()
                self.stock_scan.assert_called_once()
                issue = engine.firmware_issues(result)[pa.TARGET]
                self.assertFalse(issue["incomplete"], issue)
                self.assertTrue(issue["available"], issue)
                self.assertTrue(issue["reinstall"], issue)
                self.assertEqual(issue["firmware_reason"], "Its original backup or absence proof is missing.")

    def test_unknown_clean_baseline_offers_removal_without_promising_reinstall(self):
        extra = b"\n# unrelated user edit\n"
        self.files[pa.TARGET] += extra
        backup = storage.original(pa.TARGET)
        del self.files[backup]
        del self.files[backup + ".json"]
        self.stock_scan.return_value = recovery_defaults()
        result = self.scan_fixture.scan()
        issue = engine.firmware_issues(result)[pa.TARGET]
        self.assertTrue(issue["available"], issue)
        self.assertFalse(issue["reinstall"], issue)
        pending = engine.prepare_all(result, None, pa_desired=False)
        self.assertEqual(pending[pa.TARGET], SOURCE + extra)

    def test_valid_current_original_allows_reinstall_of_custom_baseline(self):
        extra = b"\n# original user customization\n"
        backup = storage.original(pa.TARGET)
        self.files[pa.TARGET] += extra
        self.files[backup] = SOURCE + extra
        meta = json.loads(self.files[backup + ".json"])
        meta["sha256"] = mods.digest(SOURCE + extra)
        self.files[backup + ".json"] = json.dumps(meta).encode()
        records = mod_versions.load(self.files[mod_versions.TARGET])
        del records[pa.TARGET]["firmware"]
        self.files[mod_versions.TARGET] = mod_versions.dump(records)
        self.stock_scan.return_value = recovery_defaults()
        result = self.scan_fixture.scan()
        issue = engine.firmware_issues(result)[pa.TARGET]
        self.assertTrue(issue["available"], issue)
        self.assertTrue(issue["reinstall"], issue)

    def test_interrupted_install_recovers_helper_without_changing_other_records(self):
        records = mod_versions.load(self.files[mod_versions.TARGET])
        del records[pa.TARGET]
        self.files[mod_versions.TARGET] = mod_versions.dump(records)
        self.files[pa.TARGET] = SOURCE
        self.stock_scan.return_value = recovery_defaults()
        result = self.scan_fixture.scan()
        self.stock_scan.assert_called_once()
        issue = engine.firmware_issues(result)[pa.TARGET]
        self.assertTrue(issue["available"], issue)
        self.assertTrue(issue["reinstall"], issue)
        pending = engine.prepare_all(result, None, pa_desired=False)
        self.assertEqual(pending, {pa.TARGET: SOURCE, pa.LIBRARY: None})
        request = apply_locally(result, pending, pa_desired=False)
        self.assertEqual([item["path"] for item in request["files"]], [pa.LIBRARY])
        self.assertEqual(engine.installed_records(result), records)

    def test_unknown_helper_or_damaged_ledger_remains_unresolved(self):
        pristine = dict(self.files)
        for fault in ("unknown helper", "damaged ledger"):
            with self.subTest(fault=fault):
                self.files.clear()
                self.files.update(pristine)
                self.stock_scan.reset_mock()
                records = mod_versions.load(self.files[mod_versions.TARGET])
                del records[pa.TARGET]
                self.files[mod_versions.TARGET] = mod_versions.dump(records)
                self.files[pa.TARGET] = SOURCE
                if fault == "unknown helper":
                    self.files[pa.LIBRARY] += b"unrecognized edit"
                else:
                    self.files[mod_versions.TARGET] = b"invalid ledger"
                self.stock_scan.return_value = recovery_defaults()
                result = self.scan_fixture.scan()
                self.stock_scan.assert_called_once()
                issue = engine.firmware_issues(result)[pa.TARGET]
                self.assertFalse(issue["available"], issue)
                self.assertFalse(issue["reinstall"], issue)
                with self.assertRaises(ValueError):
                    engine.prepare_all(result, None, pa_desired=False)


if __name__ == "__main__":
    unittest.main()
