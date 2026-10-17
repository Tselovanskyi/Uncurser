"""Batched scan reads and response limits, using local files and fake SSH streams."""
import base64
import contextlib
import io
import json
import shlex
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.mods import digest
from app.remote import CONFIG_TREE, Connection, READ, READ_MANY, Remote


def local_command(program, payload):
    output = io.StringIO()
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(output):
        exec(compile(program, "<local-read-check>", "exec"), {})
    return json.loads(output.getvalue())


class Channel:
    def __init__(self, code=0):
        self.code, self.closed = code, False
    def shutdown_write(self):
        pass
    def recv_exit_status(self):
        return self.code
    def close(self):
        self.closed = True


class Client:
    def __init__(self, output, error=b"", code=0):
        self.channel = Channel(code)
        self.input = io.StringIO()
        self.output = io.BytesIO(output)
        self.error = io.BytesIO(error)
        for stream in (self.input, self.output, self.error):
            stream.channel = self.channel
        self.calls = []
    def exec_command(self, command, timeout):
        self.calls.append((command, timeout))
        return self.input, self.output, self.error


def remote_with(client=None):
    remote = Remote.__new__(Remote)
    remote.client = client
    remote.connection = Connection("192.0.2.1")
    remote._read_cache = {}
    return remote


class RemoteScanTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / "build" / "file-checks"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=parent)
        self.root = Path(self.directory.name).resolve()
        self.assertTrue(self.root.is_relative_to(parent.resolve()))

    def tearDown(self):
        self.directory.cleanup()

    def test_batch_preserves_good_reads_and_isolates_unsafe_paths(self):
        good, missing = self.root / "good.cfg", self.root / "missing.cfg"
        good.write_bytes(b"[printer]\nkinematics: corexy\n")
        large = self.root / "large.cfg"
        large.write_bytes(b"x" * (2097152 + 1))
        paths = [str(good), str(missing), str(self.root), str(large)]
        link = self.root / "linked.cfg"
        try:
            link.symlink_to(good)
        except OSError:
            link = None
        if link:
            paths.append(str(link))
        records = local_command(READ_MANY, {"paths": paths})
        self.assertEqual(base64.b64decode(records[str(good)]["data"]), good.read_bytes())
        self.assertEqual(records[str(good)]["sha256"], digest(good.read_bytes()))
        self.assertTrue(all(isinstance(records[str(good)][key], int) for key in ("mode", "uid", "gid")))
        self.assertIsNone(records[str(missing)])
        self.assertIn("regular file", records[str(self.root)]["error"])
        self.assertIn("supported size", records[str(large)]["error"])
        if link:
            self.assertIn("regular file", records[str(link)]["error"])

    def test_config_tree_allows_only_the_owned_external_helper_path(self):
        config = self.root / "config"
        config.mkdir()
        root = config / "printer.cfg"
        helper = self.root / "screws_tilt_adjust.cfg"
        helper.write_text("[screws_tilt_adjust]\nscrew1: 10, 10\n", encoding="utf-8")
        program = CONFIG_TREE.replace('"/usr/share/klipper/klippy/extras/screws_tilt_adjust.cfg"', repr(str(helper)))
        root.write_text("[include " + str(helper) + "]\n[printer]\nkinematics: corexy\n", encoding="utf-8")
        result = local_command(program, {"path": str(root)})
        self.assertEqual(set(result["files"]), {str(root), str(helper)})
        unrelated = self.root / "other.cfg"
        unrelated.write_bytes(helper.read_bytes())
        root.write_text("[include " + str(unrelated) + "]\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside"):
            local_command(program, {"path": str(root)})

    def test_prefetch_cache_is_fresh_per_call_and_unknown_paths_use_normal_read(self):
        good, missing, extra = (self.root / name for name in ("good.cfg", "missing.cfg", "extra.cfg"))
        good.write_bytes(b"first\n")
        extra.write_bytes(b"uncached\n")
        remote = remote_with()
        remote.python = Mock(side_effect=lambda program, payload, **kwargs: local_command(program, payload))
        remote.prefetch([str(good), str(missing), str(self.root), str(good)])
        self.assertEqual(remote.python.call_count, 1)
        self.assertEqual(remote.python.call_args.args[1]["paths"], [str(good), str(missing), str(self.root)])
        self.assertEqual(remote.read(str(good))["data"], b"first\n")
        self.assertIsNone(remote.read(str(missing)))
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            remote.read(str(self.root))
        good.write_bytes(b"second\n")
        missing.write_bytes(b"now present\n")
        self.assertEqual(remote.read(str(good))["data"], b"first\n")
        self.assertIsNone(remote.read(str(missing)))
        self.assertEqual(remote.read(str(extra))["data"], b"uncached\n")
        self.assertEqual(remote.python.call_args.args[0], READ)
        remote.prefetch([str(good), str(missing)])
        self.assertEqual(remote.read(str(good))["data"], b"second\n")
        self.assertEqual(remote.read(str(missing))["data"], b"now present\n")
        self.assertEqual(set(remote._read_cache), {str(good), str(missing)})

    def test_large_prefetch_response_is_read_completely_above_eight_mib(self):
        paths = []
        for index in range(4):
            path = self.root / f"bundle-{index}.txt"
            path.write_bytes(bytes([65 + index]) * 2097152)
            paths.append(str(path))
        records = local_command(READ_MANY, {"paths": paths})
        output = json.dumps(records).encode()
        self.assertGreater(len(output), 8 * 1024 * 1024)
        client = Client(output)
        remote = remote_with(client)
        remote.prefetch(paths)
        self.assertEqual(len(client.calls), 1)
        self.assertTrue(client.channel.closed)
        for path in paths:
            self.assertEqual(remote.read(path)["data"], Path(path).read_bytes())

    def test_output_limit_raises_clearly_and_command_errors_keep_the_last_message(self):
        oversized = json.dumps({"blob": "x" * (8 * 1024 * 1024)}).encode()
        client = Client(oversized)
        with self.assertRaisesRegex(RuntimeError, "output exceeds"):
            remote_with(client).python("unused")
        self.assertTrue(client.channel.closed)
        client = Client(b"", error=b"Traceback details\nValueError: original is damaged\n", code=1)
        with self.assertRaisesRegex(RuntimeError, "ValueError: original is damaged"):
            remote_with(client).python("unused")
        self.assertTrue(client.channel.closed)

    def test_large_program_and_nested_payload_use_in_memory_stdin_envelope(self):
        payload = {'message': 'quotes \' " ` $()\nUnicode: \u00e9 \u041f\u0440\u0438\u043d\u0442\u0435\u0440 \u2665',
                   'nested': [{'lines': ['first\nsecond', '\\literal'], 'count': 42}]}
        program = ('# source quotes \' " and Unicode \u00e9\n' * 400
                   + 'import json, sys\nvalue = json.load(sys.stdin)\n'
                     'print(json.dumps(value, ensure_ascii=False))\n')
        self.assertGreater(len(program.encode('utf-8')), 9 * 1024)
        client = Client(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        self.assertEqual(remote_with(client).python(program, payload), payload)
        command = shlex.split(client.calls[0][0])
        self.assertEqual(command[:3], ['python3', '-B', '-c'])
        self.assertEqual(len(command), 4)
        self.assertLess(len(client.calls[0][0]), 512)
        envelope = client.input.getvalue()
        source, data = envelope.split('\n', 1)
        self.assertEqual(json.loads(source), program)
        self.assertEqual(json.loads(data), payload)
        output = io.StringIO()
        before = set(self.root.iterdir())
        with patch('sys.stdin', io.StringIO(envelope)), contextlib.redirect_stdout(output), \
                patch('builtins.open', side_effect=AssertionError('transport created a file')):
            exec(compile(command[3], '<transport-bootstrap-check>', 'exec'), {})
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertEqual(set(self.root.iterdir()), before)
        self.assertTrue(client.channel.closed)

    def test_command_is_constant_for_small_and_large_programs_and_default_payload(self):
        commands = []
        base_program = 'import json,sys\nprint(json.dumps(json.load(sys.stdin)))\n'
        for program in (base_program, '#' + 'x' * 20000 + '\n' + base_program):
            client = Client(b'{}')
            self.assertEqual(remote_with(client).python(program), {})
            commands.append(client.calls[0][0])
            source, data = client.input.getvalue().split('\n', 1)
            self.assertEqual(json.loads(source), program)
            self.assertEqual(json.loads(data), {})
        self.assertEqual(commands[0], commands[1])
        self.assertLess(len(commands[0]), 512)

    def test_textless_ssh_timeouts_and_eof_report_the_operation_and_preserve_cleanup(self):
        for error_type in (TimeoutError, EOFError):
            for operation in ("connect", "session", "upload", "stdout", "stderr", "exit status"):
                with self.subTest(error=error_type.__name__, operation=operation):
                    error = error_type()
                    client = Client(b'{}')
                    remote = remote_with(client)
                    if operation == "connect":
                        client.connect = Mock(side_effect=error)
                        client.close = Mock()
                        call = remote.__enter__
                        expected = "connecting"
                    else:
                        call = lambda: remote.python("unused")
                        if operation == "session":
                            client.exec_command = Mock(side_effect=error)
                            expected = "opening a command session"
                        elif operation == "upload":
                            client.input.write = Mock(side_effect=error)
                            expected = "sending command data"
                        else:
                            target, method = ((client.output, "read") if operation == "stdout" else
                                              (client.error, "read") if operation == "stderr" else
                                              (client.channel, "recv_exit_status"))
                            setattr(target, method, Mock(side_effect=error))
                            expected = "reading the command response"
                    with self.assertRaisesRegex(RuntimeError, expected) as raised:
                        call()
                    self.assertIs(raised.exception.__cause__, error)
                    self.assertIn("timed out" if error_type is TimeoutError else "closed", str(raised.exception))
                    if operation == "connect":
                        client.close.assert_called_once()
                    elif operation != "session":
                        self.assertTrue(client.channel.closed)

    def test_meaningful_transport_errors_are_preserved(self):
        for error_type in (TimeoutError, EOFError):
            for operation in ("connect", "read"):
                with self.subTest(error=error_type.__name__, operation=operation):
                    error = error_type("Specific printer error")
                    client = Client(b'{}')
                    remote = remote_with(client)
                    if operation == "connect":
                        client.connect = Mock(side_effect=error)
                        client.close = Mock()
                        call = remote.__enter__
                    else:
                        client.output.read = Mock(side_effect=error)
                        call = lambda: remote.python("unused")
                    with self.assertRaises(error_type) as raised:
                        call()
                    self.assertIs(raised.exception, error)
                    self.assertEqual(str(raised.exception), "Specific printer error")

    def test_cached_records_are_cleared_on_reentry_and_exit(self):
        client = Mock()
        remote = remote_with(client)
        remote._read_cache = {"old": None}
        self.assertIs(remote.__enter__(), remote)
        self.assertEqual(remote._read_cache, {})
        remote._read_cache = {"new": None}
        remote.__exit__()
        self.assertEqual(remote._read_cache, {})
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
