"""Render, activation rollback and no-database-removal contract tests."""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wpi.web import ALIAS_PLUGIN_HEADER, ALIAS_PLUGIN_NAME, HEADER, WebStack


SITE = {"id": "example-abc123", "primary": "example.com",
        "secondary": ["secondary.example.com"],
        "root": "/var/www/wpi/example-abc123/public", "tls": []}


class WebRenderTests(unittest.TestCase):
    def stack(self, stack="nginx"):
        return WebStack(Mock(), stack, "8.3")

    def test_migrated_certificate_cleanup_removes_only_selected_hostname_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            web = self.stack()
            web.migration_tls = Path(temporary)
            for host in ('example.com', 'other.example.com'):
                folder = web.migration_tls / host
                folder.mkdir()
                (folder / 'fullchain.pem').write_text('certificate')
                (folder / 'privkey.pem').write_text('private')
            web.remove_migrated_certificate('example.com')
            self.assertFalse((web.migration_tls / 'example.com').exists())
            self.assertTrue((web.migration_tls / 'other.example.com' / 'privkey.pem').is_file())

    def test_fpm_status_endpoint_is_private_for_wordpress_and_phpmyadmin(self):
        for name in ('nginx', 'apache'):
            web = self.stack(name)
            with self.subTest(stack=name), patch.object(web, 'certificate_ready', return_value=True):
                for content in (web.render_site(SITE), web.render_phpmyadmin(
                        'db.example.com', '/usr/share/phpmyadmin', '/etc/wpi/pma.htpasswd')):
                    self.assertIn('/wpi-fpm-status', content)
                    self.assertIn('return 403' if name == 'nginx' else 'Require all denied', content)

    def test_configured_body_limit_matches_php_post_size_on_both_stacks_and_pma(self):
        for name in ('nginx', 'apache'):
            web = WebStack(Mock(), name, '8.3', post_max_size_mb=282)
            expected = 'client_max_body_size 282m;' if name == 'nginx' else 'LimitRequestBody 295698432'
            with self.subTest(stack=name), patch.object(web, 'certificate_ready', return_value=True):
                for content in (web.render_site(SITE), web.render_phpmyadmin(
                        'db.example.com', '/usr/share/phpmyadmin', '/etc/wpi/pma.htpasswd')):
                    self.assertIn(expected, content)
        for value in (0, -1, 2048, True, '500m;evil'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WebStack(Mock(), 'nginx', '8.3', post_max_size_mb=value)

    def test_apache_declared_body_uses_numeric_early_413_before_fastcgi_and_keeps_stream_limit(self):
        web = WebStack(Mock(), 'apache', '8.3', post_max_size_mb=16)
        condition = ("RewriteCond expr \"req_novary('Content-Length') =~ /^[0-9]+$/ "
                     "&& req_novary('Content-Length') -gt 16777216\"")
        with patch.object(web, 'certificate_ready', return_value=True):
            for content in (web.render_site(SITE), web.render_phpmyadmin(
                    'db.example.com', '/usr/share/phpmyadmin', '/etc/wpi/pma.htpasswd')):
                self.assertIn('LimitRequestBody 16777216', content)
                self.assertIn(condition, content)
                self.assertIn('RewriteRule ^ - [R=413,END]', content)
                self.assertLess(content.index(condition), content.index('SetHandler'))
                self.assertNotIn('ErrorDocument 503', content)

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

    def test_migration_certificate_fallback_keeps_https_and_prefers_certbot_lineage(self):
        with tempfile.TemporaryDirectory() as temporary:
            for stack in ('nginx', 'apache'):
                web = self.stack(stack)
                web.live = Path(temporary) / stack / 'live'
                web.migration_tls = Path(temporary) / stack / 'migration'
                fallback = web.migration_tls / 'example.com'
                fallback.mkdir(parents=True)
                for name in ('fullchain.pem', 'privkey.pem'):
                    (fallback / name).write_text('migrated certificate')
                self.assertTrue(web.certificate_ready('example.com'))
                self.assertFalse(web.letsencrypt_ready('example.com'))
                site = {**SITE, 'secondary': [], 'tls': ['example.com']}
                rendered = web.render_site(site)
                self.assertIn((fallback / 'fullchain.pem').as_posix(), rendered)
                live = web.live / 'example.com'
                live.mkdir(parents=True)
                for name in ('fullchain.pem', 'privkey.pem'):
                    (live / name).write_text('new certificate')
                self.assertTrue(web.letsencrypt_ready('example.com'))
                self.assertEqual(web.certificate_paths('example.com'), (live / 'fullchain.pem', live / 'privkey.pem'))
                self.assertNotIn((fallback / 'fullchain.pem').as_posix(), web.render_site(site))

    def test_migration_certificate_requires_both_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            web = self.stack()
            web.live = Path(temporary) / 'live'
            web.migration_tls = Path(temporary) / 'migration'
            fallback = web.migration_tls / 'example.com'
            fallback.mkdir(parents=True)
            (fallback / 'fullchain.pem').write_text('certificate')
            self.assertFalse(web.certificate_ready('example.com'))
            (fallback / 'privkey.pem').write_text('key')
            self.assertTrue(web.certificate_ready('example.com'))

    def test_alias_serves_wordpress_without_primary_redirect(self):
        site = {**SITE, "aliases": ["alias.example.com"]}
        for name in ("nginx", "apache"):
            with self.subTest(stack=name):
                result = self.stack(name).render_site(site)
                marker = "server_name alias.example.com;" if name == "nginx" else "ServerName alias.example.com"
                section = result.split(marker, 1)[1].split("server {" if name == "nginx" else "<VirtualHost", 1)[0]
                self.assertIn("try_files $uri $uri/ /index.php?$args" if name == "nginx"
                              else "RewriteRule . /index.php [END]", section)
                self.assertIn(SITE["root"], section)
                self.assertNotIn("301", section)
                self.assertNotIn("http://example.com", section)

    def test_alias_https_upgrade_keeps_own_host_and_certificate(self):
        site = {**SITE, "aliases": ["alias.example.com"],
                "tls": ["example.com", "alias.example.com"]}
        for name in ("nginx", "apache"):
            with self.subTest(stack=name):
                result = self.stack(name).render_site(site)
                self.assertIn("https://alias.example.com", result)
                self.assertIn("/etc/letsencrypt/live/alias.example.com/fullchain.pem", result)
                self.assertEqual(result.count("listen 443 ssl;" if name == "nginx" else "<VirtualHost *:443>"), 2)
                expected = ("return 301 https://alias.example.com$request_uri;" if name == "nginx"
                            else "RewriteRule ^ https://alias.example.com%{REQUEST_URI} [R=301,L,NE]")
                self.assertIn(expected, result)

    def test_duplicate_domain_across_any_role_is_rejected(self):
        for aliases, redirects in ((["example.com"], []), (["same.example.com"], ["same.example.com"]),
                                   (["same.example.com", "same.example.com"], [])):
            with self.subTest(aliases=aliases, redirects=redirects), self.assertRaises(ValueError):
                self.stack().render_site({**SITE, "aliases": aliases, "secondary": redirects})

    def test_invalid_alias_collection_or_tls_duplicate_is_rejected(self):
        for update in ({"aliases": "alias.example.com"}, {"aliases": ["BAD.example.com"]},
                       {"tls": ["example.com", "example.com"]}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.stack().render_site({**SITE, **update})

    def test_alias_plugin_contains_only_aliases_with_correct_scheme(self):
        site = {**SITE, "aliases": ["alias.example.com", "plain.example.com"],
                "tls": ["alias.example.com"]}
        original = copy.deepcopy(site)
        plugin = self.stack().render_alias_plugin(site)
        self.assertTrue(plugin.startswith(ALIAS_PLUGIN_HEADER))
        self.assertIn("'alias.example.com' => 'https://alias.example.com'", plugin)
        self.assertIn("'plain.example.com' => 'http://plain.example.com'", plugin)
        self.assertNotIn("secondary.example.com", plugin)
        self.assertNotIn("'example.com'", plugin)
        self.assertNotIn("update_option", plugin)
        self.assertEqual(site, original)

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
            samefile = os.path.samefile

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
                patch("os.path.samefile", lambda first, second: samefile(links.get(str(first), first), links.get(str(second), second))),
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
        self.assertTrue(os.path.samefile(self.web.enabled / target.name, target))
        self.assertEqual([call.args[0] for call in self.runner.call_args_list],
                         [["nginx", "-t"], ["systemctl", "reload", "nginx"]])

    def local_plugin(self):
        content = self.base / "wordpress" / "wp-content"
        content.mkdir(parents=True, exist_ok=True)
        return content / "mu-plugins" / ALIAS_PLUGIN_NAME

    def test_alias_plugin_is_managed_and_removed_with_last_alias(self):
        plugin = self.local_plugin()
        alias_site = {**SITE, "aliases": ["alias.example.com"]}
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin):
            self.web.write_site(alias_site)
            self.assertEqual(plugin.read_text(), self.web.render_alias_plugin(alias_site))
            self.assertTrue(plugin.read_text().startswith(ALIAS_PLUGIN_HEADER))
            self.assertEqual(plugin.stat().st_mode & 0o777, 0o644 if os.name == "posix" else 0o666)
            self.web.write_site(SITE)
            self.assertFalse(plugin.exists())

    def test_failed_alias_update_restores_both_vhost_and_plugin(self):
        plugin = self.local_plugin()
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin):
            self.web.write_site({**SITE, "aliases": ["first.example.com"]})
            previous_plugin = plugin.read_bytes()
            target = self.web.available / f"wpi-{SITE['id']}.conf"
            previous_vhost = target.read_bytes()
            self.runner.side_effect = [subprocess.CalledProcessError(1, ["nginx", "-t"]),
                                       subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 0)]
            with self.assertRaises(subprocess.CalledProcessError):
                self.web.write_site({**SITE, "aliases": ["second.example.com"]})
            self.assertEqual(plugin.read_bytes(), previous_plugin)
            self.assertEqual(target.read_bytes(), previous_vhost)

    def test_failed_alias_first_write_restores_plugin_absence(self):
        plugin = self.local_plugin()
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin):
            self.runner.side_effect = subprocess.CalledProcessError(1, ["nginx", "-t"])
            with self.assertRaises(subprocess.CalledProcessError):
                self.web.write_site({**SITE, "aliases": ["alias.example.com"]})
        self.assertFalse(plugin.exists())

    def test_failed_alias_removal_restores_managed_plugin(self):
        plugin = self.local_plugin()
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin):
            self.web.write_site({**SITE, "aliases": ["alias.example.com"]})
            before = plugin.read_bytes()
            self.runner.side_effect = subprocess.CalledProcessError(1, ["nginx", "-t"])
            with self.assertRaises(subprocess.CalledProcessError):
                self.web.write_site(SITE)
        self.assertEqual(plugin.read_bytes(), before)

    def test_alias_does_not_replace_unmanaged_plugin(self):
        plugin = self.local_plugin()
        plugin.parent.mkdir()
        plugin.write_text("<?php // another application\n")
        before = plugin.read_bytes()
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin), self.assertRaises(RuntimeError):
            self.web.write_site({**SITE, "aliases": ["alias.example.com"]})
        self.assertEqual(plugin.read_bytes(), before)
        self.runner.assert_not_called()

    def test_alias_requires_existing_wordpress_content(self):
        plugin = self.base / "absent" / "wp-content" / "mu-plugins" / ALIAS_PLUGIN_NAME
        with patch.object(self.web, "_alias_plugin_path", return_value=plugin), self.assertRaises(RuntimeError):
            self.web.write_site({**SITE, "aliases": ["alias.example.com"]})
        self.assertFalse(plugin.parent.parent.exists())
        self.runner.assert_not_called()

    def test_alias_plugin_rejects_unmanaged_root_or_symlink_path(self):
        with self.assertRaises(ValueError):
            self.web._alias_plugin_path({**SITE, "root": "/var/www/wpi/another/public"})
        with patch.object(Path, "is_symlink", return_value=True), self.assertRaises(RuntimeError):
            self.web._alias_plugin_path(SITE)

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

    def test_enabled_symlink_to_other_file_is_rejected(self):
        self.web.write_site(SITE)
        target = self.web.available / f"wpi-{SITE['id']}.conf"
        link = self.web.enabled / target.name
        before = target.read_bytes()
        link.unlink()
        outside = self.base / "unmanaged.conf"
        outside.write_text("# outside managed configuration\n")
        link.symlink_to(outside)
        self.runner.reset_mock()
        with self.assertRaises(RuntimeError):
            self.web.write_site({**SITE, "primary": "new.example.com"})
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(os.path.samefile(link, outside))
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


