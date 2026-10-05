# Migrasi otomatis WPI v1.4.0

Migrasi dijalankan dari **server lama**. Pilih **15 — Migrasi otomatis ke server
baru**, lalu masukkan IP, username SSH, dan password server baru. Password tidak
tampil saat diketik. WPI menyiapkan server tujuan, menyalin seluruh situs WPI,
memeriksa impor, lalu menampilkan domain yang harus diarahkan ke IP baru.

## Server tujuan

Gunakan Ubuntu 22.04 atau 24.04 dengan systemd pada VPS bersih, atau WPI yang
belum memiliki situs/database tujuan. Target dengan situs atau database yang
bertentangan ditolak; WPI tidak menimpa aplikasi server lain. Identitas machine-id
juga diperiksa untuk menolak migrasi ke server sumber sendiri.

SSH harus menerima login password. Akun tujuan dapat berupa `root`, atau akun
yang memiliki akses sudo dengan password yang sama seperti login SSH. Login
berbasis MFA dan password sudo terpisah belum didukung. Port standar adalah
22; akses SSH serta HTTP/HTTPS harus diizinkan pada firewall penyedia VPS.

Anda tidak perlu memasang WPI, Nginx/Apache, database, PHP, atau Certbot secara
manual pada target. WPI mentransfer paket aplikasinya sendiri melalui SSH dan
memasang stack dengan pilihan yang sama seperti sumber. Konfigurasi PHP/FPM
menyesuaikan RAM/CPU tujuan; versi PHP berasal dari paket Ubuntu tujuan.
Migrasi dari Ubuntu 24.04/PHP 8.3 ke Ubuntu 22.04/PHP 8.1 ditolak agar PHP tidak
diturunkan. Gunakan Ubuntu 24.04 untuk sumber PHP 8.3. Frontend WordPress di
target diperiksa sebelum migrasi dianggap siap; kesalahan PHP/server menahan
penyelesaian impor.

## Jalankan dari server lama

```bash
sudo wpi
# Pilih 15, masukkan IP, username, dan password, lalu konfirmasi MIGRASI.
```

Perintah langsung memakai proses yang sama. Password tetap diminta lewat
terminal dan tidak tersedia sebagai opsi command line:

```bash
sudo wpi migrate --host 203.0.113.10 --user root
# Jika akun tujuan menggunakan sudo atau SSH memakai port lain:
sudo wpi migrate --host 203.0.113.10 --user ubuntu --port 2222
```

`203.0.113.10` adalah IP contoh; gunakan IP server tujuan Anda. Migrasi membawa
semua situs WPI sekaligus. Tidak perlu memilih setiap situs atau memasukkan
ulang akun administrator/domain.

## Data yang dipindahkan

| Data sumber | Hasil pada server baru |
| --- | --- |
| File WordPress, tema, plugin, uploads/media | Dipulihkan ke direktori situs dengan ID yang sama. |
| Tabel/database situs | Diimpor dan diperiksa sebelum situs dianggap siap. |
| Akun administrator WordPress | Username/password WordPress mengikuti database sumber. |
| Primary, Alias, dan Redirect 301 | Peran serta konfigurasi host dipertahankan; URL situs tidak diganti karena domain tetap sama. |
| Akun database situs | Akun dibuat pada target dengan password baru; `wp-config.php` dan kredensial WPI diperbarui. |
| phpMyAdmin WPI | Paket/UI, domain, Basic Auth, serta auto-SSL disiapkan kembali. |
| Sertifikat hostname dan private key yang valid | Ditransfer melalui SSH terenkripsi sebagai sertifikat sementara hingga ACME target selesai. |
| Konfigurasi PHP/FPM WPI | Dihitung ulang untuk resource server baru. |

Konfigurasi cron sistem di luar WordPress, mail server, aturan firewall/cloud,
API penyedia DNS, konfigurasi sistem kustom, dan aplikasi di luar WPI tidak
disalin. Data WP-Cron yang tersimpan dalam database WordPress ikut dalam
snapshot; pekerjaan sistem eksternal perlu dikelola di sumbernya.

## Snapshot dan pergantian DNS

Saat snapshot dibuat, WPI mengaktifkan maintenance WordPress, mengambil backup
database dan file, kemudian mengembalikan kondisi maintenance sumber semula. Situs
sumber tidak dihapus. Paket snapshot tetap tersimpan secara privat pada
`/var/backups/wpi/migrations/ID_MIGRASI/`, dan backup per situs juga dipertahankan.

Ini adalah pemindahan snapshot, bukan sinkronisasi dua server. Post, order,
upload, atau perubahan database sesudah snapshot tidak ikut dalam paket. Pilih
waktu sepi dan tahan penulisan sampai perpindahan DNS selesai bila situs harus
menjaga konsistensi data. Penulis eksternal atau cron yang melewati maintenance
WordPress juga perlu dihentikan selama snapshot.

Setelah WPI melaporkan server tujuan siap:

1. Ubah DNS **A** semua hostname ke IPv4 server baru.
2. Jika ada **AAAA**, arahkan ke IPv6 server baru yang dapat diakses, atau hapus
   AAAA lama bila target tidak menyediakan IPv6.
