"""Only local fixtures: reversible Fluidd edits and degree output compatibility."""
import base64
import json
import os
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from app import engine, fluidd_screws, mods, screws
from tests import test_mesh as mesh_tests, test_screws as helper_tests


class FluiddTests(unittest.TestCase):
    def test_popup_roundtrip_and_duplicates_are_rejected(self):
        for path, original in helper_tests.FLUIDD_SOURCES.items():
            changed = fluidd_screws.transform(path, original, True)
            self.assertEqual(fluidd_screws.transform(path, changed, True), changed)
            self.assertEqual(fluidd_screws.transform(path, changed, False), original)
            with self.assertRaises(ValueError):
                fluidd_screws.transform(path, original + original, True)
            with self.assertRaises(ValueError):
                fluidd_screws.transform(path, changed.replace(b'gridTemplateColumns', b'editedColumns'), False)
            pairs = fluidd_screws.blocks(path)
            self.assertGreater(len(pairs), 1)
            partial = original.replace(pairs[0][0].encode(), pairs[0][1].encode(), 1)
            for value in (partial, original + pairs[-1][0].encode(), changed + pairs[-1][1].encode(),
                          original + pairs[0][1].encode() * 2):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    fluidd_screws.transform(path, value, True)

    def test_review_shows_exact_edits_without_the_rest_of_the_bundle(self):
        current = helper_tests.snapshot()
        preview = engine.difference_all(current, None, screws_desired=True)
        self.assertIn('s.adjustMinutes*6', preview)
        self.assertIn('gridTemplateColumns', preview)
        self.assertNotIn('sw.js', preview)
        self.assertNotIn('synthetic bundle', preview)
        for _, replacement in fluidd_screws.blocks(fluidd_screws.TARGET):
            self.assertIn(replacement, preview)
        path = fluidd_screws.TARGET
        old = helper_tests.FLUIDD_SOURCES[path]
        new = fluidd_screws.transform(path, old, True) + b'// unrelated edit\n'
        with self.assertRaises(ValueError):
            fluidd_screws.replacements(path, old, new)
        self.assertIn('unrelated edit', fluidd_screws.difference(path, old, new, 'Current', 'Pending'))

    def test_cached_or_missing_frontend_blocks_only_helper(self):
        current = helper_tests.snapshot()
        class Reader:
            def read(self, path):
                if path.endswith('.gz'):
                    return helper_tests.record(b'compressed copy')
                if path == fluidd_screws.INDEX:
                    return helper_tests.record(('src="./' + fluidd_screws.ASSET + '"').encode())
                return helper_tests.record(helper_tests.FLUIDD_SOURCES.get(path))
        states = engine.scan_fluidd_screws(Reader())
        self.assertTrue(all(not state.analysis['patchable'] for state in states.values()))
        self.assertTrue(current.mesh.analysis['patchable'])
        current.mesh.original = helper_tests.SOURCE
        current.mesh.original_hash = mods.digest(helper_tests.SOURCE)
        current.screws.analysis.update(known=False, patchable=False, enabled=None)
        self.assertIn(engine.mesh.TARGET, engine.restorable(current))

    def test_degree_console_keeps_fluidd_wire_format(self):
        package = types.ModuleType('fixture_extras')
        package.probe = types.ModuleType('fixture_extras.probe')
        module = types.ModuleType('fixture_extras.screws_tilt_adjust')
        module.__package__ = package.__name__
        with patch.dict('sys.modules', {package.__name__: package, package.probe.__name__: package.probe,
                                      'mathutil': types.ModuleType('mathutil')}):
            exec(compile(screws.payload(screws.PYTHON), '<helper>', 'exec'), module.__dict__)
        helper = module.ScrewsTiltAdjust.__new__(module.ScrewsTiltAdjust)
        helper.screws = [((0, 0), 'Base'), ((1, 0), 'Right'), ((0, 1), 'Rear')]
        helper.thread, helper.direction, helper.max_diff = 2, None, None
        helper.results, helper.max_diff_error = {}, False
        messages = []
        helper.gcode = types.SimpleNamespace(respond_info=messages.append)
        helper._calculate_adjustments([(0, 0, 0), (1, 0, -.035), (0, 1, .7)])
        self.assertEqual(helper.results['screw2']['adjust'], '00:03')
        self.assertEqual(helper.results['screw3']['adjust'], '01:00')
        self.assertTrue(any('adjust CW 18°' in line for line in messages))
        self.assertTrue(any('adjust CCW 360°' in line for line in messages))


class FluiddBatchTests(unittest.TestCase):
    setUp = mesh_tests.BatchTests.setUp
    tearDown = mesh_tests.BatchTests.tearDown
    run_request = mesh_tests.BatchTests.run_request

    def request(self, enabled=True, restore=False):
        items = []
        for remote_path, original in helper_tests.FLUIDD_SOURCES.items():
            path = self.root / remote_path.rsplit('/', 1)[-1]
            if not path.exists():
                path.write_bytes(original)
            before = path.read_bytes()
            after = original if restore else fluidd_screws.transform(remote_path, before, enabled)
            backup = path.with_suffix('.jfn.Original.txt')
            items.append(dict(path=str(path), backup=str(backup), expected=mods.digest(before),
                              data=base64.b64encode(after).decode(), new_hash=mods.digest(after),
                              original_hash=mods.digest(original) if backup.exists() else None,
                              may_capture_original=True, reference='fixture', restore=restore,
                              kind='javascript', replacements=fluidd_screws.replacements(remote_path, before, after)))
        return {'files': items}

    def test_backup_toggle_and_full_restore(self):
        self.run_request(self.request())
        originals = {path: path.read_bytes() for path in self.root.glob('*.Original.txt*')}
        self.run_request(self.request(False))
        self.run_request(self.request())
        self.run_request(self.request(restore=True))
        for path, data in originals.items():
            self.assertEqual(path.read_bytes(), data)
        for path, data in helper_tests.FLUIDD_SOURCES.items():
            self.assertEqual((self.root / path.rsplit('/', 1)[-1]).read_bytes(), data)

    def test_interface_write_failure_preserves_original(self):
        native_replace = os.replace
        def replace(source, target):
            if Path(target).name == Path(fluidd_screws.TARGET).name:
                raise OSError('interface update failed')
            native_replace(source, target)
        with self.assertRaisesRegex(OSError, 'interface update failed'):
            self.run_request(self.request(), replace)
        for path, data in helper_tests.FLUIDD_SOURCES.items():
            self.assertEqual((self.root / path.rsplit('/', 1)[-1]).read_bytes(), data)

    def test_unrelated_javascript_changes_are_rejected(self):
        request = self.request()
        item = request['files'][0]
        data = base64.b64decode(item['data']) + b'// hidden change'
        item.update(data=base64.b64encode(data).decode(), new_hash=mods.digest(data))
        with self.assertRaisesRegex(ValueError, 'Unrelated Fluidd code'):
            self.run_request(request)
        self.assertFalse(list(self.root.glob('*.Original.txt')))


if __name__ == '__main__':
    unittest.main()
