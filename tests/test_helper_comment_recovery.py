"""Missing-ledger cleanup verifies implementations independently of revision labels."""
from copy import deepcopy
import unittest

from app import engine, fluidd_screws, mod_comments, mod_versions, mods, screws
from tests.test_fluidd_category_apply import installed
from tests.test_screws_recovery import previous_config, previous_python


def previous_install(revision, *, intermediate=False):
    current = installed()
    current.versions = None
    current.screws.files[screws.PYTHON].data = previous_python()
    current.screws.files[screws.CONFIG].data = previous_config(intermediate)
    frontend = current.screws.frontend[fluidd_screws.TARGET]
    frontend.data = frontend.data.replace(screws.SAVE_COMMAND.encode(), b"SCREWS_TILT_SAVE_MESH")
    for before, after in fluidd_screws.DIALOG_EDITS:
        if after.startswith("retry(){") and not intermediate:
            frontend.data = frontend.data.replace(after.encode(), after.replace(
                '"BED_SCREW_HELPER"', '"SCREWS_TILT_CALCULATE"').encode())
        elif before.startswith(("this.printerSupportsBedScrewsCalculate&&",
                                "show_screws_tilt_adjust_dialog_automatically:")):
            frontend.data = frontend.data.replace(after.encode(), before.encode())
    current.mesh.data = current.mesh.data.replace(screws.SCREWS_INCLUDE.encode(),
        screws.SCREWS_INCLUDE.encode() + b"# unrelated user configuration\n", 1)
    frontend.data += b"// unrelated user interface customization\n"
    for path in mod_versions.FILES[screws.PYTHON]:
        value = engine.file_snapshot(current, path)
        value.data = mod_comments.put(value.data, screws.PYTHON, revision, "1.1.6.4", path)
        value.sha256 = mods.digest(value.data)
    return current


class HelperCommentRecoveryTests(unittest.TestCase):
    def test_verified_implementations_recover_across_revision_labels_and_missing_files(self):
        for revision, intermediate in (("0.1.16", False), ("0.1.17", True), ("2.4.1", False)):
            for missing in (None, screws.PYTHON, screws.CONFIG):
                with self.subTest(revision=revision, missing=missing):
                    current = previous_install(revision, intermediate=intermediate)
                    if missing:
                        value = engine.file_snapshot(current, missing)
                        value.data = value.sha256 = None
                    initial = deepcopy(current)
                    plan = engine.removal_statuses(current)[screws.PYTHON]
                    self.assertTrue(plan["available"], plan)
                    self.assertTrue(plan["header"])
                    self.assertEqual(plan["version"], revision)
                    self.assertIsNone(plan["files"][screws.PYTHON])
                    self.assertIsNone(plan["files"][screws.CONFIG])
                    expected = mod_comments.remove(screws.edit_include(current.mesh.data, False), screws.PYTHON)
                    self.assertEqual(plan["files"][engine.mesh.TARGET], expected)
                    frontend = current.screws.frontend[fluidd_screws.TARGET]
                    self.assertEqual(plan["files"][fluidd_screws.TARGET],
                                     frontend.original + b"// unrelated user interface customization\n")
                    body = frontend.data.decode()
                    for before, after in engine.helper_js_replacements(
                            current, plan["files"][fluidd_screws.TARGET], tolerant=True):
                        self.assertEqual(body.count(before), 1)
                        body = body.replace(before, after, 1)
                    self.assertEqual(body.encode(), plan["files"][fluidd_screws.TARGET])
                    for path in mod_versions.FILES[screws.PYTHON]:
                        self.assertEqual(engine.file_snapshot(current, path).data,
                                         engine.file_snapshot(initial, path).data)

    def test_unknown_edits_conflicting_stamps_and_damaged_ledger_still_block_recovery(self):
        for fault in ("python", "config", "interface", "stamp", "ledger", "backup"):
            with self.subTest(fault=fault):
                current = previous_install("0.1.16")
                path = {"python": screws.PYTHON, "config": screws.CONFIG,
                        "interface": fluidd_screws.TARGET}.get(fault, screws.PYTHON)
                value = engine.file_snapshot(current, path)
                if fault == "python":
                    value.data = value.data.replace(b"range(99)", b"range(98)")
                elif fault == "config":
                    value.data = value.data.replace(b"screw1: 45, 28", b"screw1: 46, 28")
                elif fault == "interface":
                    value.data = value.data.replace(b"gridTemplateColumns", b"editedColumns")
                elif fault == "stamp":
                    value.data = mod_comments.put(value.data, screws.PYTHON, "0.1.17", "1.1.6.4", path)
                elif fault == "backup":
                    value.backup_error = "Original backup metadata is incomplete."
                else:
                    current.versions = engine.MeshSnapshot(b"broken ledger", mods.digest(b"broken ledger"), {})
                value.sha256 = mods.digest(value.data)
                plan = engine.removal_statuses(current)[screws.PYTHON]
                self.assertTrue(plan["present"])
                self.assertFalse(plan["available"], plan)


if __name__ == "__main__":
    unittest.main()
