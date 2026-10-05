"""Minimal migration prompts and lock placement."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wpi import cli


class MigrationCliTests(unittest.TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        self.manager.sites.return_value = [dict(id='123456abcdef', primary='example.com', status='active')]
        self.report = dict(host='192.0.2.10', backup='/var/backups/wpi/migrations/id', domains=['example.com'])

    def test_password_argument_is_not_exposed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.parser().parse_args(['migrate', '--password', 'Secret'])

    def test_all_visible_prompts_and_hidden_password_are_outside_operation_lock(self):
        held = False

        @contextlib.contextmanager
        def lock():
            nonlocal held
            self.assertFalse(held)
            held = True
            try:
                yield
            finally:
                held = False

        answers = iter(['192.0.2.10', 'root', 'MIGRASI'])

        def prompt(*args):
            self.assertFalse(held)
            return next(answers)

        def hidden(*args):
            self.assertFalse(held)
            return 'SecretSSH'

        def migrate(*args, **kwargs):
            self.assertTrue(held)
            return self.report

        with mock.patch.object(cli, 'Migration') as factory, \
             mock.patch.object(cli, 'operation_lock', side_effect=lock) as acquired, \
             mock.patch('builtins.input', side_effect=prompt), \
             mock.patch.object(cli.getpass, 'getpass', side_effect=hidden) as password, \
             contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.migrate.side_effect = migrate
            cli.migrate_interactive(self.manager)
        password.assert_called_once()
        acquired.assert_called_once()
        factory.return_value.migrate.assert_called_once_with('192.0.2.10', 'root', 'SecretSSH', port=22)

    def test_empty_server_password_does_not_start_migration(self):
        with mock.patch.object(cli, 'Migration') as factory, \
             mock.patch('builtins.input', side_effect=['192.0.2.10', 'root']), \
             mock.patch.object(cli.getpass, 'getpass', return_value=''), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            cli.migrate_interactive(self.manager)
        factory.assert_not_called()

    def test_cancel_does_not_acquire_lock(self):
        with mock.patch.object(cli, 'operation_lock') as lock, \
             mock.patch('builtins.input', side_effect=['192.0.2.10', 'root', 'BATAL']), \
             mock.patch.object(cli.getpass, 'getpass', return_value='SecretSSH'), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            cli.migrate_interactive(self.manager)
        lock.assert_not_called()

    def test_migration_status_is_read_only_when_operation_busy(self):
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=self.manager), \
             mock.patch.object(cli, 'Migration') as source, \
             mock.patch.object(cli, 'TargetMigration') as target, \
             mock.patch.object(cli, 'operation_lock', side_effect=ValueError('busy')) as lock, \
             contextlib.redirect_stdout(io.StringIO()):
            source.return_value.status.return_value = []
            target.return_value.status.return_value = []
            self.assertEqual(cli.main(['migration-status']), 0)
        lock.assert_not_called()

    def test_ssl_timer_and_import_each_use_one_mutation_lock(self):
        for arguments, action in [(['migration-ssl-tick'], 'ssl_tick'),
                                  (['migration-import', '/tmp/migration.zip', '--sha256', 'a' * 64,
                                    '--migration-id', 'b' * 32], 'import_bundle')]:
            with self.subTest(command=arguments[0]), \
                 mock.patch.object(cli.sys, 'platform', 'linux'), \
                 mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
                 mock.patch.object(cli, 'Manager', return_value=self.manager), \
                 mock.patch.object(cli, 'TargetMigration') as target, \
                 mock.patch.object(cli, 'operation_lock', return_value=contextlib.nullcontext()) as lock, \
                 contextlib.redirect_stdout(io.StringIO()):
                getattr(target.return_value, action).return_value = {'status': 'ready'}
                self.assertEqual(cli.main(arguments), 0)
                lock.assert_called_once()
                getattr(target.return_value, action).assert_called_once()


if __name__ == '__main__':
    unittest.main()
