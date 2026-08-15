"""MEOWcaller Voice Context Router.

Provides identity resolution and session memory path selection for voice calls.
Preserves raw identifiers (JID, LID, phone) separately and maps them to a
canonical phone number for session memory lookups.

Design constraints:
  - Never loads historical chat data.
  - Unknown/unmapped identities return restricted context (never guess).
  - Config-driven allowlisting for memory files/profiles.
  - No gateway config wiring — config example only.

Usage:
    from voice_context_router import VoiceContextRouter, CallerIdentity

    router = VoiceContextRouter(config)
    ctx = router.resolve_context(call_id="abc-123", raw_jid="6281234567890@s.whatsapp.net")
    # ctx.memory_path, ctx.context_data, ctx.identity
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("meowcaller.voice_context_router")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CallerIdentity:
    """Immutable identity record for a voice call caller.

    All fields are preserved separately — we never merge or discard raw IDs
    even when a mapping exists.
    """

    call_id: str
    raw_jid: str | None = None       # e.g. "6281234567890@s.whatsapp.net"
    raw_lid: str | None = None       # e.g. "128510068797660@lid"
    canonical_phone: str | None = None  # e.g. "+6281234567890"
    is_known: bool = False           # True if identity resolved to a known caller
    is_mapped: bool = False          # True if a JID/LID -> phone mapping exists


@dataclass
class VoiceContext:
    """Context returned by the router for a voice call.

    Only the selected session-memory file may be loaded. Historical chat is
    never included.
    """

    identity: CallerIdentity
    memory_path: str | None = None
    memory_text: str = ""
    context_data: dict[str, Any] = field(default_factory=dict)
    is_restricted: bool = True


# ---------------------------------------------------------------------------
# Identity mapping
# ---------------------------------------------------------------------------

# WhatsApp JID patterns
_PHONE_JID_RE = re.compile(r"^(\d+)@s\.whatsapp\.net$")
_GROUP_JID_RE = re.compile(r"^\d+@g\.us$")
_LID_RE = re.compile(r"^(\d+)@lid$")

# Indonesian phone normalization
_PHONE_STRIP_RE = re.compile(r"[\s\-()]")


def normalize_phone(phone: str) -> str:
    """Normalize a phone number to E.164-like format.

    Examples:
        "081234567890" -> "+6281234567890"
        "+62 812-345-6789" -> "+6281234567890"
        "6281234567890" -> "+6281234567890"
    """
    phone = _PHONE_STRIP_RE.sub("", phone.strip())
    if not phone:
        return ""
    # Already has + prefix
    if phone.startswith("+"):
        return phone
    # Starts with 0 -> Indonesian local, prepend +62
    if phone.startswith("0"):
        return "+62" + phone[1:]
    # Starts with 62 -> Indonesian, prepend +
    if phone.startswith("62"):
        return "+" + phone
    # Other: assume needs +
    return "+" + phone


def extract_phone_from_jid(jid: str) -> str | None:
    """Extract and normalize a phone number from a WhatsApp JID.

    Returns None for group JIDs or LIDs.
    """
    m = _PHONE_JID_RE.match(jid)
    if m:
        raw_phone = m.group(1)
        return normalize_phone(raw_phone)
    return None


def extract_lid_from_string(s: str) -> str | None:
    """Extract the LID prefix from a LID string.

    Returns the numeric part (e.g. "128510068797660") or None.
    """
    m = _LID_RE.match(s)
    if m:
        return m.group(1)
    return None


def is_group_jid(jid: str) -> bool:
    """Check if a JID is a group JID."""
    return bool(_GROUP_JID_RE.match(jid))


# ---------------------------------------------------------------------------
# Identity mapping store
# ---------------------------------------------------------------------------

class IdentityMapping:
    """Maps JIDs/LIDs to canonical phone numbers.

    In production, this would be backed by a database or API call.
    For the POC, it uses an in-memory dict loaded from config.
    """

    def __init__(self) -> None:
        self._jid_to_phone: dict[str, str] = {}
        self._lid_to_phone: dict[str, str] = {}

    def add_jid_mapping(self, jid: str, phone: str) -> None:
        """Register a JID -> canonical phone mapping."""
        self._jid_to_phone[jid] = normalize_phone(phone)

    def add_lid_mapping(self, lid: str, phone: str) -> None:
        """Register a LID -> canonical phone mapping."""
        self._lid_to_phone[lid] = normalize_phone(phone)

    def resolve_jid(self, jid: str) -> str | None:
        """Resolve a JID to a canonical phone number.

        First checks direct mapping, then falls back to extracting phone
        from the JID itself (if it is a phone-based JID).
        """
        # Direct mapping lookup
        if jid in self._jid_to_phone:
            return self._jid_to_phone[jid]

        # Fallback: extract phone from JID
        return extract_phone_from_jid(jid)

    def resolve_lid(self, lid: str) -> str | None:
        """Resolve a LID to a canonical phone number.

        LIDs cannot be resolved to phone numbers without an external mapping
        (e.g., WhatsApp server lookup). Returns None if no mapping exists.
        """
        return self._lid_to_phone.get(lid)

    def load_from_config(self, config: dict[str, Any]) -> None:
        """Load mappings from a config dict.

        Accepts either:
          1. The full config with "identity_mappings" key:
             {"identity_mappings": {"jid": {...}, "lid": {...}}}
          2. The raw mappings dict directly:
             {"jid": {...}, "lid": {...}}
        """
        # Support both wrapped and unwrapped formats
        mappings = config.get("identity_mappings", config)
        for jid, phone in mappings.get("jid", {}).items():
            self.add_jid_mapping(jid, phone)
        for lid, phone in mappings.get("lid", {}).items():
            self.add_lid_mapping(lid, phone)


# ---------------------------------------------------------------------------
# Session memory path selection
# ---------------------------------------------------------------------------

def select_session_memory_path(
    canonical_phone: str | None,
    call_id: str,
    base_dir: str = "agents/main/sessions/memories",
) -> str | None:
    """Select the session memory file path for a caller.

    Returns the path to the .memory.md file for the caller, or None if
    the caller is unknown/unmapped.

    The path follows the OpenClaw session memory naming convention:
        chat-whatsapp-direct-{identifier}.memory.md
    """
    if not canonical_phone:
        return None

    # The established session-memory convention keeps the canonical E.164
    # phone, including the leading '+', in the filename.
    filename = f"chat-whatsapp-direct-{canonical_phone}.memory.md"
    return os.path.join(base_dir, filename)


def is_memory_file_allowlisted(
    memory_path: str,
    allowlist: list[str] | None = None,
) -> bool:
    """Check whether a memory file path is explicitly allowlisted.

    If no allowlist is provided, preserve the existing router behavior and
    allow the selected path. When an allowlist is provided, it is enforced.
    """
    if not allowlist:
        return True
    filename = os.path.basename(memory_path)
    return any(fnmatch.fnmatchcase(filename, pattern) for pattern in allowlist)


# ---------------------------------------------------------------------------
# Event identity extraction
# ---------------------------------------------------------------------------

def extract_caller_ids_from_event(event: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract raw JID and LID from a ``call_started`` bridge event.

    Explicit ``remote_jid``/``remoteJid`` is authoritative when present.
    MEOWcaller's ``caller_id`` is also treated as the remote JID because the
    bridge populates it from the remote peer JID. Dedicated caller fields take
    precedence when supplied. A bare phone number or numeric string is never
    treated as a JID/LID — we never guess a phone from a LID.

    Returns:
        ``(raw_jid, raw_lid)`` — each may be ``None``.
    """
    # --- Remote JID ---
    # MEOWcaller sends the remote peer identifier as caller_id. Some bridge
    # versions expose the same value explicitly as remote_jid.
    remote_jid = event.get("remote_jid") or event.get("remoteJid")
    if not remote_jid:
        caller_id = event.get("caller_id", "")
        if caller_id and isinstance(caller_id, str) and "@" in caller_id:
            remote_jid = caller_id

    # --- JID/LID ---
    # The remote JID is authoritative. Dedicated fields are only fallback
    # fields for bridge versions that do not expose remote_jid explicitly.
    raw_jid: str | None = None
    raw_lid: str | None = None
    if isinstance(remote_jid, str) and "@" in remote_jid:
        if remote_jid.endswith("@lid"):
            raw_lid = remote_jid
        elif "@s.whatsapp.net" in remote_jid or "@g.us" in remote_jid:
            raw_jid = remote_jid

    remote_is_authoritative = isinstance(remote_jid, str) and "@" in remote_jid
    if not remote_is_authoritative:
        raw_jid = (
            event.get("caller_jid")
            or event.get("raw_jid")
            or event.get("jid")
        )
        raw_lid = (
            event.get("caller_lid")
            or event.get("raw_lid")
            or event.get("lid")
        )

    return raw_jid, raw_lid


