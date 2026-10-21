"""Per-mod source comments; metadata is data only and never authorizes edits."""
import json
import re

from . import storage

_BOM = b"\xef\xbb\xbf"
_PREFIX = re.compile(rb"^[ \t]*(?:#|//) UNCURSER(?:[ \t]+(.*))?$")
_CODING = re.compile(rb"^[ \t]*#.*?coding[:=][ \t]*[-\w.]+")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate Uncurser comment metadata.")
        result[key] = value
    return result


def _validate(value):
    if not isinstance(value, dict) or set(value) != {"mod", "version", "firmware"}:
        raise ValueError("Invalid Uncurser comment metadata.")
    storage._absolute(value["mod"])
    if (any(ord(char) < 32 or ord(char) == 127 for char in value["mod"])
            or not isinstance(value["version"], str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", value["version"])
            or not isinstance(value["firmware"], str)
            or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){3}", value["firmware"])):
        raise ValueError("Invalid Uncurser comment metadata.")
    return value


def _scan(data):
    if not isinstance(data, bytes):
        raise ValueError("Printer source must be bytes.")
    content = data[len(_BOM):] if data.startswith(_BOM) else data
    result = {}
    for index, line in enumerate(content.splitlines(True)):
        match = _PREFIX.fullmatch(line.rstrip(b"\r\n"))
        if match is None:
            continue
        payload = match[1] or b""
        # Existing code-region delimiters belong to their original mods.
        if payload.startswith((b"BEGIN ", b"END ")):
            continue
        try:
            value = _validate(json.loads(payload, object_pairs_hook=_unique))
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid Uncurser comment metadata.") from error
        owner = value["mod"]
        if owner in result:
            raise ValueError("Duplicate Uncurser comment for: " + owner)
        result[owner] = (index, line, value)
    return result


def read(data):
    return {owner: {key: value[key] for key in ("version", "firmware")}
            for owner, (_, _, value) in _scan(data).items()}


def lines(data):
    return {owner: line for owner, (_, line, _) in _scan(data).items()}


def strip(data):
    records = _scan(data)
    bom = _BOM if data.startswith(_BOM) else b""
    owned = {index for index, _, _ in records.values()}
    return bom + b"".join(line for index, line in enumerate(data[len(bom):].splitlines(True))
                           if index not in owned)


def remove(data, modid):
    records = _scan(data)
    if modid not in records:
        return data
    bom = _BOM if data.startswith(_BOM) else b""
    source = data[len(bom):].splitlines(True)
    del source[records[modid][0]]
    return bom + b"".join(source)


def put(data, modid, version, firmware, path):
    value = _validate({"mod": modid, "version": version, "firmware": firmware})
    records = _scan(data)
    suffix = path.rsplit(".", 1)[-1].lower()
    if suffix not in ("py", "cfg", "js"):
        raise ValueError("Unsupported source comment format: " + path)
    bom = _BOM if data.startswith(_BOM) else b""
    source = data[len(bom):].splitlines(True)
    newline = b"\r\n" if b"\r\n" in data else b"\n"
    prefix = b"//" if suffix == "js" else b"#"
    header = prefix + b" UNCURSER " + json.dumps(value, separators=(",", ":")).encode() + newline
    if modid in records:
        index, old, _ = records[modid]
        ending = b"\r\n" if old.endswith(b"\r\n") else b"\n" if old.endswith(b"\n") else b""
        source[index] = header.rstrip(b"\r\n") + ending
    else:
        index = 1 if source and source[0].startswith(b"#!") else 0
        if suffix == "py":
            for offset, line in enumerate(source[:2]):
                if _CODING.match(line):
                    index = max(index, offset + 1)
        if index and not source[index - 1].endswith((b"\n", b"\r")):
            raise ValueError("An unterminated Python header cannot safely receive metadata.")
        source.insert(index, header)
    return bom + b"".join(source)
