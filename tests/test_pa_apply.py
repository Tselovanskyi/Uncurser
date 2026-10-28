"""Focused PA batching and partial-install recovery checks; no printer access."""
import base64
import copy
import unittest
from unittest.mock import patch

from app import engine, mod_comments, mod_versions, mods, pa, removal, stock_recovery
from tests.test_stock_recovery import stock
from tests.test_warmup_apply import OFF, STATUS, apply_locally


# A minimal native source keeps these engine checks independent of a downloaded
# firmware image. Native implementation and ABI checks live in the PA tests.
SOURCE = ("import os, logging\nclass ExtruderStepper:\n"
          "    def __init__(self, config):\n" + pa._OLD_ALLOC + pa._OLD_SET).encode()
DEPENDENCIES = {pa.STOCK_HELPER: b"native helper", pa.TOOLHEAD: b"native scheduler"}
IDENTITY = ("F008", "1.1.7.0")


def snapshot():
    value = engine.Snapshot(None, None, dict(OFF), None, None, "", copy.deepcopy(STATUS), "", {}, 0)
    value.pa_dependencies = dict(DEPENDENCIES)
    value.pa = engine.MeshSnapshot(SOURCE, mods.digest(SOURCE), pa.inspect(SOURCE, SOURCE),
        guards={path: mods.digest(data) for path, data in DEPENDENCIES.items()},
        backup_guards={pa.ORIGINAL: None, pa.MANIFEST: None})
    value.pa_library = engine.MeshSnapshot(None, None, dict(OFF),
        backup_guards={engine.recorded_file(pa.LIBRARY).ORIGINAL: None,
                       engine.recorded_file(pa.LIBRARY).MANIFEST: None})
    return value


def installed():
    value = snapshot()
    changes = engine.prepare_all(value, None, pa_desired=True)
    for path in pa.FILES:
        current = engine.file_snapshot(value, path)
        current.data, current.sha256 = changes[path], mods.digest(changes[path])
        current.original, current.original_hash = (SOURCE, mods.digest(SOURCE)) if path == pa.TARGET else (None, mods.digest(b""))
        current.original_absent = path == pa.LIBRARY
        current.original_identity = IDENTITY
    value.pa.analysis = pa.inspect_files(engine.pa_current(value), {pa.TARGET: SOURCE}, DEPENDENCIES)
    data = changes[mod_versions.TARGET]
    value.versions = engine.MeshSnapshot(data, mods.digest(data), dict(OFF),
        original_hash=mods.digest(b""), original_absent=True)
    return value


class PAApplyTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(pa, "REFERENCE_HASH", mods.digest(SOURCE)))
        self.enterContext(patch.object(pa, "validate_dependencies", return_value=None))

    def test_install_orders_library_source_records_and_guards_dependencies(self):
        scanned = snapshot()
        pending = engine.prepare_all(scanned, None, pa_desired=True)
        request = apply_locally(scanned, pending, pa_desired=True)
        self.assertEqual([item["path"] for item in request["files"]],
                         [pa.LIBRARY, pa.TARGET, mod_versions.TARGET])
        library, source, ledger = request["files"]
        self.assertEqual(library["created_binary"], mod_versions.binary_created(pa.helper_data()))
        self.assertNotIn("binary_changes", library)
        self.assertTrue(library["may_capture_original"])
        self.assertIsNone(library["expected"])
        self.assertEqual(request["python_caches"], {pa.TARGET: mods.digest(SOURCE)})
        for path, data in DEPENDENCIES.items():
            self.assertEqual(request["guards"][path], mods.digest(data))
        contents = base64.b64decode(source["data"])
        self.assertEqual(mod_comments.read(contents),
            {pa.TARGET: {"version": mod_versions.VERSIONS[pa.TARGET], "firmware": IDENTITY[1]}})
        record = mod_versions.load(base64.b64decode(ledger["data"]))[pa.TARGET]
        self.assertEqual(mod_versions.undo_files(pa.TARGET,
            {path: pending[path] for path in pa.FILES}, record), {pa.TARGET: SOURCE, pa.LIBRARY: None})

    def test_remove_uses_saved_instructions_and_preserves_unrelated_source(self):
        scanned = installed()
        extra = b"\n# unrelated edit\n"
        scanned.pa.data += extra
        scanned.pa.sha256 = mods.digest(scanned.pa.data)
        pending = engine.prepare_all(scanned, None, pa_desired=False)
        self.assertEqual(pending[pa.TARGET], SOURCE + extra)
        self.assertIsNone(pending[pa.LIBRARY])
        request = apply_locally(scanned, pending, pa_desired=False)
        self.assertEqual([item["path"] for item in request["files"]],
                         [pa.TARGET, pa.LIBRARY, mod_versions.TARGET])
        self.assertEqual(request["files"][1]["created_binary"],
                         mod_versions.binary_created(scanned.pa_library.data))

    def test_restore_needs_both_source_original_and_helper_absence_proof(self):
        scanned = installed()
        self.assertTrue(set(pa.FILES).issubset(engine.restorable(scanned)))
        pending = engine.prepare_all(scanned, None, restore=True)
        request = apply_locally(scanned, pending, restore=True)
        self.assertEqual([item["path"] for item in request["files"]],
                         [pa.TARGET, pa.LIBRARY, mod_versions.TARGET])
        self.assertTrue(request["files"][1]["original_absent"])
        scanned.pa_library.original_hash = None
        self.assertTrue(set(pa.FILES).isdisjoint(engine.restorable(scanned)))

    def test_restore_can_delete_a_damaged_helper_using_verified_absence_backup(self):
        scanned = installed()
        scanned.pa_library.data = b"damaged, non-ELF installed file"
        scanned.pa_library.sha256 = mods.digest(scanned.pa_library.data)
        pending = engine.prepare_all(scanned, None, restore=True)
        request = apply_locally(scanned, pending, restore=True)
        item = next(item for item in request["files"] if item["path"] == pa.LIBRARY)
        self.assertNotIn("created_binary", item)
        self.assertTrue(item["restore"])
        self.assertTrue(item["original_absent"])
        self.assertEqual(item["expected"], scanned.pa_library.sha256)

    def test_recorded_old_code_and_helper_update_without_app_template_history(self):
        scanned = installed()
        source = scanned.pa.data.replace(b"Unable to allocate Uncurser PA history",
                                        b"Old revision allocation failure")
        library = scanned.pa_library.data + b"old helper revision"
        changes = mod_versions.capture_changes(pa.TARGET, SOURCE, source)
        record = mod_versions.capture_files(pa.TARGET,
            {pa.TARGET: SOURCE, pa.LIBRARY: None}, {pa.TARGET: source, pa.LIBRARY: library},
            {pa.TARGET: [(change["before"], change["after"]) for change in changes]}, firmware=IDENTITY[1])
        record["version"] = "0.1.17"
        for filename, data in ((pa.TARGET, source), (pa.LIBRARY, library),
                               (mod_versions.TARGET, mod_versions.dump({pa.TARGET: record}))):
            value = engine.file_snapshot(scanned, filename)
            value.data, value.sha256 = data, mods.digest(data)
        state = engine.removal_statuses(scanned)[pa.TARGET]
        self.assertTrue(state["available"], state)
        self.assertFalse(state["incomplete"], state)
        pending = engine.prepare_all(scanned, None, pa_desired=True)
        self.assertEqual(pending[pa.LIBRARY], pa.helper_data())
        self.assertNotIn(b"Old revision allocation failure", pending[pa.TARGET])
        request = apply_locally(scanned, pending, pa_desired=True)
        item = next(item for item in request["files"] if item["path"] == pa.LIBRARY)
        self.assertEqual(item["previous_created_binary"], mod_versions.binary_created(library))
        self.assertEqual(item["created_binary"], mod_versions.binary_created(pa.helper_data()))

    def test_apply_rejects_a_dependency_changed_after_scan(self):
        scanned, fresh = snapshot(), snapshot()
        fresh.pa_dependencies[pa.STOCK_HELPER] += b"changed"
        fresh.pa.guards[pa.STOCK_HELPER] = mods.digest(fresh.pa_dependencies[pa.STOCK_HELPER])
        with patch.object(engine, "rescan", return_value=fresh), \
                patch.object(engine.network, "status", return_value=scanned.status), \
                patch.object(engine, "Remote") as remote:
            with self.assertRaisesRegex(RuntimeError, "changed after"):
                engine.apply_many(None, None, scanned, None, None, False, False,
                                  lambda _: None, pa_desired=True)
        remote.assert_not_called()

    def test_comment_recovery_removes_surviving_source_without_creating_missing_library(self):
        scanned = installed()
        scanned.versions = None
        scanned.pa.original = scanned.pa.original_hash = None
        scanned.pa_library.data = scanned.pa_library.sha256 = None
        scanned.pa_library.original_hash, scanned.pa_library.original_absent = None, False
        files = {path: engine.file_snapshot(scanned, path) for path in pa.FILES}
        defaults = stock({pa.TARGET: SOURCE, pa.LIBRARY: None})
        plan = removal.inspect(files, {}, stock=defaults, identity=IDENTITY, header_fallback=True)[pa.TARGET]
        self.assertTrue(plan["available"], plan)
        self.assertTrue(plan["incomplete"])
        self.assertEqual(plan["files"], {pa.TARGET: SOURCE, pa.LIBRARY: None})
        scanned.stock = defaults
        pending = engine.prepare_all(scanned, None, pa_desired=False)
        request = apply_locally(scanned, pending, pa_desired=False)
        self.assertEqual([item["path"] for item in request["files"]], [pa.TARGET])

    def test_comment_recovery_accepts_each_combination_of_restored_owned_regions(self):
        pairs = ((pa._OLD_ALLOC, pa._NEW_ALLOC), (pa._OLD_SET, pa._NEW_SET),
                 (pa._CLASS, pa._loader() + pa._CLASS))
        extra = b"\n# unrelated user edit\n"
        for mask in range(8):
            for missing_library in (False, True):
                with self.subTest(restored_regions=mask, missing_library=missing_library):
                    scanned = installed()
                    scanned.versions = None
                    source = scanned.pa.data
                    for index, (before, after) in enumerate(pairs):
                        if mask & (1 << index):
                            source = source.replace(after.encode(), before.encode(), 1)
                    scanned.pa.data = source + extra
                    scanned.pa.sha256 = mods.digest(scanned.pa.data)
                    scanned.pa.original = scanned.pa.original_hash = None
                    if missing_library:
                        scanned.pa_library.data = scanned.pa_library.sha256 = None
                    scanned.stock = stock({pa.TARGET: SOURCE, pa.LIBRARY: None})
                    issue = engine.firmware_issues(scanned)[pa.TARGET]
                    self.assertTrue(issue["available"], issue)
                    self.assertTrue(issue["header"], issue)
                    pending = engine.prepare_removal(scanned, {pa.TARGET: False})
                    self.assertEqual(pending, {pa.TARGET: SOURCE + extra, pa.LIBRARY: None})
                    self.assertIsNone(scanned.pa.original)

    def test_comment_partial_recovery_rejects_edited_or_duplicated_regions(self):
        for fault in ("edited code", "duplicate modified region", "duplicate stock region", "edited helper"):
            with self.subTest(fault=fault):
                scanned = installed()
                scanned.versions = None
                source = scanned.pa.data.replace(pa._NEW_ALLOC.encode(), pa._OLD_ALLOC.encode(), 1)
                if fault == "edited code":
                    source = source.replace(b"Uncurser PA move is no longer queued", b"Changed owned code")
                elif fault == "duplicate modified region":
                    source += pa._NEW_SET.encode()
                elif fault == "duplicate stock region":
                    source += pa._OLD_SET.encode()
                else:
                    scanned.pa_library.data += b"unrecognized edit"
                    scanned.pa_library.sha256 = mods.digest(scanned.pa_library.data)
                scanned.pa.data, scanned.pa.sha256 = source, mods.digest(source)
                scanned.stock = stock({pa.TARGET: SOURCE, pa.LIBRARY: None})
                issue = engine.firmware_issues(scanned)[pa.TARGET]
                self.assertFalse(issue["available"], issue)
                with self.assertRaises(ValueError):
                    engine.prepare_removal(scanned, {pa.TARGET: False})

    def test_missing_helper_is_an_issue_even_when_ledger_firmware_is_current(self):
        scanned = installed()
        scanned.pa_library.data = scanned.pa_library.sha256 = None
        issue = engine.firmware_issues(scanned)[pa.TARGET]
        self.assertTrue(issue["available"], issue)
        self.assertTrue(issue["incomplete"])
        self.assertEqual(issue["installed_firmware"], IDENTITY[1])
        self.assertIn("incomplete", issue["firmware_reason"])

    def test_restored_source_or_owned_region_is_a_verified_partial_installation(self):
        for reset in ("whole source", "allocation"):
            with self.subTest(reset=reset):
                scanned = installed()
                source = (SOURCE if reset == "whole source" else
                          scanned.pa.data.replace(pa._NEW_ALLOC.encode(), pa._OLD_ALLOC.encode(), 1))
                scanned.pa.data, scanned.pa.sha256 = source, mods.digest(source)
                issue = engine.firmware_issues(scanned)[pa.TARGET]
                self.assertTrue(issue["available"], issue)
                self.assertTrue(issue["incomplete"], issue)
                pending = engine.prepare_removal(scanned, {pa.TARGET: False})
                self.assertEqual(pending[pa.TARGET], SOURCE)
                self.assertIsNone(pending[pa.LIBRARY])

    def test_partial_source_recovers_from_firmware_when_its_original_is_missing(self):
        for before, after in ((pa._OLD_ALLOC, pa._NEW_ALLOC), (pa._OLD_SET, pa._NEW_SET),
                              (pa._CLASS, pa._loader() + pa._CLASS)):
            with self.subTest(restored=before.splitlines()[0]):
                scanned = installed()
                extra = b"\n# unrelated user edit\n"
                source = scanned.pa.data.replace(after.encode(), before.encode(), 1) + extra
                scanned.pa.data, scanned.pa.sha256 = source, mods.digest(source)
                scanned.pa.original = scanned.pa.original_hash = None
                scanned.stock = stock({pa.TARGET: SOURCE, pa.LIBRARY: None})
                issue = engine.firmware_issues(scanned)[pa.TARGET]
                self.assertTrue(issue["available"], issue)
                self.assertTrue(issue["recovery"], issue)
                self.assertTrue(issue["incomplete"], issue)
                pending = engine.prepare_removal(scanned, {pa.TARGET: False})
                self.assertEqual(pending[pa.TARGET], SOURCE + extra)
                self.assertIsNone(pending[pa.LIBRARY])
                self.assertIsNone(scanned.pa.original)

    def test_partial_recovery_rejects_edited_owned_code(self):
        scanned = installed()
        source = scanned.pa.data.replace(pa._NEW_ALLOC.encode(), pa._OLD_ALLOC.encode(), 1)
        source = source.replace(b"Uncurser PA move is no longer queued", b"Changed owned code")
        scanned.pa.data, scanned.pa.sha256 = source, mods.digest(source)
        scanned.pa.original = scanned.pa.original_hash = None
        scanned.stock = stock({pa.TARGET: SOURCE, pa.LIBRARY: None})
        issue = engine.firmware_issues(scanned)[pa.TARGET]
        self.assertFalse(issue["available"], issue)
        with self.assertRaises(ValueError):
            engine.prepare_removal(scanned, {pa.TARGET: False})

    def test_missing_optional_comment_does_not_make_recorded_code_incomplete(self):
        scanned = installed()
        source = mod_comments.remove(scanned.pa.data, pa.TARGET)
        scanned.pa.data, scanned.pa.sha256 = source, mods.digest(source)
        state = engine.removal_statuses(scanned)[pa.TARGET]
        self.assertTrue(state["available"], state)
        self.assertFalse(state["incomplete"], state)
        self.assertNotIn(pa.TARGET, engine.firmware_issues(scanned))

    def test_changed_helper_is_reported_without_authorizing_removal(self):
        scanned = installed()
        scanned.pa_library.data += b"unrecognized edit"
        scanned.pa_library.sha256 = mods.digest(scanned.pa_library.data)
        issue = engine.firmware_issues(scanned)[pa.TARGET]
        self.assertTrue(issue["incomplete"], issue)
        self.assertFalse(issue["available"], issue)
        self.assertIn("differs", issue["reason"])

    def test_recovery_blocks_edited_helper_and_preserves_native_adoption(self):
        scanned = installed()
        files = {path: engine.file_snapshot(scanned, path) for path in pa.FILES}
        files[pa.LIBRARY].data += b"edited"
        files[pa.LIBRARY].sha256 = mods.digest(files[pa.LIBRARY].data)
        with self.assertRaisesRegex(ValueError, "PA helper differs"):
            stock_recovery.adaptive_pa(files, stock({pa.TARGET: SOURCE, pa.LIBRARY: None}), {})
        files[pa.LIBRARY].data = pa.helper_data()
        files[pa.LIBRARY].sha256 = mods.digest(pa.helper_data())
        native = mod_comments.strip(files[pa.TARGET].data)
        result, changed = stock_recovery.adaptive_pa(files,
            stock({pa.TARGET: native, pa.LIBRARY: pa.helper_data()}), {})
        self.assertFalse(changed)
        self.assertEqual(result[pa.LIBRARY], pa.helper_data())


if __name__ == "__main__":
    unittest.main()