def extract_canonical_phone_from_event(event: dict[str, Any]) -> str | None:
    """Extract a phone resolved by the WhatsApp layer from a call event.

    The bridge may provide ``remote_phone`` after Whatsmeow resolves the
    remote LID through its authoritative LID store. This value is trusted as
    a phone identifier, unlike the numeric part of a raw LID.
    """
    value = event.get("remote_phone") or event.get("caller_phone") or event.get("e164")
    if not value or not isinstance(value, str):
        return None
    if "@" in value:
        value = value.split("@", 1)[0]
    normalised = normalize_phone(value)
    return normalised or None


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_restricted_context(call_id: str) -> dict[str, Any]:
    """Build a restricted context for unknown/unmapped callers.

    This context provides only the minimum information needed to handle
    the call safely — never guess identity or load external data.
    """
    return {
        "caller_known": False,
        "caller_name": None,
        "conversation_history": [],
        "preferences": {},
        "restrictions": {
            "no_historical_chat": True,
            "no_identity_guess": True,
            "max_response_length": 200,
        },
    }


def build_known_context(
    canonical_phone: str,
    call_id: str,
    allowlisted_files: list[str] | None = None,
) -> dict[str, Any]:
    """Build context metadata for a known caller.

    The actual selected session-memory text is attached separately. No
    historical chat is ever loaded here.
    """
    return {
        "caller_known": True,
        "caller_phone": canonical_phone,
        "caller_name": None,
        "conversation_history": [],
        "restrictions": {
            "no_historical_chat": True,
            "no_identity_guess": True,
            "max_response_length": 500,
        },
    }


