"""Redis resource budgets, isolation, authenticated activation and recovery."""
import hashlib
import fnmatch
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi import core, redis_cache
from wpi.autotune import GIB, MIB
from wpi.redis_cache import RedisCache, RedisConnection, RedisProtocolError, redis_resource_profile


DROPIN = b'<?php // official drop-in fixture\n'


class PolicyTests(unittest.TestCase):
    def test_ram_scale_without_arbitrary_server_ceiling(self):
        small = redis_resource_profile(32 * GIB, 4)
        big = redis_resource_profile(128 * GIB, 4)
        self.assertEqual(small['maxmemory_mib'], 1638)
        self.assertEqual(big['maxmemory_mib'], 6553)
        self.assertEqual(small['reserve_mib'], small['maxmemory_mib'] * 2 + 40)
        self.assertEqual(small['memory_reserve_bytes'], small['reserve_mib'] * MIB)
        self.assertGreater(big['per_site_maxmemory_mib'], small['per_site_maxmemory_mib'])

    def test_many_instances_share_budget_and_cannot_exceed_ram(self):
        one = redis_resource_profile(GIB, 1)
        four = redis_resource_profile(GIB, 4)
        self.assertEqual(one['maxmemory_mib'], four['maxmemory_mib'])
        self.assertLess(four['per_site_maxmemory_mib'], one['per_site_maxmemory_mib'])
        self.assertGreater(four['reserve_mib'], one['reserve_mib'])
        for total, count in ((128 * MIB, 1), (GIB, 100), (GIB, -1), (True, 1), (GIB, True)):
            with self.subTest(total=total, count=count), self.assertRaises(ValueError):
                redis_resource_profile(total, count)


class FakeClient:
    def __init__(self, backend, identifier, username, password):
        self.backend, self.identifier = backend, identifier
        self.state = backend.instances.setdefault(identifier, {'keys': {}, 'limit': 0})
        self.username = username
        expected = backend.cache._admin() if username == 'wpi_admin' else backend.cache._credential(
            backend.cache._paths(identifier)['credentials'], 'wpi_' + identifier)
        if expected['password'] != password or identifier not in backend.active:
            raise RedisProtocolError('Auth failed')
        self.closed = False
        backend.clients.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def command(self, *args):
        if self.closed:
            raise RedisProtocolError('Connection closed')
        self.backend.commands.append((self.identifier, args))
        command = args[0].upper()
        if self.username != 'wpi_admin':
            tokens = self.state['acl_users'][self.username]
            if '+' + command.lower() not in tokens:
                raise RedisProtocolError('NOPERM command denied')
            if command in ('GET', 'SET', 'ZADD', 'ZRANGEBYSCORE', 'ZREMRANGEBYSCORE', 'ZCOUNT'):
                patterns = [token[1:] for token in tokens if token.startswith('~')]
                if not any(fnmatch.fnmatchcase(args[1], pattern) for pattern in patterns):
                    raise RedisProtocolError('NOPERM key denied')
        if command == 'GET':
            return self.state['keys'].get(args[1])
        if command == 'SET':
            self.state['keys'][args[1]] = args[2]
            return 'OK'
        if command in ('ZADD', 'ZRANGEBYSCORE', 'ZREMRANGEBYSCORE', 'ZCOUNT'):
            values = self.state.setdefault('sorted_sets', {}).setdefault(args[1], {})
            if command == 'ZADD':
                fresh = args[3] not in values
                values[args[3]] = float(args[2])
                return int(fresh)
            selected = sorted(((member, score) for member, score in values.items()
                               if float(args[2]) <= score <= float(args[3])), key=lambda item: item[1])
            if command == 'ZREMRANGEBYSCORE':
                for member, _ in selected:
                    del values[member]
                return len(selected)
            if command == 'ZCOUNT':
                return len(selected)
            return [part for item in selected for part in item] if 'WITHSCORES' in args else \
                [member for member, _ in selected]
        if command == 'CONFIG' and args[1] == 'GET':
            return ['maxmemory', str(self.state['limit'])]
        if command == 'CONFIG' and args[1] == 'SET':
            if args[2] == 'maxmemory':
                if self.backend.fail_resize:
                    self.backend.fail_resize = False
                    raise RedisProtocolError('Failed resize')
                self.state['limit'] = int(args[3])
            return 'OK'
        if command == 'ACL':
            if args[1] == 'LOAD':
                self.backend.load_acl(self.identifier)
                # Redis 6 frees all named users and closes their existing clients.
                for client in self.backend.clients:
                    if client.identifier == self.identifier:
                        client.closed = True
            elif args[1] == 'SETUSER':
                if args[3] != 'reset':
                    raise AssertionError('Targeted ACL update must replace all rules.')
                self.state['acl_users'][args[2]] = list(args[4:])
            return 'OK'
        if command == 'FLUSHDB':
            self.state['keys'].clear()
            self.state.setdefault('sorted_sets', {}).clear()
            return 'OK'
        if command == 'FLUSHALL' and self.username != 'wpi_admin':
            raise RedisProtocolError('Forbidden')
        if command == 'INFO':
            rss = self.state.get('rss', 2 * MIB)
            return (f'used_memory:1048576\r\nused_memory_rss:{rss}\r\n'
                    f'maxmemory:{self.state["limit"]}\r\nkeyspace_hits:7\r\n'
                    'keyspace_misses:2\r\nevicted_keys:0\r\nconnected_clients:1\r\n')
        if command == 'PING':
            return 'PONG'
        return 'OK'


