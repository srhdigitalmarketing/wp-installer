#!/usr/bin/env bash
# Destructive integration fixture; never run against an existing VPS.
# Two separate Ubuntu/systemd containers preserve real network, SSH, sudo,
# database, PHP-FPM, nginx, and scheduled-controller boundaries.
set -Eeuo pipefail

[[ "${CI:-}" == true && "${WPI_DISPOSABLE_VM:-}" == 1 ]] || {
    printf 'Requires CI=true and WPI_DISPOSABLE_VM=1 on a disposable Docker runner.\n' >&2
    exit 1
}
[[ -f wpi/core.py && -f wpi/migrate.py && -f wpi/migrate_target.py ]] || {
    printf 'Run this test from the WPI repository.\n' >&2
    exit 1
}
command -v docker >/dev/null || { printf 'Docker is required.\n' >&2; exit 1; }

WPI_CI_WORK="$(mktemp -d -t wpi-migration-ci.XXXXXXXX)"
WPI_CI_SUFFIX="$(basename "$WPI_CI_WORK" | tr '[:upper:]' '[:lower:]')"
WPI_CI_IMAGE="${WPI_CI_SUFFIX}:fixture"
WPI_CI_SOURCE="${WPI_CI_SUFFIX}-source"
WPI_CI_TARGET="${WPI_CI_SUFFIX}-target"
cleanup() {
    docker rm --force "$WPI_CI_SOURCE" "$WPI_CI_TARGET" >/dev/null 2>&1 || true
    docker image rm "$WPI_CI_IMAGE" >/dev/null 2>&1 || true
    [[ "$WPI_CI_WORK" == /tmp/wpi-migration-ci.* ]] && rm -rf -- "$WPI_CI_WORK"
}
trap cleanup EXIT