def load_selected_session_memory(path: str | None, max_chars: int = 24000) -> str:
    """Load exactly one selected .memory.md file, never a chat transcript."""
    if not path or not path.endswith(".memory.md"):
        return ""
    try:
        return Path(path).read_text(encoding="utf-8")[:max_chars]
    except (OSError, UnicodeError) as exc:
        LOG.warning("session memory unavailable: %s (%s)", path, exc)
        return ""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

@dataclass
class RouterConfig:
    """Configuration for the voice context router."""

    # Base directory for session memory files.
    memory_base_dir: str = "agents/main/sessions/memories"

    # Explicit allowlist is required before any session-memory file is loaded.
    memory_allowlist: list[str] | None = None
    max_memory_chars: int = 24000

    # Identity mappings loaded from config.
    identity_mappings: dict[str, Any] = field(default_factory=dict)

    # Whether to allow callers with no mapping (default: True, but restricted).
    allow_unknown_callers: bool = True

    # Let the caller resolve identity without reading the selected memory file.
    # The OpenClaw voice agent disables this because chat.send loads the session
    # context automatically when given the correct existing session key.
    load_memory: bool = True


class VoiceContextRouter:
    """Routes voice calls to the appropriate context based on caller identity.

    Usage:
        config = RouterConfig(
            memory_base_dir="/path/to/sessions/memories",
            memory_allowlist=["chat-whatsapp-direct-*.memory.md"],
        )
        router = VoiceContextRouter(config)
        ctx = router.resolve_context(
            call_id="abc-123",
            raw_jid="6281234567890@s.whatsapp.net",
        )
    """

    def __init__(self, config: RouterConfig | None = None) -> None:
        self.config = config or RouterConfig()
        self._mapping = IdentityMapping()
        if self.config.identity_mappings:
            self._mapping.load_from_config(self.config.identity_mappings)

    @property
    def mapping(self) -> IdentityMapping:
        """Access the identity mapping store."""
        return self._mapping

    def resolve_context(
        self,
        call_id: str,
        raw_jid: str | None = None,
        raw_lid: str | None = None,
        canonical_phone: str | None = None,
    ) -> VoiceContext:
        """Resolve a voice call to its context.

        Steps:
          1. Build CallerIdentity from raw identifiers.
          2. Map JID/LID to canonical phone.
          3. Select session memory path.
          4. Build appropriate context (restricted or known).

        Args:
            call_id: Unique call identifier.
            raw_jid: Raw WhatsApp JID (e.g. "6281234567890@s.whatsapp.net").
            raw_lid: Raw WhatsApp LID (e.g. "128510068797660@lid").

        Returns:
            VoiceContext with identity, memory path, and context data.
        """
        # Step 1: Build identity
        identity = self._build_identity(call_id, raw_jid, raw_lid)

        # Step 2: Resolve canonical phone. A phone supplied by the WhatsApp
        # layer (after GetPNForLID) is authoritative; never derive one from
        # the numeric part of a raw LID.
        canonical_phone = (
            normalize_phone(canonical_phone)
            if canonical_phone
            else self._resolve_phone(identity)
        )

        # Step 3: Update identity with resolved phone
        identity = CallerIdentity(
            call_id=identity.call_id,
            raw_jid=identity.raw_jid,
            raw_lid=identity.raw_lid,
            canonical_phone=canonical_phone,
            is_known=canonical_phone is not None,
            is_mapped=identity.is_mapped or bool(canonical_phone),
        )

        # Step 4: Select memory path
        memory_path = select_session_memory_path(
            canonical_phone,
            call_id,
            self.config.memory_base_dir,
        )

        # Step 5: Optionally load the selected memory file. Some callers only
        # need identity routing: OpenClaw's chat.send will load the existing
        # session context automatically from the supplied session key.
        if not self.config.load_memory:
            memory_path = None
            memory_text = ""
        elif memory_path and is_memory_file_allowlisted(
            memory_path, self.config.memory_allowlist
        ):
            # Keep the routed path even when the file is not present yet; the
            # loader returns empty text and logs the issue. This preserves the
            # routing decision without inventing or substituting history.
            memory_text = load_selected_session_memory(
                memory_path, self.config.max_memory_chars
            )
        else:
            if memory_path:
                LOG.warning("session memory blocked by allowlist: %s", memory_path)
            memory_path = None
            memory_text = ""

        # A validated identity remains known even if its optional memory file
        # is absent. The memory_text is empty in that case; no historical chat
        # is substituted.
        if canonical_phone:
            context_data = build_known_context(canonical_phone, call_id)
            is_restricted = False
        else:
            context_data = build_restricted_context(call_id)
            is_restricted = True

        ctx = VoiceContext(
            identity=identity,
            memory_path=memory_path,
            memory_text=memory_text,
            context_data=context_data,
            is_restricted=is_restricted,
        )

        LOG.info(
            "resolved context: call_id=%s known=%s mapped=%s phone=%s memory=%s restricted=%s",
            call_id,
            identity.is_known,
            identity.is_mapped,
            canonical_phone or "none",
            memory_path or "none",
            is_restricted,
        )

        return ctx

    def _build_identity(
        self,
        call_id: str,
        raw_jid: str | None,
        raw_lid: str | None,
    ) -> CallerIdentity:
        """Build a CallerIdentity from raw identifiers."""
        is_mapped = False

        # Check if JID is a group (not a caller)
        if raw_jid and is_group_jid(raw_jid):
            LOG.info("caller is a group JID: %s", raw_jid)
            # Group calls don't have a single caller identity
            return CallerIdentity(
                call_id=call_id,
                raw_jid=raw_jid,
                raw_lid=raw_lid,
                is_known=False,
                is_mapped=False,
            )

        # Check if we have a direct mapping for this JID
        if raw_jid and raw_jid in self._mapping._jid_to_phone:
            is_mapped = True

        # Check if we have a direct mapping for this LID
        if raw_lid and raw_lid in self._mapping._lid_to_phone:
            is_mapped = True

        return CallerIdentity(
            call_id=call_id,
            raw_jid=raw_jid,
            raw_lid=raw_lid,
            is_known=False,
            is_mapped=is_mapped,
        )

    def _resolve_phone(self, identity: CallerIdentity) -> str | None:
        """Resolve a CallerIdentity to a canonical phone number."""
        # Try JID first
        if identity.raw_jid:
            phone = self._mapping.resolve_jid(identity.raw_jid)
            if phone:
                return phone

        # Try LID mapping
        if identity.raw_lid:
            phone = self._mapping.resolve_lid(identity.raw_lid)
            if phone:
                return phone

        # If we have a JID but no mapping, check if it's phone-based
        if identity.raw_jid:
            return extract_phone_from_jid(identity.raw_jid)

        return None
