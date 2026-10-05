"""Cache operations remain scoped, serialized, and safe while timers overlap."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from wpi import cli, core


class RedisCommandTests(unittest.TestCase):
    def invoke(self, args, manager, lock=None):
        output = io.StringIO()
        with mock.patch.object(cli.sys, 'platform', 'linux'), \
             mock.patch.object(cli.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(cli, 'Manager', return_value=manager), \
             mock.patch.object(cli, 'operation_lock', lock or mock.Mock(return_value=contextlib.nullcontext())), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            result = cli.main(args)
        return result, output.getvalue()

    def test_readonly_status_works_during_occupied_operation(self):
        manager = mock.Mock()
        manager.redis_status.return_value = {'enabled': True, 'sites': []}
        lock = mock.Mock(side_effect=ValueError('occupied'))
        result, output = self.invoke(['redis-status'], manager, lock)
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output)['enabled'])
        lock.assert_not_called()
        self.assertEqual(manager.mock_calls, [mock.call.redis_status()])

    def test_timer_skips_busy_operation_without_mutating_cache(self):
        manager = mock.Mock()
        result, output = self.invoke(['performance-tick'], manager,
                                     mock.Mock(side_effect=ValueError('occupied')))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output), {'skipped': 'operation-busy'})
        manager.assert_not_called()
        self.assertEqual(manager.mock_calls, [])

    def test_timer_reports_policy_failure_instead_of_busy_skip(self):
        manager = mock.Mock()
        manager.performance_tick.side_effect = ValueError('Invalid resource policy')
        result, output = self.invoke(['performance-tick'], manager)
        self.assertEqual(result, 1)
        self.assertNotIn('operation-busy', output)

    def test_cache_mutations_lock_once_and_dispatch_only_selected_action(self):
        for command, method, argument in (
            ('redis-flush', 'redis_flush', 'example.com'),
            ('redis-disable', 'redis_disable', 'example.com'),
            ('redis-enable', 'enable_redis', 'example.com'),
            ('redis-enable', 'enable_redis', None),
            ('optimize', 'optimize', None),
        ):
            with self.subTest(command=command, argument=argument):
                manager = mock.Mock()
                getattr(manager, method).return_value = {'enabled': True}
                lock = mock.Mock(return_value=contextlib.nullcontext())
                args = [command] + ([argument] if argument else [])
                self.assertEqual(self.invoke(args, manager, lock)[0], 0)
                lock.assert_called_once_with()
                expected = [mock.call.optimize()] if command == 'optimize' else [getattr(mock.call, method)(argument)]
                self.assertEqual(manager.mock_calls, expected)


class RedisManagerHooksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.manager = core.Manager(Path(temp.name) / 'data', runner=mock.Mock())
        core.atomic_json(self.manager.data / 'config.json', {'stack': 'nginx',
            'database': 'mariadb', 'php_version': '8.3', 'setup_complete': True,
            'redis_cache': {'enabled': True}})
        self.site = {'id': 'abcdef123456', 'primary': 'example.com', 'status': 'active',
                     'redis_cache': {'enabled': False}}
        self.manager.save_site(self.site)
        self.cache = mock.Mock()
        patch = mock.patch.object(core.Manager, 'redis', new_callable=mock.PropertyMock,
                                  return_value=self.cache)
        patch.start()
        self.addCleanup(patch.stop)

    def test_optimization_respects_explicit_site_optout(self):
        self.manager.enable_redis()
        self.cache.install.assert_called_once_with()
        self.cache.enable_site.assert_not_called()

    def test_tick_only_rebudgets_and_does_not_reenable_cache(self):
        self.manager.performance_tick()
        self.assertEqual(self.cache.mock_calls, [mock.call.optimize()])

    def test_cache_install_checks_manual_php_budget_before_mutations(self):
        cfg = self.manager.config
        cfg['php_settings'] = {'memory_limit_mb': 450}
        cfg['redis_cache'] = {'enabled': False}
        core.atomic_json(self.manager.data / 'config.json', cfg)
        resources = {'memory_total': 1024 ** 3, 'memory_available': 900 * 1024 ** 2,
                     'cpus': 2.0, 'worker_rss': [], 'telemetry_ok': True}
        with mock.patch('wpi.autotune.detect_resources', return_value=resources):
            with self.assertRaises(ValueError):
                self.manager.enable_redis()
        self.cache.install.assert_not_called()

    def test_cache_install_keeps_current_rss_reservation_during_downsize(self):
        cfg = self.manager.config
        cfg['php_settings'] = {'memory_limit_mb': 500}
        cfg['redis_cache'].update(reserve_mib=950)
        core.atomic_json(self.manager.data / 'config.json', cfg)
        resources = {'memory_total': 2 * 1024 ** 3, 'memory_available': 1500 * 1024 ** 2,
                     'cpus': 2.0, 'worker_rss': [], 'telemetry_ok': True}
        with mock.patch('wpi.autotune.detect_resources', return_value=resources):
            with self.assertRaises(ValueError):
                self.manager.enable_redis()
        self.cache.install.assert_not_called()

    def test_diagnosis_does_not_start_stopped_cache(self):
        self.site['redis_cache']['enabled'] = True
        self.manager.save_site(self.site)
        with mock.patch('wpi.repair.SiteRepair') as repair:
            self.manager.repair_site(self.site['id'], check_only=True)
        self.cache.start_site.assert_not_called()
        repair.return_value.diagnose.assert_called_once_with(self.site['id'])

    def test_explicit_repair_starts_only_selected_managed_cache(self):
        self.site['redis_cache']['enabled'] = True
        self.manager.save_site(self.site)
        with mock.patch('wpi.repair.SiteRepair') as repair:
            self.manager.repair_site(self.site['id'])
        self.cache.start_site.assert_called_once_with(self.site['id'])
        repair.return_value.repair.assert_called_once_with(self.site['id'])


if __name__ == '__main__':
    unittest.main()
