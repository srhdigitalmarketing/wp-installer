"""Adaptive FPM safety policy and mutation-boundary regression tests.

All telemetry and system commands are synthetic; these tests never modify a
server and run on Windows as well as Linux.
"""
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi import autotune


class FragmentedSocket:
    """A socket returning short reads even when the caller requests more."""

    def __init__(self, data, chunk=3):
        self.data = data
        self.chunk = chunk
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.path = path

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        part = self.data[:min(size, self.chunk)]
        self.data = self.data[len(part):]
        return part

    def close(self):
        pass


def record(kind, content=b"", request=1, version=1, padding=0):
    return (struct.pack("!BBHHBB", version, kind, request, len(content), padding, 0)
            + content + b"\0" * padding)


def status_response(payload, *, headers=None, app_status=0):
    body = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    data = (headers or b"Content-Type: application/json\r\n\r\n") + body
    # CGI headers, JSON, and FCGI records are deliberately split at unrelated
    # boundaries; production sockets do not promise full-frame recv calls.
    return (record(6, data[:13], padding=3) + record(6, data[13:37])
            + record(6, data[37:]) + record(6)
            + record(3, struct.pack("!IB3x", app_status, 0)))


PHP_STATUS = {
    "pool": "www", "process manager": "dynamic", "start time": 10,
    "start since": 100, "accepted conn": 100,
    "listen queue": 0, "max listen queue": 0, "listen queue len": 128,
    "idle processes": 2, "active processes": 1, "total processes": 3,
    "max active processes": 3, "max children reached": 0, "slow requests": 0,
}


MiB = 1024 * 1024


def resources(**changes):
    sample = {
        "memory_total": 8192 * MiB, "memory_available": 6144 * MiB,
        "cpus": 4.0, "host_cpus": 4,
        "cpu_total": 10000, "cpu_idle": 8000, "cgroup_cpu_usec": None,
        "swap_in": 0, "swap_out": 0, "swap_delta": 0,
        "memory_psi": 0.0, "worker_rss": [80 * MiB],
        "telemetry_ok": True, "cpu_load": 0.20,
    }
    sample.update(changes)
    return sample


def telemetry(children=4, **changes):
    sample = {**PHP_STATUS, "total processes": children,
              "active processes": children, "idle processes": 0,
              "listen queue": 8}
    sample.update(changes)
    return sample


