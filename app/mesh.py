"""Bed-mesh preset: edit complete values in specific sections, preserving layout."""
import configparser
import hashlib
import re
from decimal import Decimal

TARGET = "/mnt/UDISK/printer_data/config/printer.cfg"
ORIGINAL = TARGET.removesuffix(".cfg") + ".cfn.Original.txt"
MANIFEST = ORIGINAL + ".json"
# Captured read-only from the owner's printer; excludes preset values and autosave.
REFERENCE_HASH = "132af1084f7f07ae4c3ce6b3b9fecad8efd48c26a70735b63a464d7aa9ede5f4"
# Recognize the previous preset's 3mm travel height without modifying it.
LEGACY_REFERENCE_HASH = "76c11afe5c8fb08e49c8c4c499aff80ad03596dc5632b7e4113b19fca273c1c5"
VALUES = {
    ("bed_mesh", "speed"): "700",
    ("bed_mesh", "probe_count"): "9,9",
    ("printer", "max_z_velocity"): "50",
    ("prtouch_v3", "lift_speed"): "50",
    ("z_align", "distance_ratio"): "0.9",
    ("z_align", "quick_speed"): "50",
}
AUTOSAVE = "#*# <---------------------- SAVE_CONFIG ---------------------->"
HEADER = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?$")
OPTION = re.compile(r"(?P<prefix>[ \t]*(?P<key>[a-zA-Z_][a-zA-Z_0-9]*)[ \t]*[:=][ \t]*)"
                    r"(?P<value>[^#;\r\n]*?)(?P<suffix>[ \t]*(?:[#;][^\r\n]*)?)(?P<eol>\r?\n|$)")
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


def split(data):
    text = data.decode("utf-8")
    lines = text.splitlines(True)
    markers = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == AUTOSAVE]
    if len(markers) > 1:
        raise ValueError("Multiple SAVE_CONFIG blocks require review.")
    index = markers[0] if markers else len(lines)
    if any(line.lstrip().startswith("#*#") for line in lines[:index]):
        raise ValueError("Unrecognized generated configuration block.")
    if any(line.strip() and not line.startswith("#*#") for line in lines[index:]):
        raise ValueError("Unexpected content after SAVE_CONFIG; no automatic edit is safe.")
    return "".join(lines[:index]), "".join(lines[index:])


def parser(text):
    result = configparser.RawConfigParser(interpolation=None, strict=True,
                                         inline_comment_prefixes=("#", ";"))
    try:
        result.read_string(text)
    except configparser.Error as error:
        raise ValueError("Ambiguous or invalid configuration: " + str(error)) from error
    if result.defaults():
        raise ValueError("Inherited DEFAULT settings are not supported.")
    return result


def generated_parser(text):
    warning = "#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated."
    return parser("\n".join(line[4:] for line in text.splitlines()[1:]
                            if line.startswith("#*# ") and line != warning))


def numeric(key, value):
    if value is None:
        return None
    if key == ("bed_mesh", "probe_count"):
        if not re.fullmatch(r"\s*\d+\s*,\s*\d+\s*", value):
            raise ValueError("probe_count must contain exactly two integers.")
        result = tuple(int(item.strip()) for item in value.split(","))
        if min(result) < 3:
            raise ValueError("probe_count must be at least 3 on both axes.")
        return result
    if not NUMBER.fullmatch(value.strip()):
        raise ValueError(f"[{key[0]}] {key[1]} must be one complete numeric value.")
    result = Decimal(value.strip())
    if not result.is_finite() or result <= 0:
        raise ValueError(f"[{key[0]}] {key[1]} must be finite and positive.")
    if key[1] == "distance_ratio" and result >= 1:
        raise ValueError("distance_ratio must be below 1.")
    if key[1] == "quick_speed" and result != result.to_integral_value():
        raise ValueError("quick_speed must be an integer.")
    return result


