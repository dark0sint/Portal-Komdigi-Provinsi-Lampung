# Portal Komdigi Provinsi Lampung

Portal pemerintah daerah: profil, berita, layanan online (permohonan dan pengaduan bertiket), data terbuka,
JDIH, publikasi PPID, chatbot, dan panel admin dengan login + OTP. Flask + SQLite, tanpa layanan pihak ketiga.

## Menjalankan di server (Docker, HTTPS otomatis)

Prasyarat: VPS/server Linux, Docker + Docker Compose, domain yang A-record-nya mengarah ke IP server, port 80 dan 443 terbuka.

    cp .env.example .env        # isi DOMAIN, kontak, ADMIN_PASSWORD (min. 12 karakter)
    docker compose up -d --build

Buka `https://DOMAIN-ANDA`. Sertifikat Let's Encrypt diurus Caddy. Data tersimpan di volume `portal_data`.

Uji cepat tanpa domain: di `.env` isi `DOMAIN=:80` dan `COOKIE_SECURE=0`, lalu buka `http://IP-SERVER`. Jangan dipakai untuk produksi.

## Tanpa Docker (systemd + nginx)

    sudo useradd -r -m -d /opt/komdigi-lampung komdigi
    # salin proyek ke /opt/komdigi-lampung, lalu:
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    sudo mkdir -p /var/lib/komdigi-lampung && sudo chown komdigi /var/lib/komdigi-lampung
    sudo cp deploy/komdigi-lampung.service /etc/systemd/system/ && sudo systemctl enable --now komdigi-lampung
    # nginx: deploy/nginx.conf, lalu: sudo certbot --nginx -d domain-anda.go.id

Uji lokal: `pip install -r requirements.txt && python app.py` lalu buka http://127.0.0.1:8000

## Login admin pertama

Alamat: `/admin/masuk`. Bila `ADMIN_PASSWORD` tidak diisi, kata sandi acak ditulis ke `DATA_DIR/admin_pertama.txt`.
Login pertama langsung diarahkan untuk mengaktifkan OTP (masukkan kunci ke Google Authenticator/Aegis dan konfirmasi kode).
Setelah itu OTP wajib. Ganti kata sandi lewat menu admin.

Perintah bantu (`docker compose exec web flask ...`):

    flask reset-2fa admin          # bila ponsel petugas hilang
    flask set-password admin       # setel ulang kata sandi
    flask purge-tiket --bulan 24   # retensi: hapus tiket selesai lama

## Yang wajib Anda ganti sebelum publik

1. Isi semua konten contoh lewat admin: Halaman (profil, visi-misi, pimpinan), Berita, Data (semua bertanda "Data contoh"), Dokumen.
2. Kebijakan Privasi, Syarat, dan Aksesibilitas adalah templat awal; minta tinjauan biro hukum/PPID sebelum tayang.
3. Isi `SITE_DOMAIN`, kontak, dan alamat di `.env`. Domain `.go.id` didaftarkan lewat PANDI/registrar resmi dengan surat instansi.
4. Tautan dokumen JDIH dan Publikasi diisi sebagai URL PDF (portal ini tidak menerima unggah berkas, demi keamanan).
5. Cadangkan volume data secara berkala (`portal.db`, `secret.key`).

## Pemetaan fitur

| Kebutuhan | Implementasi |
|---|---|
| HTTPS/TLS | Caddy (otomatis) atau certbot; HSTS, cookie Secure/HttpOnly/SameSite |
| Otentikasi ganda | Login petugas: kata sandi + TOTP (RFC 6238), anti-replay, kunci akun 15 menit setelah 5 gagal, log audit |
| Privasi UU PDP | Halaman kebijakan, persetujuan eksplisit di setiap formulir, tanpa pelacak/font/skrip luar, retensi lewat `purge-tiket` |
| Penyaringan konten | Formulir pengaduan konten negatif; penanda otomatis untuk pesan banyak tautan/kata kunci berisiko; honeypot; batas laju |
| Aksesibilitas | Ukuran teks, kontras tinggi, huruf ramah disleksia, rata kiri/kanan, baca nyaring (TTS peramban), skip link, label ARIA |
| Responsif dan ringan | Tanpa framework JS, tanpa font luar, aset ber-versi dengan cache 30 hari |
| Transparansi | Profil, Publikasi (berkala, anggaran, kinerja), Data terbuka (grafik, CSV, JSON), JDIH dengan pencarian |
| Layanan online | Permohonan bertiket, pengaduan (bisa anonim), lacak tiket, kontak, chatbot berbasis FAQ (diatur di admin) |
| Domain resmi | Bilah "Situs resmi" + panduan verifikasi .go.id di setiap halaman |

## Catatan keamanan

- Aplikasi harus berada di belakang proxy (Caddy/nginx); jangan buka port 8000 langsung ke internet (header X-Forwarded-For dipercaya).
- Batas laju disimpan di memori per proses; untuk lalu lintas tinggi tambahkan rate limit di proxy/WAF.
- Kunci OTP ditampilkan sebagai teks (tanpa QR) agar tanpa dependensi tambahan.
- Ini bukan pengganti uji penetrasi. Untuk sistem elektronik pemerintah, jadwalkan audit keamanan oleh pihak berwenang.
