"""Temporarily raise XY first-pass and bed-centre homing motion limits."""
import ast
from . import mesh, mods

TARGET = "/mnt/UDISK/printer_data/config/sensorless.cfg"
ORIGINAL = TARGET.removesuffix(".cfg") + ".cfn.Original.txt"
MANIFEST = ORIGINAL + ".json"
SECTION = "gcode_macro _HOME_Z"
ORIGINAL_LINE = "G1 x{x_park} y{y_park} F12000"
ENABLED_LINE = "_UNCURSER_HOME_CENTRE X={x_park} Y={y_park}"
REFERENCE_HASH = "9d635c7410d49325fbca7f70108451fbac39f6d5b3fed06e49a9507934764a55"


def anchor(data):
    regular, generated = mesh.split(data)
    config = mesh.parser(regular)
    if not config.has_option(SECTION, "gcode"):
        raise ValueError("The Z homing macro is missing or has no G-code.")
    if mesh.generated_parser(generated).has_section(SECTION):
        raise ValueError("The Z homing macro is also defined in SAVE_CONFIG.")
    lines, current, matches = regular.splitlines(True), None, []
    for index, line in enumerate(lines):
        header = mesh.HEADER.fullmatch(line.rstrip("\r\n"))
        if header:
            current = header[1].strip()
        elif line.strip() in (ORIGINAL_LINE, ENABLED_LINE):
            if current != SECTION:
                raise ValueError("The bed-centre move occurs outside the Z homing macro.")
            matches.append(index)
        elif (not line.lstrip().startswith(("#", ";"))
              and ("_UNCURSER_HOME_CENTRE" in line
                   or (current == "homing_override"
                       and line.strip() == "SET_VELOCITY_LIMIT ACCEL_TO_DECEL=5000"))):
            raise ValueError("The homing file contains an unexpected homing or smoothing modification.")
    if len(matches) != 1:
        raise ValueError("Expected exactly one recognized bed-centre homing move.")
    index = matches[0]
    if (index == 0 or [line.strip() for line in lines[index - 1:index]] != ["M204 S2000"]
            or [line.strip() for line in lines[index + 1:index + 3]] != ["G90", "M204 S500"]):
        raise ValueError("The bed-centre move has unexpected surrounding acceleration commands.")
    return lines, index, generated


def validate(data):
    anchor(data)


def fingerprint(data):
    lines, index, _ = anchor(data)
    lines[index] = lines[index].replace(lines[index].strip(), ORIGINAL_LINE, 1)
    return mods.digest(("".join(lines).replace("\r\n", "\n").rstrip("\n") + "\n").encode())


def inspect(data, original=None):
    try:
        lines, index, _ = anchor(data)
        enabled = lines[index].strip() == ENABLED_LINE
        reference = fingerprint(original) if original is not None else REFERENCE_HASH
        known = fingerprint(data) == reference
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if known and enabled else "Original" if known else "Unknown",
                "reason": "" if known else "sensorless.cfg differs from the recognized original outside this mod."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False,
                "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    lines, index, generated = anchor(data)
    if enabled:
        replacement = ENABLED_LINE
    else:
        if original is None:
            raise ValueError("The on-printer sensorless.cfg original is required to turn this mod off.")
        saved_lines, saved_index, _ = anchor(original)
        replacement = saved_lines[saved_index].strip()
    lines[index] = lines[index].replace(lines[index].strip(), replacement, 1)
    result = ("".join(lines) + generated).encode()
    if fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated part of the homing file changed.")
    return result


def validate_includes(includes):
    for path, data in includes.items():
        regular, generated = mesh.split(data)
        if mesh.parser(regular).has_section(SECTION) or mesh.generated_parser(generated).has_section(SECTION):
            raise ValueError("The Z homing macro is also defined in " + path)


def settings(data):
    validate(data)
    regular, _ = mesh.split(data)
    return [[SECTION, "gcode", mesh.parser(regular).get(SECTION, "gcode")]]


LIMITS = '''def _uncursed_homing_move(toolhead, move):
    accel = toolhead.get_max_accel()
    smoothing = toolhead.requested_accel_to_decel
    cruise = toolhead.min_cruise_ratio
    try:
        toolhead.printer.lookup_object('gcode').run_script_from_command(
            "SET_VELOCITY_LIMIT ACCEL=5000 ACCEL_TO_DECEL=5000")
        return move()
    finally:
        toolhead.set_max_accel(accel)
        toolhead.requested_accel_to_decel = smoothing
        toolhead.min_cruise_ratio = cruise
        toolhead._calc_junction_deviation()

'''
REGISTER = "        gcode.register_command('_UNCURSER_HOME_CENTRE', self.cmd_UNCURSER_HOME_CENTRE)\n"
CENTRE = '''    def cmd_UNCURSER_HOME_CENTRE(self, gcmd):
        x, y = gcmd.get_float('X'), gcmd.get_float('Y')
        toolhead = self.printer.lookup_object('toolhead')
        gcode = self.printer.lookup_object('gcode')
        _uncursed_homing_move(toolhead, lambda: gcode.run_script_from_command(
            "G1 X%.17g Y%.17g F12000" % (x, y)))

'''
FIRST_PREFIX = "        def _uncursed_first_home():\n"
FIRST_SUFFIX = '''        if homing_axes in ([0], [1]):
            _uncursed_homing_move(self.toolhead, _uncursed_first_home)
        else:
            _uncursed_first_home()
'''
FIRST_ORIGINAL = '''if self.stepper_z_sensorless_flag:
    hmove.homing_move(homepos, hi.speed*4, False, True, False)
else:
    hmove.homing_move(homepos, hi.speed)
'''


