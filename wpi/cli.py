"""Interactive terminal panel and scriptable command entry points."""
import argparse
import contextlib
import getpass
import json
import os
from pathlib import Path
import sys

from . import __version__
from .core import Manager, DATA
from .locking import operation_lock_status, lock_busy_message


def ask(label, default=None):
    suffix = f' [{default}]' if default is not None else ''
    answer = input(label + suffix + ': ').strip()
    if not answer and default is not None:
        return default
    if not answer:
        raise ValueError(label + ' wajib diisi.')
    return answer


def password_prompt():
    password = getpass.getpass('Password (Enter = otomatis): ')
    if not password:
        return None
    if password != getpass.getpass('Ulangi password: '):
        raise ValueError('Password tidak cocok.')
    return password


def show_sites(manager):
    sites = manager.sites()
    if not sites:
        print('Belum ada situs.')
    for site in sites:
        print(f'{site["id"]}  {site["primary"]}  [{site["status"]}]')
        if site.get('aliases'):
            print('              Alias: ' + ', '.join(site['aliases']))
        if site.get('secondary'):
            print('              Redirect -> Primary (301): ' + ', '.join(site['secondary']))
    return sites


def select_site(manager):
    sites = show_sites(manager)
    if not sites:
        raise ValueError('Install WordPress terlebih dahulu.')
    default = sites[0]['primary'] if len(sites) == 1 else None
    return manager.site(ask('ID/domain situs', default))['id']


def _configuration_selection(manager):
    """Collect setup choices before acquiring the write-operation lock."""
    if manager.config and manager.config.get('setup_complete', True):
        return
    if manager.config:
        cfg = manager.config
        print('Melanjutkan setup server yang sebelumnya terhenti...')
        return cfg['stack'], cfg['database']
    print('Setup awal VPS bersih (berlaku untuk seluruh situs).')
    stack = ask('Web server: 1=Nginx, 2=Apache', '1')
    database = ask('Database: 1=MariaDB, 2=MySQL', '1')
    if stack not in ('1', '2') or database not in ('1', '2'):
        raise ValueError('Pilihan harus 1 atau 2.')
    return ('nginx' if stack == '1' else 'apache',
            'mariadb' if database == '1' else 'mysql')


def configure(manager):
    selection = _configuration_selection(manager)
    if selection:
        run_operation(manager.setup, *selection)


def install_interactive(manager, host=None, email=None):
    host = host or ask('Domain primary')
    email = email or ask('Email admin / Let\'s Encrypt')
    title = ask('Judul situs', 'WordPress')
    admin = ask('Username admin', 'wpadmin')
    password = password_prompt()
    selection = _configuration_selection(manager)

    def install():
        # Keep setup and installation serialized as one composite action.
        # Manager reads current configuration/sites from disk under this lock.
        if selection:
            manager.setup(*selection)
        return manager.install(host, email, title, admin, password)

    site, password = run_operation(install)
    print(f'\nWordPress siap: https://{site["primary"]}/wp-admin/')
    print(f'Username: {admin}\nPassword: {password}')
    print(f'Kredensial root-only: {manager.data}/credentials/{site["id"]}.json')


def pma_interactive(manager, host=None, email=None):
    host = host or ask('Domain khusus phpMyAdmin (contoh db.example.com)')
    email = email or ask('Email Let\'s Encrypt')
    user = ask('Username Basic Auth', 'panel')
    pma, password = run_operation(manager.install_pma, host, email, user, password_prompt())
    print(f'phpMyAdmin: https://{pma["domain"]}/\nBasic Auth: {user}\nPassword: {password}')
    print('Setelah Basic Auth, login menggunakan user database situs (bukan akun root).')


def confirm(text, token):
    if input(f'{text}\nKetik {token} untuk lanjut: ').strip() != token:
        raise ValueError('Operasi dibatalkan.')


def add_domain_interactive(manager):
    identifier = select_site(manager)
    host = ask('Domain baru')
    kind = ask('Jenis domain: 1=Alias, 2=Redirect ke primary (301)', '1')
    www = ask('Tambahkan www juga? 1=Ya, 2=Tidak', '2')
    if kind not in ('1', '2') or www not in ('1', '2'):
        raise ValueError('Pilihan harus 1 atau 2.')
    role = 'alias' if kind == '1' else 'redirect'
    site = run_operation(manager.add_domain, identifier, host, kind=role, www=www == '1')
    if role == 'alias':
        print(f'Alias terpasang: {host}; menggunakan situs {site["primary"]}.')
    else:
        print('Redirect terpasang -> https://' + site['primary'] + ' (301).')


def set_primary_interactive(manager):
    identifier = select_site(manager)
    aliases = manager.site(identifier).get('aliases', [])
    if not aliases:
        raise ValueError('Tambahkan domain sebagai Alias melalui Add domain terlebih dahulu.')
    print('Alias yang dapat dijadikan primary: ' + ', '.join(aliases))
    host = ask('Domain yang dijadikan primary', aliases[0] if len(aliases) == 1 else None)
    confirm('URL WordPress akan diganti dan backup dibuat. Primary lama menjadi Alias.', host)
    site, backup = run_operation(manager.set_primary, identifier, host)
    print(f'Primary: {site["primary"]}\nBackup: {backup}')


