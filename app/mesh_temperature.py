"""Use the requested bed temperature in the native print-start mesh macro."""
from . import mesh, mod_comments, mods, storage

TARGET = "/mnt/UDISK/printer_data/config/gcode_macro.cfg"
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
SECTION = "gcode_macro BED_MESH_CALIBRATE_START_PRINT"
ORIGINAL_INITIALIZER = "{% set bed_temp = printer.custom_macro.default_bed_temp %}"
ENABLED_INITIALIZER = "{% set bed_temp = params.BED_TEMP|default(printer.heater_bed.target if printer.heater_bed.target > 0 else printer.custom_macro.default_bed_temp) %}"
ORIGINAL_CONDITION = "{% if 'BED_TEMP' in params|upper and params.BED_TEMP|default(0)|int >= printer.custom_macro.default_bed_temp %}"
# Recognize the shipped condition-only implementation for verified updates.
ENABLED_CONDITION = "{% if 'BED_TEMP' in params|upper %}"
# Read-only reference captured from the owner's printer on 2026-10-01.
REFERENCE_HASH = "71f62d87dc39b7f9de8e2eba2bbb68f524a1620ba5d3321e12454585e4fbd32a"


def anchors(data):
    mod_comments.read(data)
    regular, _ = mesh.split(data)
    config = mesh.parser(regular)
    if not config.has_option(SECTION, "gcode"):
        raise ValueError("The print-start mesh macro is missing or has no G-code.")
    lines = regular.splitlines(True)
    current, initializers, matches = None, [], []
    for index, line in enumerate(lines):
        header = mesh.HEADER.fullmatch(line.rstrip("\r\n"))
        if header:
            current = header[1].strip()
        elif current == SECTION:
            if line.strip() in (ORIGINAL_INITIALIZER, ENABLED_INITIALIZER):
                initializers.append(index)
            elif line.strip() in (ORIGINAL_CONDITION, ENABLED_CONDITION):
                matches.append(index)
    if len(initializers) != 1:
        raise ValueError("Expected exactly one recognized bed-temperature initializer in the print-start mesh macro.")
    if len(matches) != 1:
        raise ValueError("Expected exactly one recognized bed-temperature condition in the print-start mesh macro.")
    index = matches[0]
    if initializers[0] >= index:
        raise ValueError("The bed-temperature initializer must precede its condition.")
    if [line.strip() for line in lines[index + 1:index + 3]] != [
            "{% set bed_temp = params.BED_TEMP %}", "{% endif %}"]:
        raise ValueError("The bed-temperature condition has an unexpected body; no automatic edit is safe.")
    return lines, initializers[0], index


def condition(data):
    lines, _, index = anchors(data)
    return lines, index


def validate(data):
    anchors(data)


def fingerprint(data):
    lines, initializer, index = anchors(mod_comments.strip(data))
    lines[initializer] = lines[initializer].replace(lines[initializer].strip(), ORIGINAL_INITIALIZER, 1)
    lines[index] = "<UNCURSER PRINT MESH TEMPERATURE CONDITION>\n"
    return mods.digest("".join(lines).replace("\r\n", "\n").encode("utf-8"))


def inspect(data, original=None):
    try:
        lines, initializer, index = anchors(data)
        legacy = lines[index].strip() == ENABLED_CONDITION
        enabled = lines[initializer].strip() == ENABLED_INITIALIZER or legacy
        return {"known": True, "enabled": enabled, "legacy": legacy, "patchable": True,
                "label": "Uncursed" if enabled else "Original", "reason": ""}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False, "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    lines, initializer, index = anchors(data)
    if enabled:
        initial_value, replacement = ENABLED_INITIALIZER, ORIGINAL_CONDITION
    else:
        if original is None:
            raise ValueError("The on-printer gcode_macro.cfg original is required to turn this mod off.")
        saved_lines, saved_initializer, saved_index = anchors(original)
        initial_value = saved_lines[saved_initializer].strip()
        replacement = saved_lines[saved_index].strip()
    lines[initializer] = lines[initializer].replace(lines[initializer].strip(), initial_value, 1)
    lines[index] = lines[index].replace(lines[index].strip(), replacement, 1)
    _, generated = mesh.split(data)
    result = ("".join(lines) + generated).encode("utf-8")
    if fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated part of the macro file changed.")
    return result


def validate_includes(includes):
    for path, data in includes.items():
        regular, generated = mesh.split(data)
        if mesh.parser(regular).has_section(SECTION) or mesh.generated_parser(generated).has_section(SECTION):
            raise ValueError("The print-start mesh macro is also defined in " + path)


def settings(data):
    regular, _ = mesh.split(data)
    return [[SECTION, "gcode", mesh.parser(regular).get(SECTION, "gcode")]]
