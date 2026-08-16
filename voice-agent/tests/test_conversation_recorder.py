"""Tests for conversation_recorder (both-sides call recording)."""

import os
import struct
import tempfile

import pytest

from conversation_recorder import (
    ConversationRecorder,
    mix_tracks,
    wav_header,
)

FRAME = 1920  # 60 ms of 16 kHz mono s16le


def _frame(value: int) -> bytes:
    """One 60 ms frame of constant s16le sample `value`."""
    return struct.pack("<h", value) * (FRAME // 2)


class TestWavHeader:
    def test_header_shape(self):
        h = wav_header(1000)
        assert len(h) == 44
        assert h[:4] == b"RIFF"
        assert h[8:12] == b"WAVE"
        assert h[12:16] == b"fmt "
        # data size field at offset 40
        assert struct.unpack("<I", h[40:44])[0] == 1000

    def test_riff_size_includes_header(self):
        h = wav_header(1000)
        assert struct.unpack("<I", h[4:8])[0] == 36 + 1000


class TestMixTracks:
    def test_mix_averages(self):
        a = _frame(10000)
        b = _frame(2000)
        out = mix_tracks(a, b)
        assert len(out) == FRAME
        assert struct.unpack("<h", out[:2])[0] == 6000  # (10000+2000)/2

    def test_mix_never_clips(self):
        a = _frame(32767)
        b = _frame(32767)
        out = mix_tracks(a, b)
        assert struct.unpack("<h", out[:2])[0] == 32767

    def test_mix_pads_shorter_track_with_silence(self):
        a = _frame(8000) + _frame(8000)  # 2 frames
        b = _frame(8000)                 # 1 frame
        out = mix_tracks(a, b)
        assert len(out) == FRAME * 2
        # second frame: (8000 + 0)/2 = 4000
        assert struct.unpack("<h", out[FRAME:FRAME + 2])[0] == 4000

    def test_mix_empty_agent(self):
        a = _frame(6000)
        out = mix_tracks(a, b"")
        assert len(out) == FRAME
        assert struct.unpack("<h", out[:2])[0] == 3000

    def test_mix_odd_length_rejected(self):
        with pytest.raises(ValueError):
            mix_tracks(b"\x00", b"")


class TestConversationRecorder:
    def test_full_cycle(self, tmp_path: str):
        rec = ConversationRecorder()
        rec.start("call-1", tmp_path)
        rec.write_caller(_frame(10000) * 2)
        rec.write_agent(_frame(2000) * 1)
        path = rec.finish()
        assert path is not None
        assert os.path.basename(path) == "conversation-call-1.wav"
        with open(path, "rb") as fh:
            data = fh.read()
        assert data[:4] == b"RIFF"
        assert len(data) == 44 + FRAME * 2
        # first frame = (10000+2000)/2, second = (10000+0)/2
        assert struct.unpack("<h", data[44:46])[0] == 6000
        assert struct.unpack("<h", data[44 + FRAME:44 + FRAME + 2])[0] == 5000
        # raw tracks cleaned up
        assert not os.path.exists(os.path.join(tmp_path, "caller-call-1.pcm"))
        assert not os.path.exists(os.path.join(tmp_path, "agent-call-1.pcm"))

    def test_finish_idempotent(self, tmp_path: str):
        rec = ConversationRecorder()
        rec.start("call-2", tmp_path)
        rec.write_caller(_frame(4000))
        p1 = rec.finish()
        p2 = rec.finish()
        assert p1 == p2

    def test_no_data_no_file(self, tmp_path: str):
        rec = ConversationRecorder()
        rec.start("call-3", tmp_path)
        assert rec.finish() is None
        assert not os.listdir(tmp_path)

    def test_writes_after_finish_are_noops(self, tmp_path: str):
        rec = ConversationRecorder()
        rec.start("call-4", tmp_path)
        rec.write_caller(_frame(1000))
        rec.finish()
        rec.write_caller(_frame(1000))
        rec.write_agent(_frame(1000))
        # still only the finished WAV (track bytes unchanged)
        files = os.listdir(tmp_path)
        assert files == ["conversation-call-4.wav"]
        with open(os.path.join(tmp_path, "conversation-call-4.wav"), "rb") as fh:
            data = fh.read()
        assert len(data) == 44 + FRAME  # 1 frame, not 3

    def test_crash_leaves_raw_tracks_for_manual_mix(self, tmp_path: str):
        rec = ConversationRecorder()
        rec.start("call-5", tmp_path)
        rec.write_caller(_frame(1000))
        # simulate crash: never call finish()
        assert os.path.exists(os.path.join(tmp_path, "caller-call-5.pcm"))
        assert os.path.exists(os.path.join(tmp_path, "agent-call-5.pcm"))
