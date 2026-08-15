"""PCM / WAV conversion utilities for the MEOWcaller conversational agent.

Handles conversion between ElevenLabs TTS output (WAV or raw PCM at
various sample rates) and the raw PCM s16le mono 16 kHz format that the
MEOWcaller bridge expects for outbound audio.
"""

from __future__ import annotations

import io
import logging
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Union

LOG = logging.getLogger("meowcaller.audio")

# Target format constants
TARGET_SAMPLE_RATE = 16000
TARGET_CHANNELS = 1
TARGET_SAMPLE_WIDTH = 2  # 16-bit = 2 bytes
TARGET_MAX_AMP = 32767.0


def parse_wav_header(data: bytes) -> tuple[int, int, int, int] | None:
    """Parse a WAV header and return (sample_rate, channels, bits_per_sample, data_offset).

    Returns ``None`` if the data does not look like a valid WAV file.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None

    # Find "fmt " chunk
    pos = 12
    sample_rate = channels = bits_per_sample = 0
    data_offset = 0
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", data, pos + 4)[0]
        if chunk_id == b"fmt ":
            audio_fmt = struct.unpack_from("<H", data, pos + 8)[0]
            channels = struct.unpack_from("<H", data, pos + 10)[0]
            sample_rate = struct.unpack_from("<I", data, pos + 12)[0]
            bits_per_sample = struct.unpack_from("<H", data, pos + 22)[0]
        elif chunk_id == b"data":
            data_offset = pos + 8
            break
        pos += 8 + chunk_size
        if chunk_size % 2:
            pos += 1  # padding

    if sample_rate and channels and bits_per_sample and data_offset:
        return sample_rate, channels, bits_per_sample, data_offset
    return None


def wav_to_pcm_s16le_16k(data: bytes) -> bytes:
    """Convert WAV/MP3/raw audio to raw PCM s16le mono 16 kHz.

    ElevenLabs may return ``audio/mpeg`` despite a PCM output request. Detect
    MP3 containers and decode them with ffmpeg before handing bytes to the
    MEOWcaller bridge. Raw PCM is accepted only when no known container header
    is present.
    """
    header = parse_wav_header(data)
    if header is None:
        if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            return _decode_with_ffmpeg(data)
        # Treat as raw PCM s16le mono 16 kHz
        return data

    src_rate, src_channels, bits_per_sample, data_offset = header
    pcm_data = data[data_offset:]

    # Convert to s16le if needed
    if bits_per_sample == 16:
        samples = pcm_data
    elif bits_per_sample == 8:
        # Convert u8 to s16le
        import numpy as np

        arr = np.frombuffer(pcm_data, dtype=np.uint8).astype(np.int16)
        arr = (arr - 128) * 256
        samples = arr.tobytes()
    elif bits_per_sample == 32:
        import numpy as np

        arr = np.frombuffer(pcm_data, dtype=np.int32)
        arr = (arr >> 16).astype(np.int16)
        samples = arr.tobytes()
    else:
        raise ValueError(f"Unsupported bits_per_sample: {bits_per_sample}")

    # Mix to mono if stereo
    if src_channels == 2:
        import numpy as np

        arr = np.frombuffer(samples, dtype=np.int16)
        # Average L+R channels
        left = arr[0::2]
        right = arr[1::2]
        mono = ((left.astype(np.int32) + right.astype(np.int32)) // 2).astype(np.int16)
        samples = mono.tobytes()
    elif src_channels > 2:
        import numpy as np

        arr = np.frombuffer(samples, dtype=np.int16)
        arr = arr.reshape(-1, src_channels)
        mono = arr.mean(axis=1).astype(np.int16)
        samples = mono.tobytes()

    # Resample to 16 kHz if needed
    if src_rate != TARGET_SAMPLE_RATE:
        samples = _resample(samples, src_rate, TARGET_SAMPLE_RATE)

    return samples


def _find_ffmpeg() -> str:
    """Locate ffmpeg binary. Prefers PATH, falls back to known locations.

    The supervisor launches the agent with a minimal PATH that omits
    ~/.local/bin, so a bare "ffmpeg" lookup can fail even though the
    binary exists on this host.
    """
    import shutil

    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        os.path.expanduser("~/.local/bin/ffmpeg"),
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "ffmpeg"  # let subprocess raise a clear OSError


def _decode_with_ffmpeg(data: bytes) -> bytes:
    """Decode a compressed audio container to target raw PCM."""
    try:
        result = subprocess.run(
            [
                _find_ffmpeg(), "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1",
            ],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not decode compressed TTS audio: {exc}") from exc
    if not result.stdout:
        raise ValueError("compressed TTS decoder returned empty PCM")
    return result.stdout


def _resample(pcm_s16le: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of s16le mono PCM."""
    import numpy as np

    arr = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float64)
    if len(arr) == 0:
        return b""

    ratio = dst_rate / src_rate
    new_len = int(len(arr) * ratio)
    if new_len == 0:
        return b""

    indices = np.linspace(0, len(arr) - 1, new_len)
    resampled = np.interp(indices, np.arange(len(arr)), arr)
    return resampled.astype(np.int16).tobytes()


def strip_wav_header(data: bytes) -> bytes:
    """If *data* starts with a WAV header, return only the raw PCM payload."""
    header = parse_wav_header(data)
    if header is None:
        return data
    return data[header[3] :]


def pcm_to_wav(pcm_s16le_16k: bytes) -> bytes:
    """Wrap raw PCM s16le mono 16 kHz bytes in a minimal WAV header."""
    data_size = len(pcm_s16le_16k)
    # WAV header = 44 bytes: RIFF(4) + Size(4) + WAVE(4) + fmt(4) + FmtSize(4)
    #   + AudioFmt(2) + Channels(2) + SampleRate(4) + ByteRate(4)
    #   + BlockAlign(2) + BitsPerSample(2) + data(4) + DataSize(4)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,                                                      # PCM format code
        1,                                                       # PCM format (1 = PCM)
        TARGET_CHANNELS,                                         # num channels
        TARGET_SAMPLE_RATE,                                      # sample rate
        TARGET_SAMPLE_RATE * TARGET_CHANNELS * TARGET_SAMPLE_WIDTH,  # byte rate
        TARGET_CHANNELS * TARGET_SAMPLE_WIDTH,                   # block align
        TARGET_SAMPLE_WIDTH * 8,                                 # bits per sample
        b"data",
        data_size,
    )
    return header + pcm_s16le_16k


def chunk_pcm(pcm: bytes, chunk_bytes: int = 3200) -> list[bytes]:
    """Split raw PCM bytes into fixed-size chunks (default 100 ms at 16 kHz s16le)."""
    return [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
