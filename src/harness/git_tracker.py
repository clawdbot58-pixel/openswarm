"""Git-backed change tracking for workspaces.

Every workspace is its own git repository.  After an agent writes a
file or runs a piece of code, the harness server calls
:meth:`GitTracker.commit` which stages everything and creates a commit
attributed to the agent that triggered it.  The dashboard reads
:meth:`GitTracker.get_history` to render a timeline; the self-healing
code uses :meth:`GitTracker.reset_to_commit` to roll back to a known
good state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .workspace import Workspace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CommitInfo
# ---------------------------------------------------------------------------

class CommitInfo(BaseModel):
    """A single git commit in a workspace.

    The ``agent_id`` is extracted from the commit message (the harness
    always writes ``"{agent_id}: {message}"``); ``files_changed``,
    ``insertions``, and ``deletions`` come from ``git log --stat``.
    """

    model_config = {"arbitrary_types_allowed": True}

    hash: str
    agent_id: str
    message: str
    timestamp: datetime
    files_changed: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0


# ---------------------------------------------------------------------------
# GitTracker
# ---------------------------------------------------------------------------

class GitTracker:
    """Auto-commit + history + diff + reset for workspaces."""

    GIT_USER_NAME: str = "OpenSwarm"
    GIT_USER_EMAIL: str = "agent@openswarm.local"
    GITIGNORE_BODY: str = "temp/\nlogs/\n*.pyc\n__pycache__/\n"

    def __init__(self, git_binary: str = "git") -> None:
        """Initialize the tracker.

        Args:
            git_binary: Path to the ``git`` executable.  Override for
                tests that want to capture invocations.
        """
        self.git_binary = git_binary

    # -- public API -------------------------------------------------------

    def init_repo(self, workspace: Workspace) -> None:
        """Initialize a git repository in the workspace if absent.

        Idempotent: a workspace that already has a ``.git`` directory
        is left alone, with the user identity still applied so future
        commits carry the harness's author.
        """
        if not shutil.which(self.git_binary):
            raise FileNotFoundError(
                f"git binary not found on PATH: {self.git_binary}"
            )
        if not (workspace.root / ".git").exists():
            self._run_git(workspace.root, "init", "-q", "-b", "main")
        self._ensure_gitignore(workspace)
        self._run_git(
            workspace.root,
            "config", "user.name", self.GIT_USER_NAME,
        )
        self._run_git(
            workspace.root,
            "config", "user.email", self.GIT_USER_EMAIL,
        )
        self._run_git(
            workspace.root,
            "config", "commit.gpgsign", "false",
        )
        # Allow fast-forward only; harmless default.
        self._run_git(
            workspace.root,
            "config", "pull.ff", "only",
        )
        workspace.git_initialized = True
        logger.debug("git repo initialised at %s", workspace.root)

    def commit(
        self,
        workspace: Workspace,
        agent_id: str,
        message: str | None = None,
    ) -> CommitInfo:
        """Stage all changes and create a commit.

        If nothing is staged, the call is a no-op and the current
        ``HEAD`` is returned (so callers can always use the returned
        :class:`CommitInfo`).

        Args:
            workspace: The workspace whose tree to commit.
            agent_id: The agent responsible for the change.  Used as
                the commit message prefix so the history can be
                filtered by agent.
            message: Free-form commit message.  Defaults to
                ``"auto-commit"``.
        """
        self.init_repo(workspace)
        message = message or "auto-commit"
        full_message = f"{agent_id}: {message}"
        self._run_git(workspace.root, "add", "-A")
        # ``git diff --cached --quiet`` returns 1 when there are staged
        # changes, 0 when there are none.
        has_changes = subprocess.run(
            [self.git_binary, "diff", "--cached", "--quiet"],
            cwd=str(workspace.root),
            capture_output=True,
        ).returncode == 1
        if not has_changes:
            head = self._get_head(workspace)
            return self._build_commit_info(workspace, head, agent_id, message)

        # Use --author so the agent id is preserved even if a different
        # git author is configured at the host level.
        author = f"{agent_id} <{agent_id}@openswarm.local>"
        self._run_git(
            workspace.root,
            "commit",
            "-q",
            "-m",
            full_message,
            "--author",
            author,
        )
        head = self._get_head(workspace)
        info = self._build_commit_info(workspace, head, agent_id, message)
        logger.debug(
            "git commit workspace=%s hash=%s agent=%s", workspace.workflow_id, info.hash, agent_id
        )
        return info

    def get_history(self, workspace: Workspace) -> list[CommitInfo]:
        """Return the workspace's commit history, newest first.

        We split the problem in two to side-step the format-vs-``-z``
        collision that nukes the metadata NULs: first read the commit
        metadata with a unique ``|||BODY|||`` marker, then read the
        numstat for each commit hash individually.
        """
        self.init_repo(workspace)
        meta_fmt = "%H%n%an%n%ae%n%at%n%s%n|||BODY|||%b|||END|||"
        out = self._run_git_capture(
            workspace.root, "log", f"--format={meta_fmt}"
        )
        commits: list[CommitInfo] = []
        if not out.strip():
            return commits
        for raw in out.split("|||END|||"):
            raw = raw.strip()
            if not raw:
                continue
            commit = self._parse_meta_record(raw)
            if commit is None:
                continue
            files, ins, dels = self._fetch_numstat(workspace, commit.hash)
            commit.files_changed = files
            commit.insertions = ins
            commit.deletions = dels
            commits.append(commit)
        return commits

    def _parse_meta_record(self, raw: str) -> CommitInfo | None:
        """Parse one ``git log`` metadata record.

        The expected layout is::

            <hash>\\n<author>\\n<email>\\n<timestamp>\\n<subject>\\n|||BODY|||<body>
        """
        if "|||BODY|||" not in raw:
            return None
        header, body = raw.split("|||BODY|||", 1)
        body = body.strip("\n")
        lines = [line for line in header.splitlines() if line != ""]
        if len(lines) < 5:
            return None
        commit_hash, name, email, at, subject = lines[:5]
        try:
            ts = datetime.fromtimestamp(int(at), tz=timezone.utc)
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)
        agent_id = self._agent_id_from_subject(subject) or name
        return CommitInfo(
            hash=commit_hash,
            agent_id=agent_id,
            message=subject,
            timestamp=ts,
        )

    def _fetch_numstat(
        self, workspace: Workspace, commit_hash: str
    ) -> tuple[list[str], int, int]:
        try:
            out = self._run_git_capture(
                workspace.root,
                "show",
                commit_hash,
                "--numstat",
                "--format=",
            )
        except subprocess.CalledProcessError:
            return [], 0, 0
        return self._parse_numstat(out)

    def get_diff(self, workspace: Workspace, commit_hash: str) -> str:
        """Return the unified diff for ``commit_hash``.

        ``--stat`` is prepended so callers get a one-line summary of
        which files changed.
        """
        self.init_repo(workspace)
        try:
            return self._run_git_capture(
                workspace.root, "show", commit_hash, "--stat", "--patch"
            )
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"invalid commit {commit_hash!r}: {exc.stderr}") from exc

    def get_file_at_commit(
        self,
        workspace: Workspace,
        path: str,
        commit_hash: str,
    ) -> str:
        """Return the contents of ``path`` at ``commit_hash``.

        Raises :class:`FileNotFoundError` if the path did not exist
        at that revision.
        """
        self.init_repo(workspace)
        try:
            return self._run_git_capture(
                workspace.root, "show", f"{commit_hash}:{path}"
            )
        except subprocess.CalledProcessError as exc:
            raise FileNotFoundError(
                f"file {path!r} not present at {commit_hash}"
            ) from exc

    def reset_to_commit(self, workspace: Workspace, commit_hash: str) -> None:
        """Hard-reset the workspace to ``commit_hash``.

        Used by the self-healing path: the kernel asks the harness to
        roll back to the last known good state, then re-runs the
        step.  We capture the pre-reset HEAD for the audit log.
        """
        self.init_repo(workspace)
        pre_head = self._get_head(workspace)
        try:
            self._run_git(workspace.root, "reset", "--hard", commit_hash)
        except subprocess.CalledProcessError as exc:
            raise ValueError(f"invalid commit {commit_hash!r}: {exc.stderr}") from exc
        logger.warning(
            "workspace reset workflow=%s from=%s to=%s",
            workspace.workflow_id, pre_head, commit_hash,
        )

    # -- helpers ----------------------------------------------------------

    def _ensure_gitignore(self, workspace: Workspace) -> None:
        path = workspace.root / ".gitignore"
        if not path.exists():
            path.write_text(self.GITIGNORE_BODY, encoding="utf-8")
        self._run_git(workspace.root, "add", "-A", "--", ".gitignore")

    def _run_git(self, cwd: Path, *args: str) -> None:
        """Run a git command, raising on failure."""
        cmd = [self.git_binary, *args]
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

    def _run_git_capture(self, cwd: Path, *args: str) -> str:
        cmd = [self.git_binary, *args]
        result = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True
        )
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )
        return result.stdout

    def _get_head(self, workspace: Workspace) -> str:
        return self._run_git_capture(workspace.root, "rev-parse", "HEAD").strip()

    def _build_commit_info(
        self,
        workspace: Workspace,
        commit_hash: str,
        agent_id: str,
        fallback_message: str,
    ) -> CommitInfo:
        """Build a :class:`CommitInfo` from a freshly-made commit.

        Falls back to a default-constructed :class:`CommitInfo` if
        ``git show`` cannot be parsed.
        """
        try:
            show = self._run_git_capture(
                workspace.root,
                "show",
                commit_hash,
                "--format=%H%n%an%n%ae%n%at%n%s%n|||BODY|||%b|||END|||",
            )
        except subprocess.CalledProcessError:
            return CommitInfo(
                hash=commit_hash,
                agent_id=agent_id,
                message=fallback_message,
                timestamp=datetime.now(timezone.utc),
            )
        if "|||END|||" in show:
            head = show.split("|||END|||", 1)[0]
        else:
            head = show
        commit = self._parse_meta_record(head)
        if commit is None:
            return CommitInfo(
                hash=commit_hash,
                agent_id=agent_id,
                message=fallback_message,
                timestamp=datetime.now(timezone.utc),
            )
        files, ins, dels = self._fetch_numstat(workspace, commit_hash)
        commit.files_changed = files
        commit.insertions = ins
        commit.deletions = dels
        return commit

    @staticmethod
    def _parse_log_entry(meta: str, shortstat: str) -> CommitInfo:
        parts = meta.split("\x00")
        if len(parts) < 5:
            raise ValueError(f"unexpected git log format: {meta!r}")
        commit_hash, name, email, at, subject = parts[:5]
        ts = datetime.fromtimestamp(int(at), tz=timezone.utc)
        agent_id = GitTracker._agent_id_from_subject(subject) or (
            name if "@openswarm.local" in email else name
        )
        files, ins, dels = GitTracker._parse_shortstat(shortstat)
        return CommitInfo(
            hash=commit_hash,
            agent_id=agent_id,
            message=subject,
            timestamp=ts,
            files_changed=files,
            insertions=ins,
            deletions=dels,
        )

    @staticmethod
    def _agent_id_from_subject(subject: str) -> str | None:
        match = re.match(r"^([\w-]+):\s", subject)
        if match:
            return match.group(1)
        return None

    @classmethod
    def _parse_numstat(cls, block: str) -> tuple[list[str], int, int]:
        """Parse a ``git log --numstat`` body.

        Each line is ``insertions<TAB>deletions<TAB>path`` or a
        ``-``-padded rename entry.  Binaries report ``-\t-\tpath``.
        We return the per-commit file list and aggregated counts.
        """
        files: list[str] = []
        insertions = 0
        deletions = 0
        for line in block.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            ins, dels, path = parts[0], parts[1], "\t".join(parts[2:])
            # Renames look like ``a.txt => b.txt``; keep just the new name.
            if " => " in path:
                path = path.split(" => ", 1)[1]
            files.append(path)
            try:
                if ins.isdigit():
                    insertions += int(ins)
                if dels.isdigit():
                    deletions += int(dels)
            except ValueError:
                continue
        return files, insertions, deletions

    @classmethod
    def _build_info_from_meta(
        cls,
        meta: str,
        files: list[str],
        insertions: int,
        deletions: int,
    ) -> CommitInfo:
        """Backwards-compat helper.  Not used by the current parser but
        exported for tests that rely on it."""
        if "|||BODY|||" in meta:
            commit = cls._parse_meta_record(meta)
        else:
            parts = meta.split("\x00")
            while len(parts) < 5:
                parts.append("")
            commit_hash, name, email, at, subject = parts[:5]
            try:
                ts = datetime.fromtimestamp(int(at), tz=timezone.utc)
            except (TypeError, ValueError):
                ts = datetime.now(timezone.utc)
            agent_id = cls._agent_id_from_subject(subject) or name
            commit = CommitInfo(
                hash=commit_hash,
                agent_id=agent_id,
                message=subject,
                timestamp=ts,
            )
        if commit is None:
            return CommitInfo(hash="", agent_id="", message="")
        commit.files_changed = files
        commit.insertions = insertions
        commit.deletions = deletions
        return commit

    @classmethod
    def _parse_shortstat(cls, shortstat: str) -> tuple[list[str], int, int]:
        """Backwards-compat shortstat parser used by the older tests.

        Parses `` 2 files changed, 5 insertions(+), 3 deletions(-)`` style
        output and returns the (possibly empty) file list, total
        insertions, total deletions.
        """
        _SHORTSTAT_RE = re.compile(
            r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?"
            r"(?:, (\d+) deletions?\(-\))?"
        )
        files: list[str] = []
        for line in shortstat.splitlines():
            if "|" in line and "Bin" not in line and "->" not in line:
                fname = line.split("|", 1)[0].strip()
                if fname and "changed" not in fname:
                    files.append(fname)
        insertions = 0
        deletions = 0
        match = _SHORTSTAT_RE.search(shortstat)
        if match:
            if match.group(2):
                insertions = int(match.group(2))
            if match.group(3):
                deletions = int(match.group(3))
        return files, insertions, deletions


__all__ = [
    "CommitInfo",
    "GitTracker",
]