def marked(name, body, indent=""):
    return (indent + "# UNCURSER BEGIN homing_speed:" + name + "\n" + body
            + indent + "# UNCURSER END homing_speed:" + name + "\n")


def _class_method(tree, class_name, method):
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    methods = [node for owner in classes for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == method]
    if len(classes) != 1 or len(methods) != 1:
        raise ValueError("Expected one native " + class_name + "." + method + " method.")
    return classes[0], methods[0]


def _python_anchors(text):
    tree = ast.parse(text, feature_version=(3, 9))
    owner, home = _class_method(tree, "Homing", "home_rails")
    _, init = _class_method(tree, "PrinterHoming", "__init__")
    _, g28 = _class_method(tree, "PrinterHoming", "cmd_G28")
    expected = ast.dump(ast.parse(FIRST_ORIGINAL).body[0], include_attributes=False)
    first = [node for node in home.body
             if isinstance(node, ast.If) and ast.dump(node, include_attributes=False) == expected]
    register = [node for node in init.body if ast.get_source_segment(text, node)
                == "gcode.register_command('G28', self.cmd_G28)"]
    if len(first) != 1 or len(register) != 1:
        raise ValueError("Cannot locate the unique native first homing pass and G28 registration.")
    return owner, first[0], register[0], g28


class HomingModule:
    TARGET = "/usr/share/klipper/klippy/extras/homing.py"
    ORIGINAL = TARGET.removesuffix(".py") + ".ptn.Original.txt"
    MANIFEST = ORIGINAL + ".json"
    REFERENCE_HASH = "cc847821cb60fb0a322e75685ea2c27e86d608a15ef4888a0cbe3dce16a2f765"

    @staticmethod
    def strip_mod(data):
        text = data.decode().replace("\r\n", "\n")
        if "homing_speed:" not in text:
            if "_uncursed_homing" in text or "_uncursed_first_home" in text or "_UNCURSER_HOME_CENTRE" in text:
                raise ValueError("Unrecognized homing-speed code remains in homing.py.")
            _python_anchors(text)
            return text, False
        for name, body, indent in (("limits", LIMITS, ""), ("register", REGISTER, "        "),
                                    ("centre", CENTRE, "    ")):
            block = marked(name, body, indent)
            if text.count(block) != 1:
                raise ValueError("The homing-speed modification is partial or has been edited.")
            text = text.replace(block, "", 1)
        begin = "        # UNCURSER BEGIN homing_speed:first_xy\n"
        end = "        # UNCURSER END homing_speed:first_xy\n"
        if text.count(begin) != 1 or text.count(end) != 1:
            raise ValueError("The first-pass homing modification is partial or has been edited.")
        start, stop = text.index(begin), text.index(end)
        body = text[start + len(begin):stop]
        if not body.startswith(FIRST_PREFIX) or not body.endswith(FIRST_SUFFIX):
            raise ValueError("The first-pass homing modification has been edited.")
        nested = body[len(FIRST_PREFIX):-len(FIRST_SUFFIX)].splitlines(True)
        if not nested or any(line.strip() and not line.startswith("            ") for line in nested):
            raise ValueError("The original first homing pass has unexpected indentation.")
        original = "".join(line[4:] if line.strip() else line for line in nested)
        text = text[:start] + original + text[stop + len(end):]
        if "homing_speed:" in text or "_uncursed_homing" in text or "_uncursed_first_home" in text or "_UNCURSER_HOME_CENTRE" in text:
            raise ValueError("Unrecognized homing-speed code remains in homing.py.")
        _python_anchors(text)
        return text, True

    @classmethod
    def validate(cls, data):
        cls.strip_mod(data)

    @classmethod
    def fingerprint(cls, data):
        text, _ = cls.strip_mod(data)
        return mods.digest(text.encode())

    @classmethod
    def inspect(cls, data, original=None):
        try:
            _, enabled = cls.strip_mod(data)
            reference = cls.fingerprint(original) if original is not None else cls.REFERENCE_HASH
            known = cls.fingerprint(data) == reference
            return {"known": known, "enabled": enabled, "patchable": True,
                    "label": "Uncursed" if known and enabled else "Original" if known else "Unknown",
                    "reason": "" if known else "homing.py differs from the recognized original outside this mod."}
        except (ValueError, SyntaxError, UnicodeError) as error:
            return {"known": False, "enabled": None, "patchable": False,
                    "label": "Conflict", "reason": str(error)}

    @classmethod
    def transform(cls, data, enabled, original=None):
        text, current = cls.strip_mod(data)
        if enabled == current:
            return data
        newline = "\r\n" if b"\r\n" in data else "\n"
        if enabled:
            owner, first, register, g28 = _python_anchors(text)
            lines = text.splitlines(True)
            block = "".join(lines[first.lineno - 1:first.end_lineno])
            nested = "".join("    " + line if line.strip() else line for line in block.splitlines(True))
            edits = [(owner.lineno - 1, owner.lineno - 1, marked("limits", LIMITS)),
                     (register.end_lineno, register.end_lineno, marked("register", REGISTER, "        ")),
                     (g28.lineno - 1, g28.lineno - 1, marked("centre", CENTRE, "    ")),
                     (first.lineno - 1, first.end_lineno,
                      marked("first_xy", FIRST_PREFIX + nested + FIRST_SUFFIX, "        "))]
            for start, stop, replacement in sorted(edits, reverse=True):
                lines[start:stop] = [replacement]
            text = "".join(lines)
            ast.parse(text, feature_version=(3, 9))
        result = text.replace("\n", newline).encode()
        if cls.fingerprint(result) != cls.fingerprint(data):
            raise ValueError("An unrelated part of homing.py changed.")
        return result


HOMING = HomingModule()
