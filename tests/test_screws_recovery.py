"""Missing-ledger recognition accepts command renames, never unrelated edits."""
import unittest

from app import mod_comments, screws


def previous_python():
    data = screws.payload(screws.PYTHON)
    for current, previous in ((screws.COMMAND, "SCREWS_TILT_CALCULATE"),
                              (screws.SAVE_COMMAND, "SCREWS_TILT_SAVE_MESH")):
        data = data.replace(('"' + current + '"').encode(), ('"' + previous + '"').encode())
        data = data.replace(("cmd_" + current).encode(), ("cmd_" + previous).encode())
    return data.replace(b"Adjust screws and run BED_SCREW_HELPER again.",
                        b"Adjust screws and run SCREWS_TILT_CALCULATE again.")


def previous_config(intermediate=False):
    data = screws.payload(screws.CONFIG)
    wrapper = (b"[gcode_macro BED_SCREW_HELPER]\ngcode:\n  "
               + screws.COMMAND.encode() + b" {rawparams}\n")
    previous = (wrapper.replace(screws.COMMAND.encode(), b"SCREWS_TILT_CALCULATE")
                if intermediate else
                b"[gcode_macro SCREWS_TILT_CALCULATE]\n"
                b"rename_existing: _ORIGIN_SCREWS_TILT_CALCULATE\ngcode:\n"
                b"  _ORIGIN_SCREWS_TILT_CALCULATE {rawparams}\n")
    assert wrapper in data
    return data.replace(wrapper, previous)


class ScrewsRecoveryTests(unittest.TestCase):
    def test_current_and_known_renames_match_complete_latest_payload(self):
        variants = ((screws.PYTHON, previous_python()),
                    (screws.CONFIG, previous_config()),
                    (screws.CONFIG, previous_config(intermediate=True)),
                    (screws.LEGACY_CONFIG, previous_config()))
        for path, data in variants:
            with self.subTest(path=path, intermediate=b"_ORIGIN_" not in data):
                self.assertTrue(screws.recovery_recognized(path, data))
                self.assertFalse(screws.recognized(path, data))
        for path in (screws.PYTHON, screws.CONFIG):
            self.assertTrue(screws.recognized(path, screws.payload(path)))
            self.assertTrue(screws.recovery_recognized(path, screws.payload(path)))

    def test_version_metadata_and_line_endings_do_not_replace_code_verification(self):
        for version in ("0.1.16", "0.1.17", "2.4.1"):
            for path, data in ((screws.PYTHON, previous_python()),
                               (screws.CONFIG, previous_config())):
                stamped = mod_comments.put(data, screws.PYTHON, version, "1.1.7.0", path)
                with self.subTest(version=version, path=path):
                    self.assertTrue(screws.recovery_recognized(path, stamped.replace(b"\n", b"\r\n")))

    def test_other_python_and_config_edits_are_rejected(self):
        changed_python = previous_python().replace(b"for i in range(99):", b"for i in range(98):")
        changed_config = previous_config().replace(b"screw1: 45, 28", b"screw1: 46, 28")
        for path, data in ((screws.PYTHON, changed_python),
                           (screws.CONFIG, changed_config),
                           (screws.PYTHON, previous_python() + b"\n# user edit\n"),
                           (screws.CONFIG, previous_config() + b"\n[gcode_macro SCREWS_TILT_CALCULATE]\n"
                            b"rename_existing: _ORIGIN_SCREWS_TILT_CALCULATE\ngcode:\n"
                            b"  _ORIGIN_SCREWS_TILT_CALCULATE {rawparams}\n")):
            with self.subTest(path=path, data=data[-80:]):
                self.assertFalse(screws.recovery_recognized(path, data))


if __name__ == "__main__":
    unittest.main()
