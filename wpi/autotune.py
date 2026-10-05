"""Bounded, automatic PHP-FPM tuning using local aggregate telemetry only.

There is no public status endpoint or dependency outside Python's standard
library. A private FastCGI listener remains responsive when the website pool is
full. Missing measurements never authorize adding workers.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import socket
import struct
import tempfile
import time


MIB = 1024 ** 2
GIB = 1024 ** 3
STATUS_PATH = '/wpi-fpm-status'
SERVICE = 'wpi-autotune.service'
TIMER = 'wpi-autotune.timer'
STATE_VERSION = 1
GROW_COOLDOWN = 60
PRESSURE_COOLDOWN = 180


def _read(path):
    try:
        return Path(path).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None


def _number(path):
    value = _read(path)
    try:
        return int(value) if value is not None and value != 'max' else None
    except ValueError:
        return None


def _cpuset_count(value):
    if not value:
        return None
    cpus = set()
    try:
        for entry in value.split(','):
            bounds = [int(x) for x in entry.split('-')]
            if len(bounds) == 1:
                cpus.add(bounds[0])
            elif len(bounds) == 2 and 0 <= bounds[0] <= bounds[1] < 65536:
                cpus.update(range(bounds[0], bounds[1] + 1))
            else:
                return None
        return len(cpus) or None
    except ValueError:
        return None


def _ancestors(path, base):
    """Inspect all mounted ancestors, including delegated container roots."""
    try:
        path.relative_to(base)
    except ValueError:
        path = base
    while True:
        yield path
        if path == base:
            break
        path = path.parent


def _cg_path(base, relative):
    relative = relative.lstrip('/')
    candidate = base / relative
    # In a cgroup namespace the mounted root already represents this subtree.
    return candidate if candidate.exists() else base


def _psi(path):
    value = _read(path)
    if value:
        match = re.search(r'^some .*?avg10=([0-9.]+)', value, re.M)
        if match:
            return float(match.group(1))
    return None


def detect_resources(proc_root='/proc', sys_root='/sys', target_pid=None):
    """Return effective server capacity, including cgroup ancestor limits.

    Failed optional cgroup reads make telemetry incomplete instead of silently
    granting host capacity. The caller may retain/shrink existing capacity but
    must not grow it until telemetry is available again.
    """
    proc, sys = Path(proc_root), Path(sys_root)
    values = {}
    for line in (_read(proc / 'meminfo') or '').splitlines():
        match = re.match(r'([A-Za-z_]+):\s+(\d+)\s+kB', line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get('MemTotal', 0)
    available = values.get('MemAvailable', 0)
    rawstat = _read(proc / 'stat') or ''
    host_cpus = len(re.findall(r'^cpu\d+\s', rawstat, re.M)) or (os.cpu_count() or 1)
    cpus = float(host_cpus)
    if proc == Path('/proc') and hasattr(os, 'sched_getaffinity'):
        try:
            cpus = min(cpus, float(len(os.sched_getaffinity(0))))
        except OSError:
            pass
    counters = None
    match = re.search(r'^cpu\s+(.+)$', rawstat, re.M)
    if match:
        try:
            fields = [int(x) for x in match.group(1).split()]
            if len(fields) >= 4:
                # guest counters are already included in user/nice.
                counters = (sum(fields[:8]), fields[3] + (fields[4] if len(fields) > 4 else 0))
        except ValueError:
            pass
    vmstat = {}
    for line in (_read(proc / 'vmstat') or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ('pswpin', 'pswpout'):
            try:
                vmstat[parts[0]] = int(parts[1])
            except ValueError:
                pass
    cgroups = {}
    target_cgroup = proc / str(target_pid) / 'cgroup' if target_pid else proc / 'self/cgroup'
    cgroup_text = _read(target_cgroup)
    for line in (cgroup_text or '').splitlines():
        fields = line.split(':', 2)
        if len(fields) == 3:
            for controller in fields[1].split(','):
                cgroups[controller] = fields[2]
    base = sys / 'fs/cgroup'
    complete = (total > 0 and 'MemAvailable' in values and counters is not None
                and (not target_pid or cgroup_text is not None))
    cg_cpu_usec = None
    cg_cpu_limit = None
    cg_memory_limit = None
    memory_psi = _psi(proc / 'pressure/memory')
    cpu_psi = _psi(proc / 'pressure/cpu')
    if (base / 'cgroup.controllers').exists() or (base / 'memory.max').exists():
        own = _cg_path(base, cgroups.get('', '/'))
        paths = list(_ancestors(own, base))
        usage_path = base
        # Use the widest measurable ancestor's CPU usage, so this oneshot's
        # almost-idle service cgroup cannot hide PHP/database CPU saturation.
        for path in reversed(paths):
            stat = _read(path / 'cpu.stat')
            found = re.search(r'^usage_usec\s+(\d+)$', stat or '', re.M)
            if found:
                cg_cpu_usec = int(found.group(1))
                usage_path = path
                break
        for path in paths:
            memfile = path / 'memory.max'
            if memfile.exists():
                memmax = _number(memfile)
                if _read(memfile) != 'max' and memmax is None:
                    complete = False
                if memmax is not None and 0 < memmax < 2 ** 60:
                    cg_memory_limit = min(cg_memory_limit or memmax, memmax)
                    total = min(total, memmax) if total else memmax
                    current = _number(path / 'memory.current')
                    if current is None:
                        complete = False
                    else:
                        available = min(available, max(0, memmax - current))
            quota = _read(path / 'cpu.max')
            if quota:
                fields = quota.split()
                try:
                    if len(fields) != 2 or int(fields[1]) <= 0:
                        raise ValueError()
                    if fields[0] != 'max':
                        limit = int(fields[0]) / int(fields[1])
                        if limit <= 0:
                            raise ValueError()
                        cpus = min(cpus, limit)
                        narrower = cg_cpu_limit is None or limit <= cg_cpu_limit
                        cg_cpu_limit = min(cg_cpu_limit or limit, limit)
                        # Read usage at the quota-constrained ancestor (or a
                        # child thereof), rather than unrelated host groups.
                        stat = _read(path / 'cpu.stat')
                        found = re.search(r'^usage_usec\s+(\d+)$', stat or '', re.M)
                        if found and narrower:
                            cg_cpu_usec, usage_path = int(found.group(1)), path
                except ValueError:
                    complete = False
            affinity = _cpuset_count(_read(path / 'cpuset.cpus.effective'))
            if affinity:
                cpus = min(cpus, affinity)
            own_psi = _psi(path / 'memory.pressure')
            if own_psi is not None:
                memory_psi = max(memory_psi or 0, own_psi)
    else:
        for controller, dirname in (('memory', 'memory'), ('cpu', 'cpu'),
                                     ('cpuacct', 'cpuacct'), ('cpuset', 'cpuset')):
            controller_base = base / dirname
            if controller == 'cpu' and not controller_base.exists():
                controller_base = base / 'cpu,cpuacct'
            if controller == 'cpuacct' and not controller_base.exists():
                controller_base = base / 'cpu,cpuacct'
            if not controller_base.exists():
                continue
            own = _cg_path(controller_base, cgroups.get(controller, '/'))
            paths = list(_ancestors(own, controller_base))
            if controller == 'memory':
                for path in paths:
                    limitfile = path / 'memory.limit_in_bytes'
                    limit = _number(limitfile)
                    if limitfile.exists() and limit is None:
                        complete = False
                    if limit and 0 < limit < 2 ** 60:
                        cg_memory_limit = min(cg_memory_limit or limit, limit)
                        total = min(total, limit)
                        used = _number(path / 'memory.usage_in_bytes')
                        if used is None:
                            complete = False
                        else:
                            available = min(available, max(0, limit - used))
            elif controller == 'cpu':
                for path in paths:
                    quota = _number(path / 'cpu.cfs_quota_us')
                    period = _number(path / 'cpu.cfs_period_us')
                    if (path / 'cpu.cfs_quota_us').exists() and (quota is None or not period):
                        complete = False
                    if quota and quota > 0 and period and period > 0:
                        limit = quota / period
                        cpus = min(cpus, limit)
                        cg_cpu_limit = min(cg_cpu_limit or limit, limit)
            elif controller == 'cpuacct':
                for path in reversed(paths):
                    usage = _number(path / 'cpuacct.usage')
                    if usage is not None:
                        cg_cpu_usec = usage // 1000
                        break
            else:
                for path in paths:
                    affinity = _cpuset_count(_read(path / 'cpuset.cpus'))
                    if affinity:
                        cpus = min(cpus, affinity)
    worker_rss = []
    try:
        for pid in proc.iterdir():
            if not pid.name.isdigit():
                continue
            try:
                cmd = (pid / 'cmdline').read_bytes().replace(b'\0', b' ').strip()
                if cmd != b'php-fpm: pool www':
                    continue
                rss = re.search(r'^VmRSS:\s+(\d+)\s+kB', _read(pid / 'status') or '', re.M)
                if rss:
                    worker_rss.append(int(rss.group(1)) * 1024)
            except OSError:
                continue  # A worker may exit between directory and status reads.
    except OSError:
        complete = False
    return {'memory_total': max(0, total), 'memory_available': max(0, min(total, available)),
            'cpus': max(0.1, cpus), 'host_cpus': host_cpus,
            'cpu_total': counters[0] if counters else None,
            'cpu_idle': counters[1] if counters else None,
            'cgroup_cpu_usec': cg_cpu_usec, 'cgroup_cpu_limit': cg_cpu_limit,
            'cgroup_memory_limit': cg_memory_limit,
            'swap_in': vmstat.get('pswpin'), 'swap_out': vmstat.get('pswpout'),
            'memory_psi': memory_psi, 'cpu_psi': cpu_psi,
            'worker_rss': worker_rss, 'telemetry_ok': complete}


def profile(resources):
    total = resources.get('memory_total', 0)
    if total < GIB:
        limits = {'memory_mib': 128, 'upload_mib': 32, 'post_mib': 40, 'opcache_mib': 64}
    elif total < 4 * GIB:
        limits = {'memory_mib': 256, 'upload_mib': 64, 'post_mib': 80, 'opcache_mib': 128}
    else:
        limits = {'memory_mib': 384, 'upload_mib': 128, 'post_mib': 144, 'opcache_mib': 256}
    settings = resources.get('php_settings', {})
    if settings:
        # Local import keeps the pure settings policy usable by the service
        # without creating a module-initialization cycle.
        from .php_settings import configured_profile
        limits = configured_profile(limits, settings, resources)
    return limits


def render_ini(resources):
    limits = profile(resources)
    return ("; Managed automatically by WPI. Recomputed when capacity changes.\n"
            "expose_php = Off\ndisplay_errors = Off\nlog_errors = On\n"
            f"memory_limit = {limits['memory_mib']}M\n"
            f"upload_max_filesize = {limits['upload_mib']}M\n"
            f"post_max_size = {limits['post_mib']}M\n"
            "max_execution_time = 120\nmax_input_time = 120\nmax_input_vars = 5000\n"
            "session.gc_maxlifetime = 1440\nsession.gc_divisor = 1000\n"
            "allow_url_fopen = On\ncgi.fix_pathinfo = 0\n"
            "opcache.enable = 1\nopcache.enable_cli = 0\n"
            f"opcache.memory_consumption = {limits['opcache_mib']}\n"
            "opcache.interned_strings_buffer = 8\nopcache.max_accelerated_files = 20000\n"
            "opcache.validate_timestamps = 1\nopcache.revalidate_freq = 2\n")


def render_pool(children, status_socket, resources=None):
    # The controller supplies a RAM/CPU-derived capacity. Preserve it here so
    # larger servers can use their measured resources without an arbitrary cap.
    children = max(1, int(children))
    spare_min = max(1, min(4, children // 4 or 1))
    spare_max = min(children, max(spare_min, min(8, children // 2 or 1)))
    start = min(children, max(spare_min, (spare_min + spare_max) // 2))
    content = ("; WPI automatic controller: overrides Ubuntu's www pool.\n"
            "[global]\nprocess_control_timeout = 185s\n[www]\n"
            "pm = dynamic\n"
            f"pm.max_children = {children}\npm.start_servers = {start}\n"
            f"pm.min_spare_servers = {spare_min}\npm.max_spare_servers = {spare_max}\n"
            "pm.max_requests = 500\nrequest_terminate_timeout = 180s\n"
            "request_terminate_timeout_track_finished = yes\n"
            f"pm.status_listen = {Path(status_socket).as_posix()}\npm.status_path = {STATUS_PATH}\n")
    # php_admin_value cannot be overridden by an application's ini_set().
    # Keep automatic mode's existing behavior; pin only explicit settings.
    if resources and resources.get('php_settings'):
        limits = profile(resources)
        settings = resources['php_settings']
        if 'memory_limit_mb' in settings:
            content += f"php_admin_value[memory_limit] = {limits['memory_mib']}M\n"
        if 'upload_max_filesize_mb' in settings:
            content += (f"php_admin_value[upload_max_filesize] = {limits['upload_mib']}M\n"
                        f"php_admin_value[post_max_size] = {limits['post_mib']}M\n")
    return content


def capacity(resources):
    """A steady-state bound with an explicit DB/OS and OPcache reservation."""
    total = max(0, resources.get('memory_total', 0))
    reserve = max(384 * MIB, int(total * 0.35))
    opcache = profile(resources)['opcache_mib'] * MIB
    budget = max(0, min(int(total * 0.50), total - reserve - opcache))
    raw_rss = resources.get('worker_rss', [])
    if isinstance(raw_rss, (int, float)):
        raw_rss = [raw_rss]
    samples = sorted(x for x in raw_rss if x > 0)
    p90 = samples[min(len(samples) - 1, math.ceil(len(samples) * 0.9) - 1)] if samples else 0
    explicit = resources.get('php_settings', {}).get('memory_limit_mb')
    # A manually raised limit authorizes a request to use that much memory.
    # Budget its peak plus native/extension overhead before adding workers.
    floor = (int(explicit) + 32) * MIB if explicit else 96 * MIB
    worker = max(floor, math.ceil(p90 * 1.25))
    memory_cap = max(1, budget // worker)
    cpu_cap = max(1, math.floor(max(0.1, resources.get('cpus', 1)) * 8))
    return {'capacity': min(cpu_cap, memory_cap), 'worker_bytes': worker,
            'memory_budget': budget, 'reserve_bytes': reserve + opcache}


def initial_children(resources):
    return min(capacity(resources)['capacity'], max(1, math.ceil(resources.get('cpus', 1) * 2)))


def decide(resources, telemetry, state, now):
    """Pure controller policy, suitable for deterministic workload tests."""
    bounds = capacity(resources)
    current = max(1, int(state.get('children', initial_children(resources))))
    saturation = max(0, int(state.get('saturation_ticks', 0)))
    idle_ticks = max(0, int(state.get('idle_ticks', 0)))
    pressure_ticks = max(0, int(state.get('pressure_ticks', 0)))
    last_change = min(float(state.get('last_change', now)), now)
    last_pressure = state.get('last_pressure_change')
    if last_pressure is not None:
        last_pressure = min(float(last_pressure), now)
    # A previous future timestamp (NTP/reboot) must not suppress changes forever.
    elapsed = max(0, now - last_change)
    memory_total = resources.get('memory_total', 0)
    available = resources.get('memory_available', 0)
    headroom = max(128 * MIB, int(memory_total * 0.12))
    cpu = resources.get('cpu_load')
    severe_memory = available < headroom or (resources.get('memory_psi') or 0) >= 10
    swap_pressure = (resources.get('swap_delta') or 0) > 0
    cpu_pressure = cpu is not None and cpu >= 0.92
    pressure_ticks = pressure_ticks + 1 if severe_memory or swap_pressure or cpu_pressure else 0
    reason, target = 'stable', current
    if current > bounds['capacity']:
        target, reason = bounds['capacity'], 'capacity-limit'
    elif severe_memory or swap_pressure or (cpu_pressure and pressure_ticks >= 2):
        target = max(1, current - max(1, math.ceil(current * 0.20)))
        reason = 'resource-pressure'
    elif not resources.get('telemetry_ok') or telemetry is None or cpu is None:
        reason = 'telemetry-unavailable'
        saturation = idle_ticks = 0
    else:
        queue = telemetry.get('listen queue', 0)
        active = telemetry.get('active processes', 0)
        total = telemetry.get('total processes', 0)
        saturated = queue > 0 or (total >= current and active >= math.ceil(current * 0.85))
        quiet = (queue == 0 and telemetry.get('queue_measured') is not False
                 and active <= max(1, current // 4) and cpu < 0.40)
        saturation = saturation + 1 if saturated else 0
        idle_ticks = idle_ticks + 1 if quiet else 0
        if saturation >= 3 and elapsed >= GROW_COOLDOWN:
            candidate = min(bounds['capacity'], current + max(1, math.ceil(current * 0.25)))
            # Keep conservative free-memory headroom for the replacement pool
            # while the master waits for the current requests to finish.
            reload_room = max(0, available - headroom - profile(resources)['opcache_mib'] * MIB) // bounds['worker_bytes']
            candidate = min(candidate, reload_room)
            if cpu < 0.80 and candidate > current and resources.get('worker_rss'):
                target, reason = candidate, 'sustained-demand'
            else:
                reason = 'hardware-bound' if candidate <= current or cpu >= 0.80 else 'rss-unavailable'
        elif idle_ticks >= 20 and elapsed >= 300:
            baseline = initial_children(resources)
            target = min(current, max(baseline, current - max(1, math.ceil(current * 0.20))))
            reason = 'idle' if target < current else 'stable'
    if target < current and reason in ('capacity-limit', 'resource-pressure'):
        if last_pressure is not None and now - last_pressure < PRESSURE_COOLDOWN:
            target, reason = current, 'pressure-cooldown'
        else:
            last_pressure = now
    if target != current:
        saturation = idle_ticks = pressure_ticks = 0
        last_change = now
    return {**bounds, 'children': target, 'reason': reason,
            'saturation_ticks': saturation, 'idle_ticks': idle_ticks,
            'pressure_ticks': pressure_ticks, 'last_change': last_change,
            'last_pressure_change': last_pressure}


def _fcgi_record(kind, data=b'', request_id=1):
    padding = (-len(data)) % 8
    return struct.pack('!BBHHBB', 1, kind, request_id, len(data), padding, 0) + data + b'\0' * padding


def _fcgi_length(length):
    return bytes([length]) if length < 128 else struct.pack('!I', length | 0x80000000)


def _recv_exact(connection, length):
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise RuntimeError('Status PHP-FPM terputus.')
        result.extend(chunk)
    return bytes(result)


def read_fpm_status(socket_path, timeout=2):
    """FastCGI aggregate JSON query; never request/store per-request details."""
    params = {'SCRIPT_NAME': STATUS_PATH, 'SCRIPT_FILENAME': STATUS_PATH,
              'REQUEST_METHOD': 'GET', 'QUERY_STRING': 'json',
              'REQUEST_URI': STATUS_PATH + '?json', 'SERVER_PROTOCOL': 'HTTP/1.1',
              'SERVER_NAME': 'localhost', 'SERVER_PORT': '0', 'REMOTE_ADDR': '127.0.0.1'}
    payload = bytearray()
    for key, value in params.items():
        key, value = key.encode(), value.encode()
        payload.extend(_fcgi_length(len(key)) + _fcgi_length(len(value)) + key + value)
    request = (_fcgi_record(1, struct.pack('!HB5s', 1, 0, b'\0' * 5)) +
               _fcgi_record(4, bytes(payload)) + _fcgi_record(4) + _fcgi_record(5))
    output = bytearray()
    with socket.socket(getattr(socket, 'AF_UNIX', 1), socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
        connection.sendall(request)
        records = 0
        while True:
            version, kind, request_id, length, padding, _ = struct.unpack('!BBHHBB', _recv_exact(connection, 8))
            if version != 1 or request_id != 1:
                raise RuntimeError('Protokol status PHP-FPM tidak valid.')
            body = _recv_exact(connection, length)
            _recv_exact(connection, padding)
            records += 1
            if records > 128 or len(output) + length > 128 * 1024:
                raise RuntimeError('Respons status PHP-FPM terlalu besar.')
            if kind == 6:
                output.extend(body)
            elif kind == 3:
                if len(body) != 8 or struct.unpack('!IB3s', body)[:2] != (0, 0):
                    raise RuntimeError('Status PHP-FPM gagal.')
                break
    raw = bytes(output)
    if b'\r\n\r\n' in raw:
        headers, body = raw.split(b'\r\n\r\n', 1)
    elif b'\n\n' in raw:
        headers, body = raw.split(b'\n\n', 1)
    else:
        raise RuntimeError('Header status PHP-FPM tidak valid.')
    status = re.search(rb'^Status:\s*(\d+)', headers, re.I | re.M)
    if status and not 200 <= int(status.group(1)) < 300:
        raise RuntimeError('Status PHP-FPM ditolak.')
    try:
        result = json.loads(body.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise RuntimeError('JSON status PHP-FPM tidak valid.') from None
    keys = ('listen queue', 'active processes', 'idle processes', 'total processes',
            'accepted conn', 'max children reached')
    if not isinstance(result, dict) or result.get('pool') != 'www':
        raise RuntimeError('Pool status PHP-FPM tidak valid.')
    for key in keys:
        if key not in result or type(result[key]) is not int or result[key] < 0:
            raise RuntimeError('Pengukuran status PHP-FPM tidak lengkap.')
    aggregate = {'pool': 'www', **{key: result[key] for key in keys}}
    if type(result.get('start time')) is int and result['start time'] >= 0:
        aggregate['start time'] = result['start time']
    return aggregate


def read_socket_queue(runner, socket_path):
    """Linux UNIX backlog; FPM's own scoreboard only counts TCP queues.

    A missing/unsupported measurement returns None. Callers must not treat a
    failed command as a measured empty queue.
    """
    try:
        result = runner(['ss', '-xlnH'], check=False)
        if result.returncode != 0:
            return None
        expected = Path(socket_path).as_posix()
        for line in (result.stdout or '').splitlines():
            fields = line.split()
            if len(fields) >= 6 and fields[0] == 'u_str' and fields[1] == 'LISTEN' and fields[4] == expected:
                queue = int(fields[2])
                return queue if queue >= 0 else None
    except (OSError, RuntimeError, ValueError):
        pass
    return None


def _atomic_write(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.chmod(name, mode)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data.encode('utf-8') if isinstance(data, str) else data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class AutoTuner:
    def __init__(self, php_version, runner, data_dir='/var/lib/wpi', etc_root='/etc',
                 proc_root='/proc', sys_root='/sys', run_root='/run', clock=time.time):
        if php_version not in ('8.1', '8.3'):
            raise ValueError('Versi PHP autotune tidak didukung.')
        self.php_version, self.runner = php_version, runner
        self.config_file = Path(data_dir) / 'config.json'
        self.data = Path(data_dir) / 'autotune'
        self.etc, self.proc, self.sys = Path(etc_root), Path(proc_root), Path(sys_root)
        self.runtime = Path(run_root) / 'wpi-autotune'
        self.clock = clock
        self.pool = self.etc / f'php/{php_version}/fpm/pool.d/zz-wpi-autotune.conf'
        self.ini = self.etc / f'php/{php_version}/fpm/conf.d/99-wpi.ini'
        self.cli_ini = self.etc / f'php/{php_version}/cli/conf.d/99-wpi.ini'
        self.state_file = self.data / 'state.json'

    @property
    def status_socket(self):
        return self.runtime / 'status.sock'

    @contextmanager
    def _lock(self, blocking=False):
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        path = self.data / 'controller.lock'
        with path.open('a+b') as handle:
            os.chmod(path, 0o600)
            # Unix production lock; an equivalent Windows byte lock supports
            # the cross-platform unit suite without weakening the VPS lock.
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                except OSError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    yield False
                    return
                try:
                    yield True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _state(self):
        try:
            result = json.loads(self.state_file.read_text())
            return result if isinstance(result, dict) and result.get('version') == STATE_VERSION else {}
        except (OSError, ValueError):
            return {}

    def _save(self, state):
        _atomic_write(self.state_file, json.dumps({'version': STATE_VERSION, **state}, indent=2) + '\n')

    def _runtime(self):
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.runtime, 0o700)

    def _resources(self):
        pid = _number(self.runtime.parent / f'php/php{self.php_version}-fpm.pid')
        # The monitored service may have different limits from this timer's
        # cgroup. Inspect FPM's master rather than the controller when running.
        result = detect_resources(self.proc, self.sys, target_pid=pid if pid and pid > 0 else None)
        if self.config_file.exists():
            cfg = json.loads(self.config_file.read_text(encoding='utf-8'))
            settings = cfg.get('php_settings', {})
            if settings:
                from .php_settings import configured_profile
                configured_profile(profile(result), settings, result)
                result = {**result, 'php_settings': settings}
        return result

    def _apply(self, children, resources, update_ini=True):
        """Transactionally validate and reload; preserve the working config."""
        self._runtime()
        changes = {self.pool: render_pool(children, self.status_socket, resources)}
        if update_ini:
            changes[self.ini] = render_ini(resources)
            changes[self.cli_ini] = render_ini(resources)
        previous = {path: path.read_bytes() if path.exists() else None for path in changes}
        if all(previous[path] == value.encode('utf-8') for path, value in changes.items()):
            return False
        # Keep conservative RAM headroom for the replacement pool's initial
        # workers and shared OPcache before requesting a graceful reload.
        settings = dict(re.findall(r'^(pm\.[a-z_]+) = (\d+)$', changes[self.pool], re.M))
        startup = int(settings['pm.start_servers']) * capacity(resources)['worker_bytes']
        startup += profile(resources)['opcache_mib'] * MIB + 32 * MIB
        if resources.get('memory_available', 0) < startup:
            raise RuntimeError('RAM tersedia belum cukup untuk reload PHP-FPM yang aman.')
        php = f'/usr/sbin/php-fpm{self.php_version}'
        service = f'php{self.php_version}-fpm'
        try:
            for path, value in changes.items():
                _atomic_write(path, value, 0o644)
            self.runner([php, '-t'])
            self.runner(['systemctl', 'reload', service])
        except Exception:
            for path, value in previous.items():
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(path, value, 0o644)
            # If reload was the failing step it might already have signalled
            # FPM. Restore and reload the prior validated configuration too.
            try:
                self.runner([php, '-t'])
                self.runner(['systemctl', 'reload', service])
            except Exception:
                raise RuntimeError('Autotune dibatalkan; konfigurasi lama dipulihkan tetapi reload pemulihan gagal.') from None
            raise RuntimeError('Autotune dibatalkan; konfigurasi PHP-FPM lama dipulihkan.') from None
        return True

    def _install_units(self):
        unitdir = self.etc / 'systemd/system'
        service = ("[Unit]\nDescription=WPI adaptive PHP-FPM capacity controller\n"
                   f"After=php{self.php_version}-fpm.service\n"
                   "[Service]\nType=oneshot\nUser=root\nUMask=0077\n"
                   "ExecStart=/usr/local/bin/wpi autotune-tick\n"
                   "TimeoutStartSec=20\nNice=10\n")
        timer = ("[Unit]\nDescription=Automatically tune WPI PHP-FPM every 15 seconds\n"
                 "[Timer]\nOnBootSec=45s\nOnUnitActiveSec=15s\nAccuracySec=1s\n"
                 f"Unit={SERVICE}\n[Install]\nWantedBy=timers.target\n")
        _atomic_write(unitdir / SERVICE, service, 0o644)
        _atomic_write(unitdir / TIMER, timer, 0o644)
        # /run is cleared at reboot; the master must see the private directory
        # BEFORE starting FPM, independently of when the timer first fires.
        tmpfiles = self.etc / 'tmpfiles.d/wpi-autotune.conf'
        _atomic_write(tmpfiles, f'd {self.runtime} 0700 root root -\n', 0o644)
        dropin = self.etc / f'systemd/system/php{self.php_version}-fpm.service.d/wpi-autotune.conf'
        _atomic_write(dropin, f'[Service]\nExecStartPre=/usr/bin/install -d -m 0700 -o root -g root {self.runtime}\n', 0o644)
        self.runner(['systemctl', 'daemon-reload'])
        self.runner(['systemctl', 'enable', '--now', TIMER])

    def install(self):
        with self._lock(blocking=True) as acquired:
            if not acquired:
                raise RuntimeError('Autotune sedang berjalan.')
            resources = self._resources()
            if not resources.get('telemetry_ok'):
                raise RuntimeError('Kapasitas RAM/CPU tidak dapat dibaca; setup autotune belum selesai.')
            state = self._state()
            now = self.clock()
            children = min(capacity(resources)['capacity'], state.get('children', initial_children(resources)))
            changed = self._apply(children, resources)
            if state and not changed:
                self._install_units()
                return state.get('report', self._report(resources, None,
                                                       {**capacity(resources), 'children': children,
                                                        'reason': 'stable'}, now))
            state = {'children': children, 'last_change': now, 'saturation_ticks': 0,
                     'idle_ticks': 0, 'pressure_ticks': 0, 'last_pressure_change': None,
                     'profile': profile(resources), 'reload_pending': True,
                     'reload_requested_at': now, 'reload_from_start': state.get('fpm_start_time'),
                     'reload_from_accepted': state.get('fpm_accepted_conn'),
                     'sample': self._sample(resources, now),
                     'report': self._report(resources, None, {**capacity(resources),
                                                           'children': children, 'reason': 'initial'}, now)}
            self._save(state)
            self._install_units()
            return state['report']

    @staticmethod
    def _sample(resources, now):
        return {key: resources.get(key) for key in ('cpu_total', 'cpu_idle', 'cgroup_cpu_usec',
                                                   'swap_in', 'swap_out')} | {'time': now}

    @staticmethod
    def _derived(resources, previous, now):
        result = dict(resources)
        result['cpu_load'] = None
        interval = now - previous.get('time', now)
        host_load = None
        if 0 < interval < 120:
            if resources.get('cpu_total') is not None and previous.get('cpu_total') is not None:
                total = resources['cpu_total'] - previous['cpu_total']
                idle = resources['cpu_idle'] - previous['cpu_idle']
                if total > 0 and 0 <= idle <= total:
                    host_load = min(1.0, (total - idle) / total * resources['host_cpus'] / resources['cpus'])
            current_cg, prior_cg = resources.get('cgroup_cpu_usec'), previous.get('cgroup_cpu_usec')
            if current_cg is not None and prior_cg is not None and current_cg >= prior_cg:
                result['cpu_load'] = min(1.0, (current_cg - prior_cg) / (interval * 1e6 * resources['cpus']))
            if host_load is not None:
                result['cpu_load'] = max(result['cpu_load'] or 0, host_load)
        delta = 0
        for key in ('swap_in', 'swap_out'):
            if resources.get(key) is not None and previous.get(key) is not None:
                delta += max(0, resources[key] - previous[key])
        result['swap_delta'] = delta
        return result

    @staticmethod
    def _report(resources, telemetry, decision, now):
        reason = decision['reason']
        if decision['memory_budget'] < decision['worker_bytes'] and reason in ('initial', 'stable'):
            reason = 'memory-budget-exhausted'
        return {'enabled': True, 'updated_at': now, 'reason': reason,
                'children': decision['children'], 'capacity': decision['capacity'],
                'memory_total_mib': resources['memory_total'] // MIB,
                'memory_available_mib': resources['memory_available'] // MIB,
                'effective_cpus': round(resources['cpus'], 2),
                'worker_estimate_mib': round(decision['worker_bytes'] / MIB, 1),
                'memory_budget_mib': decision['memory_budget'] // MIB,
                'cpu_percent': round(resources['cpu_load'] * 100, 1) if resources.get('cpu_load') is not None else None,
                'memory_psi_percent': resources.get('memory_psi'),
                'queue': telemetry.get('listen queue') if telemetry and telemetry.get('queue_measured') is not False else None,
                'active': telemetry.get('active processes') if telemetry else None,
                'idle': telemetry.get('idle processes') if telemetry else None,
                'profile': profile(resources)}

    def tick(self):
        with self._lock() as acquired:
            if not acquired:
                return {'enabled': True, 'reason': 'controller-busy'}
            state = self._state()
            if not state:
                return {'enabled': False, 'reason': 'not-installed'}
            now = self.clock()
            resources = self._derived(self._resources(), state.get('sample', {}), now)
            telemetry = None
            try:
                telemetry = read_fpm_status(self.status_socket)
            except (OSError, RuntimeError, ValueError):
                pass
            if telemetry is not None:
                queue = read_socket_queue(self.runner, self.runtime.parent / f'php/php{self.php_version}-fpm.sock')
                if queue is None:
                    # Saturation based on active worker counts still permits
                    # growth; idle shrinking needs a known empty backlog.
                    telemetry['listen queue'] = 0
                    telemetry['queue_measured'] = False
                else:
                    telemetry['listen queue'] = queue
                    telemetry['queue_measured'] = True
            decision = decide(resources, telemetry, state, now)
            same_generation = (telemetry is not None and state.get('reload_from_start') is not None
                               and telemetry.get('start time') == state.get('reload_from_start'))
            if same_generation and state.get('reload_from_accepted') is not None:
                same_generation = telemetry['accepted conn'] >= state['reload_from_accepted']
            if state.get('reload_pending') and (telemetry is None or same_generation):
                decision.update({'children': state['children'], 'last_change': state['last_change'],
                                 'last_pressure_change': state.get('last_pressure_change'),
                                 'reason': 'reload-pending', 'saturation_ticks': 0, 'idle_ticks': 0})
            elif telemetry is not None:
                state['reload_pending'] = False
            profile_changed = state.get('profile') != profile(resources)
            changed = decision['children'] != state['children']
            if changed and decision['children'] < state['children']:
                settings = dict(re.findall(r'^(pm\.[a-z_]+) = (\d+)$',
                                           render_pool(decision['children'], self.status_socket, resources), re.M))
                startup = int(settings['pm.start_servers']) * decision['worker_bytes']
                startup += profile(resources)['opcache_mib'] * MIB + 32 * MIB
                if resources.get('memory_available', 0) < startup:
                    changed = False
                    decision.update({'children': state['children'], 'last_change': state['last_change'],
                                     'last_pressure_change': state.get('last_pressure_change'),
                                     'reason': 'memory-exhausted'})
            # Never make a hardware-profile increase from incomplete reads.
            pending = state.get('reload_pending') and decision['reason'] == 'reload-pending'
            if not pending and (changed or (profile_changed and resources.get('telemetry_ok'))):
                try:
                    applied = self._apply(decision['children'], resources, update_ini=profile_changed)
                    state['profile'] = profile(resources)
                    if applied:
                        state['reload_pending'] = True
                        state['reload_requested_at'] = now
                        state['reload_from_start'] = telemetry.get('start time') if telemetry else state.get('fpm_start_time')
                        state['reload_from_accepted'] = telemetry.get('accepted conn') if telemetry else state.get('fpm_accepted_conn')
                except RuntimeError:
                    decision.update({'children': state['children'], 'last_change': state['last_change'],
                                     'last_pressure_change': state.get('last_pressure_change'),
                                     'reason': 'reload-failed', 'saturation_ticks': 0, 'idle_ticks': 0})
            state.update({key: decision[key] for key in ('children', 'last_change', 'saturation_ticks',
                                                        'idle_ticks', 'pressure_ticks', 'last_pressure_change')})
            state['sample'] = self._sample(resources, now)
            if telemetry is not None:
                state['fpm_start_time'] = telemetry.get('start time')
                state['fpm_accepted_conn'] = telemetry.get('accepted conn')
            state['report'] = self._report(resources, telemetry, decision, now)
            state['report']['reload_pending'] = state.get('reload_pending', False)
            self._save(state)
            return state['report']

    def status(self):
        state = self._state()
        return state.get('report', {'enabled': False, 'reason': 'not-installed'})
