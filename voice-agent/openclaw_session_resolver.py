"""OpenClaw Session Resolver.

Scans sessions.json to find the existing OpenClaw session entry matching
a caller's JID / LID / phone number, and locates the corresponding
``.memory.md`` file on disk.  Designed for the MEOWcaller voice agent:
the voice call keeps its own isolated session (no shared chat history),
but the resolver ensures the caller's prior session memory is loaded as
context.

Design constraints:
  - Read-only: never writes to sessions.json.
  - No gateway config changes.
  - Safe fallback: returns None when no match is found (caller gets
    restricted context without prior memory).
  - Does NOT return the session key for chat.send — voice sessions
    always use their own isolated ``agent:main:voice:{call_id}`` key.
  - Logs matching for observability; no secrets in log output.

Usage:
    from openclaw_session_resolver import OpenClawSessionResolver

    resolver = OpenClawSessionResolver("/path/to/sessions.json")
    path = resolver.find_session_memory_path("+6287899303065")
    # path == "/…/memories/chat-whatsapp-direct-+6287899303065.memory.md"
    #       or None if no session / no file on disk
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

LOG = logging.getLogger("meowcaller.session_resolver")

# ── Session key patterns ──────────────────────────────────────────────

# Matches both current direct-session key forms:
#   agent:main:whatsapp:direct:+XXXXXXXXXXX
#   agent:main:direct:+XXXXXXXXXXX
_DIRECT_SESSION_RE = re.compile(
    r"^agent:main:(?:(?:whatsapp):)?direct:(\+\d+)$"
)

# Matches:  agent:main:telegram:direct:<identifier>
_TELEGRAM_DIRECT_RE = re.compile(
    r"^agent:main:telegram:direct:(.+)$"
)

# Matches:  agent:main:<channel>:group:<group_id>
_GROUP_RE = re.compile(
    r"^agent:main:(whatsapp|telegram):group:(.+)$"
)

# Matches:  agent:main:voice:<call_id>
_VOICE_RE = re.compile(
    r"^agent:main:voice:(.+)$"
)

# Phone normalisation
_PHONE_STRIP_RE = re.compile(r"[\s\-()]")


def _normalize_phone(phone: str) -> str:
    """Normalise a phone number to E.164-like format with leading ``+``.

    Examples:
        "081234567890"   → "+6281234567890"
        "+62 812-345-67" → "+6281234567"
        "6281234567890"  → "+6281234567890"
    """
    phone = _PHONE_STRIP_RE.sub("", phone.strip())
    if not phone:
        return ""
    if phone.startswith("+"):
        return phone
    if phone.startswith("0"):
        return "+62" + phone[1:]
    if phone.startswith("62"):
        return "+" + phone
    return "+" + phone


# ── Resolver ──────────────────────────────────────────────────────────


class OpenClawSessionResolver:
    """Finds existing OpenClaw session keys by caller identity.

    The resolver loads and indexes ``sessions.json`` once at construction
    time (or on explicit ``reload()``).  It maintains an in-memory
    index for O(1) lookups.

    Thread-safety: ``reload()`` is guarded by a lock so it can be called
    from a background thread while lookups continue on the hot path.
    """

    def __init__(self, sessions_json_path: str) -> None:
        self._path = os.path.realpath(sessions_json_path)
        self._lock = threading.Lock()

        # Index: normalised_phone → session_key  (WhatsApp direct only)
        self._phone_to_key: dict[str, str] = {}
        # Full map for secondary lookups
        self._all_keys: dict[str, dict[str, Any]] = {}
        # Stats
        self._loaded_at: float = 0.0
        self._entry_count: int = 0

        self.reload()

    # ── Public API ────────────────────────────────────────────────────

    def find_whatsapp_direct_session(
        self,
        phone: str | None,
        *,
        fuzzy: bool = False,
    ) -> str | None:
        """Find the ``agent:main:whatsapp:direct:<phone>`` session key.

        Args:
            phone: Canonical phone (e.g. "+6287899303065") or raw
                   (e.g. "081234567890").  Normalised internally.
            fuzzy: When True, also try matching without the leading
                   ``+`` and with common prefix variations (08→+628…).

        Returns:
            The matching session key string, or ``None``.
        """
        if not phone:
            return None

        normalised = _normalize_phone(phone)
        if not normalised:
            return None

        # Exact match
        key = self._phone_to_key.get(normalised)
        if key is not None:
            LOG.info("session resolved (exact): phone=%s → key=%s", self._redact(normalised), key)
            return key

        if fuzzy:
            key = self._fuzzy_phone_match(normalised)
            if key is not None:
                LOG.info("session resolved (fuzzy): phone=%s → key=%s", self._redact(normalised), key)
                return key

        LOG.info("session unresolved: phone=%s", self._redact(normalised))
        return None

    def find_session_by_key(self, session_key: str) -> dict[str, Any] | None:
        """Look up a full session entry by exact session key.

        Returns the session dict from sessions.json or ``None``.
        """
        return self._all_keys.get(session_key)

    def find_session_for_caller(
        self,
        *,
        canonical_phone: str | None = None,
        raw_jid: str | None = None,
        raw_lid: str | None = None,
    ) -> str | None:
        """Resolve the Gateway session key from the caller identifiers.

        Prefer a canonical phone lookup when the router has a trusted
        JID/LID-to-phone mapping. If no mapping exists, match the raw JID/LID
        digits against WhatsApp direct session keys and chat IDs. This handles
        WhatsApp LIDs that OpenClaw has already materialized as a direct
        session (for example ``...@lid`` → ``agent:main:direct:+<digits>``)
        without guessing that the LID is the caller's real phone number.
        """
        raw_identifiers = [value for value in (raw_jid, raw_lid) if value]
        caller_numbers: set[str] = set()
        for raw in raw_identifiers:
            bare = str(raw).strip().split("@", 1)[0].split(":", 1)[0]
            if bare.isdigit():
                caller_numbers.add("+" + bare)

        if not caller_numbers:
            return None

        candidates: list[tuple[tuple[int, int, int], str]] = []
        with self._lock:
            for key, entry in self._all_keys.items():
                if not isinstance(entry, dict):
                    continue
                match = _DIRECT_SESSION_RE.match(key)
                if not match:
                    continue

                key_phone = match.group(1)
                chat_id = str(entry.get("chatId", ""))
                if key_phone not in caller_numbers and not any(
                    chat_id == f"chat-whatsapp-direct-{number}"
                    for number in caller_numbers
                ):
                    continue

                delivery = entry.get("deliveryContext") or {}
                origin = entry.get("origin") or {}
                channel_values = {
                    str(entry.get("channel", "")).lower(),
                    str(entry.get("lastChannel", "")).lower(),
                    str(delivery.get("channel", "")).lower(),
                    str(origin.get("provider", "")).lower(),
                    str(origin.get("surface", "")).lower(),
                }
                whatsapp = int(
                    "whatsapp" in channel_values
                    or chat_id.startswith("chat-whatsapp-direct-")
                )
                exact_key = int(key == f"agent:main:whatsapp:direct:{key_phone}")
                origin_label = str(origin.get("label", "")).lower()
                meow_created = int(
                    "meowcaller" in origin_label or "voice agent" in origin_label
                )
                updated_at = int(entry.get("updatedAt") or 0)
                candidates.append(
                    ((not meow_created, whatsapp, exact_key, updated_at), key)
                )

        if candidates:
            _rank, key = max(candidates)
            LOG.info("caller session resolved from remote JID: raw=%s → key=%s", self._redact_caller(raw_identifiers), key)
            return key

        # Only use the canonical phone mapping when the remote JID itself did
        # not identify an existing session.
        if canonical_phone:
            key = self.find_whatsapp_direct_session(canonical_phone)
            if key:
                return key

        return None

    def find_any_direct_session(
        self,
        phone: str | None,
    ) -> tuple[str | None, str | None]:
        """Find *any* direct session (WhatsApp or Telegram) for a phone.

        Returns ``(session_key, channel)`` or ``(None, None)``.
        """
        if not phone:
            return None, None

        normalised = _normalize_phone(phone)
        if not normalised:
            return None, None

        # Check WhatsApp direct
        wa_key = self._phone_to_key.get(normalised)
        if wa_key:
            return wa_key, "whatsapp"

        # Fallback: scan all direct keys for a match in deliveryContext
        with self._lock:
            for key, entry in self._all_keys.items():
                dc = entry.get("deliveryContext", {})
                dc_to = dc.get("to", "")
                if dc_to and _normalize_phone(dc_to) == normalised:
                    channel = entry.get("channel", "unknown")
                    LOG.info(
                        "session resolved (deliveryContext): phone=%s → key=%s channel=%s",
                        self._redact(normalised), key, channel,
                    )
                    return key, channel

        return None, None

    def find_session_memory_path(
        self,
        phone: str | None,
        *,
        memory_base_dir: str | None = None,
    ) -> str | None:
        """Find the ``.memory.md`` file for a caller's existing session.

        This is the primary API for the voice agent.  Given a caller's
        phone number, it:

        1. Finds the matching session entry in sessions.json.
        2. Derives the memory file path from the session's ``chatId``.
        3. Verifies the file exists on disk.
        4. Returns the absolute path, or ``None``.

        The voice session always uses its own isolated key — this method
        only locates the memory file for context loading.

        Args:
            phone: Canonical phone (e.g. "+6287899303065") or raw.
            memory_base_dir: Override for the memories directory.
                   Defaults to ``~/.openclaw/agents/main/sessions/memories``.

        Returns:
            Absolute path to the ``.memory.md`` file, or ``None``.
        """
        if not phone:
            return None

        normalised = _normalize_phone(phone)
        if not normalised:
            return None

        # Find the session entry
        session_key = self._phone_to_key.get(normalised)
        if session_key is None:
            LOG.info("memory lookup: no session for phone=%s", self._redact(normalised))
            return None

        entry = self._all_keys.get(session_key)
        if entry is None:
            LOG.info("memory lookup: session key %s not in index", session_key)
            return None

        # Derive memory path from chatId (the dominant convention)
        chat_id = entry.get("chatId", "")
        if not chat_id:
            LOG.info("memory lookup: no chatId for session %s", session_key)
            return None

        base = memory_base_dir or os.path.expanduser(
            "~/.openclaw/agents/main/sessions/memories"
        )
        memory_path = os.path.join(base, f"{chat_id}.memory.md")

        if not os.path.isfile(memory_path):
            LOG.info(
                "memory lookup: file not found: %s (session=%s phone=%s)",
                memory_path, session_key, self._redact(normalised),
            )
            return None

        LOG.info(
            "memory resolved: phone=%s → session=%s → %s",
            self._redact(normalised), session_key, memory_path,
        )
        return memory_path

    @staticmethod
    def _extract_direct_phone(key: str, entry: dict[str, Any]) -> str | None:
        """Extract a direct-session phone from key or structured metadata."""
        match = _DIRECT_SESSION_RE.match(key)
        if match:
            return match.group(1)

        chat_id = entry.get("chatId", "")
        if isinstance(chat_id, str) and chat_id.startswith("chat-whatsapp-direct-"):
            return _normalize_phone(chat_id.removeprefix("chat-whatsapp-direct-"))

        # Do not infer a WhatsApp caller from a generic deliveryContext.to:
        # Telegram/user IDs are numeric too. The direct key or WhatsApp chatId
        # must establish that this is a WhatsApp session.
        return None

    def reload(self) -> int:
        """(Re)load sessions.json and rebuild the index.

        Returns the number of indexed entries.
        """
        with self._lock:
            self._phone_to_key.clear()
            self._all_keys.clear()

            try:
                with open(self._path, encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError) as exc:
                LOG.warning("failed to load %s: %s", self._path, exc)
                self._entry_count = 0
                self._loaded_at = time.time()
                return 0

            if not isinstance(data, dict):
                LOG.warning("sessions.json root is not a dict")
                self._entry_count = 0
                self._loaded_at = time.time()
                return 0

            # A phone can have multiple historical keys. Prefer the exact
            # WhatsApp direct key that passed the live Gateway test, then an
            # explicitly WhatsApp-active entry, then the canonical direct
            # form, and finally the newest update.
            best_by_phone: dict[str, tuple[tuple[int, int, int, int], str]] = {}
            for key, entry in data.items():
                self._all_keys[key] = entry
                if not isinstance(entry, dict):
                    continue

                phone = self._extract_direct_phone(key, entry)
                if not phone:
                    continue

                delivery = entry.get("deliveryContext") or {}
                origin = entry.get("origin") or {}
                channel_values = {
                    str(entry.get("channel", "")).lower(),
                    str(entry.get("lastChannel", "")).lower(),
                    str(delivery.get("channel", "")).lower(),
                    str(origin.get("provider", "")).lower(),
                }
                # Sessions created by the MEOWcaller voice agent itself (test
                # harness / gateway probe sessions) must never win the ranking
                # for chat.send: they carry stale test history and a huge
                # token footprint. Real WhatsApp sessions always win over them.
                origin_label = str(origin.get("label", "")).lower()
                meow_created = int(
                    "meowcaller" in origin_label or "voice agent" in origin_label
                )
                whatsapp_active = int("whatsapp" in channel_values)
                canonical_direct = int(key == f"agent:main:direct:{phone}")
                whatsapp_direct = int(key == f"agent:main:whatsapp:direct:{phone}")
                updated_at = int(entry.get("updatedAt") or 0)
                rank = (
                    not meow_created,
                    whatsapp_active,
                    whatsapp_direct,
                    canonical_direct,
                    updated_at,
                )

                current = best_by_phone.get(phone)
                if current is None or rank > current[0]:
                    best_by_phone[phone] = (rank, key)

            self._phone_to_key = {
                phone: key for phone, (_rank, key) in best_by_phone.items()
            }

            self._entry_count = len(self._all_keys)
            self._loaded_at = time.time()
            LOG.info(
                "session index loaded: %d entries, %d direct phones",
                self._entry_count,
                len(self._phone_to_key),
            )
            return self._entry_count

    @property
    def entry_count(self) -> int:
        """Number of indexed session entries."""
        return self._entry_count

    @property
    def direct_phone_count(self) -> int:
        """Number of WhatsApp direct phone entries indexed."""
        return len(self._phone_to_key)

    @property
    def loaded_at(self) -> float:
        """Timestamp of last successful load."""
        return self._loaded_at

    # ── Internal helpers ──────────────────────────────────────────────

    def _fuzzy_phone_match(self, normalised: str) -> str | None:
        """Try fuzzy matching: strip leading + and 62 prefix variations."""
        # Try without the +
        bare = normalised.lstrip("+")
        for candidate in (bare, "0" + bare[2:] if bare.startswith("62") else bare):
            key = self._phone_to_key.get(candidate)
            if key:
                return key

        # Also try matching the normalised form against deliveryContext.to
        with self._lock:
            for key, entry in self._all_keys.items():
                m = _DIRECT_SESSION_RE.match(key)
                if not m:
                    continue
                entry_phone = m.group(1)
                if entry_phone == normalised:
                    return key

        return None

    @staticmethod
    def _redact(phone: str) -> str:
        """Redact a phone number for safe logging.

        Shows only the country code and last 4 digits:
        "+6287899303065" → "+62****03065"
        """
        if len(phone) <= 7:
            return "***"
        return phone[:3] + "****" + phone[-5:]

    @classmethod
    def _redact_caller(cls, identifiers: list[str]) -> str:
        """Redact raw JID/LID values without exposing full identifiers."""
        if not identifiers:
            return "***"
        return ",".join(
            f"***@{value.split('@', 1)[1]}" if "@" in value else cls._redact(value)
            for value in identifiers
        )
