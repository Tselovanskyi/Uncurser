import hashlib
import re
import unittest
from unittest.mock import patch

from app import orca_gcode as gcode


PREFIX = """; MINX = {first_layer_print_min[0]}
; MINY = {first_layer_print_min[1]}
; MAXX = {first_layer_print_max[0]}
; MAXY = {first_layer_print_max[1]}
M140 S[bed_temperature_initial_layer_single]
M104 S0
{if activate_chamber_temp_control[initial_extruder]}
M191 S{chamber_temperature[initial_extruder]}
{else}
M141 S0
{endif}
START_PRINT EXTRUDER_TEMP=[nozzle_temperature_initial_layer] BED_TEMP=[bed_temperature_initial_layer_single]
T[initial_no_support_extruder]
M109 S[nozzle_temperature_initial_layer]
M204 S2000
"""
PRIME = """G1 Z3 F600
M83
G1 Y150 F12000
G1 X0 F12000
G1 Z0.2 F600
G1 X0 Y150 F6000
G1 X0 Y0 E15 F6000
G1 X150 Y0 E15 F6000
G92 E0
G1 Z1 F600
"""
SOURCE = PREFIX + PRIME + "LOAD_AI_NOZZLE_CAM_POWER_ON\n"


def expanded_prime(model_min, model_max, bed_min=(0, 0), bed_max=(350, 350)):
    """Evaluate just our template's expression subset to inspect generated paths."""
    values = {"first_layer_print_min": model_min, "first_layer_print_max": model_max,
              "print_bed_min": bed_min, "print_bed_max": bed_max,
              "min": min, "max": max}
    output = []
    stack = []
    enabled = True

    def expression(text):
        return eval(text.replace("&&", " and ").replace("||", " or "),
                    {"__builtins__": {}}, values)

    for line in gcode._template().splitlines():
        if line.startswith("{if "):
            result = enabled and bool(expression(line[4:-1]))
            stack.append((enabled, result))
            enabled = result
        elif line.startswith("{elsif "):
            parent, taken = stack[-1]
            enabled = parent and not taken and bool(expression(line[7:-1]))
            stack[-1] = (parent, taken or enabled)
        elif line == "{else}":
            parent, taken = stack[-1]
            enabled = parent and not taken
            stack[-1] = (parent, True)
        elif line == "{endif}":
            enabled = stack.pop()[0]
        elif enabled:
            if line.startswith("{") and line.endswith("}") and "=" in line:
                name, value = line[1:-1].removeprefix("local ").split("=", 1)
                values[name.strip()] = expression(value)
            else:
                output.append(re.sub(r"\{([^{}]+)\}",
                                     lambda match: str(expression(match[1])), line))
    return output, values


