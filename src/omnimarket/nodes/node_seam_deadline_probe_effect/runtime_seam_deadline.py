# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_seam_deadline_probe_effect <verb>``.

``read``   demand every declared seam and append the row (what a schedule runs).
``probe``  the same demands, appending nothing.

``--fault`` injects a drill fault; its row carries ``drill=yes``. ``--seam`` limits the run.

Exit codes: 0 the run was read (and, for ``read``, its row landed); a seam reading NOT WORKING is
still 0, since a reading changes nothing and refuses nothing. 1 the row did not land or the command
was refused. 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
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
from omnimarket.nodes.node_seam_deadline_probe_effect.models.model_seam_deadline import (
    EnumSeamFault,
    ModelSeamDeadlineCommand,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    NODE_NAME,
    load_overlay,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.protocols import ProtocolSeamBus


def _run(arguments: argparse.Namespace, write: bool) -> int:
    overlay = load_overlay()
    host = os.environ.get(overlay.ledger_append.host_env, "").strip()
    if not host:
        raise ValueError(
            f"{overlay.ledger_append.host_env} is not set; the overlay names it as this host"
        )
    bus: ProtocolSeamBus = (
        KafkaSeamBus(overlay.bus.lane)
        if overlay.bus.backend == "kafka"
        else InMemorySeamBus()
    )
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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
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
