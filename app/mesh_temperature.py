"""Use the requested bed temperature in the native print-start mesh macro."""
from . import mesh, mods

TARGET = "/mnt/UDISK/printer_data/config/gcode_macro.cfg"
ORIGINAL = TARGET.removesuffix(".cfg") + ".cfn.Original.txt"
MANIFEST = ORIGINAL + ".json"
SECTION = "gcode_macro BED_MESH_CALIBRATE_START_PRINT"
ORIGINAL_CONDITION = "{% if 'BED_TEMP' in params|upper and params.BED_TEMP|default(0)|int >= printer.custom_macro.default_bed_temp %}"
ENABLED_CONDITION = "{% if 'BED_TEMP' in params|upper %}"
# Read-only reference captured from the owner's printer on 2026-10-01.
REFERENCE_HASH = "71f62d87dc39b7f9de8e2eba2bbb68f524a1620ba5d3321e12454585e4fbd32a"


def condition(data):
    regular, _ = mesh.split(data)
    config = mesh.parser(regular)
    if not config.has_option(SECTION, "gcode"):
        raise ValueError("The print-start mesh macro is missing or has no G-code.")
    lines = regular.splitlines(True)
    current, matches = None, []
    for index, line in enumerate(lines):
        header = mesh.HEADER.fullmatch(line.rstrip("\r\n"))
        if header:
            current = header[1].strip()
        elif current == SECTION and line.strip() in (ORIGINAL_CONDITION, ENABLED_CONDITION):
            matches.append(index)
    if len(matches) != 1:
        raise ValueError("Expected exactly one recognized bed-temperature condition in the print-start mesh macro.")
    index = matches[0]
    if [line.strip() for line in lines[index + 1:index + 3]] != [
            "{% set bed_temp = params.BED_TEMP %}", "{% endif %}"]:
        raise ValueError("The bed-temperature condition has an unexpected body; no automatic edit is safe.")
    return lines, index


def validate(data):
    condition(data)


def fingerprint(data):
    lines, index = condition(data)
    lines[index] = "<UNCURSER PRINT MESH TEMPERATURE CONDITION>\n"
    return mods.digest("".join(lines).replace("\r\n", "\n").encode("utf-8"))


def inspect(data, original=None):
    try:
        lines, index = condition(data)
        enabled = lines[index].strip() == ENABLED_CONDITION
        reference = fingerprint(original) if original is not None else REFERENCE_HASH
        known = fingerprint(data) == reference
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if known and enabled else "Original" if known else "Unknown",
                "reason": "" if known else "This macro file differs from the recognized original outside this mod."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False, "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    lines, index = condition(data)
    if enabled:
        replacement = ENABLED_CONDITION
    else:
        if original is None:
            raise ValueError("The on-printer gcode_macro.cfg original is required to turn this mod off.")
        saved_lines, saved_index = condition(original)
        replacement = saved_lines[saved_index].strip()
    lines[index] = lines[index].replace(lines[index].strip(), replacement, 1)
    _, generated = mesh.split(data)
    result = ("".join(lines) + generated).encode("utf-8")
    if fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated part of the macro file changed.")
    return result


def restore(data, original):
    regular, _ = mesh.split(original)
    _, generated = mesh.split(data)
    result = (regular + generated).encode("utf-8")
    validate(result)
    return result


def validate_includes(includes):
    for path, data in includes.items():
        regular, generated = mesh.split(data)
        if mesh.parser(regular).has_section(SECTION) or mesh.generated_parser(generated).has_section(SECTION):
            raise ValueError("The print-start mesh macro is also defined in " + path)


def settings(data):
    regular, _ = mesh.split(data)
    return [[SECTION, "gcode", mesh.parser(regular).get(SECTION, "gcode")]]