cat > "$WPI_CI_WORK/Dockerfile" <<'DOCKERFILE'
FROM ubuntu:24.04
ENV container=docker DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
RUN apt-get update && apt-get install -y --no-install-recommends \
    systemd systemd-sysv dbus openssh-server sudo python3 curl ca-certificates \
    unzip openssl iproute2 procps mount && \
    useradd --create-home --shell /bin/bash migrator && \
    printf 'migrator ALL=(ALL:ALL) ALL\n' > /etc/sudoers.d/wpi-migration-ci && \
    chmod 0440 /etc/sudoers.d/wpi-migration-ci && \
    mkdir -p /etc/ssh/sshd_config.d && \
    printf 'PasswordAuthentication yes\nPermitRootLogin yes\nUsePAM yes\n' \
      > /etc/ssh/sshd_config.d/00-wpi-migration-ci.conf && \
    systemctl enable ssh && \
    rm -f /etc/ssh/ssh_host_* /etc/machine-id /var/lib/dbus/machine-id && \
    ln -s /etc/machine-id /var/lib/dbus/machine-id && \
    rm -rf /var/lib/apt/lists/*
STOPSIGNAL SIGRTMIN+3
CMD ["/bin/bash", "-c", "dbus-uuidgen --ensure=/etc/machine-id && ssh-keygen -A >/dev/null && mount -o remount,rw /sys/fs/cgroup && exec /sbin/init"]
DOCKERFILE
docker build --tag "$WPI_CI_IMAGE" "$WPI_CI_WORK"
for WPI_CI_CONTAINER in "$WPI_CI_SOURCE" "$WPI_CI_TARGET"; do
    docker run --detach --privileged --cgroupns=private --memory=2048m --cpus=2 \
        --tmpfs /run --tmpfs /run/lock \
        --name "$WPI_CI_CONTAINER" "$WPI_CI_IMAGE" >/dev/null
    for WPI_CI_ATTEMPT in {1..60}; do
        if docker exec "$WPI_CI_CONTAINER" systemctl is-active --quiet ssh; then
            break
        fi
        if [[ "$WPI_CI_ATTEMPT" -eq 60 ]]; then
            printf 'SSH did not start.\n' >&2
            # Boot happens before any fixture password or application data.
            docker logs --tail 40 "$WPI_CI_CONTAINER" >&2 || true
            exit 1
        fi
        sleep 1
    done
done

# Apt can bake /var/lib/dbus/machine-id into a shared image. Remove that ID
# above and generate each server's identity before systemd starts, preserving
# the real product guard against migrating onto the source server itself.
WPI_CI_SOURCE_ID="$(docker exec "$WPI_CI_SOURCE" cat /etc/machine-id)"
WPI_CI_TARGET_ID="$(docker exec "$WPI_CI_TARGET" cat /etc/machine-id)"
[[ "$WPI_CI_SOURCE_ID" =~ ^[a-f0-9]{32}$ && "$WPI_CI_TARGET_ID" =~ ^[a-f0-9]{32}$ \
    && "$WPI_CI_SOURCE_ID" != "$WPI_CI_TARGET_ID" ]] || {
    printf 'Disposable servers need distinct valid machine IDs.\n' >&2
    exit 1
}
printf 'Two isolated servers have distinct machine identities.\n'

WPI_CI_SOURCE_IP="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$WPI_CI_SOURCE")"
WPI_CI_TARGET_IP="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$WPI_CI_TARGET")"
[[ -n "$WPI_CI_SOURCE_IP" && -n "$WPI_CI_TARGET_IP" && "$WPI_CI_SOURCE_IP" != "$WPI_CI_TARGET_IP" ]] || exit 1

# A disposable secret travels over the real encrypted password-authenticated
# SSH transport. It is never printed, put in argv, or saved in repository files.
WPI_CI_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
printf 'migrator:%s\n' "$WPI_CI_PASSWORD" | docker exec --interactive "$WPI_CI_TARGET" chpasswd
printf 'migrator:%s\n' "$WPI_CI_PASSWORD" | docker exec --interactive "$WPI_CI_SOURCE" chpasswd
printf 'root:%s\n' "$WPI_CI_PASSWORD" | docker exec --interactive "$WPI_CI_TARGET" chpasswd
printf '%s' "$WPI_CI_PASSWORD" | docker exec --interactive "$WPI_CI_SOURCE" \
    bash -c 'umask 077; cat > /root/migration-password'
unset WPI_CI_PASSWORD

tar -czf "$WPI_CI_WORK/repository.tar.gz" wpi scripts/build_release.py install.sh README.md LICENSE
docker cp "$WPI_CI_WORK/repository.tar.gz" "$WPI_CI_SOURCE:/root/repository.tar.gz"
docker exec "$WPI_CI_SOURCE" bash -c \
    'mkdir -p /opt/wpi-repository; tar -xzf /root/repository.tar.gz -C /opt/wpi-repository'
docker exec "$WPI_CI_SOURCE" bash -c \
    'cd /opt/wpi-repository && python3 scripts/build_release.py && bundle="$(find dist -maxdepth 1 -name "wp-installer-v*.zip" -print -quit)" && bash install.sh --bundle "$bundle"'

# Seed the source key out of band. The target's first root connection below
# exercises accept-new pinning; the later non-root session reuses that pin.
for WPI_CI_IP in "$WPI_CI_SOURCE_IP"; do
    docker exec "$WPI_CI_SOURCE" bash -c \
        'mkdir -m 700 -p /var/lib/wpi/ssh; ssh-keyscan -H "$1" >> /var/lib/wpi/ssh/known_hosts 2>/dev/null; chmod 600 /var/lib/wpi/ssh/known_hosts' \
        _ "$WPI_CI_IP"
done

cat > "$WPI_CI_WORK/source-fixture.py" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

sys.path.insert(0, '/usr/local/lib/wpi')
from wpi import core
from wpi.web import WebStack

HOSTS = ['wpi-primary.example.com', 'wpi-alias.example.com', 'wpi-redirect.example.com']
def certificate(self, host, email, webroot):
    if self.certificate_ready(host):
        return
    folder = self.live / host
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                    '-keyout', str(folder / 'privkey.pem'), '-out', str(folder / 'fullchain.pem'),
                    '-subj', '/CN=' + host, '-addext', 'subjectAltName=DNS:' + host],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (folder / 'privkey.pem').chmod(0o600)

manager = core.Manager()
manager.setup('nginx', 'mariadb')
with mock.patch('wpi.core.check_dns'), mock.patch.object(WebStack, 'obtain_certificate', certificate):
    site, password = manager.install(HOSTS[0], 'migration-ci@example.com',
                                     title='WPI encrypted migration fixture',
                                     admin='migration_admin', password='FixtureAdminPassword123!')
    manager.add_domain(site['id'], HOSTS[1], kind='alias')
    manager.add_domain(site['id'], HOSTS[2], kind='redirect')
site = manager.site(site['id'])
post = manager.wp(site, 'post', 'create', '--post_type=post', '--post_status=publish',
                  '--post_title=Migration preserved article', '--post_name=migration-preserved',
                  '--post_content=Migration preserved content', '--porcelain').stdout.strip()
manager.wp(site, 'rewrite', 'flush', '--hard')
upload = Path(site['root']) / 'wp-content/uploads/migration-fixture.bin'
upload.parent.mkdir(exist_ok=True, parents=True)
upload.write_bytes(os.urandom(128 * 1024))
subprocess.run(['chown', '-R', 'www-data:www-data', str(upload.parent)], check=True)
manifest = {'site': site, 'post': post,
            'upload_sha256': hashlib.sha256(upload.read_bytes()).hexdigest(),
            'database_password_hash': hashlib.sha256(json.loads(
                (manager.data / 'credentials' / (site['id'] + '.json')).read_text()
            )['database_password'].encode()).hexdigest(),
            'admin_hash': manager.wp(site, 'user', 'get', 'migration_admin', '--field=user_pass').stdout.strip(),
            'tables': manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout.strip(),
            'ssl_hashes': {host: hashlib.sha256((manager.web.live / host / 'fullchain.pem').read_bytes()).hexdigest()
                           for host in HOSTS}}
Path('/root/source-manifest.json').write_text(json.dumps(manifest))
print('Source fixture installed: WordPress, content, administrator, upload, Alias, and Redirect.', flush=True)
PY
docker cp "$WPI_CI_WORK/source-fixture.py" "$WPI_CI_SOURCE:/root/source-fixture.py"
docker exec "$WPI_CI_SOURCE" python3 -u /root/source-fixture.py

# Before cutover these hostnames resolve to the old server from both servers.
for WPI_CI_CONTAINER in "$WPI_CI_SOURCE" "$WPI_CI_TARGET"; do
    docker exec "$WPI_CI_CONTAINER" bash -c \
        'printf "%s wpi-primary.example.com wpi-alias.example.com wpi-redirect.example.com\n" "$1" >> /etc/hosts' \
        _ "$WPI_CI_SOURCE_IP"
done

cat > "$WPI_CI_WORK/migrate-fixture.py" <<'PY'
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, '/usr/local/lib/wpi')
from wpi.core import Manager
from wpi.migrate import Migration
from wpi.ssh import SSHSession

password = Path('/root/migration-password').read_text()
source, target = sys.argv[1:]
migrator = Migration(Manager())
def root_probe_runner(argv, **kwargs):
    # Diagnose only the first harmless id command. Authentication commands,
    # upload payloads, later privileged scripts, argv, and stdin stay private.
    probe = argv[0] == 'ssh' and argv[-1] == "sh -c 'id -u'"
    result = subprocess.run([argv[0], '-vvv', *argv[1:]] if probe else argv, **kwargs)
    if probe and result.returncode:
        print('Disposable SSH root identity probe failed; bounded OpenSSH diagnostic:', flush=True)
        print((result.stderr or '').replace(password, '[redacted]')[-8000:], flush=True)
    return result
with SSHSession(target, 'root', password, runner=root_probe_runner) as session:
    assert session.run_root('id -u').stdout.strip() == '0'
    fixture = Path('/root/ssh-upload-fixture.txt')
    fixture.write_text('WPI encrypted root transfer fixture')
    session.upload(fixture, '/tmp/wpi-ssh-upload-fixture.txt')
    assert session.run_root('cat /tmp/wpi-ssh-upload-fixture.txt').stdout == fixture.read_text()
    session.run_root('rm -f /tmp/wpi-ssh-upload-fixture.txt')
    # Fail the first migration only after data import, at managed SSL unit
    # activation. A retry must reuse this snapshot and upload into the same
    # transfer directory, whose previous payloads became root-owned.
    session.run_root("printf '# Unmanaged CI fixture\\n' > /etc/systemd/system/wpi-migration-ssl.service")
assert subprocess.run(['ssh-keygen', '-F', target, '-f', '/var/lib/wpi/ssh/known_hosts'],
                      capture_output=True).returncode == 0
negative = Path('/var/lib/wpi/negative-host-pin')
(negative / 'ssh').mkdir(parents=True, mode=0o700)
wrong_key = Path('/etc/ssh/ssh_host_ed25519_key.pub').read_text().split()
(negative / 'ssh/known_hosts').write_text(f'{target} {wrong_key[0]} {wrong_key[1]}\n')
try:
    with SSHSession(target, 'root', password, data_dir=negative):
        raise AssertionError('A changed target host key was accepted.')
except RuntimeError as exc:
    assert password not in str(exc)
print('Real root password SSH login and encrypted upload passed.', flush=True)
print('First target host key pinned; changed host key rejected before password login.', flush=True)
with SSHSession(target, 'migrator', password) as session:
    assert session.run_root('id -u').stdout.strip() == '0'
print('Real non-root password SSH login and password sudo passed.', flush=True)
try:
    migrator.migrate(source, 'migrator', password)
except (RuntimeError, ValueError) as exc:
    # Failure diagnostics deliberately conceal remote output and credentials.
    assert password not in str(exc)
else:
    raise AssertionError('Migration to the source server was accepted.')
print('Same-server migration rejected before destination changes.', flush=True)
try:
    migrator.migrate(target, 'migrator', password)
except RuntimeError as exc:
    assert password not in str(exc)
else:
    raise AssertionError('The deliberately unmanaged SSL unit was overwritten.')
failed = [value for value in migrator.status() if value['host'] == target and value['status'] == 'failed']
assert len(failed) == 1
failed = failed[0]
bundle = migrator.manager.backups / 'migrations' / failed['migration_id'] / 'migration.zip'
assert bundle.is_file() and migrator.manager.file_hash(bundle) == failed.get('sha256'), \
    'Migration failed before the controlled final SSL interruption; no verified source snapshot.'
snapshots = set(migrator.manager.backups.glob('*/*/COMPLETE'))
with SSHSession(target, 'root', password) as session:
    imported = json.loads(session.run_root(
        'cat /var/lib/wpi/migrations/' + failed['migration_id'] + '.json').stdout)
    assert imported['status'] == 'incomplete'
    assert len(imported['sites']) == 1
    if any(value['status'] != 'complete' for value in imported['sites'].values()):
        stage = {'status': imported['status'], 'sites': [
            {key: value.get(key) for key in ('status', 'database_claimed', 'database_provisioned')}
            for value in imported['sites'].values()]}
        print('Unexpected import progress (safe flags only): ' + json.dumps(stage), flush=True)
        trace = session.run_root("python3 -c \"import json; from pathlib import Path; "
                                 "p=Path('/root/wpi-ci-command-trace.jsonl'); "
                                 "print(json.dumps([json.loads(s) for s in p.read_text().splitlines()[-24:]] "
                                 "if p.exists() else []))\"")
        print('Target command categories and exit codes only: ' + trace.stdout.strip(), flush=True)
    assert all(value['status'] == 'complete' for value in imported['sites'].values())
    session.run_root("[ \"$(head -n 1 /etc/systemd/system/wpi-migration-ssl.service)\" = '# Unmanaged CI fixture' ]\n"
                     "rm -f -- /etc/systemd/system/wpi-migration-ssl.service")
