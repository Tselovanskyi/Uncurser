"""Helper installation, shared-config edits and rollback, using local fixtures."""
import base64
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app import engine, fluidd_screws, mesh, mod_versions, mods, screws
from app.remote import APPLY_BATCH, Connection
from tests import test_mesh as mesh_tests

SOURCE = mesh_tests.SOURCE
FLUIDD_SOURCES = {
    fluidd_screws.TARGET: ("// synthetic bundle\n" + ";\n".join(
        before for before, _ in fluidd_screws.blocks(fluidd_screws.TARGET)) + ";\n").encode(),
}


def record(data):
    return None if data is None else {"data": data, "sha256": mods.digest(data)}


def snapshot(enabled=False, originals=False, speed=False, grid=None):
    grid = enabled if grid is None else grid
    config = mesh.transform(SOURCE, speed, SOURCE)
    config = screws.edit_include(config, enabled)
    with patch.object(mesh, 'REFERENCE_HASH', mesh.fingerprint(SOURCE)):
        root = engine.MeshSnapshot(config, mods.digest(config), mesh.inspect(config, SOURCE if originals else None),
                                   SOURCE if originals else None, mods.digest(SOURCE) if originals else None,
                                   original_identity=("F008", "1.1.6.4") if originals else None)
    class Reader:
        def read(self, path):
            for module in screws.FILES:
                if path == module.TARGET:
                    data = screws.payload(path)
                    return record(data) if enabled else None
                if path == module.ORIGINAL:
                    return record(b'') if originals else None
                if path == module.MANIFEST and originals:
                    return record(json.dumps(dict(path=module.TARGET, sha256=mods.digest(b''), absent=True,
                                                  mode=0o644, uid=0, gid=0, model="F008", firmware="1.1.6.4")).encode())
            if path == fluidd_screws.INDEX:
                return record(('<script type="module" src="./' + fluidd_screws.ASSET + '"></script>').encode())
            for module in fluidd_screws.FILES:
                original = FLUIDD_SOURCES[module.TARGET]
                if path == module.TARGET:
                    return record(fluidd_screws.transform(path, original, grid))
                if path == module.ORIGINAL and originals and grid:
                    return record(original)
                if path == module.MANIFEST and originals and grid:
                    return record(json.dumps(dict(path=module.TARGET, sha256=mods.digest(original),
                                                  mode=0o644, uid=0, gid=0, model="F008", firmware="1.1.6.4")).encode())
            return None
    files = {mesh.TARGET: {'data': base64.b64encode(config).decode()}}
    if enabled:
        files[screws.CONFIG] = {'data': base64.b64encode(screws.payload(screws.CONFIG)).decode()}
    tree = {'files': files, 'patterns': {screws.CONFIG: [screws.CONFIG]} if enabled else {}}
    fixture_modules = tuple(fluidd_screws.FluiddFile(path, mods.digest(data)) for path, data in FLUIDD_SOURCES.items())
    with patch.object(fluidd_screws, 'FILES', fixture_modules):
        helper = engine.scan_screws(Reader(), root, tree)
    return engine.Snapshot(b'', mods.digest(b''), {'enabled': False, 'known': True, 'patchable': True, 'label': 'Original'}, None, None, '',
                           {'info': {}, 'native': {'model': 'F008', 'modelVersion': '1.1.6.4'}}, '', {}, 0,
                           root, None, helper, original_identity=("F008", "1.1.6.4") if originals else None)


