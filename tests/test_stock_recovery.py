"""Known-region cleanup against synthetic verified current-firmware defaults."""
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mesh_temperature, mod_versions, mods, removal, screws, smoothing
from tests import test_helper_versions, test_mod_versions, test_smoothing_versions, test_untested_removal


def stock(sources):
    result = {"available": True, "reason": "", "identity": {"model": "F008", "firmware": "1.1.7.0", "variant": "F008_C13"},
              "files": {}, "guards": {}}
    for path, data in sources.items():
        source = "/rom" + path
        digest = mods.digest(data) if data is not None else None
        result["files"][path] = {"data": data, "sha256": digest, "source": source,
                                "available": True, "absent": data is None}
        result["guards"][source] = digest
    return result


def working(current):
    return {module.TARGET: value for module, value in engine.file_entries(current) if value is not None}


def forget_original(value):
    value.original, value.original_hash, value.original_absent = None, None, False
    value.backup_error = ""


class StockRecoveryTests(unittest.TestCase):
    def originals(self):
        return stock(test_mod_versions.SOURCES)

    def first_three(self):
        current = test_untested_removal.stamped()
        files = working(current)
        for path in test_mod_versions.SOURCES:
            forget_original(files[path])
        return files

    def helper(self):
        fixture = test_helper_versions.HelperVersionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        before, current, pending = fixture.installed(speed=True)
        files = working(current)
        defaults = stock({mesh.TARGET: before.mesh.data, screws.PYTHON: None,
                          screws.CONFIG: None, screws.LEGACY_CONFIG: None,
                          fluidd_screws.TARGET: before.screws.frontend[fluidd_screws.TARGET].data})
        return files, defaults, mod_versions.load(pending[mod_versions.TARGET])

    def test_first_three_recover_without_records_or_backups_and_preserve_unrelated_edits(self):
        files, defaults = self.first_three(), self.originals()
        extra = b"\n# unrelated new firmware edit\n"
        files[mods.TARGET].data += extra
        files[mods.TARGET].sha256 = mods.digest(files[mods.TARGET].data)
        generated = files[mesh.TARGET].data.replace(b"0.1, 0.2", b"0.4, 0.5")
        files[mesh.TARGET].data, files[mesh.TARGET].sha256 = generated, mods.digest(generated)
        plans = removal.inspect(files, {}, stock=defaults)
        for path in test_mod_versions.SOURCES:
            self.assertTrue(plans[path]["available"], plans[path])
            self.assertTrue(plans[path]["recovery"])
            self.assertTrue(plans[path]["defaults"])
            self.assertIn("1.1.7.0", plans[path]["details"])
            self.assertIn("F008_C13", plans[path]["details"])
        self.assertEqual(plans[mods.TARGET]["files"][mods.TARGET], test_mod_versions.SOURCES[mods.TARGET] + extra)
        self.assertEqual(plans[mesh_temperature.TARGET]["files"][mesh_temperature.TARGET], test_mod_versions.SOURCES[mesh_temperature.TARGET])
        self.assertIn(b"0.4, 0.5", plans[mesh.TARGET]["files"][mesh.TARGET])
        self.assertNotIn(mod_versions.TARGET, plans[mods.TARGET]["files"])
        self.assertIsNone(files[mods.TARGET].original)

    def test_missing_original_uses_current_firmware_values_not_old_record_defaults(self):
        current = test_untested_removal.stamped()
        records, files = mod_versions.load(current.versions.data), working(current)
        forget_original(files[mesh.TARGET])
        new_default = test_mod_versions.SOURCES[mesh.TARGET].replace(b"speed: 100", b"speed: 150")
        plan = removal.inspect(files, records, stock=stock({mesh.TARGET: new_default}))[mesh.TARGET]
        self.assertTrue(plan["available"], plan)
        self.assertEqual(mesh.Document(plan["files"][mesh.TARGET]).raw[("bed_mesh", "speed")], "150")

    def test_partial_mesh_requires_a_record_and_rejects_edited_record_context(self):
        current = test_untested_removal.stamped()
        records, files = mod_versions.load(current.versions.data), working(current)
        forget_original(files[mesh.TARGET])
        files[mesh.TARGET].data = files[mesh.TARGET].data.replace(b"speed: 600", b"speed: 100")
        files[mesh.TARGET].sha256 = mods.digest(files[mesh.TARGET].data)
        self.assertTrue(removal.inspect(files, records, stock=self.originals())[mesh.TARGET]["available"])
        self.assertFalse(removal.inspect(files, {}, stock=self.originals())[mesh.TARGET]["available"])
        chunk = records[mesh.TARGET]["undo"][0]["after"].encode()
        current = test_untested_removal.stamped()
        files = working(current)
        forget_original(files[mesh.TARGET])
        files[mesh.TARGET].data = files[mesh.TARGET].data.replace(chunk, chunk.replace(b"\n", b" # edited context\n", 1), 1)
        files[mesh.TARGET].sha256 = mods.digest(files[mesh.TARGET].data)
        plan = removal.inspect(files, records, stock=self.originals())[mesh.TARGET]
        self.assertFalse(plan["available"])
        self.assertIn("edited", plan["reason"])

    def test_partial_homing_recovers_retained_python_and_preserves_reset_macro(self):
        current = test_smoothing_versions.snapshot()
        test_smoothing_versions.install_records(current, engine.prepare_all(current, None, smoothing_desired=True))
        files = working(current)
        original = test_smoothing_versions.originals()
        files[smoothing.TARGET].data = original[smoothing.TARGET] + b"\n# unrelated firmware change\n"
        files[smoothing.TARGET].sha256 = mods.digest(files[smoothing.TARGET].data)
        forget_original(files[smoothing.TARGET])
        plan = removal.inspect(files, {}, stock=stock(original))[smoothing.TARGET]
        self.assertTrue(plan["available"], plan)
        self.assertEqual(plan["files"][smoothing.TARGET], files[smoothing.TARGET].data)
        self.assertEqual(plan["files"][smoothing.HOMING.TARGET], original[smoothing.HOMING.TARGET])

    def test_helper_recovers_missing_config_without_erasing_mesh_or_user_ui_edits(self):
        files, defaults, _ = self.helper()
        forget_original(files[mesh.TARGET])
        files[screws.CONFIG].data, files[screws.CONFIG].sha256 = None, None
        extra = b"\n// unrelated user interface edit\n"
        files[fluidd_screws.TARGET].data += extra
        files[fluidd_screws.TARGET].sha256 = mods.digest(files[fluidd_screws.TARGET].data)
        plan = removal.inspect(files, {}, stock=defaults)[screws.PYTHON]
        self.assertTrue(plan["available"], plan)
        self.assertIsNone(plan["files"][screws.PYTHON])
        self.assertIsNone(plan["files"][screws.CONFIG])
        self.assertIn(b"speed: 600", plan["files"][mesh.TARGET])
        self.assertNotIn(b"UNCURSER BEGIN bed_screws", plan["files"][mesh.TARGET])
        self.assertTrue(plan["files"][fluidd_screws.TARGET].endswith(extra))

    def test_damaged_backups_metadata_and_edited_helper_never_use_stock_fallback(self):
        for fault in ("backup", "metadata", "owned helper"):
            files, defaults, records = self.helper()
            forget_original(files[mesh.TARGET])
            error = ""
            if fault == "backup":
                files[screws.PYTHON].backup_error = "Backup checksum mismatch"
            elif fault == "metadata":
                error = "Uninstall records are damaged"
            else:
                files[screws.PYTHON].data += b"# edited owned module\n"
                files[screws.PYTHON].sha256 = mods.digest(files[screws.PYTHON].data)
            plan = removal.inspect(files, records, metadata_error=error, stock=defaults)[screws.PYTHON]
            self.assertFalse(plan["available"], plan)
            self.assertFalse(plan["recovery"])
            self.assertEqual(plan["files"], {})

    def test_oem_modlike_values_are_not_mistaken_for_an_unstamped_mesh_mod(self):
        files = self.first_three()
        defaults = stock({mesh.TARGET: files[mesh.TARGET].data})
        plan = removal.inspect(files, {}, stock=defaults)[mesh.TARGET]
        self.assertFalse(plan["present"])
        self.assertFalse(plan["available"])
        files, _, _ = self.helper()
        for value in files.values():
            forget_original(value)
        native = stock({path: files[path].data for path in
                        (mesh.TARGET, screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG, fluidd_screws.TARGET)})
        plan = removal.inspect(files, {}, stock=native)[screws.PYTHON]
        self.assertFalse(plan["present"])
        self.assertFalse(plan["available"])

    def test_oem_added_file_is_preserved_even_with_an_old_verified_absence_record(self):
        files, defaults, records = self.helper()
        native = b"# new OEM helper module\n"
        defaults["files"][screws.PYTHON].update(data=native, sha256=mods.digest(native), absent=False)
        defaults["guards"][defaults["files"][screws.PYTHON]["source"]] = mods.digest(native)
        plan = removal.inspect(files, records, stock=defaults)[screws.PYTHON]
        self.assertFalse(plan["available"], plan)
        self.assertIn("firmware supplies", plan["reason"])

    def test_unavailable_fluidd_and_bad_stock_proof_do_not_block_other_recoveries(self):
        files, defaults, _ = self.helper()
        forget_original(files[mesh.TARGET])
        defaults["files"][fluidd_screws.TARGET].update(available=False, reason="Different Fluidd asset", data=None, sha256=None, absent=False)
        plans = removal.inspect(files, {}, stock=defaults)
        self.assertTrue(plans[mesh.TARGET]["available"], plans[mesh.TARGET])
        self.assertFalse(plans[screws.PYTHON]["available"])
        defaults["guards"][defaults["files"][mesh.TARGET]["source"]] = "wrong"
        self.assertFalse(removal.inspect(files, {}, stock=defaults)[mesh.TARGET]["available"])

    def test_verified_original_and_record_remain_preferred_over_recovery_defaults(self):
        current = test_untested_removal.stamped()
        files, records = working(current), mod_versions.load(current.versions.data)
        defaults = self.originals()
        with patch("app.stock_recovery.early", side_effect=AssertionError("Fallback replaced verified removal")):
            plan = removal.inspect(files, records, stock=defaults)[mods.TARGET]
        self.assertTrue(plan["available"])
        self.assertFalse(plan["recovery"])
        self.assertEqual(plan["files"][mods.TARGET], current.original)


if __name__ == "__main__":
    unittest.main()
