"""Python-cache cleanup checks on synthetic files; no printer connections."""
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import patch

from app import engine, mods, screws
from app.remote import APPLY_BATCH, Connection
from tests import test_mods, test_screws
import test_mesh as mesh_tests


class PythonCacheTests(unittest.TestCase):
    setUp = mesh_tests.BatchTests.setUp
    tearDown = mesh_tests.BatchTests.tearDown
    request = mesh_tests.BatchTests.request
    run_request = mesh_tests.BatchTests.run_request

    def caches(self):
        directory = self.root / '__pycache__'
        directory.mkdir(exist_ok=True)
        paths = [self.root / 'example.pyc', self.root / 'example.pyo',
                 directory / 'example.cpython-37.pyc',
                 directory / 'example.cpython-311.opt-1.pyc',
                 directory / 'example.cpython-311.opt-2.pyc']
        contents = {path: ('cached ' + path.name).encode() for path in paths}
        for path, data in contents.items():
            path.write_bytes(data)
        return contents

    def test_changed_source_cleans_only_its_caches_and_preserves_originals(self):
        caches = self.caches()
        neighbors = [self.root / 'example.pyc.Original.txt',
                     self.root / '__pycache__' / 'example_old.cpython-311.pyc',
                     self.root / '__pycache__' / 'other.cpython-311.pyc',
                     self.root / '__pycache__' / 'example.cpython-311.pyc.Original.txt']
        for path in neighbors:
            path.write_bytes(b'keep this')
        request = self.request()
        native_replace = os.replace

        def replace(source, target):
            self.assertTrue(all(not path.exists() for path in caches))
            self.assertTrue(all(Path(item['backup']).exists() for item in request['files']))
            native_replace(source, target)

        self.run_request(request, replace)
        self.assertEqual(self.py.read_bytes(), b'value = 2\n')
        self.assertEqual(self.py.with_suffix('.ptn.Original.txt').read_bytes(), b'value = 1\n')
        for path in neighbors:
            self.assertEqual(path.read_bytes(), b'keep this')

    def test_later_failure_restores_working_files_and_deleted_caches(self):
        caches = self.caches()
        original_python = self.py.read_bytes()
        original_config = self.cfg.read_bytes()
        native_replace = os.replace

        def replace(source, target):
            if Path(target) == self.cfg:
                raise OSError('simulated later replacement failure')
            native_replace(source, target)

        with self.assertRaisesRegex(OSError, 'later replacement failure'):
            self.run_request(self.request(), replace)
        self.assertEqual(self.py.read_bytes(), original_python)
        self.assertEqual(self.cfg.read_bytes(), original_config)
        self.assertEqual(self.py.with_suffix('.ptn.Original.txt').read_bytes(), original_python)
        for path, data in caches.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(list(self.root.glob('*.uncursed-pending-*.txt')))

    def test_cleanup_only_absent_source_requires_guard_and_creates_no_backup(self):
        self.py.unlink()
        caches = self.caches()
        request = {'files': [], 'python_caches': {str(self.py): None}}
        self.run_request(request)
        self.assertFalse(self.py.exists())
        self.assertTrue(all(not path.exists() for path in caches))
        self.assertFalse(list(self.root.glob('*.Original.txt')))
        caches = self.caches()
        request['python_caches'][str(self.py)] = mods.digest(b'value = 1\n')
        with self.assertRaises(ValueError):
            self.run_request(request)
        for path, data in caches.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse(list(self.root.glob('*.Original.txt')))

    def test_unsafe_cache_entries_block_before_any_working_or_backup_write(self):
        source = self.py.read_bytes()
        cache = self.root / 'example.pyc'
        cache.mkdir()
        sentinel = cache / 'keep.txt'
        sentinel.write_bytes(b'keep this')
        with self.assertRaises(ValueError):
            self.run_request(self.request())
        self.assertEqual(self.py.read_bytes(), source)
        self.assertEqual(sentinel.read_bytes(), b'keep this')
        self.assertFalse(list(self.root.glob('*.Original.txt')))
        sentinel.unlink()
        cache.rmdir()
        cache.write_bytes(b'pretend symlink')
        native_lstat = os.lstat

        def symlink_lstat(path, *args, **kwargs):
            result = native_lstat(path, *args, **kwargs)
            if Path(path) == cache:
                fields = list(result)
                fields[0] = stat.S_IFLNK | stat.S_IMODE(result.st_mode)
                return os.stat_result(fields)
            return result

        with patch('os.lstat', symlink_lstat), self.assertRaises(ValueError):
            self.run_request(self.request())
        self.assertEqual(self.py.read_bytes(), source)
        self.assertEqual(cache.read_bytes(), b'pretend symlink')
        self.assertFalse(list(self.root.glob('*.Original.txt')))


class EngineCacheTests(unittest.TestCase):
    def test_restore_checks_caches_when_sources_are_unchanged_or_already_absent(self):
        for cleared in ([], [screws.PYTHON + 'c']):
            with self.subTest(cleared=cleared):
                current = test_screws.snapshot(False, True)
                current.data = test_mods.SOURCE
                current.sha256 = mods.digest(current.data)
                current.original = current.data
                current.original_hash = current.sha256
                with patch.object(engine, 'scan', return_value=current), \
                        patch.object(engine.network, 'require_idle'), \
                        patch.object(engine.network, 'status', return_value=current.status), \
                        patch.object(engine, 'Remote') as remote:
                    command = remote.return_value.__enter__.return_value.python
                    command.return_value = {'saved': [], 'cleared': cleared}
                    result = engine.apply(Connection('192.0.2.1'), Path('unused'), current, None, restore=True)
                script, request = command.call_args.args
                self.assertEqual(script, APPLY_BATCH)
                self.assertEqual(request['files'], [])
                self.assertEqual(request['python_caches'], {mods.TARGET: current.sha256, screws.PYTHON: None})
                self.assertEqual(request['guards'][mods.TARGET], current.sha256)
                self.assertIsNone(request['guards'][screws.PYTHON])
                self.assertEqual(request['guards'][mods.ORIGINAL], current.original_hash)
                self.assertEqual(result['written'], bool(cleared))
                self.assertEqual(result.get('power_cycle', False), bool(cleared))


if __name__ == '__main__':
    unittest.main()
