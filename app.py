"""
Portal Komdigi Provinsi Lampung
Flask + SQLite. Jalankan: `python app.py` (pengembangan) atau `gunicorn app:app` (produksi).
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import struct
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlencode

import click
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "portal.db"

SITE = {
    "nama": os.environ.get("SITE_NAME", "Komdigi Provinsi Lampung"),
    "tagline": os.environ.get("SITE_TAGLINE", "Portal informasi dan layanan digital Pemerintah Provinsi Lampung"),
    "domain": os.environ.get("SITE_DOMAIN", "domain-anda.go.id"),
    "email": os.environ.get("CONTACT_EMAIL", "info@domain-anda.go.id"),
    "telp": os.environ.get("CONTACT_PHONE", "(0721) 000 000"),
    "alamat": os.environ.get("CONTACT_ADDRESS", "Jl. Contoh No. 1, Bandar Lampung, Lampung"),
    "jam": os.environ.get("CONTACT_HOURS", "Senin sampai Jumat, 08.00 sampai 16.00 WIB"),
}

# --------------------------------------------------------------------------- app

def load_secret() -> str:
    env = os.environ.get("SECRET_KEY", "")
    if len(env) >= 32:
        return env
    p = DATA_DIR / "secret.key"
    try:
        with open(p, "x") as f:
            f.write(secrets.token_hex(32))
        os.chmod(p, 0o600)
    except FileExistsError:
        pass
    for _ in range(40):
        v = p.read_text().strip()
        if len(v) >= 32:
            return v
        time.sleep(0.05)
    raise RuntimeError("Gagal membaca secret.key")


app = Flask(__name__)
app.config.update(
    SECRET_KEY=load_secret(),
    MAX_CONTENT_LENGTH=1_000_000,
    SESSION_COOKIE_NAME="sid",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=30),
    JSON_AS_ASCII=False,
    TEMPLATES_AUTO_RELOAD=False,
)
app.url_map.strict_slashes = False
if os.environ.get("TRUST_PROXY", "1") == "1":
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

ADMIN_IDLE_SECONDS = 30 * 60
MAX_FAILED_LOGIN = 5
LOCK_SECONDS = 15 * 60

# ---------------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
  totp_secret TEXT, totp_enabled INTEGER NOT NULL DEFAULT 0, last_step INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS berita(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, judul TEXT NOT NULL, kategori TEXT NOT NULL,
  terbit TEXT NOT NULL, ringkasan TEXT NOT NULL, isi TEXT NOT NULL,
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS layanan(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, nama TEXT NOT NULL, ringkasan TEXT NOT NULL,
  syarat TEXT NOT NULL, estimasi TEXT NOT NULL,
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS jdih(
  id INTEGER PRIMARY KEY, jenis TEXT NOT NULL, nomor TEXT NOT NULL, tahun INTEGER NOT NULL,
  judul TEXT NOT NULL, status TEXT NOT NULL, url TEXT NOT NULL DEFAULT '',
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS dataset(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, judul TEXT NOT NULL, kategori TEXT NOT NULL,
  sumber TEXT NOT NULL, tahun INTEGER NOT NULL, deskripsi TEXT NOT NULL, csv TEXT NOT NULL,
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS dokumen(
  id INTEGER PRIMARY KEY, kelompok TEXT NOT NULL, judul TEXT NOT NULL, tahun INTEGER NOT NULL,
  url TEXT NOT NULL DEFAULT '', dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS halaman(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, judul TEXT NOT NULL, isi TEXT NOT NULL,
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS faq(
  id INTEGER PRIMARY KEY, kata_kunci TEXT NOT NULL, jawaban TEXT NOT NULL, tautan TEXT NOT NULL DEFAULT '',
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS tiket(
  id INTEGER PRIMARY KEY, kode TEXT UNIQUE NOT NULL, tipe TEXT NOT NULL, jenis TEXT NOT NULL,
  nama TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '', telp TEXT NOT NULL DEFAULT '',
  instansi TEXT NOT NULL DEFAULT '', tautan TEXT NOT NULL DEFAULT '', isi TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Diterima', tanggapan TEXT NOT NULL DEFAULT '',
  ditandai INTEGER NOT NULL DEFAULT 0,
  dibuat TEXT NOT NULL DEFAULT (datetime('now','+7 hours')),
  diperbarui TEXT NOT NULL DEFAULT (datetime('now','+7 hours')));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, waktu TEXT NOT NULL DEFAULT (datetime('now','+7 hours')),
  pengguna TEXT NOT NULL, aksi TEXT NOT NULL, ip TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_tiket_status ON tiket(status, dibuat);
CREATE INDEX IF NOT EXISTS ix_jdih_tahun ON jdih(tahun);
"""


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def now_wib() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=7)


# ---------------------------------------------------------------------- utilitas

BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]


@app.template_filter("tgl")
def tgl(v, jam=False):
    try:
        d = datetime.fromisoformat(str(v)[:19])
    except ValueError:
        return v
    s = f"{d.day} {BULAN[d.month - 1]} {d.year}"
    return f"{s}, {d:%H.%M} WIB" if jam else s


@app.template_filter("angka")
def angka(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    if f == int(f):
        return f"{int(f):,}".replace(",", ".")
    return f"{f:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("teks")
def render_text(text):
    """Teks polos -> HTML aman. '## ' = subjudul, '- ' = butir daftar."""
    out = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r", "").strip()):
        lines = [l.rstrip() for l in block.split("\n") if l.strip()]
        if lines and lines[0].startswith("## "):
            out.append(f"<h3>{escape(lines[0][3:])}</h3>")
            lines = lines[1:]
        if not lines:
            continue
        if all(l.startswith("- ") for l in lines):
            out.append("<ul>" + "".join(f"<li>{escape(l[2:])}</li>" for l in lines) + "</ul>")
        else:
            out.append("<p>" + "<br>".join(str(escape(l)) for l in lines) + "</p>")
    return Markup("".join(out))


@app.template_filter("butir")
def butir(text):
    return [l[2:].strip() if l.startswith("- ") else l.strip()
            for l in (text or "").replace("\r", "").split("\n") if l.strip()]


def slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:70].strip("-")


def unique_slug(table: str, base: str, exclude_id=None) -> str:
    base = slugify(base) or "item"
    s, i = base, 2
    while db().execute(f"SELECT 1 FROM {table} WHERE slug=? AND id IS NOT ?", (s, exclude_id)).fetchone():
        s, i = f"{base}-{i}", i + 1
    return s


