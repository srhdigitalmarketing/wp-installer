# WPI — WordPress Installer CLI

Panel terminal untuk memasang dan mengelola beberapa situs WordPress di Ubuntu.
Jalankan `sudo wpi`, pilih nomor menu, lalu masukkan domain dan informasi situs.
WPI mengatur server web, PHP-FPM, database, WordPress, virtual host, dan HTTPS.

## Fitur

- Auto installer WordPress dengan pilihan **Nginx / Apache** dan
  **MariaDB / MySQL**. Pilihan standar: Nginx + MariaDB.
- Beberapa situs, masing-masing memiliki direktori WordPress, database, dan
  akun database tersendiri.
- Tambah secondary domain dengan redirect permanen **301** ke primary.
  Path dan query dipertahankan, misalnya `alias.com/artikel?x=1` menjadi
  `utama.com/artikel?x=1`.
- Ganti primary domain: backup terlebih dahulu, sesuaikan URL WordPress dan
  tautan di database menggunakan WP-CLI, terbitkan SSL untuk domain baru,
  lalu ubah konfigurasi web. Domain primary lama otomatis dilepas dari vhost
  dan pengelolaan SSL WPI.
- Pasang phpMyAdmin pada domain/subdomain tersendiri dengan HTTPS dan
  Basic Auth tambahan. Hapus phpMyAdmin hanya melepas antarmuka web tersebut;
  **database dan situs WordPress tetap ada**.
- Backup/restore situs, pemeriksaan layanan, dan pengelolaan SSL melalui menu.

## Persiapan server

Gunakan **Ubuntu Server 24.04 LTS** untuk instalasi baru; Ubuntu 22.04 LTS
juga didukung. WPI dijalankan sebagai root atau melalui `sudo`, pada server
bersih dengan `systemd`. Rekomendasi awal: RAM 2 GB dan disk 20 GB.

Satu server memakai satu pilihan stack. Nginx/Apache maupun MariaDB/MySQL
dipilih saat situs pertama dibuat; situs berikutnya memakai stack yang sama.
WPI tidak mengambil alih konfigurasi situs milik aaPanel, cPanel, Plesk,
atau installer lain. Gunakan VM terpisah jika sudah ada panel hosting.

Sebelum memasang situs atau phpMyAdmin:

1. Buat DNS **A** untuk hostname yang digunakan, menuju IPv4 publik server.
2. Jika ada DNS **AAAA**, arahkan ke IPv6 server yang dapat diakses. Hapus AAAA
   yang salah jika server tidak menyediakan IPv6.
3. Buka TCP **80 dan 443** pada firewall server, security group, dan router.
4. Saat penerbitan awal SSL, gunakan DNS langsung ke server. Jika memakai
   Cloudflare, pilih **DNS only** sampai sertifikat pertama berhasil dibuat.
5. Siapkan email untuk Let's Encrypt dan email administrator WordPress.

