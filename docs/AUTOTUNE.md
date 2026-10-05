# PHP dan PHP-FPM otomatis

Mulai v1.1.0, WPI menyiapkan PHP dan PHP-FPM dari resource server dan
menjalankan controller setiap 15 detik. Pengguna cukup memasang WPI dan
WordPress; tidak perlu mengisi `memory_limit`, `pm.max_children`, atau nilai
spare worker seperti pada panel web.

Instalasi baru otomatis mengaktifkan fitur ini saat setup stack selesai.
Pada server yang sudah memakai WPI v1.0.0, v1.1.0, v1.2.0, v1.2.1, atau v1.2.2,
jalankan ulang bootstrap v1.4.0 sesuai [README](../README.md#instalasi-di-ubuntu).
Stack yang sudah selesai disiapkan akan diaktifkan otomatis, sementara situs,
database, domain, dan
backup tetap digunakan.

## Cara kerja

Ada dua lapisan penyesuaian. PHP-FPM membuat dan menghentikan proses worker
melalui process manager `dynamic`, sesuai jumlah request yang harus dikerjakan.
Controller WPI memantau kondisi server dan menyesuaikan `pm.max_children`,
`pm.start_servers`, `pm.min_spare_servers`, dan `pm.max_spare_servers` secara
berkala. Seluruh situs WPI berbagi anggaran pool yang sama, sehingga limit
tidak dikalikan secara terpisah untuk setiap domain.

Pada konfigurasi awal, WPI membaca RAM dan CPU yang tersedia. Jika berjalan
di container dengan batas resource Linux, batas tersebut ikut diperhitungkan.
Sebagian memori disisakan untuk database, sistem operasi, web server, dan
layanan lain. Anggaran worker dimulai dari sekitar 50% RAM efektif. Batas
kapasitas juga mengikuti CPU efektif, dihitung dari delapan worker per CPU.
Mulai v1.2.0, tidak ada batas tetap 128 worker: WPI memakai nilai yang lebih
kecil antara batas CPU dan anggaran memori, dengan minimum satu worker.
Batas memori dapat menghasilkan angka yang lebih rendah lagi.

RAM dan CPU efektif dibaca ulang pada setiap siklus controller. Jika resource
VPS ditambah dan sudah terlihat oleh Ubuntu, batas kapasitas dihitung ulang
tanpa mengisi konfigurasi manual. Jika penyedia VPS memerlukan reboot untuk
menerapkan upgrade, resource tersebut dapat digunakan setelah reboot itu.
Peningkatan batas tidak langsung menjalankan seluruh worker: kapasitas pool
bertambah bertahap hanya ketika pemantauan menunjukkan kebutuhan berkelanjutan
dan resource masih cukup.

Batas tersebut adalah target pool aktif. Controller menyisakan headroom
memori untuk penerapan konfigurasi dan dapat menunda perubahan ketika resource
belum cukup. Penggunaan memori tetap bergantung pada request dan plugin yang
sedang berjalan.

Saat berjalan, controller memeriksa penggunaan memori proses PHP-FPM, jumlah
worker aktif, antrean socket PHP di kernel, memori yang masih tersedia, dan
penggunaan CPU. Antrean kernel dibaca langsung karena nilai antrean pada JSON
status FPM tidak selalu menggambarkan backlog socket Unix.
Jika antrean kernel tidak dapat dibaca, status menampilkan nilai tidak
tersedia. Worker aktif yang penuh tetap dapat menunjukkan kebutuhan kenaikan,
tetapi penurunan karena sepi memerlukan antrean kosong yang benar-benar terukur.
Antrean yang meningkat dapat menaikkan batas worker jika anggaran memori dan
kapasitas CPU masih memungkinkan. Kondisi sepi atau tekanan memori dapat
menurunkan kapasitas. Pemakaian worker memakai sampel RSS, dengan perkiraan
minimal 96 MiB per proses agar pengukuran kecil tidak membuka terlalu banyak
worker. Perkiraan memori memakai persentil ke-90 sampel RSS ditambah headroom
25%, sehingga pemakaian proses yang lebih berat ikut menurunkan batas aman.

Contoh berikut adalah perhitungan kebijakan dengan anggaran awal 50% RAM,
bukan bukti kapasitas trafik produksi. Tekanan resource atau headroom untuk
reload dapat menunda pencapaian angka tersebut. Batas 512 worker tidak menjamin
512 respons WordPress serentak: koneksi database, konfigurasi web server, dan
waktu pemrosesan aplikasi juga dapat membatasi throughput.

| Resource efektif | Perkiraan memori per worker | Batas CPU | Batas kapasitas |
| --- | ---: | ---: | ---: |
| 32 GiB RAM, 16 CPU | 96 MiB (minimum) | 128 | 128 worker |
| 128 GiB RAM, 64 CPU | 96 MiB (minimum) | 512 | 512 worker |
| 128 GiB RAM, 64 CPU | RSS 200 MiB + 25% = 250 MiB | 512 | 262 worker |

Seluruh situs berbagi kapasitas ini. PHP-FPM tetap memakai `pm.max_children`
sebagai batas pool yang dihitung otomatis; yang dihapus adalah pembatas tetap
128 di kebijakan WPI, bukan perlindungan terhadap kehabisan resource.

Kenaikan memerlukan tiga sampel sibuk berturut-turut, sekitar 45 detik, dan
diterapkan secara bertahap. Controller memberi jeda minimal 60 detik antar
perubahan biasa. Penurunan karena sepi menunggu 20 sampel, sekitar lima menit.
Tekanan CPU, memori, swap, atau stall resource dapat menurunkan kapasitas
lebih cepat pada kejadian pertama; penurunan berulang diberi jeda 180 detik.
Controller juga memeriksa apakah memori cukup untuk penerapan konfigurasi
baru. Jika memori sudah terlalu sedikit, perubahan ditunda dengan status
`memory-exhausted`
agar reload tidak menambah tekanan memori. Jika telemetri tidak tersedia,
controller menahan kenaikan dan menjaga batas konservatif. Jeda dan beberapa
sampel mencegah reload pada setiap lonjakan singkat.

Perubahan konfigurasi diterapkan secara atomik, diperiksa dengan validator
PHP-FPM, lalu diaktifkan melalui graceful reload. Worker yang sedang bekerja
diberi waktu sampai batas request 180 detik; process control memakai batas
185 detik. Selama reload masih menunggu generasi FPM baru, controller menunda
perubahan berikutnya. Jika validasi atau reload gagal, konfigurasi sebelumnya
dipulihkan. Controller berikutnya tetap berjalan.
Status pemantauan FPM dibatasi pada akses lokal dan tidak ditambahkan sebagai
halaman publik pada domain WordPress.

Pada peningkatan pertama dari v1.0.0, konfigurasi FPM lama belum memakai batas
process control baru. Reload pertama dapat mengganggu request yang sedang
berjalan; WPI tidak menjanjikan setiap reload tanpa gangguan.

| Komponen | Lokasi |
| --- | --- |
| Controller berkala | `wpi-autotune.timer` dan `wpi-autotune.service` |
| Overlay pool WPI | `/etc/php/<versi>/fpm/pool.d/zz-wpi-autotune.conf` |
| Pemantauan FPM internal | Socket FastCGI `/run/wpi-autotune/status.sock`, hanya root |
| Status controller | `/var/lib/wpi/autotune/state.json`, hanya root |

Overlay menggunakan pool `www` Ubuntu dan mempertahankan socket PHP situs
yang sudah ada. WPI memantau angka agregat, tanpa menyimpan isi request atau
kredensial WordPress/database.

## PHP tidak memakai nilai tanpa batas

`memory_limit` membatasi memori sebuah skrip PHP; nilai tersebut bukan
alokasi RAM yang langsung digunakan setiap worker. Jumlah worker aman
memerlukan ukuran pemakaian memori proses yang nyata serta anggaran server.
WPI memilih profil batas memori per request berdasarkan resource untuk
konfigurasi awal; trafik yang meningkat tidak otomatis membuka limit memori
atau waktu eksekusi tanpa batas.

Profil awal mengikuti RAM efektif, termasuk limit container:

| RAM efektif | `memory_limit` | Upload | POST | OPcache |
| --- | ---: | ---: | ---: | ---: |
| Di bawah 1 GiB | 128 MiB | 32 MiB | 40 MiB | 64 MiB |
| 1 GiB hingga di bawah 4 GiB | 256 MiB | 64 MiB | 80 MiB | 128 MiB |
| 4 GiB atau lebih | 384 MiB | 128 MiB | 144 MiB | 256 MiB |

Waktu eksekusi/input PHP dipasang 120 detik, `max_input_vars` 5000,
`session.gc_maxlifetime` 1440 detik, `session.gc_divisor` 1000, dan
`allow_url_fopen` aktif. FPM membatasi request yang berjalan sampai 180 detik
serta mendaur ulang worker setelah 500 request. Profil diperiksa kembali
ketika RAM atau jumlah CPU efektif berubah, lalu diaktifkan setelah validasi
dan reload berhasil.

Batas upload, ukuran request, waktu eksekusi, dan input dipasang secara
konsisten oleh WPI. Kenaikan trafik tidak berarti ukuran upload harus
ditingkatkan. File besar dan skrip berat adalah kebutuhan berbeda dari
jumlah request yang masuk. Web server mengizinkan body request sampai
320 MiB; batas PHP yang lebih kecil mengikuti profil di atas.

## Melihat hasil

```bash
sudo wpi status
```

Menu **12 — Status dan diagnosis** menampilkan informasi yang sama. Perintah
ini membaca status terakhir controller dan layanan, tanpa mengubah konfigurasi
PHP/FPM. Gunakan status untuk melihat kapasitas yang dipilih, resource yang
terdeteksi, serta alasan keputusan atau kegagalan terakhir.

Jika anggaran memori bahkan lebih kecil daripada perkiraan satu worker,
controller mempertahankan minimum satu worker dan melaporkan
`memory-budget-exhausted`; kondisi ini tidak dianggap kapasitas sehat.

Pengaturan otomatis tetap berjalan setelah pengguna menutup panel terminal
dan setelah server dinyalakan ulang. Operasi WordPress lainnya tidak
memerlukan penyetelan FPM tambahan.

## Batas kapasitas

Kapasitas tetap dibatasi RAM dan CPU VPS. Ketika CPU sudah penuh, membuat
lebih banyak worker dapat memperpanjang antrean dan menambah konsumsi memori;
controller menahan kenaikan dalam kondisi itu. WordPress dengan query lambat,
plugin berat, atau banyak request yang tidak menggunakan cache juga tetap
mempunyai batas throughput.

WPI tidak otomatis membeli VPS, menambah RAM/CPU dari penyedia cloud,
mendistribusikan situs ke beberapa server, atau menjanjikan semua trafik
dapat ditangani. Pengelolaan worker otomatis membantu memakai kapasitas server
yang tersedia. Pembuktian lonjakan trafik nyata memerlukan uji beban di VM
uji dan pencatatan metrik sesuai [panduan verifikasi](TESTING.md).

## Referensi

- [Konfigurasi PHP-FPM](https://www.php.net/manual/en/install.fpm.configuration.php).
- [Konfigurasi inti PHP](https://www.php.net/manual/en/ini.core.php).
- [Kontrol resource cgroup v2 Linux](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