class AdaptivePolicyTests(unittest.TestCase):
    def exercise(self, sample=None, status=None, *, children=4, count=12,
                 last_change=0, now=1000, step=15):
        state = {"children": children, "last_change": last_change,
                 "saturation_ticks": 0, "idle_ticks": 0, "pressure_ticks": 0}
        decisions = []
        for index in range(count):
            result = autotune.decide(sample or resources(), status or telemetry(children),
                                     state, now + index * step)
            decisions.append(result)
            state.update(result)
        return decisions

    def test_sustained_queue_with_ram_and_cpu_headroom_increases_capacity(self):
        decisions = self.exercise()
        self.assertTrue(any(result["children"] > 4 for result in decisions), decisions)
        self.assertTrue(all(result["children"] <= result["capacity"] for result in decisions))

    def test_short_queue_spike_is_not_enough_to_reload(self):
        state = {"children": 4, "last_change": 0, "saturation_ticks": 0,
                 "idle_ticks": 0, "pressure_ticks": 0}
        burst = autotune.decide(resources(), telemetry(), state, 1000)
        self.assertEqual(burst["children"], 4)
        state.update(burst)
        recovery = autotune.decide(resources(), telemetry(**{
            "listen queue": 0, "active processes": 1, "idle processes": 3,
        }), state, 1015)
        self.assertEqual(recovery["children"], 4)
        self.assertEqual(recovery["saturation_ticks"], 0)

    def test_cpu_memory_swap_pressure_and_missing_telemetry_never_grow(self):
        cases = [
            resources(cpu_load=0.99), resources(memory_available=1 * MiB),
            resources(swap_delta=128), resources(memory_psi=99.0),
            resources(telemetry_ok=False), resources(cpu_load=None),
        ]
        for sample in cases:
            with self.subTest(sample=sample):
                decisions = self.exercise(sample)
                self.assertTrue(all(result["children"] <= 4 for result in decisions), decisions)

    def test_recent_reload_holds_capacity_despite_a_queue(self):
        decisions = self.exercise(count=20, now=1000, step=0.1, last_change=995)
        self.assertTrue(all(result["children"] == 4 for result in decisions), decisions)

    def test_sustained_idle_period_reduces_capacity_without_a_queue(self):
        decisions = self.exercise(status=telemetry(16, **{
            "listen queue": 0, "active processes": 0, "idle processes": 16,
        }), children=16, count=60)
        self.assertTrue(any(result["children"] < 16 for result in decisions), decisions)
        self.assertTrue(all(result["children"] >= 1 for result in decisions))

    def test_worker_memory_growth_reduces_safe_capacity(self):
        light = self.exercise(resources(worker_rss=[64 * MiB]), count=1)[0]
        heavy = self.exercise(resources(worker_rss=[1500 * MiB]), count=1)[0]
        self.assertLess(heavy["capacity"], light["capacity"])

    def test_idle_pool_below_cpu_baseline_never_grows(self):
        decisions = self.exercise(status=telemetry(1, **{
            "listen queue": 0, "active processes": 0, "idle processes": 1,
        }), children=1, count=60)
        self.assertTrue(all(result["children"] == 1 for result in decisions), decisions)

    def test_cpu_pressure_shrink_has_a_cooldown(self):
        decisions = self.exercise(resources(cpu_load=0.99), count=12)
        changes = []
        current = 4
        for decision in decisions:
            if decision["children"] != current:
                changes.append(decision["children"])
                current = decision["children"]
        self.assertEqual(len(changes), 1, decisions)

    def test_future_persisted_clock_does_not_block_growth_indefinitely(self):
        state = {"children": 4, "last_change": 2000, "saturation_ticks": 0,
                 "idle_ticks": 0, "pressure_ticks": 0}
        decision = autotune.decide(resources(), telemetry(), state, 1000)
        self.assertLessEqual(decision["last_change"], 1000)
        state.update(decision)
        for now in (1015, 1030, 1045, 1060, 1075):
            decision = autotune.decide(resources(), telemetry(), state, now)
            state.update(decision)
        self.assertGreater(decision["children"], 4)

    def test_unknown_unix_backlog_does_not_authorize_idle_shrink(self):
        decisions = self.exercise(status=telemetry(16, **{
            "listen queue": 0, "active processes": 0, "idle processes": 16,
            "queue_measured": False,
        }), children=16, count=60)
        self.assertTrue(all(result["children"] == 16 for result in decisions), decisions)


class ResourceDetectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.proc = self.base / "proc"
        self.sys = self.base / "sys"
        self.write(self.proc / "meminfo", "MemTotal: 2097152 kB\nMemAvailable: 1048576 kB\n")
        self.write(self.proc / "stat", "cpu  100 0 100 800 0 0 0 0 0 0\n" +
                   "".join(f"cpu{number} 25 0 25 200 0 0 0 0 0 0\n" for number in range(4)))
        self.write(self.proc / "vmstat", "pswpin 0\npswpout 0\n")
        self.write(self.proc / "pressure" / "memory", "some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        self.write(self.proc / "self" / "cgroup", "0::/user.slice/wpi\n")

    @staticmethod
    def write(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_nested_cgroup_v2_limits_override_host_memory_and_cpus(self):
        root = self.sys / "fs" / "cgroup"
        group = root / "user.slice" / "wpi"
        self.write(root / "memory.max", str(1024 * MiB))
        self.write(root / "memory.current", str(980 * MiB))
        self.write(root / "cpu.max", "200000 100000\n")
        self.write(group / "memory.max", str(512 * MiB))
        self.write(group / "memory.current", str(490 * MiB))
        self.write(group / "cpu.max", "50000 100000\n")
        self.write(group / "cpu.stat", "usage_usec 100000\n")
        result = autotune.detect_resources(self.proc, self.sys)
        self.assertEqual(result["memory_total"], 512 * MiB)
        self.assertLessEqual(result["memory_available"], 22 * MiB)
        self.assertEqual(result["cpus"], 0.5)

    def test_unlimited_cgroup_keeps_host_memory_limit(self):
        group = self.sys / "fs" / "cgroup" / "user.slice" / "wpi"
        self.write(group / "memory.max", "max\n")
        self.write(group / "memory.current", str(100 * MiB))
        self.write(group / "cpu.max", "max 100000\n")
        result = autotune.detect_resources(self.proc, self.sys)
        self.assertEqual(result["memory_total"], 2048 * MiB)
        self.assertEqual(result["memory_available"], 1024 * MiB)

    def test_cgroup_v1_memory_and_cpu_quota_override_host_limits(self):
        self.write(self.proc / "self" / "cgroup", "5:memory:/docker/wpi\n4:cpu,cpuacct:/docker/wpi\n")
        root = self.sys / "fs" / "cgroup"
        memory = root / "memory" / "docker" / "wpi"
        cpu = root / "cpu,cpuacct" / "docker" / "wpi"
        self.write(memory / "memory.limit_in_bytes", str(512 * MiB))
        self.write(memory / "memory.usage_in_bytes", str(490 * MiB))
        self.write(cpu / "cpu.cfs_quota_us", "25000\n")
        self.write(cpu / "cpu.cfs_period_us", "100000\n")
        self.write(cpu / "cpuacct.usage", "1000000000\n")
        result = autotune.detect_resources(self.proc, self.sys)
        self.assertEqual(result["memory_total"], 512 * MiB)
        self.assertLessEqual(result["memory_available"], 22 * MiB)
        self.assertEqual(result["cpus"], 0.25)
        self.assertEqual(result["cgroup_cpu_usec"], 1000000)

    def test_reads_php_worker_rss_and_ignores_unrelated_processes(self):
        for pid, command, rss in ((100, b"php-fpm: pool www\0", 120000),
                                  (101, b"php-fpm: pool www\0", 300000),
                                  (102, b"mysql\0", 900000)):
            folder = self.proc / str(pid)
            self.write(folder / "status", f"Name: process\nVmRSS: {rss} kB\n")
            (folder / "cmdline").write_bytes(command)
        result = autotune.detect_resources(self.proc, self.sys)
        self.assertEqual(sorted(result["worker_rss"]), [120000 * 1024, 300000 * 1024])

    def test_bad_memory_counters_cannot_be_used_as_healthy_telemetry(self):
        self.write(self.proc / "meminfo", "MemTotal: invalid\nMemAvailable: 1048576 kB\n")
        try:
            result = autotune.detect_resources(self.proc, self.sys)
        except (ValueError, OSError, RuntimeError):
            return
        self.assertFalse(result["telemetry_ok"])


class PoolRenderingTests(unittest.TestCase):
    def test_dynamic_process_counts_are_valid_even_on_tiny_servers(self):
        for children in (1, 2, 3, 8, 32):
            with self.subTest(children=children):
                body = autotune.render_pool(children, "/run/private/status.sock")
                settings = {}
                for line in body.splitlines():
                    if "=" in line and not line.lstrip().startswith(";"):
                        key, value = line.split("=", 1)
                        settings[key.strip()] = value.strip()
                self.assertEqual(settings["pm"], "dynamic")
                self.assertEqual(int(settings["pm.max_children"]), children)
                self.assertLessEqual(int(settings["pm.min_spare_servers"]),
                                     int(settings["pm.start_servers"]))
                self.assertLessEqual(int(settings["pm.start_servers"]),
                                     int(settings["pm.max_spare_servers"]))
                self.assertLessEqual(int(settings["pm.max_spare_servers"]), children)
                self.assertEqual(settings["pm.status_listen"], "/run/private/status.sock")
                self.assertGreater(int(settings["pm.max_requests"]), 0)


class ConfigurationTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.calls = []

        def runner(command, **kwargs):
            self.calls.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        self.runner = runner
        self.tuner = autotune.AutoTuner("8.3", runner, data_dir=self.base / "state",
                                       etc_root=self.base / "etc", proc_root=self.base / "proc",
                                       sys_root=self.base / "sys", run_root=self.base / "run",
                                       clock=lambda: 1000)
        for path, value in ((self.tuner.pool, b"original-pool\n"),
                            (self.tuner.ini, b"original-ini\n")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

    def test_bad_generated_config_restores_original_pool_and_php_ini(self):
        failed = False

        def runner(command, **kwargs):
            nonlocal failed
            result = self.runner(command, **kwargs)
            if "-t" in command and not failed:
                failed = True
                raise RuntimeError("Invalid generated configuration")
            if command[:2] == ["systemctl", "reload"]:
                self.assertEqual(self.tuner.pool.read_bytes(), b"original-pool\n")
                self.assertEqual(self.tuner.ini.read_bytes(), b"original-ini\n")
            return result

        self.tuner.runner = runner
        with self.assertRaisesRegex(RuntimeError, "dipulihkan"):
            self.tuner._apply(6, resources())
        self.assertEqual(self.tuner.pool.read_bytes(), b"original-pool\n")
        self.assertEqual(self.tuner.ini.read_bytes(), b"original-ini\n")
        self.assertEqual(sum("-t" in call for call in self.calls), 2)

    def test_reload_failure_restores_files_and_reloads_old_config(self):
        failed = False

        def runner(command, **kwargs):
            nonlocal failed
            result = self.runner(command, **kwargs)
            if command[:2] == ["systemctl", "reload"]:
                if not failed:
                    failed = True
                    raise RuntimeError("Reload failed")
                self.assertEqual(self.tuner.pool.read_bytes(), b"original-pool\n")
            return result

        self.tuner.runner = runner
        with self.assertRaisesRegex(RuntimeError, "dipulihkan"):
            self.tuner._apply(6, resources())
        self.assertEqual(sum(call[:2] == ["systemctl", "reload"] for call in self.calls), 2)
        self.assertEqual(self.tuner.ini.read_bytes(), b"original-ini\n")

    def test_successful_reload_validates_before_activation(self):
        self.tuner._apply(6, resources())
        self.assertEqual(self.calls[0], ["/usr/sbin/php-fpm8.3", "-t"])
        self.assertEqual(self.calls[1], ["systemctl", "reload", "php8.3-fpm"])
        self.assertIn("pm.max_children = 6", self.tuner.pool.read_text())

    def test_missing_status_does_not_change_working_capacity(self):
        self.tuner._save({
            "children": 4, "last_change": 0, "saturation_ticks": 3,
            "idle_ticks": 0, "pressure_ticks": 0,
            "profile": autotune.profile(resources()),
            "sample": {"time": 985, "cpu_total": 9500, "cpu_idle": 7600,
                       "cgroup_cpu_usec": None, "swap_in": 0, "swap_out": 0},
        })
        with mock.patch.object(autotune, "detect_resources", return_value=resources()), \
             mock.patch.object(autotune, "read_fpm_status", side_effect=OSError("Unavailable")):
            report = self.tuner.tick()
        self.assertEqual(report["children"], 4)
        self.assertEqual(report["reason"], "telemetry-unavailable")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.tuner.pool.read_bytes(), b"original-pool\n")

    def test_failed_adaptive_reload_keeps_persisted_capacity_and_retries_later(self):
        self.tuner._save({
            "children": 4, "last_change": 0, "saturation_ticks": 2,
            "idle_ticks": 0, "pressure_ticks": 0,
            "profile": autotune.profile(resources()),
            "sample": {"time": 985, "cpu_total": 9500, "cpu_idle": 7600,
                       "cgroup_cpu_usec": None, "swap_in": 0, "swap_out": 0},
        })
        with mock.patch.object(autotune, "detect_resources", return_value=resources()), \
             mock.patch.object(autotune, "read_fpm_status", return_value=telemetry()), \
             mock.patch.object(self.tuner, "_apply", side_effect=RuntimeError("Reload failed")):
            report = self.tuner.tick()
        self.assertEqual(report["children"], 4)
        self.assertEqual(report["reason"], "reload-failed")
        self.assertEqual(self.tuner._state()["children"], 4)
        self.assertEqual(self.tuner._state()["last_change"], 0)


class UnixBacklogTests(unittest.TestCase):
    def test_backlog_matches_exact_managed_listener(self):
        output = ("u_str LISTEN 99 511 /run/php/php8.3-fpm.sock.old 999 * 0\n"
                  "u_str LISTEN 0 511 /run/wpi-autotune/status.sock 998 * 0\n"
                  "u_str LISTEN 7 511 /run/php/php8.3-fpm.sock 997 * 0\n")
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout=output, stderr=""))
        queue = autotune.read_socket_queue(runner, "/run/php/php8.3-fpm.sock")
        self.assertEqual(queue, 7)
        runner.assert_called_once_with(["ss", "-xlnH"], check=False)

    def test_unavailable_queue_is_not_reported_as_zero(self):
        for code, output in ((1, ""), (0, ""),
                             (0, "u_str LISTEN ? 511 /run/php/php8.3-fpm.sock 999 * 0")):
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], code, stdout=output, stderr=""))
            with self.subTest(code=code, output=output):
                self.assertIsNone(autotune.read_socket_queue(runner, "/run/php/php8.3-fpm.sock"))


