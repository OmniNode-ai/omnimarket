# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`onex lab-archive`: archive lab history older than seven days to Drive (OMN-17427).

A shim over ``node_lab_history_archive_effect``; it holds no logic of its own. Hosts,
roles, source roots, staging, the Drive destination and the throttle budget come from
the node's deployment overlay. A host that carries the landing controller role is
refused. Exit 0 on ARCHIVED, NOTHING_TO_ARCHIVE or DRY_RUN, 1 on FAILED, 2 on REFUSED.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click

from omnimarket.nodes.node_lab_history_archive_effect.handlers.handler_lab_history_archive import (
    HandlerLabHistoryArchive,
)
from omnimarket.nodes.node_lab_history_archive_effect.models import (
    EnumLabHistoryArchiveVerdict,
    ModelLabHistoryArchivePartEvent,
    ModelLabHistoryArchiveRequest,
)

_EXIT = {
    EnumLabHistoryArchiveVerdict.ARCHIVED: 0,
    EnumLabHistoryArchiveVerdict.NOTHING_TO_ARCHIVE: 0,
    EnumLabHistoryArchiveVerdict.DRY_RUN: 0,
    EnumLabHistoryArchiveVerdict.FAILED: 1,
    EnumLabHistoryArchiveVerdict.REFUSED: 2,
}


class _PrintSink:
    def emit(self, event: ModelLabHistoryArchivePartEvent) -> None:
        click.echo(
            f"part={event.part} status={event.status.value} files={event.files} "
            f"bytes_staged={event.bytes_staged} deleted={event.files_deleted} "
            f"kept_changed={event.files_kept_changed} freed={event.bytes_freed}"
            + (f" detail={event.detail}" if event.detail else "")
        )


@click.group("lab-archive")
@click.pass_context
def lab_archive_group(ctx: click.Context) -> None:
    """Archive lab history older than seven days to Drive, then free the local copy."""
    ctx.ensure_object(dict)


@lab_archive_group.command("run")
@click.option(
    "--dry-run", is_flag=True, help="List the parts; stage and delete nothing."
)
@click.option(
    "--retention-days",
    type=click.IntRange(min=7),
    default=7,
    show_default=True,
    help="Files modified within this many days are never archived (floor 7).",
)
@click.option(
    "--overlay",
    "overlay_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="The deployment overlay; default is the node's overlay under the overlay roots.",
)
@click.option(
    "--as-of", type=click.DateTime(), default=None, help="UTC instant for tests."
)
def run_command(
    dry_run: bool,
    retention_days: int,
    overlay_path: Path | None,
    as_of: dt.datetime | None,
) -> None:
    """Run one archive pass on this host."""
    handler = HandlerLabHistoryArchive(sink=_PrintSink())
    result = handler.handle(
        ModelLabHistoryArchiveRequest(
            retention_days=retention_days,
            dry_run=dry_run,
            overlay_path=overlay_path,
            as_of=as_of.replace(tzinfo=dt.UTC) if as_of else None,
        )
    )
    click.echo(result.line())
    if result.detail:
        click.echo(result.detail, err=True)
    raise SystemExit(_EXIT[result.verdict])
