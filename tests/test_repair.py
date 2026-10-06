"""Repair changes configuration only and reports runtime failures honestly."""
import copy
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from wpi import core
from wpi.repair import SiteRepair


GOOD_CONFIG = "<?php\ndefine('DB_PASSWORD', 'OLDPASSWORD');\n/* That's all, stop editing! */\n"


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.www = self.base / 'www'
        self.root = self.www / 'abcdef123456' / 'public'
        self.root.mkdir(parents=True)
        self.config = self.root / 'wp-config.php'
        self.config.write_text(GOOD_CONFIG)
        (self.root / 'index.php').write_text('<?php echo "frontend";')
        (self.root / 'wp-settings.php').write_text('<?php // settings')
        self.media = self.root / 'wp-content' / 'uploads' / 'video.mp4'
        self.media.parent.mkdir(parents=True)
        self.media.write_bytes(b'NEW CONTENT MUST REMAIN')
        self.commands = []
        self.wp_commands = []
        self.frontend = 200
        self.admin = 302
        self.service_inactive = set()
        self.bad_fpm = False
        self.unreadable = set()
        self.tables = ['current_' + s for s in ('options', 'users', 'usermeta', 'posts',
                       'postmeta', 'terms', 'term_taxonomy', 'term_relationships',
                       'comments', 'commentmeta')]

        def runner(argv, **kwargs):
            self.commands.append((list(argv), dict(kwargs)))
            code, stdout = 0, ''
            if argv[0].endswith('php8.3') and '-l' in argv:
                code = int('BROKEN' in Path(argv[-1]).read_text())
            elif argv[0] == 'curl':
                code = 0
                stdout = str(self.admin if argv[-1].endswith('/wp-admin/') else self.frontend)
                # A parse-error config produces an actual 500 until replaced.
                if 'BROKEN' in self.config.read_text() or 'FATALCUSTOM' in self.config.read_text():
                    stdout = '500'
            elif argv[:2] == ['systemctl', 'is-active']:
                code = int(argv[2] in self.service_inactive)
            elif argv[:2] in (['systemctl', 'start'], ['systemctl', 'restart']):
                self.service_inactive.discard(argv[2])
            elif argv[0].endswith('php-fpm8.3'):
                code = int(self.bad_fpm)
            elif argv[0] == 'runuser':
                code = int(argv[-1] in self.unreadable)
            elif argv[0] == 'mysql':
                stdout = '\n'.join(self.tables) + '\n'
            return subprocess.CompletedProcess(argv, code, stdout, 'PRIVATE RAW LOG')

        self.manager = core.Manager(self.base / 'data', self.base / 'backups', runner)
        core.atomic_json(self.manager.data / 'config.json', {
            'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3', 'setup_complete': True})
        self.site = {'id': 'abcdef123456', 'primary': 'new.example.com',
                     'aliases': ['alias.example.com'], 'secondary': ['old.example.com'],
                     'tls': ['new.example.com'], 'root': str(self.root),
                     'email': 'owner@example.com', 'db_name': 'wpi_abcdef123456',
                     'db_user': 'wpi_abcdef123456', 'status': 'active',
                     'migration_id': '1' * 32}
        self.manager.save_site(self.site)
        self.password = 'a' * 48
        core.atomic_json(self.manager.data / 'credentials' / (self.site['id'] + '.json'), {
            'database_user': self.site['db_user'], 'database_password': self.password})
        self.web = mock.Mock()
        self.web.service = 'nginx'
        self.addCleanup(mock.patch.stopall)
        self.sleep = mock.patch('wpi.repair.time.sleep').start()
        self.tuner = SimpleNamespace(_lock=mock.MagicMock())
        mock.patch('wpi.repair.AutoTuner', return_value=self.tuner).start()
        self.tuner._lock.return_value.__enter__.return_value = True
        mock.patch.object(core, 'WWW', self.www).start()
        mock.patch.object(core.Manager, 'web', new_callable=mock.PropertyMock,
                          return_value=self.web).start()
        mock.patch.object(SiteRepair, '_memory', return_value='512M').start()

        def wp(site, *args, **kwargs):
            self.wp_commands.append((args, dict(kwargs)))
            if args[:2] == ('core', 'is-installed'):
                text = self.config.read_text() if self.config.exists() else ''
                return subprocess.CompletedProcess(args, int('BROKEN' in text or 'FATALCUSTOM' in text), '', '')
            options = [value for value in args if value.startswith('--config-file=')]
            candidate = Path(options[-1].split('=', 1)[1]) if options else self.config
            if args[:2] == ('config', 'create'):
                candidate.write_text(GOOD_CONFIG)
            elif args[:2] == ('config', 'has'):
                return subprocess.CompletedProcess(args, int(args[2] not in candidate.read_text()), '', '')
            elif args[:2] == ('config', 'set'):
                key = args[2]
                value = kwargs['input'].rstrip('\n') if '--prompt' in args else args[3]
                text = candidate.read_text()
                pattern = re.compile(r"define\('" + re.escape(key) + r"', '[^']*'\);")
                line = "define('" + key + "', '" + value + "');"
                if pattern.search(text):
                    text = pattern.sub(lambda _: line, text)
                else:
                    text = text.replace("/* That's all, stop editing! */", line + "\n/* That's all, stop editing! */")
                candidate.write_text(text)
            return subprocess.CompletedProcess(args, 0, '', '')

        self.manager.wp = wp
        self.repair = SiteRepair(self.manager)

    def test_healthy_site_is_not_restarted_or_changed_and_snapshot_is_private(self):
        before = self.config.read_bytes()
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'healthy')
        self.assertEqual(report['actions'], [])
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(any(argv[:2] == ['systemctl', 'restart'] for argv, _ in self.commands))
        snapshots = list((self.manager.data / 'config-snapshots' / self.site['id']).iterdir())
        self.assertEqual(len(snapshots), 1)
        self.assertEqual((snapshots[0] / 'wp-config.php').read_bytes(), before)
        if os.name == 'posix':
            self.assertEqual((snapshots[0] / 'wp-config.php').stat().st_mode & 0o777, 0o600)

    def test_parse_error_restores_only_config_keeps_current_db_roles_and_media(self):
        folder = self.repair.remember_config(self.site['id'])
        self.config.write_text('<?php BROKEN parse error')
        before_site = self.manager.site(self.site['id'])
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        action = next(a for a in report['actions'] if a['action'] == 'config_recovered')
        self.assertEqual(Path(action['source']).resolve(), folder.resolve())
        self.assertEqual((Path(action['preserved_config']) / 'wp-config.php').read_text(), '<?php BROKEN parse error')
        self.assertIn(self.password, self.config.read_text())
        self.assertNotIn('OLDPASSWORD', self.config.read_text())
        self.assertEqual(self.manager.site(self.site['id']), before_site)
        self.assertEqual(self.media.read_bytes(), b'NEW CONTENT MUST REMAIN')
        self.assertFalse(report['database_restored'])
        self.assertFalse(report['content_restored'])
        self.assertFalse(report['session_reset'])
        self.assertFalse(any('import' in args or 'export' in args for args, _ in self.wp_commands))
        self.assertFalse(any(argv[0] == 'mysql' for argv, _ in self.commands))
        secret_calls = [(args, kwargs) for args, kwargs in self.wp_commands if args[:3] == ('config', 'set', 'DB_PASSWORD')]
        self.assertEqual(secret_calls[0][1]['input'], self.password + '\n')
        self.assertFalse(any(self.password in argument for args, _ in self.wp_commands for argument in args))

    def test_changed_runtime_fatal_config_recovers_from_snapshot(self):
        self.repair.remember_config(self.site['id'])
        self.config.write_text(GOOD_CONFIG + '\nFATALCUSTOM();')
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        self.assertNotIn('FATALCUSTOM', self.config.read_text())

    def test_recovery_uses_current_editor_policy_instead_of_snapshot_policy(self):
        self.config.write_text(GOOD_CONFIG.replace("/* That's all, stop editing! */",
                               "define('DISALLOW_FILE_EDIT', 'true');\n/* That's all, stop editing! */"))
        self.repair.remember_config(self.site['id'])
        self.site['file_editor_enabled'] = True
        self.manager.save_site(self.site)
        self.config.write_text('<?php BROKEN parse error')

        def overlay(site, candidate=None):
            self.assertIs(site['file_editor_enabled'], True)
            self.assertNotEqual(candidate, self.config)
            self.repair._config_set(site, candidate, 'DISALLOW_FILE_EDIT', 'false', raw=True)

        with mock.patch.object(self.manager, '_site_file_editor_config', side_effect=overlay) as policy:
            report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        policy.assert_called_once()
        self.assertIn("define('DISALLOW_FILE_EDIT', 'false');", self.config.read_text())
        self.assertIs(self.manager.site(self.site['id'])['file_editor_enabled'], True)

    def _complete_backup(self, content):
        folder = self.manager.backups / self.site['id'] / '20260101-complete'
        folder.mkdir(parents=True)
        (folder / 'database.sql.gz').write_bytes(b'BACKUP SQL MUST NEVER BE READ INTO DB')
        with tarfile.open(folder / 'files.tar.gz', 'w:gz') as archive:
            entry = tarfile.TarInfo('public/wp-config.php')
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
            entry = tarfile.TarInfo('public/wp-content/uploads/video.mp4')
            entry.size = 3
            archive.addfile(entry, io.BytesIO(b'OLD'))
        (folder / 'site.json').write_text(json.dumps(self.site))
        (folder / 'manifest.json').write_text(json.dumps({
            'site_id': self.site['id'], 'sha256': {name: self.manager.file_hash(folder / name)
                for name in ('files.tar.gz', 'database.sql.gz', 'site.json')}}))
        (folder / 'COMPLETE').touch()
        return folder

    def test_legacy_complete_backup_extracts_only_wpconfig(self):
        folder = self._complete_backup(GOOD_CONFIG.encode())
        self.config.write_text('<?php BROKEN legacy')
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        action = next(a for a in report['actions'] if a['action'] == 'config_recovered')
        self.assertEqual(Path(action['source']).resolve(), folder.resolve())
        self.assertEqual(self.media.read_bytes(), b'NEW CONTENT MUST REMAIN')
        self.assertFalse(any('import' in args or 'export' in args for args, _ in self.wp_commands))

    def test_legacy_no_backup_rebuild_uses_verified_prefix_and_resets_sessions(self):
        self.config.write_text('<?php BROKEN')
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        self.assertTrue(report['session_reset'])
        create = next(args for args, _ in self.wp_commands if args[:2] == ('config', 'create'))
        self.assertIn('--dbprefix=current_', create)
        self.assertIn('--skip-salts', create)
        self.assertTrue(any(args[:3] == ('config', 'set', 'AUTH_SALT') and '--prompt' in args
                            for args, _ in self.wp_commands))
        sql = next(kwargs['input'] for argv, kwargs in self.commands if argv[0] == 'mysql')
        self.assertIn('SELECT TABLE_NAME', sql)
        self.assertNotRegex(sql, r'(?i)\b(?:CREATE|DROP|INSERT|UPDATE|DELETE)\b')

    def test_unproven_or_ambiguous_database_prefix_refuses_rebuild(self):
        self.config.write_text('<?php BROKEN')
        original = self.config.read_bytes()
        self.tables = ['some_options', 'some_posts']
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertIn('config_recovery_failed', report['errors'])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(any(args[:2] == ('config', 'create') for args, _ in self.wp_commands))
        self.tables = ['current_' + suffix for suffix in ('options', 'users', 'usermeta', 'posts',
                       'postmeta', 'terms', 'term_taxonomy', 'term_relationships', 'comments', 'commentmeta')]
        self.tables += [name.replace('current_', 'second_', 1) for name in self.tables]
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertEqual(self.config.read_bytes(), original)

    def test_missing_credentials_never_replaces_config_even_with_good_snapshot(self):
        self.repair.remember_config(self.site['id'])
        self.config.write_text('<?php BROKEN')
        (self.manager.data / 'credentials' / (self.site['id'] + '.json')).unlink()
        original = self.config.read_bytes()
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(any(args[:2] == ('config', 'set') for args, _ in self.wp_commands))

    def test_backup_hash_mismatch_is_not_restored(self):
        folder = self._complete_backup(GOOD_CONFIG.encode())
        (folder / 'site.json').write_text('{}')
        self.config.write_text('<?php BROKEN')
        self.tables = []
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertIn('BROKEN', self.config.read_text())

    def test_backup_domain_constants_follow_current_primary_without_restoring_roles(self):
        content = GOOD_CONFIG.replace("/* That's all, stop editing! */",
            "define('WP_HOME', 'https://old.example.com');\ndefine('WP_SITEURL', 'https://old.example.com');\n/* That's all, stop editing! */")
        self._complete_backup(content.encode())
        self.config.write_text('<?php BROKEN')
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        self.assertIn("define('WP_HOME', 'https://new.example.com');", self.config.read_text())
        self.assertIn("define('WP_SITEURL', 'https://new.example.com');", self.config.read_text())
        self.assertEqual(self.manager.site(self.site['id'])['primary'], 'new.example.com')
        self.assertEqual(self.manager.site(self.site['id'])['secondary'], ['old.example.com'])

    def test_preserve_failure_aborts_before_permissions_or_services_are_changed(self):
        self.frontend = 500
        with mock.patch.object(self.repair, '_preserve_config', side_effect=OSError('No space')):
            report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertIn('original_config_backup_failed', report['errors'])
        self.assertEqual(report['actions'], [])
        self.assertFalse(any(argv[0] == 'chown' or argv[:2] == ['systemctl', 'restart']
                             for argv, _ in self.commands))
        self.assertFalse(any(args[:2] == ('config', 'set') for args, _ in self.wp_commands))

    def test_snapshot_hash_mismatch_is_skipped(self):
        folder = self.repair.remember_config(self.site['id'])
        (folder / 'wp-config.php').write_text('<?php ATTACKED snapshot')
        self.config.write_text('<?php BROKEN')
        self.tables = []
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertIn('BROKEN', self.config.read_text())
        self.assertNotIn('ATTACKED', self.config.read_text())

    def test_existing_memory_constants_are_normalized_with_private_original(self):
        self.config.write_text(GOOD_CONFIG.replace("/* That's all, stop editing! */",
            "define('WP_MEMORY_LIMIT', '500');\ndefine('WP_MAX_MEMORY_LIMIT', '500');\n/* That's all, stop editing! */"))
        self.frontend = 500
        report = self.repair.repair(self.site['id'])
        action = next(a for a in report['actions'] if a['action'] == 'memory_constants_normalized')
        self.assertIn("'512M'", self.config.read_text())
        self.assertIn("'500'", (Path(action['preserved_config']) / 'wp-config.php').read_text())
        self.assertEqual(report['status'], 'unresolved')

    def test_still500_is_reported_unresolved_does_not_disable_plugins_or_leak_rawlogs(self):
        self.frontend = 500
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertEqual(report['checks']['frontend']['status'], 500)
        self.assertIn('next_step', report)
        self.assertFalse(any('plugin' in args or 'theme' in args for args, _ in self.wp_commands))
        serialized = json.dumps(report)
        self.assertNotIn('PRIVATE RAW LOG', serialized)
        self.assertNotIn(self.password, serialized)
        self.assertNotIn('OLDPASSWORD', serialized)

    def test_syntax_bad_config_is_not_executed_by_wpcli_during_diagnosis(self):
        self.config.write_text('<?php BROKEN secret')
        report = self.repair.diagnose(self.site['id'])
        self.assertFalse(report['checks']['config_syntax'])
        self.assertEqual(self.wp_commands, [])
        lint = next(argv for argv, _ in self.commands if '-l' in argv)
        self.assertIn('-n', lint)

    def test_wrong_root_is_rejected_before_any_mutations(self):
        wrong = copy.deepcopy(self.site)
        wrong['root'] = str(self.base / 'external')
        self.manager.save_site(wrong)
        with self.assertRaisesRegex(ValueError, 'document root'):
            self.repair.repair(self.site['id'])
        self.assertEqual(self.commands, [])
        self.assertEqual(self.wp_commands, [])

    def test_invalid_fpm_config_never_restarts_fpm(self):
        self.bad_fpm = True
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'unresolved')
        self.assertIn('fpm_config_invalid', report['errors'])
        self.assertFalse(any(argv[:3] == ['systemctl', 'restart', 'php8.3-fpm'] for argv, _ in self.commands))

    def test_stopped_managed_services_are_started_and_live_admin_is_probed(self):
        self.service_inactive = {'mariadb', 'php8.3-fpm', 'nginx'}
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['status'], 'resolved')
        self.assertFalse(self.service_inactive)
        http = [argv for argv, _ in self.commands if argv[0] == 'curl']
        self.assertTrue(any(argv[-1].endswith('/wp-admin/') for argv in http))
        self.assertTrue(all('--resolve' in argv and '-k' not in argv and '--insecure' not in argv for argv in http))
        self.assertTrue(all('127.0.0.1' in argv[argv.index('--resolve') + 1] for argv in http))

    def test_post_restart_http_readiness_retries_then_resolves(self):
        original = self.manager.runner
        remaining = [2]

        def delayed(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == 'curl' and argv[-1].endswith('/') and not argv[-1].endswith('/wp-admin/'):
                if remaining[0]:
                    remaining[0] -= 1
                    result.stdout = '502'
            return result

        self.manager.runner = delayed
        self.repair.runner = delayed
        report = self.repair.repair(self.site['id'])
        self.assertEqual(report['before']['frontend']['status'], 502)
        self.assertEqual(report['status'], 'resolved')
        self.assertEqual(report['checks']['frontend']['status'], 200)
        self.sleep.assert_called_once_with(0.2)

    def test_readonly_diagnosis_does_not_retry_or_wait(self):
        self.frontend = 500
        report = self.repair.diagnose(self.site['id'])
        self.assertEqual(report['checks']['frontend']['status'], 500)
        self.sleep.assert_not_called()
        self.assertEqual(len([argv for argv, _ in self.commands if argv[0] == 'curl']), 2)
        self.tuner._lock.assert_not_called()

    def test_fpm_restart_is_serialized_with_adaptive_controller(self):
        self.frontend = 500
        held = [False]

        @contextmanager
        def lock(blocking=False):
            self.assertTrue(blocking)
            held[0] = True
            try:
                yield True
            finally:
                held[0] = False

        self.tuner._lock.side_effect = lock
        original = self.repair.runner

        def serialized(argv, **kwargs):
            if argv[:3] == ['systemctl', 'restart', 'php8.3-fpm']:
                self.assertTrue(held[0])
            return original(argv, **kwargs)

        self.repair.runner = serialized
        self.web.write_site.side_effect = lambda _: self.assertFalse(held[0])
        report = self.repair.repair(self.site['id'])
        self.assertIn({'action': 'fpm_restarted_opcache_cleared'}, report['actions'])
        self.assertFalse(held[0])

    def test_busy_adaptive_lock_does_not_restart_fpm(self):
        self.frontend = 500
        self.tuner._lock.return_value.__enter__.return_value = False
        report = self.repair.repair(self.site['id'])
        self.assertIn('fpm_restart_lock_busy', report['errors'])
        self.assertFalse(any(argv[:3] == ['systemctl', 'restart', 'php8.3-fpm']
                             for argv, _ in self.commands))

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink support')
    def test_symlink_config_refused_without_following_it(self):
        target = self.base / 'outside.php'
        target.write_text(GOOD_CONFIG)
        self.config.unlink()
        self.config.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.repair.repair(self.site['id'])
        self.assertEqual(target.read_text(), GOOD_CONFIG)
        self.assertEqual(self.commands, [])


if __name__ == '__main__':
    unittest.main()
