"""Supported Redis Object Cache integration with one private instance per site.

Redis data is disposable; WordPress SQL and uploads are never copied or removed
by these operations. Separate Unix sockets make FLUSHDB site-scoped without the
upstream plugin's unsupported selective-flush or graceful-failure modes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import tempfile
import time

from . import core
from .autotune import MIB, _atomic_write, detect_resources

PLUGIN_VERSION = '3.0.0'
# Official rhubarbgroup/redis-cache tag 3.0.0, includes/object-cache.php.
DROPIN_SHA256 = '2e1758eb3049e0dd0d4e310cef4cfe2dd4359cf1c4c8bef47b65a80cacbed59d'
HEADER = '# Managed by WPI Redis Object Cache.\n'
ACL_HEADER = 'user wpi_managed_marker off\n'
SERVICE = 'wpi-redis@.service'
_ID = re.compile(r'[a-f0-9]{12}\Z')
_SECRET = re.compile(r'[a-f0-9]{64}\Z')


def redis_resource_profile(memory_total_bytes, site_count=1):
    """Reserve cache capacity plus allocator/native overhead against real RAM."""
    if isinstance(memory_total_bytes, bool) or not isinstance(memory_total_bytes, int) \
            or memory_total_bytes < 256 * MIB:
        raise ValueError('RAM tidak cukup/valid untuk Redis otomatis.')
    if isinstance(site_count, bool) or not isinstance(site_count, int) or site_count < 0:
        raise ValueError('Jumlah instance Redis tidak valid.')
    count = max(1, site_count)
    total_mib = memory_total_bytes // MIB
    budget = max(16, total_mib // 20, 4 * count)
    reserve = 2 * budget + 10 * count
    if reserve > total_mib // 5:
        raise ValueError('Jumlah situs Redis melebihi anggaran RAM yang aman.')
    return {'enabled': True, 'maxmemory_mib': budget,
            'per_site_maxmemory_mib': max(4, budget // count),
            'reserve_mib': reserve, 'memory_reserve_bytes': reserve * MIB,
            'site_count': site_count}


def _no_links(path):
    path = Path(path)
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError('Menolak symlink pada path Redis WPI.')
    return path


def _directory(path, mode=0o700):
    path = _no_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    if not path.is_dir():
        raise ValueError('Direktori Redis WPI tidak valid.')
    os.chmod(path, mode)
    return path


def _bytes(path):
    path = _no_links(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 2 * MIB:
        raise ValueError('Berkas Redis WPI tidak valid.')
    return path.read_bytes()


class RedisProtocolError(RuntimeError):
    pass


class RedisConnection:
    """Small bounded RESP2 Unix client; secrets never enter process arguments."""
    def __init__(self, path, username, password, timeout=2):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(str(path))
            self.reader = self.socket.makefile('rb')
            if self.command('AUTH', username, password) != 'OK':
                raise RedisProtocolError('Autentikasi Redis gagal.')
        except BaseException:
            self.socket.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.reader.close()
        self.socket.close()

    def _reply(self, depth=0):
        if depth > 8:
            raise RedisProtocolError('Respons Redis terlalu kompleks.')
        line = self.reader.readline(65538)
        if not line.endswith(b'\r\n'):
            raise RedisProtocolError('Respons Redis tidak valid.')
        kind, value = line[:1], line[1:-2]
        if kind == b'+':
            return value.decode('utf-8', errors='replace')
        if kind == b'-':
            # Do not expose raw server errors, potentially containing a key or secret.
            error = RedisProtocolError('Perintah Redis ditolak atau gagal.')
            code = value.split(b' ', 1)[0].decode('ascii', errors='ignore')
            error.category = code if code in ('WRONGPASS', 'NOAUTH', 'NOPERM', 'ERR') else 'unknown'
            raise error
        if kind == b':':
            return int(value)
        if kind == b'$':
            length = int(value)
            if length == -1:
                return None
            if not 0 <= length <= 2 * MIB:
                raise RedisProtocolError('Respons Redis terlalu besar.')
            result = self.reader.read(length + 2)
            if len(result) != length + 2 or not result.endswith(b'\r\n'):
                raise RedisProtocolError('Respons Redis terpotong.')
            return result[:-2].decode('utf-8', errors='replace')
        if kind == b'*':
            length = int(value)
            if length == -1:
                return None
            if not 0 <= length <= 1024:
                raise RedisProtocolError('Respons Redis terlalu besar.')
            return [self._reply(depth + 1) for _ in range(length)]
        raise RedisProtocolError('Format respons Redis tidak didukung.')

    def command(self, *args):
        values = [str(value).encode('utf-8') for value in args]
        payload = b'*' + str(len(values)).encode() + b'\r\n'
        for value in values:
            payload += b'$' + str(len(value)).encode() + b'\r\n' + value + b'\r\n'
        self.socket.sendall(payload)
        return self._reply()


class RedisCache:
    def __init__(self, manager, etc_root='/etc', run_root='/run',
                 connection_factory=RedisConnection, resources=detect_resources):
        self.manager, self.runner = manager, manager.runner
        self.etc, self.run = Path(etc_root), Path(run_root)
        self.connect, self.resources = connection_factory, resources
        self.data = manager.data / 'redis'

    @property
    def template(self):
        return self.etc / 'systemd/system' / SERVICE

    def _site(self, identifier):
        site = self.manager.site(identifier)
        if not _ID.fullmatch(str(site.get('id', ''))):
            raise ValueError('ID situs Redis tidak valid.')
        root = _no_links(site['root'])
        if root != core.WWW / site['id'] / 'public' or not root.is_dir():
            raise ValueError('Redis hanya dapat mengubah document root yang dikelola WPI.')
        _no_links(self.manager.data)
        _bytes(root / 'wp-config.php')
        return site

    def _paths(self, identifier):
        if not _ID.fullmatch(str(identifier)):
            raise ValueError('ID instance Redis tidak valid.')
        return {'config': self.etc / 'wpi/redis' / f'{identifier}.conf',
                'acl': self.etc / 'wpi/redis' / f'{identifier}.acl',
                'credentials': self.data / 'credentials' / f'{identifier}.json',
                'socket': self.run / f'wpi-redis-{identifier}/redis.sock'}

    @staticmethod
    def service(identifier):
        if not _ID.fullmatch(str(identifier)):
            raise ValueError('ID layanan Redis tidak valid.')
        return f'wpi-redis@{identifier}.service'

    def _check(self, argv, **kwargs):
        try:
            return self.runner(argv, check=False, timeout=30, **kwargs).returncode == 0
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return False

    def _package_installed(self, name):
        result = self.runner(['dpkg-query', '-W', '-f=${Status}', name],
                             check=False, timeout=30)
        return result.returncode == 0 and str(result.stdout or '').strip() == 'install ok installed'

    def _managed_write(self, path, content, mode=0o640):
        old = _bytes(path)
        expected = ACL_HEADER if path.suffix == '.acl' else HEADER
        if old is not None and not old.startswith(expected.encode()):
            raise ValueError('Konfigurasi Redis/layanan lain sudah ada; tidak ditimpa.')
        if old == content.encode():
            return False
        if path.parent != self.template.parent:
            # Recursive mkdir under a restrictive root umask would otherwise
            # leave /etc/wpi at 0700, preventing redis from reading its config.
            _directory(self.etc / 'wpi', 0o755)
        _directory(path.parent, 0o755 if path.parent == self.template.parent else 0o750)
        if path.parent != self.template.parent:
            self.runner(['chown', 'root:redis', str(path.parent)])
        _atomic_write(path, content, mode=mode)
        self.runner(['chown', 'root:redis' if mode == 0o640 else 'root:root', str(path)])
        return True

    def _credential(self, path, username, create=False):
        value = _bytes(path)
        if value is None:
            if not create:
                raise ValueError('Kredensial Redis WPI belum tersedia.')
            _directory(path.parent)
            data = {'username': username, 'password': secrets.token_hex(32)}
            _atomic_write(path, json.dumps(data) + '\n', mode=0o600)
            return data
        data = json.loads(value)
        if set(data) != {'username', 'password'} or data['username'] != username \
                or not _SECRET.fullmatch(str(data['password'])):
            raise ValueError('Kredensial Redis WPI tidak valid.')
        if os.name != 'nt' and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ValueError('Kredensial Redis WPI harus privat (0600).')
        return data

    def _admin(self, create=False):
        return self._credential(self.data / 'admin.json', 'wpi_admin', create)

    def _client(self, identifier, admin=True):
        paths = self._paths(identifier)
        credentials = self._admin() if admin else self._credential(
            paths['credentials'], f'wpi_{identifier}')
        _no_links(paths['socket'])
        return self.connect(paths['socket'], credentials['username'], credentials['password'])

    def _profile(self, count):
        return redis_resource_profile(int(self.resources()['memory_total']), count)

    def _save_policy(self, profile):
        cfg = self.manager.config
        cfg['redis_cache'] = {key: profile[key] for key in
                             ('enabled', 'maxmemory_mib', 'reserve_mib', 'site_count')}
        cfg['redis_cache']['plugin_version'] = PLUGIN_VERSION
        if cfg != self.manager.config:
            core.atomic_json(_no_links(self.manager.data / 'config.json'), cfg)

    def _ids(self):
        return [site['id'] for site in self.manager.sites()
                if site.get('redis_cache', {}).get('enabled')]

    def install(self):
        """Install binaries/template only; do not modify WordPress or other Redis."""
        php = self.manager.config.get('php_version')
        if php not in ('8.1', '8.3'):
            raise ValueError('Jalankan setup PHP WPI sebelum memasang Redis.')
        profile = self._profile(len(self._ids()))
        # Only stop the stock daemon when WPI can prove it did not exist before apt.
        binary_existed = Path('/usr/bin/redis-server').exists()
        package_existed = self._package_installed('redis-server')
        result = self.runner(['systemctl', 'show', 'redis-server.service',
                              '--property=LoadState', '--value'], check=False, timeout=30)
        service_existed = str(result.stdout or '').strip() not in ('', 'not-found')
        packages = ['redis-server', 'redis-tools', f'php{php}-redis']
        missing = [package for package in packages if not self._package_installed(package)]
        if missing:
            self.runner(['apt-get', 'update'])
            self.runner(['apt-get', 'install', '-y', *missing])
            if not (binary_existed or package_existed or service_existed):
                self.runner(['systemctl', 'disable', '--now', 'redis-server.service'])
            self.runner(['systemctl', 'reload', f'php{php}-fpm'])
        self.runner([f'/usr/bin/php{php}', '-r',
                     "exit(extension_loaded('redis') && version_compare(phpversion('redis'), '5.3.0', '>=') ? 0 : 1);"])
        _directory(self.data)
        self._admin(create=True)
        template = (HEADER + '[Unit]\nDescription=WPI Redis object cache for %i\n'
                    'After=network.target\n\n[Service]\nType=simple\nUser=redis\n'
                    'Group=www-data\nSupplementaryGroups=redis\nUMask=0077\n'
                    'RuntimeDirectory=wpi-redis-%i\nRuntimeDirectoryMode=0750\n'
                    f'ExecStart=/usr/bin/redis-server {self.etc}/wpi/redis/%i.conf\n'
                    'Restart=on-failure\nRestartSec=2\nTimeoutStopSec=15\n'
                    'NoNewPrivileges=true\nPrivateTmp=true\nProtectHome=true\n'
                    'ProtectSystem=strict\nRestrictAddressFamilies=AF_UNIX\n'
                    'RestrictSUIDSGID=true\n\n[Install]\nWantedBy=multi-user.target\n')
        if self._managed_write(self.template, template, 0o644):
            self.runner(['systemctl', 'daemon-reload'])
        self._save_policy(profile)
        self.optimize()
        return self.status()

    def _render(self, identifier, maxmemory_mib, create=False):
        paths = self._paths(identifier)
        credentials = self._credential(paths['credentials'], f'wpi_{identifier}', create)
        admin = self._admin()
        digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
        # Explicit commands work on Redis 6 and 7, avoiding broad/new permissions.
        commands = ('+get +set +setex +psetex +mget +mset +del +unlink +exists '
                    '+incrby +decrby +expire +pexpire +ttl +pttl +type +scan '
                    '+ping +info +dbsize +flushdb +multi +exec +discard +watch '
                    '+unwatch +auth +select +echo')
        # Redis 6.0 handles QUIT outside its command table; granting +quit
        # makes ACL loading fail even though closing a connection works.
        # Redis ACL files accept user declarations only, including no comments.
        acl = (ACL_HEADER + 'user default off\n'
               f'user wpi_admin on #{digest(admin["password"])} ~* +@all\n'
               f'user {credentials["username"]} on #{digest(credentials["password"])} '
               f'~wpi:{identifier}:* -@all {commands}\n')
        config = (HEADER + 'bind 127.0.0.1\nport 0\nprotected-mode yes\n'
                  f'unixsocket {paths["socket"]}\nunixsocketperm 660\n'
                  f'aclfile {paths["acl"]}\ndatabases 1\n'
                  f'maxmemory {maxmemory_mib * MIB}\nmaxmemory-policy allkeys-lfu\n'
                  'save ""\nappendonly no\ndaemonize no\nsupervised no\n'
                  f'dir {paths["socket"].parent}\nlogfile ""\n')
        return config, acl

    def _instance(self, identifier, maxmemory_mib, start=False, create=False):
        paths = self._paths(identifier)
        config, acl = self._render(identifier, maxmemory_mib, create=create)
        old_acl = _bytes(paths['acl'])
        self._managed_write(paths['acl'], acl)
        changed = self._managed_write(paths['config'], config)
        active = self._check(['systemctl', 'is-active', self.service(identifier)])
        if active:
            with self._client(identifier) as client:
                if old_acl != acl.encode():
                    client.command('ACL', 'LOAD')
                if changed:
                    client.command('CONFIG', 'SET', 'maxmemory', maxmemory_mib * MIB)
                    client.command('CONFIG', 'SET', 'maxmemory-policy', 'allkeys-lfu')
        elif start:
            self.runner(['systemctl', 'enable', '--now', self.service(identifier)])
        if start:
            last_error = None
            for attempt in range(15):
                try:
                    with self._client(identifier) as client:
                        if client.command('PING') == 'PONG':
                            return
                except (OSError, RedisProtocolError) as error:
                    last_error = error
                    if attempt < 14:
                        time.sleep(0.2)
            diagnostic = self._readiness_diagnostic(identifier, last_error)
            raise RuntimeError('Instance Redis WPI belum siap; konfigurasi dipulihkan. '
                               'Diagnostik aman: ' + json.dumps(diagnostic, sort_keys=True))

    def _readiness_diagnostic(self, identifier, error):
        """Capture structural failure before rollback, never raw journal or errors."""
        types = ('ConnectionRefusedError', 'PermissionError', 'FileNotFoundError',
                 'TimeoutError', 'RedisProtocolError', 'OSError')
        diagnostic = {'error_type': type(error).__name__ if type(error).__name__ in types else 'unknown'}
        if isinstance(error, OSError):
            diagnostic['errno'] = error.errno
        if isinstance(error, RedisProtocolError):
            diagnostic['protocol_category'] = getattr(error, 'category', 'unknown')
        fields = ('ActiveState', 'SubState', 'Result', 'ExecMainStatus')
        try:
            result = self.runner(['systemctl', 'show', self.service(identifier),
                                  *['--property=' + key for key in fields]], check=False, timeout=10)
            values = dict(line.split('=', 1) for line in str(result.stdout or '').splitlines() if '=' in line)
            diagnostic['service'] = {key: values[key] for key in fields if key in values and
                                     re.fullmatch(r'[A-Za-z0-9_-]{1,64}', values[key])}
            journal = self.runner(['journalctl', '--unit', self.service(identifier), '--no-pager',
                                   '--output=cat', '--lines=40'], check=False, timeout=10)
            content = str(journal.stdout or '').lower()
            categories = {'acl_user_keyword': 'should start with user keyword',
                          'acl_load_error': 'error loading acl', 'config_error': 'fatal config file error',
                          'permission_denied': 'permission denied', 'missing_file': 'no such file',
                          'address_family': 'address family not supported', 'ready': 'ready to accept'}
            diagnostic['journal_categories'] = {key: content.count(value) for key, value in categories.items()}
        except (OSError, RuntimeError, subprocess.SubprocessError):
            diagnostic['service'] = 'unavailable'
        return diagnostic

    def optimize(self):
        """Rebudget managed instances; never start a deliberately stopped service."""
        if not self.manager.config.get('redis_cache', {}).get('enabled'):
            return {'enabled': False, 'changed': False}
        identifiers = self._ids()
        profile = self._profile(len(identifiers))
        snapshots = {path: self._saved(path) for identifier in identifiers
                     for key, path in self._paths(identifier).items() if key in ('config', 'acl')}
        snapshots[self.manager.data / 'config.json'] = self._saved(self.manager.data / 'config.json')
        live = {}
        aggregate_rss, complete_rss = 0, True
        try:
            for identifier in identifiers:
                if self._check(['systemctl', 'is-active', self.service(identifier)]):
                    with self._client(identifier) as client:
                        pair = client.command('CONFIG', 'GET', 'maxmemory')
                        live[identifier] = int(pair[1])
                        info = str(client.command('INFO', 'memory'))
                        match = re.search(r'^used_memory_rss:(\d+)\r?$', info, re.M)
                        if match:
                            aggregate_rss += int(match[1])
                        else:
                            complete_rss = False
                self._instance(identifier, profile['per_site_maxmemory_mib'])
            # Shrinking maxmemory does not immediately return allocator pages
            # to the OS. Keep FPM out of those retained pages until they shrink.
            observed_reserve = (aggregate_rss + MIB - 1) // MIB
            if not complete_rss:
                observed_reserve = max(observed_reserve, int(self.manager.config.get(
                    'redis_cache', {}).get('reserve_mib', 0)))
            profile['reserve_mib'] = max(profile['reserve_mib'], observed_reserve)
            profile['memory_reserve_bytes'] = profile['reserve_mib'] * MIB
            self._save_policy(profile)
        except BaseException:
            self._restore(snapshots)
            for identifier, value in live.items():
                try:
                    with self._client(identifier) as client:
                        client.command('CONFIG', 'SET', 'maxmemory', value)
                        client.command('ACL', 'LOAD')
                except (OSError, RedisProtocolError):
                    pass
            raise
        return {'enabled': True, 'changed': any(_bytes(path) != value[0]
                for path, value in snapshots.items()), **profile}

    def _constants(self, site):
        credentials = self._credential(self._paths(site['id'])['credentials'], f'wpi_{site["id"]}')
        strings = {'WP_REDIS_CLIENT': 'phpredis', 'WP_REDIS_SCHEME': 'unix',
                   'WP_REDIS_PATH': str(self._paths(site['id'])['socket']),
                   'WP_REDIS_USERNAME': credentials['username'],
                   'WP_REDIS_PREFIX': f'wpi:{site["id"]}:',
                   'WP_REDIS_PASSWORD': credentials['password']}
        raw = {'WP_REDIS_DATABASE': '0', 'WP_REDIS_TIMEOUT': '0.5',
               'WP_REDIS_READ_TIMEOUT': '0.5', 'WP_REDIS_RETRY_INTERVAL': '0',
               'WP_REDIS_PERSISTENT': 'false', 'WP_REDIS_DISABLED': 'false',
               'WP_REDIS_GRACEFUL': 'false', 'WP_REDIS_SELECTIVE_FLUSH': 'false',
               'WP_REDIS_DISABLE_GROUP_FLUSH': 'true',
               'WP_REDIS_DISABLE_DROPIN_AUTOUPDATE': 'true'}
        return strings, raw

    def _config_current(self, site):
        try:
            if not site.get('redis_cache', {}).get('enabled'):
                return False
            strings, raw = self._constants(site)
            result = self.manager.wp(site, 'config', 'list', 'WP_REDIS_', '--format=json',
                                     check=False)
            if result.returncode:
                return False
            rows = json.loads(result.stdout)
            values = {row['key']: row['value'] for row in rows if row.get('type') == 'constant'}
            normalize = lambda value: str(value).lower() if isinstance(value, bool) else str(value)
            forbidden = ('WP_REDIS_SERVERS', 'WP_REDIS_SHARDS', 'WP_REDIS_CLUSTER',
                         'WP_REDIS_SENTINEL', 'WP_REDIS_MAXTTL', 'WP_REDIS_UNFLUSHABLE_GROUPS')
            return all(normalize(values.get(key)) == value for key, value in {**strings, **raw}.items()) \
                and not any(key in values for key in forbidden)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError):
            return False

    def _config_set(self, site, candidate, name, value, raw=False, private=False):
        args = ['config', 'set', name]
        kwargs = {}
        if private:
            args.append('--prompt')
            kwargs['input'] = str(value) + '\n'
        else:
            args.append(str(value))
        args.extend(['--type=constant', f'--config-file={candidate}'])
        if raw:
            args.append('--raw')
        self.manager.wp(site, *args, **kwargs)

    def overlay_config(self, site, candidate=None):
        """Overlay local credentials on a repair/import candidate without WP boot."""
        checked = self._site(site['id'])
        if not checked.get('redis_cache', {}).get('enabled'):
            return False
        path = _no_links(candidate or (Path(checked['root']) / 'wp-config.php'))
        if not path.is_file() or path.stat().st_size > MIB:
            raise ValueError('Kandidat wp-config Redis tidak valid.')
        parent = Path(checked['root']).parent
        if path != Path(checked['root']) / 'wp-config.php' and parent not in path.parents:
            raise ValueError('Kandidat Redis berada di luar direktori situs WPI.')
        credentials = self._credential(self._paths(checked['id'])['credentials'],
                                       f'wpi_{checked["id"]}')
        # Remove source-server routing and forced TTL before local Unix settings.
        for name in ('WP_REDIS_SERVERS', 'WP_REDIS_SHARDS', 'WP_REDIS_CLUSTER',
                     'WP_REDIS_SENTINEL', 'WP_REDIS_MAXTTL', 'WP_REDIS_UNFLUSHABLE_GROUPS'):
            self.manager.wp(checked, 'config', 'delete', name, '--type=constant',
                            f'--config-file={path}', check=False)
        values, raw = self._constants(checked)
        for name, value in values.items():
            self._config_set(checked, path, name, value, private=(name == 'WP_REDIS_PASSWORD'))
        for name, value in raw.items():
            self._config_set(checked, path, name, value, raw=True)
        return True

    def _stage_config(self, site, disabled=False):
        root = Path(site['root'])
        original = _bytes(root / 'wp-config.php')
        if original is None:
            raise ValueError('wp-config.php belum tersedia untuk Redis.')
        self.runner([f'/usr/bin/php{self.manager.config["php_version"]}', '-n', '-l',
                     str(root / 'wp-config.php')])
        with tempfile.TemporaryDirectory(prefix='.wpi-redis-', dir=root.parent) as folder:
            candidate = Path(folder) / 'wp-config.php'
            candidate.write_bytes(original)
            os.chmod(candidate, 0o640)
            os.chmod(folder, 0o750)
            self.runner(['chown', 'root:www-data', folder])
            self.runner(['chown', 'www-data:www-data', str(candidate)])
            if disabled:
                self._config_set(site, candidate, 'WP_REDIS_DISABLED', 'true', raw=True)
            else:
                self.overlay_config(site, candidate)
            self.runner([f'/usr/bin/php{self.manager.config["php_version"]}', '-n', '-l', str(candidate)])
            _atomic_write(root / 'wp-config.php', candidate.read_bytes(), mode=0o640)
            self.runner(['chown', 'www-data:www-data', str(root / 'wp-config.php')])

    def prepare_site_config(self, identifier):
        """Prepare an imported config before core-is-installed/SQL operations."""
        if not self.manager.config.get('redis_cache', {}).get('enabled'):
            self.install()
        site = self._site(identifier)
        if self._config_current(site):
            self._instance(site['id'], self._profile(max(1, len(self._ids())))[
                'per_site_maxmemory_mib'], start=True)
            self.optimize()
            return site['redis_cache'].copy()
        snapshots = self._capture(site)
        self._backup(site, snapshots)
        was_active = self._check(['systemctl', 'is-active', self.service(site['id'])])
        try:
            return self._prepare(site)
        except BaseException:
            self._restore(snapshots)
            if not was_active:
                self._check(['systemctl', 'disable', '--now', self.service(site['id'])])
            raise

    def prepare_disabled_config(self, identifier):
        """Keep a local opt-out when a restored archive contains an old drop-in."""
        site = self._site(identifier)
        if site.get('redis_cache', {}).get('enabled') is not False:
            return {'site_id': site['id'], 'changed': False}
        target, current = self._dropin(site)
        result = self.manager.wp(site, 'config', 'list', 'WP_REDIS_DISABLED', '--strict',
                                 '--format=json', check=False)
        disabled = False
        if result.returncode == 0:
            try:
                rows = json.loads(result.stdout)
                disabled = any(row.get('key') == 'WP_REDIS_DISABLED' and
                               str(row.get('value')).lower() == 'true' for row in rows)
            except (ValueError, TypeError):
                pass
        if current is None and disabled:
            return {'site_id': site['id'], 'changed': False}
        snapshots = self._capture(site)
        self._backup(site, snapshots)
        try:
            if not disabled:
                self._stage_config(site, disabled=True)
            if current is not None:
                target.unlink()
        except BaseException:
            self._restore(snapshots)
            raise
        return {'site_id': site['id'], 'changed': True, 'enabled': False}

    def _prepare(self, site):
        count = len(set(self._ids()) | {site['id']})
        profile = self._profile(count)
        self._instance(site['id'], profile['per_site_maxmemory_mib'], start=True, create=True)
        site['redis_cache'] = {'enabled': True, 'prefix': f'wpi:{site["id"]}:',
                               'username': f'wpi_{site["id"]}', 'plugin_version': PLUGIN_VERSION,
                               'socket': str(self._paths(site['id'])['socket'])}
        self.manager.save_site(site)
        self._stage_config(site)
        self.optimize()
        return site['redis_cache'].copy()

    def _capture(self, site):
        paths = [Path(site['root']) / 'wp-config.php', Path(site['root']) / 'wp-content/object-cache.php',
                 self.manager.data / 'sites' / f'{site["id"]}.json', self.manager.data / 'config.json']
        for identifier in set(self._ids()) | {site['id']}:
            paths.extend(path for key, path in self._paths(identifier).items()
                         if key in ('config', 'acl', 'credentials'))
        return {path: self._saved(path) for path in paths}

    @staticmethod
    def _saved(path):
        value = _bytes(path)
        return ((value, stat.S_IMODE(path.stat().st_mode), path.stat().st_uid, path.stat().st_gid)
                if value is not None else (None, 0o600, None, None))

    @staticmethod
    def _restore(snapshots):
        for path, saved in snapshots.items():
            if isinstance(saved, tuple):
                value, mode, uid, gid = saved
            else:
                value, mode, uid, gid = saved, 0o640, None, None
            _no_links(path)
            if value is None:
                if path.exists():
                    path.unlink()
            else:
                _atomic_write(path, value, mode=mode)
                if os.name != 'nt' and uid is not None:
                    os.chown(path, uid, gid)

    def _backup(self, site, snapshots):
        folder = _directory(self.data / 'backups' / (dt.datetime.now(dt.timezone.utc).strftime(
            '%Y%m%dT%H%M%S') + '-' + secrets.token_hex(4)))
        for name in ('wp-config.php', 'object-cache.php'):
            source = Path(site['root']) / ('wp-content' if name == 'object-cache.php' else '') / name
            value = snapshots.get(source, (None,))[0]
            if value is not None:
                _atomic_write(folder / name, value, mode=0o600)
        return folder

    def _dropin(self, site):
        target = Path(site['root']) / 'wp-content/object-cache.php'
        current = _bytes(target)
        if current is not None and hashlib.sha256(current).hexdigest() != DROPIN_SHA256:
            raise ValueError('object-cache.php milik plugin lain/versi lain; tidak ditimpa.')
        return target, current

    def _plugin(self, site):
        directory = Path(site['root']) / 'wp-content/plugins/redis-cache'
        _no_links(directory)
        if not directory.exists():
            self.manager.wp(site, 'plugin', 'install', 'redis-cache', f'--version={PLUGIN_VERSION}')
        self.manager.wp(site, 'plugin', 'verify-checksums', 'redis-cache', '--strict')
        source = _bytes(directory / 'includes/object-cache.php')
        if source is None or hashlib.sha256(source).hexdigest() != DROPIN_SHA256:
            raise ValueError('Drop-in Redis tidak cocok dengan rilis resmi 3.0.0.')
        return source

    def _health(self, site):
        key, value = 'wpi-probe-' + secrets.token_hex(8), secrets.token_hex(16)
        self.manager.wp(site, 'cache', 'set', key, value, 'wpi-health', '30')
        result = self.manager.wp(site, 'cache', 'get', key, 'wpi-health')
        self.manager.wp(site, 'cache', 'delete', key, 'wpi-health')
        if str(result.stdout or '').strip() != value:
            raise RuntimeError('Uji cache Redis lintas proses WordPress gagal.')

    def enable_site(self, identifier):
        site = self._site(identifier)
        target, old_dropin = self._dropin(site)
        if not self.manager.config.get('redis_cache', {}).get('enabled'):
            self.install()
        if old_dropin is not None and self._config_current(site):
            self._instance(site['id'], self._profile(max(1, len(self._ids())))[
                'per_site_maxmemory_mib'], start=True)
            self._plugin(site)
            self._health(site)
            if not self.manager.wp(site, 'plugin', 'is-active', 'redis-cache', check=False).returncode == 0:
                self.manager.wp(site, 'plugin', 'activate', 'redis-cache')
            return {'site_id': site['id'], 'enabled': True, 'plugin_version': PLUGIN_VERSION,
                    'persistent_cache_verified': True, 'changed': False}
        snapshots = self._capture(site)
        self._backup(site, snapshots)
        was_active = self._check(['systemctl', 'is-active', self.service(site['id'])])
        plugin_active = None
        try:
            # Config commands do not boot WordPress, so imported stale auth is fixed first.
            self._prepare(site)
            site = self._site(site['id'])
            source = self._plugin(site)
            _directory(target.parent, 0o755)
            _atomic_write(target, source, mode=0o644)
            self.runner(['chown', 'www-data:www-data', str(target)])
            self._health(site)
            plugin_active = self.manager.wp(site, 'plugin', 'is-active', 'redis-cache',
                                            check=False).returncode == 0
            if not plugin_active:
                self.manager.wp(site, 'plugin', 'activate', 'redis-cache')
            try:
                self.manager.remember_config(site['id'])
            except (ValueError, RuntimeError):
                pass
        except BaseException:
            if plugin_active is False:
                try:
                    self.manager.wp(site, 'plugin', 'deactivate', 'redis-cache', check=False)
                except (OSError, RuntimeError):
                    pass
            self._restore(snapshots)
            for identifier in set(self._ids()) | {site['id']}:
                try:
                    if self._check(['systemctl', 'is-active', self.service(identifier)]):
                        old = _bytes(self._paths(identifier)['config'])
                        if old is not None:
                            match = re.search(rb'^maxmemory (\d+)$', old, re.M)
                            with self._client(identifier) as client:
                                if match:
                                    client.command('CONFIG', 'SET', 'maxmemory', int(match[1]))
                                client.command('ACL', 'LOAD')
                except (OSError, ValueError, RedisProtocolError):
                    pass
            if not was_active:
                self._check(['systemctl', 'disable', '--now', self.service(site['id'])])
            raise
        return {'site_id': site['id'], 'enabled': True, 'plugin_version': PLUGIN_VERSION,
                'persistent_cache_verified': True}

    def disable_site(self, identifier):
        site = self._site(identifier)
        target, current = self._dropin(site)
        if not site.get('redis_cache', {}).get('enabled'):
            return {'site_id': site['id'], 'enabled': False}
        snapshots = self._capture(site)
        self._backup(site, snapshots)
        was_active = self._check(['systemctl', 'is-active', self.service(site['id'])])
        was_enabled = self._check(['systemctl', 'is-enabled', self.service(site['id'])])
        try:
            if current is not None:
                target.unlink()
            site['redis_cache']['enabled'] = False
            self.manager.save_site(site)
            self.runner(['systemctl', 'disable', '--now', self.service(site['id'])])
            self.optimize()
        except BaseException:
            self._restore(snapshots)
            if was_enabled:
                self._check(['systemctl', 'enable', self.service(site['id'])])
            if was_active:
                self._check(['systemctl', 'start', self.service(site['id'])])
            raise
        return {'site_id': site['id'], 'enabled': False}

    def flush_site(self, identifier):
        site = self._site(identifier)
        if not site.get('redis_cache', {}).get('enabled'):
            raise ValueError('Redis belum aktif untuk situs ini.')
        with self._client(site['id'], admin=False) as client:
            # This site's instance has exactly one database; no other site is flushed.
            if client.command('FLUSHDB', 'ASYNC') != 'OK':
                raise RuntimeError('Pembersihan cache situs gagal.')
        return {'site_id': site['id'], 'flushed': True}

    def start_site(self, identifier):
        """Explicit repair hook; optimization/status never calls this."""
        site = self._site(identifier)
        if not site.get('redis_cache', {}).get('enabled'):
            return False
        self._instance(site['id'], self._profile(max(1, len(self._ids())))[
            'per_site_maxmemory_mib'], start=True)
        return True

    def status(self):
        cfg = self.manager.config.get('redis_cache', {})
        report = {'enabled': bool(cfg.get('enabled')), 'plugin_version': PLUGIN_VERSION,
                  'maxmemory_mib': cfg.get('maxmemory_mib', 0),
                  'reserve_mib': cfg.get('reserve_mib', 0), 'sites': []}
        fields = ('used_memory', 'maxmemory', 'keyspace_hits', 'keyspace_misses',
                  'evicted_keys', 'connected_clients', 'used_memory_rss')
        for record in self.manager.sites():
            identifier = record['id']
            item = {'site_id': identifier, 'primary': record['primary'],
                    'enabled': bool(record.get('redis_cache', {}).get('enabled')),
                    'service_active': False, 'connected': False}
            try:
                self._site(identifier)
                item['service_active'] = self._check(['systemctl', 'is-active', self.service(identifier)])
                if item['enabled'] and item['service_active']:
                    with self._client(identifier) as client:
                        item['connected'] = client.command('PING') == 'PONG'
                        info = str(client.command('INFO'))
                        for name in fields:
                            match = re.search(r'^' + name + r':(\d+)\r?$', info, re.M)
                            if match:
                                item[name] = int(match[1])
            except (OSError, ValueError, RedisProtocolError):
                item['error'] = 'Redis/configuration unavailable'
            report['sites'].append(item)
        report['used_memory_bytes'] = sum(site.get('used_memory', 0) for site in report['sites'])
        report['used_memory_rss_bytes'] = sum(site.get('used_memory_rss', 0) for site in report['sites'])
        report['sampled_at'] = int(time.time())
        return report
