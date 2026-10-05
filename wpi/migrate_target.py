"""Private destination import and post-DNS ACME completion for WPI migration.

The authenticated SSH caller supplies the ZIP digest. Neither archive metadata
nor file names are trusted before the entire payload and nested backup are
validated. Imported certificates are temporary HTTPS continuity, separate from
Certbot accounts and renewal configuration on the new server.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from . import core
from .core import atomic_json, domain, email_address, site_hosts

_ID = re.compile(r'[a-f0-9]{32}\Z')
_SITE = re.compile(r'[a-f0-9]{12}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_MAX_PAYLOAD = 2 * 1024 ** 4
_MAX_METADATA = 8 * 1024 ** 2
SERVICE_HEADER = '# Managed by WPI migration.\n'
_SPACE_MARGIN = 64 * 1024 ** 2


def _file_hash(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _relative_name(value):
    if not isinstance(value, str) or '\\' in value or '\x00' in value:
        raise ValueError('Nama berkas migrasi tidak aman.')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or str(path) != value:
        raise ValueError('Path berkas migrasi tidak aman.')
    return path


def _json_file(path):
    if Path(path).stat().st_size > _MAX_METADATA:
        raise ValueError('Metadata migrasi terlalu besar.')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _safe_directory(path):
    path = Path(path)
    for ancestor in [path, *path.parents]:
        if ancestor.is_symlink():
            raise ValueError('Direktori migrasi tidak boleh berupa symlink.')
        if ancestor.exists() and not ancestor.is_dir():
            raise ValueError('Direktori migrasi bukan direktori biasa.')
    return path


def _existing_directory(path):
    path = Path(path)
    while not path.exists():
        if path.parent == path:
            raise ValueError('Filesystem target tidak dapat diperiksa.')
        path = path.parent
    return path if path.is_dir() else path.parent


def _require_space(path, required):
    existing = _existing_directory(path)
    if shutil.disk_usage(existing).free < required + _SPACE_MARGIN:
        raise ValueError('Ruang disk target tidak cukup untuk migrasi. Tambah kapasitas disk sebelum mencoba lagi.')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TargetMigration:
    def __init__(self, manager):
        self.manager = manager
        self.directory = manager.data / 'migrations'
        self.www = Path(core.WWW)
        self.units = Path('/etc/systemd/system')
        self.etc = Path('/etc')
        self._validated_resources = {}

    def _journal_path(self, migration_id):
        if not isinstance(migration_id, str) or not _ID.fullmatch(migration_id):
            raise ValueError('ID migrasi tidak valid.')
        return self.directory / (migration_id + '.json')

    def _save(self, journal):
        _safe_directory(self.directory)
        path = self._journal_path(journal['migration_id'])
        if path.is_symlink():
            raise ValueError('Journal migrasi tidak boleh berupa symlink.')
        journal['updated_at'] = int(time.time())
        atomic_json(path, journal)

    def _validate_site(self, site):
        if not isinstance(site, dict) or not _SITE.fullmatch(str(site.get('id', ''))):
            raise ValueError('Identitas situs migrasi tidak valid.')
        ident = site['id']
        if site.get('root') != f'/var/www/wpi/{ident}/public':
            raise ValueError('Document root migrasi tidak cocok dengan identitas situs.')
        for role in ('aliases', 'secondary', 'tls'):
            if not isinstance(site.get(role, []), list):
                raise ValueError('Daftar domain migrasi tidak valid.')
        names = [site.get('primary'), *site.get('aliases', []), *site.get('secondary', [])]
        if any(not isinstance(host, str) or domain(host) != host for host in names):
            raise ValueError('Hostname migrasi harus normal dan valid.')
        if len(set(names)) != len(names) or len(set(site.get('tls', []))) != len(site.get('tls', [])):
            raise ValueError('Domain migrasi duplikat.')
        if not set(site.get('tls', [])) <= set(names):
            raise ValueError('TLS migrasi berisi domain yang tidak dimiliki situs.')
        email_address(site.get('email', ''))
        if site.get('db_name') != f'wpi_{ident}' or site.get('db_user') != f'wpi_{ident}':
            raise ValueError('Nama database/user migrasi harus identitas WPI.')
        if site.get('status') != 'active':
            raise ValueError('Hanya situs WordPress aktif yang dapat dimigrasikan.')
        return site

    def _validate_tar(self, path):
        names, total, wpconfig = set(), 0, False
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                name = member.name.rstrip('/') if member.isdir() else member.name
                relative = _relative_name(name)
                if relative.parts[0] != 'public' or name in names:
                    raise ValueError('Backup WordPress memiliki path duplikat/di luar public.')
                if not (member.isdir() or member.isfile()):
                    raise ValueError('Migrasi menolak symlink, hardlink, dan berkas khusus dalam backup.')
                if member.size < 0:
                    raise ValueError('Ukuran berkas backup tidak valid.')
                names.add(name)
                total += member.size
                if total > _MAX_PAYLOAD:
                    raise ValueError('Backup WordPress terlalu besar.')
                wpconfig |= member.isfile() and name == 'public/wp-config.php'
        if not wpconfig:
            raise ValueError('Backup WordPress tidak memiliki wp-config.php.')
        # A file used as an ancestor would make extraction order ambiguous.
        with tarfile.open(path, 'r:gz') as archive:
            files = {member.name for member in archive if member.isfile()}
        if any(str(parent) in files for name in names for parent in PurePosixPath(name).parents):
            raise ValueError('Path backup bertabrakan dengan berkas induk.')
        return total

    def _unpack_bundle(self, bundle, destination, expected_sha256, migration_id):
        if not isinstance(expected_sha256, str) or not _SHA.fullmatch(expected_sha256):
            raise ValueError('Checksum migrasi tidak valid.')
        self._journal_path(migration_id)
        if _file_hash(bundle) != expected_sha256:
            raise ValueError('Checksum ZIP migrasi tidak cocok.')
        with zipfile.ZipFile(bundle) as archive:
            infos = archive.infolist()
            expanded = sum(info.file_size for info in infos)
            if len(infos) > 10000 or expanded > _MAX_PAYLOAD:
                raise ValueError('Arsip migrasi terlalu besar.')
            _require_space(destination, expanded)
            names = set()
            for info in infos:
                _relative_name(info.filename)
                mode = info.external_attr >> 16
                if info.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
                    raise ValueError('Arsip migrasi hanya boleh berisi berkas biasa.')
                if info.filename in names or info.flag_bits & 1:
                    raise ValueError('Nama berkas duplikat/ZIP terenkripsi tidak diterima.')
                names.add(info.filename)
            if not {'metadata.json', 'manifest.json', 'COMPLETE'} <= names:
                raise ValueError('Bundle migrasi tidak lengkap.')
            for name in ('metadata.json', 'manifest.json'):
                if archive.getinfo(name).file_size > _MAX_METADATA:
                    raise ValueError('Metadata migrasi terlalu besar.')
            manifest = json.loads(archive.read('manifest.json'))
            hashes = manifest.get('sha256', manifest) if isinstance(manifest, dict) else None
            if not isinstance(hashes, dict) or set(hashes) != names - {'manifest.json', 'COMPLETE'}:
                raise ValueError('Manifest harus mencakup setiap berkas payload migrasi.')
            for name, digest in hashes.items():
                if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                    raise ValueError('Hash manifest migrasi tidak valid.')
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                result = hashlib.sha256()
                with archive.open(name) as source, target.open('xb') as output:
                    os.chmod(target, 0o600)
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        result.update(block)
                        output.write(block)
                if result.hexdigest() != digest:
                    raise ValueError('Checksum payload migrasi tidak cocok.')
        metadata = _json_file(destination / 'metadata.json')
        if (not isinstance(metadata, dict) or metadata.get('schema') != 1
                or metadata.get('migration_id') != migration_id):
            raise ValueError('Metadata/identitas migrasi tidak cocok.')
        config = metadata.get('source_config')
        sites = metadata.get('sites')
        if (not isinstance(config, dict) or config.get('stack') not in ('nginx', 'apache')
                or config.get('database') not in ('mysql', 'mariadb')
                or not isinstance(sites, list) or not sites):
            raise ValueError('Konfigurasi sumber migrasi tidak valid.')
        used, identities, allowed = set(), set(), {'metadata.json'}
        for site in sites:
            self._validate_site(site)
            ident = site['id']
            if ident in identities or used.intersection(site_hosts(site)):
                raise ValueError('Situs/domain sumber migrasi duplikat.')
            identities.add(ident)
            used.update(site_hosts(site))
            prefix = f'backups/{ident}'
            backup_names = {f'{prefix}/{name}' for name in (
                'database.sql.gz', 'files.tar.gz', 'site.json', 'manifest.json', 'COMPLETE')}
            if not backup_names <= set(hashes):
                raise ValueError('Backup situs migrasi tidak lengkap.')
            allowed.update(backup_names)
            folder = destination / prefix
            nested = _json_file(folder / 'manifest.json')
            expected_files = {'database.sql.gz', 'files.tar.gz', 'site.json'}
            if (not isinstance(nested, dict) or nested.get('site_id') != ident
                    or not isinstance(nested.get('sha256'), dict)
                    or set(nested['sha256']) != expected_files):
                raise ValueError('Manifest backup situs migrasi tidak valid.')
            for name in expected_files:
                if _file_hash(folder / name) != nested['sha256'][name]:
                    raise ValueError('Checksum backup situs migrasi tidak cocok.')
            if _json_file(folder / 'site.json') != site:
                raise ValueError('Metadata backup situs berbeda dengan sumber migrasi.')
            public_size = self._validate_tar(folder / 'files.tar.gz')
            # Read through gzip now, before apt/database mutations, to detect
            # truncated/corrupt SQL archives without retaining SQL in memory.
            import gzip
            total = 0
            with gzip.open(folder / 'database.sql.gz', 'rb') as sql:
                for block in iter(lambda: sql.read(1024 * 1024), b''):
                    total += len(block)
                    if total > _MAX_PAYLOAD:
                        raise ValueError('SQL migrasi terlalu besar.')
            self._validated_resources[ident] = {
                'public': public_size, 'sql': total,
                'backup': sum((folder / name).stat().st_size for name in (
                    'database.sql.gz', 'files.tar.gz', 'site.json', 'manifest.json', 'COMPLETE'))}
            credential_name = f'credentials/{ident}.json'
            if credential_name in hashes:
                credentials = _json_file(destination / credential_name)
                if not isinstance(credentials, dict) or any(not isinstance(value, str) for value in credentials.values()):
                    raise ValueError('Kredensial pemulihan migrasi tidak valid.')
                if not set(credentials) <= {'wordpress_admin', 'wordpress_password', 'database_user', 'database_password'}:
                    raise ValueError('Kredensial pemulihan memiliki field tidak dikenal.')
                allowed.add(credential_name)
            for host in site_hosts(site):
                pair = {f'certs/{host}/fullchain.pem', f'certs/{host}/privkey.pem'}
                if pair.intersection(hashes):
                    if not pair <= set(hashes):
                        raise ValueError('Pasangan sertifikat migrasi tidak lengkap.')
                    if any((destination / name).stat().st_size > _MAX_METADATA for name in pair):
                        raise ValueError('Berkas sertifikat migrasi terlalu besar.')
                    allowed.update(pair)
        if set(hashes) != allowed:
            pma = config.get('phpmyadmin')
            if pma:
                self._validate_pma(pma, used)
                auth_name = 'phpmyadmin/htpasswd'
                if auth_name not in hashes:
                    raise ValueError('Migrasi phpMyAdmin tidak memiliki Basic Auth.')
                allowed.add(auth_name)
                self._validate_pma_auth((destination / auth_name).read_text(), pma['user'])
                pair = {f'certs/{pma["domain"]}/fullchain.pem', f'certs/{pma["domain"]}/privkey.pem'}
                if pair.intersection(hashes):
                    if not pair <= set(hashes):
                        raise ValueError('Pasangan sertifikat phpMyAdmin tidak lengkap.')
                    if any((destination / name).stat().st_size > _MAX_METADATA for name in pair):
                        raise ValueError('Berkas sertifikat migrasi terlalu besar.')
                    allowed.update(pair)
            if set(hashes) != allowed:
                raise ValueError('Bundle migrasi berisi payload tidak dikenal.')
        elif config.get('phpmyadmin'):
            raise ValueError('Migrasi phpMyAdmin tidak memiliki Basic Auth.')
        return metadata

    def _preflight_space(self, extracted, metadata, journal):
        # Sum requirements sharing a filesystem: /tmp, /var/www and backups
        # may be one disk or separate mounts. SQL is a one-site-at-a-time temp.
        grouped = {}

        def reserve(path, amount):
            existing = _existing_directory(path)
            device = existing.stat().st_dev
            entry = grouped.setdefault(device, [existing, 0])
            entry[1] += amount

        sql_peak = 0
        for site in metadata['sites']:
            ident = site['id']
            progress = journal.get('sites', {}).get(ident, {}) if journal else {}
            if progress.get('status') == 'complete':
                continue
            resources = self._validated_resources[ident]
            reserve(self.www, resources['public'])
            backup = self.manager.backups / ident / ('migration-' + metadata['migration_id'])
            if not backup.exists():
                reserve(self.manager.backups, resources['backup'])
            sql_peak = max(sql_peak, resources['sql'])
        reserve(extracted, sql_peak)
        for path, required in grouped.values():
            _require_space(path, required)

    @staticmethod
    def _validate_pma(pma, used):
        if (not isinstance(pma, dict) or not isinstance(pma.get('domain'), str)
                or domain(pma['domain']) != pma['domain'] or pma['domain'] in used
                or not re.fullmatch(r'[a-zA-Z0-9_\-]{3,40}', str(pma.get('user', '')))):
            raise ValueError('Konfigurasi phpMyAdmin sumber tidak valid.')
        email_address(pma.get('email', ''))

    @staticmethod
    def _validate_pma_auth(content, username):
        if not re.fullmatch(re.escape(username) + r':\$2[aby]\$[0-9]{2}\$[A-Za-z0-9./]{53}\n?', content):
            raise ValueError('Basic Auth phpMyAdmin harus satu user bcrypt milik konfigurasi sumber.')

    def _preflight_target(self, metadata, journal):
        source_ids = {site['id'] for site in metadata['sites']}
        existing = self.manager.sites()
        migration_tls = (self.manager.web.migration_tls if self.manager.config
                         else Path('/etc/wpi/migration-tls'))
        _safe_directory(migration_tls)
        target_pma = self.manager.config.get('phpmyadmin')
        if target_pma and not (journal and journal.get('phpmyadmin', {}).get('status') in ('importing', 'complete')
                               and target_pma.get('migration_id') == journal['migration_id']):
            raise ValueError('Target memiliki phpMyAdmin; gunakan VPS bersih.')
        if not journal and existing:
            raise ValueError('Target sudah memiliki situs WPI. Migrasi memerlukan target kosong.')
        for site in existing:
            owned = (journal and site['id'] in source_ids
                     and site.get('migration_id') == journal['migration_id'])
            if not owned:
                raise ValueError('Target berisi situs yang bukan milik migrasi ini.')
            progress = journal.get('sites', {}).get(site['id'], {})
            if progress.get('status') != 'complete' and site.get('status') not in ('migrating', 'incomplete'):
                raise ValueError('Situs target sudah aktif; impor ulang ditolak.')
        for site in metadata['sites']:
            parent = _safe_directory(self.www / site['id'])
            progress = journal.get('sites', {}).get(site['id']) if journal else None
            if parent.exists() and not progress:
                raise ValueError('Direktori situs target sudah ada dan bukan milik migrasi.')
            credential = self.manager.data / 'credentials' / (site['id'] + '.json')
            if credential.is_symlink() or (credential.exists() and not progress):
                raise ValueError('Kredensial situs target sudah ada dan bukan milik migrasi.')
            for host in site_hosts(site):
                self.manager.ensure_free_domain(host, allow_site=site['id'] if progress else None)
                certificate_directory = _safe_directory(migration_tls / host)
                if certificate_directory.exists() and not progress:
                    raise ValueError('Sertifikat migrasi target sudah ada dan bukan milik migrasi ini.')
        if metadata['source_config'].get('phpmyadmin'):
            claimed = journal and journal.get('phpmyadmin', {}).get('files_claimed')
            for path in (self.etc / 'wpi/pma.htpasswd', self.etc / 'phpmyadmin/conf.d/wpi.php'):
                _safe_directory(path.parent)
                if path.is_symlink() or (path.exists() and not claimed):
                    raise ValueError('Konfigurasi phpMyAdmin target sudah ada dan bukan milik migrasi.')
            if (self.etc / 'apache2/conf-enabled/phpmyadmin.conf').exists():
                raise ValueError('Alias global phpMyAdmin target sudah aktif dan belum dikelola WPI.')
            certificate_directory = _safe_directory(migration_tls / metadata['source_config']['phpmyadmin']['domain'])
            if certificate_directory.exists() and not claimed:
                raise ValueError('Sertifikat phpMyAdmin target sudah ada dan bukan milik migrasi ini.')

    def _database_counts(self, site):
        result = self.manager.runner(['mysql', '--protocol=socket', '-uroot', '--batch', '--skip-column-names'],
            input=(f"SELECT COUNT(*) FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME='{site['db_name']}';\n"
                   f"SELECT COUNT(*) FROM mysql.user WHERE User='{site['db_user']}';\n"))
        try:
            values = [int(value) for value in (result.stdout or '').split()]
        except ValueError:
            raise RuntimeError('Database target tidak dapat diperiksa dengan aman.') from None
        if len(values) != 2 or any(value < 0 for value in values):
            raise RuntimeError('Database target tidak dapat diperiksa dengan aman.')
        return values

    def _authenticate_owned_database(self, site, password):
        with tempfile.TemporaryDirectory(prefix='wpi-db-check-') as directory:
            config = Path(directory) / 'client.cnf'
            config.write_text(f'[client]\nuser={site["db_user"]}\npassword={password}\nhost=localhost\n', encoding='utf-8')
            os.chmod(config, 0o600)
            result = self.manager.runner(['mysql', f'--defaults-extra-file={config}', '--protocol=socket',
                '--batch', '--skip-column-names', site['db_name']], input='SELECT 1;\n', check=False)
            if result.returncode:
                raise ValueError('Database/user target tidak cocok dengan kredensial migrasi; impor ditolak.')

    def _provision_database(self, site, journal, credentials):
        progress = journal['sites'][site['id']]
        database_count, user_count = self._database_counts(site)
        password = credentials['database_password']
        if progress.get('database_claimed'):
            if user_count:
                if database_count != 1 or user_count != 1:
                    raise ValueError('Identitas database target bertabrakan; impor ditolak.')
                self._authenticate_owned_database(site, password)
            elif database_count not in (0, 1):
                raise ValueError('Identitas database target bertabrakan; impor ditolak.')
        elif database_count or user_count:
            raise ValueError('Database/user target sudah ada; migrasi tidak akan menimpanya.')
        else:
            # Record the collision-free claim before CREATE, allowing a crash
            # between database/user commands to resume without dropping data.
            progress['database_claimed'] = True
            self._save(journal)
        sql = (f"CREATE DATABASE IF NOT EXISTS `{site['db_name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
               f"CREATE USER IF NOT EXISTS '{site['db_user']}'@'localhost' IDENTIFIED BY '{password}';\n"
               f"GRANT ALL PRIVILEGES ON `{site['db_name']}`.* TO '{site['db_user']}'@'localhost';\n")
        self.manager.runner(['mysql', '--protocol=socket', '-uroot'], input=sql)
        progress['database_provisioned'] = True
        self._save(journal)

    def _extract_public(self, archive_path, site):
        parent = _safe_directory(Path(site['root']).parent)
        parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(parent, 0o755)
        # Only a journal-owned incomplete destination can reach this method.
        staged = parent / 'migration-stage'
        _safe_directory(staged)
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(mode=0o755)
        with tarfile.open(archive_path, 'r:gz') as archive:
            for member in archive:
                path = staged.joinpath(*PurePosixPath(member.name).parts)
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True, mode=0o755)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    with archive.extractfile(member) as source, path.open('xb') as output:
                        shutil.copyfileobj(source, output)
                    os.chmod(path, 0o640 if path.name == 'wp-config.php' else 0o644)
        root = Path(site['root'])
        if root.exists():
            previous = parent / ('migration-previous-' + secrets.token_hex(4))
            root.rename(previous)
        (staged / 'public').rename(root)
        staged.rmdir()
        (root / '.maintenance').unlink(missing_ok=True)
        self.manager.runner(['chown', '-R', 'www-data:www-data', str(root)])
        self.manager.runner(['chown', 'root:root', str(parent)])
        self.manager.runner(['chmod', '755', str(self.www), str(parent), str(root)])

    def _install_certificate(self, host, source):
        web = self.manager.web
        certificate, key = source / 'fullchain.pem', source / 'privkey.pem'
        if not certificate.is_file():
            return False
        # Validate hostname, expiry and matching public keys without printing
        # private PEM bytes. Invalid/expired imports wait for the DNS ACME job.
        for arguments in (['x509', '-in', str(certificate), '-noout', '-checkhost', host],
                          ['x509', '-in', str(certificate), '-noout', '-checkend', '86400']):
            if self.manager.runner(['openssl', *arguments], check=False).returncode:
                return False
        public = self.manager.runner(['openssl', 'x509', '-in', str(certificate), '-pubkey', '-noout'], check=False)
        private = self.manager.runner(['openssl', 'pkey', '-in', str(key), '-passin', 'pass:', '-pubout'], check=False)
        if public.returncode or private.returncode or not public.stdout or public.stdout != private.stdout:
            return False
        directory = _safe_directory(web.migration_tls / host)
        _safe_directory(web.migration_tls)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(web.migration_tls, 0o700)
        os.chmod(directory, 0o700)
        for name in ('fullchain.pem', 'privkey.pem'):
            target = directory / name
            if target.is_symlink():
                raise ValueError('Sertifikat migrasi target tidak boleh berupa symlink.')
            web._atomic_file(target, (source / name).read_bytes(), 0o600)
        return True

    def _frontend_health(self, site):
        """Exercise PHP and active plugins/themes through the real web server.

        DNS still reaches the source, so resolve only the trusted Primary host
        to local loopback. Never follow redirects outside this destination.
        """
        secure = site['primary'] in site.get('tls', [])
        port, scheme = (443, 'https') if secure else (80, 'http')
        command = ['curl', '--resolve', f'{site["primary"]}:{port}:127.0.0.1',
                   '--noproxy', '*', '--insecure', '--max-time', '30', '--silent',
                   '--output', '/dev/null', '--write-out', '%{http_code}',
                   f'{scheme}://{site["primary"]}/']
        for attempt in range(5):
            response = self.manager.runner(command, check=False)
            if response.returncode == 0 and (response.stdout or '').strip() in (
                    '200', '301', '302', '303', '307', '308'):
                return
            if attempt < 4:
                time.sleep(0.2)
        raise RuntimeError('WordPress tujuan belum merespons dengan benar. Impor ditandai belum selesai; '
                           'periksa kompatibilitas plugin/theme dan layanan tujuan sebelum mengganti DNS.')

    def import_bundle(self, bundle_path, expected_sha256, migration_id):
        self._journal_path(migration_id)
        journal_path = self._journal_path(migration_id)
        if journal_path.is_symlink():
            raise ValueError('Journal migrasi tidak boleh berupa symlink.')
        journal = _json_file(journal_path) if journal_path.exists() else None
        if journal and (journal.get('direction') != 'target' or journal.get('migration_id') != migration_id
                        or journal.get('sha256') != expected_sha256):
            raise ValueError('Identitas/checksum tidak cocok dengan migrasi yang sudah dimulai.')
        with tempfile.TemporaryDirectory(prefix='wpi-import-') as temporary:
            extracted = Path(temporary)
            metadata = self._unpack_bundle(Path(bundle_path), extracted, expected_sha256, migration_id)
            self._preflight_target(metadata, journal)
            self._preflight_space(extracted, metadata, journal)
            self.manager.setup(metadata['source_config']['stack'], metadata['source_config']['database'])
            if not journal:
                journal = {'schema': 1, 'direction': 'target', 'migration_id': migration_id,
                           'sha256': expected_sha256, 'status': 'importing', 'sites': {}, 'ssl': {}}
                self._save(journal)
            try:
                for original in metadata['sites']:
                    ident = original['id']
                    progress = journal['sites'].setdefault(ident, {'status': 'importing'})
                    if progress['status'] == 'complete':
                        continue
                    self._save(journal)
                    site = copy.deepcopy(original)
                    site.update({'root': str(self.www / ident / 'public'), 'status': 'migrating',
                                 'migration_id': migration_id, 'tls': []})
                    credential_path = self.manager.data / 'credentials' / (ident + '.json')
                    if credential_path.exists():
                        credentials = _json_file(credential_path)
                        if not re.fullmatch(r'[a-f0-9]{48}', str(credentials.get('database_password', ''))):
                            raise ValueError('Kredensial database migrasi tidak valid.')
                    else:
                        source_path = extracted / 'credentials' / (ident + '.json')
                        source = _json_file(source_path) if source_path.exists() else {}
                        credentials = {name: source[name] for name in ('wordpress_admin', 'wordpress_password') if name in source}
                        credentials.update(database_user=site['db_user'], database_password=secrets.token_hex(24))
                        atomic_json(credential_path, credentials)
                    self.manager.save_site(site)
                    self._save(journal)
                    self._provision_database(site, journal, credentials)
                    folder = extracted / 'backups' / ident
                    self._extract_public(folder / 'files.tar.gz', site)
                    for key, value in (('DB_NAME', site['db_name']), ('DB_USER', site['db_user']), ('DB_HOST', 'localhost')):
                        self.manager.wp(site, 'config', 'set', key, value)
                    self.manager.wp(site, 'config', 'set', 'DB_PASSWORD', '--prompt=value',
                                    input=credentials['database_password'] + '\n')
                    self.manager.runner(['chmod', '640', str(Path(site['root']) / 'wp-config.php')])
                    backup = self.manager.backups / ident / ('migration-' + migration_id)
                    _safe_directory(backup)
                    if backup.exists():
                        for name in ('database.sql.gz', 'files.tar.gz', 'site.json', 'manifest.json', 'COMPLETE'):
                            if (backup / name).is_symlink() or _file_hash(backup / name) != _file_hash(folder / name):
                                raise ValueError('Backup migrasi target sudah ada dan berbeda.')
                    else:
                        backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        shutil.copytree(folder, backup)
                    self.manager.restore_database(site, backup)
                    self.manager.wp(site, 'core', 'is-installed')
                    for host in site_hosts(site):
                        if self._install_certificate(host, extracted / 'certs' / host) or self.manager.web.letsencrypt_ready(host):
                            site['tls'].append(host)
                        journal['ssl'].setdefault(host, {'site_id': ident, 'status': 'pending', 'next_attempt': 0})
                    self.manager.web.write_site(site)
                    self._frontend_health(site)
                    site['status'] = 'active'
                    self.manager.save_site(site)
                    progress['status'] = 'complete'
                    self._save(journal)
                if metadata['source_config'].get('phpmyadmin'):
                    self._import_pma(metadata['source_config']['phpmyadmin'], extracted, journal)
                journal['status'] = 'ready'
                self._save(journal)
                self.install_ssl_timer()
            except BaseException:
                journal['status'] = 'incomplete'
                for ident, progress in journal['sites'].items():
                    if progress['status'] != 'complete':
                        try:
                            failed = self.manager.site(ident)
                            failed['status'] = 'incomplete'
                            self.manager.save_site(failed)
                        except ValueError:
                            pass
                self._save(journal)
                raise
        return self._report(journal)

    def _import_pma(self, original, extracted, journal):
        progress = journal.setdefault('phpmyadmin', {'status': 'importing'})
        if progress['status'] == 'complete':
            return
        self._save(journal)
        auth = self.etc / 'wpi' / 'pma.htpasswd'
        conf = self.etc / 'phpmyadmin' / 'conf.d' / 'wpi.php'
        for path in (auth, conf):
            _safe_directory(path.parent)
            if path.is_symlink():
                raise ValueError('Konfigurasi phpMyAdmin target tidak boleh berupa symlink.')
        if not progress.get('files_claimed') and (auth.exists() or conf.exists()):
            raise ValueError('Konfigurasi phpMyAdmin target sudah ada dan bukan milik migrasi.')
        progress['files_claimed'] = True
        self._save(journal)
        global_alias = self.etc / 'apache2' / 'conf-enabled' / 'phpmyadmin.conf'
        if global_alias.exists():
            raise ValueError('Alias global phpMyAdmin target sudah aktif dan belum dikelola WPI.')
        self.manager.runner(['debconf-set-selections'], input='phpmyadmin phpmyadmin/dbconfig-install boolean false\n'
                            'phpmyadmin phpmyadmin/reconfigure-webserver multiselect\n')
        self.manager.runner(['apt-get', 'install', '-y', '--no-install-recommends', 'phpmyadmin'])
        if global_alias.exists():
            self.manager.runner(['a2disconf', 'phpmyadmin'])
        auth.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(auth.parent, 0o755)
        conf.parent.mkdir(parents=True, exist_ok=True)
        self.manager.web._atomic_file(auth, (extracted / 'phpmyadmin' / 'htpasswd').read_bytes(), 0o640)
        php = ("<?php\n$cfg['blowfish_secret'] = '" + secrets.token_hex(16) + "';\n"
               "$cfg['Servers'][1]['auth_type'] = 'cookie';\n"
               "$cfg['Servers'][1]['host'] = 'localhost';\n"
               "$cfg['Servers'][1]['AllowNoPassword'] = false;\n"
               "$cfg['AllowArbitraryServer'] = false;\n")
        self.manager.web._atomic_file(conf, php.encode(), 0o640)
        self.manager.runner(['chown', 'root:www-data', str(auth), str(conf)])
        acme = _safe_directory(self.www / 'pma-acme')
        acme.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(acme, 0o755)
        host = original['domain']
        self._install_certificate(host, extracted / 'certs' / host)
        self.manager.web.install_phpmyadmin(host, '/usr/share/phpmyadmin', str(auth))
        config = self.manager.config
        config['phpmyadmin'] = {**original, 'migration_id': journal['migration_id']}
        atomic_json(self.manager.data / 'config.json', config)
        journal['ssl'].setdefault(host, {'kind': 'phpmyadmin', 'status': 'pending', 'next_attempt': 0})
        progress['status'] = 'complete'
        self._save(journal)

    def install_ssl_timer(self):
        _safe_directory(self.units)
        self.units.mkdir(parents=True, exist_ok=True)
        service = (SERVICE_HEADER + '[Unit]\nDescription=WPI migration automatic SSL after DNS cutover\n'
                   'After=network-online.target\nWants=network-online.target\n\n'
                   '[Service]\nType=oneshot\nUMask=0077\n'
                   'ExecStart=/usr/local/bin/wpi migration-ssl-tick\n')
        timer = (SERVICE_HEADER + '[Unit]\nDescription=Retry WPI migration SSL automatically\n\n'
                 '[Timer]\nOnBootSec=1min\nOnUnitInactiveSec=5min\nRandomizedDelaySec=30\n'
                 'Persistent=true\n\n[Install]\nWantedBy=timers.target\n')
        for name, value in (('wpi-migration-ssl.service', service), ('wpi-migration-ssl.timer', timer)):
            path = self.units / name
            if path.is_symlink() or (path.exists() and not path.read_text().startswith(SERVICE_HEADER)):
                raise ValueError('Unit migrasi target bukan milik WPI.')
            self.manager.web._atomic_file(path, value.encode(), 0o644)
        self.manager.runner(['systemctl', 'daemon-reload'])
        self.manager.runner(['systemctl', 'enable', '--now', 'wpi-migration-ssl.timer'])

    def _http_probe(self, host, root):
        try:
            core.check_dns(host)
        except (ValueError, OSError):
            return False
        root = _safe_directory(Path(root))
        directory = _safe_directory(root / '.well-known' / 'acme-challenge')
        directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        os.chmod(directory.parent, 0o755)
        os.chmod(directory, 0o755)
        token = 'wpi-migration-' + secrets.token_hex(16)
        payload = secrets.token_hex(32).encode()
        path = directory / token
        with path.open('xb') as output:
            os.chmod(path, 0o644)
            output.write(payload)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(f'http://{host}/.well-known/acme-challenge/{token}',
                                         headers={'User-Agent': 'WPI-migration/1', 'Cache-Control': 'no-cache'})
        try:
            with opener.open(request, timeout=10) as response:
                return response.status == 200 and response.read(1024) == payload
        except (urllib.error.URLError, OSError, ValueError):
            return False
        finally:
            path.unlink(missing_ok=True)

    def ssl_tick(self):
        """Caller holds operation_lock; only current, still-owned hosts change."""
        reports = []
        now = int(time.time())
        for path in sorted(self.directory.glob('*.json')):
            if path.is_symlink():
                continue
            journal = _json_file(path)
            if journal.get('direction') != 'target' or journal.get('status') != 'ready':
                continue
            for host, pending in journal.get('ssl', {}).items():
                domain(host)
                if pending.get('kind') == 'phpmyadmin':
                    self._pma_ssl_tick(host, pending, journal, now)
                    continue
                try:
                    site = self.manager.site(pending['site_id'])
                except ValueError:
                    pending['status'] = 'removed'
                    continue
                if (site.get('migration_id') != journal['migration_id'] or host not in site_hosts(site)):
                    pending['status'] = 'removed'
                    continue
                web = self.manager.web
                if pending.get('status') == 'ready' and web.letsencrypt_ready(host):
                    continue
                if now < pending.get('next_attempt', 0):
                    continue
                try:
                    # Even a pre-existing destination cert requires current
                    # vhost metadata activation, but avoids an ACME request.
                    if not web.letsencrypt_ready(host):
                        if not self._http_probe(host, site['root']):
                            pending.update(status='waiting_dns', next_attempt=now + 300)
                            continue
                        # Persist cooldown before invoking Certbot so a killed
                        # request cannot retry every five minutes.
                        pending.update(status='issuing', next_attempt=now + 3600)
                        self._save(journal)
                        web.obtain_certificate(host, site['email'], site['root'])
                    current = self.manager.site(site['id'])
                    if host not in site_hosts(current) or current.get('migration_id') != journal['migration_id']:
                        pending['status'] = 'removed'
                        continue
                    if host not in current.get('tls', []):
                        current.setdefault('tls', []).append(host)
                    web.write_site(current)
                    self.manager.save_site(current)
                    pending.update(status='ready', next_attempt=0)
                except Exception:
                    # The timer's report deliberately omits command output,
                    # exception strings and certificate/account credentials.
                    pending.update(status='retry', next_attempt=now + 3600)
            self._save(journal)
            reports.append(self._report(journal))
        return {'migrations': reports}

    def _pma_ssl_tick(self, host, pending, journal, now):
        pma = self.manager.config.get('phpmyadmin', {})
        if pma.get('domain') != host or pma.get('migration_id') != journal['migration_id']:
            pending['status'] = 'removed'
            return
        web = self.manager.web
        if pending.get('status') == 'ready' and web.letsencrypt_ready(host):
            return
        if now < pending.get('next_attempt', 0):
            return
        try:
            if not web.letsencrypt_ready(host):
                acme = self.www / 'pma-acme'
                if not self._http_probe(host, str(acme)):
                    pending.update(status='waiting_dns', next_attempt=now + 300)
                    return
                pending.update(status='issuing', next_attempt=now + 3600)
                self._save(journal)
                web.obtain_certificate(host, pma['email'], str(acme))
            web.install_phpmyadmin(host, '/usr/share/phpmyadmin', str(self.etc / 'wpi' / 'pma.htpasswd'))
            pending.update(status='ready', next_attempt=0)
        except Exception:
            pending.update(status='retry', next_attempt=now + 3600)

    def _report(self, journal):
        sites = []
        for ident, progress in journal.get('sites', {}).items():
            try:
                current = self.manager.site(ident)
                sites.append({'id': ident, 'primary': current['primary'], 'aliases': current.get('aliases', []),
                              'secondary': current.get('secondary', []), 'status': progress['status']})
            except ValueError:
                sites.append({'id': ident, 'status': progress['status']})
        pending = [host for host, value in journal.get('ssl', {}).items()
                   if value.get('status') not in ('ready', 'removed')]
        return {'migration_id': journal['migration_id'], 'status': journal['status'], 'sites': sites,
                'ssl_pending': pending,
                'phpmyadmin': journal.get('phpmyadmin', {}).get('status'),
                'ssl': {host: value['status'] for host, value in journal.get('ssl', {}).items()}}

    def status(self):
        return {'migrations': [self._report(journal) for path in sorted(self.directory.glob('*.json'))
                              if not path.is_symlink() and (journal := _json_file(path)).get('direction') == 'target']}
