"""Model/firmware guards; all printer access is mocked."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mods, network
from app.remote import APPLY, APPLY_BATCH, Connection


SUPPORTED = {"native": {"model": "F008", "modelVersion":
    "printer hw ver:;printer sw ver:;DWIN hw ver:CR0CN240110C10;DWIN sw ver:1.1.6.4;"}}
UNSUPPORTED = {"native": {"model": "F008", "modelVersion": "1.1.6.40"}}


def snapshot(status):
    data = b"value = 1\n"
    return engine.Snapshot(data, mods.digest(data),
        {"known": True, "enabled": False, "patchable": True, "reason": ""},
        None, None, "", status, "", {"history": True}, 0)


class CompatibilityTests(unittest.TestCase):
    def test_native_release_field_and_plain_release_are_supported(self):
        self.assertEqual(network.device_identity(SUPPORTED), ("F008", "1.1.6.4"))
        self.assertEqual(network.compatibility_error(SUPPORTED), "")
        network.require_supported({"native": {"model": "F008", "modelVersion": "1.1.6.4"}})

    def test_missing_ambiguous_and_substring_matches_are_blocked(self):
        invalid = [None, {}, {"native": None}, {"native": []},
                   {"native": {"model": "F009", "modelVersion": "1.1.6.4"}}]
        for version in (None, "", "1.1.6.40", "11.1.6.4", "x1.1.6.4",
                        "DWIN hw ver:1.1.6.4;DWIN sw ver:1.1.7.0;",
                        "DWIN sw ver:1.1.6.4;DWIN sw ver:1.1.7.0;",
                        "printer sw ver:1.1.6.4;DWIN sw ver:;"):
            invalid.append({"native": {"model": "F008", "modelVersion": version}})
        for status in invalid:
            with self.subTest(status=status), self.assertRaises(ValueError):
                network.require_supported(status)

    def test_full_risk_cannot_prepare_mods_or_restore_on_unsupported_firmware(self):
        scanned = snapshot(UNSUPPORTED)
        scanned.original = scanned.data
        self.assertEqual(engine.restorable(scanned, full_risk=True), set())
        for restore in (False, True):
            for desired in (False, True):
                with self.subTest(restore=restore, desired=desired):
                    with self.assertRaises(ValueError):
                        engine.prepare(scanned, desired, restore=restore, full_risk=True)
                    with self.assertRaises(ValueError):
                        engine.prepare_all(scanned, desired, mesh_desired=desired,
                                           temperature_desired=desired, restore=restore, full_risk=True)

    def test_firmware_change_at_apply_rescan_blocks_both_write_paths(self):
        scanned = snapshot(SUPPORTED)
        fresh = copy.deepcopy(scanned)
        fresh.status = UNSUPPORTED
        for mesh_desired in (None, True):
            with self.subTest(batch=mesh_desired is not None), \
                    patch.object(engine, "scan", return_value=fresh), \
                    patch.object(engine, "Remote") as remote:
                with self.assertRaises(ValueError):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, True,
                                 full_risk=True, mesh_desired=mesh_desired)
                remote.assert_not_called()

    def test_final_identity_check_blocks_writes_after_successful_preparation(self):
        scanned = snapshot(SUPPORTED)
        new = b"value = 2\n"
        for mesh_desired in (None, True):
            with self.subTest(batch=mesh_desired is not None), \
                    patch.object(engine, "scan", return_value=scanned), \
                    patch.object(engine, "prepare", return_value=new), \
                    patch.object(engine, "prepare_all", return_value={mods.TARGET: new}), \
                    patch.object(network, "require_idle"), \
                    patch.object(network, "status", return_value=UNSUPPORTED), \
                    patch.object(engine, "Remote") as remote:
                remote.return_value.__enter__.return_value.python.return_value = True
                with self.assertRaises(ValueError):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), scanned, True,
                                 full_risk=True, mesh_desired=mesh_desired)
                calls = remote.return_value.__enter__.return_value.python.call_args_list
                self.assertTrue(all(call.args[0] not in (APPLY, APPLY_BATCH) for call in calls))


if __name__ == "__main__":
    unittest.main()
