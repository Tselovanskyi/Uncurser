"""Pure reinstall classification against small firmware and ownership fixtures."""
import base64
import copy
import json
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, reinstall, screws, smoothing
from tests import test_mod_versions, test_screws, test_smoothing_versions, test_stock_apply


def snapshot():
    current = test_stock_apply.snapshot()
    current.identity = {"history": True}
    helper = test_screws.snapshot(True, True, True)
    current.mesh, current.screws = helper.mesh, helper.screws
    homing = test_smoothing_versions.snapshot(True)
    current.smoothing, current.homing = homing.smoothing, homing.homing
    sources = {**test_mod_versions.SOURCES, **test_smoothing_versions.originals(),
               **test_screws.FLUIDD_SOURCES, screws.PYTHON: None, screws.CONFIG: None,
               screws.LEGACY_CONFIG: None,
               fluidd_screws.INDEX: ('<script type="module" src="./' + fluidd_screws.ASSET + '"></script>').encode()}
    for path, data in sources.items():
        source = "/rom" + path
        digest = mods.digest(data) if data is not None else None
        current.stock["files"][path] = dict(data=data, sha256=digest, source=source, available=True, absent=data is None)
        current.stock["guards"][source] = digest
    baseline = {mesh.TARGET: screws.edit_include(current.mesh.data, False), screws.PYTHON: None,
                screws.CONFIG: None, fluidd_screws.TARGET: test_screws.FLUIDD_SOURCES[fluidd_screws.TARGET]}
    enabled = {path: engine.file_snapshot(current, path).data for path in mod_versions.FILES[screws.PYTHON]}
    records = {screws.PYTHON: engine.capture_helper(baseline, enabled),
               smoothing.TARGET: engine.capture_homing(test_smoothing_versions.originals(),
                   {path: engine.file_snapshot(current, path).data for path in mod_versions.FILES[smoothing.TARGET]})}
    ledger = mod_versions.dump(records)
    current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger), {"known": True, "enabled": True},
                                          original_hash=mods.digest(b""), original_absent=True)
    # Like the saved OTA case, the temperature mod was reset and the helper's
    # missing config prevented the original include-tree analysis.
    current.mesh_temperature.data = test_mod_versions.SOURCES[mesh_temperature.TARGET]
    current.mesh_temperature.sha256 = mods.digest(current.mesh_temperature.data)
    current.screws.files[screws.CONFIG].data = None
    current.screws.files[screws.CONFIG].sha256 = None
    for value in (current.mesh, current.mesh_temperature, current.smoothing):
        value.analysis = {"known": False, "enabled": None, "patchable": False,
                          "include_error": "Missing helper config"}
    return current


class ReinstallCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(mods, "REFERENCE_HASH", mods.digest(test_mod_versions.SOURCES[mods.TARGET])),
                        patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_screws.SOURCE)),
                        patch.object(mesh_temperature, "REFERENCE_HASH", mesh_temperature.fingerprint(test_mod_versions.SOURCES[mesh_temperature.TARGET])),
                        patch.object(smoothing, "REFERENCE_HASH", smoothing.fingerprint(test_smoothing_versions.originals()[smoothing.TARGET])),
                        patch.object(type(smoothing.HOMING), "REFERENCE_HASH", smoothing.HOMING.fingerprint(test_smoothing_versions.originals()[smoothing.HOMING.TARGET])),
                        patch.object(fluidd_screws, "FILES", tuple(fluidd_screws.FluiddFile(path, mods.digest(data))
                                     for path, data in test_screws.FLUIDD_SOURCES.items()))]
        for active in self.patches:
            active.start()
            self.addCleanup(active.stop)

    def test_partial_ota_mods_classify_from_cleaned_bytes_without_changing_snapshot(self):
        current = snapshot()
        before = copy.deepcopy(current)
        issues = engine.firmware_issues(current)
        expected = {mods.TARGET, mesh.TARGET, screws.PYTHON, smoothing.TARGET}
        self.assertEqual(set(issues), expected)
        self.assertEqual(reinstall.eligible(current, issues), expected)
        self.assertEqual(current, before)
        self.assertNotIn(mesh_temperature.TARGET, reinstall.eligible(current, issues))

    def test_missing_or_different_current_firmware_proof_blocks_all(self):
        current = snapshot()
        issues = engine.firmware_issues(current)
        for field, value in (("proof", {}), ("available", False), ("identity", {"model": "F008", "firmware": "1.1.6.4"})):
            with self.subTest(field=field):
                changed = copy.deepcopy(current)
                changed.stock[field] = value
                self.assertEqual(reinstall.eligible(changed, issues), set())

    def test_changed_native_homing_anchor_excludes_only_homing(self):
        current = snapshot()
        issues = engine.firmware_issues(current)
        entry = current.stock["files"][smoothing.HOMING.TARGET]
        entry["data"] = entry["data"].replace(b"hi.speed*4", b"hi.speed*5")
        entry["sha256"] = current.stock["guards"][entry["source"]] = mods.digest(entry["data"])
        expected = {mods.TARGET, mesh.TARGET, screws.PYTHON}
        self.assertEqual(reinstall.eligible(current, issues), expected)

    def test_missing_frontend_proof_excludes_only_helper(self):
        current = snapshot()
        issues = engine.firmware_issues(current)
        del current.stock["guards"][current.stock["files"][fluidd_screws.TARGET]["source"]]
        self.assertEqual(reinstall.eligible(current, issues), {mods.TARGET, mesh.TARGET, smoothing.TARGET})

    def test_new_native_helper_file_is_never_overwritten(self):
        current = snapshot()
        issues = engine.firmware_issues(current)
        entry = current.stock["files"][screws.CONFIG]
        entry.update(data=b"[native_helper]\n", sha256=mods.digest(b"[native_helper]\n"), absent=False)
        current.stock["guards"][entry["source"]] = entry["sha256"]
        self.assertNotIn(screws.PYTHON, reinstall.eligible(current, issues))

    def test_unavailable_unrelated_issue_is_ignored_but_failed_cleanup_blocks_reinstall(self):
        current = snapshot()
        issues = engine.firmware_issues(current)
        blocked = copy.deepcopy(issues)
        blocked[mods.TARGET]["available"] = False
        self.assertEqual(reinstall.eligible(current, blocked), {mesh.TARGET, screws.PYTHON, smoothing.TARGET})
        with patch.object(engine, "prepare_removal", side_effect=ValueError("Ownership conflict")):
            self.assertEqual(reinstall.eligible(current, issues), set())

    def test_unrecognized_clean_code_does_not_use_full_risk(self):
        current = snapshot()
        current.data += b"\n# unrelated source edit\n"
        current.sha256 = mods.digest(current.data)
        issues = engine.firmware_issues(current)
        self.assertNotIn(mods.TARGET, reinstall.eligible(current, issues))

    def test_matching_rom_bytes_do_not_substitute_for_normal_working_file_recognition(self):
        current = snapshot()
        value = current.homing
        value.data += b"\n# native change not recognized by the current app\n"
        value.sha256 = mods.digest(value.data)
        entry = current.stock["files"][smoothing.HOMING.TARGET]
        entry["data"] += b"\n# native change not recognized by the current app\n"
        entry["sha256"] = current.stock["guards"][entry["source"]] = mods.digest(entry["data"])
        self.assertNotIn(smoothing.TARGET, reinstall.eligible(current, engine.firmware_issues(current)))

    def test_helper_reinstall_preserves_a_retained_mesh_mod_and_its_record(self):
        current = snapshot()
        records = engine.installed_records(current)
        records[mods.TARGET] = mod_versions.capture(mods.TARGET, test_mod_versions.SOURCES[mods.TARGET], current.data)
        records[mesh.TARGET] = mod_versions.capture(mesh.TARGET, test_screws.SOURCE,
                                                   mesh.transform(test_screws.SOURCE, True), firmware="1.1.7.0")
        for path, record in records.items():
            if path != screws.PYTHON:
                record["firmware"] = "1.1.7.0"
        current.original = test_mod_versions.SOURCES[mods.TARGET]
        current.original_hash = mods.digest(current.original)
        current.original_identity = ("F008", "1.1.7.0")
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        issues = {screws.PYTHON: engine.firmware_issues(current)[screws.PYTHON]}
        self.assertEqual(reinstall.eligible(current, issues), {screws.PYTHON})
        self.assertEqual(engine.installed_records(current)[mesh.TARGET], records[mesh.TARGET])
        # The shared app records survive cleanup even when their own absence
        # proof was lost. That must not turn a clean reinstall into an update.
        current.versions.original_hash = None
        current.versions.original_absent = False
        current.versions.backup_guards = {mod_versions.ORIGINAL: None, mod_versions.MANIFEST: None}
        self.assertTrue(engine.firmware_issues(current)[screws.PYTHON]["reinstall"])
        pending = engine.prepare_removal(current, {screws.PYTHON: False})
        self.assertEqual(mod_versions.load(pending[mod_versions.TARGET]),
                         {path: record for path, record in records.items() if path != screws.PYTHON})

    def test_missing_native_history_excludes_only_early_heating(self):
        current = snapshot()
        current.identity = {}
        issues = engine.firmware_issues(current)
        self.assertEqual(reinstall.eligible(current, issues), {mesh.TARGET, screws.PYTHON, smoothing.TARGET})
        self.assertFalse(issues[mods.TARGET]["reinstall"])

    def test_retained_old_or_unversioned_mesh_is_preserved_during_helper_reinstall(self):
        for revision in (None, "0.1.6", "0.1.8"):
            with self.subTest(revision=revision):
                current = snapshot()
                records = engine.installed_records(current)
                records[mods.TARGET] = mod_versions.capture(mods.TARGET, test_mod_versions.SOURCES[mods.TARGET], current.data,
                                                           firmware="1.1.7.0")
                records[smoothing.TARGET]["firmware"] = "1.1.7.0"
                if revision is not None:
                    records[mesh.TARGET] = mod_versions.capture(mesh.TARGET, test_screws.SOURCE,
                                                               mesh.transform(test_screws.SOURCE, True), firmware="1.1.7.0")
                    records[mesh.TARGET]["version"] = revision
                current.versions.data = mod_versions.dump(records)
                current.versions.sha256 = mods.digest(current.versions.data)
                issues = {screws.PYTHON: engine.firmware_issues(current)[screws.PYTHON]}
                self.assertEqual(reinstall.eligible(current, issues), {screws.PYTHON})

    def test_unrelated_missing_backups_unknown_source_and_incomplete_homing_do_not_block_helper(self):
        for fault in ("missing backup", "unknown source", "missing homing half"):
            with self.subTest(fault=fault):
                current = snapshot()
                records = engine.installed_records(current)
                records[mods.TARGET] = mod_versions.capture(mods.TARGET, test_mod_versions.SOURCES[mods.TARGET], current.data,
                                                           firmware="1.1.7.0")
                records[mesh.TARGET] = mod_versions.capture(mesh.TARGET, test_screws.SOURCE,
                                                           mesh.transform(test_screws.SOURCE, True), firmware="1.1.7.0")
                records[smoothing.TARGET]["firmware"] = "1.1.7.0"
                current.original = test_mod_versions.SOURCES[mods.TARGET]
                current.original_hash = mods.digest(current.original)
                current.versions.data = mod_versions.dump(records)
                current.versions.sha256 = mods.digest(current.versions.data)
                if fault == "missing backup":
                    current.homing.original = current.homing.original_hash = None
                    current.homing.backup_guards = {smoothing.HOMING.ORIGINAL: None, smoothing.HOMING.MANIFEST: None}
                elif fault == "unknown source":
                    current.data += b"\n# user source edit\n"
                    current.sha256 = mods.digest(current.data)
                else:
                    current.homing.data = test_smoothing_versions.originals()[smoothing.HOMING.TARGET]
                    current.homing.sha256 = mods.digest(current.homing.data)
                issues = {screws.PYTHON: engine.firmware_issues(current)[screws.PYTHON]}
                self.assertEqual(reinstall.eligible(current, issues), {screws.PYTHON})

    def test_shared_helper_record_only_requires_its_overlapping_config_entry(self):
        current = snapshot()
        cfg = current.screws.files[screws.CONFIG]
        cfg.data, cfg.sha256 = screws.payload(screws.CONFIG), mods.digest(screws.payload(screws.CONFIG))
        records = engine.installed_records(current)
        records[screws.PYTHON]["firmware"] = "1.1.7.0"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        interface = current.screws.frontend[fluidd_screws.TARGET]
        interface.data = interface.sha256 = interface.original = interface.original_hash = None
        interface.backup_error = "An unrelated helper-interface backup is missing."
        issues = {mesh.TARGET: engine.firmware_issues(current)[mesh.TARGET]}
        self.assertEqual(reinstall.eligible(current, issues), {mesh.TARGET})
        self.assertEqual(engine.installed_records(current)[screws.PYTHON], records[screws.PYTHON])

    def test_conflicting_shared_undo_entry_blocks_only_the_mod_changing_that_file(self):
        current = snapshot()
        cfg = current.screws.files[screws.CONFIG]
        cfg.data, cfg.sha256 = screws.payload(screws.CONFIG), mods.digest(screws.payload(screws.CONFIG))
        records = engine.installed_records(current)
        records[screws.PYTHON]["firmware"] = "1.1.7.0"
        records[screws.PYTHON]["files"][mesh.TARGET]["undo"][0]["after"] = "speed: 666\n"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        issues = engine.firmware_issues(current)
        selected = {path: issues[path] for path in (mods.TARGET, mesh.TARGET)}
        self.assertEqual(reinstall.eligible(current, selected), {mods.TARGET})

    def test_inactive_orphan_helper_config_is_not_a_bed_mesh_dependency(self):
        current = snapshot()
        cfg = current.screws.files[screws.CONFIG]
        cfg.data, cfg.sha256 = screws.payload(screws.CONFIG), mods.digest(screws.payload(screws.CONFIG))
        orphan = current.screws.files[screws.LEGACY_CONFIG]
        orphan.data = b"Unrelated inactive config with invalid syntax.\n"
        orphan.sha256 = mods.digest(orphan.data)
        records = engine.installed_records(current)
        records[screws.PYTHON]["firmware"] = "1.1.7.0"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        issues = {mesh.TARGET: engine.firmware_issues(current)[mesh.TARGET]}
        self.assertEqual(reinstall.eligible(current, issues), {mesh.TARGET})

    def test_failed_dependency_read_uses_only_a_verified_nonconflicting_tree_copy(self):
        data = test_mod_versions.SOURCES[mesh_temperature.TARGET]
        cases = ((data, mods.digest(data), True),
                 (data, "invalid checksum", False),
                 (data + b"\n[gcode_macro _HOME_Z]\ngcode:\n  G4 P0\n",
                  None, False),
                 (None, None, False))
        for included, digest, expected in cases:
            with self.subTest(included=included, digest=digest):
                current = snapshot()
                class Reader:
                    def read(self, path):
                        raise OSError("The independent read failed.")
                current.mesh_temperature = engine.scan_config(Reader(), mesh_temperature, {})
                if included is not None:
                    current.mesh.included[mesh_temperature.TARGET] = included
                    current.mesh.guards[mesh_temperature.TARGET] = digest or mods.digest(included)
                unchanged = copy.deepcopy(current)
                issues = {smoothing.TARGET: engine.firmware_issues(current)[smoothing.TARGET]}
                self.assertEqual(smoothing.TARGET in reinstall.eligible(current, issues), expected)
                self.assertEqual(current, unchanged)

    def test_failed_owned_file_read_cannot_use_the_tree_copy_for_reinstall(self):
        current = snapshot()
        current.mesh.included[smoothing.TARGET] = current.smoothing.data
        current.mesh.guards[smoothing.TARGET] = current.smoothing.sha256
        current.smoothing = engine.MeshSnapshot(b"", "", {"known": False, "enabled": None,
            "patchable": False, "label": "Unavailable", "reason": "The independent read failed."})
        issues = {smoothing.TARGET: {"present": True, "available": True}}
        self.assertNotIn(smoothing.TARGET, reinstall.eligible(current, issues))

    def test_full_included_configuration_is_preserved_and_checked_after_virtual_cleanup(self):
        extra = "/mnt/UDISK/printer_data/config/printer_params.cfg"
        cases = ((b"[bed_mesh]\nspeed: 321\n", {mesh.TARGET, screws.PYTHON}),
                 (b"[screws_tilt_adjust]\nscrew1: 1,1\n", {screws.PYTHON}),
                 (b"[gcode_macro _HOME_Z]\ngcode:\n  G4 P0\n", {smoothing.TARGET}),
                 (b"[gcode_macro USER_MACRO]\ngcode:\n  G4 P0\n#*# unrecognized generated marker\n",
                  {mesh.TARGET, screws.PYTHON, smoothing.TARGET}),
                 (b"[gcode_macro USER_MACRO]\ngcode:\n  G4 P0\n", set()))
        for data, blocked in cases:
            with self.subTest(extra=data):
                current = snapshot()
                prefix = b"[include printer_params.cfg]\n"
                native = prefix + test_screws.SOURCE
                current.mesh.data = screws.edit_include(prefix + screws.edit_include(current.mesh.data, False), True)
                current.mesh.sha256 = mods.digest(current.mesh.data)
                current.mesh.original, current.mesh.original_hash = native, mods.digest(native)
                cfg = current.screws.files[screws.CONFIG]
                cfg.data, cfg.sha256 = screws.payload(screws.CONFIG), mods.digest(screws.payload(screws.CONFIG))
                source = current.stock["files"][mesh.TARGET]
                source.update(data=native, sha256=mods.digest(native))
                current.stock["guards"][source["source"]] = source["sha256"]
                captured = {mesh.TARGET: current.mesh.data, extra: data, screws.CONFIG: cfg.data,
                            mesh_temperature.TARGET: current.mesh_temperature.data, smoothing.TARGET: current.smoothing.data}
                saved = {mesh.TARGET: current.mesh.data, mesh.ORIGINAL: native,
                         mesh.MANIFEST: json.dumps(dict(path=mesh.TARGET, sha256=mods.digest(native),
                             mode=0o644, uid=0, gid=0, model="F008", firmware="1.1.6.4")).encode()}
                class Reader:
                    def read(self, path):
                        value = saved.get(path)
                        return None if value is None else {"data": value, "sha256": mods.digest(value)}
                tree = {"files": {path: {"data": base64.b64encode(value).decode(), "sha256": mods.digest(value)}
                                   for path, value in captured.items()},
                        "patterns": {extra: [extra], screws.CONFIG: [screws.CONFIG]}}
                with patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(native)):
                    current.mesh = engine.scan_config(Reader(), mesh, tree)
                    self.assertEqual(current.mesh.included[extra], data)
                    unchanged = copy.deepcopy(current)
                    selected = reinstall.eligible(current, engine.firmware_issues(current))
                    self.assertEqual(current, unchanged)
                self.assertEqual(selected, {mods.TARGET, mesh.TARGET, screws.PYTHON, smoothing.TARGET} - blocked)
                self.assertEqual(current.mesh.included[extra], data)


if __name__ == "__main__":
    unittest.main()
