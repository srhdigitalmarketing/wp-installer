# Editor file plugin dan theme

Mulai WPI v1.7.0, jalankan `sudo wpi`, pilih **20 — Editor file plugin / theme**,
pilih situs, lalu **1 — Aktifkan** atau **2 — Nonaktifkan**. Pilihan **0** kembali
ke menu. Perubahan diterapkan otomatis tanpa restart VPS; PHP-FPM mengikuti
revalidasi config normalnya yang sekitar dua detik pada konfigurasi WPI.

Alternatif perintah:

```bash
sudo wpi file-editor example.com
sudo wpi file-editor example.com --enable
sudo wpi file-editor example.com --disable
```

Ganti `example.com` dengan domain atau ID situs WPI. Status hanya membaca config,
sehingga dapat diperiksa ketika operasi WPI lain berjalan. Perubahan memperoleh
kunci operasi dan hanya memengaruhi situs yang dipilih.

WPI mengatur konstanta PHP `DISALLOW_FILE_EDIT` dalam `wp-config.php`: `false`
mengizinkan editor dan `true` menonaktifkannya. Ini mengatur editor kode bawaan
di **Plugins → Plugin File Editor** dan **Appearance → Theme File Editor**.
Hak akses pengguna WordPress dan pembatasan plugin tetap menentukan siapa yang
dapat membuka editor. Editor konten, pengaturan tema, dan Site Editor blok
mempunyai pengaturan tersendiri.

WPI tidak mengubah `DISALLOW_FILE_MODS`. Jika konstanta tersebut aktif,
WordPress juga memblokir editor dan pemasangan/pembaruan plugin atau tema.
Permintaan mengaktifkan editor akan menjelaskan penghalang ini dan tidak mengubah
config. Ini menjaga kebijakan pemasangan/pembaruan yang telah dipilih pengguna.

Status menampilkan izin menurut config aktual dan pilihan yang disimpan WPI.
Situs baru memiliki editor nonaktif. Upgrade situs lama mempertahankan config
yang ada; pilihan menjadi terkelola setelah fitur ini digunakan. Pilihan yang
terkelola mengikuti situs saat Repair, restore backup lama, dan migrasi ke VPS
baru. Resume instalasi gagal juga mempertahankan pilihan yang sudah disimpan.

Sebelum mengubah config, WPI memeriksa sintaks PHP, menyimpan config asli di
`/var/lib/wpi/file-editor-backups/SITE_ID/` dengan izin privat, dan menerapkan perubahan
secara atomik. Jika validasi atau penyimpanan gagal, config dan metadata
dikembalikan. Config rusak perlu diperbaiki melalui menu **16 — Repair** terlebih
dahulu. Password, konstanta database, dan isi plugin/theme tidak diubah.

Perilaku WordPress dijelaskan dalam
[dokumentasi wp-config resmi](https://developer.wordpress.org/advanced-administration/wordpress/wp-config/#disable-the-plugin-and-theme-file-editor).
