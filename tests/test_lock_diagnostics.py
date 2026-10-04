"""Kernel lock diagnostics are read-only and never trust lock-file metadata."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from wpi import locking


class LockParserTests(unittest.TestCase):
    def parse(self, text):
        return locking.parse_lock_holders(text, 8, 1, 456)

    def test_exact_granted_flock_records_include_read_and_write_holders(self):
        records = """17: FLOCK  ADVISORY  WRITE 321 08:01:456 0 EOF
18: FLOCK ADVISORY READ 654 8:1:456 0 EOF
19: FLOCK ADVISORY WRITE 321 08:01:456 0 EOF
"""
        self.assertEqual(self.parse(records), [
            {'pid': 321, 'mode': 'WRITE'},
            {'pid': 654, 'mode': 'READ'},
        ])

    def test_waiting_processes_and_other_lock_types_are_not_owners(self):
        records = """17: -> FLOCK ADVISORY WRITE 321 08:01:456 0 EOF
18: POSIX ADVISORY WRITE 654 08:01:456 0 EOF
19: OFDLCK ADVISORY WRITE -1 08:01:456 0 EOF
20: FLOCK MANDATORY WRITE 777 08:01:456 0 EOF
21: FLOCK ADVISORY WRITE 888 08:01:456 0 EOF
"""
        self.assertEqual(self.parse(records), [{'pid': 888, 'mode': 'WRITE'}])

    def test_inode_and_both_hex_device_numbers_must_match(self):
        records = """17: FLOCK ADVISORY WRITE 321 08:01:457 0 EOF
