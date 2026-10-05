"""Regression tests for the server mutation boundary; never run real commands."""

import copy
import gzip
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock

from wpi import core


class DomainValidationTests(unittest.TestCase):
    def test_canonical_domain(self):
        self.assertEqual(core.domain("Example.COM"), "example.com")
        self.assertEqual(core.domain("blog.example.com"), "blog.example.com")
        self.assertEqual(core.domain("xn--bcher-kva.example"), "xn--bcher-kva.example")

    def test_rejects_unsafe_or_non_host_values(self):
        values = [
            "", "localhost", "127.0.0.1", "::1", "[::1]", "*.example.com",
            "https://example.com", "example.com/path", "example.com:443",
            "user@example.com", "example.com;id", "example.com\nother.com",
            "example.com\r\nX-Header: injected", "example.com'", "../example.com",
            "-bad.example", "bad-.example", "a..example", "bad_name.example",
            "a" * 64 + ".example", ".example.com", "example.com\\etc",
            ".".join(["a" * 63] * 5),
        ]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    core.domain(value)

    def test_idna_length_is_checked_after_encoding(self):
        value = ".".join(["é" * 35] * 6 + ["example"])
        self.assertLess(len(value), 253)
        self.assertGreater(len(value.encode("idna")), 253)
        with self.assertRaises(ValueError):
            core.domain(value)

    def test_email_cannot_be_certbot_option(self):
        with self.assertRaises(ValueError):
            core.email_address("--server@example.com")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.commands = []

        def runner(argv, **kwargs):
            self.commands.append((list(argv), dict(kwargs)))
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        self.runner = runner
        self.manager = core.Manager(self.base / "state", self.base / "backups", runner=runner)
        core.atomic_json(self.manager.data / "config.json", {
            "stack": "nginx", "php_version": "8.3", "database": "mariadb", "ubuntu": "24.04",
        })
        self.site = {
            "id": "abcdef123456", "primary": "old.example.com", "secondary": [],
            "root": str(self.base / "www" / "abcdef123456" / "public"),
            "tls": ["old.example.com"], "email": "owner@example.com",
            "db_name": "wpi_abcdef123456", "db_user": "wpi_abcdef123456",
            "admin": "owner", "created_at": "2026-01-01T00:00:00+00:00", "status": "active",
        }
        self.manager.save_site(self.site)
        self.web = mock.Mock()
        self.web.certificate_ready.return_value = False
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(core.Manager, "web", new_callable=mock.PropertyMock,
                          return_value=self.web).start()
        mock.patch.object(core, "check_dns").start()
        mock.patch.object(core, "WWW", self.base / "www").start()

    def test_primary_secondary_and_phpmyadmin_domains_are_unique(self):
        extra = {**self.site, "id": "123456abcdef", "primary": "other.example.com",
                 "secondary": ["alias.example.com"]}
        self.manager.save_site(extra)
        cfg = self.manager.config
        cfg["phpmyadmin"] = {"domain": "db.example.com"}
        core.atomic_json(self.manager.data / "config.json", cfg)
        for host in [self.site["primary"], extra["primary"], "alias.example.com", "db.example.com"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.manager.ensure_free_domain(host)
        self.assertEqual(self.manager.ensure_free_domain(self.site["primary"],
                                                         allow_site=self.site["id"]), self.site["primary"])

    def test_secondary_is_saved_only_after_certificate_and_web_activation(self):
        result = self.manager.add_secondary(self.site["id"], "alias.example.com")
        self.assertEqual(result["primary"], self.site["primary"])
        self.assertEqual(result["secondary"], ["alias.example.com"])
        self.assertIn("alias.example.com", result["tls"])
        self.assertEqual(self.manager.site(self.site["id"]), result)
        self.web.obtain_certificate.assert_called_once_with(
            "alias.example.com", self.site["email"], self.site["root"])

    def test_legacy_secondary_records_remain_redirects_and_lookup_includes_alias(self):
        self.site['secondary'] = ['redirect.example.com']
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        self.assertEqual(core.site_hosts(self.site),
                         ['old.example.com', 'alias.example.com', 'redirect.example.com'])
        for hostname in core.site_hosts(self.site):
            self.assertEqual(self.manager.site(hostname), self.site)
        self.assertEqual(self.manager.site('alias.example.com')['secondary'],
                         ['redirect.example.com'])

    def test_alias_domains_are_reserved_against_other_sites_and_phpmyadmin(self):
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        with self.assertRaisesRegex(ValueError, 'sudah dipakai situs lain'):
            self.manager.ensure_free_domain('alias.example.com')
        self.assertEqual(self.manager.ensure_free_domain('alias.example.com', self.site['id']),
                         'alias.example.com')
        with self.assertRaises(ValueError):
            self.manager.install_pma('alias.example.com', self.site['email'])

    def test_add_domain_defaults_to_alias_and_preserves_legacy_redirects(self):
        self.site['secondary'] = ['redirect.example.com']
        self.manager.save_site(self.site)
        result = self.manager.add_domain(self.site['id'], 'alias.example.com')
        self.assertEqual(result['primary'], self.site['primary'])
        self.assertEqual(result['aliases'], ['alias.example.com'])
        self.assertEqual(result['secondary'], ['redirect.example.com'])
        self.assertIn('alias.example.com', result['tls'])
        self.assertEqual(self.manager.site('alias.example.com'), result)
        self.assertFalse(any('search-replace' in argv for argv, _ in self.commands))

    def test_www_aliases_commit_together_after_both_certificates(self):
        states_at_issue = []

        def certificate(host, email, root):
            states_at_issue.append(self.manager.site(self.site['id']))

        self.web.obtain_certificate.side_effect = certificate
        result = self.manager.add_domain(self.site['id'], 'alias.example.com', www=True)
        self.assertEqual(result['aliases'], ['alias.example.com', 'www.alias.example.com'])
        self.assertEqual(states_at_issue, [self.site, self.site])
        self.assertEqual(self.web.obtain_certificate.call_args_list, [
            mock.call('alias.example.com', self.site['email'], self.site['root']),
            mock.call('www.alias.example.com', self.site['email'], self.site['root']),
        ])
        self.assertTrue(set(result['aliases']).issubset(result['tls']))

    def test_www_redirects_use_the_redirect_role(self):
        result = self.manager.add_domain(self.site['id'], 'redirect.example.com',
                                         kind='redirect', www=True)
        self.assertEqual(result['secondary'],
                         ['redirect.example.com', 'www.redirect.example.com'])
        self.assertEqual(result.get('aliases', []), [])

    def test_www_validation_failure_does_not_partially_add_bare_domain(self):
        extra = {**self.site, 'id': '111111111111', 'primary': 'www.alias.example.com'}
        self.manager.save_site(extra)
        with self.assertRaises(ValueError):
            self.manager.add_domain(self.site['id'], 'alias.example.com', www=True)
        self.assertEqual(self.manager.site(self.site['id']), self.site)
        self.web.write_site.assert_not_called()
        self.web.obtain_certificate.assert_not_called()

    def test_www_second_certificate_failure_rolls_back_every_domain(self):
        self.web.obtain_certificate.side_effect = [None, RuntimeError('www certificate failed')]
        with mock.patch.object(self.manager, 'delete_certificate') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'www certificate failed'):
                self.manager.add_domain(self.site['id'], 'alias.example.com', www=True)
        self.assertEqual(self.manager.site(self.site['id']), self.site)
        self.assertEqual(self.web.write_site.call_args.args[0], self.site)
        self.assertEqual(cleanup.call_args_list,
                         [mock.call('alias.example.com'), mock.call('www.alias.example.com')])

    def test_failed_add_preserves_a_preexisting_certificate(self):
        self.web.certificate_ready.return_value = True
        self.web.obtain_certificate.side_effect = RuntimeError('certificate failed')
        with mock.patch.object(self.manager, 'delete_certificate') as cleanup:
            with self.assertRaises(RuntimeError):
                self.manager.add_domain(self.site['id'], 'alias.example.com')
        cleanup.assert_not_called()
        self.assertEqual(self.manager.site(self.site['id']), self.site)

    def test_add_domain_rejects_duplicate_and_unknown_role_before_mutating(self):
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        for hostname, kind in [('alias.example.com', 'alias'), ('other.example.com', 'primary')]:
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                self.manager.add_domain(self.site['id'], hostname, kind=kind)
        self.web.write_site.assert_not_called()
        self.web.obtain_certificate.assert_not_called()

    def test_secondary_certificate_failure_restores_original_configuration_and_state(self):
        self.web.obtain_certificate.side_effect = RuntimeError("certificate failed")
        with self.assertRaises(RuntimeError):
            self.manager.add_secondary(self.site["id"], "alias.example.com")
        self.assertEqual(self.manager.site(self.site["id"]), self.site)
        self.assertEqual(self.web.write_site.call_args.args[0], self.site)

    def test_secondary_cancel_restores_configuration_and_state(self):
        self.web.obtain_certificate.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.manager.add_secondary(self.site["id"], "alias.example.com")
        self.assertEqual(self.manager.site(self.site["id"]), self.site)
        self.assertEqual(self.web.write_site.call_args.args[0], self.site)

    def test_primary_replacement_uses_serialized_aware_search_and_detaches_old_domain(self):
        snapshot = self.base / "backup"
        with mock.patch.object(self.manager, "backup", return_value=snapshot):
            changed, returned_snapshot = self.manager.change_primary(self.site["id"], "new.example.com")
        self.assertEqual(returned_snapshot, snapshot)
        self.assertEqual(changed["primary"], "new.example.com")
        self.assertNotIn(self.site["primary"], [*changed["secondary"], *changed["tls"]])
        searches = [argv for argv, _ in self.commands if "search-replace" in argv]
        self.assertEqual(len(searches), 6)
        for command in searches:
            self.assertIn("--precise", command)
            self.assertIn("--regex", command)
            self.assertIn("--recurse-objects", command)
            self.assertIn("--all-tables-with-prefix", command)
            self.assertIn("--skip-columns=guid", command)
        self.assertEqual(self.manager.site(self.site["id"]), changed)

    def test_promoting_secondary_removes_self_redirect_and_retains_other_alias(self):
        self.site["secondary"] = ["new.example.com", "keep.example.com"]
        self.site["tls"] += self.site["secondary"]
        self.manager.save_site(self.site)
        with mock.patch.object(self.manager, "backup", return_value=self.base / "backup"):
            changed, _ = self.manager.change_primary(self.site["id"], "new.example.com")
        self.assertEqual(changed["secondary"], ["keep.example.com"])
        self.assertEqual(changed["tls"], ["new.example.com", "keep.example.com"])

    def test_set_primary_promotes_alias_and_keeps_old_primary_as_alias(self):
        self.site['aliases'] = ['new.example.com', 'keep.example.com']
        self.site['secondary'] = ['redirect.example.com']
        self.site['tls'] += self.site['aliases'] + self.site['secondary']
        self.manager.save_site(self.site)
        with mock.patch.object(self.manager, 'backup', return_value=self.base / 'backup'), \
             mock.patch.object(self.manager, 'delete_certificate') as cleanup:
            changed, _ = self.manager.set_primary(self.site['id'], 'new.example.com')
        self.assertEqual(changed['primary'], 'new.example.com')
        self.assertEqual(changed['aliases'], ['keep.example.com', 'old.example.com'])
        self.assertEqual(changed['secondary'], ['redirect.example.com'])
        self.assertIn('old.example.com', changed['tls'])
        self.assertNotIn('new.example.com', changed['aliases'] + changed['secondary'])
        self.assertEqual(self.manager.site('old.example.com'), changed)
        cleanup.assert_not_called()
        option_updates = [argv for argv, _ in self.commands if 'option' in argv]
        self.assertEqual(len(option_updates), 2)
        self.assertTrue(all(argv[-1] == 'https://new.example.com' for argv in option_updates))
        self.assertEqual(len([argv for argv, _ in self.commands if 'search-replace' in argv]), 6)

    def test_set_primary_old_domain_can_become_redirect_or_be_removed(self):
        for old_role in ['redirect', 'remove']:
            with self.subTest(old_role=old_role):
                self.site['aliases'] = ['new.example.com']
                self.manager.save_site(self.site)
                with mock.patch.object(self.manager, 'backup', return_value=self.base / 'backup'), \
                     mock.patch.object(self.manager, 'delete_certificate') as cleanup:
                    changed, _ = self.manager.set_primary(self.site['id'], 'new.example.com',
                                                          old_domain=old_role)
                if old_role == 'redirect':
                    self.assertEqual(changed['secondary'], ['old.example.com'])
                    self.assertIn('old.example.com', changed['tls'])
                    cleanup.assert_not_called()
                else:
                    self.assertNotIn('old.example.com', core.site_hosts(changed))
                    self.assertNotIn('old.example.com', changed['tls'])
                    cleanup.assert_called_once_with('old.example.com')

    def test_set_primary_requires_registered_alias_and_rejects_current_primary(self):
        self.site['secondary'] = ['redirect.example.com']
        self.manager.save_site(self.site)
        with mock.patch.object(self.manager, 'backup') as backup:
            for host in ['unknown.example.com', 'redirect.example.com', 'old.example.com']:
                with self.subTest(host=host), self.assertRaises(ValueError):
                    self.manager.set_primary(self.site['id'], host)
        backup.assert_not_called()
        self.web.write_site.assert_not_called()

    def test_failed_alias_promotion_restores_original_roles_and_database(self):
        self.site['aliases'] = ['new.example.com', 'keep.example.com']
        self.site['secondary'] = ['redirect.example.com']
        self.manager.save_site(self.site)
        snapshot = self.base / 'backup'
        self.web.write_site.side_effect = [None, RuntimeError('reload failed'), None]
        with mock.patch.object(self.manager, 'backup', return_value=snapshot), \
             mock.patch.object(self.manager, 'restore_database') as restore:
            with self.assertRaisesRegex(RuntimeError, 'database dan konfigurasi dikembalikan'):
                self.manager.set_primary(self.site['id'], 'new.example.com')
        self.assertEqual(self.manager.site(self.site['id']), self.site)
        restore.assert_called_once_with(self.site, snapshot)
        self.assertEqual(self.web.write_site.call_args.args[0], self.site)

    def test_search_replace_failure_imports_backup_and_restores_domain(self):
        snapshot = self.base / "backup"
        original = copy.deepcopy(self.site)
        original_runner = self.manager.runner

        def failing_runner(argv, **kwargs):
            if "search-replace" in argv:
                raise RuntimeError("partial database mutation")
            return original_runner(argv, **kwargs)

        self.manager.runner = failing_runner
        with mock.patch.object(self.manager, "backup", return_value=snapshot), \
             mock.patch.object(self.manager, "restore_database") as restore:
            with self.assertRaisesRegex(RuntimeError, "database dan konfigurasi dikembalikan"):
                self.manager.change_primary(self.site["id"], "new.example.com")
        restore.assert_called_once_with(original, snapshot)
        self.assertEqual(self.manager.site(self.site["id"]), original)
        self.assertEqual(self.web.write_site.call_args.args[0], original)
        self.assertTrue(any("maintenance-mode" in args and "deactivate" in args
                            for args, _ in self.commands))

    def test_final_web_activation_failure_restores_database_even_after_replacement(self):
        snapshot = self.base / "backup"
        self.web.write_site.side_effect = [None, RuntimeError("reload failed"), None]
        with mock.patch.object(self.manager, "backup", return_value=snapshot), \
             mock.patch.object(self.manager, "restore_database") as restore:
            with self.assertRaisesRegex(RuntimeError, "database dan konfigurasi dikembalikan"):
                self.manager.change_primary(self.site["id"], "new.example.com")
        restore.assert_called_once_with(self.site, snapshot)
        self.assertEqual(self.manager.site(self.site["id"]), self.site)
        self.assertEqual(self.web.write_site.call_args.args[0], self.site)
        self.assertTrue(any("search-replace" in args for args, _ in self.commands))

    def test_primary_change_cancel_after_mutation_rolls_back(self):
        snapshot = self.base / "backup"
        original_runner = self.manager.runner

        def interrupted(argv, **kwargs):
            if "search-replace" in argv:
                raise KeyboardInterrupt()
            return original_runner(argv, **kwargs)

        self.manager.runner = interrupted
        with mock.patch.object(self.manager, "backup", return_value=snapshot), \
             mock.patch.object(self.manager, "restore_database") as restore:
            with self.assertRaisesRegex(RuntimeError, "database dan konfigurasi dikembalikan"):
                self.manager.change_primary(self.site["id"], "new.example.com")
        restore.assert_called_once_with(self.site, snapshot)
        self.assertEqual(self.manager.site(self.site["id"]), self.site)

    def test_failed_recovery_reports_backup_and_does_not_claim_success(self):
        snapshot = self.base / "backup"
        self.web.write_site.side_effect = RuntimeError("server unavailable")
        with mock.patch.object(self.manager, "backup", return_value=snapshot), \
             mock.patch.object(self.manager, "restore_database", side_effect=RuntimeError("SQL unavailable")):
            with self.assertRaisesRegex(RuntimeError, "pemulihan belum selesai.*Backup:"):
                self.manager.change_primary(self.site["id"], "new.example.com")

    def test_cannot_delete_primary_as_secondary(self):
        with mock.patch.object(self.manager, "backup") as backup:
            with self.assertRaises(ValueError):
                self.manager.remove_secondary(self.site["id"], self.site["primary"])
        backup.assert_not_called()
        self.web.write_site.assert_not_called()

    def test_delete_alias_removes_only_domain_and_certificate_reference(self):
        self.site['aliases'] = ['alias.example.com', 'keep.example.com']
        self.site['tls'] += self.site['aliases']
        self.manager.save_site(self.site)
        with mock.patch.object(self.manager, 'backup') as backup, \
             mock.patch.object(self.manager, 'delete_certificate') as cleanup:
            changed = self.manager.remove_domain(self.site['id'], 'alias.example.com')
        self.assertEqual(changed['primary'], self.site['primary'])
        self.assertEqual(changed['aliases'], ['keep.example.com'])
        self.assertNotIn('alias.example.com', changed['tls'])
        for key in ['root', 'db_name', 'db_user']:
            self.assertEqual(changed[key], self.site[key])
        backup.assert_called_once_with(self.site['id'])
        cleanup.assert_called_once_with('alias.example.com')
        self.assertEqual(self.commands, [])

    def test_delete_primary_or_unknown_domain_refuses_before_backup(self):
        with mock.patch.object(self.manager, 'backup') as backup:
            for host in ['old.example.com', 'unknown.example.com']:
                with self.subTest(host=host), self.assertRaises(ValueError):
                    self.manager.remove_domain(self.site['id'], host)
        backup.assert_not_called()
        self.web.write_site.assert_not_called()

    def test_delete_alias_web_failure_preserves_domain_state_and_certificate(self):
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        self.web.write_site.side_effect = [RuntimeError('reload failed'), None]
        with mock.patch.object(self.manager, 'backup'), \
             mock.patch.object(self.manager, 'delete_certificate') as cleanup:
            with self.assertRaisesRegex(RuntimeError, 'reload failed'):
                self.manager.remove_domain(self.site['id'], 'alias.example.com')
        self.assertEqual(self.manager.site(self.site['id']), self.site)
        cleanup.assert_not_called()

    def test_install_passwords_use_stdin_not_process_arguments(self):
        password = "ExampleSecretPassword123!"
        with mock.patch.object(core.secrets, "token_hex", side_effect=["111111111111", "abc123" * 8]):
            result, returned_password = self.manager.install("fresh.example.com", "owner@example.com",
                                                           password=password)
        self.assertEqual(result["status"], "active")
        self.assertEqual(returned_password, password)
        for argv, _ in self.commands:
            self.assertFalse(any(password in value for value in argv))
            self.assertFalse(any("abc123" * 8 in value for value in argv))
            self.assertNotIn("--admin_password=" + password, argv)
        prompts = {next(value for value in argv if value.startswith("--prompt=")): options.get("input")
                   for argv, options in self.commands if any(value.startswith("--prompt=") for value in argv)}
        self.assertEqual(prompts["--prompt=admin_password"], password + "\n")
        self.assertEqual(prompts["--prompt=dbpass"], "abc123" * 8 + "\n")
        self.assertTrue(any(argv[0] == "mysql" and "CREATE DATABASE" in options.get("input", "")
                            for argv, options in self.commands))

    def test_incomplete_install_retains_managed_database_for_recovery(self):
        self.web.obtain_certificate.side_effect = RuntimeError("ACME unavailable")
        with self.assertRaises(RuntimeError):
            self.manager.install("fresh.example.com", "owner@example.com", password="ASecretPassword123!")
        failed = self.manager.site("fresh.example.com")
        self.assertEqual(failed["status"], "incomplete")
        self.assertFalse(any("DROP DATABASE" in options.get("input", "") for _, options in self.commands))

    def test_resume_repairs_partial_core_without_recreating_installed_wordpress(self):
        self.site["status"] = "incomplete"
        self.manager.save_site(self.site)
        root = Path(self.site["root"])
        root.mkdir(parents=True)
        (root / "wp-load.php").write_text("partial-core")
        (root / "wp-config.php").write_text("existing-config")
        core.atomic_json(self.manager.data / "credentials" / (self.site["id"] + ".json"), {
            "wordpress_password": "OriginalPassword123!", "database_password": "abc123" * 8,
        })
        runner = self.manager.runner
        checks = 0

        def partial_core(argv, **kwargs):
            nonlocal checks
            result = runner(argv, **kwargs)
            if "verify-checksums" in argv:
                checks += 1
                if checks == 1:
                    return subprocess.CompletedProcess(argv, 1, stdout="", stderr="missing files")
            return result

        self.manager.runner = partial_core
        result, password = self.manager.resume_install(self.site["id"])
        self.assertEqual(result["status"], "active")
        self.assertEqual(password, "OriginalPassword123!")
        self.assertTrue(any("download" in argv and "--force" in argv for argv, _ in self.commands))
        self.assertFalse(any("install" in argv and "core" in argv for argv, _ in self.commands))
        self.assertEqual((root / "wp-config.php").read_text(), "existing-config")

    def test_phpmyadmin_delete_never_invokes_sql_or_removes_a_package(self):
        cfg = self.manager.config
        cfg["phpmyadmin"] = {"domain": "db.example.com", "email": "owner@example.com", "user": "panel"}
        core.atomic_json(self.manager.data / "config.json", cfg)
        with mock.patch.object(core.Path, "unlink") as unlink:
            self.manager.remove_pma()
        self.web.remove_phpmyadmin.assert_called_once_with("db.example.com")
        self.assertEqual(unlink.call_count, 2)
        self.assertNotIn("phpmyadmin", self.manager.config)
        self.assertEqual(self.manager.site(self.site["id"]), self.site)
        self.assertFalse(any(argv[0] in {"mysql", "mariadb", "apt-get", "runuser"}
                             for argv, _ in self.commands))

    def test_cert_is_retained_while_referenced_by_another_managed_hostname(self):
        self.web.certificate_ready.return_value = True
        self.manager.delete_certificate(self.site["primary"])
        self.assertEqual(self.commands, [])

    def test_alias_certificate_remains_while_referenced(self):
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        self.web.certificate_ready.return_value = True
        self.manager.delete_certificate('alias.example.com')
        self.assertEqual(self.commands, [])
        self.web.remove_migrated_certificate.assert_not_called()

    def test_retired_migration_fallback_does_not_delete_unknown_certbot_lineage(self):
        self.web.certificate_ready.return_value = True
        self.web.letsencrypt_ready.return_value = False
        self.manager.delete_certificate('retired.example.com')
        self.assertEqual(self.commands, [])
        self.web.remove_migrated_certificate.assert_called_once_with('retired.example.com')

    def test_ssl_renewal_includes_alias_and_redirect_domains(self):
        self.site['aliases'] = ['alias.example.com']
        self.site['secondary'] = ['redirect.example.com']
        self.manager.save_site(self.site)
        renewed = self.manager.renew_ssl(self.site['id'])
        self.assertEqual([call.args[0] for call in self.web.obtain_certificate.call_args_list],
                         core.site_hosts(self.site))
        self.assertEqual(set(renewed['tls']), set(core.site_hosts(self.site)))

    def test_replacement_boundaries_do_not_change_similar_domains(self):
        pairs = self.manager.replacement_pairs("old.example.com", "new.example.com")
        pattern, replacement = pairs[0]
        value = "https://old.example.com/path https://old.example.com.evil/path https://old.example.com-other/path"
        self.assertEqual(re.sub(pattern, replacement, value),
                         "https://new.example.com/path https://old.example.com.evil/path https://old.example.com-other/path")

    def test_run_error_does_not_expose_argv_sql_or_child_output(self):
        error = subprocess.CalledProcessError(2, ["mysql", "--secret=token"],
                                              output="customer-data", stderr="password=secret")
        with mock.patch.object(core.subprocess, "run", side_effect=error):
            with self.assertRaises(RuntimeError) as captured:
                core.run(["mysql", "--secret=token"], input="secret SQL")
        text = str(captured.exception)
        for secret in ["token", "customer-data", "password=secret", "secret SQL"]:
            self.assertNotIn(secret, text)

    def test_verified_backup_rejects_tampering_and_other_site(self):
        folder = self.manager.backups / self.site["id"] / "sample"
        folder.mkdir(parents=True)
        for name in ["database.sql.gz", "files.tar.gz", "site.json"]:
            (folder / name).write_bytes(b"original")
        core.atomic_json(folder / "manifest.json", {
            "site_id": self.site["id"], "sha256": {
                name: self.manager.file_hash(folder / name)
                for name in ["database.sql.gz", "files.tar.gz", "site.json"]
            },
        })
        (folder / "COMPLETE").touch()
        self.assertEqual(self.manager.verified_backup(self.site, folder), folder.resolve())
        with self.assertRaises(ValueError):
            self.manager.verified_backup({**self.site, "id": "111111111111"}, folder)
        (folder / "database.sql.gz").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "Checksum"):
            self.manager.verified_backup(self.site, folder)

    def test_restore_reissues_missing_primary_certificate_before_swapping_content(self):
        root = Path(self.site["root"])
        root.mkdir(parents=True)
        (root / "index.php").write_text("old-content")
        snapshot = self.manager.backup(self.site["id"])
        current = {**self.site, "primary": "new.example.com", "tls": ["new.example.com"]}
        self.manager.save_site(current)
        (root / "index.php").write_text("current-content")
        ready = {"new.example.com"}
        self.web.certificate_ready.side_effect = lambda host: host in ready

        def certificate(host, email, webroot):
            self.assertEqual((root / "index.php").read_text(), "current-content")
            ready.add(host)

        self.web.obtain_certificate.side_effect = certificate
        with mock.patch.object(self.manager, "restore_database") as database:
            restored, safety = self.manager.restore(current["id"], snapshot)
        self.assertEqual(restored["primary"], self.site["primary"])
        self.assertIn(self.site["primary"], restored["tls"])
        self.web.obtain_certificate.assert_called_once_with(self.site["primary"], self.site["email"], self.site["root"])
        self.assertEqual((root / "index.php").read_text(), "old-content")
        self.assertEqual(json.loads((safety / "site.json").read_text())["primary"], current["primary"])
        database.assert_called_once_with(restored, snapshot.resolve())

    def test_restore_imported_snapshot_keeps_destination_sql_password_and_acme_identity(self):
        root = Path(self.site['root'])
        root.mkdir(parents=True)
        (root / 'wp-config.php').write_text('source-db-config')
        (root / 'index.php').write_text('snapshot-content')
        snapshot = self.manager.backup(self.site['id'])
        current = {**self.site, 'migration_id': 'd' * 32}
        self.manager.save_site(current)
        credential = {'database_user': current['db_user'], 'database_password': 'a' * 48}
        core.atomic_json(self.manager.data / 'credentials' / (current['id'] + '.json'), credential)
        (root / 'index.php').write_text('current-content')
        self.web.certificate_ready.return_value = True
        with mock.patch.object(self.manager, 'restore_database') as database:
            restored, _ = self.manager.restore(current['id'], snapshot)
        self.assertEqual(restored['migration_id'], current['migration_id'])
        self.assertEqual((root / 'index.php').read_text(), 'snapshot-content')
        self.assertEqual(self.manager.site(current['id'])['migration_id'], current['migration_id'])
        password_call = next((argv, options) for argv, options in self.commands
                             if 'DB_PASSWORD' in argv)
        self.assertIn('--prompt=value', password_call[0])
        self.assertNotIn(credential['database_password'], password_call[0])
        self.assertEqual(password_call[1]['input'], credential['database_password'] + '\n')
        database.assert_called_once_with(restored, snapshot.resolve())

    def test_restore_migration_refuses_missing_credentials_before_file_swap(self):
        root = Path(self.site['root'])
        root.mkdir(parents=True)
        (root / 'index.php').write_text('snapshot-content')
        snapshot = self.manager.backup(self.site['id'])
        self.manager.save_site({**self.site, 'migration_id': 'd' * 32})
        (root / 'index.php').write_text('current-content')
        with self.assertRaises(FileNotFoundError):
            self.manager.restore(self.site['id'], snapshot)
        self.assertEqual((root / 'index.php').read_text(), 'current-content')

    def test_restore_certificate_failure_does_not_replace_files_or_database(self):
        root = Path(self.site["root"])
        root.mkdir(parents=True)
        (root / "index.php").write_text("old-content")
        snapshot = self.manager.backup(self.site["id"])
        current = {**self.site, "primary": "new.example.com", "tls": ["new.example.com"]}
        self.manager.save_site(current)
        (root / "index.php").write_text("current-content")
        self.web.certificate_ready.side_effect = lambda host: host == "new.example.com"
        self.web.obtain_certificate.side_effect = RuntimeError("ACME unavailable")
        with mock.patch.object(self.manager, "restore_database") as database:
            with self.assertRaises(RuntimeError):
                self.manager.restore(current["id"], snapshot)
        self.assertEqual((root / "index.php").read_text(), "current-content")
        self.assertEqual(self.manager.site(current["id"]), current)
        self.assertEqual(self.web.write_site.call_args.args[0], current)
        database.assert_not_called()

    def test_restore_reissues_alias_and_redirect_certificates_before_replacing_files(self):
        self.site['aliases'] = ['alias.example.com']
        self.site['secondary'] = ['redirect.example.com']
        self.site['tls'] += self.site['aliases'] + self.site['secondary']
        self.manager.save_site(self.site)
        root = Path(self.site['root'])
        root.mkdir(parents=True)
        (root / 'index.php').write_text('backup-content')
        snapshot = self.manager.backup(self.site['id'])
        current = {**self.site, 'primary': 'new.example.com', 'aliases': [],
                   'secondary': [], 'tls': ['new.example.com']}
        self.manager.save_site(current)
        (root / 'index.php').write_text('current-content')
        ready = {'new.example.com'}
        self.web.certificate_ready.side_effect = lambda host: host in ready

        def issue(host, email, webroot):
            self.assertEqual((root / 'index.php').read_text(), 'current-content')
            ready.add(host)

        self.web.obtain_certificate.side_effect = issue
        with mock.patch.object(self.manager, 'restore_database'):
            restored, _ = self.manager.restore(current['id'], snapshot)
        self.assertEqual([call.args[0] for call in self.web.obtain_certificate.call_args_list],
                         core.site_hosts(self.site))
        self.assertEqual(restored['aliases'], self.site['aliases'])
        self.assertEqual(restored['secondary'], self.site['secondary'])
        self.assertEqual(set(restored['tls']), set(core.site_hosts(self.site)))
        self.assertEqual((root / 'index.php').read_text(), 'backup-content')

    def test_restore_refuses_alias_now_owned_by_another_site_before_changing_content(self):
        self.site['aliases'] = ['alias.example.com']
        self.manager.save_site(self.site)
        root = Path(self.site['root'])
        root.mkdir(parents=True)
        (root / 'index.php').write_text('backup-content')
        snapshot = self.manager.backup(self.site['id'])
        current = {**self.site, 'aliases': []}
        self.manager.save_site(current)
        self.manager.save_site({**self.site, 'id': '111111111111', 'primary': 'other.example.com'})
        (root / 'index.php').write_text('current-content')
        self.web.reset_mock()
        with mock.patch.object(self.manager, 'backup') as safety, \
             mock.patch.object(self.manager, 'restore_database') as database:
            with self.assertRaisesRegex(ValueError, 'sudah dipakai situs lain'):
                self.manager.restore(current['id'], snapshot)
        self.assertEqual((root / 'index.php').read_text(), 'current-content')
        self.assertEqual(self.manager.site(current['id']), current)
        self.web.write_site.assert_not_called()
        self.web.obtain_certificate.assert_not_called()
        safety.assert_not_called()
        database.assert_not_called()

    def test_existing_setup_cannot_switch_stack_or_database(self):
        for stack, database in [("apache", "mariadb"), ("nginx", "mysql")]:
            with self.subTest(stack=stack, database=database), self.assertRaises(ValueError):
                self.manager.setup(stack, database)
        self.assertEqual(self.commands, [])

    def test_existing_setup_automatically_migrates_php_fpm(self):
        with mock.patch.object(self.manager, 'enable_autotune') as enable:
            self.manager.setup('nginx', 'mariadb')
        enable.assert_called_once_with()

    def test_upgrade_installs_controller_preserving_site_and_database(self):
        cfg = self.manager.config
        cfg['phpmyadmin'] = {'domain': 'db.example.com'}
        core.atomic_json(self.manager.data / 'config.json', cfg)
        with mock.patch.object(core, 'AutoTuner') as tuner, \
             mock.patch.object(core.shutil, 'which', return_value='/usr/bin/ss'):
            tuner.return_value.install.return_value = {'enabled': True}
            self.assertEqual(self.manager.enable_autotune(), {'enabled': True})
        tuner.assert_called_once_with('8.3', self.runner, data_dir=self.manager.data)
        self.assertTrue(self.manager.config['autotune_enabled'])
        self.assertEqual(self.manager.site(self.site['id']), self.site)
        self.web.write_site.assert_called_once_with(self.site)
        self.web.install_phpmyadmin.assert_called_once_with(
            'db.example.com', '/usr/share/phpmyadmin', '/etc/wpi/pma.htpasswd')
        self.assertEqual(self.commands, [])

    def test_incomplete_setup_does_not_enable_or_tick_controller(self):
        cfg = self.manager.config
        cfg['setup_complete'] = False
        core.atomic_json(self.manager.data / 'config.json', cfg)
        with mock.patch.object(core, 'AutoTuner') as tuner:
            self.assertFalse(self.manager.enable_autotune()['enabled'])
            self.assertFalse(self.manager.autotune_tick()['enabled'])
        tuner.assert_not_called()

    def test_restore_sql_is_not_world_readable(self):
        folder = self.base / "backup"
        folder.mkdir()
        with gzip.open(folder / "database.sql.gz", "wb") as stream:
            stream.write(b"SELECT 1;\n")
        modes = []

        def checked_wp(site, *args, **kwargs):
            if args[:2] == ("db", "import"):
                sql = Path(args[2])
                modes.extend([sql.stat().st_mode & 0o777, sql.parent.stat().st_mode & 0o777])
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(self.manager, "verified_backup", return_value=folder), \
             mock.patch.object(self.manager, "wp", side_effect=checked_wp):
            self.manager.restore_database(self.site, folder)
        if core.os.name == "posix":
            self.assertEqual(modes, [0o640, 0o750])
        else:
            self.assertEqual(len(modes), 2)  # Windows chmod has no POSIX group semantics.


if __name__ == "__main__":
    unittest.main()
