"""The unified OpenSwarm CLI.

This is the only command the user should ever need.  It exposes a
Click command group with subcommands that match the mental model of
modern dev tools (Vercel, Railway, Docker):

* ``openswarm init``       — create ``data/``, ``workspaces/``, default manifests.
* ``openswarm start``      — spin up kernel + dashboard + workers + main agent.
* ``openswarm stop``       — clean shutdown of every child.
* ``openswarm status``     — pretty table of all child processes.
* ``openswarm run <goal>`` — submit a goal to the running swarm.
* ``openswarm logs``       — stream live logs.
* ``openswarm config …``   — manage the TOML config file.

Design principles (per the Phase 11 brief):

1. ``openswarm start`` works out of the box with reasonable defaults.
2. No 10-command sequences; every user action is a single command.
3. Output is colored (green / yellow / red / gray) and progress
   indicators appear for long-running operations.
4. Strings are user-facing — we never say "Connection refused";
   we say "Kernel not running. Run ``openswarm start``".
5. Auth is opt-in for local dev, required for production (set
   ``OPENSWARM_AUTH__ENABLED=true``).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

import click

from cli.formatter import (
    StatusTable,
    error,
    heading,
    info,
    kv,
    make_console,
    spinner,
    success,
    warn,
)
from cli.types import StartupConfig
from config import (
    OpenSwarmConfig,
    get_config,
    load_config,
    write_config,
)
from process_manager import (
    ProcessManager,
    ProcessManagerError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Version + epilog
# ---------------------------------------------------------------------------

__version__ = "0.1.0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_manager(ctx: click.Context) -> ProcessManager:
    """Return the :class:`ProcessManager` from the Click context."""
    pm: ProcessManager | None = ctx.obj.get("pm") if ctx.obj else None
    if pm is None:
        cfg: OpenSwarmConfig = ctx.obj["config"]
        pm = ProcessManager(
            project_root=cfg.project_root,
            data_dir=cfg.kernel.data_dir,
        )
        ctx.obj["pm"] = pm
    return pm


def _resolve_config(ctx: click.Context) -> OpenSwarmConfig:
    cfg: OpenSwarmConfig | None = ctx.obj.get("config") if ctx.obj else None
    if cfg is None:
        cfg = load_config()
        ctx.obj["config"] = cfg
    return cfg


def _check_kernel_reachable(url: str) -> bool:
    """Return True if the kernel's /health endpoint is up."""
    try:
        with urllib.request.urlopen(url, timeout=1.0) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _resolve_kernel_url(cfg: OpenSwarmConfig, pm: ProcessManager | None) -> str:
    """Prefer the live kernel URL from ``data/state.json`` over config defaults."""
    if pm is not None and pm.state_file.is_file():
        try:
            data = json.loads(pm.state_file.read_text(encoding="utf-8"))
            if data.get("kernel_url"):
                return str(data["kernel_url"]).rstrip("/")
        except (OSError, json.JSONDecodeError):
            pass
    return f"http://127.0.0.1:{cfg.kernel.port}"


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group(
    name="openswarm",
    help="OpenSwarm — autonomous agent swarm.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="openswarm")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, exists=False, dir_okay=False),
    default=None,
    help="Path to a TOML config file (default: auto-discover).",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Disable colored output.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: Path | None,
    no_color: bool,
) -> None:
    """Top-level Click group. Configures shared state for subcommands."""
    ctx.ensure_object(dict)
    cfg = ctx.obj.get("config")
    if cfg is None:
        if config_path is not None:
            cfg = load_config(config_path=config_path)
        else:
            cfg = load_config()
        ctx.obj["config"] = cfg
    if no_color:
        cfg.cli.color = False
    ctx.obj.setdefault("pm", None)
    ctx.obj["console"] = make_console(color=cfg.cli.color)
    if config_path is not None or "config_path" not in ctx.obj:
        ctx.obj["config_path"] = config_path


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "-f",
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite the existing config file if present.",
)
@click.pass_context
def init(ctx: click.Context, force: bool) -> None:
    """Initialize a new OpenSwarm workspace."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]

    heading(console, "Initializing OpenSwarm workspace")

    # 1. Directories.
    directories = [
        cfg.kernel.data_dir,
        cfg.dashboard.db_path.parent,
        cfg.billing.db_path.parent,
        cfg.marketplace.db_path.parent,
        cfg.project_root / "workspaces",
    ]
    for d in directories:
        d.mkdir(parents=True, exist_ok=True)
        success(console, f"Created {d.relative_to(cfg.project_root)}")

    # 2. Heartbeats dir (kernel config also points here; keep both
    #    consistent so the heartbeat monitor never has to make a
    #    filesystem decision at runtime).
    (cfg.project_root / "heartbeats").mkdir(parents=True, exist_ok=True)
    success(console, "Created heartbeats/")

    # 3. Config file.
    config_target = cfg.project_root / "config" / "openswarm.toml"
    if config_target.is_file() and not force:
        info(console, f"Config file already exists at {config_target} (use --force to overwrite)")
    else:
        write_config(cfg, config_target)
        success(console, f"Created {config_target.relative_to(cfg.project_root)}")

    # 4. Sanity-check the default manifests exist; warn if any are
    #    missing so the user knows ``openswarm start`` will be a
    #    no-op for the affected workers.
    from process_manager import _project_root as _pr  # local import: test seams

    root = _pr()
    for manifest in cfg.workers.manifests:
        path = (root / "manifests" / manifest).resolve()
        if path.is_file():
            success(console, f"Manifest present: {manifest}")
        else:
            warn(console, f"Manifest missing: {manifest} (skipping on start)")

    heading(console, "Done. Next: openswarm start")


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--kernel/--no-kernel",
    default=True,
    help="Start the kernel (default: yes).",
)
@click.option(
    "--dashboard/--no-dashboard",
    default=True,
    help="Start the dashboard backend (default: yes).",
)
@click.option(
    "-w",
    "--worker",
    "workers",
    multiple=True,
    help="Worker manifest to spawn (repeatable). Default: from config.",
)
@click.option(
    "--telegram/--no-telegram",
    default=None,
    help="Start the Telegram bot (default: from config).",
)
@click.option(
    "-p",
    "--port",
    "dashboard_port",
    type=int,
    default=None,
    help="Dashboard port (overrides --kernel-port and config).",
)
@click.option(
    "--kernel-port",
    type=int,
    default=None,
    help="Kernel port (overrides config).",
)
@click.option(
    "--detach/--no-detach",
    default=True,
    help="Detach children from the terminal (default: yes).",
)
@click.pass_context
def start(
    ctx: click.Context,
    kernel: bool,
    dashboard: bool,
    workers: tuple[str, ...],
    telegram: Optional[bool],
    dashboard_port: Optional[int],
    kernel_port: Optional[int],
    detach: bool,
) -> None:
    """Start OpenSwarm (kernel + dashboard + workers + main agent)."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]
    pm = _resolve_manager(ctx)

    if dashboard_port is None:
        dashboard_port = cfg.dashboard.port
    if kernel_port is None:
        kernel_port = cfg.kernel.port
    if telegram is None:
        telegram = cfg.telegram.enabled
    if not workers:
        workers = tuple(cfg.workers.manifests) if cfg.workers.auto_start else ()

    start_cfg = StartupConfig(
        kernel=kernel,
        dashboard=dashboard,
        workers=list(workers),
        telegram=bool(telegram),
        port=dashboard_port,
        kernel_port=kernel_port,
        detach=detach,
    )

    heading(console, "Starting OpenSwarm")
    try:
        with spinner(console, "Spawning processes..."):
            procs = pm.start_all(start_cfg)
    except ProcessManagerError as exc:
        error(console, str(exc))
        sys.exit(1)

    for proc in procs:
        success(console, f"Started {proc.kind.value}: {proc.label} (pid {proc.pid})")

    heading(console, "OpenSwarm is running")
    by_kind = {proc.kind.value: proc for proc in procs}
    actual_kernel_port = (
        by_kind.get("kernel").extra.get("port", str(kernel_port))
        if by_kind.get("kernel")
        else str(kernel_port)
    )
    actual_dashboard_port = (
        by_kind.get("dashboard").extra.get("port", str(dashboard_port))
        if by_kind.get("dashboard")
        else str(dashboard_port)
    )

    if kernel:
        info(console, f"  Kernel:    http://127.0.0.1:{actual_kernel_port}")
    if dashboard:
        info(console, f"  Dashboard: http://127.0.0.1:{actual_dashboard_port}/ui/")
    if telegram:
        info(console, "  Telegram:  bot polling for updates")
    info(console, "")
    info(console, "  Run `openswarm status` to check health,")
    info(console, "  `openswarm logs` to stream output,")
    info(console, "  `openswarm stop` to shut down.")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--timeout",
    type=float,
    default=5.0,
    help="Seconds to wait for graceful shutdown before SIGKILL.",
)
@click.pass_context
def stop(ctx: click.Context, timeout: float) -> None:
    """Stop all OpenSwarm processes."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]
    pm = _resolve_manager(ctx)

    heading(console, "Stopping OpenSwarm")
    with spinner(console, "Sending SIGTERM..."):
        survivors = pm.stop_all(timeout=timeout)
    if survivors:
        for s in survivors:
            warn(console, f"Force-killed survivor: {s.label} (pid {s.pid})")
    else:
        success(console, "All processes stopped cleanly.")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print machine-readable JSON instead of a table.",
)
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Show swarm status."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]
    pm = _resolve_manager(ctx)

    snap = pm.get_status()
    if as_json:
        click.echo(
            json.dumps(
                {
                    "kernel": {
                        "running": snap.kernel_running,
                        "pid": snap.kernel_pid,
                        "url": snap.kernel_url,
                    },
                    "dashboard": {
                        "running": snap.dashboard_running,
                        "pid": snap.dashboard_pid,
                        "url": snap.dashboard_url,
                    },
                    "main_agent": {
                        "running": snap.main_agent_running,
                        "pid": snap.main_agent_pid,
                    },
                    "telegram": {
                        "running": snap.telegram_running,
                        "pid": snap.telegram_pid,
                    },
                    "workers": {
                        "running": snap.workers_running,
                        "total": snap.workers_total,
                    },
                    "agents_registered": snap.agents_registered,
                    "workflows_active": snap.workflows_active,
                },
                indent=2,
            )
        )
        return

    heading(console, "OpenSwarm Status")

    def glyph(b: bool) -> str:
        return "[bold green]●[/]" if b else "[dim]○[/]"

    rows = [
        (
            "Kernel",
            "running" if snap.kernel_running else "stopped",
            snap.kernel_url or "—",
        ),
        (
            "Dashboard",
            "running" if snap.dashboard_running else "stopped",
            snap.dashboard_url or "—",
        ),
        (
            "Main agent",
            "running" if snap.main_agent_running else "stopped",
            f"pid {snap.main_agent_pid}" if snap.main_agent_pid else "—",
        ),
        (
            "Telegram",
            "running" if snap.telegram_running else "stopped",
            f"pid {snap.telegram_pid}" if snap.telegram_pid else "—",
        ),
        (
            "Workers",
            f"{snap.workers_running}/{snap.workers_total} ready",
            f"{snap.agents_registered} agents registered",
        ),
    ]
    table = StatusTable(
        console,
        headers=("Component", "Status", "Detail"),
        title=None,
    )
    for row in rows:
        table.add_row(*row)
    table.render()
    info(console, f"Workflows (queue total): {snap.workflows_active}")

    if not snap.kernel_running:
        warn(console, "Kernel not running. Run `openswarm start`.")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


@cli.command(context_settings={"ignore_unknown_options": False})
@click.argument("goal", nargs=-1, required=True)
@click.option(
    "--model",
    default=None,
    help="Force a specific model tier (e.g. gpt-4o-mini).",
)
@click.option(
    "--async",
    "async_",
    is_flag=True,
    default=False,
    help="Submit the goal and return immediately.",
)
@click.option(
    "--wait",
    type=float,
    default=120.0,
    help="Max seconds to wait for the result (sync mode).",
)
@click.pass_context
def run(
    ctx: click.Context,
    goal: tuple[str, ...],
    model: Optional[str],
    async_: bool,
    wait: float,
) -> None:
    """Run a goal through the swarm."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]

    prompt = " ".join(goal).strip()
    if not prompt:
        error(console, "No goal provided. Usage: openswarm run 'Build a login page'")
        sys.exit(2)

    pm = _resolve_manager(ctx)
    kernel_url = _resolve_kernel_url(cfg, pm)
    if not _check_kernel_reachable(f"{kernel_url}/health"):
        error(console, "Kernel not running. Run `openswarm start`.")
        sys.exit(1)

    body = {
        "goal": prompt,
        "model": model,
        "async": async_,
    }
    info(console, f"Submitting goal to {kernel_url}/goals")
    try:
        req = urllib.request.Request(  # noqa: S310
            f"{kernel_url}/goals",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with spinner(console, "Submitting..."):
            with urllib.request.urlopen(req, timeout=5.0) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            detail = {"code": "http_error", "message": str(exc)}
        error(console, f"Kernel rejected the goal: {detail.get('message', exc)}")
        sys.exit(1)
    except (urllib.error.URLError, OSError) as exc:
        error(console, f"Kernel unreachable: {exc}")
        sys.exit(1)

    workflow_id = payload.get("workflow_id") or payload.get("id")
    if not workflow_id:
        warn(console, "Kernel accepted the goal but returned no workflow id.")
        info(console, json.dumps(payload, indent=2))
        return

    success(console, f"Workflow {workflow_id} created")
    initial_status = str(payload.get("status") or "").lower()
    if initial_status in {"queued", "queued_no_main_agent"}:
        info(
            console,
            "Goal queued. The current kernel accepts goals but does not "
            "complete workflow records synchronously yet.",
        )
        return
    if async_:
        info(console, "Running asynchronously. Use `openswarm status` to monitor.")
        return

    # Sync mode: poll the workflow status endpoint.
    deadline = time.time() + wait
    last_state = None
    with spinner(console, "Waiting for completion...") as status_spinner:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(  # noqa: S310
                    f"{kernel_url}/workflows/{workflow_id}", timeout=2.0
                ) as resp:
                    wf = json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, OSError) as exc:
                status_spinner.update(f"Waiting... (last error: {exc})")
                time.sleep(1.0)
                continue
            state = wf.get("status", "unknown")
            if state != last_state:
                status_spinner.update(f"State: {state}")
                last_state = state
            if state in {"completed", "failed", "cancelled", "done"}:
                break
            time.sleep(0.5)
        else:
            error(console, f"Timed out after {wait}s waiting for {workflow_id}")
            sys.exit(2)

    success(console, f"Workflow {workflow_id} finished: {last_state}")
    info(console, json.dumps(wf, indent=2, default=str))


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--agent",
    "labels",
    multiple=True,
    help="Restrict to one or more process labels (repeatable).",
)
@click.option(
    "--tail",
    type=int,
    default=200,
    help="Show the last N lines before following (default: 200).",
)
@click.option(
    "--no-follow",
    is_flag=True,
    default=False,
    help="Print existing log and exit.",
)
@click.pass_context
def logs(
    ctx: click.Context,
    labels: tuple[str, ...],
    tail: int,
    no_follow: bool,
) -> None:
    """Stream live logs from running processes."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]
    pm = _resolve_manager(ctx)

    label_list = list(labels) if labels else None
    try:
        for line in pm.stream_logs(
            labels=label_list,
            follow=not no_follow,
            tail=tail,
        ):
            click.echo(line, nl=False)
    except KeyboardInterrupt:
        info(console, "")
        info(console, "Stopped following logs.")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@cli.group(invoke_without_command=True)
@click.pass_context
def config(ctx: click.Context) -> None:
    """Manage OpenSwarm configuration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_show)


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show the current effective config."""
    cfg = _resolve_config(ctx)
    console = ctx.obj["console"]
    heading(console, "OpenSwarm configuration")
    click.echo(cfg.to_toml())


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """Set a config value (writes the TOML file)."""
    console = ctx.obj["console"]
    cfg = _resolve_config(ctx)
    path = ctx.obj.get("config_path") or (cfg.project_root / "config" / "openswarm.toml")

    try:
        _apply_set(cfg, key, value)
    except (KeyError, ValueError) as exc:
        error(console, str(exc))
        sys.exit(2)

    write_config(cfg, path)
    success(console, f"Set {key} = {value}")


def _apply_set(cfg: OpenSwarmConfig, key: str, value: str) -> None:
    """Apply ``openswarm config set <key> <value>`` to ``cfg`` in place."""
    parts = key.split(".")
    if not parts or not all(parts):
        raise ValueError(f"Invalid key: {key!r}")
    target: Any = cfg
    for part in parts[:-1]:
        if not hasattr(target, part):
            raise KeyError(f"Unknown config section: {part!r}")
        target = getattr(target, part)
    leaf = parts[-1]
    if not hasattr(target, leaf):
        raise KeyError(f"Unknown config key: {key!r}")
    # Best-effort type coercion.
    current = getattr(target, leaf)
    coerced: Any
    if isinstance(current, bool):
        coerced = value.lower() in {"true", "1", "yes", "on"}
    elif isinstance(current, int):
        coerced = int(value)
    elif isinstance(current, float):
        coerced = float(value)
    elif isinstance(current, list):
        coerced = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(current, Path):
        coerced = Path(value)
    else:
        coerced = value
    setattr(target, leaf, coerced)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


@cli.command(hidden=True)
def version() -> None:
    """Print the OpenSwarm CLI version."""
    click.echo(f"openswarm {__version__}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Console-script entry point used by ``pyproject.toml``."""
    cli(obj={})


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["cli", "main", "__version__"]
