"""Tests for PCM / WAV conversion utilities (no external deps beyond numpy)."""

from __future__ import annotations

import struct
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_convert import (
    chunk_pcm,
    parse_wav_header,
    pcm_to_wav,
    strip_wav_header,
    wav_to_pcm_s16le_16k,
    TARGET_SAMPLE_RATE,
    TARGET_CHANNELS,
    TARGET_SAMPLE_WIDTH,
)


def _make_wav(
    pcm_data: bytes,
    sample_rate: int = 44100,
    channels: int = 1,
    bits_per_sample: int = 16,
) -> bytes:
    """Build a minimal WAV file from raw PCM data."""
    byte_rate = sample_rate * channels * (bits_per_sample // 8)
    block_align = channels * (bits_per_sample // 8)
    data_size = len(pcm_data)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + pcm_data


def test_parse_wav_header_valid():
    pcm = b"\x00\x00" * 100  # 100 samples of silence
    wav = _make_wav(pcm, sample_rate=44100, channels=2, bits_per_sample=16)
    result = parse_wav_header(wav)
    assert result is not None
    rate, ch, bits, offset = result
    assert rate == 44100
    assert ch == 2
    assert bits == 16
    assert offset == 44


def test_parse_wav_header_not_wav():
    assert parse_wav_header(b"RIFFxxxxJUNK") is None
    assert parse_wav_header(b"") is None
    assert parse_wav_header(b"not a wav file") is None


def test_parse_wav_header_too_short():
    assert parse_wav_header(b"RIFF") is None


def test_strip_wav_header():
    pcm = b"\x01\x02" * 50
    wav = _make_wav(pcm)
    stripped = strip_wav_header(wav)
    assert stripped == pcm


def test_strip_wav_header_no_wav():
    raw = b"\x03\x04" * 50
    assert strip_wav_header(raw) == raw


def test_wav_to_pcm_passthrough_already_16k_mono():
    """If WAV is already 16 kHz mono 16-bit, output should match raw PCM."""
    pcm = b"\x00\x10" * 100  # some 16-bit samples
    wav = _make_wav(pcm, sample_rate=16000, channels=1, bits_per_sample=16)
    result = wav_to_pcm_s16le_16k(wav)
    assert result == pcm


def test_wav_to_pcm_stereo_to_mono():
    """Stereo 16-bit 16 kHz should be mixed to mono."""
    import numpy as np

    # Create stereo PCM: left=1000, right=3000 → mono should be 2000
    left = np.full(10, 1000, dtype=np.int16)
    right = np.full(10, 3000, dtype=np.int16)
    stereo = np.empty(20, dtype=np.int16)
    stereo[0::2] = left
    stereo[1::2] = right
    pcm_stereo = stereo.tobytes()

    wav = _make_wav(pcm_stereo, sample_rate=16000, channels=2, bits_per_sample=16)
    result = wav_to_pcm_s16le_16k(wav)
    result_arr = np.frombuffer(result, dtype=np.int16)
    assert len(result_arr) == 10
    assert all(abs(v - 2000) <= 1 for v in result_arr)  # average of 1000 and 3000


def test_wav_to_pcm_resample_441k_to_16k():
    """44.1 kHz → 16 kHz resampling."""
    import numpy as np

    # Create 44100 samples (1 second) of a sine-like signal
    t = np.linspace(0, 1, 44100, endpoint=False)
    signal = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    wav = _make_wav(signal.tobytes(), sample_rate=44100, channels=1, bits_per_sample=16)

    result = wav_to_pcm_s16le_16k(wav)
    result_arr = np.frombuffer(result, dtype=np.int16)
    # Expected: ~16000 samples (16000/44100 * 44100 ≈ 16000)
    assert 15900 < len(result_arr) < 16100


def test_wav_to_pcm_u8_to_s16():
    """8-bit unsigned PCM should be converted to 16-bit signed."""
    import numpy as np

    u8 = np.array([128, 255, 0, 128], dtype=np.uint8)
    pcm_u8 = u8.tobytes()
    wav = _make_wav(pcm_u8, sample_rate=16000, channels=1, bits_per_sample=8)
    result = wav_to_pcm_s16le_16k(wav)
    result_arr = np.frombuffer(result, dtype=np.int16)
    assert len(result_arr) == 4
    assert result_arr[0] == 0  # 128 → 0
    assert result_arr[1] == 32512  # 255 → (255-128)*256 = 32512
    assert result_arr[2] == -32768  # 0 → (0-128)*256 = -32768


def test_wav_to_pcm_passthrough_raw():
    """If input is raw PCM (not WAV), return as-is."""
    raw = b"\xff\xfe" * 50
    assert wav_to_pcm_s16le_16k(raw) == raw


def test_chunk_pcm():
    pcm = b"\x00\x00" * 1000  # 2000 bytes
    chunks = chunk_pcm(pcm, chunk_bytes=320)
    assert len(chunks) == 7  # 2000 / 320 = 6.25 → 7 chunks
    assert len(chunks[0]) == 320
    assert len(chunks[-1]) == 80  # remainder


def test_chunk_pcm_exact():
    pcm = b"\x00\x00" * 1600  # 3200 bytes
    chunks = chunk_pcm(pcm, chunk_bytes=3200)
    assert len(chunks) == 1
    assert len(chunks[0]) == 3200


def test_chunk_pcm_empty():
    chunks = chunk_pcm(b"", chunk_bytes=320)
    assert chunks == []


def test_pcm_to_wav():
    pcm = b"\x00\x10" * 100
    wav = pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"
    result = parse_wav_header(wav)
    assert result is not None
    rate, ch, bits, offset = result
    assert rate == TARGET_SAMPLE_RATE
    assert ch == TARGET_CHANNELS
    assert bits == 16
    assert wav[offset:] == pcm


def test_pcm_to_wav_roundtrip():
    """pcm_to_wav → wav_to_pcm_s16le_16k should be identity."""
    pcm = b"\x12\x34" * 500
    wav = pcm_to_wav(pcm)
    result = wav_to_pcm_s16le_16k(wav)
    assert result == pcm


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