class OrcaGcodeTests(unittest.TestCase):
    def test_exact_roundtrip_with_line_endings_and_eof(self):
        for ending in ("\n", "\r\n", "\r"):
            for source in (SOURCE, PREFIX + PRIME.rstrip("\n")):
                source = source.replace("\n", ending)
                with self.subTest(ending=repr(ending), eof=source[-1]):
                    converted = gcode.to_adaptive(source)
                    self.assertTrue(gcode.is_adaptive(converted))
                    self.assertEqual(gcode.to_original(converted), source)
                    self.assertEqual(gcode.to_adaptive(converted), converted)

    def test_only_original_prime_changes_and_outside_edits_survive(self):
        converted = gcode.to_adaptive(SOURCE)
        self.assertTrue(converted.startswith(PREFIX))
        self.assertTrue(converted.endswith("LOAD_AI_NOZZLE_CAM_POWER_ON\n"))
        changed = converted.replace("M140 S[bed_temperature_initial_layer_single]", "M140 S60")
        changed = changed.replace("LOAD_AI_NOZZLE_CAM_POWER_ON", "MY_LED_COMMAND")
        self.assertEqual(gcode.to_original(changed),
                         SOURCE.replace("M140 S[bed_temperature_initial_layer_single]", "M140 S60")
                         .replace("LOAD_AI_NOZZLE_CAM_POWER_ON", "MY_LED_COMMAND"))

    def test_saved_data_allows_reversal_after_template_changes(self):
        converted = gcode.to_adaptive(SOURCE)
        with patch.object(gcode, "_template", return_value="future implementation\n"):
            self.assertEqual(gcode.to_original(converted), SOURCE)

    def test_saved_original_uses_one_block_of_plain_comments(self):
        converted = gcode.to_adaptive(SOURCE)
        commented = "".join("; " + line for line in PRIME.splitlines(keepends=True))
        self.assertIn(gcode._ORIGINAL_BEGIN + "\n" + commented
                      + gcode._ORIGINAL_END + "\n", converted)
        self.assertNotIn(gcode._SAVED, converted)

    def test_backup_is_hidden_and_metadata_is_not_generated(self):
        converted = gcode.to_adaptive(SOURCE)
        before, hidden = converted.split("{if false}\n", 1)
        hidden, after = hidden.rsplit("{endif}\n", 1)
        self.assertIn("; KAMP\n", before)
        self.assertIn(gcode._ORIGINAL_BEGIN, hidden)
        self.assertIn(gcode._ORIGINAL_END, hidden)
        for marker in (gcode._DIGEST, gcode._EOF, gcode._GENERATED):
            self.assertNotIn(marker, converted)
        self.assertNotIn("; G1 X150 Y0 E15 F6000", before + after)
        self.assertEqual(gcode.to_original(converted), SOURCE)

    def test_existing_output_still_reverses(self):
        for prime, tail in ((PRIME, "LOAD_AI_NOZZLE_CAM_POWER_ON\n"), (PRIME.rstrip("\n"), "")):
            eof = not prime.endswith("\n")
            metadata = (gcode._DIGEST + hashlib.sha256(prime.encode()).hexdigest() + "\n"
                        + gcode._EOF + str(int(eof)) + "\n"
                        + gcode._GENERATED + hashlib.sha256(gcode._template().encode()).hexdigest() + "\n")
            commented = "".join("; " + line for line in prime.splitlines(keepends=True))
            prefixed = "".join(gcode._SAVED + line for line in prime.splitlines(keepends=True))
            if eof:
                commented += "\n"
                prefixed += "\n"
            saved = gcode._ORIGINAL_BEGIN + "\n" + commented + gcode._ORIGINAL_END + "\n"
            for body in (metadata + gcode._template() + prefixed,
                         metadata + gcode._template() + saved,
                         gcode._template() + "{if false}\n" + metadata + saved + "{endif}\n"):
                legacy = PREFIX + gcode.BEGIN + "\n" + body + gcode.END + "\n" + tail
                with self.subTest(body=body[:40], eof=eof):
                    self.assertEqual(gcode.to_original(legacy), PREFIX + prime + tail)

    def test_reverse_conversion_uses_saved_comments_without_checksums(self):
        converted = gcode.to_adaptive(SOURCE).replace("E30 F360", "E31 F360")
        converted = converted.replace("; G1 X150", "; G1 X151")
        self.assertEqual(gcode.to_original(converted), SOURCE.replace("G1 X150", "G1 X151"))

    def test_incomplete_or_uncommented_saved_blocks_are_rejected(self):
        converted = gcode.to_adaptive(SOURCE)
        for changed in (converted.replace("; G1 X150", "G1 X150"),
                        converted.replace(gcode._ORIGINAL_BEGIN, ""),
                        converted.replace(gcode._ORIGINAL_END, ""),
                        converted.replace(gcode._ORIGINAL_BEGIN,
                                          gcode._ORIGINAL_BEGIN + "\n" + gcode._ORIGINAL_BEGIN),
                        converted.replace(gcode._ORIGINAL_END,
                                          gcode._ORIGINAL_END + "\n" + gcode._ORIGINAL_END),
                        converted.replace("{if false}", "{if true}"),
                        converted.replace(gcode._ORIGINAL_END + "\n{endif}", gcode._ORIGINAL_END),
                        converted.replace(gcode.END, "")):
            with self.subTest(changed=changed[-100:]), self.assertRaises(gcode.ConversionError):
                gcode.to_original(changed)

    def test_refuses_unrecognized_or_ambiguous_extrusions(self):
        for source in (SOURCE.replace("X150 Y0 E15", "X150 Y0 E16"),
                       SOURCE + PRIME, "G1 Z20\n" + SOURCE,
                       SOURCE + "G1 E10\n", SOURCE.replace("M83", "M82"),
                       SOURCE + "; MINX = 0\n", "; KAMP\n" + SOURCE):
            with self.subTest(source=source[-60:]), self.assertRaises(gcode.ConversionError):
                gcode.to_adaptive(source)
        with self.assertRaises(gcode.ConversionError):
            gcode.convert(SOURCE, slicer="cura")

    def test_constant_formatting_does_not_change_recognition(self):
        source = SOURCE.replace("G1 X0 Y150 F6000", "G1X0.0Y150.0F6000.0")
        self.assertEqual(gcode.to_original(gcode.to_adaptive(source)), source)
        source = "M83\n" + SOURCE.replace("M83\n", "")
        self.assertEqual(gcode.to_original(gcode.to_adaptive(source)), source)

    def test_all_sides_and_mesh_bounds_cover_prime_and_wipe(self):
        cases = [((100, 100), (150, 150), 1), ((5, 100), (150, 150), 2),
                 ((5, 100), (345, 150), 3), ((5, 5), (345, 150), 4),
                 ((325, 325), (345, 345), 1), ((5, 5), (345, 345), 0)]
        for model_min, model_max, expected in cases:
            with self.subTest(model_min=model_min, model_max=model_max):
                lines, values = expanded_prime(model_min, model_max)
                self.assertEqual(values["uncurser_prime_side"], expected)
                moves = [gcode._command(line)[1] for line in lines if line.startswith("G1 ")]
                xy = [(move["X"], move["Y"]) for move in moves if "X" in move]
                if not expected:
                    self.assertEqual(moves, [])
                else:
                    self.assertEqual(len(xy), 3)
                    for x, y in xy:
                        self.assertTrue(5 <= x <= 345 and 5 <= y <= 345)
                        self.assertFalse(model_min[0] <= x <= model_max[0]
                                         and model_min[1] <= y <= model_max[1])
                    self.assertAlmostEqual(abs(xy[1][0] - xy[0][0])
                                           + abs(xy[1][1] - xy[0][1]), 30)
                    self.assertAlmostEqual(abs(xy[2][0] - xy[1][0])
                                           + abs(xy[2][1] - xy[1][1]), 10)
                bounds = {line[2:6].strip(): float(line.split("=", 1)[1])
                          for line in lines if line.startswith(("; MIN", "; MAX"))}
                self.assertEqual(bounds, {"MINX": min([model_min[0]] + [x for x, y in xy]),
                                          "MINY": min([model_min[1]] + [y for x, y in xy]),
                                          "MAXX": max([model_max[0]] + [x for x, y in xy]),
                                          "MAXY": max([model_max[1]] + [y for x, y in xy])})
                first_motion = next((i for i, line in enumerate(lines) if line.startswith("G1 ")), len(lines))
                self.assertLess(lines.index("; KAMP"), first_motion)
                self.assertTrue(all(i < first_motion for i, line in enumerate(lines)
                                    if line.startswith(("; MIN", "; MAX"))))


if __name__ == "__main__":
    unittest.main()
