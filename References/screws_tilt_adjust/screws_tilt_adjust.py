# Helper script to adjust bed screws tilt using Z probe
#
# Copyright (C) 2019  Rui Caridade <rui.mcbc@gmail.com>
# Copyright (C) 2021  Matthew Lloyd <github@matthewlloyd.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import configparser
import copy
import math
import mathutil
import re


PROBE_REPORT = re.compile(r'\bprobe at\s+([-+\d.eE]+)\s*,\s*([-+\d.eE]+)\s+is z=([-+\d.eE]+)\s*$', re.MULTILINE)


def fit_plane(matrix, mesh_min, mesh_max):
    rows, columns = len(matrix), len(matrix[0])
    # Center and scale XY so the native solver's tolerance is in mesh-height mm.
    positions = [(i / (columns - 1) - .5, j / (rows - 1) - .5, z)
                 for j, row in enumerate(matrix) for i, z in enumerate(row)]
    params = {'x_adjust': 0., 'y_adjust': 0.,
              'z_adjust': sum(sum(row) for row in matrix) / (rows * columns)}
    def errorfunc(params):
        return sum((z - x*params['x_adjust'] - y*params['y_adjust']
                    - params['z_adjust'])**2 for x, y, z in positions)
    fitted = mathutil.coordinate_descent(params.keys(), params, errorfunc)
    a = fitted['x_adjust'] / (mesh_max[0] - mesh_min[0])
    b = fitted['y_adjust'] / (mesh_max[1] - mesh_min[1])
    return a, b, fitted['z_adjust'] - a*(mesh_min[0]+mesh_max[0])/2 - b*(mesh_min[1]+mesh_max[1])/2

