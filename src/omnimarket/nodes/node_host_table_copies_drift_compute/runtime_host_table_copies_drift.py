# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_table_copies_drift_compute <verb>``.

``daily``  read every declared copy of the lab host table and append the reading row.
``probe``  the same read, appending nothing.

The copy paths come from the node's overlay; with no overlay the run is refused (exit 3). Output is one
``READING`` line, then one ``DIFF`` line per differing field naming the host, the field and the value in each copy.

Exit codes: 0 the read ran and its row was written (a reading of NOT WORKING is still 0: a reading
changes nothing and refuses nothing); 1 the read or its row failed (an unreadable copy, an append that
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
from omnimarket.nodes.node_host_table_copies_drift_compute.adapters.copy_source import (
    LocalCopySource,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.handlers.handler_host_table_copies_drift import (
    HandlerHostTableCopiesDrift,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.models import (
    ModelHostTableDriftCommand,
)
from omnimarket.nodes.node_host_table_copies_drift_compute.overlay import (
    NODE_NAME,
    ModelHostTableCopiesDriftOverlay,
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
    overlay: ModelHostTableCopiesDriftOverlay,
    environ: Mapping[str, str],
    write: bool,
) -> tuple[HandlerHostTableCopiesDrift, str]:
    host = _required(overlay.ledger_append.host_env, environ)
    handler = HandlerHostTableCopiesDrift(
        overlay=overlay,
        host=host,
        source=LocalCopySource(overlay.copies, environ),
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
        ModelHostTableDriftCommand(
            correlation_id=f"host-table-copies-drift-{host}-{now}", write=write
        )
    )
    if result.status == "failed":
        sys.stderr.write(f"HOST-TABLE-COPIES-DRIFT-FAILED {result.failure}\n")
        return 1
    sys.stdout.write(
        f"READING host={result.host} verdict={result.verdict.value} "
        f"outcome={result.outcome.value} copies={','.join(result.copies)} "
        f"hosts={result.hosts_compared} differing_hosts={result.differing_hosts} "
        f"differing_fields={len(result.differences)} "
        f"declared_fields={len(result.declared)} ledger={result.ledger} "
        f"key={result.row_key} reason={','.join(result.reasons)}\n"
    )
    for item in result.differences:
        shown = " ".join(f"{value.copy_name}={value.value}" for value in item.values)
        sys.stdout.write(f"DIFF host={item.host} field={item.field} {shown}\n")
    for item in result.declared:
        shown = " ".join(f"{value.copy_name}={value.value}" for value in item.values)
        sys.stdout.write(f"DECLARED host={item.host} field={item.field} {shown}\n")
    for name in result.stale_declarations:
        sys.stdout.write(f"STALE-DECLARATION {name}\n")
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
