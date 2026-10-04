"""Real advisory-lock contention checks on Linux, with no server changes."""

from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from wpi import cli


@unittest.skipUnless(sys.platform == 'linux', 'POSIX flock is used by the Ubuntu installer.')
class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)

    def run_child(self, command):
        # Mock only provisioning and the root requirement. The CLI uses the
        # actual Linux lock file and flock syscall in this separate process.
        script = '''
from pathlib import Path
import sys
from unittest import mock
from wpi import cli
cli.DATA = Path(sys.argv[1])
manager = mock.Mock(config={})
manager.sites.return_value = []
manager.doctor.return_value = []
manager.add_domain.side_effect = lambda *args, **kwargs: print('MUTATION_EXECUTED')
with mock.patch.object(cli, 'Manager', return_value=manager), \
     mock.patch.object(cli.os, 'geteuid', return_value=0), \
     mock.patch('builtins.input', return_value='0'):
    result = cli.main(sys.argv[2:])
sys.exit(result)
'''
        return subprocess.run([sys.executable, '-c', script, str(self.data), *command],
                              capture_output=True, text=True, timeout=10)

    def test_contending_process_is_rejected_then_succeeds_after_owner_releases(self):
        with mock.patch.object(cli, 'DATA', self.data):
            with cli.operation_lock():
                path = self.data / 'operation.lock'
                original_inode = path.stat().st_ino
                busy = self.run_child(['add-domain', 'old.example.com', 'alias.example.com'])
                self.assertEqual(busy.returncode, 1, busy.stdout + busy.stderr)
                self.assertIn('Operasi WPI lain sedang berjalan', busy.stderr)
                self.assertIn(str(os.getpid()), busy.stderr)
                self.assertIn('lslocks', busy.stderr)
                self.assertNotIn('MUTATION_EXECUTED', busy.stdout)
                self.assertEqual(path.stat().st_ino, original_inode)
            available = self.run_child(['add-domain', 'old.example.com', 'alias.example.com'])
        self.assertEqual(available.returncode, 0, available.stdout + available.stderr)
        self.assertIn('MUTATION_EXECUTED', available.stdout)
        self.assertTrue(path.exists(), 'Releasing a lock must not unlink the shared lock file.')
        self.assertEqual(path.stat().st_ino, original_inode)

    def test_an_occupied_operation_lock_does_not_block_menu_or_read_only_commands(self):
        with mock.patch.object(cli, 'DATA', self.data), cli.operation_lock():
            for command in ([], ['menu'], ['list'], ['status'], ['lock-status']):
                with self.subTest(command=command):
                    result = self.run_child(command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn('MUTATION_EXECUTED', result.stdout)

    def test_lock_status_reports_actual_owner_while_lock_is_held(self):
        with mock.patch.object(cli, 'DATA', self.data), cli.operation_lock():
            result = self.run_child(['lock-status'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            status = json.loads(result.stdout)
            self.assertIs(status['locked'], True)
            self.assertEqual(status['lock_file'], str(self.data / 'operation.lock'))
            self.assertTrue(any(holder['pid'] == os.getpid() and holder['mode'] == 'WRITE'
                                for holder in status['holders']), status)
        released = self.run_child(['lock-status'])
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        released_status = json.loads(released.stdout)
        self.assertIsNone(released_status['locked'])
        self.assertEqual(released_status['holders'], [])
        self.assertEqual(released_status['reason'], 'no-visible-owner')

    def test_lock_is_released_when_an_operation_raises(self):
        with mock.patch.object(cli, 'DATA', self.data):
            with self.assertRaisesRegex(RuntimeError, 'operation failed'):
                with cli.operation_lock():
                    raise RuntimeError('operation failed')
            with cli.operation_lock():
                self.assertTrue((self.data / 'operation.lock').exists())

    def test_abrupt_owner_exit_releases_lock_without_deleting_lock_file(self):
        script = '''
import os
from pathlib import Path
import sys
from wpi import cli
cli.DATA = Path(sys.argv[1])
with cli.operation_lock():
    os._exit(7)
'''
        crashed = subprocess.run([sys.executable, '-c', script, str(self.data)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(crashed.returncode, 7, crashed.stdout + crashed.stderr)
        path = self.data / 'operation.lock'
        original_inode = path.stat().st_ino
        available = self.run_child(['add-domain', 'old.example.com', 'alias.example.com'])
        self.assertEqual(available.returncode, 0, available.stdout + available.stderr)
        self.assertIn('MUTATION_EXECUTED', available.stdout)
        self.assertEqual(path.stat().st_ino, original_inode)


if __name__ == '__main__':
    unittest.main()
