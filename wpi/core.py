"""Provisioning and recoverable WordPress operations (Python standard library)."""
from __future__ import annotations

import copy
import datetime as dt
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import urllib.request

from .web import WebStack
from .autotune import AutoTuner

DATA = Path('/var/lib/wpi')
BACKUPS = Path('/var/backups/wpi')
WWW = Path('/var/www/wpi')
WP = '/usr/local/lib/wpi-tools/wp-cli.phar'


def domain(value):
    value = value.strip().lower().rstrip('.')
    if len(value) > 253 or any(c in value for c in '/:@*\\'):
        raise ValueError('Masukkan hostname, bukan URL/IP/path/wildcard.')
    try:
        value = value.encode('idna').decode('ascii')
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError('Domain tidak boleh berupa alamat IP.')
    labels = value.split('.')
    if len(value) > 253:
        raise ValueError('Hostname terlalu panjang setelah konversi IDNA.')
    if len(labels) < 2 or not re.fullmatch(r'[a-z][a-z0-9-]*', labels[-1]):
        raise ValueError('Gunakan domain publik, contoh example.com.')
    if not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', s) for s in labels):
        raise ValueError('Hostname tidak valid.')
    return value


def email_address(value):
    if not re.fullmatch(r'[A-Za-z0-9][^\s@]*@[^\s@]+\.[^\s@]+', value) or len(value) > 254:
        raise ValueError('Alamat email tidak valid.')
    return value


def site_hosts(site):
    """All managed hostnames; legacy secondary entries remain redirects."""
    return list(dict.fromkeys([site['primary'], *site.get('aliases', []),
                               *site.get('secondary', [])]))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        os.chmod(tmp, 0o600)
        json.dump(value, handle, indent=2)
        handle.write('\n')
    os.replace(tmp, path)


def run(argv, **kwargs):
    kwargs.setdefault('text', True)
    kwargs.setdefault('capture_output', True)
    kwargs.setdefault('check', True)
    # Never log argv, SQL, or stdout: they can contain credentials/customer data.
    env = os.environ.copy()
    env.update({'DEBIAN_FRONTEND': 'noninteractive', 'LC_ALL': 'C.UTF-8'})
    env.update(kwargs.pop('env', {}))
    try:
        return subprocess.run(argv, env=env, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f'Perintah {Path(argv[0]).name} gagal (exit {exc.returncode}). '
                           'Periksa status layanan; detail sensitif tidak ditampilkan.') from None


def check_dns(host):
    """Refuse unresolved DNS; ACME verifies public reachability/ownership."""
    try:
        answers = socket.getaddrinfo(host, 80, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError(f'DNS {host} belum tersedia. Arahkan A/AAAA ke VPS terlebih dahulu.') from None
    for answer in answers:
        addr = ipaddress.ip_address(answer[4][0])
        if not addr.is_global:
            raise ValueError(f'DNS {host} menunjuk IP nonpublik. SSL membutuhkan DNS publik.')


def download(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'WPI/1.0'})
    with urllib.request.urlopen(request, timeout=90) as response, Path(path).open('wb') as out:
        shutil.copyfileobj(response, out)


