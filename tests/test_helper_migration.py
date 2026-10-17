"""Legacy helper relocation preserves ownership, shared settings and originals."""
import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mod_versions, mods, screws
from app.config_blocks import LEGACY_SCREWS_INCLUDE
from app.remote import APPLY_BATCH, Connection
from tests import test_helper_versions, test_mods, test_screws


class HelperMigrationTests(unittest.TestCase):
    setUp = test_helper_versions.HelperVersionTests.setUp
    installed = test_helper_versions.HelperVersionTests.installed

    def legacy_installed(self, speed=False, calibration=False):
        before, current, _ = self.installed(speed=speed)
        current.mesh.data = current.mesh.data.replace(
            screws.SCREWS_INCLUDE.encode(), LEGACY_SCREWS_INCLUDE.encode(), 1)
        if calibration:
            current.mesh.data = current.mesh.data.replace(b"0.1, 0.2", b"0.3, 0.4")
        current.mesh.sha256 = mods.digest(current.mesh.data)
        legacy = copy.deepcopy(current.screws.files[screws.CONFIG])
        old_module, new_module = screws.LEGACY_FILE, screws.HelperFile(screws.CONFIG)
        legacy.backup_guards = {old_module.ORIGINAL if key == new_module.ORIGINAL
                               else old_module.MANIFEST if key == new_module.MANIFEST else key: value
                               for key, value in legacy.backup_guards.items()}
        current.screws.files[screws.LEGACY_CONFIG] = legacy
        current.screws.files[screws.CONFIG] = copy.deepcopy(before.screws.files[screws.CONFIG])
        records = mod_versions.load(current.versions.data)
        record = records[screws.PYTHON]
        record["version"] = "0.1.12"
        record["files"][screws.LEGACY_CONFIG] = record["files"].pop(screws.CONFIG)
        for change in record["files"][mesh.TARGET]["undo"]:
            for key in ("before", "after"):
                change[key] = change[key].replace(screws.SCREWS_INCLUDE, LEGACY_SCREWS_INCLUDE)
        test_helper_versions.stamp(current, records)
        return before, current

    def test_update_relocates_owned_config_and_captures_only_new_scope(self):
        before, current = self.legacy_installed()
        old_proof = copy.deepcopy(current.screws.files[screws.LEGACY_CONFIG])
        pending = engine.prepare_all(current, None, update=True)
        self.assertIsNone(pending[screws.LEGACY_CONFIG])
        self.assertEqual(pending[screws.CONFIG], screws.payload(screws.CONFIG))
        self.assertEqual(pending[mesh.TARGET], screws.edit_include(before.mesh.data, True))
        record = mod_versions.load(pending[mod_versions.TARGET])[screws.PYTHON]
        self.assertEqual(record["version"], mod_versions.VERSIONS[screws.PYTHON])
        self.assertEqual(set(record["files"]), set(mod_versions.FILES[screws.PYTHON]))
        self.assertNotIn(screws.LEGACY_CONFIG, record["files"])
        installed = {path: pending[path] for path in mod_versions.file_scope(screws.PYTHON)}
        self.assertEqual(mod_versions.undo_files(screws.PYTHON, installed, record),
                         engine.helper_current(before))
        self.assertEqual(current.screws.files[screws.LEGACY_CONFIG], old_proof)

    def test_disable_legacy_helper_does_not_touch_new_location(self):
        before, current = self.legacy_installed()
        current.screws.files[screws.CONFIG].data = b"unrelated user configuration\n"
        current.screws.files[screws.CONFIG].sha256 = mods.digest(current.screws.files[screws.CONFIG].data)
        pending = engine.prepare_all(current, None, screws_desired=False)
        self.assertNotIn(screws.CONFIG, pending)
        self.assertIsNone(pending[screws.LEGACY_CONFIG])
        self.assertIsNone(pending[screws.PYTHON])
        self.assertEqual(pending[mesh.TARGET], before.mesh.data)
        self.assertEqual(pending[fluidd_screws.TARGET], test_screws.FLUIDD_SOURCES[fluidd_screws.TARGET])
        self.assertNotIn(screws.PYTHON, mod_versions.load(pending[mod_versions.TARGET]))

    def test_occupied_new_location_blocks_update_but_not_verified_legacy_uninstall(self):
        _, current = self.legacy_installed()
        value = current.screws.files[screws.CONFIG]
        value.data = b"[user_configuration]\nvalue: 1\n"
        value.sha256 = mods.digest(value.data)
        value.analysis = {"known": False, "enabled": None, "patchable": False,
                          "label": "Unknown", "reason": "Unrelated occupied new destination"}
        with self.assertRaises(ValueError):
            engine.prepare_all(current, None, update=True)
        pending = engine.prepare_all(current, None, screws_desired=False)
        self.assertNotIn(screws.CONFIG, pending)
        self.assertIsNone(pending[screws.LEGACY_CONFIG])

    def test_unrelated_new_file_does_not_block_legacy_disable_after_inspection(self):
        _, current = self.legacy_installed()
        destination = current.screws.files[screws.CONFIG]
        destination.data = b"[user_configuration]\nvalue: 1\n"
        destination.sha256 = mods.digest(destination.data)
        documents = {fluidd_screws.INDEX: (
            '<script type="module" src="./' + fluidd_screws.ASSET + '"></script>').encode()}
        for module, value in engine.file_entries(current):
            if value is None:
                continue
            documents[module.TARGET] = value.data
            absent = getattr(value, 'original_absent', False)
            original = b'' if absent else value.original
            if original is not None:
                documents[module.ORIGINAL] = original
                documents[module.MANIFEST] = json.dumps(dict(path=module.TARGET,
                    sha256=mods.digest(original), absent=absent,
                    mode=0o644, uid=0, gid=0)).encode()
        documents[mods.TARGET] = test_mods.SOURCE
        tree = {'files': {path: {'data': base64.b64encode(documents[path]).decode(),
                                 'sha256': mods.digest(documents[path])}
                          for path in (mesh.TARGET, screws.LEGACY_CONFIG)},
                'patterns': {screws.LEGACY_CONFIG: [screws.LEGACY_CONFIG]}}

        class Reader:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def prefetch(self, paths):
                pass
            def read(self, name):
                data = documents.get(name)
                return None if data is None else {'data': data, 'sha256': mods.digest(data)}
            def identity(self):
                return {'history': True}
            def python(self, *args):
                return tree

        with patch.object(engine, 'Remote', return_value=Reader()), \
                patch.object(engine.network, 'status', return_value=current.status):
            scanned = engine.scan(Connection('192.0.2.1'), Path('unused'))
        self.assertTrue(scanned.screws.analysis['known'])
        self.assertTrue(scanned.screws.analysis['enabled'])
        with self.assertRaises(ValueError):
            engine.prepare_all(scanned, None, update=True)
        pending = engine.prepare_all(scanned, None, screws_desired=False)
        self.assertNotIn(screws.CONFIG, pending)
        self.assertIsNone(pending[screws.LEGACY_CONFIG])

    def test_full_restore_uses_legacy_absence_proof_without_new_location_backup(self):
        before, current = self.legacy_installed(calibration=True)
        self.assertIsNone(current.screws.files[screws.CONFIG].original_hash)
        pending = engine.prepare_all(current, None, restore=True)
        self.assertNotIn(screws.CONFIG, pending)
        self.assertIsNone(pending[screws.LEGACY_CONFIG])
        self.assertIsNone(pending[screws.PYTHON])
        self.assertEqual(pending[mesh.TARGET], before.mesh.data.replace(b"0.1, 0.2", b"0.3, 0.4"))
        self.assertIsNone(pending[mod_versions.TARGET])

    def test_relocation_preserves_mesh_mod_and_latest_generated_calibration(self):
        _, current = self.legacy_installed(speed=True, calibration=True)
        generated = mesh.split(current.mesh.data)[1]
        pending = engine.prepare_all(current, None, update=True)
        self.assertEqual(mesh.split(pending[mesh.TARGET])[1], generated)
        self.assertEqual(mesh.Document(pending[mesh.TARGET]).raw[('bed_mesh', 'speed')], '600')
        records = mod_versions.load(pending[mod_versions.TARGET])
        disabled_mesh = mod_versions.undo(pending[mesh.TARGET], records[mesh.TARGET])
        self.assertIn(screws.SCREWS_INCLUDE.encode(), disabled_mesh)
        self.assertNotIn(LEGACY_SCREWS_INCLUDE.encode(), disabled_mesh)
        self.assertEqual(mesh.split(disabled_mesh)[1], generated)
        self.assertEqual(mod_versions.undo_files(screws.PYTHON,
            {path: pending.get(path, engine.file_snapshot(current, path).data)
             for path in mod_versions.file_scope(screws.PYTHON)},
            records[screws.PYTHON])[mesh.TARGET],
            mesh.transform(test_screws.SOURCE, True).replace(b"0.1, 0.2", b"0.3, 0.4"))

    def test_apply_creates_new_dependency_before_include_and_deletes_old_after(self):
        _, current = self.legacy_installed()
        pending = engine.prepare_all(current, None, update=True)
        _, after, _ = self.installed()
        for path, data in pending.items():
            if path == mod_versions.TARGET:
                continue
            value = engine.file_snapshot(after, path)
            value.data = data
            value.sha256 = mods.digest(data) if data is not None else None
        test_helper_versions.stamp(after, mod_versions.load(pending[mod_versions.TARGET]))
        with patch.object(engine, 'scan', side_effect=[current, after]), \
                patch.object(engine.network, 'require_idle'), \
                patch.object(engine.network, 'status', return_value=current.status), \
                patch.object(engine, 'Remote') as remote:
            result = engine.apply(Connection('192.0.2.1'), Path('unused'), current, None, update=True)
        script, request = remote.return_value.__enter__.return_value.python.call_args.args
        self.assertEqual(script, APPLY_BATCH)
        paths = [item['path'] for item in request['files']]
        self.assertLess(paths.index(screws.CONFIG), paths.index(mesh.TARGET))
        self.assertLess(paths.index(mesh.TARGET), paths.index(screws.LEGACY_CONFIG))
        self.assertEqual(paths[-1], mod_versions.TARGET)
        target = next(item for item in request['files'] if item['path'] == screws.CONFIG)
        self.assertIsNone(target['expected'])
        self.assertEqual(base64.b64decode(target['data']), screws.payload(screws.CONFIG))
        self.assertTrue(result['written'])


if __name__ == '__main__':
    unittest.main()
