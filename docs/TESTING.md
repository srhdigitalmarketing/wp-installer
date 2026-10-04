# Verifikasi

## Tes otomatis lokal

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q wpi
```

Tes Python memakai direktori sementara dan runner palsu. Tes ini tidak memasang
paket, mengubah database server, meminta sertifikat, atau menghapus berkas sistem.
Tujuannya memeriksa batas input, domain yang sudah dipakai, redirect secondary,
rollback pergantian primary, serta pemisahan penghapusan phpMyAdmin dari database.

Untuk controller PHP/FPM, tes dengan data resource dan status FPM buatan dapat
memeriksa keputusan tanpa membebani VPS. Cakupan yang diperlukan: pembatasan
RAM/CPU container, perhitungan kapasitas awal, kenaikan ketika antrean bertambah,
penahanan kenaikan saat CPU penuh, penurunan saat memori menipis, jeda antar
reload, serta pemulihan konfigurasi setelah validasi/reload gagal. Data status
yang tidak tersedia harus menghasilkan keputusan konservatif.

Untuk v1.2.0, data uji juga harus membuktikan kapasitas dapat melewati 128 worker
ketika resource mencukupi: 128 GiB/64 CPU dengan proses ringan mempunyai batas
512 worker, sedangkan RSS 200 MiB ditambah headroom 25% membatasi kapasitas
menjadi 262 worker. Simulasikan perubahan dari 32 GiB/16 CPU ke 128 GiB/64 CPU
untuk memastikan resource dibaca ulang dan kapasitas bertambah sesuai kebutuhan
berkelanjutan. Penurunan batas resource harus tetap mengurangi target kapasitas.
Angka tersebut adalah tes kebijakan dengan resource buatan, bukan pengujian
512 proses produksi sungguhan.

## Integrasi Ubuntu sekali pakai

`tests/integration.sh` hanya boleh dijalankan di VM CI sekali pakai. Skrip ini
memasang dan mengubah layanan sistem serta menghapus stack/data bawaan VM,
sehingga menolak berjalan tanpa `CI=true`, `WPI_DISPOSABLE_VM=1`, dan akses root.
Gunakan VM terpisah untuk Nginx dan Apache.

```bash
sudo env CI=true WPI_DISPOSABLE_VM=1 bash tests/integration.sh nginx
sudo env CI=true WPI_DISPOSABLE_VM=1 bash tests/integration.sh apache
```

Tes integrasi menguji server web, PHP-FPM, database, dan WordPress yang benar-benar
berjalan, lalu memeriksa perubahan URL termasuk data option terserialisasi,
redirect secondary 301, dan akses database sesudah UI phpMyAdmin dihapus.
Bootstrap memasang bundle ZIP yang dibangun dari source dan menjalankan launcher
terpasang. Setelah seluruh alur, pemasangan ulang aplikasi wajib mempertahankan
seluruh metadata situs dan kredensial yang sama.
Untuk PHP/FPM otomatis, periksa konfigurasi pool dan INI hasil pemasangan,
layanan pemantauan lokal, timer aktif, serta informasi pada `sudo wpi status`.
Pemasangan ulang pada stack yang sudah dikelola harus mengaktifkan controller
tanpa mengganti database, situs, atau kredensial.

Pengujian antrean memakai request HTTP yang benar-benar mengisi pool PHP,
status FastCGI lokal, dan backlog socket Unix dari kernel. Keputusan kenaikan
kapasitas memakai fixture RAM/CPU agar tetap deterministik di runner CI;
konfigurasi pool yang baru kemudian benar-benar divalidasi, di-reload, dan
diperiksa jumlah workernya. Hasil ini membuktikan jalur antrean hingga
penerapan FPM, bukan ukuran kapasitas produksi atau pengukuran RAM/CPU beban
nyata pada VPS pengguna.
Domain uji diarahkan ke loopback dengan `curl --resolve`. Konfigurasi HTTPS
diperiksa menggunakan sertifikat self-signed sementara. DNS publik dan penerbitan
sertifikat ACME sengaja tidak dipanggil; hasil tes integrasi ini tidak membuktikan
sertifikat Let's Encrypt dapat diterbitkan untuk domain produksi.

## Penerimaan di Ubuntu dengan domain nyata

Sebelum pemakaian produksi, uji pada Ubuntu bersih dengan domain/subdomain yang
A/AAAA-nya menunjuk VM tersebut. Jika domain memiliki AAAA, IPv6 juga harus
mencapai VM. Buka port 80 dan 443 di firewall cloud. Simpan backup sebelum
perubahan domain.

| Alur | Bukti yang diperiksa |
| --- | --- |
| Instalasi Nginx dan Apache | Halaman WordPress dapat dibuka melalui HTTPS; login administrator berhasil; `nginx -t` atau `apachectl configtest` berhasil. |
| SSL otomatis | Sertifikat valid untuk hostname, rantai dipercaya browser, HTTP redirect ke HTTPS, timer pembaruan Certbot aktif. |
| Secondary | `/artikel?x=1` menghasilkan 301 ke primary dengan path dan query yang sama, melalui HTTP maupun HTTPS. |
| Ganti primary | `home`, `siteurl`, permalink, media, post, dan option terserialisasi memakai primary baru; hostname lama tidak lagi terdaftar. |
| Rollback | Ganggu reload server web saat uji ganti primary; setelah kegagalan, domain, konfigurasi, dan data WordPress lama tetap dapat dipakai. |
| phpMyAdmin | UI HTTPS menuntut Basic Auth sebelum halaman login database; kredensial panel tidak tampil pada daftar proses. |
| Hapus phpMyAdmin | Hostname/UI tidak dapat dibuka; WordPress dan seluruh tabel/database masih tersedia. |
| Backup/restore | Cadangkan, ubah sebuah post uji, restore, lalu periksa isi dan URL situs kembali benar. |
| PHP/FPM otomatis | Resource server terbaca, konfigurasi PHP/FPM valid, pemantauan hanya dapat diakses lokal, timer aktif sesudah reboot, dan status menampilkan keputusan terakhir. |
| Lonjakan request PHP | Gunakan beban PHP terkontrol pada VM uji; ketika antrean/worker sibuk meningkat dan masih ada resource, batas worker bertambah. Setelah beban turun, kapasitas kembali disesuaikan tanpa reload setiap sampel. |
| Batas resource | Pada VM/container dengan batas RAM/CPU, target kapasitas mengikuti batas yang dihitung; tekanan memori menurunkan kapasitas jika reload aman dan CPU penuh menahan kenaikan. Headroom resource diperiksa sebelum penerapan konfigurasi. WordPress tetap dapat diakses setelah reload selesai. |
| Upgrade resource VPS | Tambahkan RAM/CPU pada VM uji, reboot jika penyedia memerlukannya, lalu periksa resource efektif yang terlihat di Ubuntu dan status controller. Batas kapasitas dihitung ulang, dapat melewati 128 bila perhitungan mengizinkan, dan bertambah bertahap ketika beban membutuhkan. Penambahan resource sendiri tidak langsung menjalankan seluruh kapasitas worker. |
| Pemulihan FPM | Simulasikan kegagalan validasi/reload di VM uji; konfigurasi sebelumnya dipulihkan, kegagalan tercatat tanpa kredensial, dan controller mencoba lagi pada siklus berikutnya. |

Catat versi Ubuntu, stack, output pemeriksaan konfigurasi, status HTTP, dan hasil
tes. Jangan memasukkan password, dump database, atau data pelanggan ke laporan.

Bedakan hasil tes kebijakan dengan bukti beban nyata. Tes unit dengan metrik
buatan membuktikan keputusan controller; integrasi membuktikan pemasangan dan
operasi layanan. Keduanya belum membuktikan kapasitas trafik produksi atau
hasil pada kombinasi tema/plugin tertentu. Catat tingkat request, RSS worker,
antrean FPM, CPU, memori, perubahan batas worker, dan error HTTP dalam uji beban.
