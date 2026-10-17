"""Optional per-mod firmware stamps; no printer or filesystem access."""
import copy
import json
import unittest

from app import fluidd_screws, mesh, mod_versions, mods, screws, smoothing


class FirmwareRecordTests(unittest.TestCase):
    def single(self, **options):
        return mod_versions.capture(mods.TARGET, b"value = 1\n", b"value = 2\n", **options)

    def multiple(self, **options):
        baseline = {smoothing.TARGET: b"[gcode_macro HOME]\ngcode: STOCK\n", smoothing.HOMING.TARGET: b"value = 1\n"}
        enabled = {smoothing.TARGET: baseline[smoothing.TARGET].replace(b"STOCK", b"BOOST"), smoothing.HOMING.TARGET: b"value = 2\n"}
        pairs = {path: [(baseline[path].decode(), enabled[path].decode())] for path in baseline}
        return mod_versions.capture_files(smoothing.TARGET, baseline, enabled, pairs, **options), baseline, enabled

    def test_single_and_multi_file_stamps_roundtrip_without_changing_undo_or_revision(self):
        versions = dict(mod_versions.VERSIONS)
        single = self.single(firmware="1.1.7.0")
        multiple, baseline, enabled = self.multiple(firmware="1.1.7.0")
        records = {mods.TARGET: single, smoothing.TARGET: multiple}
        encoded = mod_versions.dump(records)
        self.assertEqual(json.loads(encoded)["schema"], 1)
        self.assertEqual(mod_versions.load(encoded), records)
        self.assertTrue(all(record["firmware"] == "1.1.7.0" for record in records.values()))
        self.assertEqual(mod_versions.undo(b"value = 2\n", single), b"value = 1\n")
        self.assertEqual(mod_versions.undo_files(smoothing.TARGET, enabled, multiple), baseline)
        self.assertEqual(mod_versions.VERSIONS, versions)
        self.assertEqual(mod_versions.file_scope(smoothing.TARGET, multiple), mod_versions.FILES[smoothing.TARGET])

    def test_old_records_remain_valid_unknown_and_are_not_retroactively_stamped(self):
        single = self.single()
        multiple, _, _ = self.multiple()
        records = {mods.TARGET: single, smoothing.TARGET: multiple}
        before = copy.deepcopy(records)
        migrated = mod_versions.load(mod_versions.dump(records))
        self.assertEqual(migrated, before)
        self.assertTrue(all("firmware" not in record for record in migrated.values()))
        self.assertNotIn("firmware", self.single(firmware=None))
        self.assertEqual(records, before)

    def test_firmware_requires_canonical_four_part_release_and_no_extra_record_fields(self):
        invalid = (None, True, 1170, [], {}, "", "1.1.7", "1.1.7.0.1", "v1.1.7.0", "1.1.7.0 ",
                   " 1.1.7.0", "01.1.7.0", "1.01.7.0", "1.1.7.-1", "١.1.7.0")
        for value in invalid:
            record = {**self.single(), "firmware": value}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "firmware"):
                mod_versions.dump({mods.TARGET: record})
            if value is not None:
                with self.assertRaisesRegex(ValueError, "firmware"):
                    self.single(firmware=value)
        for record, path in ((self.single(firmware="1.1.7.0"), mods.TARGET),
                             (self.multiple(firmware="1.1.7.0")[0], smoothing.TARGET)):
            record["extra"] = "still invalid"
            with self.assertRaises(ValueError):
                mod_versions.dump({path: record})

    def test_stamped_historical_helper_scope_is_preserved(self):
        record = {"version": "0.1.12", "firmware": "1.1.6.4", "files": {
            mesh.TARGET: {"undo": [{"before": "", "after": "# old helper include\n"}]},
            screws.PYTHON: {"created": "# helper module\n"},
            screws.LEGACY_CONFIG: {"created": "[screws_tilt_adjust]\n"},
            fluidd_screws.TARGET: {"undo": [{"before": "old dialog", "after": "new dialog"}]}}}
        decoded = mod_versions.load(mod_versions.dump({screws.PYTHON: record}))[screws.PYTHON]
        scope = mod_versions.file_scope(screws.PYTHON, decoded)
        self.assertIn(screws.LEGACY_CONFIG, scope)
        self.assertNotIn(screws.CONFIG, scope)
        self.assertEqual(decoded, record)
        record["firmware"] = "invalid"
        with self.assertRaisesRegex(ValueError, "firmware"):
            mod_versions.file_scope(screws.PYTHON, record)


if __name__ == "__main__":
    unittest.main()
