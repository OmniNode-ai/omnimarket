# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_overlay_drift_compute <verb>``.

``daily``  run the join check over the overlay's consumers and append the reading row.
``probe``  the same read, appending nothing.

The consumers, paths, units and park list scans come from the node's overlay; with no overlay the run is
refused (exit 3). Output is one ``READING`` line, then one ``MISMATCH`` line per consumer whose value
differs from the overlay's and one ``PARK-LIST`` line per park list found outside an overlay.

Exit codes: 0 the read ran and its row was written (a reading of NOT WORKING is still 0: a reading
changes nothing and refuses nothing); 1 the read or its row failed (an unreadable source, an append that
did not land); 3 a deployment fact is missing (INDETERMINATE).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_overlay_drift_compute.adapters.file_source import (
    LocalFileSource,
)
from omnimarket.nodes.node_overlay_drift_compute.handlers.handler_overlay_drift_compute import (
    HandlerOverlayDriftCompute,
)
from omnimarket.nodes.node_overlay_drift_compute.models import (
    ModelOverlayJoinCommand,
)
from omnimarket.nodes.node_overlay_drift_compute.overlay import (
    NODE_NAME,
    ModelOverlayDriftComputeOverlay,
    load_overlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_appender,
)


def _required(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is not set; the {NODE_NAME} overlay names it")
    return value


def build_handler(
    overlay: ModelOverlayDriftComputeOverlay,
    environ: Mapping[str, str],
    write: bool,
) -> tuple[HandlerOverlayDriftCompute, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    handler = HandlerOverlayDriftCompute(
        overlay=overlay,
        host=host,
        files=LocalFileSource(environ),
        environ=environ,
        appender=build_appender(overlay.ledger_append, overlay.publish.lane, environ)
        if write
        else None,
        clock=lambda: datetime.now(UTC),
    )
    return handler, host


def _run(write: bool) -> int:
    overlay = load_overlay()
    handler, host = build_handler(overlay, os.environ, write)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = handler.handle(
        ModelOverlayJoinCommand(
            correlation_id=f"overlay-consumer-join-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"OVERLAY-CONSUMER-JOIN-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} consumers={result.consumers_checked} "
        f"mismatched={len(result.mismatches)} park_lists={len(result.park_lists)} "
        f"unreadable={len(result.unreadable)} ledger={result.ledger} "
        f"key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    for item in result.mismatches:
        sys.stdout.write(
            f"MISMATCH consumer={item.consumer} overlay={item.overlay_value} "
            f"consumer_value={item.consumer_value} via={item.consumer_via} "
            f"overlay_{overlay.base_unit}={item.overlay_normalized} "
            f"consumer_{overlay.base_unit}={item.consumer_normalized}\n"
        )
    for found in result.park_lists:
        sys.stdout.write(
            f"PARK-LIST scan={found.scan} line={found.line} text={found.text}\n"
        )
    for name in result.unreadable:
        sys.stdout.write(f"UNREADABLE {name}\n")
    return 1 if result.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
        parser.add_argument("verb", choices=("daily", "probe"))
        args = parser.parse_args(arguments)
        return _run(write=args.verb == "daily")
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
