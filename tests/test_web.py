"""Render, activation rollback and no-database-removal contract tests."""

import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wpi.web import HEADER, WebStack


SITE = {"id": "example-abc123", "primary": "example.com",
        "secondary": ["secondary.example.com"],
        "root": "/var/www/wpi/example-abc123/public", "tls": []}


class WebRenderTests(unittest.TestCase):
    def stack(self, stack="nginx"):
        return WebStack(Mock(), stack, "8.3")

    def test_nginx_http_permalink_php_and_upload_protection(self):
        result = self.stack().render_site(SITE)
        self.assertIn("try_files $uri $uri/ /index.php?$args", result)
        self.assertIn("fastcgi_pass unix:/run/php/php8.3-fpm.sock", result)
        self.assertIn("location ~* /wp-content/uploads/.*", result)
        self.assertIn("wp-config\\.php", result)
        self.assertIn("return 301 http://example.com$request_uri", result)
        self.assertIn("if ($host != example.com) { return 444; }", result)
        self.assertNotIn("listen 443", result)

    def test_apache_http_php_and_canonical_redirect(self):
        result = self.stack("apache").render_site(SITE)
        self.assertIn('<FilesMatch "\\.php$">', result)
        self.assertIn('SetHandler "proxy:unix:/run/php/php8.3-fpm.sock|fcgi://localhost/"', result)
        self.assertIn("RewriteRule . /index.php [END]", result)
        self.assertIn("AllowOverride None", result)
        self.assertIn("RewriteRule ^ http://example.com%{REQUEST_URI} [R=301,L,NE]", result)
        self.assertIn("RewriteCond %{HTTP_HOST} !^example\\.com(?::[0-9]+)?$ [NC]", result)
        self.assertIn('wp-content/uploads">', result)
        self.assertIn("Require all denied", result)

    def test_tls_separate_certificates_and_http_acme_exception(self):
        site = {**SITE, "tls": ["example.com", "secondary.example.com"]}
        for stack in ("nginx", "apache"):
            with self.subTest(stack=stack):
                result = self.stack(stack).render_site(site)
                self.assertIn("/etc/letsencrypt/live/example.com/fullchain.pem", result)
                self.assertIn("/etc/letsencrypt/live/secondary.example.com/fullchain.pem", result)
                self.assertIn("https://example.com", result)
                self.assertIn("/.well-known/acme-challenge/", result)
                self.assertNotIn("$host$request_uri", result)
                self.assertNotIn("%{HTTP_HOST}%{REQUEST_URI}", result)
                if stack == "nginx":
                    self.assertIn("location / { return 301 https://example.com$request_uri; }", result)
                    self.assertEqual(result.count("listen 443 ssl;"), 2)
                else:
                    self.assertIn("RewriteCond %{REQUEST_URI} !^/\\.well-known/acme-challenge/", result)
                    self.assertEqual(result.count("<VirtualHost *:443>"), 2)

    def test_certificate_not_requested_for_unknown_site_alias(self):
        site = {**SITE, "tls": ["outside.example.com"]}
        with self.assertRaises(ValueError):
            self.stack().render_site(site)

    def test_domain_and_path_injection_are_rejected(self):
        bad_sites = [
            {**SITE, "id": "../../nginx"},
            {**SITE, "primary": "example.com; return 200"},
            {**SITE, "primary": "-example.com"},
            {**SITE, "primary": "Example.com"},
            {**SITE, "primary": "https://example.com"},
            {**SITE, "root": "/etc/wpi"},
            {**SITE, "root": "/var/www/wpi/../../etc"},
            {**SITE, "root": "/var/www/wpi/thing\nserver {"},
            {**SITE, "secondary": ["example.com"]},
        ]
        for site in bad_sites:
            with self.subTest(site=site), self.assertRaises(ValueError):
                self.stack().render_site(site)

    def test_pma_bootstrap_exposes_only_acme(self):
        for stack in ("nginx", "apache"):
            web = self.stack(stack)
            with self.subTest(stack=stack), patch.object(web, "certificate_ready", return_value=False):
                result = web.render_phpmyadmin("db.example.com", "/usr/share/phpmyadmin", "/etc/wpi/pma.htpasswd")
                self.assertIn("/var/www/wpi/pma-acme", result)
                self.assertNotIn("/usr/share/phpmyadmin", result)
                self.assertNotIn("fastcgi_pass", result)
                self.assertNotIn("SetHandler", result)
                self.assertNotIn("443", result)
                self.assertIn("return 503" if stack == "nginx" else "Require all denied", result)

    def test_pma_tls_has_browser_auth_and_isolated_acme(self):
        for stack in ("nginx", "apache"):
            web = self.stack(stack)
            with self.subTest(stack=stack), patch.object(web, "certificate_ready", return_value=True):
                result = web.render_phpmyadmin("db.example.com", "/usr/share/phpmyadmin", "/etc/wpi/pma.htpasswd")
                self.assertIn("/usr/share/phpmyadmin", result)
                self.assertIn("/etc/wpi/pma.htpasswd", result)
                self.assertIn("/var/www/wpi/pma-acme", result)
                self.assertIn("https://db.example.com", result)
                self.assertIn("auth_basic_user_file" if stack == "nginx" else "Require valid-user", result)
                self.assertIn("auth_basic off" if stack == "nginx" else "AuthType None", result)

    def test_render_does_not_change_supplied_state(self):
        site = copy.deepcopy(SITE)
        self.stack().render_site(site)
        self.assertEqual(site, SITE)


class WebActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.runner = Mock(return_value=subprocess.CompletedProcess([], 0))
        self.web = WebStack(self.runner, "nginx", "8.1")
        self.web.available = self.base / "available"
        self.web.enabled = self.base / "enabled"
        self.web.live = self.base / "live"
        self.web.hook_dir = self.base / "hooks"
        self.patchers = []
        # Windows may disallow symlink creation without developer mode.
        probe = self.base / "link-probe"
        try:
            probe.symlink_to(self.base / "target-probe")
            probe.unlink()
        except OSError:
            # Exercise transaction logic on Windows without elevating the
            # process. Ubuntu CI exercises real symlink operations.
            links = {}
            is_symlink, unlink = Path.is_symlink, Path.unlink
            readlink, lexists = os.readlink, os.path.lexists

            def virtual_unlink(path, *args, **kwargs):
                if str(path) in links:
                    del links[str(path)]
                else:
                    return unlink(path, *args, **kwargs)

            self.patchers = [
                patch.object(Path, "symlink_to", lambda path, target, **kw: links.__setitem__(str(path), str(target))),
                patch.object(Path, "is_symlink", lambda path: str(path) in links or is_symlink(path)),
                patch.object(Path, "unlink", virtual_unlink),
                patch("os.readlink", lambda path, **kw: links[str(path)] if str(path) in links else readlink(path, **kw)),
                patch("os.path.lexists", lambda path: str(path) in links or lexists(path)),
            ]
            for patcher in self.patchers:
                patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def test_activation_validates_before_reload(self):
        self.web.write_site(SITE)
        target = self.web.available / f"wpi-{SITE['id']}.conf"
        self.assertTrue(target.read_text().startswith(HEADER))
        self.assertEqual(os.readlink(self.web.enabled / target.name), str(target))
        self.assertEqual([call.args[0] for call in self.runner.call_args_list],
                         [["nginx", "-t"], ["systemctl", "reload", "nginx"]])

    def test_failed_syntax_check_rolls_back_existing_file(self):
        self.web.write_site(SITE)
        target = self.web.available / f"wpi-{SITE['id']}.conf"
        before = target.read_bytes()
        self.runner.reset_mock()
        self.runner.side_effect = [subprocess.CalledProcessError(1, ["nginx", "-t"]),
                                   subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
        with self.assertRaises(subprocess.CalledProcessError):
            self.web.write_site({**SITE, "primary": "new.example.com"})
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue((self.web.enabled / target.name).is_symlink())

    def test_failed_first_write_restores_absence(self):
        self.runner.side_effect = subprocess.CalledProcessError(1, ["nginx", "-t"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.web.write_site(SITE)
        self.assertEqual(list(self.web.available.iterdir()), [])
        self.assertEqual(list(self.web.enabled.iterdir()), [])

    def test_failed_removal_restores_vhost(self):
        self.web.write_site(SITE)
        target = self.web.available / f"wpi-{SITE['id']}.conf"
        before = target.read_bytes()
        self.runner.side_effect = [subprocess.CalledProcessError(1, ["nginx", "-t"]),
                                   subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
        with self.assertRaises(subprocess.CalledProcessError):
            self.web.remove_site(SITE["id"])
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue((self.web.enabled / target.name).is_symlink())

    def test_unmanaged_and_symlink_config_are_not_overwritten(self):
        self.web.available.mkdir()
        target = self.web.available / f"wpi-{SITE['id']}.conf"
        target.write_text("# someone else's config\n")
        with self.assertRaises(RuntimeError):
            self.web.write_site(SITE)
        self.assertEqual(target.read_text(), "# someone else's config\n")
        target.unlink()
        target.symlink_to(self.base / "unmanaged")
        with self.assertRaises(RuntimeError):
            self.web.write_site(SITE)
        self.assertTrue(target.is_symlink())
        self.runner.assert_not_called()

    def test_pma_remove_has_no_database_or_package_commands(self):
        with patch.object(self.web, "certificate_ready", return_value=False):
            self.web.install_phpmyadmin("db.example.com", "/usr/share/phpmyadmin", "/etc/wpi/pma.htpasswd")
        self.runner.reset_mock()
        self.web.remove_phpmyadmin("db.example.com")
        self.assertEqual([call.args[0] for call in self.runner.call_args_list],
                         [["nginx", "-t"], ["systemctl", "reload", "nginx"]])
        self.assertEqual(list(self.web.available.iterdir()), [])

    def test_certbot_is_webroot_only_with_managed_renew_hook(self):
        cert_dir = self.web.live / "example.com"
        cert_dir.mkdir(parents=True)
        for filename in ("fullchain.pem", "privkey.pem"):
            (cert_dir / filename).write_text("test fixture")
        # Avoid creating Ubuntu paths from a local Windows test.
        self.web.hook_dir.mkdir(parents=True)
        with patch("wpi.web.Path.mkdir"):
            self.web.obtain_certificate("example.com", "owner@example.com", SITE["root"])
        command = self.runner.call_args.args[0]
        self.assertEqual(command[:3], ["certbot", "certonly", "--webroot"])
        self.assertNotIn("--nginx", command)
        self.assertNotIn("--apache", command)
        self.assertIn("--non-interactive", command)
        hook = self.web.hook_dir / "wpi-reload-nginx"
        self.assertIn("nginx -t\nsystemctl reload nginx", hook.read_text())

    def test_default_guard_is_separate_and_denies_unknown_host(self):
        self.web.install_default_guard()
        config = self.web.available / "000-wpi-default.conf"
        self.assertIn("listen 80 default_server", config.read_text())
        self.assertIn("return 444", config.read_text())
        self.assertNotIn("root ", config.read_text())


if __name__ == "__main__":
    unittest.main()
