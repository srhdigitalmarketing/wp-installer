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
ALIAS_PLUGIN_HEADER = "<?php\n// Managed by WPI. Edit domains through the wpi CLI.\n"
ALIAS_PLUGIN_NAME = "wpi-domain-aliases.php"
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
        # A migrated certificate keeps HTTPS available during DNS cutover.
        # Certbot owns live/ and renewal state; imported keys stay separate.
        self.migration_tls = Path("/etc/wpi/migration-tls")
        self.hook_dir = Path("/etc/letsencrypt/renewal-hooks/deploy")

    @property
    def socket(self) -> str:
        return f"/run/php/php{self.php_version}-fpm.sock"

    def _site(self, site: dict) -> dict:
        if not isinstance(site, dict) or not _SITE_ID.fullmatch(str(site.get("id", ""))):
            raise ValueError("ID situs tidak valid.")
        primary = _domain(site["primary"])
        aliases = site.get("aliases", [])
        redirects = site.get("secondary", [])
        tls = site.get("tls", [])
        if not all(isinstance(names, list) for names in (aliases, redirects, tls)):
            raise ValueError("Alias, Redirect, dan TLS harus berupa daftar domain.")
        aliases = [_domain(name) for name in aliases]
        redirects = [_domain(name) for name in redirects]
        domains = [primary, *aliases, *redirects]
        if len(set(domains)) != len(domains):
            raise ValueError("Domain situs duplikat.")
        tls = [_domain(name) for name in tls]
        if len(set(tls)) != len(tls) or not set(tls) <= set(domains):
            raise ValueError("TLS berisi domain yang tidak terpasang pada situs.")
        return {**site, "primary": primary, "aliases": aliases, "secondary": redirects,
                "root": _path(site["root"], ("/var/www/wpi",)), "tls": tls}

    def _ssl_nginx(self, domain: str) -> str:
        certificate, key = self.certificate_paths(domain)
        return (f"    ssl_certificate {certificate.as_posix()};\n"
                f"    ssl_certificate_key {key.as_posix()};\n"
                "    ssl_protocols TLSv1.2 TLSv1.3;\n")

    def _ssl_apache(self, domain: str) -> str:
        certificate, key = self.certificate_paths(domain)
        return ("    SSLEngine on\n"
                f"    SSLCertificateFile {certificate.as_posix()}\n"
                f"    SSLCertificateKeyFile {key.as_posix()}\n"
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
            for domain in [primary, *site["aliases"]]:
                domain_secure = domain in tls
                if domain_secure:
                    parts.append(self._nginx_redirect(domain, f"https://{domain}", root, False))
                    parts.append("server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n"
                                 f"    server_name {domain};\n" + self._nginx_host(domain) + self._ssl_nginx(domain)
                                 + self._nginx_wordpress(root) + "}\n")
                else:
                    parts.append("server {\n    listen 80;\n    listen [::]:80;\n"
                                 f"    server_name {domain};\n" + self._nginx_host(domain) + self._nginx_wordpress(root) + "}\n")
            for domain in site["secondary"]:
                parts.append(self._nginx_redirect(domain, target, root, False))
                if domain in tls:
                    parts.append(self._nginx_redirect(domain, target, root, True))
        else:
            for domain in [primary, *site["aliases"]]:
                domain_secure = domain in tls
                if domain_secure:
                    parts.append(self._apache_redirect(domain, f"https://{domain}", root, False))
                parts.append(f"<VirtualHost *:{443 if domain_secure else 80}>\n    ServerName {domain}\n"
                             + self._apache_host(domain)
                             + (self._ssl_apache(domain) if domain_secure else "")
                             + self._apache_wordpress(root) + "</VirtualHost>\n")
            for domain in site["secondary"]:
                parts.append(self._apache_redirect(domain, target, root, False))
                if domain in tls:
                    parts.append(self._apache_redirect(domain, target, root, True))
        return "\n".join(parts)

    def write_site(self, site: dict) -> None:
        site = self._site(site)
        # Stage WordPress's in-memory URL aliases before exposing their vhost.
        # Both files return to their former state on a failed activation.
        plugin, previous, mode, changed = self._stage_alias_plugin(site)
        try:
            self._activate(f"wpi-{site['id']}.conf", self.render_site(site))
        except BaseException:
            if changed:
                if previous is None:
                    plugin.unlink(missing_ok=True)
                else:
                    self._atomic_file(plugin, previous, mode)
            raise

    def render_alias_plugin(self, site: dict) -> str:
        """Keep WordPress links on explicitly configured Alias request hosts.

        Database options remain canonical. Redirect domains deliberately never
        enter this allowlist, and WP-CLI must always see the stored URLs.
        """
        site = self._site(site)
        tls = set(site["tls"])
        aliases = "\n".join(
            f"    '{domain}' => '{'https' if domain in tls else 'http'}://{domain}',"
            for domain in site["aliases"])
        return (ALIAS_PLUGIN_HEADER
                + "if (!defined('ABSPATH') || (defined('WP_CLI') && WP_CLI)) { return; }\n"
                "// Reject unknown Host values before registering any URL filter.\n"
                "$wpi_alias_host = strtolower((string) ($_SERVER['HTTP_HOST'] ?? ''));\n"
                "if (!preg_match('/\\A([a-z0-9.-]+)(?::(?:80|443))?\\z/D', $wpi_alias_host, $wpi_alias_match)) { return; }\n"
                "$wpi_aliases = array(\n" + aliases + "\n);\n"
                "if (!isset($wpi_aliases[$wpi_alias_match[1]])) { return; }\n"
                "$wpi_alias_origin = $wpi_aliases[$wpi_alias_match[1]];\n"
                "$wpi_alias_url = static function ($url) use ($wpi_alias_origin) {\n"
                "    return is_string($url) ? preg_replace('#\\Ahttps?://[^/]+#', $wpi_alias_origin, $url, 1) : $url;\n"
                "};\n"
                "add_filter('option_home', $wpi_alias_url, 20);\n"
                "add_filter('option_siteurl', $wpi_alias_url, 20);\n"
                "unset($wpi_alias_host, $wpi_alias_match, $wpi_aliases, $wpi_alias_origin, $wpi_alias_url);\n")

    @staticmethod
    def _alias_plugin_path(site: dict) -> Path:
        expected = f"/var/www/wpi/{site['id']}/public"
        if site["root"] != expected:
            raise ValueError("Plugin Alias memerlukan document root situs WPI yang dikelola.")
        root = Path(site["root"])
        content = root / "wp-content"
        directory = content / "mu-plugins"
        plugin = directory / ALIAS_PLUGIN_NAME
        for directory_path in (Path("/var/www/wpi"), root.parent, root, content, directory):
            if directory_path.is_symlink():
                raise RuntimeError("Menolak symlink direktori plugin Alias.")
            if directory_path.exists() and not directory_path.is_dir():
                raise RuntimeError("Direktori plugin Alias tidak valid.")
        if plugin.is_symlink() or (plugin.exists() and not plugin.is_file()):
            raise RuntimeError("Menolak symlink atau berkas plugin Alias yang tidak valid.")
        real_root, real_plugin = root.resolve(), plugin.resolve()
        if real_root not in real_plugin.parents:
            raise ValueError("Plugin Alias keluar dari document root yang dikelola.")
        return plugin

    def _stage_alias_plugin(self, site: dict) -> tuple[Path, bytes | None, int, bool]:
        plugin = self._alias_plugin_path(site)
        previous = plugin.read_bytes() if plugin.exists() else None
        mode = plugin.stat().st_mode & 0o777 if previous is not None else 0o644
        if previous is not None and not previous.startswith(ALIAS_PLUGIN_HEADER.encode()):
            raise RuntimeError("Plugin Alias sudah ada tetapi bukan milik WPI.")
        desired = self.render_alias_plugin(site).encode() if site["aliases"] else None
        if previous == desired:
            return plugin, previous, mode, False
        if desired is None:
            plugin.unlink()
        else:
            if not plugin.parent.parent.is_dir():
                raise RuntimeError("WordPress belum terpasang: wp-content tidak tersedia untuk Alias.")
            plugin.parent.mkdir(mode=0o755, exist_ok=True)
            self._atomic_file(plugin, desired, 0o644)
        return plugin, previous, mode, True

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
        return self.letsencrypt_ready(domain) or self.migrated_certificate_ready(domain)

    def letsencrypt_ready(self, domain: str) -> bool:
        domain = _domain(domain)
        return all((self.live / domain / name).is_file() for name in ("fullchain.pem", "privkey.pem"))

    def migrated_certificate_ready(self, domain: str) -> bool:
        domain = _domain(domain)
        directory = self.migration_tls / domain
        return (not directory.is_symlink() and all(
            (directory / name).is_file() and not (directory / name).is_symlink()
            for name in ("fullchain.pem", "privkey.pem")))

    def certificate_paths(self, domain: str) -> tuple[Path, Path]:
        domain = _domain(domain)
        base = self.live if self.letsencrypt_ready(domain) or not self.migrated_certificate_ready(domain) else self.migration_tls
        return base / domain / "fullchain.pem", base / domain / "privkey.pem"

    def remove_migrated_certificate(self, domain: str) -> None:
        domain = _domain(domain)
        directory = self.migration_tls / domain
        if self.migration_tls.is_symlink() or directory.is_symlink():
            raise ValueError("Direktori sertifikat migrasi tidak boleh symlink.")
        for name in ("fullchain.pem", "privkey.pem"):
            path = directory / name
            if path.is_symlink():
                raise ValueError("Sertifikat migrasi tidak boleh symlink.")
            path.unlink(missing_ok=True)
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

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
        if not self.letsencrypt_ready(domain):
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
