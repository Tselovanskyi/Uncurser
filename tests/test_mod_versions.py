"""Focused version/undo and restoration checks with synthetic local files."""
import base64
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mesh_temperature, mod_versions, mods, screws
from app.remote import Connection
from tests import test_mesh, test_mesh_temperature, test_mods, test_screws


SOURCES = {
    mods.TARGET: test_mods.SOURCE,
    mesh.TARGET: test_mesh.SOURCE,
    mesh_temperature.TARGET: test_mesh_temperature.SOURCE,
}
STATUS = {"native": {"model": "F008", "modelVersion": "1.1.6.4"}}


def installed_snapshot():
    files = {}
    for path, original in SOURCES.items():
        data = mod_versions.enabled(path, original)
        analysis = {"known": True, "enabled": True, "patchable": True,
                    "label": "Uncursed", "reason": ""}
        files[path] = engine.MeshSnapshot(data, mods.digest(data), analysis,
                                         original, mods.digest(original))
    heating = files[mods.TARGET]
    return engine.Snapshot(heating.data, heating.sha256, heating.analysis,
                           heating.original, heating.original_hash, "", STATUS, "",
                           {"history": True}, 0, files[mesh.TARGET],
                           files[mesh_temperature.TARGET])


def helper_ready_snapshot(stamped=False):
    current = installed_snapshot()
    current.screws = test_screws.snapshot().screws
    if stamped:
        records = {path: mod_versions.capture(path, original, engine.file_snapshot(current, path).data)
                   for path, original in SOURCES.items()}
        ledger = mod_versions.dump(records)
        current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {"known": True, "enabled": True, "patchable": True},
            original_hash=mods.digest(b""), original_absent=True)
    return current


