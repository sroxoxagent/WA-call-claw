"""Tests for MEOWcaller Voice Context Router."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from voice_context_router import (
    CallerIdentity,
    IdentityMapping,
    RouterConfig,
    VoiceContext,
    VoiceContextRouter,
    build_known_context,
    build_restricted_context,
    extract_lid_from_string,
    extract_phone_from_jid,
    is_group_jid,
    is_memory_file_allowlisted,
    normalize_phone,
    select_session_memory_path,
)


# ---------------------------------------------------------------------------
# normalize_phone tests
# ---------------------------------------------------------------------------

class TestNormalizePhone:
    def test_indonesian_local(self):
        assert normalize_phone("081234567890") == "+6281234567890"

    def test_indonesian_with_country_code(self):
        assert normalize_phone("6281234567890") == "+6281234567890"

    def test_already_e164(self):
        assert normalize_phone("+6281234567890") == "+6281234567890"

    def test_with_spaces(self):
        assert normalize_phone("+62 812-345-6789") == "+628123456789"

    def test_with_parens(self):
        assert normalize_phone("(021) 555-1234") == "+62215551234"

    def test_empty(self):
        assert normalize_phone("") == ""

    def test_whitespace_only(self):
        assert normalize_phone("   ") == ""

    def test_us_number(self):
        assert normalize_phone("12025551234") == "+12025551234"


# ---------------------------------------------------------------------------
# extract_phone_from_jid tests
# ---------------------------------------------------------------------------

class TestExtractPhoneFromJid:
    def test_phone_jid(self):
        assert extract_phone_from_jid("6281234567890@s.whatsapp.net") == "+6281234567890"

    def test_phone_jid_local_format(self):
        assert extract_phone_from_jid("081234567890@s.whatsapp.net") == "+6281234567890"

    def test_group_jid_returns_none(self):
        assert extract_phone_from_jid("120363304009533937@g.us") is None

    def test_lid_returns_none(self):
        assert extract_phone_from_jid("999999999999999@lid") is None

    def test_empty_returns_none(self):
        assert extract_phone_from_jid("") is None


# ---------------------------------------------------------------------------
# extract_lid_from_string tests
# ---------------------------------------------------------------------------

class TestExtractLidFromString:
    def test_valid_lid(self):
        assert extract_lid_from_string("999999999999999@lid") == "999999999999999"

    def test_non_lid(self):
        assert extract_lid_from_string("6281234567890@s.whatsapp.net") is None

    def test_empty(self):
        assert extract_lid_from_string("") is None


# ---------------------------------------------------------------------------
# is_group_jid tests
# ---------------------------------------------------------------------------

class TestIsGroupJid:
    def test_group_jid(self):
        assert is_group_jid("120363304009533937@g.us") is True

    def test_phone_jid(self):
        assert is_group_jid("6281234567890@s.whatsapp.net") is False

    def test_lid(self):
        assert is_group_jid("999999999999999@lid") is False


# ---------------------------------------------------------------------------
# IdentityMapping tests
# ---------------------------------------------------------------------------

class TestIdentityMapping:
    def test_add_and_resolve_jid(self):
        mapping = IdentityMapping()
        mapping.add_jid_mapping("6281234567890@s.whatsapp.net", "+6281234567890")
        assert mapping.resolve_jid("6281234567890@s.whatsapp.net") == "+6281234567890"

    def test_jid_resolve_fallback_to_extraction(self):
        mapping = IdentityMapping()
        # No explicit mapping, but JID contains a phone
        assert mapping.resolve_jid("6289999999999@s.whatsapp.net") == "+6289999999999"

    def test_add_and_resolve_lid(self):
        mapping = IdentityMapping()
        mapping.add_lid_mapping("999999999999999@lid", "+6281234567890")
        assert mapping.resolve_lid("999999999999999@lid") == "+6281234567890"

    def test_lid_no_mapping_returns_none(self):
        mapping = IdentityMapping()
        assert mapping.resolve_lid("unknown@lid") is None

    def test_load_from_config(self):
        mapping = IdentityMapping()
        config = {
            "identity_mappings": {
                "jid": {
                    "6281111111111@s.whatsapp.net": "+6281111111111",
                },
                "lid": {
                    "9999999999@lid": "+6281111111111",
                },
            }
        }
        mapping.load_from_config(config)
        assert mapping.resolve_jid("6281111111111@s.whatsapp.net") == "+6281111111111"
        assert mapping.resolve_lid("9999999999@lid") == "+6281111111111"

    def test_phone_normalization_in_mapping(self):
        mapping = IdentityMapping()
        mapping.add_jid_mapping("6281234567890@s.whatsapp.net", "081234567890")
        # Should normalize to E.164
        assert mapping.resolve_jid("6281234567890@s.whatsapp.net") == "+6281234567890"


# ---------------------------------------------------------------------------
# select_session_memory_path tests
# ---------------------------------------------------------------------------

class TestSelectSessionMemoryPath:
    def test_known_phone(self):
        path = select_session_memory_path("+6281234567890", "call-001")
        assert path is not None
        assert "chat-whatsapp-direct-+6281234567890.memory.md" in path

    def test_custom_base_dir(self):
        path = select_session_memory_path(
            "+6281234567890", "call-001", base_dir="/tmp/memories"
        )
        assert path.startswith("/tmp/memories/")
        assert "chat-whatsapp-direct-+6281234567890.memory.md" in path

    def test_unknown_phone_returns_none(self):
        assert select_session_memory_path(None, "call-001") is None

    def test_empty_phone_returns_none(self):
        assert select_session_memory_path("", "call-001") is None


# ---------------------------------------------------------------------------
# is_memory_file_allowlisted tests
# ---------------------------------------------------------------------------

class TestIsMemoryFileAllowlisted:
    def test_no_allowlist_allows_all(self):
        assert is_memory_file_allowlisted("/any/path/file.md", None) is True

    def test_empty_allowlist_allows_all(self):
        assert is_memory_file_allowlisted("/any/path/file.md", []) is True

    def test_wildcard_allows_all(self):
        assert is_memory_file_allowlisted("/any/path/file.md", ["*"]) is True

    def test_exact_match(self):
        path = "/memories/chat-whatsapp-direct-+6281234567890.memory.md"
        assert is_memory_file_allowlisted(path, ["chat-whatsapp-direct-+6281234567890.memory.md"]) is True

    def test_extension_match(self):
        path = "/memories/chat-whatsapp-direct-+6281234567890.memory.md"
        assert is_memory_file_allowlisted(path, ["*.memory.md"]) is True

    def test_prefix_match(self):
        path = "/memories/chat-whatsapp-direct-+6281234567890.memory.md"
        assert is_memory_file_allowlisted(path, ["chat-whatsapp-direct-*"]) is True

    def test_no_match(self):
        path = "/memories/other-file.txt"
        assert is_memory_file_allowlisted(path, ["*.memory.md"]) is False


# ---------------------------------------------------------------------------
# build_restricted_context tests
# ---------------------------------------------------------------------------

class TestBuildRestrictedContext:
    def test_returns_restricted(self):
        ctx = build_restricted_context("call-001")
        assert ctx["caller_known"] is False
        assert ctx["caller_name"] is None
        assert ctx["conversation_history"] == []
        assert ctx["restrictions"]["no_historical_chat"] is True
        assert ctx["restrictions"]["no_identity_guess"] is True

    def test_max_response_length_is_short(self):
        ctx = build_restricted_context("call-001")
        assert ctx["restrictions"]["max_response_length"] == 200


# ---------------------------------------------------------------------------
# build_known_context tests
# ---------------------------------------------------------------------------

class TestBuildKnownContext:
    def test_returns_known(self):
        ctx = build_known_context("+6281234567890", "call-001")
        assert ctx["caller_known"] is True
        assert ctx["caller_phone"] == "+6281234567890"
        assert ctx["restrictions"]["no_historical_chat"] is True
        assert ctx["restrictions"]["no_identity_guess"] is True

    def test_max_response_length_is_longer(self):
        ctx = build_known_context("+6281234567890", "call-001")
        assert ctx["restrictions"]["max_response_length"] == 500


# ---------------------------------------------------------------------------
# VoiceContextRouter tests
# ---------------------------------------------------------------------------

class TestVoiceContextRouter:
    def test_resolve_known_jid(self):
        config = RouterConfig(
            identity_mappings={
                "jid": {"6281234567890@s.whatsapp.net": "+6281234567890"}
            }
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-001",
            raw_jid="6281234567890@s.whatsapp.net",
        )

        assert ctx.identity.is_known is True
        assert ctx.identity.is_mapped is True
        assert ctx.identity.canonical_phone == "+6281234567890"
        assert ctx.identity.raw_jid == "6281234567890@s.whatsapp.net"
        assert ctx.is_restricted is False
        assert ctx.memory_path is not None
        assert "+6281234567890" in ctx.memory_path

    def test_resolve_unmapped_phone_jid(self):
        config = RouterConfig()
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-002",
            raw_jid="6289999999999@s.whatsapp.net",
        )

        # Phone-based JID should still resolve to a phone
        assert ctx.identity.is_known is True
        assert ctx.identity.is_mapped is False
        assert ctx.identity.canonical_phone == "+6289999999999"
        assert ctx.is_restricted is False

    def test_resolve_lid_with_mapping(self):
        config = RouterConfig(
            identity_mappings={
                "lid": {"999999999999999@lid": "+6281234567890"}
            }
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-003",
            raw_lid="999999999999999@lid",
        )

        assert ctx.identity.is_known is True
        assert ctx.identity.is_mapped is True
        assert ctx.identity.canonical_phone == "+6281234567890"
        assert ctx.identity.raw_lid == "999999999999999@lid"
        assert ctx.is_restricted is False

    def test_resolve_lid_without_mapping_returns_restricted(self):
        config = RouterConfig()
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-004",
            raw_lid="unknown_lid@lid",
        )

        assert ctx.identity.is_known is False
        assert ctx.identity.canonical_phone is None
        assert ctx.is_restricted is True
        assert ctx.memory_path is None
        assert ctx.context_data["restrictions"]["no_identity_guess"] is True

    def test_resolve_no_identifiers_returns_restricted(self):
        config = RouterConfig()
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(call_id="call-005")

        assert ctx.identity.is_known is False
        assert ctx.identity.canonical_phone is None
        assert ctx.is_restricted is True
        assert ctx.memory_path is None

    def test_resolve_group_jid_returns_restricted(self):
        config = RouterConfig()
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-006",
            raw_jid="120363304009533937@g.us",
        )

        # Group JIDs should not resolve to a caller identity
        assert ctx.identity.is_known is False
        assert ctx.is_restricted is True

    def test_allowlist_blocks_memory_path(self):
        config = RouterConfig(
            memory_allowlist=["chat-whatsapp-direct-+6281234567890.memory.md"],
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-007",
            raw_jid="6289999999999@s.whatsapp.net",
        )

        # Phone resolves, but memory file is not in allowlist
        assert ctx.identity.canonical_phone == "+6289999999999"
        assert ctx.memory_path is None  # Blocked by allowlist

    def test_allowlist_permits_matching_pattern(self):
        config = RouterConfig(
            memory_allowlist=["*.memory.md"],
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-008",
            raw_jid="6281234567890@s.whatsapp.net",
        )

        assert ctx.memory_path is not None
        assert ctx.memory_path.endswith(".memory.md")

    def test_custom_memory_base_dir(self):
        config = RouterConfig(
            memory_base_dir="/custom/memories",
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="call-009",
            raw_jid="6281234567890@s.whatsapp.net",
        )

        assert ctx.memory_path.startswith("/custom/memories/")
        assert "chat-whatsapp-direct-+6281234567890.memory.md" in ctx.memory_path


# ---------------------------------------------------------------------------
# CallerIdentity data model tests
# ---------------------------------------------------------------------------

class TestCallerIdentity:
    def test_frozen(self):
        identity = CallerIdentity(call_id="test")
        with pytest.raises(AttributeError):
            identity.call_id = "changed"  # type: ignore[misc]

    def test_defaults(self):
        identity = CallerIdentity(call_id="test")
        assert identity.raw_jid is None
        assert identity.raw_lid is None
        assert identity.canonical_phone is None
        assert identity.is_known is False
        assert identity.is_mapped is False


# ---------------------------------------------------------------------------
# VoiceContext data model tests
# ---------------------------------------------------------------------------

class TestVoiceContext:
    def test_defaults(self):
        identity = CallerIdentity(call_id="test")
        ctx = VoiceContext(identity=identity)
        assert ctx.memory_path is None
        assert ctx.context_data == {}
        assert ctx.is_restricted is True


# ---------------------------------------------------------------------------
# Integration: full routing lifecycle
# ---------------------------------------------------------------------------

class TestRoutingLifecycle:
    """Integration tests simulating real call scenarios."""

    def test_full_known_caller_flow(self):
        """Simulate a call from a known caller with JID mapping."""
        config = RouterConfig(
            memory_base_dir="/sessions/memories",
            memory_allowlist=["*.memory.md"],
            identity_mappings={
                "jid": {"6281234567890@s.whatsapp.net": "+6281234567890"},
                "lid": {"999999999999999@lid": "+6281234567890"},
            },
        )
        router = VoiceContextRouter(config)

        ctx = router.resolve_context(
            call_id="lifecycle-001",
            raw_jid="6281234567890@s.whatsapp.net",
            raw_lid="999999999999999@lid",
        )

        # Identity
        assert ctx.identity.call_id == "lifecycle-001"
        assert ctx.identity.raw_jid == "6281234567890@s.whatsapp.net"
        assert ctx.identity.raw_lid == "999999999999999@lid"
        assert ctx.identity.canonical_phone == "+6281234567890"
        assert ctx.identity.is_known is True
        assert ctx.identity.is_mapped is True

        # Memory path
        assert ctx.memory_path is not None
        assert "chat-whatsapp-direct-+6281234567890.memory.md" in ctx.memory_path

        # Context
        assert ctx.is_restricted is False
        assert ctx.context_data["caller_known"] is True
        assert ctx.context_data["caller_phone"] == "+6281234567890"
        assert ctx.context_data["restrictions"]["no_historical_chat"] is True

    def test_full_unknown_caller_flow(self):
        """Simulate a call from an unknown LID without mapping."""
        config = RouterConfig(
            memory_base_dir="/sessions/memories",
        )
        router = VoiceContextRouter(config)

        ctx = router.resolve_context(
            call_id="lifecycle-002",
            raw_lid="unknown_lid_12345@lid",
        )

        # Identity
        assert ctx.identity.call_id == "lifecycle-002"
        assert ctx.identity.raw_lid == "unknown_lid_12345@lid"
        assert ctx.identity.canonical_phone is None
        assert ctx.identity.is_known is False

        # No memory path for unknown
        assert ctx.memory_path is None

        # Restricted context
        assert ctx.is_restricted is True
        assert ctx.context_data["caller_known"] is False
        assert ctx.context_data["restrictions"]["no_identity_guess"] is True
        assert ctx.context_data["restrictions"]["max_response_length"] == 200

    def test_phone_based_jid_auto_resolves(self):
        """A phone-based JID should auto-resolve without explicit mapping."""
        config = RouterConfig()
        router = VoiceContextRouter(config)

        ctx = router.resolve_context(
            call_id="lifecycle-003",
            raw_jid="6287777777777@s.whatsapp.net",
        )

        assert ctx.identity.canonical_phone == "+6287777777777"
        assert ctx.identity.is_known is True
        assert ctx.is_restricted is False
