/* Tanpa pustaka luar. Semua pengaturan aksesibilitas disimpan di perangkat pengguna (localStorage). */
(function () {
  'use strict';
  var root = document.documentElement;
  root.classList.add('js');

  /* ---------- Aksesibilitas ---------- */
  var KEY = 'a11y-v1';
  var defaults = { size: '0', contrast: 'normal', font: 'normal', align: 'left' };
  var state = Object.assign({}, defaults);
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) {}

  function apply() {
    Object.keys(defaults).forEach(function (k) { root.setAttribute('data-' + k, state[k]); });
    document.querySelectorAll('[data-set]').forEach(function (b) {
      var p = b.getAttribute('data-set').split(':');
      b.setAttribute('aria-pressed', String(state[p[0]] === p[1]));
    });
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
  }
  apply();

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-set]');
    if (b) { var p = b.getAttribute('data-set').split(':'); state[p[0]] = p[1]; apply(); return; }
    if (ev.target.closest('[data-reset]')) { state = Object.assign({}, defaults); apply(); }
  });

  var panelBtn = document.getElementById('a11y-toggle');
  var panel = document.getElementById('a11y-panel');
  if (panelBtn && panel) {
    panelBtn.addEventListener('click', function () {
      var open = panel.hidden;
      panel.hidden = !open;
      panelBtn.setAttribute('aria-expanded', String(open));
    });
  }

  /* ---------- Konfirmasi hapus (admin) ---------- */
  document.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-confirm]');
    if (b && !window.confirm('Hapus data ini? Tindakan tidak dapat dibatalkan.')) ev.preventDefault();
  });

  /* ---------- Menu mobile ---------- */
  var mbtn = document.getElementById('menu-toggle');
  var nav = document.getElementById('nav');
  if (mbtn && nav) {
    mbtn.addEventListener('click', function () {
      var open = nav.classList.toggle('open');
      mbtn.setAttribute('aria-expanded', String(open));
    });
  }

  /* ---------- Baca nyaring (Web Speech API) ---------- */
  var readBtn = document.getElementById('read-aloud');
  if (readBtn) {
    if (!('speechSynthesis' in window)) {
      readBtn.disabled = true;
      readBtn.title = 'Perangkat ini tidak mendukung baca nyaring';
    } else {
      var speaking = false;
      var stop = function () { speechSynthesis.cancel(); speaking = false; readBtn.setAttribute('aria-pressed', 'false'); readBtn.textContent = 'Baca halaman'; };
      readBtn.addEventListener('click', function () {
        if (speaking) { stop(); return; }
        var main = document.getElementById('konten');
        var sel = String(window.getSelection() || '').trim();
        var text = (sel || (main && main.innerText) || '').replace(/\s+/g, ' ').trim();
        if (!text) return;
        var parts = text.match(/[^.!?]+[.!?]*/g) || [text];
        var chunks = [], cur = '';
        parts.forEach(function (s) { if ((cur + s).length > 180) { if (cur) chunks.push(cur); cur = s; } else { cur += s; } });
        if (cur) chunks.push(cur);
        var voices = speechSynthesis.getVoices();
        var idv = voices.filter(function (v) { return /^id/i.test(v.lang); })[0];
        speaking = true; readBtn.setAttribute('aria-pressed', 'true'); readBtn.textContent = 'Berhenti membaca';
        chunks.forEach(function (c, i) {
          var u = new SpeechSynthesisUtterance(c);
          u.lang = 'id-ID'; if (idv) u.voice = idv; u.rate = 0.95;
          if (i === chunks.length - 1) u.onend = stop;
          speechSynthesis.speak(u);
        });
      });
      window.addEventListener('pagehide', function () { speechSynthesis.cancel(); });
    }
  }

  /* ---------- Chatbot ---------- */
  var cbtn = document.getElementById('chat-toggle');
  var box = document.getElementById('chat');
  if (cbtn && box) {
    var log = document.getElementById('chat-log');
    var form = document.getElementById('chat-form');
    var input = document.getElementById('chat-input');
    var csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    function add(text, who, link) {
      var d = document.createElement('div');
      d.className = 'msg ' + who;
      d.appendChild(document.createTextNode(text));
      if (link && /^\/(?!\/)/.test(link)) {
        d.appendChild(document.createElement('br'));
        var a = document.createElement('a');
        a.href = link; a.textContent = 'Buka halaman terkait';
        d.appendChild(a);
      }
      log.appendChild(d); log.scrollTop = log.scrollHeight;
    }
    function toggle(show) {
      box.hidden = !show;
      cbtn.setAttribute('aria-expanded', String(show));
      if (show) { if (!log.children.length) add('Halo, saya asisten portal. Tanyakan misalnya: cara melapor judi online, atau cara meminta subdomain.', 'bot'); input.focus(); }
    }
    cbtn.addEventListener('click', function () { toggle(box.hidden); });
    document.getElementById('chat-close').addEventListener('click', function () { toggle(false); cbtn.focus(); });
    box.addEventListener('keydown', function (e) { if (e.key === 'Escape') { toggle(false); cbtn.focus(); } });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = input.value.trim(); if (!q) return;
      add(q, 'me'); input.value = '';
      fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, body: JSON.stringify({ q: q }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { add(d.jawaban, 'bot', d.tautan); })
        .catch(function () { add('Layanan chatbot sedang tidak tersedia. Silakan gunakan halaman Kontak.', 'bot', '/kontak'); });
    });
  }
})();