class Backend:
    def __init__(self, data, root):
        self.data, self.root = data, root
        self.calls, self.wp_calls, self.commands = [], [], []
        self.active, self.plugins_active = set(), set()
        self.instances, self.memory = {}, 32 * GIB
        self.clients = []
        self.installed_packages = {'redis-server', 'redis-tools', 'php8.3-redis'}
        self.stock_service_exists, self.fail_resize, self.fail_health = False, False, False
        self.config_name_field = 'name'

    def runner(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc, out = 0, ''
        if argv[0] == 'dpkg-query':
            rc = 0 if argv[-1] in self.installed_packages else 1
            out = 'install ok installed' if rc == 0 else ''
        elif argv[:2] == ['systemctl', 'show']:
            out = 'loaded' if self.stock_service_exists else 'not-found'
        elif argv[:2] == ['systemctl', 'is-active']:
            identifier = self.identifier(argv[-1])
            rc = 0 if identifier in self.active else 3
        elif argv[:2] == ['systemctl', 'enable'] and '--now' in argv:
            identifier = self.identifier(argv[-1])
            self.active.add(identifier)
            config = self.cache._paths(identifier)['config'].read_text()
            limit = int(re.search(r'^maxmemory (\d+)$', config, re.M)[1])
            self.instances.setdefault(identifier, {'keys': {}})['limit'] = limit
            self.load_acl(identifier)
        elif argv[:2] == ['systemctl', 'start']:
            self.active.add(self.identifier(argv[-1]))
        elif argv[:2] == ['systemctl', 'disable']:
            self.active.discard(self.identifier(argv[-1]))
        return subprocess.CompletedProcess(argv, rc, out, '')

    def load_acl(self, identifier):
        acl = self.cache._paths(identifier)['acl'].read_text()
        self.instances[identifier]['acl_users'] = {row[1]: row[2:] for line in acl.splitlines()
                                                   if (row := line.split())}

    @staticmethod
    def identifier(service):
        match = re.search(r'@([a-f0-9]{12})', service)
        return match[1] if match else 'stock'

    def connect(self, path, username, password):
        identifier = re.search(r'wpi-redis-([a-f0-9]{12})', str(path))[1]
        return FakeClient(self, identifier, username, password)

    def wp(self, site, *args, **kwargs):
        self.wp_calls.append((site['id'], args, kwargs))
        config_arg = next((arg for arg in args if arg.startswith('--config-file=')), None)
        path = Path(config_arg.split('=', 1)[1]) if config_arg else Path(site['root']) / 'wp-config.php'
        rc, out = 0, ''
        if args[:2] == ('config', 'set'):
            key = args[2]
            value = kwargs['input'].strip() if '--prompt' in args else args[3]
            content = path.read_text()
            content = re.sub(r"^define\('" + key + r"',.*?\);\n", '', content, flags=re.M)
            expression = value if '--raw' in args else json.dumps(value)
            path.write_text(content + f"define('{key}', {expression});\n")
        elif args[:2] == ('config', 'delete'):
            path.write_text(re.sub(r"^define\('" + args[2] + r"',.*?\);\n", '', path.read_text(), flags=re.M))
        elif args[:2] == ('config', 'list'):
            values = re.findall(r"^define\('(WP_REDIS_[A-Z_]+)', (.*)\);$", path.read_text(), re.M)
            filtered = [(key, value) for key, value in values if
                        (key == args[2] if '--strict' in args else args[2] in key)]
            out = json.dumps([{self.config_name_field: key, 'value': json.loads(value),
                               'type': 'constant'} for key, value in filtered])
        elif args[:2] == ('plugin', 'install'):
            target = Path(site['root']) / 'wp-content/plugins/redis-cache/includes/object-cache.php'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(DROPIN)
        elif args[:2] == ('plugin', 'is-active'):
            rc = 0 if site['id'] in self.plugins_active else 1
        elif args[:2] == ('plugin', 'activate'):
            self.plugins_active.add(site['id'])
        elif args[:2] == ('plugin', 'deactivate'):
            self.plugins_active.discard(site['id'])
        elif args[0] == 'cache':
            keys = self.instances[site['id']]['keys']
            key = (args[3] if args[1] != 'set' else args[4]) + ':' + args[2]
            if args[1] == 'set':
                keys[key] = args[3]
            elif args[1] == 'get':
                out = 'broken' if self.fail_health else keys.get(key, '')
            elif args[1] == 'delete':
                keys.pop(key, None)
        return subprocess.CompletedProcess([], rc, out, '')


class ActivationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.data = self.base / 'data'
        self.data.mkdir()
        core.atomic_json(self.data / 'config.json', {'php_version': '8.3', 'stack': 'nginx'})
        self.backend = Backend(self.data, self.base / 'www')
        self.manager = core.Manager(data_dir=self.data, runner=self.backend.runner)
        self.manager.wp = self.backend.wp
        self.manager.remember_config = lambda identifier: None
        self.cache = RedisCache(self.manager, etc_root=self.base / 'etc', run_root=self.base / 'run',
                                connection_factory=self.backend.connect,
                                resources=lambda: {'memory_total': self.backend.memory})
        self.backend.cache = self.cache
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core, 'WWW', self.base / 'www').start()
        mock.patch.object(redis_cache, 'DROPIN_SHA256', hashlib.sha256(DROPIN).hexdigest()).start()
        for number in (1, 2):
            identifier = f'{number:012x}'
            root = self.base / 'www' / identifier / 'public'
            (root / 'wp-content').mkdir(parents=True)
            (root / 'wp-config.php').write_text('<?php\n// existing configuration\n')
            self.manager.save_site({'id': identifier, 'primary': f'site{number}.example.com',
                                    'root': str(root), 'aliases': [], 'secondary': []})
        self.a, self.b = '000000000001', '000000000002'

    def test_install_keeps_existing_unmanaged_daemon_and_creates_socket_only_template(self):
        self.backend.stock_service_exists = True
        self.backend.installed_packages.remove('php8.3-redis')
        report = self.cache.install()
        self.assertTrue(report['enabled'])
        self.assertFalse(any(call[:2] == ['systemctl', 'disable'] for call in self.backend.calls))
        template = self.cache.template.read_text()
        self.assertIn('RestrictAddressFamilies=AF_UNIX', template)
        self.assertIn('RuntimeDirectory=wpi-redis-%i', template)
        self.assertIn('Restart=on-failure', template)
        self.assertEqual(self.manager.config['redis_cache']['site_count'], 0)

    def test_new_stock_daemon_is_disabled_only_when_proven_new(self):
        self.backend.installed_packages.clear()
        self.cache.install()
        self.assertIn(['systemctl', 'disable', '--now', 'redis-server.service'], self.backend.calls)

    def test_activation_uses_pinned_plugin_private_auth_and_cross_process_health(self):
        report = self.cache.enable_site(self.a)
        self.assertTrue(report['persistent_cache_verified'])
        self.assertIn(self.a, self.backend.plugins_active)
        site = self.manager.site(self.a)
        credential = self.cache._credential(self.cache._paths(self.a)['credentials'], 'wpi_' + self.a)
        config, acl = self.cache._render(self.a, 32)
        self.assertIn('port 0\n', config)
        self.assertIn('save ""\nappendonly no', config)
        self.assertIn('maxmemory-policy allkeys-lfu', config)
        self.assertNotIn(credential['password'], acl)
        self.assertIn('user default off', acl)
        self.assertTrue(all(line.startswith('user ') for line in acl.splitlines()))
        self.assertEqual(acl.splitlines()[0], 'user wpi_managed_marker off')
        self.assertIn('+flushdb', acl)
        self.assertNotIn('+flushall', acl)
        self.assertNotIn('+eval', acl)
        self.assertNotIn('+quit', acl)  # QUIT is not an ACL command in Redis 6.0.
        password_calls = [entry for entry in self.backend.wp_calls if entry[1][:3] ==
                          ('config', 'set', 'WP_REDIS_PASSWORD')]
        self.assertTrue(password_calls)
        for _, args, kwargs in password_calls:
            self.assertNotIn(credential['password'], ' '.join(args))
            self.assertIn('--prompt', args)
            self.assertEqual(kwargs['input'].strip(), credential['password'])
        self.assertTrue(self.cache._config_current(site))
        self.assertEqual(len([entry for entry in self.backend.wp_calls if entry[1][:2] == ('cache', 'get')]), 1)
        plugin_calls = [entry[1] for entry in self.backend.wp_calls if entry[1][:2] == ('plugin', 'install')]
        self.assertIn('--version=3.0.0', plugin_calls[0])

    def test_flush_and_disable_are_confined_to_one_instance(self):
        self.cache.enable_site(self.a)
        self.cache.enable_site(self.b)
        self.backend.instances[self.a]['keys']['sentinel'] = 'A'
        self.backend.instances[self.b]['keys']['sentinel'] = 'B'
        self.cache.flush_site(self.a)
        self.assertNotIn('sentinel', self.backend.instances[self.a]['keys'])
        self.assertEqual(self.backend.instances[self.b]['keys']['sentinel'], 'B')
        self.cache.disable_site(self.a)
        self.assertFalse(self.manager.site(self.a)['redis_cache']['enabled'])
        self.assertIn(self.b, self.backend.active)
        self.assertNotIn(self.a, self.backend.active)
        self.assertFalse((Path(self.manager.site(self.a)['root']) / 'wp-content/object-cache.php').exists())
        self.assertIn('sentinel', self.backend.instances[self.b]['keys'])

    def test_metrics_acl_upgrade_allows_collection_and_pruning_without_admin_or_peer_access(self):
        self.cache.enable_site(self.a)
        self.cache.enable_site(self.b)
        metric_key = f'wpi:{self.a}:wp_:redis-cache:metrics'
        peer_key = f'wpi:{self.b}:wp_:redis-cache:metrics'
        self.backend.instances[self.a]['keys']['sentinel'] = 'keep A'
        self.backend.instances[self.b]['keys']['sentinel'] = 'keep B'
        credentials = {identifier: hashlib.sha256(self.cache._paths(identifier)[
            'credentials'].read_bytes()).hexdigest() for identifier in (self.a, self.b)}
        acl_path = self.cache._paths(self.a)['acl']
        old_acl = acl_path.read_text()
        for command in ('zadd', 'zrangebyscore', 'zremrangebyscore', 'zcount'):
            old_acl = old_acl.replace(' +' + command, '')
        acl_path.write_bytes(old_acl.encode())
        self.backend.load_acl(self.a)
        with self.cache._client(self.a, admin=False) as client:
            with self.assertRaisesRegex(RedisProtocolError, 'NOPERM'):
                client.command('ZADD', metric_key, 100, 'first')

        self.backend.calls.clear()
        self.backend.commands.clear()
        self.assertTrue(self.cache.optimize()['changed'])
        self.assertTrue(any(identifier == self.a and args[:3] ==
                            ('ACL', 'SETUSER', 'wpi_' + self.a)
                            for identifier, args in self.backend.commands))
        self.assertFalse(any(call[:2] in (['systemctl', 'start'], ['systemctl', 'restart'],
                                         ['systemctl', 'enable']) for call in self.backend.calls))
        with self.cache._client(self.a, admin=False) as client:
            self.assertEqual(client.command('ZADD', metric_key, 100, 'first'), 1)
            self.assertEqual(client.command('ZADD', metric_key, 160, 'second'), 1)
            self.assertEqual(client.command('ZRANGEBYSCORE', metric_key, '-inf', '+inf',
                                            'WITHSCORES'), ['first', 100.0, 'second', 160.0])
            self.assertEqual(client.command('ZCOUNT', metric_key, '-inf', '+inf'), 2)
            self.assertEqual(client.command('ZREMRANGEBYSCORE', metric_key, 0, 100), 1)
            self.assertEqual(client.command('ZCOUNT', metric_key, '-inf', '+inf'), 1)
            for command in (('ZADD', peer_key, 100, 'foreign'),
                            ('ZRANGEBYSCORE', peer_key, '-inf', '+inf'),
                            ('ZCOUNT', peer_key, '-inf', '+inf'),
                            ('ZREMRANGEBYSCORE', peer_key, 0, 100),
                            ('FLUSHALL',), ('EVAL', 'return 1', 0),
                            ('CONFIG', 'SET', 'maxmemory', 1), ('ACL', 'LOAD')):
                with self.subTest(command=command[0]), self.assertRaisesRegex(
                        RedisProtocolError, 'NOPERM'):
                    client.command(*command)
        for identifier in (self.a, self.b):
            self.assertEqual(hashlib.sha256(self.cache._paths(identifier)[
                'credentials'].read_bytes()).hexdigest(), credentials[identifier])
            self.assertIn('sentinel', self.backend.instances[identifier]['keys'])
        self.assertFalse(self.cache.optimize()['changed'])

    def test_foreign_dropin_is_rejected_before_any_mutation(self):
        target = Path(self.manager.site(self.a)['root']) / 'wp-content/object-cache.php'
        target.write_bytes(b'<?php // another cache')
        before = (self.data / 'config.json').read_bytes()
        with self.assertRaisesRegex(ValueError, 'tidak ditimpa'):
            self.cache.enable_site(self.a)
        self.assertEqual(target.read_bytes(), b'<?php // another cache')
        self.assertEqual((self.data / 'config.json').read_bytes(), before)
        self.assertFalse(self.backend.calls)

    def test_health_failure_restores_configuration_dropin_acl_and_site_metadata(self):
        self.cache.install()
        site = self.manager.site(self.a)
        snapshots = self.cache._capture(site)
        self.backend.fail_health = True
        with self.assertRaisesRegex(RuntimeError, 'lintas proses'):
            self.cache.enable_site(self.a)
        for path, saved in snapshots.items():
            self.assertEqual(redis_cache._bytes(path), saved[0], str(path))
        self.assertNotIn(self.a, self.backend.active)
        originals = list((self.data / 'redis/backups').glob('*/wp-config.php'))
        self.assertTrue(originals)
        self.assertEqual(originals[0].read_bytes(), snapshots[Path(site['root']) / 'wp-config.php'][0])

    def test_held_app_and_admin_connections_survive_combined_acl_and_memory_upgrade(self):
        self._held_upgrade(fail=False)

    def test_failed_combined_upgrade_restores_live_acl_and_quota_without_disconnecting_clients(self):
        self._held_upgrade(fail=True)

    def _held_upgrade(self, fail):
        self.cache.enable_site(self.a)
        acl_path = self.cache._paths(self.a)['acl']
        old_acl = acl_path.read_text()
        for command in ('zadd', 'zrangebyscore', 'zremrangebyscore', 'zcount'):
            old_acl = old_acl.replace(' +' + command, '')
        acl_path.write_bytes(old_acl.encode())
        self.backend.load_acl(self.a)
        self.backend.memory = 128 * GIB
        self.backend.fail_resize = fail
        old_limit = self.backend.instances[self.a]['limit']
        protected = {name: tokens.copy() for name, tokens in self.backend.instances[
            self.a]['acl_users'].items() if name in ('default', 'wpi_admin')}
        key = f'wpi:{self.a}:test:sentinel'
        metric_key = f'wpi:{self.a}:wp_:redis-cache:metrics'
        with self.cache._client(self.a, admin=False) as app, self.cache._client(self.a) as admin:
            self.assertEqual(app.command('SET', key, 'keep'), 'OK')
            with self.assertRaisesRegex(RedisProtocolError, 'NOPERM'):
                app.command('ZADD', metric_key, 100, 'sample')
            self.backend.commands.clear()
            if fail:
                with self.assertRaises(RedisProtocolError):
                    self.cache.optimize()
                self.assertEqual(acl_path.read_bytes(), old_acl.encode())
                self.assertEqual(admin.command('CONFIG', 'GET', 'maxmemory')[1], str(old_limit))
                with self.assertRaisesRegex(RedisProtocolError, 'NOPERM'):
                    app.command('ZADD', metric_key, 100, 'sample')
            else:
                self.assertTrue(self.cache.optimize()['changed'])
                self.assertGreater(int(admin.command('CONFIG', 'GET', 'maxmemory')[1]), old_limit)
                self.assertEqual(app.command('ZADD', metric_key, 100, 'sample'), 1)
            self.assertEqual(app.command('PING'), 'PONG')
            self.assertEqual(app.command('GET', key), 'keep')
            self.assertEqual(admin.command('PING'), 'PONG')
            self.assertFalse(any(args[:2] == ('ACL', 'LOAD') for _, args in self.backend.commands))
            updates = [args for _, args in self.backend.commands if args[:2] == ('ACL', 'SETUSER')]
            self.assertTrue(updates)
            self.assertTrue(all(args[2:5] == ('wpi_' + self.a, 'reset', 'on') for args in updates))
        for name, tokens in protected.items():
            self.assertEqual(self.backend.instances[self.a]['acl_users'][name], tokens)

    def test_repeat_enable_preserves_exact_config_and_backup_set(self):
        self.cache.enable_site(self.a)
        config = Path(self.manager.site(self.a)['root']) / 'wp-config.php'
        before = config.read_bytes(), list((self.data / 'redis/backups').iterdir())
        self.backend.wp_calls.clear()
        report = self.cache.enable_site(self.a)
        self.assertFalse(report['changed'])
        self.assertEqual(config.read_bytes(), before[0])
        self.assertEqual(list((self.data / 'redis/backups').iterdir()), before[1])
        self.assertFalse(any(entry[1][:2] in (('config', 'set'), ('config', 'delete'))
                             for entry in self.backend.wp_calls))

    def test_config_current_accepts_real_wpcli_name_schema_and_legacy_key_schema(self):
        self.cache.enable_site(self.a)
        site = self.manager.site(self.a)
        for field in ('name', 'key'):
            with self.subTest(field=field):
                self.backend.config_name_field = field
                self.assertTrue(self.cache._config_current(site))
                output = self.backend.wp(site, 'config', 'list', 'WP_REDIS_', '--format=json')
                constants = self.cache._config_constants(output.stdout)
                self.assertIs(constants['WP_REDIS_DISABLED'], False)
                self.assertIs(constants['WP_REDIS_DISABLE_GROUP_FLUSH'], True)
                self.assertEqual(constants['WP_REDIS_TIMEOUT'], 0.5)

    def test_noop_rejects_php_strings_and_boolean_numeric_type_confusion(self):
        self.cache.enable_site(self.a)
        site = self.manager.site(self.a)
        config = Path(site['root']) / 'wp-config.php'
        original = config.read_text()
        for name, expression, replacement in (
                ('WP_REDIS_DISABLED', 'false', '"false"'),
                ('WP_REDIS_DATABASE', '0', '"0"'),
                ('WP_REDIS_TIMEOUT', '0.5', '"0.5"'),
                ('WP_REDIS_DISABLED', 'false', '0'),
                ('WP_REDIS_DATABASE', '0', 'false'),
                ('WP_REDIS_DISABLE_GROUP_FLUSH', 'true', '1')):
            with self.subTest(name=name):
                config.write_text(original.replace(f"define('{name}', {expression});",
                                                   f"define('{name}', {replacement});"))
                self.assertFalse(self.cache._config_current(site))
        config.write_text(original)
        self.assertTrue(self.cache._config_current(site))

    def test_repeated_prepare_preserves_all_persistent_managed_state(self):
        self.cache.enable_site(self.a)
        def state():
            return {str(path.relative_to(self.base)): path.read_bytes()
                    for path in self.base.rglob('*') if path.is_file()}
        before = state()
        self.backend.wp_calls.clear()
        self.cache.prepare_site_config(self.a)
        self.assertEqual(state(), before)
        self.assertFalse(any(entry[1][:2] in (('config', 'set'), ('config', 'delete'))
                             for entry in self.backend.wp_calls))

    def test_optimize_only_resizes_live_instances_and_never_restarts_stopped_site(self):
        self.cache.enable_site(self.a)
        self.cache.enable_site(self.b)
        self.backend.active.remove(self.b)
        old_limit = self.backend.instances[self.a]['limit']
        self.backend.calls.clear()
        self.backend.memory = 128 * GIB
        self.cache.optimize()
        self.assertGreater(self.backend.instances[self.a]['limit'], old_limit)
        self.assertNotIn(self.b, self.backend.active)
        self.assertFalse(any(call[:2] in (['systemctl', 'start'], ['systemctl', 'restart'],
                                         ['systemctl', 'enable']) for call in self.backend.calls))
        self.backend.commands.clear()
        report = self.cache.optimize()
        self.assertFalse(report['changed'])
        self.assertFalse(any(args[:2] == ('CONFIG', 'SET') for _, args in self.backend.commands))

    def test_failed_resize_restores_disk_and_live_quota_without_flushing_keys(self):
        self.cache.enable_site(self.a)
        self.backend.instances[self.a]['keys']['sentinel'] = 'keep'
        paths = [self.cache._paths(self.a)['config'], self.data / 'config.json']
        original = {path: path.read_bytes() for path in paths}
        permissions = {path: (path.stat().st_mode, path.stat().st_uid, path.stat().st_gid) for path in paths}
        old_limit = self.backend.instances[self.a]['limit']
        self.backend.fail_resize = True
        self.backend.memory = 128 * GIB
        with self.assertRaises(RedisProtocolError):
            self.cache.optimize()
        for path, value in original.items():
            self.assertEqual(path.read_bytes(), value)
            self.assertEqual((path.stat().st_mode, path.stat().st_uid, path.stat().st_gid), permissions[path])
        self.assertEqual(self.backend.instances[self.a]['limit'], old_limit)
        self.assertEqual(self.backend.instances[self.a]['keys']['sentinel'], 'keep')

    def test_ram_downscale_retains_observed_allocator_rss_until_pages_are_released(self):
        self.cache.enable_site(self.a)
        self.backend.instances[self.a]['rss'] = 900 * MIB + 1
        self.backend.memory = 2 * GIB
        expected = redis_resource_profile(2 * GIB, 1)
        report = self.cache.optimize()
        self.assertEqual(report['maxmemory_mib'], expected['maxmemory_mib'])
        self.assertEqual(self.manager.config['redis_cache']['reserve_mib'], 901)
        self.assertEqual(report['memory_reserve_bytes'], 901 * MIB)
        config = (self.data / 'config.json').read_bytes()
        self.assertFalse(self.cache.optimize()['changed'])
        self.assertEqual((self.data / 'config.json').read_bytes(), config)
        self.backend.instances[self.a]['rss'] = 10 * MIB
        self.cache.optimize()
        self.assertEqual(self.manager.config['redis_cache']['reserve_mib'], expected['reserve_mib'])

    def test_disable_failure_does_not_start_previously_stopped_instance(self):
        self.cache.enable_site(self.a)
        self.backend.active.remove(self.a)
        self.backend.calls.clear()
        with mock.patch.object(self.cache, 'optimize', side_effect=RuntimeError('resize failed')):
            with self.assertRaises(RuntimeError):
                self.cache.disable_site(self.a)
        self.assertNotIn(self.a, self.backend.active)
        self.assertTrue(self.manager.site(self.a)['redis_cache']['enabled'])
        self.assertFalse(any(call[:2] == ['systemctl', 'start'] for call in self.backend.calls))

    def test_status_is_readonly_secrets_free_and_reports_aggregate_usage(self):
        self.cache.enable_site(self.a)
        self.backend.calls.clear()
        self.backend.wp_calls.clear()
        cfg = (self.data / 'config.json').read_bytes()
        report = self.cache.status()
        self.assertEqual(report['used_memory_bytes'], MIB)
        self.assertEqual(report['used_memory_rss_bytes'], 2 * MIB)
        secret = self.cache._credential(self.cache._paths(self.a)['credentials'], 'wpi_' + self.a)['password']
        self.assertNotIn(secret, json.dumps(report))
        self.assertFalse(self.backend.wp_calls)
        self.assertTrue(all(call[:2] == ['systemctl', 'is-active'] for call in self.backend.calls))
        self.assertEqual((self.data / 'config.json').read_bytes(), cfg)

    def test_prepare_import_fixes_credentials_before_wordpress_boot_and_removes_unsupported_routes(self):
        self.cache.install()
        site = self.manager.site(self.a)
        path = Path(site['root']) / 'wp-config.php'
        path.write_text("<?php\ndefine('WP_REDIS_PASSWORD', \"old-server-secret\");\n"
                        "define('WP_REDIS_CLUSTER', \"foreign-server\");\n"
                        "define('WP_REDIS_MAXTTL', \"60\");\n")
        self.cache.prepare_site_config(self.a)
        self.assertNotIn('old-server-secret', path.read_text())
        self.assertNotIn('WP_REDIS_CLUSTER', path.read_text())
        self.assertNotIn('WP_REDIS_MAXTTL', path.read_text())
        self.assertIn(self.a, self.backend.active)
        self.assertFalse(any(entry[1][0] not in ('config',) for entry in self.backend.wp_calls))

    def test_disabled_restore_removes_only_official_snapshot_dropin_before_wordpress_boot(self):
        self.cache.enable_site(self.a)
        self.cache.disable_site(self.a)
        site = self.manager.site(self.a)
        target = Path(site['root']) / 'wp-content/object-cache.php'
        target.write_bytes(DROPIN)  # Restored snapshot taken while Redis was enabled.
        self.backend.calls.clear()
        self.backend.wp_calls.clear()
        report = self.cache.prepare_disabled_config(self.a)
        self.assertTrue(report['changed'])
        self.assertFalse(target.exists())
        self.assertNotIn(self.a, self.backend.active)
        self.assertFalse(self.manager.site(self.a)['redis_cache']['enabled'])
        self.assertIn("define('WP_REDIS_DISABLED', true);", (Path(site['root']) / 'wp-config.php').read_text())
        self.assertFalse(any(call[0] == 'systemctl' for call in self.backend.calls))
        self.assertTrue(all(entry[1][0] == 'config' for entry in self.backend.wp_calls))
        before = (Path(site['root']) / 'wp-config.php').read_bytes()
        backups = list((self.data / 'redis/backups').iterdir())
        for field in ('name', 'key'):
            with self.subTest(field=field):
                self.backend.config_name_field = field
                self.assertFalse(self.cache.prepare_disabled_config(self.a)['changed'])
        self.assertEqual((Path(site['root']) / 'wp-config.php').read_bytes(), before)
        self.assertEqual(list((self.data / 'redis/backups').iterdir()), backups)

    def test_disabled_restore_refuses_foreign_snapshot_dropin_without_mutation(self):
        site = self.manager.site(self.a)
        site['redis_cache'] = {'enabled': False}
        self.manager.save_site(site)
        target = Path(site['root']) / 'wp-content/object-cache.php'
        target.write_bytes(b'<?php // another plugin')
        before = (Path(site['root']) / 'wp-config.php').read_bytes()
        with self.assertRaisesRegex(ValueError, 'tidak ditimpa'):
            self.cache.prepare_disabled_config(self.a)
        self.assertEqual(target.read_bytes(), b'<?php // another plugin')
        self.assertEqual((Path(site['root']) / 'wp-config.php').read_bytes(), before)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.backend.wp_calls)

    def test_readiness_failure_reports_safe_errno_and_state_before_rollback(self):
        self.cache.install()
        with mock.patch.object(self.cache, 'connect', side_effect=FileNotFoundError(2, 'SECRET key:error')), \
                mock.patch.object(redis_cache.time, 'sleep'):
            with self.assertRaises(RuntimeError) as failure:
                self.cache.prepare_site_config(self.a)
        self.assertIn('"errno": 2', str(failure.exception))
        self.assertIn('"error_type": "FileNotFoundError"', str(failure.exception))
        self.assertNotIn('SECRET', str(failure.exception))
        self.assertNotIn('key:error', str(failure.exception))
        self.assertFalse(self.manager.site(self.a).get('redis_cache', {}).get('enabled'))

    def test_restore_candidate_outside_managed_site_is_rejected(self):
        self.cache.enable_site(self.a)
        foreign = self.base / 'outside.php'
        foreign.write_text('<?php\n')
        with self.assertRaisesRegex(ValueError, 'di luar'):
            self.cache.overlay_config(self.manager.site(self.a), foreign)
        self.assertEqual(foreign.read_text(), '<?php\n')

    def test_root_or_config_symlink_is_rejected(self):
        root = Path(self.manager.site(self.a)['root'])
        original = root / 'wp-config.php'
        foreign = self.base / 'outside.php'
        foreign.write_text('<?php\n')
        original.unlink()
        try:
            original.symlink_to(foreign)
        except OSError:
            self.skipTest('Symlink unavailable on this Windows runner')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.cache.enable_site(self.a)


class ProtocolTests(unittest.TestCase):
    def test_auth_and_values_use_socket_only_and_server_errors_are_sanitized(self):
        fake = mock.Mock()
        fake.makefile.return_value = io.BytesIO(b'+OK\r\n+PONG\r\n-ERR secret customer:key\r\n')
        with mock.patch.object(redis_cache.socket, 'socket', return_value=fake), \
                mock.patch.object(redis_cache.socket, 'AF_UNIX', 1, create=True):
            with RedisConnection('/run/private.sock', 'user', 'password') as client:
                self.assertEqual(client.command('PING'), 'PONG')
                with self.assertRaises(RedisProtocolError) as failure:
                    client.command('GET', 'customer:key')
                self.assertNotIn('secret', str(failure.exception))
                self.assertNotIn('customer:key', str(failure.exception))
        transmitted = b''.join(call.args[0] for call in fake.sendall.call_args_list)
        self.assertIn(b'AUTH', transmitted)
        self.assertIn(b'password', transmitted)
        fake.connect.assert_called_once_with('/run/private.sock')


if __name__ == '__main__':
    unittest.main()
