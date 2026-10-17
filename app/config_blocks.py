"""Small owned config additions shared by mods that edit printer.cfg."""
import re

SCREWS_CONFIG = "/usr/share/klipper/klippy/extras/screws_tilt_adjust.cfg"
LEGACY_SCREWS_CONFIG = "/mnt/UDISK/printer_data/config/screws_tilt_adjust.cfg"
LEGACY_SCREWS_INCLUDE = ("# UNCURSER BEGIN bed_screws_v1\n"
                         "[include screws_tilt_adjust.cfg]\n"
                         "# UNCURSER END bed_screws_v1\n")
SCREWS_INCLUDE = LEGACY_SCREWS_INCLUDE.replace("include screws_tilt_adjust.cfg", "include " + SCREWS_CONFIG)


def screws_include_path(text):
    for block, path in ((SCREWS_INCLUDE, SCREWS_CONFIG), (LEGACY_SCREWS_INCLUDE, LEGACY_SCREWS_CONFIG)):
        pattern = r"\r?\n".join(re.escape(line) for line in block.split("\n"))
        if re.match(pattern, text):
            strip_screws_include(text)
            return path
    strip_screws_include(text)
    return None


def strip_screws_include(text):
    match = None
    for block in (SCREWS_INCLUDE, LEGACY_SCREWS_INCLUDE):
        pattern = r"\r?\n".join(re.escape(line) for line in block.split("\n"))
        match = re.match(pattern, text)
        if match:
            break
    base = text[match.end():] if match else text
    if "UNCURSER BEGIN bed_screws" in base or "UNCURSER END bed_screws" in base:
        raise ValueError("The bed screw helper include was edited or duplicated.")
    return base, match is not None
