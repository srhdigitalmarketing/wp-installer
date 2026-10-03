"""Managed Ubuntu Nginx/Apache virtual hosts and webroot certificates.

This module writes only WPI-owned virtual hosts. Package installation, DNS
checks, htpasswd creation and phpMyAdmin download/configuration belong to the
CLI layer; deleting its virtual host never invokes a database command.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable


HEADER = "# Managed by WPI. Edit through the wpi CLI.\n"
_DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SITE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9_./-]+\Z")


def _domain(value: str) -> str:
    if not isinstance(value, str) or not _DOMAIN.fullmatch(value):
        raise ValueError("Domain harus nama DNS huruf kecil tanpa skema/path.")
    return value


def _path(value: str, roots: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not _SAFE_PATH.fullmatch(value):
        raise ValueError("Path berisi karakter yang tidak diizinkan.")
    candidate = PurePosixPath(value)
    if ".." in candidate.parts or str(candidate) != value:
        raise ValueError("Path harus absolut dan sudah dinormalisasi.")
    if not any(candidate == PurePosixPath(base) or PurePosixPath(base) in candidate.parents for base in roots):
        raise ValueError("Path di luar direktori WPI yang diizinkan.")
    # Resolve existing ancestors on the target Linux host, including a symlink
    # to a directory that may otherwise escape the managed area.
    if os.name == "posix":
        real = Path(value).resolve()
        if not any(real == Path(base) or Path(base) in real.parents for base in roots):
            raise ValueError("Symlink path keluar dari direktori yang diizinkan.")
    return value


class WebStack:
    """Render and atomically activate isolated managed vhosts.

    ``runner`` accepts an argv list and subprocess.run-compatible keywords.
    The ``tls`` site field is a list of domains with an issued certificate;
    certificate issuance is deliberately separate from vhost activation.
    """

    def __init__(self, runner: Callable, stack: str, php_version: str):
        if stack not in {"nginx", "apache"}:
            raise ValueError("Stack harus nginx atau apache.")
        if php_version not in {"8.1", "8.3"}:
            raise ValueError("Ubuntu 22.04/24.04 menggunakan PHP 8.1/8.3.")
        self.runner = runner
        self.stack = stack
        self.php_version = php_version
        service = "nginx" if stack == "nginx" else "apache2"
        self.service = service
        self.available = Path(f"/etc/{service}/sites-available")
        self.enabled = Path(f"/etc/{service}/sites-enabled")
        self.live = Path("/etc/letsencrypt/live")
        self.hook_dir = Path("/etc/letsencrypt/renewal-hooks/deploy")

    @property
    def socket(self) -> str:
        return f"/run/php/php{self.php_version}-fpm.sock"

    def _site(self, site: dict) -> dict:
        if not isinstance(site, dict) or not _SITE_ID.fullmatch(str(site.get("id", ""))):
            raise ValueError("ID situs tidak valid.")
        primary = _domain(site["primary"])
        aliases = site.get("secondary", [])
        tls = site.get("tls", [])
        if not isinstance(aliases, list) or not isinstance(tls, list):
            raise ValueError("Secondary dan TLS harus berupa daftar domain.")
        aliases = [_domain(name) for name in aliases]
        if len(set([primary, *aliases])) != len(aliases) + 1:
            raise ValueError("Domain situs duplikat.")
        tls = [_domain(name) for name in tls]
        if not set(tls) <= set([primary, *aliases]):
            raise ValueError("TLS berisi domain yang tidak terpasang pada situs.")
        return {**site, "primary": primary, "secondary": aliases,
                "root": _path(site["root"], ("/var/www/wpi",)), "tls": tls}

    def _ssl_nginx(self, domain: str) -> str:
        return (f"    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;\n"
                f"    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;\n"
                "    ssl_protocols TLSv1.2 TLSv1.3;\n")

    def _ssl_apache(self, domain: str) -> str:
        return ("    SSLEngine on\n"
                f"    SSLCertificateFile /etc/letsencrypt/live/{domain}/fullchain.pem\n"
                f"    SSLCertificateKeyFile /etc/letsencrypt/live/{domain}/privkey.pem\n"
                "    SSLProtocol -all +TLSv1.2 +TLSv1.3\n")

    def _nginx_acme(self, root: str) -> str:
        return ("    location ^~ /.well-known/acme-challenge/ {\n"
                f"        root {root};\n"
                "        default_type text/plain;\n"
                "        auth_basic off;\n"
                "        try_files $uri =404;\n"
                "    }\n")

    def _nginx_php(self) -> str:
        return ("    client_max_body_size 320m;\n"
                "    location = /wpi-fpm-status { return 403; }\n"
                "    location ~ \\.php$ {\n"
                "        try_files $uri =404;\n"
                "        include fastcgi_params;\n"
                "        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;\n"
                "        fastcgi_param HTTPS $https if_not_empty;\n"
                "        fastcgi_read_timeout 180s;\n"
                "        fastcgi_send_timeout 180s;\n"
                f"        fastcgi_pass unix:{self.socket};\n"
                "    }\n")

    @staticmethod
    def _nginx_host(domain: str) -> str:
        # The first vhost can be the default when no Host matches. Never let
        # this fallback expose a removed domain's former WordPress website.
        return f"    if ($host != {domain}) {{ return 444; }}\n"

    @staticmethod
    def _apache_host(domain: str) -> str:
        pattern = re.escape(domain)
        return ("    RewriteEngine On\n"
                f"    RewriteCond %{{HTTP_HOST}} !^{pattern}(?::[0-9]+)?$ [NC]\n"
                "    RewriteRule ^ - [F,END]\n")

    def _nginx_wordpress(self, root: str) -> str:
        return (f"    root {root};\n"
                "    index index.php index.html;\n"
                "    autoindex off;\n"
                + self._nginx_acme(root)
                + "    location ~* /wp-content/uploads/.*\\.(?:php[0-9]*|phtml|phar)(?:/|$) { deny all; }\n"
                "    location ~ /\\.(?!well-known(?:/|$)) { deny all; }\n"
                "    location ~* (?:^|/)(?:wp-config\\.php|readme\\.html|license\\.txt|composer\\.(?:json|lock)|package(?:-lock)?\\.json)(?:$|/) { deny all; }\n"
                "    location ~* \\.(?:sql|bak|old|orig|save|swp|ini|log)$ { deny all; }\n"
                "    location / { try_files $uri $uri/ /index.php?$args; }\n"
                + self._nginx_php())

    def _nginx_redirect(self, domain: str, target: str, root: str, tls: bool) -> str:
        listen = "    listen 443 ssl;\n    listen [::]:443 ssl;\n" if tls else "    listen 80;\n    listen [::]:80;\n"
        return ("server {\n" + listen + f"    server_name {domain};\n"
                + self._nginx_host(domain)
                + (self._ssl_nginx(domain) if tls else "")
                + self._nginx_acme(root)
                + f"    location / {{ return 301 {target}$request_uri; }}\n"
                + "}\n")

    def _apache_acme(self, root: str) -> str:
        return (f"    Alias /.well-known/acme-challenge/ {root}/.well-known/acme-challenge/\n"
                f"    <Directory \"{root}/.well-known/acme-challenge\">\n"
                "        Options None\n"
                "        AllowOverride None\n"
                "        Require all granted\n"
                "    </Directory>\n"
                "    <Location /.well-known/acme-challenge/>\n"
                "        AuthType None\n"
                "        Require all granted\n"
                "    </Location>\n")

    def _apache_php(self) -> str:
        return ("    LimitRequestBody 335544320\n"
                "    ProxyTimeout 180\n"
                "    <LocationMatch \"^/wpi-fpm-status(?:/|$)\">\n"
                "        Require all denied\n"
                "    </LocationMatch>\n"
                "    <FilesMatch \"\\.php$\">\n"
                + f"        SetHandler \"proxy:unix:{self.socket}|fcgi://localhost/\"\n"
                "    </FilesMatch>\n")

    def _apache_wordpress(self, root: str) -> str:
        return (f"    DocumentRoot {root}\n"
                "    DirectoryIndex index.php index.html\n"
                + self._apache_acme(root)
                + f"    <Directory \"{root}\">\n"
                "        Options -Indexes -ExecCGI +FollowSymLinks\n"
                "        AllowOverride None\n"
                "        Require all granted\n"
                "        RewriteEngine On\n"
                "        RewriteRule ^\\.well-known/acme-challenge/ - [END]\n"
                "        RewriteRule ^index\\.php$ - [END]\n"
                "        RewriteCond %{REQUEST_FILENAME} !-f\n"
                "        RewriteCond %{REQUEST_FILENAME} !-d\n"
                "        RewriteRule . /index.php [END]\n"
                "    </Directory>\n"
                + f"    <Directory \"{root}/wp-content/uploads\">\n"
                "        AllowOverride None\n"
                "        <FilesMatch \"(?i)\\.(php[0-9]*|phtml|phar)(/|$)\">\n"
                "            Require all denied\n"
                "        </FilesMatch>\n"
                "    </Directory>\n"
                "    <FilesMatch \"(?i)^(?:\\.|wp-config\\.php|readme\\.html|license\\.txt|composer\\.(?:json|lock)|package(?:-lock)?\\.json)|\\.(?:sql|bak|old|orig|save|swp|ini|log)$\">\n"
                "        Require all denied\n"
                "    </FilesMatch>\n"
                "    <LocationMatch \"/(?:\\.(?!well-known(?:/|$))[^/]+)(?:/|$)\">\n"
                "        Require all denied\n"
                "    </LocationMatch>\n"
                + self._apache_php())

    def _apache_redirect(self, domain: str, target: str, root: str, tls: bool) -> str:
        port = 443 if tls else 80
        return (f"<VirtualHost *:{port}>\n    ServerName {domain}\n"
                + self._apache_host(domain)
                + f"    DocumentRoot {root}\n"
                + (self._ssl_apache(domain) if tls else "")
                + self._apache_acme(root)
                + "    RewriteEngine On\n"
                "    RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/\n"
                + f"    RewriteRule ^ {target}%{{REQUEST_URI}} [R=301,L,NE]\n"
                + "</VirtualHost>\n")

    def render_site(self, site: dict) -> str:
        site = self._site(site)
        primary, root, tls = site["primary"], site["root"], set(site["tls"])
        secure = primary in tls
        target = f"{'https' if secure else 'http'}://{primary}"
        parts = [HEADER]
        if self.stack == "nginx":
            if secure:
                parts.append(self._nginx_redirect(primary, target, root, False))
                parts.append("server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n"
                             f"    server_name {primary};\n" + self._nginx_host(primary) + self._ssl_nginx(primary)
                             + self._nginx_wordpress(root) + "}\n")
            else:
                parts.append("server {\n    listen 80;\n    listen [::]:80;\n"
                             f"    server_name {primary};\n" + self._nginx_host(primary) + self._nginx_wordpress(root) + "}\n")
            for alias in site["secondary"]:
                parts.append(self._nginx_redirect(alias, target, root, False))
                if alias in tls:
                    parts.append(self._nginx_redirect(alias, target, root, True))
        else:
            if secure:
                parts.append(self._apache_redirect(primary, target, root, False))
            parts.append(f"<VirtualHost *:{443 if secure else 80}>\n    ServerName {primary}\n"
                         + self._apache_host(primary)
                         + (self._ssl_apache(primary) if secure else "")
                         + self._apache_wordpress(root) + "</VirtualHost>\n")
            for alias in site["secondary"]:
                parts.append(self._apache_redirect(alias, target, root, False))
                if alias in tls:
                    parts.append(self._apache_redirect(alias, target, root, True))
        return "\n".join(parts)

    def write_site(self, site: dict) -> None:
        site = self._site(site)
        self._activate(f"wpi-{site['id']}.conf", self.render_site(site))

    def install_default_guard(self) -> None:
        """Activate an HTTP fallback that serves no application content.

        On a clean Ubuntu server the CLI disables the package welcome vhost
        before this method. Existing external vhosts are never edited here.
        TLS fallback is protected by the per-host checks in managed SSL vhosts.
        """
        if self.stack == "nginx":
            content = (HEADER + "server {\n    listen 80 default_server;\n"
                       "    listen [::]:80 default_server;\n    server_name _;\n"
                       "    return 444;\n}\n")
        else:
            content = (HEADER + "<VirtualHost *:80>\n    ServerName wpi.invalid\n"
                       "    <Location />\n        Require all denied\n    </Location>\n"
                       "</VirtualHost>\n")
        self._activate("000-wpi-default.conf", content)

    def remove_site(self, site_id: str) -> None:
        if not isinstance(site_id, str) or not _SITE_ID.fullmatch(site_id):
            raise ValueError("ID situs tidak valid.")
        self._activate(f"wpi-{site_id}.conf", None)

    def validate_reload(self) -> None:
        command = ["nginx", "-t"] if self.stack == "nginx" else ["apache2ctl", "configtest"]
        self.runner(command, check=True, capture_output=True, text=True)
        self.runner(["systemctl", "reload", self.service], check=True, capture_output=True, text=True)

    @staticmethod
    def _atomic_file(path: Path, content: bytes, mode: int = 0o644) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _link_targets(link: Path, target: Path, recorded: str) -> bool:
        # Windows readlink may return a \\?\ namespace path for an ordinary
        # C:\ path. Compare the actual files before the lexical fallback used
        # for a managed dangling symlink on Linux.
        try:
            return os.path.samefile(link, target)
        except OSError:
            return Path(recorded).resolve() == target.resolve()

    def _activate(self, filename: str, content: str | None) -> None:
        """Rollback disk configuration if syntax checking or reloading fails."""
        self.available.mkdir(parents=True, exist_ok=True)
        self.enabled.mkdir(parents=True, exist_ok=True)
        target, link = self.available / filename, self.enabled / filename
        if target.is_symlink():
            raise RuntimeError("Menolak menimpa symlink konfigurasi.")
        previous = target.read_bytes() if target.exists() else None
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
        if previous is not None and not previous.startswith(HEADER.encode()):
            raise RuntimeError("Konfigurasi sudah ada tetapi bukan milik WPI.")
        old_link = os.readlink(link) if link.is_symlink() else None
        if os.path.lexists(link) and (old_link is None or not self._link_targets(link, target, old_link)):
            raise RuntimeError("Sites-enabled berisi konfigurasi bukan milik WPI.")
        try:
            if content is None:
                if os.path.lexists(link):
                    link.unlink()
                if target.exists():
                    target.unlink()
            else:
                self._atomic_file(target, content.encode())
                if old_link is None:
                    link.symlink_to(target)
            self.validate_reload()
        except BaseException:
            if os.path.lexists(link):
                link.unlink()
            if previous is None:
                if target.exists():
                    target.unlink()
            else:
                self._atomic_file(target, previous, mode)
            if old_link is not None:
                link.symlink_to(old_link)
            # A reload error may have happened after the server accepted the
            # new config. Restore the former config in memory as well.
            try:
                self.validate_reload()
            except Exception:
                pass
            raise

    def certificate_ready(self, domain: str) -> bool:
        domain = _domain(domain)
        return all((self.live / domain / name).is_file() for name in ("fullchain.pem", "privkey.pem"))

    def obtain_certificate(self, domain: str, email: str, webroot: str) -> None:
        domain = _domain(domain)
        webroot = _path(webroot, ("/var/www/wpi", "/usr/share/phpmyadmin"))
        if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or email.startswith("-"):
            raise ValueError("Alamat email SSL tidak valid.")
        Path(webroot, ".well-known", "acme-challenge").mkdir(parents=True, exist_ok=True)
        self.hook_dir.mkdir(parents=True, exist_ok=True)
        hook = self.hook_dir / f"wpi-reload-{self.service}"
        command = "nginx -t" if self.stack == "nginx" else "apache2ctl configtest"
        hook_content = ("#!/bin/sh\n# Managed by WPI.\nset -eu\n"
                        f"{command}\nsystemctl reload {self.service}\n")
        if hook.is_symlink():
            raise RuntimeError("Menolak symlink deploy hook.")
        if hook.exists() and not hook.read_text().startswith("#!/bin/sh\n# Managed by WPI.\n"):
            raise RuntimeError("Deploy hook sudah ada dan bukan milik WPI.")
        self._atomic_file(hook, hook_content.encode(), 0o755)
        self.runner(["certbot", "certonly", "--webroot", "--webroot-path", webroot,
                     "--cert-name", domain, "--domain", domain, "--email", email,
                     "--agree-tos", "--non-interactive", "--keep-until-expiring"],
                    check=True, capture_output=True, text=True)
        if not self.certificate_ready(domain):
            raise RuntimeError("Certbot selesai tetapi berkas sertifikat belum tersedia.")

    def render_phpmyadmin(self, domain: str, root: str, auth_file: str) -> str:
        domain = _domain(domain)
        root = _path(root, ("/usr/share/phpmyadmin", "/var/www/wpi"))
        auth_file = _path(auth_file, ("/etc/wpi",))
        acme_root = "/var/www/wpi/pma-acme"
        secure = self.certificate_ready(domain)
        target = f"https://{domain}"
        if self.stack == "nginx":
            # A bootstrap vhost exposes only ACME. The browser application is
            # never accessible over cleartext, even before certificate issuance.
            if not secure:
                return (HEADER + "server {\n    listen 80;\n    listen [::]:80;\n"
                        f"    server_name {domain};\n" + self._nginx_host(domain) + self._nginx_acme(acme_root)
                        + "    location / { return 503; }\n}\n")
            return (HEADER + self._nginx_redirect(domain, target, acme_root, False)
                    + "server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n"
                    f"    server_name {domain};\n" + self._nginx_host(domain) + self._ssl_nginx(domain)
                    + f"    root {root};\n    index index.php;\n    autoindex off;\n"
                    "    auth_basic \"phpMyAdmin\";\n"
                    + f"    auth_basic_user_file {auth_file};\n"
                    + self._nginx_acme(acme_root)
                    + "    location ~ /\\. { deny all; }\n"
                    "    location ~ ^/(?:setup|libraries|templates)/ { deny all; }\n"
                    "    location ~* (?:config\\.inc\\.php|\\.(?:sql|bak|ini|log))$ { deny all; }\n"
                    "    location / { try_files $uri $uri/ =404; }\n"
                    + self._nginx_php() + "}\n")
        if not secure:
            return (HEADER + f"<VirtualHost *:80>\n    ServerName {domain}\n"
                    + self._apache_host(domain)
                    + f"    DocumentRoot {acme_root}\n" + self._apache_acme(acme_root)
                    + "    <Location />\n        Require all denied\n    </Location>\n"
                    # Location sections merge in order; ACME's exception last.
                    + "    <Location /.well-known/acme-challenge/>\n        Require all granted\n    </Location>\n"
                    + "</VirtualHost>\n")
        return (HEADER + self._apache_redirect(domain, target, acme_root, False)
                + f"<VirtualHost *:443>\n    ServerName {domain}\n    DocumentRoot {root}\n"
                + self._apache_host(domain) + self._ssl_apache(domain) + "    DirectoryIndex index.php\n"
                + f"    <Directory \"{root}\">\n        Options -Indexes +FollowSymLinks\n"
                "        AllowOverride None\n        AuthType Basic\n        AuthName \"phpMyAdmin\"\n"
                + f"        AuthUserFile {auth_file}\n        Require valid-user\n    </Directory>\n"
                + self._apache_acme(acme_root)
                + "    <LocationMatch \"^/(?:setup|libraries|templates)/\">\n        Require all denied\n    </LocationMatch>\n"
                "    <FilesMatch \"(?i)^(?:\\.|config\\.inc\\.php)|\\.(?:sql|bak|ini|log)$\">\n        Require all denied\n    </FilesMatch>\n"
                + self._apache_php() + "</VirtualHost>\n")

    def install_phpmyadmin(self, domain: str, root: str, auth_file: str) -> None:
        domain = _domain(domain)
        content = self.render_phpmyadmin(domain, root, auth_file)
        self._activate(self._pma_filename(domain), content)

    def remove_phpmyadmin(self, domain: str) -> None:
        domain = _domain(domain)
        self._activate(self._pma_filename(domain), None)

    @staticmethod
    def _pma_filename(domain: str) -> str:
        # A valid hostname may contain 253 bytes, exceeding the filesystem
        # filename limit once a config prefix and extension are added.
        return f"wpi-pma-{hashlib.sha256(domain.encode()).hexdigest()}.conf"
