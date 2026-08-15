"""ElevenLabs Text-to-Speech client with streaming support.

Uses the ElevenLabs v1 REST API (``/v1/text-to-speech/{voice_id}/stream``)
with PCM output format.  No API keys are stored in this file.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

import httpx

LOG = logging.getLogger("meowcaller.tts")


class TTSError(RuntimeError):
    """TTS request error."""


class ElevenLabsTTSClient:
    """Streaming TTS via ElevenLabs REST API.

    Default voice is ``Kira`` (gmnazjXOFoOcWA59sd5m), matching the
    existing Indonesian WAV announcement. The response is requested as
    ``pcm_16000hz_mono_s16le`` so the caller receives raw little-endian
    signed-16-bit mono audio at 16 kHz — exactly what the MEOWcaller bridge expects.
    """

    DEFAULT_VOICE_ID = "gmnazjXOFoOcWA59sd5m"  # Kira (Indonesian announcement voice)

    def __init__(
        self,
        api_key: str = "",
        voice_id: str = DEFAULT_VOICE_ID,
        *,
        model_id: str = "eleven_flash_v2_5",
        output_format: str = "pcm_16000hz_mono_s16le",
        timeout: float = 30.0,
        stability: float = 0.5,
        similarity_boost: float = 0.5,
        style: float = 0.5,
        use_speaker_boost: bool = True,
        speed: float = 0.78,
    ) -> None:
        if not api_key:
            raise TTSError("TTS API key must be provided")
        self.api_key = api_key
        self.voice_id = voice_id
        self.model_id = model_id
        self.output_format = output_format
        self.timeout = timeout
        self.stability = stability
        self.similarity_boost = similarity_boost
        self.style = style
        self.use_speaker_boost = use_speaker_boost
        self.speed = speed

    @property
    def _endpoint(self) -> str:
        return (
            f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "xi-api-key": self.api_key,
            "Accept": "audio/wav",
        }

    async def synthesize_streaming(self, text: str) -> AsyncIterator[bytes]:
        """Yield raw audio bytes (chunks of WAV/PCM data) for *text*.

        When ``output_format`` is ``pcm_*`` the API returns raw PCM
        bytes directly; otherwise it returns a WAV container.
        """
        if not text.strip():
            return

        payload = {
            "text": text,
            "model_id": self.model_id,
            "output_format": self.output_format,
            "voice_settings": {
                "stability": self.stability,
                "similarity_boost": self.similarity_boost,
                "style": self.style,
                "use_speaker_boost": self.use_speaker_boost,
                "speed": self.speed,
            },
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                async with client.stream(
                    "POST",
                    self._endpoint,
                    headers=self._headers,
                    json=payload,
                ) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(chunk_size=4096):
                        if chunk:
                            yield chunk
            except httpx.HTTPStatusError as exc:
                raise TTSError(
                    f"TTS HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                ) from exc
            except httpx.RequestError as exc:
                raise TTSError(f"TTS request error: {exc}") from exc

    async def synthesize(self, text: str) -> bytes:
        """Non-streaming convenience: return all audio bytes."""
        parts: list[bytes] = []
        async for chunk in self.synthesize_streaming(text):
            parts.append(chunk)
        return b"".join(parts)


def build_tts_client_from_env() -> ElevenLabsTTSClient:
    """Construct a client from ``ELEVENLABS_API_KEY`` and optional env vars.

    Voice settings match the existing MEOWcaller WAV announcement config:
    Kira voice (gmnazjXOFoOcWA59sd5m), speed 0.78, stability 0.5, etc.
    """
    import os

    return ElevenLabsTTSClient(
        api_key=os.getenv("ELEVENLABS_API_KEY", ""),
        voice_id=os.getenv("ELEVENLABS_TTS_VOICE_ID", ElevenLabsTTSClient.DEFAULT_VOICE_ID),
        model_id=os.getenv("ELEVENLABS_TTS_MODEL", "eleven_multilingual_v2"),
        speed=float(os.getenv("ELEVENLABS_TTS_SPEED", "0.78")),
        stability=float(os.getenv("ELEVENLABS_TTS_STABILITY", "0.5")),
        similarity_boost=float(os.getenv("ELEVENLABS_TTS_SIMILARITY_BOOST", "0.5")),
        style=float(os.getenv("ELEVENLABS_TTS_STYLE", "0.5")),
    )