def like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def paginate(total: int, per: int):
    pages = max(1, -(-total // per))
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    return min(max(page, 1), pages), pages


def audit(aksi: str, pengguna: str | None = None):
    u = pengguna or (g.user["username"] if getattr(g, "user", None) else "-")
    db().execute("INSERT INTO audit(pengguna,aksi,ip) VALUES(?,?,?)", (u, aksi[:300], request.remote_addr or "-"))
    db().commit()


_hits: dict = {}


def rate_limited(key, limit: int, window: int) -> bool:
    now = time.time()
    if len(_hits) > 20000:
        _hits.clear()
    q = [t for t in _hits.get(key, []) if now - t < window]
    if len(q) >= limit:
        _hits[key] = q
        return True
    q.append(now)
    _hits[key] = q
    return False


# ------------------------------------------------------------------------- TOTP

def totp_code(secret_b32: str, step: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 10**6:06d}"


def totp_verify(secret_b32: str, code: str, last_step: int = 0):
    """Kembalikan step yang cocok (int) atau None. Menolak pemakaian ulang kode."""
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    cur = int(time.time() // 30)
    for st in (cur - 1, cur, cur + 1):
        if st > last_step and hmac.compare_digest(totp_code(secret_b32, st), code):
            return st
    return None


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


# ----------------------------------------------------------------- keamanan HTTP

def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


@app.before_request
def csrf_protect():
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Sesi formulir kedaluwarsa. Muat ulang halaman lalu coba lagi.")


@app.after_request
def security_headers(resp):
    h = resp.headers
    h["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'; frame-ancestors 'none'")
    h["X-Content-Type-Options"] = "nosniff"
    h["X-Frame-Options"] = "DENY"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
    h["Cross-Origin-Opener-Policy"] = "same-origin"
    h["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.is_secure:
        h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/admin") or request.path.startswith("/lacak"):
        h["Cache-Control"] = "no-store"
    h.pop("Server", None)
    return resp


@app.context_processor
def inject():
    def asset(name: str) -> str:
        p = BASE_DIR / "static" / name
        v = int(p.stat().st_mtime) if p.exists() else 0
        return url_for("static", filename=name, v=v)

    def page_url(n: int) -> str:
        a = request.args.to_dict()
        a["page"] = n
        return request.path + "?" + urlencode(a)

    return dict(site=SITE, csrf_token=csrf_token, asset=asset, page_url=page_url,
                tahun_ini=now_wib().year, ENT_MENU=list(ENT.items()))


@app.errorhandler(HTTPException)
def http_error(e):
    info = {400: "Permintaan tidak dapat diproses", 403: "Akses ditolak",
            404: "Halaman tidak ditemukan", 405: "Metode tidak diizinkan",
            413: "Data terlalu besar", 429: "Terlalu banyak permintaan"}
    return render_template("error.html", kode=e.code, judul=info.get(e.code, e.name),
                           pesan=e.description if e.code in (400, 429) else "", page_title=str(e.code)), e.code


# ------------------------------------------------------------- data contoh (seed)

DEFAULT_PRIVASI = """Kebijakan ini menjelaskan bagaimana portal ini mengumpulkan, memakai, menyimpan, dan melindungi data pribadi Anda sesuai Undang-Undang Nomor 27 Tahun 2022 tentang Pelindungan Data Pribadi.

## Data yang kami kumpulkan
- Data yang Anda isi sendiri di formulir: nama, alamat email, nomor telepon, instansi, dan isi pesan, permohonan, atau laporan.
- Tautan atau keterangan yang Anda lampirkan pada laporan konten negatif.
- Data teknis yang perlu untuk keamanan: alamat IP dan waktu akses pada log server, serta satu cookie sesi untuk melindungi formulir dari pemalsuan permintaan.

Kami tidak meminta NIK, tidak memasang pelacak iklan, tidak memakai layanan analitik pihak ketiga, dan tidak memuat huruf atau skrip dari server luar.

## Tujuan dan dasar pemrosesan
- Menindaklanjuti permohonan layanan, pengaduan, dan pesan Anda.
- Menghubungi Anda bila diperlukan informasi tambahan.
- Menjaga keamanan dan mencegah penyalahgunaan portal.
- Dasar pemrosesan adalah persetujuan Anda saat mengirim formulir dan pelaksanaan tugas pelayanan publik instansi.

## Penyimpanan dan penghapusan
Data tiket disimpan selama diperlukan untuk tindak lanjut dan kewajiban arsip, lalu dihapus atau dianonimkan sesuai jadwal retensi instansi. Anda dapat meminta penghapusan lebih awal sepanjang tidak bertentangan dengan kewajiban hukum.

## Hak Anda
- Mengakses dan memperoleh salinan data pribadi Anda.
- Memperbaiki data yang tidak akurat.
- Meminta penghapusan atau pembatasan pemrosesan.
- Menarik persetujuan dan mengajukan keberatan.

Ajukan hak tersebut lewat halaman Kontak atau email resmi yang tercantum di bagian bawah situs.

## Pengungkapan kepada pihak lain
Data tidak dijual dan tidak dibagikan untuk kepentingan komersial. Data hanya dapat dibagikan kepada unit atau instansi berwenang yang perlu menindaklanjuti laporan Anda, atau bila diwajibkan oleh peraturan perundang-undangan.

## Keamanan
Seluruh akses memakai HTTPS. Kata sandi petugas disimpan dalam bentuk hash, akun petugas dilindungi verifikasi dua langkah (OTP), dan setiap tindakan petugas dicatat dalam log audit.

## Anak-anak
Portal ini tidak ditujukan untuk mengumpulkan data anak tanpa persetujuan orang tua atau wali. Laporan tentang konten yang membahayakan anak dapat dikirim lewat halaman Pengaduan.

## Perubahan kebijakan
Perubahan akan diumumkan pada halaman ini beserta tanggal pembaruan."""

DEFAULT_SYARAT = """Dengan memakai portal ini, Anda menyetujui syarat berikut.

## Penggunaan portal
- Gunakan portal untuk keperluan yang sah dan berikan data yang benar.
- Jangan mengirim konten yang melanggar hukum, menyerang pihak lain, atau berisi program berbahaya.
- Jangan mencoba mengakses area petugas atau mengganggu kerja sistem.

## Layanan dan pengaduan
Permohonan dan pengaduan diproses sesuai standar layanan yang tertera. Kode tiket bersifat rahasia; siapa pun yang memegang kode dan email terkait dapat melihat status tiket.

## Konten dan hak cipta
Informasi di portal ini bersifat publik dan boleh dikutip dengan menyebut sumber. Data terbuka mengikuti keterangan lisensi pada masing-masing set data.

## Batasan tanggung jawab
Kami berupaya menjaga informasi tetap akurat dan mutakhir, namun tidak menjamin bebas dari kekeliruan. Untuk keputusan hukum, rujuk dokumen resmi pada JDIH.

## Perubahan
Syarat dapat diperbarui sewaktu-waktu. Penggunaan portal setelah perubahan berarti Anda menerima syarat yang baru."""

DEFAULT_AKSES = """Kami berupaya agar portal ini dapat dipakai oleh semua orang, termasuk penyandang disabilitas, pengguna ponsel dengan jaringan terbatas, dan pengguna teknologi bantu.

## Fitur yang tersedia
- Ubah ukuran teks dari 100% sampai 150%.
- Mode kontras tinggi.
- Huruf ramah disleksia dengan jarak antarhuruf lebih longgar.
- Perataan teks rata kiri atau rata kanan.
- Baca nyaring: browser membacakan isi halaman dalam Bahasa Indonesia bila perangkat memiliki suara Indonesia.
- Tautan lompat ke konten utama, urutan fokus keyboard yang jelas, dan label untuk pembaca layar.
- Tampilan responsif untuk ponsel dan desktop, tanpa pustaka besar dan tanpa font luar sehingga cepat dimuat.

Pengaturan Anda disimpan di perangkat Anda sendiri dan tidak dikirim ke server.

## Laporkan kendala
Bila Anda menemui hambatan akses, sampaikan lewat halaman Kontak dengan pilihan kategori Aksesibilitas. Kami menargetkan pedoman WCAG 2.1 tingkat AA dan akan memperbaikinya secara berkala."""


def seed(conn: sqlite3.Connection):
    if conn.execute("SELECT COUNT(*) FROM halaman").fetchone()[0]:
        return
    H = [
        ("profil", "Profil instansi",
         "Ganti teks ini melalui panel admin (menu Halaman).\n\nSelayang pandang\nKomdigi Provinsi Lampung menyelenggarakan urusan komunikasi, informatika, statistik, dan persandian di lingkungan Pemerintah Provinsi Lampung. Uraikan dasar hukum pembentukan, tugas pokok, dan fungsi instansi di sini."),
        ("visi-misi", "Visi dan misi",
         "## Visi\nTulis visi instansi di sini.\n\n## Misi\n- Tulis misi pertama.\n- Tulis misi kedua.\n- Tulis misi ketiga."),
        ("struktur-organisasi", "Struktur organisasi",
         "Unggah bagan resmi atau tuliskan susunan organisasi di sini. Contoh susunan:\n\n- Kepala Dinas\n- Sekretariat\n- Bidang Informasi dan Komunikasi Publik\n- Bidang Aplikasi Informatika dan Infrastruktur TIK\n- Bidang Statistik dan Persandian\n- Kelompok Jabatan Fungsional"),
        ("pimpinan", "Pimpinan",
         "## Kepala Dinas\n[Nama pimpinan]\nRiwayat singkat pendidikan dan jabatan.\n\n## Sekretaris\n[Nama sekretaris]"),
        ("kebijakan-privasi", "Kebijakan privasi", DEFAULT_PRIVASI),
        ("syarat-ketentuan", "Syarat dan ketentuan", DEFAULT_SYARAT),
        ("aksesibilitas", "Pernyataan aksesibilitas", DEFAULT_AKSES),
    ]
    conn.executemany("INSERT INTO halaman(slug,judul,isi) VALUES(?,?,?)", H)

    L = [
        ("Permohonan subdomain go.id", "Pengajuan nama subdomain untuk situs resmi perangkat daerah, UPTD, atau desa.",
         "- Surat permohonan resmi dari kepala instansi\n- Nama subdomain yang diinginkan\n- Nama dan kontak penanggung jawab teknis\n- Alamat IP atau penyedia hosting yang dipakai", "5 hari kerja"),
        ("Permohonan hosting dan pusat data", "Penempatan aplikasi atau situs di infrastruktur pemerintah provinsi.",
         "- Surat permohonan resmi\n- Deskripsi aplikasi dan kebutuhan sumber daya\n- Hasil uji keamanan dasar\n- Nama penanggung jawab aplikasi", "10 hari kerja"),
        ("Rekomendasi aplikasi dan SPBE", "Telaah kesesuaian rencana pengembangan aplikasi dengan arsitektur SPBE provinsi.",
         "- Proposal pengembangan aplikasi\n- Kerangka acuan kerja\n- Perkiraan anggaran", "14 hari kerja"),
        ("Permohonan informasi publik", "Permintaan informasi sesuai Undang-Undang Keterbukaan Informasi Publik lewat PPID.",
         "- Identitas pemohon\n- Rincian informasi yang diminta\n- Tujuan penggunaan informasi", "10 hari kerja"),
        ("Permohonan data statistik sektoral", "Permintaan data atau tabulasi statistik yang belum tersedia di menu Data.",
         "- Identitas dan asal instansi pemohon\n- Rincian data yang diminta\n- Tujuan penggunaan data", "7 hari kerja"),
        ("Permohonan publikasi dan liputan", "Pengajuan liputan kegiatan atau penayangan informasi di kanal resmi.",
         "- Surat atau nota permintaan\n- Waktu dan tempat kegiatan\n- Narahubung kegiatan", "3 hari kerja"),
    ]
    for nama, ring, syarat, est in L:
        conn.execute("INSERT INTO layanan(slug,nama,ringkasan,syarat,estimasi) VALUES(?,?,?,?,?)",
                     (slugify(nama), nama, ring, syarat, est))

    B = [
        ("Portal layanan digital Lampung kini satu pintu", "Pengumuman", "2026-09-01",
         "Permohonan layanan, pengaduan konten negatif, data terbuka, dan produk hukum kini dapat diakses dari satu portal yang ramah ponsel.",
         "Portal ini menggabungkan layanan yang sebelumnya tersebar. Masyarakat dapat mengajukan permohonan, melaporkan konten negatif, mencari produk hukum, dan mengunduh data terbuka tanpa perlu datang ke kantor.\n\nSetiap permohonan dan pengaduan mendapat kode tiket untuk memantau perkembangan.\n\nCatatan: berita di portal ini adalah contoh awal. Ganti melalui panel admin."),
        ("Cara melaporkan konten negatif dan penipuan digital", "Artikel", "2026-09-05",
         "Temukan judi online, hoaks, atau penipuan? Ini langkah singkat melapor lewat portal.",
         "Buka menu Pengaduan, pilih kategori yang paling sesuai, lalu sertakan tautan atau tangkapan layar yang menjelaskan temuan Anda.\n\nAnda dapat melapor tanpa nama. Bila mengisi email, Anda dapat memantau tindak lanjutnya dengan kode tiket.\n\nJangan membagikan ulang konten yang dilaporkan, terutama yang berkaitan dengan anak."),
        ("Waspada penipuan yang mengatasnamakan instansi pemerintah", "Siaran Pers", "2026-09-12",
         "Ciri pesan penipuan dan cara memastikan bahwa sebuah situs benar-benar milik pemerintah.",
         "Situs resmi pemerintah daerah memakai alamat berakhiran go.id. Periksa ejaan alamat dengan teliti dan pastikan ada ikon gembok pada peramban.\n\nInstansi pemerintah tidak meminta kode OTP, kata sandi, atau transfer uang lewat pesan pribadi.\n\nBila ragu, hubungi kanal resmi yang tertera di halaman Kontak."),
        ("Panduan permohonan subdomain untuk perangkat daerah", "Pengumuman", "2026-09-20",
         "Syarat dan alur pengajuan subdomain go.id bagi OPD dan desa.",
         "Ajukan permohonan lewat menu Layanan. Siapkan surat resmi kepala instansi dan data penanggung jawab teknis.\n\nSetelah disetujui, tim akan mengonfigurasi subdomain dan memberi tahu Anda lewat email."),
    ]
    for judul, kat, tg, ring, isi in B:
        conn.execute("INSERT INTO berita(slug,judul,kategori,terbit,ringkasan,isi) VALUES(?,?,?,?,?,?)",
                     (slugify(judul), judul, kat, tg, ring, isi))

    J = [
        ("Undang-Undang", "14", 2008, "Keterbukaan Informasi Publik", "Berlaku"),
        ("Undang-Undang", "25", 2009, "Pelayanan Publik", "Berlaku"),
        ("Undang-Undang", "27", 2022, "Pelindungan Data Pribadi", "Berlaku"),
        ("Peraturan Pemerintah", "71", 2019, "Penyelenggaraan Sistem dan Transaksi Elektronik", "Berlaku"),
        ("Peraturan Pemerintah", "17", 2025, "Tata Kelola Penyelenggaraan Sistem Elektronik dalam Pelindungan Anak", "Berlaku"),
        ("Peraturan Presiden", "95", 2018, "Sistem Pemerintahan Berbasis Elektronik", "Berlaku"),
        ("Peraturan Presiden", "39", 2019, "Satu Data Indonesia", "Berlaku"),
    ]
    for j in J:
        conn.execute("INSERT INTO jdih(jenis,nomor,tahun,judul,status) VALUES(?,?,?,?,?)", j)

    kab = [("Bandar Lampung", 126), ("Metro", 22), ("Lampung Selatan", 260), ("Lampung Tengah", 305),
           ("Lampung Timur", 264), ("Lampung Utara", 247), ("Lampung Barat", 131), ("Tanggamus", 299),
           ("Way Kanan", 237), ("Tulang Bawang", 148), ("Tulang Bawang Barat", 93), ("Pesawaran", 144),
           ("Pringsewu", 131), ("Mesuji", 105), ("Pesisir Barat", 118)]
    csv1 = "Kabupaten/Kota,Desa/Kelurahan terjangkau internet\n" + "\n".join(f"{k},{v}" for k, v in kab)
    csv2 = "Kategori,Jumlah layanan digital\nPerizinan,12\nKesehatan,9\nPendidikan,11\nKependudukan,6\nSosial,7\nTransportasi,5"
    csv3 = "Kategori laporan,Jumlah laporan\nJudi online,42\nPenipuan online,37\nHoaks,21\nKonten anak,9\nGangguan situs pemerintah,6"
    D = [
        ("Desa/kelurahan terjangkau internet per kabupaten/kota", "Infrastruktur Digital", 2026,
         "Contoh struktur data. Ganti dengan data resmi hasil pendataan.", csv1),
        ("Layanan digital pemerintah menurut kategori", "Informasi Publik", 2026,
         "Contoh struktur data. Ganti dengan data resmi.", csv2),
        ("Laporan konten negatif menurut kategori", "Persandian", 2026,
         "Contoh struktur data. Ganti dengan rekap laporan sebenarnya.", csv3),
    ]
    for judul, kat, th, des, c in D:
        conn.execute("INSERT INTO dataset(slug,judul,kategori,sumber,tahun,deskripsi,csv) VALUES(?,?,?,?,?,?,?)",
                     (slugify(judul), judul, kat, "Data contoh, ganti dengan data resmi", th, des, c))

    DOK = [
        ("Informasi berkala", "Laporan Kinerja Instansi (LKjIP)", 2025), ("Informasi berkala", "Rencana Strategis", 2025),
        ("Anggaran", "Dokumen Pelaksanaan Anggaran (DPA)", 2026), ("Anggaran", "Realisasi Anggaran Semester I", 2026),
        ("Informasi setiap saat", "Daftar Informasi Publik", 2026), ("Informasi serta merta", "Prosedur Peringatan Dini dan Informasi Darurat", 2026),
    ]
    for k, j, t in DOK:
        conn.execute("INSERT INTO dokumen(kelompok,judul,tahun) VALUES(?,?,?)", (k, j, t))

    F = [
        ("lapor,pengaduan,konten,judi,hoaks,penipuan", "Untuk melaporkan konten negatif, judi online, hoaks, atau penipuan, buka halaman Pengaduan. Anda boleh melapor tanpa nama.", "/pengaduan"),
        ("lacak,status,tiket,kode", "Masukkan kode tiket pada halaman Lacak untuk melihat status permohonan atau pengaduan Anda.", "/lacak"),
        ("subdomain,domain,go.id,situs", "Permohonan subdomain diajukan lewat menu Layanan. Siapkan surat resmi dari kepala instansi.", "/layanan"),
        ("produk hukum,peraturan,jdih,perda,undang", "Cari peraturan dan produk hukum pada menu JDIH.", "/jdih"),
        ("data,statistik,dataset,unduh", "Data terbuka dapat dilihat dan diunduh dalam format CSV atau JSON pada menu Data.", "/data"),
        ("informasi publik,ppid,anggaran,kinerja,laporan", "Dokumen anggaran, laporan kinerja, dan informasi berkala tersedia di menu Publikasi.", "/publikasi"),
        ("kontak,alamat,telepon,email,jam", "Alamat, telepon, dan jam layanan ada di halaman Kontak.", "/kontak"),
        ("privasi,data pribadi,pdp", "Kebijakan pelindungan data pribadi kami dapat dibaca di halaman Kebijakan Privasi.", "/halaman/kebijakan-privasi"),
    ]
    conn.executemany("INSERT INTO faq(kata_kunci,jawaban,tautan) VALUES(?,?,?)", F)


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    seed(conn)
    if not conn.execute("SELECT 1 FROM users").fetchone():
        user = os.environ.get("ADMIN_USER", "admin").strip().lower() or "admin"
        pw = os.environ.get("ADMIN_PASSWORD", "")
        generated = False
        if len(pw) < 12:
            pw, generated = secrets.token_urlsafe(14), True
        conn.execute("INSERT INTO users(username,pw_hash) VALUES(?,?)", (user, generate_password_hash(pw)))
        if generated:
            note = DATA_DIR / "admin_pertama.txt"
            note.write_text(f"Pengguna: {user}\nKata sandi awal: {pw}\nHapus berkas ini setelah login dan ganti kata sandi.\n")
            os.chmod(note, 0o600)
            print(f"[SETUP] Akun admin dibuat. Lihat {note}", flush=True)
    conn.commit()
    conn.close()


DUMMY_HASH = generate_password_hash("kata-sandi-tiruan")

# ------------------------------------------------------------------ halaman publik

@app.route("/")
def index():
    c = db()
    stat = {
        "dataset": c.execute("SELECT COUNT(*) FROM dataset").fetchone()[0],
        "jdih": c.execute("SELECT COUNT(*) FROM jdih").fetchone()[0],
        "layanan": c.execute("SELECT COUNT(*) FROM layanan").fetchone()[0],
        "selesai": c.execute("SELECT COUNT(*) FROM tiket WHERE status='Selesai'").fetchone()[0],
    }
    ds = c.execute("SELECT * FROM dataset ORDER BY id DESC LIMIT 1").fetchone()
    grafik = make_chart(ds["csv"]) if ds else None
    return render_template(
        "index.html",
        berita=c.execute("SELECT * FROM berita ORDER BY terbit DESC, id DESC LIMIT 5").fetchall(),
        layanan=c.execute("SELECT * FROM layanan ORDER BY id LIMIT 6").fetchall(),
        jdih=c.execute("SELECT * FROM jdih ORDER BY tahun DESC, id DESC LIMIT 4").fetchall(),
        ds=ds, grafik=grafik, stat=stat)


@app.route("/halaman/<slug>")
def halaman(slug):
    h = db().execute("SELECT * FROM halaman WHERE slug=?", (slug,)).fetchone() or abort(404)
    return render_template("halaman.html", h=h, page_title=h["judul"])


@app.route("/profil")
def profil():
    rows = db().execute("SELECT * FROM halaman WHERE slug IN ('profil','visi-misi','struktur-organisasi','pimpinan')").fetchall()
    order = ["profil", "visi-misi", "struktur-organisasi", "pimpinan"]
    rows = sorted(rows, key=lambda r: order.index(r["slug"]))
    return render_template("profil.html", rows=rows, page_title="Profil")


@app.route("/berita")
def berita_list():
    q = request.args.get("q", "").strip()[:80]
    kat = request.args.get("kategori", "").strip()[:40]
    w, a = "1=1", []
    if q:
        w += " AND (judul LIKE ? ESCAPE '\\' OR ringkasan LIKE ? ESCAPE '\\')"
        a += [like(q), like(q)]
    if kat:
        w += " AND kategori=?"
        a.append(kat)
    per = 8
    total = db().execute(f"SELECT COUNT(*) FROM berita WHERE {w}", a).fetchone()[0]
    page, pages = paginate(total, per)
    rows = db().execute(f"SELECT * FROM berita WHERE {w} ORDER BY terbit DESC, id DESC LIMIT ? OFFSET ?",
                        a + [per, (page - 1) * per]).fetchall()
    return render_template("berita_list.html", rows=rows, q=q, kat=kat, page=page, pages=pages, total=total,
                           kategori=[r[0] for r in db().execute("SELECT DISTINCT kategori FROM berita ORDER BY 1")],
                           page_title="Berita")


@app.route("/berita/<slug>")
def berita_detail(slug):
    b = db().execute("SELECT * FROM berita WHERE slug=?", (slug,)).fetchone() or abort(404)
    lain = db().execute("SELECT judul,slug,terbit FROM berita WHERE id!=? ORDER BY terbit DESC LIMIT 3", (b["id"],)).fetchall()
    return render_template("berita_detail.html", b=b, lain=lain, page_title=b["judul"], meta_desc=b["ringkasan"])


@app.route("/layanan")
def layanan_list():
    rows = db().execute("SELECT * FROM layanan ORDER BY id").fetchall()
    return render_template("layanan_list.html", rows=rows, page_title="Layanan")


# ------------------------------------------------------------ tiket (layanan/aduan)

ALFABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RISIKO = re.compile(r"(?i)\b(slot gacor|togel|casino online|situs gacor|pinjol ilegal|judi online)\b")
URL_RE = re.compile(r"https?://", re.I)
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
KATEGORI_ADUAN = ["Judi online", "Penipuan online", "Hoaks atau disinformasi", "Pornografi atau konten anak",
                  "Ujaran kebencian", "Penyalahgunaan data pribadi", "Gangguan situs atau layanan pemerintah", "Lainnya"]
KATEGORI_KONTAK = ["Pertanyaan umum", "Saran", "Aksesibilitas", "Permintaan hak data pribadi", "Lainnya"]


def clean(s: str, n: int) -> str:
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s or "").strip()
    return s[:n]


def kode_tiket(pref: str) -> str:
    return f"{pref}-" + "".join(secrets.choice(ALFABET) for _ in range(8))


def baca_form(email_wajib=False, nama_wajib=True, tautan=False):
    f = request.form
    v = {k: clean(f.get(k, ""), n) for k, n in
         (("nama", 100), ("email", 200), ("telp", 30), ("instansi", 150), ("tautan", 500), ("isi", 4000), ("jenis", 100))}
    e = {}
    if nama_wajib and len(v["nama"]) < 2:
        e["nama"] = "Isi nama Anda."
    if (email_wajib or v["email"]) and not EMAIL_RE.match(v["email"]):
        e["email"] = "Isi alamat email yang valid, misalnya nama@contoh.com."
    if v["telp"] and not re.fullmatch(r"[0-9+()\-\s]{6,30}", v["telp"]):
        e["telp"] = "Nomor telepon hanya boleh berisi angka, spasi, +, -, dan tanda kurung."
    if len(v["isi"]) < 20:
        e["isi"] = "Jelaskan dengan minimal 20 karakter agar petugas dapat menindaklanjuti."
    if tautan and v["tautan"] and not re.match(r"^https?://\S+$", v["tautan"]):
        e["tautan"] = "Tautan harus diawali http:// atau https://."
    if not f.get("setuju"):
        e["setuju"] = "Centang persetujuan untuk melanjutkan."
    return v, e


def simpan_tiket(tipe, pref, v):
    ditandai = int(len(URL_RE.findall(v["isi"])) > 3 or (tipe != "pengaduan" and bool(RISIKO.search(v["isi"]))))
    for _ in range(5):
        kode = kode_tiket(pref)
        try:
            db().execute(
                "INSERT INTO tiket(kode,tipe,jenis,nama,email,telp,instansi,tautan,isi,ditandai) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (kode, tipe, v["jenis"], v["nama"], v["email"].lower(), v["telp"], v["instansi"], v["tautan"], v["isi"], ditandai))
            db().commit()
            return kode
        except sqlite3.IntegrityError:
            continue
    abort(500)


def guard_form(nama: str):
    if rate_limited((nama, request.remote_addr), 6, 600):
        abort(429, "Terlalu banyak kiriman dari alamat ini. Coba lagi dalam beberapa menit.")


@app.route("/layanan/<slug>", methods=["GET", "POST"])
def layanan_detail(slug):
    l = db().execute("SELECT * FROM layanan WHERE slug=?", (slug,)).fetchone() or abort(404)
    v, e = {}, {}
    if request.method == "POST":
        guard_form("layanan")
        if request.form.get("situs"):  # honeypot
            return redirect(url_for("layanan_list"))
        v, e = baca_form(email_wajib=True)
        if not e:
            v["jenis"] = l["nama"]
            kode = simpan_tiket("layanan", "LYN", v)
            return render_template("tiket_sukses.html", kode=kode, tipe="permohonan", page_title="Permohonan diterima")
    return render_template("layanan_detail.html", l=l, v=v, e=e, page_title=l["nama"], meta_desc=l["ringkasan"])


@app.route("/pengaduan", methods=["GET", "POST"])
def pengaduan():
    v, e = {}, {}
    if request.method == "POST":
        guard_form("aduan")
        if request.form.get("situs"):
            return redirect(url_for("pengaduan"))
        v, e = baca_form(nama_wajib=False, tautan=True)
        if v["jenis"] not in KATEGORI_ADUAN:
            e["jenis"] = "Pilih kategori laporan."
        if not e:
            v["nama"] = v["nama"] or "Anonim"
            kode = simpan_tiket("pengaduan", "ADU", v)
            return render_template("tiket_sukses.html", kode=kode, tipe="laporan", page_title="Laporan diterima")
    return render_template("pengaduan.html", v=v, e=e, kategori=KATEGORI_ADUAN, page_title="Pengaduan konten negatif")


@app.route("/kontak", methods=["GET", "POST"])
def kontak():
    v, e = {}, {}
    if request.method == "POST":
        guard_form("kontak")
        if request.form.get("situs"):
            return redirect(url_for("kontak"))
        v, e = baca_form(email_wajib=True)
        if v["jenis"] not in KATEGORI_KONTAK:
            e["jenis"] = "Pilih kategori pesan."
        if not e:
            kode = simpan_tiket("kontak", "KTK", v)
            return render_template("tiket_sukses.html", kode=kode, tipe="pesan", page_title="Pesan terkirim")
    return render_template("kontak.html", v=v, e=e, kategori=KATEGORI_KONTAK, page_title="Kontak")


@app.route("/lacak", methods=["GET", "POST"])
def lacak():
    t, galat = None, ""
    kode = request.values.get("kode", "").strip().upper()[:20]
    if request.method == "POST":
        if rate_limited(("lacak", request.remote_addr), 10, 300):
            abort(429, "Terlalu banyak percobaan. Coba lagi dalam beberapa menit.")
        email = request.form.get("email", "").strip().lower()[:200]
        r = db().execute("SELECT * FROM tiket WHERE kode=?", (kode,)).fetchone()
        ok = bool(r) and (not r["email"] or hmac.compare_digest(r["email"], email))
        if ok:
            t = r
        else:
            galat = "Kode tiket tidak ditemukan, atau email tidak cocok dengan yang dipakai saat mengirim."
    return render_template("lacak.html", t=t, galat=galat, kode=kode, page_title="Lacak tiket")


# --------------------------------------------------------------------- data terbuka

def parse_csv(text: str):
    rows = [r for r in csv.reader(io.StringIO((text or "").strip())) if any(c.strip() for c in r)]
    return (rows[0], rows[1:]) if rows else ([], [])


def make_chart(text: str):
    head, rows = parse_csv(text)
    if len(head) < 2 or not rows:
        return None
    sample = rows[:30]
    for j in range(1, len(head)):
        try:
            [float(r[j]) for r in sample]
        except (ValueError, IndexError):
            continue
        items = []
        for r in rows[:20]:
            try:
                items.append((r[0], float(r[j])))
            except (ValueError, IndexError):
                pass
        if not items:
            return None
        mx = max(v for _, v in items) or 1
        return {"judul": head[j], "n": len(items), "tinggi": 26 * len(items) + 8,
                "items": [{"label": (l if len(l) <= 26 else l[:25] + "…"), "val": v,
                           "w": max(2, round(v / mx * 380)), "y": 26 * i} for i, (l, v) in enumerate(items)]}
    return None


@app.route("/data")
def data_list():
    q = request.args.get("q", "").strip()[:80]
    kat = request.args.get("kategori", "").strip()[:60]
    w, a = "1=1", []
    if q:
        w += " AND (judul LIKE ? ESCAPE '\\' OR deskripsi LIKE ? ESCAPE '\\')"
        a += [like(q), like(q)]
    if kat:
        w += " AND kategori=?"
        a.append(kat)
    rows = db().execute(f"SELECT id,slug,judul,kategori,sumber,tahun,deskripsi FROM dataset WHERE {w} ORDER BY tahun DESC, id DESC", a).fetchall()
    return render_template("data_list.html", rows=rows, q=q, kat=kat, page_title="Data terbuka",
                           kategori=[r[0] for r in db().execute("SELECT DISTINCT kategori FROM dataset ORDER BY 1")])


@app.route("/data/<slug>")
def data_detail(slug):
    d = db().execute("SELECT * FROM dataset WHERE slug=?", (slug,)).fetchone() or abort(404)
    head, rows = parse_csv(d["csv"])
    return render_template("data_detail.html", d=d, head=head, rows=rows[:200], total=len(rows),
                           grafik=make_chart(d["csv"]), page_title=d["judul"], meta_desc=d["deskripsi"])


@app.route("/data/<slug>.csv")
def data_csv(slug):
    d = db().execute("SELECT judul,csv FROM dataset WHERE slug=?", (slug,)).fetchone() or abort(404)
    return Response("\ufeff" + d["csv"], mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'})


@app.route("/data/<slug>.json")
def data_json(slug):
    d = db().execute("SELECT * FROM dataset WHERE slug=?", (slug,)).fetchone() or abort(404)
    head, rows = parse_csv(d["csv"])
    payload = {"judul": d["judul"], "kategori": d["kategori"], "sumber": d["sumber"], "tahun": d["tahun"],
               "kolom": head, "data": [dict(zip(head, r)) for r in rows]}
    return Response(json.dumps(payload, ensure_ascii=False, indent=1), mimetype="application/json; charset=utf-8")


# ------------------------------------------------------------------ JDIH, publikasi

@app.route("/jdih")
def jdih():
    q = request.args.get("q", "").strip()[:100]
    jenis = request.args.get("jenis", "").strip()[:60]
    tahun = request.args.get("tahun", "").strip()[:4]
    w, a = "1=1", []
    if q:
        w += " AND (judul LIKE ? ESCAPE '\\' OR nomor LIKE ? ESCAPE '\\')"
        a += [like(q), like(q)]
    if jenis:
        w += " AND jenis=?"
        a.append(jenis)
    if tahun.isdigit():
        w += " AND tahun=?"
        a.append(int(tahun))
    per = 10
    total = db().execute(f"SELECT COUNT(*) FROM jdih WHERE {w}", a).fetchone()[0]
    page, pages = paginate(total, per)
    rows = db().execute(f"SELECT * FROM jdih WHERE {w} ORDER BY tahun DESC, id DESC LIMIT ? OFFSET ?",
                        a + [per, (page - 1) * per]).fetchall()
    return render_template(
        "jdih.html", rows=rows, q=q, jenis=jenis, tahun=tahun, page=page, pages=pages, total=total,
        jenis_opsi=[r[0] for r in db().execute("SELECT DISTINCT jenis FROM jdih ORDER BY 1")],
        tahun_opsi=[r[0] for r in db().execute("SELECT DISTINCT tahun FROM jdih ORDER BY 1 DESC")],
        page_title="JDIH: produk hukum")


@app.route("/publikasi")
def publikasi():
    rows = db().execute("SELECT * FROM dokumen ORDER BY kelompok, tahun DESC, id DESC").fetchall()
    grup: dict = {}
    for r in rows:
        grup.setdefault(r["kelompok"], []).append(r)
    return render_template("publikasi.html", grup=grup, page_title="Publikasi dan keterbukaan informasi")


@app.route("/cari")
def cari():
    q = request.args.get("q", "").strip()[:80]
    hasil = {}
    if q:
        L = like(q)
        c = db()
        hasil["Berita"] = c.execute("SELECT judul,slug,ringkasan FROM berita WHERE judul LIKE ? ESCAPE '\\' OR isi LIKE ? ESCAPE '\\' ORDER BY terbit DESC LIMIT 8", (L, L)).fetchall()
        hasil["Layanan"] = c.execute("SELECT nama,slug,ringkasan FROM layanan WHERE nama LIKE ? ESCAPE '\\' OR ringkasan LIKE ? ESCAPE '\\' LIMIT 8", (L, L)).fetchall()
        hasil["Produk hukum"] = c.execute("SELECT jenis,nomor,tahun,judul FROM jdih WHERE judul LIKE ? ESCAPE '\\' OR nomor LIKE ? ESCAPE '\\' LIMIT 8", (L, L)).fetchall()
        hasil["Data"] = c.execute("SELECT judul,slug,deskripsi FROM dataset WHERE judul LIKE ? ESCAPE '\\' OR deskripsi LIKE ? ESCAPE '\\' LIMIT 8", (L, L)).fetchall()
        hasil["Dokumen publikasi"] = c.execute("SELECT judul,tahun,kelompok,url FROM dokumen WHERE judul LIKE ? ESCAPE '\\' LIMIT 8", (L,)).fetchall()
    jumlah = sum(len(v) for v in hasil.values())
    return render_template("cari.html", q=q, hasil=hasil, jumlah=jumlah, page_title="Pencarian")


# ------------------------------------------------------------------------ chatbot

STOP = {"yang", "dan", "di", "ke", "dari", "untuk", "apa", "bagaimana", "cara", "saya", "mau", "ingin", "bisa",
        "apakah", "dengan", "atau", "itu", "ini", "tolong", "mohon", "minta", "ada", "bagaimanakah", "gimana"}


def tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 2 and w not in STOP}


@app.route("/api/chat", methods=["POST"])
def api_chat():
    if rate_limited(("chat", request.remote_addr), 30, 300):
        return jsonify(jawaban="Terlalu banyak pertanyaan. Coba lagi sebentar lagi.", tautan=""), 429
    body = request.get_json(silent=True) or {}
    q = clean(str(body.get("q", "")), 200)
    if len(q) < 3:
        return jsonify(jawaban="Tulis pertanyaan Anda, misalnya: cara melapor judi online.", tautan="")
    ql, qt = q.lower(), tokens(q)
    best, skor = None, 0
    for r in db().execute("SELECT * FROM faq"):
        s = 0
        for kw in (k.strip().lower() for k in r["kata_kunci"].split(",") if k.strip()):
            if kw in ql:
                s += 3
            elif tokens(kw) & qt:
                s += 1
        if s > skor:
            best, skor = r, s
    if best and skor >= 1:
        return jsonify(jawaban=best["jawaban"], tautan=best["tautan"])
    return jsonify(jawaban="Maaf, saya belum menemukan jawabannya. Coba kata kunci lain, atau kirim pesan lewat halaman Kontak agar dijawab petugas.",
                   tautan="/kontak")


# ---------------------------------------------------------------- SEO & kesehatan

@app.route("/healthz")
def healthz():
    db().execute("SELECT 1")
    return jsonify(status="ok")


@app.route("/robots.txt")
def robots():
    return Response(f"User-agent: *\nDisallow: /admin\nDisallow: /lacak\nDisallow: /api/\nSitemap: {request.url_root}sitemap.xml\n",
                    mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    urls = ["/", "/profil", "/berita", "/layanan", "/data", "/jdih", "/publikasi", "/pengaduan", "/kontak"]
    c = db()
    urls += [f"/berita/{r[0]}" for r in c.execute("SELECT slug FROM berita")]
    urls += [f"/layanan/{r[0]}" for r in c.execute("SELECT slug FROM layanan")]
    urls += [f"/data/{r[0]}" for r in c.execute("SELECT slug FROM dataset")]
    urls += [f"/halaman/{r[0]}" for r in c.execute("SELECT slug FROM halaman")]
    xml = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + \
          "".join(f"<url><loc>{escape(base + u)}</loc></url>" for u in urls) + "</urlset>"
    return Response(xml, mimetype="application/xml")


# ============================================================================ ADMIN

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        uid = session.get("uid")
        if not uid or time.time() - session.get("last", 0) > ADMIN_IDLE_SECONDS:
            session.pop("uid", None)
            return redirect(url_for("admin_masuk", next=request.path if request.method == "GET" else None))
        g.user = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not g.user or not g.user["totp_enabled"]:
            session.clear()
            return redirect(url_for("admin_masuk"))
        session["last"] = time.time()
        return fn(*a, **k)
    return wrapper


def aman_next(n):
    return n if n and n.startswith("/admin") and not n.startswith("//") and "\\" not in n else url_for("admin_dashboard")


def mulai_sesi(uid):
    session.clear()
    session["uid"] = uid
    session["last"] = time.time()
    session.permanent = True


@app.route("/admin/masuk", methods=["GET", "POST"])
def admin_masuk():
    pesan = ""
    if request.method == "POST":
        if rate_limited(("login", request.remote_addr), 12, 300):
            abort(429, "Terlalu banyak percobaan masuk. Tunggu beberapa menit.")
        uname = request.form.get("username", "").strip().lower()[:60]
        pw = request.form.get("password", "")[:200]
        otp = request.form.get("otp", "")
        u = db().execute("SELECT * FROM users WHERE username=?", (uname,)).fetchone()
        now = time.time()
        generik = "Nama pengguna, kata sandi, atau kode OTP tidak sesuai."
        if u and u["locked_until"] > now:
            pesan = "Akun dikunci sementara karena terlalu banyak percobaan gagal. Coba lagi nanti."
        else:
            pw_ok = check_password_hash(u["pw_hash"] if u else DUMMY_HASH, pw) and bool(u)
            if pw_ok and not u["totp_enabled"]:
                session.clear()
                session["pending_uid"] = u["id"]
                audit("Login awal, menunggu setup 2FA", u["username"])
                return redirect(url_for("admin_2fa"))
            step = totp_verify(u["totp_secret"], otp, u["last_step"]) if pw_ok else None
            if pw_ok and step:
                db().execute("UPDATE users SET failed=0, locked_until=0, last_step=? WHERE id=?", (step, u["id"]))
                db().commit()
                nxt = aman_next(request.args.get("next"))
                mulai_sesi(u["id"])
                audit("Login berhasil", u["username"])
                return redirect(nxt)
            pesan = generik
            if u:
                failed = u["failed"] + 1
                lock = now + LOCK_SECONDS if failed >= MAX_FAILED_LOGIN else 0
                db().execute("UPDATE users SET failed=?, locked_until=? WHERE id=?", (0 if lock else failed, lock, u["id"]))
                db().commit()
                audit("Login gagal" + (" (akun dikunci)" if lock else ""), u["username"])
    return render_template("admin/masuk.html", pesan=pesan, page_title="Masuk petugas")


@app.route("/admin/2fa", methods=["GET", "POST"])
def admin_2fa():
    uid = session.get("pending_uid")
    if not uid:
        return redirect(url_for("admin_masuk"))
    u = db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(403)
    if "totp_tmp" not in session:
        session["totp_tmp"] = new_totp_secret()
    secret = session["totp_tmp"]
    pesan = ""
    if request.method == "POST":
        if rate_limited(("2fa", request.remote_addr), 10, 300):
            abort(429, "Terlalu banyak percobaan.")
        step = totp_verify(secret, request.form.get("otp", ""))
        if step:
            db().execute("UPDATE users SET totp_secret=?, totp_enabled=1, last_step=? WHERE id=?", (secret, step, uid))
            db().commit()
            mulai_sesi(uid)
            audit("2FA diaktifkan", u["username"])
            flash("Verifikasi dua langkah aktif. Mulai sekarang kode OTP diperlukan setiap kali masuk.")
            return redirect(url_for("admin_dashboard"))
        pesan = "Kode tidak cocok. Pastikan jam ponsel Anda akurat lalu coba lagi."
    uri = f"otpauth://totp/{SITE['nama'].replace(' ', '%20')}:{u['username']}?secret={secret}&issuer={SITE['nama'].replace(' ', '%20')}"
    pretty = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    return render_template("admin/setup2fa.html", secret=pretty, uri=uri, pesan=pesan, page_title="Aktifkan 2FA")


@app.route("/admin/keluar", methods=["POST"])
def admin_keluar():
    session.clear()
    return redirect(url_for("admin_masuk"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    c = db()
    hitung = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ENT}
    baru = c.execute("SELECT COUNT(*) FROM tiket WHERE status='Diterima'").fetchone()[0]
    ditandai = c.execute("SELECT COUNT(*) FROM tiket WHERE ditandai=1 AND status='Diterima'").fetchone()[0]
    terbaru = c.execute("SELECT * FROM tiket ORDER BY id DESC LIMIT 6").fetchall()
    return render_template("admin/dashboard.html", hitung=hitung, baru=baru, ditandai=ditandai, terbaru=terbaru, page_title="Dasbor")


@app.route("/admin/sandi", methods=["GET", "POST"])
@admin_required
def admin_sandi():
    pesan = ""
    if request.method == "POST":
        lama, baru, ulang = (request.form.get(k, "") for k in ("lama", "baru", "ulang"))
        if not check_password_hash(g.user["pw_hash"], lama):
            pesan = "Kata sandi saat ini salah."
        elif len(baru) < 12 or baru.lower() == g.user["username"] or not (re.search(r"[A-Za-z]", baru) and re.search(r"\d", baru)):
            pesan = "Kata sandi baru minimal 12 karakter dan memuat huruf serta angka."
        elif baru != ulang:
            pesan = "Konfirmasi kata sandi tidak sama."
        else:
            db().execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(baru), g.user["id"]))
            db().commit()
            audit("Kata sandi diganti")
            flash("Kata sandi berhasil diganti.")
            return redirect(url_for("admin_dashboard"))
    return render_template("admin/sandi.html", pesan=pesan, page_title="Ganti kata sandi")


@app.route("/admin/audit")
@admin_required
def admin_audit():
    rows = db().execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("admin/audit.html", rows=rows, page_title="Log audit")


# ---- tiket

STATUS = ["Diterima", "Diproses", "Selesai", "Ditolak"]


@app.route("/admin/tiket")
@admin_required
def admin_tiket():
    tipe = request.args.get("tipe", "")
    st = request.args.get("status", "")
    w, a = "1=1", []
    if tipe in ("layanan", "pengaduan", "kontak"):
        w += " AND tipe=?"
        a.append(tipe)
    if st in STATUS:
        w += " AND status=?"
        a.append(st)
    rows = db().execute(f"SELECT * FROM tiket WHERE {w} ORDER BY ditandai DESC, id DESC LIMIT 300", a).fetchall()
    return render_template("admin/tiket.html", rows=rows, tipe=tipe, st=st, STATUS=STATUS, page_title="Tiket")


@app.route("/admin/tiket/<int:tid>", methods=["GET", "POST"])
@admin_required
def admin_tiket_detail(tid):
    t = db().execute("SELECT * FROM tiket WHERE id=?", (tid,)).fetchone() or abort(404)
    if request.method == "POST":
        st = request.form.get("status")
        if st not in STATUS:
            abort(400)
        db().execute("UPDATE tiket SET status=?, tanggapan=?, ditandai=0, diperbarui=datetime('now','+7 hours') WHERE id=?",
                     (st, clean(request.form.get("tanggapan", ""), 3000), tid))
        db().commit()
        audit(f"Tiket {t['kode']} -> {st}")
        flash("Tiket diperbarui.")
        return redirect(url_for("admin_tiket_detail", tid=tid))
    return render_template("admin/tiket_detail.html", t=t, STATUS=STATUS, page_title=t["kode"])


# ---- CRUD generik

def F(name, label, tipe="text", req=True, hint=""):
    return dict(name=name, label=label, tipe=tipe, req=req, hint=hint)


ENT = {
    "berita": dict(label="Berita", kolom=["judul", "kategori", "terbit"], urut="terbit DESC, id DESC", slug_src="judul", fields=[
        F("judul", "Judul"), F("kategori", "Kategori", "select:Berita,Pengumuman,Siaran Pers,Artikel"),
        F("terbit", "Tanggal terbit", "date"), F("ringkasan", "Ringkasan", "textarea", hint="Satu sampai dua kalimat."),
        F("isi", "Isi berita", "textarea-lg", hint="Pisahkan paragraf dengan baris kosong. Awali baris dengan '## ' untuk subjudul dan '- ' untuk butir daftar.")]),
    "layanan": dict(label="Layanan", kolom=["nama", "estimasi"], urut="id", slug_src="nama", fields=[
        F("nama", "Nama layanan"), F("ringkasan", "Ringkasan", "textarea"),
        F("syarat", "Persyaratan", "textarea", hint="Satu persyaratan per baris, awali dengan '- '."),
        F("estimasi", "Estimasi waktu", hint="Contoh: 5 hari kerja")]),
    "jdih": dict(label="Produk hukum (JDIH)", kolom=["jenis", "nomor", "tahun", "judul", "status"], urut="tahun DESC, id DESC", fields=[
        F("jenis", "Jenis", "select:Undang-Undang,Peraturan Pemerintah,Peraturan Presiden,Peraturan Menteri,Peraturan Daerah,Peraturan Gubernur,Keputusan Gubernur,Lainnya"),
        F("nomor", "Nomor"), F("tahun", "Tahun", "number"), F("judul", "Tentang"),
        F("status", "Status", "select:Berlaku,Diubah,Dicabut"), F("url", "Tautan dokumen (PDF)", "url", False)]),
    "dataset": dict(label="Data terbuka", kolom=["judul", "kategori", "tahun"], urut="id DESC", slug_src="judul", fields=[
        F("judul", "Judul"), F("kategori", "Kategori", "select:Infrastruktur Digital,Statistik Sektoral,Informasi Publik,Persandian,Lainnya"),
        F("sumber", "Sumber data", hint="Tulis sumber resmi. Awali dengan 'Data contoh' bila masih contoh."), F("tahun", "Tahun", "number"),
        F("deskripsi", "Deskripsi", "textarea"),
        F("csv", "Isi data (CSV)", "csv", hint="Baris pertama adalah judul kolom. Kolom pertama label, kolom berikutnya angka dengan titik desimal tanpa pemisah ribuan.")]),
    "dokumen": dict(label="Dokumen publikasi", kolom=["kelompok", "judul", "tahun"], urut="kelompok, tahun DESC", fields=[
        F("kelompok", "Kelompok", "select:Informasi berkala,Informasi serta merta,Informasi setiap saat,Anggaran,Laporan kinerja"),
        F("judul", "Judul"), F("tahun", "Tahun", "number"), F("url", "Tautan dokumen (PDF)", "url", False)]),
    "halaman": dict(label="Halaman statis", kolom=["slug", "judul"], urut="id", fields=[
        F("slug", "Alamat (slug)", "slug", hint="Huruf kecil, angka, dan tanda hubung. Contoh: profil"), F("judul", "Judul"),
        F("isi", "Isi", "textarea-lg", hint="Awali baris dengan '## ' untuk subjudul dan '- ' untuk butir daftar.")]),
    "faq": dict(label="Jawaban chatbot", kolom=["kata_kunci", "jawaban"], urut="id", fields=[
        F("kata_kunci", "Kata kunci", hint="Pisahkan dengan koma. Contoh: lapor, pengaduan, judi"),
        F("jawaban", "Jawaban", "textarea"), F("tautan", "Tautan halaman", "path", False, "Awali dengan '/', contoh /pengaduan")]),
}


def opsi(f):
    return f["tipe"][7:].split(",") if f["tipe"].startswith("select:") else []


def validasi(cfg, form):
    v, e = {}, {}
    for f in cfg["fields"]:
        raw = (form.get(f["name"], "") or "").replace("\r", "")
        maks = 200000 if f["tipe"] == "csv" else 20000 if f["tipe"].startswith("textarea") else 3000 if f["tipe"] == "textarea" else 500
        val = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", raw).strip()[:maks]
        v[f["name"]] = val
        if not val:
            if f["req"]:
                e[f["name"]] = "Wajib diisi."
            continue
        t = f["tipe"]
        if t.startswith("select:") and val not in opsi(f):
            e[f["name"]] = "Pilihan tidak valid."
        elif t == "date":
            try:
                datetime.strptime(val, "%Y-%m-%d")
            except ValueError:
                e[f["name"]] = "Tanggal tidak valid."
        elif t == "number" and not (val.isdigit() and 1900 <= int(val) <= 2100):
            e[f["name"]] = "Isi tahun 4 digit."
        elif t == "url" and not re.match(r"^https?://\S+$", val):
            e[f["name"]] = "Harus diawali http:// atau https://."
        elif t == "path" and (not val.startswith("/") or val.startswith("//") or " " in val):
            e[f["name"]] = "Harus diawali satu '/'."
        elif t == "slug" and not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", val):
            e[f["name"]] = "Hanya huruf kecil, angka, dan tanda hubung."
        elif t == "csv":
            h, r = parse_csv(val)
            if len(h) < 2 or not r:
                e[f["name"]] = "Minimal 2 kolom dan 1 baris data."
    return v, e


def ent_or_404(ent):
    return ENT.get(ent) or abort(404)


@app.route("/admin/<ent>")
@admin_required
def admin_list(ent):
    cfg = ent_or_404(ent)
    rows = db().execute(f"SELECT * FROM {ent} ORDER BY {cfg['urut']} LIMIT 500").fetchall()
    return render_template("admin/list.html", ent=ent, cfg=cfg, rows=rows, page_title=cfg["label"])


@app.route("/admin/<ent>/baru", methods=["GET", "POST"])
@app.route("/admin/<ent>/<int:rid>/ubah", methods=["GET", "POST"])
@admin_required
def admin_form(ent, rid=None):
    cfg = ent_or_404(ent)
    row = None
    if rid:
        row = db().execute(f"SELECT * FROM {ent} WHERE id=?", (rid,)).fetchone() or abort(404)
    v = dict(row) if row else {}
    e = {}
    if request.method == "POST":
        v, e = validasi(cfg, request.form)
        if "slug" in v and not e.get("slug"):
            dup = db().execute("SELECT 1 FROM halaman WHERE slug=? AND id IS NOT ?", (v["slug"], rid)).fetchone() if ent == "halaman" else None
            if dup:
                e["slug"] = "Alamat sudah dipakai."
        if not e:
            cols = [f["name"] for f in cfg["fields"]]
            vals = [v[c] for c in cols]
            if row:
                db().execute(f"UPDATE {ent} SET {','.join(c + '=?' for c in cols)} WHERE id=?", vals + [rid])
            else:
                if cfg.get("slug_src"):
                    cols.append("slug")
                    vals.append(unique_slug(ent, v[cfg["slug_src"]]))
                db().execute(f"INSERT INTO {ent}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            db().commit()
            audit(f"{'Ubah' if row else 'Tambah'} {ent}: {v.get(cfg['fields'][0]['name'], '')[:60]}")
            flash("Perubahan disimpan.")
            return redirect(url_for("admin_list", ent=ent))
    return render_template("admin/form.html", ent=ent, cfg=cfg, v=v, e=e, row=row, opsi=opsi,
                           page_title=("Ubah " if row else "Tambah ") + cfg["label"])


@app.route("/admin/<ent>/<int:rid>/hapus", methods=["POST"])
@admin_required
def admin_hapus(ent, rid):
    ent_or_404(ent)
    db().execute(f"DELETE FROM {ent} WHERE id=?", (rid,))
    db().commit()
    audit(f"Hapus {ent} #{rid}")
    flash("Data dihapus.")
    return redirect(url_for("admin_list", ent=ent))


# ------------------------------------------------------------------------- CLI

@app.cli.command("purge-tiket")
@click.option("--bulan", default=24, show_default=True, help="Hapus tiket selesai/ditolak yang lebih lama dari N bulan.")
def purge_tiket(bulan):
    """Retensi data pribadi: hapus tiket lama sesuai kebijakan privasi."""
    batas = (now_wib() - timedelta(days=30 * bulan)).strftime("%Y-%m-%d %H:%M:%S")
    with app.app_context():
        cur = db().execute("DELETE FROM tiket WHERE status IN ('Selesai','Ditolak') AND diperbarui < ?", (batas,))
        db().commit()
        click.echo(f"{cur.rowcount} tiket dihapus.")


@app.cli.command("reset-2fa")
@click.argument("username")
def reset_2fa(username):
    """Reset 2FA petugas (kode akan dibuat ulang saat login berikutnya)."""
    with app.app_context():
        cur = db().execute("UPDATE users SET totp_enabled=0, totp_secret=NULL, last_step=0 WHERE username=?", (username.lower(),))
        db().commit()
        click.echo("2FA direset." if cur.rowcount else "Pengguna tidak ditemukan.")


@app.cli.command("set-password")
@click.argument("username")
@click.password_option()
def set_password(username, password):
    """Setel ulang kata sandi petugas."""
    if len(password) < 12:
        raise click.ClickException("Minimal 12 karakter.")
    with app.app_context():
        cur = db().execute("UPDATE users SET pw_hash=?, failed=0, locked_until=0 WHERE username=?",
                           (generate_password_hash(password), username.lower()))
        db().commit()
        click.echo("Kata sandi diganti." if cur.rowcount else "Pengguna tidak ditemukan.")


init_db()

if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), debug=False)
