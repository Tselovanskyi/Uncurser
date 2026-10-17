"""Validate helper relocation records without executing printer metadata."""
import copy
import unittest

from app import fluidd_screws, mesh, mod_versions, screws


def fixture(config, revision):
    baseline = {mesh.TARGET: b"printer baseline\n", screws.PYTHON: None,
                config: None, fluidd_screws.TARGET: b"original dialog\n"}
    installed = {mesh.TARGET: b"printer baseline\nhelper include\n",
                 screws.PYTHON: b"installed helper Python\n", config: b"helper configuration\n",
                 fluidd_screws.TARGET: b"changed dialog\n"}
    record = {"version": revision, "files": {
        mesh.TARGET: {"undo": [{"before": "", "after": "helper include\n"}]},
        screws.PYTHON: {"created": installed[screws.PYTHON].decode()},
        config: {"created": installed[config].decode()},
        fluidd_screws.TARGET: {"undo": [{"before": "original dialog\n", "after": "changed dialog\n"}]}}}
    return baseline, installed, record


class HelperScopeTests(unittest.TestCase):
    def test_legacy_record_uninstalls_only_its_verified_old_paths(self):
        baseline, installed, record = fixture(screws.LEGACY_CONFIG, "0.1.12")
        self.assertEqual(mod_versions.load(mod_versions.dump({screws.PYTHON: record})),
                         {screws.PYTHON: record})
        self.assertEqual(set(mod_versions.file_scope(screws.PYTHON, record)), set(baseline))
        self.assertEqual(mod_versions.undo_files(screws.PYTHON, installed, record), baseline)
        self.assertNotIn(screws.CONFIG, baseline)

    def test_new_records_capture_only_current_paths(self):
        baseline, installed, _ = fixture(screws.CONFIG, "0.1.15")
        replacements = {mesh.TARGET: [("", "helper include\n")],
                        fluidd_screws.TARGET: [("original dialog\n", "changed dialog\n")]}
        record = mod_versions.capture_files(screws.PYTHON, baseline, installed, replacements)
        self.assertEqual(record["version"], "0.1.15")
        self.assertEqual(set(record["files"]), set(mod_versions.FILES[screws.PYTHON]))
        self.assertNotIn(screws.LEGACY_CONFIG, record["files"])
        self.assertEqual(mod_versions.undo_files(screws.PYTHON, installed, record), baseline)
        self.assertEqual(mod_versions.file_scope(screws.PYTHON), mod_versions.FILES[screws.PYTHON])
        self.assertNotIn(screws.LEGACY_CONFIG, mod_versions.ADDED_FILES[screws.PYTHON])

    def test_legacy_scope_cannot_be_stamped_as_new_or_future_revision(self):
        for revision in ("0.1.15", "0.1.16", "9.0.0"):
            with self.subTest(revision=revision):
                _, _, record = fixture(screws.LEGACY_CONFIG, revision)
                with self.assertRaises(ValueError):
                    mod_versions.dump({screws.PYTHON: record})

    def test_unknown_union_and_incomplete_scopes_are_rejected(self):
        _, _, legacy = fixture(screws.LEGACY_CONFIG, "0.1.12")
        unknown = copy.deepcopy(legacy)
        unknown["files"]["/tmp/unrelated.cfg"] = unknown["files"].pop(screws.LEGACY_CONFIG)
        union = copy.deepcopy(legacy)
        union["files"][screws.CONFIG] = {"created": "new helper configuration\n"}
        incomplete = copy.deepcopy(legacy)
        incomplete["files"].pop(screws.LEGACY_CONFIG)
        for record in (unknown, union, incomplete):
            with self.subTest(files=list(record["files"])):
                with self.assertRaises(ValueError):
                    mod_versions.dump({screws.PYTHON: record})

    def test_created_file_proof_depends_on_the_selected_scope(self):
        for config, revision in ((screws.LEGACY_CONFIG, "0.1.12"),
                                 (screws.CONFIG, "0.1.15")):
            with self.subTest(config=config):
                _, _, record = fixture(config, revision)
                record["files"][config] = {"undo": [{"before": "old", "after": "new"}]}
                with self.assertRaises(ValueError):
                    mod_versions.dump({screws.PYTHON: record})

    def test_uninstall_requires_exact_scope_and_installed_ownership(self):
        _, installed, record = fixture(screws.LEGACY_CONFIG, "0.1.12")
        changed = dict(installed)
        changed[screws.LEGACY_CONFIG] += b"user edit\n"
        current_scope = dict(installed)
        current_scope[screws.CONFIG] = current_scope.pop(screws.LEGACY_CONFIG)
        extra = dict(installed, **{"/tmp/unrelated.cfg": b"user file"})
        for files in (changed, current_scope, extra):
            with self.subTest(files=list(files)):
                with self.assertRaises(ValueError):
                    mod_versions.undo_files(screws.PYTHON, files, record)

    def test_new_capture_rejects_legacy_file_scope(self):
        baseline, installed, _ = fixture(screws.LEGACY_CONFIG, "0.1.12")
        with self.assertRaises(ValueError):
            mod_versions.capture_files(screws.PYTHON, baseline, installed, {})


if __name__ == "__main__":
    unittest.main()
