"""Transport boundaries: anonymous credentials, pinned hosts, and safe cleanup."""
import os
import json
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi.ssh import SSHSession, remote_path, validate_target

SSH_CLIENT = shutil.which('ssh')


class TargetTests(unittest.TestCase):
    def test_ipv4_and_ipv6_are_normalized(self):
        self.assertEqual(validate_target('192.0.2.3', 'root', 'secret'), ('192.0.2.3', 'root', 22))
        self.assertEqual(validate_target('2001:db8:0::3', 'ubuntu', 'secret', 2222),
                         ('2001:db8::3', 'ubuntu', 2222))

    def test_unsafe_hosts_users_ports_and_passwords_are_rejected(self):
        cases = [
            ('host.example', 'root', 'secret', 22), ('https://192.0.2.1', 'root', 'secret', 22),
            ('-oProxyCommand=id', 'root', 'secret', 22), ('127.0.0.1\nother', 'root', 'secret', 22),
            ('[2001:db8::1]', 'root', 'secret', 22), ('fe80::1%eth0', 'root', 'secret', 22),
            ('0.0.0.0', 'root', 'secret', 22), ('224.0.0.1', 'root', 'secret', 22),
            ('192.0.2.1', 'root;id', 'secret', 22), ('192.0.2.1', '-root', 'secret', 22),
            ('192.0.2.1', 'root@other', 'secret', 22), ('192.0.2.1', 'root', 'secret', True),
            ('192.0.2.1', 'root', 'secret', '22'), ('192.0.2.1', 'root', 'secret', 0),
            ('192.0.2.1', 'root', 'secret', 65536), ('192.0.2.1', 'root', '', 22),
            ('192.0.2.1', 'root', 'secret\nnext', 22), ('192.0.2.1', 'root', 'secret\rnext', 22),
            ('192.0.2.1', 'root', 'secret\x00next', 22), ('192.0.2.1', 'root', 'a' * 1025, 22),
        ]
        for args in cases:
            with self.subTest(args=(args[0], args[1], args[3])), self.assertRaises(ValueError):
                validate_target(*args)

    def test_remote_upload_paths_cannot_be_options_or_shell_syntax(self):
        self.assertEqual(remote_path('/tmp/wpi-migrate-abc/site.tar.gz'),
                         '/tmp/wpi-migrate-abc/site.tar.gz')
        for path in ('relative', '/', '/tmp/../root/x', '/tmp/./x', '/tmp//x', '/tmp/x/',
                     '/tmp/$(id)', '/tmp/x;id', '/tmp/a b', '/tmp/a\nb', '/tmp/a:b', '/tmp/a\\b'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                remote_path(path)


class SSHSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / 'state'
        self.password = "this-is-private $'\\ password"
        self.calls = []
        self.authentication_fds = []
        self.password_bytes = []
        self.response = None

        def runner(argv, **kwargs):
            self.calls.append((list(argv), dict(kwargs)))
            if argv[0] == 'sshpass':
                fd = int(argv[2])
                self.authentication_fds.append(fd)
                self.assertEqual(kwargs['pass_fds'], (fd,))
                self.password_bytes.append(os.read(fd, 2048))
            if self.response:
                return self.response(argv, kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')

        self.runner = runner
        self.which = mock.patch('wpi.ssh.shutil.which', side_effect=lambda name: '/usr/bin/' + name)
        self.which.start()
        self.addCleanup(self.which.stop)

    def session(self, username='root', host='192.0.2.1', **kwargs):
        session = SSHSession(host, username, self.password, data_dir=self.data,
                             runner=self.runner, **kwargs)
        self.addCleanup(session.close)
        return session

    def test_password_uses_inherited_anonymous_fd_then_is_closed(self):
        session = self.session().connect()
        self.assertTrue(session.connected)
        self.assertEqual(self.password_bytes, [(self.password + '\n').encode()])
        for argv, kwargs in self.calls:
            self.assertNotIn(self.password, ' '.join(argv))
            self.assertNotIn(self.password, str(kwargs['env']))
            self.assertNotIn('SSHPASS', kwargs['env'])
        for fd in self.authentication_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        master = self.calls[0][0]
        self.assertEqual(master[:2], ['sshpass', '-d'])
        self.assertIn('-N', master)
        self.assertIn('-f', master)
        self.assertIn('ControlMaster=yes', master)
        self.assertEqual(self.calls[1][0][-3:], ['-O', 'check', 'root@192.0.2.1'])

    @unittest.skipUnless(SSH_CLIENT, 'OpenSSH is needed for its read-only configuration parser.')
    def test_real_openssh_master_flags_enable_unattended_sessions(self):
        # An alive check also succeeds in ControlMaster=ask mode, but opening
        # a real command then fails without an interactive permission prompt.
        # Parse the actual generated master arguments using OpenSSH -G: this
        # detects option interactions without connecting to a server or using
        # an authentication credential.
        with self.session():
            master = next(argv for argv, _ in self.calls if argv[0] == 'sshpass')
            args = [SSH_CLIENT, '-G', *master[master.index('ssh') + 1:]]
            # The native Windows client names its empty file NUL. All master
            # flags remain identical to those supplied to OpenSSH on Ubuntu.
            args[args.index('-F') + 1] = os.devnull
            parsed = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            self.assertIn('controlmaster true', parsed.stdout.splitlines())

    def test_first_key_is_pinned_and_global_config_or_agent_cannot_override(self):
        with self.session() as session:
            session.run_root('printf hello')
            for argv, _ in self.calls:
                self.assertIn('/dev/null', argv)
                self.assertIn('GlobalKnownHostsFile=/dev/null', argv)
                self.assertIn(f'UserKnownHostsFile={session.known_hosts}', argv)
                self.assertIn('IdentityAgent=none', argv)
                self.assertIn('ForwardAgent=no', argv)
                self.assertIn('PermitLocalCommand=no', argv)
                self.assertIn('PubkeyAuthentication=no', argv)
            self.assertIn('StrictHostKeyChecking=accept-new', self.calls[0][0])
            slave = self.calls[-1][0]
            self.assertIn('StrictHostKeyChecking=yes', slave)
            self.assertIn('BatchMode=yes', slave)
            self.assertIn('PreferredAuthentications=none', slave)
            self.assertIn('PasswordAuthentication=no', slave)
            self.assertIn('ProxyCommand=false', slave)
            self.assertEqual(session.known_hosts.stat().st_size, 0)
            if os.name == 'posix':
                self.assertEqual(stat.S_IMODE(session.known_hosts.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(session.known_hosts.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(session.control_path.parent.stat().st_mode), 0o700)

    def test_root_script_does_not_forward_password_on_stdin(self):
        script = 'printf "%s" "a quote: \' and a dollar: $PATH"'
        with self.session() as session:
            session.run_root(script)
            argv, kwargs = self.calls[-1]
            self.assertEqual(shlex.split(argv[-1]), ['sh', '-c', script])
            self.assertEqual(kwargs['input'], '')
            self.assertIn('-T', argv)
            self.assertNotIn('sudo', argv[-1])

    def test_nonroot_script_receives_one_password_line_and_quoted_sudo_command(self):
        script = "printf '%s' 'a quoted value'; test -f /tmp/archive.tar.gz"
        with self.session('ubuntu') as session:
            session.run_root(script)
            argv, kwargs = self.calls[-1]
            self.assertEqual(kwargs['input'], self.password + '\n')
            remote = shlex.split(argv[-1])
            self.assertEqual(remote[:2], ['sh', '-c'])
            wrapper = remote[2]
            self.assertIn('IFS= read -r wpi_password', wrapper)
            self.assertIn("sudo -S -p '' -- sh -c " + shlex.quote(script), wrapper)
            self.assertNotIn(self.password, wrapper)
            self.assertNotIn('export', wrapper)
            self.assertIn('unset wpi_password', wrapper)

    def test_upload_is_over_private_master_and_ipv6_is_bracketed_for_scp(self):
        source = Path(self.temp.name) / '-archive.gz'
        source.write_bytes(b'archive')
        with self.session('ubuntu', '2001:db8::1', port=2222) as session:
            session.upload(source, '/tmp/wpi-stage/archive.gz')
            argv, kwargs = self.calls[-1]
            self.assertEqual(argv[:5], ['scp', '-F', '/dev/null', '-P', '2222'])
            self.assertEqual(argv[-3:], ['--', str(source.resolve()),
                                        'ubuntu@[2001:db8::1]:/tmp/wpi-stage/archive.gz'])
            self.assertIn('ControlMaster=no', argv)
            self.assertIn('BatchMode=yes', argv)
            self.assertEqual(kwargs['input'], '')
            self.assertNotIn('pass_fds', kwargs)

    def test_invalid_uploads_fail_before_another_command(self):
        with self.session() as session:
            before = len(self.calls)
            with self.assertRaises(ValueError):
                session.upload(Path(self.temp.name) / 'missing', '/tmp/x')
            source = Path(self.temp.name) / 'x'
            source.write_bytes(b'file')
            with self.assertRaises(ValueError):
                session.upload(source, '/tmp/../root/x')
            self.assertEqual(len(self.calls), before)

    def test_close_exits_only_own_master_removes_socket_and_keeps_host_pin(self):
        session = self.session().connect()
        private_dir = session.control_path.parent
        session.known_hosts.write_text('server.example ssh-ed25519 a-key\n')
        session.close()
        self.assertEqual(self.calls[-1][0][-3:], ['-O', 'exit', 'root@192.0.2.1'])
        self.assertFalse(private_dir.exists())
        self.assertIn('a-key', session.known_hosts.read_text())
        self.assertFalse(session.connected)
        self.assertIsNone(session._password)
        before = len(self.calls)
        session.close()
        self.assertEqual(len(self.calls), before)
        with self.assertRaises(ValueError):
            session.connect()

    @unittest.skipUnless(os.name == 'posix', 'Ubuntu uses a fixed short /tmp socket path.')
    def test_master_socket_ignores_a_long_user_temp_directory(self):
        long_temp = Path(self.temp.name) / ('a' * 100)
        long_temp.mkdir()
        with mock.patch.dict(os.environ, {'TMPDIR': str(long_temp)}):
            with self.session() as session:
                self.assertEqual(session.control_path.parent.parent, Path('/tmp'))
                self.assertLess(len(os.fsencode(session.control_path)), 100)

    def test_context_exception_cleans_up_and_propagates_original_error(self):
        session = self.session()
        with self.assertRaisesRegex(ValueError, 'migration failed'):
            with session:
                raise ValueError('migration failed')
        self.assertFalse(session.control_path.parent.exists())
        self.assertIsNone(session._password)
        self.assertEqual(self.calls[-1][0][-3:], ['-O', 'exit', 'root@192.0.2.1'])

    def test_authentication_failure_does_not_show_stderr_or_password_and_closes_fd(self):
        self.response = lambda argv, kwargs: subprocess.CompletedProcess(
            argv, 5 if argv[0] == 'sshpass' else 255,
            stdout=self.password, stderr='permission denied: ' + self.password)
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, 'Autentikasi SSH gagal') as exc:
            session.connect()
        self.assertNotIn(self.password, str(exc.exception))
        self.assertFalse(session.control_path.parent.exists())
        for fd in self.authentication_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertIsNone(session._password)

    def test_changed_key_rejection_is_not_retried_with_weaker_host_checking(self):
        self.response = lambda argv, kwargs: subprocess.CompletedProcess(
            argv, 7 if argv[0] == 'sshpass' else 255, stdout='', stderr='changed key')
        session = self.session()
        with self.assertRaisesRegex(RuntimeError, 'Kunci host SSH ditolak'):
            session.connect()
        self.assertEqual(sum(argv[0] == 'sshpass' for argv, _ in self.calls), 1)
        self.assertFalse(any('StrictHostKeyChecking=no' in argv for argv, _ in self.calls))

    def test_openssh_changed_key_exit_255_is_identified_without_echoing_stderr(self):
        self.response = lambda argv, kwargs: subprocess.CompletedProcess(
            argv, 255, stdout='', stderr='REMOTE HOST IDENTIFICATION HAS CHANGED ' + self.password)
        with self.assertRaisesRegex(RuntimeError, 'Kunci host SSH ditolak') as exc:
            self.session().connect()
        self.assertNotIn(self.password, str(exc.exception))

    def test_runner_exception_with_sensitive_output_is_suppressed_and_cleaned_up(self):
        def fail(argv, kwargs):
            if argv[0] == 'sshpass':
                raise subprocess.CalledProcessError(255, argv, stderr=self.password)
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        self.response = fail
        session = self.session()
        with self.assertRaises(RuntimeError) as exc:
            session.connect()
        self.assertNotIn(self.password, str(exc.exception))
        self.assertFalse(session.control_path.parent.exists())

    def test_short_password_cannot_corrupt_successful_internal_migration_protocol(self):
        marker = 'WPI_MIGRATION_PREFLIGHT_OK\n'
        report = {'migration_id': 'abcdef' * 5 + 'ab', 'status': 'ready',
                  'sites': [{'primary': 'main.example.com', 'status': 'complete'}]}
        protocol = marker + json.dumps(report) + '\n'
        for password in ('a', 'ready', '0', 'WPI_MIGRATION_PREFLIGHT_OK'):
            with self.subTest(password=password):
                self.password = password
                self.response = None
                with self.session() as session:
                    self.response = lambda argv, kwargs: subprocess.CompletedProcess(
                        argv, 0, stdout=protocol, stderr=self.password)
                    result = session.run_root('printf internal-protocol')
                    self.assertEqual(result.stdout, protocol)
                    self.assertIn('WPI_MIGRATION_PREFLIGHT_OK', result.stdout.splitlines())
                    self.assertEqual(json.loads(result.stdout.splitlines()[-1]), report)
                    self.assertEqual(result.stderr, '[redacted]')

    def test_commands_require_a_connected_session(self):
        session = self.session()
        with self.assertRaises(ValueError):
            session.run_root('true')
        with self.assertRaises(ValueError):
            session.upload('/tmp/x', '/tmp/y')
        self.assertEqual(self.calls, [])

    def test_client_dependencies_are_installed_automatically_only_when_missing(self):
        with mock.patch('wpi.ssh.shutil.which', return_value=None):
            self.session().connect()
        self.assertEqual(self.calls[0][0], ['apt-get', 'update'])
        self.assertEqual(self.calls[1][0], ['apt-get', 'install', '-y', '--no-install-recommends',
                                           'openssh-client', 'sshpass'])
        self.assertEqual(self.calls[0][1]['env']['DEBIAN_FRONTEND'], 'noninteractive')
        self.assertEqual(sum(argv[0] == 'apt-get' for argv, _ in self.calls), 2)

    @unittest.skipUnless(os.name == 'posix', 'POSIX symlink permissions apply on Ubuntu.')
    def test_symlink_known_hosts_cannot_change_an_unrelated_file(self):
        self.data.mkdir()
        ssh_dir = self.data / 'ssh'
        ssh_dir.mkdir()
        victim = Path(self.temp.name) / 'victim'
        victim.write_text('keep')
        pin = ssh_dir / 'known_hosts'
        pin.symlink_to(victim)
        with self.assertRaises(ValueError):
            self.session().connect()
        self.assertEqual(victim.read_text(), 'keep')
        self.assertFalse(any(argv[0] == 'sshpass' for argv, _ in self.calls))

    @unittest.skipUnless(os.name == 'posix', 'A real POSIX shell verifies sudo stdin framing.')
    def test_nonroot_wrapper_consumes_password_once_and_preserves_shell_script(self):
        # No sudo privilege or SSH server is used: this executable verifies
        # password framing and then executes the exact root-script arguments.
        bin_dir = Path(self.temp.name) / 'bin'
        bin_dir.mkdir()
        sudo = bin_dir / 'sudo'
        sudo.write_text("#!/bin/sh\nIFS= read -r received || exit 70\n"
                        "test \"$received\" = \"$EXPECTED_PASSWORD\" || exit 71\n"
                        "test \"$1\" = -S && test \"$2\" = -p && test -z \"$3\" "
                        "&& test \"$4\" = -- || exit 72\nshift 4\nexec \"$@\"\n")
        sudo.chmod(0o700)
        with self.session('ubuntu') as session:
            script = "printf '%s\\n' 'quotes: \" and $HOME'; "
            script += 'IFS= read -r extra && exit 73; exit 0'
            session.run_root(script)
            argv, kwargs = self.calls[-1]
            env = os.environ.copy()
            env['PATH'] = str(bin_dir) + os.pathsep + env.get('PATH', '')
            env['EXPECTED_PASSWORD'] = self.password
            result = subprocess.run(shlex.split(argv[-1]), input=kwargs['input'],
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, 'quotes: " and $HOME\n')
            self.assertNotIn(self.password, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
