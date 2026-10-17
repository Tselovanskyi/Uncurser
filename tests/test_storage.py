"""Read-only storage discovery preserves originals and rejects ambiguity."""
import copy
import hashlib
import unittest

from app import fluidd_screws, mesh, mesh_temperature, mod_versions, mods, screws, smoothing, storage


def file(data=b"immutable original\n", **attrs):
    return dict(data=data, sha256=hashlib.sha256(data).hexdigest(), **attrs)


class Remote:
    def __init__(self, files):
        self.files = copy.deepcopy(files)
        self.reads = []
        self.prefetched = []

    def read(self, path):
        self.reads.append(path)
        return copy.deepcopy(self.files.get(path))

    def prefetch(self, paths):
        self.prefetched = list(paths)

    def identity(self):
        return {"model": "F008"}


class StorageTests(unittest.TestCase):
    def test_original_paths_mirror_full_working_paths_with_safe_markers(self):
        for target, marker in ((mods.TARGET, ".ptn"), (mesh.TARGET, ".cfn"),
                               (fluidd_screws.TARGET, ".jfn")):
            stem = target.rsplit(".", 1)[0]
            previous = stem + marker + ".Original.txt"
            canonical = storage.ROOT + "/originals" + previous
            self.assertEqual(storage.original(target), canonical)
            self.assertEqual(storage.legacy_path(canonical), previous)
            self.assertEqual(storage.legacy_path(canonical + ".json"), previous + ".json")
            self.assertIsNone(storage.legacy_path(target))
        self.assertNotEqual(storage.original("/a/one.cfg"), storage.original("/b/one.cfg"))

    def test_records_keep_old_baseline_manifest_path_alias(self):
        old = storage.LEGACY_RECORDS.removesuffix(".json") + ".jsn.Original.txt"
        self.assertEqual(mod_versions.TARGET, storage.RECORDS)
        self.assertEqual(mod_versions.ORIGINAL, storage.ROOT + "/originals" + old)
        self.assertEqual(storage.legacy_path(mod_versions.ORIGINAL), old)
        self.assertEqual(storage.legacy_path(storage.RECORDS), storage.LEGACY_RECORDS)

    def test_all_registered_baselines_use_dedicated_storage(self):
        modules = (mods, mesh, mesh_temperature, smoothing, smoothing.HOMING,
                   *screws.SCAN_FILES, *fluidd_screws.FILES, mod_versions)
        for module in modules:
            with self.subTest(module=module.TARGET):
                self.assertEqual(module.ORIGINAL, storage.original(module.TARGET))
                self.assertEqual(module.MANIFEST, module.ORIGINAL + ".json")

    def test_legacy_fallback_plans_migration_without_changing_files_or_manifests(self):
        canonical = mods.ORIGINAL
        previous = storage.legacy_path(canonical)
        raw = Remote({previous: file(mode=0o444, uid=0, gid=0),
                      previous + ".json": file(b'{"path":"/old/working.py"}')})
        unchanged = copy.deepcopy(raw.files)
        reader = storage.Reader(raw)
        self.assertEqual(reader.read(canonical), raw.files[previous])
        self.assertEqual(reader.read(canonical + ".json")["data"], raw.files[previous + ".json"]["data"])
        reader.read(canonical)
        self.assertEqual(len(reader.moves), 2)
        move = next(item for item in reader.moves if item["destination"] == canonical)
        self.assertEqual(move, {
            "source": previous, "destination": canonical,
            "source_hash": raw.files[previous]["sha256"], "destination_hash": None,
            "attrs": {"mode": 0o444, "uid": 0, "gid": 0}})
        self.assertEqual(raw.files, unchanged)

    def test_identical_duplicates_are_still_planned_for_guarded_legacy_removal(self):
        canonical = mod_versions.TARGET
        previous = storage.LEGACY_RECORDS
        raw = Remote({canonical: file(mode=0o600), previous: file(mode=0o600)})
        reader = storage.Reader(raw)
        self.assertEqual(reader.read(canonical), raw.files[canonical])
        self.assertEqual(reader.moves[0]["destination_hash"], raw.files[canonical]["sha256"])
        self.assertEqual(reader.moves[0]["attrs"], {"mode": 0o600})

    def test_conflicting_contents_or_known_attributes_block_discovery(self):
        canonical = mods.ORIGINAL
        previous = storage.legacy_path(canonical)
        for current in (file(b"other original\n"), file(mode=0o600), file(uid=1), file(gid=1)):
            with self.subTest(current=current):
                reader = storage.Reader(Remote({canonical: current,
                                                previous: file(mode=0o444, uid=0, gid=0)}))
                with self.assertRaisesRegex(ValueError, "Conflicting"):
                    reader.read(canonical)
                self.assertEqual(reader.moves, [])

    def test_reported_hash_is_verified_for_either_location(self):
        for path in (mods.TARGET, mods.ORIGINAL, storage.legacy_path(mods.ORIGINAL)):
            with self.subTest(path=path):
                bad = file()
                bad["sha256"] = "0" * 64
                reader = storage.Reader(Remote({path: bad}))
                with self.assertRaisesRegex(ValueError, "reported hash"):
                    reader.read(mods.ORIGINAL if path != mods.TARGET else path)

    def test_prefetch_expands_and_deduplicates_then_clears_stale_move_plan(self):
        previous = storage.legacy_path(mesh.ORIGINAL)
        raw = Remote({previous: file()})
        reader = storage.Reader(raw)
        reader.read(mesh.ORIGINAL)
        self.assertEqual(len(reader.moves), 1)
        paths = [mesh.TARGET, mesh.ORIGINAL, mesh.ORIGINAL, mod_versions.TARGET]
        reader.prefetch(paths)
        self.assertEqual(raw.prefetched, [mesh.TARGET, mesh.ORIGINAL, previous,
                                         mod_versions.TARGET, storage.LEGACY_RECORDS])
        self.assertEqual(reader.moves, [])
        self.assertEqual(reader.identity(), {"model": "F008"})

    def test_canonical_only_and_absent_data_do_not_plan_moves(self):
        raw = Remote({mods.ORIGINAL: file()})
        reader = storage.Reader(raw)
        self.assertEqual(reader.read(mods.ORIGINAL), raw.files[mods.ORIGINAL])
        self.assertIsNone(reader.read(mesh.ORIGINAL))
        self.assertEqual(reader.moves, [])
        reader.read(mods.TARGET)
        self.assertEqual(raw.reads[-1], mods.TARGET)

    def test_aliases_reject_unrelated_or_non_normalized_paths(self):
        self.assertIsNone(storage.legacy_path(storage.ROOT + "/originals/etc/other.txt"))
        for path in ("relative.cfg", "/tmp/../etc/test.cfg", "//tmp/test.cfg", "/tmp/a\\b.cfg"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                storage.original(path)


if __name__ == "__main__":
    unittest.main()
