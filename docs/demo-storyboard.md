# Storyboard WPI: menu terbaru dan arsip video 1.0.0

Video publik yang sudah diunggah adalah rekaman **v1.0.0**, dengan alur domain
lama: secondary selalu Redirect 301 dan pergantian primary melepas domain lama.
Video tersebut belum memperlihatkan Alias atau Set as Primary pada v1.3.0 dan
tidak diregenerasi dalam rilis ini. Transkrip historis di bawah dipertahankan
untuk menjelaskan rekaman yang tersedia.

## Diagram menu v1.3.0

Diagram berikut menunjukkan menu terbaru; ini bukan transkrip video lama:

```text
(3) Add domain        -> Alias (standar) atau Redirect 301; www opsional
(4) Set as Primary    -> Pilih Alias; backup dan replace URL; primary lama Alias
(5) Delete domain     -> Lepas hostname Alias/Redirect; situs dan database tetap
```

Satu situs mempunyai satu Primary. Untuk menjadikan domain baru sebagai primary,
tambahkan sebagai Alias melalui menu 3, lalu pilih domain tersebut pada menu 4.
Menu 5 menolak penghapusan primary sampai Alias lain dijadikan primary. Semua
hostname baru tetap memerlukan DNS ke VPS agar SSL dapat dikonfigurasi otomatis.

## Arsip rekaman v1.0.0

Durasi ekspor: 142 detik, 8 adegan. Bahasa: Indonesia. Rekam menu dan prompt asli
`wpi/cli.py` dengan backend simulasi. Domain contoh tidak dipasang pada VPS.

Label permanen di layar: **DEMO SIMULASI • domain contoh • bukan server produksi**.
Footer tambahan saat membahas SSL: **DNS, instalasi paket, database, dan penerbitan SSL disimulasikan.**
Jangan menampilkan sertifikat publik, browser WordPress live, atau keluaran ACME
yang mengesankan pemasangan sungguhan. Password dalam keluaran demo disamarkan.

Waktu tabel adalah rancangan awal; ekspor menambahkan waktu membaca terminal.
Video final: `dist/WPI-demo-Indonesia.mp4`, H.264/AAC, 1920×1080, 24 fps.
Subtitle tersedia dalam video dan sebagai `dist/WPI-demo-Indonesia.srt`.
Narasi sintetis Indonesia memakai voice `id-ID-ArdiNeural`.

Renderer ada di `scripts/make_demo.py`. Gunakan Python + Pillow + FFmpeg,
font Consolas/Segoe UI Windows, serta delapan WAV mono PCM16 dengan sample rate
sama bernama `scene-01.wav` hingga `scene-08.wav` dalam folder audio.
Contoh: `python scripts/make_demo.py --ffmpeg PATH_FFMPEG --audio-dir FOLDER_WAV`.
Renderer memanggil fungsi CLI asli dengan `DemoManager` dalam memori; tidak
menjalankan apt, SQL, Certbot, atau penggantian domain pada server sungguhan.

