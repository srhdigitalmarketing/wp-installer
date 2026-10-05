#!/usr/bin/env bash
# Destructive fresh-server test. NEVER use on a VPS containing user data.
set -Eeuo pipefail

[[ "${CI:-}" == true && "${WPI_DISPOSABLE_VM:-}" == 1 && "$EUID" -eq 0 ]] || {
    printf 'Requires root, CI=true, and WPI_DISPOSABLE_VM=1 in a disposable Ubuntu VM.\n' >&2
    exit 1
}
STACK="${1:-}"
[[ "$STACK" == nginx || "$STACK" == apache ]] || { printf 'Choose nginx or apache.\n' >&2; exit 1; }
[[ -f wpi/core.py && -f /etc/os-release ]] || { printf 'Run from the repository on Ubuntu.\n' >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == ubuntu && ( "${VERSION_ID:-}" == 22.04 || "${VERSION_ID:-}" == 24.04 ) ]] || exit 1

# Hosted runners include an unmanaged MySQL server. Purge these known stacks
# and their data ONLY after the disposable-VM guard above, before fresh setup.
systemctl disable --now nginx apache2 mysql mariadb redis-server >/dev/null 2>&1 || true
mapfile -t SERVER_PACKAGES < <(dpkg-query -W -f='${Package}\n' \
    'nginx*' 'apache2*' 'mysql-server*' 'mysql-client*' 'mysql-common' \
    'mariadb-server*' 'mariadb-client*' 'mariadb-common' 'phpmyadmin' \
    'redis-server' 'redis-tools' 2>/dev/null || true)