class ScrewsTiltAdjust:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.screws = []
        self.results = {}
        self.max_diff = None
        self.max_diff_error = False
        self.direction = None
        self.probed_temperature = None
        self._mesh = self._signature = None
        self._busy = False
        self._reset_progress()
        # Read config
        for i in range(99):
            prefix = "screw%d" % (i + 1,)
            if config.get(prefix, None) is None:
                break
            screw_coord = config.getfloatlist(prefix, count=2)
            screw_name = "screw at %.3f,%.3f" % screw_coord
            screw_name = config.get(prefix + "_name", screw_name)
            self.screws.append((screw_coord, screw_name))
        if len(self.screws) < 3:
            raise config.error("screws_tilt_adjust: Must have "
                               "at least three screws")
        threads = {'CW-M3': 0, 'CCW-M3': 1, 'CW-M4': 2, 'CCW-M4': 3,
                   'CW-M5': 4, 'CCW-M5': 5, 'CW-M6': 6, 'CCW-M6': 7}
        self.thread = config.getchoice('screw_thread', threads,
                                       default='CW-M3')
        self.horizontal_move_z = config.getfloat('horizontal_move_z', 5., minval=0.)
        mesh_config = config.getsection('bed_mesh')
        self.mesh_min = tuple(mesh_config.getfloatlist('mesh_min', count=2))
        self.mesh_max = tuple(mesh_config.getfloatlist('mesh_max', count=2))
        self.probe_count = tuple(mesh_config.getintlist('probe_count', count=2))
        if (not all(math.isfinite(value) for value in self.mesh_min + self.mesh_max)
                or any(high <= low for low, high in zip(self.mesh_min, self.mesh_max))
                or min(self.probe_count) < 3
                or not math.isfinite(self.horizontal_move_z)
                or any(not all(math.isfinite(value) for value in coord)
                       or any(not low <= value <= high for value, low, high in zip(coord, self.mesh_min, self.mesh_max))
                       for coord, _ in self.screws)):
            raise config.error("Invalid full-mesh bounds, count, height or screw coordinates")
        # Register command
        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_output_handler(self._handle_output)
        self.gcode.register_command("SCREWS_TILT_CALCULATE",
                                    self.cmd_SCREWS_TILT_CALCULATE,
                                    desc=self.cmd_SCREWS_TILT_CALCULATE_help)
        self.gcode.register_command("SCREWS_TILT_SAVE_MESH", self.cmd_SCREWS_TILT_SAVE_MESH,
                                    desc="Save the latest successful bed screw helper mesh")
    cmd_SCREWS_TILT_CALCULATE_help = "Tool to help adjust bed leveling " \
                                     "screws by calculating the number " \
                                     "of turns to level it."

    def cmd_SCREWS_TILT_CALCULATE(self, gcmd):
        self._require_available(gcmd)
        self._reset_progress()
        try:
            self._run_calculation(gcmd)
        except Exception as error:
            self.phase, self.error_message = 'error', str(error)
            self._mesh = self._signature = None
            self.probed_temperature = None
            raise
        else:
            self.phase = 'complete'
        finally:
            self._busy = False
            if self._started_at is not None:
                self._stopped_at = self.printer.get_reactor().monotonic()

    def _run_calculation(self, gcmd):
        max_diff = gcmd.get_float("MAX_DEVIATION", None, minval=0.)
        if max_diff is not None and not math.isfinite(max_diff):
            raise gcmd.error("MAX_DEVIATION must be a finite, non-negative number")
        # Option to force all turns to be in the given direction (CW or CCW)
        direction = gcmd.get("DIRECTION", default=None)
        if direction is not None:
            direction = direction.upper()
            if direction not in ('CW', 'CCW'):
                raise gcmd.error(
                    "Error on '%s': DIRECTION must be either CW or CCW" % (
                        gcmd.get_commandline(),))
        self._require_available(gcmd)
        self._busy, self.phase = True, 'homing'
        self._started_at = self.printer.get_reactor().monotonic()
        self.max_diff = max_diff
        self.direction = direction
        self.results = {}
        self.max_diff_error = False
        self.probed_temperature = None
        self._mesh = self._signature = None
        bed_mesh = self.printer.lookup_object('bed_mesh')
        prtouch = self.printer.lookup_object('prtouch_v3')
        try:
            toolhead = self.printer.lookup_object('toolhead')
            if toolhead.get_status(self.printer.get_reactor().monotonic())['homed_axes'] != 'xyz':
                self.gcode.run_script_from_command('G28')
                if self._printing():
                    raise gcmd.error("Bed screw tools require the printer to be idle and not printing")
            old_height = prtouch.def_hmvz
            limits = toolhead.get_status(self.printer.get_reactor().monotonic())
            old_accel, old_smoothing = float(limits['max_accel']), float(limits['max_accel_to_decel'])
            old_velocity, old_corner = float(limits['max_velocity']), float(limits['square_corner_velocity'])
            kinematics = toolhead.get_kinematics()
            old_z_velocity, old_z_accel = kinematics.get_max_z_velocity(), kinematics.get_max_z_accel()
            previous_mesh, previous_profile = bed_mesh.get_mesh(), bed_mesh.pmgr.current_profile
            owned_save = bed_mesh.__dict__.get('save_profile')
            had_save = 'save_profile' in bed_mesh.__dict__
            if (not callable(bed_mesh.save_profile) or not callable(bed_mesh.update_status)
                    or not callable(kinematics.set_max_z_velocity) or not callable(kinematics.set_max_z_accel)):
                raise TypeError('unsupported profile interface')
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise gcmd.error("The native bed mesh travel interface is not supported") from error
        if not all(isinstance(value, (int, float)) and math.isfinite(value)
                   for value in (old_height, old_accel, old_smoothing, old_velocity, old_corner,
                                 old_z_velocity, old_z_accel)) or min(old_accel, old_velocity, old_z_velocity, old_z_accel) <= 0 or min(old_smoothing, old_corner) < 0:
            raise gcmd.error("The native bed mesh travel settings are not supported")
        completed = False
        try:
            temperature = self.printer.lookup_object('heaters').lookup_heater('heater_bed').get_status(
                self.printer.get_reactor().monotonic())['temperature']
            if (not isinstance(temperature, (int, float))
                    or not math.isfinite(temperature) or temperature < 0):
                raise gcmd.error("The actual bed temperature is unavailable")
            # Native calibration saves default twice; both saves are suppressed.
            bed_mesh.save_profile = lambda name: None
            prtouch.def_hmvz = self.horizontal_move_z
            self.phase = 'probing'
            self.gcode.run_script_from_command('BED_MESH_CALIBRATE')
            self.phase = 'calculating'
            current = bed_mesh.get_mesh()
            if current is None or current is previous_mesh:
                raise gcmd.error("Native calibration did not produce a fresh full-bed mesh")
            signature = self._mesh_values(current)
            try:
                plane = fit_plane(signature[2], signature[0], signature[1])
            except (ValueError, ZeroDivisionError, OverflowError) as error:
                raise gcmd.error("The measured bed plane could not be calculated") from error
            if not all(math.isfinite(value) for value in plane):
                raise gcmd.error("The measured bed plane is not finite")
            a, b, c = plane
            positions = [(x, y, a*x + b*y + c) for (x, y), _ in self.screws]
            self._calculate_adjustments(positions)
            self.probed_temperature = float(temperature)
            self._mesh, self._signature = current, signature
            bed_mesh.pmgr.current_profile = ''
            bed_mesh.update_status()
            completed = True
            self.gcode.respond_info("Probed at %.1f°C." % temperature)
        finally:
            try:
                try:
                    if had_save:
                        bed_mesh.save_profile = owned_save
                    else:
                        bed_mesh.__dict__.pop('save_profile', None)
                    prtouch.def_hmvz = old_height
                    if not completed:
                        self._mesh = self._signature = None
                        self.probed_temperature = None
                        bed_mesh.pmgr.current_profile = previous_profile
                        bed_mesh.set_mesh(previous_mesh)
                        bed_mesh.update_status()
                finally:
                    try:
                        kinematics.set_max_z_accel(old_z_accel)
                    finally:
                        try:
                            kinematics.set_max_z_velocity(old_z_velocity)
                        finally:
                            self.gcode.run_script_from_command(
                                'SET_VELOCITY_LIMIT ACCEL=%.17g ACCEL_TO_DECEL=%.17g VELOCITY=%.17g SQUARE_CORNER_VELOCITY=%.17g'
                                % (old_accel, old_smoothing, old_velocity, old_corner))
            except Exception:
                self._mesh = self._signature = None
                self.probed_temperature = None
                raise

    def _reset_progress(self):
        self.phase, self.error_message = 'idle', ''
        self._started_at = self._stopped_at = self._probe_started_at = None
        self._eta_deadline = None
        self._completed_points = set()
        self._current_point = None

    def _handle_output(self, message):
        if self.phase != 'probing' or not self._busy or not isinstance(message, str):
            return
        for match in PROBE_REPORT.finditer(message):
            try:
                values = tuple(float(value) for value in match.groups())
                if not all(math.isfinite(value) for value in values):
                    continue
                point = []
                for value, low, high, count in zip(values[:2], self.mesh_min, self.mesh_max, self.probe_count):
                    step = (high - low) / (count - 1)
                    index = round((value - low) / step)
                    if not 0 <= index < count or abs(value - (low + index*step)) > min(.250001, step/3):
                        break
                    point.append(index)
                if len(point) != 2:
                    continue
                point = tuple(point)
                if point == self._current_point or point in self._completed_points:
                    continue
                now = self.printer.get_reactor().monotonic()
                if self._current_point is None:
                    self._probe_started_at = now
                else:
                    self._completed_points.add(self._current_point)
                    completed = len(self._completed_points)
                    if completed >= 2:
                        remaining = self.probe_count[0] * self.probe_count[1] - completed
                        self._eta_deadline = now + (now - self._probe_started_at) / completed * remaining
                self._current_point = point
            except (ValueError, TypeError, OverflowError, ZeroDivisionError):
                continue

    def _require_available(self, gcmd):
        if self._busy or self._printing():
            raise gcmd.error("Bed screw tools require the printer to be idle and not printing")

    def _printing(self):
        stats = self.printer.lookup_object('print_stats', None)
        sdcard = self.printer.lookup_object('virtual_sdcard', None)
        now = self.printer.get_reactor().monotonic()
        return (stats is not None and stats.get_status(now).get('state') in ('printing', 'paused')
                or sdcard is not None and sdcard.is_active())

    def _mesh_values(self, current):
        try:
            params = current.get_mesh_params()
            minimum = tuple(float(params[key]) for key in ('min_x', 'min_y'))
            maximum = tuple(float(params[key]) for key in ('max_x', 'max_y'))
            matrix = tuple(tuple(float(value) for value in row) for row in current.get_probed_matrix())
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as error:
            raise self.gcode.error("The measured bed mesh data is invalid") from error
        if (len(matrix) != self.probe_count[1]
                or any(len(row) != self.probe_count[0] for row in matrix)
                or tuple(params.get(key) for key in ('x_count', 'y_count')) != self.probe_count
                or any(not math.isfinite(value) for row in matrix for value in row)
                or any(not math.isfinite(value) or not math.isclose(value, expected, rel_tol=0., abs_tol=0.000001)
                       for value, expected in zip(minimum + maximum, self.mesh_min + self.mesh_max))):
            raise self.gcode.error("The native mesh is not a finite full-bed grid with the configured bounds and count")
        return minimum, maximum, matrix, copy.deepcopy(params)

    def _latest_mesh(self):
        current = self.printer.lookup_object('bed_mesh').get_mesh()
        return (not self._busy and self._mesh is not None and current is self._mesh
                and self._mesh_values(current) == self._signature)

    def cmd_SCREWS_TILT_SAVE_MESH(self, gcmd):
        self._require_available(gcmd)
        self.printer.lookup_object('toolhead').wait_moves()
        self._require_available(gcmd)
        if not self._latest_mesh():
            raise gcmd.error("Run the bed screw helper again before saving; its mesh is no longer current")
        use_default = gcmd.get_int('DEFAULT', 0, minval=0, maxval=1)
        name = 'default' if use_default else 'screws_%dC' % round(self.probed_temperature)
        bed_mesh = self.printer.lookup_object('bed_mesh')
        configfile = self.printer.lookup_object('configfile')
        section = 'bed_mesh ' + name
        try:
            written = self._saved_config(configfile)
            self._check_pending(configfile, section, written)
            saved, manager = configfile.autosave.fileconfig, bed_mesh.pmgr
            old_section = dict(saved.items(section)) if saved.has_section(section) else None
            old_profile = copy.deepcopy(manager.get_profiles().get(name))
            old_current = manager.current_profile
            had_pending = section in configfile.status_save_pending
            old_pending = copy.deepcopy(configfile.status_save_pending.get(section))
            old_flag = configfile.save_config_pending
            try:
                bed_mesh.save_profile(name)
                profile = manager.get_profiles().get(name)
                if (not profile or not saved.has_section(section) or not saved.has_option(section, 'points')
                        or tuple(tuple(float(value) for value in row) for row in profile['points']) != self._signature[2]
                        or profile['mesh_params'] != self._signature[3]):
                    raise gcmd.error("The measured mesh was not prepared for saving")
                expected = self._section(saved, section)
                self._check_pending(configfile, section, written)
                self.gcode.run_script_from_command('CXSAVE_CONFIG')
                if self._section(self._saved_config(configfile), section) != expected:
                    raise gcmd.error("The requested mesh was not found in the written configuration")
            except Exception:
                profiles = dict(manager.get_profiles())
                if old_profile is None:
                    profiles.pop(name, None)
                else:
                    profiles[name] = old_profile
                manager.profiles, manager.current_profile = profiles, old_current
                saved.remove_section(section)
                if old_section is not None:
                    saved.add_section(section)
                    for key, value in old_section.items():
                        saved.set(section, key, value)
                if had_pending:
                    configfile.status_save_pending[section] = old_pending
                else:
                    configfile.status_save_pending.pop(section, None)
                configfile.save_config_pending = old_flag
                bed_mesh.update_status()
                raise
        except (AttributeError, KeyError, TypeError, ValueError, OSError, configparser.Error) as error:
            raise gcmd.error("The mesh could not be saved and verified: %s" % error) from error
        self.gcode.respond_info("Saved mesh profile '%s'." % name)

    def _saved_config(self, configfile):
        filename = self.printer.get_start_args()['config_file']
        with open(filename, encoding='utf-8') as stream:
            text = stream.read()
        written = configparser.RawConfigParser(interpolation=None, strict=True)
        written.read_string(configfile._find_autosave_data(text)[1])
        return written

    def _section(self, parser, section):
        return ({key: '\n'.join(line.strip() for line in value.strip().splitlines())
                 for key, value in parser.items(section)}
                if parser.has_section(section) else None)

    def _check_pending(self, configfile, selected, written):
        for section in configfile.status_save_pending:
            if section != selected and self._section(configfile.autosave.fileconfig, section) != self._section(written, section):
                message = ("Another mesh profile has unsaved changes; save or discard those changes first"
                           if section.lower().startswith('bed_mesh ') else
                           "Other configuration changes are pending; save or discard them first")
                raise self.gcode.error(message)

    def get_status(self, eventtime):
        try:
            can_save = self._latest_mesh() and not self._printing()
        except Exception:
            can_save = False
        total = self.probe_count[0] * self.probe_count[1]
        completed = total if self.phase == 'complete' else min(len(self._completed_points), total - 1)
        stopped = self._stopped_at if self._stopped_at is not None else eventtime
        elapsed = max(0., stopped - self._started_at) if self._started_at is not None else 0.
        eta = None
        if self.phase == 'probing' and self._eta_deadline is not None and self._eta_deadline > eventtime:
            eta = self._eta_deadline - eventtime
        elif self.phase == 'complete':
            eta = 0.
        return {'error': self.max_diff_error,
            'max_deviation': self.max_diff,
            'results': self.results.copy(), 'probed_temperature': self.probed_temperature,
            'can_save_mesh': bool(can_save), 'phase': self.phase, 'running': self._busy,
            'completed_points': completed, 'total_points': total, 'elapsed': elapsed,
            'eta': eta, 'error_message': self.error_message}

    def _calculate_adjustments(self, positions):
        # Factors used for CW-M3, CCW-M3, CW-M4, CCW-M4, CW-M5, CCW-M5, CW-M6
        #and CCW-M6
        threads_factor = {0: 0.5, 1: 0.5, 2: 0.7, 3: 0.7, 4: 0.8, 5: 0.8,
        6: 1.0, 7: 1.0}
        is_clockwise_thread = (self.thread & 1) == 0
        screw_diff = []
        # Process the read Z values
        if self.direction is not None:
            # Lowest or highest screw is the base position used for comparison
            use_max = ((is_clockwise_thread and self.direction == 'CW')
                    or (not is_clockwise_thread and self.direction == 'CCW'))
            min_or_max = max if use_max else min
            i_base, z_base = min_or_max(
                enumerate([pos[2] for pos in positions]), key=lambda v: v[1])
        else:
            # First screw is the base position used for comparison
            i_base, z_base = 0, positions[0][2]
        # Provide the user some information on how to read the results
        self.gcode.respond_info("Adjustments are shown in degrees, "
                                "CW=clockwise, CCW=counter-clockwise")
        for i, screw in enumerate(self.screws):
            z = positions[i][2]
            coord, name = screw
            if i == i_base:
                # Show the results
                self.gcode.respond_info(
                    "%s : x=%.1f, y=%.1f, z=%.5f" %
                    (name + ' (base)', coord[0], coord[1], z))
                sign = "CW" if is_clockwise_thread else "CCW"
                self.results["screw%d" % (i + 1,)] = {'z': z, 'sign': sign,
                    'adjust': '00:00', 'is_base': True}
            else:
                # Calculate how knob must be adjusted for other positions
                diff = z_base - z
                screw_diff.append(abs(diff))
                if abs(diff) < 0.001:
                    adjust = 0
                else:
                    adjust = diff / threads_factor.get(self.thread, 0.5)
                if is_clockwise_thread:
                    sign = "CW" if adjust >= 0 else "CCW"
                else:
                    sign = "CCW" if adjust >= 0 else "CW"
                full_turns, minutes = divmod(int(round(abs(adjust) * 60)), 60)
                # Show the results
                self.gcode.respond_info(
                    "%s : x=%.1f, y=%.1f, z=%.5f : adjust %s %d°" %
                    (name, coord[0], coord[1], z, sign, (full_turns * 60 + minutes) * 6))
                self.results["screw%d" % (i + 1,)] = {'z': z, 'sign': sign,
                    'adjust':"%02d:%02d" % (full_turns, minutes),
                    'is_base': False}
        if (self.max_diff is not None
                and any(d > self.max_diff for d in screw_diff)):
            self.max_diff_error = True
            raise self.gcode.error(
                "bed level exceeds configured limits ({}mm)! " \
                "Adjust screws and run SCREWS_TILT_CALCULATE again.".format(
                    self.max_diff))

def load_config(config):
    return ScrewsTiltAdjust(config)