3. Sertakan hostname www, Alias, Redirect, dan phpMyAdmin pada perpindahan tersebut.
4. Periksa homepage, permalink, login, media, dan alur aplikasi di server baru.

WPI tidak mengubah record pada penyedia DNS. Selama propagasi, pengunjung dapat
masih mencapai server lama atau server baru; kedua salinan tidak tersinkron.
Pertahankan server sumber dan backup sampai pemeriksaan perpindahan selesai.

## HTTPS dan auto-SSL di server baru

WPI menyalin pasangan sertifikat/private key hanya untuk hostname yang
dimigrasikan. Target memeriksa hostname sertifikat, pasangan public/private
key, serta masa berlaku setidaknya satu hari. Jika valid, sertifikat itu
digunakan untuk HTTPS pada target sementara DNS berpindah. Sertifikat kedaluwarsa,
tidak cocok, atau belum tersedia tidak dipasang; hostname menunggu auto-SSL.
Akun ACME dan konfigurasi pembaruan sumber tidak disalin.

Timer `wpi-migration-ssl.timer` aktif otomatis pada target. Pemeriksaan pertama
sekitar satu menit setelah boot; pemeriksaan berikutnya sekitar setiap lima
menit, dengan jeda acak hingga 30 detik. Untuk setiap hostname, WPI membuat
file challenge acak di server baru lalu meminta URL HTTP domain tersebut.
Penerbitan ACME dimulai setelah respons domain cocok dengan file target.
Pemeriksaan ini mencegah pengajuan selama domain masih menuju sumber.

DNS belum siap menghasilkan status `waiting_dns` dan pemeriksaan berikutnya
sekitar lima menit kemudian. Jika Certbot atau aktivasi konfigurasi gagal,
status menjadi `retry` dengan jeda satu jam sebelum mencoba kembali. Jeda
dipersistenkan sebelum permintaan sehingga proses terputus tidak langsung
mengulang pengajuan sertifikat pada setiap siklus.

Setelah berhasil, status hostname menjadi `ready`, vhost memakai sertifikat
Let's Encrypt yang dikelola target, dan pembaruan Certbot berjalan dari target.
Penerbitan tetap bergantung pada DNS publik, port 80, serta validasi Let's
Encrypt dari internet. HTTP-01 hanya bekerja pada port 80.
[Dokumentasi HTTP-01 Let's Encrypt](https://letsencrypt.org/docs/challenge-types/).

Jalankan pemeriksaan berikut pada **server baru**:

```bash
sudo wpi migration-status
sudo systemctl status wpi-migration-ssl.timer --no-pager
sudo systemctl list-timers wpi-migration-ssl.timer --all
```

`status: ready` pada migrasi berarti file/database dan situs telah dipulihkan.
`ssl_pending` dapat masih berisi hostname yang menunggu DNS/ACME. Periksa
hostname tersebut sampai status SSL masing-masing menjadi `ready`. Tidak perlu
mengisi settings SSL atau PHP/FPM secara manual.

## Jika migrasi terputus

Jalankan migrasi lagi dari server lama ke IP, username, dan port yang sama.
WPI menggunakan journal untuk melanjutkan snapshot yang checksum-nya cocok,
bukan membuat salinan WordPress baru pada setiap percobaan. Impor pada target
juga mencatat kemajuan tiap situs. Kegagalan tidak menghapus server sumber.

Snapshot yang dilanjutkan tetap berisi keadaan pada waktu snapshot awal;
perubahan sumber setelahnya tidak masuk. Jangan memperlakukan percobaan ulang
sebagai sinkronisasi data terbaru. Journal sumber ada pada
`/var/lib/wpi/migrations-out/`, sedangkan journal impor/SSL target ada pada
`/var/lib/wpi/migrations/`. Keduanya dan backup hanya dapat dibaca root.

## Koneksi dan kredensial SSH

Password SSH diteruskan melalui anonymous pipe ke `sshpass`, lalu koneksi
OpenSSH memakai socket privat selama migrasi. Untuk akun non-root, password
yang sama diteruskan melalui stdin sudo. Password tidak masuk argv, environment,
file kredensial, atau pesan error. Setelah sesi selesai, master SSH ditutup dan
socket sementara dihapus.

Pada koneksi pertama, OpenSSH `accept-new` menerima dan menyimpan kunci host
secara otomatis dalam `/var/lib/wpi/ssh/known_hosts`. Ini memakai trust on first
use; bukan pemeriksaan fingerprint melalui penyedia VPS. Koneksi berikutnya
menolak kunci yang berubah. Jangan menonaktifkan pemeriksaan atau menghapus pin
tanpa memeriksa identitas server tujuan terlebih dahulu.
[OpenSSH host-key checking](https://manpages.ubuntu.com/manpages/jammy/man5/ssh_config.5.html),
[sshpass anonymous file descriptor](https://manpages.ubuntu.com/manpages/jammy/man1/sshpass.1.html).

Backup migrasi memuat data situs, kredensial, dan private key SSL. Aksesnya
dibatasi ke root; gunakan penyimpanan terpisah yang privat untuk salinan backup.
Password database target yang baru dapat dilihat dari **menu 13** pada target,
sedangkan akun WordPress mengikuti database yang dipindahkan.
