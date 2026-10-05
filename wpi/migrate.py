"""Copy a managed WPI server to Ubuntu over an authenticated SSH connection."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import zipfile

from . import __version__
from .core import atomic_json, site_hosts
from .ssh import SSHSession


def _quote(value):
    return shlex.quote(str(value))


class Migration:
    """Export a recoverable snapshot; never remove the source server's data."""

    def __init__(self, manager, session_factory=SSHSession):
        self.manager = manager
        self.session_factory = session_factory

    @staticmethod
    def _machine_id():
        value = Path('/etc/machine-id').read_text().strip()
        if not re.fullmatch(r'[a-f0-9]{32}', value):
            raise ValueError('Identitas server sumber tidak dapat dibaca.')
        return value

    def _journal_path(self, migration_id):
        if not re.fullmatch(r'[a-f0-9]{32}', migration_id):
            raise ValueError('ID migrasi tidak valid.')
        return self.manager.data / 'migrations-out' / (migration_id + '.json')

    def _plan(self, host, username, port):
        # An interrupted transfer/import resumes the same immutable snapshot.
        folder = self.manager.data / 'migrations-out'
        for path in sorted(folder.glob('*.json'), reverse=True):
            record = json.loads(path.read_text())
            if (record.get('host'), record.get('username'), record.get('port')) == (host, username, port):
                if record.get('status') in ('prepared', 'transferring', 'importing', 'failed'):
                    bundle = self.manager.backups / 'migrations' / record['migration_id'] / 'migration.zip'
                    if bundle.is_file() and self.manager.file_hash(bundle) == record.get('sha256'):
                        return record
        return {'migration_id': secrets.token_hex(16), 'host': host, 'username': username,
                'port': port, 'status': 'preflight',
                'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}

    def _save(self, plan, **changes):
        plan.update(changes)
        atomic_json(self._journal_path(plan['migration_id']), plan)

    def _preflight(self, session, plan):
        source_id = self._machine_id()
        migration_id = plan['migration_id']
        script = f'''set -eu
. /etc/os-release
[ "$ID" = ubuntu ] && {{ [ "$VERSION_ID" = 22.04 ] || [ "$VERSION_ID" = 24.04 ]; }} || {{ echo 'Gunakan Ubuntu 22.04/24.04 pada server baru.' >&2; exit 1; }}
[ {_quote(self.manager.config.get('php_version', '8.1'))} != 8.3 ] || [ "$VERSION_ID" != 22.04 ] || {{ echo 'Migrasi PHP 8.3 memerlukan Ubuntu 24.04 agar PHP tidak diturunkan.' >&2; exit 1; }}
[ "$(cat /etc/machine-id)" != {_quote(source_id)} ] || {{ echo 'Server tujuan sama dengan server sumber.' >&2; exit 1; }}
for marker in /www/server/panel /usr/local/hestia /usr/local/cpanel; do
    [ ! -e "$marker" ] || {{ echo 'Server tujuan memiliki panel hosting lain.' >&2; exit 1; }}
done
if [ -d /var/lib/wpi/sites ] && find /var/lib/wpi/sites -maxdepth 1 -name '*.json' -print -quit | grep -q .; then
    [ -f /var/lib/wpi/migrations/{migration_id}.json ] || {{ echo 'Server tujuan sudah memiliki situs WPI. Gunakan server baru.' >&2; exit 1; }}
fi
if [ -e /usr/local/lib/wpi ] && [ ! -L /usr/local/lib/wpi ]; then
    echo 'Direktori aplikasi tujuan tidak dikelola WPI.' >&2; exit 1
fi
if [ -L /usr/local/lib/wpi ]; then
    case "$(readlink -f /usr/local/lib/wpi)" in /usr/local/lib/wpi-releases/*) ;; *) echo 'Symlink aplikasi tujuan tidak dikelola WPI.' >&2; exit 1;; esac
fi
if [ -e /usr/local/bin/wpi ]; then
    [ -f /usr/local/bin/wpi ] && [ ! -L /usr/local/bin/wpi ] && [ "$(sed -n '2p' /usr/local/bin/wpi)" = '# WPI managed launcher' ] || {{ echo 'Launcher tujuan digunakan aplikasi lain.' >&2; exit 1; }}
fi
for path in /usr/local/lib/wpi-releases /var/lib/wpi /var/lib/wpi/sites /var/backups/wpi /var/www/wpi; do
    [ ! -L "$path" ] || {{ echo 'Direktori tujuan tidak boleh symlink.' >&2; exit 1; }}
done
echo WPI_MIGRATION_PREFLIGHT_OK
'''
        result = session.run_root(script)
        if 'WPI_MIGRATION_PREFLIGHT_OK' not in result.stdout.splitlines():
            raise RuntimeError('Pemeriksaan server baru belum selesai.')

    def _space_check(self, sites):
        total = 0
        for site in sites:
            root = Path(site['root'])
            if not root.is_dir() or root.is_symlink():
                raise ValueError('Direktori situs sumber tidak aman atau belum tersedia.')
            for path in root.rglob('*'):
                if path.is_symlink():
                    raise ValueError('Migrasi memerlukan file situs biasa; symlink situs belum didukung.')
                if path.is_file():
                    total += path.stat().st_size
        self.manager.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(self.manager.backups).free < total * 2 + 64 * 1024 * 1024:
            raise ValueError('Ruang disk sumber belum cukup untuk backup dan paket migrasi.')

    def _export(self, plan):
        sites = self.manager.sites()
        if not sites or any(site.get('status') != 'active' for site in sites):
            raise ValueError('Migrasi memerlukan situs WPI aktif; selesaikan instalasi yang belum selesai dahulu.')
        self._space_check(sites)
        cfg = self.manager.config
        destination = self.manager.backups / 'migrations' / plan['migration_id']
        destination.mkdir(parents=True, mode=0o700)
        os.chmod(destination, 0o700)
        payload = {}
        snapshots = []
        maintenance = []
        try:
            # Normal WordPress requests are paused while SQL/files are captured.
            # External writers/cron outside WordPress are not a continuous sync.
            for site in sites:
                active = self.manager.wp(site, 'maintenance-mode', 'is-active', check=False)
                if active.returncode:
                    self.manager.wp(site, 'maintenance-mode', 'activate')
                    maintenance.append(site)
            for site in sites:
                snapshot = self.manager.backup(site['id'])
                snapshots.append(str(snapshot))
                for name in ('database.sql.gz', 'files.tar.gz', 'site.json', 'manifest.json', 'COMPLETE'):
                    payload[f'backups/{site["id"]}/{name}'] = snapshot / name
                credentials = self.manager.data / 'credentials' / (site['id'] + '.json')
                if credentials.is_file() and not credentials.is_symlink():
                    payload[f'credentials/{site["id"]}.json'] = credentials
        finally:
            for site in reversed(maintenance):
                self.manager.wp(site, 'maintenance-mode', 'deactivate', check=False)
        hosts = list(dict.fromkeys(h for site in sites for h in site_hosts(site)))
        pma = cfg.get('phpmyadmin')
        if pma:
            hosts.append(pma['domain'])
            auth = Path('/etc/wpi/pma.htpasswd')
            if not auth.is_file() or auth.is_symlink():
                raise ValueError('Basic Auth phpMyAdmin sumber tidak tersedia.')
            payload['phpmyadmin/htpasswd'] = auth
        web = self.manager.web
        for host in dict.fromkeys(hosts):
            if web.certificate_ready(host):
                certificate, key = web.certificate_paths(host)
                # Only the selected hostname's cert/key, never unrelated ACME accounts.
                payload[f'certs/{host}/fullchain.pem'] = Path(certificate)
                payload[f'certs/{host}/privkey.pem'] = Path(key)
        metadata = {'schema': 1, 'migration_id': plan['migration_id'],
                    'version': __version__, 'source_config': cfg, 'sites': sites,
                    'snapshot_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        atomic_json(destination / 'metadata.json', metadata)
        payload['metadata.json'] = destination / 'metadata.json'
        manifest = {name: self.manager.file_hash(path) for name, path in payload.items()}
        bundle = destination / 'migration.zip'
        temporary = destination / 'migration.zip.tmp'
        with temporary.open('wb') as handle:
            os.chmod(temporary, 0o600)
            with zipfile.ZipFile(handle, 'w', zipfile.ZIP_STORED, allowZip64=True) as archive:
                for name, path in sorted(payload.items()):
                    archive.write(path, name)
                archive.writestr('manifest.json', json.dumps({'sha256': manifest}))
                archive.writestr('COMPLETE', '')
        os.replace(temporary, bundle)
        self._save(plan, status='prepared', sha256=self.manager.file_hash(bundle),
                   snapshots=snapshots, domains=list(dict.fromkeys(hosts)),
                   snapshot_at=metadata['snapshot_at'])
        return bundle

    def _package(self, directory):
        package = directory / 'wpi.zip'
        with package.open('wb') as handle:
            os.chmod(package, 0o600)
            with zipfile.ZipFile(handle, 'w', zipfile.ZIP_DEFLATED) as archive:
                for source in sorted(Path(__file__).parent.glob('*.py')):
                    if source.is_symlink():
                        raise ValueError('Paket WPI sumber tidak aman.')
                    info = zipfile.ZipInfo('wpi/' + source.name)
                    info.external_attr = (stat.S_IFREG | 0o644) << 16
                    archive.writestr(info, source.read_bytes().replace(b'\r\n', b'\n'))
                for name in ('README.md', 'LICENSE'):
                    source = Path(__file__).parent.parent / name
                    if source.is_file() and not source.is_symlink():
                        archive.write(source, name)
        return package

    def _install_remote_package(self, session, remote, package_hash):
        # Validate/extract only our hashed regular-file application package.
        # The payload is transferred from this installation, not a moving URL.
        script = f'''set -eu
export DEBIAN_FRONTEND=noninteractive
if ! command -v python3 >/dev/null; then apt-get update; apt-get install -y --no-install-recommends python3; fi
python3 - {_quote(remote + '/wpi.zip')} {_quote(package_hash)} {_quote(__version__)} <<'PY'
import hashlib, os, pathlib, py_compile, re, stat, sys, tempfile, zipfile
source, expected, version = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if source.is_symlink() or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    sys.exit('Checksum aplikasi migrasi tidak cocok.')
allowed = re.compile(r'wpi/[a-z_][a-z0-9_]*\\.py|README\\.md|LICENSE')
releases = pathlib.Path('/usr/local/lib/wpi-releases')
releases.mkdir(parents=True, exist_ok=True)
stage = pathlib.Path(tempfile.mkdtemp(prefix='v' + version + '-migration-', dir=releases))
stage.chmod(0o755)
with zipfile.ZipFile(source) as archive:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) != len(set(names)) or len(names) > 100 or sum(e.file_size for e in entries) > 8 * 1024 * 1024:
        sys.exit('Paket aplikasi migrasi tidak valid.')
    if not all('wpi/' + n in names for n in ('__init__.py', 'cli.py', 'core.py', 'migrate.py', 'migrate_target.py', 'ssh.py')):
        sys.exit('Paket aplikasi migrasi belum lengkap.')
    for entry in entries:
        mode = entry.external_attr >> 16
        if (not allowed.fullmatch(entry.filename) or entry.is_dir() or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) and not stat.S_ISREG(mode))):
            sys.exit('Path paket aplikasi tidak aman.')
        target = stage / entry.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(entry))
        target.chmod(0o644)
for path in (stage / 'wpi').glob('*.py'):
    py_compile.compile(str(path), doraise=True)
(stage / 'VERSION').write_text('v' + version + '\\n')
link = releases / ('.migration-link-' + stage.name)
link.symlink_to(stage)
os.replace(link, '/usr/local/lib/wpi')
launcher = pathlib.Path('/usr/local/bin/wpi')
launcher.parent.mkdir(parents=True, exist_ok=True)
content = '#!/usr/bin/env bash\\n# WPI managed launcher\\nset -euo pipefail\\ncd /usr/local/lib/wpi\\nexec env -u PYTHONPATH -u PYTHONHOME /usr/bin/python3 -m wpi.cli "$@"\\n'
fd, name = tempfile.mkstemp(prefix='.wpi-migration-', dir=launcher.parent)
with os.fdopen(fd, 'w') as output:
    output.write(content)
os.chmod(name, 0o755)
os.replace(name, launcher)
print('WPI_MIGRATION_PACKAGE_OK')
PY
'''
        result = session.run_root(script)
        if 'WPI_MIGRATION_PACKAGE_OK' not in result.stdout.splitlines():
            raise RuntimeError('Paket WPI pada server baru belum siap.')

    def migrate(self, host, username, password, port=22):
        if not self.manager.config.get('setup_complete', True) or not self.manager.sites():
            raise ValueError('Siapkan situs WordPress WPI sebelum migrasi server.')
        # SSHSession validates the destination and never persists the password.
        session = self.session_factory(host, username, password, port=port, data_dir=self.manager.data)
        host, username, port = session.host, session.username, session.port
        plan = self._plan(host, username, port)
        remote = '/tmp/wpi-migrate-' + plan['migration_id']
        bundle = self.manager.backups / 'migrations' / plan['migration_id'] / 'migration.zip'
        self._save(plan, remote_path=remote)
        try:
            with session:
                print('Memeriksa akses SSH dan Ubuntu pada server baru...')
                self._preflight(session, plan)
                if not bundle.is_file() or self.manager.file_hash(bundle) != plan.get('sha256'):
                    print('Membuat snapshot situs dan database...')
                    bundle = self._export(plan)
                package = self._package(bundle.parent)
                # Only this validated, unique migration directory is touched.
                session.run_root(f'set -eu\n[ ! -L {_quote(remote)} ]\n'
                                 f'mkdir -p -- {_quote(remote)}\nchmod 700 -- {_quote(remote)}\n'
                                 f'rm -f -- {_quote(remote + "/migration.zip")} {_quote(remote + "/wpi.zip")}\n'
                                 f'chown -- {_quote(username)} {_quote(remote)}\n')
                self._save(plan, status='transferring')
                print('Mengirim file dan database melalui SSH terenkripsi...')
                session.upload(bundle, remote + '/migration.zip')
                session.upload(package, remote + '/wpi.zip')
                session.run_root(f'chown -R root:root -- {_quote(remote)}\nchmod 700 -- {_quote(remote)}')
                self._install_remote_package(session, remote, self.manager.file_hash(package))
                self._save(plan, status='importing')
                print('Menyiapkan web server, database, PHP-FPM, dan auto-SSL di server baru...')
                result = session.run_root('exec /usr/local/bin/wpi migration-import '
                                          + _quote(remote + '/migration.zip') + ' --sha256 '
                                          + _quote(plan['sha256']) + ' --migration-id '
                                          + _quote(plan['migration_id']))
                try:
                    report = json.loads(result.stdout.strip().splitlines()[-1])
                except (IndexError, ValueError):
                    raise RuntimeError('Hasil verifikasi server tujuan belum tersedia.') from None
                if not isinstance(report, dict) or report.get('status') != 'ready':
                    raise RuntimeError('Impor server tujuan belum selesai; snapshot sumber tetap tersedia.')
                self._save(plan, status='ready', completed_at=dt.datetime.now(dt.timezone.utc).isoformat())
                # Delete only the transfer directory. Import backups/journal remain root-only.
                try:
                    session.run_root(f'rm -rf -- {_quote(remote)}')
                except RuntimeError:
                    self._save(plan, transfer_cleanup_pending=True)
                return {**report, 'migration_id': plan['migration_id'], 'host': host,
                        'backup': str(bundle.parent), 'domains': plan['domains'],
                        'snapshot_at': plan['snapshot_at']}
        except Exception as error:
            self._save(plan, status='failed', last_stage=plan['status'])
            # Never repeat remote stdout/stderr/argv or passwords in a failure.
            raise RuntimeError('Migrasi belum selesai. Situs sumber tetap tersedia. '
                               f'ID: {plan["migration_id"]}. Ulangi migrasi ke server yang sama '
                               'untuk melanjutkan snapshot; periksa akses SSH/sudo dan layanan tujuan.') from error

    def status(self):
        return [json.loads(path.read_text()) for path in sorted(
            (self.manager.data / 'migrations-out').glob('*.json'))]