| Waktu | Adegan | Subtitle satu kalimat | Narasi singkat |
| --- | --- | --- | --- |
| 00:00–00:12 | Persiapan Ubuntu | Mulai dengan Ubuntu bersih, DNS ke VPS, dan port 80/443 terbuka. | Ini demo simulasi WPI, panel WordPress untuk Ubuntu. Gunakan VPS bersih, arahkan DNS domain ke server, lalu buka port delapan puluh dan empat empat tiga. |
| 00:12–00:27 | Install script | Unduh rilis resmi, periksa checksum, lalu jalankan installer. | Unduh installer dari GitHub Releases, verifikasi checksum, kemudian jalankan dengan sudo. Script memasang panel WPI; paket server dipasang saat situs pertama dibuat. |
| 00:27–00:37 | Panel CLI | Ketik sudo wpi untuk membuka menu pengelolaan situs. | Panel menyediakan instalasi WordPress, pengelolaan domain, phpMyAdmin, backup, SSL, dan diagnosis. Pilih nomor menu, lalu ikuti pertanyaan yang muncul di terminal. |
| 00:37–00:57 | Install WordPress | Masukkan domain dan akun admin, lalu pilih Nginx/Apache serta MariaDB/MySQL. | Masukkan domain, email, judul, dan akun administrator. Pada contoh ini kita memilih Nginx dan MariaDB. WPI mengatur PHP, database, WordPress, serta SSL secara otomatis. |
| 00:57–01:11 | Secondary domain | Secondary mengarah ke primary dengan 301, termasuk path dan query. | Tambahkan alias sebagai secondary. Pengunjung dialihkan permanen ke primary dengan kode tiga kosong satu. Alamat artikel dan parameter query tetap dipertahankan dalam pengalihan. |
| 01:11–01:29 | Change primary | Ganti primary: backup otomatis, replace URL database, lalu lepas domain lama. | Saat primary diganti, WPI membuat backup, mengganti URL dalam database, dan melepas domain lama dari konfigurasi. Tautan langsung dalam file tema perlu disesuaikan terpisah. |
| 01:29–01:48 | phpMyAdmin install/delete | phpMyAdmin memakai domain khusus; hapus panel tetap mempertahankan database. | phpMyAdmin dipasang pada subdomain khusus, dengan HTTPS dan Basic Auth. Setelah itu gunakan akun database situs. Menu hapus hanya menutup panel; database WordPress tetap tersedia. |
| 01:48–02:00 | Backup, diagnosis, tautan | Simpan backup, periksa layanan, dan dapatkan script gratis di GitHub Releases. | Buat backup situs dan database, lalu periksa status layanan. Script tersedia gratis di GitHub Releases. Semua operasi server dalam video ini menggunakan backend simulasi. |

## Perintah dan input yang sesuai implementasi

### Adegan 1 — persiapan

Gunakan kartu informasi, bukan keluaran palsu terminal:

```text
Ubuntu Server 24.04 LTS bersih
DNS A/AAAA -> IP VPS yang benar
TCP 80 dan 443 terbuka
Email admin / Let's Encrypt tersedia
```

Ubuntu 22.04 LTS juga didukung. DNS dan firewall cloud disiapkan pemilik server.
Semua domain video memakai nama contoh `example.com`, `alias.example.com`,
`example.net`, dan `db.example.net`.

### Adegan 2 — bootstrap

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates
WPI_URL="https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.0.0"
curl -fL --proto '=https' --proto-redir '=https' "$WPI_URL/install.sh" -o install.sh
curl -fL --proto '=https' --proto-redir '=https' "$WPI_URL/install.sh.sha256" -o install.sh.sha256
sha256sum --check install.sh.sha256 && sudo bash install.sh --version v1.0.0
sudo wpi
```

Jika perintah tidak benar-benar dijalankan pada Ubuntu, tampilkan sebagai
**contoh perintah instalasi**, tanpa mengklaim hasil pemasangan nyata.
Kalimat sukses bootstrap yang memang terdapat dalam `install.sh`:
`WPI 1.0.0 berhasil dipasang. Jalankan: sudo wpi`.

### Adegan 3 — menu asli pada rekaman v1.0.0

Cuplikan berikut adalah menu historis sebelum stack dikonfigurasi; menu terbaru
untuk nomor 3, 4, dan 5 ditunjukkan pada diagram v1.3.0 di atas:

```text
WPI — WordPress Installer  v1.0.0
Ubuntu CLI | Setup otomatis pada instalasi pertama
(1) Install WordPress otomatis           (2) Daftar situs & domain
(3) Add domain                           (4) Change domain primary
(5) Delete domain secondary              (6) Install phpMyAdmin
(7) Delete panel phpMyAdmin              (8) Backup situs + database
(9) Restore backup                       (10) SSL / perbaiki instalasi SSL
(11) Update WordPress core               (12) Status & diagnosis
(13) Lihat kredensial situs               (14) Lanjutkan instalasi gagal
(0) Keluar
Masukkan nomor:
```

### Adegan 4 — instalasi pertama

Urutan input menu asli:

```text
Masukkan nomor: 1
Domain primary: example.com
Email admin / Let's Encrypt: admin@example.com
Judul situs [WordPress]: Situs Demo
Username admin [wpadmin]: wpadmin
Password (Enter = otomatis): [Enter]
Setup awal VPS bersih (berlaku untuk seluruh situs).
Web server: 1=Nginx, 2=Apache [1]: 1
Database: 1=MariaDB, 2=MySQL [1]: 1
```

Keluaran yang sesuai kode:

```text
Menginstal web server, PHP-FPM, database, Certbot, dan WP-CLI...
WordPress siap: https://example.com/wp-admin/
Username: wpadmin
Password: [disamarkan untuk video]
Kredensial root-only: /var/lib/wpi/credentials/a1b2c3d4e5f6.json
```

`a1b2c3d4e5f6` adalah ID situs tetap untuk backend simulasi. ID pemasangan
sungguhan diacak. Jangan menambahkan klaim `SSL publik terverifikasi`.

### Adegan 5 — tambah secondary

```text
Masukkan nomor: 3
a1b2c3d4e5f6  example.com  [active]
ID/domain situs [example.com]: [Enter]
Domain secondary baru: alias.example.com
Secondary terpasang -> https://example.com (301).
```

Ilustrasi pengalihan, diberi judul **hasil yang diharapkan — simulasi**:

```text
https://alias.example.com/artikel?x=1
       301 -> https://example.com/artikel?x=1
