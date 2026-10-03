"""Printer preferences and trusted SSH keys in one portable file."""

import base64
import json
import os
from pathlib import Path
import tempfile

import paramiko


FILENAME = "Uncurser_settings"


def load(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError):
        raise ValueError("Uncurser_settings is damaged; it has not been overwritten.") from None
    if not isinstance(value, dict):
        raise ValueError("Uncurser_settings must contain a settings object.")
    return value


def update(path: Path, **changes):
    value = load(path)
    value.update(changes)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".pending", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def host_keys(value):
    keys = paramiko.HostKeys()
    for hostname, records in value.get("ssh_host_keys", {}).items():
        for key_type, encoded in records.items():
            keys.add(hostname, key_type, paramiko.PKey.from_type_string(key_type, base64.b64decode(encoded, validate=True)))
    return keys


def encode_keys(keys):
    return {host: {kind: key.get_base64() for kind, key in records.items()} for host, records in keys.items()}


def migrate(path: Path):
    if path.exists():
        return
    legacy = path.parent / "data"
    if not (legacy / "settings.json").exists() and not (legacy / "known_hosts").exists():
        return
    value = load(legacy / "settings.json")
    keys = paramiko.HostKeys()
    if (legacy / "known_hosts").exists():
        keys.load(str(legacy / "known_hosts"))
    value["ssh_host_keys"] = encode_keys(keys)
    update(path, **value)