HTTP-01 Let's Encrypt memerlukan port 80 yang dapat dijangkau dari internet;
DNS dan firewall cloud tetap perlu disiapkan oleh pemilik server.
[Dokumentasi Let's Encrypt](https://letsencrypt.org/docs/challenge-types/)

## Instalasi di Ubuntu

Artefak aplikasi dihosting melalui GitHub Releases. URL menggunakan versi
tetap agar isi yang dipasang dapat ditinjau dan diverifikasi.

```bash
sudo apt-get update
sudo apt-get install -y curl ca-certificates

curl -fL --proto '=https' --proto-redir '=https' \
  https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.0.0/install.sh \
  -o install.sh
curl -fL --proto '=https' --proto-redir '=https' \
  https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.0.0/install.sh.sha256 \
  -o install.sh.sha256
sha256sum --check install.sh.sha256 && sudo bash install.sh --version v1.0.0

sudo wpi
```

Bootstrap memverifikasi SHA256 bundle dan memasang aplikasi serta kebutuhan
dasarnya. Paket Nginx/Apache, database, PHP, dan Certbot dipasang saat situs
WordPress pertama dibuat. Checksum dari release membantu mendeteksi file rusak;
untuk memverifikasi identitas rilis, bandingkan hash dengan sumber yang Anda
percaya atau tinjau kode pada tag versi tersebut.

### Memakai bundle lokal

Unduh `wp-installer-v1.0.0.zip` dan `wp-installer-v1.0.0.zip.sha256`
dari [release v1.0.0](https://github.com/srhdigitalmarketing/wp-installer/releases/tag/v1.0.0),
lalu salin ke server bersama `install.sh`.

```bash
sudo bash install.sh --bundle ./wp-installer-v1.0.0.zip
sudo wpi
```

Hash yang diperoleh secara terpisah juga dapat diberikan melalui `--sha256`:

```bash
sudo bash install.sh --bundle ./wp-installer-v1.0.0.zip --sha256 HASH_SHA256_RILIS
```

Validasi bundle tanpa pemasangan, tanpa akses root, dan tanpa jaringan:

```bash
bash install.sh --bundle ./wp-installer-v1.0.0.zip --check-only
```

Mode ini memerlukan Bash dan Python 3.10+. SHA256, keamanan path ZIP, kelengkapan
paket, dan sintaks Python diperiksa. Untuk install lokal, apt masih dapat
mengunduh kebutuhan bootstrap jika belum tersedia; pemasangan WordPress tetap
memerlukan jaringan.

## Penggunaan menu

Jalankan `sudo wpi` untuk panel interaktif. Pilih instalasi WordPress, isi domain
tanpa `https://` dan tanpa path, lalu ikuti pilihan stack dan akun administrator.
Password dimasukkan di terminal dan disembunyikan saat diketik. Simpan
kredensial yang ditampilkan setelah pemasangan di pengelola password.

| Nomor | Menu | Nomor | Menu |
| --- | --- | --- | --- |
| 1 | Install WordPress | 2 | Daftar situs dan domain |
| 3 | Add secondary domain (301) | 4 | Change primary domain |
| 5 | Delete secondary domain | 6 | Install phpMyAdmin |
| 7 | Delete panel phpMyAdmin | 8 | Backup situs dan database |
| 9 | Restore backup | 10 | SSL / perbaiki SSL |
| 11 | Update WordPress core | 12 | Status dan diagnosis |
| 13 | Lihat kredensial situs | 14 | Lanjutkan instalasi gagal |
| 0 | Keluar | | |

Untuk secondary domain, pilih situs primary lalu masukkan hostname secondary.
Setiap hostname harus mempunyai DNS yang benar agar SSL dan redirect HTTPS
berfungsi. Secondary tidak membuat salinan WordPress atau database baru.

Untuk mengganti primary, arahkan domain baru ke server sebelum menjalankan menu
pergantian. WPI memperbarui `home`, `siteurl`, serta URL di tabel situs dengan
penanganan data PHP yang terserialisasi. Tautan yang ditulis langsung di file
tema/plugin atau layanan eksternal perlu disesuaikan di sumbernya.
[WP-CLI search-replace](https://developer.wordpress.org/cli/commands/search-replace/)

Menghapus domain secondary melepas konfigurasi hostname tersebut dari server.
Mengganti atau melepas domain di WPI tidak mengubah registrasi domain maupun
record DNS pada penyedia DNS.

phpMyAdmin menggunakan hostname khusus, contohnya `db.example.com`. Pertama
masukkan akun Basic Auth panel, kemudian login menggunakan akun database situs.
Akun administrator WordPress dan akun database adalah dua akun berbeda.
Jangan memakai akun root database untuk akses browser sehari-hari.
Paket phpMyAdmin dapat tetap terpasang untuk menerima pembaruan keamanan Ubuntu;
menu hapus menutup virtual host dan melepas konfigurasi akses WPI.

### Perintah langsung

Menu dan perintah berikut menjalankan operasi yang sama. `SITE` dapat berupa
ID situs atau primary domain yang tercantum pada `sudo wpi list`.
Perintah install tetap meminta judul, username, dan password melalui terminal.

```bash
# Opsional: tentukan stack sebelum membuat situs pertama.
sudo wpi setup --stack nginx --database mariadb
# Alternatif pada server bersih: --stack apache --database mysql

sudo wpi install example.com --email admin@example.com
sudo wpi list
sudo wpi add-domain example.com alias.example.com
sudo wpi change-domain example.com example.net
sudo wpi delete-domain example.net alias.example.com

sudo wpi pma-install db.example.net --email admin@example.net
sudo wpi pma-delete

sudo wpi backup example.net
sudo wpi ssl example.net
sudo wpi update example.net
sudo wpi status
```

Restore memakai path folder backup lengkap yang ditampilkan oleh perintah
backup: `sudo wpi restore SITE /var/backups/wpi/ID/FOLDER_BACKUP`.
Operasi restore dan pergantian domain meminta konfirmasi di terminal sebelum
mengubah konten. WPI membuat backup kondisi saat ini sebelum restore.

Jika proses instalasi terhenti, pilih **menu 14** atau jalankan
`sudo wpi retry-install SITE`. Kredensial tersimpan memungkinkan proses
dilanjutkan tanpa membuat database baru. Jika WordPress sudah selesai terpasang
dan hanya SSL yang gagal, pilih **menu 10** atau `sudo wpi ssl SITE` setelah
DNS dan port 80 diperbaiki.

Pilih **menu 13** untuk melihat username/password WordPress dan database
situs. Kredensial ini tampil hanya pada terminal root; simpan secara pribadi.

## Data dan backup

| Lokasi | Isi |
| --- | --- |
| `/usr/local/bin/wpi` | Perintah panel. |
| `/usr/local/lib/wpi` | Symlink menuju versi aplikasi aktif. |
| `/usr/local/lib/wpi-releases/` | Versi aplikasi yang dipasang. |
| `/var/lib/wpi/` | Metadata stack, situs, domain, dan operasi WPI. |
| `/var/lib/wpi/credentials/` | Kredensial pemulihan situs; hanya root. |
| `/var/backups/wpi/` | Backup situs dan database. |
| `/var/log/wpi/` | Direktori yang disiapkan; output SQL/kredensial tidak direkam. |

Metadata dan backup hanya dapat dibaca root. Password database WordPress
disimpan pada `wp-config.php` situs agar WordPress dapat tersambung ke database.
Backup dapat memuat kredensial dan data pengguna; salin backup ke penyimpanan
terpisah yang aksesnya dibatasi. Log panel tidak mencatat password.

Menjalankan bootstrap lagi mengganti aplikasi secara atomik dan mempertahankan
data situs serta backup. Paket server dan konten WordPress dikelola terpisah
dari pembaruan aplikasi WPI.

## Pengembangan dan verifikasi

Aplikasi menggunakan Python standard library; tidak memerlukan pip atau
layanan panel berbayar. Pengujian Python dapat dijalankan di Windows dan Linux,
sedangkan operasi server hanya didukung di Ubuntu.

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q wpi
bash -n install.sh
shellcheck --severity=warning install.sh
```

Lihat [panduan pengujian](docs/TESTING.md) untuk integrasi Ubuntu dan penerimaan
dengan domain nyata. Tes unit atau bundle valid belum membuktikan DNS, browser,
dan sertifikat produksi; alur tersebut diperiksa di server dengan domain nyata.

## Referensi resmi

- [Persyaratan server WordPress](https://make.wordpress.org/hosting/handbook/server-environment/).
- [Migrasi WordPress](https://developer.wordpress.org/advanced-administration/upgrade/migrating/).
- [WP-CLI search-replace](https://developer.wordpress.org/cli/commands/search-replace/).
- [Tantangan ACME Let's Encrypt](https://letsencrypt.org/docs/challenge-types/).
- [Konfigurasi dan keamanan phpMyAdmin](https://docs.phpmyadmin.net/en/latest/setup.html).

Lisensi: [MIT](LICENSE).