def menu(manager):
    choices = [
        ('1', 'Install WordPress otomatis'), ('2', 'Daftar situs & domain'),
        ('3', 'Add domain'), ('4', 'Set as Primary'),
        ('5', 'Delete domain'), ('6', 'Install phpMyAdmin'),
        ('7', 'Delete panel phpMyAdmin'), ('8', 'Backup situs + database'),
        ('9', 'Restore backup'), ('10', 'SSL / perbaiki instalasi SSL'),
        ('11', 'Update WordPress core'), ('12', 'Status & diagnosis'),
        ('13', 'Lihat kredensial situs'), ('14', 'Lanjutkan instalasi gagal'),
        ('0', 'Keluar'),
    ]
    while True:
        print('\n' + '=' * 78)
        print(f' WPI — WordPress Installer  v{__version__}'.center(78))
        cfg = manager.config
        print((' Ubuntu CLI | ' + (f'{cfg["stack"]} / {cfg["database"]} / PHP {cfg["php_version"]}'
                                  if cfg else 'Setup otomatis pada instalasi pertama')).center(78))
        print('=' * 78)
        for i in range(0, len(choices), 2):
            left = f'({choices[i][0]}) {choices[i][1]}'
            right = f'({choices[i+1][0]}) {choices[i+1][1]}' if i + 1 < len(choices) else ''
            print(f'{left:<40}{right}')
        print('=' * 78)
        choice = input('Masukkan nomor: ').strip()
        try:
            if choice == '0':
                return
            elif choice == '1':
                install_interactive(manager)
            elif choice == '2':
                show_sites(manager)
            elif choice == '3':
                add_domain_interactive(manager)
            elif choice == '4':
                set_primary_interactive(manager)
            elif choice == '5':
                identifier = select_site(manager)
                host = ask('Domain yang dihapus')
                confirm('Lepas domain dari vhost dan SSL situs.', host)
                run_operation(manager.remove_domain, identifier, host)
                print('Domain dilepas; situs dan database tetap tersedia.')
            elif choice == '6':
                pma_interactive(manager)
            elif choice == '7':
                confirm('Hapus akses browser phpMyAdmin. Database situs tetap tersedia.', 'HAPUS PANEL')
                run_operation(manager.remove_pma)
                print('Panel phpMyAdmin dilepas; database tetap tersedia.')
            elif choice == '8':
                print('Backup lengkap: ' + str(run_operation(manager.backup, select_site(manager))))
            elif choice == '9':
                identifier = select_site(manager)
                folder = ask('Path folder backup lengkap')
                confirm('Restore akan mengganti file dan database situs. Backup kondisi sekarang dibuat.', 'RESTORE')
                _, safety = run_operation(manager.restore, identifier, folder)
                print('Restore selesai. Backup sebelum restore: ' + str(safety))
            elif choice == '10':
                run_operation(manager.retry_install_ssl, select_site(manager))
                print('SSL aktif. Renewal timer Certbot sudah dikonfigurasi.')
            elif choice == '11':
                print('Update selesai. Backup: ' + str(run_operation(manager.update_wordpress, select_site(manager))))
            elif choice == '12':
                print('\n'.join(manager.doctor()))
            elif choice == '13':
                identifier = select_site(manager)
                path = manager.data / 'credentials' / (identifier + '.json')
                print(path.read_text(encoding='utf-8'))
            elif choice == '14':
                site, password = run_operation(manager.resume_install, select_site(manager))
                print(f'https://{site["primary"]}/wp-admin/\nUsername: {site["admin"]}\nPassword: {password}')
            else:
                print('Pilihan tidak dikenal.')
        except (ValueError, RuntimeError, OSError) as error:
            print('Gagal: ' + str(error))
        input('\nTekan Enter untuk kembali ke panel...')


