"""Obsolete backup discovery never writes or invents firmware provenance."""
import copy
import hashlib
import json
import unittest

from app import mods, screws, storage


def file(data, **attrs):
    return dict(data=data, sha256=hashlib.sha256(data).hexdigest(), mode=0o444, uid=0, gid=0, **attrs)


def pair(target=mods.TARGET, *, firmware="1.1.6.4", model="F008", absent=False, canonical=False):
    path = storage.original(target)
    if not canonical:
        path = storage.legacy_path(path)
    data = b"" if absent else b"verified original\n"
    manifest = dict(path=target, sha256=hashlib.sha256(data).hexdigest(), absent=absent,
                    mode=0o644, uid=0, gid=0, model=model, firmware=firmware)
    return {path: file(data), path + ".json": file(json.dumps(manifest).encode())}


class Remote:
    def __init__(self, files):
        self.files = copy.deepcopy(files)

    def read(self, path):
        return copy.deepcopy(self.files.get(path))


class ObsoleteStorageTests(unittest.TestCase):
    def discover(self, files, targets=(mods.TARGET,)):
        return storage.obsolete_originals(storage.Reader(Remote(files)), targets, ("F008", "1.1.7.0"))

    def test_verified_older_pair_uses_physical_paths_hashes_and_backup_attributes(self):
        files = pair()
        result = self.discover(files)
        self.assertEqual(len(result), 1)
        plan = result[0]
        self.assertEqual(plan["target"], mods.TARGET)
        self.assertEqual(plan["backup"], storage.legacy_path(mods.ORIGINAL))
        self.assertEqual((plan["model"], plan["firmware"]), ("F008", "1.1.6.4"))
        self.assertEqual(plan["reason"], "obsolete")
        self.assertEqual({item["path"] for item in plan["files"]}, set(files))
        for item in plan["files"]:
            self.assertEqual(item["sha256"], files[item["path"]]["sha256"])
            self.assertEqual(item["attrs"], {"mode": 0o444, "uid": 0, "gid": 0})

    def test_unknown_same_newer_or_wrong_model_pairs_are_not_obsolete(self):
        for version in ("1.1.7.0", "1.1.10.0"):
            with self.subTest(version=version):
                self.assertEqual(self.discover(pair(firmware=version)), ())
        self.assertEqual(self.discover(pair(model="F009")), ())
        for version in (None, "", "unknown"):
            with self.subTest(version=version):
                self.assertEqual(self.discover(pair(firmware=version))[0]["reason"], "metadata")
        self.assertEqual(self.discover(pair(firmware="DWIN sw ver:1.1.6.4;"))[0]["firmware"], "1.1.6.4")

    def test_incomplete_or_invalid_metadata_is_classified_using_actual_file_proof(self):
        for fault in ("backup missing", "manifest missing", "bad hash", "wrong path", "bad absent", "absent nonempty",
                      "metadata attrs", "file attrs", "malformed"):
            with self.subTest(fault=fault):
                files = pair()
                backup = storage.legacy_path(mods.ORIGINAL)
                meta = json.loads(files[backup + ".json"]["data"])
                if fault == "backup missing":
                    files.pop(backup)
                elif fault == "manifest missing":
                    files.pop(backup + ".json")
                elif fault == "bad hash":
                    files[backup]["sha256"] = "0" * 64
                elif fault == "file attrs":
                    files[backup].pop("gid")
                elif fault == "malformed":
                    files[backup + ".json"] = file(b"{invalid")
                else:
                    if fault == "wrong path": meta["path"] = screws.PYTHON
                    if fault == "bad absent": meta["absent"] = 0
                    if fault == "absent nonempty": meta["absent"] = True
                    if fault == "metadata attrs": meta["mode"] = True
                    files[backup + ".json"] = file(json.dumps(meta).encode())
                plans = self.discover(files)
                if fault in ("bad hash", "file attrs"):
                    self.assertEqual(plans, ())
                else:
                    self.assertEqual(len(plans), 1)
                    self.assertEqual(plans[0]["reason"], "metadata")
                    self.assertEqual(len(plans[0]["files"]), 2)
                    for entry in plans[0]["files"]:
                        if entry["path"] not in files:
                            self.assertIsNone(entry["sha256"])
                            self.assertIsNone(entry["attrs"])

    def test_manifest_checksum_failure_uses_actual_hash_and_attributes(self):
        for firmware in ("1.1.7.0", None):
            with self.subTest(firmware=firmware):
                files = pair(firmware=firmware)
                backup = storage.legacy_path(mods.ORIGINAL)
                meta = json.loads(files[backup + ".json"]["data"])
                meta["sha256"] = "0" * 64
                files[backup + ".json"] = file(json.dumps(meta).encode())
                plans = self.discover(files)
                self.assertEqual(plans[0]["reason"], "checksum" if firmware else "metadata")
                self.assertEqual(plans[0]["files"][0]["sha256"], files[backup]["sha256"])
                self.assertNotEqual(plans[0]["files"][0]["sha256"], meta["sha256"])

    def test_identical_duplicates_each_get_a_physical_pair_plan(self):
        files = {**pair(), **pair(canonical=True)}
        result = self.discover(files)
        self.assertEqual(len(result), 2)
        self.assertEqual({item["backup"] for item in result}, {mods.ORIGINAL, storage.legacy_path(mods.ORIGINAL)})
        files[mods.ORIGINAL] = file(b"different baseline\n")
        self.assertEqual(self.discover(files), ())

    def test_partial_copy_with_intact_current_firmware_copy_uses_relocation(self):
        files = {**pair(firmware="1.1.7.0"), **pair(firmware="1.1.7.0", canonical=True)}
        files.pop(mods.MANIFEST)
        self.assertEqual(self.discover(files), ())

    def test_legacy_manifest_without_absence_field_still_identifies_an_old_original(self):
        files = pair()
        path = storage.legacy_path(mods.MANIFEST)
        meta = json.loads(files[path]["data"])
        meta.pop("absent")
        files[path] = file(json.dumps(meta).encode())
        self.assertEqual(self.discover(files)[0]["reason"], "obsolete")

    def test_helper_absence_proofs_include_legacy_scope_but_ledger_is_excluded(self):
        files = {}
        targets = (screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG, storage.RECORDS, "/tmp/unrelated.cfg")
        for target in targets:
            files.update(pair(target, absent=True))
        self.assertEqual({item["target"] for item in self.discover(files, targets)},
                         {screws.PYTHON, screws.CONFIG, screws.LEGACY_CONFIG})

    def test_discovery_never_changes_moves_or_data(self):
        files = pair()
        raw = Remote(files)
        reader = storage.Reader(raw)
        reader.read(mods.ORIGINAL)
        before_files, before_moves = copy.deepcopy(raw.files), copy.deepcopy(reader.moves)
        self.assertTrue(storage.obsolete_originals(reader, (mods.TARGET, mods.TARGET), ("F008", "1.1.7.0")))
        self.assertEqual(raw.files, before_files)
        self.assertEqual(reader.moves, before_moves)
        self.assertEqual(storage.obsolete_originals(reader, (mods.TARGET,), ("F008", "unknown")), ())


if __name__ == "__main__":
    unittest.main()
