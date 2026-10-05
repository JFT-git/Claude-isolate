"""A flaky external connection must neither fake success nor remove CI gates."""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from windows.integration import PROBE


class WindowsProbeTests(unittest.TestCase):
    def probe(self, responses):
        # Run the exact guest retry function, without booting or opening sockets.
        tree = ast.parse(PROBE)
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'public_pair']
        pool = Mock()
        pool.__enter__ = Mock(return_value=pool)
        pool.__exit__ = Mock(return_value=False)
        pool.map.side_effect = responses
        executor = Mock(return_value=pool)
        output = Mock()
        delay = Mock()
        namespace = {'concurrent': types.SimpleNamespace(
            futures=types.SimpleNamespace(ThreadPoolExecutor=executor)),
            'public_https': Mock(), 'print': output, 'time': types.SimpleNamespace(sleep=delay)}
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<guest probe>', 'exec'), namespace)
        return namespace['public_pair'], output, delay, executor

    def test_success_requires_both_connections_in_one_attempt(self):
        def incomplete():
            yield 0
            raise RuntimeError('Network lease closed before second connection')
        probe, output, delay, executor = self.probe([incomplete(), [0, 1]])
        probe()
        successful = [call.args for call in output.call_args_list if call.args[0].endswith('PUBLIC-HTTPS-OK')]
        self.assertEqual(successful, [('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK', 0),
                                      ('WINDOWS-INTEGRATION: PUBLIC-HTTPS-OK', 1)])
        self.assertEqual(executor.call_count, 2)
        delay.assert_called_once_with(5)

    def test_persistent_failure_still_fails_after_bounded_attempts(self):
        probe, output, delay, executor = self.probe([OSError('unavailable')] * 3)
        with self.assertRaises(OSError):
            probe()
        self.assertFalse(any(call.args[0].endswith('PUBLIC-HTTPS-OK') for call in output.call_args_list))
        self.assertEqual(executor.call_count, 3)
        self.assertEqual(delay.call_count, 2)

    def test_first_success_does_not_retry(self):
        probe, output, delay, executor = self.probe([[0, 1]])
        probe()
        self.assertEqual(executor.call_count, 1)
        self.assertEqual(output.call_count, 2)
        delay.assert_not_called()


if __name__ == '__main__':
    unittest.main()
