"""Focused local batch and supplementary-comment regressions; no printer access."""
import base64
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mod_comments, mod_versions, mods, screws, smoothing, warmup
from app.remote import APPLY_BATCH, Connection


SOURCE = b"""class ControlBangBang:
    def temperature_update(self):
        if is_chamber_heater and heater_bed_state == 1:
            self.temp_coff = 0
            self.heater.start_heating_seconds = 0
"""
STATUS = {"info": {"state": "ready"}, "objects": {},
          "native": {"model": "F008", "modelVersion": "1.1.7.0", "deviceState": 0}}
OFF = {"known": True, "enabled": False, "patchable": True, "label": "Original"}


def snapshot():
    value = engine.Snapshot(None, None, dict(OFF), None, None, "", STATUS, "", {}, 0)
    value.warmup = engine.MeshSnapshot(SOURCE, mods.digest(SOURCE), warmup.inspect(SOURCE, SOURCE),
        backup_guards={warmup.ORIGINAL: None, warmup.MANIFEST: None})
    return value


def apply_locally(scanned, pending, *, restore=False, **choices):
    verified = copy.deepcopy(scanned)
    if mod_versions.TARGET in pending:
        data = pending[mod_versions.TARGET]
        verified.versions = engine.MeshSnapshot(data,
            mods.digest(data) if data is not None else None, dict(OFF))
    for path, data in pending.items():
        if path != mod_versions.TARGET:
            value = engine.file_snapshot(verified, path)
            value.data, value.sha256 = data, mods.digest(data) if data is not None else None
    with patch.object(engine, "rescan", side_effect=(copy.deepcopy(scanned), verified)), \
            patch.object(engine.network, "status", return_value=scanned.status), \
            patch.object(engine, "Remote") as remote:
        command = remote.return_value.__enter__.return_value.python
        command.side_effect = lambda _script, request: {"cleared": list(request["python_caches"])}
        result = engine.apply_many(Connection("192.0.2.1"), Path("unused"), scanned,
                                  None, None, restore, False, lambda _: None, **choices)
    command.assert_called_once()
    program, request = command.call_args.args
    assert program == APPLY_BATCH
    assert result["written"] and result["power_cycle"]
    return request


