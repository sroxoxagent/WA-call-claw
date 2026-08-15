"""Tests for MEOWcaller MD Profile Context Loader.

All tests use temp directories — no real workspace files required.
Run with: pytest tests/test_md_profile_loader.py -v
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from md_profile_loader import (
    ContextProfile,
    MdProfileLoader,
    ProfileFile,
    load_profile_from_config,
)


# ---------------------------------------------------------------------------
# ProfileFile / ContextProfile data model tests
# ---------------------------------------------------------------------------


class TestProfileFile:
    def test_defaults(self):
        pf = ProfileFile(name="TEST", path="test.md")
        assert pf.name == "TEST"
        assert pf.path == "test.md"
        assert pf.max_chars == 4000

    def test_custom_max(self):
        pf = ProfileFile(name="X", path="x.md", max_chars=1000)
        assert pf.max_chars == 1000


class TestContextProfile:
    def test_defaults(self):
        cp = ContextProfile()
        assert cp.base_path == "/opt/wa-call-claw"
        assert cp.files == []
        assert cp.total_max_chars == 16000


# ---------------------------------------------------------------------------
# MdProfileLoader tests
# ---------------------------------------------------------------------------


class TestMdProfileLoader:
    def test_load_existing_files(self, tmp_path: Path):
        (tmp_path / "IDENTITY.md").write_text("# Identity\nI am Sroxox.", encoding="utf-8")
        (tmp_path / "USER.md").write_text("# User\nOwner.", encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[
                ProfileFile(name="IDENTITY", path="IDENTITY.md", max_chars=4000),
                ProfileFile(name="USER", path="USER.md", max_chars=4000),
            ],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()

        assert "IDENTITY" in result
        assert "USER" in result
        assert "Sroxox" in result["IDENTITY"]
        assert "Owner" in result["USER"]

    def test_skip_missing_files(self, tmp_path: Path):
        (tmp_path / "IDENTITY.md").write_text("exists", encoding="utf-8")
        # USER.md does not exist

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[
                ProfileFile(name="IDENTITY", path="IDENTITY.md", max_chars=4000),
                ProfileFile(name="USER", path="USER.md", max_chars=4000),
            ],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()

        assert "IDENTITY" in result
        assert "USER" not in result

    def test_individual_max_chars_cap(self, tmp_path: Path):
        (tmp_path / "BIG.md").write_text("A" * 5000, encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[ProfileFile(name="BIG", path="BIG.md", max_chars=100)],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()

        assert len(result["BIG"]) == 100

    def test_total_max_chars_cap(self, tmp_path: Path):
        (tmp_path / "A.md").write_text("A" * 100, encoding="utf-8")
        (tmp_path / "B.md").write_text("B" * 100, encoding="utf-8")
        (tmp_path / "C.md").write_text("C" * 100, encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[
                ProfileFile(name="A", path="A.md", max_chars=200),
                ProfileFile(name="B", path="B.md", max_chars=200),
                ProfileFile(name="C", path="C.md", max_chars=200),
            ],
            total_max_chars=150,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()

        # A gets 100, B gets 50 (cap), C gets 0 (cap reached)
        assert len(result["A"]) == 100
        assert len(result["B"]) == 50
        assert "C" not in result

    def test_path_traversal_blocked(self, tmp_path: Path):
        (tmp_path / "safe.md").write_text("safe", encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[
                ProfileFile(name="EVIL", path="../../../etc/passwd", max_chars=4000),
                ProfileFile(name="SAFE", path="safe.md", max_chars=4000),
            ],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()

        assert "EVIL" not in result
        assert "SAFE" in result

    def test_empty_files_list(self, tmp_path: Path):
        profile = ContextProfile(base_path=str(tmp_path), files=[])
        loader = MdProfileLoader(profile)
        result = loader.load()
        assert result == {}

    def test_load_context_text_format(self, tmp_path: Path):
        (tmp_path / "A.md").write_text("Content A", encoding="utf-8")
        (tmp_path / "B.md").write_text("Content B", encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[
                ProfileFile(name="A", path="A.md", max_chars=4000),
                ProfileFile(name="B", path="B.md", max_chars=4000),
            ],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        text = loader.load_context_text()

        assert "=== A.md ===" in text
        assert "Content A" in text
        assert "=== B.md ===" in text
        assert "Content B" in text

    def test_load_context_text_empty(self, tmp_path: Path):
        profile = ContextProfile(base_path=str(tmp_path), files=[])
        loader = MdProfileLoader(profile)
        assert loader.load_context_text() == ""

    def test_unicode_content(self, tmp_path: Path):
        (tmp_path / "ID.md").write_text("Ini konteks Indonesia: 你好世界", encoding="utf-8")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[ProfileFile(name="ID", path="ID.md", max_chars=4000)],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()
        assert "你好世界" in result["ID"]

    def test_binary_file_skipped(self, tmp_path: Path):
        (tmp_path / "BIN.md").write_bytes(b"\x00\x01\x02\x03")

        profile = ContextProfile(
            base_path=str(tmp_path),
            files=[ProfileFile(name="BIN", path="BIN.md", max_chars=4000)],
            total_max_chars=16000,
        )
        loader = MdProfileLoader(profile)
        result = loader.load()
        # Binary file may or may not be readable — either outcome is acceptable
        # The key is no crash
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# load_profile_from_config tests
# ---------------------------------------------------------------------------


class TestLoadProfileFromConfig:
    def test_full_config(self):
        config = {
            "context_profile": {
                "base_path": "/tmp/test",
                "files": [
                    {"name": "IDENTITY", "path": "IDENTITY.md", "max_chars": 3000},
                    {"name": "USER", "path": "USER.md", "max_chars": 2000},
                ],
                "total_max_chars": 8000,
            }
        }
        profile = load_profile_from_config(config)
        assert profile.base_path == "/tmp/test"
        assert len(profile.files) == 2
        assert profile.files[0].name == "IDENTITY"
        assert profile.files[0].max_chars == 3000
        assert profile.total_max_chars == 8000

    def test_empty_config_defaults(self):
        profile = load_profile_from_config({})
        assert profile.base_path == "/opt/wa-call-claw"
        assert profile.files == []
        assert profile.total_max_chars == 16000

    def test_missing_files_key(self):
        config = {"context_profile": {"base_path": "/tmp"}}
        profile = load_profile_from_config(config)
        assert profile.files == []

    def test_partial_config(self):
        config = {
            "context_profile": {
                "files": [{"name": "X", "path": "x.md"}],
            }
        }
        profile = load_profile_from_config(config)
        assert len(profile.files) == 1
        assert profile.files[0].max_chars == 4000  # default
        assert profile.total_max_chars == 16000  # default
