# Repair error situs dan pengaturan PHP

Jalankan `sudo wpi`, pilih **16 — Repair error situs**, lalu pilih situs WPI.
WPI mendiagnosis penyebab umum, menjalankan perbaikan yang aman untuk situs
tersebut, dan memeriksa kembali respons frontend serta admin. Repair hanya
berjalan atas perintah pengguna melalui menu atau CLI; tidak ada pemantauan
atau perbaikan error background.

HTTP **500** pada URL HTTPS adalah respons error dari server/aplikasi. Itu
belum membuktikan sertifikat SSL bermasalah. Perubahan `wp-config.php` dapat
memunculkan error sintaks atau runtime PHP; setelah isi file dikembalikan,
runtime FPM/OPcache yang masih berjalan juga perlu diperiksa. WPI memeriksa
lapisan ini secara terpisah dan tidak menerbitkan ulang SSL secara membabi buta.

## Diagnosis dan repair

```bash
sudo wpi list

# SITE bisa ID situs atau domain yang tercantum pada daftar.
# Hanya menampilkan hasil pemeriksaan; tidak menerapkan perbaikan.
sudo wpi repair example.com --check

# Diagnosis, perbaikan penyebab umum, kemudian pemeriksaan ulang.
sudo wpi repair example.com
```

Pemeriksaan meliputi sintaks `wp-config.php`, apakah WordPress dapat membaca
instalasi/database, status layanan database dan PHP-FPM, validitas konfigurasi
web/FPM, serta respons frontend dan `/wp-admin/`. Probe HTTP memakai domain
primary yang diarahkan ke localhost, sehingga server ini dapat diperiksa tanpa
mengikuti perubahan DNS publik. Probe HTTPS tetap memverifikasi sertifikat.
WPI hanya menampilkan status dan kode HTTP, bukan isi halaman atau log yang
dapat mengandung kredensial/data pelanggan.

