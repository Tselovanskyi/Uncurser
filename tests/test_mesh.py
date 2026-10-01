"""Focused safety checks using synthetic files; no printer connections."""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import engine, mesh, mods, network
from app.remote import APPLY_BATCH, Connection


SOURCE = b'''[printer]
kinematics: corexy
max_velocity: 800
max_z_velocity: 30
[prtouch_v3]
speed: 5
quick_lift_speed: 100
[bed_mesh]
speed: 100
probe_count: 7,7
horizontal_move_z: 5
algorithm: bicubic
[z_align]
distance_ratio: 0.85 # existing comment
quick_speed: 30
slow_speed: 10
retries: 5

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*# [bed_mesh default]
#*# points = 0.1, 0.2
'''


class MeshTests(unittest.TestCase):
    def test_exact_values_roundtrip_and_idempotence(self):
        modified = mesh.transform(SOURCE, True)
        self.assertEqual(mesh.transform(modified, True), modified)
        self.assertEqual(mesh.transform(modified, False, SOURCE), SOURCE)
        self.assertEqual(mesh.Document(modified).raw, mesh.VALUES)
        self.assertIn(b'retries: 5', modified)
        self.assertIn(b'speed: 5\nquick_lift_speed: 100', modified)

    def test_no_number_concatenation_for_different_initial_values(self):
        for speed in (b'50', b'5.5', b'5e1'):
            source = SOURCE.replace(b'speed: 5\n', b'speed: ' + speed + b'\n').replace(b'speed: 100\n', b'speed: 1000\n')
            modified = mesh.transform(source, True)
            self.assertIn(b'lift_speed: 50\n', modified)
            self.assertEqual(mesh.Document(modified).raw[('bed_mesh', 'speed')], '700')
            self.assertIn(b'speed: ' + speed + b'\n', modified)
            self.assertEqual(mesh.transform(modified, False, source), source)

    def test_existing_lift_restores_from_backup_not_a_default(self):
        source = SOURCE.replace(b'[prtouch_v3]\n', b'[prtouch_v3]\nlift_speed: 17.5\n')
        self.assertEqual(mesh.transform(mesh.transform(source, True), False, source), source)

    def test_probe_travel_height_is_never_modified(self):
        for line in (b'horizontal_move_z: 3\n', b'horizontal_move_z: 7.5 # custom\n', b''):
            with self.subTest(line=line):
                source = SOURCE.replace(b'horizontal_move_z: 5\n', line)
                enabled = mesh.transform(source, True, SOURCE)
                # Even a different on-printer original must not restore this setting.
                self.assertEqual(mesh.transform(enabled, False, SOURCE), source)
                self.assertNotIn(('bed_mesh', 'horizontal_move_z'), mesh.Document(enabled).raw)
                self.assertEqual(mesh.fingerprint(enabled), mesh.fingerprint(source))

    def test_duplicate_and_malformed_values_are_blocked(self):
        for source in (SOURCE.replace(b'speed: 100', b'speed: 100\nspeed: 200'),
                       SOURCE.replace(b'speed: 100', b'speed: 100foo'),
                       SOURCE.replace(b'quick_speed: 30', b'quick_speed: 30.5'),
                       SOURCE.replace(b'probe_count: 7,7', b'probe_count: 7,7,7')):
            with self.assertRaises(ValueError):
                mesh.transform(source, True)

    def test_current_generated_data_and_unrelated_edits_survive_disable_and_restore(self):
        modified = mesh.transform(SOURCE, True).replace(b'0.1, 0.2', b'0.3, 0.4')
        modified = modified.replace(b'slow_speed: 10', b'slow_speed: 12')
        restored = mesh.transform(modified, False, SOURCE)
        self.assertIn(b'slow_speed: 12', restored)
        self.assertIn(b'0.3, 0.4', restored)
        full = mesh.restore(modified, SOURCE)
        self.assertIn(b'slow_speed: 10', full)
        self.assertIn(b'0.3, 0.4', full)
        self.assertEqual(mesh.stable_digest(SOURCE), mesh.stable_digest(SOURCE.replace(b'0.1, 0.2', b'9, 9')))

    def test_generated_or_included_override_is_blocked(self):
        with self.assertRaises(ValueError):
            mesh.transform(SOURCE + b'#*# [bed_mesh]\n#*# speed = 900\n', True)
        with self.assertRaises(ValueError):
            mesh.validate_includes({'other.cfg': b'[bed_mesh]\nspeed: 900\n'})

    def test_state_is_detected_from_values_and_crlf_is_preserved(self):
        with patch.object(mesh, 'REFERENCE_HASH', mesh.fingerprint(SOURCE)):
            self.assertFalse(mesh.inspect(SOURCE)['enabled'])
            enabled = mesh.transform(SOURCE, True)
            self.assertTrue(mesh.inspect(enabled)['enabled'])
            partial = enabled.replace(b'quick_speed: 50', b'quick_speed: 30')
            self.assertIsNone(mesh.inspect(partial, SOURCE)['enabled'])
        crlf = SOURCE.replace(b'\n', b'\r\n')
        self.assertEqual(mesh.transform(mesh.transform(crlf, True), False, crlf), crlf)
        with self.assertRaises(ValueError):
            mesh.transform(SOURCE, False)


class BatchTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / 'build' / 'file-checks'
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name)
        self.py = self.root / 'example.py'
        self.cfg = self.root / 'printer.cfg'
        self.py.write_bytes(b'value = 1\n')
        self.cfg.write_bytes(SOURCE)
        self.sync = self.root / 'sync'
        self.sync.write_bytes(b'')

    def tearDown(self):
        for path in self.root.iterdir():
            path.chmod(0o600)
        self.directory.cleanup()

    def request(self):
        items = []
        for path, new, kind, marker in ((self.py, b'value = 2\n', 'python', 'ptn'),
                                        (self.cfg, mesh.transform(SOURCE, True), 'config', 'cfn')):
            old = path.read_bytes()
            item = dict(path=str(path), backup=str(path.with_suffix('.' + marker + '.Original.txt')),
                        expected=mods.digest(old), data=base64.b64encode(new).decode(), new_hash=mods.digest(new),
                        original_hash=None, may_capture_original=True, reference='synthetic', restore=False, kind=kind)
            if kind == 'config':
                item['settings'] = [[section, key, value] for (section, key), value in mesh.Document(new).raw.items()]
            items.append(item)
        return {'files': items}

    def run_request(self, request, replace=None):
        native_open = os.open
        def portable_open(path, flags, *args, **kwargs):
            return native_open(str(self.sync) if Path(path).is_dir() else path, flags, *args, **kwargs)
        with patch('sys.stdin', io.StringIO(json.dumps(request))), contextlib.redirect_stdout(io.StringIO()), \
                patch('os.open', portable_open), patch('os.fsync', lambda fd: None), \
                patch('os.fchown', lambda *args: None, create=True), patch('os.fchmod', lambda *args: None, create=True), \
                patch('os.replace', replace or os.replace):
            exec(compile(APPLY_BATCH, '<local-batch-check>', 'exec'), {})

    def test_both_originals_exist_before_first_replacement(self):
        request = self.request()
        native_replace = os.replace
        def replace(source, target):
            for item in request['files']:
                self.assertTrue(Path(item['backup']).exists())
                self.assertTrue(Path(item['backup'] + '.json').exists())
            native_replace(source, target)
        self.run_request(request, replace)
        self.assertEqual(self.cfg.read_bytes(), mesh.transform(SOURCE, True))
        self.assertEqual(self.py.read_bytes(), b'value = 2\n')
        self.assertEqual(self.cfg.with_suffix('.cfn.Original.txt').read_bytes(), SOURCE)

    def test_failure_on_second_file_rolls_back_first(self):
        native_replace = os.replace
        def replace(source, target):
            if Path(target) == self.cfg:
                raise OSError('simulated replacement failure')
            native_replace(source, target)
        with self.assertRaises(OSError):
            self.run_request(self.request(), replace)
        self.assertEqual(self.py.read_bytes(), b'value = 1\n')
        self.assertEqual(self.cfg.read_bytes(), SOURCE)
        self.assertFalse(list(self.root.glob('*.uncursed-pending-*.txt')))

    def test_stale_config_blocks_all_replacements_and_backups(self):
        request = self.request()
        self.cfg.write_bytes(SOURCE.replace(b'retries: 5', b'retries: 6'))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_request(request)
        self.assertEqual(self.py.read_bytes(), b'value = 1\n')
        self.assertFalse(list(self.root.glob('*.Original.txt')))

    def test_wrong_prepared_value_cannot_be_written(self):
        request = self.request()
        item = request['files'][1]
        data = base64.b64decode(item['data']).replace(b'lift_speed: 50\n', b'lift_speed: 500\n')
        item.update(data=base64.b64encode(data).decode(), new_hash=mods.digest(data))
        with self.assertRaisesRegex(ValueError, 'value mismatch'):
            self.run_request(request)
        self.assertEqual(self.cfg.read_bytes(), SOURCE)

    def test_disable_uses_immutable_on_printer_backup(self):
        self.run_request(self.request())
        original = self.cfg.with_suffix('.cfn.Original.txt')
        saved = original.read_bytes()
        current = self.cfg.read_bytes().replace(b'slow_speed: 10', b'slow_speed: 12').replace(b'0.1, 0.2', b'0.7, 0.8')
        self.cfg.write_bytes(current)
        new = mesh.transform(current, False, saved)
        item = self.request()['files'][1]
        item.update(data=base64.b64encode(new).decode(), new_hash=mods.digest(new),
                    original_hash=mods.digest(saved), may_capture_original=False,
                    settings=[[section, key, value] for (section, key), value in mesh.Document(new).raw.items()])
        self.run_request({'files': [item]})
        self.assertEqual(original.read_bytes(), SOURCE)
        self.assertEqual(self.cfg.read_bytes(), SOURCE.replace(b'slow_speed: 10', b'slow_speed: 12').replace(b'0.1, 0.2', b'0.7, 0.8'))

    def test_backup_matching_include_is_blocked_before_creation(self):
        request = self.request()
        request['patterns'] = {str(self.root / '*.txt'): []}
        with self.assertRaisesRegex(ValueError, 'would match a config include'):
            self.run_request(request)
        self.assertFalse(list(self.root.glob('*.Original.txt')))
        self.assertEqual(self.py.read_bytes(), b'value = 1\n')

    def test_active_print_blocks_apply_before_any_remote_write(self):
        status = {'info': {'state': 'ready'}, 'objects': {'print_stats': {'state': 'printing'}},
                  'native': {'model': 'F008', 'modelVersion': '1.1.6.4'}}
        snapshot = engine.Snapshot(b'', '', {}, None, None, '', status, '', {}, 0)
        with patch.object(engine, 'scan', return_value=snapshot), patch.object(engine, 'Remote') as remote:
            with self.assertRaisesRegex(RuntimeError, 'print'):
                engine.apply(Connection('192.0.2.1'), Path('unused'), snapshot, None, mesh_desired=True)
            remote.assert_not_called()


if __name__ == '__main__':
    unittest.main()
