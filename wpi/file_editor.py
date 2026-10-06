"""Per-site WordPress plugin/theme file editor policy and safe config changes."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile

from . import core
from .autotune import _atomic_write


MAX_CONFIG = 1024 * 1024
EDIT = 'DISALLOW_FILE_EDIT'
MODS = 'DISALLOW_FILE_MODS'


def _no_links(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Menolak symlink pada konfigurasi editor file WPI.')
    return path


def _snapshot(path):
    path = _no_links(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CONFIG:
        raise ValueError('Berkas konfigurasi editor file WPI tidak valid.')
    return path.read_bytes(), stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid


def _php_truthy(value):
    # PHP treats the strings 'false' and 'true' as truthy, but '0' as false.
    if isinstance(value, str):
        return value not in ('', '0')
    return bool(value)


class FileEditor:
    def __init__(self, manager):
        self.manager, self.runner = manager, manager.runner

    def _site(self, identifier, *, require_config=True):
        _no_links(self.manager.data)
        directory = _no_links(self.manager.data / 'sites')
        for path in directory.glob('*.json'):
            _snapshot(path)
        site = self.manager.site(identifier)
        if not re.fullmatch(r'[a-f0-9]{12}', str(site.get('id', ''))):
            raise ValueError('ID situs editor file tidak valid.')
        root = _no_links(site['root'])
        if root != Path(core.WWW) / site['id'] / 'public' or not root.is_dir():
            raise ValueError('Editor file hanya untuk document root yang dikelola WPI.')
        config = _no_links(root / 'wp-config.php')
        if require_config or config.exists():
            _snapshot(config)
        self._managed(site)
        return site

    @staticmethod
    def _managed(site):
        if 'file_editor_enabled' not in site:
            return None
        value = site['file_editor_enabled']
        if type(value) is not bool:
            raise ValueError('Pengaturan editor file tersimpan harus boolean.')
        return value

    def _lint(self, path):
        _snapshot(path)
        version = self.manager.config.get('php_version')
        if version not in ('8.1', '8.3'):
            raise ValueError('Versi PHP WPI tidak valid.')
        result = self.runner([f'/usr/bin/php{version}', '-n', '-l', str(path)], check=False)
        if result.returncode:
            raise ValueError('wp-config.php tidak valid. Jalankan repair sebelum mengatur editor file.')

    def _constants(self, site, path=None):
        args = ['config', 'list', '--format=json']
        if path is not None:
            args.append(f'--config-file={path}')
        result = self.manager.wp(site, *args, check=False)
        if result.returncode:
            raise ValueError('Konfigurasi editor file WordPress belum dapat dibaca.')
        try:
            rows = json.loads(result.stdout)
            if not isinstance(rows, list):
                raise ValueError
            values = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError
                name = row.get('name', row.get('key'))
                if row.get('type') != 'constant' or name not in (EDIT, MODS):
                    continue
                if name in values or 'value' not in row:
                    raise ValueError
                values[name] = row['value']
            return values
        except (TypeError, ValueError):
            raise ValueError('Format konfigurasi editor file WordPress tidak valid.') from None

    def _report(self, site, values):
        configured = not _php_truthy(values.get(EDIT))
        blocked = _php_truthy(values.get(MODS))
        return {'site_id': site['id'], 'primary': core.domain(site['primary']),
                'enabled': configured and not blocked, 'configured_enabled': configured,
                'managed_enabled': self._managed(site), 'blocked_by_file_mods': blocked}

    def status(self, identifier):
        site = self._site(identifier)
        self._lint(Path(site['root']) / 'wp-config.php')
        return self._report(site, self._constants(site))

    def _set(self, site, path, enabled):
        self.manager.wp(site, 'config', 'set', EDIT, 'false' if enabled else 'true',
                        '--raw', '--type=constant', f'--config-file={path}')
        self._lint(path)

    def _candidate(self, site, path):
        path = _no_links(path)
        root = Path(site['root'])
        if path.name != 'wp-config.php' or (path != root / 'wp-config.php' and
                                           root.parent not in path.parents):
            raise ValueError('Kandidat editor file berada di luar direktori situs WPI.')
        _snapshot(path)
        return path

    @staticmethod
    def _verify(values, after, enabled):
        if after.get(EDIT) is not (not enabled) or \
                (type(after.get(MODS)), after.get(MODS)) != (type(values.get(MODS)), values.get(MODS)):
            raise RuntimeError('Konfigurasi editor file tidak sesuai; perubahan dibatalkan.')

    def _stage(self, site, contents, enabled, values):
        with tempfile.TemporaryDirectory(prefix='.wpi-file-editor-', dir=Path(site['root']).parent) as folder:
            candidate = Path(folder) / 'wp-config.php'
            candidate.write_bytes(contents)
            os.chmod(candidate, 0o640)
            os.chmod(folder, 0o750)
            self.runner(['chown', 'root:www-data', folder])
            self.runner(['chown', 'www-data:www-data', str(candidate)])
            self._set(site, candidate, enabled)
            after = self._constants(site, candidate)
            self._verify(values, after, enabled)
            return candidate.read_bytes(), after

    def overlay_config(self, site, candidate=None):
        """Replay only an explicit policy; never alter DISALLOW_FILE_MODS."""
        site = self._site(site['id'], require_config=False)
        enabled = self._managed(site)
        if enabled is None:
            return False
        path = self._candidate(site, candidate or (Path(site['root']) / 'wp-config.php'))
        self._lint(path)
        values = self._constants(site, path)
        if type(values.get(EDIT)) is bool and values[EDIT] is (not enabled):
            return False
        saved = _snapshot(path)
        changed = False
        try:
            if path == Path(site['root']) / 'wp-config.php':
                contents, _ = self._stage(site, saved[0], enabled, values)
                if _snapshot(path)[0] != saved[0]:
                    raise RuntimeError('wp-config.php berubah selama operasi. Coba lagi setelah selesai.')
                changed = True
                _atomic_write(_no_links(path), contents, mode=0o640)
                self.runner(['chown', 'www-data:www-data', str(path)])
            else:
                changed = True
                self._set(site, path, enabled)
                self._verify(values, self._constants(site, path), enabled)
        except BaseException:
            if changed:
                self._restore(path, saved)
            raise
        return True

    @staticmethod
    def _restore(path, saved):
        contents, mode, uid, gid = saved
        path = _no_links(path)
        _atomic_write(path, contents, mode=mode)
        if os.name != 'nt':
            os.chown(path, uid, gid)

    def _backup(self, site, config, metadata):
        base = self.manager.data / 'file-editor-backups'
        folder = base / site['id'] / (dt.datetime.now(dt.timezone.utc).strftime(
            '%Y%m%dT%H%M%S') + '-' + secrets.token_hex(8))
        for path in (base, base / site['id'], folder):
            _no_links(path).mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        for name, contents in (('wp-config.php', config[0]), ('site.json', metadata[0])):
            with (folder / name).open('xb') as output:
                os.chmod(folder / name, 0o600)
                output.write(contents)
                output.flush()
                os.fsync(output.fileno())
        return folder

    def apply(self, identifier, enabled):
        if type(enabled) is not bool:
            raise ValueError('Pilihan editor file harus boolean.')
        site = self._site(identifier)
        if site.get('status', 'active') != 'active':
            raise ValueError('Selesaikan instalasi atau migrasi situs sebelum mengatur editor file.')
        path = Path(site['root']) / 'wp-config.php'
        self._lint(path)
        values = self._constants(site)
        if enabled and _php_truthy(values.get(MODS)):
            raise ValueError('Editor file diblokir oleh DISALLOW_FILE_MODS. '
                             'Pengaturan tersebut tidak diubah oleh WPI.')
        config_changed = values.get(EDIT) is not (not enabled)
        if not config_changed and self._managed(site) is enabled:
            return {**self._report(site, values), 'changed': False}
        metadata_path = self.manager.data / 'sites' / f'{site["id"]}.json'
        config, metadata = _snapshot(path), _snapshot(metadata_path)
        backup = self._backup(site, config, metadata)
        config_applied = metadata_attempted = False
        try:
            if config_changed:
                contents, values_after = self._stage(site, config[0], enabled, values)
                if _snapshot(path)[0] != config[0]:
                    raise RuntimeError('wp-config.php berubah selama operasi. Coba lagi setelah selesai.')
                config_applied = True
                _atomic_write(_no_links(path), contents, mode=0o640)
                self.runner(['chown', 'www-data:www-data', str(path)])
                values = values_after
            changed_site = copy.deepcopy(site)
            changed_site['file_editor_enabled'] = enabled
            metadata_attempted = True
            self.manager.save_site(changed_site)
        except BaseException:
            if config_applied:
                self._restore(path, config)
            if metadata_attempted:
                self._restore(metadata_path, metadata)
            raise
        try:
            self.manager.remember_config(site['id'])
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError):
            pass
        return {**self._report(changed_site, values), 'changed': True, 'backup': str(backup)}
