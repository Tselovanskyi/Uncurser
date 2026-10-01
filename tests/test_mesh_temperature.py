"""Focused checks for the print-only temperature fix; no printer access."""
import base64
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mesh_temperature as mod, mods
from app.remote import Connection
import test_mesh
import test_mods


SOURCE = ("""[gcode_macro G29]
gcode:
  %s
    {%% set bed_temp = params.BED_TEMP %%}
  {%% endif %%}

[gcode_macro BED_MESH_CALIBRATE_START_PRINT]
gcode:
  {%% set bed_temp = printer.custom_macro.default_bed_temp %%}
  %s
    {%% set bed_temp = params.BED_TEMP %%}
  {%% endif %%}
  M140 S{bed_temp}
  M190 S{bed_temp}
  BED_MESH_CALIBRATE

[gcode_macro UNRELATED]
gcode:
  G4 P100
""" % (mod.ORIGINAL_CONDITION, mod.ORIGINAL_CONDITION)).encode()


class TemperatureTests(unittest.TestCase):
    def test_only_print_macro_changes_one_line_and_roundtrips(self):
        enabled = mod.transform(SOURCE, True)
        self.assertEqual(sum(a != b for a, b in zip(SOURCE.splitlines(), enabled.splitlines())), 1)
        self.assertEqual(SOURCE.split(b'[gcode_macro BED_MESH')[0], enabled.split(b'[gcode_macro BED_MESH')[0])
        self.assertIn(b'M140 S{bed_temp}\n  M190 S{bed_temp}', enabled)
        self.assertEqual(mod.transform(enabled, True), enabled)
        self.assertEqual(mod.transform(enabled, False, SOURCE), SOURCE)
        self.assertTrue(mod.inspect(enabled, SOURCE)['enabled'])
        self.assertTrue(mod.inspect(enabled, SOURCE)['known'])

    def test_disable_uses_backup_and_preserves_unrelated_edits(self):
        current = mod.transform(SOURCE, True).replace(b'G4 P100', b'G4 P123')
        self.assertEqual(mod.transform(current, False, SOURCE), SOURCE.replace(b'G4 P100', b'G4 P123'))
        self.assertFalse(mod.inspect(current, SOURCE)['known'])
        with self.assertRaisesRegex(ValueError, 'original'):
            mod.transform(current, False)

    def test_windows_line_endings_do_not_change_recognition(self):
        windows = SOURCE.replace(b'\n', b'\r\n')
        self.assertEqual(mod.fingerprint(windows), mod.fingerprint(SOURCE))
        self.assertEqual(mod.transform(windows, True), mod.transform(SOURCE, True).replace(b'\n', b'\r\n'))

    def test_duplicates_and_changed_condition_body_are_blocked(self):
        for value in (SOURCE + SOURCE, SOURCE.replace(b'{% set bed_temp = params.BED_TEMP %}', b'{% set bed_temp = 90 %}')):
            with self.assertRaises(ValueError):
                mod.transform(value, True)

    def test_no_condition_in_other_macro_is_used_as_fallback(self):
        current = SOURCE.replace(b'[gcode_macro BED_MESH_CALIBRATE_START_PRINT]', b'[gcode_macro SOMETHING_ELSE]')
        self.assertFalse(mod.inspect(current)['patchable'])

    def test_full_restore_preserves_current_generated_data(self):
        generated = b'\n#*# <---------------------- SAVE_CONFIG ---------------------->\n#*# [bed_mesh default]\n#*# points = 0.2, 0.3\n'
        original = SOURCE + generated
        current = mod.transform(original, True).replace(b'0.2, 0.3', b'0.4, 0.5')
        self.assertEqual(mod.restore(current, original), original.replace(b'0.2, 0.3', b'0.4, 0.5'))

    def test_included_override_is_blocked(self):
        with self.assertRaisesRegex(ValueError, 'also defined'):
            mod.validate_includes({'other.cfg': SOURCE})

    def test_batch_contains_both_configs_without_cross_file_guards(self):
        status = {'info': {'state': 'ready'}, 'objects': {'print_stats': {'state': 'standby'},
                  'idle_timeout': {'state': 'Idle'}, 'toolhead': {}},
                  'native': {'deviceState': 0, 'model': 'F008', 'modelVersion': '1.1.6.4'}}
        analysis = {'known': True, 'enabled': False, 'patchable': True, 'label': 'Original', 'reason': ''}
        snapshot = engine.Snapshot(test_mods.SOURCE, mods.digest(test_mods.SOURCE), analysis.copy(),
                                   None, None, '', status, '', {}, 0,
                                   engine.MeshSnapshot(test_mesh.SOURCE, mods.digest(test_mesh.SOURCE), analysis.copy()),
                                   engine.MeshSnapshot(SOURCE, mods.digest(SOURCE), analysis.copy()))
        snapshot.mesh.guards = {mod.TARGET: mods.digest(SOURCE)}
        snapshot.mesh_temperature.guards = {mesh.TARGET: mods.digest(test_mesh.SOURCE)}
        saved = copy.deepcopy(snapshot)
        saved.mesh.data = mesh.transform(test_mesh.SOURCE, True)
        saved.mesh.sha256 = mods.digest(saved.mesh.data)
        saved.mesh_temperature.data = mod.transform(SOURCE, True)
        saved.mesh_temperature.sha256 = mods.digest(saved.mesh_temperature.data)
        with patch.object(engine, 'scan', side_effect=[snapshot, saved]), \
                patch.object(engine.network, 'status', return_value=status), patch.object(engine, 'Remote') as remote:
            engine.apply(Connection('192.0.2.1'), Path('unused'), snapshot, None,
                         mesh_desired=True, temperature_desired=True)
            request = remote.return_value.__enter__.return_value.python.call_args.args[1]
        self.assertEqual({item['path'] for item in request['files']}, {mesh.TARGET, mod.TARGET})
        self.assertEqual(request['guards'], {})
        self.assertTrue(all(item['may_capture_original'] for item in request['files']))

    def test_real_batch_validator_accepts_macro_and_preserves_its_original(self):
        harness = test_mesh.BatchTests()
        harness.setUp()
        try:
            request = harness.request()
            path = harness.root / 'gcode_macro.cfg'
            path.write_bytes(SOURCE)
            new = mod.transform(SOURCE, True)
            item = dict(path=str(path), backup=str(path.with_suffix('.cfn.Original.txt')),
                        expected=mods.digest(SOURCE), data=base64.b64encode(new).decode(), new_hash=mods.digest(new),
                        original_hash=None, may_capture_original=True, reference='synthetic', restore=False,
                        kind='config', settings=mod.settings(new))
            request['files'].append(item)
            harness.run_request(request)
            self.assertEqual(path.read_bytes(), new)
            self.assertEqual(Path(item['backup']).read_bytes(), SOURCE)
            self.assertEqual(harness.cfg.read_bytes(), mesh.transform(test_mesh.SOURCE, True))
        finally:
            harness.tearDown()


if __name__ == '__main__':
    unittest.main()
