"""Source metadata follows each mod's verified install and removal ownership."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mod_comments, mod_versions, mods, screws, warmup
from tests import test_screws


HEATERS = Path(__file__).resolve().parents[1] / "build/firmware-analysis/1.1.7.0/rootfs/usr/share/klipper/klippy/extras/heaters.py"


def installed(before, pending):
    """Represent the files and immutable originals after the planned install."""
    current = copy.deepcopy(before)
    for path, data in pending.items():
        if path == mod_versions.TARGET and current.versions is None:
            current.versions = engine.MeshSnapshot(None, None,
                {"known": True, "enabled": False, "patchable": True, "label": "Not installed"})
        value = engine.file_snapshot(current, path)
        if value.original_hash is None:
            value.original = value.data
            value.original_absent = value.data is None
            value.original_hash = mods.digest(value.data if value.data is not None else b"")
            value.original_identity = ("F008", "1.1.6.4")
        value.data, value.sha256 = data, mods.digest(data) if data is not None else None
        if path == mod_versions.TARGET:
            value.analysis = {"known": True, "enabled": True, "patchable": True, "label": "Version records"}
    return current


class ModCommentsEngineTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_screws.SOURCE)))
        self.enterContext(patch.object(fluidd_screws, "FILES", tuple(
            fluidd_screws.FluiddFile(path, mods.digest(data))
            for path, data in test_screws.FLUIDD_SOURCES.items())))

    def warmup_snapshot(self):
        if not HEATERS.exists():
            self.skipTest("The local native heater fixture is unavailable")
        current = test_screws.snapshot()
        data = HEATERS.read_bytes()
        current.warmup = engine.MeshSnapshot(data, mods.digest(data), warmup.inspect(data))
        return current

    def test_warmup_install_captures_its_comment_and_exact_native_undo(self):
        before = self.warmup_snapshot()
        pending = engine.prepare_all(before, None, warmup_desired=True)
        self.assertEqual(set(pending), {warmup.TARGET, mod_versions.TARGET})
        data = pending[warmup.TARGET]
        self.assertEqual(mod_comments.read(data), {warmup.TARGET: {
            "version": mod_versions.VERSIONS[warmup.TARGET], "firmware": "1.1.6.4"}})
        self.assertTrue(warmup.inspect(data)["enabled"])
        record = mod_versions.load(pending[mod_versions.TARGET])[warmup.TARGET]
        self.assertEqual(mod_versions.undo(data, record), before.warmup.data)
        self.assertFalse(mod_comments.read(before.warmup.data))

    def test_warmup_removal_preserves_user_edits_and_removes_only_its_comment(self):
        before = self.warmup_snapshot()
        current = installed(before, engine.prepare_all(before, None, warmup_desired=True))
        user_edit = b"\n# user note preserved\n"
        current.warmup.data += user_edit
        current.warmup.sha256 = mods.digest(current.warmup.data)
        pending = engine.prepare_all(current, None, warmup_desired=False)
        self.assertEqual(pending[warmup.TARGET], before.warmup.data + user_edit)
        self.assertFalse(mod_comments.read(pending[warmup.TARGET]))
        self.assertNotIn(warmup.TARGET, mod_versions.load(pending[mod_versions.TARGET]))

    def test_shared_helper_and_mesh_comments_remove_independently(self):
        before = test_screws.snapshot()
        pending = engine.prepare_all(before, None, mesh_desired=True, screws_desired=True)
        records = mod_versions.load(pending[mod_versions.TARGET])
        self.assertEqual(set(mod_comments.read(pending[mesh.TARGET])), {mesh.TARGET, screws.PYTHON})
        current = installed(before, pending)

        mesh_removed = engine.prepare_all(current, None, mesh_desired=False)
        self.assertEqual(set(mod_comments.read(mesh_removed[mesh.TARGET])), {screws.PYTHON})
        self.assertIn(screws.SCREWS_INCLUDE.encode(), mesh_removed[mesh.TARGET])
        self.assertFalse(mesh.inspect(mesh_removed[mesh.TARGET], before.mesh.data)["enabled"])
        self.assertEqual(mod_versions.load(mesh_removed[mod_versions.TARGET])[screws.PYTHON], records[screws.PYTHON])

        helper_removed = engine.prepare_all(current, None, screws_desired=False)
        self.assertEqual(set(mod_comments.read(helper_removed[mesh.TARGET])), {mesh.TARGET})
        self.assertNotIn(screws.SCREWS_INCLUDE.encode(), helper_removed[mesh.TARGET])
        self.assertTrue(mesh.inspect(helper_removed[mesh.TARGET], before.mesh.data)["enabled"])
        self.assertEqual(mod_versions.load(helper_removed[mod_versions.TARGET])[mesh.TARGET], records[mesh.TARGET])


if __name__ == "__main__":
    unittest.main()
