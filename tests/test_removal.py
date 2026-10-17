"""Data-only removal plans using local synthetic snapshots and uninstall records."""
import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import config_blocks, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, removal, screws, smoothing
from tests import test_mesh, test_mesh_temperature, test_mods


def snapshot(data, original=None, *, absent=False):
    return SimpleNamespace(data=data, sha256=mods.digest(data) if data is not None else None,
                           original=original, original_hash=mods.digest(b"" if absent else original) if absent or original is not None else None,
                           original_absent=absent, backup_error="")


def single(before="value = 1\n", after="value = 2\n"):
    return {"version": mod_versions.VERSIONS[mods.TARGET], "undo": [{"before": before, "after": after}]}


def helper(legacy=False):
    path = screws.LEGACY_CONFIG if legacy else screws.CONFIG
    block = config_blocks.LEGACY_SCREWS_INCLUDE if legacy else config_blocks.SCREWS_INCLUDE
    original = b"[printer]\nkinematics: corexy\n[bed_mesh]\nspeed: 100\n"
    files = {mesh.TARGET: snapshot(block.encode() + original, original),
             screws.PYTHON: snapshot(b"# owned helper module\n", absent=True),
             path: snapshot(b"[screws_tilt_adjust]\nscrew1: 45,28\n", absent=True),
             fluidd_screws.TARGET: snapshot(b"// user change\nnew_dialog();\n", b"old_dialog();\n")}
    record = {"version": "0.1.12" if legacy else mod_versions.VERSIONS[screws.PYTHON], "files": {
        mesh.TARGET: {"undo": [{"before": "", "after": block}]},
        screws.PYTHON: {"created": files[screws.PYTHON].data.decode()},
        path: {"created": files[path].data.decode()},
        fluidd_screws.TARGET: {"undo": [{"before": "old_dialog();", "after": "new_dialog();"}]}}}
    return files, record


