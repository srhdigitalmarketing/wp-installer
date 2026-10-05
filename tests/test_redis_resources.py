"""Redis reservations constrain both PHP policy and adaptive hardware resizing."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from wpi import autotune, core
from wpi.php_settings import PHPSettings, configured_profile

MIB = autotune.MIB
GIB = autotune.GIB


def sample(memory_mib=2048):
    return {'memory_total': memory_mib * MIB, 'memory_available': memory_mib * MIB * 7 // 8,
            'cpus': 16.0, 'host_cpus': 16, 'worker_rss': [64 * MIB],
            'telemetry_ok': True, 'cpu_load': 0.1, 'memory_psi': 0.0}


def cache(limit=102, reserve=214, **extra):
    return {'enabled': True, 'maxmemory_mib': limit, 'memory_reserve_bytes': reserve * MIB,
            'site_count': 1, **extra}


class RedisBudgetPolicyTests(unittest.TestCase):
    def test_actual_cache_profile_matches_budget_contract_across_ram_and_site_counts(self):
        from wpi.redis_cache import redis_resource_profile
        for memory_mib in (1024, 2048, 32768, 131072):
            for sites in (1, 4, 10):
                with self.subTest(memory_mib=memory_mib, sites=sites):
                    planned = redis_resource_profile(memory_mib * MIB, site_count=sites)
                    resources = {**sample(memory_mib), 'redis_cache': planned}
                    bounds = autotune.capacity(resources)
                    self.assertEqual(bounds['redis_reserve_bytes'], planned['reserve_mib'] * MIB)
                    self.assertEqual(planned['memory_reserve_bytes'], planned['reserve_mib'] * MIB)
                    self.assertGreaterEqual(planned['reserve_mib'], 2 * planned['maxmemory_mib'] + 10 * sites)
                    self.assertLessEqual(bounds['memory_budget'] + bounds['reserve_bytes'], memory_mib * MIB)
                    self.assertGreaterEqual(planned['per_site_maxmemory_mib'], 4)

    def test_disabled_cache_does_not_change_existing_php_capacity(self):
        before = autotune.capacity(sample())
        after = autotune.capacity({**sample(), 'redis_cache': {'enabled': False,
                                                            'memory_reserve_bytes': GIB}})
        self.assertEqual(before, after)
        self.assertEqual(after['redis_reserve_bytes'], 0)

    def test_reserve_is_additive_to_database_os_and_opcache(self):
        resources = {**sample(), 'redis_cache': cache()}
        bounds = autotune.capacity(resources)
        expected_os = int(2048 * MIB * 0.35)
        self.assertEqual(bounds['os_database_reserve_bytes'], expected_os)
        self.assertEqual(bounds['opcache_reserve_bytes'], 128 * MIB)
        self.assertEqual(bounds['redis_reserve_bytes'], 214 * MIB)
        self.assertEqual(bounds['reserve_bytes'], expected_os + (128 + 214) * MIB)
        self.assertEqual(bounds['memory_budget'], 2048 * MIB - bounds['reserve_bytes'])
        self.assertLess(bounds['memory_budget'], autotune.capacity(sample())['memory_budget'])
        self.assertLessEqual(bounds['capacity'] * bounds['worker_bytes'], bounds['memory_budget'])

    def test_manual500_on2gb_still_fits_both_budgets_with_cache(self):
        resources = {**sample(), 'redis_cache': cache()}
        limits = configured_profile(autotune.profile(resources),
                                    {'memory_limit_mb': 500, 'upload_max_filesize_mb': 128},
                                    resources, check_capacity=True)
        bounds = autotune.capacity({**resources, 'php_settings': {'memory_limit_mb': 500}})
        self.assertEqual(limits['memory_mib'], 500)
        self.assertEqual(bounds['capacity'], 1)
        self.assertEqual(bounds['worker_bytes'], 532 * MIB)
        self.assertLessEqual(bounds['worker_bytes'], bounds['memory_budget'])

    def test_manual_setting_that_fit_before_cache_is_rejected_after_reserve(self):
        original = sample(1024)
        configured_profile(autotune.profile(original), {'memory_limit_mb': 450},
                           original, check_capacity=True)
        with self.assertRaisesRegex(ValueError, 'Redis'):
            configured_profile(autotune.profile(original), {'memory_limit_mb': 450},
                               {**original, 'redis_cache': cache(51, 112)}, check_capacity=True)

    def test_cache_and_actual_rss_reserve_cannot_be_underreported(self):
        resources = {**sample(), 'redis_cache': cache(100, 20)}
        self.assertEqual(autotune.redis_reserve_bytes(resources), 200 * MIB)
        resources['redis_cache']['used_memory_rss_bytes'] = 250 * MIB
        self.assertEqual(autotune.redis_reserve_bytes(resources), 250 * MIB)
        resources['redis_cache']['used_memory_rss_bytes'] = None
        self.assertEqual(autotune.redis_reserve_bytes(resources), 200 * MIB)

    def test_invalid_reserve_metadata_fails_closed(self):
        for metadata in ([], {'enabled': True, 'memory_reserve_bytes': -1},
                         {'enabled': True, 'memory_reserve_bytes': '256'},
                         {'enabled': True, 'maxmemory_mib': True},
                         {'enabled': True, 'used_memory_rss_bytes': 'unknown'}):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                autotune.capacity({**sample(), 'redis_cache': metadata})

    def test_tiny_server_reports_exhausted_budget_instead_of_negative_or_unbounded(self):
        resources = {**sample(512), 'redis_cache': cache(25, 60)}
        bounds = autotune.capacity(resources)
        self.assertEqual(bounds['capacity'], 1)
        self.assertEqual(bounds['memory_budget'], 4 * MIB)
        decision = {**bounds, 'children': 1, 'reason': 'initial'}
        report = autotune.AutoTuner._report(resources, None, decision, 1000)
        self.assertEqual(report['reason'], 'memory-budget-exhausted')
        self.assertEqual(report['redis_cache']['memory_reserve_mib'], 60)
        with self.assertRaises(ValueError):
            configured_profile(autotune.profile(resources), {'memory_limit_mb': 128},
                               resources, check_capacity=True)

    def test_adaptive_status_returns_only_aggregate_cache_measurements(self):
        resources = {**sample(), 'redis_cache': cache(102, 214, planned_maxmemory_mib=204,
            used_memory_bytes=20 * MIB, used_memory_rss_bytes=30 * MIB,
            sampled_at=1000, keys=['private-customer-key'], password='PRIVATE')}
        bounds = autotune.capacity(resources)
        report = autotune.AutoTuner._report(resources, None,
                                           {**bounds, 'children': 2, 'reason': 'stable'}, 1001)
        self.assertEqual(report['redis_cache']['used_memory_mib'], 20)
        self.assertEqual(report['redis_cache']['used_memory_rss_mib'], 30)
        self.assertEqual(report['redis_cache']['planned_maxmemory_mib'], 204)
        self.assertEqual(report['redis_cache']['sampled_at'], 1000)
        self.assertNotIn('PRIVATE', json.dumps(report))
        self.assertNotIn('private-customer-key', json.dumps(report))


class RedisResourceReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        self.tuner = autotune.AutoTuner('8.3', self.runner, data_dir=self.base / 'data',
                                      etc_root=self.base / 'etc', run_root=self.base / 'run')
        self.config = {'stack': 'nginx', 'php_version': '8.3', 'database': 'mariadb',
                       'redis_cache': {'enabled': True, 'maxmemory_mib': 102,
                                       'reserve_mib': 214, 'site_count': 1}}
        core.atomic_json(self.tuner.config_file, self.config)
        self.profile_calls = []

        def policy(total, site_count=1):
            self.profile_calls.append((total, site_count))
            limit = total // MIB // 20
            return {'maxmemory_mib': limit, 'memory_reserve_bytes': (2 * limit + 10 * site_count) * MIB}

        self.policy = mock.Mock(side_effect=policy)
        self.module = SimpleNamespace(redis_resource_profile=self.policy)
        self.module_patch = mock.patch.dict(sys.modules, {'wpi.redis_cache': self.module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    def read(self, memory_mib):
        with mock.patch.object(autotune, 'detect_resources', return_value=sample(memory_mib)):
            return self.tuner._resources()

    def test_hardware_increase_plans_larger_cache_before_it_has_been_applied(self):
        before = self.read(2048)
        after = self.read(4096)
        self.assertEqual(before['redis_cache']['memory_reserve_bytes'], 214 * MIB)
        self.assertEqual(after['redis_cache']['memory_reserve_bytes'], 418 * MIB)
        self.assertEqual(after['redis_cache']['maxmemory_mib'], 102)
        self.assertEqual(after['redis_cache']['planned_maxmemory_mib'], 204)
        self.assertEqual(self.profile_calls, [(2048 * MIB, 1), (4096 * MIB, 1)])
        self.runner.assert_not_called()

    def test_hardware_downsize_keeps_existing_cache_reserve_until_optimizer_reconfigures(self):
        self.config['redis_cache'].update(maxmemory_mib=204, reserve_mib=418)
        core.atomic_json(self.tuner.config_file, self.config)
        before = self.read(2048)
        self.assertEqual(before['redis_cache']['memory_reserve_bytes'], 418 * MIB)
        self.config['redis_cache'].update(maxmemory_mib=102, reserve_mib=214)
        core.atomic_json(self.tuner.config_file, self.config)
        after = self.read(2048)
        self.assertEqual(after['redis_cache']['memory_reserve_bytes'], 214 * MIB)
        self.assertGreater(autotune.capacity(after)['memory_budget'], autotune.capacity(before)['memory_budget'])

    def test_site_count_native_reserve_and_observed_rss_are_kept(self):
        self.config['redis_cache'].update(site_count=3, reserve_mib=234,
                                         used_memory_rss_bytes=300 * MIB,
                                         used_memory_bytes=50 * MIB, sampled_at=1234)
        core.atomic_json(self.tuner.config_file, self.config)
        resources = self.read(2048)
        self.assertEqual(self.profile_calls[-1], (2048 * MIB, 3))
        self.assertEqual(autotune.redis_reserve_bytes(resources), 300 * MIB)
        self.assertEqual(autotune.redis_resource_report(resources)['site_count'], 3)

    def test_unknown_hardware_profile_prevents_growth_but_does_not_skip_fpm_safety_policy(self):
        self.policy.side_effect = ValueError('Too little RAM for cache native memory')
        resources = self.read(512)
        bounds = autotune.capacity(resources)
        self.assertEqual(bounds['capacity'], 1)
        self.assertEqual(bounds['memory_budget'], 0)
        self.assertFalse(autotune.redis_resource_report(resources)['profile_available'])
        decision = autotune.decide(resources, {'listen queue': 1, 'active processes': 10,
                                  'total processes': 10}, {'children': 10, 'last_change': 0}, 1000)
        self.assertEqual(decision['children'], 1)

    def test_zero_enabled_sites_reserves_one_planned_first_site_without_creating_anything(self):
        self.config['redis_cache']['site_count'] = 0
        core.atomic_json(self.tuner.config_file, self.config)
        self.read(2048)
        self.assertEqual(self.profile_calls[-1], (2048 * MIB, 1))
        self.runner.assert_not_called()

    def test_php_settings_preserves_cache_reserve_when_removing_old_php_override(self):
        self.config['php_settings'] = {'memory_limit_mb': 500}
        core.atomic_json(self.tuner.config_file, self.config)
        manager = core.Manager(self.base / 'data', self.base / 'backups', self.runner)
        settings = PHPSettings(manager, etc_root=self.base / 'etc', run_root=self.base / 'run')
        with mock.patch.object(autotune, 'detect_resources', return_value=sample()):
            clean = settings._resources()
            self.assertNotIn('php_settings', clean)
            self.assertIn('redis_cache', clean)
            report = settings.status()
        self.assertEqual(report['redis_cache']['memory_reserve_mib'], 214)
        self.assertEqual(report['capacity'], 1)
        self.runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