report = migrator.migrate(target, 'migrator', password)
assert report['status'] == 'ready', report
assert len(report['sites']) == 1, report
assert report['migration_id'] == failed['migration_id']
assert migrator.manager.file_hash(bundle) == failed['sha256']
assert set(migrator.manager.backups.glob('*/*/COMPLETE')) == snapshots
resumed = [value for value in migrator.status() if value['migration_id'] == report['migration_id']][0]
assert resumed['status'] == 'ready' and resumed['snapshots'] == failed['snapshots']
Path('/root/migration-report.json').write_text(json.dumps(report))
assert Path(report['backup']).exists(), 'Source rollback backup was not retained.'
print('Real encrypted SSH password login, password sudo, bootstrap, and migration completed.', flush=True)
print('Interrupted final activation resumed through non-root SSH using the same snapshot, database, migration ID, and bundle checksum.', flush=True)
PY
docker cp "$WPI_CI_WORK/migrate-fixture.py" "$WPI_CI_SOURCE:/root/migrate-fixture.py"

# Before the controlled SSL interruption, track only safe command categories
# and return codes from the real target processes. Patching subprocess.run
# covers existing default runner bindings without changing their behavior.
cat > "$WPI_CI_WORK/import-diagnostic.py" <<'PY'
import json
import os
from pathlib import Path
import subprocess

