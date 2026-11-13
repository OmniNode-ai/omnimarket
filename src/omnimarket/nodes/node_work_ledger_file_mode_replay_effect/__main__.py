# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_file_mode_replay_effect`` (OMN-19998).

The command form of ``replay_from_file`` and ``drain``: re-emit named rows (``--row-id``,
repeatable, ``--row-ids-file`` or ``--failure-log``) or a stamp window (``--since`` and ``--until``) from the
ledger file of record through the work-ledger emit path, and print the typed result as JSON.
Every setting comes from the node overlay. Exit 0 when every selected row was published or
was already replayed, 1 when a row is pending, refused or absent from the file, 78 when the
overlay is absent or invalid.
"""

from __future__ import annotations

import contextlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.ledger_file_rows import (
    failure_log_row_ids,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    load_replay_overlay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_wiring import (
    bound_bus_lane,
    build_replay_handler,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
)

EXIT_OVERLAY_INVALID = 78


def _stamp(_: click.Context, __: click.Parameter, value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@click.command("replay-from-file")
@click.option("--row-id", "row_ids", multiple=True, help="A row id to re-emit.")
@click.option(
    "--row-ids-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="A file with one row id per line.",
)
@click.option(
    "--failure-log",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="The dual write's failure log: every row id it names is re-emitted if the ledger file holds it.",
)
@click.option("--since", callback=_stamp, help="UTC YYYY-MM-DDTHH:MM:SSZ, inclusive.")
@click.option("--until", callback=_stamp, help="UTC YYYY-MM-DDTHH:MM:SSZ, inclusive.")
@click.option(
    "--bus-lane",
    help="Publish through this bus lane's declared transport; without it the emit path uses the environment's broker settings.",
)
def main(
    row_ids: tuple[str, ...],
    row_ids_file: Path | None,
    failure_log: Path | None,
    since: datetime | None,
    until: datetime | None,
    bus_lane: str | None,
) -> None:
    ids = list(row_ids)
    if row_ids_file is not None:
        ids.extend(
            line.strip()
            for line in row_ids_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    if failure_log is not None:
        ids.extend(failure_log_row_ids(failure_log))
    registry_root = os.environ.get("OMNI_HOME")
    try:
        stack = contextlib.ExitStack()
        emitter = (
            stack.enter_context(
                bound_bus_lane(
                    bus_lane,
                    registry_root=Path(registry_root) if registry_root else None,
                )
            )
            if bus_lane
            else None
        )
        handler = build_replay_handler(load_replay_overlay(), emitter=emitter)
        request = ModelFileModeReplayRequest(
            operation=EnumReplayOperation.REPLAY_FROM_FILE,
            row_ids=ids,
            window_since=since,
            window_until=until,
        )
    except (NodeOverlayError, ValueError) as exc:
        click.echo(f"replay-from-file: {exc}", err=True)
        sys.exit(EXIT_OVERLAY_INVALID)
    with stack:
        result = handler.handle(request)
    click.echo(result.model_dump_json(indent=2))
    sys.exit(1 if (result.still_pending or result.refused or result.not_in_file) else 0)


if __name__ == "__main__":
    main()
