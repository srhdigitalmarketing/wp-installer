# WPI — WordPress Installer CLI

Panel terminal untuk memasang dan mengelola beberapa situs WordPress di Ubuntu.
Jalankan `sudo wpi`, pilih nomor menu, lalu masukkan domain dan informasi situs.
WPI mengatur server web, PHP-FPM, database, WordPress, virtual host, dan HTTPS.
Redis object cache juga dipasang dan diaktifkan otomatis untuk setiap situs.

[Tonton / unduh video demo CLI (MP4, 1080p)](https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.0.0/WPI-demo-Indonesia.mp4)
— 2 menit 22 detik, narasi Indonesia dan subtitle. Video menggunakan menu/prompt
asli dengan backend simulasi serta domain contoh; tidak memasang VPS produksi.
Video merekam v1.0.0. Pengelolaan Alias dan Set as Primary pada v1.3.0 mengikuti
panduan terbaru di bawah; perilakunya berbeda dari rekaman lama.

## Fitur

- Auto installer WordPress dengan pilihan **Nginx / Apache** dan
  **MariaDB / MySQL**. Pilihan standar: Nginx + MariaDB.
- Konfigurasi PHP dan PHP-FPM otomatis berdasarkan RAM/CPU yang tersedia,
  termasuk batas resource container. Controller berkala memantau antrean PHP,
  penggunaan worker, memori, dan beban CPU, lalu menyesuaikan kapasitas FPM
  ketika diperlukan. Mulai v1.2.0, kapasitas mengikuti resource server tanpa
  batas tetap 128 worker. Tidak perlu mengisi nilai PHP/FPM secara manual.
- Beberapa situs, masing-masing memiliki direktori WordPress, database, dan
  akun database tersendiri.
- **Redis object cache otomatis**, melalui PhpRedis dan plugin Redis Object Cache
  resmi. Setiap situs memiliki instance dan Unix socket sendiri; flush cache satu
  situs hanya memengaruhi situs tersebut. Anggaran cache mengikuti RAM efektif,
  memakai eviction `allkeys-lfu`, dan dihitung bersama kapasitas PHP-FPM.
  **Menu 18** mengoptimalkan Redis/PHP dan **menu 19** mengelola cache situs.
  [Panduan Redis dan optimasi](docs/REDIS.md).
  v1.6.1 memperbaiki izin Metrics plugin yang belum lengkap pada v1.6.0;
  upgrade mempertahankan kredensial dan cache. Grafik membutuhkan request
  WordPress pada beberapa menit sebelum data ditampilkan.
- **Add domain**: pilih **Alias** untuk membuka situs WordPress yang sama pada
  domain tambahan, atau **Redirect** untuk pengalihan permanen **301** ke primary.
  Untuk domain tanpa www, pilihan **www** menambahkan hostname www juga.
- **Set as Primary**: pilih Alias yang sudah terpasang. WPI membuat backup dan
  menyesuaikan URL WordPress serta tautan database menggunakan WP-CLI, termasuk
  data terserialisasi. Primary lama menjadi Alias secara standar.
- **Delete domain**: melepas Alias atau Redirect dan SSL hostname tersebut;
  aplikasi, file WordPress, serta database tetap tersedia.
- Pasang phpMyAdmin pada domain/subdomain tersendiri dengan HTTPS dan
  Basic Auth tambahan. Hapus phpMyAdmin hanya melepas antarmuka web tersebut;
  **database dan situs WordPress tetap ada**.
- Backup/restore situs, pemeriksaan layanan, dan pengelolaan SSL melalui menu.
- **Repair error situs** melalui menu: pemeriksaan PHP `wp-config.php`, runtime
  WordPress, layanan, dan respons frontend/admin. WPI menyimpan config asli
  secara privat, memperbaiki penyebab umum yang dapat diverifikasi, lalu
  melaporkan apakah situs pulih. Database dan media tidak dikembalikan ke backup.
