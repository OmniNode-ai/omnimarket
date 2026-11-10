# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
r"""CLI of node_stop_drill_effect: the drill's on-demand trigger and its own schedule verbs.

Usage:
    python -m omnimarket.nodes.node_stop_drill_effect run [--run-id ID] [--trigger NAME] [--state-dir DIR]
    python -m omnimarket.nodes.node_stop_drill_effect schedule check|install|uninstall|run|status ...

Every setting comes from the drill overlay (``ONEX_SKILL_OVERLAY_ROOTS``), never a flag.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ModelScheduleResult,
)
from omnimarket.nodes.node_stop_drill_effect.handlers.schedule import (
    OVERLAY_ROOTS_ENV,
    StopDrillScheduler,
    load_schedule,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay
from omnimarket.nodes.node_stop_drill_effect.overlay import load_overlay
from omnimarket.nodes.node_stop_drill_effect.runtime_stop_drill import (
    EXIT_OVERLAY_REFUSED,
    run_once,
    write_evidence,
)

drill_group = click.Group("stop-drill", help="The hourly stop drill.")


@drill_group.command("run")
@click.option("--run-id", default=None, help="Default: <trigger>-<UTC stamp>.")
@click.option("--trigger", default="manual", show_default=True)
@click.option(
    "--state-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where the timer's last-run evidence is written.",
)
def run_command(run_id: str | None, trigger: str, state_dir: Path | None) -> None:
    """Run the drill once, write its rows and print the result."""
    code, finished_id, verdict = run_once(
        run_id,
        trigger=trigger,
        emit=click.echo,
        emit_error=lambda text: click.echo(text, err=True),
    )
    if state_dir is not None:
        write_evidence(state_dir, finished_id, verdict, code, datetime.now(UTC))
    if code:
        sys.exit(code)


def _overlay() -> ModelStopDrillOverlay:
    try:
        return load_overlay()
    except (NodeOverlayError, ValidationError) as exc:
        click.echo(f"overlay refused: {exc}", err=True)
        sys.exit(EXIT_OVERLAY_REFUSED)


def _scheduler() -> StopDrillScheduler:
    return StopDrillScheduler(
        schedule=load_schedule(), overlay=_overlay(), home=Path.home()
    )


def _finish(result: ModelScheduleResult) -> None:
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, err=True, nl=False)
    if result.exit_code:
        sys.exit(result.exit_code)


schedule_group = click.Group("schedule", help="The drill's own systemd user timer.")
drill_group.add_command(schedule_group)


@schedule_group.command("check")
def schedule_check() -> None:
    """Exit 0 only when the overlay declares a schedule host."""
    _finish(_scheduler().check())


@schedule_group.command("install")
@click.option("--host", required=True)
@click.option(
    "--omni-root",
    envvar="OMNI_HOME",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option(
    "--overlay-root",
    envvar=OVERLAY_ROOTS_ENV,
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
)
@click.option("--dry-run", is_flag=True)
def schedule_install(
    host: str, omni_root: Path, overlay_root: Path, dry_run: bool
) -> None:
    """Write the two unit files and enable the timer; ``--dry-run`` prints them."""
    _finish(
        _scheduler().install(
            host, omni_root=omni_root, overlay_root=overlay_root, dry_run=dry_run
        )
    )


@schedule_group.command("uninstall")
@click.option("--host", required=True)
def schedule_uninstall(host: str) -> None:
    """The rollback: disable the timer and remove the two unit files."""
    _finish(_scheduler().uninstall(host))


@schedule_group.command("run")
@click.option("--host", required=True)
def schedule_run(host: str) -> None:
    """Start the installed service once."""
    _finish(_scheduler().run_now(host))


@schedule_group.command("status")
@click.option("--host", required=True)
def schedule_status(host: str) -> None:
    """Whether the timer is enabled, and when it next fires."""
    _finish(_scheduler().status(host))


def main() -> None:
    """Run the stop drill CLI."""
    with contextlib.suppress(BrokenPipeError):
        drill_group()


if __name__ == "__main__":
    main()
