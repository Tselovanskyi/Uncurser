"""Fresh native mesh, plane guidance and explicit saving, without a printer."""
import configparser
import copy
import io
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
NATIVE_SOLVER = Mock(return_value={'x_adjust': .2, 'y_adjust': -.4, 'z_adjust': .2 + 1./9})
with patch.dict('sys.modules', {'mathutil': SimpleNamespace(coordinate_descent=NATIVE_SOLVER)}):
    MODULE = runpy.run_path(str(ROOT / 'References/screws_tilt_adjust/screws_tilt_adjust.py'))
Helper = MODULE['ScrewsTiltAdjust']
MARKER = '#*# <---------------------- SAVE_CONFIG ---------------------->'
WARNING = '#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.'


class CommandError(Exception):
    pass


class Command:
    error = CommandError

    def __init__(self, **values):
        self.values = values

    def get(self, name, default=None):
        return self.values.get(name, default)

    def get_float(self, name, default=None, **bounds):
        return self.get(name, default)

    def get_int(self, name, default=0, **bounds):
        value = self.get(name, default)
        if not isinstance(value, int) or not bounds.get('minval', value) <= value <= bounds.get('maxval', value):
            raise CommandError('invalid integer')
        return value


class CommandRegistrationTests(unittest.TestCase):
    def test_private_commands_preserve_existing_klipper_command(self):
        stock_command = Mock()
        registered = {'SCREWS_TILT_CALCULATE': stock_command}

        def register(name, handler, **kwargs):
            self.assertNotIn(name, registered)
            registered[name] = handler

        gcode = SimpleNamespace(register_command=register, register_output_handler=Mock())
        printer = SimpleNamespace(lookup_object=lambda name: gcode)
        values = {'screw1': (0., 0.), 'screw2': (20., 0.), 'screw3': (20., 20.),
                  'mesh_min': (0., 0.), 'mesh_max': (20., 20.)}
        config = SimpleNamespace(get_printer=lambda: printer,
            get=lambda name, default=None: values.get(name, default),
            getfloatlist=lambda name, **kwargs: values[name],
            getfloat=lambda name, default, **kwargs: default,
            getintlist=lambda name, **kwargs: (3, 3),
            getchoice=lambda name, choices, default: choices[default],
            error=ValueError)
        config.getsection = lambda name: config

        helper = Helper(config)

        self.assertEqual(set(registered), {'SCREWS_TILT_CALCULATE',
            '_UNCURSER_BED_SCREW_HELPER', '_UNCURSER_BED_SCREW_SAVE_MESH'})
        self.assertIs(registered['SCREWS_TILT_CALCULATE'], stock_command)
        self.assertEqual(registered['_UNCURSER_BED_SCREW_HELPER'],
                         helper.cmd__UNCURSER_BED_SCREW_HELPER)
        self.assertEqual(registered['_UNCURSER_BED_SCREW_SAVE_MESH'],
                         helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH)
        config_text = (ROOT / 'References/screws_tilt_adjust/screws_tilt_adjust.cfg').read_text()
        self.assertIn('[gcode_macro BED_SCREW_HELPER]', config_text)
        self.assertIn('_UNCURSER_BED_SCREW_HELPER {rawparams}', config_text)
        self.assertNotIn('SCREWS_TILT_CALCULATE', config_text)
        self.assertNotIn('rename_existing:', config_text)


class Mesh:
    def __init__(self, flat=False):
        self.params = dict(min_x=0., min_y=0., max_x=20., max_y=20., x_count=3, y_count=3)
        self.matrix = [[0. if flat else .3 + .01*x - .02*y + (1. if x == y == 10 else 0.)
                        for x in (0, 10, 20)] for y in (0, 10, 20)]

    def get_mesh_params(self):
        return self.params

    def get_probed_matrix(self):
        return self.matrix