class Document:
    def __init__(self, data):
        self.regular, self.generated = split(data)
        self.config = parser(self.regular)
        self.lines = self.regular.splitlines(True)
        self.options, self.sections = {}, {}
        section = None
        for index, line in enumerate(self.lines):
            header = HEADER.fullmatch(line.rstrip("\r\n"))
            if header:
                section = header[1]
                self.sections[section] = index
            option = OPTION.fullmatch(line)
            if option and (section, option["key"].lower()) in VALUES:
                key = (section, option["key"].lower())
                if key in self.options:
                    raise ValueError(f"Duplicate setting: [{section}] {key[1]}")
                self.options[key] = (index, option)
        self.raw, self.values = {}, {}
        for key in VALUES:
            section, option = key
            if section not in self.sections:
                raise ValueError(f"Required section [{section}] was not found.")
            value = self.config.get(section, option, fallback=None)
            if value is None and key != ("prtouch_v3", "lift_speed"):
                raise ValueError(f"Required setting [{section}] {option} was not found.")
            if value is not None and (key not in self.options or value.strip() != self.options[key][1]["value"].strip()):
                raise ValueError(f"Multiline or ambiguous setting: [{section}] {option}")
            self.raw[key], self.values[key] = value, numeric(key, value)
        generated_config = generated_parser(self.generated)
        for section, option in VALUES:
            if generated_config.has_option(section, option):
                raise ValueError(f"[{section}] {option} also occurs in SAVE_CONFIG; automatic editing is blocked.")

    def edit(self, changes):
        lines = self.lines.copy()
        additions = {}
        newline = "\r\n" if "\r\n" in self.regular else "\n"
        for key, value in changes.items():
            if key in self.options:
                index, match = self.options[key]
                lines[index] = (match["prefix"] + value + match["suffix"] + match["eol"]) if value is not None else ""
            elif value is not None:
                index = self.sections[key[0]]
                additions.setdefault(index, []).append(key[1] + ": " + value + newline)
        for index in sorted(additions, reverse=True):
            if not lines[index].endswith("\n"):
                lines[index] += newline
            lines[index + 1:index + 1] = additions[index]
        return ("".join(lines) + self.generated).encode("utf-8")


def validate(data):
    Document(data)


def stable_digest(data):
    regular, _ = split(data)
    return hashlib.sha256((regular.replace("\r\n", "\n").rstrip() + "\n").encode()).hexdigest()


def fingerprint(data):
    doc = Document(data)
    indices = {index for index, match in doc.options.values()}
    text = "".join(line for index, line in enumerate(doc.lines) if index not in indices)
    return hashlib.sha256((text.replace("\r\n", "\n").rstrip() + "\n").encode()).hexdigest()


def inspect(data, original=None):
    try:
        doc = Document(data)
        applied = all(doc.values[key] == numeric(key, value) for key, value in VALUES.items())
        original_values = Document(original).values if original is not None else None
        partial = original_values is not None and doc.values != original_values and not applied
        known = fingerprint(data) in (REFERENCE_HASH, LEGACY_REFERENCE_HASH)
        return {"known": known, "enabled": None if partial else applied, "patchable": True,
                "label": ("Partial" if partial else "Uncursed" if applied else "Original") if known else "Unknown",
                "reason": "" if known else "printer.cfg differs from the captured reference outside the mesh preset settings."}
    except (ValueError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False, "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    doc = Document(data)
    if enabled:
        changes = VALUES
    else:
        if original is None:
            raise ValueError("The initial printer.cfg backup is needed to restore its original settings.")
        changes = Document(original).raw
    result = doc.edit(changes)
    verified = Document(result)
    if any(verified.values[key] != numeric(key, value) for key, value in changes.items()):
        raise ValueError("Prepared mesh values do not exactly match the requested values.")
    if verified.generated != doc.generated or fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated setting or generated calibration was changed.")
    return result


def restore(data, original):
    # A full config restoration still retains current generated calibration data.
    regular, _ = split(original)
    _, generated = split(data)
    result = (regular + generated).encode("utf-8")
    validate(result)
    return result


def validate_includes(includes):
    for path, data in includes.items():
        regular, generated = split(data)
        config = parser(regular)
        saved = generated_parser(generated)
        for section, key in VALUES:
            if config.has_option(section, key) or saved.has_option(section, key):
                raise ValueError(f"[{section}] {key} is also defined in included file {path}.")