original_run = subprocess.run
groups = {'core', 'config', 'db', 'user', 'post', 'maintenance-mode', 'rewrite', 'cache', 'search-replace'}
verbs = {'download', 'verify-checksums', 'is-installed', 'create', 'set', 'get', 'export', 'import',
         'tables', 'activate', 'deactivate', 'is-active', 'flush', 'structure'}
programs = {'mysql', 'openssl', 'curl', 'nginx', 'apache2ctl', 'systemctl', 'apt-get', 'runuser'}
def category(argv):
    if not isinstance(argv, (list, tuple)) or not argv or not isinstance(argv[0], str):
        return None
    program = Path(argv[0]).name
    if program not in programs:
        return None
    label = program
    if program == 'runuser':
        # WP-CLI arguments follow its one fixed --path option. Include only
        # known verbs and DB constant names, never positional values or flags.
        paths = [index for index, value in enumerate(argv) if isinstance(value, str) and value.startswith('--path=')]
        if not paths:
            return None
        tail = argv[paths[0] + 1:]
        while tail and tail[0] in {'--skip-plugins', '--skip-themes'}:
            tail = tail[1:]
        if tail and tail[0] in groups:
            label = 'wp.' + tail[0]
            if len(tail) > 1 and tail[1] in verbs:
                label += '.' + tail[1]
            if len(tail) > 2 and tail[0] == 'config' and tail[2] in {'DB_NAME', 'DB_USER', 'DB_HOST', 'DB_PASSWORD'}:
                label += '.' + tail[2]
    return program, label
