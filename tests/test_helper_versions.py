"""Multi-file helper version and uninstall checks using synthetic local files."""
import base64
import contextlib
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mod_versions, mods, screws
from app.remote import Connection
from tests import test_mods, test_screws


def stamp(snapshot, records):
    data = mod_versions.dump(records)
    snapshot.versions = engine.MeshSnapshot(data, mods.digest(data),
        {"known": True, "enabled": True, "patchable": True},
        original_hash=mods.digest(b""), original_absent=True)
    return snapshot


class HelperVersionTests(unittest.TestCase):
    def setUp(self):
        for fixture in (
            patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_screws.SOURCE)),
            patch.object(fluidd_screws, "FILES", tuple(
                fluidd_screws.FluiddFile(path, mods.digest(data))
                for path, data in test_screws.FLUIDD_SOURCES.items())),
        ):
            fixture.start()
            self.addCleanup(fixture.stop)

    def installed(self, speed=False):
        before = test_screws.snapshot()
        pending = engine.prepare_all(before, None,
            mesh_desired=True if speed else None, screws_desired=True)
        current = stamp(test_screws.snapshot(True, True, speed),
                        mod_versions.load(pending[mod_versions.TARGET]))
        return before, current, pending

    def test_absent_helper_with_unavailable_fluidd_does_not_block_print_mod(self):
        for data in (None, b"// unsupported Fluidd interface\n"):
            with self.subTest(fluidd=data):
                current = test_screws.snapshot()
                current.screws.analysis = {"known": False, "enabled": None, "patchable": False,
                    "label": "Unavailable", "reason": "Unsupported Fluidd interface"}
                frontend = current.screws.frontend[fluidd_screws.TARGET]
                frontend.data = data
                frontend.analysis = dict(current.screws.analysis)
                pending = engine.prepare_all(current, None, mesh_desired=True)
                self.assertEqual(pending[mesh.TARGET], mesh.transform(test_screws.SOURCE, True))
                self.assertEqual(set(pending), {mesh.TARGET, mod_versions.TARGET})
                self.assertEqual(set(mod_versions.load(pending[mod_versions.TARGET])), {mesh.TARGET})

    def test_partial_or_recorded_helper_still_blocks_unrelated_mod(self):
        for fault in ("python", "config", "include", "popup", "unreadable helper", "record"):
            with self.subTest(fault=fault):
                current = test_screws.snapshot()
                current.screws.analysis = {"known": False, "enabled": None, "patchable": False,
                    "label": "Conflict", "reason": "An incomplete helper needs review"}
                frontend = current.screws.frontend[fluidd_screws.TARGET]
                frontend.analysis = dict(current.screws.analysis)
                if fault in ("python", "config"):
                    name = screws.PYTHON if fault == "python" else screws.CONFIG
                    current.screws.files[name].data = screws.payload(name)
                elif fault == "include":
                    current.screws.helper_enabled = True
                elif fault == "popup":
                    frontend.analysis["enabled"] = True
                elif fault == "unreadable helper":
                    current.screws.helper_enabled = None
                else:
                    _, installed, _ = self.installed()
                    current.versions = installed.versions
                with self.assertRaisesRegex(ValueError, "Cannot enable another mod"):
                    engine.prepare_all(current, None, mesh_desired=True, full_risk=True)

    def test_registration_check_rejects_missing_revision_or_file_scope(self):
        choices = {path: None for path in mod_versions.VERSIONS}
        mod_versions.require_registered(choices)
        for registry in ("VERSIONS", "FILES", "ADDED_FILES"):
            with self.subTest(registry=registry):
                values = dict(getattr(mod_versions, registry))
                values.pop(screws.PYTHON)
                with patch.object(mod_versions, registry, values), self.assertRaises(ValueError):
                    mod_versions.require_registered(choices)
        versions = {**mod_versions.VERSIONS, screws.PYTHON: "invalid"}
        with patch.object(mod_versions, "VERSIONS", versions), self.assertRaises(ValueError):
            mod_versions.require_registered(choices)

    def test_first_install_and_legacy_enrollment_record_every_affected_file(self):
        before, current, pending = self.installed()
        records = mod_versions.load(pending[mod_versions.TARGET])
        helper = records[screws.PYTHON]
        self.assertEqual(helper["version"], mod_versions.VERSIONS[screws.PYTHON])
        self.assertEqual(set(helper["files"]), set(mod_versions.FILES[screws.PYTHON]))
        self.assertEqual(set(name for name, entry in helper["files"].items() if "created" in entry),
                         {screws.PYTHON, screws.CONFIG})
        self.assertEqual(mod_versions.undo_files(screws.PYTHON, engine.helper_current(current), helper),
                         engine.helper_current(before))
        self.assertEqual(helper["files"][mesh.TARGET]["undo"],
                         [{"before": "", "after": screws.SCREWS_INCLUDE}])
        self.assertNotIn("// synthetic bundle", helper["files"][fluidd_screws.TARGET]["undo"][0]["after"])

        legacy = test_screws.snapshot(True, True)
        self.assertEqual(engine.version_details(legacy, screws.PYTHON), "Unknown version")
        enrolled = engine.prepare_all(legacy, None, update=True)
        self.assertEqual(mod_versions.load(enrolled[mod_versions.TARGET])[screws.PYTHON], helper)
        for name, data in engine.helper_current(legacy).items():
            self.assertEqual(enrolled[name], data)

    def test_uninstall_uses_saved_undo_without_current_payload_or_popup_templates(self):
        before, current, _ = self.installed()
        with patch.object(screws, "payload", side_effect=AssertionError("latest payload used")), \
                patch.object(fluidd_screws, "transform", side_effect=AssertionError("latest transform used")), \
                patch.object(fluidd_screws, "replacements", side_effect=AssertionError("latest popup used")):
            disabled = engine.prepare_all(current, None, screws_desired=False)
            pairs = engine.helper_js_replacements(current, disabled[fluidd_screws.TARGET])
        self.assertEqual({name: disabled[name] for name in mod_versions.FILES[screws.PYTHON]},
                         engine.helper_current(before))
        self.assertNotIn(screws.PYTHON, mod_versions.load(disabled[mod_versions.TARGET]))
        text = current.screws.frontend[fluidd_screws.TARGET].data.decode()
        for old, new in pairs:
            self.assertEqual(text.count(old), 1)
            text = text.replace(old, new, 1)
        self.assertEqual(text.encode(), disabled[fluidd_screws.TARGET])

    def test_shared_config_uninstalls_preserve_other_mod_and_current_calibration(self):
        _, current, _ = self.installed(speed=True)
        current.mesh.data = current.mesh.data.replace(b"0.1, 0.2", b"0.3, 0.4")
        current.mesh.sha256 = mods.digest(current.mesh.data)
        helper_off = engine.prepare_all(current, None, screws_desired=False)
        self.assertEqual(helper_off[mesh.TARGET],
                         mesh.transform(test_screws.SOURCE, True).replace(b"0.1, 0.2", b"0.3, 0.4"))
        retained_mesh = mod_versions.load(helper_off[mod_versions.TARGET])[mesh.TARGET]
        self.assertEqual(mod_versions.undo(helper_off[mesh.TARGET], retained_mesh),
                         test_screws.SOURCE.replace(b"0.1, 0.2", b"0.3, 0.4"))

        speed_off = engine.prepare_all(current, None, mesh_desired=False)
        self.assertEqual(speed_off[mesh.TARGET],
                         screws.edit_include(test_screws.SOURCE, True).replace(b"0.1, 0.2", b"0.3, 0.4"))
        helper_record = mod_versions.load(speed_off[mod_versions.TARGET])[screws.PYTHON]
        after = {**engine.helper_current(current), mesh.TARGET: speed_off[mesh.TARGET]}
        self.assertEqual(mod_versions.undo_files(screws.PYTHON, after, helper_record)[mesh.TARGET],
                         test_screws.SOURCE.replace(b"0.1, 0.2", b"0.3, 0.4"))
        for change in mod_versions.load(current.versions.data)[mesh.TARGET]["undo"]:
            self.assertNotIn("bed_screws_v1", change["before"] + change["after"])

    def test_update_all_keeps_outdated_shared_mods_staged_off(self):
        for speed_choice, helper_choice in ((False, None), (None, False), (False, False)):
            with self.subTest(speed=speed_choice, helper=helper_choice):
                _, current, _ = self.installed(speed=True)
                records = mod_versions.load(current.versions.data)
                records[mesh.TARGET]["version"] = "0.1.4"
                records[screws.PYTHON]["version"] = "0.1.5"
                stamp(current, records)
                current.mesh.data = current.mesh.data.replace(b"0.1, 0.2", b"0.3, 0.4")
                current.mesh.sha256 = mods.digest(current.mesh.data)
                guard = (patch.object(screws, "payload", side_effect=AssertionError("disabled helper was rebuilt"))
                         if helper_choice is False else contextlib.nullcontext())
                with guard:
                    pending = engine.prepare_all(current, None, mesh_desired=speed_choice,
                                                 screws_desired=helper_choice, update=True)
                expected = mesh.transform(test_screws.SOURCE, speed_choice is not False, test_screws.SOURCE)
                expected = screws.edit_include(expected, helper_choice is not False).replace(b"0.1, 0.2", b"0.3, 0.4")
                self.assertEqual(pending[mesh.TARGET], expected)
                installed = mod_versions.load(pending[mod_versions.TARGET])
                for name, choice in ((mesh.TARGET, speed_choice), (screws.PYTHON, helper_choice)):
                    if choice is False:
                        self.assertNotIn(name, installed)
                    else:
                        self.assertEqual(installed[name]["version"], mod_versions.VERSIONS[name])
                if helper_choice is False:
                    self.assertIsNone(pending[screws.PYTHON])
                    self.assertIsNone(pending[screws.CONFIG])
                    self.assertEqual(pending[fluidd_screws.TARGET], test_screws.FLUIDD_SOURCES[fluidd_screws.TARGET])

    def test_invalid_scope_changed_owned_files_and_missing_absence_proof_block_uninstall(self):
        _, current, _ = self.installed()
        helper = mod_versions.load(current.versions.data)[screws.PYTHON]
        incomplete = copy.deepcopy(helper)
        incomplete["files"].pop(screws.CONFIG)
        with self.assertRaises(ValueError):
            mod_versions.dump({screws.PYTHON: incomplete})
        for name in mod_versions.FILES[screws.PYTHON]:
            with self.subTest(file=name):
                files = engine.helper_current(current)
                files.pop(name)
                with self.assertRaises(ValueError):
                    mod_versions.undo_files(screws.PYTHON, files, helper)
                files = engine.helper_current(current)
                files[name] = None
                with self.assertRaises(ValueError):
                    mod_versions.undo_files(screws.PYTHON, files, helper)
                files = engine.helper_current(current)
                if name in (screws.PYTHON, screws.CONFIG):
                    files[name] += b"\n# user edit\n"
                elif name == mesh.TARGET:
                    files[name] = files[name].replace(screws.CONFIG.encode(), b"changed.cfg")
                else:
                    files[name] = files[name].replace(fluidd_screws.GRID_RENDER.encode(), b"changed_popup")
                with self.assertRaises(ValueError):
                    mod_versions.undo_files(screws.PYTHON, files, helper)

        for proof in ("absent", "checksum", "backup error"):
            with self.subTest(proof=proof):
                broken = copy.deepcopy(current)
                value = broken.screws.files[screws.PYTHON]
                if proof == "absent":
                    value.original_absent = False
                elif proof == "checksum":
                    value.original_hash = None
                else:
                    value.backup_error = "damaged absence proof"
                with self.assertRaises(ValueError):
                    engine.prepare_all(broken, None, screws_desired=False, full_risk=True)

    def test_partial_restore_keeps_helper_record_when_shared_group_cannot_restore(self):
        _, current, _ = self.installed()
        current.mesh.original = None
        current.mesh.original_hash = None
        current.data = mods.transform(test_mods.SOURCE, True)
        current.sha256 = mods.digest(current.data)
        current.analysis = {"known": True, "enabled": True, "patchable": True}
        current.original = test_mods.SOURCE
        current.original_hash = mods.digest(test_mods.SOURCE)
        records = mod_versions.load(current.versions.data)
        records[mods.TARGET] = mod_versions.capture(mods.TARGET, current.original, current.data)
        stamp(current, records)
        restored = engine.prepare_all(current, None, restore=True)
        self.assertEqual(restored[mods.TARGET], test_mods.SOURCE)
        for name in mod_versions.FILES[screws.PYTHON]:
            self.assertNotIn(name, restored)
        retained = mod_versions.load(restored.get(mod_versions.TARGET, current.versions.data))
        self.assertEqual(retained[screws.PYTHON], records[screws.PYTHON])
        self.assertNotIn(mods.TARGET, retained)

    def test_older_helper_updates_from_its_recorded_baseline(self):
        before, current, _ = self.installed()
        baseline = engine.helper_current(before)
        old = engine.helper_current(current)
        old[screws.PYTHON] += b"\n# previous helper implementation\n"
        old[screws.CONFIG] = old[screws.CONFIG].replace(
            b'[screws_tilt_adjust]\n', b'[screws_tilt_adjust]\nspeed: 150\n', 1)
        self.assertIn(b'speed: 150\n', old[screws.CONFIG])
        old_render = fluidd_screws.GRID_RENDER + "/* previous popup */"
        old[fluidd_screws.TARGET] = baseline[fluidd_screws.TARGET].replace(
            fluidd_screws.ORIGINAL_RENDER.encode(), old_render.encode(), 1)
        record = mod_versions.capture_files(screws.PYTHON, baseline, old, {
            mesh.TARGET: [["", screws.SCREWS_INCLUDE]],
            fluidd_screws.TARGET: [[fluidd_screws.ORIGINAL_RENDER, old_render]],
        })
        record["version"] = "0.1.6"
        for name, data in old.items():
            value = engine.file_snapshot(current, name)
            value.data, value.sha256 = data, mods.digest(data)
        stamp(current, {screws.PYTHON: record})

        documents = {mods.TARGET: test_mods.SOURCE,
            fluidd_screws.INDEX: ('<script type="module" src="./' + fluidd_screws.ASSET + '"></script>').encode()}
        for module, value in engine.file_entries(current):
            if module is mods or value is None:
                continue
            documents[module.TARGET] = value.data
            original = b"" if value.original_absent else value.original
            if original is not None:
                documents[module.ORIGINAL] = original
                documents[module.MANIFEST] = json.dumps(dict(path=module.TARGET,
                    sha256=mods.digest(original), absent=value.original_absent,
                    mode=0o644, uid=0, gid=0)).encode()
        tree = {"files": {name: {"data": base64.b64encode(old[name]).decode(),
                                     "sha256": mods.digest(old[name])}
                          for name in (mesh.TARGET, screws.CONFIG)},
                "patterns": {screws.CONFIG: [screws.CONFIG]}}

        class Reader:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def prefetch(self, paths):
                pass
            def read(self, name):
                data = documents.get(name)
                return None if data is None else {"data": data, "sha256": mods.digest(data)}
            def identity(self):
                return {"history": True}
            def python(self, *args):
                return tree

        self.assertFalse(engine.scan_screws(Reader(), current.mesh, tree).analysis["patchable"])
        with patch.object(engine, "Remote", return_value=Reader()), \
                patch.object(engine.network, "status", return_value=current.status):
            current = engine.scan(Connection("192.0.2.1"), Path("unused"))
            self.assertEqual({name: current.screws.analysis[name] for name in ("known", "enabled", "patchable")},
                             {"known": True, "enabled": True, "patchable": True})
            for conflict in ("Fluidd alternate", "duplicate config"):
                with self.subTest(conflict=conflict):
                    if conflict == "Fluidd alternate":
                        documents[fluidd_screws.TARGET + ".gz"] = b"unsupported compressed asset"
                    else:
                        duplicate = "/mnt/UDISK/printer_data/config/duplicate.cfg"
                        data = b"[bed_mesh]\nspeed: 123\n"
                        tree["files"][duplicate] = {"data": base64.b64encode(data).decode(),
                                                    "sha256": mods.digest(data)}
                    blocked = engine.scan(Connection("192.0.2.1"), Path("unused"))
                    self.assertFalse(blocked.screws.analysis["patchable"])
                    if conflict == "Fluidd alternate":
                        self.assertIn("dependency_error", blocked.screws.frontend[fluidd_screws.TARGET].analysis)
                        del documents[fluidd_screws.TARGET + ".gz"]
                    else:
                        self.assertIn("include_error", blocked.mesh.analysis)
                        del tree["files"][duplicate]

        self.assertEqual({path for path, status in engine.update_statuses(current).items()
                          if status["needed"] and status["available"]}, {screws.PYTHON})
        updated = engine.prepare_all(current, None, update=True)
        new_record = mod_versions.load(updated[mod_versions.TARGET])[screws.PYTHON]
        self.assertEqual(new_record["version"], mod_versions.VERSIONS[screws.PYTHON])
        self.assertNotIn(b'\nspeed:', updated[screws.CONFIG])
        self.assertEqual(updated[screws.PYTHON], screws.payload(screws.PYTHON))
        self.assertEqual(updated[fluidd_screws.TARGET], fluidd_screws.transform(
            fluidd_screws.TARGET, baseline[fluidd_screws.TARGET], True))
        self.assertEqual(mod_versions.undo_files(screws.PYTHON,
            {name: updated[name] for name in mod_versions.FILES[screws.PYTHON]}, new_record), baseline)
        pairs = engine.helper_js_replacements(current, updated[fluidd_screws.TARGET])
        latest_pairs = [list(pair) for pair in fluidd_screws.blocks(fluidd_screws.TARGET)]
        self.assertEqual(pairs, [[old_render, fluidd_screws.ORIGINAL_RENDER], *latest_pairs])
        self.assertEqual(new_record['files'][fluidd_screws.TARGET]['undo'],
                         [{'before': before, 'after': after} for before, after in latest_pairs])


if __name__ == "__main__":
    unittest.main()
