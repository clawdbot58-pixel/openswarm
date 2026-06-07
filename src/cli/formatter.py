"""Colored output helpers for the OpenSwarm CLI.

Wraps :mod:`rich` with a single, consistent visual language:

* green   — success, ready, running
* yellow  — busy, warning, pending
* red     — error, crashed
* gray    — idle, stopped
* blue    — heading / section
* bold    — emphasis

When stdout is not a TTY (e.g. piped to a file) or the user disables
color via ``--no-color`` / :attr:`CLISection.color`, all helpers
silently drop the ANSI escapes. This makes scripts that scrape the
CLI trivial to write.

The :class:`StatusTable` class renders the multi-row tables used by
``openswarm status`` and the Telegram formatter. It produces fixed-
width columns so output is readable in any terminal width.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

try:
    from rich.console import Console
    from rich.table import Table as RichTable
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RICH_AVAILABLE = False
    Console = None  # type: ignore[assignment]
    RichTable = None  # type: ignore[assignment]
    Text = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Status → color mapping
# ---------------------------------------------------------------------------

_STATUS_STYLE: dict[str, str] = {
    "running": "bold green",
    "ready": "green",
    "alive": "green",
    "busy": "yellow",
    "pending": "yellow",
    "warning": "yellow",
    "idle": "dim",
    "stopped": "dim",
    "offline": "dim",
    "zombie": "bold red",
    "error": "bold red",
    "failed": "bold red",
    "crashed": "bold red",
    "dead": "bold red",
}


def status_glyph(status: str) -> str:
    """Return a single-character glyph for a status word."""
    s = status.lower()
    if s in {"running", "ready", "alive", "active"}:
        return "●"
    if s in {"busy", "pending", "warning"}:
        return "◐"
    if s in {"idle", "stopped", "offline"}:
        return "○"
    return "✗"


def status_color(status: str) -> str:
    """Return the rich color name for a status word."""
    return _STATUS_STYLE.get(status.lower(), "white")


# ---------------------------------------------------------------------------
# Console factory
# ---------------------------------------------------------------------------


def _isatty(stream: Any) -> bool:
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False


def make_console(*, color: bool | None = None, file: Any | None = None) -> Any:
    """Return a :class:`rich.console.Console` honoring our color policy.

    * ``color=None``  → auto-detect from the stream.
    * ``color=True``  → force colors (used by tests that want them
      even when stdout is captured).
    * ``color=False`` → force no colors.
    """
    target = file or sys.stdout
    if color is None:
        color = _isatty(target) and os.environ.get("NO_COLOR") is None
    if not _RICH_AVAILABLE:  # pragma: no cover
        return _NullConsole(target)
    return Console(
        file=target,
        force_terminal=color,
        no_color=not color,
        soft_wrap=False,
        highlight=False,
    )


class _NullConsole:
    """Tiny stand-in used when :mod:`rich` is not installed."""

    def __init__(self, file: Any) -> None:
        self._file = file

    def print(self, *args: Any, **kwargs: Any) -> None:
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        self._file.write(sep.join(str(a) for a in args) + end)


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def info(console: Any, message: str) -> None:
    """Print a neutral informational line."""
    console.print(f"  {message}")


def success(console: Any, message: str) -> None:
    """Print a green checkmark line for completed steps."""
    console.print(f"  [bold green]✓[/] {message}")


def warn(console: Any, message: str) -> None:
    """Print a yellow warning line."""
    console.print(f"  [bold yellow]![/] {message}")


def error(console: Any, message: str) -> None:
    """Print a red error line."""
    console.print(f"  [bold red]✗[/] {message}", style="bold red")


def heading(console: Any, message: str) -> None:
    """Print a blue section heading."""
    console.print(f"\n[bold blue]{message}[/]\n")


def kv(console: Any, key: str, value: Any, *, style_value: str | None = None) -> None:
    """Print a key-value pair, aligned with simple spacing."""
    if style_value and _RICH_AVAILABLE:
        console.print(f"  [dim]{key}:[/] [{style_value}]{value}[/]")
    else:
        console.print(f"  [dim]{key}:[/] {value}")


@contextmanager
def spinner(console: Any, message: str) -> Iterator[Any]:
    """Context manager that displays a spinner while a block runs.

    Falls back to a plain print when rich isn't available. The yielded
    object is a no-op ``status`` that callers can ``.update()``.
    """
    if not _RICH_AVAILABLE:
        info(console, message)
        yield _NullStatus()
        return
    with console.status(message, spinner="dots") as status:
        yield status


class _NullStatus:
    def update(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


# ---------------------------------------------------------------------------
# Table rendering
# ---------------------------------------------------------------------------


class StatusTable:
    """Aligned status table with rich-aware coloring.

    Example::

        t = StatusTable(console, headers=("Kernel", "Status", "Detail"))
        t.add_row("kernel", "running", "v0.1.0")
        t.add_row("dashboard", "stopped", "—")
        t.render()
    """

    def __init__(
        self,
        console: Any,
        headers: Sequence[str],
        *,
        title: str | None = None,
    ) -> None:
        self._console = console
        self._headers = tuple(headers)
        self._title = title
        self._rows: list[tuple[str, ...]] = []

    def add_row(self, *cells: Any) -> None:
        self._rows.append(tuple(str(c) for c in cells))

    def render(self) -> None:
        if not _RICH_AVAILABLE:
            self._render_plain()
            return
        table = RichTable(
            title=self._title,
            show_header=True,
            header_style="bold blue",
            box=None,
            pad_edge=False,
        )
        for h in self._headers:
            table.add_column(h)
        for row in self._rows:
            rendered: list[Any] = []
            for idx, cell in enumerate(row):
                if idx == 1 and len(self._headers) > 1 and self._headers[1].lower() in {
                    "status",
                    "state",
                }:
                    rendered.append(
                        Text(
                            f"{status_glyph(cell)} {cell}",
                            style=status_color(cell),
                        )
                    )
                else:
                    rendered.append(cell)
            table.add_row(*rendered)
        self._console.print(table)

    def _render_plain(self) -> None:
        widths = [len(h) for h in self._headers]
        for row in self._rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))
        if self._title:
            self._console.print(self._title)
            self._console.print("-" * sum(widths))
        line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(self._headers))
        self._console.print(line)
        for row in self._rows:
            line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))
            self._console.print(line)


# ---------------------------------------------------------------------------
# Plain (no-rich) formatters used by the Telegram adapter
# ---------------------------------------------------------------------------


def to_plain(console: Any) -> Any:
    """Return a console wrapper that always emits plain text.

    Used by the Telegram formatter, which can't render ANSI.
    """
    buf = io.StringIO()
    if _RICH_AVAILABLE:
        return Console(file=buf, force_terminal=False, no_color=True, width=4096)
    return _NullConsole(buf)


def terminal_width(fallback: int = 80) -> int:
    """Best-effort terminal width detection."""
    try:
        return shutil.get_terminal_size((fallback, 24)).columns
    except Exception:  # noqa: BLE001
        return fallback


__all__ = [
    "StatusTable",
    "console_print",
    "error",
    "heading",
    "info",
    "kv",
    "make_console",
    "spinner",
    "status_color",
    "status_glyph",
    "success",
    "terminal_width",
    "to_plain",
    "warn",
]


def console_print(console: Any, *args: Any, **kwargs: Any) -> None:
    """Forwarder for ``console.print`` so call sites don't import rich."""
    console.print(*args, **kwargs)