def record(argv, code):
    tag = category(argv)
    if tag:
        value = json.dumps({'program': tag[0], 'category': tag[1], 'returncode': code}) + '\n'
        try:
            fd = os.open('/root/wpi-ci-command-trace.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, value.encode())
            finally:
                os.close(fd)
        except OSError:
            pass
def traced_run(argv, **kwargs):
    try:
        result = original_run(argv, **kwargs)
    except subprocess.CalledProcessError as exc:
        record(argv, exc.returncode)
        raise
    except subprocess.TimeoutExpired:
        record(argv, 'timeout')
        raise
    record(argv, result.returncode)
    return result
subprocess.run = traced_run
PY
install_target_python_hook() {
    docker cp "$1" "$WPI_CI_TARGET:/root/wpi-ci-sitecustomize.py"
    docker exec --interactive "$WPI_CI_TARGET" python3 - <<'PY'
from pathlib import Path
import py_compile
import sitecustomize
# Ubuntu ships a stdlib sitecustomize before dist-packages on sys.path.
# Replace the module that this disposable target actually imports, rather
# than writing a shadowed module and silently losing the fixture hook.
module = Path(sitecustomize.__file__).resolve(strict=True)
assert module.name == 'sitecustomize.py'
assert Path('/usr/lib') in module.parents or Path('/etc') in module.parents
print('Disposable Python hook installed at actual startup module: ' + str(module), flush=True)
module.write_bytes(Path('/root/wpi-ci-sitecustomize.py').read_bytes())
module.chmod(0o644)
py_compile.compile(str(module), doraise=True)
PY
}
install_target_python_hook "$WPI_CI_WORK/import-diagnostic.py"
docker exec "$WPI_CI_TARGET" python3 -c \
    'import subprocess; assert subprocess.run.__module__ == "sitecustomize"; print("Disposable import diagnostics startup hook verified.")'
docker exec "$WPI_CI_SOURCE" python3 -u /root/migrate-fixture.py "$WPI_CI_SOURCE_IP" "$WPI_CI_TARGET_IP"
docker cp "$WPI_CI_SOURCE:/root/source-manifest.json" "$WPI_CI_WORK/source-manifest.json"
docker cp "$WPI_CI_WORK/source-manifest.json" "$WPI_CI_TARGET:/root/source-manifest.json"

# Target verification and deterministic DNS/ACME fixture are installed below.
# Public DNS ownership and Let's Encrypt issuance are deliberately excluded;
# the HTTP challenge routing, TLS vhosts, database, and systemd service are real.
docker exec "$WPI_CI_TARGET" systemctl stop wpi-migration-ssl.timer
cat > "$WPI_CI_WORK/sitecustomize.py" <<'PY'
"""Disposable CI only: allow exactly three private fixture DNS names.

Public DNS/ACME ownership cannot be asserted for domains we do not own. This
replaces only the public-IP eligibility check. The installed controller still
uses real DNS answers, HTTP challenge tokens, response checking, and TLS.
"""
import sys
sys.path.insert(0, '/usr/local/lib/wpi')
from wpi import core
original_check = core.check_dns
allowed = {'wpi-primary.example.com', 'wpi-alias.example.com', 'wpi-redirect.example.com'}
def fixture_check(host):
    if host not in allowed:
        return original_check(host)
core.check_dns = fixture_check
PY
install_target_python_hook "$WPI_CI_WORK/sitecustomize.py"
docker exec "$WPI_CI_TARGET" python3 -c \
    'from wpi import core; assert core.check_dns.__module__ == "sitecustomize"; print("Disposable private-DNS eligibility startup hook verified.")'

cat > "$WPI_CI_WORK/certbot-fixture.py" <<'PY'
#!/usr/bin/env python3
"""Disposable ACME substitute; never copied into the published package."""
from pathlib import Path
import subprocess
import sys
argv = sys.argv[1:]
assert argv[0] == 'certonly' and '--webroot' in argv and '--non-interactive' in argv
host = argv[argv.index('--domain') + 1]
root = Path(argv[argv.index('--webroot-path') + 1])
assert host in {'wpi-primary.example.com', 'wpi-alias.example.com', 'wpi-redirect.example.com'}
assert root.is_dir() and root.as_posix().startswith('/var/www/wpi/')
folder = Path('/etc/letsencrypt/live') / host
folder.mkdir(parents=True, exist_ok=True)
subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-keyout', str(folder / 'privkey.pem'), '-out', str(folder / 'fullchain.pem'),
                '-subj', '/CN=' + host, '-addext', 'subjectAltName=DNS:' + host],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
