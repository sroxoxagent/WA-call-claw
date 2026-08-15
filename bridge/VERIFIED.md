# ✅ VERIFIED — WhatsApp Call (Inbound 2-Arah + Outbound)

**Update terakhir:** 2026-08-15 22:30 WIB
**Status:** JALAN & TERBUKTI — inbound 1:1 **dua arah** + outbound announcement

## Hasil tes OUTBOUND (bot menelepon keluar)
| # | Waktu | Tujuan | Hasil |
|---|-------|--------|-------|
| 1 | 17:07 | Owner | Audio terdengar ✅ |
| 2 | 17:08 | Owner | Audio terdengar ✅ |
| 3 | 17:11 | Tester | Audio terdengar + receive 150+ frame ✅ |

## Hasil tes INBOUND (bot di-telepon) — DUA ARAH ✅
| # | Waktu | Caller | Hasil |
|---|-------|--------|-------|
| 1 | 22:05 | Owner | Call 2m06s: **796 frames** caller audio direkam (1.5 MB WAV) + **TTS playback terdengar caller** + **5× barge-in** (caller motong playback, STT jalan) ✅ |

Bukti log voice agent (22:06–22:07): `barge-in: STT confirmed 6 word(s) — stopping playback`, `TTS cancelled by barge-in (user started speaking)` — interaksi 2 arah normal.

> **Catatan koreksi (22:30):** Versi VERIFIED.md 17:12 menyatakan "audio bot TIDAK
> diteruskan ke penelpon pada call inbound" — **KLAIM ITU SALAH/OUTDATED**.
> Kesimpulan itu diambil dari tes outbound saja. Setelah multi-relay fix (PR #26:
> `connectAndAllocateAll` — bind ke SEMUA relay endpoint yang ditawarkan,
> broadcast outbound, merge inbound dengan replay filter), audio inbound
> mengalir **dua arah**. Jangan revert ke single-relay binding.

## Fix yang membuat ini jalan
1. **Multi-relay (PR #26)** — `engine_media.go`: `connectAndAllocateAll()` untuk
   inbound 1:1; phone yang migrasi relay setelah accept tetap kedengaran.
2. **Send loop pacing** — sleep timing sesuai pacing frame; player frame
   dikonsumsi (`nextFrame`); rate limiting/packet drop yang bikin audio nyangkut
   dihapus.
3. **Instrumentasi** — log "send loop tick" tiap 15 frame (from_player + pcm_rms + payload_len).

## Aturan penting (hasil observasi)
- **Inbound 1:1 = dua arah** (dengan multi-relay fix). Caller dengar bot, bot dengar caller.
- **Outbound = dua arah juga** (bot caller, bot bisa dengar lawan bicara).
- **Group call**: core library support, bridge masih in progress.
- Kalau inbound tiba-tiba "tuli"/caller tidak dengar bot → cek `connectAndAllocateAll` masih intact.

## Checksum file kunci
228c1b94786ddeee01fe6810901d88706830eb6ff07683224085cd3bb7c10830  third_party/meowcaller/engine_media.go
3bb75e702581f8d6df3850500d2372cc551671794e026bb3e7a090190cb074c1  scripts/wacall.py
ba8b7e5f271b1d36ca65ed16094755f5ae8001f13cd0b1a068655c17ce954fdb  (removed — personal audio, purged 2026-08-15)
1a0e57428e3f05b361ec11fa86c327b6a0a4ddf9d80f11aee9dad21d02c07e9a  config.yaml