```

Perintah langsung yang setara: `sudo wpi add-domain example.com alias.example.com`.

### Adegan 6 — ganti primary

```text
Masukkan nomor: 4
ID/domain situs [example.com]: [Enter]
Domain primary baru: example.net
URL database akan diganti, domain primary lama dilepas. Backup otomatis dibuat.
Ketik example.net untuk lanjut: example.net
Primary: example.net
Backup: /var/backups/wpi/a1b2c3d4e5f6/20261004T030000-a1b2c3
```

Lalu pilih menu `2` untuk menunjukkan metadata simulasi setelah pergantian:

```text
a1b2c3d4e5f6  example.net  [active]
              Secondary -> 301: alias.example.com
```

Keterangan grafis: `home/siteurl dan URL tabel WordPress diperbarui;
example.com dilepas dari vhost dan pengelolaan SSL WPI`.
Domain lama tidak otomatis menjadi redirect. Registrasi dan DNS domain tidak
dihapus pada penyedia DNS. Secondary yang sudah ada kini menuju primary baru.

Perintah langsung: `sudo wpi change-domain example.com example.net`.

### Adegan 7 — phpMyAdmin

```text
Masukkan nomor: 6
Domain khusus phpMyAdmin (contoh db.example.com): db.example.net
Email Let's Encrypt: admin@example.net
Username Basic Auth [panel]: panel
Password (Enter = otomatis): [Enter]
phpMyAdmin: https://db.example.net/
Basic Auth: panel
Password: [disamarkan untuk video]
Setelah Basic Auth, login menggunakan user database situs (bukan akun root).
```

Kembali ke menu dengan Enter, kemudian:

```text
Masukkan nomor: 7
Hapus akses browser phpMyAdmin. Database situs tetap tersedia.
Ketik HAPUS PANEL untuk lanjut: HAPUS PANEL
Panel phpMyAdmin dilepas; database tetap tersedia.
```

Perintah langsung: `sudo wpi pma-install db.example.net --email admin@example.net`
dan `sudo wpi pma-delete`.
Penghapusan panel melepas vhost, konfigurasi akses, dan sertifikat terkait;
paket phpMyAdmin dapat tetap terpasang untuk pembaruan keamanan Ubuntu.

### Adegan 8 — backup dan status

```text
Masukkan nomor: 8
ID/domain situs [example.net]: [Enter]
Backup lengkap: /var/backups/wpi/a1b2c3d4e5f6/20261004T030100-b2c3d4
```

Kembali dengan Enter, lalu:

```text
Masukkan nomor: 12
nginx: aktif
mariadb: aktif
php8.3-fpm: aktif
certbot.timer: aktif
example.net: active; WordPress OK
```

Output status di atas berasal dari backend simulasi, bukan pemeriksaan VPS.
Perintah langsung: `sudo wpi backup example.net` dan `sudo wpi status`.

Kartu penutup dengan tautan pendek yang mudah dibaca:

```text
Script gratis + panduan Ubuntu:
github.com/srhdigitalmarketing/wp-installer
Rilis: v1.0.0
DEMO SIMULASI — pemasangan server & SSL tidak dijalankan dalam video
```

Sumber transkrip historis: `README.md`, `wpi/cli.py`, `wpi/core.py`, dan
`install.sh` pada rilis v1.0.0. Diagram terbaru mengikuti menu v1.3.0. Overlay
penjelasan dibedakan dari keluaran terminal asli.
