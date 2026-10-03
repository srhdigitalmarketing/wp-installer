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
systemctl disable --now nginx apache2 mysql mariadb >/dev/null 2>&1 || true
mapfile -t SERVER_PACKAGES < <(dpkg-query -W -f='${Package}\n' \
    'nginx*' 'apache2*' 'mysql-server*' 'mysql-client*' 'mysql-common' \
    'mariadb-server*' 'mariadb-client*' 'mariadb-common' 'phpmyadmin' 2>/dev/null || true)
if ((${#SERVER_PACKAGES[@]})); then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y "${SERVER_PACKAGES[@]}"
fi
rm -rf -- /etc/nginx /etc/apache2 /etc/mysql /etc/phpmyadmin /etc/letsencrypt \
    /var/lib/mysql /var/www/wpi /var/lib/wpi /var/backups/wpi /etc/wpi

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
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

# Run integration against the package actually installed by bootstrap.
sys.path.insert(0, '/usr/local/lib/wpi')
from wpi import core
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
    for _ in range(10):
        response = request(host, **kwargs)
        if response[0] == status and response[3] == 0:
            return response
        time.sleep(0.2)
    return response

def verify_denied(host, https):
    status, headers, body, code = request(host, https=https)
    assert status in (0, 403, 404, 410, 421) and 'wp-content' not in body, (status, headers)
    assert code in (0, 52, 56), code  # Nginx 444 closes the socket without an HTTP response.

with mock.patch.object(core, 'check_dns', return_value=None), \
     mock.patch.object(WebStack, 'obtain_certificate', local_certificate):
    site, _ = manager.install(old, 'owner@example.com', admin='ciadmin', password='CiWordPressPassword123!')
    manager.wp(site, 'core', 'is-installed')
    status, _, body, code = expected_request(old, 200)
    assert code == 0 and status == 200 and 'wp-content' in body, (status, body[:200])
    assert request(old, '/wp-config.php')[0] == 403
    verify_redirect(old, old, https=False)

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

    tables_before = manager.wp(restored, 'db', 'tables', '--all-tables-with-prefix').stdout
    manager.install_pma(pma, 'owner@example.com', password='CiPanelPassword123!')
    assert expected_request(pma, 401)[0] == 401  # Basic Auth precedes the phpMyAdmin cookie login.
    status, _, body, _ = expected_request(pma, 200, auth=True)
    assert status == 200 and 'phpMyAdmin' in body, (status, body[:200])
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
    state_before = {str(path.relative_to(manager.data)): path.read_bytes()
                    for path in manager.data.rglob('*') if path.is_file()}
    subprocess.run(['bash', 'install.sh', '--bundle', os.environ['WPI_CI_BUNDLE'],
                    '--sha256', os.environ['WPI_CI_BUNDLE_SHA']], check=True)
    state_after = {str(path.relative_to(manager.data)): path.read_bytes()
                   for path in manager.data.rglob('*') if path.is_file()}
    assert state_after == state_before, 'Application reinstall changed managed site state or credentials.'
    version = subprocess.run(['/usr/local/bin/wpi', '--version'], check=True, text=True, capture_output=True)
    assert version.stdout.strip() == os.environ['WPI_CI_VERSION']
    release_path = Path('/usr/local/lib/wpi').resolve()
    expected_version = 'v' + os.environ['WPI_CI_VERSION']
    assert (release_path / 'VERSION').read_text().strip() == expected_version
    assert release_path.name.startswith(expected_version + '.'), release_path.name
    subprocess.run(['/usr/local/bin/wpi', 'status'], check=True)
    listing = subprocess.run(['/usr/local/bin/wpi', 'list'], check=True, text=True, capture_output=True)
    assert old in listing.stdout, listing.stdout
    print(f'Integration passed: Ubuntu {manager.config["ubuntu"]}, {stack}, {database}, PHP {manager.config["php_version"]}.')
    print('TLS routing tested with self-signed certificates; public DNS/ACME issuance was not tested.')
PY
