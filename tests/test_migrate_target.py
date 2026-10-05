"""Destination migration isolation, payload validation and DNS SSL completion."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import warnings

from wpi import core
from wpi.migrate_target import TargetMigration
from wpi.web import WebStack

IDENT = 'abcdef123456'
MIGRATION = 'a' * 32
SITE = {'id': IDENT, 'primary': 'primary.example.com', 'aliases': ['alias.example.com'],
        'secondary': ['redirect.example.com'], 'tls': ['primary.example.com'],
        'root': f'/var/www/wpi/{IDENT}/public', 'email': 'owner@example.com',
        'db_name': f'wpi_{IDENT}', 'db_user': f'wpi_{IDENT}', 'admin': 'owner',
        'status': 'active', 'title': 'Original WordPress'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def public_tar(unsafe=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name, content in [('public/wp-config.php', b'<?php // original configuration'),
                              ('public/index.php', b'<?php // index'),
                              ('public/.maintenance', b'<?php $upgrading=1;'),
                              ('public/wp-content/uploads/photo.jpg', b'photo')]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        if unsafe:
            info = tarfile.TarInfo(unsafe[0])
            info.type = unsafe[1]
            info.linkname = '/etc/passwd'
            archive.addfile(info)
    return buffer.getvalue()


def payloads(site=SITE, unsafe=None, pma=False):
    config = {'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3'}
    if pma:
        config['phpmyadmin'] = {'domain': 'db.example.com', 'email': 'owner@example.com', 'user': 'panel'}
    prefix = f'backups/{site["id"]}'
    files = {'database.sql.gz': gzip.compress(b'CREATE TABLE wp_example (id INT);'),
             'files.tar.gz': public_tar(unsafe),
             'site.json': json.dumps(site).encode()}
    nested = {'site_id': site['id'], 'sha256': {name: digest(data) for name, data in files.items()}}
    files.update({'manifest.json': json.dumps(nested).encode(), 'COMPLETE': b''})
    payload = {f'{prefix}/{name}': data for name, data in files.items()}
    payload['metadata.json'] = json.dumps({'schema': 1, 'migration_id': MIGRATION,
                                         'source_config': config, 'sites': [site]}).encode()
    payload[f'credentials/{site["id"]}.json'] = json.dumps({
        'wordpress_admin': 'owner', 'wordpress_password': 'admin-secret-preserved',
        'database_user': site['db_user'], 'database_password': 'original-db-password'}).encode()
    if pma:
        payload['phpmyadmin/htpasswd'] = b'panel:$2y$10$' + b'A' * 53 + b'\n'
    return payload


def write_bundle(path, payload=None, extras=()):
    payload = payload or payloads()
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in payload.items():
            archive.writestr(name, data)
        archive.writestr('manifest.json', json.dumps({'sha256': {name: digest(data) for name, data in payload.items()}}))
        archive.writestr('COMPLETE', b'')
        for name, data in extras:
            archive.writestr(name, data)
    return digest(path.read_bytes())


class TargetImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.commands = []
        self.database_counts = [0, 0]

        def runner(argv, **kwargs):
            self.commands.append((argv, kwargs))
            sql = kwargs.get('input', '')
            output = ''
            if 'SELECT COUNT(*)' in sql:
                output = '\n'.join(map(str, self.database_counts)) + '\n'
            elif 'CREATE DATABASE' in sql:
                self.database_counts = [1, 1]
            elif argv[0] == 'curl':
                output = '200'
            return subprocess.CompletedProcess(argv, 0, output, '')

        self.manager = core.Manager(self.root / 'state', self.root / 'backups', runner)
        core.atomic_json(self.manager.data / 'config.json', {'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3'})
        self.manager.setup = Mock()
        self.manager.wp = Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.manager.restore_database = Mock()
        self.web = Mock(spec=WebStack)
        self.web.migration_tls = self.root / 'etc' / 'migration-tls'
        self.web.letsencrypt_ready.return_value = False
        self.web._atomic_file.side_effect = WebStack._atomic_file
        self.addCleanup(patch.stopall)
        patch.object(core.Manager, 'web', new_callable=unittest.mock.PropertyMock, return_value=self.web).start()
        patch.object(core, 'WWW', self.root / 'www').start()
        self.target = TargetMigration(self.manager)
        self.target.units = self.root / 'units'
        self.target.etc = self.root / 'etc'
        self.bundle = self.root / 'bundle.zip'
        self.sha = write_bundle(self.bundle)

    def import_bundle(self):
        return self.target.import_bundle(self.bundle, self.sha, MIGRATION)

    def test_real_file_import_preserves_content_roles_admin_and_rotates_database_secret(self):
        report = self.import_bundle()
        imported = self.manager.site(IDENT)
        self.assertEqual(imported['primary'], SITE['primary'])
        self.assertEqual(imported['aliases'], SITE['aliases'])
        self.assertEqual(imported['secondary'], SITE['secondary'])
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(set(report['ssl_pending']), set(core.site_hosts(SITE)))
        root = Path(imported['root'])
        self.assertEqual((root / 'wp-content/uploads/photo.jpg').read_bytes(), b'photo')
        self.assertFalse((root / '.maintenance').exists())
        credentials = json.loads((self.manager.data / 'credentials' / (IDENT + '.json')).read_text())
        self.assertEqual(credentials['wordpress_password'], 'admin-secret-preserved')
        self.assertNotEqual(credentials['database_password'], 'original-db-password')
        self.assertEqual(len(credentials['database_password']), 48)
        password_call = next(call for call in self.manager.wp.call_args_list if 'DB_PASSWORD' in call.args)
        self.assertIn('--prompt=value', password_call.args)
        self.assertNotIn(credentials['database_password'], password_call.args)
        self.assertEqual(password_call.kwargs['input'], credentials['database_password'] + '\n')
        self.manager.restore_database.assert_called_once()
        self.assertTrue((self.target.units / 'wpi-migration-ssl.timer').is_file())
        self.assertIn('migration-ssl-tick', (self.target.units / 'wpi-migration-ssl.service').read_text())
        self.assertEqual(imported['status'], 'active')

    def test_completed_import_retry_does_not_reimport_or_overwrite_current_site(self):
        self.import_bundle()
        current = self.manager.site(IDENT)
        current['title'] = 'Edited on target'
        self.manager.save_site(current)
        self.manager.wp.reset_mock()
        self.manager.restore_database.reset_mock()
        self.import_bundle()
        self.manager.wp.assert_not_called()
        self.manager.restore_database.assert_not_called()
        self.assertEqual(self.manager.site(IDENT)['title'], 'Edited on target')

    def test_wrong_zip_digest_is_rejected_before_setup_or_journal(self):
        with self.assertRaisesRegex(ValueError, 'Checksum ZIP'):
            self.target.import_bundle(self.bundle, '0' * 64, MIGRATION)
        self.manager.setup.assert_not_called()
        self.assertFalse(self.target.directory.exists())

    def test_insufficient_staging_disk_refuses_before_unzip_or_setup(self):
        with patch('wpi.migrate_target.shutil.disk_usage', return_value=Mock(free=0)):
            with self.assertRaisesRegex(ValueError, 'Ruang disk target'):
                self.import_bundle()
        self.manager.setup.assert_not_called()
        self.assertFalse(self.target.directory.exists())

    def test_shared_disk_space_sums_public_backup_and_sql_before_setup(self):
        from wpi.migrate_target import _SPACE_MARGIN
        checks = []

        def space(path):
            checks.append(path)
            if len(checks) == 1:
                return Mock(free=1024 ** 3)  # Transfer can be unpacked.
            # Each individual persistent allocation fits, but all three share
            # one disk and together must be rejected before apt/SQL changes.
            largest = max(self.target._validated_resources[IDENT].values())
            return Mock(free=_SPACE_MARGIN + largest + 1)

        with patch('wpi.migrate_target.shutil.disk_usage', side_effect=space):
            with self.assertRaisesRegex(ValueError, 'Ruang disk target'):
                self.import_bundle()
        self.assertEqual(len(checks), 2)
        self.manager.setup.assert_not_called()
        self.assertFalse(self.commands)

    def test_insufficient_persistent_disk_refuses_before_setup_or_database(self):
        with patch('wpi.migrate_target._require_space', side_effect=[None, ValueError('Ruang disk target')]):
            with self.assertRaisesRegex(ValueError, 'Ruang disk target'):
                self.import_bundle()
        self.manager.setup.assert_not_called()
        self.assertFalse(self.commands)

    def test_zip_traversal_duplicate_and_symlink_are_rejected_before_setup(self):
        cases = ['../outside', 'metadata.json', 'link']
        for name in cases:
            with self.subTest(name=name):
                write_bundle(self.bundle)
                with warnings.catch_warnings(), zipfile.ZipFile(self.bundle, 'a') as archive:
                    warnings.simplefilter('ignore', UserWarning)
                    if name == 'link':
                        info = zipfile.ZipInfo(name)
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        archive.writestr(info, b'/etc/passwd')
                    else:
                        archive.writestr(name, b'bad')
                with self.assertRaises(ValueError):
                    self.target.import_bundle(self.bundle, digest(self.bundle.read_bytes()), MIGRATION)
                self.manager.setup.assert_not_called()

    def test_nested_checksum_corruption_is_rejected_even_if_outer_hashes_match(self):
        payload = payloads()
        payload[f'backups/{IDENT}/database.sql.gz'] = gzip.compress(b'tampered sql')
        self.sha = write_bundle(self.bundle, payload)
        with self.assertRaisesRegex(ValueError, 'Checksum backup'):
            self.import_bundle()
        self.manager.setup.assert_not_called()

    def test_tar_traversal_symlink_hardlink_device_and_duplicate_are_rejected_before_setup(self):
        cases = [('public/../../outside', tarfile.DIRTYPE), ('public/link', tarfile.SYMTYPE),
                 ('public/hard', tarfile.LNKTYPE), ('public/device', tarfile.CHRTYPE),
                 ('public/wp-config.php', tarfile.DIRTYPE)]
        for unsafe in cases:
            with self.subTest(unsafe=unsafe):
                self.sha = write_bundle(self.bundle, payloads(unsafe=unsafe))
                with self.assertRaises(ValueError):
                    self.import_bundle()
                self.manager.setup.assert_not_called()

    def test_missing_manifest_payload_and_unknown_payload_are_rejected(self):
        self.sha = write_bundle(self.bundle, extras=[('unknown.txt', b'hello')])
        with self.assertRaisesRegex(ValueError, 'Manifest'):
            self.import_bundle()
        payload = payloads()
        payload['unexpected/root.key'] = b'secret'
        self.sha = write_bundle(self.bundle, payload)
        with self.assertRaisesRegex(ValueError, 'tidak dikenal'):
            self.import_bundle()
        self.manager.setup.assert_not_called()

    def test_existing_unrelated_site_root_and_credentials_refuse_import(self):
        extra = {**SITE, 'id': '123456abcdef', 'primary': 'other.example.com', 'aliases': [], 'secondary': []}
        self.manager.save_site(extra)
        with self.assertRaisesRegex(ValueError, 'sudah memiliki situs'):
            self.import_bundle()
        (self.manager.data / 'sites' / (extra['id'] + '.json')).unlink()
        root = self.target.www / IDENT
        root.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Direktori situs'):
            self.import_bundle()
        root.rmdir()
        credential = self.manager.data / 'credentials' / (IDENT + '.json')
        core.atomic_json(credential, {'database_password': 'unrelated'})
        with self.assertRaisesRegex(ValueError, 'Kredensial situs'):
            self.import_bundle()
        self.manager.setup.assert_not_called()

    def test_existing_unrelated_migration_certificate_is_not_overwritten(self):
        directory = self.web.migration_tls / SITE['primary']
        directory.mkdir(parents=True)
        certificate = directory / 'fullchain.pem'
        certificate.write_text('unrelated certificate')
        with self.assertRaisesRegex(ValueError, 'Sertifikat migrasi target sudah ada'):
            self.import_bundle()
        self.assertEqual(certificate.read_text(), 'unrelated certificate')
        self.manager.setup.assert_not_called()

    def test_database_or_user_collision_never_runs_create_drop_or_import(self):
        for collision in ([1, 0], [0, 1], [1, 1]):
            with self.subTest(collision=collision):
                self.database_counts = collision
                with self.assertRaisesRegex(ValueError, 'sudah ada'):
                    self.import_bundle()
                self.assertFalse(any('CREATE DATABASE' in call[1].get('input', '') for call in self.commands))
                self.manager.restore_database.assert_not_called()
                # Retry is still refused: failed import did not claim unrelated DB.
                with self.assertRaisesRegex(ValueError, 'sudah ada'):
                    self.import_bundle()

    def test_failed_import_resumes_owned_database_without_drop(self):
        self.manager.restore_database.side_effect = [RuntimeError('interrupted'), None]
        with self.assertRaises(RuntimeError):
            self.import_bundle()
        self.assertEqual(self.manager.site(IDENT)['status'], 'incomplete')
        report = self.import_bundle()
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(self.manager.restore_database.call_count, 2)
        self.assertTrue(any('--defaults-extra-file=' in ' '.join(call[0]) for call in self.commands))
        self.assertFalse(any('DROP ' in call[1].get('input', '') for call in self.commands))

    def test_frontend_php_error_or_transport_failure_keeps_import_incomplete(self):
        original_runner = self.manager.runner
        for returncode, status in ((0, '500'), (7, '000')):
            with self.subTest(returncode=returncode, status=status):
                def runner(argv, **kwargs):
                    if argv[0] == 'curl':
                        return subprocess.CompletedProcess(argv, returncode, status, '')
                    return original_runner(argv, **kwargs)

                self.manager.runner = runner
                with patch('wpi.migrate_target.time.sleep'), self.assertRaisesRegex(RuntimeError, 'belum merespons'):
                    self.import_bundle()
                journal = json.loads(self.target._journal_path(MIGRATION).read_text())
                self.assertEqual(journal['status'], 'incomplete')
                self.assertEqual(self.manager.site(IDENT)['status'], 'incomplete')
                self.assertTrue((Path(self.manager.site(IDENT)['root']) / 'wp-content/uploads/photo.jpg').is_file())
                self.assertEqual(self.database_counts, [1, 1])
                self.assertFalse(any('DROP ' in call[1].get('input', '') for call in self.commands))

    def test_frontend_health_uses_local_primary_and_does_not_follow_redirect(self):
        self.import_bundle()
        request = next(argv for argv, _ in self.commands if argv[0] == 'curl')
        self.assertIn('primary.example.com:80:127.0.0.1', request)
        self.assertIn('--noproxy', request)
        self.assertNotIn('--location', request)
        self.assertNotIn('-L', request)
        self.assertNotIn('alias.example.com', ' '.join(request))
        # Source's persisted HTTPS URL can redirect until pending DNS SSL is
        # issued; a local 301 is healthy and must not escape to the source.
        original_runner = self.manager.runner

        def redirect_runner(argv, **kwargs):
            if argv[0] == 'curl':
                return subprocess.CompletedProcess(argv, 0, '301', '')
            return original_runner(argv, **kwargs)

        self.manager.runner = redirect_runner
        self.target._frontend_health({**SITE, 'tls': [SITE['primary']]})

    def test_phpmyadmin_migrates_hashed_basic_auth_without_database_mutations(self):
        self.sha = write_bundle(self.bundle, payloads(pma=True))
        report = self.import_bundle()
        cfg = self.manager.config
        self.assertEqual(cfg['phpmyadmin']['domain'], 'db.example.com')
        self.assertIn('db.example.com', report['ssl_pending'])
        self.assertEqual((self.target.etc / 'wpi/pma.htpasswd').read_bytes(), b'panel:$2y$10$' + b'A' * 53 + b'\n')
        self.assertIn("['auth_type'] = 'cookie'", (self.target.etc / 'phpmyadmin/conf.d/wpi.php').read_text())
        self.web.install_phpmyadmin.assert_called_once()
        create = [call for call in self.commands if 'CREATE DATABASE' in call[1].get('input', '')]
        self.assertEqual(len(create), 1)

    def test_phpmyadmin_wrong_user_or_plaintext_auth_rejected_before_setup(self):
        payload = payloads(pma=True)
        payload['phpmyadmin/htpasswd'] = b'panel:plaintext\n'
        self.sha = write_bundle(self.bundle, payload)
        with self.assertRaisesRegex(ValueError, 'Basic Auth'):
            self.import_bundle()
        self.manager.setup.assert_not_called()

    def test_phpmyadmin_existing_unrelated_auth_rejected_before_setup(self):
        self.sha = write_bundle(self.bundle, payloads(pma=True))
        auth = self.target.etc / 'wpi/pma.htpasswd'
        auth.parent.mkdir(parents=True)
        auth.write_text('unrelated auth')
        with self.assertRaisesRegex(ValueError, 'bukan milik migrasi'):
            self.import_bundle()
        self.manager.setup.assert_not_called()

    def test_imported_certificate_hostname_and_matching_key_checked_by_openssl(self):
        executable = shutil.which('openssl')
        if not executable and os.name == 'nt':
            candidate = Path(r'C:\laragon\bin\git\usr\bin\openssl.exe')
            executable = str(candidate) if candidate.is_file() else None
        if not executable:
            self.skipTest('OpenSSL tidak tersedia pada host tes.')
        source = self.root / 'certs'
        source.mkdir()
        subprocess.run([executable, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '7',
                        '-subj', '/CN=primary.example.com', '-addext', 'subjectAltName=DNS:primary.example.com',
                        '-keyout', str(source / 'privkey.pem'), '-out', str(source / 'fullchain.pem')],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        original_runner = self.manager.runner

        def openssl_runner(argv, **kwargs):
            if argv[0] == 'openssl':
                kwargs.setdefault('capture_output', True)
                kwargs.setdefault('text', True)
                return subprocess.run([executable, *argv[1:]], **kwargs)
            return original_runner(argv, **kwargs)

        self.manager.runner = openssl_runner
        self.assertTrue(self.target._install_certificate('primary.example.com', source))
        self.assertEqual((self.web.migration_tls / 'primary.example.com/privkey.pem').read_bytes(),
                         (source / 'privkey.pem').read_bytes())
        self.assertFalse(self.target._install_certificate('wrong.example.com', source))
        subprocess.run([executable, 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:2048',
                        '-out', str(source / 'privkey.pem')], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertFalse(self.target._install_certificate('primary.example.com', source))


class MigrationSSLTests(unittest.TestCase):
    import_bundle = TargetImportTests.import_bundle

    def setUp(self):
        TargetImportTests.setUp(self)
        self.import_bundle()
        self.web.reset_mock()
        self.target._http_probe = Mock(return_value=False)

    def test_dns_not_cut_over_never_attempts_acme(self):
        report = self.target.ssl_tick()['migrations'][0]
        self.assertEqual(set(report['ssl'].values()), {'waiting_dns'})
        self.web.obtain_certificate.assert_not_called()
        self.assertEqual(self.manager.site(IDENT)['tls'], [])

    def test_acme_attempt_stores_hour_cooldown_and_failure_keeps_existing_site(self):
        self.target._http_probe.return_value = True
        self.web.obtain_certificate.side_effect = RuntimeError('private details')
        with patch('wpi.migrate_target.time.time', return_value=1000):
            first = self.target.ssl_tick()
            self.target.ssl_tick()
        self.assertEqual(self.web.obtain_certificate.call_count, 3)
        journal = json.loads(self.target._journal_path(MIGRATION).read_text())
        self.assertEqual({value['next_attempt'] for value in journal['ssl'].values()}, {4600})
        self.assertNotIn('private details', json.dumps(first))
        self.assertEqual(self.manager.site(IDENT)['tls'], [])

    def test_success_adds_tls_to_current_roles_and_deleted_domain_never_reappears(self):
        current = self.manager.site(IDENT)
        current['primary'] = 'alias.example.com'
        current['aliases'] = []  # former primary was deleted after migration.
        current['title'] = 'Target edit preserved'
        self.manager.save_site(current)
        self.target._http_probe.return_value = True
        report = self.target.ssl_tick()['migrations'][0]
        self.assertEqual(report['ssl']['primary.example.com'], 'removed')
        self.assertEqual(report['ssl']['alias.example.com'], 'ready')
        actual = self.manager.site(IDENT)
        self.assertEqual(actual['primary'], 'alias.example.com')
        self.assertEqual(actual['aliases'], [])
        self.assertEqual(actual['title'], 'Target edit preserved')
        self.assertEqual(set(actual['tls']), {'alias.example.com', 'redirect.example.com'})
        self.assertEqual(self.web.obtain_certificate.call_count, 2)

    def test_already_ready_certificate_activates_without_dns_or_acme(self):
        self.web.letsencrypt_ready.return_value = True
        report = self.target.ssl_tick()['migrations'][0]
        self.target._http_probe.assert_not_called()
        self.web.obtain_certificate.assert_not_called()
        self.assertEqual(report['ssl_pending'], [])
        self.assertEqual(set(self.manager.site(IDENT)['tls']), set(core.site_hosts(SITE)))

    def test_http_probe_exact_random_body_and_no_redirect_handlers(self):
        self.target._http_probe = TargetMigration._http_probe.__get__(self.target)
        root = Path(self.manager.site(IDENT)['root'])
        opener = Mock()

        def request(req, timeout):
            token = req.full_url.rsplit('/', 1)[1]
            payload = (root / '.well-known/acme-challenge' / token).read_bytes()
            response = Mock()
            response.status = 200
            response.read.return_value = payload
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=None)
            return response

        opener.open.side_effect = request
        with patch.object(core, 'check_dns'), patch('wpi.migrate_target.urllib.request.build_opener', return_value=opener) as build:
            self.assertTrue(self.target._http_probe(SITE['primary'], str(root)))
        self.assertEqual(list((root / '.well-known/acme-challenge').iterdir()), [])
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_unresolved_or_nonpublic_dns_returns_waiting_without_token(self):
        self.target._http_probe = TargetMigration._http_probe.__get__(self.target)
        root = Path(self.manager.site(IDENT)['root'])
        with patch.object(core, 'check_dns', side_effect=ValueError('DNS belum siap')):
            self.assertFalse(self.target._http_probe(SITE['primary'], str(root)))
        self.assertFalse((root / '.well-known').exists())


if __name__ == '__main__':
    unittest.main()
