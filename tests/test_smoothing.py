"""Focused local checks for temporary first-pass and bed-centre homing limits."""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest

from app import mesh, smoothing as mod


_parser_path = Path(__file__).parent / "fixtures/k2_extended_gcode.py"
_parser = {}
exec(compile(_parser_path.read_text(encoding="utf-8"), str(_parser_path), "exec"), _parser)
K2GCodeCommand = _parser["GCodeCommand"]
K2GCodeDispatch = _parser["GCodeDispatch"]
K2CommandError = _parser["CommandError"]


SOURCE = b"""[gcode_macro _IF_HOME_Z]
gcode:
  M204 S100

[gcode_macro _HOME_Z]
gcode:
  _IF_HOME_Z
  G91
  M204 S2000
  G1 x{x_park} y{y_park} F12000
  G90
  M204 S500
  G28 Z
  G1 Z5 F600

[homing_override]
axes: xyz
gcode:
  M204 S500
  _HOME_Y
  _HOME_X
  BOX_GO_TO_EXTRUDE_POS
  PRES_CHECK RUN=1
  BOX_NOZZLE_CLEAN
  PRES_CHECK RUN=0
  _HOME_Z
  {% set acc = printer.toolhead.max_accel %}
  M204 S{acc}

[gcode_macro UNRELATED]
gcode:
  G4 P100
"""
HOMING_SOURCE = b"""class Homing:
    def home_rails(self, rails, forcepos, movepos):
        homing_axes = [axis for axis in range(3) if forcepos[axis] is not None]
        homepos = movepos
        hi = rails[0].get_homing_info()
        hmove = HomingMove(self.printer, [])
        if self.stepper_z_sensorless_flag:
            hmove.homing_move(homepos, hi.speed*4, False, True, False)
        else:
            hmove.homing_move(homepos, hi.speed)
        if hi.retract_dist:
            self.toolhead.move(movepos, hi.retract_speed)
            hmove.homing_move(homepos, hi.second_homing_speed)

class PrinterHoming:
    def __init__(self, printer):
        self.printer = printer
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('G28', self.cmd_G28)
    def cmd_G28(self, gcmd):
        pass
"""
GENERATED = (b"#*# <---------------------- SAVE_CONFIG ---------------------->\n"
             b"#*# [bed_mesh default]\n#*# points = 0.2, 0.3\n")


class MotionFixture:
    def __init__(self, accel=100., smoothing=100., cruise=None):
        self.accel, self.requested_accel_to_decel, self.min_cruise_ratio = accel, smoothing, cruise
        self.events = []
        self.fail_set = self.fail_move = False
        self.printer = self
        self.gcode = self
        self._calc_junction_deviation()

    def lookup_object(self, name):
        return self

    def get_max_accel(self):
        return self.accel

    def set_max_accel(self, accel):
        self.accel = accel

    def _calc_junction_deviation(self):
        if self.min_cruise_ratio is not None:
            self.requested_accel_to_decel = self.accel * (1 - self.min_cruise_ratio)
        self.effective_smoothing = min(self.accel, self.requested_accel_to_decel)

    def register_command(self, name, handler):
        pass

    def state(self):
        return self.accel, self.requested_accel_to_decel, self.min_cruise_ratio, self.effective_smoothing

    def run_script_from_command(self, command):
        if command == "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=5000":
            self.accel = self.requested_accel_to_decel = 5000.
            self.min_cruise_ratio = None
            self._calc_junction_deviation()
            if self.fail_set:
                raise RuntimeError("limit setter failed")
        elif command.startswith("G1 "):
            self.events.append(("centre", command, self.state()))
            if self.fail_move:
                raise RuntimeError("centre move failed")
        else:
            raise AssertionError(command)

    def move(self, pos, speed):
        self.events.append(("retract", speed, self.state()))

    def homing_move(self, pos, speed, *flags):
        self.events.append(("home", speed, self.state(), flags))
        if self.fail_move:
            raise RuntimeError("homing failed")


class ExtendedMotionFixture(MotionFixture, K2GCodeDispatch):
    register_command = K2GCodeDispatch.register_command

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.ready_gcode_handlers, self.base_gcode_handlers, self.gcode_help = {}, {}, {}
        self.respond_info = self.respond_raw = self.check_cancel_running = lambda *args, **kwargs: None

    def dispatch(self, command):
        name = command.split(None, 1)[0].upper()
        gcmd = K2GCodeCommand(self, name, command, {}, False)
        return self.ready_gcode_handlers[name](gcmd)


def payload(fixture):
    namespace = {"HomingMove": lambda printer, endstops: fixture}
    exec(mod.HOMING.transform(HOMING_SOURCE, True), namespace)
    return namespace


