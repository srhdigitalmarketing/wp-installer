"""Conservative recovery of WPI WordPress configuration and runtime.

Recovery changes only wp-config.php and managed service/vhost state. A PHP
parse error is checked without executing the file. Existing content and SQL
are never rolled back, and plugin/theme failures remain visible as unresolved.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile
import tempfile
import time

from . import core
from .autotune import AutoTuner

MAX_CONFIG = 1024 * 1024
_ID = re.compile(r'[a-f0-9]{12}\Z')
_DB = re.compile(r'wpi_[a-f0-9]{12}\Z')
_PREFIX = re.compile(r'[A-Za-z0-9_]{1,64}\Z')
_GOOD_HTTP = {200, 201, 202, 204, 301, 302, 303, 307, 308}


def _no_links(path):
    path = Path(path)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Menolak symlink pada path pemulihan WPI.')
    return path


def _private_directory(path):
    path = _no_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _private_file(path, content):
    path = _no_links(path)
    # Unique names and exclusive creation prevent overwriting an existing file.
    with path.open('xb') as output:
        os.chmod(path, 0o600)
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


class SiteRepair:
    def __init__(self, manager):
        self.manager = manager
        self.runner = manager.runner

    def _site(self, identifier):
        site = self.manager.site(identifier)
        if not _ID.fullmatch(str(site.get('id', ''))):
            raise ValueError('ID situs tidak valid untuk repair.')
        root = Path(site['root'])
        if root != core.WWW / site['id'] / 'public':
            raise ValueError('Repair hanya untuk document root yang dikelola WPI.')
        _no_links(root)
        _no_links(self.manager.data)
        if not root.is_dir():
            raise ValueError('Document root WordPress tidak tersedia.')
        for name in ('wp-config.php', 'index.php', 'wp-settings.php'):
            path = _no_links(root / name)
            if path.exists() and not path.is_file():
                raise ValueError('Berkas inti WordPress tidak valid.')
        return site

    @property
    def php(self):
        version = self.manager.config.get('php_version')
        if version not in ('8.1', '8.3'):
            raise ValueError('Versi PHP WPI tidak valid.')
        return version

    def _check(self, argv, **kwargs):
        try:
            result = self.runner(argv, check=False, **kwargs)
            return result.returncode == 0, result
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return False, None

    def _lint(self, path):
        path = _no_links(path)
        if not path.is_file() or path.stat().st_size > MAX_CONFIG:
            return False
        # -n ignores php.ini and -l parses only; wp-config is never executed
        # by root while diagnosing a possible syntax error.
        return self._check([f'/usr/bin/php{self.php}', '-n', '-l', str(path)])[0]

    def _installed(self, site):
        try:
            return self.manager.wp(site, 'core', 'is-installed', check=False,
                                   timeout=45).returncode == 0
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return False

    def _http(self, site, suffix):
        tls = site['primary'] in site.get('tls', [])
        scheme, port = ('https', 443) if tls else ('http', 80)
        argv = ['curl', '--silent', '--show-error', '--noproxy', '*',
                '--connect-timeout', '5', '--max-time', '20', '--output', '/dev/null',
                '--write-out', '%{http_code}', '--resolve',
                f'{site["primary"]}:{port}:127.0.0.1',
                f'{scheme}://{site["primary"]}{suffix}']
        ok, result = self._check(argv, timeout=25)
        value = str(result.stdout or '').strip() if result is not None else ''
        code = int(value) if re.fullmatch(r'\d{3}', value) else 0
        return {'status': code, 'ok': ok and code in _GOOD_HTTP,
                'transport_ok': ok, 'scheme': scheme}

    def diagnose(self, identifier):
        site = self._site(identifier)
        config_ok = self._lint(Path(site['root']) / 'wp-config.php')
        db = 'mariadb' if self.manager.config.get('database') == 'mariadb' else 'mysql'
        checks = {
            'config_syntax': config_ok,
            'wordpress_installed': self._installed(site) if config_ok else False,
            'database_service': self._check(['systemctl', 'is-active', db])[0],
            'fpm_config': self._check([f'/usr/sbin/php-fpm{self.php}', '-t'])[0],
            'fpm_service': self._check(['systemctl', 'is-active', f'php{self.php}-fpm'])[0],
            'web_config': self._check(['nginx', '-t'] if self.manager.config['stack'] == 'nginx'
                                      else ['apache2ctl', 'configtest'])[0],
            'web_service': self._check(['systemctl', 'is-active', self.manager.web.service])[0],
            'frontend': self._http(site, '/'),
            'admin': self._http(site, '/wp-admin/'),
        }
        return {'site_id': site['id'], 'primary': site['primary'], 'checks': checks,
                'status': 'healthy' if self._healthy(checks) else 'unresolved'}

    @staticmethod
    def _healthy(checks):
        return all(checks.get(name) for name in ('config_syntax', 'wordpress_installed',
                   'database_service', 'fpm_config', 'fpm_service', 'web_config', 'web_service')) \
            and checks['frontend']['ok'] and checks['admin']['ok']

    def _settled_diagnosis(self, identifier, site):
        report = self.diagnose(identifier)
        checks = report['checks']
        # A successful systemctl return can precede the first ready request
        # from new FPM/web workers. Retry HTTP briefly only after structural,
        # database and service checks have all passed. Read-only diagnose does
        # not wait/retry, and persistent application failures remain failures.
        ready = all(checks.get(name) for name in ('config_syntax', 'wordpress_installed',
                    'database_service', 'fpm_config', 'fpm_service', 'web_config', 'web_service'))
        if ready:
            for _ in range(4):
                if checks['frontend']['ok'] and checks['admin']['ok']:
                    break
                time.sleep(0.2)
                checks['frontend'] = self._http(site, '/')
                checks['admin'] = self._http(site, '/wp-admin/')
        report['status'] = 'healthy' if self._healthy(checks) else 'unresolved'
        return report

    def remember_config(self, identifier):
        """Save a syntax-valid, database-loadable configuration, without SQL/media."""
        site = self._site(identifier)
        path = Path(site['root']) / 'wp-config.php'
        if not self._lint(path) or not self._installed(site):
            raise ValueError('Konfigurasi gagal validasi; snapshot config tidak dibuat.')
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        directory = _private_directory(self.manager.data / 'config-snapshots' / site['id'])
        # Immutable snapshots are associated with current identities; the
        # domain metadata is descriptive and is never restored during repair.
        for folder in sorted(directory.iterdir(), reverse=True):
            try:
                metadata = self._snapshot_metadata(site, folder)
                if metadata['sha256'] == digest:
                    return folder
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                continue
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        folder = _private_directory(directory / (stamp + '-' + secrets.token_hex(3)))
        _private_file(folder / 'wp-config.php', content)
        metadata = {'site_id': site['id'], 'db_name': site['db_name'],
                    'db_user': site['db_user'], 'primary': site['primary'],
                    'sha256': digest, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        _private_file(folder / 'manifest.json', (json.dumps(metadata, indent=2) + '\n').encode())
        _private_file(folder / 'COMPLETE', b'')
        return folder

    def _snapshot_metadata(self, site, folder):
        folder = _no_links(folder)
        expected = self.manager.data / 'config-snapshots' / site['id']
        if folder.parent != expected or not folder.is_dir():
            raise ValueError('Snapshot config di luar direktori situs.')
        for name in ('COMPLETE', 'manifest.json', 'wp-config.php'):
            path = _no_links(folder / name)
            if not path.is_file():
                raise ValueError('Snapshot config tidak lengkap.')
        if (folder / 'wp-config.php').stat().st_size > MAX_CONFIG:
            raise ValueError('Snapshot config terlalu besar.')
        metadata = json.loads((folder / 'manifest.json').read_text())
        if not isinstance(metadata, dict):
            raise ValueError('Manifest snapshot config tidak valid.')
        if (metadata['site_id'], metadata['db_name'], metadata['db_user']) != \
                (site['id'], site['db_name'], site['db_user']):
            raise ValueError('Snapshot config milik identitas database lain.')
        if metadata['sha256'] != core.Manager.file_hash(folder / 'wp-config.php'):
            raise ValueError('Checksum snapshot config tidak cocok.')
        return metadata

    def _latest_snapshot(self, site):
        directory = _no_links(self.manager.data / 'config-snapshots' / site['id'])
        if directory.exists():
            for folder in sorted(directory.iterdir(), reverse=True):
                try:
                    self._snapshot_metadata(site, folder)
                    if self._lint(folder / 'wp-config.php'):
                        return folder
                except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                    continue
        return None

    def _preserve_config(self, site):
        path = Path(site['root']) / 'wp-config.php'
        if not path.exists():
            return None
        if path.stat().st_size > MAX_CONFIG:
            raise ValueError('wp-config melebihi ukuran aman untuk pemulihan.')
        base = _private_directory(self.manager.data / 'config-repairs' / site['id'])
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%f')
        folder = _private_directory(base / (stamp + '-' + secrets.token_hex(3)))
        _private_file(folder / 'wp-config.php', path.read_bytes())
        _private_file(folder / 'manifest.json', (json.dumps({
            'site_id': site['id'], 'sha256': core.Manager.file_hash(path),
            'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2) + '\n').encode())
        return folder

    def _credentials(self, site):
        path = _no_links(self.manager.data / 'credentials' / (site['id'] + '.json'))
        credentials = json.loads(path.read_text())
        if not isinstance(credentials, dict) or not _DB.fullmatch(str(site.get('db_name', ''))) \
                or not _DB.fullmatch(str(site.get('db_user', ''))) \
                or credentials.get('database_user') != site['db_user'] \
                or not re.fullmatch(r'[a-f0-9]{48}', str(credentials.get('database_password', ''))):
            raise ValueError('Kredensial database WPI tidak cocok; config tidak diubah.')
        return credentials

    def _prefix(self, site):
        if not _DB.fullmatch(str(site.get('db_name', ''))):
            raise ValueError('Identitas database WPI tidak valid.')
        query = ("SELECT TABLE_NAME FROM information_schema.TABLES "
                 f"WHERE TABLE_SCHEMA = '{site['db_name']}';")
        ok, result = self._check(['mysql', '--protocol=socket', '-uroot', '--batch',
                                 '--skip-column-names'], input=query, timeout=30)
        if not ok:
            raise ValueError('Tabel database belum dapat diverifikasi; config tidak dibuat ulang.')
        tables = set(str(result.stdout or '').splitlines())
        suffixes = {'options', 'users', 'usermeta', 'posts', 'postmeta', 'terms',
                    'term_taxonomy', 'term_relationships', 'comments', 'commentmeta'}
        candidates = {name[:-len('options')] for name in tables if name.endswith('options')}
        candidates = {prefix for prefix in candidates if _PREFIX.fullmatch(prefix)
                      and all(prefix + suffix in tables for suffix in suffixes)}
        if len(candidates) != 1:
            raise ValueError('Prefix tabel WordPress tidak unik/terbukti; config tidak dibuat ulang.')
        return candidates.pop()

    def _backup_config(self, site, destination):
        base = _no_links(self.manager.backups / site['id'])
        if not base.is_dir():
            return None
        for folder in sorted(base.iterdir(), reverse=True):
            try:
                _no_links(folder)
                for name in ('COMPLETE', 'manifest.json', 'site.json', 'files.tar.gz', 'database.sql.gz'):
                    _no_links(folder / name)
                folder = self.manager.verified_backup(site, folder)
                metadata = json.loads((folder / 'site.json').read_text())
                if not isinstance(metadata, dict):
                    continue
                if (metadata['id'], metadata['root'], metadata['db_name'], metadata['db_user']) != \
                        (site['id'], site['root'], site['db_name'], site['db_user']):
                    continue
                # Extract exactly one regular config file, never the database,
                # media, plugins, metadata or a tar link.
                with tarfile.open(folder / 'files.tar.gz', 'r:gz') as archive:
                    members = [m for m in archive.getmembers() if m.name == 'public/wp-config.php']
                    if len(members) != 1 or not members[0].isfile() or members[0].size > MAX_CONFIG:
                        continue
                    with archive.extractfile(members[0]) as handle:
                        content = handle.read(MAX_CONFIG + 1)
                if len(content) > MAX_CONFIG:
                    continue
                destination.write_bytes(content)
                os.chmod(destination, 0o600)
                if self._lint(destination):
                    return str(folder)
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError, tarfile.TarError):
                continue
        destination.unlink(missing_ok=True)
        return None

    def _config_set(self, site, candidate, key, value, private=False, raw=False):
        args = ['config', 'set', key]
        kwargs = {'timeout': 45}
        if private:
            args.append('--prompt')
            kwargs['input'] = value + '\n'
        else:
            args.append(value)
        if raw:
            args.append('--raw')
        args.append(f'--config-file={candidate}')
        self.manager.wp(site, *args, **kwargs)

    def _has(self, site, candidate, key):
        try:
            return self.manager.wp(site, 'config', 'has', key,
                                   f'--config-file={candidate}', check=False,
                                   timeout=45).returncode == 0
        except (RuntimeError, OSError, subprocess.SubprocessError):
            return False

    def _memory(self):
        from .php_settings import PHPSettings
        return str(PHPSettings(self.manager).effective()['memory_mib']) + 'M'

    def _overlay(self, site, candidate, credentials, fresh=False):
        self.runner(['chown', 'www-data:www-data', str(candidate)])
        for key, value in (('DB_NAME', site['db_name']), ('DB_USER', site['db_user']),
                           ('DB_HOST', 'localhost')):
            self._config_set(site, candidate, key, value)
        self._config_set(site, candidate, 'DB_PASSWORD', credentials['database_password'], private=True)
        url = ('https' if site['primary'] in site.get('tls', []) else 'http') + '://' + site['primary']
        for key in ('WP_HOME', 'WP_SITEURL'):
            if self._has(site, candidate, key):
                self._config_set(site, candidate, key, url)
        # Repair only known memory constants. Keep all other custom PHP from
        # a validated recovery candidate and keep the SQL options untouched.
        memory = self._memory()
        for key in ('WP_MEMORY_LIMIT', 'WP_MAX_MEMORY_LIMIT'):
            if fresh or self._has(site, candidate, key):
                self._config_set(site, candidate, key, memory)
        if fresh:
            self._config_set(site, candidate, 'DISALLOW_FILE_EDIT', 'true', raw=True)
            self._config_set(site, candidate, 'WP_AUTO_UPDATE_CORE', 'minor')
        if not self._lint(candidate):
            raise ValueError('Kandidat config gagal PHP lint; config aktif tidak diubah.')
        self.runner(['chown', 'www-data:www-data', str(candidate)])
        os.chmod(candidate, 0o640)

    def _recover_config(self, site):
        credentials = self._credentials(site)
        root = Path(site['root'])
        preserved = self._preserve_config(site)
        with tempfile.TemporaryDirectory(prefix='.wpi-repair-', dir=root.parent) as temp:
            stage = _no_links(Path(temp))
            self.runner(['chown', 'www-data:www-data', str(stage)])
            os.chmod(stage, 0o700)
            candidate = stage / 'wp-config.php'
            snapshot = self._latest_snapshot(site)
            source = None
            if snapshot:
                candidate.write_bytes((snapshot / 'wp-config.php').read_bytes())
                source = str(snapshot)
            else:
                source = self._backup_config(site, candidate)
            fresh = not source
            if fresh:
                prefix = self._prefix(site)
                self.manager.wp(site, 'config', 'create', f'--dbname={site["db_name"]}',
                                f'--dbuser={site["db_user"]}', '--dbhost=localhost',
                                f'--dbprefix={prefix}', '--skip-salts', '--prompt=dbpass',
                                f'--config-file={candidate}',
                                input=credentials['database_password'] + '\n', timeout=45)
                # Local cryptographically random salts do not need a remote
                # salt API or a root-executed custom configuration.
                for key in ('AUTH_KEY', 'SECURE_AUTH_KEY', 'LOGGED_IN_KEY', 'NONCE_KEY',
                            'AUTH_SALT', 'SECURE_AUTH_SALT', 'LOGGED_IN_SALT', 'NONCE_SALT'):
                    self._config_set(site, candidate, key, secrets.token_hex(48), private=True)
                source = 'rebuilt_from_verified_database'
            self._overlay(site, candidate, credentials, fresh)
            os.replace(candidate, root / 'wp-config.php')
        return {'action': 'config_recovered', 'source': source,
                'preserved_config': str(preserved) if preserved else None,
                'session_reset': fresh,
                'custom_config_reset': fresh}

    def _normalize_memory(self, site):
        root = Path(site['root'])
        config = root / 'wp-config.php'
        keys = [key for key in ('WP_MEMORY_LIMIT', 'WP_MAX_MEMORY_LIMIT')
                if self._has(site, config, key)]
        if not keys:
            return None
        memory = self._memory()
        # A no-change candidate is discarded; never alter an otherwise
        # healthy config just to produce an action in a repair report.
        with tempfile.TemporaryDirectory(prefix='.wpi-repair-', dir=root.parent) as temp:
            self.runner(['chown', 'www-data:www-data', temp])
            candidate = Path(temp) / 'wp-config.php'
            candidate.write_bytes(config.read_bytes())
            self.runner(['chown', 'www-data:www-data', str(candidate)])
            os.chmod(candidate, 0o600)
            for key in keys:
                self._config_set(site, candidate, key, memory)
            if candidate.read_bytes() == config.read_bytes():
                return None
            if not self._lint(candidate):
                raise ValueError('Kandidat memory config gagal lint; config aktif tidak diubah.')
            preserved = self._preserve_config(site)
            os.chmod(candidate, 0o640)
            os.replace(candidate, config)
        return {'action': 'memory_constants_normalized', 'memory_limit': memory,
                'preserved_config': str(preserved)}

    def _permissions(self, site):
        root = Path(site['root'])
        changed = []
        for path in (root.parent, root):
            if not self._check(['runuser', '-u', 'www-data', '--', 'test', '-x', str(path)])[0]:
                os.chmod(path, 0o755)
                changed.append(str(path))
        for name in ('wp-config.php', 'index.php', 'wp-settings.php'):
            path = root / name
            if path.is_file() and not self._check(['runuser', '-u', 'www-data', '--',
                                                  'test', '-r', str(path)])[0]:
                self.runner(['chown', 'www-data:www-data', str(path)])
                os.chmod(path, 0o640 if name == 'wp-config.php' else 0o644)
                changed.append(name)
        return {'action': 'permissions_repaired', 'paths': changed} if changed else None

    def repair(self, identifier):
        site = self._site(identifier)
        before = self.diagnose(identifier)
        report = {'site_id': site['id'], 'primary': site['primary'], 'before': before['checks'],
                  'actions': [], 'errors': [], 'database_restored': False,
                  'content_restored': False, 'session_reset': False}
        if before['status'] == 'healthy':
            report.update({'status': 'healthy', 'checks': before['checks']})
            try:
                report['config_snapshot'] = str(self.remember_config(identifier))
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                report['errors'].append('config_snapshot_failed')
            return report
        checks = before['checks']
        try:
            preserved = self._preserve_config(site)
            report['preserved_config'] = str(preserved) if preserved else None
        except (ValueError, OSError):
            # An unreadable/oversized original cannot be backed up safely.
            # Leave the config and permissions unchanged in that situation.
            report.update({'status': 'unresolved', 'checks': checks})
            report['errors'].append('original_config_backup_failed')
            return report
        db = 'mariadb' if self.manager.config.get('database') == 'mariadb' else 'mysql'
        if not checks['database_service']:
            try:
                self.runner(['systemctl', 'start', db])
                report['actions'].append({'action': 'database_service_started'})
            except (RuntimeError, OSError, subprocess.SubprocessError):
                report['errors'].append('database_service_start_failed')
        try:
            action = self._permissions(site)
            if action:
                report['actions'].append(action)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            report['errors'].append('permissions_repair_failed')
        snapshot = self._latest_snapshot(site)
        changed = bool(snapshot and (Path(site['root']) / 'wp-config.php').is_file()
                       and core.Manager.file_hash(snapshot / 'wp-config.php') !=
                       core.Manager.file_hash(Path(site['root']) / 'wp-config.php'))
        config_ok = self._lint(Path(site['root']) / 'wp-config.php')
        installed = self._installed(site) if config_ok else False
        need_recovery = not config_ok or (changed and not installed)
        try:
            if need_recovery:
                action = self._recover_config(site)
                report['actions'].append(action)
                report['session_reset'] = action['session_reset']
            elif config_ok:
                action = self._normalize_memory(site)
                if action:
                    report['actions'].append(action)
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError, json.JSONDecodeError):
            report['errors'].append('config_recovery_failed')
        # Restart only after validating the installed FPM config; this clears
        # stale OPcache after a config was edited and then put back by hand.
        try:
            # Autotune runs outside the WPI operation lock. Serialize its
            # config writer/reloader with this validation/restart boundary.
            tuner = AutoTuner(self.php, self.runner, data_dir=self.manager.data)
            with tuner._lock(blocking=True) as acquired:
                if not acquired:
                    report['errors'].append('fpm_restart_lock_busy')
                elif self._check([f'/usr/sbin/php-fpm{self.php}', '-t'])[0]:
                    self.runner(['systemctl', 'restart', f'php{self.php}-fpm'])
                    report['actions'].append({'action': 'fpm_restarted_opcache_cleared'})
                else:
                    report['errors'].append('fpm_config_invalid')
        except (RuntimeError, OSError, subprocess.SubprocessError):
            report['errors'].append('fpm_restart_failed')
        web = self.manager.web
        command = ['nginx', '-t'] if self.manager.config['stack'] == 'nginx' else ['apache2ctl', 'configtest']
        try:
            # Existing external-invalid configuration is not rewritten. If
            # the service is stopped, validate before starting/reloading it.
            if not self._check(['systemctl', 'is-active', web.service])[0]:
                if not self._check(command)[0]:
                    raise ValueError('Konfigurasi web server belum valid.')
                self.runner(['systemctl', 'start', web.service])
            web.write_site(site)
            report['actions'].append({'action': 'managed_vhost_regenerated'})
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            report['errors'].append('web_config_repair_failed')
        after = self._settled_diagnosis(identifier, site)
        report['checks'] = after['checks']
        report['status'] = 'resolved' if after['status'] == 'healthy' else 'unresolved'
        if after['status'] == 'healthy':
            try:
                report['config_snapshot'] = str(self.remember_config(identifier))
            except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
                report['errors'].append('config_snapshot_failed')
        else:
            report['next_step'] = ('Periksa log PHP/web server secara lokal untuk kegagalan plugin, tema, '
                                   'kode custom, koneksi database atau sertifikat; repair tidak menonaktifkan '
                                   'plugin/tema atau memulihkan database/media.')
        return report
