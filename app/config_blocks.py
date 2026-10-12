"""Small owned config additions shared by mods that edit printer.cfg."""
import re

SCREWS_INCLUDE = ("# UNCURSER BEGIN bed_screws_v1\n"
                  "[include screws_tilt_adjust.cfg]\n"
                  "# UNCURSER END bed_screws_v1\n")


def strip_screws_include(text):
    pattern = r"\r?\n".join(re.escape(line) for line in SCREWS_INCLUDE.split("\n"))
    match = re.match(pattern, text)
    base = text[match.end():] if match else text
    if "UNCURSER BEGIN bed_screws" in base or "UNCURSER END bed_screws" in base:
        raise ValueError("The bed screw helper include was edited or duplicated.")
    return base, match is not None
