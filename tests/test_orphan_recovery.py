"""Orphan safeguard discovery and cleanup use local fixtures and mocked SSH only."""
import contextlib
import copy
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch

from app import engine, mesh, mod_versions, mods, screws, storage
from app.remote import APPLY_BATCH, Connection
from tests import test_firmware_engine, test_recovery, test_screws


def inactive():
    current = test_firmware_engine.inactive()
    current.screws = test_screws.snapshot().screws
    return current


def candidate(target=screws.PYTHON, data=b"previously removed helper\n"):
    digest = mods.digest(data)
    return {"target": target, "path": storage.recovery(target, digest),
            "sha256": digest, "attrs": {"mode": 0o644, "uid": 0, "gid": 0}}


class OrphanRecoveryTests(unittest.TestCase):
    def test_inventory_is_read_only_and_excludes_non_recovery_data(self):
        parent = Path(__file__).resolve().parents[1] / "build" / "file-checks"
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as directory, patch.object(storage, "ROOT", Path(directory).as_posix()):
            root = Path(directory)
            data = b"old helper source\n"
            verified = Path(storage.recovery(screws.PYTHON, mods.digest(data)))
            preserved = {
                verified: data,
                Path(storage.recovery(screws.PYTHON, "0" * 64)): b"wrong checksum",
                root / "recovery" / ("fluidd-category-" + mods.digest(data) + ".txt"): data,
                root / "recovery" / ("unrelated." + mods.digest(data) + ".txt"): data,
                root / "originals" / "screws_tilt_adjust.ptn.Original.txt": b"",
                root / "originals" / "screws_tilt_adjust.ptn.Original.txt.json": b'{"absent":true}',
            }
            for path, contents in preserved.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)

            def local_python(program, payload):
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(output):
                    exec(program, {})
                result = json.loads(output.getvalue())
                # The printer uses POSIX paths; Windows glob joins locally with \\.
                for item in result:
                    item["path"] = Path(item["path"]).as_posix()
                return result

            result = storage.recovery_copies(Mock(python=local_python), [screws.PYTHON])
            self.assertEqual([(item["target"], item["path"], item["sha256"]) for item in result],
                             [(screws.PYTHON, verified.as_posix(), mods.digest(data))])
            self.assertEqual({path: path.read_bytes() for path in preserved}, preserved)
            self.assertEqual(set(root.rglob("*.txt")), {path for path in preserved if path.suffix == ".txt"})

    def test_only_proven_removed_owners_allow_cleanup(self):
        current = inactive()
        helper, shared = candidate(), candidate(mesh.TARGET)
        self.assertEqual(engine.orphan_recovery_copies(current, (helper, shared)), (helper, shared))
        current.mesh.analysis["enabled"] = True
        self.assertEqual(engine.orphan_recovery_copies(current, (helper, shared)), (helper,))
        current.mesh.analysis.update(known=False, enabled=None)
        self.assertEqual(engine.orphan_recovery_copies(current, (shared,)), ())
        current = inactive()
        record = {"future-mod": {"version": "0.2.0", "firmware": "1.1.7.0"}}
        data = mod_versions.dump(record)
        current.versions = engine.MeshSnapshot(data, mods.digest(data), {"known": True, "enabled": True})
        self.assertEqual(engine.orphan_recovery_copies(current, (helper,)), ())

    def test_fix_issues_handles_only_orphan_copies_without_mod_removal(self):
        current = inactive()
        current.recovery_cleanup = (candidate(),)
        issues = engine.firmware_issues(current)
        self.assertEqual(set(issues), {storage.ROOT})
        self.assertTrue(issues[storage.ROOT]["available"])
        self.assertTrue(issues[storage.ROOT]["recovery"])
        with patch.object(engine, "apply", return_value={"written": True}) as apply:
            result = engine.fix_issues(Connection("192.0.2.1"), Path("unused"), current)
        self.assertTrue(result["written"])
        self.assertTrue(apply.call_args.kwargs["cleanup_recovery"])
        self.assertFalse(any(value is False for key, value in apply.call_args.kwargs.items()
                             if key == "desired" or key.endswith("_desired")))

    def test_orphan_only_apply_uses_batch_guards_without_restart_or_mod_writes(self):
        before, after = inactive(), inactive()
        before.recovery_cleanup = (candidate(),)
        with patch.object(engine, "scan", side_effect=[copy.deepcopy(before), after]), \
                patch.object(engine.network, "status", return_value=before.status), \
                patch.object(engine.network, "require_idle"), \
                patch.object(engine, "Remote") as remote:
            session = remote.return_value.__enter__.return_value
            session.python.return_value = {"saved": [], "recovery_removed": [candidate()["path"]]}
            result = engine.apply(Connection("192.0.2.1"), Path("unused"), before, None, cleanup_recovery=True)
        script, request = session.python.call_args.args
        self.assertEqual(script, APPLY_BATCH)
        self.assertEqual(request["files"], [])
        self.assertEqual(request["recovery_inventory"], list(before.recovery_cleanup))
        self.assertIsNone(request["recovery_targets"][screws.PYTHON])
        self.assertIsNone(request["guards"][screws.PYTHON])
        self.assertTrue(result["written"])
        self.assertFalse(result["power_cycle"])
        self.assertFalse(result["restarted"])

    def test_changed_inventory_blocks_before_opening_write_session(self):
        before, fresh = inactive(), inactive()
        before.recovery_cleanup = (candidate(),)
        fresh.recovery_cleanup = (candidate(data=b"a different verified copy\n"),)
        with patch.object(engine, "scan", return_value=fresh), \
                patch.object(engine.network, "require_idle"), \
                patch.object(engine, "Remote") as remote, \
                self.assertRaisesRegex(RuntimeError, "[Rr]ecovery.*changed"):
            engine.apply(Connection("192.0.2.1"), Path("unused"), before, None, cleanup_recovery=True)
        remote.assert_not_called()

    def test_cleanup_only_batch_revalidates_copy_and_preserves_unrelated_data(self):
        for altered in (False, True):
            with self.subTest(altered=altered):
                fixture = test_recovery.RecoveryWriteTests()
                fixture.setUp()
                try:
                    fixture.backups.mkdir()
                    data = b"leftover recovery safeguard\n"
                    path = fixture.backups / ("printer." + mods.digest(data) + ".txt")
                    path.write_bytes(data)
                    info = path.stat()
                    preserved = {
                        fixture.backups / ("fluidd-category-" + mods.digest(data) + ".txt"): data,
                        fixture.root / "originals" / "printer.cfn.Original.txt": b"immutable baseline",
                        fixture.root / "originals" / "printer.cfn.Original.txt.json": b"immutable manifest",
                    }
                    for kept, contents in preserved.items():
                        kept.parent.mkdir(parents=True, exist_ok=True)
                        kept.write_bytes(contents)
                    fixture.request = {"files": [], "guards": {str(fixture.path): mods.digest(fixture.before)},
                        "recovery_targets": {str(fixture.path): mods.digest(fixture.before)},
                        "recovery_inventory": [{"target": str(fixture.path), "path": str(path),
                            "sha256": mods.digest(data), "attrs": {"mode": stat.S_IMODE(info.st_mode),
                                "uid": info.st_uid, "gid": info.st_gid}}]}
                    if altered:
                        path.write_bytes(b"changed after scan")
                        with self.assertRaisesRegex(ValueError, "[Rr]ecovery.*changed"):
                            fixture.run_write()
                        self.assertTrue(path.exists())
                    else:
                        fixture.run_write()
                        self.assertFalse(path.exists())
                    self.assertEqual(fixture.path.read_bytes(), fixture.before)
                    self.assertEqual({kept: kept.read_bytes() for kept in preserved}, preserved)
                    fixture.assert_no_temporary_files()
                finally:
                    fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
