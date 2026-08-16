"""Conversation recorder — records BOTH sides of a WhatsApp call.

The bridge only records the caller's audio (``incoming-<caller>.wav``). This
recorder lives in the voice agent, which sees both streams:

  - caller audio: every inbound PCM frame received from the bridge
    (forwarded to STT in ``_forward_audio``)
  - agent audio: every TTS PCM frame sent to the bridge
    (streamed in ``_stream_pcm_frames``)

Both streams are 16 kHz mono s16le PCM (1920 bytes = 960 samples = 60 ms per
frame). While the call runs, each stream is appended to its own raw track
file; at call end the two tracks are mixed into a single WAV:

    <recordings_dir>/conversation-<call_id>.wav

Mixing is ``(a + b) / 2`` (no clipping possible), sample-aligned from the
start of the call; whichever track is shorter is zero-padded (silence) to
the longer one. Track lengths differ naturally: the agent may still be
talking when the caller hangs up, or vice versa.

Failure policy: recording must NEVER break the call pipeline. Public methods
swallow errors and log them; the agent disables the recorder on the first
write error (sets it to None). If the agent crashes before ``finish()``, the
raw ``caller-<call_id>.pcm`` / ``agent-<call_id>.pcm`` tracks remain on disk
and can be mixed manually.
"""

import array
import logging
import os
import struct

LOG = logging.getLogger("meowcaller.conversation_recorder")

# Standard 44-byte RIFF/WAVE header for PCM.
_RIFF = struct.Struct("<4sI4s4sIHHIIHH4sI")


def wav_header(data_size: int, sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """Build a canonical 44-byte WAV header for `data_size` bytes of PCM."""
    block_align = channels * bits // 8
    byte_rate = sample_rate * block_align
    return _RIFF.pack(
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,  # audio format: PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )


def mix_tracks(caller: bytes, agent: bytes, chunk: int = 4096) -> bytes:
    """Mix two s16le PCM tracks sample-aligned: ``(a + b) / 2``.

    The shorter track is zero-padded (silence) to the longer one. Returns
    s16le PCM with length ``max(len(a), len(b))``. Both inputs must have
    even byte lengths (full 16-bit samples).
    """
    if len(caller) % 2 or len(agent) % 2:
        raise ValueError("PCM tracks must have even byte lengths")
    a = array.array("h")
    a.frombytes(caller)
    b = array.array("h")
    b.frombytes(agent)
    if len(a) < len(b):
        a.extend(array.array("h", [0]) * (len(b) - len(a)))
    elif len(b) < len(a):
        b.extend(array.array("h", [0]) * (len(a) - len(b)))
    out = array.array("h")
    n = len(a)
    for i in range(0, n, chunk):
        end = min(i + chunk, n)
        out.extend(
            array.array("h", (int((x + y) / 2) for x, y in zip(a[i:end], b[i:end])))
        )
    return out.tobytes()


class ConversationRecorder:
    """Streams both sides of a call to raw tracks, mixes to WAV at finish.

    Usage: ``start(call_id, out_dir)`` → ``write_caller(pcm)`` /
    ``write_agent(pcm)`` during the call → ``finish()`` at call end.
    ``finish()`` is idempotent and safe to call from any thread.
    """

    def __init__(self) -> None:
        self.call_id: str | None = None
        self._caller_path: str | None = None
        self._agent_path: str | None = None
        self._caller_fh = None
        self._agent_fh = None
        self._caller_bytes = 0
        self._agent_bytes = 0
        self._finished = False
        self.last_path: str | None = None

    def start(self, call_id: str, out_dir: str) -> None:
        """Open raw track files for a new call. Closes any previous session."""
        self.finish()
        os.makedirs(out_dir, exist_ok=True)
        self.call_id = call_id
        self._caller_path = os.path.join(out_dir, f"caller-{call_id}.pcm")
        self._agent_path = os.path.join(out_dir, f"agent-{call_id}.pcm")
        self._caller_fh = open(self._caller_path, "wb")
        self._agent_fh = open(self._agent_path, "wb")
        self._caller_bytes = 0
        self._agent_bytes = 0
        self._finished = False
        LOG.info("conversation recording started: call=%s", call_id)

    def write_caller(self, pcm: bytes) -> None:
        """Append one inbound (caller) PCM frame to the caller track."""
        if self._caller_fh is None or self._finished:
            return
        self._caller_fh.write(pcm)
        self._caller_bytes += len(pcm)

    def write_agent(self, pcm: bytes) -> None:
        """Append one outbound (agent TTS) PCM frame to the agent track."""
        if self._agent_fh is None or self._finished:
            return
        self._agent_fh.write(pcm)
        self._agent_bytes += len(pcm)

    def finish(self) -> str | None:
        """Close the tracks and mix them into ``conversation-<call_id>.wav``.

        Returns the WAV path, or None when there is nothing to save (no
        session, or both tracks are empty). Idempotent. Raw track files are
        removed after a successful mix.
        """
        if self._finished:
            return self.last_path
        self._finished = True

        cpath, apath = self._caller_path, self._agent_path
        cbytes, abytes = self._caller_bytes, self._agent_bytes
        call_id = self.call_id
        for fh in (self._caller_fh, self._agent_fh):
            if fh is not None:
                try:
                    fh.close()
                except OSError:
                    pass
        self._caller_fh = self._agent_fh = None

        if call_id is None or (cbytes == 0 and abytes == 0):
            self._cleanup_tracks(cpath, apath)
            return None

        try:
            with open(cpath, "rb") as fh:
                caller = fh.read()
            with open(apath, "rb") as fh:
                agent = fh.read()
        except OSError as exc:
            LOG.warning("conversation recording: read track failed: %s", exc)
            self._cleanup_tracks(cpath, apath)
            return None

        try:
            mixed = mix_tracks(caller, agent)
        except ValueError as exc:
            LOG.warning("conversation recording: mix failed: %s", exc)
            self._cleanup_tracks(cpath, apath)
            return None

        out_path = os.path.join(os.path.dirname(cpath), f"conversation-{call_id}.wav")
        try:
            with open(out_path, "wb") as fh:
                fh.write(wav_header(len(mixed)))
                fh.write(mixed)
        except OSError as exc:
            LOG.warning("conversation recording: write WAV failed: %s", exc)
            self._cleanup_tracks(cpath, apath)
            return None

        self._cleanup_tracks(cpath, apath)
        self.last_path = out_path
        LOG.info(
            "conversation recording saved: %s (caller=%d bytes, agent=%d bytes)",
            out_path,
            cbytes,
            abytes,
        )
        return out_path

    @staticmethod
    def _cleanup_tracks(cpath: str | None, apath: str | None) -> None:
        for p in (cpath, apath):
            if not p:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
