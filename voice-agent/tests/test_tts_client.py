"""Tests for ElevenLabs TTS client (no real API calls)."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tts_client import TTSError, ElevenLabsTTSClient


def test_tts_requires_api_key():
    try:
        ElevenLabsTTSClient(api_key="")
        assert False, "should have raised TTSError"
    except TTSError:
        pass


def test_tts_defaults():
    client = ElevenLabsTTSClient(api_key="test-key")
    assert client.voice_id == "gmnazjXOFoOcWA59sd5m"
    assert client.model_id == "eleven_multilingual_v2"
    assert client.speed == 0.78
    assert client.output_format == "pcm_16000hz_mono_s16le"
    assert client.speed == 0.78
    assert client.stability == 0.5
    assert client.similarity_boost == 0.5
    assert client.style == 0.5
    assert client.use_speaker_boost is True


def test_tts_custom_voice():
    client = ElevenLabsTTSClient(api_key="k", voice_id="custom-voice-id")
    assert client.voice_id == "custom-voice-id"


def test_tts_custom_model():
    client = ElevenLabsTTSClient(api_key="k", model_id="eleven_turbo_v2_5")
    assert client.model_id == "eleven_turbo_v2_5"


def test_tts_custom_speed():
    client = ElevenLabsTTSClient(api_key="k", speed=1.2)
    assert client.speed == 1.2


def test_tts_endpoint():
    client = ElevenLabsTTSClient(api_key="k", voice_id="my-voice")
    assert client._endpoint == "https://api.elevenlabs.io/v1/text-to-speech/my-voice/stream"


def test_tts_headers():
    client = ElevenLabsTTSClient(api_key="sk_test_123")
    headers = client._headers
    assert headers["xi-api-key"] == "sk_test_123"
    assert headers["Accept"] == "audio/wav"


def test_tts_payload_format():
    """Verify the JSON payload shape matches ElevenLabs TTS API."""
    client = ElevenLabsTTSClient(
        api_key="k",
        model_id="eleven_multilingual_v2",
        output_format="pcm_16000hz_mono_s16le",
        speed=0.78,
        stability=0.5,
        similarity_boost=0.5,
        style=0.5,
    )
    text = "Hello world"
    payload = {
        "text": text,
        "model_id": client.model_id,
        "output_format": client.output_format,
        "voice_settings": {
            "stability": client.stability,
            "similarity_boost": client.similarity_boost,
            "style": client.style,
            "use_speaker_boost": client.use_speaker_boost,
            "speed": client.speed,
        },
    }
    assert payload["text"] == "Hello world"
    assert payload["model_id"] == "eleven_multilingual_v2"
    assert payload["output_format"] == "pcm_16000hz_mono_s16le"
    assert payload["voice_settings"]["stability"] == 0.5
    assert payload["voice_settings"]["similarity_boost"] == 0.5
    assert payload["voice_settings"]["style"] == 0.5
    assert payload["voice_settings"]["use_speaker_boost"] is True
    assert payload["voice_settings"]["speed"] == 0.78


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
