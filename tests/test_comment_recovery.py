"""Source comments identify current partial mods without inventing uninstall data."""
import copy
import unittest

from app import (engine, fluidd_screws, mesh, mesh_temperature, mod_comments,
                 mod_versions, mods, removal, screws, smoothing, warmup)
from tests import test_mesh, test_mesh_temperature, test_mods, test_screws, test_smoothing
from tests.test_stock_recovery import stock


IDENTITY = ("F008", "1.1.7.0")
WARMUP = b'''class ControlBangBang:
    def temperature_update(self, read_time, temp, target_temp):
        if is_chamber_heater and heater_bed_state == 1:
            self.temp_coff = 0
            self.heater.start_heating_seconds = 0
'''


def file(data, original=None):
    return engine.MeshSnapshot(data, mods.digest(data) if data is not None else None, {},
                               original, mods.digest(original) if original is not None else None)


def marked(path, data, filename=None, *, revision=None):
    return mod_comments.put(data, path, revision or mod_versions.VERSIONS[path], IDENTITY[1], filename or path)


def inspect(files, defaults=None, records=None, **kwargs):
    return removal.inspect(files, records or {}, stock=defaults, identity=IDENTITY,
                           header_fallback=True, **kwargs)


class CommentRecoveryTests(unittest.TestCase):
    def test_single_file_mods_recover_and_do_not_mutate_input(self):
        for module, source in ((mods, test_mods.SOURCE), (mesh, test_mesh.SOURCE),
                               (mesh_temperature, test_mesh_temperature.SOURCE), (warmup, WARMUP)):
            with self.subTest(module=module.TARGET):
                data = module.transform(source, True)
                files = {module.TARGET: file(marked(module.TARGET, data))}
                before = copy.deepcopy(files)
                plan = inspect(files, stock({module.TARGET: source}))[module.TARGET]
                self.assertTrue(plan["available"], plan)
                self.assertTrue(plan["header"])
                self.assertEqual(plan["version"], mod_versions.VERSIONS[module.TARGET])
                self.assertEqual(plan["firmware"], IDENTITY[1])
                self.assertEqual(plan["files"][module.TARGET], source)
                self.assertEqual(files, before)

    def test_homing_missing_native_file_stays_absent(self):
        defaults = {smoothing.TARGET: test_smoothing.SOURCE,
                    smoothing.HOMING.TARGET: test_smoothing.HOMING_SOURCE}
        for missing in defaults:
            with self.subTest(missing=missing):
                files = {module.TARGET: file(None if module.TARGET == missing else marked(
                    smoothing.TARGET, module.transform(defaults[module.TARGET], True), module.TARGET))
                    for module in (smoothing, smoothing.HOMING)}
                plan = inspect(files, stock(defaults))[smoothing.TARGET]
                self.assertTrue(plan["available"], plan)
                self.assertTrue(plan["incomplete"])
                self.assertIsNone(plan["files"][missing])
                for path in defaults.keys() - {missing}:
                    self.assertEqual(plan["files"][path], defaults[path])

    def helper(self):
        originals = {mesh.TARGET: test_screws.SOURCE, screws.PYTHON: None,
                     screws.CONFIG: None, screws.LEGACY_CONFIG: None,
                     fluidd_screws.TARGET: test_screws.FLUIDD_SOURCES[fluidd_screws.TARGET]}
        files = {mesh.TARGET: file(screws.edit_include(originals[mesh.TARGET], True)),
                 screws.PYTHON: file(screws.payload(screws.PYTHON)),
                 screws.CONFIG: file(screws.payload(screws.CONFIG)),
                 screws.LEGACY_CONFIG: file(None),
                 fluidd_screws.TARGET: file(fluidd_screws.transform(
                     fluidd_screws.TARGET, originals[fluidd_screws.TARGET], True))}
        for path in mod_versions.FILES[screws.PYTHON]:
            files[path] = file(marked(screws.PYTHON, files[path].data, path))
        return files, originals

    def test_helper_removes_survivors_when_any_owned_file_is_missing(self):
        for missing in mod_versions.FILES[screws.PYTHON]:
            with self.subTest(missing=missing):
                files, originals = self.helper()
                files[missing] = file(None)
                plan = inspect(files, stock(originals))[screws.PYTHON]
                self.assertTrue(plan["available"], plan)
                self.assertTrue(plan["incomplete"])
                for path, data in plan["files"].items():
                    self.assertEqual(data, None if path == missing else originals[path])

    def test_shared_headers_and_generated_calibration_are_preserved(self):
        source = test_mesh.SOURCE
        source = marked(screws.PYTHON, source, mesh.TARGET)
        data = marked(mesh.TARGET, mesh.transform(source, True))
        files = {mesh.TARGET: file(data)}
        plan = inspect(files, stock({mesh.TARGET: source}))[mesh.TARGET]
        self.assertTrue(plan["available"], plan)
        self.assertEqual(plan["files"][mesh.TARGET], source)
        self.assertEqual(mesh.split(data)[1], mesh.split(plan["files"][mesh.TARGET])[1])

    def test_partial_mesh_values_must_still_match_current_implementation(self):
        source = test_mesh.SOURCE
        data = mesh.transform(source, True).replace(b"speed: 600", b"speed: 100")
        files = {mesh.TARGET: file(marked(mesh.TARGET, data))}
        self.assertTrue(inspect(files, stock({mesh.TARGET: source}))[mesh.TARGET]["available"])
        for value in (b"700", b"6000"):
            files[mesh.TARGET] = file(marked(mesh.TARGET, data.replace(b"speed: 100", b"speed: " + value)))
            self.assertFalse(inspect(files, stock({mesh.TARGET: source}))[mesh.TARGET]["available"])

    def test_added_files_need_exact_payload_and_verified_native_absence(self):
        for fault in ("edited", "other owner", "native file"):
            with self.subTest(fault=fault):
                files, originals = self.helper()
                if fault == "edited":
                    files[screws.PYTHON] = file(files[screws.PYTHON].data + b"# user edit\n")
                elif fault == "other owner":
                    files[screws.PYTHON] = file(marked(mesh.TARGET, files[screws.PYTHON].data, screws.PYTHON))
                else:
                    originals[screws.PYTHON] = b"# native firmware helper\n"
                plan = inspect(files, stock(originals))[screws.PYTHON]
                self.assertFalse(plan["available"], plan)

    def test_same_firmware_verified_original_is_sufficient_without_rom(self):
        data = marked(warmup.TARGET, warmup.transform(WARMUP, True))
        value = file(data, WARMUP)
        value.original_identity = IDENTITY
        files = {warmup.TARGET: value}
        self.assertTrue(inspect(files)[warmup.TARGET]["available"])
        value.original_identity = ("F008", "1.1.6.4")
        self.assertFalse(inspect(files)[warmup.TARGET]["available"])

    def test_native_adopted_chamber_value_is_preserved(self):
        source = warmup.transform(WARMUP, True)
        files = {warmup.TARGET: file(marked(warmup.TARGET, source))}
        plan = inspect(files, stock({warmup.TARGET: source}))[warmup.TARGET]
        self.assertTrue(plan["available"], plan)
        self.assertEqual(plan["files"][warmup.TARGET], source)

    def test_mesh_temperature_comment_recovery_handles_either_remaining_anchor(self):
        source = test_mesh_temperature.SOURCE
        enabled = mesh_temperature.transform(source, True)
        source_lines, source_initial, source_condition = mesh_temperature.anchors(source)
        enabled_lines, enabled_initial, enabled_condition = mesh_temperature.anchors(enabled)
        enabled_lines[enabled_condition] = enabled_lines[enabled_condition].replace(
            enabled_lines[enabled_condition].strip(), mesh_temperature.ENABLED_CONDITION)
        for remaining in ("initializer", "condition", "both"):
            with self.subTest(remaining=remaining):
                lines = list(source_lines)
                if remaining != "condition":
                    lines[source_initial] = enabled_lines[enabled_initial]
                if remaining != "initializer":
                    lines[source_condition] = enabled_lines[enabled_condition]
                data = marked(mesh_temperature.TARGET, "".join(lines).encode())
                files = {mesh_temperature.TARGET: file(data)}
                plan = inspect(files, stock({mesh_temperature.TARGET: source}))[mesh_temperature.TARGET]
                self.assertTrue(plan["available"], plan)
                self.assertEqual(plan["files"][mesh_temperature.TARGET], source)

    def test_old_mesh_temperature_comment_without_json_remains_unknown(self):
        source = test_mesh_temperature.SOURCE
        lines, index = mesh_temperature.condition(source)
        lines[index] = lines[index].replace(mesh_temperature.ORIGINAL_CONDITION,
                                            mesh_temperature.ENABLED_CONDITION)
        data = marked(mesh_temperature.TARGET, "".join(lines).encode(), revision="0.1.15")
        files = {mesh_temperature.TARGET: file(data)}
        plan = inspect(files, stock({mesh_temperature.TARGET: source}))[mesh_temperature.TARGET]
        self.assertFalse(plan["available"], plan)
        self.assertIn("unsupported mod revisions", plan["reason"])

    def test_recorded_rom_recovery_removes_only_selected_metadata(self):
        for module, source in ((mods, test_mods.SOURCE), (mesh, test_mesh.SOURCE),
                               (mesh_temperature, test_mesh_temperature.SOURCE), (warmup, WARMUP)):
            with self.subTest(module=module.TARGET):
                source = marked(screws.PYTHON, source, module.TARGET)
                applied = marked(module.TARGET, module.transform(source, True))
                record = mod_versions.capture(module.TARGET, source, applied, firmware=IDENTITY[1])
                plan = removal.inspect({module.TARGET: file(applied)}, {module.TARGET: record},
                                       stock=stock({module.TARGET: source}), identity=IDENTITY)[module.TARGET]
                self.assertTrue(plan["available"], plan)
                self.assertTrue(plan["recovery"])
                self.assertEqual(plan["files"][module.TARGET], source)
                self.assertNotIn(module.TARGET, mod_comments.read(plan["files"][module.TARGET]))
                self.assertIn(screws.PYTHON, mod_comments.read(plan["files"][module.TARGET]))

    def test_recorded_native_removal_allows_only_missing_own_supplemental_metadata(self):
        for module, source in ((mods, test_mods.SOURCE), (mesh, test_mesh.SOURCE),
                               (mesh_temperature, test_mesh_temperature.SOURCE), (warmup, WARMUP)):
            with self.subTest(module=module.TARGET):
                body = module.transform(source, True)
                record = mod_versions.capture(module.TARGET, source, marked(module.TARGET, body), firmware=IDENTITY[1])
                files = {module.TARGET: file(body, source)}
                plan = removal.inspect(files, {module.TARGET: record})[module.TARGET]
                self.assertTrue(plan["available"], plan)
                self.assertEqual(plan["files"][module.TARGET], source)
                files[module.TARGET] = file(marked(module.TARGET, body, revision="0.1.1"), source)
                self.assertFalse(removal.inspect(files, {module.TARGET: record})[module.TARGET]["available"])

    def test_recorded_helper_removes_unstamped_payload_but_not_changed_added_files(self):
        files, originals = self.helper()
        before = {path: originals[path] for path in mod_versions.FILES[screws.PYTHON]}
        record = engine.capture_helper(before, {path: files[path].data for path in before}, firmware=IDENTITY[1])
        for path, value in files.items():
            if path not in before:
                continue
            value.data = mod_comments.remove(value.data, screws.PYTHON)
            value.sha256 = mods.digest(value.data)
            value.original = before[path]
            value.original_hash = mods.digest(before[path] or b"")
            value.original_absent = before[path] is None
        plan = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
        self.assertTrue(plan["available"], plan)
        self.assertEqual(plan["files"], before)
        for edit in (b"# unrelated user edit\n", marked(mesh.TARGET, b"", screws.PYTHON)):
            changed = copy.deepcopy(files)
            value = changed[screws.PYTHON]
            value.data += edit
            value.sha256 = mods.digest(value.data)
            self.assertFalse(removal.inspect(changed, {screws.PYTHON: record})[screws.PYTHON]["available"])

    def test_bad_metadata_revision_and_unread_files_block_header_fallback(self):
        files, originals = self.helper()
        for error in ("revision", "json", "unread", "checksum"):
            with self.subTest(error=error):
                changed = copy.deepcopy(files)
                kwargs = {}
                if error == "revision":
                    data = mod_comments.remove(changed[screws.PYTHON].data, screws.PYTHON)
                    changed[screws.PYTHON] = file(marked(screws.PYTHON, data, revision="0.1.15"))
                elif error == "json":
                    kwargs["metadata_error"] = "Uninstall JSON is corrupt"
                elif error == "unread":
                    changed[screws.CONFIG] = file(b"")
                    changed[screws.CONFIG].sha256 = ""
                else:
                    changed[screws.PYTHON].sha256 = "bad"
                self.assertFalse(inspect(changed, stock(originals), **kwargs)[screws.PYTHON]["available"])


if __name__ == "__main__":
    unittest.main()
