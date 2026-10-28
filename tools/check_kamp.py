"""Focused local binary/uninstall/guarded-write checks; never connects to a printer."""
import base64
import copy
import importlib.util
import os
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import kamp, mod_versions, mods, stock_recovery, storage


def rejects(action):
    try:
        action()
    except ValueError:
        return
    raise AssertionError("An unsafe binary operation was accepted.")


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build/screw-speed-readonly/prtouch_v3_wrapper.cpython-39.so"
    original = source.read_bytes()
    assert mods.digest(original) == kamp.REFERENCE_HASH
    enabled = kamp.transform(original, True)
    assert len(enabled) == len(original) and mods.digest(enabled) == kamp.ENABLED_HASH
    assert kamp.transform(enabled, False) == original
    assert kamp.inspect(enabled)["present"] and not kamp.inspect(original)["present"]
    assert storage.original(kamp.TARGET).endswith(".sbn.Original.txt")
    assert storage.recovery(kamp.TARGET, kamp.ENABLED_HASH).endswith(".sbn." + kamp.ENABLED_HASH + ".txt")

    for version in ("1.1.6.4", "1.1.7.0"):
        fixture = ROOT / "build/firmware-analysis" / version / "rootfs" / kamp.TARGET.lstrip("/")
        if fixture.exists():
            assert fixture.read_bytes() == original, version
    record = mod_versions.capture(kamp.TARGET, original, enabled, firmware="1.1.7.0")
    assert mod_versions.load(mod_versions.dump({kamp.TARGET: record}))[kamp.TARGET] == record
    operation = record["binary_undo"]
    changed = bytearray(enabled)
    changed[0x50000] ^= 1  # Outside the patch: recorded removal must preserve it.
    restored = mod_versions.undo_binary(bytes(changed), operation)
    expected = bytearray(original)
    expected[0x50000] ^= 1
    assert restored == bytes(expected)
    rejects(lambda: kamp.transform(bytes(changed), False))
    changed = bytearray(enabled)
    changed[operation["changes"][0]["offset"]] ^= 1
    rejects(lambda: mod_versions.undo_binary(bytes(changed), operation))
    malformed = copy.deepcopy(operation)
    malformed["changes"][1]["offset"] = malformed["changes"][0]["offset"]
    rejects(lambda: mod_versions.validate_binary_undo(malformed))

    # A missing ledger can recover only an exact known module or validated
    # mixture of its original/installed spans, using the guarded active ROM.
    rom = "/rom" + kamp.TARGET
    stock = {"available": True, "files": {kamp.TARGET: {
        "data": original, "source": rom, "sha256": kamp.REFERENCE_HASH}},
        "guards": {rom: kamp.REFERENCE_HASH}}
    changed_firmware = bytearray(original)
    changed_firmware[0x50000] ^= 1
    changed_firmware = bytes(changed_firmware)
    new_stock = {"available": True, "files": {kamp.TARGET: {
        "data": changed_firmware, "source": rom, "sha256": mods.digest(changed_firmware)}},
        "guards": {rom: mods.digest(changed_firmware)}}
    files = {kamp.TARGET: SimpleNamespace(data=enabled, sha256=mods.digest(enabled))}
    rejects(lambda: stock_recovery.adaptive_purge(files, new_stock, {}))
    for offset, before, after in kamp.PATCHES:
        partial = bytearray(enabled)
        partial[offset:offset + len(before)] = before
        partial = bytes(partial)
        assert kamp.transform(partial, False) == original
        files = {kamp.TARGET: SimpleNamespace(data=partial, sha256=mods.digest(partial))}
        result, _ = stock_recovery.adaptive_purge(files, stock, {})
        assert result[kamp.TARGET] == original

    # Reuse the existing project-local batch harness, including Windows-safe
    # fsync/chown stand-ins. This executes the actual maintenance transaction.
    spec = importlib.util.spec_from_file_location("storage_batch_fixture", ROOT / "tests/test_storage_batch.py")
    fixture_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture_module)
    case = fixture_module.StorageBatchTests()
    case.setUp()
    try:
        path = case.old / "probe.so"
        path.write_bytes(original)
        backup = case.storage / "originals/probe.sbn.Original.txt"
        item = {"path": str(path), "backup": str(backup), "expected": mods.digest(original),
                "data": base64.b64encode(enabled).decode(), "new_hash": mods.digest(enabled),
                "original_hash": None, "original_absent": False, "may_capture_original": True,
                "reference": kamp.REFERENCE_HASH, "restore": False, "kind": "binary",
                "binary_size": len(original), "binary_changes": operation["changes"]}
        case.run_request({"files": [item], "model": "F008", "firmware": "1.1.7.0"})
        assert path.read_bytes() == enabled and backup.read_bytes() == original
        item.update(expected=mods.digest(enabled), data=base64.b64encode(original).decode(),
                    new_hash=mods.digest(original), original_hash=mods.digest(original), may_capture_original=False,
                    binary_changes=mod_versions.binary_changes(enabled, original))
        bad = copy.deepcopy(item)
        bad["binary_changes"][0]["before"] = "ff" * (len(bad["binary_changes"][0]["before"]) // 2)
        rejects(lambda: case.run_request({"files": [bad]}))
        assert path.read_bytes() == enabled
        case.run_request({"files": [item]})
        assert path.read_bytes() == original
        first = dict(item, expected=mods.digest(original), data=base64.b64encode(enabled).decode(),
                     new_hash=mods.digest(enabled), binary_changes=operation["changes"])
        second_path = case.old / "second.so"
        second_path.write_bytes(original)
        second = dict(first, path=str(second_path), backup=str(case.storage / "originals/second.sbn.Original.txt"),
                      original_hash=None, may_capture_original=True)

        native_replace = os.replace
        # The harness patches os.replace, so keep the real implementation bound.
        def replace(source, destination):
            if destination == str(second_path):
                raise OSError("Simulated second-file replacement failure")
            return native_replace(source, destination)

        try:
            case.run_request({"files": [first, second]}, replace=replace)
        except OSError:
            pass
        else:
            raise AssertionError("The injected replacement failure was not reached.")
        assert path.read_bytes() == original and second_path.read_bytes() == original
        path.unlink()
        missing_restore = dict(item, expected=None, restore=True)
        missing_restore.pop("binary_changes")
        missing_restore.pop("binary_size")
        case.run_request({"files": [missing_restore]})
        assert path.read_bytes() == original
        deletion = dict(item, expected=mods.digest(original), data=None, new_hash=None, original_absent=True)
        rejects(lambda: case.run_request({"files": [deletion]}))
        assert path.read_bytes() == original
        case.assert_no_candidates()
    finally:
        case.tearDown()
    print("KAMP binary, uninstall, recovery and guarded-write checks passed.")


if __name__ == "__main__":
    main()
