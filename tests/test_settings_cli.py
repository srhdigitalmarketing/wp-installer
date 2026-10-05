"""User-triggered repair/settings dispatch, prompt boundaries and exit status."""
import contextlib
import io
import unittest
from unittest import mock

from wpi import cli


SETTINGS = {'scope': 'server', 'manual': {'memory_limit_mb': 500, 'upload_max_filesize_mb': 8},
            'effective': {'memory_mib': 500, 'upload_mib': 8, 'post_mib': 16, 'opcache_mib': 256},
            'capacity': 30}
HEALTHY = {'primary': 'example.com', 'status': 'healthy', 'checks': {'config_syntax': True}}
UNRESOLVED = {'primary': 'example.com', 'status': 'unresolved',
              'checks': {'config_syntax': False, 'frontend': {'status': 500, 'scheme': 'https', 'ok': False}}}


class SettingsAndRepairCliTests(unittest.TestCase):
    def setUp(self):
        self.manager = mock.Mock()
        self.manager.config = {'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3'}
        self.manager.php_settings_status.return_value = SETTINGS
        self.manager.set_php_settings.return_value = SETTINGS
        self.manager.repair_site.return_value = HEALTHY

    @contextlib.contextmanager
    def main_environment(self):
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            yield output, errors

    def test_status_and_check_only_remain_available_while_mutation_lock_is_busy(self):
        for arguments in (['php-settings'], ['repair', 'example.com', '--check']):
            with self.subTest(command=arguments), self.main_environment(), \
                 mock.patch.object(cli, 'operation_lock', side_effect=ValueError('occupied')) as lock:
                self.assertEqual(cli.main(arguments), 0)
                lock.assert_not_called()
        self.manager.php_settings_status.assert_called_once_with()
        self.manager.repair_site.assert_called_once_with('example.com', check_only=True)
        self.manager.set_php_settings.assert_not_called()

    def test_each_mutation_holds_one_lock_and_releases_it_before_returning(self):
        cases = [(['php-settings', '--memory-limit', '500'], 'set_php_settings', SETTINGS,
                  {'memory_limit': '500', 'upload_max_filesize': None, 'reset': False}),
                 (['php-settings', '--upload-max-filesize', '256M'], 'set_php_settings', SETTINGS,
                  {'memory_limit': None, 'upload_max_filesize': '256M', 'reset': False}),
                 (['php-settings', '--reset'], 'set_php_settings', SETTINGS,
                  {'memory_limit': None, 'upload_max_filesize': None, 'reset': True}),
                 (['repair', 'example.com'], 'repair_site', {**HEALTHY, 'status': 'resolved'}, {})]
        for arguments, action, report, kwargs in cases:
            held, events = [False], []

            @contextlib.contextmanager
            def lock():
                self.assertFalse(held[0])
                held[0] = True
                events.append('enter')
                try:
                    yield
                finally:
                    held[0] = False
                    events.append('exit')

            def mutation(*args, **values):
                self.assertTrue(held[0])
                self.assertEqual(values, kwargs)
                if action == 'repair_site':
                    self.assertEqual(args, ('example.com',))
                return report

            with self.subTest(command=arguments), self.main_environment(), \
                 mock.patch.object(cli, 'operation_lock', side_effect=lock) as acquired, \
                 mock.patch.object(self.manager, action, side_effect=mutation) as invoked:
                self.assertEqual(cli.main(arguments), 0)
                self.assertFalse(held[0])
                self.assertEqual(events, ['enter', 'exit'])
                acquired.assert_called_once()
                invoked.assert_called_once()

    def test_operation_error_releases_lock_and_returns_failure_without_claiming_success(self):
        events = []

        @contextlib.contextmanager
        def lock():
            events.append('enter')
            try:
                yield
            finally:
                events.append('exit')

        self.manager.set_php_settings.side_effect = RuntimeError('Konfigurasi lama dipulihkan.')
        with self.main_environment() as (output, errors), \
             mock.patch.object(cli, 'operation_lock', side_effect=lock):
            self.assertEqual(cli.main(['php-settings', '--memory-limit', '500']), 1)
        self.assertEqual(events, ['enter', 'exit'])
        self.assertIn('Gagal: Konfigurasi lama dipulihkan.', errors.getvalue())
        self.assertNotIn('Memory: 500M', output.getvalue())

    def test_busy_mutation_never_executes_manager_action(self):
        with self.main_environment() as (_, errors), \
             mock.patch.object(cli, 'operation_lock', side_effect=ValueError('Operasi masih berjalan')):
            self.assertEqual(cli.main(['repair', 'example.com']), 1)
        self.manager.repair_site.assert_not_called()
        self.assertIn('Operasi masih berjalan', errors.getvalue())

    def test_unresolved_repair_and_diagnosis_return_exit_one(self):
        self.manager.repair_site.return_value = UNRESOLVED
        for suffix in ([], ['--check']):
            with self.subTest(check_only=bool(suffix)), self.main_environment() as (output, _), \
                 mock.patch.object(cli, 'operation_lock', return_value=contextlib.nullcontext()) as lock:
                self.assertEqual(cli.main(['repair', 'example.com', *suffix]), 1)
                self.assertIn('Belum terselesaikan', output.getvalue())
                self.assertIn('HTTP 500', output.getvalue())
                self.assertIn('Database dan konten tidak di-restore', output.getvalue())
                self.assertEqual(lock.call_count, 0 if suffix else 1)

    def test_reset_cannot_be_combined_with_either_limit_and_rejects_before_lock(self):
        for flag in ('--memory-limit', '--upload-max-filesize'):
            with self.subTest(flag=flag), self.main_environment() as (_, errors), \
                 mock.patch.object(cli, 'operation_lock') as lock:
                self.assertEqual(cli.main(['php-settings', '--reset', flag, '500']), 1)
                self.assertIn('--reset tidak digabung', errors.getvalue())
                lock.assert_not_called()
        self.manager.set_php_settings.assert_not_called()

    def test_parser_accepts_both_limits_together_and_auto_reset_of_individual_field(self):
        args = cli.parser().parse_args(['php-settings', '--memory-limit', '500M',
                                        '--upload-max-filesize', 'auto'])
        self.assertEqual((args.memory_limit, args.upload_max_filesize, args.reset), ('500M', 'auto', False))
        args = cli.parser().parse_args(['repair', 'example.com', '--check'])
        self.assertTrue(args.check)

    def run_menu(self, answers, expected_kwargs):
        held, events = [False], []
        choices = iter(answers)

        @contextlib.contextmanager
        def lock():
            held[0] = True
            events.append('enter')
            try:
                yield
            finally:
                held[0] = False
                events.append('exit')

        def prompt(label):
            self.assertFalse(held[0], label)
            events.append('prompt')
            return next(choices)

        def mutation(**kwargs):
            self.assertTrue(held[0])
            self.assertEqual(kwargs, expected_kwargs)
            return SETTINGS

        with mock.patch('builtins.input', side_effect=prompt), \
             mock.patch.object(cli, 'operation_lock', side_effect=lock) as acquired, \
             mock.patch.object(self.manager, 'set_php_settings', side_effect=mutation), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.menu(self.manager)
        self.assertFalse(held[0])
        self.assertIn('(17) PHP memory limit & upload size', output.getvalue())
        acquired.assert_called_once()
        self.assertLess(events.index('enter'), events.index('exit'))
        self.assertNotIn('prompt', events[events.index('enter') + 1:events.index('exit')])

    def test_menu_17_preserves_manual_defaults_and_all_prompts_precede_lock(self):
        self.run_menu(['17', '1', '', '', '', '0'],
                      {'memory_limit': '500', 'upload_max_filesize': '8'})

    def test_menu_17_reset_requests_no_manual_limit_and_prompt_outside_lock(self):
        self.run_menu(['17', '2', '', '0'], {'reset': True})

    def test_invalid_interactive_mode_never_acquires_mutation_lock(self):
        with mock.patch('builtins.input', return_value='9'), \
             mock.patch.object(cli, 'operation_lock') as lock, \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            cli.php_settings_interactive(self.manager)
        lock.assert_not_called()
        self.manager.set_php_settings.assert_not_called()


if __name__ == '__main__':
    unittest.main()
