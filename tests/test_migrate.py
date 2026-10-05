"""Source snapshot lifecycle and interrupted encrypted transfer recovery."""
import gzip
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from wpi import core
from wpi.migrate import Migration


class SourceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.manager = core.Manager(self.base / 'state', self.base / 'backups')
        core.atomic_json(self.manager.data / 'config.json', {
            'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3', 'setup_complete': True})
        self.site = {'id': '123456abcdef', 'root': str(self.base / 'www' / 'public'),
                     'primary': 'main.example.com', 'aliases': ['alias.example.com'],
                     'secondary': ['old.example.com'], 'tls': [], 'status': 'active',
                     'email': 'owner@example.com', 'admin': 'owner',
                     'db_name': 'wpi_123456abcdef', 'db_user': 'wpi_123456abcdef'}
        root = Path(self.site['root'])
        root.mkdir(parents=True)
        (root / 'wp-config.php').write_text('<?php /* original site */')
        (root / 'media.jpg').write_bytes(b'original uploaded media')
        self.manager.save_site(self.site)
        core.atomic_json(self.manager.data / 'credentials' / (self.site['id'] + '.json'),
                         {'wordpress_password': 'OriginalAdminPassword', 'database_password': 'OldDbPassword'})
        self.wp_calls = []
        self.backup_calls = []

        def wp(site, *args, **kwargs):
            self.wp_calls.append(args)
            return subprocess.CompletedProcess([], 1 if args == ('maintenance-mode', 'is-active') else 0)

        def backup(identifier):
            self.backup_calls.append(identifier)
            directory = self.manager.backups / identifier / ('snapshot-' + str(len(self.backup_calls)))
            directory.mkdir(parents=True)
            with gzip.open(directory / 'database.sql.gz', 'wb') as out:
                out.write(b'CREATE TABLE original_posts(id INT);')
            with tarfile.open(directory / 'files.tar.gz', 'w:gz') as archive:
                archive.add(self.site['root'], arcname='public')
            core.atomic_json(directory / 'site.json', self.site)
            core.atomic_json(directory / 'manifest.json', {'site_id': identifier, 'sha256': {
                name: self.manager.file_hash(directory / name)
                for name in ('database.sql.gz', 'files.tar.gz', 'site.json')}})
            (directory / 'COMPLETE').touch()
            return directory

        self.manager.wp = mock.Mock(side_effect=wp)
        self.manager.backup = mock.Mock(side_effect=backup)
        self.web = mock.Mock()
        self.web.certificate_ready.return_value = False
        self.web_patch = mock.patch.object(core.Manager, 'web', new_callable=mock.PropertyMock,
                                          return_value=self.web)
        self.web_patch.start()
        self.addCleanup(self.web_patch.stop)
        self.sessions = []
        self.fail_import = False
        self.fail_preflight = False
        test = self

        class Session:
            def __init__(self, host, username, password, port, data_dir):
                self.host, self.username, self.port = host, username, port
                self.scripts, self.uploads = [], []
                self.closed = False
                test.sessions.append(self)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.closed = True

            def run_root(self, script):
                self.scripts.append(script)
                if 'WPI_MIGRATION_PREFLIGHT_OK' in script:
                    if test.fail_preflight:
                        raise RuntimeError('preflight refused')
                    output = 'WPI_MIGRATION_PREFLIGHT_OK\n'
                elif 'WPI_MIGRATION_PACKAGE_OK' in script:
                    output = 'WPI_MIGRATION_PACKAGE_OK\n'
                elif 'exec /usr/local/bin/wpi migration-import' in script:
                    if test.fail_import:
                        raise RuntimeError('import stopped')
                    output = 'Preparing destination...\n' + json.dumps({
                        'status': 'ready', 'sites': [test.site], 'ssl_pending': ['main.example.com']})
                else:
                    output = ''
                return subprocess.CompletedProcess([], 0, stdout=output)

            def upload(self, path, destination):
                self.uploads.append((path, destination))

        self.migration = Migration(self.manager, session_factory=Session)
        self.id_patch = mock.patch.object(Migration, '_machine_id', return_value='a' * 32)
        self.id_patch.start()
        self.addCleanup(self.id_patch.stop)

    def migrate(self):
        return self.migration.migrate('192.0.2.10', 'root', 'SshSecretNeverPersisted', port=22)

    def test_snapshot_preserves_domains_media_and_credentials_but_not_ssh_password(self):
        report = self.migrate()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['domains'], ['main.example.com', 'alias.example.com', 'old.example.com'])
        bundle = Path(report['backup']) / 'migration.zip'
        with zipfile.ZipFile(bundle) as archive:
            metadata = json.loads(archive.read('metadata.json'))
            self.assertEqual(metadata['sites'][0], self.site)
            with tarfile.open(fileobj=io.BytesIO(archive.read('backups/123456abcdef/files.tar.gz')),
                              mode='r:gz') as files:
                self.assertEqual(files.extractfile('public/media.jpg').read(), b'original uploaded media')
            self.assertEqual(json.loads(archive.read('credentials/123456abcdef.json'))['wordpress_password'],
                             'OriginalAdminPassword')
            for name in archive.namelist():
                self.assertNotIn(b'SshSecretNeverPersisted', archive.read(name))
        self.assertEqual(self.wp_calls, [('maintenance-mode', 'is-active'),
                                       ('maintenance-mode', 'activate'), ('maintenance-mode', 'deactivate')])
        self.assertTrue(self.sessions[-1].closed)
        self.assertTrue(Path(self.site['root']).is_dir())
        for path in self.manager.data.rglob('*.json'):
            self.assertNotIn('SshSecretNeverPersisted', path.read_text())

    def test_backup_failure_releases_only_our_maintenance_and_never_uploads(self):
        self.manager.backup.side_effect = RuntimeError('disk full')
        with self.assertRaisesRegex(RuntimeError, 'Migrasi belum selesai'):
            self.migrate()
        self.assertIn(('maintenance-mode', 'deactivate'), self.wp_calls)
        self.assertEqual(self.sessions[-1].uploads, [])
        self.assertTrue(self.sessions[-1].closed)

    def test_existing_maintenance_is_preserved_on_source(self):
        self.manager.wp.side_effect = lambda *args, **kwargs: subprocess.CompletedProcess([], 0)
        self.migrate()
        self.assertEqual(self.manager.wp.call_args_list,
                         [mock.call(self.site, 'maintenance-mode', 'is-active', check=False)])

    def test_failed_import_resumes_the_identical_snapshot_and_migration_id(self):
        self.fail_import = True
        with self.assertRaises(RuntimeError):
            self.migrate()
        failed = self.migration.status()[0]
        self.assertEqual(failed['last_stage'], 'importing')
        self.fail_import = False
        report = self.migrate()
        self.assertEqual(report['migration_id'], failed['migration_id'])
        self.assertEqual(len(self.backup_calls), 1)
        self.assertEqual(self.migration.status()[0]['status'], 'ready')

    def test_failed_destination_preflight_does_not_pause_or_backup_source(self):
        self.fail_preflight = True
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.manager.wp.assert_not_called()
        self.manager.backup.assert_not_called()
        self.assertFalse(self.sessions[-1].uploads)

    def test_incomplete_site_refuses_snapshot(self):
        self.manager.save_site({**self.site, 'status': 'incomplete'})
        with self.assertRaises(RuntimeError):
            self.migrate()
        self.manager.wp.assert_not_called()
        self.assertFalse(self.sessions[-1].uploads)

    def test_insufficient_source_space_refuses_backup(self):
        with mock.patch('wpi.migrate.shutil.disk_usage', return_value=mock.Mock(free=0)):
            with self.assertRaises(RuntimeError):
                self.migrate()
        self.manager.backup.assert_not_called()

    def test_remote_python_installer_source_compiles_and_only_accepts_regular_python_files(self):
        self.migrate()
        script = next(s for s in self.sessions[-1].scripts if 'WPI_MIGRATION_PACKAGE_OK' in s)
        embedded = script.split("<<'PY'\n", 1)[1].rsplit('\nPY\n', 1)[0]
        compile(embedded, '<remote-installer>', 'exec')
        self.assertIn('stat.S_ISLNK(mode)', embedded)
        self.assertIn('py_compile.compile', embedded)
        self.assertIn('hashlib.sha256', embedded)

    def test_selected_certificate_keys_are_in_private_archive_only(self):
        cert = self.base / 'fullchain.pem'
        key = self.base / 'privkey.pem'
        cert.write_text('original certificate')
        key.write_text('original private key')
        self.web.certificate_ready.side_effect = lambda host: host == self.site['primary']
        self.web.certificate_paths.return_value = (cert, key)
        report = self.migrate()
        with zipfile.ZipFile(Path(report['backup']) / 'migration.zip') as archive:
            self.assertEqual(archive.read('certs/main.example.com/privkey.pem'), b'original private key')
            self.assertNotIn('certs/alias.example.com/privkey.pem', archive.namelist())

    def test_invalid_journal_id_cannot_select_arbitrary_transfer_directory(self):
        with self.assertRaises(ValueError):
            self.migration._journal_path('../other')


if __name__ == '__main__':
    unittest.main()