class RemovalTests(unittest.TestCase):
    def test_recorded_removal_uses_stored_code_and_preserves_unrelated_edits(self):
        original = b"value = 1\n"
        current = b"# new firmware or user edit\nvalue = 2\n"
        files = {mods.TARGET: snapshot(current, original)}
        with patch.object(mods, "transform", side_effect=AssertionError("current template used")):
            result = removal.inspect(files, {mods.TARGET: single()})[mods.TARGET]
        self.assertTrue(result["available"], result)
        self.assertEqual(result["files"], {mods.TARGET: b"# new firmware or user edit\nvalue = 1\n"})
        self.assertEqual(files[mods.TARGET].data, current)

    def test_partial_firmware_reset_skips_reverted_region_and_removes_retained_region(self):
        original_cfg = b"[gcode_macro HOME]\ngcode:\n  STOCK\n"
        current_cfg = original_cfg + b"\n[gcode_macro NEW_FIRMWARE]\ngcode: M400\n"
        record = {"version": mod_versions.VERSIONS[smoothing.TARGET], "files": {
            smoothing.TARGET: {"undo": [{"before": "  STOCK\n", "after": "  BOOST\n"}]},
            smoothing.HOMING.TARGET: {"undo": [{"before": "value = 1\n", "after": "value = 2\n"}]}}}
        files = {smoothing.TARGET: snapshot(current_cfg, original_cfg),
                 smoothing.HOMING.TARGET: snapshot(b"# unrelated\nvalue = 2\n", b"value = 1\n")}
        result = removal.inspect(files, {smoothing.TARGET: record})[smoothing.TARGET]
        self.assertTrue(result["available"], result)
        self.assertEqual(result["files"][smoothing.TARGET], current_cfg)
        self.assertEqual(result["files"][smoothing.HOMING.TARGET], b"# unrelated\nvalue = 1\n")

    def test_edited_duplicated_missing_or_ambiguously_absent_owned_code_blocks_mod(self):
        for data in (b"value = 3\n", b"value = 2\nvalue = 2\n", b"value = 1\nvalue = 1\n", None):
            with self.subTest(data=data):
                result = removal.inspect({mods.TARGET: snapshot(data, b"value = 1\n")}, {mods.TARGET: single()})[mods.TARGET]
                self.assertFalse(result["available"])
                self.assertEqual(result["files"], {})

    def test_absent_insertion_requires_whole_original_match(self):
        record = single("", "# owned insertion\n")
        original = b"value = 1\n"
        result = removal.inspect({mods.TARGET: snapshot(original, original)}, {mods.TARGET: record})[mods.TARGET]
        self.assertTrue(result["available"])
        self.assertEqual(result["files"][mods.TARGET], original)
        result = removal.inspect({mods.TARGET: snapshot(original + b"# changed\n", original)}, {mods.TARGET: record})[mods.TARGET]
        self.assertFalse(result["available"])

    def test_cfg_generated_calibration_is_preserved_and_ignored_for_original_match(self):
        original = b"[bed_mesh]\nspeed: 100\n"
        generated = (mesh.AUTOSAVE + "\n#*# [bed_mesh default]\n#*# points = 0.4, 0.5\n").encode()
        record = {"version": mod_versions.VERSIONS[mesh.TARGET], "undo": [{"before": "speed: 100", "after": "speed: 600"}]}
        current = original.replace(b"100", b"600") + generated
        result = removal.inspect({mesh.TARGET: snapshot(current, original)}, {mesh.TARGET: record})[mesh.TARGET]
        self.assertTrue(result["available"], result)
        self.assertEqual(result["files"][mesh.TARGET], original + generated)
        record["undo"] = [{"before": "", "after": "# owned missing insertion\n"}]
        result = removal.inspect({mesh.TARGET: snapshot(original + generated, original)}, {mesh.TARGET: record})[mesh.TARGET]
        self.assertTrue(result["available"], result)
        self.assertEqual(result["files"][mesh.TARGET], original + generated)

    def test_all_modified_file_removals_require_verified_originals(self):
        for fault in ("missing", "hash", "damaged", "absent"):
            value = snapshot(b"value = 2\n", b"value = 1\n")
            if fault == "missing":
                value.original = None
            elif fault == "hash":
                value.original_hash = "wrong"
            elif fault == "damaged":
                value.backup_error = "damaged manifest"
            else:
                value.original_absent = True
            with self.subTest(fault=fault):
                result = removal.inspect({mods.TARGET: value}, {mods.TARGET: single()})[mods.TARGET]
                self.assertFalse(result["available"])
                self.assertIn("original", result["reason"].lower())

    def test_helper_current_and_legacy_scopes_delete_only_verified_owned_added_files(self):
        for legacy in (False, True):
            files, record = helper(legacy)
            result = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
            self.assertTrue(result["available"], result)
            self.assertIsNone(result["files"][screws.PYTHON])
            self.assertIsNone(result["files"][screws.LEGACY_CONFIG if legacy else screws.CONFIG])
            self.assertEqual(result["files"][mesh.TARGET], files[mesh.TARGET].original)
            self.assertEqual(result["files"][fluidd_screws.TARGET], b"// user change\nold_dialog();\n")

    def test_already_absent_added_file_needs_no_backup_to_remain_absent(self):
        files, record = helper(True)
        files[screws.LEGACY_CONFIG] = snapshot(None)
        result = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
        self.assertTrue(result["available"], result)
        self.assertIsNone(result["files"][screws.LEGACY_CONFIG])
        del files[screws.LEGACY_CONFIG]
        result = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
        self.assertFalse(result["available"])
        self.assertIn("not read", result["reason"])

    def test_edited_added_file_or_missing_absence_proof_blocks_whole_helper(self):
        for fault in ("edited", "no proof", "bad backup"):
            files, record = helper()
            if fault == "edited":
                files[screws.PYTHON] = snapshot(b"# user content\n", absent=True)
            elif fault == "no proof":
                files[screws.PYTHON].original_hash = None
            else:
                files[screws.PYTHON].backup_error = "absence proof is damaged"
            with self.subTest(fault=fault):
                result = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
                self.assertFalse(result["available"])
                self.assertEqual(result["files"], {})

    def test_shared_file_removal_preserves_the_other_mod(self):
        files, helper_record = helper()
        files[mesh.TARGET].data = files[mesh.TARGET].data.replace(b"speed: 100", b"speed: 600")
        files[mesh.TARGET].sha256 = mods.digest(files[mesh.TARGET].data)
        mesh_record = {"version": mod_versions.VERSIONS[mesh.TARGET], "undo": [{"before": "speed: 100", "after": "speed: 600"}]}
        results = removal.inspect(files, {screws.PYTHON: helper_record, mesh.TARGET: mesh_record})
        self.assertTrue(results[screws.PYTHON]["available"])
        self.assertTrue(results[mesh.TARGET]["available"])
        self.assertNotIn(config_blocks.SCREWS_INCLUDE.encode(), results[screws.PYTHON]["files"][mesh.TARGET])
        self.assertIn(b"speed: 600", results[screws.PYTHON]["files"][mesh.TARGET])
        self.assertIn(config_blocks.SCREWS_INCLUDE.encode(), results[mesh.TARGET]["files"][mesh.TARGET])
        self.assertIn(b"speed: 100", results[mesh.TARGET]["files"][mesh.TARGET])

    def test_only_the_three_shipped_unstamped_mods_can_use_legacy_recognition(self):
        for path, module, source in ((mods.TARGET, mods, test_mods.SOURCE),
                                     (mesh.TARGET, mesh, test_mesh.SOURCE),
                                     (mesh_temperature.TARGET, mesh_temperature, test_mesh_temperature.SOURCE)):
            reference = mods.digest(source) if module is mods else module.fingerprint(source)
            with self.subTest(path=path), patch.object(module, "REFERENCE_HASH", reference):
                installed = mod_versions.enabled(path, source)
                result = removal.inspect({path: snapshot(installed, source)}, {})[path]
                self.assertTrue(result["available"], result)
                self.assertEqual(result["files"][path], source)
        files = {mesh.TARGET: snapshot(config_blocks.SCREWS_INCLUDE.encode() + b"[printer]\n"),
                 smoothing.HOMING.TARGET: snapshot(b"# UNCURSER BEGIN homing_speed:limits\n")}
        results = removal.inspect(files, {})
        for path in (screws.PYTHON, smoothing.TARGET):
            self.assertTrue(results[path]["present"])
            self.assertFalse(results[path]["available"])
            self.assertIn("Unknown installed mod version", results[path]["reason"])

    def test_bad_records_or_storage_metadata_block_removal_without_using_templates(self):
        files = {mods.TARGET: snapshot(b"value = 2\n", b"value = 1\n")}
        invalid = single()
        invalid["undo"][0]["execute"] = "not executable"
        for records, error in (({mods.TARGET: invalid}, ""), ({mods.TARGET: single()}, "Storage copies conflict")):
            result = removal.inspect(files, records, metadata_error=error)[mods.TARGET]
            self.assertFalse(result["available"])
            self.assertEqual(result["files"], {})
            self.assertTrue(result["reason"])

    def test_uninstalled_mods_and_unrelated_files_are_untouched(self):
        files = {"/user/other.py": snapshot(b"value = 2\n", b"value = 1\n")}
        before = copy.deepcopy(files)
        results = removal.inspect(files, {})
        self.assertTrue(all(not value["present"] and not value["available"] and value["files"] == {} for value in results.values()))
        self.assertEqual(files, before)

    def test_stock_homing_speed_is_not_a_mod_and_unread_files_never_count_as_absent(self):
        files = {smoothing.TARGET: snapshot(b"[stepper_x]\nhoming_speed: 100\n"),
                 smoothing.HOMING.TARGET: snapshot(b"# stock module\n")}
        self.assertFalse(removal.inspect(files, {})[smoothing.TARGET]["present"])
        for data in (None, b""):
            files, record = helper()
            files[screws.PYTHON] = snapshot(data, absent=True)
            files[screws.PYTHON].sha256 = ""
            result = removal.inspect(files, {screws.PYTHON: record})[screws.PYTHON]
            self.assertFalse(result["available"])
            self.assertEqual(result["files"], {})
        with patch.object(mods, "inspect", side_effect=SyntaxError("malformed printer file")):
            result = removal.inspect({mods.TARGET: snapshot(b"malformed")}, {})[mods.TARGET]
            self.assertTrue(result["present"])
            self.assertFalse(result["available"])


if __name__ == "__main__":
    unittest.main()