class ModVersionTests(unittest.TestCase):
    def test_update_status_keeps_unstamped_installed_mods_visible_when_backups_block(self):
        current = installed_snapshot()
        statuses = engine.update_statuses(current)
        self.assertEqual(set(statuses), set(mod_versions.VERSIONS))
        for path in SOURCES:
            self.assertEqual(statuses[path], {"needed": True, "available": True, "reason": ""})
        self.assertFalse(statuses[screws.PYTHON]["needed"])
        self.assertEqual({path for path, status in statuses.items()
                          if status["needed"] and status["available"]}, set(SOURCES))
        for path in SOURCES:
            for fault in ("missing", "damaged"):
                with self.subTest(path=path, fault=fault):
                    broken = installed_snapshot()
                    value = engine.file_snapshot(broken, path)
                    if fault == "missing":
                        value.original = None
                    else:
                        value.backup_error = "checksum mismatch"
                    status = engine.update_statuses(broken)[path]
                    self.assertTrue(status["needed"])
                    self.assertFalse(status["available"])
                    self.assertIn("backup", status["reason"])
                    with self.assertRaisesRegex(ValueError, "Cannot update"):
                        engine.prepare_all(broken, None, update=True)
        helper = test_screws.snapshot(True, True)
        self.assertEqual(engine.update_statuses(helper)[screws.PYTHON],
                         {"needed": True, "available": True, "reason": ""})
        helper.screws.backup_error = "The installed helper is missing its initial on-printer backups."
        blocked = engine.update_statuses(helper)[screws.PYTHON]
        self.assertTrue(blocked["needed"])
        self.assertFalse(blocked["available"])
        self.assertIn("backups", blocked["reason"])

    def test_update_status_distinguishes_latest_older_and_conflicting_records(self):
        current = helper_ready_snapshot(stamped=True)
        for path in SOURCES:
            self.assertEqual(engine.update_statuses(current)[path],
                             {"needed": False, "available": False, "reason": ""})
        records = mod_versions.load(current.versions.data)
        records[mesh.TARGET]["version"] = "0.1.4"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        self.assertEqual(engine.update_statuses(current)[mesh.TARGET],
                         {"needed": True, "available": True, "reason": ""})
        unknown = copy.deepcopy(current)
        unknown.mesh.analysis["known"] = False
        unknown.mesh.analysis["reason"] = "Unrecognised manual changes"
        self.assertTrue(engine.update_statuses(unknown)[mesh.TARGET]["available"])
        with self.assertRaisesRegex(ValueError, "unknown files"):
            engine.prepare_all(unknown, None, update=True)
        updated = engine.prepare_all(unknown, None, update=True, full_risk=True)
        self.assertEqual(mod_versions.load(updated[mod_versions.TARGET])[mesh.TARGET]["version"], mod_versions.VERSIONS[mesh.TARGET])
        disabled = engine.prepare_all(unknown, None, mesh_desired=False, update=True, full_risk=True)
        self.assertEqual(disabled[mesh.TARGET], test_mesh.SOURCE)
        self.assertNotIn(mesh.TARGET, mod_versions.load(disabled[mod_versions.TARGET]))

        current.mesh.data = current.mesh.data.replace(b"speed: 600", b"speed: 601")
        current.mesh.sha256 = mods.digest(current.mesh.data)
        status = engine.update_statuses(current)[mesh.TARGET]
        self.assertTrue(status["needed"])
        self.assertFalse(status["available"])
        self.assertIn("differs from its undo record", status["reason"])
        with self.assertRaisesRegex(ValueError, "Cannot update printer.cfg"):
            engine.prepare_all(current, None, update=True)

        helper = test_screws.snapshot(True, True)
        record = engine.capture_helper(engine.helper_baseline(helper), engine.helper_current(helper))
        record["version"] = "0.1.5"
        ledger = mod_versions.dump({screws.PYTHON: record})
        helper.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {"known": True, "enabled": True, "patchable": True},
            original_hash=mods.digest(b""), original_absent=True)
        helper.screws.analysis.update(patchable=False, reason="Latest helper code needs review")
        with self.assertRaisesRegex(ValueError, "Cannot update screws_tilt_adjust.py"):
            engine.prepare_all(helper, None, update=True)
        # Explicitly turning a blocked update off still uses its valid saved undo.
        removed = engine.prepare_all(helper, None, screws_desired=False, update=True)
        self.assertIsNone(removed[screws.PYTHON])
        self.assertNotIn(screws.PYTHON, mod_versions.load(removed[mod_versions.TARGET]))

    def test_update_status_reports_unreadable_metadata_without_verifying_unknown_legacy_code(self):
        current = installed_snapshot()
        current.versions = engine.MeshSnapshot(b"{broken-json", "invalid",
            {"known": False, "enabled": True, "patchable": False},
            original_hash=mods.digest(b""), original_absent=True)
        for path in SOURCES:
            status = engine.update_statuses(current)[path]
            self.assertTrue(status["needed"])
            self.assertFalse(status["available"])
            self.assertIn("version records need attention", status["reason"])
        self.assertFalse(any(status["needed"] and status["available"]
                             for status in engine.update_statuses(current).values()))
        with self.assertRaisesRegex(ValueError, "version records need attention"):
            engine.prepare_all(current, None, update=True)
        current.analysis["known"] = False
        current.analysis["reason"] = "Unknown printer code"
        unknown = engine.update_statuses(current)[mods.TARGET]
        self.assertFalse(unknown["needed"])
        self.assertFalse(unknown["available"])
        self.assertEqual(unknown["reason"], "Unknown printer code")

    def test_new_helper_requires_legacy_three_enrollment_in_same_preparation(self):
        current = helper_ready_snapshot()
        choices = {path: None for path in SOURCES}
        choices[screws.PYTHON] = True
        self.assertEqual(engine.automatic_updates(current, choices), set(SOURCES))
        pending = engine.prepare_all(current, None, screws_desired=True)
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(set(records), {*SOURCES, screws.PYTHON})
        self.assertEqual(records[screws.PYTHON]["version"], mod_versions.VERSIONS[screws.PYTHON])
        for path in (mods.TARGET, mesh_temperature.TARGET):
            self.assertEqual(pending[path], engine.file_snapshot(current, path).data)
        self.assertEqual(pending[mesh.TARGET], screws.edit_include(current.mesh.data, True))
        self.assertEqual(mod_versions.undo(pending[mesh.TARGET], records[mesh.TARGET]),
                         screws.edit_include(test_mesh.SOURCE, True))

    def test_new_helper_verifies_latest_records_and_updates_only_older_stamps(self):
        current = helper_ready_snapshot(stamped=True)
        choices = {path: None for path in SOURCES}
        choices[screws.PYTHON] = True
        self.assertEqual(engine.automatic_updates(current, choices), set())
        first = mod_versions.load(engine.prepare_all(current, None, screws_desired=True)[mod_versions.TARGET])
        self.assertEqual(first[screws.PYTHON]["version"], mod_versions.VERSIONS[screws.PYTHON])
        self.assertEqual({path: first[path] for path in SOURCES}, mod_versions.load(current.versions.data))
        records = mod_versions.load(current.versions.data)
        records[mesh_temperature.TARGET]["version"] = "0.1.4"
        current.versions.data = mod_versions.dump(records)
        current.versions.sha256 = mods.digest(current.versions.data)
        self.assertEqual(engine.automatic_updates(current, choices), {mesh_temperature.TARGET})
        pending = engine.prepare_all(current, None, screws_desired=True)
        updated = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(updated[mesh_temperature.TARGET]["version"], "0.1.5")
        self.assertEqual(updated[mods.TARGET], records[mods.TARGET])
        damaged = helper_ready_snapshot(stamped=True)
        damaged.data = damaged.data.replace(b"chamber = float(match.group(2))", b"chamber = 99")
        with self.assertRaises(ValueError):
            engine.automatic_updates(damaged, choices)

    def test_unsafe_retained_legacy_mod_blocks_new_helper_even_at_full_risk(self):
        for fault in ("unknown", "ambiguous", "missing original"):
            with self.subTest(fault=fault):
                current = helper_ready_snapshot()
                if fault == "unknown":
                    current.analysis["known"] = False
                elif fault == "ambiguous":
                    current.analysis["enabled"] = None
                else:
                    current.original = None
                with self.assertRaises(ValueError):
                    engine.prepare_all(current, None, screws_desired=True, full_risk=True)

    def test_staged_off_legacy_mod_is_disabled_without_enrollment(self):
        current = helper_ready_snapshot()
        current.analysis["known"] = False
        choices = {path: None for path in SOURCES}
        choices.update({mods.TARGET: False, screws.PYTHON: True})
        self.assertEqual(engine.automatic_updates(current, choices), {mesh.TARGET, mesh_temperature.TARGET})
        pending = engine.prepare_all(current, False, screws_desired=True, full_risk=True)
        self.assertEqual(pending[mods.TARGET], test_mods.SOURCE)
        self.assertEqual(set(mod_versions.load(pending[mod_versions.TARGET])),
                         {mesh.TARGET, mesh_temperature.TARGET, screws.PYTHON})

    def test_optional_update_can_be_combined_with_removal_without_reenabling_it(self):
        current = installed_snapshot()
        removal_only = engine.prepare_all(current, False, update=False)
        self.assertEqual(removal_only, {mods.TARGET: test_mods.SOURCE})
        combined = engine.prepare_all(current, False, update=True)
        self.assertEqual(combined[mods.TARGET], test_mods.SOURCE)
        records = mod_versions.load(combined[mod_versions.TARGET])
        self.assertEqual(set(records), {mesh.TARGET, mesh_temperature.TARGET})
        for path in records:
            self.assertEqual(records[path]["version"], mod_versions.VERSIONS[path])
            self.assertEqual(combined[path], engine.file_snapshot(current, path).data)

    def test_legacy_three_enroll_without_runtime_changes(self):
        current = installed_snapshot()
        self.assertEqual({path for path, status in engine.update_statuses(current).items()
                          if status["needed"] and status["available"]}, set(SOURCES))
        for path in SOURCES:
            self.assertEqual(engine.version_details(current, path), "v0.1.5")
        pending = engine.prepare_all(current, None, update=True)
        self.assertEqual(set(pending), {*SOURCES, mod_versions.TARGET})
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(set(records), set(SOURCES))
        for path, original in SOURCES.items():
            with self.subTest(path=path):
                self.assertEqual(pending[path], engine.file_snapshot(current, path).data)
                self.assertEqual(records[path]["version"], mod_versions.VERSIONS[path])
                self.assertEqual(mod_versions.undo(pending[path], records[path]), original)

    def test_old_700_mesh_upgrades_instead_of_receiving_a_false_latest_stamp(self):
        old = mesh.transform(test_mesh.SOURCE, True).replace(b'speed: 600', b'speed: 7e2')
        for stamped in (False, True):
            with self.subTest(stamped=stamped), patch.object(mesh, 'REFERENCE_HASH', mesh.fingerprint(test_mesh.SOURCE)):
                current = installed_snapshot()
                current.mesh.data = old.replace(b'0.1, 0.2', b'0.3, 0.4')
                current.mesh.sha256 = mods.digest(current.mesh.data)
                current.mesh.analysis = mesh.inspect(current.mesh.data, current.mesh.original)
                self.assertTrue(current.mesh.analysis['legacy'])
                if stamped:
                    record = mod_versions.capture(mesh.TARGET, test_mesh.SOURCE, old)
                    record['version'] = '0.1.5'
                    ledger = mod_versions.dump({mesh.TARGET: record})
                    current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
                        {'known': True, 'enabled': True, 'patchable': True},
                        original_hash=mods.digest(b''), original_absent=True)
                pending = engine.prepare_all(current, None, update=True)
                record = mod_versions.load(pending[mod_versions.TARGET])[mesh.TARGET]
                self.assertEqual(mesh.Document(pending[mesh.TARGET]).raw[('bed_mesh', 'speed')], '600')
                self.assertEqual(record['version'], mod_versions.VERSIONS[mesh.TARGET])
                self.assertEqual(mod_versions.undo(pending[mesh.TARGET], record),
                                 test_mesh.SOURCE.replace(b'0.1, 0.2', b'0.3, 0.4'))
                self.assertEqual(current.mesh.original, test_mesh.SOURCE)
                if not stamped:
                    current.screws = test_screws.snapshot().screws
                    automatic = engine.prepare_all(current, None, screws_desired=True)
                    records = mod_versions.load(automatic[mod_versions.TARGET])
                    self.assertEqual(mesh.Document(automatic[mesh.TARGET]).raw[('bed_mesh', 'speed')], '600')
                    self.assertEqual(records[mesh.TARGET]['version'], mod_versions.VERSIONS[mesh.TARGET])
                    self.assertEqual(mod_versions.undo(automatic[mesh.TARGET], records[mesh.TARGET]),
                                     screws.edit_include(test_mesh.SOURCE, True).replace(b'0.1, 0.2', b'0.3, 0.4'))

    def test_recorded_undo_ignores_latest_templates_and_preserves_other_data(self):
        original = test_mesh.SOURCE
        installed = mesh.transform(original, True)
        record = mod_versions.capture(mesh.TARGET, original, installed)
        customized = installed.replace(b"0.1, 0.2", b"0.7, 0.8") + b"#*# user_note = retained\n"
        with patch.object(mesh, "transform", side_effect=AssertionError("latest template used")), \
                patch.object(mesh, "VALUES", {("bed_mesh", "speed"): "999"}):
            restored = mod_versions.undo(customized, record)
        self.assertEqual(restored, original.replace(b"0.1, 0.2", b"0.7, 0.8") + b"#*# user_note = retained\n")

    def test_changed_owned_code_conflicts_instead_of_overwriting(self):
        original = test_mesh.SOURCE
        installed = mesh.transform(original, True)
        record = mod_versions.capture(mesh.TARGET, original, installed)
        edited = installed.replace(b"speed: 600", b"speed: 601")
        with self.assertRaisesRegex(ValueError, "differs from its undo record"):
            mod_versions.undo(edited, record)
        duplicated = installed + record["undo"][0]["after"].encode()
        with self.assertRaisesRegex(ValueError, "differs from its undo record"):
            mod_versions.undo(duplicated, record)

    def test_invalid_schema_paths_and_undo_are_rejected(self):
        record = {"version": "0.1.5", "undo": [{"before": "x = 1\n", "after": "x = 2\n"}]}
        invalid = [
            {"schema": 2, "mods": {}},
            {"schema": 1, "mods": {"/etc/shadow": record}},
            {"schema": 1, "mods": {mods.TARGET: {**record, "version": "latest"}}},
            {"schema": 1, "mods": {mods.TARGET: {**record, "undo": [{"before": "x", "after": ""}]}}},
            {"schema": 1, "mods": {mods.TARGET: {**record, "undo": [{"before": "x", "after": "y", "exec": "ignored"}]}}},
        ]
        for document in invalid:
            with self.subTest(document=document), self.assertRaises(ValueError):
                mod_versions.load(json.dumps(document).encode())
        with self.assertRaisesRegex(ValueError, "Duplicate key"):
            mod_versions.load(b'{"schema":1,"schema":1,"mods":{}}')

    def test_full_restore_of_unknown_configs_preserves_current_calibration(self):
        current = installed_snapshot()
        for path in SOURCES:
            engine.file_snapshot(current, path).analysis = {
                "known": False, "enabled": None, "patchable": False,
                "label": "Conflict", "reason": "Old implementation"}
        # Original files need not contain any settings expected by current mods.
        current.mesh.original = b"[printer]\nkinematics: corexy\n"
        current.mesh.original_hash = mods.digest(current.mesh.original)
        current.mesh.data = current.mesh.data.replace(b"0.1, 0.2", b"0.5, 0.6")
        current.mesh.sha256 = mods.digest(current.mesh.data)
        current.mesh_temperature.original = b"[gcode_macro OLD]\ngcode:\n  G4 P1\n"
        current.mesh_temperature.original_hash = mods.digest(current.mesh_temperature.original)
        with patch.object(mesh, "transform", side_effect=AssertionError("latest mod used")), \
                patch.object(mesh_temperature, "transform", side_effect=AssertionError("latest mod used")):
            pending = engine.prepare_all(current, None, restore=True)
        self.assertEqual(pending[mods.TARGET], test_mods.SOURCE)
        self.assertEqual(pending[mesh.TARGET], current.mesh.original + mesh.split(current.mesh.data)[1].encode())
        self.assertIn(b"points = 0.5, 0.6", pending[mesh.TARGET])
        self.assertEqual(pending[mesh_temperature.TARGET], current.mesh_temperature.original)

    def test_json_install_failure_rolls_back_prior_working_file(self):
        harness = test_mesh.BatchTests()
        harness.setUp()
        try:
            original = harness.py.read_bytes()
            request = harness.request()
            request["files"] = request["files"][:1]
            ledger = harness.root / "Uncurser_mods.json"
            new = mod_versions.dump({mods.TARGET: {
                "version": "0.1.5", "undo": [{"before": "value = 1\n", "after": "value = 2\n"}]}})
            request["files"].append(dict(path=str(ledger),
                backup=str(ledger.with_suffix(".jsn.Original.txt")), expected=None,
                data=base64.b64encode(new).decode(), new_hash=mods.digest(new),
                original_hash=None, may_capture_original=True, original_absent=False,
                reference="fixture", restore=False, kind="json"))
            native_link = os.link
            def fail_metadata(source, target):
                if Path(target) == ledger:
                    raise OSError("metadata installation failed")
                return native_link(source, target)
            with patch("os.link", fail_metadata), self.assertRaisesRegex(OSError, "metadata installation failed"):
                harness.run_request(request)
            self.assertEqual(harness.py.read_bytes(), original)
            self.assertFalse(ledger.exists())
            self.assertFalse(list(harness.root.glob("*.uncursed-pending-*.txt")))
        finally:
            harness.tearDown()

    def test_actual_metadata_enrollment_preserves_runtime_and_guards_dependencies(self):
        harness = test_mesh.BatchTests()
        harness.setUp()
        try:
            current = installed_snapshot()
            paths = {mods.TARGET: harness.py, mesh.TARGET: harness.cfg,
                     mesh_temperature.TARGET: harness.root / "gcode_macro.cfg",
                     mod_versions.TARGET: harness.root / "Uncurser_mods.json"}
            for module in (*(mod_versions.MODULES[path] for path in SOURCES), mod_versions):
                local = paths[module.TARGET]
                paths[module.ORIGINAL] = local.with_suffix(".jsn.Original.txt" if module is mod_versions
                    else ".ptn.Original.txt" if module is mods else ".cfn.Original.txt")
                paths[module.MANIFEST] = Path(str(paths[module.ORIGINAL]) + ".json")
            for path, original in SOURCES.items():
                value, module = engine.file_snapshot(current, path), mod_versions.MODULES[path]
                paths[path].write_bytes(value.data)
                paths[module.ORIGINAL].write_bytes(original)
                manifest = json.dumps(dict(path=path, sha256=mods.digest(original),
                                            mode=0o644, uid=0, gid=0)).encode()
                paths[module.MANIFEST].write_bytes(manifest)
                value.backup_guards = {module.ORIGINAL: mods.digest(original),
                                       module.MANIFEST: mods.digest(manifest)}
            dependency = "/mnt/UDISK/printer_data/config/dependency.cfg"
            paths[dependency] = harness.root / "dependency.cfg"
            paths[dependency].write_bytes(b"[gcode_macro LOCAL]\ngcode:\n  G4 P1\n")
            dependency_hash = mods.digest(paths[dependency].read_bytes())
            pattern = "/mnt/UDISK/printer_data/config/*.cfg"
            members = [mesh.TARGET, mesh_temperature.TARGET, dependency]
            for value in (current.mesh, current.mesh_temperature):
                value.guards = {dependency: dependency_hash}
                value.patterns = {pattern: members}
            originals = {path: paths[path].read_bytes() for path in SOURCES}
            requests = []

            class LocalRemote:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return None
                def read(self, path):
                    local = paths[path]
                    if not local.exists():
                        return None
                    data = local.read_bytes()
                    if path == mod_versions.MANIFEST:
                        document = json.loads(data)
                        document["path"] = mod_versions.TARGET
                        data = json.dumps(document).encode()
                    return {"data": data, "sha256": mods.digest(data)}
                def python(self, script, request):
                    requests.append(copy.deepcopy(request))
                    mapped = copy.deepcopy(request)
                    for item in mapped["files"]:
                        item["path"] = str(paths[item["path"]])
                        item["backup"] = str(paths[item["backup"]])
                    mapped["guards"] = {str(paths[path]): digest for path, digest in mapped["guards"].items()}
                    mapped["patterns"] = {str(harness.root / "*.cfg"):
                        [str(paths[path]) for path in members] for pattern, members in mapped["patterns"].items()}
                    harness.run_request(mapped)

            remote = LocalRemote()
            def saved_snapshot(*args):
                saved = copy.deepcopy(current)
                saved.versions = engine.scan_versions(remote)
                return saved
            calls = iter((lambda: current, lambda: saved_snapshot()))
            with patch.object(engine, "scan", side_effect=lambda *args: next(calls)()), \
                    patch.object(engine, "Remote", return_value=remote), \
                    patch.object(engine.network, "status", return_value=STATUS), \
                    patch.object(engine.network, "require_idle"):
                result = engine.apply(Connection("192.0.2.1"), harness.root / "unused", current, None, update=True)
            self.assertEqual([item["path"] for item in requests[0]["files"]], [mod_versions.TARGET])
            self.assertEqual(requests[0]["guards"][dependency], dependency_hash)
            self.assertEqual(requests[0]["patterns"][pattern], members)
            self.assertTrue(all(paths[path].read_bytes() == data for path, data in originals.items()))
            records = mod_versions.load(paths[mod_versions.TARGET].read_bytes())
            self.assertEqual(set(records), set(SOURCES))
            self.assertFalse(result["power_cycle"])
            self.assertFalse(result["snapshot"].versions.backup_error)
        finally:
            harness.tearDown()

    def test_future_update_captures_the_recorded_baseline(self):
        current = installed_snapshot()
        baseline = test_mesh.SOURCE.replace(b"horizontal_move_z: 5", b"horizontal_move_z: 4")
        current.mesh.data = mesh.transform(baseline, True)
        current.mesh.sha256 = mods.digest(current.mesh.data)
        old_record = mod_versions.capture(mesh.TARGET, baseline, current.mesh.data)
        ledger = mod_versions.dump({mesh.TARGET: old_record})
        current.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {"known": True, "enabled": True, "patchable": True},
            original_hash=mods.digest(b""), original_absent=True)
        next_version = list(mod_versions.version(mod_versions.VERSIONS[mesh.TARGET]))
        next_version[2] += 1
        next_version = '.'.join(map(str, next_version))
        with patch.object(mesh, "VALUES", {**mesh.VALUES, ("bed_mesh", "horizontal_move_z"): "3"}), \
                patch.object(mod_versions, "VERSIONS", {**mod_versions.VERSIONS, mesh.TARGET: next_version}):
            pending = engine.prepare_all(current, None, update=True)
        record = mod_versions.load(pending[mod_versions.TARGET])[mesh.TARGET]
        self.assertEqual(record["version"], next_version)
        self.assertIn(b"horizontal_move_z: 3", pending[mesh.TARGET])
        self.assertEqual(mod_versions.undo(pending[mesh.TARGET], record), baseline)

    def test_malformed_ledger_can_restore_only_with_valid_absence_proof(self):
        current = installed_snapshot()
        invalid = b"{broken-json"
        documents = {
            mod_versions.TARGET: invalid,
            mod_versions.ORIGINAL: b"",
            mod_versions.MANIFEST: json.dumps(dict(path=mod_versions.TARGET, sha256=mods.digest(b""),
                absent=True, mode=0o644, uid=0, gid=0)).encode(),
        }
        class Reader:
            def read(self, path):
                data = documents.get(path)
                return None if data is None else {"data": data, "sha256": mods.digest(data)}
        current.versions = engine.scan_versions(Reader())
        self.assertFalse(current.versions.backup_error)
        self.assertFalse(current.versions.analysis["patchable"])
        restored = engine.prepare_all(current, None, restore=True)
        self.assertIsNone(restored[mod_versions.TARGET])
        for path, original in SOURCES.items():
            self.assertEqual(restored[path], original)
        del documents[mod_versions.MANIFEST]
        current.versions = engine.scan_versions(Reader())
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, restore=True)

    def test_installed_records_do_not_hide_conflicting_included_settings(self):
        current = installed_snapshot()
        documents, records = {}, {}
        for path, original in SOURCES.items():
            module, value = mod_versions.MODULES[path], engine.file_snapshot(current, path)
            documents[path] = value.data
            documents[module.ORIGINAL] = original
            documents[module.MANIFEST] = json.dumps(dict(path=path, sha256=mods.digest(original),
                mode=0o644, uid=0, gid=0)).encode()
            records[path] = mod_versions.capture(path, original, value.data)
        documents[mod_versions.TARGET] = mod_versions.dump(records)
        documents[mod_versions.ORIGINAL] = b""
        documents[mod_versions.MANIFEST] = json.dumps(dict(path=mod_versions.TARGET,
            sha256=mods.digest(b""), absent=True, mode=0o644, uid=0, gid=0)).encode()
        duplicate = "/mnt/UDISK/printer_data/config/duplicate.cfg"
        active = {mesh.TARGET: current.mesh.data,
                  mesh_temperature.TARGET: current.mesh_temperature.data,
                  duplicate: b"[bed_mesh]\nspeed: 123\n"}
        tree = {"files": {path: {"data": base64.b64encode(data).decode(), "sha256": mods.digest(data)}
                          for path, data in active.items()}, "patterns": {}}
        class Reader:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return None
            def read(self, path):
                data = documents.get(path)
                return None if data is None else {"data": data, "sha256": mods.digest(data)}
            def python(self, *args):
                return tree
            def identity(self):
                return {"history": True}
            def prefetch(self, paths):
                pass
        with patch.object(engine, "Remote", return_value=Reader()), \
                patch.object(engine, "scan_screws", return_value=None), \
                patch.object(engine.network, "status", return_value=STATUS), \
                patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_mesh.SOURCE)):
            result = engine.scan(Connection("192.0.2.1"), Path("unused"))
        self.assertFalse(result.mesh.analysis["known"])
        self.assertFalse(result.mesh.analysis["patchable"])
        self.assertIn(duplicate, result.mesh.analysis["include_error"])
        self.assertEqual(engine.version_details(result, mesh.TARGET), "v" + mod_versions.VERSIONS[mesh.TARGET])
        with self.assertRaisesRegex(ValueError, "also defined in included file"):
            engine.prepare_all(result, None, mesh_desired=False, full_risk=True)


if __name__ == "__main__":
    unittest.main()
