"""Limit parsing, shared-pool budgeting, persistence and activation rollback."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi import autotune, core
from wpi.php_settings import PHPSettings, configured_profile, parse_mebibytes
from wpi.web import HEADER, WebStack


def resources(memory=32, cpus=16):
    return {'memory_total': memory * autotune.GIB, 'memory_available': memory * autotune.GIB * 7 // 8,
            'cpus': cpus, 'host_cpus': cpus, 'worker_rss': [64 * autotune.MIB],
            'telemetry_ok': True}


class LimitPolicyTests(unittest.TestCase):
    def test_cli_bare_numbers_are_mib_and_unlimited_is_rejected(self):
        for value, expected in ((500, 500), ('500', 500), ('500M', 500),
                                ('500 MiB', 500), ('1G', 1024), ('auto', None)):
            self.assertEqual(parse_mebibytes(value), expected)
        for value in (0, -1, '0', '-1', 'unlimited', True, 1.5, 'nan', '500M;evil', '1T'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_mebibytes(value)

    def test_post_headroom_and_memory_upload_relationship(self):
        sample = resources()
        limits = configured_profile(autotune.profile(sample),
                                    {'memory_limit_mb': 500, 'upload_max_filesize_mb': 256}, sample,
                                    check_capacity=True)
        self.assertEqual(limits['memory_mib'], 500)
        self.assertEqual(limits['upload_mib'], 256)
        self.assertEqual(limits['post_mib'], 282)
        for settings in ({'memory_limit_mb': 200, 'upload_max_filesize_mb': 256},
                         {'memory_limit_mb': 16384},
                         {'memory_limit_mb': 10000, 'upload_max_filesize_mb': 2047},
                         {'memory_limit_mb': -1}, {'unexpected': 500}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                configured_profile(autotune.profile(sample), settings, sample, check_capacity=True)

    def test_hardware_capacity_instead_of_fixed_memory_ceiling(self):
        for memory in (32, 128):
            sample = resources(memory=memory, cpus=memory // 2)
            limits = configured_profile(autotune.profile(sample), {'memory_limit_mb': 8192}, sample,
                                        check_capacity=True)
            self.assertEqual(limits['memory_mib'], 8192)
        with self.assertRaises(ValueError):
            configured_profile(autotune.profile(resources(memory=2)), {'memory_limit_mb': 8192},
                               resources(memory=2), check_capacity=True)

    def test_manual_peak_memory_reduces_worker_parallelism(self):
        sample = resources()
        before = autotune.capacity(sample)
        after = autotune.capacity({**sample, 'php_settings': {'memory_limit_mb': 500}})
        self.assertLess(after['capacity'], before['capacity'])
        self.assertGreaterEqual(after['worker_bytes'], 532 * autotune.MIB)
        self.assertLessEqual(after['capacity'] * after['worker_bytes'], after['memory_budget'])

    def test_pool_enforces_only_explicit_overrides_and_keeps_worker_controller(self):
        sample = {**resources(), 'php_settings': {'memory_limit_mb': 500, 'upload_max_filesize_mb': 256}}
        text = autotune.render_pool(24, '/run/private/status.sock', sample)
        for fragment in ('pm.max_children = 24', 'php_admin_value[memory_limit] = 500M',
                         'php_admin_value[upload_max_filesize] = 256M',
                         'php_admin_value[post_max_size] = 282M'):
            self.assertIn(fragment, text)
        self.assertNotIn('php_admin_value', autotune.render_pool(24, '/run/private/status.sock', resources()))


class FakeManager:
    def __init__(self, data, sites, runner):
        self.data, self._sites, self.runner = data, sites, runner
        self.calls = []

    @property
    def config(self):
        return json.loads((self.data / 'config.json').read_text())

    def sites(self):
        return self._sites

    def wp(self, site, *args, **kwargs):
        self.calls.append((site['id'], args))
        if args[:2] == ('config', 'set'):
            path = Path(site['root']) / 'wp-config.php'
            path.write_text(path.read_text() + f"\ndefine('{args[2]}', '{args[3]}');\n")
        return subprocess.CompletedProcess([], 0, stdout='', stderr='')


class SettingsActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.data = self.base / 'data'
        self.data.mkdir()
        self.etc = self.base / 'etc'
        self.config = {'stack': 'nginx', 'database': 'mariadb', 'php_version': '8.3',
                       'autotune_enabled': True}
        (self.data / 'config.json').write_text(json.dumps(self.config))
        self.sites = []
        for index in (1, 2):
            root = self.base / 'www' / f'{index:012x}' / 'public'
            root.mkdir(parents=True)
            (root / 'wp-config.php').write_text("<?php\n// old config\n")
            site = {'id': f'{index:012x}', 'primary': f'site{index}.example.com',
                    'root': str(root), 'aliases': [], 'secondary': [], 'tls': []}
            self.sites.append(site)
        self.calls = []

        def runner(argv, **kwargs):
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

        self.manager = FakeManager(self.data, self.sites, runner)
        self.settings = PHPSettings(self.manager, etc_root=self.etc, proc_root=self.base / 'proc',
                                    sys_root=self.base / 'sys', run_root=self.base / 'run')
        tuner = self.settings._tuner()
        for path, value in ((tuner.pool, 'original pool'), (tuner.ini, 'original ini'),
                            (tuner.cli_ini, 'original cli ini')):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        tuner._save({'children': 100, 'profile': autotune.profile(resources()), 'last_change': 0})
        self.originals = {path: path.read_bytes() for path in (
            self.data / 'config.json', tuner.pool, tuner.ini, tuner.cli_ini, tuner.state_file,
            *[Path(site['root']) / 'wp-config.php' for site in self.sites])}
        # The isolated unit filesystem has temporary roots; rendering path
        # containment itself is covered by test_web's real boundary tests.
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, 'WWW', self.base / 'www').start()
        probe = self.base / 'symlink-probe'
        try:
            probe.symlink_to(self.base / 'absent-target')
            probe.unlink()
        except OSError:
            # Match the existing web transaction harness when Windows lacks
            # symlink privileges. Linux CI uses actual filesystem links.
            links = {}
            is_symlink, unlink = Path.is_symlink, Path.unlink
            readlink, lexists, samefile = os.readlink, os.path.lexists, os.path.samefile

            def unlink_virtual(path, *args, **kwargs):
                if str(path) in links:
                    del links[str(path)]
                else:
                    return unlink(path, *args, **kwargs)

            mock.patch.object(Path, 'symlink_to', lambda path, target, **kw: links.__setitem__(str(path), str(target))).start()
            mock.patch.object(Path, 'is_symlink', lambda path: str(path) in links or is_symlink(path)).start()
            mock.patch.object(Path, 'unlink', unlink_virtual).start()
            mock.patch('os.readlink', lambda path, **kw: links[str(path)] if str(path) in links else readlink(path, **kw)).start()
            mock.patch('os.path.lexists', lambda path: str(path) in links or lexists(path)).start()
            mock.patch('os.path.samefile', lambda first, second: samefile(links.get(str(first), first), links.get(str(second), second))).start()
        mock.patch('wpi.web._path', side_effect=lambda value, roots: value).start()
        mock.patch('wpi.autotune.detect_resources', return_value=resources()).start()
        self.repair = mock.patch('wpi.repair.SiteRepair').start().return_value

    def test_server_wide_success_updates_both_inis_wordpress_and_every_vhost(self):
        report = self.settings.apply(memory_limit='500', upload_max_filesize='256M')
        self.assertEqual(report['scope'], 'server')
        self.assertEqual(report['manual'], {'memory_limit_mb': 500, 'upload_max_filesize_mb': 256})
        self.assertEqual(report['effective']['post_mib'], 282)
        self.assertEqual(len(self.manager.calls), 4)
        for site_id, command in self.manager.calls:
            self.assertEqual(command[3], '500M')
            self.assertNotIn('--raw', command)
        tuner = self.settings._tuner()
        for path in (tuner.ini, tuner.cli_ini):
            text = path.read_text()
            self.assertIn('memory_limit = 500M', text)
            self.assertIn('upload_max_filesize = 256M', text)
            self.assertIn('post_max_size = 282M', text)
        for site in self.sites:
            vhost = self.etc / 'nginx/sites-available' / f'wpi-{site["id"]}.conf'
            self.assertIn('client_max_body_size 282m', vhost.read_text())
            self.assertTrue((self.etc / 'nginx/sites-enabled' / vhost.name).is_symlink())
        self.assertEqual(tuner._state()['children'], report['capacity'])
        self.assertTrue(tuner._state()['reload_pending'])
        self.assertTrue(Path(report['backup'], 'paths.json').is_file())
        self.assertEqual(self.repair.remember_config.call_count, 4)

    def test_validation_and_bad_config_abort_before_mutation(self):
        with self.assertRaises(ValueError):
            self.settings.apply(memory_limit=200, upload_max_filesize=256)
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.calls, [])
        self.manager.runner = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout='', stderr='secret')
        with self.assertRaisesRegex(ValueError, 'auto repair'):
            self.settings.apply(memory_limit=500)
        self.assertEqual(self.manager.calls, [])
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_site_root_link_to_other_managed_site_is_rejected_before_wp_mutation(self):
        first = Path(self.sites[0]['root'])
        (first / 'wp-config.php').unlink()
        first.rmdir()
        first.symlink_to(Path(self.sites[1]['root']), target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.settings.apply(memory_limit=500)
        self.assertEqual(self.manager.calls, [])
        self.assertEqual(self.calls, [])
        self.assertFalse((self.data / 'php-settings-backups').exists())

    def test_mismatched_site_root_and_linked_configuration_parent_are_rejected(self):
        original = self.sites[0]['root']
        self.sites[0]['root'] = self.sites[1]['root']
        with self.assertRaisesRegex(ValueError, 'Document root'):
            self.settings.apply(memory_limit=500)
        self.assertEqual(self.manager.calls, [])
        self.sites[0]['root'] = original
        parent = self.etc / 'nginx/sites-available'
        destination = self.base / 'outside-configs'
        destination.mkdir()
        parent.parent.mkdir(parents=True)
        parent.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.settings.apply(memory_limit=500)
        self.assertEqual(self.manager.calls, [])

    def test_web_validation_failure_rolls_back_php_wp_config_json_and_pool_state(self):
        failed = False
        original_runner = self.manager.runner

        def runner(argv, **kwargs):
            nonlocal failed
            result = original_runner(argv, **kwargs)
            if argv == ['nginx', '-t'] and not failed:
                failed = True
                raise RuntimeError('bad web config')
            return result

        self.manager.runner = runner
        with self.assertRaisesRegex(RuntimeError, 'lama dipulihkan'):
            self.settings.apply(memory_limit=500, upload_max_filesize=256)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original, path)
        for site in self.sites:
            self.assertFalse((self.etc / 'nginx/sites-available' / f'wpi-{site["id"]}.conf').exists())
            self.assertFalse((self.etc / 'nginx/sites-enabled' / f'wpi-{site["id"]}.conf').is_symlink())
        self.assertTrue(any((self.data / 'php-settings-backups').iterdir()))

    def test_fpm_reload_failure_restores_both_inis_and_wp_constants(self):
        failed = False
        original_runner = self.manager.runner

        def runner(argv, **kwargs):
            nonlocal failed
            result = original_runner(argv, **kwargs)
            if argv == ['systemctl', 'reload', 'php8.3-fpm'] and not failed:
                failed = True
                raise RuntimeError('reload failure')
            return result

        self.manager.runner = runner
        with self.assertRaisesRegex(RuntimeError, 'lama dipulihkan'):
            self.settings.apply(memory_limit=500)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original, path)
        self.assertGreaterEqual(self.calls.count(['systemctl', 'reload', 'php8.3-fpm']), 2)

    def test_config_save_failure_restores_previous_active_vhosts_and_links(self):
        available = self.etc / 'nginx/sites-available'
        enabled = self.etc / 'nginx/sites-enabled'
        available.mkdir(parents=True)
        enabled.mkdir(parents=True)
        for site in self.sites:
            name = f'wpi-{site["id"]}.conf'
            target = available / name
            target.write_bytes((HEADER + '// old vhost\n').encode())
            (enabled / name).symlink_to(target)
        original_write = autotune._atomic_write
        failed = False

        def write(path, *args, **kwargs):
            nonlocal failed
            if Path(path) == self.data / 'config.json' and not failed:
                failed = True
                raise OSError('disk full')
            return original_write(path, *args, **kwargs)

        with mock.patch('wpi.autotune._atomic_write', side_effect=write), \
             self.assertRaisesRegex(RuntimeError, 'lama dipulihkan'):
            self.settings.apply(memory_limit=500)
        for path, original in self.originals.items():
            self.assertEqual(path.read_bytes(), original, path)
        for site in self.sites:
            name = f'wpi-{site["id"]}.conf'
            self.assertEqual((available / name).read_text(), HEADER + '// old vhost\n')
            self.assertTrue((enabled / name).is_symlink())

    def test_bare_byte_wordpress_runtime_error_does_not_prevent_safe_config_fix(self):
        self.repair.remember_config.side_effect = [ValueError('runtime config failure'),
                                                   ValueError('runtime config failure'), None, None]
        report = self.settings.apply(memory_limit=500)
        self.assertEqual(report['effective']['memory_mib'], 500)
        self.assertTrue(Path(report['backup'], 'paths.json').exists())

    def test_reset_restores_auto_and_settings_survive_reloaded_controller(self):
        self.settings.apply(memory_limit=500, upload_max_filesize=256)
        tuner = self.settings._tuner()
        detected = tuner._resources()
        self.assertEqual(detected['php_settings']['memory_limit_mb'], 500)
        self.assertEqual(autotune.profile(detected)['memory_mib'], 500)
        # A new hardware read changes the automatic profile without changing
        # the explicit limits or undoing them on an upgrade/enable operation.
        with mock.patch('wpi.autotune.detect_resources', return_value=resources(128, 64)):
            self.assertEqual(autotune.profile(tuner._resources())['upload_mib'], 256)
        report = self.settings.reset()
        self.assertEqual(report['manual'], {})
        self.assertEqual(report['effective'], autotune.profile(resources()))
        self.assertNotIn('php_admin_value', tuner.pool.read_text())


if __name__ == '__main__':
    unittest.main()
