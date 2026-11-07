# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_seam_deadline_probe_effect <verb>``.

``read``   demand every declared seam and append the row (what a schedule runs).
``probe``  the same demands, appending nothing.
``schedule`` install | uninstall | status | run-now, for a host in the overlay's schedule_hosts.

``--fault`` injects a drill fault; its row carries ``drill=yes``. ``--seam`` limits the run.

Exit codes: 0 the run was read (and, for ``read``, its row landed); a seam reading NOT WORKING is
still 0, since a reading changes nothing and refuses nothing. 1 the row did not land or the command
was refused. 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.kafka_seam_bus import (
    KafkaSeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.handler_seam_deadline import (
    HandlerSeamDeadline,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.schedule import (
    SeamDeadlineScheduler,
    load_schedule,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamFault,
    ModelSeamDeadlineCommand,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.protocols import ProtocolSeamBus

_SEAM_BUSES: dict[str, Callable[[str], ProtocolSeamBus]] = {
    "inmemory": lambda _lane: InMemorySeamBus(),
    "kafka": KafkaSeamBus,
}


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    bus: ProtocolSeamBus = _SEAM_BUSES[overlay.bus.backend](overlay.bus.lane)
    handler = HandlerSeamDeadline(
        overlay=overlay,
        host=host,
        bus=bus,
        appender=build_appender(overlay.ledger_append, overlay.publish.lane)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelSeamDeadlineCommand(
            correlation_id=f"seam-deadline-{host}-{now}",
            seams=tuple(arguments.seam or ()),
            write=write,
            fault=EnumSeamFault(arguments.fault),
        )
    )
    for reading in result.seams:
        sys.stdout.write(
            f"SEAM seam={reading.seam} verdict={reading.verdict.value} "
            f"crossing={reading.crossing.value} elapsed_ms={reading.elapsed_ms} "
            f"deadline_s={reading.deadline_seconds} demand={reading.correlation_id}"
            + (f" detail={reading.detail}" if reading.detail else "")
            + "\n"
        )
    sys.stdout.write(
        f"RUN host={host} verdict={result.verdict.value} outcome={result.outcome.value} "
        f"status={result.status} fault={result.fault.value} ledger={result.ledger} "
        f"key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    if result.ledger.startswith("append-failed") or not result.seams:
        sys.stderr.write(f"SEAM-DEADLINE-FAILED {result.failure}\n")
        return 1
    return 0


def _schedule(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="schedule")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("install", "uninstall", "status", "run-now"):
        sub = verbs.add_parser(verb)
        sub.add_argument("--host", required=True, help="the host's name in the overlay")
        if verb == "install":
            sub.add_argument(
                "--workspace-root",
                dest="workspace_root",
                default=os.environ.get("OMNI_HOME", ""),
            )
            sub.add_argument("--dry-run", action="store_true")
        if verb == "run-now":
            sub.add_argument(
                "--drill",
                action="store_true",
                help="start the scheduled drill's service once instead of the hourly run",
            )
    args = parser.parse_args(list(arguments))
    scheduler = SeamDeadlineScheduler(
        schedule=load_schedule(), overlay=load_overlay(), home=Path.home()
    )
    if args.verb == "install":
        if not args.workspace_root:
            raise ValueError("OMNI_HOME is not set and --workspace-root was not passed")
        found = find_node_overlay(NODE_NAME)
        if found is None:
            raise NodeOverlayError(
                "the overlay file cannot be found to pin its root in the unit"
            )
        result = scheduler.install(
            args.host,
            omni_root=Path(args.workspace_root),
            overlay_root=found.parent.parent,
            dry_run=args.dry_run,
        )
    elif args.verb == "uninstall":
        result = scheduler.uninstall(args.host)
    elif args.verb == "status":
        result = scheduler.status(args.host)
    else:
        result = scheduler.run_now(args.host, drill=args.drill)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments and arguments[0] == "schedule":
            return _schedule(arguments[1:])
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("read", "probe"))
        parser.add_argument("--seam", action="append")
        parser.add_argument(
            "--fault",
            choices=[fault.value for fault in EnumSeamFault],
            default=EnumSeamFault.NONE.value,
        )
        args = parser.parse_args(arguments)
        return _run(args, write=args.verb == "read")
    except (
        NodeOverlayError,
        LedgerRouteError,
        ValidationError,
        ValueError,
        OSError,
    ) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
