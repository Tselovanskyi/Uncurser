"""Reversible Orca machine-start conversion; no printer or filesystem access."""

import re


class ConversionError(ValueError):
    """The supplied start code cannot be changed without guessing ownership."""


BEGIN = "; UNCURSER ADAPTIVE PRIME BEGIN"
END = "; UNCURSER ADAPTIVE PRIME END"
_SAVED = "; UNCURSER ORIGINAL | "
_ORIGINAL_BEGIN = "; UNCURSER ORIGINAL BEGIN"
_ORIGINAL_END = "; UNCURSER ORIGINAL END"
_DIGEST = "; UNCURSER ORIGINAL SHA256 "
_EOF = "; UNCURSER ORIGINAL EOF "
_GENERATED = "; UNCURSER GENERATED SHA256 "
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_FIELD = re.compile(r"([A-Za-z])\s*(" + _NUMBER + r")")
_COMMAND = re.compile(r"^([GMTgmt]\d+)\s*(.*)$")


def _command(line):
    """Read constant commands only. Unknown expressions never match a prime."""
    code = line.split(";", 1)[0].strip()
    match = _COMMAND.fullmatch(code)
    if not match:
        return code.upper(), None
    fields = {}
    tail = match[2]
    cursor = 0
    for field in _FIELD.finditer(tail):
        if tail[cursor:field.start()].strip() or field[1].upper() in fields:
            return code.upper(), None
        fields[field[1].upper()] = float(field[2])
        cursor = field.end()
    if tail[cursor:].strip():
        return code.upper(), None
    return match[1].upper(), fields


def _matches(command, name, fields, optional=("F",)):
    cmd, values = command
    return (cmd == name and values is not None
            and all(values.get(key) == value for key, value in fields.items())
            and not set(values).difference(fields, optional))


def _original_span(text):
    lines = text.splitlines(keepends=True)
    active = [(i, _command(line)) for i, line in enumerate(lines)
              if line.split(";", 1)[0].strip()]
    triples = []
    for n in range(len(active) - 2):
        if (_matches(active[n][1], "G1", {"X": 0, "Y": 150})
                and _matches(active[n + 1][1], "G1", {"X": 0, "Y": 0, "E": 15})
                and _matches(active[n + 2][1], "G1", {"X": 150, "Y": 0, "E": 15})):
            triples.append(n)
    if len(triples) != 1:
        raise ConversionError("Expected exactly one original L-shaped prime line. "
                              "Custom prime lines must be restored before conversion.")
    n = triples[0]
    start = n
    approaches = (("G1", {"Z": 3}), ("G1", {"Y": 150}),
                  ("G1", {"X": 0}), ("G1", {"Z": .2}),
                  ("M83", {}), ("G90", {}), ("G92", {"E": 0}))
    while start and any(_matches(active[start - 1][1], cmd, values)
                        for cmd, values in approaches):
        start -= 1
    finish = n + 3
    if finish >= len(active) or not _matches(active[finish][1], "G92", {"E": 0}):
        raise ConversionError("The original prime line must end with G92 E0.")
    finish += 1
    if finish < len(active) and _matches(active[finish][1], "G1", {"Z": 1}):
        finish += 1
    first = active[start][0]
    last = active[finish - 1][0] + 1
    if any(cmd in ("G0", "G1", "G00", "G01") for _, (cmd, _) in active[:start]):
        raise ConversionError("An earlier G0/G1 would stop the printer's header scan. "
                              "Move that custom motion after the prime block first.")
    for index, (cmd, values) in active:
        if first <= index < last:
            continue
        if cmd in ("G0", "G1", "G00", "G01") and (values is None or values.get("E", 0) > 0):
            raise ConversionError("Another extrusion or custom motion is not part of "
                                  "the recognized L-shaped prime line.")
    extrusion_mode = next((cmd for _, (cmd, _) in reversed(active[:n])
                           if cmd in ("M82", "M83")), None)
    if extrusion_mode != "M83":
        raise ConversionError("The original L-shaped prime requires M83 relative extrusion.")
    xy_mode = next((cmd for _, (cmd, _) in reversed(active[:n])
                   if cmd in ("G90", "G91")), "G90")
    if xy_mode != "G90":
        raise ConversionError("The original prime line is under G91 relative positioning.")
    return "".join(lines[:first]), "".join(lines[first:last]), "".join(lines[last:])