if ((${#SERVER_PACKAGES[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${SERVER_PACKAGES[@]}"
fi
rm -rf -- /etc/nginx /etc/apache2 /etc/mysql /etc/phpmyadmin /etc/letsencrypt \
    /var/lib/mysql /var/lib/redis /etc/redis /var/www/wpi /var/lib/wpi /var/backups/wpi /etc/wpi

python3 scripts/build_release.py
WPI_VERSION="$(python3 -c 'from wpi import __version__; print(__version__)')"
WPI_BUNDLE="$(pwd)/dist/wp-installer-v${WPI_VERSION}.zip"
read -r WPI_BUNDLE_SHA _ < "${WPI_BUNDLE}.sha256"
bash install.sh --bundle "$WPI_BUNDLE" --sha256 "$WPI_BUNDLE_SHA"
[[ "$(/usr/local/bin/wpi --version)" == "$WPI_VERSION" ]] || exit 1
[[ "$(cat /usr/local/lib/wpi/VERSION)" == "v$WPI_VERSION" ]] || exit 1
/usr/local/bin/wpi status
/usr/local/bin/wpi list
export WPI_CI_BUNDLE="$WPI_BUNDLE" WPI_CI_BUNDLE_SHA="$WPI_BUNDLE_SHA" WPI_CI_VERSION="$WPI_VERSION"
export WPI_CI_STACK="$STACK"
python3 -u - <<'PY'
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

# Run integration against the package actually installed by bootstrap.
sys.path.insert(0, '/usr/local/lib/wpi')
from wpi import core, autotune
from wpi.web import WebStack

stack = os.environ['WPI_CI_STACK']
database = 'mariadb' if stack == 'nginx' else 'mysql'

def ci_run(argv, **kwargs):
    # Preserve the production subprocess contract. Only emit diagnostic output
    # for known checks/maintenance commands on this disposable fixture, which
    # contain no SQL, credentials, or customer application data.
    kwargs.setdefault('text', True)
    kwargs.setdefault('capture_output', True)
    check = kwargs.pop('check', True)
    env = os.environ.copy()
    env.update({'DEBIAN_FRONTEND': 'noninteractive', 'LC_ALL': 'C.UTF-8'})
    env.update(kwargs.pop('env', {}))
    # Repair uses strict TLS validation. Trust only this fixture's own cert
    # for its explicit loopback host probe; production code never disables TLS.
    if argv[0] == 'curl' and '--resolve' in argv and '--insecure' not in argv:
        host, port, address = argv[argv.index('--resolve') + 1].split(':')
        if host.endswith('.example.com') and port == '443' and address == '127.0.0.1':
            argv = [*argv, '--cacert', f'/etc/letsencrypt/live/{host}/fullchain.pem']
    result = subprocess.run(argv, env=env, check=False, **kwargs)
    if check and result.returncode:
        safe = ('core' in argv and 'verify-checksums' in argv) or any(
            name in argv for name in ('rewrite', 'maintenance-mode', 'cache', 'search-replace')
        ) or argv[:2] in (
            ['nginx', '-t'], ['apache2ctl', 'configtest'],
        )
        if safe:
            print('Safe CI diagnostic for verification command:', flush=True)
            print(result.stdout or '', flush=True)
            print(result.stderr or '', flush=True)
        raise RuntimeError(f'CI command {Path(argv[0]).name} failed (exit {result.returncode}).')
    return result

manager = core.Manager(runner=ci_run)
manager.setup(stack=stack, database=database)

# The controller is enabled by initial stack setup, without a settings step.
for action in ('is-enabled', 'is-active'):
    subprocess.run(['systemctl', action, '--quiet', 'wpi-autotune.timer'], check=True)
tuner = autotune.AutoTuner(manager.config['php_version'], ci_run, data_dir=manager.data)
assert tuner.status_socket.parent.stat().st_mode & 0o777 == 0o700
for attempt in range(25):
    try:
        fpm_status = autotune.read_fpm_status(tuner.status_socket)
        break
    except (OSError, ValueError, RuntimeError):
        if attempt == 24:
            raise
        time.sleep(0.2)
assert fpm_status['pool'] == 'www', fpm_status
assert fpm_status['total processes'] >= 1, fpm_status
# This invokes the installed systemd unit, not a unit-test runner/mocked socket.
subprocess.run(['systemctl', 'start', 'wpi-autotune.service'], check=True)
assert tuner.status(), 'The automatic controller did not publish a status report.'

old = 'wpi-old.example.com'
new = 'wpi-new.example.com'
alias = 'wpi-alias.example.com'
pma = 'wpi-db.example.com'

def local_certificate(self, domain, email, webroot):
    # Exercise genuine TLS vhosts using disposable self-signed certificates.
    # This replaces only public DNS ownership/ACME issuance, not web services.
    folder = self.live / domain
    if self.certificate_ready(domain):
        return
    folder.mkdir(parents=True, mode=0o755)
    subprocess.run([
        'openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-keyout', str(folder / 'privkey.pem'), '-out', str(folder / 'fullchain.pem'),
        '-subj', '/CN=' + domain, '-addext', 'subjectAltName=DNS:' + domain,
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (folder / 'privkey.pem').chmod(0o600)

def request(host, path='/', https=True, auth=False):
    with tempfile.TemporaryDirectory(prefix='wpi-http-ci-') as tmp:
        body = Path(tmp) / 'body'
        port = 443 if https else 80
        command = [
            'curl', '--silent', '--show-error', '--insecure', '--max-time', '30',
            '--noproxy', '*', '--resolve', f'{host}:{port}:127.0.0.1',
            '--dump-header', '-', '--output', str(body),
            '--write-out', '\nWPI_STATUS:%{http_code}\n',
        ]
        secret = None
        if auth:
            command += ['--config', '-']
            secret = 'user = "panel:CiPanelPassword123!"\n'
        command.append(f'{"https" if https else "http"}://{host}{path}')
        response = subprocess.run(command, input=secret, text=True, capture_output=True)
        status = re.search(r'WPI_STATUS:(\d+)', response.stdout)
        return (int(status[1]) if status else 0, response.stdout,
                body.read_text(errors='replace') if body.exists() else '', response.returncode)

def verify_redirect(host, target, https):
    # Nginx graceful reload acknowledges the signal before its new workers are
    # ready. Retry the exact expected route for at most two seconds.
    expected = f'https://{target}/article?x=1&y=2'
    for _ in range(10):
        status, headers, _, code = request(host, '/article?x=1&y=2', https=https)
        location = re.search(r'^location:\s*(.+?)\r?$', headers, re.I | re.M)
        if code == 0 and status == 301 and location and location[1].strip() == expected:
            return
        time.sleep(0.2)
    raise AssertionError((host, status, headers))

def expected_request(host, status, **kwargs):
    # Allow normal vhost readiness and FPM OPcache timestamp revalidation after
    # an atomic change to the managed Alias plugin (default interval: 2 sec).
    for _ in range(25):
        response = request(host, **kwargs)
        if response[0] == status and response[3] == 0:
            return response
        time.sleep(0.2)
    return response

def verify_denied(host, https):
    status, headers, body, code = request(host, https=https)
    assert status in (0, 403, 404, 410, 421) and 'wp-content' not in body, (status, headers)
    assert code in (0, 52, 56), code  # Nginx 444 closes the socket without an HTTP response.

def verify_real_fpm_congestion(site):
    """Real queued HTTP requests + real validated FPM growth.

    Only the RAM/CPU headroom and elapsed policy clock are deterministic
    fixtures. The web requests, private FastCGI status and FPM reload are real.
    This avoids treating a busy shared CI runner as stable production capacity.
    """
    subprocess.run(['systemctl', 'stop', 'wpi-autotune.timer'], check=True)
    slow = Path(site['root']) / 'wpi-ci-slow.php'
    try:
        with tuner._lock(blocking=True):
            old_children = tuner._state()['children']
            actual_resources = autotune.detect_resources()
            # Validate a ceiling above the old hard limit with real PHP-FPM.
            # Syntax checks do not signal/reload the running server or spawn
            # 512 workers; the fixture restores the original file immediately.
            original_pool = tuner.pool.read_bytes()
            try:
                larger_pool = autotune.render_pool(512, tuner.status_socket)
                assert 'pm.max_children = 512\n' in larger_pool
                startup = re.search(r'^pm.start_servers = (\d+)$', larger_pool, re.M)
                assert startup and int(startup[1]) <= 6
                autotune._atomic_write(tuner.pool, larger_pool, 0o644)
                subprocess.run([f'/usr/sbin/php-fpm{manager.config["php_version"]}', '-t'],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            finally:
                autotune._atomic_write(tuner.pool, original_pool, 0o644)
            print('Real PHP-FPM accepted pm.max_children=512 with at most 6 startup workers; '
                  'syntax-only validation, no 512-worker generation was activated.', flush=True)
            tuner._apply(2, actual_resources, update_ini=False)
            time.sleep(1)
            slow.write_text('<?php usleep(8000000); echo "wpi-ci-slow"; ?>', encoding='utf-8')
            slow.chmod(0o644)
            fixture = {
                **actual_resources,
                'memory_total': 8 * autotune.GIB,
                'memory_available': 6 * autotune.GIB,
                'cpus': 4.0, 'host_cpus': 4, 'cpu_load': 0.10,
                'memory_psi': 0.0, 'swap_delta': 0,
                'worker_rss': [64 * autotune.MIB], 'telemetry_ok': True,
            }
            state = {'children': 2, 'last_change': 0, 'saturation_ticks': 0,
                     'idle_ticks': 0, 'pressure_ticks': 0}
            try:
                with ThreadPoolExecutor(max_workers=6) as requests:
                    futures = [requests.submit(request, site['primary'], '/wpi-ci-slow.php')
                               for _ in range(6)]
                    # An independent status listener must remain responsive
                    # while both website workers are occupied and requests queue.
                    deadline = time.monotonic() + 5
                    maximum_queue, decision = 0, None
                    iteration = 0
                    while time.monotonic() < deadline:
                        status = autotune.read_fpm_status(tuner.status_socket)
                        # Native FPM JSON does not measure UNIX-listener queues.
                        # Read the actual kernel backlog using production code.
                        queue = autotune.read_socket_queue(
                            ci_run, Path(f'/run/php/php{manager.config["php_version"]}-fpm.sock'))
                        assert queue is not None, 'The UNIX listen queue could not be measured.'
                        status['listen queue'] = queue
                        status['queue_measured'] = True
                        maximum_queue = max(maximum_queue, status['listen queue'])
                        decision = autotune.decide(fixture, status, state, 1000 + iteration * 15)
                        state.update(decision)
                        if decision['children'] > 2:
                            break
                        iteration += 1
                        time.sleep(0.2)
                    assert maximum_queue > 0, 'Real FPM congestion did not expose a listen queue.'
                    assert decision and decision['children'] > 2, decision
                    grown = decision['children']
                    tuner._apply(grown, actual_resources, update_ini=False)
                    assert f'pm.max_children = {grown}\n' in tuner.pool.read_text()
                    # Graceful reload may wait for old sleeping requests to
                    # finish before new workers start. Wait within curl's bound.
                    deadline = time.monotonic() + 20
                    observed = 0
                    while time.monotonic() < deadline:
                        try:
                            observed = autotune.read_fpm_status(tuner.status_socket)['total processes']
                            if observed >= grown:
                                break
                        except (OSError, RuntimeError, ValueError):
                            pass
                        time.sleep(0.2)
                    assert observed >= grown, (grown, observed)
                    for future in futures:
                        status, _, body, code = future.result()
                        assert status == 200 and code == 0 and body == 'wpi-ci-slow', (status, code)
                print(f'Real FPM queue {maximum_queue}; workers grew 2 -> {grown}. '
                      'Policy RAM/CPU headroom and clock were injected CI fixtures.', flush=True)
            finally:
                tuner._apply(old_children, actual_resources, update_ini=False)
    finally:
        slow.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'start', 'wpi-autotune.timer'], check=True)


def reinstall_with_idle_panel():
    """Exercise the installed launcher while another real menu waits for input."""
    panel = subprocess.Popen(['/usr/local/bin/wpi'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             env={**os.environ, 'PYTHONUNBUFFERED': '1'})
    try:
        output = b''
        deadline = time.monotonic() + 10
        while b'Masukkan nomor:' not in output:
            remaining = deadline - time.monotonic()
            assert remaining > 0 and panel.poll() is None, 'Idle panel did not reach its menu.'
            ready, _, _ = select.select([panel.stdout], [], [], remaining)
            assert ready, 'Timed out waiting for the idle panel menu.'
            chunk = os.read(panel.stdout.fileno(), 65536)
            assert chunk, 'Panel exited before displaying its menu.'
            output += chunk
        for command in ('status', 'list', 'lock-status', 'redis-status'):
            subprocess.run(['/usr/local/bin/wpi', command], check=True,
                           text=True, capture_output=True, timeout=20)
        subprocess.run(['bash', 'install.sh', '--bundle', os.environ['WPI_CI_BUNDLE'],
                        '--sha256', os.environ['WPI_CI_BUNDLE_SHA']], check=True, timeout=120)
        assert panel.poll() is None, 'Application upgrade closed the existing idle panel.'
    finally:
        try:
            panel.communicate(input=b'0\n', timeout=10)
        except subprocess.TimeoutExpired:
            panel.terminate()
            panel.communicate(timeout=10)
    assert panel.returncode == 0, 'Idle panel did not close normally.'
    print('Real installed idle panel allowed status, list, and bootstrap upgrade; '
          'panel closed normally with option 0.', flush=True)


def cache_probe(site, key, expected=None):
    """A new real WP-CLI process reports only hit and value-match booleans."""
    script = "$found=false; $value=wp_cache_get(" + json.dumps(key) + ", 'wpi_ci', false, $found); "
    script += "echo json_encode(['persistent'=>wp_using_ext_object_cache(), 'found'=>$found, "
    script += "'matches'=>$value === " + json.dumps(expected) + "]);"
    return json.loads(manager.wp(site, 'eval', script).stdout)


def redis_failure_diagnostics():
    """Classify disposable Redis errors without printing config, ACL, or secrets."""
    try:
        inventory = subprocess.run(['systemctl', 'list-units', '--all', '--plain', '--no-legend',
                                    'wpi-redis@*.service'], capture_output=True, text=True, timeout=15)
        units = sorted(set(re.findall(r'wpi-redis@[a-f0-9]{12}\.service', inventory.stdout)) |
                       {'wpi-redis@' + site['id'] + '.service' for site in manager.sites()
                        if re.fullmatch('[a-f0-9]{12}', str(site.get('id', '')))})
        paths = [Path('/etc/wpi'), Path('/etc/wpi/redis')]
        patterns = {'acl_user_keyword': 'should start with user keyword',
                    'acl_load_error': 'error loading acl', 'config_error': 'fatal config file error',
                    'permission_denied': 'permission denied', 'missing_file': 'no such file',
                    'address_family': 'address family not supported', 'ready': 'ready to accept'}
        for unit in units:
            identifier = unit.split('@')[1].split('.')[0]
            properties = ('LoadState', 'ActiveState', 'SubState', 'Result', 'ExecMainStatus',
                          'ExecMainCode', 'User', 'Group', 'RuntimeDirectoryMode')
            state = subprocess.run(['systemctl', 'show', unit,
                                    *['--property=' + key for key in properties]],
                                   capture_output=True, text=True, timeout=15)
            fields = dict(line.split('=', 1) for line in state.stdout.splitlines() if '=' in line)
            journal = subprocess.run(['journalctl', '--unit', unit, '--no-pager', '--output=cat', '--lines=60'],
                                     capture_output=True, text=True, timeout=15).stdout.lower()
            counts = {key: journal.count(value) for key, value in patterns.items()}
            print('Safe Redis service diagnostic: ' + json.dumps({'unit': unit,
                  'state': {key: fields.get(key) for key in properties}, 'journal_categories': counts}), flush=True)
            paths.extend([Path('/etc/wpi/redis') / (identifier + suffix) for suffix in ('.conf', '.acl')])
            paths.append(Path('/run') / ('wpi-redis-' + identifier))
        permissions = []
        for path in paths:
            try:
                metadata = path.stat()
                permissions.append({'path': str(path), 'mode': oct(metadata.st_mode & 0o777),
                                    'uid': metadata.st_uid, 'gid': metadata.st_gid})
            except OSError:
                permissions.append({'path': str(path), 'missing': True})
        print('Safe Redis path permissions: ' + json.dumps(permissions), flush=True)
    except (OSError, subprocess.SubprocessError):
        print('Safe Redis diagnostics unavailable.', flush=True)


def install_with_redis_diagnostics(*args, **kwargs):
    try:
        return manager.install(*args, **kwargs)
    except Exception:
        redis_failure_diagnostics()
        raise


def verify_redis_cache(site):
    """Real plugin/Redis persistence, SQL reduction, isolation, and explicit recovery."""
    site = manager.site(site['id'])
    assert site['redis_cache']['enabled'], 'Fresh WordPress did not enable Redis automatically.'
    assert manager.wp(site, 'plugin', 'get', 'redis-cache', '--field=version').stdout.strip() == '3.0.0'
    assert manager.wp(site, 'eval', "echo wp_using_ext_object_cache() ? 'yes' : 'no';").stdout == 'yes'
    manager.wp(site, 'cache', 'set', 'wpi-cross-process', 'process-persistence', 'wpi_ci')
    assert cache_probe(site, 'wpi-cross-process', 'process-persistence') == {
        'persistent': True, 'found': True, 'matches': True}

    # A separate PHP-FPM request must see the key created by WP-CLI rather
    # than only its own process-local WordPress cache.
    probe = Path(site['root']) / 'wpi-ci-redis.php'
    probe.write_text("<?php require __DIR__ . '/wp-load.php'; header('Content-Type: application/json'); "
                     "$found=false; $v=wp_cache_get('wpi-cross-process','wpi_ci',false,$found); "
                     "echo json_encode(['persistent'=>wp_using_ext_object_cache(), "
                     "'found'=>$found,'matches'=>$v === 'process-persistence']);")
    probe.chmod(0o644)
    try:
        status, _, body, code = expected_request(site['primary'], 200, path='/wpi-ci-redis.php')
        assert status == 200 and code == 0
        assert json.loads(body) == {'persistent': True, 'found': True, 'matches': True}

        peer, _ = install_with_redis_diagnostics('wpi-cache-peer.example.com', 'owner@example.com',
                                               admin='cachepeer', password='CiPeerWordPressPassword123!')
        peer = manager.site(peer['id'])
        assert peer['redis_cache']['enabled']
        assert peer['redis_cache']['socket'] != site['redis_cache']['socket']
        manager.wp(peer, 'cache', 'set', 'wpi-peer-sentinel', 'keep-peer-cache', 'wpi_ci')
        subprocess.run(['/usr/local/bin/wpi', 'redis-flush', site['id']],
                       check=True, capture_output=True, text=True)
        assert cache_probe(site, 'wpi-cross-process')['found'] is False
        assert cache_probe(peer, 'wpi-peer-sentinel', 'keep-peer-cache')['matches'] is True

        # Twenty deliberately non-autoloaded options isolate query savings
        # from changing startup queries and shared-runner response timings.
        manager.wp(site, 'eval', "for ($i=0;$i<20;$i++) { add_option('wpi_ci_nonautoload_'.$i, "
                   "'fixture-'.$i, '', false); }")
        subprocess.run(['/usr/local/bin/wpi', 'redis-flush', site['id']],
                       check=True, capture_output=True, text=True)
        query_script = ("global $wpdb; $start=$wpdb->num_queries; $ok=true; "
                        "for ($i=0;$i<20;$i++) { $ok=$ok && "
                        "get_option('wpi_ci_nonautoload_'.$i) === 'fixture-'.$i; } "
                        "echo json_encode(['queries'=>$wpdb->num_queries-$start,'values_ok'=>$ok]);")
        cold = json.loads(manager.wp(site, 'eval', query_script).stdout)
        warm = json.loads(manager.wp(site, 'eval', query_script).stdout)
        assert cold['values_ok'] and warm['values_ok']
        assert cold['queries'] >= 20 and warm['queries'] == 0, (cold['queries'], warm['queries'])

        for current in (site, peer):
            socket = Path(current['redis_cache']['socket'])
            assert socket.is_socket(), 'Managed Redis UNIX socket is missing.'
            conf = (Path('/etc/wpi/redis') / (current['id'] + '.conf')).read_text()
            assert re.search(r'^port 0$', conf, re.M)
            assert re.search(r'^maxmemory-policy allkeys-lfu$', conf, re.M)
            credentials = manager.data / 'redis/credentials' / (current['id'] + '.json')
            assert credentials.stat().st_mode & 0o777 == 0o600
            assert manager.wp(current, 'eval',
                              "echo ((defined('WP_REDIS_GRACEFUL') && WP_REDIS_GRACEFUL) || "
                              "(defined('WP_REDIS_SELECTIVE_FLUSH') && WP_REDIS_SELECTIVE_FLUSH)) "
                              "? 'unsupported' : 'supported';").stdout == 'supported'
        listeners = subprocess.run(['ss', '-ltnp'], check=True, capture_output=True, text=True).stdout
        assert 'redis-server' not in listeners, 'Redis opened a TCP listener.'

        # Upstream Redis Object Cache reports connection failures. Diagnosis
        # and the resource timer must not silently reactivate a stopped service.
        unit = 'wpi-redis@' + site['id'] + '.service'
        subprocess.run(['systemctl', 'stop', unit], check=True)
        assert expected_request(site['primary'], 500)[0] == 500, 'Redis outage was not visible over HTTP.'
        diagnosis = manager.repair_site(site['id'], check_only=True)
        assert diagnosis['status'] == 'unresolved'
        subprocess.run(['/usr/local/bin/wpi', 'redis-status'], check=True, capture_output=True, text=True)
        subprocess.run(['/usr/local/bin/wpi', 'performance-tick'], check=True, capture_output=True, text=True)
        assert subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode != 0
        assert expected_request(peer['primary'], 200)[0] == 200, 'One Redis outage affected another instance.'
        recovered = manager.repair_site(site['id'])
        assert recovered['status'] == 'resolved'
        assert expected_request(site['primary'], 200)[0] == 200
        assert manager.wp(site, 'eval', "echo wp_using_ext_object_cache() ? 'yes' : 'no';").stdout == 'yes'
        manager.wp(site, 'cache', 'set', 'wpi-cross-process', 'process-persistence', 'wpi_ci')
        assert cache_probe(site, 'wpi-cross-process', 'process-persistence')['matches'] is True
        assert cache_probe(peer, 'wpi-peer-sentinel', 'keep-peer-cache')['matches'] is True

        # The user can disable cache, and optimize must respect that choice.
        enabled_snapshot = manager.backup(site['id'])
        subprocess.run(['/usr/local/bin/wpi', 'redis-disable', site['id']],
                       check=True, capture_output=True, text=True)
        subprocess.run(['/usr/local/bin/wpi', 'optimize'], check=True, capture_output=True, text=True)
        assert manager.site(site['id'])['redis_cache']['enabled'] is False
        assert expected_request(site['primary'], 200)[0] == 200
        disabled_restore, _ = manager.restore(site['id'], enabled_snapshot)
        assert disabled_restore['redis_cache']['enabled'] is False
        assert manager.site(site['id'])['redis_cache']['enabled'] is False
        assert manager.wp(site, 'eval', "echo wp_using_ext_object_cache() ? 'yes' : 'no';").stdout == 'no'
        assert expected_request(site['primary'], 200)[0] == 200
        assert cache_probe(peer, 'wpi-peer-sentinel', 'keep-peer-cache')['matches'] is True
        subprocess.run(['/usr/local/bin/wpi', 'redis-enable', site['id']],
                       check=True, capture_output=True, text=True)
        assert manager.site(site['id'])['redis_cache']['enabled'] is True
        manager.wp(site, 'cache', 'set', 'wpi-cross-process', 'process-persistence', 'wpi_ci')
        assert cache_probe(site, 'wpi-cross-process', 'process-persistence')['matches'] is True
        print(f'Real Redis Object Cache 3.0.0: WP-CLI/PHP persistence; '
              f'20 option reads SQL cold={cold["queries"]}, warm={warm["queries"]}; '
              'two-site flush isolation, UNIX-only listeners, visible outage and explicit repair recovery; '
              'disable respected by optimize and restore of an enabled snapshot.', flush=True)
        return peer
    finally:
        probe.unlink(missing_ok=True)


def verify_runcloud_domains(site, post):
    """Real WordPress Alias access, promotion and domain-only deletion."""
    primary = site['primary']
    live_alias = 'wpi-live.example.com'
    paired_alias = 'wpi-pair.example.com'
    paired_redirect = 'wpi-forward.example.com'
    slug = manager.wp(site, 'post', 'get', post, '--field=post_name').stdout.strip()
    path = '/' + slug + '/'
    manager.add_domain(site['id'], live_alias, kind='alias')
    manager.add_domain(site['id'], paired_alias, kind='alias', www=True)
    for host in (live_alias, paired_alias, 'www.' + paired_alias):
        status, headers, body, code = expected_request(host, 200, path=path)
        assert status == 200 and code == 0 and 'Domain regression' in body, (host, status)
        assert f'https://{host}{path}' in body, 'Alias canonical URL did not keep its hostname.'
        assert not re.search(r'^location:', headers, re.I | re.M), headers
        verify_redirect(host, host, https=False)
    # Must-use URL filters never alter the persistent primary or WP-CLI work.
    assert manager.wp(site, 'option', 'get', 'home').stdout.strip() == 'https://' + primary
    manager.add_domain(site['id'], paired_redirect, kind='redirect', www=True)
    for host in (paired_redirect, 'www.' + paired_redirect):
        verify_redirect(host, primary, https=True)
    tables_before = manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout
    promoted, _ = manager.set_primary(site['id'], live_alias)
    assert promoted['primary'] == live_alias and primary in promoted['aliases']
    assert live_alias not in promoted['aliases']
    for option in ('home', 'siteurl'):
        assert manager.wp(promoted, 'option', 'get', option).stdout.strip() == 'https://' + live_alias
    assert f'https://{live_alias}/inside' in manager.wp(promoted, 'post', 'get', post, '--field=post_content').stdout
    assert expected_request(primary, 200, path=path)[0] == 200, 'Former primary Alias became a redirect.'
    for host in (paired_redirect, 'www.' + paired_redirect):
        verify_redirect(host, live_alias, https=True)
    try:
        manager.remove_domain(site['id'], live_alias)
        raise AssertionError('Deleting the active primary was allowed.')
    except ValueError:
        pass
    manager.remove_domain(site['id'], primary)
    time.sleep(0.2)
    verify_denied(primary, https=False)
    verify_denied(primary, https=True)
    assert manager.wp(promoted, 'db', 'tables', '--all-tables-with-prefix').stdout == tables_before
    manager.wp(promoted, 'core', 'is-installed')
    assert expected_request(live_alias, 200, path=path)[0] == 200
    # Return to the existing fixture's primary through the same public flow.
    manager.add_domain(site['id'], primary, kind='alias')
    restored, _ = manager.set_primary(site['id'], primary)
    for host in (live_alias, paired_alias, 'www.' + paired_alias, paired_redirect, 'www.' + paired_redirect):
        manager.remove_domain(site['id'], host)
    assert expected_request(primary, 200, path=path)[0] == 200
    print('RunCloud-style domains passed: Alias permalink HTTP 200, www pair, Redirect 301, '
          'existing Alias promoted with WordPress URL replacement, old domain deleted, '
          'primary deletion refused, and database preserved.', flush=True)
    return manager.site(restored['id'])


def verify_repair_and_php_settings(site):
    """Actual HTTP 500 recovery and runtime/upload settings through installed CLI."""
    root = Path(site['root'])
    manager.wp(site, 'option', 'add', 'wpi_repair_sentinel', 'keep-database-content')
    users = manager.wp(site, 'user', 'list', '--field=ID').stdout
    tables = manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout
    media = root / 'wp-content/uploads/wpi-repair-proof.txt'
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_text('retain media during config-only repair')
    media_hash = manager.file_hash(media)
    config = root / 'wp-config.php'
    baseline = config.read_bytes()
    snapshots = list((manager.data / 'config-snapshots' / site['id']).glob('*/COMPLETE'))
    assert snapshots, 'Normal installation did not create a validated config snapshot.'
    config.write_bytes(baseline + b"\ndefine('WP_MEMORY_LIMIT', '500M'\n")
    subprocess.run(['systemctl', 'restart', f'php{manager.config["php_version"]}-fpm'], check=True)
    assert expected_request(site['primary'], 500)[0] == 500, 'Broken config did not cause a genuine HTTP 500.'
    diagnosis = manager.repair_site(site['id'], check_only=True)
    assert diagnosis['status'] == 'unresolved' and not diagnosis['checks']['config_syntax']
    broken = config.read_bytes()
    report = manager.repair_site(site['id'])
    assert report['status'] == 'resolved', report
    assert report['before']['frontend']['status'] == 500 and report['checks']['frontend']['status'] == 200
    assert not report['database_restored'] and not report['content_restored']
    assert any(action['action'] == 'config_recovered' for action in report['actions']), report
    preserved = Path(report['preserved_config']) / 'wp-config.php'
    assert preserved.read_bytes() == broken and preserved.stat().st_mode & 0o777 == 0o600
    assert manager.wp(site, 'option', 'get', 'wpi_repair_sentinel').stdout.strip() == 'keep-database-content'
    assert manager.wp(site, 'user', 'list', '--field=ID').stdout == users
    assert manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout == tables
    assert manager.file_hash(media) == media_hash
    assert request(site['primary'], '/wp-admin/')[0] in (200, 302)
    print('Real HTTPS HTTP 500 from malformed wp-config recovered to HTTP 200; '
          'admin users, database content, media, and private broken-config backup preserved.', flush=True)

    # Apply through the real installed command, then measure actual PHP-FPM.
    subprocess.run(['/usr/local/bin/wpi', 'php-settings', '--memory-limit', '500',
                    '--upload-max-filesize', '8'], check=True, capture_output=True, text=True)
    probe = root / 'wpi-ci-limits.php'
    probe.write_text("<?php require __DIR__ . '/wp-load.php'; header('Content-Type: application/json'); "
                     "echo json_encode(['memory'=>ini_get('memory_limit'), "
                     "'upload'=>ini_get('upload_max_filesize'), 'post'=>ini_get('post_max_size'), "
                     "'wp_memory'=>WP_MEMORY_LIMIT, 'wp_max'=>WP_MAX_MEMORY_LIMIT, "
                     "'upload_error'=>$_FILES['sample']['error'] ?? null]);")
    probe.chmod(0o644)
    try:
        for _ in range(30):
            status, _, body, code = request(site['primary'], '/wpi-ci-limits.php')
            values = json.loads(body) if status == 200 and code == 0 else {}
            if values.get('memory') == '500M' and values.get('upload') == '8M':
                break
            time.sleep(0.2)
        assert values == {'memory': '500M', 'upload': '8M', 'post': '16M',
                          'wp_memory': '500M', 'wp_max': '500M', 'upload_error': None}, values
        with tempfile.TemporaryDirectory(prefix='wpi-upload-ci-') as tmp:
            upload = Path(tmp) / 'sample.bin'
            for size, expected_status, expected_error in ((1, 200, 0), (9, 200, 1), (17, 413, None)):
                with upload.open('wb') as output:
                    output.truncate(size * autotune.MIB)
                result = subprocess.run(['curl', '--silent', '--insecure', '--noproxy', '*',
                                         '--max-time', '20', '--resolve',
                                         f'{site["primary"]}:443:127.0.0.1', '--write-out', '\n%{http_code}',
                                         '-F', f'sample=@{upload}',
                                         f'https://{site["primary"]}/wpi-ci-limits.php'],
                                        capture_output=True, text=True, check=True)
                body, http = result.stdout.rsplit('\n', 1)
                assert int(http) == expected_status, (size, http)
                if expected_error is not None:
                    assert json.loads(body)['upload_error'] == expected_error, (size, body)
        manager.autotune_tick()
        assert manager.php_settings_status()['effective']['memory_mib'] == 500
        reinstall_with_idle_panel()
        assert manager.config['php_settings'] == {'memory_limit_mb': 500, 'upload_max_filesize_mb': 8}
        assert manager.php_settings_status()['effective']['memory_mib'] == 500
        subprocess.run(['/usr/local/bin/wpi', 'php-settings', '--reset'], check=True,
                       text=True, capture_output=True)
        assert manager.config['php_settings'] == {}
        assert manager.php_settings_status()['manual'] == {}
        automatic = manager.php_settings_status()['effective']
        for _ in range(30):
            status, _, body, code = request(site['primary'], '/wpi-ci-limits.php')
            values = json.loads(body) if status == 200 and code == 0 else {}
            if values.get('memory') == f"{automatic['memory_mib']}M" \
                    and values.get('upload') == f"{automatic['upload_mib']}M" \
                    and values.get('post') == f"{automatic['post_mib']}M":
                break
            time.sleep(0.2)
        assert values.get('memory') == f"{automatic['memory_mib']}M", values
        assert values.get('upload') == f"{automatic['upload_mib']}M", values
        assert values.get('post') == f"{automatic['post_mib']}M", values
        print('Actual FPM/WordPress 500M, upload 8M, POST/web 16M verified; '
              'small upload accepted, PHP oversize error, web HTTP 413, '
              'autotune/upgrade persistence and automatic reset passed.', flush=True)
    finally:
        probe.unlink(missing_ok=True)


with mock.patch.object(core, 'check_dns', return_value=None), \
     mock.patch.object(WebStack, 'obtain_certificate', local_certificate):
    site, _ = install_with_redis_diagnostics(old, 'owner@example.com', admin='ciadmin', password='CiWordPressPassword123!')
    manager.wp(site, 'core', 'is-installed')
    status, _, body, code = expected_request(old, 200)
    assert code == 0 and status == 200 and 'wp-content' in body, (status, body[:200])
    assert request(old, '/wp-config.php')[0] == 403
    assert request(old, autotune.STATUS_PATH)[0] == 403
    verify_redirect(old, old, https=False)
    verify_real_fpm_congestion(site)
    cache_peer = verify_redis_cache(site)
    verify_repair_and_php_settings(site)

    # A PHP array option is stored serialized by WordPress. The replacement
    # must retain its structure while changing all canonical URL variants.
    payload = {
        'url': f'https://{old}/inside',
        'nested': [f'http://{old}/legacy', f'//{old}/image.png'],
        'unrelated': f'https://{old}.evil.example/path',
    }
    manager.wp(site, 'option', 'add', 'wpi_ci_serialized', json.dumps(payload), '--format=json')
    escaped_text = '{"url":"https:\\/\\/' + old + '/escaped"}'
    manager.wp(site, 'option', 'add', 'wpi_ci_json_text', escaped_text)
    post = manager.wp(site, 'post', 'create', '--post_status=publish', '--post_title=Domain regression',
                      '--post_content=' + f'<a href="https://{old}/inside">test</a>', '--porcelain').stdout.strip()

    manager.add_secondary(site['id'], alias)
    verify_redirect(alias, old, https=False)
    verify_redirect(alias, old, https=True)
    # ACME exception remains usable even when the hostname redirects.
    acme = Path(site['root']) / '.well-known' / 'acme-challenge' / 'wpi-ci-token'
    acme.parent.mkdir(parents=True, exist_ok=True)
    acme.write_text('ci-acme-proof')
    assert request(alias, '/.well-known/acme-challenge/wpi-ci-token', https=False)[2] == 'ci-acme-proof'

    changed, snapshot = manager.change_primary(site['id'], new)
    assert changed['primary'] == new and old not in changed['tls']
    for option in ('home', 'siteurl'):
        assert manager.wp(changed, 'option', 'get', option).stdout.strip() == 'https://' + new
    value = json.loads(manager.wp(changed, 'option', 'get', 'wpi_ci_serialized', '--format=json').stdout)
    assert value == {'url': f'https://{new}/inside', 'nested': [f'https://{new}/legacy', f'//{new}/image.png'],
                     'unrelated': f'https://{old}.evil.example/path'}, value
    text = manager.wp(changed, 'post', 'get', post, '--field=post_content').stdout
    assert f'https://{new}/inside' in text and f'https://{old}/inside' not in text, text
    escaped = manager.wp(changed, 'option', 'get', 'wpi_ci_json_text').stdout.strip()
    assert escaped == '{"url":"https:\\/\\/' + new + '/escaped"}', escaped
    assert expected_request(new, 200)[0] == 200
    verify_redirect(alias, new, https=True)
    verify_denied(old, https=False)
    verify_denied(old, https=True)

    # Reintroducing an old domain through restore must restore HTTPS as well
    # as SQL/files. The safety snapshot preserves the state before restore.
    # Self-signed CI certs have no Certbot renewal lineage; emulate completed
    # removal so restore must request and activate the missing old certificate.
    shutil.rmtree(manager.web.live / old, ignore_errors=True)
    restored, _ = manager.restore(changed['id'], snapshot)
    assert restored['primary'] == old
    assert manager.wp(restored, 'option', 'get', 'siteurl').stdout.strip() == 'https://' + old
    assert expected_request(old, 200)[0] == 200
    restored = verify_runcloud_domains(restored, post)
    assert manager.wp(restored, 'eval', "echo wp_using_ext_object_cache() ? 'yes' : 'no';").stdout == 'yes'
    manager.wp(restored, 'cache', 'set', 'wpi-after-restore', 'restore-persistence', 'wpi_ci')
    assert cache_probe(restored, 'wpi-after-restore', 'restore-persistence')['matches'] is True
    assert cache_probe(cache_peer, 'wpi-peer-sentinel', 'keep-peer-cache')['matches'] is True
    print('Redis remained usable after backup restore, URL replacement, and Alias promotion; '
          'the other site cache was retained.', flush=True)

    tables_before = manager.wp(restored, 'db', 'tables', '--all-tables-with-prefix').stdout
    manager.install_pma(pma, 'owner@example.com', password='CiPanelPassword123!')
    assert expected_request(pma, 401)[0] == 401  # Basic Auth precedes the phpMyAdmin cookie login.
    status, _, body, _ = expected_request(pma, 200, auth=True)
    assert status == 200 and 'phpMyAdmin' in body, (status, body[:200])
    assert request(pma, autotune.STATUS_PATH, auth=True)[0] == 403
    verify_redirect(pma, pma, https=False)
    # Public ACME tokens bypass Basic Auth while normal UI remains protected.
    pma_acme = core.WWW / 'pma-acme' / '.well-known' / 'acme-challenge' / 'wpi-pma-token'
    pma_acme.parent.mkdir(parents=True, exist_ok=True)
    pma_acme.write_text('ci-pma-proof')
    assert request(pma, '/.well-known/acme-challenge/wpi-pma-token', https=False)[2] == 'ci-pma-proof'
    manager.remove_pma()
    manager.wp(restored, 'core', 'is-installed')
    assert manager.wp(restored, 'db', 'tables', '--all-tables-with-prefix').stdout == tables_before
    assert request(old)[0] == 200
    verify_denied(pma, https=False)
    verify_denied(pma, https=True)
    manager.web.validate_reload()
    # Controller telemetry is expected to change every 15 seconds. Site state,
    # credentials and all other managed data must remain byte-for-byte intact.
    def persistent_state():
        return {str(path.relative_to(manager.data)): path.read_bytes()
                for path in manager.data.rglob('*') if path.is_file()
                and path.relative_to(manager.data).parts[0] != 'autotune'
                and path.relative_to(manager.data).as_posix() != 'operation.lock'}
    state_before = persistent_state()
    reinstall_with_idle_panel()
    state_after = persistent_state()
    if state_after != state_before:
        before_paths, after_paths = set(state_before), set(state_after)
        changed = {'added': sorted(after_paths - before_paths),
                   'removed': sorted(before_paths - after_paths),
                   'changed': sorted(path for path in before_paths & after_paths
                                     if state_before[path] != state_after[path])}
        # Relative names only: never emit bytes, hashes, credentials, SQL, or
        # config contents from the persistent state snapshots.
        print('Safe reinstall persistent-state path changes: ' + json.dumps(changed), flush=True)
    assert state_after == state_before, 'Application reinstall changed managed site state or credentials.'
    version = subprocess.run(['/usr/local/bin/wpi', '--version'], check=True, text=True, capture_output=True)
    assert version.stdout.strip() == os.environ['WPI_CI_VERSION']
    release_path = Path('/usr/local/lib/wpi').resolve()
    expected_version = 'v' + os.environ['WPI_CI_VERSION']
    assert (release_path / 'VERSION').read_text().strip() == expected_version
    assert release_path.name.startswith(expected_version + '.'), release_path.name
    general_status = subprocess.run(['/usr/local/bin/wpi', 'status'], check=True,
                                    text=True, capture_output=True)
    assert 'autotune' in general_status.stdout.lower(), general_status.stdout
    tuning_status = subprocess.run(['/usr/local/bin/wpi', 'autotune-status'], check=True,
                                   text=True, capture_output=True)
    assert tuning_status.stdout.strip(), 'CLI autotune status is empty.'
    assert tuner.status()['enabled'] is True
    listing = subprocess.run(['/usr/local/bin/wpi', 'list'], check=True, text=True, capture_output=True)
    assert old in listing.stdout, listing.stdout
    print(f'Integration passed: Ubuntu {manager.config["ubuntu"]}, {stack}, {database}, PHP {manager.config["php_version"]}.')
    print('TLS routing tested with self-signed certificates; public DNS/ACME issuance was not tested.')
PY