(folder / 'privkey.pem').chmod(0o600)
with Path('/root/certbot-fixture-hosts').open('a') as out:
    out.write(host + '\n')
PY
docker cp "$WPI_CI_WORK/certbot-fixture.py" "$WPI_CI_TARGET:/usr/local/bin/certbot"
docker exec "$WPI_CI_TARGET" chmod 755 /usr/local/bin/certbot

cat > "$WPI_CI_WORK/target-verify.py" <<'PY'
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/usr/local/lib/wpi')
from wpi.core import Manager, atomic_json
from wpi.migrate_target import TargetMigration

stage = sys.argv[1]
manifest = json.loads(Path('/root/source-manifest.json').read_text())
original = manifest['site']
manager = Manager()
target = TargetMigration(manager)
site = manager.site(original['id'])
report = target.status()['migrations'][0]
hosts = [site['primary'], *site['aliases'], *site['secondary']]
assert len(manager.sites()) == 1
for name in ('id', 'primary', 'aliases', 'secondary', 'admin', 'title', 'db_name', 'db_user'):
    assert site[name] == original[name], name
assert manager.wp(site, 'option', 'get', 'home').stdout.strip() == 'https://' + site['primary']
assert manager.wp(site, 'user', 'get', 'migration_admin', '--field=user_pass').stdout.strip() == manifest['admin_hash']
assert manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout.strip() == manifest['tables']
upload = Path(site['root']) / 'wp-content/uploads/migration-fixture.bin'
assert hashlib.sha256(upload.read_bytes()).hexdigest() == manifest['upload_sha256']
assert manager.wp(site, 'post', 'get', manifest['post'], '--field=post_content').stdout.strip() == 'Migration preserved content'
manager.wp(site, 'core', 'verify-checksums')
assert manager.wp(site, 'maintenance-mode', 'is-active', check=False).returncode != 0
assert site['status'] == 'active'
assert report['status'] == 'ready'
for action in ('is-enabled', 'is-active'):
    subprocess.run(['systemctl', action, '--quiet', 'wpi-autotune.timer'], check=True)
subprocess.run(['systemctl', 'is-enabled', '--quiet', 'wpi-migration-ssl.timer'], check=True)

if stage == 'before-dns':
    credentials = json.loads((manager.data / 'credentials' / (site['id'] + '.json')).read_text())
    new_password = credentials['database_password']
    # Only hashes are compared with the source fixture. Password values stay
    # in memory and captured WP-CLI output; no assertion or log prints them.
    assert hashlib.sha256(new_password.encode()).hexdigest() != manifest['database_password_hash']
    assert manager.wp(site, 'config', 'get', 'DB_PASSWORD').stdout.strip() == new_password
    imported_backup = manager.backups / site['id'] / ('migration-' + report['migration_id'])
    restored, safety = manager.restore(site['id'], imported_backup)
    site = manager.site(site['id'])
    assert restored['migration_id'] == site['migration_id'] == report['migration_id']
    for name in ('primary', 'aliases', 'secondary'):
        assert site[name] == original[name]
    assert Path(safety).is_dir()
    preserved = json.loads((manager.data / 'credentials' / (site['id'] + '.json')).read_text())
    assert preserved['database_password'] == new_password
    assert manager.wp(site, 'config', 'get', 'DB_PASSWORD').stdout.strip() == new_password
    assert manager.wp(site, 'db', 'tables', '--all-tables-with-prefix').stdout.strip() == manifest['tables']
    assert manager.wp(site, 'user', 'get', 'migration_admin', '--field=user_pass').stdout.strip() == manifest['admin_hash']
    assert manager.wp(site, 'post', 'get', manifest['post'], '--field=post_content').stdout.strip() == 'Migration preserved content'
    assert hashlib.sha256(upload.read_bytes()).hexdigest() == manifest['upload_sha256']
    assert manager.wp(site, 'maintenance-mode', 'is-active', check=False).returncode != 0
    manager.wp(site, 'core', 'verify-checksums')
    print('Raw source backup restored on destination: regenerated SQL credentials and migration ownership retained; database/content/admin/media accessible.', flush=True)

