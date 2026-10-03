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

    def test_secondary_certificate_failure_restores_original_configuration_and_state(self):
        self.web.obtain_certificate.side_effect = RuntimeError("certificate failed")
        with self.assertRaises(RuntimeError):
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
