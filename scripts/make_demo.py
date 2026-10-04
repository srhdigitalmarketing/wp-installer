"""Render an honest, captioned CLI walkthrough without provisioning a server.

Uses the real CLI prompts/menu with a harmless in-memory demo manager.
Requires Pillow and an FFmpeg binary. Optional scene01.wav ... scene08.wav
provide Indonesian narration. No production credentials or domains are used.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import textwrap
from unittest.mock import patch
import wave

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wpi import cli
from wpi import __version__

WIDTH, HEIGHT = 1920, 1080
BG, PANEL = '#0b1120', '#121c2e'
TEXT, MUTED, ACCENT = '#eef4ff', '#9aaecb', '#71e8bf'


class DemoManager:
    def __init__(self):
        self.config = {}
        self.data = PurePosixPath('/var/lib/wpi')
        self.items = []

    def sites(self):
        return self.items

    def site(self, identifier):
        return next(s for s in self.items if identifier in [
            s['id'], s['primary'], *s['aliases'], *s['secondary']])

    def setup(self, stack, database):
        print('Menginstal web server, PHP-FPM, database, Certbot, dan WP-CLI...')
        self.config = {'stack': stack, 'database': database, 'php_version': '8.3'}

    def install(self, host, email, title, admin, password):
        site = {'id': 'a1b2c3d4e5f6', 'primary': host, 'aliases': [],
                'secondary': [], 'status': 'active', 'admin': admin}
        self.items.append(site)
        return site, '[disamarkan untuk video]'

    def add_secondary(self, identifier, host):
        return self.add_domain(identifier, host, kind='redirect')

    def add_domain(self, identifier, host, kind='alias', www=False):
        if kind not in ('alias', 'redirect'):
            raise ValueError('Jenis domain harus alias atau redirect.')
        site = self.site(identifier)
        role = 'aliases' if kind == 'alias' else 'secondary'
        hosts = [host]
        if www:
            hosts.append('www.' + host)
        for domain in hosts:
            if domain not in site[role]:
                site[role].append(domain)
        return site

    def change_primary(self, identifier, host):
        site = self.site(identifier)
        if host in site['aliases']:
            site['aliases'].remove(host)
        if host in site['secondary']:
            site['secondary'].remove(host)
        site['primary'] = host
        return site, self.backup(identifier)

    def set_primary(self, identifier, host, old_domain='alias'):
        site = self.site(identifier)
        if host not in site['aliases']:
            raise ValueError('Tambahkan domain sebagai Alias sebelum Set as Primary.')
        if old_domain not in ('alias', 'redirect', 'remove'):
            raise ValueError('Pilihan domain lama tidak valid.')
        old = site['primary']
        site, backup = self.change_primary(identifier, host)
        if old_domain != 'remove':
            site['aliases' if old_domain == 'alias' else 'secondary'].append(old)
        return site, backup

    def remove_domain(self, identifier, host):
        site = self.site(identifier)
        if host == site['primary']:
            raise ValueError('Set as Primary domain lain terlebih dahulu.')
        for role in ('aliases', 'secondary'):
            if host in site[role]:
                site[role].remove(host)
        return site

    def remove_secondary(self, identifier, host):
        return self.remove_domain(identifier, host)

    def backup(self, identifier):
        return PurePosixPath('/var/backups/wpi/a1b2c3d4e5f6/20261004T030000-a1b2c3')

    def install_pma(self, host, email, user, password):
        self.config['phpmyadmin'] = {'domain': host, 'user': user}
        return self.config['phpmyadmin'], '[disamarkan untuk video]'

    def remove_pma(self):
        del self.config['phpmyadmin']

    def doctor(self):
        return ['nginx: aktif', 'mariadb: aktif', 'php8.3-fpm: aktif',
                'certbot.timer: aktif', 'example.net: active; WordPress OK']


def capture(function, answers=()):
    output = io.StringIO()
    values = iter(answers)

    def enter(prompt):
        answer = next(values)
        output.write(prompt + (answer or '[Enter]') + '\n')
        return answer

    def password(prompt):
        output.write(prompt + '[Enter: otomatis]\n')
        return ''

    with contextlib.redirect_stdout(output), patch('builtins.input', enter), \
         patch('getpass.getpass', password), \
         patch.object(cli, 'operation_lock', contextlib.nullcontext):
        function()
    return output.getvalue().strip('\n')


def build_scenes():
    manager = DemoManager()
    menu = capture(lambda: cli.menu(manager), ['0']).replace('Masukkan nomor: 0', 'Masukkan nomor:')
    install = capture(lambda: cli.install_interactive(manager),
                      ['example.com', 'admin@example.com', 'Situs Demo', 'wpadmin', '1', '1'])

    domain_text = capture(lambda: cli.add_domain_interactive(manager), ['', 'example.net', '', ''])
    change_text = capture(lambda: cli.set_primary_interactive(manager), ['', 'example.net', 'example.net'])
    pma_install = capture(lambda: cli.pma_interactive(manager), ['db.example.net', 'admin@example.net', 'panel'])

    def remove_pma():
        cli.confirm('Hapus akses browser phpMyAdmin. Database situs tetap tersedia.', 'HAPUS PANEL')
        manager.remove_pma()
        print('Panel phpMyAdmin dilepas; database tetap tersedia.')

    pma_delete = capture(remove_pma, ['HAPUS PANEL'])

    def backup_status():
        print('Backup lengkap: ' + str(manager.backup(cli.select_site(manager))))
        print('\nroot@ubuntu:~# sudo wpi status')
        print('\n'.join(manager.doctor()))

    backup_text = capture(backup_status, [''])
    return [
        dict(title='Mulai dari Ubuntu bersih', label='PERSIAPAN', minimum=13,
             caption='Siapkan VPS Ubuntu, DNS domain, dan akses port 80/443.',
             lines=['WPI / WORDPRESS INSTALLER', '', 'Persiapan sebelum menjalankan panel:', '',
                    '  Ubuntu Server 24.04 LTS bersih', '  DNS A/AAAA mengarah ke IP VPS',
                    '  Port TCP 80 dan 443 terbuka', '  Email admin / Let\'s Encrypt tersedia', '',
                    'Semua domain dalam demo memakai nama contoh.',
                    'Operasi server menggunakan backend simulasi.'],
             facts=['Ubuntu 24.04 LTS', '22.04 LTS juga didukung', 'DNS dan firewall cloud', 'disiapkan pemilik server.']),
        dict(title='Pasang script dari GitHub', label='BOOTSTRAP', minimum=16,
             caption='Unduh script, verifikasi SHA256, lalu buka panel dengan sudo wpi.',
             lines=['# Contoh perintah instalasi di Ubuntu', '',
                    'sudo apt-get update', 'sudo apt-get install -y curl ca-certificates', '',
                    'WPI_URL="https://github.com/srhdigitalmarketing/\\',
                    f'wp-installer/releases/download/v{__version__}"', '',
                    'curl -fL "$WPI_URL/install.sh" -o install.sh',
                    'curl -fL "$WPI_URL/install.sh.sha256" -o install.sh.sha256', '',
                    'sha256sum --check install.sh.sha256 && \\',
                    f'sudo bash install.sh --version v{__version__}', '', 'sudo wpi'],
             facts=['Hosting script gratis', f'GitHub Releases v{__version__}', 'Checksum SHA256', 'Bundle aplikasi diperiksa', 'sebelum dipasang.']),
        dict(title='Satu panel, semua operasi', label='PANEL CLI', minimum=13,
             caption='Pilih nomor menu untuk instalasi, domain, phpMyAdmin, dan backup.',
             lines=menu.splitlines(), facts=['Menu CLI asli', 'Ditampilkan dari wpi/cli.py', '14 pilihan operasi', 'Install · domain · SSL', 'Backup · status · recovery']),
        dict(title='Install WordPress otomatis', label='MENU 1', minimum=22,
             caption='Isi domain dan akun admin, lalu pilih web server serta database.',
             lines=['Masukkan nomor: 1', *install.splitlines()],
             facts=['Contoh pilihan', 'Nginx + MariaDB', 'Pilihan lainnya', 'Apache + MySQL', 'Konfigurasi otomatis', 'PHP-FPM · WordPress · SSL']),
        dict(title='Tambahkan domain Alias atau Redirect', label='MENU 3', minimum=16,
             caption='Pilih Alias untuk aplikasi yang sama, atau Redirect untuk 301 ke Primary.',
             lines=['Masukkan nomor: 3', *domain_text.splitlines(), '',
                    '# Konfigurasi yang diharapkan — simulasi', 'example.net: Alias aplikasi WordPress',
                    'Alias dapat dipilih menjadi Primary pada menu 4.'],
             facts=['Alias baru', 'example.net', 'Pilihan lainnya', 'Redirect 301 → Primary', 'www opsional', 'DNS dan SSL diperiksa.']),
        dict(title='Set as Primary dari daftar Alias', label='MENU 4', minimum=20,
             caption='Backup otomatis dan replace URL database; domain primary lama tetap menjadi Alias.',
             lines=['Masukkan nomor: 4', *change_text.splitlines()],
             facts=['Primary baru', 'example.net', 'Primary lama tetap Alias', 'example.com', 'Database URL diganti', 'Data terserialisasi ditangani', 'Tautan dalam file tema', 'disesuaikan di sumbernya.']),
        dict(title='Kelola panel phpMyAdmin', label='MENU 6 + 7', minimum=24,
             caption='Hapus akses phpMyAdmin tetap mempertahankan database WordPress.',
             lines=['Masukkan nomor: 6', *pma_install.splitlines(), '', 'Masukkan nomor: 7', *pma_delete.splitlines()],
             facts=['Domain panel terpisah', 'db.example.net', 'Dua langkah login', 'Basic Auth + akun database', 'Hapus panel browser', 'Database situs tetap ada.']),
        dict(title='Backup, status, dan pemulihan', label='MENU 8 + 12', minimum=17,
             caption='Simpan backup, periksa layanan, dan unduh script dari GitHub Releases.',
             lines=['Masukkan nomor: 8', *backup_text.splitlines()],
             facts=['Fitur tambahan', 'Restore backup', 'Update WordPress core', 'Perbaikan SSL', 'Lanjutkan instalasi gagal', '', 'github.com/', 'srhdigitalmarketing/', 'wp-installer']),
    ]


def fonts():
    base = Path('C:/Windows/Fonts')
    return {name: ImageFont.truetype(str(base / filename), size) for name, filename, size in [
        ('brand', 'segoeuib.ttf', 28), ('title', 'segoeuib.ttf', 53), ('subtitle', 'segoeui.ttf', 27),
        ('mono', 'consola.ttf', 24), ('small', 'segoeui.ttf', 22), ('fact', 'segoeui.ttf', 28),
        ('factbold', 'segoeuib.ttf', 27), ('caption', 'segoeui.ttf', 30)]}


def draw_frame(scene, index, shown, fraction, output, font):
    canvas = Image.new('RGB', (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((72, 36), 'WPI  /  WORDPRESS INSTALLER', font=font['brand'], fill=ACCENT)
    draw.rounded_rectangle((1328, 31, 1848, 79), radius=14, fill='#223146')
    draw.text((1350, 40), 'DEMO SIMULASI  ·  DOMAIN CONTOH', font=font['small'], fill='#ffda8c')
    draw.text((72, 106), scene['title'], font=font['title'], fill=TEXT)
    draw.text((75, 177), f'{index+1:02d} / 08   •   {scene["label"]}', font=font['subtitle'], fill=MUTED)
    draw.rounded_rectangle((72, 235, 1352, 925), radius=18, fill='#080e17', outline='#25334a', width=2)
    draw.rounded_rectangle((72, 235, 1352, 281), radius=18, fill='#1a2639')
    draw.rectangle((73, 258, 1350, 282), fill='#1a2639')
    for i, color in enumerate(('#ff6a73', '#ffce61', '#73d7a7')):
        draw.ellipse((91+i*27, 251, 103+i*27, 263), fill=color)
    draw.text((196, 244), 'ubuntu  /  wpi   —   contoh alur CLI', font=font['small'], fill=MUTED)
    wrapped = []
    for line in shown:
        wrapped.extend(textwrap.wrap(line, 84, replace_whitespace=False, drop_whitespace=False) or [''])
    for row, line in enumerate(wrapped[-19:]):
        color = TEXT
        if line.startswith('#') or set(line.strip()) <= {'='}:
            color = '#67849f'
        elif 'terpasang' in line or 'aktif' in line or 'WordPress siap' in line or 'Primary:' in line:
            color = ACCENT
        elif ': ' in line and '[disamarkan' not in line:
            color = '#b7d7ff'
        draw.text((100, 299+row*32), line, font=font['mono'], fill=color)
    if len(wrapped) > 19:
        draw.text((1275, 290), '↑', font=font['small'], fill=MUTED)
    draw.rounded_rectangle((1380, 235, 1848, 925), radius=18, fill=PANEL)
    draw.text((1406, 263), 'CATATAN ALUR', font=font['small'], fill=ACCENT)
    y = 315
    for number, fact in enumerate(scene['facts']):
        is_title = number == 0 or (index in (3, 4, 5, 6) and number % 2 == 0)
        chosen = font['factbold'] if is_title else font['fact']
        for line in textwrap.wrap(fact, width=29) or ['']:
            draw.text((1406, y), line, font=chosen, fill=TEXT if is_title else MUTED)
            y += 38
        y += 13 if not is_title else 2
    draw.text((1406, 822), 'Backend simulasi.', font=font['small'], fill='#ffda8c')
    draw.text((1406, 854), 'Bukan rekaman VPS produksi.', font=font['small'], fill=MUTED)
    caption_lines = textwrap.wrap(scene['caption'], 104)
    for row, line in enumerate(caption_lines):
        length = draw.textlength(line, font=font['caption'])
        draw.text(((WIDTH-length)/2, 952+row*37), line, font=font['caption'], fill=TEXT)
    draw.rounded_rectangle((72, 1036, 1848, 1043), radius=3, fill='#223146')
    draw.rounded_rectangle((72, 1036, 72+1776*((index+fraction)/8), 1043), radius=3, fill=ACCENT)
    canvas.save(output)


def srt_time(value):
    millis = round(value * 1000)
    return f'{millis//3600000:02d}:{millis//60000%60:02d}:{millis//1000%60:02d},{millis%1000:03d}'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ffmpeg', required=True)
    parser.add_argument('--audio-dir', type=Path, default=ROOT / '.local/video-audio')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/WPI-demo-Indonesia.mp4')
    args = parser.parse_args()
    scenes = build_scenes()
    folder = ROOT / '.local/video-frames'
    folder.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(exist_ok=True)
    font = fonts()
    audio_parts = []
    rate = None
    for index, scene in enumerate(scenes):
        clip = args.audio_dir / f'scene-{index+1:02d}.wav'
        if clip.exists():
            with wave.open(str(clip), 'rb') as audio:
                channels, sample_width, clip_rate = audio.getnchannels(), audio.getsampwidth(), audio.getframerate()
                if channels != 1 or sample_width != 2 or (rate is not None and rate != clip_rate):
                    raise ValueError('Narration clips must be mono PCM16 with a shared rate.')
                rate = clip_rate
                blob = audio.readframes(audio.getnframes())
                length = len(blob) / (2 * rate)
            scene['duration'] = max(scene['minimum'], math.ceil(length + 2.2))
            audio_parts.append(b'\0' * round(rate * 2 * 0.6) + blob +
                               b'\0' * round(rate * 2 * (scene['duration']-length-0.6)))
        else:
            scene['duration'] = scene['minimum']
            audio_parts.append(None)
    narrated = all(part is not None for part in audio_parts)
    narration = folder / 'narration.wav'
    if narrated:
        with wave.open(str(narration), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            for part in audio_parts:
                audio.writeframes(part)
    concat = ['ffconcat version 1.0']
    subs, chapter_data = [], []
    start = 0.0
    for index, scene in enumerate(scenes):
        lines = scene['lines']
        # Reveal real CLI output progressively, with an initial shell/context line.
        steps = list(range(1, len(lines)+1)) if index != 2 else [len(lines)]
        duration = scene['duration']
        reveal = duration * (0.60 if index in (3, 6) else 0.48)
        per_step = reveal / len(steps)
        for number, count in enumerate(steps):
            image_path = folder / f'scene{index+1:02d}-{number:03d}.png'
            draw_frame(scene, index, lines[:count], number / len(steps), image_path, font)
            concat += [f"file '{image_path.name}'", f'duration {per_step:.6f}']
        final = folder / f'scene{index+1:02d}-final.png'
        draw_frame(scene, index, lines, 0.97, final, font)
        concat += [f"file '{final.name}'", f'duration {duration-reveal:.6f}']
        subs.append(f'{index+1}\n{srt_time(start)} --> {srt_time(start+duration)}\n{scene["caption"]}\n')
        chapter_data.append({'scene': index+1, 'title': scene['title'], 'start': start,
                             'duration': duration, 'caption': scene['caption']})
        start += duration
    concat.append(f"file '{final.name}'")
    (folder / 'frames.ffconcat').write_text('\n'.join(concat)+'\n', encoding='utf-8')
    args.output.with_suffix('.srt').write_text('\n'.join(subs), encoding='utf-8')
    (folder / 'chapters.json').write_text(json.dumps(chapter_data, indent=2, ensure_ascii=False), encoding='utf-8')
    command = [args.ffmpeg, '-hide_banner', '-y', '-f', 'concat', '-safe', '0', '-i', 'frames.ffconcat']
    if narrated:
        command += ['-i', 'narration.wav']
    command += ['-vf', 'fps=24,tpad=stop_mode=clone:stop_duration=2',
                '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
                '-r', '24', '-t', str(start), '-movflags', '+faststart',
                '-metadata', 'title=WPI - Demo CLI WordPress (simulasi)',
                '-metadata', 'comment=Indonesian narrated walkthrough. Real CLI prompts with simulated backend.']
    if narrated:
        command += ['-c:a', 'aac', '-b:a', '160k']
    command += [str(args.output.resolve())]
    print(f'Rendering {start:.0f}s at 1920x1080; narrated={narrated}', flush=True)
    with (folder / 'encode.log').open('w', encoding='utf-8') as log:
        subprocess.run(command, cwd=folder, stdout=log, stderr=log, check=True)
    # Share a clean poster as well as the MP4 and editable subtitles.
    draw_frame(scenes[2], 2, scenes[2]['lines'], 0.5, args.output.with_suffix('.jpg'), font)
    print('Video:', args.output)
    print('Duration:', start, 'seconds; bytes:', args.output.stat().st_size)


if __name__ == '__main__':
    main()