def request(host, path, https=True):
    with tempfile.TemporaryDirectory() as directory:
        body = Path(directory) / 'body'
        command = ['curl', '--silent', '--show-error', '--insecure', '--noproxy', '*',
                   '--resolve', f'{host}:{443 if https else 80}:127.0.0.1',
                   '--dump-header', '-', '--output', str(body), '--write-out', '\nWPI_STATUS:%{http_code}\n',
                   '--max-time', '15', f'{"https" if https else "http"}://{host}{path}']
        result = subprocess.run(command, capture_output=True, text=True)
        status = re.search(r'WPI_STATUS:(\d+)', result.stdout)
        return (int(status[1]) if status else 0, result.stdout,
                body.read_text(errors='replace') if body.exists() else '', result.returncode)

def expected(host, path, status, https=True):
    for attempt in range(25):
        response = request(host, path, https)
        if response[0] == status and response[3] == 0:
            return response
        time.sleep(0.2)
    raise AssertionError((host, path, response[0], response[3]))

for host in [site['primary'], *site['aliases']]:
    response = expected(host, '/migration-preserved/', 200)
    assert 'Migration preserved content' in response[2]
    assert 'https://' + host in response[2]
    assert not re.search(r'^location:', response[1], re.I | re.M)
    denied = expected(host, '/wp-config.php', 403)
    assert 'DB_PASSWORD' not in denied[2]
    denied = request(host, '/wpi-fpm-status')
    assert denied[0] in (403, 404)
for host in site['secondary']:
    response = expected(host, '/article?x=1', 301)
    assert re.search(r'^location:\s*https://' + re.escape(site['primary']) + r'/article\?x=1\s*$',
                     response[1], re.I | re.M)

if stage == 'before-dns':
    for host in hosts:
        certificate, key = manager.web.certificate_paths(host)
        assert str(certificate).startswith('/etc/wpi/migration-tls/')
        assert hashlib.sha256(certificate.read_bytes()).hexdigest() == manifest['ssl_hashes'][host]
        assert key.stat().st_mode & 0o777 == 0o600
        assert not manager.web.letsencrypt_ready(host)
    # Trigger the installed unit while DNS still points to the source. Only
    # this test resets backoff; production intervals remain untouched.
    journal_path = target._journal_path(report['migration_id'])
    journal = json.loads(journal_path.read_text())
    for value in journal['ssl'].values():
        value['next_attempt'] = 0
    # A formerly attached hostname can remain in the retry queue. It must
    # never receive a new certificate or be reattached by the controller.
    journal['ssl']['wpi-deleted.example.com'] = {
        'site_id': site['id'], 'status': 'pending', 'next_attempt': 0}
    atomic_json(journal_path, journal)
    subprocess.run(['systemctl', 'start', 'wpi-migration-ssl.service'], check=True)
    pending = target.status()['migrations'][0]
    assert set(pending['ssl_pending']) == set(hosts), pending
    assert all(pending['ssl'][host] == 'waiting_dns' for host in hosts), pending
    assert pending['ssl']['wpi-deleted.example.com'] == 'removed', pending
    assert not Path('/root/certbot-fixture-hosts').exists()
    print('Before DNS: imported SQL, admin, media, roles, HTTPS fallback and private routes verified; SSL waits for destination challenge.', flush=True)
