"""Skill loader — reads ``SKILL.md`` files from the filesystem.

Skills are the OpenClaw-equivalent of project memory: a domain
expert's "how to do this thing" distilled into a markdown file.
The :class:`ContextAssembler` reads them from disk and renders
them into the preamble before each inference.

The on-disk format is one directory per skill, with a
``SKILL.md`` inside:

::

    skills/
        python/
            SKILL.md
        security-review/
            SKILL.md

The first ``#`` heading in ``SKILL.md`` is the skill's title
(``SKILL: {title}``); everything below is the body.  Callers can
ask for the body (default) or the raw markdown.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# Matches the first ``#`` heading.  Captures the title text.
_TITLE_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Skill:
    """A loaded skill.

    Attributes:
        skill_id: The directory name (e.g. ``"python"``).
        title: The first ``#`` heading of the SKILL.md body.
        path: Absolute path to the SKILL.md file.
        body: The full markdown content.
    """

    skill_id: str
    title: str
    path: Path
    body: str

    def summary(self, max_chars: int = 200) -> str:
        """Return a one-line summary suitable for an LLM system prompt.

        We use the first non-heading paragraph; this matches the
        "Description / When to Use" block the Phase 6 spec shows.
        """
        for chunk in self.body.split("\n\n"):
            stripped = chunk.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                continue
            if len(stripped) <= max_chars:
                return stripped
            return stripped[: max_chars - 1] + "…"
        return self.title


class SkillNotFoundError(FileNotFoundError):
    """Raised when a requested skill doesn't exist on disk."""


class SkillLoader:
    """Loads skills from a directory tree.

    Args:
        skills_dir: Root directory of the skills tree.  Default
            ``Path("skills")`` relative to the process CWD.  Each
            immediate subdirectory is a skill; each must contain a
            ``SKILL.md``.
    """

    SKILLS_DIR: Path = Path("skills")
    SKILL_FILENAME: str = "SKILL.md"

    def __init__(self, skills_dir: Path | str | None = None) -> None:
        self._skills_dir = Path(skills_dir) if skills_dir is not None else self.SKILLS_DIR

    @property
    def skills_dir(self) -> Path:
        """The configured skills root."""
        return self._skills_dir

    # -- public API -------------------------------------------------------

    def list_available(self) -> list[str]:
        """Return the ids of every skill discoverable under the root.

        The root need not exist; missing-root returns ``[]`` rather
        than raising so a fresh checkout doesn't crash the kernel.
        """
        if not self._skills_dir.exists():
            return []
        return sorted(
            entry.name
            for entry in self._skills_dir.iterdir()
            if entry.is_dir() and (entry / self.SKILL_FILENAME).is_file()
        )

    async def load(self, skill_id: str) -> Skill:
        """Load a single skill by id.

        Args:
            skill_id: The directory name under the skills root.

        Returns:
            A :class:`Skill` with the parsed title and body.

        Raises:
            SkillNotFoundError: If the skill or its ``SKILL.md`` is
                missing.
        """
        path = self._skill_path(skill_id)
        if not path.is_file():
            raise SkillNotFoundError(
                f"skill {skill_id!r} not found at {path}"
            )
        body = path.read_text(encoding="utf-8")
        title_match = _TITLE_RE.search(body)
        title = title_match.group(1) if title_match else skill_id
        return Skill(skill_id=skill_id, title=title, path=path, body=body)

    async def load_multiple(self, skill_ids: Iterable[str]) -> dict[str, Skill]:
        """Load several skills at once; missing skills are silently skipped.

        Returns a ``{skill_id: Skill}`` map for the ones that were
        found.  Use :meth:`load` directly if you need to know which
        ids failed.
        """
        result: dict[str, Skill] = {}
        for sid in skill_ids:
            try:
                result[sid] = await self.load(sid)
            except SkillNotFoundError:
                logger.warning("skill %r not found; skipping", sid)
        return result

    def list_available_sync(self) -> list[str]:
        """Sync alias for :meth:`list_available` for code that doesn't await."""
        return self.list_available()

    # -- helpers ----------------------------------------------------------

    def _skill_path(self, skill_id: str) -> Path:
        """Return the on-disk path to ``skill_id``'s ``SKILL.md``."""
        if not skill_id or "/" in skill_id or ".." in skill_id.split("/"):
            # Refuse anything that smells like path traversal.
            raise SkillNotFoundError(f"invalid skill id: {skill_id!r}")
        return self._skills_dir / skill_id / self.SKILL_FILENAME


__all__ = ["Skill", "SkillLoader", "SkillNotFoundError"]