def _template():
    # Orca's first-layer bounds include brim, skirt, supports and wipe tower.
    # Keep the union comments after old model-only comments, before the first G1.
    return """; KAMP
{local uncurser_prime_xmin = print_bed_min[0] + 5}
{local uncurser_prime_ymin = print_bed_min[1] + 5}
{local uncurser_prime_xmax = print_bed_max[0] - 5}
{local uncurser_prime_ymax = print_bed_max[1] - 5}
{local uncurser_prime_xstart = max(uncurser_prime_xmin, min(first_layer_print_min[0], uncurser_prime_xmax - 40))}
{local uncurser_prime_ystart = max(uncurser_prime_ymin, min(first_layer_print_min[1], uncurser_prime_ymax - 40))}
{local uncurser_prime_side = 0}
{local uncurser_prime_x = first_layer_print_min[0]}
{local uncurser_prime_y = first_layer_print_min[1]}
{local uncurser_prime_dx = 0}
{local uncurser_prime_dy = 1}
{if first_layer_print_min[0] - 10 >= uncurser_prime_xmin && first_layer_print_min[0] - 10 <= uncurser_prime_xmax && uncurser_prime_ymax - uncurser_prime_ymin >= 40}
{uncurser_prime_side = 1}
{uncurser_prime_x = first_layer_print_min[0] - 10}
{uncurser_prime_y = uncurser_prime_ystart}
{elsif first_layer_print_max[0] + 10 <= uncurser_prime_xmax && first_layer_print_max[0] + 10 >= uncurser_prime_xmin && uncurser_prime_ymax - uncurser_prime_ymin >= 40}
{uncurser_prime_side = 2}
{uncurser_prime_x = first_layer_print_max[0] + 10}
{uncurser_prime_y = uncurser_prime_ystart}
{elsif first_layer_print_min[1] - 10 >= uncurser_prime_ymin && first_layer_print_min[1] - 10 <= uncurser_prime_ymax && uncurser_prime_xmax - uncurser_prime_xmin >= 40}
{uncurser_prime_side = 3}
{uncurser_prime_x = uncurser_prime_xstart}
{uncurser_prime_y = first_layer_print_min[1] - 10}
{uncurser_prime_dx = 1}
{uncurser_prime_dy = 0}
{elsif first_layer_print_max[1] + 10 <= uncurser_prime_ymax && first_layer_print_max[1] + 10 >= uncurser_prime_ymin && uncurser_prime_xmax - uncurser_prime_xmin >= 40}
{uncurser_prime_side = 4}
{uncurser_prime_x = uncurser_prime_xstart}
{uncurser_prime_y = first_layer_print_max[1] + 10}
{uncurser_prime_dx = 1}
{uncurser_prime_dy = 0}
{endif}
{if uncurser_prime_side > 0}
; MINX = {min(first_layer_print_min[0], uncurser_prime_x)}
; MINY = {min(first_layer_print_min[1], uncurser_prime_y)}
; MAXX = {max(first_layer_print_max[0], uncurser_prime_x + 40 * uncurser_prime_dx)}
; MAXY = {max(first_layer_print_max[1], uncurser_prime_y + 40 * uncurser_prime_dy)}
G90
M83
G92 E0
G1 Z3 F600
G1 X{uncurser_prime_x} Y{uncurser_prime_y} F6000
G1 Z0.8 F600
G1 X{uncurser_prime_x + 30 * uncurser_prime_dx} Y{uncurser_prime_y + 30 * uncurser_prime_dy} E30 F360
G92 E0
G1 E-0.5 F2100
G1 X{uncurser_prime_x + 40 * uncurser_prime_dx} Y{uncurser_prime_y + 40 * uncurser_prime_dy} F6000
G92 E0
G1 Z1 F600
{else}
; MINX = {first_layer_print_min[0]}
; MINY = {first_layer_print_min[1]}
; MAXX = {first_layer_print_max[0]}
; MAXY = {first_layer_print_max[1]}
M83
G92 E0
; No safe prime-line space: prime skipped.
{endif}
"""


def _render(original, newline):
    generated = _template().replace("\n", newline)
    ending = newline if original.endswith(("\n", "\r")) else ""
    saved = "".join("; " + line for line in original.splitlines(keepends=True))
    if not ending:
        saved += newline
    return (BEGIN + newline + generated + "{if false}" + newline
            + _ORIGINAL_BEGIN + newline + saved
            + _ORIGINAL_END + newline + "{endif}" + newline + END + ending)


