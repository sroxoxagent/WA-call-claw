"""MEOWcaller MD Profile Context Loader.

Loads explicit Markdown context files (IDENTITY.md, USER.md, SOUL.md, MEMORY.md)
from a config-driven profile, with safe allowlisting and size caps.

Design constraints:
  - Only loads files explicitly listed in config — never auto-scans workspace.
  - Each file has an individual max_chars cap.
  - Total combined output has a total_max_chars cap.
  - Files are loaded from a fixed base_path — no path traversal.
  - Returns a formatted context string suitable for bootstrap injection.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger("meowcaller.md_profile_loader")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ProfileFile:
    """A single MD file entry in the context profile."""

    name: str
    path: str
    max_chars: int = 4000


@dataclass
class ContextProfile:
    """Configuration for which MD files to load as context."""

    base_path: str = "/opt/wa-call-claw"
    files: list[ProfileFile] = field(default_factory=list)
    total_max_chars: int = 16000


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class MdProfileLoader:
    """Loads MD context files from a config-driven profile.

    Usage::

        profile = ContextProfile(
            base_path="/opt/wa-call-claw",
            files=[
                ProfileFile(name="IDENTITY", path="IDENTITY.md", max_chars=4000),
                ProfileFile(name="USER", path="USER.md", max_chars=4000),
            ],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        context_text = loader.load_context_text()
    """

    def __init__(self, profile: ContextProfile) -> None:
        self.profile = profile

    def load(self) -> dict[str, str]:
        """Load all configured MD files and return ``{name: content}`` mapping.

        Only loads files that:

        1. Are explicitly listed in the profile config.
        2. Exist on disk as regular files.
        3. Stay within the base_path (no path traversal).
        4. Don't exceed individual max_chars.
        5. Don't exceed total_max_chars.
        """
        result: dict[str, str] = {}
        total_chars = 0

        base = Path(self.profile.base_path).resolve()

        for file_entry in self.profile.files:
            if total_chars >= self.profile.total_max_chars:
                LOG.info("total max chars reached, skipping remaining files")
                break

            # Build absolute path and validate it stays within base
            file_path = (base / file_entry.path).resolve()
            if not str(file_path).startswith(str(base) + os.sep) and str(file_path) != str(base):
                LOG.warning("path traversal blocked: %s", file_entry.path)
                continue

            if not file_path.is_file():
                LOG.debug("MD file not found, skipping: %s", file_path)
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                LOG.warning("failed to read MD file %s: %s", file_path, exc)
                continue

            # Apply individual cap
            if len(content) > file_entry.max_chars:
                content = content[: file_entry.max_chars]
                LOG.info("truncated %s to %d chars", file_entry.name, file_entry.max_chars)

            # Apply total cap
            remaining = self.profile.total_max_chars - total_chars
            if len(content) > remaining:
                content = content[:remaining]
                LOG.info(
                    "truncated %s to fit total cap (%d chars remaining)",
                    file_entry.name,
                    remaining,
                )

            total_chars += len(content)
            result[file_entry.name] = content

        LOG.info(
            "loaded %d MD files, total %d/%d chars",
            len(result),
            total_chars,
            self.profile.total_max_chars,
        )
        return result

    def load_context_text(self) -> str:
        """Load all MD files and return a formatted context string.

        Format::

            === IDENTITY.md ===
            (content)

            === USER.md ===
            (content)
        """
        files = self.load()
        if not files:
            return ""

        parts: list[str] = []
        for name, content in files.items():
            parts.append(f"=== {name}.md ===\n{content}\n")

        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_profile_from_config(config: dict[str, Any]) -> ContextProfile:
    """Build a :class:`ContextProfile` from a config dict (JSON or parsed YAML).

    Expected config structure::

        {
            "context_profile": {
                "base_path": "/opt/wa-call-claw",
                "files": [
                    {"name": "IDENTITY", "path": "IDENTITY.md", "max_chars": 4000}
                ],
                "total_max_chars": 16000
            }
        }
    """
    profile_config = config.get("context_profile", {})
    base_path = profile_config.get("base_path", "/opt/wa-call-claw")
    total_max_chars = profile_config.get("total_max_chars", 16000)

    files: list[ProfileFile] = []
    for f in profile_config.get("files", []):
        files.append(
            ProfileFile(
                name=f.get("name", "unknown"),
                path=f.get("path", ""),
                max_chars=f.get("max_chars", 4000),
            )
        )

    return ContextProfile(
        base_path=base_path,
        files=files,
        total_max_chars=total_max_chars,
    )