elif stage == 'after-dns':
    def controller_diagnostics():
        journal = json.loads(target._journal_path(report['migration_id']).read_text())
        print('SSL retry state: ' + json.dumps({host: {
            'status': value.get('status'), 'next_attempt': value.get('next_attempt')}
            for host, value in journal['ssl'].items()}), flush=True)
        for unit, properties in (
                ('wpi-migration-ssl.service', ('ActiveState', 'SubState', 'Result',
                 'ExecMainStatus', 'ExecMainCode', 'ExecMainStartTimestamp')),
                ('wpi-migration-ssl.timer', ('ActiveState', 'SubState', 'LastTriggerUSec',
                 'NextElapseUSecRealtime', 'NextElapseUSecMonotonic', 'AccuracyUSec'))):
            diagnostic = subprocess.run(['systemctl', 'show', unit,
                *['--property=' + value for value in properties]],
                capture_output=True, text=True, check=False)
            print(unit + ' scheduling state:\n' + diagnostic.stdout, flush=True)
    for host in hosts:
        if not target._http_probe(host, site['root']):
            controller_diagnostics()
            raise AssertionError('Destination HTTP challenge probe failed: ' + host)
    print('After DNS: destination HTTP challenge routing passed for every domain.', flush=True)
    for attempt in range(60):
        status = target.status()['migrations'][0]
        if not status['ssl_pending']:
            break
        time.sleep(1)
    else:
        controller_diagnostics()
        raise AssertionError(status)
    subprocess.run(['systemctl', 'stop', 'wpi-migration-ssl.timer'], check=True)
    assert all(status['ssl'][host] == 'ready' for host in hosts), status
    assert status['ssl']['wpi-deleted.example.com'] == 'removed', status
    issued = Path('/root/certbot-fixture-hosts').read_text().splitlines()
    assert sorted(issued) == sorted(hosts), issued
    for host in hosts:
        certificate, key = manager.web.certificate_paths(host)
        assert str(certificate).startswith('/etc/letsencrypt/live/')
        assert hashlib.sha256(certificate.read_bytes()).hexdigest() != manifest['ssl_hashes'][host]
    print('After DNS: actual scheduled SSL controller passed HTTP challenge for every domain, activated new TLS, and retained Alias/301 protection.', flush=True)
else:
    raise AssertionError('Unknown phase')
PY
docker cp "$WPI_CI_WORK/target-verify.py" "$WPI_CI_TARGET:/root/target-verify.py"
docker exec "$WPI_CI_TARGET" python3 -u /root/target-verify.py before-dns

# Simulate the user's DNS cutover only on the destination's resolver. No
# transfer or imported WordPress URL changes are performed at this point.
docker exec --interactive "$WPI_CI_TARGET" python3 - "$WPI_CI_TARGET_IP" <<'PY'
from pathlib import Path
import sys
hosts = Path('/etc/hosts')
content = [line for line in hosts.read_text().splitlines() if 'wpi-primary.example.com' not in line]
content.append(sys.argv[1] + ' wpi-primary.example.com wpi-alias.example.com wpi-redirect.example.com')
# /etc/hosts is a Docker bind mount, so replace its contents in place.
hosts.write_text('\n'.join(content) + '\n')
import json
for path in Path('/var/lib/wpi/migrations').glob('*.json'):
    journal = json.loads(path.read_text())
    for value in journal['ssl'].values():
        value['next_attempt'] = 0
    path.write_text(json.dumps(journal))
PY
docker exec "$WPI_CI_TARGET" bash -c \
    'mkdir -p /run/systemd/system/wpi-migration-ssl.timer.d; printf "[Timer]\nOnBootSec=\nOnBootSec=1s\nOnUnitInactiveSec=\nOnUnitInactiveSec=1s\nRandomizedDelaySec=0\nAccuracySec=100ms\n" > /run/systemd/system/wpi-migration-ssl.timer.d/ci.conf; systemctl daemon-reload; systemctl restart wpi-migration-ssl.timer'
docker exec "$WPI_CI_TARGET" python3 -u /root/target-verify.py after-dns
docker exec --interactive "$WPI_CI_SOURCE" python3 - <<'PY'
import json
from pathlib import Path
import sys
sys.path.insert(0, '/usr/local/lib/wpi')
from wpi.core import Manager
manager = Manager()
manifest = json.loads(Path('/root/source-manifest.json').read_text())
site = manager.site(manifest['site']['id'])
assert site['status'] == 'active'
assert manager.wp(site, 'maintenance-mode', 'is-active', check=False).returncode != 0
assert manager.wp(site, 'user', 'get', 'migration_admin', '--field=user_pass').stdout.strip() == manifest['admin_hash']
manager.wp(site, 'core', 'is-installed')
assert Path(json.loads(Path('/root/migration-report.json').read_text())['backup']).is_dir()
print('Source retained: WordPress remains installed, maintenance mode cleared, and rollback snapshot available.', flush=True)
PY
printf 'Migration integration passed: two Ubuntu servers, real root/non-root encrypted SSH, password sudo, database/media/admin/roles preserved, and scheduled auto-SSL after DNS. Public ACME issuance was a disposable fixture.\n'
