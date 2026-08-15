# ✅ VERIFIED — Outgoing WhatsApp Call (Audio Terdengar)

**Tanggal verifikasi:** 2026-08-15 17:12 WIB
**Status:** JALAN & TERBUKTI (3x call keluar sukses)

## Hasil tes
| # | Waktu | Tujuan | Hasil |
|---|-------|--------|-------|
| 1 | 17:07 | Owner | Audio terdengar ✅ |
| 2 | 17:08 | Owner | Audio terdengar ✅ |
| 3 | 17:11 | Tester | Audio terdengar + receive 150+ frame ✅ |

## Fix yang membuat ini jalan (engine_media.go send loop)
1. Sleep timing diperbaiki (delay antar frame sesuai pacing, bukan busy loop)
2. Player frame benar-benar dikonsumsi (nextFrame dipanggil, bukan di-skip)
3. Rate limiting / packet drop yang bikin audio nyangkut dihapus
4. Instrumentasi: log "send loop tick" tiap 15 frame (from_player + pcm_rms + payload_len)

## Aturan penting (hasil observasi)
- **Relay WA hanya meneruskan audio caller → callee.** Bot HARUS jadi caller (outbound call).
- Call inbound (bot di-telepon): audio bot TIDAK diteruskan ke penelpon. Jangan pakai untuk use case inbound.
- Receive path outbound jalan (bot bisa dengar lawan bicara).

## Checksum file kunci
228c1b94786ddeee01fe6810901d88706830eb6ff07683224085cd3bb7c10830  third_party/meowcaller/engine_media.go
3bb75e702581f8d6df3850500d2372cc551671794e026bb3e7a090190cb074c1  scripts/wacall.py
ba8b7e5f271b1d36ca65ed16094755f5ae8001f13cd0b1a068655c17ce954fdb  (removed — personal audio, purged 2026-08-15)
1a0e57428e3f05b361ec11fa86c327b6a0a4ddf9d80f11aee9dad21d02c07e9a  config.yaml
