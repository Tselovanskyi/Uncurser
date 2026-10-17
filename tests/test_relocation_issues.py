"""Legacy storage appears in issue scans independently of installed mod changes."""
import unittest

from app import engine, mods, network, screws, storage
from tests.test_firmware_engine import CURRENT, inactive, installed


def with_move(snapshot):
    snapshot.storage_moves = ({
        "source": "/mnt/UDISK/printer_data/config/printer.cfn.Original.txt",
        "destination": storage.ROOT + "/printer.cfn.Original.txt",
        "source_hash": mods.digest(b"original\n"), "destination_hash": None,
        "attrs": {"mode": 0o444, "uid": 0, "gid": 0},
    },)
    return snapshot


def with_obsolete(snapshot, target=mods.TARGET):
    backup = storage.legacy_path(storage.original(target))
    snapshot.storage_cleanup = ({"target": target, "backup": backup, "model": "F008",
        "firmware": "1.1.6.4", "reason": "obsolete", "files": [
            {"path": path, "sha256": mods.digest(b"old backup"), "attrs": {"mode": 0o444, "uid": 0, "gid": 0}}
            for path in (backup, backup + ".json")]},)
    return snapshot


class RelocationIssueTests(unittest.TestCase):
    def test_current_stamped_active_owner_is_included_when_its_backup_is_obsolete(self):
        snapshot = with_obsolete(installed(CURRENT))
        issues = engine.firmware_issues(snapshot)
        self.assertEqual(set(issues), {mods.TARGET, storage.ROOT})
        self.assertTrue(issues[mods.TARGET]["available"])
        self.assertEqual(issues[mods.TARGET]["installed_firmware"], CURRENT)
        self.assertEqual(issues[mods.TARGET]["current_firmware"], CURRENT)
        self.assertEqual(issues[mods.TARGET]["firmware_reason"], "Its original backup belongs to older firmware.")
        self.assertTrue(issues[storage.ROOT]["available"])

    def test_obsolete_backup_does_not_bypass_owner_removal_verification(self):
        snapshot = with_obsolete(installed(CURRENT))
        blocked = {mods.TARGET: {"present": True, "available": False, "files": {}, "reason": "Owned code was edited."}}
        issues = engine.firmware_issues(snapshot, removals=blocked)
        self.assertFalse(issues[mods.TARGET]["available"])
        self.assertEqual(issues[mods.TARGET]["reason"], "Owned code was edited.")
        self.assertFalse(issues[storage.ROOT]["available"])

    def test_inactive_owner_is_not_added_for_an_obsolete_backup(self):
        issues = engine.firmware_issues(with_obsolete(inactive()))
        self.assertEqual(set(issues), {storage.ROOT})
        self.assertTrue(issues[storage.ROOT]["available"])

    def test_relocation_is_actionable_without_removing_or_updating_any_mod(self):
        for snapshot in (inactive(), installed(CURRENT)):
            with self.subTest(installed=snapshot.analysis["enabled"]):
                with_move(snapshot)
                records = snapshot.versions.data if snapshot.versions else None
                issues = engine.firmware_issues(snapshot)
                self.assertEqual(issues, {storage.ROOT: {
                    "kind": "relocation", "present": True,
                    "available": True, "reason": "",
                }})
                self.assertEqual(engine.prepare_all(snapshot, None), {})
                self.assertEqual(snapshot.versions.data if snapshot.versions else None, records)
                snapshot.storage_moves = ()
                self.assertEqual(engine.firmware_issues(snapshot), {})

    def test_damaged_storage_remains_blocked_even_with_an_available_helper_removal(self):
        snapshot = with_move(inactive())
        snapshot.storage_error = "The original backup checksum is invalid."
        issues = engine.firmware_issues(snapshot, removals={
            screws.PYTHON: {"present": True, "available": True, "files": {}, "reason": ""},
        })
        self.assertTrue(issues[screws.PYTHON]["available"])
        self.assertFalse(issues[storage.ROOT]["available"])
        self.assertEqual(issues[storage.ROOT]["reason"], snapshot.storage_error)

    def test_missing_include_requires_an_available_helper_issue_in_the_same_fix(self):
        snapshot = with_move(inactive())
        snapshot.storage_error = engine.INCLUDE_STORAGE_ERROR
        for present, available in ((True, True), (True, False), (False, True)):
            with self.subTest(present=present, available=available):
                issues = engine.firmware_issues(snapshot, removals={
                    screws.PYTHON: {"present": present, "available": available, "files": {}, "reason": ""},
                })
                allowed = present and available
                self.assertEqual(issues[storage.ROOT]["available"], allowed)
                self.assertEqual(issues[storage.ROOT]["reason"], "" if allowed else engine.INCLUDE_STORAGE_ERROR)

    def test_relocation_does_not_make_unsupported_printers_actionable(self):
        snapshot = with_move(inactive())
        snapshot.status["native"]["model"] = "F009"
        self.assertEqual(network.firmware_state(snapshot.status), "unsupported")
        self.assertEqual(engine.firmware_issues(snapshot), {})


if __name__ == "__main__":
    unittest.main()
