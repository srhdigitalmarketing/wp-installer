"""Server-wide PHP limits with persistent overrides and recoverable activation.

WPI uses Ubuntu's shared www pool. A setting applies to every managed website
and phpMyAdmin, and is never presented as an isolated per-site memory budget.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat

from . import autotune


FIELDS = ('memory_limit_mb', 'upload_max_filesize_mb')


def parse_mebibytes(value):
    """CLI numbers mean MiB; PHP itself treats a bare number as bytes."""
    if value is None or isinstance(value, str) and value.strip().lower() == 'auto':
        return None
    if type(value) is int:
        result = value
    elif isinstance(value, str):
        match = re.fullmatch(r'([0-9]{1,12})\s*(m(?:b|ib)?|g(?:b|ib)?)?', value.strip(), re.I)
        if not match:
            raise ValueError('Gunakan MiB positif, misalnya 500, 500M, 1G, atau auto.')
        result = int(match[1]) * (1024 if (match[2] or '').lower().startswith('g') else 1)
    else:
        raise ValueError('Batas PHP harus bilangan MiB positif atau auto.')
    if result <= 0:
        raise ValueError('Batas PHP harus positif; 0 dan unlimited/-1 tidak didukung.')
    return result


def _settings(settings):
    if not isinstance(settings, dict) or set(settings) - set(FIELDS):
        raise ValueError('Struktur pengaturan PHP tidak valid.')
    if any(type(value) is not int or value <= 0 for value in settings.values()):
        raise ValueError('Pengaturan PHP tersimpan harus MiB positif.')
    return settings


def _no_symlink_ancestors(path):
    path = Path(path)
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('Menolak symlink pada path pengaturan PHP/WordPress.')
    return path


def configured_profile(default, settings, resources, *, check_capacity=False):
    settings = _settings(settings)
    result = dict(default)
    if 'memory_limit_mb' in settings:
        result['memory_mib'] = settings['memory_limit_mb']
    if 'upload_max_filesize_mb' in settings:
        upload = settings['upload_max_filesize_mb']
        result['upload_mib'] = upload
        result['post_mib'] = upload + max(8, math.ceil(upload * 0.10))
    if result['post_mib'] >= result['memory_mib']:
        raise ValueError('PHP memory limit harus lebih besar daripada post max size '
                         '(upload ditambah ruang untuk multipart). Naikkan memory atau turunkan upload.')
    # Apache's LimitRequestBody accepts at most 2 GiB - 1. Keep one profile
    # compatible with either supported web stack; zero must never mean unlimited.
    if not 1 <= result['post_mib'] <= 2047:
        raise ValueError('Upload terlalu besar; post max size maksimal 2047 MiB.')
    if check_capacity:
        total = resources.get('memory_total', 0)
        if total <= 0 or not resources.get('telemetry_ok'):
            raise RuntimeError('Kapasitas RAM belum dapat dibaca; pengaturan PHP tidak diubah.')
        reserve = max(384 * autotune.MIB, int(total * 0.35))
        opcache = result['opcache_mib'] * autotune.MIB
        budget = max(0, min(int(total * 0.50), total - reserve - opcache))
        maximum = max(0, budget // autotune.MIB - 32)
        if 'memory_limit_mb' in settings and result['memory_mib'] > maximum:
            raise ValueError(f'PHP memory limit melebihi anggaran RAM server. '
                             f'Maksimal saat ini {maximum} MiB setelah cadangan OS/database/OPcache.')
    return result


class PHPSettings:
    def __init__(self, manager, *, etc_root='/etc', proc_root='/proc', sys_root='/sys', run_root='/run'):
        self.manager = manager
        self.data = Path(manager.data)
        self.etc = Path(etc_root)
        self.proc, self.sys, self.runtime = Path(proc_root), Path(sys_root), Path(run_root)

    def _tuner(self):
        cfg = self.manager.config
        if not cfg or not cfg.get('php_version'):
            raise ValueError('Jalankan setup WordPress terlebih dahulu.')
        return autotune.AutoTuner(cfg['php_version'], self.manager.runner, data_dir=self.data,
                                 etc_root=self.etc, proc_root=self.proc, sys_root=self.sys,
                                 run_root=self.runtime)

    def _resources(self):
        # Read physical/cgroup capacity without carrying the existing override
        # into prospective profile validation.
        return {key: value for key, value in self._tuner()._resources().items() if key != 'php_settings'}

    def validate(self, settings, resources=None):
        resources = resources if resources is not None else self._resources()
        clean = {key: value for key, value in resources.items() if key != 'php_settings'}
        return configured_profile(autotune.profile(clean), settings, clean, check_capacity=True)

    def effective(self, resources=None):
        resources = resources if resources is not None else self._resources()
        clean = {key: value for key, value in resources.items() if key != 'php_settings'}
        return configured_profile(autotune.profile(clean), self.manager.config.get('php_settings', {}), clean)

    def web_body_mib(self, effective=None, settings=None):
        """Automatic uploads retain room for hardware profile changes.

        PHP enforces the automatic upload/POST size. Its adaptive controller
        can raise those values after a VPS resize without changing vhosts or
        taking the WPI operation lock. Explicit uploads pin the web limit to
        the matching POST size instead.
        """
        settings = _settings(self.manager.config.get('php_settings', {}) if settings is None else settings)
        if 'upload_max_filesize_mb' not in settings:
            return 320
        effective = self.effective() if effective is None else effective
        return effective['post_mib']

    def status(self):
        resources = self._resources()
        effective = self.effective(resources)
        manual = _settings(self.manager.config.get('php_settings', {}))
        bounded = {**resources, 'php_settings': manual}
        return {'scope': 'server', 'manual': manual, 'effective': effective,
                'web_body_mib': self.web_body_mib(effective, manual),
                'memory_total_mib': resources.get('memory_total', 0) // autotune.MIB,
                'capacity': autotune.capacity(bounded)['capacity'],
                'sites': [site['primary'] for site in self.manager.sites()]}

    @staticmethod
    def _capture(path):
        path = _no_symlink_ancestors(path)
        if not path.exists():
            return None
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError('Berkas pengaturan bukan file biasa.')
        return (path.read_bytes(), stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)

    @staticmethod
    def _restore(path, previous):
        if previous is None:
            Path(path).unlink(missing_ok=True)
            return
        contents, mode, uid, gid = previous
        if Path(path).is_file() and not Path(path).is_symlink() and Path(path).read_bytes() == contents:
            os.chmod(path, mode)
            if hasattr(os, 'chown'):
                os.chown(path, uid, gid)
            return
        autotune._atomic_write(path, contents, mode)
        if hasattr(os, 'chown'):
            os.chown(path, uid, gid)

    def _lint(self, site):
        from . import core
        cfg = self.manager.config
        identifier = str(site.get('id', ''))
        expected = Path(core.WWW) / identifier / 'public'
        if not re.fullmatch(r'[a-f0-9]{12}', identifier) or Path(site['root']) != expected:
            raise ValueError('Document root pengaturan PHP bukan milik situs WPI.')
        _no_symlink_ancestors(expected)
        path = Path(site['root']) / 'wp-config.php'
        if path.is_symlink() or not path.is_file():
            raise ValueError('wp-config.php harus file biasa. Jalankan auto repair terlebih dahulu.')
        result = self.manager.runner([f'/usr/bin/php{cfg["php_version"]}', '-n', '-l', str(path)], check=False)
        if result.returncode:
            raise ValueError(f'wp-config.php {site["primary"]} tidak valid. Jalankan auto repair terlebih dahulu.')

    def apply(self, memory_limit=None, upload_max_filesize=None):
        """Preserve omitted values; 'auto' clears an explicit field."""
        cfg = copy.deepcopy(self.manager.config)
        tuner = self._tuner()
        resources = self._resources()
        settings = dict(_settings(cfg.get('php_settings', {})))
        for key, value in (('memory_limit_mb', memory_limit),
                           ('upload_max_filesize_mb', upload_max_filesize)):
            if value is None:
                continue
            parsed = parse_mebibytes(value)
            if parsed is None:
                settings.pop(key, None)
            else:
                settings[key] = parsed
        effective = self.validate(settings, resources)
        sites = self.manager.sites()
        for site in sites:
            self._lint(site)
        # The caller holds the WPI operation lock. Also exclude the 15-second
        # adaptive controller while changing its configuration and state.
        with tuner._lock(blocking=True) as acquired:
            if not acquired:
                raise RuntimeError('Autotune sedang berjalan; pengaturan PHP belum diubah.')
            from .web import WebStack
            web = WebStack(self.manager.runner, cfg['stack'], cfg['php_version'],
                           post_max_size_mb=self.web_body_mib(effective, settings))
            service = 'nginx' if cfg['stack'] == 'nginx' else 'apache2'
            web.available = self.etc / service / 'sites-available'
            web.enabled = self.etc / service / 'sites-enabled'
            web.live = self.etc / 'letsencrypt/live'
            web.migration_tls = self.etc / 'wpi/migration-tls'
            changes = web.body_limit_contents(sites, cfg.get('phpmyadmin'))
            paths = [self.data / 'config.json', tuner.pool, tuner.ini, tuner.cli_ini,
                     tuner.state_file, *[Path(site['root']) / 'wp-config.php' for site in sites],
                     *[web.available / name for name in changes]]
            previous = {path: self._capture(path) for path in paths}
            links = {}
            for name in changes:
                path = web.enabled / name
                _no_symlink_ancestors(path.parent)
                if os.path.lexists(path) and not path.is_symlink():
                    raise ValueError('Sites-enabled berisi file bukan milik WPI.')
                links[path] = os.readlink(path) if path.is_symlink() else None
            backup = self.data / 'php-settings-backups' / secrets.token_hex(16)
            _no_symlink_ancestors(backup)
            backup.mkdir(parents=True, mode=0o700)
            os.chmod(backup, 0o700)
            record = []
            for index, (path, contents) in enumerate(previous.items()):
                record.append({'path': str(path), 'file': str(index) if contents else None})
                if contents:
                    autotune._atomic_write(backup / str(index), contents[0], 0o600)
            autotune._atomic_write(backup / 'paths.json', json.dumps(record, indent=2), 0o600)
            # Existing validated snapshots remain available to auto repair.
            from .repair import SiteRepair
            repair = SiteRepair(self.manager)
            for site in sites:
                try:
                    repair.remember_config(site['id'])
                except (ValueError, RuntimeError):
                    # A syntactically valid config can still fail WordPress's
                    # runtime check (including a bare-byte memory constant).
                    # The private original bytes above remain recoverable.
                    pass
            try:
                value = f"{effective['memory_mib']}M"
                for site in sites:
                    for name in ('WP_MEMORY_LIMIT', 'WP_MAX_MEMORY_LIMIT'):
                        self.manager.wp(site, 'config', 'set', name, value, '--type=constant')
                    self._lint(site)
                prospective = {**resources, 'php_settings': settings}
                state = tuner._state()
                bounds = autotune.capacity(prospective)
                children = min(bounds['capacity'], int(state.get('children', autotune.initial_children(prospective))))
                tuner._apply(children, prospective)
                web.write_body_limits(sites, cfg.get('phpmyadmin'))
                cfg['php_settings'] = settings
                autotune._atomic_write(self.data / 'config.json', json.dumps(cfg, indent=2) + '\n', 0o600)
                now = tuner.clock()
                decision = {**bounds, 'children': children, 'reason': 'settings-changed'}
                state.update({'children': children, 'profile': effective, 'last_change': now,
                              'saturation_ticks': 0, 'idle_ticks': 0, 'pressure_ticks': 0,
                              'reload_pending': True, 'reload_requested_at': now,
                              'reload_from_start': state.get('fpm_start_time'),
                              'reload_from_accepted': state.get('fpm_accepted_conn'),
                              'sample': tuner._sample(prospective, now),
                              'report': tuner._report(prospective, None, decision, now)})
                tuner._save(state)
                for site in sites:
                    repair.remember_config(site['id'])
            except BaseException:
                recovery_failed = False
                for path, contents in previous.items():
                    try:
                        self._restore(path, contents)
                    except Exception:
                        # Attempt every restoration even if a full filesystem
                        # or permission failure prevents restoring one file.
                        recovery_failed = True
                for link, target in links.items():
                    try:
                        if os.path.lexists(link):
                            link.unlink()
                        if target is not None:
                            link.symlink_to(target)
                    except Exception:
                        recovery_failed = True
                try:
                    self.manager.runner([f'/usr/sbin/php-fpm{cfg["php_version"]}', '-t'])
                    self.manager.runner(['systemctl', 'reload', f'php{cfg["php_version"]}-fpm'])
                    if changes:
                        web.validate_reload()
                except Exception:
                    recovery_failed = True
                if recovery_failed:
                    raise RuntimeError('Pengaturan dibatalkan; pemulihan file/reload belum lengkap. '
                                       f'Jalankan auto repair. Backup asli: {backup}.') from None
                raise RuntimeError('Pengaturan PHP dibatalkan; konfigurasi lama dipulihkan. '
                                   f'Backup tersimpan di {backup}.') from None
        return {**self.status(), 'backup': str(backup)}

    def reset(self):
        return self.apply(memory_limit='auto', upload_max_filesize='auto')
