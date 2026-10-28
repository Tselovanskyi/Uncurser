"""Verified K2 Plus native L-prime probing patch; binary bytes never become text."""
from . import mods, storage

TARGET = "/usr/share/klipper/klippy/extras/prtouch_v3_wrapper.cpython-39.so"
ORIGINAL = storage.original(TARGET)
MANIFEST = ORIGINAL + ".json"
BINARY = True
REFERENCE_HASH = "177f2cd0aeefb292b6c1da3d13b1d42ed2a29d30beb752f40f826345de7a870f"
ENABLED_HASH = "00349dbd72f0923f916aedf46be903a47369141be8b9e041c46acaf298b6243c"
FILE_SIZE = 1298608
# Validated for the byte-identical native module in firmware 1.1.6.4 and 1.1.7.0.
PATCHES = (
    (0x44, bytes.fromhex("b8421300b8421300"), bytes.fromhex("1043130010431300")),
    (0x47f00, bytes.fromhex("0700a0e1"), bytes.fromhex("ecb003ea")),
    (0xd3b0c, bytes.fromhex("a8309de5"), bytes.fromhex("f88101ea")),
    (0x1342b8, bytes.fromhex("00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"), bytes.fromhex("080097e5060050e31000d705fc000002e40050031800970534109f0501005001bc01d7014d1005030100500164009d05141080050700a0e1034ffceac8309de5143093e5000053e3437ffe1aa8309de5007efeea3b204b41")),
    (0x135650, bytes.fromhex("14000000"), bytes.fromhex("18000000")),
)

def validate(data):
    if not isinstance(data, bytes) or not 52 <= len(data) <= 2 * 1024 * 1024 or data[:5] != b"\x7fELF\x01":
        raise ValueError("The native probe module is not a supported 32-bit ELF file.")


def _baseline(data):
    if not isinstance(data, bytes) or len(data) != FILE_SIZE:
        return None
    result = bytearray(data)
    for offset, before, after in PATCHES:
        if data[offset:offset + len(before)] not in (before, after):
            return None
        result[offset:offset + len(before)] = before
    return bytes(result) if mods.digest(result) == REFERENCE_HASH else None


def fingerprint(data):
    baseline = _baseline(data)
    return mods.digest(baseline if baseline is not None else data)


def inspect(data, original=None):
    if not isinstance(data, bytes):
        return {"known": False, "enabled": False, "patchable": False, "present": False,
                "label": "Not installed", "reason": "The native probe module is missing."}
    digest = mods.digest(data)
    stock, enabled = digest == REFERENCE_HASH, digest == ENABLED_HASH
    signatures = [entry for entry in PATCHES if entry[0] in (0x47f00, 0xd3b0c, 0x1342b8)]
    present = enabled or any(data[offset:offset + len(after)] == after for offset, _, after in signatures)
    return {"known": stock or enabled, "enabled": True if enabled else False if stock else None,
            "patchable": stock or enabled, "present": present,
            "label": "Uncursed" if enabled else "Original" if stock else "Unknown",
            "reason": "" if stock or enabled else "The native probe module does not match the verified binary layout."}


def transform(data, desired, original=None):
    validate(data)
    digest = mods.digest(data)
    if desired:
        if digest == ENABLED_HASH:
            return data
        if digest != REFERENCE_HASH:
            raise ValueError("Only the verified original native probe module can receive this binary patch.")
        result = bytearray(data)
        for offset, before, after in PATCHES:
            if result[offset:offset + len(before)] != before:
                raise ValueError("The native probe module changed at a patch location.")
            result[offset:offset + len(before)] = after
        if mods.digest(result) != ENABLED_HASH:
            raise ValueError("The patched native probe module failed verification.")
        return bytes(result)
    result = _baseline(data)
    if result is None:
        raise ValueError("The native probe module cannot be restored using this implementation.")
    return result


def capture(baseline, enabled):
    if mods.digest(baseline) != REFERENCE_HASH or mods.digest(enabled) != ENABLED_HASH:
        raise ValueError("The binary uninstall record requires the verified original and installed module.")
    return {"size": len(baseline), "changes": [
        {"offset": offset, "before": before.hex(), "after": after.hex()}
        for offset, before, after in PATCHES]}