18: FLOCK ADVISORY WRITE 654 09:01:456 0 EOF
19: FLOCK ADVISORY WRITE 777 08:02:456 0 EOF
20: FLOCK ADVISORY WRITE 888 08:01:456 0 EOF
"""
        self.assertEqual(self.parse(records), [{'pid': 888, 'mode': 'WRITE'}])
        self.assertEqual(locking.parse_lock_holders(
            '20: FLOCK ADVISORY WRITE 888 fe:0a:456 0 EOF', 254, 10, 456),
            [{'pid': 888, 'mode': 'WRITE'}])

    def test_unknown_owner_pid_is_reported_without_inventing_a_process(self):
        self.assertEqual(self.parse(
            '17: FLOCK ADVISORY WRITE -1 08:01:456 0 EOF'),
            [{'pid': None, 'mode': 'WRITE'}])

    def test_empty_or_malformed_records_are_ignored(self):
        self.assertEqual(self.parse(''), [])
        self.assertEqual(self.parse('garbage\n17: FLOCK ADVISORY\n'
                                    '18: FLOCK ADVISORY EXECUTE 321 08:01:456 0 EOF\n'
                                    '19: FLOCK ADVISORY WRITE invalid 08:01:456 0 EOF\n'
                                    '20: FLOCK ADVISORY WRITE 321 08:01:bad 0 EOF'), [])


class LockStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.proc = self.root / 'proc'
        self.data.mkdir()
        self.proc.mkdir()
        self.lock_file = self.data / 'operation.lock'
        self.lock_file.write_text('', encoding='utf-8')
        self.stat = SimpleNamespace(st_dev=123, st_ino=456)

    def status(self, lock_stat=None):
        with mock.patch.object(locking, 'device_numbers', return_value=(8, 1)):
            result = locking.operation_lock_status(
                self.data, proc_root=self.proc, lock_stat=lock_stat or self.stat)
        self.assertEqual(result['lock_file'], str(self.lock_file))
        self.assertIsInstance(result['reason'], str)
        self.assertTrue(result['reason'])
        return result

    def write_locks(self, contents):
        (self.proc / 'locks').write_text(contents, encoding='utf-8')

    def write_comm(self, pid, contents):
        directory = self.proc / str(pid)
        directory.mkdir(exist_ok=True)
        (directory / 'comm').write_text(contents, encoding='utf-8')

    def test_lock_file_json_is_not_evidence_of_a_kernel_lock(self):
        self.lock_file.write_text('{"pid":9999,"operation":"add-domain"}', encoding='utf-8')
        self.write_locks('17: FLOCK ADVISORY WRITE 9999 08:01:999 0 EOF\n')
        result = self.status()
        self.assertIsNone(result['locked'])
        self.assertEqual(result['holders'], [])
        self.assertEqual(result['reason'], 'no-visible-owner')
        self.assertEqual(self.lock_file.read_text(encoding='utf-8'),
                         '{"pid":9999,"operation":"add-domain"}')

    def test_only_verified_kernel_holders_are_enriched_with_process_names(self):
        self.lock_file.write_text('{"pid":9999}', encoding='utf-8')
        self.write_locks('17: FLOCK ADVISORY WRITE 321 08:01:456 0 EOF\n'
                         '18: FLOCK ADVISORY READ 654 08:01:456 0 EOF\n')
        self.write_comm(321, 'python3\n')
        self.write_comm(654, 'inspection\n')
        result = self.status()
        self.assertIs(result['locked'], True)
        self.assertEqual(result['holders'], [
            {'pid': 321, 'mode': 'WRITE', 'process': 'python3'},
            {'pid': 654, 'mode': 'READ', 'process': 'inspection'},
        ])

    def test_waiting_process_is_not_evidence_of_a_verified_holder(self):
        self.write_locks('17: -> FLOCK ADVISORY WRITE 321 08:01:456 0 EOF\n')
        result = self.status()
        self.assertIsNone(result['locked'])
        self.assertEqual(result['holders'], [])

    def test_unavailable_proc_locks_means_unknown_not_unlocked(self):
        result = self.status()
        self.assertIsNone(result['locked'])
        self.assertEqual(result['holders'], [])

    def test_unreadable_proc_locks_means_unknown_not_unlocked(self):
        self.write_locks('')
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('unavailable')):
            result = self.status()
        self.assertIsNone(result['locked'])
        self.assertEqual(result['holders'], [])

    def test_missing_process_name_does_not_discard_verified_holder(self):
        self.write_locks('17: FLOCK ADVISORY WRITE 321 08:01:456 0 EOF\n')
        result = self.status()
        self.assertIs(result['locked'], True)
        self.assertEqual(result['holders'], [{'pid': 321, 'mode': 'WRITE', 'process': None}])

    def test_unknown_pid_does_not_read_a_guessed_process(self):
        self.write_locks('17: FLOCK ADVISORY WRITE -1 08:01:456 0 EOF\n')
        result = self.status()
        self.assertIs(result['locked'], True)
        self.assertEqual(result['holders'], [{'pid': None, 'mode': 'WRITE', 'process': None}])

    def test_process_name_controls_are_removed_and_long_names_are_bounded(self):
        self.write_locks('17: FLOCK ADVISORY WRITE 321 08:01:456 0 EOF\n')
        self.write_comm(321, '\x1b[31m\x00python\r\n\t\x7f' + 'x' * 300)
        name = self.status()['holders'][0]['process']
        self.assertIsInstance(name, str)
        self.assertTrue(name)
        self.assertLessEqual(len(name), 128)
        self.assertNotRegex(name, r'[\x00-\x1f\x7f]')

    def test_injected_open_file_stat_is_used_instead_of_replaced_path_inode(self):
        self.write_locks('17: FLOCK ADVISORY WRITE 321 08:01:456 0 EOF\n')
        result = self.status(lock_stat=SimpleNamespace(st_dev=123, st_ino=457))
        self.assertIsNone(result['locked'])
        self.assertEqual(result['holders'], [])

    def test_without_injected_stat_each_call_reads_the_current_lock_file_inode(self):
        # Simulate replacement in this temporary fixture only. The diagnostic
        # must not cache an inode or treat the lock path as persistent ownership.
        original_inode = self.lock_file.stat().st_ino
        replacement = self.data / 'replacement.lock'
        replacement.write_text('', encoding='utf-8')
        self.assertNotEqual(replacement.stat().st_ino, original_inode)
        self.write_locks(f'17: FLOCK ADVISORY WRITE 321 08:01:{original_inode} 0 EOF\n')
        with mock.patch.object(locking, 'device_numbers', return_value=(8, 1)):
            first = locking.operation_lock_status(self.data, proc_root=self.proc)
            replacement.replace(self.lock_file)
            second = locking.operation_lock_status(self.data, proc_root=self.proc)
        self.assertIs(first['locked'], True)
        self.assertIsNone(second['locked'])
        self.assertEqual(second['holders'], [])

    def test_absent_lock_file_is_reported_without_creating_it(self):
        self.lock_file.unlink()
        result = locking.operation_lock_status(self.data, proc_root=self.proc)
        self.assertIs(result['locked'], False)
        self.assertEqual(result['holders'], [])
        self.assertEqual(result['reason'], 'not-created')
        self.assertFalse(self.lock_file.exists())


if __name__ == '__main__':
    unittest.main()
