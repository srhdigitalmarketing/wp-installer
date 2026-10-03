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

Catat versi Ubuntu, stack, output pemeriksaan konfigurasi, status HTTP, dan hasil
tes. Jangan memasukkan password, dump database, atau data pelanggan ke laporan.
