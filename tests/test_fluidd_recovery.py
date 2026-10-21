"""Recovery recognizes command renames without broadening normal UI edits."""
import unittest

from app import fluidd_screws, mod_comments, screws


PATH = fluidd_screws.TARGET


def fixture(enabled=False, old=False):
    pairs = fluidd_screws.blocks(PATH)
    data = ("// unrelated printer customization\n" + ";\n".join(
        replacement if enabled else original for original, replacement in pairs) + ";\n").encode()
    if old:
        data = data.replace(b"_UNCURSER_BED_SCREW_SAVE_MESH", b"SCREWS_TILT_SAVE_MESH")
        for original, replacement in fluidd_screws.DIALOG_EDITS:
            if replacement.startswith("retry(){"):
                data = data.replace(replacement.encode(), replacement.replace(
                    '"BED_SCREW_HELPER"', '"SCREWS_TILT_CALCULATE"').encode())
            elif original.startswith(('this.printerSupportsBedScrewsCalculate&&',
                                      'show_screws_tilt_adjust_dialog_automatically:')):
                data = data.replace(replacement.encode(), original.encode())
    return data


class FluiddRecoveryTests(unittest.TestCase):
    def test_recovers_known_renames_preserving_comments_and_unrelated_edits(self):
        for revision in ("0.1.16", "0.1.17", "0.1.18"):
            data = mod_comments.put(fixture(True, True), screws.PYTHON, revision, "1.1.7.0", PATH)
            expected = mod_comments.put(fixture(), screws.PYTHON, revision, "1.1.7.0", PATH)
            self.assertEqual(fluidd_screws.recovery_undo(PATH, data, fixture()), expected)
            self.assertEqual(fluidd_screws.recovery_undo(PATH, expected, fixture()), expected)
            pairs = fluidd_screws.recovery_replacements(PATH, data, expected)
            result = data.decode()
            for before, after in pairs:
                result = result.replace(before, after, 1)
            self.assertEqual(result.encode(), expected)

    def test_preserves_recognized_firmware_interface_instead_of_forcing_stock(self):
        self.assertEqual(fluidd_screws.recovery_undo(PATH, fixture(True), fixture(True, True)),
                         fixture(True, True))
        self.assertEqual(fluidd_screws.recovery_undo(PATH, fixture(True, True), fixture(True, True)),
                         fixture(True, True))

    def test_current_and_original_interfaces_still_work(self):
        self.assertEqual(fluidd_screws.recovery_undo(PATH, fixture(True), fixture()), fixture())
        self.assertEqual(fluidd_screws.recovery_undo(PATH, fixture(), fixture()), fixture())
        with self.assertRaises(ValueError):
            fluidd_screws.strip_mod(PATH, fixture(True, True))

    def test_recovery_does_not_rewrite_unrelated_code(self):
        data = fixture(True, True) + b"// preserved user edit\n"
        expected = fixture() + b"// preserved user edit\n"
        self.assertEqual(fluidd_screws.recovery_undo(PATH, data, fixture()), expected)
        with self.assertRaisesRegex(ValueError, "Unrelated Fluidd code"):
            fluidd_screws.recovery_replacements(PATH, data, fixture())
        self.assertTrue(fluidd_screws.recovery_replacements(PATH, data, expected))

    def test_edited_duplicated_and_partially_modified_blocks_are_rejected(self):
        old = fixture(True, True)
        original, replacement = fluidd_screws.blocks(PATH)[1]
        variants = (
            old.replace(b"gridTemplateColumns", b"editedColumns"),
            old.replace(replacement.encode(), original.encode()),
            old + replacement.encode(),
            old + original.encode(),
            old + fluidd_screws.GRID_RENDER.encode(),
        )
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValueError):
                fluidd_screws.recovery_undo(PATH, data, fixture())
            with self.subTest(reference=data), self.assertRaises(ValueError):
                fluidd_screws.recovery_undo(PATH, fixture(True), data)


if __name__ == "__main__":
    unittest.main()