class WarmupApplyTests(unittest.TestCase):
    def installed(self, firmware):
        scanned = snapshot()
        scanned.status = copy.deepcopy(STATUS)
        scanned.status["native"]["modelVersion"] = firmware
        pending = engine.prepare_all(scanned, None, warmup_desired=True)
        scanned.warmup = engine.MeshSnapshot(pending[warmup.TARGET],
            mods.digest(pending[warmup.TARGET]), warmup.inspect(pending[warmup.TARGET], SOURCE),
            SOURCE, mods.digest(SOURCE), original_identity=("F008", firmware))
        data = pending[mod_versions.TARGET]
        scanned.versions = engine.MeshSnapshot(data, mods.digest(data), dict(OFF),
            original_hash=mods.digest(b""), original_absent=True)
        return scanned

    def test_full_restore_uses_python_backup_and_keeps_batch_guards(self):
        for firmware in ("1.1.6.4", "1.1.7.0"):
            with self.subTest(firmware=firmware):
                scanned = self.installed(firmware)
                extra = b"\n# unrelated edit after installation\n"
                scanned.warmup.data += extra
                scanned.warmup.sha256 = mods.digest(scanned.warmup.data)
                disabled = engine.prepare_all(scanned, None, warmup_desired=False)
                self.assertEqual(disabled[warmup.TARGET], SOURCE + extra)
                pending = engine.prepare_all(scanned, None, restore=True)
                self.assertEqual(pending[warmup.TARGET], SOURCE)
                self.assertIsNone(pending[mod_versions.TARGET])
                request = apply_locally(scanned, pending, restore=True)
                item, ledger = request["files"]
                self.assertEqual(item["path"], warmup.TARGET)
                self.assertEqual(item["kind"], "python")
                self.assertTrue(item["restore"])
                self.assertEqual(base64.b64decode(item["data"]), SOURCE)
                self.assertEqual(item["expected"], scanned.warmup.sha256)
                self.assertEqual(item["original_hash"], mods.digest(SOURCE))
                self.assertEqual(item["backup"], warmup.ORIGINAL)
                self.assertEqual(request["guards"][warmup.ORIGINAL], mods.digest(SOURCE))
                self.assertEqual(request["python_caches"], {warmup.TARGET: scanned.warmup.sha256})
                self.assertEqual(request["firmware"], firmware)
                self.assertEqual(ledger["path"], mod_versions.TARGET)
                self.assertIsNone(ledger["data"])

    def test_full_restore_excludes_cross_firmware_or_damaged_python_backup(self):
        for fault in ("firmware", "checksum"):
            with self.subTest(fault=fault):
                scanned = self.installed("1.1.7.0")
                if fault == "firmware":
                    scanned.warmup.original_identity = ("F008", "1.1.6.4")
                else:
                    scanned.warmup.backup_error = "Original backup checksum mismatch."
                self.assertNotIn(warmup.TARGET, engine.restorable(scanned))
                with self.assertRaisesRegex(ValueError, "No eligible on-printer original backups"):
                    engine.prepare_all(scanned, None, restore=True)

    def test_new_install_batches_python_headers_records_and_cache_guard(self):
        scanned = snapshot()
        pending = engine.prepare_all(scanned, None, warmup_desired=True)
        request = apply_locally(scanned, pending, warmup_desired=True)
        self.assertEqual([item["path"] for item in request["files"]], [warmup.TARGET, mod_versions.TARGET])
        item, ledger = request["files"]
        self.assertEqual(item["kind"], "python")
        self.assertEqual(item["expected"], mods.digest(SOURCE))
        self.assertEqual(item["backup"], warmup.ORIGINAL)
        self.assertIsNone(item["original_hash"])
        self.assertTrue(item["may_capture_original"])
        self.assertFalse(item["original_absent"])
        self.assertFalse(item["restore"])
        installed = base64.b64decode(item["data"])
        self.assertEqual(item["new_hash"], mods.digest(installed))
        stamp = {"version": mod_versions.VERSIONS[warmup.TARGET], "firmware": "1.1.7.0"}
        self.assertEqual(mod_comments.read(installed), {warmup.TARGET: stamp})
        self.assertEqual(mod_comments.strip(installed), warmup.transform(SOURCE, True))
        self.assertEqual(request["python_caches"], {warmup.TARGET: mods.digest(SOURCE)})
        self.assertEqual(request["model"], "F008")
        self.assertEqual(request["firmware"], "1.1.7.0")
        self.assertEqual(ledger["kind"], "json")
        self.assertIsNone(ledger["expected"])
        self.assertTrue(ledger["may_capture_original"])
        records = mod_versions.load(base64.b64decode(ledger["data"]))
        self.assertEqual(set(records), {warmup.TARGET})
        self.assertEqual(mod_versions.undo(installed, records[warmup.TARGET]), SOURCE)

    def test_missing_comment_is_optional_but_changed_code_or_metadata_is_not(self):
        installed = mod_comments.put(warmup.transform(SOURCE, True), warmup.TARGET,
            mod_versions.VERSIONS[warmup.TARGET], "1.1.7.0", warmup.TARGET)
        record = mod_versions.capture(warmup.TARGET, SOURCE, installed, firmware="1.1.7.0")
        uncommented = mod_comments.remove(installed, warmup.TARGET)
        self.assertEqual(mod_versions.undo(uncommented, record), SOURCE)
        for changed in (uncommented.replace(b"self.temp_coff = 0.5", b"self.temp_coff = 0.6"),
                        mod_comments.put(installed, warmup.TARGET, "0.1.15", "1.1.7.0", warmup.TARGET)):
            with self.assertRaises(ValueError):
                mod_versions.undo(changed, record)
        created = mod_comments.put(b"# helper\nvalue=1\n", screws.PYTHON, "0.1.16", "1.1.7.0", screws.PYTHON)
        bare = mod_comments.remove(created, screws.PYTHON)
        self.assertTrue(mod_versions.created_matches(bare, created.decode(), screws.PYTHON))
        self.assertFalse(mod_versions.created_matches(bare + b"# edit\n", created.decode(), screws.PYTHON))

    def test_partial_commented_homing_removal_keeps_missing_config_absent(self):
        from tests.test_smoothing import HOMING_SOURCE
        scanned = snapshot()
        installed = mod_comments.put(smoothing.HOMING.transform(HOMING_SOURCE, True), smoothing.TARGET,
            mod_versions.VERSIONS[smoothing.TARGET], "1.1.7.0", smoothing.HOMING.TARGET)
        scanned.smoothing = engine.MeshSnapshot(None, None, dict(OFF))
        scanned.homing = engine.MeshSnapshot(installed, mods.digest(installed),
            smoothing.HOMING.inspect(installed, HOMING_SOURCE), HOMING_SOURCE, mods.digest(HOMING_SOURCE),
            original_identity=("F008", "1.1.7.0"))
        pending = engine.prepare_all(scanned, None, smoothing_desired=False)
        self.assertIsNone(pending[smoothing.TARGET])
        self.assertEqual(pending[smoothing.HOMING.TARGET], HOMING_SOURCE)
        request = apply_locally(scanned, pending, smoothing_desired=False)
        self.assertNotIn(smoothing.TARGET, {item["path"] for item in request["files"]})
        self.assertEqual({item["path"] for item in request["files"]}, {smoothing.HOMING.TARGET})


if __name__ == "__main__":
    unittest.main()
