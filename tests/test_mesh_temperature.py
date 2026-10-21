"""Focused checks for the print-only temperature fix; no printer access."""
import base64
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import engine, mesh, mesh_temperature as mod, mod_versions, mods
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

    def test_condition_only_legacy_is_recognized_and_upgraded(self):
        marker = b'[gcode_macro BED_MESH'
        prefix, section = SOURCE.split(marker, 1)
        legacy = prefix + marker + section.replace(mod.ORIGINAL_CONDITION.encode(), mod.ENABLED_CONDITION.encode())
        state = mod.inspect(legacy, SOURCE)
        self.assertTrue(state['enabled'])
        self.assertTrue(state['legacy'])
        self.assertTrue(state['known'])
        enabled = mod.transform(legacy, True)
        self.assertFalse(mod.inspect(enabled, SOURCE)['legacy'])
        # The unrelated G29 condition is preserved.
        self.assertEqual(enabled.split(marker)[0], legacy.split(marker)[0])
        restored = mod.transform(enabled, False, SOURCE)
        self.assertEqual(restored, legacy.split(marker)[0] + marker + SOURCE.split(marker, 1)[1])

    def test_mixed_legacy_condition_is_normalized_without_losing_the_fix(self):
        enabled = mod.transform(SOURCE, True)
        lines, _, condition_index = mod.anchors(enabled)
        lines[condition_index] = lines[condition_index].replace(mod.ORIGINAL_CONDITION, mod.ENABLED_CONDITION)
        mixed = ''.join(lines).encode()
        self.assertTrue(mod.inspect(mixed, SOURCE)['legacy'])
        self.assertEqual(mod.transform(mixed, True), enabled)

    def test_missing_duplicate_or_reordered_initializer_is_blocked(self):
        for source in (SOURCE.replace(mod.ORIGINAL_INITIALIZER.encode(), b'{% set bed_temp = 90 %}'),
                       SOURCE.replace(mod.ORIGINAL_INITIALIZER.encode(),
                                      (mod.ORIGINAL_INITIALIZER + '\n  ' + mod.ORIGINAL_INITIALIZER).encode()),
                       SOURCE.replace((mod.ORIGINAL_INITIALIZER + '\n  ').encode(), b'').replace(
                           b'  M140 S{bed_temp}', (f'  {mod.ORIGINAL_INITIALIZER}\n  M140 S{{bed_temp}}').encode())):
            with self.subTest(source=source), self.assertRaises(ValueError):
                mod.transform(source, True)

    @staticmethod
    def selected_temperature(data, target, params):
        """Evaluate only the macro's two selection statements; no Jinja dependency."""
        lines, initializer, condition = mod.anchors(data)
        statement = lines[initializer].strip()
        if statement == mod.ORIGINAL_INITIALIZER:
            value = 50
        else:
            expression = statement.split('params.BED_TEMP|default(', 1)[1].removesuffix(') %}')
            printer = SimpleNamespace(heater_bed=SimpleNamespace(target=target),
                                      custom_macro=SimpleNamespace(default_bed_temp=50))
            fallback = eval(expression, {'__builtins__': {}}, {'printer': printer})
            value = params.get('BED_TEMP', fallback)
        if 'BED_TEMP' in params and (lines[condition].strip() == mod.ENABLED_CONDITION or int(float(params['BED_TEMP'])) >= 50):
            value = params['BED_TEMP']
        return float(value)

    def test_native_callers_and_macro_select_bed_target_on_both_firmwares(self):
        fixtures = Path(__file__).resolve().parents[1] / 'build/firmware-analysis'
        cases = [(42, None, 42), (50, None, 50), (100, None, 100),
                 (100, 42, 42), (100, 0, 0), (0, None, 50)]
        for firmware in ('1.1.6.4', '1.1.7.0'):
            root = fixtures / firmware / 'rootfs/usr/share/klipper'
            if not (root / 'klippy/extras/virtual_sdcard.py').is_file():
                self.skipTest('Local firmware extraction is unavailable.')
            caller = ast.parse((root / 'klippy/extras/virtual_sdcard.py').read_text(encoding='utf-8'))
            gates = [node.test for node in ast.walk(caller) if isinstance(node, ast.If)
                     and ast.unparse(node.test) == 'target_temp > default_bed_temp']
            self.assertEqual(len(gates), 1)
            gate = compile(ast.Expression(gates[0]), '<native mesh temperature gate>', 'eval')
            for variant in ('F008_CR0CN240319C13', 'F008_CR0CN240319C13_1'):
                native = (root / 'config' / variant / 'gcode_macro.cfg').read_bytes()
                enabled = mod.transform(native, True)
                self.assertEqual(sum(a != b for a, b in zip(native.splitlines(), enabled.splitlines())), 1)
                self.assertEqual(mod.fingerprint(enabled), mod.fingerprint(native))
                self.assertEqual(mod.transform(enabled, False, native), native)
                self.assertEqual(self.selected_temperature(native, 42, {}), 50)
                for target, explicit, expected in cases:
                    params = {}
                    if eval(gate, {'__builtins__': {}}, {'target_temp': target, 'default_bed_temp': 50}):
                        params['BED_TEMP'] = str(target)
                    if explicit is not None:
                        params['BED_TEMP'] = str(explicit)
                    with self.subTest(firmware=firmware, variant=variant, target=target, explicit=explicit):
                        self.assertEqual(self.selected_temperature(enabled, target, params), expected)

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
        snapshot = engine.MeshSnapshot(current, None, {}, original=original)
        self.assertEqual(engine.prepare_mesh(snapshot, None, restore=True, module=mod),
                         original.replace(b'0.2, 0.3', b'0.4, 0.5'))

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
        plan = engine.prepare_all(snapshot, None, mesh_desired=True, temperature_desired=True)
        saved.mesh.data = plan[mesh.TARGET]
        saved.mesh.sha256 = mods.digest(saved.mesh.data)
        saved.mesh_temperature.data = plan[mod.TARGET]
        saved.mesh_temperature.sha256 = mods.digest(saved.mesh_temperature.data)
        ledger = plan[mod_versions.TARGET]
        saved.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {'known': True, 'enabled': True, 'patchable': True},
            original_hash=mods.digest(b''), original_absent=True)
        with patch.object(engine, 'scan', side_effect=[snapshot, saved]), \
                patch.object(engine.network, 'status', return_value=status), patch.object(engine, 'Remote') as remote:
            engine.apply(Connection('192.0.2.1'), Path('unused'), snapshot, None,
                         mesh_desired=True, temperature_desired=True)
            request = remote.return_value.__enter__.return_value.python.call_args.args[1]
        self.assertEqual({item['path'] for item in request['files']}, {mesh.TARGET, mod.TARGET, mod_versions.TARGET})
        self.assertEqual(request['files'][-1]['path'], mod_versions.TARGET)
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
