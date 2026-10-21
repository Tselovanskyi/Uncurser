"""Update the shipped condition-only mod using its saved ownership, offline."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from app import engine, mesh, mesh_temperature as mod, mod_comments, mod_versions, mods
from tests import test_mesh


FIRMWARE = Path(__file__).resolve().parents[1] / "build/firmware-analysis"


def snapshot(firmware, revision=None, *, mesh_enabled=True):
    path = FIRMWARE / firmware / "rootfs/usr/share/klipper/config/F008_CR0CN240319C13/gcode_macro.cfg"
    original = path.read_bytes()
    if not original.endswith(b"\n"):
        original += b"\n"
    lines, index = mod.condition(original)
    lines[index] = lines[index].replace(mod.ORIGINAL_CONDITION, mod.ENABLED_CONDITION)
    legacy = "".join(lines).encode()
    note = (b"#*# <---------------------- SAVE_CONFIG ---------------------->\n"
            b"#*# [bed_mesh default]\n#*# points = 0.2, 0.3\n")
    current = legacy + note
    analysis = {"known": True, "enabled": False, "patchable": True, "label": "Original"}
    bed = mesh.transform(test_mesh.SOURCE, mesh_enabled, test_mesh.SOURCE)
    bed_snapshot = engine.MeshSnapshot(bed, mods.digest(bed), mesh.inspect(bed, test_mesh.SOURCE),
        test_mesh.SOURCE, mods.digest(test_mesh.SOURCE), original_identity=("F008", firmware))
    temperature = engine.MeshSnapshot(current, mods.digest(current), mod.inspect(current, original),
        original, mods.digest(original), original_identity=("F008", firmware))
    # Current generated calibration is unrelated to the recorded change.
    status = {"native": {"model": "F008", "modelVersion": firmware}}
    value = engine.Snapshot(b"", mods.digest(b""), analysis, None, None, "", status, "", {}, 0,
                            bed_snapshot, temperature)
    records = {}
    if revision is not None:
        record = mod_versions.capture(mod.TARGET, original, legacy, firmware=firmware)
        record["version"] = revision
        records[mod.TARGET] = record
        if mesh_enabled:
            records[mesh.TARGET] = mod_versions.capture(mesh.TARGET, test_mesh.SOURCE, bed, firmware=firmware)
        ledger = mod_versions.dump(records)
        value.versions = engine.MeshSnapshot(ledger, mods.digest(ledger),
            {"known": True, "enabled": True, "patchable": True},
            original_hash=mods.digest(b""), original_absent=True)
    return value, records, note


class MeshTemperatureUpdateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(mesh, "REFERENCE_HASH", mesh.fingerprint(test_mesh.SOURCE)))

    def test_previous_revisions_update_and_uninstall_without_changing_other_records(self):
        for firmware in ("1.1.6.4", "1.1.7.0"):
            for revision in ("0.1.5",):
                with self.subTest(firmware=firmware, revision=revision):
                    current, old_records, note = snapshot(firmware, revision)
                    self.assertTrue(engine.update_statuses(current)[mod.TARGET]["available"])
                    pending = engine.prepare_all(current, None, update=True)
                    self.assertEqual(set(pending), {mod.TARGET, mod_versions.TARGET})
                    records = mod_versions.load(pending[mod_versions.TARGET])
                    self.assertEqual(records[mesh.TARGET], old_records[mesh.TARGET])
                    self.assertEqual(records[mod.TARGET]["version"], mod_versions.VERSIONS[mod.TARGET])
                    self.assertEqual(records[mod.TARGET]["firmware"], firmware)
                    self.assertEqual(mod_comments.read(pending[mod.TARGET])[mod.TARGET]["version"], mod_versions.VERSIONS[mod.TARGET])
                    self.assertEqual(mod_versions.undo(pending[mod.TARGET], records[mod.TARGET]),
                                     current.mesh_temperature.original + note)
                    updated = copy.deepcopy(current)
                    updated.mesh_temperature.data = pending[mod.TARGET]
                    updated.mesh_temperature.sha256 = mods.digest(pending[mod.TARGET])
                    updated.versions.data = pending[mod_versions.TARGET]
                    updated.versions.sha256 = mods.digest(updated.versions.data)
                    removed = engine.prepare_all(updated, None, temperature_desired=False)
                    self.assertEqual(removed[mod.TARGET], current.mesh_temperature.original + note)
                    self.assertEqual(mod_versions.load(removed[mod_versions.TARGET]), {mesh.TARGET: old_records[mesh.TARGET]})

    def test_recordless_enrollment_installs_fixed_code_instead_of_restamping_legacy(self):
        for firmware in ("1.1.6.4", "1.1.7.0"):
            with self.subTest(firmware=firmware):
                current, _, _ = snapshot(firmware, mesh_enabled=False)
                pending = engine.prepare_all(current, None, mesh_desired=True)
                state = mod.inspect(pending[mod.TARGET], current.mesh_temperature.original)
                self.assertTrue(state["enabled"])
                self.assertFalse(state.get("legacy", False))
                self.assertIn(mod.ENABLED_INITIALIZER.encode(), pending[mod.TARGET])
                records = mod_versions.load(pending[mod_versions.TARGET])
                self.assertEqual(records[mod.TARGET]["version"], mod_versions.VERSIONS[mod.TARGET])

    def test_owned_code_conflicts_block_update(self):
        current, _, _ = snapshot("1.1.7.0", "0.1.5")
        current.mesh_temperature.data = current.mesh_temperature.data.replace(
            mod.ENABLED_CONDITION.encode(), b"{% if 'BED_TEMP' in params|upper and 1 %}")
        current.mesh_temperature.sha256 = mods.digest(current.mesh_temperature.data)
        status = engine.update_statuses(current)[mod.TARGET]
        self.assertTrue(status["needed"])
        self.assertFalse(status["available"])


if __name__ == "__main__":
    unittest.main()
