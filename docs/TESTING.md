# Verifikasi

## Tes otomatis lokal

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q wpi
```

Tes Python memakai direktori sementara dan runner palsu. Tes ini tidak memasang
paket, mengubah database server, meminta sertifikat, atau menghapus berkas sistem.
Tujuannya memeriksa batas input, domain yang sudah dipakai, Alias dan Redirect,
rollback pergantian primary, serta pemisahan penghapusan phpMyAdmin dari database.

Untuk domain v1.3.0, periksa bahwa Add domain memakai Alias secara standar,
opsi Redirect mempertahankan 301, dan penambahan tanpa www beserta www berlaku
sebagai satu operasi: kegagalan salah satu hostname mengembalikan metadata,
konfigurasi web, dan pengelolaan SSL. Set as Primary hanya menerima Alias yang
sudah terpasang, memperbarui URL database, membuat backup, dan mempertahankan
primary lama sebagai Alias secara standar. Delete domain harus menolak primary
serta hanya melepas hostname lain; konten dan database tidak boleh dihapus.
Metadata secondary lama tetap berarti Redirect 301 sesudah upgrade.

Tes panel memastikan menu dan prompt tidak memegang kunci operasi, sementara
setiap perubahan memakai satu kunci selama seluruh rangkaian operasi. Pemilihan
situs sebelum operasi harus memakai ID yang stabil, dan metadata situs/domain
dibaca ulang serta divalidasi ketika operasi mulai dijalankan.

Pada Linux, tes subprocess menggunakan `flock` dari kernel dan direktori
sementara untuk membuktikan panel yang menunggu pilihan tidak menghalangi
perintah lain. Operasi kedua harus ditolak selama perubahan pertama berjalan;
perintah baca `list` dan `status` tetap tersedia. Periksa pelepasan kunci setelah
operasi selesai, gagal, atau proses berakhir. File kunci yang masih ada tanpa
pemegang aktif tidak boleh menghalangi operasi berikutnya. Tes Linux tersebut
dilewati pada Windows; tes dispatch/prompt tetap dijalankan di kedua sistem.

Untuk diagnosis v1.3.0, tes Linux memeriksa bahwa `lock-status` menemukan PID
pemegang kunci yang sebenarnya dari `/proc/locks` dan tetap tersedia ketika
operasi sedang berjalan. Parser harus mencocokkan device/inode yang tepat serta
mengabaikan proses yang hanya menunggu kunci. Data `/proc` yang tidak tersedia
atau berubah saat diperiksa harus menghasilkan status tidak diketahui, tanpa
menganggap metadata lama sebagai bukti pemegang aktif. Diagnosis tidak boleh
menghapus file, menghentikan proses, atau membolehkan dua perubahan bersamaan.

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

Untuk migrasi v1.4.0, tes source/target menggunakan paket backup buatan serta
runner SSH/server palsu. Periksa checksum paket dan manifest per berkas,
penolakan traversal ZIP/tar, symlink, hostname/ID/database tidak valid, dan
konflik dengan situs/database tujuan. Pemindahan mempertahankan akun WordPress,
media, peran Primary/Alias/Redirect, serta pemisahan penghapusan UI phpMyAdmin
dari database. Password database baru harus diterapkan pada `wp-config.php`
dan kredensial tujuan. Journal harus melanjutkan snapshot yang sama sesudah
kegagalan tanpa menimpa situs yang tidak dimiliki migrasi tersebut.

Tes transport memeriksa bahwa password diteruskan melalui anonymous FD ke
`sshpass`, descriptor ditutup setelah login, dan password tidak masuk argv,
environment, file, atau pesan error. Kunci host dipin pada koneksi awal dan
kunci yang berubah ditolak. Socket master privat harus dibersihkan pada akhir
context atau kegagalan; slave tidak boleh memakai koneksi TCP cadangan apabila
master hilang. Pada POSIX, jalankan tes framing stdin menggunakan shell nyata
dan executable sudo fixture tanpa elevasi untuk memastikan satu baris password
dikonsumsi dan script dikutip dengan benar. Tes POSIX tersebut dilewati pada
Windows; pembentukan argumen dan framing tetap diperiksa di kedua sistem.

Tes auto-SSL migrasi harus membuktikan domain yang masih menuju sumber tidak
memicu Certbot. Hanya respons HTTP yang cocok dengan token acak server tujuan
boleh memulai penerbitan. DNS yang belum siap menunggu siklus lima menit, kegagalan
ACME memakai jeda satu jam, dan jeda disimpan sebelum pengajuan agar proses
terputus tidak terus mengajukan sertifikat. Domain yang sudah dihapus atau
dipindahkan ke situs lain tidak boleh diaktifkan ulang oleh journal lama.

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
berjalan. Alias harus membuka halaman serta permalink WordPress dengan HTTP
200 pada hostname Alias. Periksa perubahan URL termasuk option terserialisasi
setelah Set as Primary, akses domain lama sebagai Alias, Redirect 301 dengan
path/query yang sama, serta keberadaan database sesudah Delete domain dan
penghapusan UI phpMyAdmin. Penambahan pasangan www juga harus diperiksa pada
kedua hostname.
Bootstrap memasang bundle ZIP yang dibangun dari source dan menjalankan launcher
terpasang. Setelah seluruh alur, pemasangan ulang aplikasi wajib mempertahankan
seluruh metadata situs dan kredensial yang sama.
Untuk PHP/FPM otomatis, periksa konfigurasi pool dan INI hasil pemasangan,
layanan pemantauan lokal, timer aktif, serta informasi pada `sudo wpi status`.
Pemasangan ulang pada stack yang sudah dikelola harus mengaktifkan controller
tanpa mengganti database, situs, atau kredensial.

Untuk regresi v1.2.1, buka panel terpasang dan biarkan menunggu pilihan saat
menjalankan pemeriksaan status serta pemasangan ulang aplikasi. Kedua alur harus
berhasil karena panel yang diam tidak memegang kunci operasi. Pengujian harus
tetap membuktikan perubahan dari proses lain tidak bisa berjalan bersamaan
dengan operasi yang sedang menulis. Panel versi lama hingga v1.2.0 tetap perlu
ditutup dari menunya sebelum upgrade, karena proses lama memakai perilaku kunci
yang lama sampai berakhir.

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

## Integrasi migrasi dua server sekali pakai

`tests/migration-integration.sh` menyediakan fixture dua container Ubuntu 24.04
dengan systemd, OpenSSH, sudo, Nginx, MariaDB, PHP-FPM, dan WordPress nyata.
Skrip ini memasang layanan, membuat akun/password sementara, mengubah resolver,
dan menghapus container serta image fixture setelah selesai. Jalankan hanya
pada runner Docker sekali pakai dengan `CI=true` dan `WPI_DISPOSABLE_VM=1`:

```bash
env CI=true WPI_DISPOSABLE_VM=1 bash tests/migration-integration.sh
```

Fixture memeriksa login SSH password sebagai root dan akun non-root, upload
terenkripsi, sudo dengan password, bootstrap target, serta transfer database
dan file. Akun administrator, isi post, hash media, tabel, peran Alias/Redirect,
dan konfigurasi PHP/FPM target diperiksa melalui layanan yang benar-benar
berjalan. Situs sumber dan backup harus tetap tersedia dan maintenance telah
dinonaktifkan setelah snapshot.

Sebelum simulasi DNS berpindah, target memakai pasangan sertifikat sumber yang
ditransfer; HTTPS Primary/Alias dan Redirect 301 diperiksa. Controller terpasang
harus melihat token challenge menuju sumber lalu menghasilkan `waiting_dns`
tanpa mengajukan sertifikat. Setelah resolver fixture diarahkan ke target,
service/timer systemd yang terpasang memanggil controller untuk memeriksa token
HTTP nyata, mengganti sertifikat, dan mengaktifkan konfigurasi TLS baru.

Tes ini memakai hostname contoh, IP privat, dan sertifikat self-signed sementara.
Pemeriksaan kelayakan DNS publik dan executable Certbot diganti fixture hanya
di container CI; interval timer juga dipercepat pada fixture. Jalur SSH, sudo,
database, web server, routing challenge, serta controller tetap nyata. Hasil
uji tidak membuktikan penerbitan Let's Encrypt publik, propagasi DNS internet,
migrasi Apache/MySQL dua server, atau kapasitas beban produksi. Laporkan hasil
run CI yang selesai sebagai bukti; keberadaan skrip ini sendiri bukan bukti
bahwa pengujiannya telah lulus.

## Penerimaan di Ubuntu dengan domain nyata

Sebelum pemakaian produksi, uji pada Ubuntu bersih dengan domain/subdomain yang
A/AAAA-nya menunjuk VM tersebut. Jika domain memiliki AAAA, IPv6 juga harus
mencapai VM. Buka port 80 dan 443 di firewall cloud. Simpan backup sebelum
perubahan domain.

| Alur | Bukti yang diperiksa |
| --- | --- |
| Instalasi Nginx dan Apache | Halaman WordPress dapat dibuka melalui HTTPS; login administrator berhasil; `nginx -t` atau `apachectl configtest` berhasil. |
| SSL otomatis | Sertifikat valid untuk hostname, rantai dipercaya browser, HTTP redirect ke HTTPS, timer pembaruan Certbot aktif. |
| Add domain — Alias | Homepage dan permalink menghasilkan HTTP 200 pada hostname Alias, tanpa pengalihan ke primary. Tautan WordPress yang dihasilkan memakai hostname Alias; tautan tersimpan dalam konten tetap diperiksa terpisah. |
| Add domain — Redirect | `/artikel?x=1` menghasilkan 301 ke primary dengan path dan query yang sama, melalui HTTP maupun HTTPS. |
| Pasangan www | Hostname tanpa www dan www terpasang sesuai peran yang dipilih; kegagalan salah satu hostname membatalkan penambahan pasangan tanpa mengubah domain lama. |
| Set as Primary | Alias yang dipilih menjadi satu-satunya primary; `home`, `siteurl`, permalink, media, post, dan option terserialisasi memakai primary baru. Backup dibuat dan primary lama tetap membuka situs sebagai Alias. Opsi Redirect/remove pada perintah langsung harus mengikuti pilihan tersebut. |
| Delete domain | Alias atau Redirect yang dipilih dilepas dari vhost/SSL; file, seluruh tabel/database, dan hostname lain tetap tersedia. Primary ditolak sampai Alias lain dijadikan primary. |
| Upgrade domain lama | Metadata secondary yang sudah terpasang tetap menghasilkan Redirect 301; tidak ada perubahan peran otomatis menjadi Alias. |
| Rollback | Ganggu reload server web saat uji Set as Primary; setelah kegagalan, domain, konfigurasi, dan data WordPress lama tetap dapat dipakai. |
| phpMyAdmin | UI HTTPS menuntut Basic Auth sebelum halaman login database; kredensial panel tidak tampil pada daftar proses. |
| Hapus phpMyAdmin | Hostname/UI tidak dapat dibuka; WordPress dan seluruh tabel/database masih tersedia. |
| Backup/restore | Cadangkan, ubah sebuah post uji, restore, lalu periksa isi dan URL situs kembali benar. |
| PHP/FPM otomatis | Resource server terbaca, konfigurasi PHP/FPM valid, pemantauan hanya dapat diakses lokal, timer aktif sesudah reboot, dan status menampilkan keputusan terakhir. |
| Lonjakan request PHP | Gunakan beban PHP terkontrol pada VM uji; ketika antrean/worker sibuk meningkat dan masih ada resource, batas worker bertambah. Setelah beban turun, kapasitas kembali disesuaikan tanpa reload setiap sampel. |
| Batas resource | Pada VM/container dengan batas RAM/CPU, target kapasitas mengikuti batas yang dihitung; tekanan memori menurunkan kapasitas jika reload aman dan CPU penuh menahan kenaikan. Headroom resource diperiksa sebelum penerapan konfigurasi. WordPress tetap dapat diakses setelah reload selesai. |
| Upgrade resource VPS | Tambahkan RAM/CPU pada VM uji, reboot jika penyedia memerlukannya, lalu periksa resource efektif yang terlihat di Ubuntu dan status controller. Batas kapasitas dihitung ulang, dapat melewati 128 bila perhitungan mengizinkan, dan bertambah bertahap ketika beban membutuhkan. Penambahan resource sendiri tidak langsung menjalankan seluruh kapasitas worker. |
| Pemulihan FPM | Simulasikan kegagalan validasi/reload di VM uji; konfigurasi sebelumnya dipulihkan, kegagalan tercatat tanpa kredensial, dan controller mencoba lagi pada siklus berikutnya. |
| Migrasi server | Dari Ubuntu sumber ke Ubuntu tujuan bersih, masukkan hanya IP, username, password tersembunyi, lalu konfirmasi. Periksa semua situs, akun administrator, file/media, tabel, peran domain, konfigurasi web, serta password database tujuan yang baru. Sumber dan backup tetap tersedia. |
| HTTPS saat migrasi | Pasangan sertifikat sumber yang masih valid dipasang melalui SSH terenkripsi. HTTPS pada IP tujuan melalui hostname domain tetap dapat dibuka; hostname/key/expiry diperiksa dan sertifikat tidak valid tidak dipasang. |
| Auto-SSL sesudah DNS | A/AAAA seluruh hostname diarahkan ke target; timer aktif, token HTTP berasal dari target, sertifikat baru diterbitkan dan dipercaya browser, lalu konfigurasi serta timer pembaruan Certbot tersedia pada target. Periksa hostname Primary/Alias/Redirect/phpMyAdmin. |
| Menunggu DNS / kegagalan SSL | Saat DNS masih menuju sumber, status `waiting_dns` tanpa pengajuan ACME. Jika Certbot gagal, status `retry` dan jeda satu jam. Ulangi pemeriksaan setelah perbaikan DNS/firewall tanpa mengisi konfigurasi SSL manual. |
| Migrasi terputus | Putuskan transfer di VM uji lalu jalankan ulang dengan IP/username/port yang sama. Snapshot/checksum dan journal dilanjutkan; situs sumber tidak hilang dan situs target lain tidak tertimpa. Perubahan sesudah snapshot tetap tidak tersalin. |
| Akses SSH migrasi | Login root dan non-root sudo diperiksa. Password tidak tampil pada daftar proses/log/error, pin host tersimpan privat, dan perubahan kunci host ditolak tanpa fallback pemeriksaan longgar. |

Catat versi Ubuntu, stack, output pemeriksaan konfigurasi, status HTTP, dan hasil
tes. Jangan memasukkan password, dump database, atau data pelanggan ke laporan.

Bedakan hasil tes kebijakan dengan bukti beban nyata. Tes unit dengan metrik
buatan membuktikan keputusan controller; integrasi membuktikan pemasangan dan
operasi layanan. Keduanya belum membuktikan kapasitas trafik produksi atau
hasil pada kombinasi tema/plugin tertentu. Catat tingkat request, RSS worker,
antrean FPM, CPU, memori, perubahan batas worker, dan error HTTP dalam uji beban.
