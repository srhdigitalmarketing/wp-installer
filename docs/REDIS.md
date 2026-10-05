# Redis dan optimasi WordPress

Mulai WPI v1.6.0, instalasi WordPress menyiapkan persistent object cache
otomatis. WPI memasang Redis dari paket Ubuntu, ekstensi PHP Redis, plugin
[Redis Object Cache](https://wordpress.org/plugins/redis-cache/) versi 3.0.0
dari WordPress.org, serta drop-in `wp-content/object-cache.php`. Plugin yang
dipasang diperiksa checksum-nya sebelum cache diaktifkan. Pengguna tidak perlu
mengisi port, password Redis, prefix, atau batas memori.

Object cache menyimpan hasil pembacaan data WordPress supaya proses PHP
berikutnya dapat menggunakan data yang sudah tersedia di Redis. Hasilnya
dapat mengurangi query SQL yang berulang. HTML halaman tetap dibuat oleh
WordPress; fitur ini tidak menjanjikan full-page cache atau penyimpanan
halaman pribadi pengguna. Besarnya penghematan tergantung tema, plugin,
jenis request, dan cache hit yang sebenarnya.

## Perintah dan menu

Gunakan menu **18 — Optimalkan Redis & PHP** untuk memasang atau memperbarui
konfigurasi performa pada situs WPI yang sudah ada. Menu **19 — Status / kelola cache Redis**
menampilkan status layanan, cache situs, dan anggaran memori. Instalasi baru
serta upgrade bootstrap mengaktifkan cache pada situs aktif yang memenuhi
syarat.

```bash
sudo wpi optimize
sudo wpi redis-status
sudo wpi list
```

Perintah untuk satu situs memakai ID situs dari `wpi list`:

```bash
sudo wpi redis-enable ID_SITUS
sudo wpi redis-flush ID_SITUS
sudo wpi redis-disable ID_SITUS
```

`redis-enable` tanpa ID memproses situs aktif yang memenuhi syarat.
`redis-flush` menghapus cache situs yang dipilih; WordPress mengisinya lagi
ketika data dibaca. Database SQL, konten, akun, dan media tetap tersedia.
`redis-disable` mematikan object cache WPI untuk situs tersebut. Pilihan
disable disimpan dan dipertahankan oleh `optimize`, upgrade, serta migrasi.
Aktifkan lagi secara sengaja dengan `redis-enable ID_SITUS`.

WPI memeriksa kepemilikan plugin dan drop-in sebelum perubahan. Situs yang
memakai object cache lain tidak ditimpa otomatis. Status menampilkan alasan
situs dilewati atau aktivasi gagal agar pengguna dapat menyelesaikan konflik
plugin terlebih dahulu.

## Isolasi dan koneksi

Setiap situs memakai instance Redis sendiri melalui socket Unix
`/run/wpi-redis-ID_SITUS/redis.sock` dan layanan systemd
`wpi-redis@ID_SITUS.service`. Konfigurasi memakai `port 0`, sehingga Redis
WPI tidak membuka koneksi TCP. Setiap situs mendapat username/password dan
prefix `wpi:ID_SITUS:` sendiri.

Instance terpisah menjaga flush situs A agar tidak menghapus cache situs B.
Plugin memakai perintah `FLUSHDB` yang didukung; ACL situs menolak
`FLUSHALL` dan perintah administrasi. WPI tidak memakai opsi eksperimental
`WP_REDIS_SELECTIVE_FLUSH` atau `WP_REDIS_GRACEFUL`. Perilaku flush seluruh
database Redis dijelaskan dalam [dokumentasi Redis FLUSHDB](https://redis.io/docs/latest/commands/flushdb/).

Password tersimpan di direktori privat WPI dengan izin root, dan konfigurasi
WordPress menerima kredensial yang diperlukan untuk koneksi. Password tidak
ditampilkan pada status, dimasukkan ke argumen proses, atau dicetak dalam
diagnosis. File konfigurasi dan ACL dikelola di `/etc/wpi/redis/`.

## Anggaran RAM otomatis

WPI menghitung satu anggaran Redis untuk seluruh server dari RAM efektif
yang terlihat oleh Ubuntu, termasuk batas memori container. Batas data
cache sekitar 5% RAM efektif, dengan minimum 16 MiB untuk server dan
4 MiB per instance. Anggaran dibagi di antara instance situs. Cadangan
RAM Redis sebesar dua kali anggaran data cache ditambah 10 MiB per instance
memperhitungkan overhead proses; konfigurasi ditolak jika cadangan ini
melebihi 20% RAM efektif.
Jika pemakaian RSS instance yang masih berjalan lebih besar, cadangan memakai
nilai RSS tersebut hingga memori benar-benar turun. Ini mencegah FPM menganggap
memori allocator lama sudah bebas sesudah RAM VPS diperkecil.
PHP-FPM dan batas PHP memperhitungkan cadangan Redis yang sama, di samping
cadangan OS/database serta OPcache, sehingga
penambahan situs tidak menganggap seluruh RAM tersedia lagi untuk setiap
situs.

Kebijakan `allkeys-lfu` membuang objek yang jarang dipakai ketika anggaran
cache terisi. WordPress dapat membacanya lagi dari database dan membentuk
cache baru. Redis menjelaskan mekanisme ini pada
[dokumentasi key eviction](https://redis.io/docs/latest/develop/reference/eviction/).
Cache bukan salinan cadangan database.

Timer `wpi-performance.timer` memeriksa anggaran secara berkala. Jika RAM
VPS ditambah dan sudah terlihat oleh Ubuntu, batas memori dihitung ulang.
Jika penyedia memerlukan reboot untuk menerapkan RAM baru, perhitungan
berubah setelah reboot tersebut. Controller PHP-FPM tetap menyesuaikan
kapasitas worker terhadap antrean, CPU, serta memori aktual; penambahan
cache tidak menjamin jumlah pengunjung tertentu.

```bash
sudo systemctl status wpi-performance.timer --no-pager
sudo wpi redis-status
sudo wpi autotune-status
```

## Jika Redis gagal

Redis Object Cache menampilkan kegagalan koneksi ketika instance tidak
tersedia; situs dapat menghasilkan HTTP 500. Diagnosis tidak menyembunyikan
kegagalan plugin. `redis-status`, `repair --check`, menu yang menunggu input,
dan timer anggaran tidak menjalankan repair atau mengaktifkan kembali
layanan yang sengaja dihentikan.

Gunakan diagnosis lalu repair eksplisit:

```bash
sudo wpi redis-status
sudo wpi repair ID_SITUS --check
sudo wpi repair ID_SITUS
```

Repair memulai kembali instance Redis WPI untuk situs yang masih mengaktifkan
cache, kemudian memeriksa konfigurasi dan HTTP. Jika perlu memulihkan akses
sambil menyelidiki layanan, `redis-disable ID_SITUS` melepas cache yang
dikelola WPI. Plugin atau drop-in lain tetap memerlukan penanganan pemiliknya.

## Backup, domain, dan migrasi

Perubahan domain dan restore membersihkan cache situs yang bersangkutan
agar URL atau data lama tidak terus digunakan. Cache situs lain dipertahankan.
Restore menerapkan kembali endpoint Redis serta kredensial server saat ini
sebelum WordPress dijalankan, termasuk ketika snapshot berasal dari server
sebelumnya.

[Migrasi ke server baru](MIGRATION.md) membuat instance Redis tujuan dan
password baru secara otomatis. Isi cache Redis sumber tidak dipindahkan;
cache tujuan terbentuk dari WordPress di server baru. Database, konten, dan
media mengikuti alur migrasi yang sama. Cache sumber tetap berada pada
server sumber, dan situs yang memilih disable tetap memilih disable di
tujuan.

## Cakupan pengujian

Fixture Ubuntu pada `tests/integration.sh` mencakup nginx dan Apache:
cache antar proses WP-CLI dan PHP-FPM, perbandingan query SQL untuk 20
option non-autoload, dua situs WordPress dengan flush terisolasi, socket
Unix tanpa listener TCP, serta kegagalan layanan dan repair eksplisit.
Alur restore, perubahan domain, disable, optimize, dan upgrade juga memakai
plugin Redis yang sebenarnya.

Fixture `tests/migration-integration.sh` membandingkan hash password sumber
dan tujuan, memastikan entri sumber tidak disalin, serta memeriksa cache
tujuan sesudah restore. Pengujian ini tidak mengukur kapasitas produksi
100–200 ribu pengunjung per hari; gunakan beban aplikasi nyata untuk
menilai throughput dan query SQL situs Anda. Hasil lulus CI harus dilihat
terpisah dari pemeriksaan sintaks lokal.
