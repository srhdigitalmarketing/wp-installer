"""Panel and command regression tests without requiring root or Linux services."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wpi import cli, core


class CliTests(unittest.TestCase):
    def test_help_is_available_without_root_and_does_not_create_manager(self):
        with mock.patch.object(cli, "Manager") as manager, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        manager.assert_not_called()

    def test_parser_has_no_password_argument(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                cli.parser().parse_args(["install", "example.com", "--password", "SecretPassword123!"])
        self.assertEqual(result.exception.code, 2)

    def test_nonlinux_execution_cannot_modify_server(self):
        with mock.patch.object(cli.sys, "platform", "win32"), \
             mock.patch.object(cli, "Manager") as manager, \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            result = cli.main(["setup"])
        self.assertEqual(result, 1)
        self.assertIn("sudo wpi", errors.getvalue())
        manager.assert_not_called()

    def test_password_is_requested_hidden_and_confirmed(self):
        with mock.patch.object(cli.getpass, "getpass", side_effect=["SecretPassword123!", "SecretPassword123!"]) as hidden, \
             mock.patch("builtins.input") as visible:
            self.assertEqual(cli.password_prompt(), "SecretPassword123!")
        self.assertEqual(hidden.call_count, 2)
        visible.assert_not_called()

    def test_empty_password_requests_generation(self):
        with mock.patch.object(cli.getpass, "getpass", return_value="") as hidden:
            self.assertIsNone(cli.password_prompt())
        hidden.assert_called_once()

    def test_password_mismatch_rejected(self):
        with mock.patch.object(cli.getpass, "getpass", side_effect=["first", "second"]):
            with self.assertRaisesRegex(ValueError, "tidak cocok"):
                cli.password_prompt()

    def test_empty_required_prompt_is_rejected(self):
        with mock.patch("builtins.input", return_value=""):
            with self.assertRaises(ValueError):
                cli.ask("Domain")
            self.assertEqual(cli.ask("Judul", "WordPress"), "WordPress")

    def run_main(self, arguments, manager, answer=None):
        with mock.patch.object(cli.sys, "platform", "linux"), \
             mock.patch.object(cli.os, "geteuid", return_value=0, create=True), \
             mock.patch.object(cli, "Manager", return_value=manager), \
             mock.patch.object(cli, "operation_lock", return_value=contextlib.nullcontext()), \
             mock.patch("builtins.input", return_value=answer or ""), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            return cli.main(arguments)

    def test_pma_delete_dispatches_only_panel_removal(self):
        manager = mock.Mock()
        result = self.run_main(["pma-delete"], manager, "HAPUS PANEL")
        self.assertEqual(result, 0)
        self.assertEqual(manager.mock_calls, [mock.call.remove_pma()])

    def test_background_autotune_runs_while_panel_operation_lock_is_occupied(self):
        manager = mock.Mock()
        manager.autotune_tick.return_value = {'enabled': True, 'max_children': 8}
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', side_effect=ValueError('occupied')) as lock, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['autotune-tick']), 0)
        lock.assert_not_called()
        manager.autotune_tick.assert_called_once_with()

    def test_autotune_status_only_reads_controller_status(self):
        manager = mock.Mock()
        manager.autotune_status.return_value = {'enabled': True}
        self.assertEqual(self.run_main(['autotune-status'], manager), 0)
        self.assertEqual(manager.mock_calls, [mock.call.autotune_status()])

    def test_autotune_upgrade_activation_has_no_interactive_prompt(self):
        manager = mock.Mock()
        manager.enable_autotune.return_value = {'enabled': True}
        self.assertEqual(self.run_main(['autotune-enable'], manager), 0)
        self.assertEqual(manager.mock_calls, [mock.call.enable_autotune()])

    def test_pma_delete_wrong_token_cancels_before_mutation(self):
        manager = mock.Mock()
        result = self.run_main(["pma-delete"], manager, "wrong")
        self.assertEqual(result, 1)
        manager.remove_pma.assert_not_called()

    def test_primary_change_dispatches_selected_site_and_new_host(self):
        manager = mock.Mock()
        manager.change_primary.return_value = ({"primary": "new.example.com"}, Path("/backup"))
        result = self.run_main(["change-domain", "old.example.com", "new.example.com"],
                               manager, "new.example.com")
        self.assertEqual(result, 0)
        manager.change_primary.assert_called_once_with("old.example.com", "new.example.com")

    def test_secondary_add_does_not_change_primary(self):
        manager = mock.Mock()
        result = self.run_main(["add-domain", "example.com", "alias.example.com"], manager)
        self.assertEqual(result, 0)
        self.assertEqual(manager.mock_calls, [mock.call.add_secondary("example.com", "alias.example.com")])

    def test_existing_stack_does_not_reprompt_setup(self):
        manager = mock.Mock(config={"stack": "apache", "database": "mysql"})
        with mock.patch("builtins.input") as visible:
            cli.configure(manager)
        manager.setup.assert_not_called()
        visible.assert_not_called()

    def test_setup_stack_and_database_are_selected_explicitly(self):
        manager = mock.Mock(config={})
        with mock.patch("builtins.input", side_effect=["2", "2"]), \
             mock.patch.object(cli, 'operation_lock', return_value=contextlib.nullcontext()), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.configure(manager)
        manager.setup.assert_called_once_with("apache", "mysql")

    def test_invalid_stack_selection_has_no_mutation(self):
        manager = mock.Mock(config={})
        with mock.patch("builtins.input", side_effect=["9", "1"]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                cli.configure(manager)
        manager.setup.assert_not_called()

    def test_select_site_without_sites_cannot_continue(self):
        manager = mock.Mock()
        manager.sites.return_value = []
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            cli.select_site(manager)

    def test_menu_has_required_operations_and_exit(self):
        manager = mock.Mock(config={})
        with mock.patch("builtins.input", return_value="0"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            cli.menu(manager)
        text = output.getvalue()
        for label in ["Install WordPress", "Add domain secondary", "Change domain primary",
                      "Install phpMyAdmin", "Delete panel phpMyAdmin", "Backup", "Restore", "Keluar"]:
            self.assertIn(label, text)
        manager.setup.assert_not_called()

    def test_opening_idle_menu_does_not_acquire_operation_lock(self):
        manager = mock.Mock(config={})
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', side_effect=ValueError('occupied')) as lock, \
             mock.patch('builtins.input', return_value='0'), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([]), 0)
        lock.assert_not_called()

    def test_read_only_commands_work_during_another_operation(self):
        for command in ('list', 'status'):
            with self.subTest(command=command):
                manager = mock.Mock()
                manager.sites.return_value = []
                manager.doctor.return_value = ['Services available']
                with mock.patch.object(cli.sys, 'platform', 'linux'), \
                     mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
                     mock.patch.object(cli, 'Manager', return_value=manager), \
                     mock.patch.object(cli, 'operation_lock', side_effect=ValueError('occupied')) as lock, \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main([command]), 0)
                lock.assert_not_called()

    def test_scripted_mutation_locks_only_after_confirmation(self):
        manager = mock.Mock()
        manager.change_primary.return_value = ({'primary': 'new.example.com'}, Path('/backup'))
        locked = False
        acquisitions = []

        @contextlib.contextmanager
        def operation():
            nonlocal locked
            self.assertFalse(locked)
            acquisitions.append(True)
            locked = True
            try:
                yield
            finally:
                locked = False

        def answer(prompt):
            self.assertFalse(locked, 'An unanswered prompt must not block another panel.')
            return 'new.example.com'

        def change(*args):
            self.assertTrue(locked, 'A domain change must be serialized with other mutations.')
            return {'primary': 'new.example.com'}, Path('/backup')

        manager.change_primary.side_effect = change
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', side_effect=operation), \
             mock.patch('builtins.input', side_effect=answer), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['change-domain', 'old.example.com', 'new.example.com']), 0)
        self.assertEqual(len(acquisitions), 1)
        self.assertFalse(locked)

    def test_initial_setup_and_wordpress_install_share_one_operation_lock(self):
        manager = mock.Mock(config={}, data=Path('/var/lib/wpi'))
        site = {'id': 'abcdef123456', 'primary': 'new.example.com'}
        locked = False
        operations = []

        @contextlib.contextmanager
        def operation():
            nonlocal locked
            self.assertFalse(locked)
            operations.append('acquire')
            locked = True
            try:
                yield
            finally:
                locked = False
                operations.append('release')

        def setup(stack, database):
            self.assertTrue(locked)
            self.assertEqual((stack, database), ('nginx', 'mariadb'))
            operations.append('setup')

        def install(host, email, title, admin, password):
            self.assertTrue(locked)
            self.assertEqual((host, email, title, admin, password),
                             ('new.example.com', 'owner@example.com', 'WordPress', 'wpadmin', None))
            operations.append('install')
            return site, 'GeneratedPassword'

        replies = iter(['new.example.com', 'owner@example.com', '', '', '1', '1'])

        def answer(prompt):
            self.assertFalse(locked, 'Initial configuration questions must not hold the operation lock.')
            return next(replies)

        def hidden(prompt):
            self.assertFalse(locked, 'Password input must finish before lock acquisition.')
            return ''

        manager.setup.side_effect = setup
        manager.install.side_effect = install
        with mock.patch.object(cli, 'operation_lock', side_effect=operation), \
             mock.patch('builtins.input', side_effect=answer), \
             mock.patch.object(cli.getpass, 'getpass', side_effect=hidden), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.install_interactive(manager)
        self.assertEqual(operations, ['acquire', 'setup', 'install', 'release'])
        self.assertFalse(locked)

    def test_menu_mutation_releases_lock_before_returning_to_menu(self):
        manager = mock.Mock(config={})
        site = {'id': 'abcdef123456', 'primary': 'old.example.com',
                'secondary': [], 'status': 'active'}
        manager.sites.return_value = [site]
        manager.site.return_value = site
        locked = False
        acquired = []

        @contextlib.contextmanager
        def operation():
            nonlocal locked
            self.assertFalse(locked)
            acquired.append(True)
            locked = True
            try:
                yield
            finally:
                locked = False

        def add(identifier, domain):
            self.assertTrue(locked)
            self.assertEqual((identifier, domain), ('abcdef123456', 'alias.example.com'))
            return site

        replies = iter(['3', '', 'alias.example.com', '', '2', '', '0'])

        def answer(prompt):
            self.assertFalse(locked, 'Menu input and confirmation must not own the mutation lock.')
            return next(replies)

        manager.add_secondary.side_effect = add
        with mock.patch.object(cli, 'operation_lock', side_effect=operation), \
             mock.patch('builtins.input', side_effect=answer), \
             contextlib.redirect_stdout(io.StringIO()):
            cli.menu(manager)
        self.assertEqual(len(acquired), 1)
        self.assertFalse(locked)
        manager.add_secondary.assert_called_once()

    def test_menu_busy_operation_can_be_retried_without_reopening_panel(self):
        manager = mock.Mock(config={})
        site = {'id': 'abcdef123456', 'primary': 'old.example.com',
                'secondary': [], 'status': 'active'}
        manager.sites.return_value = [site]
        manager.site.return_value = site
        manager.add_secondary.return_value = site
        acquisitions = 0

        @contextlib.contextmanager
        def operation():
            nonlocal acquisitions
            acquisitions += 1
            if acquisitions == 1:
                raise ValueError('Operasi WPI lain sedang berjalan; coba lagi setelah selesai.')
            yield

        with mock.patch.object(cli, 'operation_lock', side_effect=operation), \
             mock.patch('builtins.input', side_effect=[
                 '3', '', 'alias.example.com', '', '2', '',
                 '3', '', 'alias.example.com', '', '0',
             ]), contextlib.redirect_stdout(io.StringIO()) as output:
            cli.menu(manager)
        self.assertIn('Operasi WPI lain sedang berjalan', output.getvalue())
        self.assertEqual(acquisitions, 2)
        manager.add_secondary.assert_called_once_with('abcdef123456', 'alias.example.com')
        self.assertGreaterEqual(manager.sites.call_count, 3)

    def test_busy_scripted_mutation_returns_error_without_mutating(self):
        manager = mock.Mock()
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', side_effect=ValueError('operation occupied')), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(cli.main(['add-domain', 'old.example.com', 'alias.example.com']), 1)
        manager.add_secondary.assert_not_called()
        self.assertIn('operation occupied', errors.getvalue())

    def test_domain_uniqueness_is_rechecked_from_disk_after_lock_acquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = mock.Mock()
            manager = core.Manager(Path(folder) / 'state', Path(folder) / 'backups', runner=runner)
            primary = {'id': 'abcdef123456', 'primary': 'old.example.com',
                       'secondary': [], 'status': 'active'}
            other = {'id': '123456abcdef', 'primary': 'other.example.com',
                     'secondary': [], 'status': 'active'}
            manager.save_site(primary)
            other_manager = core.Manager(manager.data, manager.backups, runner=runner)

            @contextlib.contextmanager
            def operation():
                # A different panel claimed the chosen domain while the user was
                # answering prompts. The operation must reread this new state.
                other['secondary'] = ['alias.example.com']
                other_manager.save_site(other)
                yield

            with mock.patch.object(cli, 'operation_lock', side_effect=operation), \
                 mock.patch.object(core, 'check_dns', side_effect=AssertionError('Conflict must be rejected before DNS.')), \
                 mock.patch('builtins.input', side_effect=['3', '', 'alias.example.com', '', '0']), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                cli.menu(manager)
            self.assertIn('sudah dipakai situs lain', output.getvalue())
            self.assertEqual(manager.site(primary['id'])['secondary'], [])
            self.assertEqual(manager.site(other['id'])['secondary'], ['alias.example.com'])
            runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
