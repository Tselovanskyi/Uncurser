"""Ordinary update labels exclude conflicts and firmware repair issues."""
import copy
import unittest
from unittest.mock import patch

from app import engine, mesh, mod_versions, mods
from tests import test_firmware_engine, test_mod_versions


def outdated_snapshot():
    current = test_firmware_engine.installed(test_firmware_engine.CURRENT)
    records = mod_versions.load(current.versions.data)
    records[mesh.TARGET]["version"] = "0.1.5"
    test_firmware_engine.stamp(current, records)
    return current


class OutdatedTests(unittest.TestCase):
    def test_verified_outdated_mod_is_counted_and_latest_mods_are_not(self):
        current = outdated_snapshot()
        statuses = engine.update_statuses(current)
        issues = engine.firmware_issues(current)
        self.assertTrue(statuses[mesh.TARGET]["needed"])
        self.assertTrue(statuses[mesh.TARGET]["available"])
        self.assertEqual(engine.outdated_mods(current, statuses, issues), {mesh.TARGET})
        self.assertEqual(engine.outdated_mods(None, statuses, issues), set())

    def test_unknown_or_different_firmware_is_an_issue_instead_of_outdated(self):
        for firmware in (None, "1.1.6.4"):
            with self.subTest(firmware=firmware):
                current = outdated_snapshot()
                records = mod_versions.load(current.versions.data)
                if firmware is None:
                    records[mesh.TARGET].pop("firmware")
                else:
                    records[mesh.TARGET]["firmware"] = firmware
                test_firmware_engine.stamp(current, records)
                statuses = engine.update_statuses(current)
                issues = engine.firmware_issues(current)
                self.assertIn(mesh.TARGET, issues)
                self.assertTrue(statuses[mesh.TARGET]["available"])
                self.assertEqual(engine.outdated_mods(current, statuses, issues), set())

    def test_missing_backup_stays_blocked_without_counting_as_ordinary_update(self):
        current = outdated_snapshot()
        current.mesh.original = None
        statuses = engine.update_statuses(current)
        self.assertTrue(statuses[mesh.TARGET]["needed"])
        self.assertFalse(statuses[mesh.TARGET]["available"])
        self.assertTrue(statuses[mesh.TARGET]["reason"])
        self.assertEqual(engine.outdated_mods(current, statuses, {}), set())

    def test_unknown_incomplete_or_unpatchable_code_is_not_an_ordinary_update(self):
        for key, value in (("known", False), ("enabled", None), ("enabled", False), ("patchable", False)):
            with self.subTest(key=key, value=value):
                current = outdated_snapshot()
                current.mesh.analysis[key] = value
                statuses = engine.update_statuses(current)
                self.assertTrue(statuses[mesh.TARGET]["needed"])
                self.assertTrue(statuses[mesh.TARGET]["available"])
                self.assertEqual(engine.outdated_mods(current, statuses, {}), set())

    def test_legacy_enrollment_safety_is_unchanged_when_its_issue_hides_update_label(self):
        current = test_mod_versions.installed_snapshot()
        statuses = engine.update_statuses(current)
        original_statuses = copy.deepcopy(statuses)
        issues = engine.firmware_issues(current)
        self.assertEqual(set(issues), set(test_mod_versions.SOURCES))
        self.assertEqual(engine.outdated_mods(current, statuses, issues), set())
        self.assertEqual(statuses, original_statuses)
        for path in test_mod_versions.SOURCES:
            self.assertTrue(statuses[path]["needed"])
            self.assertTrue(statuses[path]["available"])

    def test_classification_never_repeats_expensive_checks(self):
        current = outdated_snapshot()
        statuses = engine.update_statuses(current)
        issues = engine.firmware_issues(current)
        with patch.object(engine, "update_statuses", side_effect=AssertionError("Repeated update check")), \
                patch.object(engine, "firmware_issues", side_effect=AssertionError("Repeated firmware check")), \
                patch.object(engine, "removal_statuses", side_effect=AssertionError("Repeated removal check")):
            self.assertEqual(engine.outdated_mods(current, statuses, issues), {mesh.TARGET})

    def test_update_batch_matches_displayed_eligible_mods_and_leaves_hidden_issues_alone(self):
        current = outdated_snapshot()
        records = mod_versions.load(current.versions.data)
        records[mods.TARGET].update(version="0.1.4")
        records[mods.TARGET].pop("firmware")
        test_firmware_engine.stamp(current, records)
        current.mesh_temperature.original = None
        statuses = engine.update_statuses(current)
        eligible = engine.outdated_mods(current, statuses, engine.firmware_issues(current))
        self.assertEqual(eligible, {mesh.TARGET})
        prepared = engine.prepare_all(current, None, update=True)
        self.assertEqual(set(prepared), {mesh.TARGET, mod_versions.TARGET})
        after = mod_versions.load(prepared[mod_versions.TARGET])
        self.assertEqual(after[mods.TARGET], records[mods.TARGET])
        self.assertEqual(after[mesh.TARGET]["version"], mod_versions.VERSIONS[mesh.TARGET])
        self.assertEqual(after[mesh.TARGET]["firmware"], test_firmware_engine.CURRENT)

    def test_update_only_with_hidden_issues_does_not_rewrite_records(self):
        current = outdated_snapshot()
        records = mod_versions.load(current.versions.data)
        records[mesh.TARGET].pop("firmware")
        test_firmware_engine.stamp(current, records)
        before = current.versions.data
        self.assertEqual(engine.prepare_all(current, None, update=True), {})
        self.assertEqual(current.versions.data, before)

    def test_hidden_issue_still_blocks_an_explicit_mod_choice(self):
        current = outdated_snapshot()
        current.mesh.original = None
        with self.assertRaisesRegex(ValueError, "backup"):
            engine.prepare_all(current, None, mesh_desired=True, update=True)

    def test_new_mod_still_requires_safe_mandatory_enrollment(self):
        current = test_mod_versions.helper_ready_snapshot()
        current.mesh.original = None
        with self.assertRaisesRegex(ValueError, "Cannot enable another mod"):
            engine.prepare_all(current, None, screws_desired=True, update=True)


if __name__ == "__main__":
    unittest.main()
