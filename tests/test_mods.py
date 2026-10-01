"""Focused tests of reversibility, job gating, temperature parsing and cancellation.

The fixture is synthetic; this project contains no copy of the printer baseline.
"""
import io
import json
import types
import unittest
from unittest.mock import patch

from app import mods


SOURCE = b'''import os, json, logging
class PauseResume:
    def handle_connect(self):
        self.v_sd = self.printer.lookup_object('virtual_sdcard', None)
    def _handle_cancel_continue_print_request(self, web_request):
        print_stats = self.printer.lookup_object('print_stats', None)
        if print_stats:
            print_stats.power_loss = 0
    def _handle_cancel_request(self, web_request):
        self.gcode.run_script("CANCEL_PRINT")
    def _handle_fast_response(self, web_request):
        self.gcode.invoke_action()
    def cmd_CANCEL_PRINT(self, gcmd):
        pass
    def get_status(self, eventtime):
        return {}
'''


class ModTests(unittest.TestCase):
    def test_exact_roundtrip_and_idempotence(self):
        enabled = mods.transform(SOURCE, True)
        self.assertEqual(mods.transform(enabled, True), enabled)
        self.assertEqual(mods.transform(enabled, False), SOURCE)
        self.assertEqual(mods.transform(SOURCE, False), SOURCE)

    def test_preserves_unrelated_edits(self):
        enabled = mods.transform(SOURCE, True) + b"\n# user customization\n"
        self.assertEqual(mods.transform(enabled, False), SOURCE + b"\n# user customization\n")

    def test_partial_or_edited_mod_is_not_removed(self):
        enabled = mods.transform(SOURCE, True).replace(b"chamber = float(match.group(2))", b"chamber = 90")
        self.assertFalse(mods.inspect(enabled)["patchable"])
        with self.assertRaises(ValueError):
            mods.transform(enabled, False)

    def test_duplicate_anchor_rejected(self):
        with self.assertRaises(ValueError):
            mods.transform(SOURCE + SOURCE, True)

    def make_printer(self, gcode_text, job_id=2):
        namespace = {}
        exec(mods.transform(SOURCE, True), namespace)
        instance = namespace["PauseResume"]()
        commands = []
        code = types.SimpleNamespace(error=ValueError, cancel_pending=False,
                                     run_script=commands.append, respond_info=lambda value: None)
        heaters = types.SimpleNamespace(lookup_heater=lambda name: types.SimpleNamespace(max_temp=120 if name == "heater_bed" else 60))
        instance.gcode = code
        instance.printer = types.SimpleNamespace(lookup_object=lambda name, default=None: heaters if name == "heaters" else None)
        instance.v_sd = types.SimpleNamespace(sdcard_dirname="/gcodes")
        instance._uncursed_job_id = 1
        instance._uncursed_cancelled = True
        instance._uncursed_job = lambda: {"id": job_id, "filename": "/gcodes/test.gcode"}
        return instance, commands, patch("builtins.open", lambda *args, **kwargs: io.StringIO(gcode_text))

    def test_starts_both_then_waits_once(self):
        p, commands, opened = self.make_printer("M140 S0\nM191 S45\nSTART_PRINT BED_TEMP=70 EXTRUDER_TEMP=262\n")
        with opened:
            p._uncursed_preheat()
            p._uncursed_preheat()
        self.assertEqual(commands, ["M140 S70\nM141 S45\nM190 S70\nM191 S45"])

    def test_chamber_off_ignores_footer(self):
        p, commands, opened = self.make_printer("M141 S0\nSTART_PRINT BED_TEMP=70\n; chamber_temperature = 45\nM191 S45\n")
        with opened:
            p._uncursed_preheat()
        self.assertEqual(commands, ["M140 S70\nM141 S0\nM190 S70"])

    def test_missing_chamber_does_not_enable_it(self):
        p, commands, opened = self.make_printer("START_PRINT BED_TEMP=70\n")
        with opened:
            p._uncursed_preheat()
        self.assertNotIn("M191", commands[0])
        self.assertIn("M141 S0", commands[0])

    def test_existing_job_and_recovery_cleanup_do_not_heat(self):
        p, commands, opened = self.make_printer("START_PRINT BED_TEMP=70", job_id=1)
        with opened:
            p._uncursed_preheat()
        self.assertEqual(commands, [])

    def test_cancel_before_callback_consumes_job(self):
        p, commands, opened = self.make_printer("START_PRINT BED_TEMP=70")
        with opened:
            p._uncursed_reset()
            p._uncursed_preheat()
        self.assertEqual(commands, [])

    def test_cancellation_during_wait_does_not_report_success(self):
        p, commands, opened = self.make_printer("M191 S45\nSTART_PRINT BED_TEMP=70")
        def cancel(script):
            commands.append(script)
            p._uncursed_reset()
        p.gcode.run_script = cancel
        with opened, self.assertRaisesRegex(ValueError, "cancelled"):
            p._uncursed_preheat()
        self.assertEqual(len(commands), 1)

    def test_invalid_target_fails_before_heat_or_consuming_job(self):
        for text in ("M191 S90\nSTART_PRINT BED_TEMP=70", "M140 S-1\nSTART_PRINT", "M141 R45\nSTART_PRINT BED_TEMP=70", "G28\nSTART_PRINT BED_TEMP=70"):
            p, commands, opened = self.make_printer(text)
            with opened, self.assertRaises(ValueError):
                p._uncursed_preheat()
            self.assertEqual(commands, [])
            self.assertEqual(p._uncursed_job_id, 1)


if __name__ == "__main__":
    unittest.main()
