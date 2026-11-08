# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
r"""CLI of node_lab_test_harness_effect: the harness's on-demand trigger and its reader.

Usage:
    python -m omnimarket.nodes.node_lab_test_harness_effect run [--run-id ID]
    python -m omnimarket.nodes.node_lab_test_harness_effect read [--now ISO8601]
    python -m omnimarket.nodes.node_lab_test_harness_effect schedule check|install|uninstall|run|status ...

Every setting comes from the harness overlay (\`\`ONEX_SKILL_OVERLAY_ROOTS\`\`), never a flag.
\`\`run\`\` is the same handler the schedule node's request reaches; it prints the reading and
exits 0 only when it is WORKING, 1 when it is not, 69 when the bus is unavailable and 65 when
the overlay refuses. \`\`read\`\` prints the stored reading as it stands at a read time (the
staleness rule included) and exits 0 only when WORKING.
"""

from __future__ import annotations

import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessReadingState,
    ModelLabTestHarnessOverlay,
    reading_at,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers.schedule import (
    HarnessScheduler,
    ModelHarnessScheduleResult,
    load_unit,
)
from omnimarket.nodes.node_lab_test_harness_effect.runtime_lab_test_harness import (
    EXIT_NOT_WORKING,
    EXIT_OVERLAY_REFUSED,
    EXIT_WORKING,
    run_once,
)

OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"


@click.group("lab-test-harness")
def harness_group() -> None:
    """The central test harness."""


def _overlay() -> ModelLabTestHarnessOverlay:
    from omnimarket.handlers.node_overlay_reader import load_structured_overlay

    try:
        return load_structured_overlay(
            "node_lab_test_harness_effect", ModelLabTestHarnessOverlay
        )
    except (NodeOverlayError, ValidationError) as exc:
        click.echo(f"overlay refused: {exc}", err=True)
        sys.exit(EXIT_OVERLAY_REFUSED)


@harness_group.command("run")
@click.option(
    "--workspace-root",
    envvar="OMNIBASE_PATH",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
@click.option("--run-id", default=None, help="Default: manual-<UTC stamp>.")
def run_command(workspace_root: Path | None, run_id: str | None) -> None:
    """Run every case once, write the current reading and print it."""
    code, _run_id, _state = run_once(
        workspace_root,
        run_id,
        trigger="manual",
        emit=click.echo,
        emit_error=lambda text: click.echo(text, err=True),
    )
    if code != EXIT_WORKING:
        sys.exit(code)


@harness_group.command("read")
@click.option("--now", "now_text", default=None, help="Read as of this ISO 8601 time.")
def read_command(now_text: str | None) -> None:
    """Print the stored reading at a read time; exit 0 only when WORKING."""
    overlay = _overlay()
    now = datetime.fromisoformat(now_text) if now_text else datetime.now(UTC)
    store = HandlerLabTestHarnessEffect(overlay=overlay).store()
    reading = reading_at(
        store.read(),
        piece=overlay.piece,
        tag=overlay.tag,
        max_age_seconds=overlay.max_age_seconds,
        now=now,
    )
    click.echo(reading.model_dump_json(indent=2))
    if reading.state is not EnumLabTestHarnessReadingState.WORKING:
        sys.exit(EXIT_NOT_WORKING)


def _scheduler() -> HarnessScheduler:
    return HarnessScheduler(unit=load_unit(), overlay=_overlay(), home=Path.home())


def _finish(result: ModelHarnessScheduleResult) -> None:
    if result.stdout:
        click.echo(result.stdout, nl=False)
    if result.stderr:
        click.echo(result.stderr, err=True, nl=False)
    if result.exit_code:
        sys.exit(result.exit_code)


@harness_group.group("schedule")
def schedule_group() -> None:
    """The harness's own systemd user timer."""


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
    """Run the harness CLI."""
    with contextlib.suppress(BrokenPipeError):
        harness_group()


if __name__ == "__main__":
    main()