class AliasPluginPhpTests(unittest.TestCase):
    """Execute generated PHP when a runtime is present; real WP is in CI integration."""

    def test_alias_filters_use_allowlisted_host_and_skip_cli(self):
        php = shutil.which("php")
        local_php = Path("C:/laragon/bin/php/php-8.3.33-Win32-vs16-x64/php.exe")
        if php is None and local_php.is_file():
            php = str(local_php)
        if php is None:
            self.skipTest("PHP runtime absent; WordPress filter behavior is exercised in Ubuntu integration")
        web = WebStack(Mock(), "nginx", "8.3")
        site = {**SITE, "aliases": ["alias.example.com", "plain.example.com"], "tls": ["alias.example.com"]}
        with tempfile.TemporaryDirectory() as directory:
            plugin = Path(directory) / ALIAS_PLUGIN_NAME
            plugin.write_text(web.render_alias_plugin(site), encoding="utf-8")
            harness = Path(directory) / "harness.php"
            harness.write_text("<?php\n"
                               "define('ABSPATH', '/');\n"
                               "if ($argv[3] === 'cli') { define('WP_CLI', true); }\n"
                               "$_SERVER['HTTP_HOST'] = $argv[2];\n"
                               "$filters = array();\n"
                               "function add_filter($name, $callback, $priority) { global $filters; $filters[$name] = $callback; }\n"
                               "include $argv[1];\n"
                               "$home = isset($filters['option_home']) ? $filters['option_home']('https://example.com/blog') : 'https://example.com/blog';\n"
                               "$siteurl = isset($filters['option_siteurl']) ? $filters['option_siteurl']('https://example.com/wp') : 'https://example.com/wp';\n"
                               "echo json_encode(array($home, $siteurl));\n", encoding="utf-8")
            cases = (("alias.example.com", "web", "https://alias.example.com"),
                     ("ALIAS.EXAMPLE.COM:443", "web", "https://alias.example.com"),
                     ("plain.example.com", "web", "http://plain.example.com"),
                     ("alias.example.com", "cli", "https://example.com"),
                     ("secondary.example.com", "web", "https://example.com"),
                     ("example.com", "web", "https://example.com"),
                     ("alias.example.com.evil.test", "web", "https://example.com"),
                     ("alias.example.com:666", "web", "https://example.com"),
                     ("alias.example.com\n", "web", "https://example.com"),
                     ("", "web", "https://example.com"))
            for host, mode, expected in cases:
                with self.subTest(host=host, mode=mode):
                    result = subprocess.run([php, "-n", str(harness), str(plugin), host, mode],
                                            check=True, capture_output=True, text=True)
                    self.assertEqual(json.loads(result.stdout), [expected + "/blog", expected + "/wp"])


if __name__ == "__main__":
    unittest.main()
