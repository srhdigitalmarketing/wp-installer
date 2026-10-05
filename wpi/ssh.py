"""Password-authenticated migration transport using the Ubuntu OpenSSH client.

Only the short-lived master connection receives the login password. Subsequent
commands and uploads use its private control socket. Passwords never enter a
command argument, an environment variable, a credential file, or an error.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile


def validate_target(host, username, password, port=22):
    """Validate values before using them in OpenSSH arguments or sudo stdin."""
    if not isinstance(host, str) or host != host.strip() or '%' in host:
        raise ValueError('Masukkan alamat IP server baru yang valid.')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError('Masukkan alamat IP server baru, bukan domain atau URL.') from None
    if address.is_unspecified or address.is_multicast:
        raise ValueError('Alamat IP server baru tidak boleh unspecified atau multicast.')
    if not isinstance(username, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', username):
        raise ValueError('Username SSH tidak valid.')
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('Port SSH harus antara 1 dan 65535.')
    if not isinstance(password, str) or not password or any(c in password for c in '\r\n\x00'):
        raise ValueError('Password SSH wajib diisi dan tidak boleh berisi baris baru atau NUL.')
    # Writing one bounded value before spawning avoids a blocked pipe writer.
    try:
        encoded = password.encode('utf-8')
    except UnicodeError:
        raise ValueError('Password SSH harus berupa teks UTF-8 yang valid.') from None
    if len(encoded) > 1024:
        raise ValueError('Password SSH terlalu panjang (maksimum 1024 byte).')
    return str(address), username, port


def remote_path(value):
    """A deliberately restricted absolute upload path, safe for old scp too."""
    value = str(value)
    if (not re.fullmatch(r'/[A-Za-z0-9_./-]+', value)
            or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or value.endswith('/') or value == '/'):
        raise ValueError('Path tujuan SSH harus berupa path absolut yang aman.')
    return str(PurePosixPath(value))


class SSHSession:
    """One isolated password-authenticated SSH master; use as a context manager.

    Non-root users must have sudo access with their SSH login password. The
    transport intentionally does not support an interactive MFA challenge.
    ``runner`` is injectable for command-boundary tests and has subprocess.run's
    interface; real execution is POSIX-only, as WPI runs on Ubuntu.
    """

    def __init__(self, host, username, password, port=22, data_dir=Path('/var/lib/wpi'),
                 runner=subprocess.run):
        self.host, self.username, self.port = validate_target(host, username, password, port)
        self._password = password
        self.data = Path(data_dir)
        self.runner = runner
        self.known_hosts = self.data / 'ssh' / 'known_hosts'
        self.control_path = None
        self._temporary = None
        self._connected = False
        self._master_attempted = False
        self._closed = False

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    @property
    def connected(self):
        return self._connected

    def _environment(self):
        env = os.environ.copy()
        for name in ('SSHPASS', 'SSH_AUTH_SOCK', 'SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE'):
            env.pop(name, None)
        env.update(LC_ALL='C', DEBIAN_FRONTEND='noninteractive')
        return env

    def _execute(self, argv, *, timeout=None, check=True, **kwargs):
        # Even an injected runner must not leak remote output or the stdin
        # password through its exception representation.
        try:
            result = self.runner(argv, check=False, capture_output=True, text=True,
                                 env=self._environment(), timeout=timeout, **kwargs)
        except (OSError, subprocess.SubprocessError, RuntimeError):
            raise RuntimeError('Perintah SSH gagal atau melewati batas waktu; detail sensitif tidak ditampilkan.') from None
        if check and result.returncode:
            output = str(getattr(result, 'stderr', '') or '')
            if ('REMOTE HOST IDENTIFICATION HAS CHANGED' in output
                    or 'Host key verification failed' in output):
                message = 'Kunci host SSH ditolak. Periksa identitas server baru sebelum mencoba lagi.'
            elif argv[0] == 'sshpass' and result.returncode == 5:
                message = 'Autentikasi SSH gagal. Periksa username dan password server baru.'
            elif argv[0] == 'sshpass' and result.returncode in (6, 7):
                message = 'Kunci host SSH ditolak. Periksa identitas server baru sebelum mencoba lagi.'
            else:
                message = f'Perintah {Path(argv[0]).name} gagal (exit {result.returncode}); detail sensitif tidak ditampilkan.'
            raise RuntimeError(message) from None
        # Remote commands can produce output for the migration protocol. Avoid
        # returning a password accidentally echoed by a remote login wrapper.
        for attr in ('stdout', 'stderr'):
            value = getattr(result, attr, None)
            if isinstance(value, str) and self._password:
                setattr(result, attr, value.replace(self._password, '[redacted]'))
        return result

    def _install_clients(self):
        missing = []
        if not shutil.which('ssh') or not shutil.which('scp'):
            missing.append('openssh-client')
        if not shutil.which('sshpass'):
            missing.append('sshpass')
        if missing:
            self._execute(['apt-get', 'update'], timeout=600)
            self._execute(['apt-get', 'install', '-y', '--no-install-recommends', *missing],
                          timeout=900)

    def _private_paths(self):
        # The WPI state directory is root-owned on Ubuntu. Reject symlinks so
        # chmod, host pinning and socket creation cannot follow another path.
        for directory in (self.data, self.known_hosts.parent):
            if directory.is_symlink():
                raise ValueError('Direktori state SSH tidak boleh berupa symlink.')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not directory.is_dir():
                raise ValueError('Direktori state SSH tidak valid.')
            os.chmod(directory, 0o700)
        if self.known_hosts.is_symlink():
            raise ValueError('File known_hosts SSH tidak boleh berupa symlink.')
        flags = (os.O_CREAT | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
                 | getattr(os, 'O_NONBLOCK', 0))
        try:
            fd = os.open(self.known_hosts, flags, 0o600)
        except OSError:
            raise ValueError('File known_hosts SSH tidak dapat dibuka dengan aman.') from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('File known_hosts SSH harus berupa file biasa.')
            os.fchmod(fd, 0o600) if hasattr(os, 'fchmod') else os.chmod(self.known_hosts, 0o600)
        finally:
            os.close(fd)
        # /tmp is short enough for the Unix-domain socket limit. mkdtemp makes
        # this directory private; only this session ever knows its socket path.
        self._temporary = tempfile.TemporaryDirectory(
            prefix='wpi-ssh-', dir='/tmp' if os.name == 'posix' else None)
        os.chmod(self._temporary.name, 0o700)
        self.control_path = Path(self._temporary.name) / 'master'

    def _options(self, *, master=False):
        values = [
            'StrictHostKeyChecking=accept-new' if master else 'StrictHostKeyChecking=yes',
            f'UserKnownHostsFile={self.known_hosts}',
            'GlobalKnownHostsFile=/dev/null', 'UpdateHostKeys=no',
            'IdentityAgent=none', 'IdentitiesOnly=yes', 'ForwardAgent=no',
            'ForwardX11=no', 'ClearAllForwardings=yes', 'PermitLocalCommand=no',
            'ConnectTimeout=20', 'ConnectionAttempts=1',
            'ServerAliveInterval=15', 'ServerAliveCountMax=3',
            'NumberOfPasswordPrompts=1', 'PubkeyAuthentication=no',
            f'ControlPath={self.control_path}',
            'ControlMaster=yes' if master else 'ControlMaster=no',
            'ControlPersist=no', 'BatchMode=no' if master else 'BatchMode=yes',
            'PreferredAuthentications=password,keyboard-interactive' if master else 'PreferredAuthentications=none',
            'PasswordAuthentication=yes' if master else 'PasswordAuthentication=no',
            'KbdInteractiveAuthentication=yes' if master else 'KbdInteractiveAuthentication=no',
        ]
        if not master:
            # OpenSSH otherwise falls back to a fresh TCP connection when the
            # control socket disappears. Deny that fallback even if a remote
            # server happened to accept unauthenticated ("none") sessions.
            values.append('ProxyCommand=false')
        return [item for value in values for item in ('-o', value)]

    def _ssh(self, *arguments):
        return ['ssh', '-F', '/dev/null', '-p', str(self.port), *self._options(),
                *arguments, f'{self.username}@{self.host}']

    def connect(self):
        if self._closed:
            raise ValueError('Sesi SSH sudah ditutup. Buat sesi baru untuk mencoba lagi.')
        if self._connected:
            return self
        try:
            self._install_clients()
            self._private_paths()
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, self._password.encode('utf-8') + b'\n')
                os.close(write_fd)
                write_fd = None
                command = ['sshpass', '-d', str(read_fd), 'ssh', '-F', '/dev/null',
                           '-p', str(self.port), *self._options(master=True),
                           '-M', '-f', '-N', f'{self.username}@{self.host}']
                self._master_attempted = True
                self._execute(command, timeout=90, pass_fds=(read_fd,), input='')
            finally:
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
            # Verify that the private master, rather than a direct fallback,
            # really exists before any privileged operation is attempted.
            self._execute(self._ssh('-O', 'check'), timeout=20, input='')
            self._connected = True
            return self
        except BaseException:
            self.close()
            raise

    def _require_connection(self):
        if not self._connected or self._closed:
            raise ValueError('Sesi SSH belum terhubung atau sudah ditutup.')

    def run_root(self, script):
        """Run a root shell script; commands must read files, not SSH stdin."""
        self._require_connection()
        if not isinstance(script, str) or not script.strip() or '\x00' in script:
            raise ValueError('Script remote SSH tidak valid.')
        if self.username == 'root':
            remote = 'sh -c ' + shlex.quote(script)
            payload = ''
        else:
            wrapper = ("IFS= read -r wpi_password || exit 65; "
                       "printf '%s\\n' \"$wpi_password\" | sudo -S -p '' -- sh -c "
                       + shlex.quote(script) + "; wpi_rc=$?; unset wpi_password; exit \"$wpi_rc\"")
            remote = 'sh -c ' + shlex.quote(wrapper)
            payload = self._password + '\n'
        return self._execute([*self._ssh('-T'), remote], input=payload)

    def upload(self, source, destination):
        self._require_connection()
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            raise ValueError('Sumber upload SSH harus berupa file biasa.')
        destination = remote_path(destination)
        # Resolve source to an absolute path so a filename beginning with '-'
        # cannot become an scp option, and cannot be parsed as host:path.
        source = source.resolve()
        host = f'[{self.host}]' if ':' in self.host else self.host
        argv = ['scp', '-F', '/dev/null', '-P', str(self.port), *self._options(),
                '--', str(source), f'{self.username}@{host}:{destination}']
        return self._execute(argv, input='')

    def close(self):
        if self._closed:
            return
        try:
            if self._master_attempted and self.control_path is not None:
                try:
                    self._execute(self._ssh('-O', 'exit'), check=False, timeout=20, input='')
                except RuntimeError:
                    pass
        finally:
            self._connected = False
            self._password = None
            self._closed = True
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
