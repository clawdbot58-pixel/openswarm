"""Tests for :class:`memory.skill_loader.SkillLoader`."""

from __future__ import annotations

from pathlib import Path

import pytest

from memory.skill_loader import Skill, SkillLoader, SkillNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skills_root(tmp_path: Path) -> Path:
    """A small skills tree with two skills."""
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "SKILL.md").write_text(
        "# Alpha Skill\n\n"
        "When to Use\n\n"
        "- Working with alpha data.\n\n"
        "Description: an alpha skill.\n",
        encoding="utf-8",
    )
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "SKILL.md").write_text(
        "# Beta Skill\n\n"
        "When to Use\n\n"
        "- Working with beta data.\n",
        encoding="utf-8",
    )
    # An empty directory should be ignored.
    (tmp_path / "empty").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# list_available
# ---------------------------------------------------------------------------


def test_list_available_returns_ids(skills_root: Path):
    loader = SkillLoader(skills_root)
    assert loader.list_available() == ["alpha", "beta"]


def test_list_available_handles_missing_root(tmp_path: Path):
    """A non-existent skills root is a soft failure, not an exception."""
    loader = SkillLoader(tmp_path / "does-not-exist")
    assert loader.list_available() == []


def test_list_available_ignores_dirs_without_skill_md(tmp_path: Path):
    """Directories without SKILL.md are not skills."""
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "README.md").write_text("not a skill")
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "SKILL.md").write_text("# OK\n")
    loader = SkillLoader(tmp_path)
    assert loader.list_available() == ["ok"]


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_returns_skill(skills_root: Path):
    loader = SkillLoader(skills_root)
    skill = await loader.load("alpha")
    assert isinstance(skill, Skill)
    assert skill.skill_id == "alpha"
    assert skill.title == "Alpha Skill"
    assert "Alpha Skill" in skill.body


@pytest.mark.asyncio
async def test_load_missing_raises(skills_root: Path):
    loader = SkillLoader(skills_root)
    with pytest.raises(SkillNotFoundError):
        await loader.load("ghost")


@pytest.mark.asyncio
async def test_load_rejects_path_traversal(skills_root: Path):
    """A skill id with / or .. is rejected to avoid path traversal."""
    loader = SkillLoader(skills_root)
    with pytest.raises(SkillNotFoundError):
        await loader.load("../etc/passwd")


@pytest.mark.asyncio
async def test_load_defaults_title_to_skill_id(tmp_path: Path):
    (tmp_path / "noheading").mkdir()
    (tmp_path / "noheading" / "SKILL.md").write_text(
        "just some text, no heading\n", encoding="utf-8"
    )
    loader = SkillLoader(tmp_path)
    skill = await loader.load("noheading")
    assert skill.title == "noheading"


# ---------------------------------------------------------------------------
# load_multiple
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_multiple_returns_dict(skills_root: Path):
    loader = SkillLoader(skills_root)
    skills = await loader.load_multiple(["alpha", "beta"])
    assert set(skills.keys()) == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_load_multiple_skips_missing(skills_root: Path):
    loader = SkillLoader(skills_root)
    skills = await loader.load_multiple(["alpha", "ghost"])
    assert set(skills.keys()) == {"alpha"}


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_uses_first_non_heading_paragraph(skills_root: Path):
    loader = SkillLoader(skills_root)
    skill = await loader.load("alpha")
    assert skill.summary() == "When to Use"


def test_summary_truncates_long_paragraphs(tmp_path: Path):
    import asyncio
    (tmp_path / "long").mkdir()
    long_text = "A" * 500
    (tmp_path / "long" / "SKILL.md").write_text(
        f"# Long\n\n{long_text}\n", encoding="utf-8"
    )
    loader = SkillLoader(tmp_path)
    skill = asyncio.run(loader.load("long"))
    assert skill.summary(max_chars=50).endswith("…")
