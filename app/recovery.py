"""Narrow startup repair when newer firmware deletes the helper config."""
import base64
import posixpath
import re

from . import config_blocks, mesh, mods, network, screws, storage
from .remote import APPLY_BATCH, Remote


def supports_startup_repair(status):
    return (network.firmware_state(status) != "unsupported"
            and tuple(map(int, network.device_identity(status)[1].split("."))) > (1, 1, 6, 4))


def inspect(status, root_file_read, legacy_cfg_read, current_cfg_read):
    if not supports_startup_repair(status):
        return None
    info = status.get("info", {})
    message = info.get("state_message", "")
    match = re.fullmatch(r"Include file '([^'\r\n]+)' does not exist", message.splitlines()[0] if message else "")
    paths = (screws.LEGACY_CONFIG, screws.CONFIG)
    if info.get("state") != "error" or not match or posixpath.normpath(match[1]) not in paths:
        return None
    missing = posixpath.normpath(match[1])
    result = {"available": False, "message": "", "missingpath": missing}
    try:
        if posixpath.normpath(info.get("config_file", "")) != mesh.TARGET:
            raise ValueError("The active printer configuration path could not be verified.")
        objects = status.get("objects", {})
        if (status.get("native", {}).get("deviceState") != 0
                or objects.get("print_stats", {}).get("state") in ("printing", "paused")
                or objects.get("idle_timeout", {}).get("state") == "Printing"
                or objects.get("toolhead", {}).get("G29_flag")):
            raise ValueError("Wait until the printer has stopped before repairing startup.")
        if dict(zip(paths, (legacy_cfg_read, current_cfg_read)))[missing] is not None:
            raise ValueError("The reported missing helper config is now present. Scan again.")
        if not root_file_read or not isinstance(root_file_read.get("data"), bytes):
            raise ValueError("The active printer configuration could not be read safely.")
        before = root_file_read["data"]
        if mods.digest(before) != root_file_read.get("sha256"):
            raise ValueError("The printer configuration checksum is invalid.")
        attrs = {key: root_file_read.get(key) for key in ("mode", "uid", "gid")}
        if any(type(value) is not int or value < 0 for value in attrs.values()) or attrs["mode"] > 0o7777:
            raise ValueError("The printer configuration attributes could not be verified.")
        regular, generated = mesh.split(before)
        remaining, enabled = config_blocks.strip_screws_include(regular)
        block = config_blocks.LEGACY_SCREWS_INCLUDE if missing == screws.LEGACY_CONFIG else config_blocks.SCREWS_INCLUDE
        prefix = regular[:len(regular) - len(remaining)]
        if not enabled or prefix.replace("\r\n", "\n") != block:
            raise ValueError("The missing include is not an unchanged Uncurser helper block.")
        for line in remaining.splitlines():
            include = re.fullmatch(r"\s*\[include ([^\]]+)\]\s*(?:[#;].*)?", line)
            if include and posixpath.normpath(posixpath.join(posixpath.dirname(mesh.TARGET), include[1].strip())) in paths:
                raise ValueError("Another helper include needs review before startup can be repaired.")
        mesh.parser(remaining)
        after = (remaining + generated).encode("utf-8")
        if before[len(prefix.encode("utf-8")):] != after:
            raise ValueError("Startup repair would change unrelated configuration data.")
        result.update(available=True, message="The firmware update removed a mod's file.",
                      sha256=root_file_read["sha256"], before=before, after=after, attrs=attrs)
    except (ValueError, TypeError, UnicodeError) as error:
        result["message"] = str(error)
    return result


def repair(connection, known_hosts, scanned_recovery, progress=lambda message: None):
    if not scanned_recovery or not scanned_recovery.get("available"):
        raise ValueError("Scan the printer and review the startup repair first.")
    progress("Checking the startup error and current configuration…")
    with Remote(connection, known_hosts) as remote:
        root = remote.read(mesh.TARGET)
        legacy = remote.read(screws.LEGACY_CONFIG)
        current = remote.read(screws.CONFIG)
        fresh = inspect(network.status(connection.host), root, legacy, current)
        if not fresh or not fresh["available"]:
            raise ValueError(fresh["message"] if fresh else "The reported startup error changed. Scan again.")
        if any(fresh[key] != scanned_recovery.get(key) for key in ("sha256", "before", "after", "missingpath", "attrs")):
            raise ValueError("Printer files changed after the last scan. Scan again before repairing startup.")
        progress("Saving a recovery copy and removing the missing helper include…")
        prefix = fresh["before"][:len(fresh["before"]) - len(fresh["after"])]
        remote.python(APPLY_BATCH, {"files": [{"path": mesh.TARGET, "kind": "config", "recovery": True,
            "backup": storage.ROOT + "/recovery/printer." + fresh["sha256"] + ".txt",
            "expected": fresh["sha256"], "expected_attrs": fresh["attrs"],
            "data": base64.b64encode(fresh["after"]).decode(), "new_hash": mods.digest(fresh["after"]),
            "removed_prefix": base64.b64encode(prefix).decode()}], "guards": {fresh["missingpath"]: None}})
        saved = remote.read(mesh.TARGET)
        if not saved or saved["data"] != fresh["after"] or any(saved[key] != fresh["attrs"][key] for key in fresh["attrs"]):
            raise RuntimeError("Could not verify startup repair. Scan the printer before restarting it.")
    return {"message": "Startup repair saved. Physically power cycle the printer. The bed screw helper is disabled; other mods have not been changed.",
            "written": True, "power_cycle": True}
