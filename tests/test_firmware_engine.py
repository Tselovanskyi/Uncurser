"""Installed firmware stamps and automatic issues use local synthetic snapshots."""
import copy
import json
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing, storage
from tests import test_mod_versions, test_screws, test_smoothing_versions


CURRENT = "1.1.7.0"
DWIN = "printer hw ver:;DWIN sw ver:1.1.7.0;"


def stamp(current, records):
    data = mod_versions.dump(records)
    current.versions = engine.MeshSnapshot(data, mods.digest(data),
        {"known": True, "enabled": True, "patchable": True},
        original_hash=mods.digest(b""), original_absent=True)
    return current


def installed(firmware=None):
    current = test_mod_versions.installed_snapshot()
    current.status = {"native": {"model": "F008", "modelVersion": CURRENT}}
    records = {path: mod_versions.capture(path, original, engine.file_snapshot(current, path).data)
               for path, original in test_mod_versions.SOURCES.items()}
    if firmware is not None:
        for record in records.values():
            record["firmware"] = firmware
    return stamp(current, records)


def inactive():
    current = test_mod_versions.installed_snapshot()
    current.status = {"native": {"model": "F008", "modelVersion": DWIN}}
    for path, original in test_mod_versions.SOURCES.items():
        value = engine.file_snapshot(current, path)
        value.data, value.sha256 = original, mods.digest(original)
        value.analysis = {"known": True, "enabled": False, "patchable": True, "reason": ""}
    return current