- **PHP memory limit dan max upload size** dapat diatur dari panel, misalnya
  memory **500 MiB** dan upload **256 MiB**, atau dikembalikan ke profil otomatis.
  Pengaturan berlaku untuk semua situs dan phpMyAdmin pada pool PHP bersama;
  controller FPM tetap aktif. [Panduan repair dan PHP](docs/REPAIR-PHP.md).
- **Migrasi otomatis ke server baru**: masukkan IP, username SSH, dan password
  yang disembunyikan saat diketik. WPI menyiapkan Ubuntu tujuan, menyalin semua
  situs beserta database/domain/phpMyAdmin, dan mengaktifkan auto-SSL. Setelah
  selesai, arahkan DNS A/AAAA ke IP server baru. [Panduan migrasi](docs/MIGRATION.md).

WordPress diunduh dari ZIP resmi dan diverifikasi checksum sebelum pemasangan.
ZIP menghindari masalah nama file panjang pada ekstraksi tar WordPress 7.
[Laporan upstream WP-CLI](https://github.com/wp-cli/core-command/issues/336)

## Persiapan server

Gunakan **Ubuntu Server 24.04 LTS** untuk instalasi baru; Ubuntu 22.04 LTS
juga didukung. WPI dijalankan sebagai root atau melalui `sudo`, pada server
bersih dengan `systemd`. Rekomendasi awal: RAM 2 GB dan disk 20 GB.

Satu server memakai satu pilihan stack. Nginx/Apache maupun MariaDB/MySQL
dipilih saat situs pertama dibuat; situs berikutnya memakai stack yang sama.
WPI tidak mengambil alih konfigurasi situs milik aaPanel, cPanel, Plesk,
atau installer lain. Gunakan VM terpisah jika sudah ada panel hosting.

Sebelum memasang situs atau phpMyAdmin:

1. Buat DNS **A** untuk setiap hostname yang digunakan, menuju IPv4 publik
   server. Jika menambahkan www, siapkan DNS untuk kedua hostname tersebut.
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
  https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.6.1/install.sh \
  -o install.sh
curl -fL --proto '=https' --proto-redir '=https' \
  https://github.com/srhdigitalmarketing/wp-installer/releases/download/v1.6.1/install.sh.sha256 \
  -o install.sh.sha256
sha256sum --check install.sh.sha256 && sudo bash install.sh --version v1.6.1

sudo wpi
```

Bootstrap memverifikasi SHA256 bundle dan memasang aplikasi serta kebutuhan
dasarnya. Paket Nginx/Apache, database, PHP, Redis, dan Certbot dipasang saat situs
WordPress pertama dibuat. Checksum dari release membantu mendeteksi file rusak;
untuk memverifikasi identitas rilis, bandingkan hash dengan sumber yang Anda
percaya atau tinjau kode pada tag versi tersebut.

### Memakai bundle lokal

Unduh `wp-installer-v1.6.1.zip` dan `wp-installer-v1.6.1.zip.sha256`
dari [release v1.6.1](https://github.com/srhdigitalmarketing/wp-installer/releases/tag/v1.6.1),
lalu salin ke server bersama `install.sh`.

```bash
sudo bash install.sh --bundle ./wp-installer-v1.6.1.zip
sudo wpi
```

Hash yang diperoleh secara terpisah juga dapat diberikan melalui `--sha256`:

```bash
sudo bash install.sh --bundle ./wp-installer-v1.6.1.zip --sha256 HASH_SHA256_RILIS
```

Validasi bundle tanpa pemasangan, tanpa akses root, dan tanpa jaringan:

```bash
bash install.sh --bundle ./wp-installer-v1.6.1.zip --check-only
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
| 3 | Add domain | 4 | Set as Primary |
| 5 | Delete domain | 6 | Install phpMyAdmin |
| 7 | Delete panel phpMyAdmin | 8 | Backup situs dan database |
| 9 | Restore backup | 10 | SSL / perbaiki SSL |
| 11 | Update WordPress core | 12 | Status dan diagnosis |
| 13 | Lihat kredensial situs | 14 | Lanjutkan instalasi gagal |
| 15 | Migrasi otomatis ke server baru | 16 | Repair error situs |
| 17 | PHP memory limit / max upload size | 18 | Optimalkan Redis dan PHP |
| 19 | Status / kelola cache Redis | 0 | Keluar |

Setiap situs mempunyai satu **Primary**. Pada menu **3 — Add domain**, pilih
situs, masukkan domain baru, lalu pilih **Alias** atau **Redirect**. Alias adalah
pilihan standar dan membuka situs WordPress yang sama pada domain tambahan.
Redirect mengirim pengunjung ke primary dengan kode 301, mempertahankan path
dan query, misalnya `tambahan.com/artikel?x=1` menuju
`utama.com/artikel?x=1`. Keduanya memakai file dan database situs yang sama.

Untuk menambahkan `example.net` dan `www.example.net` bersamaan, masukkan
`example.net` lalu jawab **Ya** pada pertanyaan www. DNS kedua hostname harus
menunjuk server. WPI menyiapkan konfigurasi web dan SSL setiap hostname secara
otomatis; jika pemasangan salah satu hostname gagal, pasangan baru dibatalkan.

Pada Alias, alamat dan tautan yang dihasilkan WordPress mengikuti domain Alias
yang sedang dibuka. Tautan yang sudah tertulis di konten, tema/plugin, atau
layanan eksternal masih dapat menuju primary; menambah Alias tidak mengganti
seluruh tautan yang sudah tersimpan.

Untuk mengganti primary melalui menu **4 — Set as Primary**, tambahkan domain
baru sebagai Alias terlebih dahulu, lalu pilih Alias tersebut. WPI membuat
backup dan memperbarui `home`, `siteurl`, serta URL di tabel situs dengan
penanganan data PHP yang terserialisasi. Primary lama tetap terpasang sebagai
Alias. Tautan yang ditulis langsung di file tema/plugin atau layanan eksternal
perlu disesuaikan di sumbernya.
[WP-CLI search-replace](https://developer.wordpress.org/cli/commands/search-replace/)

Menu **5 — Delete domain** melepas satu hostname Alias atau Redirect dari
konfigurasi situs dan pengelolaan SSL. File WordPress dan database tetap ada.
Primary tidak dapat dihapus sebelum Alias lain dijadikan primary. Untuk
pasangan tanpa www dan www, masing-masing hostname dapat dilepas terpisah.
Mengganti atau melepas domain di WPI tidak mengubah registrasi maupun record
DNS pada penyedia DNS.

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
# Alias adalah pilihan standar; --www menambahkan example.net dan www.example.net.
sudo wpi add-domain example.com example.net --type alias --www
sudo wpi add-domain example.com redirect.example.com --type redirect
sudo wpi set-primary example.com example.net
sudo wpi delete-domain example.net redirect.example.com

sudo wpi pma-install db.example.net --email admin@example.net
sudo wpi pma-delete

sudo wpi backup example.net
sudo wpi ssl example.net
sudo wpi update example.net
sudo wpi status
sudo wpi lock-status

# Diagnosis saja, tanpa menerapkan perbaikan.
sudo wpi repair example.net --check
# Jalankan diagnosis dan perbaikan penyebab umum yang terverifikasi.
sudo wpi repair example.net

# Berlaku untuk seluruh situs dan phpMyAdmin, angka dalam MiB.
sudo wpi php-settings --memory-limit 500 --upload-max-filesize 256
sudo wpi php-settings
# Kembalikan kedua limit ke profil resource otomatis.
sudo wpi php-settings --reset

# Upgrade/bootstrap mengaktifkan Redis secara otomatis; dapat dijalankan ulang.
sudo wpi optimize
sudo wpi redis-status
sudo wpi redis-flush example.net
sudo wpi redis-disable example.net
sudo wpi redis-enable example.net

# Jalankan di server lama. Password diminta di terminal, bukan sebagai argumen.
sudo wpi migrate --host 203.0.113.10 --user root
# Untuk SSH tujuan pada port selain 22:
sudo wpi migrate --host 203.0.113.10 --user ubuntu --port 2222

# Jalankan di server baru untuk melihat status migrasi dan SSL.
sudo wpi migration-status
```

Pada `set-primary`, `--old-domain alias` adalah pilihan standar. Gunakan
`--old-domain redirect` untuk menjadikan primary lama Redirect 301, atau
`--old-domain remove` untuk melepasnya setelah pergantian selesai. Contoh:
`sudo wpi set-primary example.com example.net --old-domain redirect`.

Perintah kompatibilitas `sudo wpi change-domain SITE DOMAIN_BARU` tetap dapat
dipakai untuk mengganti primary dan melepas primary lama, seperti versi
sebelumnya. Perintah ini berbeda dari menu Set as Primary yang mempertahankan
domain lama secara standar.

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

Pilih **menu 16 — Repair error situs** jika situs menampilkan HTTP 500 setelah
perubahan config. Repair berjalan ketika dipilih, tanpa monitor atau perbaikan
background. WPI memeriksa kembali frontend dan admin setelah tindakan; error
plugin/tema atau penyebab lain yang belum dapat dipulihkan dilaporkan sebagai
`unresolved`, tanpa menonaktifkan plugin/tema secara otomatis.

Pilih **menu 17** untuk mengatur memory dan ukuran upload melalui prompt.
Nilai `500`, `500M`, atau `500MiB` berarti **500 MiB**; `1G` berarti **1024 MiB**.
WPI mengatur PHP, konstanta memory WordPress, batas request web server, dan
`post_max_size` secara konsisten. Pilihan `auto` mengembalikan satu limit ke
profil otomatis; `--reset` mengembalikan kedua limit. Pengaturan manual disimpan
dan dipakai controller FPM. [Detail dan batas repair](docs/REPAIR-PHP.md).

## Migrasi ke server baru

Jalankan **menu 15** di server lama, masukkan IP server baru, username SSH, dan
password. WPI memindahkan seluruh situs yang dikelolanya. Server tujuan harus
berupa Ubuntu 22.04/24.04 dengan systemd yang bersih atau instalasi WPI kosong.
Gunakan akun root atau akun sudo yang memakai password login SSH yang sama.
Stack web/database mengikuti server sumber; PHP/FPM dihitung ulang dari
resource server tujuan. WPI memasang kebutuhan SSH yang belum tersedia pada
server sumber secara otomatis.

Migrasi mempertahankan akun WordPress, tema/plugin, media, database, Primary,
Alias, Redirect, dan akses phpMyAdmin. Password akun database di server baru
dibuat ulang dan diterapkan pada `wp-config.php` serta kredensial WPI. Password
SSH hanya dipakai selama sesi; tidak disimpan dalam file, argumen, atau
environment. Kunci host server baru dipin pada koneksi pertama dan perubahan
kunci pada koneksi berikutnya ditolak.

Setelah laporan **server tujuan siap**, ubah DNS A/AAAA semua domain, termasuk
www, Alias, Redirect, serta phpMyAdmin, ke IP server baru. Sertifikat sumber
yang masih valid beserta private key ditransfer melalui SSH terenkripsi untuk
mempertahankan HTTPS selama pergantian. Timer di server baru memeriksa rute
HTTP domain sekitar setiap lima menit, kemudian menerbitkan sertifikat Let's
Encrypt baru dan menyiapkan pembaruannya. Tidak perlu menjalankan konfigurasi
SSL secara manual; DNS dan akses port 80/443 harus mencapai server tujuan.

Server sumber tetap tersedia. WordPress memasuki maintenance saat snapshot
file/database dibuat, kemudian aktif kembali. Perubahan setelah snapshot
tidak ikut disalin: jadwalkan saat sepi dan hentikan penulisan post/order sampai
pergantian selesai bila situs sering menerima data baru. Migrasi bukan sinkronisasi
berkelanjutan. Backup sumber disimpan untuk pemulihan; migrasi yang terputus
dapat diulang ke server yang sama untuk melanjutkan snapshot tersebut.

Lihat [panduan migrasi dan auto-SSL](docs/MIGRATION.md) untuk status, batas cakupan,
dan penanganan DNS/SSL yang masih menunggu.

## Data dan backup

| Lokasi | Isi |
| --- | --- |
| `/usr/local/bin/wpi` | Perintah panel. |
| `/usr/local/lib/wpi` | Symlink menuju versi aplikasi aktif. |
| `/usr/local/lib/wpi-releases/` | Versi aplikasi yang dipasang. |
| `/var/lib/wpi/` | Metadata stack, situs, domain, dan operasi WPI. |
| `/var/lib/wpi/credentials/` | Kredensial pemulihan situs; hanya root. |
| `/var/lib/wpi/autotune/` | Status dan keputusan terakhir controller PHP/FPM; hanya root. |
| `/var/lib/wpi/redis/` | Kredensial privat dan backup config cache Redis. |
| `/etc/wpi/redis/` | Konfigurasi dan ACL instance Redis per situs. |
| `/run/wpi-redis-ID/redis.sock` | Unix socket cache situs; tanpa port TCP. |
| `/var/lib/wpi/ssh/known_hosts` | Pin kunci host SSH tujuan; hanya root. |
| `/var/lib/wpi/migrations-out/` | Journal migrasi pada server sumber; tanpa password SSH. |
| `/var/lib/wpi/migrations/` | Journal impor dan auto-SSL pada server tujuan; hanya root. |
| `/var/backups/wpi/` | Backup situs dan database. |
| `/var/backups/wpi/migrations/` | Snapshot migrasi lengkap dan privat pada server sumber. |
| `/var/log/wpi/` | Direktori yang disiapkan; output SQL/kredensial tidak direkam. |

Metadata dan backup hanya dapat dibaca root. Password database WordPress
disimpan pada `wp-config.php` situs agar WordPress dapat tersambung ke database.
Backup dapat memuat kredensial dan data pengguna; salin backup ke penyimpanan
terpisah yang aksesnya dibatasi. Log panel tidak mencatat password.

Menjalankan bootstrap lagi mengganti aplikasi secara atomik dan mempertahankan
data situs serta backup. Paket server dan konten WordPress dikelola terpisah
dari pembaruan aplikasi WPI. Untuk memperbarui instalasi versi sebelumnya,
tutup menu lama yang sedang menunggu pilihan dengan **0**, lalu jalankan ulang
perintah instalasi v1.6.1 di atas. Tunggu operasi yang sedang berjalan selesai
sebelum menutup panel. Pada stack WPI yang sudah
selesai disiapkan, bootstrap otomatis mengaktifkan Redis dan pengelolaan PHP/FPM tanpa
menginstal ulang WordPress atau meminta pengaturan tambahan.

Upgrade ke v1.6.1 mempertahankan domain dan data yang sudah terpasang. Domain
secondary versi sebelumnya tetap menjadi Redirect 301; domain tersebut tidak
otomatis diubah menjadi Alias. Add domain baru memakai Alias secara standar.

### Panel atau operasi WPI sedang berjalan

Versi hingga v1.2.0 mengunci seluruh sesi panel, termasuk saat menunggu pilihan.
Pesan `Panel WPI lain sedang berjalan. Tutup panel tersebut dahulu.` dapat
muncul ketika panel lama masih terbuka di terminal atau sesi SSH lain. Tutup
panel tersebut dengan **0** saat sudah kembali ke menu, lalu pasang v1.6.1.

Mulai v1.2.1, menu, prompt, `sudo wpi list`, dan `sudo wpi status` tidak menahan
kunci operasi. Beberapa panel dapat dibuka bersamaan; operasi yang mengubah
situs atau konfigurasi tetap dijalankan satu per satu. Jika operasi lain sedang
berjalan, tunggu hingga selesai dan ulangi pilihan Anda.

Pesan `Operasi WPI lain sedang berjalan` saat **Add domain** berarti kunci
sedang digunakan. Alur Add domain mengambil satu kunci sesudah
input selesai; diagnosis di server tetap diperlukan untuk membedakan operasi
aktif dari panel versi lama yang masih terbuka. Upgrade aplikasi tidak menutup
proses panel lama yang sudah berjalan.

Mulai v1.3.0, pemeriksaan berikut menampilkan status kunci beserta PID dan nama
proses yang dikonfirmasi melalui informasi kunci kernel. Perintah ini hanya
membaca status dan tetap dapat dijalankan ketika kunci sedang digunakan:

```bash
sudo wpi --version
sudo wpi lock-status
```

Jika pemegang tidak dapat ditemukan, status akan melaporkan informasi tidak
tersedia; hal tersebut tidak membuktikan kunci bebas. Pemeriksaan tambahan:

```bash
sudo lslocks -o PID,COMMAND,PATH | grep '/var/lib/wpi/operation.lock'
```

Gunakan hasil diagnosis untuk menemukan terminal atau sesi SSH pemegang kunci.
Keluar dengan **0** hanya ketika panel sudah kembali ke menu; tunggu instalasi,
backup, atau perubahan domain yang masih berjalan hingga selesai. WPI tidak
otomatis menutup proses atau melewati kunci tersebut.

File `/var/lib/wpi/operation.lock` tetap ada setelah panel ditutup; keberadaan
file tersebut adalah normal. **Jangan hapus file kunci.** Kunci dilepas otomatis
ketika operasi atau proses pemegangnya berakhir; menghapus file saat masih
dipakai dapat membuat dua operasi mengubah situs bersamaan.

## PHP dan FPM otomatis

WPI menyiapkan konfigurasi awal dari resource server dan menjalankan controller
setiap 15 detik. PHP-FPM juga otomatis membuat worker sesuai permintaan dalam
batas yang dihitung controller. Saat antrean meningkat dan resource masih
tersedia, kapasitas dapat bertambah; ketika penggunaan turun atau memori
menipis, kapasitas disesuaikan kembali. Perubahan diperiksa sebelum reload.

RAM dan CPU efektif dihitung kembali pada setiap siklus. Ketika Ubuntu melihat
resource tambahan setelah VPS di-upgrade, batas kapasitas ikut dihitung ulang.
Worker bertambah bertahap ketika ada kebutuhan, bukan langsung memenuhi batas
baru. Batas dihitung dari nilai yang lebih kecil antara delapan worker per CPU
efektif dan anggaran memori proses PHP; tidak ada pembatas tetap 128 worker.
Sebagai contoh perhitungan dengan proses ringan, 32 GiB/16 CPU dapat mencapai
128 worker, sedangkan 128 GiB/64 CPU dapat mencapai 512 worker. Pemakaian memori
worker yang lebih besar dapat menghasilkan batas lebih rendah.

Jalankan `sudo wpi status` atau pilih menu **12** untuk melihat hasil pemantauan.
Perintah status hanya membaca informasi, bukan mengubah konfigurasi. Detail
cara kerja dan batas kapasitas tersedia di [PHP/FPM otomatis](docs/AUTOTUNE.md).

Redis mengurangi query database melalui object cache yang bertahan antar-request.
Timer resource Redis menghitung ulang anggaran sekitar setiap 60 detik saat
Ubuntu melihat RAM baru. Anggaran ini dibagi ke situs yang memakai cache dan
dipertimbangkan oleh FPM agar keduanya tidak berebut seluruh memori. Redis
object cache tidak menyimpan seluruh halaman HTML; hasilnya bergantung pada
tema, plugin, query, dan rasio cache hit. [Detail Redis](docs/REDIS.md).

Otomatisasi ini bekerja dalam RAM dan CPU server yang tersedia. Ketika CPU
sudah penuh, menambah worker tidak mempercepat pemrosesan; controller menahan
penambahan kapasitas. WPI tidak membeli VPS atau menambah resource cloud.
Nilai memori PHP tidak dinaikkan tanpa batas hanya karena trafik bertambah.

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