class FastCGIStatusTests(unittest.TestCase):
    def fetch(self, data, chunk=3):
        fake = FragmentedSocket(data, chunk)
        with mock.patch.object(autotune.socket, "socket", return_value=fake), \
             mock.patch.object(autotune.socket, "AF_UNIX", 1, create=True):
            result = autotune.read_fpm_status(Path("status.sock"))
        return result, fake

    def test_fragmented_headers_records_json_and_padding(self):
        actual, fake = self.fetch(status_response(PHP_STATUS), chunk=1)
        self.assertEqual(actual["listen queue"], 0)
        self.assertEqual(actual["active processes"], 1)
        self.assertIn(b"json", fake.sent)

    def test_invalid_or_truncated_status_fails_closed(self):
        cases = [
            b"", status_response(b"{invalid json}"),
            status_response({}),
            status_response({**PHP_STATUS, "listen queue": -1}),
            status_response(PHP_STATUS)[:-2],
            status_response(PHP_STATUS, app_status=1),
            status_response(PHP_STATUS, headers=b"Status: 403 Forbidden\r\n\r\n"),
            record(6, b"Content-Type: application/json\r\n\r\n{}", version=2),
        ]
        for data in cases:
            with self.subTest(data=data[:20]), self.assertRaises((ValueError, OSError, RuntimeError)):
                self.fetch(data)

    def test_process_urls_are_not_returned_in_aggregate_status(self):
        payload = {**PHP_STATUS, "processes": [{
            "request uri": "/private-customer?token=sensitive",
            "script": "/var/www/private.php", "pid": 123,
        }]}
        result, _ = self.fetch(status_response(payload))
        self.assertNotIn("processes", result)
        self.assertNotIn("sensitive", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