class ScrewsTests(unittest.TestCase):
    def test_enable_then_disable_preserves_other_mod_and_calibration(self):
        before = snapshot()
        combined = engine.prepare_all(before, None, mesh_desired=True, screws_desired=True)
        self.assertEqual(len(combined), 5)
        self.assertEqual(combined[mesh.TARGET], screws.edit_include(mesh.transform(SOURCE, True), True))
        current = snapshot(True, True, True)
        current.mesh.data = current.mesh.data.replace(b'0.1, 0.2', b'0.3, 0.4')
        disabled = engine.prepare_all(current, None, screws_desired=False)
        self.assertIsNone(disabled[screws.PYTHON])
        self.assertIsNone(disabled[screws.CONFIG])
        self.assertEqual(disabled[mesh.TARGET], mesh.transform(SOURCE, True).replace(b'0.1, 0.2', b'0.3, 0.4'))
        self.assertTrue(current.screws.analysis['enabled'])
        self.assertTrue(current.mesh.analysis['known'])

    def test_initial_review_and_full_restore(self):
        before, enabled = snapshot(), snapshot(True, True, True)
        first_diff, _ = engine.initial_difference(before, screws.MOD_ID)
        later_diff, _ = engine.initial_difference(enabled, screws.MOD_ID)
        self.assertEqual(first_diff, later_diff)
        self.assertIn('_ORIGIN_SCREWS_TILT_CALCULATE {rawparams}', first_diff)
        self.assertIn('+++ Enabled ' + screws.PYTHON, first_diff)
        diff = engine.difference_all(enabled, None, screws_desired=False)
        self.assertIn('+++ /dev/null', diff)
        restored = engine.prepare_all(enabled, None, restore=True)
        self.assertEqual(restored[mesh.TARGET], SOURCE)
        self.assertIsNone(restored[screws.PYTHON])
        self.assertIsNone(restored[screws.CONFIG])

    def test_missing_backup_blocks_removal_and_shared_config_restore(self):
        current = snapshot(True, False)
        with self.assertRaisesRegex(ValueError, 'backups'):
            engine.prepare_all(current, None, screws_desired=False)
        current.mesh.original = SOURCE
        self.assertNotIn(mesh.TARGET, engine.restorable(current))

    def test_conflicting_existing_file_and_include_are_blocked(self):
        current = snapshot()
        current.screws.files[screws.PYTHON].data = b'# user helper\n'
        analysis = screws.inspect(current.mesh, current.screws.files, {mesh.TARGET: SOURCE}, {})
        self.assertFalse(analysis['patchable'])
        current.screws.files[screws.PYTHON].data = None
        for config, patterns in ((SOURCE, {'/mnt/UDISK/printer_data/config/*.cfg': [mesh.TARGET]}),
                                  (b'[screws_tilt_adjust]\nscrew1: 1,2\n' + SOURCE, {})):
            analysis = screws.inspect(current.mesh, current.screws.files, {mesh.TARGET: config}, patterns)
            self.assertFalse(analysis['patchable'])

    def test_crlf_roundtrip_and_modified_marker_rejection(self):
        original = SOURCE.replace(b'\n', b'\r\n')
        enabled = screws.edit_include(original, True)
        self.assertEqual(screws.edit_include(enabled, False), original)
        self.assertEqual(mesh.fingerprint(enabled), mesh.fingerprint(original))
        with self.assertRaises(ValueError):
            screws.edit_include(enabled.replace(screws.CONFIG.encode(), b'/edited_screws.cfg'), False)

    def test_apply_submits_one_combined_config_after_new_files(self):
        before, after = snapshot(), snapshot(True, True, True)
        ledger = engine.prepare_all(before, None, mesh_desired=True,
                                    screws_desired=True)[mod_versions.TARGET]
        after.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {'known': True, 'enabled': True, 'patchable': True},
            original_hash=mods.digest(b''), original_absent=True)
        with patch.object(engine, 'scan', side_effect=[before, after]), \
                patch.object(engine.network, 'require_idle'), \
                patch.object(engine.network, 'status', return_value=before.status), \
                patch.object(engine, 'Remote') as remote:
            result = engine.apply(Connection('192.0.2.1'), Path('unused'), before, None,
                                  mesh_desired=True, screws_desired=True)
        script, request = remote.return_value.__enter__.return_value.python.call_args.args
        self.assertEqual(script, APPLY_BATCH)
        self.assertEqual([item['path'] for item in request['files']],
                         [screws.PYTHON, screws.CONFIG, mesh.TARGET, fluidd_screws.TARGET, mod_versions.TARGET])
        self.assertIsNone(request['files'][0]['expected'])
        self.assertEqual(base64.b64decode(request['files'][2]['data']), after.mesh.data)
        self.assertEqual(request['files'][3]['kind'], 'javascript')
        self.assertTrue(request['files'][3]['replacements'])
        self.assertTrue(result['written'])