def _owned(text):
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == BEGIN]
    ends = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == END]
    if not starts and not ends:
        if any(marker in text for marker in ("UNCURSER ADAPTIVE PRIME", _SAVED,
                                             _ORIGINAL_BEGIN, _ORIGINAL_END)):
            raise ConversionError("The Uncurser-generated block is incomplete or edited.")
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ConversionError("Expected one complete Uncurser-generated prime block.")
    start, end = starts[0], ends[0]
    newline = lines[start][len(BEGIN):]
    if newline not in ("\n", "\r\n", "\r"):
        raise ConversionError("The Uncurser-generated block has an invalid line ending.")
    try:
        block = lines[start + 1:end]
        saved_starts = [i for i, line in enumerate(block)
                        if line.rstrip("\r\n") == _ORIGINAL_BEGIN]
        saved_ends = [i for i, line in enumerate(block)
                      if line.rstrip("\r\n") == _ORIGINAL_END]
        if saved_starts or saved_ends:
            if len(saved_starts) != 1 or len(saved_ends) != 1 or saved_starts[0] >= saved_ends[0]:
                raise ValueError
            saved_start, saved_end = saved_starts[0], saved_ends[0]
            saved_lines, prefix = block[saved_start + 1:saved_end], "; "
            boundary = saved_start - 1
            # Older output may have metadata between the false branch and saved lines.
            while boundary >= 0 and block[boundary].startswith((_DIGEST, _EOF, _GENERATED)):
                boundary -= 1
            hidden = boundary >= 0 and block[boundary].rstrip("\r\n") == "{if false}"
            if block[saved_end + 1:] != (["{endif}" + newline] if hidden else []):
                raise ValueError
        else:
            saved_start = next(i for i, line in enumerate(block) if line.startswith(_SAVED))
            saved_lines, prefix = block[saved_start:], _SAVED
        if not saved_lines or any(not line.startswith(prefix) for line in saved_lines):
            raise ValueError
        original = "".join(line[len(prefix):] for line in saved_lines)
        legacy_eof = [line[len(_EOF):].rstrip("\r\n") for line in block[:saved_start]
                      if line.startswith(_EOF)]
        if len(legacy_eof) > 1 or (legacy_eof and legacy_eof[0] not in ("0", "1")):
            raise ValueError
        unterminated = legacy_eof[0] == "1" if legacy_eof else not lines[end].endswith(("\n", "\r"))
        if unterminated:
            if not original.endswith(newline):
                raise ValueError
            original = original[:-len(newline)]
    except (ValueError, IndexError, StopIteration):
        raise ConversionError("The saved original block is incomplete or contains uncommented lines. "
                              "Restore its markers and comments before converting it.") from None
    return "".join(lines[:start]), original, "".join(lines[end + 1:])


def is_adaptive(text):
    """Identify and verify this tool's generated format."""
    return _owned(text) is not None


def to_adaptive(text):
    """Replace only a recognized classic L prime; keep all outside text exact."""
    if _owned(text) is not None:
        return text
    if any(line.strip() == "; KAMP" for line in text.splitlines()):
        raise ConversionError("Remove the existing ; KAMP marker before conversion.")
    if "uncurser_prime_" in text:
        raise ConversionError("The start code already uses Uncurser's reserved variable names.")
    head, original, tail = _original_span(text)
    if any(re.match(r"^\s*;\s*MIN[XY]\s*=|^\s*;\s*MAX[XY]\s*=", line, re.I)
           for line in tail.splitlines()):
        raise ConversionError("Print-area bounds after the prime block could override "
                              "the adaptive area. Move those comments before the prime block.")
    newline = next((match[0] for match in re.finditer(r"\r\n|\n|\r", text)), "\n")
    return head + _render(original, newline) + tail


def to_original(text):
    """Restore the exact commented original; keep edits outside the block."""
    owned = _owned(text)
    return text if owned is None else "".join(owned)


def convert(text, adaptive=True, *, slicer="orca"):
    """Convert Orca custom G-code, never a complete sliced print file."""
    if slicer.casefold() not in ("orca", "orcaslicer"):
        raise ConversionError("This converter supports OrcaSlicer machine start G-code only.")
    return to_adaptive(text) if adaptive else to_original(text)
