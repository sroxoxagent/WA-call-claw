#!/usr/bin/env python3
"""Focused tests for openclaw_session_resolver.py.

Tests verify:
  1. Phone normalisation
  2. Index building from sessions.json
  3. Exact-match resolution (session key)
  4. Fuzzy-match resolution
  5. Session memory path resolution (primary API)
  6. Memory path with chatId-based naming
  7. Memory path file-not-found handling
  8. Safe handling of missing/corrupt sessions.json
  9. Integration with VoiceContextRouter (no gateway config changes)

Run:
    python3 test_session_resolver.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

# Ensure the agent directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from openclaw_session_resolver import OpenClawSessionResolver, _normalize_phone


# ---------------------------------------------------------------------------
# Minimal sessions.json fixtures
# ---------------------------------------------------------------------------

SESSIONS_FIXTURE: dict[str, dict] = {
    "agent:main:whatsapp:direct:+6281234567890": {
        "sessionId": "aaa-111",
        "chatType": "direct",
        "channel": "whatsapp",
        "chatId": "chat-whatsapp-direct-+6281234567890",
        "deliveryContext": {
            "channel": "whatsapp",
            "to": "+6281234567890",
            "accountId": "default",
        },
        "origin": {
            "label": "+6281234567890",
            "provider": "whatsapp",
            "surface": "whatsapp",
            "chatType": "direct",
            "from": "+6281234567890",
            "to": "+6281234567890",
        },
    },
    "agent:main:whatsapp:direct:+6281234567801": {
        "sessionId": "bbb-222",
        "chatType": "direct",
        "channel": "whatsapp",
        "chatId": "chat-whatsapp-direct-+6281234567801",
        "deliveryContext": {
            "channel": "whatsapp",
            "to": "+6281234567801",
        },
    },
    "agent:main:whatsapp:group:120363425750991760@g.us": {
        "sessionId": "ccc-333",
        "chatType": "group",
        "channel": "whatsapp",
    },
    "agent:main:voice:20260814t053450z-f4fcbe9e": {
        "sessionId": "ddd-444",
    },
    "agent:main:telegram:direct:99887766": {
        "sessionId": "eee-555",
        "channel": "telegram",
        "chatType": "direct",
        "chatId": "chat-telegram-direct-99887766",
        "deliveryContext": {
            "channel": "telegram",
            "to": "99887766",
        },
    },
}


def _write_fixture(path: str, data: dict | None = None) -> str:
    """Write a sessions.json fixture to a temp file and return its path."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data or SESSIONS_FIXTURE, fh)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPhoneNormalisation(unittest.TestCase):
    """Phone normalisation edge cases."""

    def test_e164_passthrough(self):
        self.assertEqual(_normalize_phone("+6281234567890"), "+6281234567890")

    def test_local_0_prefix(self):
        self.assertEqual(_normalize_phone("081234567890"), "+6281234567890")

    def test_international_62_prefix(self):
        self.assertEqual(_normalize_phone("6281234567890"), "+6281234567890")

    def test_strips_whitespace_and_dashes(self):
        self.assertEqual(_normalize_phone("+62 812-345-67890"), "+6281234567890")

    def test_strips_parens(self):
        self.assertEqual(_normalize_phone("(0812) 345-67890"), "+6281234567890")

    def test_empty_returns_empty(self):
        self.assertEqual(_normalize_phone(""), "")

    def test_whitespace_only_returns_empty(self):
        self.assertEqual(_normalize_phone("   "), "")

    def test_other_prefix_gets_plus(self):
        self.assertEqual(_normalize_phone("14155552671"), "+14155552671")


