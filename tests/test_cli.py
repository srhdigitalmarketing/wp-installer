"""Panel and command regression tests without requiring root or Linux services."""

import contextlib
import io
from pathlib import Path
import unittest
from unittest import mock

from wpi import cli


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


if __name__ == "__main__":
    unittest.main()
