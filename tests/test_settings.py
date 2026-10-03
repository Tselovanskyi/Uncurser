import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import paramiko

from app import settings
from app.remote import Connection, NewHostKey, Remote, trust_key


class SettingsTests(unittest.TestCase):
    def setUp(self):
        folder = Path(__file__).resolve().parents[1] / "build" / "settings-check"
        folder.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=folder)
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / settings.FILENAME
        self.key = paramiko.RSAKey.generate(1024)
        self.printers = [{"name": "Printer", "host": "192.0.2.1", "username": "root", "password": "test-password"}]

    def test_printers_and_host_keys_share_one_file(self):
        settings.update(self.path, printers=self.printers)
        trust_key(self.path, NewHostKey("192.0.2.1", self.key))
        settings.update(self.path, printers=[{**self.printers[0], "name": "Renamed"}])
        self.assertEqual(settings.load(self.path)["printers"][0]["name"], "Renamed")
        remote = Remote(Connection("192.0.2.1"), self.path)
        keys = remote.client.get_host_keys()
        self.assertTrue(keys.check("192.0.2.1", self.key))
        self.assertFalse(keys.check("192.0.2.1", paramiko.RSAKey.generate(1024)))
        remote.client.close()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_migration_preserves_legacy_data_and_does_not_repeat(self):
        legacy = self.path.parent / "data"
        legacy.mkdir()
        (legacy / "settings.json").write_text(json.dumps({"printers": self.printers}), encoding="utf-8")
        keys = paramiko.HostKeys()
        keys.add("192.0.2.1", self.key.get_name(), self.key)
        keys.save(str(legacy / "known_hosts"))
        before = {p.name: p.read_bytes() for p in legacy.iterdir()}
        settings.migrate(self.path)
        self.assertEqual(settings.load(self.path)["printers"], self.printers)
        self.assertTrue(settings.host_keys(settings.load(self.path)).check("192.0.2.1", self.key))
        settings.update(self.path, printers=[])
        settings.migrate(self.path)
        self.assertEqual(settings.load(self.path)["printers"], [])
        self.assertEqual({p.name: p.read_bytes() for p in legacy.iterdir()}, before)

    def test_failed_save_and_damaged_settings_are_not_overwritten(self):
        settings.update(self.path, printers=self.printers)
        original = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("Blocked")):
            with self.assertRaises(OSError):
                settings.update(self.path, printers=[])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.path.write_text("damaged", encoding="utf-8")
        with self.assertRaises(ValueError):
            settings.update(self.path, printers=[])
        self.assertEqual(self.path.read_text(), "damaged")
