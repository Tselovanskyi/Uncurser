"""Allow half-power chamber heating while the bed performs its initial warmup."""
import ast

from . import mod_comments, mods, storage

TARGET = "/usr/share/klipper/klippy/extras/heaters.py"
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
REFERENCE_HASH = "f514c45d7bfb1b5348524a0e627323164df4773b8d33930a491c4e3646db93fb"
_CONDITION = ast.dump(ast.parse("is_chamber_heater and heater_bed_state == 1", mode="eval").body)


def _anchor(data):
    text = data.decode("utf-8-sig")
    tree = ast.parse(text, filename=TARGET, feature_version=(3, 9))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ControlBangBang"]
    methods = [node for owner in classes for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == "temperature_update"]
    branches = [node for method in methods for node in ast.walk(method)
                if isinstance(node, ast.If) and ast.dump(node.test) == _CONDITION]
    if len(classes) != 1 or len(methods) != 1 or len(branches) != 1:
        raise ValueError("Expected one native chamber heating gate in ControlBangBang.temperature_update.")
    body = branches[0].body
    if (len(body) != 2 or any(not isinstance(node, ast.Assign) or len(node.targets) != 1 for node in body)
            or ast.dump(body[0].targets[0]) != ast.dump(ast.parse("self.temp_coff = 0").body[0].targets[0])
            or ast.dump(body[1]) != ast.dump(ast.parse("self.heater.start_heating_seconds = 0").body[0])
            or not isinstance(body[0].value, ast.Constant)
            or type(body[0].value.value) not in (int, float) or body[0].value.value not in (0, 0.5)):
        raise ValueError("The native chamber heating gate has changed; no automatic edit is safe.")
    return body[0].value


def _replace(data, value):
    node = _anchor(data)
    bom = b"\xef\xbb\xbf" if data.startswith(b"\xef\xbb\xbf") else b""
    lines = data[len(bom):].splitlines(True)
    if node.lineno != node.end_lineno:
        raise ValueError("The chamber heating coefficient must be one complete value.")
    line = lines[node.lineno - 1]
    lines[node.lineno - 1] = line[:node.col_offset] + value + line[node.end_col_offset:]
    return bom + b"".join(lines)


def validate(data):
    mod_comments.read(data)
    _anchor(data)


def fingerprint(data):
    return mods.digest(_replace(mod_comments.strip(data), b"0").replace(b"\r\n", b"\n"))


def inspect(data, original=None):
    try:
        validate(data)
        enabled = _anchor(data).value == 0.5
        reference = fingerprint(original) if original is not None else REFERENCE_HASH
        known = fingerprint(data) == reference
        return {"known": known, "enabled": enabled, "patchable": True,
                "label": "Uncursed" if known and enabled else "Original" if known else "Unknown",
                "reason": "" if known else "heaters.py differs from the recognized original outside this mod."}
    except (ValueError, SyntaxError, UnicodeError) as error:
        return {"known": False, "enabled": None, "patchable": False,
                "label": "Conflict", "reason": str(error)}


def transform(data, enabled, original=None):
    validate(data)
    if not enabled and original is not None and _anchor(original).value != 0:
        raise ValueError("The original chamber heating gate is not the native zero-power value.")
    result = _replace(data, b"0.5" if enabled else b"0")
    if fingerprint(result) != fingerprint(data):
        raise ValueError("An unrelated part of the heater controller changed.")
    return result