class Fixture:
    def __init__(self, folder):
        self.path = Path(folder) / 'printer.cfg'
        self.configfile = SimpleNamespace(autosave=SimpleNamespace(fileconfig=configparser.RawConfigParser()),
            status_save_pending={}, save_config_pending=False, _find_autosave_data=self.extract)
        self.native = SimpleNamespace(speed=600.)
        self.prtouch = SimpleNamespace(def_hmvz=5.)
        self.homed_axes = 'xyz'
        self.now = 1.
        self.limits = dict(max_accel=2000., max_accel_to_decel=5000., max_velocity=400., square_corner_velocity=7.)
        self.z_limits = dict(max_z_velocity=30., max_z_accel=1000.)
        self.kinematics = SimpleNamespace(get_max_z_velocity=lambda: self.z_limits['max_z_velocity'],
            get_max_z_accel=lambda: self.z_limits['max_z_accel'],
            set_max_z_velocity=lambda value: self.z_limits.update(max_z_velocity=value),
            set_max_z_accel=lambda value: self.z_limits.update(max_z_accel=value))
        self.toolhead = SimpleNamespace(get_status=lambda now: dict(self.limits, homed_axes=self.homed_axes), wait_moves=lambda: None,
                                       get_kinematics=lambda: self.kinematics)
        self.profiles = SimpleNamespace(profiles={}, current_profile='default')
        self.profiles.get_profiles = lambda: self.profiles.profiles
        self.mesh = Mesh(flat=True)
        self.next_mesh = Mesh()
        self.bed_mesh = SimpleNamespace(bmc=SimpleNamespace(probe_helper=self.native), pmgr=self.profiles,
            get_mesh=lambda: self.mesh, set_mesh=self.set_mesh, update_status=lambda: None,
            save_profile=self.save_profile)
        self.temperature = 70.4
        self.state, self.active_sd = 'standby', False
        self.commands, self.messages, self.travel = [], [], []
        self.output_handlers, self.progress, self.probe_reports = [], [], []
        self.probe_error = self.save_error = self.skip_write = self.corrupt_write = False
        self.gcode = SimpleNamespace(error=CommandError, respond_info=self.messages.append,
                                    run_script_from_command=self.run,
                                    register_output_handler=self.output_handlers.append)
        self.objects = dict(bed_mesh=self.bed_mesh, prtouch_v3=self.prtouch, toolhead=self.toolhead,
            configfile=self.configfile, gcode=self.gcode,
            print_stats=SimpleNamespace(get_status=lambda now: {'state': self.state}),
            virtual_sdcard=SimpleNamespace(is_active=lambda: self.active_sd),
            heaters=SimpleNamespace(lookup_heater=lambda name:
                SimpleNamespace(get_status=lambda now: {'temperature': self.temperature, 'target': 100.})))
        self.printer = SimpleNamespace(lookup_object=lambda name, default=None: self.objects.get(name, default),
            get_reactor=lambda: SimpleNamespace(monotonic=lambda: self.now),
            get_start_args=lambda: {'config_file': str(self.path)})
        self.save_profile('default')
        self.configfile.status_save_pending.clear()
        self.configfile.save_config_pending = False
        self.write()
        self.helper = Helper.__new__(Helper)
        self.helper.printer, self.helper.gcode = self.printer, self.gcode
        self.helper.screws = [((x, y), name) for x, y, name in
            ((0, 0, 'front left'), (20, 0, 'front right'), (20, 20, 'rear right'), (0, 20, 'rear left'))]
        self.helper.results = {}
        self.helper.max_diff = self.helper.direction = self.helper.probed_temperature = None
        self.helper.max_diff_error = self.helper._busy = False
        self.helper._mesh = self.helper._signature = None
        self.helper.thread = 2
        self.helper.mesh_min, self.helper.mesh_max, self.helper.probe_count = (0., 0.), (20., 20.), (3, 3)
        self.helper.horizontal_move_z = 10.
        self.helper._reset_progress()
        self.gcode.register_output_handler(self.helper._handle_output)

    def set_mesh(self, value):
        self.mesh = value

    def save_profile(self, name):
        self.profiles.profiles = dict(self.profiles.profiles)
        self.profiles.profiles[name] = dict(points=copy.deepcopy(self.mesh.matrix), mesh_params=copy.deepcopy(self.mesh.params))
        self.profiles.current_profile = name
        section = 'bed_mesh ' + name
        parser = self.configfile.autosave.fileconfig
        if not parser.has_section(section):
            parser.add_section(section)
        for key, value in self.mesh.params.items():
            parser.set(section, key, str(value))
        parser.set(section, 'version', '1')
        parser.set(section, 'points', '\n' + '\n'.join(', '.join('%.6f' % value for value in row) for row in self.mesh.matrix))
        self.configfile.status_save_pending[section] = dict(parser.items(section))
        self.configfile.save_config_pending = True

    def write(self):
        stream = io.StringIO()
        self.configfile.autosave.fileconfig.write(stream)
        text = '[printer]\nkinematics: corexy\n\n' + MARKER + '\n' + WARNING + '\n'
        text += '\n'.join('#*# ' + line for line in stream.getvalue().splitlines()) + '\n'
        self.path.write_text(text, encoding='utf-8')

    @staticmethod
    def extract(text):
        if MARKER not in text:
            return text, ''
        regular, generated = text.split(MARKER, 1)
        return regular, '\n'.join(line[4:] for line in generated.splitlines()
                                   if line.startswith('#*# ') and line != WARNING)

    def run(self, command):
        self.commands.append(command)
        if command == 'G28':
            self.progress.append(self.helper.get_status(self.now))
            self.now += 2.
            self.homed_axes = 'xyz'
        elif command == 'BED_MESH_CALIBRATE':
            self.travel.append((self.native.speed, self.prtouch.def_hmvz))
            self.progress.append(self.helper.get_status(self.now))
            for report in self.probe_reports:
                self.now += 1.
                for handler in self.output_handlers:
                    handler(report)
                self.progress.append(self.helper.get_status(self.now))
            self.mesh = self.next_mesh
            self.bed_mesh.save_profile('default')
            self.bed_mesh.save_profile('default')
            self.limits.update(max_accel=30000., max_accel_to_decel=100., max_velocity=800., square_corner_velocity=10.)
            self.z_limits.update(max_z_velocity=50., max_z_accel=1500.)
            self.temperature = 65.
            if self.probe_error:
                raise CommandError('native probe failed')
        elif command.startswith('SET_VELOCITY_LIMIT '):
            values = dict(item.split('=', 1) for item in command.split()[1:])
            self.limits.update(max_accel=float(values['ACCEL']), max_accel_to_decel=float(values['ACCEL_TO_DECEL']),
                               max_velocity=float(values['VELOCITY']), square_corner_velocity=float(values['SQUARE_CORNER_VELOCITY']))
        elif command == 'CXSAVE_CONFIG':
            if self.save_error:
                raise CommandError('disk write failed')
            if not self.skip_write:
                self.write()
            if self.corrupt_write:
                self.path.write_text('[printer]\nkinematics: corexy\n', encoding='utf-8')
        else:
            raise AssertionError(command)

    def calculate(self):
        self.helper.cmd__UNCURSER_BED_SCREW_HELPER(Command())


class MeshScrewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='screws-mesh-test-', dir=ROOT / 'build')
        self.fx = Fixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_completed_results_notify_subscribers_after_probing(self):
        observed = []
        native_run = self.fx.run

        def subscribed_run(command):
            if command == 'BED_MESH_CALIBRATE':
                observed.append(self.fx.helper.get_status(1.))
            native_run(command)

        self.fx.gcode.run_script_from_command = subscribed_run
        self.fx.calculate()
        completed = self.fx.helper.get_status(2.)
        self.assertEqual(observed[0]['results'], {})
        self.assertEqual(len(completed['results']), 4)
        changes = {key: value for key, value in completed.items()
                   if value != observed[0].get(key)}
        self.assertIn('results', changes)

    def test_fresh_raw_grid_plane_temperature_and_default_protection(self):
        NATIVE_SOLVER.reset_mock()
        original = self.fx.path.read_bytes()
        profile = copy.deepcopy(self.fx.profiles.profiles['default'])
        self.fx.calculate()
        status = self.fx.helper.get_status(1.)
        self.assertEqual(self.fx.travel, [(600., 10.)])
        self.assertEqual((self.fx.native.speed, self.fx.prtouch.def_hmvz), (600., 5.))
        self.assertEqual(self.fx.limits, dict(max_accel=2000., max_accel_to_decel=5000., max_velocity=400., square_corner_velocity=7.))
        self.assertEqual(self.fx.z_limits, dict(max_z_velocity=30., max_z_accel=1000.))
        self.assertEqual(self.fx.path.read_bytes(), original)
        self.assertEqual(self.fx.profiles.profiles['default'], profile)
        self.assertFalse(self.fx.configfile.status_save_pending)
        self.assertEqual(self.fx.profiles.current_profile, '')
        self.assertEqual(status['probed_temperature'], 70.4)
        self.assertTrue(status['can_save_mesh'])
        self.assertAlmostEqual(status['results']['screw2']['z'] - status['results']['screw1']['z'], .2)
        self.assertEqual(status['results']['screw2']['adjust'], '00:17')
        self.assertEqual(status['results']['screw2']['sign'], 'CCW')
        NATIVE_SOLVER.assert_called_once()
        keys, initial, objective = NATIVE_SOLVER.call_args.args
        self.assertEqual(set(keys), {'x_adjust', 'y_adjust', 'z_adjust'})
        self.assertAlmostEqual(initial['z_adjust'], .2 + 1./9)
        self.assertAlmostEqual(objective(NATIVE_SOLVER.return_value), 8./9)
        self.assertGreater(objective({'x_adjust': 0., 'y_adjust': 0., 'z_adjust': 0.}), 8./9)
        self.assertEqual(self.fx.bed_mesh.save_profile, self.fx.save_profile)
        self.assertNotIn('Z_TILT_ADJUST', (ROOT / 'References/screws_tilt_adjust/screws_tilt_adjust.cfg').read_text())

    def test_failed_or_invalid_native_mesh_restores_scoped_state(self):
        for failure in ('native', 'stale', 'ragged', 'nan', 'bounds', 'count', 'temperature'):
            with self.subTest(failure=failure):
                self.fx = Fixture(self.temp.name)
                old_mesh = self.fx.mesh
                if failure == 'native': self.fx.probe_error = True
                if failure == 'stale': self.fx.next_mesh = old_mesh
                if failure == 'ragged': self.fx.next_mesh.matrix[0].pop()
                if failure == 'nan': self.fx.next_mesh.matrix[0][0] = float('nan')
                if failure == 'bounds': self.fx.next_mesh.params['max_x'] = 10.
                if failure == 'count': self.fx.next_mesh.params['x_count'] = 5
                if failure == 'temperature': self.fx.temperature = -1.
                with self.assertRaises(CommandError): self.fx.calculate()
                self.assertIs(self.fx.mesh, old_mesh)
                self.assertEqual(self.fx.profiles.current_profile, 'default')
                self.assertEqual(self.fx.limits, dict(max_accel=2000., max_accel_to_decel=5000., max_velocity=400., square_corner_velocity=7.))
                self.assertEqual(self.fx.z_limits, dict(max_z_velocity=30., max_z_accel=1000.))
                self.assertEqual(self.fx.bed_mesh.save_profile, self.fx.save_profile)
                self.assertEqual((self.fx.native.speed, self.fx.prtouch.def_hmvz), (600., 5.))
                self.assertFalse(self.fx.helper.get_status(1.)['can_save_mesh'])
                self.assertEqual(self.fx.helper.phase, 'error')
                self.assertFalse(self.fx.helper.get_status(self.fx.now)['running'])
                self.assertIsNone(self.fx.helper.probed_temperature)
        self.fx.state = 'printing'
        self.fx.helper.results = {'preserved': True}
        self.fx.commands.clear()
        with self.assertRaises(CommandError): self.fx.calculate()
        self.assertEqual(self.fx.helper.results, {'preserved': True})
        self.assertFalse(self.fx.commands)

    def test_mesh_cleanup_failure_still_restores_limits_and_clears_busy(self):
        for failure in ('set_mesh', 'update_status'):
            with self.subTest(failure=failure):
                self.fx = Fixture(self.temp.name)
                self.fx.probe_error = True
                setattr(self.fx.bed_mesh, failure, Mock(side_effect=CommandError('mesh cleanup failed')))
                with self.assertRaisesRegex(CommandError, 'mesh cleanup failed'):
                    self.fx.calculate()
                self.assertFalse(self.fx.helper._busy)
                self.assertIsNone(self.fx.helper._mesh)
                self.assertIsNone(self.fx.helper.probed_temperature)
                self.assertEqual(self.fx.profiles.current_profile, 'default')
                self.assertEqual(self.fx.bed_mesh.save_profile, self.fx.save_profile)
                self.assertEqual((self.fx.native.speed, self.fx.prtouch.def_hmvz), (600., 5.))
                self.assertEqual(self.fx.limits, dict(max_accel=2000., max_accel_to_decel=5000., max_velocity=400., square_corner_velocity=7.))
                self.assertEqual(self.fx.z_limits, dict(max_z_velocity=30., max_z_accel=1000.))
                self.assertTrue(self.fx.commands[-1].startswith('SET_VELOCITY_LIMIT '))
                self.assertFalse(self.fx.helper.get_status(1.)['can_save_mesh'])
                self.assertEqual(self.fx.helper.phase, 'error')
                self.assertIn('mesh cleanup failed', self.fx.helper.error_message)

    def test_progress_tracks_native_grid_reports_without_counting_pairs_or_retries(self):
        self.fx.homed_axes = ''
        self.fx.native.speed = 725.
        points = ((10, 10), (10, 20), (20, 20), (20, 10), (20, 0), (10, 0), (0, 0), (0, 10), (0, 20))
        self.fx.probe_reports = []
        for index, (x, y) in enumerate(points):
            self.fx.probe_reports.extend((
                'probe at %.2f,%.2f is z=-0.2 z_compensation=0.001' % (x + .25, y - .25),
                'probe at %.2f,%.2f is z=-0.2' % (x + .25, y - .25)))
            if index == 1:
                self.fx.probe_reports.extend((
                    'probe at 10.25,19.75 is z=-0.21',
                    'probe at 10.25,9.75 is z=-0.2',
                    'probe at 20,20 is z=',
                    'probe at 20,20 is z=1e999',
                    'probe at ..,20 is z=0.2',
                    'probe at 15,15 is z=0.2',
                    'probe at 99,99 is z=0.2',
                    {'unrelated': True}))
        calculating = []
        def fit(*args):
            calculating.append(self.fx.helper.get_status(self.fx.now))
            return NATIVE_SOLVER.return_value
        with patch.object(NATIVE_SOLVER, 'side_effect', fit):
            self.fx.calculate()
        self.assertEqual(self.fx.travel, [(725., 10.)])
        self.assertEqual(self.fx.native.speed, 725.)
        self.assertEqual(self.fx.progress[0]['phase'], 'homing')
        self.assertTrue(self.fx.progress[0]['running'])
        probing = [status for status in self.fx.progress if status['phase'] == 'probing']
        self.assertTrue(all(status['running'] and status['total_points'] == 9 for status in probing))
        counts = [status['completed_points'] for status in probing]
        self.assertEqual(counts[:5], [0, 0, 0, 0, 1])
        self.assertEqual(counts[5:13], [1] * 8)
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], 8)
        self.assertIsNone(probing[4]['eta'])
        self.assertTrue(any(status['eta'] is not None and status['eta'] > 0 for status in probing))
        self.assertEqual(calculating[0]['phase'], 'calculating')
        self.assertTrue(calculating[0]['running'])
        self.assertEqual(calculating[0]['completed_points'], 8)
        status = self.fx.helper.get_status(self.fx.now)
        self.assertEqual((status['phase'], status['running'], status['completed_points']), ('complete', False, 9))
        self.assertEqual(status['eta'], 0.)
        self.assertEqual(status['elapsed'], self.fx.now - 1.)
        self.assertEqual(self.fx.helper.get_status(self.fx.now + 60)['elapsed'], status['elapsed'])
        status['results'].clear()
        self.assertTrue(self.fx.helper.results)
        previous = self.fx.helper._current_point
        self.fx.helper._handle_output('probe at 10,10 is z=0.2')
        self.assertEqual(self.fx.helper._current_point, previous)
        config = (ROOT / 'References/screws_tilt_adjust/screws_tilt_adjust.cfg').read_text()
        self.assertNotIn('speed:', config)

    def test_progress_errors_stop_and_preflight_failure_invalidates_old_results(self):
        self.fx.probe_reports = ['probe at %d,%d is z=0.2' % (x, y)
                                 for y in (0, 10, 20) for x in (0, 10, 20)]
        self.fx.probe_error = True
        with self.assertRaisesRegex(CommandError, 'native probe failed'):
            self.fx.calculate()
        status = self.fx.helper.get_status(self.fx.now)
        self.assertEqual((status['phase'], status['running'], status['completed_points']), ('error', False, 8))
        self.assertIn('native probe failed', status['error_message'])
        self.assertIsNone(status['eta'])
        self.assertFalse(status['can_save_mesh'])
        self.assertEqual(self.fx.helper.get_status(self.fx.now + 60)['elapsed'], status['elapsed'])
        self.fx = Fixture(self.temp.name)
        self.fx.calculate()
        with self.assertRaisesRegex(CommandError, 'MAX_DEVIATION'):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_HELPER(Command(MAX_DEVIATION=float('nan')))
        status = self.fx.helper.get_status(self.fx.now)
        self.assertEqual((status['phase'], status['running'], status['completed_points']), ('error', False, 0))
        self.assertEqual(status['elapsed'], 0.)
        self.assertIsNone(self.fx.helper._mesh)
        self.assertFalse(status['can_save_mesh'])
        self.fx = Fixture(self.temp.name)
        self.fx.calculate()
        previous = self.fx.helper.get_status(self.fx.now)
        mesh = self.fx.helper._mesh
        self.fx.state = 'paused'
        with self.assertRaises(CommandError): self.fx.calculate()
        self.assertEqual(self.fx.helper.phase, previous['phase'])
        self.assertEqual(self.fx.helper.get_status(self.fx.now)['completed_points'], previous['completed_points'])
        self.assertIs(self.fx.helper._mesh, mesh)

    def test_eta_counts_down_without_reestimating_on_polls_or_retries(self):
        helper = self.fx.helper
        helper.phase, helper._busy, helper._started_at = 'probing', True, 1.
        self.fx.now = 10.
        self.assertIsNone(helper.get_status(self.fx.now)['eta'])
        def report(now, x, y):
            self.fx.now = now
            helper._handle_output('probe at %.2f,%.2f is z=-0.2' % (x, y))
        report(10., 10.25, 9.75)
        self.assertEqual(helper._probe_started_at, 10.)
        report(14., 10.25, 19.75)
        self.assertIsNone(helper.get_status(14.)['eta'])
        report(18., 20.25, 19.75)
        self.assertEqual(helper.get_status(18.)['completed_points'], 2)
        self.assertEqual(helper.get_status(18.)['eta'], 28.)
        self.assertEqual(helper.get_status(20.)['eta'], 26.)
        self.assertEqual(helper.get_status(20.)['elapsed'], 19.)
        deadline = helper._eta_deadline
        report(22., 20., 20.)
        report(24., 10., 10.)
        self.assertEqual(helper._eta_deadline, deadline)
        self.assertEqual(helper.get_status(24.)['completed_points'], 2)
        self.assertEqual(helper.get_status(24.)['eta'], 22.)
        report(47., 20.25, 19.75)
        self.assertEqual(helper._eta_deadline, deadline)
        self.assertTrue(helper.get_status(47.)['running'])
        self.assertIsNone(helper.get_status(47.)['eta'])
        report(54., 20.25, 9.75)
        self.assertEqual(helper.get_status(54.)['completed_points'], 3)
        self.assertAlmostEqual(helper.get_status(54.)['eta'], 88.)
        self.assertGreater(helper._eta_deadline, deadline)
        self.assertAlmostEqual(helper.get_status(55.)['eta'], 87.)
        helper._reset_progress()
        self.assertIsNone(helper._eta_deadline)
        self.assertIsNone(helper.get_status(55.)['eta'])

    def test_homing_follows_idle_guard_and_preserves_post_home_limits(self):
        for state, active_sd in (('printing', False), ('paused', False), ('standby', True)):
            with self.subTest(state=state, active_sd=active_sd):
                self.fx = Fixture(self.temp.name)
                self.fx.homed_axes = ''
                self.fx.state, self.fx.active_sd = state, active_sd
                with self.assertRaises(CommandError): self.fx.calculate()
                self.assertFalse(self.fx.commands)
        for axes in ('xyz', 'xy', ''):
            with self.subTest(homed_axes=axes):
                self.fx = Fixture(self.temp.name)
                self.fx.homed_axes = axes
                original_run = self.fx.run
                def run(command):
                    original_run(command)
                    if command == 'G28':
                        self.fx.limits.update(max_accel=2500., max_accel_to_decel=3000., max_velocity=450., square_corner_velocity=4.)
                        self.fx.z_limits.update(max_z_velocity=35., max_z_accel=800.)
                self.fx.gcode.run_script_from_command = run
                self.fx.calculate()
                self.assertEqual(self.fx.commands.count('G28'), 0 if axes == 'xyz' else 1)
                if axes != 'xyz':
                    self.assertEqual(self.fx.commands[:2], ['G28', 'BED_MESH_CALIBRATE'])
                    self.assertEqual(self.fx.limits, dict(max_accel=2500., max_accel_to_decel=3000., max_velocity=450., square_corner_velocity=4.))
                    self.assertEqual(self.fx.z_limits, dict(max_z_velocity=35., max_z_accel=800.))
        self.fx = Fixture(self.temp.name)
        self.fx.homed_axes = ''
        original_run = self.fx.run
        def start_print_after_home(command):
            original_run(command)
            if command == 'G28': self.fx.state = 'printing'
        self.fx.gcode.run_script_from_command = start_print_after_home
        with self.assertRaises(CommandError): self.fx.calculate()
        self.assertEqual(self.fx.commands, ['G28'])
        self.fx = Fixture(self.temp.name)
        self.fx.calculate()
        self.fx.homed_axes = ''
        self.fx.gcode.run_script_from_command = Mock(side_effect=CommandError('homing failed'))
        with self.assertRaisesRegex(CommandError, 'homing failed'):
            self.fx.calculate()
        self.assertIsNone(self.fx.helper._mesh)
        self.assertIsNone(self.fx.helper.probed_temperature)
        self.assertFalse(self.fx.helper.get_status(1.)['can_save_mesh'])
        with self.assertRaisesRegex(CommandError, 'no longer current'):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.fx.gcode.run_script_from_command.assert_called_once_with('G28')
        config = (ROOT / 'References/screws_tilt_adjust/screws_tilt_adjust.cfg').read_text()
        self.assertNotIn('G28', config)
        self.assertIn('[gcode_macro BED_SCREW_HELPER]', config)
        self.assertIn('_UNCURSER_BED_SCREW_HELPER {rawparams}', config)
        self.assertNotIn('rename_existing:', config)

    def test_named_and_explicit_default_save_are_persisted_without_restart(self):
        old_default = copy.deepcopy(self.fx.profiles.profiles['default'])
        self.fx.calculate()
        self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.assertEqual(set(self.fx.profiles.profiles), {'default', 'screws_70C'})
        self.assertEqual(self.fx.profiles.profiles['default'], old_default)
        self.assertIn('[bed_mesh screws_70C]', self.fx.path.read_text())
        self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command(DEFAULT=1))
        self.assertEqual(self.fx.profiles.profiles['default']['points'], self.fx.next_mesh.matrix)
        self.assertEqual(self.fx.commands.count('CXSAVE_CONFIG'), 3)
        self.assertNotIn('SAVE_CONFIG', self.fx.commands)

    def test_stale_mesh_and_unrelated_pending_changes_block_before_staging(self):
        self.fx.calculate()
        previous = self.fx.mesh.matrix[0][0]
        self.fx.mesh.matrix[0][0] += .1
        with self.assertRaisesRegex(CommandError, 'no longer current'):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.fx.mesh.matrix[0][0] = previous
        self.fx.configfile.autosave.fileconfig.add_section('prtouch_v3')
        self.fx.configfile.autosave.fileconfig.set('prtouch_v3', 'z_offset', '0.1')
        self.fx.configfile.status_save_pending['prtouch_v3'] = {'z_offset': '0.1'}
        with self.assertRaisesRegex(CommandError, 'Other configuration'):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.fx.configfile.status_save_pending.clear()
        self.fx.configfile.autosave.fileconfig.remove_section('prtouch_v3')
        self.fx.configfile.autosave.fileconfig.set('bed_mesh default', 'version', '2')
        self.fx.configfile.status_save_pending['bed_mesh default'] = {'version': '2'}
        with self.assertRaisesRegex(CommandError, 'Another mesh profile'):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.assertNotIn('screws_70C', self.fx.profiles.profiles)
        self.assertNotIn('CXSAVE_CONFIG', self.fx.commands)
        self.fx.configfile.autosave.fileconfig.set('bed_mesh default', 'version', '1')
        self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
        self.assertIn('screws_70C', self.fx.profiles.profiles)

    def test_saved_unrelated_pending_flags_do_not_block_mesh_saves(self):
        parser = self.fx.configfile.autosave.fileconfig
        parser.add_section('input_shaper')
        parser.set('input_shaper', 'shaper_freq_x', '50.0')
        self.fx.configfile.status_save_pending['input_shaper'] = {'shaper_freq_x': '50.0'}
        self.fx.write()
        self.fx.calculate()
        self.assertTrue(self.fx.helper.get_status(1.)['can_save_mesh'])
        for command in (Command(), Command(DEFAULT=1)):
            self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(command)
        written = self.fx.helper._saved_config(self.fx.configfile)
        self.assertEqual(dict(written.items('input_shaper')), {'shaper_freq_x': '50.0'})
        self.assertEqual(self.fx.configfile.status_save_pending['input_shaper'], {'shaper_freq_x': '50.0'})
        self.assertEqual(self.fx.commands.count('CXSAVE_CONFIG'), 2)

    def test_failed_write_or_disk_verification_restores_selected_pending_state(self):
        for failure in ('save_error', 'skip_write', 'corrupt_write'):
            with self.subTest(failure=failure):
                self.fx = Fixture(self.temp.name)
                self.fx.save_profile('screws_70C')
                self.fx.write()
                self.fx.calculate()
                old_profile = copy.deepcopy(self.fx.profiles.profiles['screws_70C'])
                old_section = dict(self.fx.configfile.autosave.fileconfig.items('bed_mesh screws_70C'))
                old_pending = copy.deepcopy(self.fx.configfile.status_save_pending)
                setattr(self.fx, failure, True)
                with self.assertRaises(CommandError):
                    self.fx.helper.cmd__UNCURSER_BED_SCREW_SAVE_MESH(Command())
                self.assertEqual(self.fx.profiles.profiles['screws_70C'], old_profile)
                self.assertEqual(dict(self.fx.configfile.autosave.fileconfig.items('bed_mesh screws_70C')), old_section)
                self.assertEqual(self.fx.configfile.status_save_pending, old_pending)
                self.assertEqual(self.fx.profiles.current_profile, '')
                self.assertFalse(any(message.startswith('Saved mesh') for message in self.fx.messages))


if __name__ == '__main__':
    unittest.main()