class Manager:
    def __init__(self, data_dir=DATA, backup_dir=BACKUPS, runner=run):
        self.data = Path(data_dir)
        self.backups = Path(backup_dir)
        self.runner = runner

    @property
    def config(self):
        path = self.data / 'config.json'
        return json.loads(path.read_text()) if path.exists() else {}

    @property
    def web(self):
        cfg = self.config
        if not cfg:
            raise ValueError('Jalankan setup atau install WordPress terlebih dahulu.')
        from .php_settings import PHPSettings
        settings = PHPSettings(self)
        return WebStack(self.runner, cfg['stack'], cfg['php_version'],
                        post_max_size_mb=settings.web_body_mib())

    def remember_config(self, identifier):
        from .repair import SiteRepair
        site = self.site(identifier)
        if (Path(site['root']) / 'wp-config.php').is_file():
            return SiteRepair(self).remember_config(identifier)

    def _site_memory_settings(self, site):
        if self.config.get('php_settings'):
            from .php_settings import PHPSettings
            value = f"{PHPSettings(self).effective()['memory_mib']}M"
            for name in ('WP_MEMORY_LIMIT', 'WP_MAX_MEMORY_LIMIT'):
                self.wp(site, 'config', 'set', name, value, '--type=constant')

    def repair_site(self, identifier, check_only=False):
        from .repair import SiteRepair
        repair = SiteRepair(self)
        cache_started = False
        if not check_only and self.site(identifier).get('redis_cache', {}).get('enabled'):
            # Start only this managed cache, before WordPress needs it. Config
            # recovery below also refreshes its local credential overlay.
            service = self.redis.service(self.site(identifier)['id'])
            cache_started = self.runner(['systemctl', 'is-active', '--quiet', service],
                                        check=False).returncode != 0
            self.redis.start_site(identifier)
        report = repair.diagnose(identifier) if check_only else repair.repair(identifier)
        if cache_started:
            report.setdefault('actions', []).append({'redis': 'started'})
            if report['status'] == 'healthy':
                report['status'] = 'resolved'
        return report

    def php_settings_status(self):
        from .php_settings import PHPSettings
        return PHPSettings(self).status()

    def set_php_settings(self, memory_limit=None, upload_max_filesize=None, reset=False):
        from .php_settings import PHPSettings
        settings = PHPSettings(self)
        return settings.reset() if reset else settings.apply(memory_limit, upload_max_filesize)

    @property
    def redis(self):
        from .redis_cache import RedisCache
        return RedisCache(self)

    def enable_redis(self, identifier=None):
        if self.config.get('php_settings'):
            from .autotune import detect_resources, profile, redis_reserve_bytes
            from .php_settings import configured_profile
            from .redis_cache import redis_resource_profile
            resources = detect_resources()
            candidates = {site['id'] for site in self.sites() if
                          site.get('redis_cache', {}).get('enabled') or
                          (identifier is None and site['status'] == 'active' and
                           site.get('redis_cache', {}).get('enabled') is not False)}
            if identifier is not None:
                candidates.add(self.site(identifier)['id'])
            current_cache = self.config.get('redis_cache', {})
            resources['redis_cache'] = {**current_cache,
                'memory_reserve_bytes': current_cache.get('reserve_mib', 0) * 1024 * 1024}
            current_reserve = redis_reserve_bytes(resources)
            resources['redis_cache'] = redis_resource_profile(
                resources['memory_total'], max(1, len(candidates)))
            resources['redis_cache']['memory_reserve_bytes'] = max(
                current_reserve, resources['redis_cache']['memory_reserve_bytes'])
            configured_profile(profile(resources), self.config['php_settings'],
                               resources, check_capacity=True)
        self.redis.install()
        if identifier is not None:
            return self.redis.enable_site(identifier)
        reports = []
        for site in self.sites():
            if site['status'] != 'active' or site.get('redis_cache', {}).get('enabled') is False:
                continue
            reports.append(self.redis.enable_site(site['id']))
        return {'server': self.redis.status(), 'sites': reports}

    def redis_status(self):
        return self.redis.status()

    def redis_flush(self, identifier):
        return self.redis.flush_site(identifier)

    def redis_disable(self, identifier):
        return self.redis.disable_site(identifier)

    def _site_cache_config(self, site):
        if self.config.get('redis_cache', {}).get('enabled') \
                and site.get('redis_cache', {}).get('enabled'):
            self.redis.prepare_site_config(site['id'])

    def _enable_site_cache(self, identifier):
        if self.config.get('redis_cache', {}).get('enabled'):
            site = self.site(identifier)
            if site.get('redis_cache', {}).get('enabled') is not False:
                return self.redis.enable_site(identifier)

    def optimize(self):
        if not self.config or not self.config.get('setup_complete', True):
            return {'enabled': False, 'reason': 'Menunggu setup server selesai.'}
        redis = self.enable_redis()
        tuning = self.enable_autotune()
        self._install_performance_timer()
        return {'enabled': True, 'redis': redis, 'php': tuning}

    def performance_tick(self):
        if not self.config.get('setup_complete', True) \
                or not self.config.get('redis_cache', {}).get('enabled'):
            return {'enabled': False}
        # Adjust resource limits only. A stopped cache is handled by Repair.
        return self.redis.optimize()

    def _install_performance_timer(self):
        units = Path('/etc/systemd/system')
        files = {
            'wpi-performance.service': '[Unit]\nDescription=WPI Redis resource optimization\n'
                'After=network.target\n[Service]\nType=oneshot\n'
                'ExecStart=/usr/local/bin/wpi performance-tick\nUMask=0077\n'
                'Nice=10\nNoNewPrivileges=true\n',
            'wpi-performance.timer': '[Unit]\nDescription=WPI Redis resource updates\n'
                '[Timer]\nOnBootSec=90s\nOnUnitActiveSec=60s\nAccuracySec=10s\n'
                '[Install]\nWantedBy=timers.target\n',
        }
        for name, contents in files.items():
            target = units / name
            if target.is_symlink() or target.exists() and not target.read_text().startswith(
                    '[Unit]\nDescription=WPI Redis resource'):
                raise ValueError('Unit optimasi Redis sudah dipakai aplikasi lain.')
            if not target.exists() or target.read_text() != contents:
                WebStack._atomic_file(target, contents.encode(), 0o644)
        self.runner(['systemctl', 'daemon-reload'])
        self.runner(['systemctl', 'enable', '--now', 'wpi-performance.timer'])

    def sites(self):
        return [json.loads(p.read_text()) for p in sorted((self.data / 'sites').glob('*.json'))]

    def save_site(self, site):
        if not re.fullmatch(r'[a-f0-9]{12}', site['id']):
            raise ValueError('ID situs tidak valid.')
        atomic_json(self.data / 'sites' / (site['id'] + '.json'), site)

    def site(self, identifier):
        for site in self.sites():
            if identifier in [site['id'], *site_hosts(site)]:
                return site
        raise ValueError('Situs tidak ditemukan. Lihat menu daftar situs.')

    def ensure_free_domain(self, host, allow_site=None):
        host = domain(host)
        for site in self.sites():
            if host in site_hosts(site) and site['id'] != allow_site:
                raise ValueError(f'Domain {host} sudah dipakai situs lain.')
        pma = self.config.get('phpmyadmin', {})
        if host == pma.get('domain'):
            raise ValueError('Domain sudah dipakai phpMyAdmin.')
        return host

    def wp(self, site, *args, **kwargs):
        cfg = self.config
        # WP-CLI launches child commands (rewrite/db). www-data must be able
        # to traverse their working directory, regardless of root's current cwd.
        kwargs.setdefault('cwd', site['root'])
        env = dict(kwargs.pop('env', {}))
        env['WP_CLI_PHP'] = f'/usr/bin/php{cfg["php_version"]}'
        kwargs['env'] = env
        command = ['runuser', '-u', 'www-data', '--', f'/usr/bin/php{cfg["php_version"]}',
                   WP, f'--path={site["root"]}', '--skip-plugins', '--skip-themes', *args]
        return self.runner(command, **kwargs)

    def setup(self, stack='nginx', database='mariadb'):
        if stack not in ('nginx', 'apache') or database not in ('mariadb', 'mysql'):
            raise ValueError('Stack/database tidak dikenal.')
        if self.config:
            if (self.config['stack'], self.config['database']) != (stack, database):
                raise ValueError('Stack server sudah ditetapkan. Gunakan pilihan yang sama.')
            if self.config.get('setup_complete', True):
                if not self.config.get('redis_cache', {}).get('enabled'):
                    self.optimize()
                elif not self.config.get('autotune_enabled'):
                    self.enable_autotune()
                return self.config
        release = dict(line.strip().split('=', 1) for line in Path('/etc/os-release').read_text().splitlines()
                       if '=' in line)
        version = release.get('VERSION_ID', '').strip('"')
        if release.get('ID', '').strip('"') != 'ubuntu' or version not in ('22.04', '24.04'):
            raise ValueError('Gunakan Ubuntu 22.04 atau 24.04 LTS bersih.')
        # Do not adopt/dismantle somebody else's database or hosting panel.
        if not self.config:
            for service in ('nginx', 'apache2', 'mysql', 'mariadb'):
                existing = self.runner(['systemctl', 'is-active', service], check=False)
                if existing.returncode == 0:
                    raise ValueError(f'Layanan {service} sudah aktif dan belum dikelola WPI. Gunakan VPS bersih.')
            for marker in ('/www/server/panel', '/usr/local/hestia', '/usr/local/cpanel'):
                if Path(marker).exists():
                    raise ValueError('Panel hosting lain terdeteksi. Gunakan VPS terpisah.')
            mysql_data = Path('/var/lib/mysql')
            if mysql_data.exists() and any(mysql_data.iterdir()):
                raise ValueError('Data database yang belum dikelola WPI terdeteksi. Gunakan VPS bersih.')
            for package in ('nginx', 'apache2', 'mysql-server', 'mariadb-server'):
                result = self.runner(['dpkg-query', '-W', '-f=${Status}', package], check=False)
                if result.returncode == 0 and 'install ok installed' in (result.stdout or ''):
                    raise ValueError(f'Paket {package} sudah terpasang. Gunakan VPS bersih.')
        php = '8.1' if version == '22.04' else '8.3'
        cfg = {'stack': stack, 'database': database, 'php_version': php,
               'ubuntu': version, 'setup_complete': False}
        # Journal the selected stack so interrupted apt setup can safely resume.
        atomic_json(self.data / 'config.json', cfg)
        print('Menginstal web server, PHP-FPM, database, Certbot, dan WP-CLI...')
        self.runner(['apt-get', 'update'])
        packages = [stack if stack == 'nginx' else 'apache2', f'{database}-server',
                    'curl', 'ca-certificates', 'unzip', 'openssl', 'certbot', 'apache2-utils', 'iproute2',
                    f'php{php}-fpm', f'php{php}-cli', f'php{php}-mysql', f'php{php}-curl',
                    f'php{php}-gd', f'php{php}-mbstring', f'php{php}-xml', f'php{php}-zip',
                    f'php{php}-intl', f'php{php}-bcmath']
        self.runner(['apt-get', 'install', '-y', '--no-install-recommends', *packages])
        db_service = 'mariadb' if database == 'mariadb' else 'mysql'
        # Local socket only: no exposed SQL TCP port. DB root retains Ubuntu socket auth.
        db_conf = Path('/etc/mysql/conf.d/wpi-local.cnf')
        db_conf.write_text('[mysqld]\nbind-address = 127.0.0.1\n', encoding='utf-8')
        self.runner(['systemctl', 'restart', db_service])
        self.runner(['systemctl', 'enable', '--now', db_service, f'php{php}-fpm'])
        self.redis.install()
        cfg = self.config
        # Resource profiles and the running controller configure PHP/FPM without
        # asking the user to enter process counts or PHP memory limits.
        AutoTuner(php, self.runner, data_dir=self.data).install()
        if stack == 'apache':
            self.runner(['a2enmod', 'rewrite', 'proxy', 'proxy_fcgi', 'setenvif', 'ssl',
                         'auth_basic', 'authn_file', 'headers'])
            self.runner(['a2dissite', '000-default'])
        else:
            Path('/etc/nginx/sites-enabled/default').unlink(missing_ok=True)
        Path(WP).parent.mkdir(parents=True, exist_ok=True)
        # Obtain current WP-CLI through official HTTPS distribution, then validate its runtime.
        download('https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar', WP)
        os.chmod(WP, 0o755)
        self.runner([f'/usr/bin/php{php}', WP, '--info'])
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        WWW.mkdir(parents=True, exist_ok=True, mode=0o755)
        web = WebStack(self.runner, stack, php)
        # Minimal/cloud images can prevent apt from starting daemons. The
        # managed guard reloads its configuration, so start the service first.
        self.runner(['systemctl', 'enable', '--now', 'nginx' if stack == 'nginx' else 'apache2'])
        web.install_default_guard()
        web.validate_reload()
        # Use Ubuntu Certbot's existing renewal timer; hooks reload only after successful renewal.
        self.runner(['systemctl', 'enable', '--now', 'certbot.timer'])
        self.runner(['mysql', '--protocol=socket', '-uroot'], input=
                    "DROP USER IF EXISTS ''@'localhost';\nDROP DATABASE IF EXISTS test;\n")
        cfg['setup_complete'] = True
        cfg['autotune_enabled'] = True
        atomic_json(self.data / 'config.json', cfg)
        self._install_performance_timer()
        return cfg

    def enable_autotune(self):
        cfg = self.config
        if not cfg or not cfg.get('setup_complete', True):
            return {'enabled': False, 'reason': 'Menunggu setup server selesai.'}
        if not shutil.which('ss'):
            self.runner(['apt-get', 'update'])
            self.runner(['apt-get', 'install', '-y', '--no-install-recommends', 'iproute2'])
        report = AutoTuner(cfg['php_version'], self.runner, data_dir=self.data).install()
        # Update managed vhosts too so older installations inherit the new
        # automatic upload ceiling and private status route protection.
        web = self.web
        for site in self.sites():
            web.write_site(site)
        pma = cfg.get('phpmyadmin')
        if pma:
            web.install_phpmyadmin(pma['domain'], '/usr/share/phpmyadmin', '/etc/wpi/pma.htpasswd')
        cfg['autotune_enabled'] = True
        atomic_json(self.data / 'config.json', cfg)
        # Upgrade establishes a baseline only for a loadable configuration.
        # Broken legacy files are left for the user-selected Repair operation.
        for site in self.sites():
            try:
                self.remember_config(site['id'])
            except (ValueError, RuntimeError, OSError):
                pass
        return report

    def autotune_tick(self):
        cfg = self.config
        if not cfg or not cfg.get('setup_complete', True):
            return {'enabled': False, 'reason': 'Menunggu setup server selesai.'}
        return AutoTuner(cfg['php_version'], self.runner, data_dir=self.data).tick()

    def autotune_status(self):
        cfg = self.config
        if not cfg or not cfg.get('autotune_enabled'):
            return {'enabled': False, 'reason': 'Auto PHP-FPM belum diaktifkan.'}
        return AutoTuner(cfg['php_version'], self.runner, data_dir=self.data).status()

    def install(self, host, email, title='WordPress', admin='admin', password=None):
        host = self.ensure_free_domain(host)
        email = email_address(email)
        if not re.fullmatch(r'[a-zA-Z0-9_\-]{3,60}', admin):
            raise ValueError('Username admin: 3-60 karakter huruf/angka/_/-.')
        cfg = self.config
        if not cfg or not cfg.get('setup_complete', True):
            self.setup(cfg.get('stack', 'nginx'), cfg.get('database', 'mariadb'))
        check_dns(host)
        password = password or secrets.token_urlsafe(24)
        if len(password) < 12 or '\n' in password or '\r' in password:
            raise ValueError('Password minimal 12 karakter dan tidak boleh berisi baris baru.')
        ident = secrets.token_hex(6)
        root = WWW / ident / 'public'
        if root.parent.exists():
            raise ValueError('Direktori situs sudah ada.')
        site = {'id': ident, 'primary': host, 'aliases': [], 'secondary': [], 'root': str(root), 'tls': [],
                'email': email, 'db_name': f'wpi_{ident}', 'db_user': f'wpi_{ident}',
                'admin': admin, 'title': title, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'status': 'installing'}
        root.mkdir(parents=True, mode=0o755)
        dbpass = secrets.token_hex(24)
        atomic_json(self.data / 'credentials' / (ident + '.json'),
                    {'wordpress_admin': admin, 'wordpress_password': password,
                     'database_user': site['db_user'], 'database_password': dbpass})
        # Persist failed installs for diagnosis/retry. Never remove somebody else's DB/data.
        self.save_site(site)
        try:
            self.runner(['chown', '-R', 'www-data:www-data', str(root.parent)])
            sql = (f"CREATE DATABASE `{site['db_name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
                   f"CREATE USER '{site['db_user']}'@'localhost' IDENTIFIED BY '{dbpass}';\n"
                   f"GRANT ALL PRIVILEGES ON `{site['db_name']}`.* TO '{site['db_user']}'@'localhost';\n")
            self.runner(['mysql', '--protocol=socket', '-uroot'], input=sql)
            # ZIP avoids PHP PharData truncating long filenames in WP 7 tarballs.
            self.wp(site, 'core', 'download', 'https://wordpress.org/latest.zip')
            self.wp(site, 'core', 'verify-checksums')
            self.wp(site, 'config', 'create', f'--dbname={site["db_name"]}',
                    f'--dbuser={site["db_user"]}', '--dbhost=localhost',
                    f'--dbprefix=wp_{ident[:6]}_', '--prompt=dbpass', input=dbpass + '\n')
            self.runner(['chmod', '640', str(root / 'wp-config.php')])
            self.wp(site, 'config', 'set', 'DISALLOW_FILE_EDIT', 'true', '--raw')
            self.wp(site, 'config', 'set', 'WP_AUTO_UPDATE_CORE', 'minor')
            self._site_memory_settings(site)
            self.wp(site, 'core', 'install', f'--url=https://{host}', f'--title={title}',
                    f'--admin_user={admin}', f'--admin_email={email}', '--skip-email',
                    '--prompt=admin_password', input=password + '\n')
            self.wp(site, 'rewrite', 'structure', '/%postname%/')
            self.web.write_site(site)
            self.web.obtain_certificate(host, email, str(root))
            site['tls'] = [host]
            self.web.write_site(site)
            site['status'] = 'active'
            self.save_site(site)
            # Root-only recovery credentials; omitted from backup archives and command/log output.
            atomic_json(self.data / 'credentials' / (ident + '.json'),
                        {'wordpress_admin': admin, 'wordpress_password': password,
                         'database_user': site['db_user'], 'database_password': dbpass})
            self._enable_site_cache(ident)
            site = self.site(ident)
            self.remember_config(ident)
            return site, password
        except BaseException:
            site['status'] = 'incomplete'
            self.save_site(site)
            raise

    def retry_install_ssl(self, identifier):
        site = self.site(identifier)
        self.wp(site, 'core', 'is-installed')
        self.renew_ssl(identifier)
        site = self.site(identifier)
        site['status'] = 'active'
        self.save_site(site)
        self._enable_site_cache(identifier)
        self.remember_config(identifier)

    def resume_install(self, identifier):
        site = self.site(identifier)
        if site['status'] == 'active':
            raise ValueError('Situs sudah aktif. Gunakan SSL/update sesuai kebutuhan.')
        credentials = json.loads((self.data / 'credentials' / (site['id'] + '.json')).read_text())
        check_dns(site['primary'])
        dbpass = credentials['database_password']
        sql = (f"CREATE DATABASE IF NOT EXISTS `{site['db_name']}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
               f"CREATE USER IF NOT EXISTS '{site['db_user']}'@'localhost' IDENTIFIED BY '{dbpass}';\n"
               f"GRANT ALL PRIVILEGES ON `{site['db_name']}`.* TO '{site['db_user']}'@'localhost';\n")
        self.runner(['mysql', '--protocol=socket', '-uroot'], input=sql)
        self.runner(['chown', '-R', 'www-data:www-data', str(Path(site['root']).parent)])
        intact = self.wp(site, 'core', 'verify-checksums', check=False)
        if intact.returncode:
            # Preserve partial/corrupt core directories for recovery, then fetch
            # complete directories. wp-content and wp-config.php are retained.
            preserved = Path(site['root']).parent / ('incomplete-core-' + secrets.token_hex(4))
            preserved.mkdir(mode=0o700)
            for name in ('wp-admin', 'wp-includes'):
                source = Path(site['root']) / name
                if source.exists():
                    source.rename(preserved / name)
            self.wp(site, 'core', 'download', 'https://wordpress.org/latest.zip', '--force')
        self.wp(site, 'core', 'verify-checksums')
        if not (Path(site['root']) / 'wp-config.php').exists():
            self.wp(site, 'config', 'create', f'--dbname={site["db_name"]}', f'--dbuser={site["db_user"]}',
                    '--dbhost=localhost', f'--dbprefix=wp_{site["id"][:6]}_', '--prompt=dbpass', input=dbpass + '\n')
        self.runner(['chmod', '640', str(Path(site['root']) / 'wp-config.php')])
        self._site_cache_config(site)
        installed = self.wp(site, 'core', 'is-installed', check=False)
        if installed.returncode:
            self.wp(site, 'core', 'install', f'--url=https://{site["primary"]}',
                    f'--title={site.get("title", "WordPress")}', f'--admin_user={site["admin"]}',
                    f'--admin_email={site["email"]}', '--skip-email', '--prompt=admin_password',
                    input=credentials['wordpress_password'] + '\n')
        self.wp(site, 'config', 'set', 'DISALLOW_FILE_EDIT', 'true', '--raw')
        self.wp(site, 'config', 'set', 'WP_AUTO_UPDATE_CORE', 'minor')
        self._site_memory_settings(site)
        self.wp(site, 'rewrite', 'structure', '/%postname%/')
        self.web.write_site(site)
        self.retry_install_ssl(identifier)
        return self.site(identifier), credentials['wordpress_password']

    def add_domain(self, identifier, host, kind='alias', www=False):
        if kind not in ('alias', 'redirect'):
            raise ValueError('Jenis domain harus Alias atau Redirect.')
        site = self.site(identifier)
        host = domain(host)
        hosts = [host]
        if www and not host.startswith('www.'):
            hosts.append(domain('www.' + host))
        for candidate in hosts:
            self.ensure_free_domain(candidate, allow_site=site['id'])
            if candidate in site_hosts(site):
                raise ValueError(f'Domain {candidate} sudah terpasang pada situs ini.')
            check_dns(candidate)
        old = copy.deepcopy(site)
        role = 'aliases' if kind == 'alias' else 'secondary'
        site.setdefault(role, []).extend(hosts)
        new_certificates = [candidate for candidate in hosts
                            if not self.web.certificate_ready(candidate)]
        try:
            self.web.write_site(site)
            for candidate in hosts:
                self.web.obtain_certificate(candidate, site['email'], site['root'])
                if candidate not in site['tls']:
                    site['tls'].append(candidate)
            self.web.write_site(site)
            self.save_site(site)
        except BaseException:
            self.web.write_site(old)
            self.save_site(old)
            for candidate in new_certificates:
                self.delete_certificate(candidate)
            raise
        return site

    def add_secondary(self, identifier, host):
        """Compatibility entry point for the former secondary/301 command."""
        return self.add_domain(identifier, host, kind='redirect')

    def remove_domain(self, identifier, host):
        site = self.site(identifier)
        host = domain(host)
        if host == site['primary']:
            raise ValueError('Primary tidak dapat dihapus. Set as Primary domain lain terlebih dahulu.')
        if host not in site_hosts(site):
            raise ValueError('Domain tidak ditemukan pada situs ini.')
        self.backup(identifier)
        old = copy.deepcopy(site)
        for role in ('aliases', 'secondary'):
            if host in site.get(role, []):
                site[role].remove(host)
        site['tls'] = [h for h in site['tls'] if h != host]
        try:
            self.web.write_site(site)
            self.save_site(site)
        except BaseException:
            self.web.write_site(old)
            self.save_site(old)
            raise
        self.delete_certificate(host)
        return site

    def remove_secondary(self, identifier, host):
        site = self.site(identifier)
        host = domain(host)
        if host == site['primary']:
            raise ValueError('Primary tidak dapat dihapus. Ganti primary terlebih dahulu.')
        if host not in site.get('secondary', []):
            raise ValueError('Domain secondary tidak ditemukan.')
        return self.remove_domain(identifier, host)

    def delete_certificate(self, host):
        # Only single-domain WPI certs are created; remove them after no managed host references remain.
        if any(host in site_hosts(s) for s in self.sites()):
            return
        if host == self.config.get('phpmyadmin', {}).get('domain'):
            return
        if self.web.certificate_ready(host) and self.web.letsencrypt_ready(host):
            result = self.runner(['certbot', 'delete', '--cert-name', host, '--non-interactive'], check=False)
            if result.returncode:
                print('Domain dilepas; sertifikat lama belum dapat dibersihkan. Periksa certbot certificates.')
        self.web.remove_migrated_certificate(host)

    def replacement_pairs(self, old, new):
        # Boundaries protect similarly named hosts; serialized and JSON escaped URLs both supported.
        boundary = r'(?![A-Za-z0-9_.:-])'
        return [(re.escape(prefix + old) + boundary, target + new)
                for prefix, target in [('https://', 'https://'), ('http://', 'https://'),
                                       ('//', '//'), ('https:\\/\\/', 'https:\\/\\/'),
                                       ('http:\\/\\/', 'https:\\/\\/'), ('\\/\\/', '\\/\\/')]]

    def change_primary(self, identifier, new):
        """Replace and detach the old primary, retained for existing CLI clients."""
        return self._change_primary(identifier, new, old_domain='remove')

    def set_primary(self, identifier, new, old_domain='alias'):
        """Promote an existing Alias and retain the old domain unless requested."""
        if old_domain not in ('alias', 'redirect', 'remove'):
            raise ValueError('Peran primary lama harus Alias, Redirect, atau remove.')
        site = self.site(identifier)
        new = domain(new)
        if new == site['primary']:
            raise ValueError('Domain sudah menjadi primary.')
        if new not in site.get('aliases', []):
            raise ValueError('Tambahkan domain sebagai Alias sebelum Set as Primary.')
        return self._change_primary(identifier, new, old_domain=old_domain)

    def _change_primary(self, identifier, new, old_domain):
        site = self.site(identifier)
        new = self.ensure_free_domain(new, allow_site=site['id'])
        if new == site['primary']:
            raise ValueError('Domain baru sama dengan primary saat ini.')
        check_dns(new)
        snapshot = self.backup(identifier)
        original = copy.deepcopy(site)
        old = site['primary']
        # Provision new ACME hostname before touching content or old primary configuration.
        temporary = copy.deepcopy(site)
        if new not in site_hosts(temporary):
            temporary.setdefault('aliases', []).append(new)
        try:
            self.web.write_site(temporary)
            self.web.obtain_certificate(new, site['email'], site['root'])
            self.wp(site, 'maintenance-mode', 'activate')
            for pattern, replacement in self.replacement_pairs(old, new):
                self.wp(site, 'search-replace', pattern, replacement, '--regex', '--precise',
                        '--recurse-objects', '--all-tables-with-prefix', '--skip-columns=guid',
                        '--regex-delimiter=~', '--report-changed-only')
            self.wp(site, 'option', 'update', 'home', 'https://' + new)
            self.wp(site, 'option', 'update', 'siteurl', 'https://' + new)
            site['primary'] = new
            for role in ('aliases', 'secondary'):
                if role in site:
                    site[role] = [h for h in site[role] if h not in (old, new)]
            if old_domain != 'remove':
                role = 'aliases' if old_domain == 'alias' else 'secondary'
                site.setdefault(role, []).append(old)
            else:
                site['tls'] = [h for h in site['tls'] if h != old]
            if new not in site['tls']:
                site['tls'].append(new)
            self.web.write_site(site)
            self.wp(site, 'rewrite', 'flush')
            self.wp(site, 'cache', 'flush')
            self.save_site(site)
        except BaseException as error:
            try:
                self.restore_database(original, snapshot)
                self.web.write_site(original)
                self.save_site(original)
            except Exception as recovery:
                raise RuntimeError(f'Pergantian gagal dan pemulihan belum selesai. Backup: {snapshot}. '
                                   'Jalankan menu restore setelah memeriksa layanan.') from recovery
            raise RuntimeError(f'Pergantian dibatalkan; database dan konfigurasi dikembalikan. Backup: {snapshot}') from error
        finally:
            self.wp(original, 'maintenance-mode', 'deactivate', check=False)
        if old_domain == 'remove':
            self.delete_certificate(old)
        self.remember_config(identifier)
        return site, snapshot

    def backup(self, identifier):
        site = self.site(identifier)
        self.wp(site, 'core', 'is-installed')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S')
        folder = self.backups / site['id'] / (stamp + '-' + secrets.token_hex(3))
        folder.mkdir(parents=True, mode=0o700)
        os.chmod(self.backups, 0o700)
        try:
            with (folder / 'database.sql').open('w', encoding='utf-8') as output:
                self.wp(site, 'db', 'export', '-', '--quiet', stdout=output, capture_output=False, stderr=subprocess.PIPE)
            with (folder / 'database.sql').open('rb') as source, gzip.open(folder / 'database.sql.gz', 'wb') as target:
                shutil.copyfileobj(source, target)
            (folder / 'database.sql').unlink()
            with tarfile.open(folder / 'files.tar.gz', 'w:gz') as archive:
                archive.add(site['root'], arcname='public', recursive=True)
            atomic_json(folder / 'site.json', site)
            sums = {name: self.file_hash(folder / name) for name in ('database.sql.gz', 'files.tar.gz', 'site.json')}
            atomic_json(folder / 'manifest.json', {'site_id': site['id'], 'sha256': sums})
            (folder / 'COMPLETE').touch(mode=0o600)
        except Exception:
            (folder / 'INCOMPLETE').touch(mode=0o600)
            raise
        return folder

    @staticmethod
    def file_hash(path):
        result = hashlib.sha256()
        with Path(path).open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                result.update(block)
        return result.hexdigest()

    def verified_backup(self, site, folder):
        folder = Path(folder).resolve()
        if not folder.is_relative_to(self.backups.resolve()) or not (folder / 'COMPLETE').is_file():
            raise ValueError('Backup harus lengkap dan berada di direktori backup WPI.')
        manifest = json.loads((folder / 'manifest.json').read_text())
        if manifest['site_id'] != site['id']:
            raise ValueError('Backup milik situs lain.')
        for name in ('database.sql.gz', 'files.tar.gz', 'site.json'):
            if self.file_hash(folder / name) != manifest['sha256'][name]:
                raise ValueError('Checksum backup tidak cocok: ' + name)
        return folder

    def restore_database(self, site, folder):
        folder = self.verified_backup(site, folder)
        # WP runs under www-data; make an isolated readable SQL temp outside the public root.
        with tempfile.TemporaryDirectory(prefix='wpi-restore-') as temp:
            self.runner(['chown', 'root:www-data', temp])
            os.chmod(temp, 0o750)
            sql = Path(temp) / 'database.sql'
            with gzip.open(folder / 'database.sql.gz', 'rb') as source, sql.open('wb') as target:
                shutil.copyfileobj(source, target)
            self.runner(['chown', 'root:www-data', str(sql)])
            os.chmod(sql, 0o640)
            self.wp(site, 'db', 'import', str(sql), '--quiet')

    def restore(self, identifier, folder):
        current = self.site(identifier)
        folder = self.verified_backup(current, folder)
        old = json.loads((folder / 'site.json').read_text())
        if old['root'] != current['root'] or old['id'] != current['id']:
            raise ValueError('Lokasi/identitas backup tidak cocok.')
        # Cache belongs to this server's runtime, not the restored content.
        # Preserve local opt-out and instance credentials across snapshots.
        if 'redis_cache' in current:
            old['redis_cache'] = copy.deepcopy(current['redis_cache'])
        restore_credentials = None
        if current.get('migration_id'):
            # Imported snapshots retain the source's wp-config and manifest.
            # The destination has a new SQL password; restore must keep it and
            # the destination's pending ACME ownership while restoring content.
            credential_path = self.data / 'credentials' / (current['id'] + '.json')
            if credential_path.is_symlink():
                raise ValueError('Kredensial database target tidak boleh symlink.')
            restore_credentials = json.loads(credential_path.read_text())
            if (restore_credentials.get('database_user') != current['db_user']
                    or not re.fullmatch(r'[a-f0-9]{48}', str(restore_credentials.get('database_password', '')))
                    or (old['db_name'], old['db_user']) != (current['db_name'], current['db_user'])):
                raise ValueError('Kredensial/identitas database target tidak cocok dengan backup migrasi.')
            old['migration_id'] = current['migration_id']
        for host in site_hosts(old):
            self.ensure_free_domain(host, allow_site=current['id'])
        root = Path(current['root'])
        if root != WWW / current['id'] / 'public' or root.is_symlink():
            raise ValueError('Lokasi WordPress tidak aman.')
        staged = root.parent / 'restore-stage'
        displaced = root.parent / ('previous-' + secrets.token_hex(4))
        if staged.exists():
            raise ValueError('Restore staging tersisa. Periksa dahulu.')
        safety = self.backup(identifier)
        # Removed domains lose their old certificates. Restore ACME routes and issue
        # missing certificates before putting the restored HTTPS URLs into service.
        temporary = copy.deepcopy(current)
        needed = [h for h in site_hosts(old) if not self.web.certificate_ready(h)]
        if needed:
            for host in needed:
                check_dns(host)
                if host not in site_hosts(temporary):
                    temporary.setdefault('aliases', []).append(host)
            try:
                self.web.write_site(temporary)
                for host in needed:
                    self.web.obtain_certificate(host, old['email'], old['root'])
            except BaseException:
                self.web.write_site(current)
                raise
        try:
            staged.mkdir(mode=0o700)
            with tarfile.open(folder / 'files.tar.gz', 'r:gz') as archive:
                members = archive.getmembers()
                for member in members:
                    path = Path(member.name)
                    if path.is_absolute() or '..' in path.parts or path.parts[0] != 'public' or not (member.isfile() or member.isdir()):
                        raise ValueError('Backup berisi path/link yang tidak aman.')
                # Only validated regular files/directories; works on Ubuntu Python 3.10.
                archive.extractall(staged, members=members)
            self.wp(current, 'maintenance-mode', 'activate')
            root.rename(displaced)
            (staged / 'public').rename(root)
            self.runner(['chown', '-R', 'www-data:www-data', str(root)])
            if restore_credentials:
                for key, value in (('DB_NAME', current['db_name']), ('DB_USER', current['db_user']),
                                   ('DB_HOST', 'localhost')):
                    self.wp(old, 'config', 'set', key, value)
                # WP-CLI named prompts only support options. The missing
                # positional value must use --prompt; stdin stays private.
                self.wp(old, 'config', 'set', 'DB_PASSWORD', '--prompt',
                        input=restore_credentials['database_password'] + '\n')
                self.runner(['chmod', '640', str(root / 'wp-config.php')])
            self._site_cache_config(old)
            self.restore_database(old, folder)
            old['tls'] = [h for h in site_hosts(old) if self.web.certificate_ready(h)]
            self.web.write_site(old)
            self.save_site(old)
            self.wp(old, 'cache', 'flush')
        except BaseException:
            if displaced.exists():
                if root.exists():
                    root.rename(root.parent / ('failed-restore-' + secrets.token_hex(4)))
                displaced.rename(root)
                self.restore_database(current, safety)
                self.web.write_site(current)
                self.save_site(current)
            else:
                self.web.write_site(current)
            raise
        finally:
            self.wp(current, 'maintenance-mode', 'deactivate', check=False)
            if staged.exists():
                shutil.rmtree(staged)
        self._site_memory_settings(old)
        self._enable_site_cache(identifier)
        self.remember_config(identifier)
        return self.site(identifier), safety

    def renew_ssl(self, identifier):
        site = self.site(identifier)
        for host in site_hosts(site):
            check_dns(host)
            self.web.obtain_certificate(host, site['email'], site['root'])
            if host not in site['tls']:
                site['tls'].append(host)
        self.web.write_site(site)
        self.save_site(site)
        return site

    def install_pma(self, host, email, user='panel', password=None):
        cfg = self.config
        if not cfg:
            raise ValueError('Install WordPress / setup server terlebih dahulu.')
        if cfg.get('phpmyadmin'):
            raise ValueError('phpMyAdmin sudah terpasang.')
        host = self.ensure_free_domain(host)
        email = email_address(email)
        if not re.fullmatch(r'[a-zA-Z0-9_\-]{3,40}', user):
            raise ValueError('Username Basic Auth tidak valid.')
        password = password or secrets.token_urlsafe(24)
        if len(password) < 12 or any(c in password for c in '\r\n'):
            raise ValueError('Password minimal 12 karakter, satu baris.')
        check_dns(host)
        if Path('/etc/apache2/conf-enabled/phpmyadmin.conf').exists():
            raise ValueError('phpMyAdmin global sudah aktif. Nonaktifkan alias global dahulu.')
        self.runner(['debconf-set-selections'], input='phpmyadmin phpmyadmin/dbconfig-install boolean false\n'
                    'phpmyadmin phpmyadmin/reconfigure-webserver multiselect\n')
        self.runner(['apt-get', 'install', '-y', '--no-install-recommends', 'phpmyadmin'])
        # Prevent package-provided global Alias bypassing our private vhost Basic Auth.
        if Path('/etc/apache2/conf-enabled/phpmyadmin.conf').exists():
            self.runner(['a2disconf', 'phpmyadmin'])
        auth = Path('/etc/wpi/pma.htpasswd')
        auth.parent.mkdir(mode=0o755, exist_ok=True)
        self.runner(['htpasswd', '-Bci', str(auth), user], input=password + '\n')
        self.runner(['chown', 'root:www-data', str(auth)])
        os.chmod(auth, 0o640)
        conf = Path('/etc/phpmyadmin/conf.d/wpi.php')
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("<?php\n$cfg['blowfish_secret'] = '" + secrets.token_hex(16) + "';\n"
                        "$cfg['Servers'][1]['auth_type'] = 'cookie';\n"
                        "$cfg['Servers'][1]['host'] = 'localhost';\n"
                        "$cfg['Servers'][1]['AllowNoPassword'] = false;\n"
                        "$cfg['AllowArbitraryServer'] = false;\n", encoding='utf-8')
        os.chmod(conf, 0o640)
        self.runner(['chown', 'root:www-data', str(conf)])
        acme = WWW / 'pma-acme'
        acme.mkdir(parents=True, mode=0o755, exist_ok=True)
        try:
            self.web.install_phpmyadmin(host, '/usr/share/phpmyadmin', str(auth))
            self.web.obtain_certificate(host, email, str(acme))
            self.web.install_phpmyadmin(host, '/usr/share/phpmyadmin', str(auth))
            cfg['phpmyadmin'] = {'domain': host, 'email': email, 'user': user}
            atomic_json(self.data / 'config.json', cfg)
        except BaseException:
            self.web.remove_phpmyadmin(host)
            auth.unlink(missing_ok=True)
            raise
        return cfg['phpmyadmin'], password

    def remove_pma(self):
        cfg = self.config
        pma = cfg.get('phpmyadmin')
        if not pma:
            raise ValueError('phpMyAdmin belum terpasang.')
        self.web.remove_phpmyadmin(pma['domain'])
        Path('/etc/wpi/pma.htpasswd').unlink(missing_ok=True)
        Path('/etc/phpmyadmin/conf.d/wpi.php').unlink(missing_ok=True)
        del cfg['phpmyadmin']
        atomic_json(self.data / 'config.json', cfg)
        self.delete_certificate(pma['domain'])
        # Intentionally retain apt package for Ubuntu security updates; never touch SQL here.

    def update_wordpress(self, identifier):
        snapshot = self.backup(identifier)
        site = self.site(identifier)
        self.wp(site, 'core', 'update')
        self.wp(site, 'core', 'update-db')
        self.wp(site, 'core', 'verify-checksums')
        self.wp(site, 'cache', 'flush')
        self.remember_config(identifier)
        return snapshot

    def doctor(self):
        cfg = self.config
        if not cfg:
            return ['Server belum dikonfigurasi.']
        lines = []
        for service in ('nginx' if cfg['stack'] == 'nginx' else 'apache2',
                        'mariadb' if cfg['database'] == 'mariadb' else 'mysql',
                        f'php{cfg["php_version"]}-fpm', 'certbot.timer', 'wpi-autotune.timer'):
            result = self.runner(['systemctl', 'is-active', service], check=False)
            lines.append(f'{service}: {"aktif" if result.returncode == 0 else "PERLU DIPERIKSA"}')
        report = self.autotune_status()
        if report.get('enabled') and 'children' in report:
            reasons = {'initial': 'konfigurasi awal', 'stable': 'stabil',
                       'sustained-demand': 'kapasitas naik mengikuti antrean',
                       'idle': 'kapasitas turun karena sepi',
                       'resource-pressure': 'kapasitas turun karena tekanan resource',
                       'capacity-limit': 'mengikuti batas kapasitas server',
                       'hardware-bound': 'mencapai batas RAM/CPU',
                       'telemetry-unavailable': 'menunggu telemetri',
                       'rss-unavailable': 'menunggu sampel memori worker',
                       'pressure-cooldown': 'menunggu setelah penurunan kapasitas',
                       'memory-exhausted': 'menunggu ruang RAM untuk perubahan yang aman',
                       'reload-pending': 'menunggu request berjalan selesai sebelum konfigurasi baru aktif',
                       'reload-failed': 'perubahan gagal; konfigurasi dipulihkan'}
            lines.append(f'Auto PHP-FPM: worker maksimum {report["children"]}; '
                         f'batas kapasitas {report.get("capacity", "?")}; '
                         f'antrean {report.get("queue") if report.get("queue") is not None else "?"}; '
                         f'CPU {report.get("cpu_percent") if report.get("cpu_percent") is not None else "?"}%.')
            lines.append('Keputusan otomatis: ' + reasons.get(report.get('reason'), str(report.get('reason'))))
            if report.get('reload_pending'):
                lines.append('Reload PHP-FPM sedang menunggu request berjalan selesai.')
            limits = report.get('profile', {})
            lines.append(f'Profil PHP: memori {limits.get("memory_mib", "?")} MB; '
                         f'upload {limits.get("upload_mib", "?")} MB; '
                         f'RAM tersedia {report.get("memory_available_mib", "?")} MB.')
            updated = report.get('updated_at', 0)
            age = dt.datetime.now(dt.timezone.utc).timestamp() - updated
            if age > 90:
                lines.append('Telemetri PHP-FPM belum diperbarui lebih dari 90 detik; periksa layanan autotune.')
        else:
            lines.append('Auto PHP-FPM: ' + str(report.get('reason', 'belum tersedia')))
        for site in self.sites():
            result = self.wp(site, 'core', 'is-installed', check=False)
            lines.append(f'{site["primary"]}: {site["status"]}; WordPress '
                         f'{"OK" if result.returncode == 0 else "belum lengkap"}')
        return lines