PHP lint memakai `-n -l`: file diperiksa sebagai sintaks tanpa mengeksekusi
kode PHP di dalamnya. Error runtime memerlukan pemeriksaan WordPress/HTTP
terpisah. [Dokumentasi PHP CLI](https://www.php.net/manual/en/features.commandline.options.php).

Saat ada masalah, WPI menyimpan `wp-config.php` asli di direktori privat
`/var/lib/wpi/config-repairs/ID/`. Jika backup config gagal dibuat, file aktif
tidak diubah. Perbaikan umum yang dapat dilakukan:

- Memulihkan config dengan error sintaks dari snapshot config tervalidasi.
  Config yang berubah dan tidak lagi dapat memuat WordPress juga dapat
  dipulihkan dari snapshot tersebut.
- Bila snapshot belum tersedia, membaca **hanya `wp-config.php`** dari backup
  situs WPI yang lengkap dan cocok checksum.
- Bila config rusak/hilang dan tidak ada backup config yang valid, membuat
  ulang config dari kredensial WPI dan prefix tabel WordPress yang terlebih
  dahulu terbukti ada dan unik di database. WPI tidak membuat instalasi atau
  database pengganti. Jika identitas/kredensial/prefix belum dapat dibuktikan,
  pemulihan config berhenti dan situs dilaporkan belum pulih.
- Menyesuaikan konstanta memory WordPress yang sudah ada dengan limit PHP
  terkelola, memperbaiki akses berkas inti yang terbukti tidak dapat dibaca,
  memulai layanan terkelola yang berhenti, meregenerasi virtual host WPI,
  dan me-restart PHP-FPM setelah konfigurasi FPM lolos validasi untuk
  membersihkan OPcache lama.

Snapshot config berada di `/var/lib/wpi/config-snapshots/ID/`, bersifat privat
dan disimpan setelah PHP lint serta pembacaan instalasi WordPress berhasil.
Pemulihan config menggunakan kredensial database **server saat ini**, termasuk
password baru pada server tujuan migrasi. Primary, Alias, Redirect, metadata
migrasi, database, posting, akun, plugin, tema, dan media tidak dikembalikan
ke keadaan backup oleh repair.

Jika config harus dibuat ulang, konstanta/kode custom dari config rusak dapat
tidak masuk ke config baru. Config asli tetap tersimpan untuk ditinjau, dan
laporan menandai `custom_config_reset`. Salt autentikasi baru membuat sesi login
lama berakhir; laporan menandai `session_reset`, sehingga pengguna perlu login
WordPress kembali. Pemulihan dari snapshot yang valid mempertahankan salt.

Hasil repair membedakan `healthy`, `resolved`, dan `unresolved`. Situs baru
dinyatakan pulih jika pemeriksaan konfigurasi/layanan, pembacaan WordPress,
serta respons frontend dan admin semuanya berhasil. Repair tidak menjamin
semua HTTP 500 dapat diperbaiki: kegagalan plugin/tema, kode custom lain,
kerusakan database, atau konfigurasi layanan eksternal dapat membutuhkan
pemeriksaan log lokal. WPI tidak menonaktifkan plugin/tema atau menghapus data
secara otomatis. Mengedit backup privat harus dilakukan dengan hati-hati;
config berisi password database dan salt autentikasi.

## PHP memory limit dan max upload size

Pilih **17 — PHP memory limit / max upload size** untuk mengisi nilai melalui
prompt. Alternatif CLI:

```bash
# Lihat pengaturan tersimpan dan limit efektif.
sudo wpi php-settings

# 500 berarti 500 MiB; 256 berarti 256 MiB.
sudo wpi php-settings --memory-limit 500 --upload-max-filesize 256

# Bentuk satuan juga didukung.
sudo wpi php-settings --memory-limit 1G --upload-max-filesize 256M

# Kembalikan satu limit ke profil otomatis.
sudo wpi php-settings --memory-limit auto

# Kembalikan kedua limit ke profil otomatis.
sudo wpi php-settings --reset
```

WPI memakai satu pool PHP-FPM bersama. Pengaturan ini berlaku **server-wide**
untuk semua situs WPI dan phpMyAdmin, bukan kuota memory yang terpisah untuk
masing-masing domain. Limit memory berlaku per proses/request PHP; total RAM
server tetap dibagi dengan worker lain, database, OPcache, dan sistem operasi.
Menaikkan memory per worker dapat mengurangi jumlah worker yang dapat berjalan
bersamaan dalam anggaran RAM yang sama.

Nilai angka tanpa satuan pada **CLI WPI** diartikan sebagai MiB. Berbeda dengan
nilai langsung di PHP yang dapat memakai byte, WPI menulis format PHP seperti
`500M`, sehingga pengguna tidak perlu menambahkan kode `wp-config.php` manual.
[Format ukuran PHP](https://www.php.net/manual/en/faq.using.php#faq.using.shorthandbytes).

WPI menerapkan `memory_limit`, `upload_max_filesize`, `post_max_size`, konstanta
`WP_MEMORY_LIMIT`/`WP_MAX_MEMORY_LIMIT`, dan batas body request Nginx/Apache
secara konsisten. Batas POST diberi ruang tambahan untuk multipart; memory
harus lebih besar daripada batas POST. Batas memory PHP-FPM juga menjadi
ceiling sehingga kode WordPress tidak dapat menaikkannya melampaui pengaturan
terkelola. Limit terlalu besar untuk anggaran RAM saat ini ditolak sebelum
diterapkan. `0`, nilai negatif, dan unlimited tidak didukung.

Konfigurasi manual disimpan di state WPI dan tetap digunakan saat controller
FPM menyesuaikan jumlah worker atau membaca resource baru. Mode `auto`/reset
memakai profil otomatis berdasarkan resource efektif server. Pengaturan
berkas dan layanan diterapkan setelah validasi; config lama disimpan dan
dikembalikan bila aktivasi gagal. Jika `wp-config.php` sudah memiliki error
sintaks, jalankan repair terlebih dahulu sebelum mengubah limit.

Ukuran upload efektif dapat juga dibatasi CDN, proxy, atau layanan lain di
depan VPS; mengubah WPI tidak mengubah batas pada layanan tersebut. Fitur ini
tidak menjamin kapasitas traffic tertentu. Pengujian unit/integrasi WPI tidak
berarti VPS produksi pengguna sudah diperbaiki atau dikonfigurasi.
