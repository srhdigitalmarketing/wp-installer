"""Per-site editor policy, safe configuration transactions and replay."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi import core
from wpi.file_editor import FileEditor


class Backend:
    def __init__(self):
        self.calls, self.wp_calls, self.remembered = [], [], []
        self.name_field = 'name'
        self.bad_candidate = False
        self.corrupt_mods = False
        self.fail_live_chown = False
        self.malformed_json = False

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        rc = 0
        if '-l' in argv:
            rc = int('BROKEN' in Path(argv[-1]).read_text() or
                     self.bad_candidate and '.wpi-file-editor-' in argv[-1])
        if argv[:2] == ['chown', 'www-data:www-data'] and self.fail_live_chown and \
                '.wpi-file-editor-' not in argv[-1]:
            raise RuntimeError('Simulated ownership failure')
        return subprocess.CompletedProcess(argv, rc, '', '')

    def wp(self, site, *args, **kwargs):
        self.wp_calls.append((site['id'], args, kwargs))
        config = next((arg.split('=', 1)[1] for arg in args if arg.startswith('--config-file=')), None)
        path = Path(config) if config else Path(site['root']) / 'wp-config.php'
        contents = path.read_text()
        if args[:2] == ('config', 'list'):
            if self.malformed_json:
                return subprocess.CompletedProcess([], 0, 'private-secret-not-json', '')
            values = re.findall(r"^define\('([A-Z_]+)', (.*)\);$", contents, re.M)
            out = json.dumps([{self.name_field: name, 'type': 'constant', 'value': json.loads(value)}
                              for name, value in values])
            return subprocess.CompletedProcess([], 0, out, '')
        if args[:2] != ('config', 'set') or args[2] != 'DISALLOW_FILE_EDIT' or '--raw' not in args:
            raise AssertionError('Unexpected WordPress mutation')
        contents = re.sub(r"^define\('DISALLOW_FILE_EDIT', .*?\);\n", '', contents, flags=re.M)
        contents += f"define('DISALLOW_FILE_EDIT', {args[3]});\n"
        if self.corrupt_mods:
            contents += "define('DISALLOW_FILE_MODS', true);\n"
        path.write_bytes(contents.encode())
        return subprocess.CompletedProcess([], 0, '', '')


class FileEditorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.backend = Backend()
        self.manager = core.Manager(data_dir=self.base / 'data', runner=self.backend.runner)
        core.atomic_json(self.manager.data / 'config.json', {'php_version': '8.3'})
        self.manager.wp = self.backend.wp
        self.manager.remember_config = self.backend.remembered.append
        self.editor = FileEditor(self.manager)
        patch = mock.patch.object(core, 'WWW', self.base / 'www')
        patch.start()
        self.addCleanup(patch.stop)
        for number in (1, 2):
            identifier = f'{number:012x}'
            root = core.WWW / identifier / 'public'
            root.mkdir(parents=True)
            config = root / 'wp-config.php'
            config.write_bytes(b"<?php\ndefine('DB_PASSWORD', \"private-secret\");\n"
                               b"define('DISALLOW_FILE_EDIT', true);\n")
            config.chmod(0o640)
            self.manager.save_site({'id': identifier, 'root': str(root),
                                    'primary': f'site{number}.example.com', 'aliases': [], 'secondary': []})
        self.a, self.b = '000000000001', '000000000002'

    def config(self, identifier=None):
        return Path(self.manager.site(identifier or self.a)['root']) / 'wp-config.php'

    def tree(self):
        return {str(path.relative_to(self.base)): path.read_bytes()
                for path in self.base.rglob('*') if path.is_file()}

    def test_status_is_readonly_and_sanitized_for_current_and_legacy_wpcli_schema(self):
        before = self.tree()
        for field in ('name', 'key'):
            self.backend.name_field = field
            report = self.editor.status(self.a)
            self.assertEqual(report, {'site_id': self.a, 'primary': 'site1.example.com',
                                     'enabled': False, 'configured_enabled': False,
                                     'managed_enabled': None, 'blocked_by_file_mods': False})
            self.assertNotIn('private-secret', json.dumps(report))
        self.assertEqual(self.tree(), before)
        self.assertTrue(all('-l' in call for call in self.backend.calls))
        self.assertTrue(all(args[:2] == ('config', 'list') for _, args, _ in self.backend.wp_calls))

    def test_enable_then_disable_preserves_other_settings_and_peer_and_remembers_policy(self):
        peer_before = self.config(self.b).read_bytes()
        report = self.editor.apply(self.a, True)
        self.assertTrue(report['enabled'])
        self.assertTrue(report['changed'])
        self.assertTrue(report['managed_enabled'])
        backup = Path(report['backup'])
        self.assertIn(b"define('DISALLOW_FILE_EDIT', true);", (backup / 'wp-config.php').read_bytes())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE((backup / 'wp-config.php').stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        self.assertIn(b'private-secret', self.config().read_bytes())
        self.assertEqual(self.config().read_text().count("define('DISALLOW_FILE_EDIT'"), 1)
        self.assertEqual(self.config(self.b).read_bytes(), peer_before)
        self.assertEqual(self.backend.remembered, [self.a])
        report = self.editor.apply(self.a, False)
        self.assertFalse(report['enabled'])
        self.assertFalse(report['managed_enabled'])
        self.assertEqual(self.config(self.b).read_bytes(), peer_before)

    def test_repeated_choice_does_not_rewrite_config_create_backup_or_add_constants(self):
        self.editor.apply(self.a, True)
        before = self.tree()
        self.backend.wp_calls.clear()
        self.backend.calls.clear()
        self.assertFalse(self.editor.apply(self.a, True)['changed'])
        self.assertEqual(self.tree(), before)
        self.assertFalse(any(args[:2] == ('config', 'set') for _, args, _ in self.backend.wp_calls))
        self.assertTrue(all('-l' in call for call in self.backend.calls))

    def test_modification_blocker_refuses_enabling_before_mutation_but_allows_disabling(self):
        with self.config().open('ab') as output:
            output.write(b"define('DISALLOW_FILE_MODS', true);\n")
        before = self.tree()
        with self.assertRaisesRegex(ValueError, 'DISALLOW_FILE_MODS'):
            self.editor.apply(self.a, True)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.backend.remembered)
        self.assertFalse(any(args[:2] == ('config', 'set') for _, args, _ in self.backend.wp_calls))
        report = self.editor.apply(self.a, False)
        self.assertTrue(report['blocked_by_file_mods'])
        self.assertFalse(report['enabled'])
        self.assertIn(b"define('DISALLOW_FILE_MODS', true);", self.config().read_bytes())

    def test_status_uses_php_truthiness_instead_of_python_string_false_assumption(self):
        for value, blocked in ((False, False), (0, False), ('0', False), ('', False),
                               ('false', True), (True, True), (1, True)):
            with self.subTest(value=value):
                self.config().write_bytes(("<?php\ndefine('DISALLOW_FILE_EDIT', false);\n"
                                           f"define('DISALLOW_FILE_MODS', {json.dumps(value)});\n").encode())
                report = self.editor.status(self.a)
                self.assertEqual(report['blocked_by_file_mods'], blocked)
                self.assertEqual(report['enabled'], not blocked)

    def test_failed_candidate_or_activation_restores_original_bytes_metadata_and_ownership(self):
        for failure in ('bad_candidate', 'fail_live_chown', 'corrupt_mods'):
            with self.subTest(failure=failure):
                config_before = self.config().read_bytes()
                site_path = self.manager.data / 'sites' / f'{self.a}.json'
                metadata_before = site_path.read_bytes()
                attributes = self.config().stat()
                setattr(self.backend, failure, True)
                with self.assertRaises((ValueError, RuntimeError)):
                    self.editor.apply(self.a, True)
                setattr(self.backend, failure, False)
                self.assertEqual(self.config().read_bytes(), config_before)
                self.assertEqual(site_path.read_bytes(), metadata_before)
                self.assertEqual((self.config().stat().st_mode, self.config().stat().st_uid,
                                  self.config().stat().st_gid),
                                 (attributes.st_mode, attributes.st_uid, attributes.st_gid))
                self.assertFalse(self.backend.remembered)
        self.assertTrue(list((self.manager.data / 'file-editor-backups').rglob('wp-config.php')))

    def test_metadata_write_failure_rolls_back_an_activated_config(self):
        original = self.config().read_bytes()
        path = self.manager.data / 'sites' / f'{self.a}.json'
        original_metadata = path.read_bytes()
        save = self.manager.save_site
        def partial_save(site):
            save(site)
            raise RuntimeError('Simulated postwrite failure')
        with mock.patch.object(self.manager, 'save_site', side_effect=partial_save), \
                self.assertRaises(RuntimeError):
            self.editor.apply(self.a, True)
        self.assertEqual(self.config().read_bytes(), original)
        self.assertEqual(path.read_bytes(), original_metadata)

    def test_overlay_replays_current_explicit_policy_into_restore_candidate_only(self):
        self.editor.apply(self.a, True)
        live = self.config().read_bytes()
        candidate = self.config().parent.parent / 'candidate' / 'wp-config.php'
        candidate.parent.mkdir()
        candidate.write_bytes(b"<?php\ndefine('DISALLOW_FILE_EDIT', true);\n"
                              b"define('DISALLOW_FILE_MODS', true);\n")
        self.assertTrue(self.editor.overlay_config(self.manager.site(self.a), candidate))
        self.assertIn(b"define('DISALLOW_FILE_EDIT', false);", candidate.read_bytes())
        self.assertIn(b"define('DISALLOW_FILE_MODS', true);", candidate.read_bytes())
        self.assertEqual(self.config().read_bytes(), live)
        self.assertFalse(self.editor.overlay_config(self.manager.site(self.a), candidate))
        self.config().unlink()  # Repair can rebuild a missing original config.
        self.assertFalse(self.editor.overlay_config(self.manager.site(self.a), candidate))

    def test_overlay_without_explicit_policy_preserves_an_imported_config(self):
        before = self.config().read_bytes()
        self.assertFalse(self.editor.overlay_config(self.manager.site(self.a)))
        self.assertEqual(self.config().read_bytes(), before)
        self.assertFalse(self.backend.wp_calls)

    def test_absent_flags_use_wordpress_defaults_and_explicit_choice_adds_one_boolean(self):
        self.config().write_bytes(b"<?php\ndefine('DB_PASSWORD', \"private-secret\");\n")
        self.assertTrue(self.editor.status(self.a)['enabled'])
        self.assertTrue(self.editor.apply(self.a, True)['changed'])
        self.assertEqual(self.config().read_text().count("define('DISALLOW_FILE_EDIT'"), 1)
        self.assertFalse(self.editor.apply(self.a, True)['changed'])

    def test_overlay_can_replay_into_installing_or_migrating_site_but_manual_apply_cannot(self):
        for status in ('installing', 'incomplete', 'migrating'):
            with self.subTest(status=status):
                site = self.manager.site(self.a)
                site['status'], site['file_editor_enabled'] = status, True
                self.manager.save_site(site)
                self.config().write_bytes(b"<?php\ndefine('DISALLOW_FILE_EDIT', true);\n")
                self.assertTrue(self.editor.overlay_config(site))
                self.assertIn(b"define('DISALLOW_FILE_EDIT', false);", self.config().read_bytes())
                with self.assertRaisesRegex(ValueError, 'instalasi atau migrasi'):
                    self.editor.apply(self.a, False)

    def test_live_overlay_stages_changes_and_rolls_back_failed_activation(self):
        site = self.manager.site(self.a)
        site['file_editor_enabled'] = True
        self.manager.save_site(site)
        before = self.config().read_bytes()
        self.backend.fail_live_chown = True
        with self.assertRaises(RuntimeError):
            self.editor.overlay_config(site)
        self.assertEqual(self.config().read_bytes(), before)

    def test_optional_repair_snapshot_failure_does_not_report_successful_change_as_failed(self):
        for error in (OSError('snapshot unavailable'), subprocess.SubprocessError('snapshot failed')):
            with self.subTest(error=type(error).__name__):
                self.config().write_bytes(b"<?php\ndefine('DISALLOW_FILE_EDIT', true);\n")
                with mock.patch.object(self.manager, 'remember_config', side_effect=error):
                    report = self.editor.apply(self.a, True)
                self.assertTrue(report['enabled'])
                self.assertTrue(report['changed'])
                self.assertTrue(self.manager.site(self.a)['file_editor_enabled'])

    def test_broken_config_invalid_json_or_nonboolean_input_never_changes_metadata(self):
        before = self.tree()
        for invalid in (1, 'true', None):
            with self.assertRaises(ValueError):
                self.editor.apply(self.a, invalid)
        self.backend.malformed_json = True
        with self.assertRaisesRegex(ValueError, 'Format konfigurasi') as failure:
            self.editor.status(self.a)
        self.assertNotIn('private-secret', str(failure.exception))
        self.backend.malformed_json = False
        self.config().write_bytes(b'<?php BROKEN')
        metadata = self.manager.site(self.a)
        with self.assertRaisesRegex(ValueError, 'repair'):
            self.editor.apply(self.a, True)
        self.assertEqual(self.manager.site(self.a), metadata)
        self.assertNotIn('file-editor-backups', ' '.join(before))

    def test_foreign_root_and_candidate_are_rejected(self):
        site = self.manager.site(self.a)
        site['root'] = str(Path(self.manager.site(self.b)['root']))
        self.manager.save_site(site)
        with self.assertRaisesRegex(ValueError, 'document root'):
            self.editor.apply(self.a, True)
        site['root'] = str(core.WWW / self.a / 'public')
        site['file_editor_enabled'] = True
        self.manager.save_site(site)
        foreign = self.base / 'wp-config.php'
        foreign.write_bytes(b'<?php\n')
        with self.assertRaisesRegex(ValueError, 'di luar'):
            self.editor.overlay_config(site, foreign)

    def test_symlink_config_or_ancestor_is_rejected_before_any_wordpress_command(self):
        original = self.config().read_bytes()
        target = self.base / 'outside.php'
        target.write_bytes(original)
        self.config().unlink()
        try:
            self.config().symlink_to(target)
        except OSError:
            self.skipTest('Windows does not permit creating symlinks in this environment.')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.editor.apply(self.a, True)
        self.assertFalse(self.backend.wp_calls)
        self.assertEqual(target.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