class FirmwareEngineTests(unittest.TestCase):
    def test_missing_different_and_matching_stamps_are_distinguished_per_present_mod(self):
        current = installed()
        records = mod_versions.load(current.versions.data)
        records[mesh.TARGET]["firmware"] = "1.1.6.4"
        records[mesh_temperature.TARGET]["firmware"] = CURRENT
        stamp(current, records)
        current.status["native"]["modelVersion"] = DWIN
        issues = engine.firmware_issues(current)
        self.assertEqual(set(issues), {mods.TARGET, mesh.TARGET})
        self.assertIsNone(issues[mods.TARGET]["installed_firmware"])
        self.assertEqual(issues[mesh.TARGET]["installed_firmware"], "1.1.6.4")
        for value in issues.values():
            self.assertEqual(value["current_firmware"], CURRENT)
            self.assertTrue(value["firmware_reason"])
            for key in ("present", "available", "reason", "files"):
                self.assertIn(key, value)

    def test_clean_matching_unknown_and_wrong_model_states_do_not_raise_issues(self):
        self.assertEqual(engine.firmware_issues(inactive()), {})
        self.assertEqual(engine.firmware_issues(installed(CURRENT)), {})
        self.assertEqual(engine.firmware_issues(None), {})
        for native in ({}, {"model": "F009", "modelVersion": CURRENT},
                       {"model": "F008", "modelVersion": "unknown"},
                       {"model": "F008", "modelVersion": "1.1.5.0"}):
            with self.subTest(native=native):
                current = installed()
                current.status = {"native": native}
                self.assertEqual(engine.firmware_issues(current), {})

    def test_legacy_ledger_and_missing_canonical_storage_do_not_hide_missing_stamps(self):
        current = installed()
        ledger = current.versions.data
        baseline = storage.legacy_path(mod_versions.ORIGINAL)
        legacy = {storage.LEGACY_RECORDS: ledger, baseline: b"", baseline + ".json": json.dumps({
            "path": storage.LEGACY_RECORDS, "sha256": mods.digest(b""), "absent": True,
            "mode": 0o644, "uid": 0, "gid": 0}).encode()}
        class Reader:
            def read(self, path):
                data = legacy.get(path)
                return {"data": data, "sha256": mods.digest(data)} if data is not None else None
        remote = storage.Reader(Reader())
        current.versions = engine.scan_versions(remote)
        self.assertFalse(current.versions.backup_error)
        self.assertTrue(remote.moves)
        self.assertEqual(set(engine.firmware_issues(current)), set(test_mod_versions.SOURCES))
        self.assertEqual(mod_versions.load(current.versions.data), mod_versions.load(ledger))
        current.versions = None
        self.assertEqual(set(engine.firmware_issues(current)), set(test_mod_versions.SOURCES))

    def test_enabling_each_single_file_mod_stamps_canonical_current_firmware(self):
        revisions = copy.deepcopy(mod_versions.VERSIONS)
        current = inactive()
        prepared = engine.prepare_all(current, True, mesh_desired=True, temperature_desired=True)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(set(records), set(test_mod_versions.SOURCES))
        for path, record in records.items():
            self.assertEqual(record["firmware"], CURRENT)
            self.assertEqual(record["version"], revisions[path])
        self.assertEqual(mod_versions.VERSIONS, revisions)

    def test_true_update_stamps_only_the_requested_mod_and_preserves_other_records(self):
        current = installed("1.1.6.4")
        before = mod_versions.load(current.versions.data)
        current.status["native"]["modelVersion"] = DWIN
        prepared = engine.prepare_all(current, None, mesh_desired=True)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(records[mesh.TARGET]["firmware"], CURRENT)
        self.assertEqual(records[mesh.TARGET]["version"], before[mesh.TARGET]["version"])
        self.assertEqual(records[mods.TARGET], before[mods.TARGET])
        self.assertEqual(records[mesh_temperature.TARGET], before[mesh_temperature.TARGET])
        self.assertEqual(current.versions.data, mod_versions.dump(before))

    def test_unmodified_records_and_storage_migration_do_not_invent_stamps(self):
        for firmware in (None, "1.1.6.4"):
            with self.subTest(firmware=firmware):
                current = installed(firmware)
                before = current.versions.data
                current.storage_moves = ({"source": storage.LEGACY_RECORDS,
                    "destination": mod_versions.TARGET, "source_hash": mods.digest(before),
                    "destination_hash": None, "attrs": {"mode": 0o644, "uid": 0, "gid": 0}},)
                self.assertEqual(engine.prepare_all(current, None), {})
                self.assertEqual(current.versions.data, before)
                statuses = engine.update_statuses(current)
                for path in test_mod_versions.SOURCES:
                    self.assertFalse(statuses[path]["needed"])

    def test_removing_a_mod_drops_its_record_and_preserves_other_firmware_stamps(self):
        current = installed("1.1.6.4")
        before = mod_versions.load(current.versions.data)
        prepared = engine.prepare_all(current, None, mesh_desired=False)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertNotIn(mesh.TARGET, records)
        self.assertEqual(records, {path: record for path, record in before.items() if path != mesh.TARGET})

    def test_helper_and_homing_capture_current_firmware_without_revision_changes(self):
        revisions = copy.deepcopy(mod_versions.VERSIONS)
        homing = test_smoothing_versions.snapshot()
        homing.status = {"native": {"model": "F008", "modelVersion": DWIN}}
        prepared = engine.prepare_all(homing, None, smoothing_desired=True)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(records[smoothing.TARGET]["firmware"], CURRENT)
        helper = test_screws.snapshot()
        helper.status = {"native": {"model": "F008", "modelVersion": DWIN}}
        with patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_screws.SOURCE)), \
                patch.object(fluidd_screws, "FILES", tuple(fluidd_screws.FluiddFile(path, mods.digest(data))
                             for path, data in test_screws.FLUIDD_SOURCES.items())):
            prepared = engine.prepare_all(helper, None, screws_desired=True)
        records = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(records[screws.PYTHON]["firmware"], CURRENT)
        self.assertEqual(records[screws.PYTHON]["version"], revisions[screws.PYTHON])
        self.assertEqual(mod_versions.VERSIONS, revisions)


if __name__ == "__main__":
    unittest.main()