class HelperBatchTests(unittest.TestCase):
    setUp = mesh_tests.BatchTests.setUp
    tearDown = mesh_tests.BatchTests.tearDown
    run_request = mesh_tests.BatchTests.run_request

    def helper_request(self, enabled, root_data=None):
        helper_py = self.root / 'screws_tilt_adjust.py'
        helper_cfg = self.root / 'screws_tilt_adjust.cfg'
        prepared = {helper_py: screws.payload(screws.PYTHON) if enabled else None,
                    helper_cfg: screws.payload(screws.CONFIG) if enabled else None,
                    self.cfg: root_data if root_data is not None else screws.edit_include(self.cfg.read_bytes(), enabled)}
        if not enabled:
            prepared = {self.cfg: prepared.pop(self.cfg), **prepared}
        items = []
        for path, new in prepared.items():
            kind = 'python' if path.suffix == '.py' else 'config'
            backup = path.with_suffix('.ptn.Original.txt' if kind == 'python' else '.cfn.Original.txt')
            manifest = Path(str(backup) + '.json')
            meta = json.loads(manifest.read_bytes()) if manifest.exists() else {}
            old = path.read_bytes() if path.exists() else None
            items.append(dict(path=str(path), backup=str(backup), expected=mods.digest(old) if old is not None else None,
                              data=base64.b64encode(new).decode() if new is not None else None,
                              new_hash=mods.digest(new) if new is not None else None,
                              original_hash=meta.get('sha256'), original_absent=meta.get('absent', False),
                              may_capture_original=True, reference='fixture', restore=False, kind=kind))
        return {'files': items, 'patterns': {str(helper_cfg): [str(helper_cfg)]} if helper_cfg.exists() else {}}

    def test_create_remove_reenable_keeps_originals_immutable(self):
        self.run_request(self.helper_request(True))
        self.assertTrue((self.root / 'screws_tilt_adjust.py').exists())
        originals = {p: p.read_bytes() for p in self.root.glob('*.Original.txt*')}
        metadata = json.loads((self.root / 'screws_tilt_adjust.ptn.Original.txt.json').read_bytes())
        self.assertTrue(metadata['absent'])
        self.cfg.write_bytes(mesh.transform(self.cfg.read_bytes(), True))
        self.run_request(self.helper_request(False))
        self.assertFalse((self.root / 'screws_tilt_adjust.py').exists())
        self.assertFalse((self.root / 'screws_tilt_adjust.cfg').exists())
        self.assertEqual(self.cfg.read_bytes(), mesh.transform(SOURCE, True))
        self.run_request(self.helper_request(True))
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)

    def test_file_appearing_since_scan_blocks_all_writes(self):
        request = self.helper_request(True)
        path = self.root / 'screws_tilt_adjust.py'
        path.write_bytes(b'# appeared after scan\n')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_request(request)
        self.assertEqual(path.read_bytes(), b'# appeared after scan\n')
        self.assertEqual(self.cfg.read_bytes(), SOURCE)
        self.assertFalse(list(self.root.glob('*.Original.txt')))

    def test_include_failure_rolls_back_new_files(self):
        def fail_replace(source, target):
            raise OSError('simulated include write failure')
        with self.assertRaisesRegex(OSError, 'simulated'):
            self.run_request(self.helper_request(True), fail_replace)
        self.assertFalse((self.root / 'screws_tilt_adjust.py').exists())
        self.assertFalse((self.root / 'screws_tilt_adjust.cfg').exists())
        self.assertEqual(self.cfg.read_bytes(), SOURCE)

    def test_remove_failure_restores_include_and_deleted_module(self):
        self.run_request(self.helper_request(True))
        request = self.helper_request(False)
        saved = {Path(item['path']): Path(item['path']).read_bytes() for item in request['files']}
        native_unlink = os.unlink
        fail_path = self.root / 'screws_tilt_adjust.cfg'
        def unlink(path, *args, **kwargs):
            if Path(path) == fail_path:
                raise OSError('simulated deletion failure')
            return native_unlink(path, *args, **kwargs)
        with patch('os.unlink', unlink), self.assertRaisesRegex(OSError, 'simulated'):
            self.run_request(request)
        for path, data in saved.items():
            self.assertEqual(path.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
