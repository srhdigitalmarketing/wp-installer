"""Per-site editor commands and menus expose only status and serialize mutations."""
import contextlib
import io
import unittest
from unittest import mock

from wpi import cli


class FileEditorCLIBase(unittest.TestCase):
    def report(self, enabled=False, managed=False, blocked=False, **extra):
        return {'site_id': 'abcdef123456', 'primary': 'example.com', 'enabled': enabled,
                'configured_enabled': enabled or blocked, 'managed_enabled': managed,
                'blocked_by_file_mods': blocked, **extra}

    def invoke(self, args, manager, lock=None):
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', lock or mock.Mock(
                 return_value=contextlib.nullcontext())), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = cli.main(args)
        return result, output.getvalue(), errors.getvalue()


class FileEditorCommandTests(FileEditorCLIBase):
    def test_status_is_readonly_and_works_while_operation_is_busy(self):
        manager = mock.Mock()
        manager.file_editor_status.return_value = self.report(
            enabled=True, managed=None, constants={'DB_PASSWORD': 'private-password'})
        lock = mock.Mock(side_effect=ValueError('occupied'))
        result, output, errors = self.invoke(['file-editor', 'example.com'], manager, lock)
        self.assertEqual(result, 0)
        self.assertIn('Editor menurut konfigurasi: Aktif', output)
        self.assertIn('Pengaturan WPI: Belum ditetapkan', output)
        self.assertNotIn('private-password', output + errors)
        self.assertNotIn('DB_PASSWORD', output + errors)
        self.assertEqual(manager.mock_calls, [mock.call.file_editor_status('example.com')])
        lock.assert_not_called()

    def test_selected_mutation_acquires_one_lock_without_confirmation(self):
        for flag, enabled in (('--enable', True), ('--disable', False)):
            with self.subTest(flag=flag):
                events = []

                @contextlib.contextmanager
                def operation():
                    events.append('locked')
                    yield
                    events.append('unlocked')

                manager = mock.Mock()
                manager.set_file_editor.side_effect = lambda identifier, enabled: (
                    events.append(('mutate', identifier, enabled)) or
                    self.report(enabled=enabled, managed=enabled, changed=True))
                lock = mock.Mock(side_effect=operation)
                with mock.patch('builtins.input', side_effect=AssertionError('Unexpected prompt')):
                    result, output, _ = self.invoke(['file-editor', 'abcdef123456', flag], manager, lock)
                self.assertEqual(result, 0)
                self.assertIn('Pengaturan editor disimpan.', output)
                self.assertEqual(events, ['locked', ('mutate', 'abcdef123456', enabled), 'unlocked'])
                lock.assert_called_once_with()
                self.assertEqual(manager.mock_calls, [mock.call.set_file_editor(
                    'abcdef123456', enabled=enabled)])

    def test_file_mods_blocker_reports_actual_and_managed_status_without_a_write(self):
        manager = mock.Mock()
        manager.file_editor_status.return_value = self.report(
            enabled=False, managed=True, blocked=True, DB_PASSWORD='private-password')
        result, output, errors = self.invoke(['file-editor', 'example.com'], manager)
        self.assertEqual(result, 0)
        self.assertIn('Editor menurut konfigurasi: Nonaktif', output)
        self.assertIn('Pengaturan WPI: Aktif', output)
        self.assertIn('DISALLOW_FILE_MODS aktif', output)
        self.assertIn('DISALLOW_FILE_EDIT: mengizinkan editor', output)
        self.assertNotIn('private-password', output + errors)
        manager.set_file_editor.assert_not_called()

    def test_mutation_failure_reports_fixed_file_mods_reason(self):
        manager = mock.Mock()
        manager.set_file_editor.side_effect = ValueError(
            'DISALLOW_FILE_MODS aktif; perubahan editor diblokir.')
        lock = mock.Mock(return_value=contextlib.nullcontext())
        result, output, errors = self.invoke(['file-editor', 'example.com', '--enable'], manager, lock)
        self.assertEqual(result, 1)
        self.assertEqual(output, '')
        self.assertIn('DISALLOW_FILE_MODS aktif', errors)
        lock.assert_called_once_with()
        self.assertEqual(manager.mock_calls, [mock.call.set_file_editor('example.com', enabled=True)])

    def test_flags_are_mutually_exclusive_and_site_is_required(self):
        for args in (['file-editor'], ['file-editor', 'example.com', '--enable', '--disable']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as error:
                cli.parser().parse_args(args)
            self.assertEqual(error.exception.code, 2)


class FileEditorMenuTests(FileEditorCLIBase):
    def manager(self):
        manager = mock.Mock()
        manager.config = {'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3'}
        manager.sites.return_value = [{'id': 'abcdef123456', 'primary': 'example.com',
                                       'status': 'active'}]
        manager.site.return_value = manager.sites.return_value[0]
        manager.file_editor_status.return_value = self.report(managed=None)
        manager.set_file_editor.side_effect = lambda identifier, enabled: self.report(
            enabled=enabled, managed=enabled, changed=True)
        return manager

    def test_menu_selects_site_before_status_then_mutates_only_selected_editor(self):
        for selection, enabled in (('1', True), ('2', False)):
            with self.subTest(selection=selection):
                manager = self.manager()
                lock = mock.Mock(return_value=contextlib.nullcontext())
                with mock.patch('builtins.input', side_effect=['20', '', selection, '', '0']) as prompt:
                    result, output, _ = self.invoke(['menu'], manager, lock)
                self.assertEqual(result, 0)
                self.assertIn('(20) Editor file plugin / theme', output)
                self.assertIn('(19) Status / kelola cache Redis', output)
                self.assertIn('Editor menurut konfigurasi: Nonaktif', output)
                self.assertEqual(manager.mock_calls, [mock.call.sites(), mock.call.site('example.com'),
                    mock.call.file_editor_status('abcdef123456'),
                    mock.call.set_file_editor('abcdef123456', enabled=enabled)])
                self.assertEqual(prompt.call_count, 5)
                lock.assert_called_once_with()

    def test_return_keeps_status_readonly_and_never_locks(self):
        manager = self.manager()
        lock = mock.Mock(side_effect=ValueError('occupied'))
        with mock.patch('builtins.input', side_effect=['20', '', '0', '', '0']):
            result, output, _ = self.invoke(['menu'], manager, lock)
        self.assertEqual(result, 0)
        self.assertIn('Pengaturan WPI: Belum ditetapkan', output)
        manager.file_editor_status.assert_called_once_with('abcdef123456')
        manager.set_file_editor.assert_not_called()
        lock.assert_not_called()

    def test_invalid_submenu_selection_never_mutates(self):
        manager = self.manager()
        lock = mock.Mock(side_effect=ValueError('occupied'))
        with mock.patch('builtins.input', side_effect=['20', '', '9', '', '0']):
            result, output, _ = self.invoke(['menu'], manager, lock)
        self.assertEqual(result, 0)
        self.assertIn('Pilihan harus 0, 1, atau 2', output)
        manager.set_file_editor.assert_not_called()
        lock.assert_not_called()


if __name__ == '__main__':
    unittest.main()