@contextlib.contextmanager
def operation_lock():
    # Serialize changes, while idle menus and read-only commands remain usable.
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (DATA / 'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # A real holder exists. Match the descriptor's device/inode against
            # kernel records; a leftover file or its contents prove nothing.
            report = operation_lock_status(DATA, lock_stat=os.fstat(lock.fileno()))
            raise ValueError(lock_busy_message(report)) from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_operation(action, *args, **kwargs):
    """Lock only the actual mutation, after prompts and confirmations finish."""
    with operation_lock():
        return action(*args, **kwargs)


def parser():
    result = argparse.ArgumentParser(description='WPI: panel WordPress otomatis untuk Ubuntu.')
    result.add_argument('--version', action='version', version=__version__)
    commands = result.add_subparsers(dest='command')
    commands.add_parser('menu')
    commands.add_parser('list')
    commands.add_parser('status')
    commands.add_parser('lock-status', help='Lihat PID pemegang kunci operasi tanpa mengubah server.')
    commands.add_parser('autotune-enable', help='Aktivasi otomatis saat upgrade instalasi WPI.')
    commands.add_parser('autotune-tick', help='Perintah internal timer PHP-FPM.')
    commands.add_parser('autotune-status', help='Lihat kapasitas dan keputusan PHP-FPM otomatis.')
    setup = commands.add_parser('setup')
    setup.add_argument('--stack', choices=['nginx', 'apache'], default='nginx')
    setup.add_argument('--database', choices=['mariadb', 'mysql'], default='mariadb')
    for name in ('install', 'pma-install'):
        command = commands.add_parser(name)
        command.add_argument('domain', nargs='?')
        command.add_argument('--email')
    for name in ('add-domain', 'set-primary', 'change-domain', 'delete-domain'):
        command = commands.add_parser(name)
        command.add_argument('site')
        command.add_argument('domain')
        if name == 'add-domain':
            command.add_argument('--type', dest='kind', choices=['alias', 'redirect'], default='alias')
            command.add_argument('--www', action='store_true', help='Tambahkan hostname www juga.')
        elif name == 'set-primary':
            command.add_argument('--old-domain', choices=['alias', 'redirect', 'remove'], default='alias')
    for name in ('backup', 'ssl', 'retry-install', 'update'):
        command = commands.add_parser(name)
        command.add_argument('site')
    restore = commands.add_parser('restore')
    restore.add_argument('site')
    restore.add_argument('backup')
    commands.add_parser('pma-delete')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    if sys.platform != 'linux' or os.geteuid() != 0:
        print('Jalankan sebagai root di Ubuntu: sudo wpi', file=sys.stderr)
        return 1
    manager = Manager()
    try:
        command = args.command or 'menu'
        # Menus do not hold a lock while waiting for input. The background
        # controller has its own lock; read-only commands need neither lock.
        if command == 'lock-status':
            print(json.dumps(operation_lock_status(DATA), ensure_ascii=False, sort_keys=True))
            return 0
        if command == 'autotune-tick':
            print(json.dumps(manager.autotune_tick(), ensure_ascii=False, sort_keys=True))
            return 0
        if command == 'autotune-status':
            print(json.dumps(manager.autotune_status(), ensure_ascii=False, sort_keys=True))
            return 0
        if command == 'menu':
            menu(manager)
        elif command == 'setup':
            run_operation(manager.setup, args.stack, args.database)
        elif command == 'autotune-enable':
            print(json.dumps(run_operation(manager.enable_autotune), ensure_ascii=False, sort_keys=True))
        elif command == 'list':
            show_sites(manager)
        elif command == 'status':
            print('\n'.join(manager.doctor()))
        elif command == 'install':
            install_interactive(manager, args.domain, args.email)
        elif command == 'pma-install':
            pma_interactive(manager, args.domain, args.email)
        elif command == 'pma-delete':
            confirm('Hapus akses browser phpMyAdmin; database tetap tersedia.', 'HAPUS PANEL')
            run_operation(manager.remove_pma)
        elif command == 'add-domain':
            run_operation(manager.add_domain, args.site, args.domain, kind=args.kind, www=args.www)
        elif command == 'set-primary':
            confirm('Jadikan Alias sebagai primary; URL WordPress diganti dan backup dibuat.', args.domain)
            site, backup = run_operation(manager.set_primary, args.site, args.domain, old_domain=args.old_domain)
            print(f'Primary: {site["primary"]}\nBackup: {backup}')
        elif command == 'change-domain':
            confirm('Ganti primary dan lepas domain lama; backup otomatis.', args.domain)
            site, backup = run_operation(manager.change_primary, args.site, args.domain)
            print(f'Primary: {site["primary"]}\nBackup: {backup}')
        elif command == 'delete-domain':
            confirm('Lepas domain dari situs; file WordPress dan database tetap tersedia.', args.domain)
            run_operation(manager.remove_domain, args.site, args.domain)
        elif command == 'backup':
            print(run_operation(manager.backup, args.site))
        elif command == 'restore':
            confirm('Restore file dan database situs.', 'RESTORE')
            _, safety = run_operation(manager.restore, args.site, args.backup)
            print('Restore selesai. Backup sebelum restore: ' + str(safety))
        elif command == 'ssl':
            run_operation(manager.retry_install_ssl, args.site)
        elif command == 'retry-install':
            site, password = run_operation(manager.resume_install, args.site)
            print(f'https://{site["primary"]}/wp-admin/\nUsername: {site["admin"]}\nPassword: {password}')
        elif command == 'update':
            print('Update selesai. Backup: ' + str(run_operation(manager.update_wordpress, args.site)))
    except (ValueError, RuntimeError, OSError) as error:
        print('Gagal: ' + str(error), file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\nPanel ditutup. Data yang sudah dibuat tetap tersedia.')
        return 130
    return 0


if __name__ == '__main__':
    sys.exit(main())
