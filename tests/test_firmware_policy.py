"""Firmware maintenance policy and idle guards use local status fixtures only."""
import copy
import unittest

from app import network


def status(version="1.1.6.4", *, model="F008", state="ready"):
    return {"native": {"model": model, "modelVersion": version, "deviceState": 0},
            "info": {"state": state},
            "objects": {"print_stats": {"state": "standby"},
                        "idle_timeout": {"state": "Ready"}, "toolhead": {"G29_flag": False}}}


class FirmwarePolicyTests(unittest.TestCase):
    def test_verified_release_is_supported_for_all_operations(self):
        for firmware in ("1.1.6.4", "1.1.7.0"):
            for version in (firmware, f"printer sw ver:;DWIN hw ver:CR0CN240110C10;DWIN sw ver:{firmware};"):
                with self.subTest(version=version):
                    current = status(version)
                    self.assertEqual(network.firmware_state(current), "supported")
                    self.assertFalse(network.is_untested(current))
                    self.assertEqual(network.compatibility_error(current), "")
                    network.require_supported(current)
                    network.require_supported(current, allow_untested=True)
                    network.require_removal_supported(current)

    def test_only_verified_newer_numeric_releases_are_untested(self):
        for version in ("1.1.7.1", "1.1.10.0", "1.1.6.40", "1.2.0.0", "2.0.0.0"):
            with self.subTest(version=version):
                current = status(version)
                self.assertEqual(network.firmware_state(current), "untested")
                self.assertTrue(network.is_untested(current))
                self.assertEqual(network.compatibility_error(current),
                    f"Firmware {version} is not tested. Mod compatibility is not guaranteed.")
                network.require_supported(current, allow_untested=True)
                network.require_removal_supported(current)
                with self.assertRaises(ValueError):
                    network.require_supported(current)

    def test_native_dwin_field_receives_the_same_numeric_policy(self):
        current = status("printer sw ver:;DWIN hw ver:CR0CN240110C10;DWIN sw ver:1.1.7.1;")
        self.assertEqual(network.firmware_state(current), "untested")
        self.assertIn("Firmware 1.1.7.1 is not tested", network.compatibility_error(current))
        network.require_supported(current, allow_untested=True)

    def test_older_other_model_unknown_and_ambiguous_releases_are_unsupported(self):
        invalid = [None, {}, {"native": None}, status("1.1.6.4", model="F009"),
                   status("1.1.7.0", model="F009"), status("1.1.7.0", model=""),
                   status("DWIN sw ver:1.1.7.0;DWIN sw ver:1.1.8.0;")]
        invalid.extend(status(version) for version in (
            None, "", "1.1.6.3", "1.1.5.9", "1.0.10.0", "1.1.7", "1.1.7.0-beta", "x1.1.7.0"))
        for current in invalid:
            with self.subTest(status=current):
                self.assertEqual(network.firmware_state(current), "unsupported")
                self.assertFalse(network.is_untested(current))
                self.assertTrue(network.compatibility_error(current))
                with self.assertRaises(ValueError):
                    network.require_supported(current)
                with self.assertRaises(ValueError):
                    network.require_supported(current, allow_untested=True)
                with self.assertRaises(ValueError):
                    network.require_removal_supported(current)

    def test_idle_guard_default_remains_ready_only(self):
        network.require_idle(status())
        for state in ("error", "shutdown", "startup", "disconnected", "", None):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                network.require_idle(status(state=state))

    def test_error_allowance_does_not_permit_other_unready_states(self):
        network.require_idle(status(state="error"), allow_error=True)
        network.require_idle(status(), allow_error=True)
        for state in ("shutdown", "startup", "disconnected", "", None):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                network.require_idle(status(state=state), allow_error=True)

    def test_error_allowance_retains_every_activity_guard(self):
        cases = (("native", "deviceState", 1), ("native", "deviceState", None),
                 ("print_stats", "state", "printing"), ("print_stats", "state", "paused"),
                 ("idle_timeout", "state", "Printing"), ("toolhead", "G29_flag", True))
        for state in ("ready", "error"):
            for group, key, value in cases:
                with self.subTest(state=state, group=group, value=value):
                    current = copy.deepcopy(status(state=state))
                    target = current["native"] if group == "native" else current["objects"][group]
                    target[key] = value
                    with self.assertRaises(RuntimeError):
                        network.require_idle(current, allow_error=True)


if __name__ == "__main__":
    unittest.main()
