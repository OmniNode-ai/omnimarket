# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
r"""CLI of node_lab_test_harness_effect: the harness's on-demand trigger and its reader.

Usage:
    python -m omnimarket.nodes.node_lab_test_harness_effect run [--run-id ID]
    python -m omnimarket.nodes.node_lab_test_harness_effect read [--now ISO8601]

Every setting comes from the harness overlay (\`\`ONEX_SKILL_OVERLAY_ROOTS\`\`), never a flag.
\`\`run\`\` is the same handler the schedule node's request reaches; it prints the reading and
exits 0 only when it is WORKING, 1 when it is not, 69 when the bus is unavailable and 65 when
the overlay refuses. \`\`read\`\` prints the stored reading as it stands at a read time (the
staleness rule included) and exits 0 only when WORKING.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from datetime import UTC, datetime
from pathlib import Path

import click
from pydantic import ValidationError

from omnimarket.delegated_test_loop.lane_bus import LabRunBusError
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.lab_test_harness import (
    EnumLabTestHarnessReadingState,
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessRequested,
    reading_at,
)
from omnimarket.nodes.node_lab_test_harness_effect.handlers import (
    HandlerLabTestHarnessEffect,
    open_lab_work_runner,
)

EXIT_NOT_WORKING = 1
EXIT_OVERLAY_REFUSED = 65
EXIT_BUS_UNAVAILABLE = 69


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
    overlay = _overlay()
    now = datetime.now(UTC)
    handler = HandlerLabTestHarnessEffect(
        overlay=overlay,
        runner_opener=lambda ov: open_lab_work_runner(
            ov, workspace_root=workspace_root
        ),
    )
    request = ModelLabTestHarnessRequested(
        run_id=run_id or f"manual-{now:%Y%m%dT%H%M%SZ}",
        requested_at=now,
        trigger="manual",
    )
    try:
        reading = asyncio.run(handler.handle(request))
    except LabRunBusError as exc:
        click.echo(f"bus: {exc}", err=True)
        sys.exit(EXIT_BUS_UNAVAILABLE)
    click.echo(reading.model_dump_json(indent=2))
    if reading.state is not EnumLabTestHarnessReadingState.WORKING:
        sys.exit(EXIT_NOT_WORKING)


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


def main() -> None:
    """Run the harness CLI."""
    with contextlib.suppress(BrokenPipeError):
        harness_group()


if __name__ == "__main__":
    main()
