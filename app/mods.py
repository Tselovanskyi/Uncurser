"""Versioned, reversible edits. No stock printer files are shipped with the app."""

import ast
import hashlib
from dataclasses import dataclass

TARGET = "/usr/share/klipper/klippy/extras/pause_resume.py"
ORIGINAL = TARGET.removesuffix(".py") + ".ptn.Original.txt"
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "a95b2348f0a65f19b9f64189cb97ea74926296db80411b3d3ce3b4626cb0ef91"
MOD_ID = "early_heat_v1"

# These blocks are additions to the existing class, not a new printer module.
HELPERS = '''    def _uncursed_job(self):
        try:
            with open("/mnt/UDISK/creality/userdata/history/print_history_record.json") as f:
                jobs = json.load(f)["list"]
                return jobs[0] if jobs else {"id": 0}
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            return {}

    def _uncursed_reset(self):
        self._uncursed_job_id = self._uncursed_job().get("id")
        self._uncursed_cancelled = True

    def _uncursed_preheat(self):
        import re, math
        job = self._uncursed_job()
        job_id = job.get("id")
        previous = self._uncursed_job_id
        if job_id is None or previous is None or job_id == previous:
            self._uncursed_job_id = job_id
            return
        try:
            root = os.path.realpath(self.v_sd.sdcard_dirname)
            path = os.path.realpath(job["filename"])
            if os.path.commonpath([root, path]) != root or not path.lower().endswith(".gcode"):
                raise ValueError("job filename is outside the G-code directory")
            bed, chamber, found = None, 0., False
            with open(path, encoding="utf-8-sig") as f:
                for line in f.read(2 * 1024 * 1024).splitlines():
                    command = line.split(";", 1)[0].strip().upper()
                    if not command:
                        continue
                    if re.match(r"^START_PRINT(?:\\s|$)", command):
                        match = re.search(r"\\bBED_TEMP=([-+0-9.E]+)(?:\\s|$)", command)
                        if match:
                            bed = float(match.group(1))
                        found = True
                        break
                    if re.match(r"^(?:G0?[0123]|G28|G29|BED_MESH_CALIBRATE)(?:\\s|[XYZEF]|$)", command):
                        raise ValueError("motion occurs before START_PRINT")
                    if re.match(r"^M(?:140|190|141|191)(?:\\s|S|R|$)", command):
                        match = re.fullmatch(r"(M140|M190|M141|M191)\\s*S\\s*([-+0-9.E]+)", command)
                        if not match:
                            raise ValueError("unsupported startup temperature command")
                        if match.group(1) in ("M140", "M190"):
                            bed = float(match.group(2))
                        else:
                            chamber = float(match.group(2))
            if not found or bed is None:
                raise ValueError("START_PRINT with a bed target was not found")
            heaters = self.printer.lookup_object("heaters")
            for name, target in (("heater_bed", bed), ("chamber_heater", chamber)):
                if not math.isfinite(target) or not 0 <= target <= heaters.lookup_heater(name).max_temp:
                    raise ValueError("invalid %s temperature" % name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise self.gcode.error("Uncurser early heating: %s" % error)
        # Consume after validation, before any yielding command.
        self._uncursed_job_id = job_id
        self._uncursed_cancelled = False
        self.gcode.respond_info("Uncurser: job %s early heat, bed=%g chamber=%g" % (job_id, bed, chamber))
        # One G-code lock covers BOTH heater starts and BOTH waits.
        commands = ["M140 S%g" % bed, "M141 S%g" % chamber]
        if bed > 0:
            commands.append("M190 S%g" % bed)
        if chamber > 0:
            commands.append("M191 S%g" % chamber)
        self.gcode.run_script("\\n".join(commands))
        if self._uncursed_cancelled or self.gcode.cancel_pending:
            raise self.gcode.error("Uncurser early heating cancelled")
        self.gcode.respond_info("Uncurser: early temperature waits finished")

'''


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate(data: bytes):
    ast.parse(data.decode("utf-8"), filename=TARGET, feature_version=(3, 9))


@dataclass(frozen=True)
class Insertion:
    name: str
    anchor: str
    body: str
    before: bool = False

    @property
    def marked(self):
        indent = "    " if self.name == "helpers" else "        "
        return (indent + "# UNCURSER BEGIN " + MOD_ID + ":" + self.name + "\n"
                + self.body + indent + "# UNCURSER END " + MOD_ID + ":" + self.name + "\n")


INSERTIONS = (
    Insertion("connect", "        self.v_sd = self.printer.lookup_object('virtual_sdcard', None)\n", "        self._uncursed_reset()\n"),
    Insertion("preheat", "    def _handle_cancel_request(self, web_request):\n", "        self._uncursed_preheat()\n", True),
    Insertion("cancel", "    def _handle_cancel_request(self, web_request):\n", "        self._uncursed_reset()\n"),
    Insertion("fast_cancel", "    def _handle_fast_response(self, web_request):\n", "        self._uncursed_reset()\n"),
    Insertion("cancel_command", "    def cmd_CANCEL_PRINT(self, gcmd):\n", "        self._uncursed_reset()\n"),
    Insertion("helpers", "    def get_status(self, eventtime):\n", HELPERS, True),
)


def strip_mod(data: bytes) -> tuple[bytes, bool]:
    text = data.decode("utf-8")
    if "UNCURSER" not in text and "_uncursed_" not in text:
        return data, False
    for ins in INSERTIONS:
        if text.count(ins.marked) != 1:
            raise ValueError("The early-heating modification is partial or has been edited. Automatic reversal is unavailable.")
        text = text.replace(ins.marked, "", 1)
    if "_uncursed_" in text or "UNCURSER" in text:
        raise ValueError("Unrecognized Uncurser code remains in this file.")
    return text.encode("utf-8"), True


def transform(data: bytes, enabled: bool) -> bytes:
    base, current = strip_mod(data)
    validate(base)
    if current == enabled:
        return data
    if not enabled:
        return base
    text = base.decode("utf-8")
    for ins in INSERTIONS:
        if text.count(ins.anchor) != 1:
            raise ValueError("Cannot locate the unique insertion point: " + ins.name)
        text = text.replace(ins.anchor, ins.marked + ins.anchor if ins.before else ins.anchor + ins.marked, 1)
    result = text.encode("utf-8")
    validate(result)
    return result


def inspect(data: bytes):
    try:
        base, enabled = strip_mod(data)
        validate(base)
        transform(data, not enabled)
        known = digest(base) == REFERENCE_HASH
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": ("Uncursed" if enabled else "Original") if known else ("Unknown + Uncursed" if enabled else "Unknown"),
                "reason": "" if known else "This file differs from the original reference captured from your printer."}
    except (ValueError, SyntaxError, UnicodeError) as e:
        return {"known": False, "enabled": None, "patchable": False, "label": "Conflict", "reason": str(e)}
