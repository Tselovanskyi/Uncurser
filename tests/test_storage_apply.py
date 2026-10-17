"""Storage-only Apply checks with synthetic snapshots and no network access."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mods
from app.remote import APPLY_BATCH, Connection
from tests import test_mods


class StorageApplyTests(unittest.TestCase):
    def snapshot(self, moves=True):
        source = test_mods.SOURCE
        snapshot = engine.Snapshot(source, mods.digest(source),
            {"enabled": False, "known": True, "patchable": True}, None, None, "",
            {"native": {"model": "F008", "modelVersion": "1.1.6.4"}}, "", {}, 0)
        if moves:
            snapshot.storage_moves = ({
                "source": "/mnt/UDISK/printer_data/config/printer.cfn.Original.txt",
                "destination": "/mnt/UDISK/Uncurser/printer.cfn.Original.txt",
                "source_hash": mods.digest(b"original\n"), "destination_hash": None,
                "attrs": {"mode": 0o444, "uid": 0, "gid": 0},
            },)
        return snapshot

    def test_migration_only_uses_guarded_batch_without_mod_writes_or_restart(self):
        before, after = self.snapshot(), self.snapshot(False)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(before), after]), \
                patch.object(engine, "Remote") as remote, \
                patch.object(engine.network, "status", return_value=before.status), \
                patch.object(engine.network, "require_idle") as idle, \
                patch.object(engine.network, "http") as http:
            session = remote.return_value.__enter__.return_value
            session.python.return_value = {"saved": [], "cleared": [], "moved": [before.storage_moves[0]["source"]]}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        self.assertEqual(session.python.call_count, 1)
        script, request = session.python.call_args.args
        self.assertEqual(script, APPLY_BATCH)
        self.assertEqual(request["files"], [])
        self.assertEqual(request["python_caches"], {})
        self.assertEqual(request["moves"], list(before.storage_moves))
        move = before.storage_moves[0]
        self.assertEqual(request["guards"][move["destination"]], move["source_hash"])
        self.assertEqual(idle.call_count, 2)
        http.assert_not_called()
        self.assertTrue(result["written"])
        self.assertFalse(result["power_cycle"])
        self.assertFalse(result["restarted"])
        self.assertIs(result["snapshot"], after)

    def test_changed_storage_plan_blocks_before_opening_write_session(self):
        for change in ("source_hash", "destination", "attrs", "removed"):
            with self.subTest(change=change):
                before, fresh = self.snapshot(), self.snapshot()
                if change == "removed":
                    fresh.storage_moves = ()
                elif change == "attrs":
                    fresh.storage_moves[0]["attrs"]["mode"] = 0o644
                else:
                    fresh.storage_moves[0][change] += "-changed"
                with patch.object(engine, "scan", return_value=fresh), \
                        patch.object(engine.network, "require_idle"), \
                        patch.object(engine, "Remote") as remote, \
                        self.assertRaisesRegex(RuntimeError, "Backup storage changed"):
                    engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
                remote.assert_not_called()

    def test_storage_error_blocks_before_write(self):
        before = self.snapshot()
        before.storage_error = "The original backup checksum is invalid."
        with patch.object(engine, "scan", return_value=before), \
                patch.object(engine.network, "require_idle"), \
                patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(ValueError, "storage migration is blocked"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        remote.assert_not_called()

    def test_storage_only_does_not_bypass_firmware_or_idle_guards(self):
        before = self.snapshot()
        unsupported = copy.deepcopy(before)
        unsupported.status["native"]["model"] = "F009"
        with patch.object(engine, "scan") as scan, patch.object(engine, "Remote") as remote, \
                self.assertRaises(ValueError):
            engine.apply(Connection("192.0.2.1"), Path("unused"), unsupported, None)
        scan.assert_not_called()
        remote.assert_not_called()
        with patch.object(engine, "scan", return_value=before), \
                patch.object(engine.network, "require_idle", side_effect=RuntimeError("Printer is printing")), \
                patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(RuntimeError, "printing"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), before, None)
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
