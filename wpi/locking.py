"""Read-only diagnostics for the kernel-held WPI operation lock."""
import os
from pathlib import Path
import re


def device_numbers(device):
    return os.major(device), os.minor(device)


def parse_lock_holders(text, major, minor, inode):
    """Match granted flock records, never queued requests or other files."""
    pattern = re.compile(
        r'^\d+:\s+FLOCK\s+ADVISORY\s+(READ|WRITE)\s+(-?\d+)\s+'
        r'([0-9a-fA-F]+):([0-9a-fA-F]+):(\d+)\s', re.M)
    holders = []
    seen = set()
    for match in pattern.finditer(text):
        mode, pid, record_major, record_minor, record_inode = match.groups()
        if (int(record_major, 16), int(record_minor, 16), int(record_inode)) != (major, minor, inode):
            continue
        pid = int(pid)
        pid = pid if pid > 0 else None
        if (pid, mode) not in seen:
            holders.append({'pid': pid, 'mode': mode})
            seen.add((pid, mode))
    return holders


def operation_lock_status(data_dir, proc_root=Path('/proc'), lock_stat=None):
    """Report a snapshot from /proc; lock-file contents are not ownership proof."""
    path = Path(data_dir) / 'operation.lock'
    report = {'locked': None, 'lock_file': str(path), 'holders': [], 'reason': 'lock-unavailable'}
    if lock_stat is None:
        try:
            lock_stat = path.stat()
        except FileNotFoundError:
            return {**report, 'locked': False, 'reason': 'not-created'}
        except OSError:
            return report
    proc = Path(proc_root)
    try:
        major, minor = device_numbers(lock_stat.st_dev)
        records = (proc / 'locks').read_text(encoding='utf-8', errors='replace')
    except (OSError, AttributeError):
        return {**report, 'reason': 'kernel-status-unavailable'}
    holders = parse_lock_holders(records, major, minor, lock_stat.st_ino)
    for holder in holders:
        name = None
        if holder['pid'] is not None:
            try:
                raw = (proc / str(holder['pid']) / 'comm').read_text(encoding='utf-8', errors='replace')
                name = ''.join(character for character in raw if character.isprintable()).strip()[:64] or None
            except OSError:
                pass
        holder['process'] = name
    # Missing visible records do not prove the lock is free: /proc is filtered
    # by the PID namespace and ownership can change during this snapshot.
    return {**report, 'locked': True if holders else None, 'holders': holders,
            'reason': 'kernel-owner' if holders else 'no-visible-owner'}


def lock_busy_message(report):
    message = 'Operasi WPI lain sedang berjalan.'
    actors = []
    for holder in report['holders'][:4]:
        if holder['pid'] is not None:
            name = f' ({holder["process"]})' if holder.get('process') else ''
            actors.append(f'PID {holder["pid"]}{name}')
    if actors:
        message += ' Pemegang kunci: ' + ', '.join(actors) + '.'
    else:
        message += ' PID pemegang kunci belum dapat dibaca.'
    message += ' Tunggu operasi selesai; jika panel pemegang kunci sedang di menu, keluar dengan 0.'
    message += '\nDiagnosis: sudo wpi lock-status'
    message += '\nCek kernel: sudo lslocks -o PID,COMMAND,PATH | grep /var/lib/wpi/operation.lock'
    return message