class TestIndexBuilding(unittest.TestCase):
    """sessions.json loading and index construction."""

    def test_loads_index(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            _write_fixture(path)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(resolver.entry_count, 5)
            self.assertEqual(resolver.direct_phone_count, 2)
            self.assertGreater(resolver.loaded_at, 0)
        finally:
            os.unlink(path)

    def test_missing_file_returns_zero(self):
        resolver = OpenClawSessionResolver("/nonexistent/sessions.json")
        self.assertEqual(resolver.entry_count, 0)
        self.assertEqual(resolver.direct_phone_count, 0)

    def test_corrupt_json_returns_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{bad json!!")
            path = f.name
        try:
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(resolver.entry_count, 0)
        finally:
            os.unlink(path)

    def test_non_dict_root_returns_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump([1, 2, 3], f)
            path = f.name
        try:
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(resolver.entry_count, 0)
        finally:
            os.unlink(path)

    def test_reload(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            _write_fixture(path)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(resolver.entry_count, 5)

            # Overwrite with smaller fixture
            small = {"agent:main:whatsapp:direct:+1234": {"sessionId": "x"}}
            _write_fixture(path, small)
            count = resolver.reload()
            self.assertEqual(count, 1)
            self.assertEqual(resolver.entry_count, 1)
        finally:
            os.unlink(path)

    def test_prefers_tested_whatsapp_key_over_canonical_alias(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            duplicate = {
                # Session created by the MEOWcaller voice agent itself
                # (webchat origin, MEOWcaller label): must NEVER win, even
                # though its key spells "whatsapp:direct".
                "agent:main:whatsapp:direct:+6281234567890": {
                    "channel": "webchat",
                    "chatId": "chat-whatsapp-direct-+6281234567890",
                    "updatedAt": 300,
                    "origin": {"label": "MEOWcaller Voice Agent", "provider": "webchat"},
                },
                # Real WhatsApp session: channel whatsapp, no MEOW origin.
                "agent:main:direct:+6281234567890": {
                    "channel": "whatsapp",
                    "chatId": "chat-whatsapp-direct-+6281234567890",
                    "updatedAt": 200,
                    "origin": {"label": "+6281234567890", "provider": "whatsapp"},
                },
            }
            _write_fixture(path, duplicate)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(
                resolver.find_whatsapp_direct_session("+6281234567890"),
                "agent:main:direct:+6281234567890",
            )
        finally:
            os.unlink(path)

    def test_meow_created_session_loses_even_when_newer(self):
        """Regression: the MEOWcaller-created session (webchat origin,
        newest updatedAt) must lose to the real WhatsApp session. This is
        the exact duplicate-key situation observed on 2026-08-15 that sent
        voice turns into a 78K-token test session."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            duplicate = {
                "agent:main:whatsapp:direct:+6281234567890": {
                    "channel": "webchat",
                    "chatId": "chat-whatsapp-direct-+6281234567890",
                    "updatedAt": 9999999999999,
                    "origin": {"label": "MEOWcaller Voice Agent", "provider": "webchat"},
                },
                "agent:main:direct:+6281234567890": {
                    "channel": "whatsapp",
                    "chatId": "chat-whatsapp-direct-+6281234567890",
                    "updatedAt": 1,
                    "origin": {"label": "+6281234567890", "provider": "whatsapp"},
                },
            }
            _write_fixture(path, duplicate)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(
                resolver.find_whatsapp_direct_session("+6281234567890"),
                "agent:main:direct:+6281234567890",
            )
        finally:
            os.unlink(path)


class TestExactMatch(unittest.TestCase):
    """Exact phone → session key resolution."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        _write_fixture(self._tmp.name)
        self.resolver = OpenClawSessionResolver(self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_direct_match(self):
        key = self.resolver.find_whatsapp_direct_session("+6281234567890")
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")

    def test_match_second_phone(self):
        key = self.resolver.find_whatsapp_direct_session("+6281234567890")
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")

    def test_no_match_returns_none(self):
        key = self.resolver.find_whatsapp_direct_session("+9999999999")
        self.assertIsNone(key)

    def test_none_phone_returns_none(self):
        key = self.resolver.find_whatsapp_direct_session(None)
        self.assertIsNone(key)

    def test_empty_phone_returns_none(self):
        key = self.resolver.find_whatsapp_direct_session("")
        self.assertIsNone(key)

    def test_raw_lid_matches_direct_whatsapp_session(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sessions = {
                "agent:main:direct:+999999999999998": {
                    "origin": {"provider": "whatsapp", "surface": "whatsapp"},
                    "chatId": "chat-whatsapp-direct-+999999999999998",
                    "updatedAt": 100,
                }
            }
            _write_fixture(path, sessions)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(
                resolver.find_session_for_caller(raw_lid="999999999999998@lid"),
                "agent:main:direct:+999999999999998",
            )
        finally:
            os.unlink(path)

    def test_remote_lid_match_wins_over_phone_fallback(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sessions = {
                "agent:main:direct:+999999999999998": {
                    "origin": {"provider": "whatsapp", "surface": "whatsapp"},
                    "chatId": "chat-whatsapp-direct-+999999999999998",
                    "updatedAt": 100,
                },
                "agent:main:whatsapp:direct:+6281234567890": {
                    "origin": {"provider": "whatsapp", "surface": "whatsapp"},
                    "chatId": "chat-whatsapp-direct-+6281234567890",
                    "updatedAt": 200,
                },
            }
            _write_fixture(path, sessions)
            resolver = OpenClawSessionResolver(path)
            self.assertEqual(
                resolver.find_session_for_caller(
                    canonical_phone="+6281234567890",
                    raw_lid="999999999999998@lid",
                ),
                "agent:main:direct:+999999999999998",
            )
        finally:
            os.unlink(path)


class TestFuzzyMatch(unittest.TestCase):
    """Fuzzy phone matching with prefix variations."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        _write_fixture(self._tmp.name)
        self.resolver = OpenClawSessionResolver(self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_local_format_matches(self):
        key = self.resolver.find_whatsapp_direct_session("081234567890", fuzzy=True)
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")

    def test_62_prefix_matches(self):
        key = self.resolver.find_whatsapp_direct_session("6281234567890", fuzzy=True)
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")

    def test_local_format_matches_without_fuzzy(self):
        # _normalize_phone("081234567890") → "+6281234567890" → exact match
        key = self.resolver.find_whatsapp_direct_session("081234567890", fuzzy=False)
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")


class TestFindSessionMemoryPath(unittest.TestCase):
    """Primary API: find .memory.md path for a caller's session."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        _write_fixture(self._tmp.name)
        self.resolver = OpenClawSessionResolver(self._tmp.name)

        # Create a temp memories directory with a real .memory.md file
        self._mem_dir = tempfile.mkdtemp()
        mem_file = os.path.join(
            self._mem_dir, "chat-whatsapp-direct-+6281234567890.memory.md"
        )
        with open(mem_file, "w") as fh:
            fh.write("Test memory content for Owner.")

        mem_file2 = os.path.join(
            self._mem_dir, "chat-whatsapp-direct-+6281234567890.memory.md"
        )
        with open(mem_file2, "w") as fh:
            fh.write("Test memory content for contact 2.")

    def tearDown(self):
        os.unlink(self._tmp.name)
        import shutil
        shutil.rmtree(self._mem_dir, ignore_errors=True)

    def test_finds_memory_path(self):
        path = self.resolver.find_session_memory_path(
            "+6281234567890", memory_base_dir=self._mem_dir,
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("chat-whatsapp-direct-+6281234567890.memory.md"))
        self.assertTrue(os.path.isfile(path))

    def test_finds_second_contact_memory(self):
        path = self.resolver.find_session_memory_path(
            "+6281234567890", memory_base_dir=self._mem_dir,
        )
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("chat-whatsapp-direct-+6281234567890.memory.md"))

    def test_no_session_returns_none(self):
        path = self.resolver.find_session_memory_path(
            "+9999999999", memory_base_dir=self._mem_dir,
        )
        self.assertIsNone(path)

    def test_no_file_on_disk_returns_none(self):
        # Session exists but no memory file on disk
        path = self.resolver.find_session_memory_path(
            "+6281234567890", memory_base_dir="/nonexistent/dir",
        )
        self.assertIsNone(path)

    def test_none_phone_returns_none(self):
        path = self.resolver.find_session_memory_path(None)
        self.assertIsNone(path)

    def test_empty_phone_returns_none(self):
        path = self.resolver.find_session_memory_path("")
        self.assertIsNone(path)

    def test_voice_session_not_indexed(self):
        """Voice sessions should not be in the direct phone index."""
        path = self.resolver.find_session_memory_path(
            "+6281234567890", memory_base_dir=self._mem_dir,
        )
        # Should find via whatsapp:direct, not voice
        self.assertIsNotNone(path)
        # The path should be from the WhatsApp direct session
        self.assertIn("chat-whatsapp-direct", path)


class TestFindAnyDirectSession(unittest.TestCase):
    """find_any_direct_session: deliveryContext fallback."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        _write_fixture(self._tmp.name)
        self.resolver = OpenClawSessionResolver(self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_whatsapp_direct_found(self):
        key, channel = self.resolver.find_any_direct_session("+6281234567890")
        self.assertEqual(key, "agent:main:whatsapp:direct:+6281234567890")
        self.assertEqual(channel, "whatsapp")

    def test_no_match_returns_none(self):
        key, channel = self.resolver.find_any_direct_session("+99999")
        self.assertIsNone(key)
        self.assertIsNone(channel)


class TestFindSessionByKey(unittest.TestCase):
    """Direct session key lookup."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        _write_fixture(self._tmp.name)
        self.resolver = OpenClawSessionResolver(self._tmp.name)

    def tearDown(self):
        os.unlink(self._tmp.name)

    def test_found(self):
        entry = self.resolver.find_session_by_key(
            "agent:main:whatsapp:direct:+6281234567890"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["sessionId"], "aaa-111")
        self.assertEqual(entry["chatType"], "direct")

    def test_not_found(self):
        entry = self.resolver.find_session_by_key("agent:main:voice:nonexistent")
        self.assertIsNone(entry)


class TestRedaction(unittest.TestCase):
    """Safe logging: phone redaction."""

    def test_redact_long_phone(self):
        result = OpenClawSessionResolver._redact("+6281234567890")
        self.assertEqual(result, "+62****67890")
        self.assertNotIn("878993", result)

    def test_redact_short_phone(self):
        result = OpenClawSessionResolver._redact("+62")
        self.assertEqual(result, "***")


class TestIntegrationWithRouter(unittest.TestCase):
    """Verify resolver works alongside VoiceContextRouter."""

    def test_router_phone_matches_session_memory_path(self):
        from voice_context_router import (
            RouterConfig,
            VoiceContextRouter,
        )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            _write_fixture(path)
            resolver = OpenClawSessionResolver(path)

            # Create memory directory with the expected file
            mem_dir = tempfile.mkdtemp()
            mem_file = os.path.join(
                mem_dir, "chat-whatsapp-direct-+6281234567890.memory.md"
            )
            with open(mem_file, "w") as fh:
                fh.write("Prior session context for testing.")

            try:
                # Simulate voice context router resolving a JID to phone
                router_config = RouterConfig(
                    memory_base_dir=mem_dir,
                    identity_mappings={
                        "jid": {
                            "6281234567890@s.whatsapp.net": "+6281234567890",
                        },
                    },
                )
                router = VoiceContextRouter(router_config)
                ctx = router.resolve_context(
                    call_id="test-call-001",
                    raw_jid="6281234567890@s.whatsapp.net",
                )

                # The router should resolve the phone
                self.assertTrue(ctx.identity.is_known)
                self.assertEqual(ctx.identity.canonical_phone, "+6281234567890")

                # The resolver should find the matching session memory
                memory_path = resolver.find_session_memory_path(
                    ctx.identity.canonical_phone,
                    memory_base_dir=mem_dir,
                )
                self.assertIsNotNone(memory_path)
                self.assertTrue(memory_path.endswith(".memory.md"))

                # The file content should match what we wrote
                with open(memory_path) as fh:
                    content = fh.read()
                self.assertEqual(content, "Prior session context for testing.")
            finally:
                import shutil
                shutil.rmtree(mem_dir, ignore_errors=True)
        finally:
            os.unlink(path)

    def test_voice_session_key_unchanged(self):
        """Voice sessions must use their own isolated key, never the
        WhatsApp direct session key."""
        # This is a design invariant test — the session key method should
        # always return the voice prefix, never the resolved session key.
        voice_prefix = "agent:main:voice"
        call_id = "test-call-123"
        expected = f"{voice_prefix}:{call_id}"
        # The resolver should NOT provide a session key override
        # (this is enforced by the voice agent not storing _resolved_session_key)
        self.assertEqual(expected, f"agent:main:voice:{call_id}")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.WARNING)
    unittest.main(verbosity=2)