class HomingSpeedTests(unittest.TestCase):
    def test_config_compatibility_ignores_unrelated_macros_and_sections(self):
        current = SOURCE.replace(b'G4 P100', b'G4 P321') + b'\n[gcode_macro NEW_MACRO]\ngcode:\n  G4 P25\n'
        for value in (current, mod.transform(current, True)):
            with self.subTest(enabled=mod.inspect(value)['enabled']):
                self.assertTrue(mod.inspect(value)['known'])
                self.assertTrue(mod.inspect(value, SOURCE)['patchable'])
                self.assertEqual(mod.fingerprint(mod.transform(value, True)), mod.fingerprint(value))
        self.assertEqual(mod.transform(mod.transform(current, True), False, SOURCE), current)

    def test_config_owned_anchor_and_neighbor_changes_remain_conflicts(self):
        for value in (SOURCE.replace(mod.ORIGINAL_LINE.encode(), b'G1 X100 Y100 F12000'),
                      SOURCE.replace(mod.ORIGINAL_LINE.encode(), (mod.ORIGINAL_LINE + '\n  ' + mod.ORIGINAL_LINE).encode()),
                      SOURCE.replace(b'M204 S2000', b'M204 S3000'),
                      SOURCE.replace(b'G90\n  M204 S500', b'G90\n  M204 S600')):
            with self.subTest(value=value):
                state = mod.inspect(value, SOURCE)
                self.assertFalse(state['known'])
                self.assertFalse(state['patchable'])
                self.assertEqual(state['label'], 'Conflict')

    def test_first_xy_pass_and_only_first_pass_uses_5000(self):
        hi = SimpleNamespace(speed=100., second_homing_speed=50., retract_speed=60., retract_dist=10.)
        for axis in (0, 1, 2):
            for cruise in (None, .4):
                fixture = MotionFixture(smoothing=100. if cruise is None else 60., cruise=cruise)
                before = fixture.state()
                namespace = payload(fixture)
                homing = namespace["Homing"]()
                homing.printer, homing.toolhead = fixture, fixture
                homing.stepper_z_sensorless_flag = False
                forcepos = [None, None, None]
                forcepos[axis] = 0.
                homing.home_rails([SimpleNamespace(get_homing_info=lambda: hi)], forcepos, [0., 0., 0.])
                first, retract, second = fixture.events
                self.assertEqual(first[:2], ("home", 100.))
                self.assertEqual(first[2], (5000., 5000., None, 5000.) if axis in (0, 1) else before)
                self.assertEqual(retract[:2], ("retract", 60.))
                self.assertEqual(second[:2], ("home", 50.))
                self.assertEqual(retract[2], before)
                self.assertEqual(second[2], before)
                self.assertEqual(fixture.state(), before)

    def test_native_sensorless_flag_and_arguments_are_preserved(self):
        fixture = MotionFixture()
        namespace = payload(fixture)
        homing = namespace["Homing"]()
        homing.printer, homing.toolhead = fixture, fixture
        homing.stepper_z_sensorless_flag = True
        hi = SimpleNamespace(speed=100., second_homing_speed=50., retract_speed=60., retract_dist=0.)
        homing.home_rails([SimpleNamespace(get_homing_info=lambda: hi)], [0., None, None], [0., 0., 0.])
        self.assertEqual(fixture.events[0], ("home", 400., (5000., 5000., None, 5000.), (False, True, False)))

    def test_centre_command_dispatch_preserves_relative_coordinates_speed_and_prior_settings(self):
        enabled = mod.transform(SOURCE, True)
        regular, _ = mesh.split(enabled)
        gcode = mesh.parser(regular).get("gcode_macro _HOME_Z", "gcode")
        template = next(line.strip() for line in gcode.splitlines()
                        if line.strip().startswith("_UNCURSER_HOME_CENTRE "))
        command = template.format(x_park=-160.25, y_park=170.125)
        for cruise in (None, .4):
            fixture = ExtendedMotionFixture(accel=2000., smoothing=100. if cruise is None else 1200., cruise=cruise)
            before = fixture.state()
            namespace = payload(fixture)
            namespace["PrinterHoming"](fixture)
            with self.assertRaisesRegex(K2CommandError, "key514"):
                fixture.dispatch(command.replace("X=", "X").replace("Y=", "Y"))
            self.assertEqual(fixture.events, [])
            self.assertEqual(fixture.state(), before)
            fixture.dispatch(command)
            self.assertEqual(fixture.events, [("centre", "G1 X-160.25 Y170.125 F12000", (5000., 5000., None, 5000.))])
            self.assertEqual(fixture.state(), before)
            self.assertIn(b"  M204 S2000\n  " + mod.ENABLED_LINE.encode() + b"\n  G90\n  M204 S500\n", enabled)
            self.assertIn(b"  G28 Z\n  G1 Z5 F600\n", enabled)

    def test_limits_restore_when_motion_or_limit_command_fails(self):
        for fail in ("fail_move", "fail_set"):
            for cruise in (None, .4):
                fixture = MotionFixture(smoothing=7000. if cruise is None else 60., cruise=cruise)
                before = fixture.state()
                setattr(fixture, fail, True)
                namespace = payload(fixture)
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    namespace["_uncursed_homing_move"](fixture, lambda: fixture.homing_move([], 100.))
                self.assertEqual(fixture.state(), before)

    def test_cfg_and_python_roundtrip_preserve_user_edits_endings_and_generated_data(self):
        for source, module in ((SOURCE, mod), (HOMING_SOURCE, mod.HOMING)):
            for original in (source, source.replace(b"\n", b"\r\n"), source.rstrip(b"\n")):
                enabled = module.transform(original, True)
                self.assertEqual(module.transform(enabled, True), enabled)
                self.assertEqual(module.transform(enabled, False, original), original)
                self.assertTrue(module.inspect(enabled, original)["known"])
                if b"\r\n" in original:
                    self.assertNotIn(b"\n", enabled.replace(b"\r\n", b""))
        enabled = mod.transform(SOURCE + GENERATED, True).replace(b"0.2, 0.3", b"0.4, 0.5")
        self.assertEqual(mesh.split(enabled)[1], mesh.split(mod.transform(enabled, False, SOURCE))[1])
        current = mod.transform(SOURCE, True).replace(b"G4 P100", b"G4 P123")
        self.assertEqual(mod.transform(current, False, SOURCE), SOURCE.replace(b"G4 P100", b"G4 P123"))
        user = SOURCE.replace(b"G4 P100", b"SET_VELOCITY_LIMIT ACCEL_TO_DECEL=1000")
        self.assertEqual(mod.transform(mod.transform(user, True), False, SOURCE), user)
        self.assertNotIn(b"SET_VELOCITY_LIMIT ACCEL_TO_DECEL=5000", mod.transform(SOURCE, True))
        current = mod.HOMING.transform(HOMING_SOURCE, True).replace(b"        pass\n", b"        return 123\n")
        self.assertEqual(mod.HOMING.transform(current, False, HOMING_SOURCE), HOMING_SOURCE.replace(b"        pass\n", b"        return 123\n"))

    def test_partial_edited_or_ambiguous_hooks_are_blocked(self):
        enabled_cfg = mod.transform(SOURCE, True)
        for current in (SOURCE + SOURCE, enabled_cfg.replace(b"_UNCURSER_HOME_CENTRE", b"_UNCURSER_HOME_CENTRE_EDITED"),
                        SOURCE.replace(b"M204 S2000", b"M204 S3000"),
                        SOURCE.replace(b"M204 S{acc}", b"M204 S{acc}\n  SET_VELOCITY_LIMIT ACCEL_TO_DECEL=5000"),
                        SOURCE + GENERATED.replace(b"[bed_mesh default]", b"[gcode_macro _HOME_Z]")):
            self.assertFalse(mod.inspect(current)["patchable"])
            with self.assertRaises(ValueError):
                mod.transform(current, True)
        enabled_python = mod.HOMING.transform(HOMING_SOURCE, True)
        for current in (HOMING_SOURCE + HOMING_SOURCE,
                        enabled_python.replace(b"ACCEL=5000", b"ACCEL=50001"),
                        enabled_python.replace(b"# UNCURSER END homing_speed:limits", b"# REMOVED"),
                        enabled_python.replace(b"hi.speed*4", b"hi.speed*5")):
            self.assertFalse(mod.HOMING.inspect(current)["patchable"])
            with self.assertRaises((ValueError, SyntaxError)):
                mod.HOMING.transform(current, True)
        with self.assertRaisesRegex(ValueError, "original"):
            mod.transform(enabled_cfg, False)

    def test_readonly_reference_and_included_macro_validation(self):
        root = Path(__file__).resolve().parents[1] / "build/screw-speed-readonly"
        for name, module in (("sensorless.cfg", mod), ("extras-homing.py", mod.HOMING)):
            if (root / name).exists():
                original = (root / name).read_bytes()
                self.assertEqual(module.fingerprint(original), module.REFERENCE_HASH)
                enabled = module.transform(original, True)
                self.assertEqual(module.transform(enabled, False, original), original)
                ast.parse(enabled) if module is mod.HOMING else None
        for included in (SOURCE, GENERATED.replace(b"[bed_mesh default]", b"[gcode_macro _HOME_Z]")):
            with self.assertRaisesRegex(ValueError, "also defined"):
                mod.validate_includes({"other.cfg": included})


if __name__ == "__main__":
    unittest.main()
